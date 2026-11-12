"""Agent tool interface.

Each tool has a typed argument model (-> JSON schema for LLM tool use) and returns JSON-serialisable
data. Tools are READ-ONLY with respect to production data; the only "write" tools operate on the
sandbox or on the investigation's own state. Practice scoping is enforced by the context: a tool can
only see the note/run/policy belonging to the investigation's practice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from ..config_registry import ConfigRegistry, PolicyVersion
from ..domain import PipelineConfig, PipelineRun, SourceNote
from ..evaluation import apply_quality_gate, compare_summaries
from ..evaluation.runner import RegressionRunner
from ..sandbox import ReplayRequest, SandboxReplayEngine
from .observations import Observation, observe_run


@dataclass
class ToolContext:
    investigation_id: str
    practice_id: str
    note: SourceNote
    run: PipelineRun
    expected: list[str] | None
    run_policy: PolicyVersion | None
    registry_policy: PolicyVersion | None
    base_config: PipelineConfig
    registry: ConfigRegistry
    engine: SandboxReplayEngine
    regression: RegressionRunner
    gate_cfg: dict[str, Any]
    similar_fn: Callable[[int], list[dict[str, Any]]] = lambda n: []
    _observations: list[Observation] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.note.practice_id != self.practice_id or self.run.practice_id != self.practice_id:
            raise PermissionError("practice isolation violation: note/run do not belong to investigation practice")

    def observations(self) -> list[Observation]:
        if self._observations is None:
            self._observations = observe_run(self.note, self.run, self.expected, self.registry)
        return self._observations


# ---------------- argument models ----------------
class NoArgs(BaseModel):
    pass


class SimilarArgs(BaseModel):
    limit: int = Field(5, ge=1, le=20)


class InspectArgs(BaseModel):
    stage: Literal["extraction", "coding", "qa"]


class SchemaArgs(BaseModel):
    field_path: str = Field(..., description="e.g. crown.status")
    schema_id: str | None = Field(None, description="defaults to the schema used by the failed run")


class ReplayArgs(BaseModel):
    label: str = Field(..., description="short human-readable experiment name")
    from_stage: Literal["extraction", "coding"] = Field(
        "coding", description="'coding' reuses the recorded extraction output; 'extraction' re-runs extraction")
    field_overrides: dict[str, Any] = Field(default_factory=dict,
                                            description="extraction fields to inject, e.g. {'crown.status': 'prepared'}")
    candidate_change_ids: list[str] = Field(default_factory=list, description="sandbox catalog change ids")
    billing_event_override: Literal["prep", "seat"] | None = Field(
        None, description="counterfactual practice policy (sandbox only)")
    expected_override: list[str] | None = Field(
        None, description="derived expectation for counterfactuals; omit to compare with the ticket label")
    tests_hypotheses: list[str] = Field(default_factory=list, description="hypothesis ids this experiment tests")


class EvalArgs(BaseModel):
    candidate_change_ids: list[str] = Field(..., min_length=1)


class HypothesisIn(BaseModel):
    cause_id: str
    stage: str
    statement: str


class RecordHypothesesArgs(BaseModel):
    hypotheses: list[HypothesisIn]


class VerdictIn(BaseModel):
    hypothesis_id: str
    status: Literal["supported", "refuted", "inconclusive", "untestable"]
    roles: list[str] = Field(default_factory=list)
    supporting_experiments: list[str] = Field(default_factory=list)
    refuting_experiments: list[str] = Field(default_factory=list)
    supporting_observations: list[str] = Field(default_factory=list)
    rationale: str


class SubmitDiagnosisArgs(BaseModel):
    earliest_incorrect_stage: str | None
    verdicts: list[VerdictIn]
    summary: str


class EscalationArgs(BaseModel):
    reason: str
    questions_for_human: list[str] = Field(..., min_length=1)


# ---------------- tool implementations ----------------
def get_original_note(ctx: ToolContext, _: NoArgs) -> dict[str, Any]:
    return {"note_id": ctx.note.note_id, "practice_id": ctx.note.practice_id, "text": ctx.note.text,
            "synthetic": ctx.note.synthetic}


def get_practice_policy(ctx: ToolContext, _: NoArgs) -> dict[str, Any]:
    def dump(p: PolicyVersion | None) -> dict[str, Any] | None:
        return None if p is None else {"ref": p.ref, "crown_billing_event": p.crown_billing_event, "text": p.text}
    return {"policy_attached_to_run": dump(ctx.run_policy), "registry_current_policy": dump(ctx.registry_policy)}


def get_pipeline_traces(ctx: ToolContext, _: NoArgs) -> dict[str, Any]:
    return {"run_id": ctx.run.run_id, "pipeline_version": ctx.run.pipeline_version,
            "traces": [t.model_dump() for t in ctx.run.traces]}


def get_predicted_codes(ctx: ToolContext, _: NoArgs) -> dict[str, Any]:
    return {"predicted_codes": ctx.run.predicted_codes, "qa_passed": ctx.run.qa_passed}


def get_expected_codes(ctx: ToolContext, _: NoArgs) -> dict[str, Any]:
    return {"expected_codes": ctx.expected, "source": "ticket_label" if ctx.expected is not None else None}


def find_similar_incidents(ctx: ToolContext, args: SimilarArgs) -> dict[str, Any]:
    return {"note": "similarity is a retrieval signal, not proof of a shared root cause",
            "candidates": ctx.similar_fn(args.limit)}


def inspect_stage_output(ctx: ToolContext, args: InspectArgs) -> dict[str, Any]:
    trace = ctx.run.trace(args.stage)
    obs = [o.model_dump(mode="json") for o in ctx.observations() if o.stage == args.stage]
    return {"stage": args.stage, "stage_version": trace.stage_version, "inputs": trace.inputs,
            "output": trace.output, "deterministic_observations": obs}


def inspect_schema(ctx: ToolContext, args: SchemaArgs) -> dict[str, Any]:
    schema_id = args.schema_id or ctx.run.trace("extraction").stage_version.split("/")[0]
    schema = ctx.registry.extraction_schema(schema_id)
    group, _, name = args.field_path.partition(".")
    spec = schema["fields"].get(group, {}).get(name)
    return {"schema_id": schema_id, "field_path": args.field_path, "spec": spec,
            "unrepresentable_projection": (schema.get("unrepresentable_projection") or {}).get(args.field_path)}


def run_sandbox_replay(ctx: ToolContext, args: ReplayArgs) -> dict[str, Any]:
    req = ReplayRequest(
        label=args.label, from_stage=args.from_stage, field_overrides=args.field_overrides,
        candidate_change_ids=args.candidate_change_ids, billing_event_override=args.billing_event_override,
        expected_override=args.expected_override,
        expected_source="derived_counterfactual" if args.expected_override is not None else "ticket_label",
    )
    return ctx.engine.replay(ctx.note, ctx.run, ctx.base_config, req, ctx.expected).model_dump(mode="json")


def run_targeted_evaluation(ctx: ToolContext, args: EvalArgs) -> dict[str, Any]:
    baseline, _ = ctx.regression.summary(ctx.base_config)
    label = "candidate:" + "+".join(sorted(args.candidate_change_ids))
    cand_cfg = ctx.engine.build_config(ctx.base_config, args.candidate_change_ids, label)
    candidate, cached = ctx.regression.summary(cand_cfg)
    report = compare_summaries(baseline, candidate)
    target = None
    if ctx.expected is not None:
        target_exp = ctx.engine.replay(
            ctx.note, ctx.run, ctx.base_config,
            ReplayRequest(label=f"target-check:{label}", from_stage="extraction",
                          candidate_change_ids=args.candidate_change_ids),
            ctx.expected)
        target = target_exp.comparison
    gate = apply_quality_gate(report, target, ctx.gate_cfg)
    return {
        "candidate_change_ids": sorted(args.candidate_change_ids),
        "components": cand_cfg.components(),
        "dataset": ctx.regression.dataset.ref,
        "cached": cached,
        "regression_report": report.model_dump(mode="json"),
        "candidate_by_category": candidate.by_category,
        "target": target.model_dump() if target else None,
        "gate": {"passed": gate.passed, "checks": [c.model_dump() for c in gate.checks]},
    }


ToolFn = Callable[[ToolContext, Any], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: ToolFn | None           # None -> handled by the controller (state-changing control tools)
    cost: Literal["free", "experiment", "regression", "control"] = "free"

    def json_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": self.args_model.model_json_schema()}


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("get_original_note", "Source clinical note (synthetic) for this investigation.", NoArgs, get_original_note),
    ToolSpec("get_practice_policy", "Policy attached to the run and the practice's current registry policy.",
             NoArgs, get_practice_policy),
    ToolSpec("get_pipeline_traces", "Saved structured outputs of every pipeline stage (read-only).", NoArgs,
             get_pipeline_traces),
    ToolSpec("get_predicted_codes", "Codes predicted by the failed run and whether QA passed.", NoArgs,
             get_predicted_codes),
    ToolSpec("get_expected_codes", "Expected codes from the ticket label, if known.", NoArgs, get_expected_codes),
    ToolSpec("find_similar_incidents", "Retrieve related investigations (metadata filter + similarity). "
             "Candidates only; never proof of a shared cause.", SimilarArgs, find_similar_incidents),
    ToolSpec("inspect_stage_output", "Stage output plus deterministic evidence checks with note spans.",
             InspectArgs, inspect_stage_output),
    ToolSpec("inspect_schema", "Inspect an extraction schema field definition (enum, nullability).", SchemaArgs,
             inspect_schema),
    ToolSpec("run_sandbox_replay", "Run a controlled sandbox experiment: inject corrected fields, swap candidate "
             "components, or apply a counterfactual policy. Never touches production.", ReplayArgs,
             run_sandbox_replay, "experiment"),
    ToolSpec("run_targeted_evaluation", "Evaluate a bundle of sandbox candidate changes on the labeled regression "
             "suite and the target ticket; returns metrics and quality-gate decision.", EvalArgs,
             run_targeted_evaluation, "regression"),
    ToolSpec("record_hypotheses", "Record root-cause hypotheses before testing them.", RecordHypothesesArgs,
             None, "control"),
    ToolSpec("submit_diagnosis", "Finish the investigation with verdicts that cite experiment ids.",
             SubmitDiagnosisArgs, None, "control"),
    ToolSpec("create_escalation_report", "Stop and escalate to a human with specific questions.", EscalationArgs,
             None, "control"),
]}
