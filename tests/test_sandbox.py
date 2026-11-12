"""Sandbox replay engine: the four required demonstrations + reproducibility + isolation."""
from __future__ import annotations

import copy

import pytest

from codenta_diag.domain import SourceNote
from codenta_diag.pipeline import production_config, run_pipeline
from codenta_diag.sandbox import ReplayRequest, SandboxReplayEngine

from .conftest import INTERVIEW_NOTE

SEAT_EXPECTED = ["D0220", "D2392"]


@pytest.fixture
def setup(registry):
    note = SourceNote(note_id="N-1001", practice_id="P-SEAT-01", text=INTERVIEW_NOTE)
    cfg = production_config(registry)
    run = run_pipeline(note, "P-SEAT-01@v3", cfg, registry, run_id="RUN-1001")
    return SandboxReplayEngine(registry), note, run, cfg


def test_1_original_failure_reproduces_d2790(setup):
    engine, note, run, cfg = setup
    res = engine.replay(note, run, cfg, ReplayRequest(label="baseline"), SEAT_EXPECTED)
    assert "D2790" in res.predicted_codes
    assert res.comparison.false_positives == ["D2790"]
    assert res.qa_passed  # the production QA did not catch it


def test_2_material_only_correction_is_insufficient(setup):
    engine, note, run, cfg = setup
    res = engine.replay(note, run, cfg, ReplayRequest(label="material", field_overrides={
        "crown.material": "all_ceramic"}), SEAT_EXPECTED)
    assert res.predicted_codes == ["D0220", "D2392", "D2740"]
    assert not res.comparison.exact_match and res.comparison.false_positives == ["D2740"]


def test_3_status_and_policy_handling_remove_crown_from_this_visit(setup):
    engine, note, run, cfg = setup
    res = engine.replay(note, run, cfg, ReplayRequest(label="status+material", field_overrides={
        "crown.material": "all_ceramic", "crown.status": "prepared"}), SEAT_EXPECTED)
    assert res.comparison.exact_match and res.predicted_codes == SEAT_EXPECTED


def test_4_prep_billing_policy_produces_d2740(setup):
    engine, note, run, cfg = setup
    res = engine.replay(note, run, cfg, ReplayRequest(
        label="prep counterfactual", field_overrides={"crown.material": "all_ceramic", "crown.status": "prepared"},
        billing_event_override="prep", expected_override=["D0220", "D2392", "D2740"],
        expected_source="derived_counterfactual"), SEAT_EXPECTED)
    assert res.comparison.exact_match and "D2740" in res.predicted_codes
    assert res.coding_inputs["policy_source"] == "sandbox_counterfactual"


def test_candidate_components_rerun_extraction(setup):
    engine, note, run, cfg = setup
    res = engine.replay(note, run, cfg, ReplayRequest(label="v2", from_stage="extraction", candidate_change_ids=[
        "extraction-schema-v2-crown-stage", "extraction-prompt-v2-selected-material"]), SEAT_EXPECTED)
    assert res.extraction["crown"]["status"] == "prepared"
    assert res.extraction["crown"]["material"] == "all_ceramic"
    assert res.components["extraction_schema"] == "v2" and res.comparison.exact_match


def test_experiment_ids_are_deterministic_and_cached(setup, registry):
    engine, note, run, cfg = setup
    req = ReplayRequest(label="a", field_overrides={"crown.status": "prepared"})
    first = engine.replay(note, run, cfg, req, SEAT_EXPECTED)
    second = engine.replay(note, run, cfg, req.model_copy(update={"label": "b"}), SEAT_EXPECTED)
    assert first.experiment_id == second.experiment_id and second.cached and not first.cached
    fresh = SandboxReplayEngine(registry).replay(note, run, cfg, req, SEAT_EXPECTED)
    assert fresh.experiment_id == first.experiment_id  # reproducible across processes
    assert first.config_fingerprint == registry.fingerprint()


def test_replay_never_mutates_recorded_trace(setup):
    engine, note, run, cfg = setup
    before = copy.deepcopy(run.model_dump())
    engine.replay(note, run, cfg, ReplayRequest(label="x", field_overrides={"crown.status": "prepared"}), None)
    assert run.model_dump() == before


def test_candidate_configs_are_always_sandbox(setup):
    engine, _, _, cfg = setup
    cand = engine.build_config(cfg, ["qa-v2-independent-policy-and-evidence-checks"], "c")
    assert cand.sandbox is True and cfg.sandbox is False and cfg.qa == "qa-v1"
