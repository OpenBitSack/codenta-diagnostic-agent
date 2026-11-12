"""Unit tests: simulated production pipeline + rule card + note analyzer."""
from __future__ import annotations

import json

import pytest

from codenta_diag.domain import SourceNote, compare_codes
from codenta_diag.pipeline import production_config, run_pipeline
from codenta_diag.pipeline.lexicon import NoteAnalyzer
from codenta_diag.settings import PROJECT_ROOT

from .conftest import INTERVIEW_NOTE


def _run(registry, text, policy, practice="P-SEAT-01", overrides=None):
    cfg = production_config(registry)
    if overrides:
        cfg = cfg.with_overrides(overrides, "test")
    return run_pipeline(SourceNote(note_id="N", practice_id=practice, text=text), policy, cfg, registry, run_id="R")


def test_simulated_v13_reproduces_interview_trace_exactly(registry):
    fixture = json.loads((PROJECT_ROOT / "data" / "fixtures" / "interview_case.json").read_text())
    run = _run(registry, fixture["note"], "P-SEAT-01@v3")
    assert run.trace("extraction").output == fixture["intermediate_extraction"]
    assert run.predicted_codes == fixture["pipeline_prediction"]
    assert run.qa_passed is fixture["qa_passed"]


def test_candidate_config_is_policy_dependent(registry):
    cand = {"extraction_schema": "v2", "extraction_prompt": "extract-v2-selected"}
    seat = _run(registry, INTERVIEW_NOTE, "P-SEAT-01@v3", overrides=cand)
    prep = _run(registry, INTERVIEW_NOTE, "P-PREP-02@v1", practice="P-PREP-02", overrides=cand)
    assert sorted(seat.predicted_codes) == ["D0220", "D2392"]
    assert sorted(prep.predicted_codes) == ["D0220", "D2392", "D2740"]


@pytest.mark.parametrize("text,expected", [
    ("#30 O composite filling done today.", ["D2391"]),
    ("#19 MO composite filling done today.", ["D2392"]),
    ("Adult prophy completed. 1 PA taken of #14.", ["D0220", "D1110"]),
    ("1 PA taken. #20 DO composite scheduled for next visit.", ["D0220"]),
    ("PA of #3 taken. Reviewed PA with pt; no caries noted.", ["D0220"]),
])
def test_rule_card_codes(registry, text, expected):
    assert sorted(_run(registry, text, "P-SEAT-01@v3").predicted_codes) == sorted(expected)


def test_note_analyzer_selection_and_conflict(registry):
    lex = registry.lexicon()
    an = NoteAnalyzer("#14 crown seated today. Pt chose PFM over all-ceramic.", lex)
    material, spans, reason = an.determinable_material()
    assert (material, reason) == ("pfm", "selection_cue")
    assert an.material_mentions()[-1][0] == "all_ceramic"  # why 'last mentioned' is a bad heuristic
    status, _ = NoteAnalyzer("#30 crown seated today. Seat #30 in 2 wks.", lex).clinical_crown_status()
    assert status is None  # contradictory stage documentation is not resolved silently


def test_interview_note_evidence_spans_point_into_note(registry):
    an = NoteAnalyzer(INTERVIEW_NOTE, registry.lexicon())
    status, spans = an.clinical_crown_status()
    assert status == "prepared"
    assert {INTERVIEW_NOTE[s.start:s.end] for s in spans} >= {"Prepped", "temp placed", "Seat #14 in 2 wks"}


def test_compare_codes_is_multiset():
    cmp = compare_codes(["D0220", "D0220"], ["D0220"])
    assert cmp.false_positives == ["D0220"] and not cmp.exact_match


def test_unknown_override_rejected(registry):
    with pytest.raises(ValueError):
        production_config(registry).with_overrides({"not_a_component": "x"}, "bad")
