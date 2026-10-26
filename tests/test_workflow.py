from copy import deepcopy

import pytest

from assr.engine import Engine
from assr.models import DecisionInput, ProjectSpec, State, digest, uid
from assr.runner import RunnerUnavailable
from assr.store import Conflict


def create(engine):
    return engine.create(ProjectSpec(name="Test campaign", goal="Measure optimization change"), uid())


def advance(engine, cid, target, limit=100):
    for _ in range(limit):
        d = engine.store.get(cid)
        if d["state"] == target:
            return d
        if d["state"] == "AWAITING_APPROVAL":
            engine.decision(cid, DecisionInput(revision=d["revision"], action="approve"), uid())
        elif d["state"] in ("PAUSED", "FAILED", "BUDGET_EXHAUSTED", "CANCELLED"):
            pytest.fail(str(d.get("last_error", d["state"])))
        else:
            engine.tick(cid)
    pytest.fail(f"Never reached {target}")


def test_end_to_end_measured_baseline_and_treatment(engine):
    d = create(engine)
    d = advance(engine, d["id"], State.COMPLETED)
    assert len(d["runs"]) == 8
    assert all(r["status"] == "SUCCEEDED" for r in d["runs"])
    full = [r for r in d["runs"] if r["phase"] == "full"]
    assert len(full) == 6
    assert len({r["spec"]["seed"] for r in full}) == 3
    assert len({r["result"]["metrics"]["accuracy"] for r in full}) > 1
    assert d["conclusion"]["verdict"] == d["summary"]["verdict"]
    assert len(d["reviews"]) == 4
    assert d["gpu_reserved_seconds"] == 0
    assert engine.store.history(d["id"])[-1]["payload"]["to"] == "COMPLETED"


def test_initial_human_gate_persists_on_restart(engine):
    d = create(engine)
    d = advance(engine, d["id"], State.AWAITING_APPROVAL)
    second = Engine(engine.settings)
    assert second.tick(d["id"])["state"] == "AWAITING_APPROVAL"
    assert not second.store.get(d["id"])["runs"]


def test_running_resume_and_repeat_submit(engine):
    d = create(engine)
    d = advance(engine, d["id"], State.RUNNING)
    spec = d["runs"][-1]["spec"]
    runner = engine.runner(d)
    first = runner.submit(spec)  # simulate remote acceptance BEFORE local completion
    second = Engine(engine.settings)
    after = second.tick(d["id"])
    assert after["runs"][-1]["result"] == first
    assert runner.submit(spec) == first
    assert len(list((engine.settings.root / "demo-jobs").glob("*/record.json"))) == 1


def test_idempotent_create_and_decisions(engine):
    p = ProjectSpec(name="Test", goal="Measure code changes")
    first = engine.create(p, "same-key")
    assert engine.create(p, "same-key") == first
    with pytest.raises(Conflict):
        engine.create(p.model_copy(update={"name": "Other"}), "same-key")
    d = advance(engine, first["id"], State.AWAITING_APPROVAL)
    request = DecisionInput(revision=d["revision"], action="approve")
    result = engine.decision(d["id"], request, "approve-key")
    assert engine.decision(d["id"], request, "approve-key") == result
    with pytest.raises(Conflict):
        engine.decision(d["id"], request, "different-key")


def test_single_campaign(engine):
    create(engine)
    with pytest.raises(Conflict):
        create(engine)


def test_code_digest_change_stops_before_submit(engine):
    d = create(engine)
    d = advance(engine, d["id"], State.QUEUED)
    with engine.store.transaction() as c:
        data = engine.store.get(d["id"], c)
        data["implementation"]["files"]["train.py"] += "\n# changed\n"
        engine.store.save(c, data, data["revision"])
    result = engine.tick(d["id"])
    assert result["state"] == "PAUSED"
    assert not result["runs"]


def test_run_provenance_and_metrics_fail_closed(engine):
    d = create(engine)
    d = advance(engine, d["id"], State.COMPLETED)
    original = d["runs"][0]
    for mutate in [
        lambda r: r["result"].update(spec_digest="bad"),
        lambda r: r["result"].update(artifact_manifest={}),
        lambda r: r["result"]["metrics"].update(accuracy=float("nan")),
    ]:
        run = deepcopy(original)
        mutate(run)
        with pytest.raises(ValueError):
            engine.validate_run(run)


