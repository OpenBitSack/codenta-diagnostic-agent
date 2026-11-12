"""Checkpointable investigation state. Everything here is JSON-serialisable and persisted after
every step so a crashed worker resumes instead of restarting (and re-paying for) an investigation."""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

HypothesisStatus = Literal["proposed", "supported", "refuted", "inconclusive", "untestable"]
Phase = Literal["observe", "hypothesize", "test", "conclude", "fix", "done", "escalated"]


class Hypothesis(BaseModel):
    id: str
    label: str                      # display letter, e.g. "A"
    cause_id: str                   # stable root-cause signature, e.g. extraction.crown.status.value_incorrect
    stage: str
    statement: str
    status: HypothesisStatus = "proposed"
    roles: list[str] = Field(default_factory=list)   # sufficient | necessary | latent | contributing
    supporting_experiments: list[str] = Field(default_factory=list)
    refuting_experiments: list[str] = Field(default_factory=list)
    supporting_observations: list[str] = Field(default_factory=list)
    rationale: str = ""
    origin: Literal["deterministic_policy", "llm"] = "deterministic_policy"
    verification_note: str = ""     # set when the controller downgrades an unsupported claim


class ToolCallRecord(BaseModel):
    step: int
    tool: str
    args: dict[str, Any]
    result_summary: dict[str, Any]
    ts: float = Field(default_factory=time.time)


class Decision(BaseModel):
    step: int
    phase: Phase
    action: str
    rationale: str


class BudgetUsage(BaseModel):
    reasoning_steps: int = 0
    experiments: int = 0
    regression_runs: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    started_at: float = Field(default_factory=time.time)
    elapsed_before_resume: float = 0.0
    elapsed_at_checkpoint: float = 0.0


class InvestigationState(BaseModel):
    investigation_id: str
    reasoner: str
    phase: Phase = "observe"
    observations: list[dict[str, Any]] = Field(default_factory=list)
    symptom_signature: str = ""
    earliest_incorrect_stage: str | None = None
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    experiments: list[dict[str, Any]] = Field(default_factory=list)     # ExperimentResult dumps
    plan_done: list[str] = Field(default_factory=list)                   # deterministic planner keys
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    budget: BudgetUsage = Field(default_factory=BudgetUsage)
    llm_messages: list[dict[str, Any]] = Field(default_factory=list)
    diagnosis: dict[str, Any] | None = None
    fix_attempts: list[dict[str, Any]] = Field(default_factory=list)
    validated_fix: dict[str, Any] | None = None
    escalation: dict[str, Any] | None = None
    fast_path: dict[str, Any] | None = None    # set when resolved by known-issue counterfactual replay
    resumed_count: int = 0

    def hypothesis(self, hid: str) -> Hypothesis | None:
        return next((h for h in self.hypotheses if h.id == hid or h.cause_id == hid), None)

    def experiment(self, exp_id: str) -> dict[str, Any] | None:
        return next((e for e in self.experiments if e["experiment_id"] == exp_id), None)

    def experiment_by_label(self, label: str) -> dict[str, Any] | None:
        return next((e for e in self.experiments if e["label"] == label), None)
