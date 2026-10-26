import json
from pathlib import Path

import pytest
from assr.evaluator import evaluate
from assr.models import digest
from assr.runner import SSHRunner, validate_data
from assr.workspace import safe_files


@pytest.mark.parametrize(
    "path", ["../train.py", "/tmp/train.py", ".git/config", "a/../train.py", "evaluator.py"]
)
def test_disallowed_code_paths(path):
    with pytest.raises(ValueError):
        safe_files({path: "pass"}, ["train.py"])


@pytest.mark.parametrize("prediction", [float("nan"), float("inf"), True, "1", 2])
def test_invalid_predictions(prediction):
    with pytest.raises(ValueError):
        evaluate([{"id": "a", "prediction": prediction}], [{"id": "a", "label": 1}])


def test_predictions_ids():
    with pytest.raises(ValueError):
        evaluate([{"id": "wrong", "prediction": 1}], [{"id": "a", "label": 1}])


def test_data_digest_and_leakage(engine):
    p = json.loads((engine.settings.root / "profiles" / "toy.json").read_text())
    validate_data(p)
    file = Path(p["data_dir"]) / "validation.json"
    data = json.loads(file.read_text())
    data[0]["label"] = 1
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="digest"):
        validate_data(p)
    all_data = {f.name: json.loads(f.read_text()) for f in Path(p["data_dir"]).glob("*.json")}
    p["dataset_digest"] = digest(all_data)
    with pytest.raises(ValueError, match="leaked"):
        validate_data(p)


@pytest.mark.parametrize("host", ["-oProxyCommand=sh", "host; rm", "host\nsh"])
def test_ssh_rejects_shell_injection(host):
    with pytest.raises(ValueError):
        SSHRunner(host, "toy")


def test_data_mount_rejects_extra_secret_file(engine):
    p = json.loads((engine.settings.root / "profiles" / "toy.json").read_text())
    (Path(p["data_dir"]) / "secret.txt").write_text("not-for-training")
    with pytest.raises(ValueError, match="data mount"):
        validate_data(p)
