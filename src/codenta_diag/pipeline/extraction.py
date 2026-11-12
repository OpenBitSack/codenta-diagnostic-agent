"""SIMULATED extraction stage.

In production this is an LLM call (prompt + output schema). Here it is a deterministic function whose
behaviour is parameterised by `schema_id` and `prompt_id` so that schema-level and prompt-level
defects can be reproduced and tested in the sandbox. It is not, and is not presented as, an LLM.

Modelled prompt behaviours (synthetic assumptions):
  extract-v1             material = first crown material mentioned (discussed != selected confusion)
  extract-v2-selected    material = material the patient selected; null if not documented
  extract-last-mentioned material = last crown material mentioned (naive patch; used in Demo E)
"""
from __future__ import annotations

from typing import Any

from ..config_registry import ConfigRegistry
from .lexicon import NoteAnalyzer

PROMPTS = {"extract-v1", "extract-v2-selected", "extract-last-mentioned"}


def _project(value: str | None, field_path: str, schema: dict[str, Any]) -> str | None:
    """Project a clinical value onto the schema enum (models an LLM forced into a closed enum)."""
    if value is None:
        return None
    group, name = field_path.split(".")
    enum = schema["fields"][group][name].get("enum")
    if enum is None or value in enum:
        return value
    return (schema.get("unrepresentable_projection") or {}).get(field_path, {}).get(value)


def _crown_material(an: NoteAnalyzer, prompt_id: str) -> str | None:
    mentions = an.material_mentions()
    if prompt_id == "extract-v1":
        return mentions[0][0] if mentions else None
    if prompt_id == "extract-last-mentioned":
        return mentions[-1][0] if mentions else None
    if prompt_id == "extract-v2-selected":
        material, _, _ = an.determinable_material()
        return material
    raise ValueError(f"Unknown extraction prompt {prompt_id!r}")


def run_extraction(note_text: str, schema_id: str, prompt_id: str, registry: ConfigRegistry,
                   lexicon_id: str = "v1") -> dict[str, Any]:
    if prompt_id not in PROMPTS:
        raise ValueError(f"Unknown extraction prompt {prompt_id!r}")
    schema = registry.extraction_schema(schema_id)
    an = NoteAnalyzer(note_text, registry.lexicon(lexicon_id))
    out: dict[str, Any] = {}

    if (xr := an.xray()) is not None:
        out["xray"] = {"type": "PA", "count": xr[0]}

    if an.prophylaxis():
        out["prophylaxis"] = {"performed": True}

    fillings = an.fillings()
    if fillings:
        f = fillings[0]  # prototype limitation: one instance per procedure family
        out["filling"] = {k: f[k] for k in ("tooth", "surfaces", "material", "status")}

    if an.crown_tooth is not None:
        clinical_status, _ = an.clinical_crown_status()
        crown: dict[str, Any] = {
            "tooth": an.crown_tooth[0],
            "status": _project(clinical_status, "crown.status", schema),
        }
        if "materials_discussed" in schema["fields"]["crown"]:
            seen: list[str] = []
            for m, _ in an.material_mentions():
                if m not in seen:
                    seen.append(m)
            crown["materials_discussed"] = seen
        crown["material"] = _crown_material(an, prompt_id)
        out["crown"] = crown
    return out
