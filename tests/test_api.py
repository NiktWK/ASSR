from fastapi.testclient import TestClient
from assr.api import create_app


def test_api_auth_idempotency_and_conflict(engine):
    engine.settings.token = "testing-token"
    with TestClient(create_app(engine.settings, engine)) as client:
        assert client.get("/campaigns").status_code == 401
        headers = {"Authorization": "Bearer testing-token", "Idempotency-Key": "create-one"}
        assert client.get("/", headers=headers).status_code == 200
        payload = {"name": "API campaign", "goal": "Measure training changes"}
        first = client.post("/campaigns", json=payload, headers=headers)
        assert first.status_code == 200
        assert client.post("/campaigns", json=payload, headers=headers).json() == first.json()
        cid = first.json()["id"]
        assert client.get("/campaigns/" + cid + "/events", headers=headers).json()
        assert client.get("/campaigns/" + cid + "/report", headers=headers).status_code == 200
        stale = client.post(
            "/campaigns/" + cid + "/decisions", json={"revision": 0, "action": "cancel"}, headers=headers
        )
        assert stale.status_code == 409
        cross = client.post("/campaigns", json=payload, headers={**headers, "Origin": "https://evil.example"})
        assert cross.status_code == 403
        assert client.get("/runs/nonexistent", headers=headers).status_code == 404


def test_requires_idempotency_header(engine):
    with TestClient(create_app(engine.settings, engine)) as client:
        assert client.post("/campaigns", json={"name": "x", "goal": "Long enough goal"}).status_code == 422
