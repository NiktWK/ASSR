import json
from types import SimpleNamespace
import pytest
from assr.agents import Agents
from assr.models import ReviewDecision


def test_structured_output_repair_and_usage(monkeypatch, tmp_path):
    replies = [
        {"bad": "shape"},
        {"verdict": "accept", "target_digest": "abc", "checks": ["ok"], "evidence_refs": ["run-1"]},
    ]
    calls = []

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kw):
            calls.append(kw)
            body = {
                "choices": [{"message": {"content": json.dumps(replies.pop(0))}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            }
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    monkeypatch.setattr("assr.agents.httpx.Client", Client)
    agent = Agents(
        "live", tmp_path, "https://provider.example/v1", "secret", "model", input_rate=1, output_rate=2
    )
    result, usage = agent.invoke("k2_code", {"target_digest": "abc"}, ReviewDecision)
    assert result.verdict == "accept"
    assert len(calls) == 2
    assert usage["input_tokens"] == 20
    assert usage["cost_usd"] == pytest.approx(0.0001)
    agent.invoke("k2_code", {"target_digest": "abc"}, ReviewDecision)
    assert len(calls) == 2  # cached completed side effect
    assert "secret" not in next(tmp_path.glob("*.json")).read_text()
