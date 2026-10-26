"""Install on a dedicated GPU host; run as a non-root service user with rootless Docker.
A profile is administrator-owned, not supplied by an agent. All jobs are serialized by flock.
"""

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .models import RunSpec, digest, now
from .runner import validate_data
from .workspace import load_profile, safe_files, write_json


def docker(*args, check=True):
    p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=25)
    if check and p.returncode:
        raise RuntimeError("Docker operation failed: " + p.stderr[-1000:])
    return p


def container_name(spec):
    return "assr-" + spec.idempotency_key


def sandbox_args(name, profile, job, evaluator=False):
    uid = f"{os.getuid()}:{os.getgid()}"
    args = [
        "create",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--user",
        uid,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "128",
        "--memory",
        profile.get("memory", "2g"),
        "--cpus",
        str(profile.get("cpus", 2)),
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=1",
    ]
    if evaluator:
        args += [
            "-v",
            f"{job}/output:/output:ro",
            "-v",
            f"{job}/evaluation:/evaluation:rw",
            "-v",
            f"{Path(profile['labels_path']).resolve()}:/labels.json:ro",
        ]
    else:
        args += [
            "-v",
            f"{job}/source:/work:ro",
            "-v",
            f"{job}/output:/output:rw",
            "-v",
            f"{job}/config:/config:ro",
            "-v",
            f"{Path(profile['data_dir']).resolve()}:/data:ro",
            "-w",
            "/work",
        ]
    return args