def test_unavailable_runner_retains_same_identity(engine):
    d = create(engine)
    d = advance(engine, d["id"], State.RUNNING)

    class Down:
        def submit(self, spec):
            raise RunnerUnavailable()

        def status(self, spec):
            raise RunnerUnavailable()

        def cancel(self, spec):
            raise RunnerUnavailable()

    engine.runner_factory = lambda d: Down()
    first = engine.tick(d["id"])
    second = engine.tick(d["id"])
    assert first["active_run"] == second["active_run"]
    assert second["state"] == "RUNNING"
    cancel = engine.decision(d["id"], DecisionInput(revision=second["revision"], action="cancel"), uid())
    assert engine.tick(d["id"])["state"] == "CANCEL_PENDING"


def test_pause_resume_does_not_discard_run(engine):
    d = create(engine)
    d = advance(engine, d["id"], State.RUNNING)
    paused = engine.decision(d["id"], DecisionInput(revision=d["revision"], action="pause"), uid())
    assert engine.tick(d["id"])["state"] == "PAUSED"
    resumed = engine.decision(d["id"], DecisionInput(revision=paused["revision"], action="resume"), uid())
    assert resumed["active_run"] == d["active_run"]
    assert resumed["state"] == "RUNNING"


def test_baseline_tolerance_blocks_treatment(engine):
    p = ProjectSpec(
        name="Baseline mismatch", goal="Check mismatch handling", baseline_expected=0, baseline_tolerance=0
    )
    d = engine.create(p, uid())
    for _ in range(70):
        d = engine.tick(d["id"])
        if d["state"] == "AWAITING_APPROVAL":
            d = engine.decision(d["id"], DecisionInput(revision=d["revision"], action="approve"), uid())
        if d["state"] == "PAUSED":
            break
    assert "baseline" in d["last_error"]
    assert not [r for r in d["runs"] if r["phase"] == "full" and r["variant"] == "treatment"]


def test_gpu_budget_before_submission(engine):
    d = create(engine)
    d = advance(engine, d["id"], State.QUEUED)
    # Exercise reservation code without calling live providers or GPU.
    with engine.store.transaction() as c:
        x = engine.store.get(d["id"], c)
        x["project"]["mode"] = "live"
        x["project"]["budget"]["gpu_seconds"] = 1
        engine.store.save(c, x, x["revision"])
    result = engine.tick(d["id"])
    assert result["state"] == "BUDGET_EXHAUSTED"
    assert not result["runs"]


def test_paused_inflight_decision_wins(engine):
    d = create(engine)
    d = advance(engine, d["id"], State.RUNNING)
    original = engine.runner(d)

    class Pausing:
        def submit(self, spec):
            latest = engine.store.get(d["id"])
            engine.decision(d["id"], DecisionInput(revision=latest["revision"], action="pause"), uid())
            return original.submit(spec)

    engine.runner_factory = lambda d: Pausing()
    assert engine.tick(d["id"])["state"] == "PAUSED"
    assert engine.store.get(d["id"])["lease"] is None


def test_llm_reservation_survives_provider_failure(engine):
    d = create(engine)
    with engine.store.transaction() as c:
        x = engine.store.get(d["id"], c)
        x["project"]["mode"] = "live"
        engine.store.save(c, x, x["revision"])
    engine.settings.input_rate = 1
    engine.settings.output_rate = 1

    class Broken:
        def invoke(self, *a):
            raise RuntimeError("provider unavailable")

    engine.agents_factory = lambda d: Broken()
    result = engine.tick(d["id"])
    assert result["state"] == "PAUSED"
    assert result["llm_reserved_usd"] > 0
    assert Engine(engine.settings).store.get(d["id"])["llm_reserved_usd"] == result["llm_reserved_usd"]


def test_incomplete_comparison_cannot_be_analyzed(engine):
    d = create(engine)
    d = advance(engine, d["id"], State.VALIDATING)
    with engine.store.transaction() as c:
        x = engine.store.get(d["id"], c)
        x["runs"].pop()
        engine.store.save(c, x, x["revision"])
    result = engine.tick(d["id"])
    assert result["state"] == "PAUSED"
    assert "incomplete" in result["last_error"]


def test_demo_cancel_tombstone_prevents_late_submit(engine):
    d = create(engine)
    d = advance(engine, d["id"], State.RUNNING)
    runner = engine.runner(d)
    spec = d["runs"][0]["spec"]
    assert runner.cancel(spec)["status"] == "CANCELLED"
    assert runner.submit(spec)["status"] == "CANCELLED"


def test_independent_repeat_of_fixture_seed(engine):
    d = create(engine)
    d = advance(engine, d["id"], State.RUNNING)
    spec = deepcopy(d["runs"][0]["spec"])
    runner = engine.runner(d)
    first = runner.submit(spec)
    spec["run_id"] = uid()
    spec["idempotency_key"] = digest({"run_id": spec["run_id"]})
    repeated = runner.submit(spec)
    assert repeated["job_id"] != first["job_id"]
    assert repeated["metrics"] == first["metrics"]
