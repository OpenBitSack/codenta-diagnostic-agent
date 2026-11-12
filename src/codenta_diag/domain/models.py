from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MissingPolicyBehavior = Literal["default_prep", "default_seat", "hold"]
PolicyResolution = Literal["run_input", "registry_fallback"]


def utcnow() -> datetime:
    """Naive UTC timestamp: identical semantics on SQLite and PostgreSQL. All stored times are UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PipelineConfig(BaseModel):
    """Component versions of one pipeline variant (production or sandbox candidate)."""

    model_config = ConfigDict(frozen=True)

    version: str
    extraction_schema: str
    extraction_prompt: str
    coding: str
    missing_policy_behavior: MissingPolicyBehavior
    policy_resolution: PolicyResolution
    qa: str
    rule_card: str = "interview-v1"
    lexicon: str = "v1"
    sandbox: bool = False

    def with_overrides(self, overrides: dict[str, Any], label: str) -> "PipelineConfig":
        allowed = set(type(self).model_fields) - {"version", "sandbox"}
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f"Unknown pipeline override(s): {sorted(unknown)}")
        return self.model_copy(update={**overrides, "version": label, "sandbox": True})

    def components(self) -> dict[str, str]:
        return self.model_dump(exclude={"version", "sandbox"})


class StageTrace(BaseModel):
    stage: Literal["extraction", "coding", "qa"]
    stage_version: str
    simulated: bool = True  # every stage in this prototype is a deterministic simulation of an LLM stage
    inputs: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any]


class PipelineRun(BaseModel):
    run_id: str
    practice_id: str
    note_id: str
    pipeline_version: str
    policy_ref: str | None
    traces: list[StageTrace]
    predicted_codes: list[str]
    qa_passed: bool

    def trace(self, stage: str) -> StageTrace:
        for t in self.traces:
            if t.stage == stage:
                return t
        raise KeyError(stage)


class SourceNote(BaseModel):
    model_config = ConfigDict(frozen=True)

    note_id: str
    practice_id: str
    text: str
    synthetic: bool = True


class CodeComparison(BaseModel):
    predicted: list[str]
    expected: list[str]
    false_positives: list[str]
    false_negatives: list[str]
    exact_match: bool


def compare_codes(predicted: list[str], expected: list[str]) -> CodeComparison:
    """Multiset comparison: each code instance counts (e.g. one D0220 per image)."""
    p, e = Counter(predicted), Counter(expected)
    fp = sorted((p - e).elements())
    fn = sorted((e - p).elements())
    return CodeComparison(
        predicted=sorted(predicted),
        expected=sorted(expected),
        false_positives=fp,
        false_negatives=fn,
        exact_match=not fp and not fn,
    )
