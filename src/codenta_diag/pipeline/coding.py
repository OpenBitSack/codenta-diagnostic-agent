"""SIMULATED coding stage: maps extracted facts to CDT codes using the versioned rule card.

Generic over procedures/codes: adding a code or a multi-visit procedure is a rule-card change.
"""
from __future__ import annotations

from typing import Any

FIELD_PATHS = {
    "image_type": "type",
    "material": "material",
    "region": "tooth",
    "surface_count": "surfaces",
    "patient": None,
}


def _facts(procedure: str, data: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    if procedure == "xray":
        return {"image_type": data.get("type")}
    if procedure == "prophylaxis":
        return {"patient": "adult"}  # synthetic assumption: the card only defines adult cleaning
    if procedure == "filling":
        region = "posterior" if data.get("tooth") in card["posterior_teeth"] else "anterior"
        return {"material": data.get("material"), "region": region,
                "surface_count": len(data.get("surfaces") or [])}
    return {"material": data.get("material")}


def _billable(procedure: str, data: dict[str, Any], card: dict[str, Any],
              billing_event: str | None) -> tuple[bool, str | None]:
    billing = card["procedures"][procedure]["billing"]
    status = data.get("status")
    if billing["mode"] == "current_visit":
        allowed = billing.get("billable_statuses")
        return (allowed is None or status in allowed), None
    if billing_event is None:
        return False, f"{procedure}: no billing policy available; held for review"
    return status in billing["event_statuses"][billing_event], None


def run_coding(extraction: dict[str, Any], billing_event: str | None, card: dict[str, Any]) -> dict[str, Any]:
    codes: list[dict[str, Any]] = []
    flags: list[str] = []
    for procedure in ("xray", "prophylaxis", "filling", "crown"):
        data = extraction.get(procedure)
        if not data:
            continue
        billable, flag = _billable(procedure, data, card, billing_event)
        if flag:
            flags.append(flag)
        if not billable:
            continue
        facts = _facts(procedure, data, card)
        matches = [
            (code, spec) for code, spec in card["codes"].items()
            if spec["procedure"] == procedure
            and all(facts.get(k) == v for k, v in spec["match"].items())
        ]
        if not matches:
            flags.append(f"{procedure}: no rule-card code matches facts {facts}")
            continue
        code, spec = matches[0]
        derived = [f"{procedure}.{FIELD_PATHS[k]}" for k in spec["match"] if FIELD_PATHS.get(k)]
        if "status" in data:
            derived.append(f"{procedure}.status")
        codes.append({"code": code, "procedure": procedure, "derived_from": derived})
        if spec.get("per_image") and (data.get("count") or 1) > 1:
            flags.append(f"{procedure}: {data['count']} images; additional images not on rule card")
    return {"codes": codes, "predicted_codes": [c["code"] for c in codes], "flags": flags}
