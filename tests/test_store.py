import pytest
from assr.store import Store
from test_workflow import create


def test_snapshot_and_events_are_atomic(engine):
    d = create(engine)
    with pytest.raises(RuntimeError):
        with engine.store.transaction() as c:
            x = engine.store.get(d["id"], c)
            engine.store.event(c, x, "must.rollback", {"a": 1})
            x["state"] = "FAILED"
            engine.store.save(c, x, x["revision"])
            raise RuntimeError("crash before commit")
    reopened = Store(engine.settings.database_url)
    assert reopened.get(d["id"])["state"] == "DRAFT"
    assert not any(e["type"] == "must.rollback" for e in reopened.history(d["id"]))
