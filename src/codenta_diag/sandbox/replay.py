"""Sandbox replay engine.

Isolation guarantees (enforced by construction):
  * Inputs are deep-copied read-only snapshots of production traces; nothing is written back.
  * Candidate configs are always marked `sandbox=True`; there is no API to promote them.
  * Every experiment gets a deterministic ID derived from its full input + versions, so re-running
    an identical experiment is a cache hit and results are reproducible across machines.
"""
from __future__ import annotations

import copy
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config_registry import ConfigRegistry, stable_hash
from ..domain import CodeComparison, PipelineConfig, PipelineRun, SourceNote, compare_codes
from ..pipeline import run_pipeline
from .cache import InMemoryCache, ResultCache


class ReplayRequest(BaseModel):
    label: str
    from_stage: Literal["extraction", "coding"] = "coding"
    field_overrides: dict[str, Any] = Field(default_factory=dict)
    candidate_change_ids: list[str] = Field(default_factory=list)
    billing_event_override: Literal["prep", "seat"] | None = None
    expected_override: list[str] | None = None  # e.g. derived expectation for a policy counterfactual
    expected_source: str = "ticket_label"


class ExperimentResult(BaseModel):
    experiment_id: str
    label: str
    request: ReplayRequest
    pipeline_label: str
    components: dict[str, str]
    extraction: dict[str, Any]
    coding_inputs: dict[str, Any]
    predicted_codes: list[str]
    comparison: CodeComparison | None
    expected_source: str | None
    qa_passed: bool
    qa_failed_rules: list[str]
    config_fingerprint: str
    cached: bool = False
    duration_ms: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "label": self.label,
            "predicted_codes": self.predicted_codes,
            "exact_match": self.comparison.exact_match if self.comparison else None,
            "false_positives": self.comparison.false_positives if self.comparison else None,
            "false_negatives": self.comparison.false_negatives if self.comparison else None,
            "expected_source": self.expected_source,
            "qa_passed": self.qa_passed,
            "qa_failed_rules": self.qa_failed_rules,
            "cached": self.cached,
        }


class SandboxReplayEngine:
    def __init__(self, registry: ConfigRegistry, cache: ResultCache | None = None) -> None:
        self.registry = registry
        self.cache = cache or InMemoryCache()
        self._config_fp = registry.fingerprint()

    def build_config(self, base: PipelineConfig, change_ids: list[str], label: str) -> PipelineConfig:
        overrides: dict[str, Any] = {}
        for cid in sorted(change_ids):
            overrides.update(self.registry.candidate_change(cid)["overrides"])
        if not overrides:
            return base
        return base.with_overrides(overrides, label)

    def replay(self, note: SourceNote, recorded: PipelineRun, base_config: PipelineConfig,
               request: ReplayRequest, expected: list[str] | None) -> ExperimentResult:
        expected_codes = request.expected_override if request.expected_override is not None else expected
        config = self.build_config(base_config, request.candidate_change_ids,
                                   f"sandbox:{'+'.join(sorted(request.candidate_change_ids)) or 'baseline'}")
        recorded_extraction = recorded.trace("extraction").output
        key_material = {
            "note": stable_hash(note.text),
            "recorded_extraction": stable_hash(recorded_extraction),
            "policy_ref": recorded.policy_ref,
            "practice": note.practice_id,
            "request": request.model_dump(exclude={"label"}),
            "components": config.components(),
            "expected": expected_codes,
            "config_fp": self._config_fp,
        }
        key = "replay:" + stable_hash(key_material, 24)
        experiment_id = "EXP-" + key.split(":")[1][:10]
        if (hit := self.cache.get(key)) is not None:
            res = ExperimentResult(**hit)
            return res.model_copy(update={"cached": True, "label": request.label, "duration_ms": 0.0})

        t0 = time.perf_counter()
        run = run_pipeline(
            note,
            recorded.policy_ref,
            config,
            self.registry,
            run_id=f"sandbox:{experiment_id}",
            extraction_override=copy.deepcopy(recorded_extraction) if request.from_stage == "coding" else None,
            field_overrides=request.field_overrides,
            billing_event_override=request.billing_event_override,
        )
        qa = run.trace("qa").output
        result = ExperimentResult(
            experiment_id=experiment_id,
            label=request.label,
            request=request,
            pipeline_label=config.version,
            components=config.components(),
            extraction=run.trace("extraction").output,
            coding_inputs=run.trace("coding").inputs,
            predicted_codes=run.predicted_codes,
            comparison=compare_codes(run.predicted_codes, expected_codes) if expected_codes is not None else None,
            expected_source=request.expected_source if expected_codes is not None else None,
            qa_passed=run.qa_passed,
            qa_failed_rules=[c["rule_id"] for c in qa["checks"] if not c["passed"]],
            config_fingerprint=self._config_fp,
            duration_ms=round((time.perf_counter() - t0) * 1000, 3),
        )
        self.cache.put(key, "replay", result.model_dump(mode="json"))
        return result
