from types import SimpleNamespace
from assr.tracking import Tracking


def test_tracking_reuses_stable_id(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, *a, **kw):
            return SimpleNamespace(status_code=200, json=lambda: {"experiment": {"experiment_id": "e"}})

        def post(self, endpoint, json):
            calls.append(endpoint)
            value = {"runs": [{"info": {"run_id": "existing"}}]} if endpoint == "runs/search" else {}
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)

    monkeypatch.setattr("assr.tracking.httpx.Client", Client)
    spec = {
        k: "x"
        for k in (
            "commit_sha",
            "source_digest",
            "config_digest",
            "dataset_digest",
            "evaluator_digest",
            "image_digest",
            "variant",
            "seed",
            "run_id",
        )
    }
    run = {"spec": spec, "result": {"metrics": {"accuracy": 0.75}}}
    assert Tracking("http://tracking").log({"id": "c", "project": {"mode": "demo"}}, run) == "existing"
    assert "runs/create" not in calls
    assert "runs/log-batch" in calls
