"""One durable state transition per LangGraph invocation.
SQL snapshots are authoritative; a lease is committed BEFORE external I/O.
"""

from copy import deepcopy
import json
import statistics
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import Agents
from .models import (
    CodeProposal,
    ExperimentSpec,
    ProjectSpec,
    ResearchConclusion,
    ReviewDecision,
    RunSpec,
    State,
    TERMINAL,
    digest,
    finite_metric,
    now,
    uid,
)
from .runner import DemoRunner, RunnerUnavailable, SSHRunner
from .store import Conflict, Store
from .tracking import Tracking
from .workspace import commit_bundle, load_profile, safe_files

AGENT_STATES = {
    State.DRAFT,
    State.PLAN_REVIEW,
    State.IMPLEMENTING,
    State.CODE_REVIEW,
    State.VALIDATING,
    State.ANALYZING,
    State.CONCLUSION_REVIEW,
}


class Frame(TypedDict):
    data: dict


class Engine:
    def __init__(self, settings, store=None, agents_factory=None, runner_factory=None):
        self.settings = settings
        self.store = store or Store(settings.database_url)
        self.agents_factory, self.runner_factory = agents_factory, runner_factory
        self.tracking = Tracking(settings.mlflow_url)
        graph = StateGraph(Frame)
        handlers = {
            "DRAFT": self.plan,
            "PLAN_REVIEW": self.plan_review,
            "IMPLEMENTING": self.implement,
            "CODE_REVIEW": self.code_review,
            "SMOKE_TEST": self.smoke,
            "QUEUED": self.queue,
            "RUNNING": self.running,
            "VALIDATING": self.validate,
            "ANALYZING": self.analyze,
            "CONCLUSION_REVIEW": self.conclusion_review,
            "CANCEL_PENDING": self.cancel_pending,
        }
        for name, fn in handlers.items():

            def node(frame, fn=fn):
                fn(frame["data"])
                return frame

            graph.add_node(name, node)
            graph.add_edge(name, END)
        graph.add_conditional_edges(START, lambda frame: frame["data"]["state"], {k: k for k in handlers})
        self.graph = graph.compile()

    def create(self, project: ProjectSpec, key):
        profile = load_profile(self.settings.root / "profiles", project.profile)
        if project.mode == "live":
            if not all(
                (
                    self.settings.ssh_host,
                    self.settings.llm_key,
                    self.settings.llm_model,
                    self.settings.mlflow_url,
                    self.settings.token,
                )
            ):
                raise ValueError("live mode requires SSH, LLM, MLflow and API authentication settings")
            if self.settings.input_rate <= 0 or self.settings.output_rate <= 0:
                raise ValueError("configure positive LLM prices for budget reservation")
            if project.baseline_expected is None:
                raise ValueError("live project requires baseline_expected and tolerance")
            if "@sha256:" not in profile["image_digest"]:
                raise ValueError("live profile needs immutable image digest")
        fp = digest(project.model_dump())
        with self.store.transaction() as c:
            prior = self.store.replay(c, "create:" + key, fp)
            if prior:
                return prior
            from .store import campaigns
            from sqlalchemy import select

            if any(
                json.loads(row[0])["state"] not in TERMINAL for row in c.execute(select(campaigns.c.body))
            ):
                raise Conflict("MVP permits one nonterminal campaign at a time")
            d = {
                "id": uid(),
                "revision": 1,
                "state": State.DRAFT,
                "created_at": now(),
                "project": project.model_dump(),
                "profile": profile,
                "runs": [],
                "reviews": [],
                "experiments": [],
                "usage": [],
                "llm_reserved_usd": 0.0,
                "gpu_reserved_seconds": 0,
                "repair_count": 0,
                "infra_retries": 0,
                "iteration": 1,
                "feedback": [],
                "lease": None,
            }
            self.store.event(c, d, "campaign.created", {"project": project.model_dump()})
            self.store.insert(c, d)
            result = self.public(d)
            self.store.remember(c, "create:" + key, fp, result)
            return result

    @staticmethod
    def public(d):
        out = deepcopy(d)
        out.pop("lease", None)
        return out

    def decision(self, cid, request, key):
        fp = digest(request.model_dump())
        with self.store.transaction() as c:
            prior = self.store.replay(c, cid + ":" + key, fp)
            if prior:
                return prior
            d = self.store.get(cid, c)
            rev = d["revision"]
            if request.revision != rev:
                raise Conflict("stale revision; refresh before deciding")
            if d["state"] in TERMINAL:
                raise Conflict("campaign already terminal")
            action = request.action
            if action == "approve":
                if d["state"] != State.AWAITING_APPROVAL:
                    raise Conflict("no plan awaiting approval")
                d["approval_digest"] = digest(d["experiment"])
                d["state"] = State.IMPLEMENTING
            elif action == "pause":
                if d["state"] in (State.PAUSED, State.CANCEL_PENDING):
                    raise Conflict("already paused or cancelling")
                d["resume_state"] = d["state"]
                d["state"] = State.PAUSED
            elif action == "resume":
                if d["state"] != State.PAUSED:
                    raise Conflict("not paused")
                d["state"] = d.pop("resume_state", State.DRAFT)
            elif action == "request_changes":
                if d["state"] not in (State.AWAITING_APPROVAL, State.PAUSED):
                    raise Conflict("pause before changing a plan")
                if any(r["status"] in ("PENDING", "RUNNING") for r in d["runs"]):
                    raise Conflict("cancel or finish active runs before replanning")
                if not request.comment.strip():
                    raise ValueError("changes require a comment")
                if d["iteration"] >= d["project"]["budget"]["max_iterations"]:
                    raise Conflict("iteration limit reached; create a new campaign")
                d["iteration"] += 1
                d["feedback"].append(request.comment)
                d.pop("approval_digest", None)
                d.pop("code_review_digest", None)
                d["repair_count"] = 0
                d["state"] = State.DRAFT
            elif action == "cancel":
                d["state"] = State.CANCEL_PENDING
            self.store.event(c, d, "human.decision", request.model_dump(), sender="human")
            self.store.save(c, d, rev)
            out = self.public(d)
            self.store.remember(c, cid + ":" + key, fp, out)
            return out

    def tick(self, cid):
        token = uid()
        with self.store.transaction() as c:
            d = self.store.get(cid, c)
            if d["state"] in TERMINAL or d["state"] in (State.PAUSED, State.AWAITING_APPROVAL):
                return d
            if d.get("lease") and d["lease"]["until"] > time.time():
                return d
            rev = d["revision"]
            if d["state"] in AGENT_STATES and d["project"]["mode"] == "live":
                # Up to three requests; token upper bound conservatively exceeds UTF-8 bytes ceiling.
                reserve = (
                    3 * (200000 * self.settings.input_rate + 3000 * self.settings.output_rate) / 1_000_000
                )
                if d["llm_reserved_usd"] + reserve > d["project"]["budget"]["llm_usd"]:
                    d["state"] = State.BUDGET_EXHAUSTED
                    self.store.event(c, d, "budget.exhausted", {"resource": "LLM"})
                    self.store.save(c, d, rev)
                    return d
                d["llm_reserved_usd"] += reserve
            d["lease"] = {"token": token, "until": time.time() + 300}
            self.store.save(c, d, rev)
        before = d["state"]
        d.pop("last_error", None)
        try:
            self.graph.invoke({"data": d})
        except RunnerUnavailable:
            d["last_error"] = "Runner unavailable; retaining job identity for reconciliation"
        except Exception as exc:
            d["resume_state"] = before
            d["state"] = State.PAUSED
            # Avoid provider exception text that can contain URLs/credentials.
            d["last_error"] = (
                f"{type(exc).__name__}: operation failed; inspect local service logs or artifacts"
            )
            if isinstance(exc, (ValueError, Conflict)):
                d["last_error"] = str(exc)[:1000]
        with self.store.transaction() as c:
            current = self.store.get(cid, c)
            if (current.get("lease") or {}).get("token") != token:
                return current
            if current["revision"] != d["revision"]:
                # A human paused/cancelled while I/O was in flight; never overwrite that decision.
                current["lease"] = None
                self.store.save(c, current, current["revision"])
                return current
            rev = d["revision"]
            d["lease"] = None
            for review in d["reviews"][len(current["reviews"]) :]:
                self.store.event(
                    c, d, "review.completed", review, sender=review["role"], recipient="orchestrator"
                )
            for usage in d["usage"][len(current["usage"]) :]:
                self.store.event(
                    c, d, "agent.completed", usage, sender=usage["role"], recipient="orchestrator"
                )
            if d.get("experiment") != current.get("experiment"):
                self.store.event(
                    c, d, "experiment.proposed", d["experiment"], sender="researcher", recipient="k1"
                )
            self.store.event(
                c, d, "workflow.transition", {"from": before, "to": d["state"], "error": d.get("last_error")}
            )
            self.store.save(c, d, rev)
        return d

    def agents(self, d):
        if self.agents_factory:
            return self.agents_factory(d)
        s = self.settings
        return Agents(
            d["project"]["mode"],
            s.root / "agent-cache",
            s.llm_url,
            s.llm_key,
            s.llm_model,
            input_rate=s.input_rate,
            output_rate=s.output_rate,
        )

    def invoke(self, d, role, context, schema):
        result, usage = self.agents(d).invoke(role, context, schema)
        d["usage"].append({"role": role, **usage})
        # Reservations remain conservative; never release an ambiguous/crashed call's reservation.
        return result

    def review(self, d, role, target, context, evidence):
        target_digest = digest(target)
        result = self.invoke(
            d, role, {"target_digest": target_digest, "evidence_refs": evidence, **context}, ReviewDecision
        )
        if result.target_digest != target_digest or not set(result.evidence_refs).issubset(set(evidence)):
            raise ValueError("review references stale or unknown evidence")
        d["reviews"].append({"role": role, **result.model_dump()})
        return result

    def plan(self, d):
        result = self.invoke(
            d,
            "researcher",
            {
                "project": d["project"],
                "feedback": d["feedback"],
                "baseline_files": d["profile"]["files"],
                "supported_protocol": "binary JSON classification; /data/train.json, /data/validation.json without labels; /config/config.json; write /output/predictions.json",
            },
            ExperimentSpec,
        )
        if result.seeds != d["project"]["seeds"] or result.delta != d["project"]["delta"]:
            raise ValueError("researcher changed frozen seeds or delta")
        if not set(result.hypothesis.source_refs).issubset(set(d["project"]["sources"])):
            raise ValueError("researcher cited an unsupplied source")
        result.experiment_id = uid()
        result.revision = d["iteration"]
        d["experiment"] = result.model_dump()
        d["experiments"].append(result.model_dump())
        d["state"] = State.PLAN_REVIEW

    def plan_review(self, d):
        r = self.review(
            d,
            "k1_plan",
            d["experiment"],
            {"project": d["project"], "experiment": d["experiment"]},
            [d["experiment"]["experiment_id"]],
        )
        if r.verdict == "accept":
            d["state"] = State.AWAITING_APPROVAL
        else:
            self.pause(d, State.DRAFT, "K1 requests plan revision: " + str(r.requested_actions))

    def implement(self, d):
        if d.get("approval_digest") != digest(d["experiment"]):
            raise ValueError("plan approval missing or stale")
        baseline = d["profile"]["files"]
        current = d.get("implementation", {}).get("files", baseline) if d["repair_count"] else baseline
        proposal = self.invoke(
            d,
            "developer",
            {
                "experiment": d["experiment"],
                "files": current,
                "allowed_files": d["profile"]["allowed_files"],
                "feedback": d["feedback"],
            },
            CodeProposal,
        )
        safe_files(proposal.files, d["profile"]["allowed_files"])
        files = {**baseline, **proposal.files}
        if files == baseline:
            raise ValueError("developer produced no change")
        for name, source in files.items():
            if name.endswith(".py"):
                compile(source, name, "exec")
        root = self.settings.root / "workspaces" / d["id"]
        base_sha = commit_bundle(root / "baseline", baseline, "Baseline input snapshot")
        sha = commit_bundle(root / "treatment", files, "Implement approved hypothesis")
        import difflib

        diff = "\n".join(
            "".join(
                difflib.unified_diff(
                    baseline.get(n, "").splitlines(True),
                    v.splitlines(True),
                    fromfile="baseline/" + n,
                    tofile="treatment/" + n,
                )
            )
            for n, v in files.items()
        )
        d["implementation"] = {
            "files": files,
            "commit_sha": sha,
            "base_commit": base_sha,
            "source_digest": digest(files),
            "diff": diff,
            "explanation": proposal.explanation,
        }
        d["state"] = State.CODE_REVIEW

    def repair(self, d, message):
        if d["repair_count"] >= d["project"]["budget"]["max_repairs"]:
            self.pause(d, State.CODE_REVIEW, "repair limit reached: " + message)
            return
        d["repair_count"] += 1
        d["feedback"].append(message)
        d.pop("code_review_digest", None)
        d["state"] = State.IMPLEMENTING

    def code_review(self, d):
        r = self.review(
            d,
            "k2_code",
            d["implementation"],
            {"implementation": d["implementation"], "experiment": d["experiment"]},
            [d["implementation"]["commit_sha"]],
        )
        if r.verdict == "accept":
            d["code_review_digest"] = digest(d["implementation"])
            d["schedule"] = [
                ("smoke", "baseline", d["project"]["seeds"][0]),
                ("smoke", "treatment", d["project"]["seeds"][0]),
            ]
            d["schedule"] += [
                ("full", variant, seed)
                for variant in ("baseline", "treatment")
                for seed in d["project"]["seeds"]
            ]
            d["state"] = State.SMOKE_TEST
        elif r.verdict == "revise":
            self.repair(d, str(r.requested_actions))
        else:
            self.pause(d, State.CODE_REVIEW, str(r.issues))

    def smoke(self, d):
        if d.get("code_review_digest") != digest(d["implementation"]):
            raise ValueError("code approval is stale")
        d["state"] = State.QUEUED

    def queue(self, d):
        if d.get("code_review_digest") != digest(d["implementation"]):
            raise ValueError("code changed after acceptance")
        if not d["schedule"]:
            d["state"] = State.VALIDATING
            return
        phase, variant, seed = d["schedule"][0]
        full = [r for r in self.current_runs(d) if r["phase"] == "full" and r["status"] == "SUCCEEDED"]
        bases = [r["result"]["metrics"]["accuracy"] for r in full if r["variant"] == "baseline"]
        if phase == "full" and variant == "treatment" and not any(r["variant"] == "treatment" for r in full):
            expected = d["project"]["baseline_expected"]
            if expected is not None and (
                len(bases) != len(d["project"]["seeds"])
                or abs(statistics.mean(bases) - expected) > d["project"]["baseline_tolerance"]
            ):
                self.pause(d, State.QUEUED, "baseline outside predefined tolerance")
                return
        gpu = 0 if d["project"]["mode"] == "demo" else 1
        wall = (
            min(300, d["project"]["budget"]["walltime_seconds"])
            if phase == "smoke"
            else d["project"]["budget"]["walltime_seconds"]
        )
        charge = wall * gpu
        if d["gpu_reserved_seconds"] + charge > d["project"]["budget"]["gpu_seconds"]:
            d["state"] = State.BUDGET_EXHAUSTED
            return
        profile = d["profile"]
        impl = d["implementation"]
        files = profile["files"] if variant == "baseline" else impl["files"]
        config = {"seed": seed, "phase": phase}
        rid = uid()
        spec = RunSpec(
            run_id=rid,
            experiment_id=d["experiment"]["experiment_id"],
            revision=d["iteration"],
            variant="smoke" if phase == "smoke" else variant,
            seed=seed,
            commit_sha=impl["base_commit"] if variant == "baseline" else impl["commit_sha"],
            source_digest=digest(files),
            config_digest=digest(config),
            dataset_digest=profile["dataset_digest"],
            evaluator_digest=profile["evaluator_digest"],
            image_digest=profile["image_digest"],
            walltime_seconds=wall,
            gpu_count=gpu,
            idempotency_key=digest({"run_id": rid}),
            argv=profile["smoke_argv"] if phase == "smoke" else profile["argv"],
            config=config,
            files=files,
        )
        d["gpu_reserved_seconds"] += charge
        d["runs"].append({"spec": spec.model_dump(), "phase": phase, "variant": variant, "status": "PENDING"})
        d["active_run"] = rid
        d["state"] = State.RUNNING  # committed before submit

    def runner(self, d):
        if self.runner_factory:
            return self.runner_factory(d)
        if d["project"]["mode"] == "demo":
            return DemoRunner(self.settings.root / "demo-jobs", d["profile"])
        return SSHRunner(self.settings.ssh_host, d["project"]["profile"])

    def running(self, d):
        run = next(r for r in d["runs"] if r["spec"]["run_id"] == d["active_run"])
        runner = self.runner(d)
        result = runner.submit(run["spec"]) if run["status"] == "PENDING" else runner.status(run["spec"])
        if result["status"] in ("SUBMITTING", "RUNNING", "EVALUATING"):
            run["status"] = "RUNNING"
            run["job_id"] = result["job_id"]
            return
        run["result"] = result
        run["status"] = result["status"]
        if result["status"] != "SUCCEEDED":
            kind = result.get("error_class", "UNKNOWN")
            if kind.startswith("INFRA_") and d["infra_retries"] < d["project"]["budget"]["max_infra_retries"]:
                d["infra_retries"] += 1
                d["state"] = State.QUEUED
            elif kind == "CODE_ERROR" and run["variant"] == "treatment":
                self.repair(d, "Execution failed: " + result.get("logs", "")[-3000:])
            else:
                self.pause(d, State.QUEUED, "Run failed: " + kind)
            return
        self.validate_run(run)
        run["mlflow_run_id"] = self.tracking.log(d, run)
        d["schedule"].pop(0)
        d["state"] = State.QUEUED

    def validate_run(self, run):
        spec, result = run["spec"], run["result"]
        RunSpec.model_validate(spec)
        if result.get("spec_digest") != digest(spec) or result.get("spec") != spec:
            raise ValueError("run provenance mismatch")
        if result.get("exit_code") != 0 or not result.get("artifact_manifest"):
            raise ValueError("missing successful execution evidence")
        finite_metric(result.get("metrics", {}).get("accuracy"))
        if result["artifact_manifest"].get("predictions.json") is None:
            raise ValueError("predictions artifact missing")

    def current_runs(self, d):
        return [
            r
            for r in d["runs"]
            if r["spec"]["experiment_id"] == d["experiment"]["experiment_id"]
            and r["spec"]["commit_sha"]
            in (d["implementation"]["base_commit"], d["implementation"]["commit_sha"])
        ]

    def validate(self, d):
        runs = [r for r in self.current_runs(d) if r["status"] == "SUCCEEDED" and r["phase"] == "full"]
        for run in runs:
            self.validate_run(run)
        keys = [(r["variant"], r["spec"]["seed"]) for r in runs]
        expected = [(v, s) for v in ("baseline", "treatment") for s in d["project"]["seeds"]]
        if sorted(keys) != sorted(expected):
            raise ValueError("incomplete or duplicate seed comparison")
        refs = [r["spec"]["run_id"] for r in runs]
        # Include original records, not another agent's summary.
        r = self.review(d, "k2_result", runs, {"runs": runs, "experiment": d["experiment"]}, refs)
        if r.verdict != "accept":
            self.pause(d, State.VALIDATING, str(r.requested_actions))
            return
        d["state"] = State.ANALYZING

    def analyze(self, d):
        runs = [r for r in self.current_runs(d) if r["status"] == "SUCCEEDED" and r["phase"] == "full"]
        pairs = {s: {} for s in d["project"]["seeds"]}
        for run in runs:
            pairs[run["spec"]["seed"]][run["variant"]] = finite_metric(run["result"]["metrics"]["accuracy"])
        deltas = [v["treatment"] - v["baseline"] for v in pairs.values()]
        mean = statistics.mean(deltas)
        # A descriptive pilot rule, not a p-value or claim of statistical significance.
        delta = d["project"]["delta"]
        verdict = (
            "supported"
            if all(x > 0 and x >= delta for x in deltas)
            else ("not_supported" if all(x < delta for x in deltas) else "inconclusive")
        )
        summary = {
            "paired": pairs,
            "mean_delta": mean,
            "stdev_delta": statistics.stdev(deltas),
            "verdict": verdict,
            "rule": "all paired improvements positive and >= practical delta; no significance claim",
        }
        d["summary"] = summary
        conclusion = self.invoke(
            d,
            "researcher_conclusion",
            {"summary": summary, "experiment": d["experiment"]},
            ResearchConclusion,
        )
        if conclusion.verdict != verdict or conclusion.experiment_refs != [d["experiment"]["experiment_id"]]:
            raise ValueError("conclusion disagrees with computed evidence")
        d["conclusion"] = conclusion.model_dump()
        d["state"] = State.CONCLUSION_REVIEW

    def conclusion_review(self, d):
        r = self.review(
            d,
            "k1_conclusion",
            d["conclusion"],
            {
                "conclusion": d["conclusion"],
                "summary": d["summary"],
                "project": d["project"],
                "runs": self.current_runs(d),
            },
            [d["experiment"]["experiment_id"]] + [x["spec"]["run_id"] for x in self.current_runs(d)],
        )
        if r.verdict == "accept":
            d["state"] = State.COMPLETED
        else:
            self.pause(d, State.ANALYZING, str(r.requested_actions))

    def cancel_pending(self, d):
        for run in d["runs"]:
            if run["status"] in ("PENDING", "RUNNING"):
                result = self.runner(d).cancel(run["spec"])
                if result["status"] not in ("CANCELLED", "SUCCEEDED", "FAILED"):
                    return
                run["status"] = result["status"]
                run["result"] = result
        d["state"] = State.CANCELLED

    @staticmethod
    def pause(d, resume, reason):
        d["resume_state"] = resume
        d["state"] = State.PAUSED
        d["last_error"] = reason
