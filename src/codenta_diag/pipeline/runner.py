"""Fixed-sequence pipeline runner: extraction -> coding -> qa.

This is the (simulated) PRODUCTION prediction pipeline. It is deliberately not an agent: the stage
order is fixed, each stage emits a structured, saved output, and policies are inputs.
The sandbox reuses this runner with candidate configs and injected intermediate outputs.
"""
from __future__ import annotations

import copy
from typing import Any

from ..config_registry import ConfigRegistry
from ..domain import PipelineConfig, PipelineRun, SourceNote, StageTrace
from .coding import run_coding
from .extraction import run_extraction
from .qa import QAContext, run_qa


def production_config(registry: ConfigRegistry, version: str = "v1.3") -> PipelineConfig:
    spec = registry.pipeline_version(version)
    spec.pop("status", None)
    return PipelineConfig(version=version, **spec)


def resolve_billing_event(policy_ref: str | None, practice_id: str, config: PipelineConfig,
                          registry: ConfigRegistry,
                          billing_event_override: str | None = None) -> tuple[str | None, str, str | None]:
    """Returns (billing_event, policy_source, effective_policy_ref)."""
    if billing_event_override is not None:
        return billing_event_override, "sandbox_counterfactual", f"COUNTERFACTUAL@{billing_event_override}"
    policy = registry.policy_from_ref(policy_ref)
    if policy is not None:
        return policy.crown_billing_event, "run_input", policy.ref
    if config.policy_resolution == "registry_fallback":
        reg = registry.policy(practice_id)
        if reg is not None:
            return reg.crown_billing_event, "registry", reg.ref
    if config.missing_policy_behavior == "default_prep":
        return "prep", "default_prep", None
    if config.missing_policy_behavior == "default_seat":
        return "seat", "default_seat", None
    return None, "none", None


def set_field(data: dict[str, Any], path: str, value: Any) -> None:
    group, name = path.split(".", 1)
    data.setdefault(group, {})[name] = value


def run_pipeline(
    note: SourceNote,
    policy_ref: str | None,
    config: PipelineConfig,
    registry: ConfigRegistry,
    *,
    run_id: str,
    extraction_override: dict[str, Any] | None = None,
    field_overrides: dict[str, Any] | None = None,
    billing_event_override: str | None = None,
) -> PipelineRun:
    card = registry.rule_card(config.rule_card)
    lexicon = registry.lexicon(config.lexicon)

    if extraction_override is not None:
        extraction = copy.deepcopy(extraction_override)
        extraction_source = "injected_recorded_output"
    else:
        extraction = run_extraction(note.text, config.extraction_schema, config.extraction_prompt,
                                    registry, config.lexicon)
        extraction_source = "simulated_stage"
    for path, value in (field_overrides or {}).items():
        set_field(extraction, path, value)
    if field_overrides:
        extraction_source += "+field_overrides"

    billing_event, policy_source, effective_ref = resolve_billing_event(
        policy_ref, note.practice_id, config, registry, billing_event_override)
    coding = run_coding(extraction, billing_event, card)
    qa_ctx = QAContext(note.text, extraction, coding, billing_event, policy_source, card, lexicon)
    qa = run_qa(qa_ctx, registry.qa_rules(config.qa))

    traces = [
        StageTrace(stage="extraction", stage_version=f"{config.extraction_schema}/{config.extraction_prompt}",
                   inputs={"note_id": note.note_id, "source": extraction_source}, output=extraction),
        StageTrace(stage="coding", stage_version=config.coding,
                   inputs={"policy_ref": effective_ref, "policy_source": policy_source,
                           "billing_event": billing_event, "rule_card": config.rule_card},
                   output=coding),
        StageTrace(stage="qa", stage_version=config.qa, inputs={"rules": registry.qa_rules(config.qa)},
                   output=qa),
    ]
    return PipelineRun(
        run_id=run_id,
        practice_id=note.practice_id,
        note_id=note.note_id,
        pipeline_version=config.version,
        policy_ref=policy_ref,
        traces=traces,
        predicted_codes=coding["predicted_codes"],
        qa_passed=qa["passed"],
    )
