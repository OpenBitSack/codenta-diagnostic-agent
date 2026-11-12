"""Regression evaluation + quality gate (including Demo E: a fix that helps one case and breaks another)."""
from __future__ import annotations

import pytest

from codenta_diag.domain import compare_codes
from codenta_diag.evaluation import apply_quality_gate, compare_summaries, evaluate, load_dataset
from codenta_diag.pipeline import production_config

FULL_BUNDLE = ["extraction-prompt-v2-selected-material", "extraction-schema-v2-crown-stage",
               "pipeline-policy-registry-resolution", "qa-v2-independent-policy-and-evidence-checks"]


@pytest.fixture(scope="module")
def ds():
    return load_dataset()


@pytest.fixture(scope="module")
def baseline(registry, ds):
    return evaluate(production_config(registry), ds, registry)


def _cand(registry, ds, change_ids):
    overrides = {}
    for cid in change_ids:
        overrides.update(registry.candidate_change(cid)["overrides"])
    return evaluate(production_config(registry).with_overrides(overrides, "cand"), ds, registry)


def test_dataset_covers_required_categories(ds):
    cats = {c for case in ds.cases for c in case.categories}
    required = {"xray", "filling_one_surface", "filling_two_surface", "crown_prep_today", "seat_billing",
                "prep_billing", "crown_seated_today", "gold_discussed_ceramic_selected", "multiple_materials_discussed",
                "multiple_procedures", "future_planned_procedures", "duplicate_xray_mentions",
                "ambiguous_documentation", "conflicting_documentation", "different_policy_versions",
                "overlapping_extraction_errors", "missing_policy"}
    assert required <= cats
    ids = {c.id for c in ds.cases}
    assert {"interview-note-seat-billing", "interview-note-prep-billing"} <= ids


def test_interview_case_labels_are_policy_dependent(ds):
    by_id = {c.id: c for c in ds.cases}
    assert by_id["interview-note-seat-billing"].expected_codes == ["D0220", "D2392"]
    assert by_id["interview-note-prep-billing"].expected_codes == ["D0220", "D2392", "D2740"]


def test_baseline_reproduces_the_business_problem(baseline):
    wrong = [c for c in baseline.cases if not c.exact]
    assert len(wrong) == 9
    assert baseline.escaped_errors == 9  # every wrong prediction passed QA
    assert baseline.policy_violations == 2
    assert 0 < baseline.f1 < 1 and baseline.by_category["seat_billing"]["n"] > 0


def test_full_bundle_passes_gate(registry, ds, baseline):
    cand = _cand(registry, ds, FULL_BUNDLE)
    report = compare_summaries(baseline, cand)
    gate = apply_quality_gate(report, compare_codes(["D0220", "D2392"], ["D0220", "D2392"]),
                              registry.agent_config()["quality_gate"])
    assert gate.passed and not report.regressions and len(report.improvements) == 9
    assert cand.f1 == 1.0 and cand.escaped_errors == 0 and cand.policy_violations == 0


def test_demo_e_last_mentioned_material_fix_is_rejected(registry, ds, baseline):
    """Fixes the interview note's material but breaks 'Pt chose PFM over all-ceramic'."""
    cand = _cand(registry, ds, ["extraction-prompt-last-mentioned-material"])
    report = compare_summaries(baseline, cand)
    gate = apply_quality_gate(report, None, registry.agent_config()["quality_gate"])
    assert not gate.passed
    assert "pfm-chosen-over-ceramic" in report.critical_regressions
    assert {c.name for c in gate.failed} >= {"no_regressions", "no_critical_regressions"}


def test_demo_e_default_seat_policy_patch_is_rejected(registry, ds, baseline):
    report = compare_summaries(baseline, _cand(registry, ds, ["coding-missing-policy-default-seat"]))
    assert "missing-policy-prep-practice-seated" in report.critical_regressions


def test_gate_requires_target_ticket_fixed(registry, ds, baseline):
    report = compare_summaries(baseline, baseline)
    gate = apply_quality_gate(report, compare_codes(["D0220", "D2392", "D2790"], ["D0220", "D2392"]),
                              registry.agent_config()["quality_gate"])
    assert not gate.passed and [c.name for c in gate.failed] == ["target_ticket_fixed"]
