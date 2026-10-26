"""Durable runner protocol; SSH transports JSON, never model-produced shell commands."""

import json
import math
import re
import subprocess
import sys
from pathlib import Path

from .evaluator import evaluate
from .models import RunSpec, digest, now
from .workspace import write_json


class RunnerUnavailable(Exception):
    pass


def validate_data(profile):
    directory = Path(profile["data_dir"])
    entries = list(directory.iterdir())
    if {x.name for x in entries} != {"train.json", "validation.json"} or any(
        x.is_symlink() or not x.is_file() for x in entries
    ):
        raise ValueError("data mount must contain only regular train.json and validation.json files")
    data = {x.name: json.loads(x.read_text()) for x in entries}
    if digest(data) != profile["dataset_digest"]:
        raise ValueError("dataset digest mismatch")
    train, val = data["train.json"], data["validation.json"]
    a, b = [r["id"] for r in train], [r["id"] for r in val]
    if len(a) != len(set(a)) or len(b) != len(set(b)) or set(a) & set(b):
        raise ValueError("duplicate or overlapping data split ids")
    if any("label" in r for r in val):
        raise ValueError("evaluation labels leaked into validation features")
    if not train or not val:
        raise ValueError("empty dataset split")
    dimensions = set()
    for rows, keys in ((train, {"id", "x", "label"}), (val, {"id", "x"})):
        for row in rows:
            if set(row) != keys or not isinstance(row["id"], str) or not isinstance(row["x"], list):
                raise ValueError("unsupported dataset row schema")
            dimensions.add(len(row["x"]))
            if not row["x"] or any(
                isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                for x in row["x"]
            ):
                raise ValueError("features must be finite numeric vectors")
    if len(dimensions) != 1 or any(type(r["label"]) is not int or r["label"] not in (0, 1) for r in train):
        raise ValueError("feature dimension or binary label mismatch")
    from . import evaluator

    labels = json.loads(Path(profile["labels_path"]).read_text())
    if (
        digest({"source": Path(evaluator.__file__).read_text(), "labels": labels})
        != profile["evaluator_digest"]
    ):
        raise ValueError("evaluator digest mismatch")
    return labels


class DemoRunner:
    """Runs ONLY the two shipped fixture programs. This is not a sandbox for arbitrary code."""

    def __init__(self, root, profile):
        self.root, self.profile = Path(root), profile

    def submit(self, spec):
        spec = RunSpec.model_validate(spec)
        path = self.root / spec.idempotency_key / "record.json"
        if path.exists():
            saved = json.loads(path.read_text())
            if saved["spec_digest"] != digest(spec.model_dump()):
                raise ValueError("idempotency conflict")
            return saved
        from . import demo_train

        baseline = Path(demo_train.__file__).read_text()
        if spec.files not in (
            {"train.py": baseline},
            {"train.py": baseline.replace("STEPS = 4", "STEPS = 40")},
        ):
            raise ValueError("demo runner rejects non-fixture code; use Docker runner for live code")
        labels = validate_data(self.profile)
        work = path.parent
        work.mkdir(parents=True, exist_ok=True)
        source = work / "train.py"
        source.write_text(spec.files["train.py"])
        config = work / "config.json"
        write_json(config, spec.config)
        record = {
            "job_id": spec.idempotency_key,
            "spec_digest": digest(spec.model_dump()),
            "status": "RUNNING",
            "started_at": now(),
            "mode": "demo",
            "spec": spec.model_dump(),
        }
        write_json(path, record)
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(source),
                    "--data",
                    self.profile["data_dir"],
                    "--config",
                    str(config),
                    "--out",
                    str(work / "output"),
                ],
                capture_output=True,
                text=True,
                timeout=spec.walltime_seconds,
            )
            record.update(exit_code=proc.returncode, logs=proc.stdout[-20000:] + proc.stderr[-20000:])
            if proc.returncode:
                record.update(status="FAILED", error_class="CODE_ERROR")
            else:
                predictions = json.loads((work / "output" / "predictions.json").read_text())
                record.update(
                    status="SUCCEEDED",
                    metrics=evaluate(predictions, labels),
                    predictions=predictions,
                    artifact_manifest={"predictions.json": digest(predictions)},
                )
        except subprocess.TimeoutExpired:
            record.update(status="FAILED", error_class="TIMEOUT")
        except (ValueError, OSError) as exc:
            record.update(status="FAILED", error_class="INVALID_ARTIFACT", logs=str(exc))
        record["ended_at"] = now()
        write_json(path, record)
        return record

    def status(self, spec):
        p = self.root / spec["idempotency_key"] / "record.json"
        if not p.exists():
            return self.submit(spec)
        data = json.loads(p.read_text())
        if data["status"] == "RUNNING":
            # The synchronous fixture process was interrupted. Do not silently rerun.
            data.update(status="FAILED", error_class="INFRA_INTERRUPTED")
            write_json(p, data)
        return data

    def cancel(self, spec):
        path = self.root / spec["idempotency_key"] / "record.json"
        if path.exists():
            record = json.loads(path.read_text())
            if record["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                return record
        record = {
            "status": "CANCELLED",
            "job_id": spec["idempotency_key"],
            "spec_digest": digest(spec),
            "spec": spec,
            "ended_at": now(),
        }
        write_json(path, record)
        return record


class SSHRunner:
    def __init__(self, host, profile):
        if not re.fullmatch(r"[a-zA-Z0-9_.@-]+", host) or host.startswith("-"):
            raise ValueError("invalid SSH host")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", profile):
            raise ValueError("invalid profile")
        self.host, self.profile = host, profile

    def rpc(self, op, spec):
        try:
            p = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    "ConnectTimeout=10",
                    self.host,
                    "python3 -m assr.runner_rpc",
                ],
                input=json.dumps({"op": op, "profile": self.profile, "spec": spec}),
                text=True,
                capture_output=True,
                timeout=45,
            )
            if p.returncode:
                raise RunnerUnavailable("SSH/runner failure; remote job state unknown")
            result = json.loads(p.stdout)
            if "error" in result:
                raise ValueError(result["error"])
            return result
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
            raise RunnerUnavailable("SSH unavailable; do not resubmit with a new key") from exc

    def submit(self, spec):
        return self.rpc("submit", spec)

    def status(self, spec):
        return self.rpc("status", spec)

    def cancel(self, spec):
        return self.rpc("cancel", spec)
