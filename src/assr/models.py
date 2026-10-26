from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def uid() -> str:
    return str(uuid4())


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1


class State(StrEnum):
    DRAFT = "DRAFT"
    PLAN_REVIEW = "PLAN_REVIEW"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    IMPLEMENTING = "IMPLEMENTING"
    CODE_REVIEW = "CODE_REVIEW"
    SMOKE_TEST = "SMOKE_TEST"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    ANALYZING = "ANALYZING"
    CONCLUSION_REVIEW = "CONCLUSION_REVIEW"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


TERMINAL = {State.COMPLETED, State.CANCELLED, State.FAILED, State.BUDGET_EXHAUSTED}


class Budget(Contract):
    gpu_seconds: int = Field(gt=0, default=7200)
    llm_usd: float = Field(gt=0, default=5)
    walltime_seconds: int = Field(gt=0, le=86400, default=120)
    max_repairs: int = Field(ge=0, le=10, default=3)
    max_infra_retries: int = Field(ge=0, le=5, default=2)
    max_iterations: int = Field(ge=1, le=10, default=3)


class ProjectSpec(Contract):
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=8, max_length=12000)
    profile: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", default="toy")
    mode: Literal["demo", "live"] = "demo"
    budget: Budget = Field(default_factory=Budget)
    seeds: list[int] = Field(default_factory=lambda: [11, 22, 33], min_length=3, max_length=20)
    delta: float = Field(default=0.01, ge=0)
    baseline_expected: float | None = Field(default=None, ge=0, le=1)
    baseline_tolerance: float = Field(default=0.15, ge=0)
    sources: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("seeds")
    @classmethod
    def unique_seeds(cls, seeds):
        if len(seeds) != len(set(seeds)) or any(x < 0 for x in seeds):
            raise ValueError("seeds must be distinct nonnegative integers")
        return seeds


class Hypothesis(Contract):
    claim: str = Field(min_length=8)
    rationale: str
    refutation: str
    source_refs: list[str] = Field(default_factory=list)


class ExperimentSpec(Contract):
    experiment_id: str = Field(default_factory=uid)
    revision: int = Field(default=1, ge=1)
    hypothesis: Hypothesis
    primary_metric: Literal["accuracy"] = "accuracy"
    direction: Literal["maximize"] = "maximize"
    seeds: list[int]
    delta: float = Field(ge=0)
    fixed_conditions: list[str]
    treatment: str


class CodeProposal(Contract):
    files: dict[str, str]
    explanation: str


class ReviewDecision(Contract):
    verdict: Literal["accept", "revise", "escalate"]
    target_digest: str
    checks: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)
    requested_actions: list[str] = Field(default_factory=list)


class RunSpec(Contract):
    run_id: str
    experiment_id: str
    revision: int
    variant: Literal["baseline", "treatment", "smoke"]
    seed: int
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    evaluator_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    image_digest: str
    walltime_seconds: int = Field(gt=0, le=86400)
    gpu_count: int = Field(ge=0, le=1, default=1)
    idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    argv: list[str] = Field(min_length=1)
    config: dict
    files: dict[str, str]

    @model_validator(mode="after")
    def verify(self):
        if digest(self.files) != self.source_digest or digest(self.config) != self.config_digest:
            raise ValueError("source/config digest mismatch")
        return self


class ResearchConclusion(Contract):
    verdict: Literal["supported", "not_supported", "inconclusive"]
    claim: str
    experiment_refs: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    next_step: str


class Envelope(Contract):
    message_id: str = Field(default_factory=uid)
    campaign_id: str
    sender: str
    recipient: str
    type: str
    correlation_id: str
    causation_id: str | None = None
    target_revision: int
    created_at: str = Field(default_factory=now)
    payload: dict


class DecisionInput(Contract):
    revision: int
    action: Literal["approve", "request_changes", "pause", "resume", "cancel"]
    comment: str = Field(default="", max_length=4000)


def finite_metric(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("non-finite or missing metric")
    if not 0 <= value <= 1:
        raise ValueError("accuracy out of range")
    return float(value)
