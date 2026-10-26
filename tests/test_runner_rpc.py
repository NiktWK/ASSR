"""Docker contract tests with a fake engine, plus opt-in real-container tests elsewhere."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from assr.engine import State
from assr.models import digest
from assr.runner_rpc import DockerRunner, sandbox_args
from test_workflow import advance, create


def fixture(engine, monkeypatch):
    d = create(engine)
    d = advance(engine, d["id"], State.RUNNING)
    profile = deepcopy(d["profile"])
    profile.update(
        output_quota_verified=True,
        image_digest="python@sha256:" + "a" * 64,
        evaluator_image="assr@sha256:" + "b" * 64,
    )
    spec = deepcopy(d["runs"][0]["spec"])
    spec["image_digest"] = profile["image_digest"]
    monkeypatch.setattr("assr.runner_rpc.os.getuid", lambda: 1000)
    return profile, spec


def test_docker_cancel_before_submit_is_durable(engine, monkeypatch, tmp_path):
    profile, spec = fixture(engine, monkeypatch)
    runner = DockerRunner(tmp_path / "jobs", profile)

    def no_docker(*a, **kw):
        pytest.fail("cancelled task must never reach Docker")

    monkeypatch.setattr("assr.runner_rpc.docker", no_docker)
    assert runner.operate("cancel", spec)["status"] == "CANCELLED"
    assert runner.operate("submit", spec)["status"] == "CANCELLED"


def test_docker_duplicate_submission_and_restart(engine, monkeypatch, tmp_path):
    profile, spec = fixture(engine, monkeypatch)
    containers = {}
    creates = []

    def fake(*args, check=True):
        if args[0] == "inspect":
            state = containers.get(args[1])
            return SimpleNamespace(
                returncode=0 if state else 1,
                stdout=json.dumps([{"State": state}]) if state else "",
                stderr="",
            )
        if args[0] == "create":
            name = args[args.index("--name") + 1]
            creates.append(name)
            containers[name] = {"Status": "created", "Running": False}
        if args[0] == "start":
            containers[args[1]] = {"Status": "running", "Running": True}
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("assr.runner_rpc.docker", fake)
    root = tmp_path / "jobs"
    first = DockerRunner(root, profile).operate("submit", spec)
    second = DockerRunner(root, profile).operate("submit", spec)
    assert first["job_id"] == second["job_id"]
    assert len(creates) == 1


def test_sandbox_no_network_privileges_or_secrets(engine, monkeypatch, tmp_path):
    profile, spec = fixture(engine, monkeypatch)
    args = sandbox_args("job", profile, tmp_path)
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert not any("labels" in arg or "docker.sock" in arg for arg in args)


@pytest.mark.parametrize(
    "code,oom,expected", [(1, False, "CODE_ERROR"), (124, False, "TIMEOUT"), (137, True, "OOM")]
)
def test_exit_classification(engine, monkeypatch, tmp_path, code, oom, expected):
    profile, spec = fixture(engine, monkeypatch)
    job = tmp_path / "jobs" / spec["idempotency_key"]
    job.mkdir(parents=True)
    (job / "record.json").write_text(
        json.dumps({"spec_digest": digest(spec), "spec": spec, "status": "RUNNING", "deadline": 99999999999})
    )

    def fake(*args, check=True):
        state = {"Status": "exited", "Running": False, "ExitCode": code, "OOMKilled": oom}
        return SimpleNamespace(
            returncode=0, stdout=json.dumps([{"State": state}]) if args[0] == "inspect" else "", stderr=""
        )

    monkeypatch.setattr("assr.runner_rpc.docker", fake)
    result = DockerRunner(tmp_path / "jobs", profile).operate("status", spec)
    assert result["status"] == "FAILED"
    assert result["error_class"] == expected