class DockerRunner:
    def __init__(self, root, profile):
        self.root, self.profile = Path(root).resolve(), profile
        if os.getuid() == 0:
            raise ValueError("runner must use a non-root service account")
        if not profile.get("output_quota_verified"):
            raise ValueError("administrator must configure filesystem output quota before live execution")
        for field in ("image_digest", "evaluator_image"):
            if not re.fullmatch(r"[a-zA-Z0-9_./:-]+@sha256:[a-f0-9]{64}", profile[field]):
                raise ValueError("live images must be pinned by sha256 digest")

    def operate(self, op, raw):
        spec = RunSpec.model_validate(raw)
        profile = self.profile
        safe_files(spec.files, profile["allowed_files"])
        for field in ("image_digest", "dataset_digest", "evaluator_digest"):
            if getattr(spec, field) != profile[field]:
                raise ValueError(f"{field} does not match administrator profile")
        expected_argv = profile["smoke_argv"] if spec.variant == "smoke" else profile["argv"]
        if spec.argv != expected_argv or spec.walltime_seconds > profile.get("max_walltime", 3600):
            raise ValueError("command or walltime exceeds profile policy")
        if spec.gpu_count > profile.get("max_gpus", 1):
            raise ValueError("GPU allocation exceeds profile")
        job = self.root / spec.idempotency_key
        name = container_name(spec)
        p = job / "record.json"
        if p.exists():
            record = json.loads(p.read_text())
            if record["spec_digest"] != digest(raw):
                raise ValueError("idempotency key conflict")
            if record["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                return record
        else:
            if op == "cancel":
                record = {
                    "status": "CANCELLED",
                    "job_id": spec.idempotency_key,
                    "spec_digest": digest(raw),
                    "spec": raw,
                    "ended_at": now(),
                }
                write_json(p, record)
                return record
            validate_data(profile)
            job.mkdir(parents=True, exist_ok=True)
            for folder in ("source", "output", "config", "evaluation"):
                (job / folder).mkdir(exist_ok=True)
            for file, text in spec.files.items():
                target = job / "source" / file
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            write_json(job / "config" / "config.json", spec.config)
            record = {
                "job_id": spec.idempotency_key,
                "spec": raw,
                "spec_digest": digest(raw),
                "status": "SUBMITTING",
                "started_at": now(),
                "deadline": time.time() + spec.walltime_seconds + 10,
            }
            write_json(p, record)  # durable intent BEFORE Docker side effect
        info = docker("inspect", name, check=False)
        if op == "cancel":
            for target in (name, name + "-eval"):
                found = docker("inspect", target, check=False)
                if found.returncode == 0:
                    docker("stop", "--time", "5", target)
            record.update(status="CANCELLED", ended_at=now())
            write_json(p, record)
            return record
        if info.returncode:
            # Only SUBMITTING permits creation. Lost history must not duplicate a job.
            if record["status"] != "SUBMITTING":
                record.update(status="FAILED", error_class="INFRA_LOST_CONTAINER")
                write_json(p, record)
                return record
            args = sandbox_args(name, profile, job)
            if spec.gpu_count:
                args += ["--gpus", str(spec.gpu_count)]
            args += [
                spec.image_digest,
                "timeout",
                "--signal=TERM",
                "--kill-after=5",
                str(spec.walltime_seconds),
                *spec.argv,
            ]
            docker(*args)
            info = docker("inspect", name)
        state = json.loads(info.stdout)[0]["State"]
        if state["Status"] == "created":
            docker("start", name)
            record["status"] = "RUNNING"
        elif state["Running"]:
            if time.time() > record["deadline"]:
                docker("kill", name)
                record.update(status="FAILED", error_class="TIMEOUT")
            else:
                record["status"] = "RUNNING"
        elif state["Status"] in ("exited", "dead"):
            code = state["ExitCode"]
            record["exit_code"] = code
            record["logs"] = docker("logs", "--tail", "100", name, check=False).stdout[-20000:]
            if code:
                kind = (
                    "OOM" if state.get("OOMKilled") else ("TIMEOUT" if code in (124, 137) else "CODE_ERROR")
                )
                record.update(status="FAILED", error_class=kind)
            else:
                self.collect(spec, job, name, record)
        if record["status"] in ("FAILED", "SUCCEEDED"):
            record["ended_at"] = now()
        write_json(p, record)
        return record

    def collect(self, spec, job, name, record):
        output = job / "output" / "predictions.json"
        if not output.is_file() or output.is_symlink() or output.stat().st_size > 10_000_000:
            record.update(status="FAILED", error_class="INVALID_ARTIFACT")
            return
        ename = name + "-eval"
        existing = docker("inspect", ename, check=False)
        if existing.returncode:
            args = sandbox_args(ename, self.profile, job, evaluator=True)
            args += [
                self.profile["evaluator_image"],
                "timeout",
                "--kill-after=5",
                "30",
                "python",
                "-m",
                "assr.evaluator",
                "--predictions",
                "/output/predictions.json",
                "--labels",
                "/labels.json",
                "--out",
                "/evaluation/metrics.json",
            ]
            docker(*args)
            existing = docker("inspect", ename)
        state = json.loads(existing.stdout)[0]["State"]
        if state["Status"] == "created":
            docker("start", ename)
            record["status"] = "EVALUATING"
            return
        if state["Running"]:
            record["status"] = "EVALUATING"
            return
        if state["ExitCode"] or not (job / "evaluation" / "metrics.json").exists():
            record.update(status="FAILED", error_class="INVALID_ARTIFACT")
            return
        predictions = json.loads(output.read_text())
        metrics = json.loads((job / "evaluation" / "metrics.json").read_text())
        record.update(
            status="SUCCEEDED",
            metrics=metrics,
            artifact_manifest={"predictions.json": digest(predictions), "metrics.json": digest(metrics)},
            artifacts_uri=str(job),
        )


def main():
    root = Path(os.environ.get("ASSR_RUNNER_ROOT", "~/.assr-runner")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    try:
        request = json.loads(sys.stdin.read(1_000_001))
        if request["op"] not in ("submit", "status", "cancel") or not re.fullmatch(
            r"[a-zA-Z0-9_-]+", request["profile"]
        ):
            raise ValueError("invalid RPC")
        profile = load_profile(root / "profiles", request["profile"])
        with (root / "runner.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = DockerRunner(root / "jobs", profile).operate(request["op"], request["spec"])
        print(json.dumps(result, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))


if __name__ == "__main__":
    main()
