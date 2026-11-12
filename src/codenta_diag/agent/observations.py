"""Deterministic stage inspection.

These are ordinary functions, not agent reasoning: comparing extracted fields with note evidence,
checking schema enums, re-running a stage on its recorded inputs, and checking QA outcome against
labels. Every observation carries provenance (stage, field, recorded value, note spans).
Observations are FACTS ABOUT THE TRACE; they are not root-cause conclusions.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config_registry import ConfigRegistry
from ..domain import PipelineRun, SourceNote, compare_codes
from ..pipeline.coding import run_coding
from ..pipeline.lexicon import NoteAnalyzer

ObservationKind = Literal[
    "code_false_positive", "code_false_negative", "field_contradicted_by_note",
    "field_unsupported_by_note", "schema_cannot_represent", "policy_defaulted",
    "policy_missing_at_runtime", "stage_reproduces_from_inputs", "stage_not_reproducible",
    "qa_passed_with_mismatch", "expected_codes_unknown",
]

# Generic mapping from observation type to the cause hypothesis it motivates.
CAUSE_FOR_KIND: dict[str, str] = {
    "field_contradicted_by_note": "extraction.{field}.value_incorrect",
    "field_unsupported_by_note": "extraction.{field}.value_incorrect",
    "schema_cannot_represent": "extraction.schema.{field}.unrepresentable_value",
    "policy_missing_at_runtime": "input.policy.missing_at_runtime",
    "policy_defaulted": "coding.policy.silent_default_when_missing",
    "qa_passed_with_mismatch": "qa.missed_error",
}

# Legacy v1 value semantics used when comparing recorded values against note evidence.
EQUIVALENT_VALUES = {("crown.status", "completed"): {"seated"}}


class Observation(BaseModel):
    id: str = ""
    kind: ObservationKind
    stage: str
    field: str | None = None
    recorded: Any = None
    evidence_value: Any = None
    evidence_spans: list[dict[str, Any]] = Field(default_factory=list)
    detail: str
    source: Literal["deterministic_check", "llm"] = "deterministic_check"

    def symptom(self) -> str:
        return f"{self.kind}:{self.field or ''}"


def _schema_id(run: PipelineRun) -> str:
    return run.trace("extraction").stage_version.split("/")[0]


def _matches(field: str, recorded: Any, evidence: Any) -> bool:
    return recorded == evidence or evidence in EQUIVALENT_VALUES.get((field, recorded), set())


def inspect_extraction(note: SourceNote, run: PipelineRun, registry: ConfigRegistry) -> list[Observation]:
    out: list[Observation] = []
    extraction = run.trace("extraction").output
    schema = registry.extraction_schema(_schema_id(run))
    an = NoteAnalyzer(note.text, registry.lexicon())

    crown = extraction.get("crown")
    if crown is not None and an.crown_tooth is not None:
        status, spans = an.clinical_crown_status()
        if status is not None and not _matches("crown.status", crown.get("status"), status):
            out.append(Observation(
                kind="field_contradicted_by_note", stage="extraction", field="crown.status",
                recorded=crown.get("status"), evidence_value=status,
                evidence_spans=[s.to_dict() for s in spans],
                detail=f"extracted crown.status={crown.get('status')!r} but note evidence indicates {status!r}"))
            enum = schema["fields"]["crown"]["status"].get("enum", [])
            if status not in enum:
                out.append(Observation(
                    kind="schema_cannot_represent", stage="extraction", field="crown.status",
                    recorded=enum, evidence_value=status,
                    detail=f"schema {schema['id']} crown.status enum {enum} has no value for {status!r}"))
        material, mspans, reason = an.determinable_material()
        recorded_material = crown.get("material")
        if material is not None and recorded_material != material:
            out.append(Observation(
                kind="field_contradicted_by_note", stage="extraction", field="crown.material",
                recorded=recorded_material, evidence_value=material,
                evidence_spans=[s.to_dict() for s in mspans],
                detail=f"extracted crown.material={recorded_material!r} but note indicates {material!r} ({reason})"))
        elif material is None and recorded_material is not None and reason != "no_mention":
            out.append(Observation(
                kind="field_unsupported_by_note", stage="extraction", field="crown.material",
                recorded=recorded_material, evidence_value=None,
                evidence_spans=[s.to_dict() for s in mspans],
                detail=f"extracted crown.material={recorded_material!r} but note does not document a selection ({reason})"))

    fillings = an.fillings()
    filling = extraction.get("filling")
    if fillings and filling:
        ev = fillings[0]
        for fld in ("tooth", "surfaces", "status"):
            if filling.get(fld) != ev[fld]:
                out.append(Observation(
                    kind="field_contradicted_by_note", stage="extraction", field=f"filling.{fld}",
                    recorded=filling.get(fld), evidence_value=ev[fld], evidence_spans=[ev["span"].to_dict()],
                    detail=f"extracted filling.{fld}={filling.get(fld)!r}; note indicates {ev[fld]!r}"))
    xr = an.xray()
    if xr and extraction.get("xray") and extraction["xray"].get("count") != xr[0]:
        out.append(Observation(
            kind="field_contradicted_by_note", stage="extraction", field="xray.count",
            recorded=extraction["xray"].get("count"), evidence_value=xr[0],
            evidence_spans=[s.to_dict() for s in xr[1]],
            detail=f"extracted xray.count={extraction['xray'].get('count')} but note indicates {xr[0]} image(s)"))
    # Order mirrors code provenance: code-identity attributes (material) before billing attributes (status).
    rank = {"crown.material": 0, "crown.status": 1}
    return sorted(out, key=lambda o: (o.kind == "schema_cannot_represent", rank.get(o.field or "", 2)))


def inspect_coding(run: PipelineRun, registry: ConfigRegistry) -> list[Observation]:
    out: list[Observation] = []
    trace = run.trace("coding")
    inputs = trace.inputs
    if inputs.get("policy_source", "").startswith("default"):
        out.append(Observation(
            kind="policy_missing_at_runtime", stage="coding", field="policy",
            recorded=run.policy_ref, detail="no practice policy was attached to the pipeline run"))
        out.append(Observation(
            kind="policy_defaulted", stage="coding", field="policy",
            recorded=inputs.get("policy_source"), evidence_value=inputs.get("billing_event"),
            detail=f"coding silently defaulted to {inputs.get('billing_event')!r} billing without flagging"))
    replayed = run_coding(run.trace("extraction").output, inputs.get("billing_event"),
                          registry.rule_card(inputs.get("rule_card", "interview-v1")))
    if sorted(replayed["predicted_codes"]) == sorted(run.predicted_codes):
        out.append(Observation(
            kind="stage_reproduces_from_inputs", stage="coding",
            detail="re-running coding on its recorded inputs reproduces the recorded codes "
                   "(coding is consistent with the facts it was given)"))
    else:
        out.append(Observation(
            kind="stage_not_reproducible", stage="coding", recorded=run.predicted_codes,
            evidence_value=replayed["predicted_codes"],
            detail="coding output differs when re-run on recorded inputs (non-determinism or version drift)"))
    return out


def observe_run(note: SourceNote, run: PipelineRun, expected: list[str] | None,
                registry: ConfigRegistry) -> list[Observation]:
    obs: list[Observation] = []
    card = registry.rule_card()
    if expected is None:
        obs.append(Observation(kind="expected_codes_unknown", stage="output",
                               detail="no expected codes supplied; fixes cannot be verified automatically"))
    else:
        cmp = compare_codes(run.predicted_codes, expected)
        for code in cmp.false_positives:
            proc = card["codes"].get(code, {}).get("procedure", "unknown")
            obs.append(Observation(kind="code_false_positive", stage="output", field=proc, recorded=code,
                                   detail=f"{code} ({card['codes'].get(code, {}).get('description')}) predicted but not expected"))
        for code in cmp.false_negatives:
            proc = card["codes"].get(code, {}).get("procedure", "unknown")
            obs.append(Observation(kind="code_false_negative", stage="output", field=proc, recorded=code,
                                   detail=f"{code} ({card['codes'].get(code, {}).get('description')}) expected but not predicted"))
        if not cmp.exact_match and run.qa_passed:
            obs.append(Observation(kind="qa_passed_with_mismatch", stage="qa",
                                   detail="all automated QA checks passed although predicted codes are wrong"))
    obs.extend(inspect_extraction(note, run, registry))
    obs.extend(inspect_coding(run, registry))
    for i, o in enumerate(obs, 1):
        o.id = f"OBS-{i:02d}"
    return obs


def symptom_signature(observations: list[Observation]) -> str:
    """Deterministic, evidence-derived signature (NOT an embedding). Used for conservative grouping."""
    ignored = {"stage_reproduces_from_inputs"}
    return "|".join(sorted({o.symptom() for o in observations if o.kind not in ignored}))


def causes_from_observations(observations: list[Observation]) -> list[str]:
    causes: list[str] = []
    for o in observations:
        template = CAUSE_FOR_KIND.get(o.kind)
        if template:
            cause = template.format(field=o.field)
            if cause not in causes:
                causes.append(cause)
    return causes


def earliest_incorrect_stage(observations: list[Observation]) -> str | None:
    incorrect = {"field_contradicted_by_note", "field_unsupported_by_note", "policy_defaulted",
                 "policy_missing_at_runtime", "stage_not_reproducible", "qa_passed_with_mismatch"}
    for stage in ("extraction", "coding", "qa"):
        if any(o.stage == stage and o.kind in incorrect for o in observations):
            return stage
    return None


def observations_payload(observations: list[Observation]) -> list[dict[str, Any]]:
    return [o.model_dump(mode="json") for o in observations]
