"""SIMULATED automated QA stage. Rules are selected by the versioned QA rule set."""
from __future__ import annotations

from typing import Any, Callable

from .lexicon import NoteAnalyzer


class QAContext:
    def __init__(self, note_text: str, extraction: dict[str, Any], coding: dict[str, Any],
                 billing_event: str | None, policy_source: str, card: dict[str, Any],
                 lexicon: dict[str, Any]) -> None:
        self.note = NoteAnalyzer(note_text, lexicon)
        self.extraction = extraction
        self.codes: list[str] = coding["predicted_codes"]
        self.billing_event = billing_event
        self.policy_source = policy_source
        self.card = card

    def crown_codes(self) -> list[str]:
        return [c for c in self.codes if self.card["codes"][c]["procedure"] == "crown"]


Result = tuple[bool, str]


def code_in_rule_card(ctx: QAContext) -> Result:
    bad = [c for c in ctx.codes if c not in ctx.card["codes"]]
    return not bad, f"unknown codes {bad}" if bad else "all codes on rule card"


def tooth_in_adult_range(ctx: QAContext) -> Result:
    lo, hi = ctx.card["adult_teeth"]["min"], ctx.card["adult_teeth"]["max"]
    bad = [v["tooth"] for v in ctx.extraction.values()
           if isinstance(v, dict) and "tooth" in v and not lo <= v["tooth"] <= hi]
    return not bad, f"teeth out of range {bad}" if bad else "teeth within 1-32"


def filling_surface_count_matches_code(ctx: QAContext) -> Result:
    filling = ctx.extraction.get("filling")
    for c in ctx.codes:
        spec = ctx.card["codes"][c]
        if spec["procedure"] == "filling":
            n = len((filling or {}).get("surfaces") or [])
            if spec["match"]["surface_count"] != n:
                return False, f"{c} implies {spec['match']['surface_count']} surfaces, extraction has {n}"
    return True, "filling surface counts consistent"


def xray_codes_within_image_count(ctx: QAContext) -> Result:
    n_codes = ctx.codes.count("D0220")
    n_images = (ctx.extraction.get("xray") or {}).get("count", 0)
    ok = n_codes <= max(n_images, 0)
    return ok, f"{n_codes} D0220 for {n_images} image(s)"


def policy_present(ctx: QAContext) -> Result:
    ok = ctx.policy_source in ("run_input", "registry", "sandbox_counterfactual")
    return ok, f"policy source: {ctx.policy_source}"


def crown_billing_event_note_evidence(ctx: QAContext) -> Result:
    """Independent check: re-reads the NOTE (not the extraction) for the crown's clinical stage."""
    if ctx.note.crown_tooth is None:
        return True, "no crown in note"
    status, _ = ctx.note.clinical_crown_status()
    if ctx.billing_event is None or status is None:
        return True, "crown stage or policy unknown; deferred to policy_present"
    statuses = ctx.card["procedures"]["crown"]["billing"]["event_statuses"][ctx.billing_event]
    billable = status in statuses
    if ctx.crown_codes() and not billable:
        return False, f"crown billed but note indicates '{status}' and policy bills at {ctx.billing_event}"
    material, _, _ = ctx.note.determinable_material()
    if not ctx.crown_codes() and billable and material is not None:
        return False, f"note indicates billable '{status}' crown under {ctx.billing_event} policy but no crown code"
    return True, f"crown billing consistent with note stage '{status}' and {ctx.billing_event} policy"


def crown_material_note_evidence(ctx: QAContext) -> Result:
    crown_codes = ctx.crown_codes()
    if not crown_codes:
        return True, "no crown code billed"
    material, _, reason = ctx.note.determinable_material()
    if material is None:
        return False, f"crown billed but selected material not determinable from note ({reason})"
    billed = ctx.card["codes"][crown_codes[0]]["match"]["material"]
    if billed != material:
        return False, f"crown billed as {billed} but note indicates {material} ({reason})"
    return True, f"crown material {material} supported by note ({reason})"


RULES: dict[str, Callable[[QAContext], Result]] = {
    f.__name__: f
    for f in (
        code_in_rule_card,
        tooth_in_adult_range,
        filling_surface_count_matches_code,
        xray_codes_within_image_count,
        policy_present,
        crown_billing_event_note_evidence,
        crown_material_note_evidence,
    )
}


def run_qa(ctx: QAContext, rule_ids: list[str]) -> dict[str, Any]:
    checks = []
    for rid in rule_ids:
        ok, msg = RULES[rid](ctx)
        checks.append({"rule_id": rid, "passed": ok, "message": msg})
    return {"passed": all(c["passed"] for c in checks), "checks": checks}
