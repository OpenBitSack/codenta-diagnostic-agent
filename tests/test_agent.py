"""Diagnostic agent: verdicts for the crown case, budgets/stopping, evidence verification, Claude tool loop."""
from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from codenta_diag.agent import AgentBudget, AgentController, DeterministicReasoner, ToolContext
from codenta_diag.agent.reasoner_base import ReasonerOutput, ToolCall
from codenta_diag.agent.reasoner_claude import ClaudeReasoner
from codenta_diag.domain import SourceNote
from codenta_diag.evaluation import load_dataset
from codenta_diag.evaluation.runner import RegressionRunner
from codenta_diag.pipeline import production_config, run_pipeline
from codenta_diag.sandbox import SandboxReplayEngine

from .conftest import INTERVIEW_NOTE

FULL_BUNDLE = ["extraction-prompt-v2-selected-material", "extraction-schema-v2-crown-stage",
               "pipeline-policy-registry-resolution", "qa-v2-independent-policy-and-evidence-checks"]


def make_ctx(registry, mode: str = "seat", note_text: str = INTERVIEW_NOTE, expected: list[str] | None = None,
             policy: str | None = "default") -> ToolContext:
    practice, pol, exp = (("P-SEAT-01", "P-SEAT-01@v3", ["D0220", "D2392"]) if mode == "seat" else
                          ("P-PREP-02", "P-PREP-02@v1", ["D0220", "D2392", "D2740"]))
    pol = pol if policy == "default" else policy
    note = SourceNote(note_id="N-T", practice_id=practice, text=note_text)
    cfg = production_config(registry)
    run = run_pipeline(note, pol, cfg, registry, run_id="RUN-T")
    return ToolContext(investigation_id="INV-T", practice_id=practice, note=note, run=run,
                       expected=exp if expected is None else expected, run_policy=registry.policy_from_ref(pol),
                       registry_policy=registry.policy(practice), base_config=cfg, registry=registry,
                       engine=SandboxReplayEngine(registry), regression=RegressionRunner(registry, load_dataset()),
                       gate_cfg=registry.agent_config()["quality_gate"])


def run_agent(registry, ctx: ToolContext, reasoner=None, **budget) -> Any:
    b = AgentBudget.from_config(registry.agent_config(), **budget)
    return AgentController(ctx, reasoner or DeterministicReasoner(), b).run()


def h(state, cause):
    return next(x for x in state.hypotheses if x.cause_id == cause)


def test_demo_a_crown_case_multi_cause_diagnosis(registry):
    state = run_agent(registry, make_ctx(registry, "seat"))
    assert state.earliest_incorrect_stage == "extraction"
    material = h(state, "extraction.crown.material.value_incorrect")
    status = h(state, "extraction.crown.status.value_incorrect")
    assert (material.label, status.label) == ("A", "B")
    assert material.status == "supported" and material.roles == ["latent"]
    assert status.status == "supported" and {"sufficient", "necessary"} <= set(status.roles)
    coding = h(state, "coding.policy.not_applied")
    assert coding.label == "C" and coding.status == "refuted" and coding.refuting_experiments
    qa = h(state, "qa.missed_error")
    assert qa.label == "D" and qa.status == "supported"
    assert h(state, "extraction.schema.crown.status.unrepresentable_value").status == "supported"
    assert h(state, "model.capability").status == "untestable"

    # The loop: material-only correction left the error in place, and the agent kept going.
    material_only = state.experiment_by_label("single:crown.material")
    assert material_only["predicted_codes"] == ["D0220", "D2392", "D2740"]
    assert not material_only["comparison"]["exact_match"]
    assert any("error in place" in d.rationale for d in state.decisions)

    # Fix search: naive candidates rejected by the gate, then a validated bundle.
    assert [a["gate_passed"] for a in state.fix_attempts] == [False, False, True]
    assert "pfm-chosen-over-ceramic" in state.fix_attempts[0]["critical_regressions"]
    assert state.validated_fix["change_ids"] == FULL_BUNDLE
    assert state.phase == "done" and state.budget.llm_calls == 0 and state.budget.tokens_in == 0


def test_demo_b_prep_policy_material_becomes_necessary(registry):
    state = run_agent(registry, make_ctx(registry, "prep"))
    assert "necessary" in h(state, "extraction.crown.material.value_incorrect").roles
    assert "necessary" in h(state, "extraction.crown.status.value_incorrect").roles
    assert h(state, "coding.policy.not_applied").status == "refuted"
    combined = state.experiment_by_label("combined:all-fields")
    assert combined["predicted_codes"] == ["D0220", "D2392", "D2740"] and combined["comparison"]["exact_match"]
    assert state.validated_fix is not None


def test_missing_policy_case_has_different_root_causes(registry):
    ctx = make_ctx(registry, "seat", note_text="#5 all-ceramic crown seated today, cemented.",
                   expected=["D2740"], policy=None)
    state = run_agent(registry, ctx)
    causes = {x.cause_id: x.status for x in state.hypotheses}
    assert not any(c.startswith("extraction.") for c in causes)  # extraction is consistent with the note
    assert causes["input.policy.missing_at_runtime"] == "supported"
    assert causes["coding.policy.silent_default_when_missing"] == "supported"
    assert causes["coding.policy.not_applied"] == "refuted"
    assert state.earliest_incorrect_stage == "coding"


@pytest.mark.parametrize("budget,dimension", [
    ({"max_experiments": 3}, "experiments"),
    ({"max_reasoning_steps": 4}, "reasoning_steps"),
    ({"deadline_seconds": 0.0}, "deadline"),
    ({"max_regression_runs": 1}, "regression_runs"),
])
def test_budget_exhaustion_escalates_with_evidence(registry, budget, dimension):
    state = run_agent(registry, make_ctx(registry, "seat"), **budget)
    assert state.phase == "escalated"
    assert state.escalation["reason"] == f"budget_exhausted:{dimension}"
    assert state.escalation["questions_for_human"]
    assert len(state.escalation["already_done_do_not_repeat"]) == len(state.experiments)
    assert state.budget.experiments <= budget.get("max_experiments", 99)


def test_label_documentation_conflict_escalates(registry):
    ctx = make_ctx(registry, "seat", note_text="#3 crown seated today. Discussed gold vs PFM; material not documented.",
                   expected=["D2750"])
    state = run_agent(registry, ctx)
    assert state.phase == "escalated" and state.escalation["reason"] == "label_documentation_conflict"
    assert any("lab Rx" in q for q in state.escalation["questions_for_human"])
    assert state.experiment_by_label("label-implied:D2750")["comparison"]["exact_match"]
    assert state.fix_attempts == []  # no automated fix attempts on a label conflict


class LyingReasoner:
    """Claims a cause is supported, citing an experiment that never ran."""
    kind, is_llm = "test-liar", False

    def __init__(self) -> None:
        self.step = 0

    def next_actions(self, state, ctx):
        self.step += 1
        if self.step == 1:
            return ReasonerOutput([ToolCall("record_hypotheses", {"hypotheses": [
                {"cause_id": "coding.policy.not_applied", "stage": "coding", "statement": "coding bug"}]})])
        return ReasonerOutput([ToolCall("submit_diagnosis", {"earliest_incorrect_stage": "coding", "summary": "x",
                                                             "verdicts": [{"hypothesis_id": "H-A", "status": "supported",
                                                                           "supporting_experiments": ["EXP-made-up"],
                                                                           "rationale": "trust me"}]})])

    def record_results(self, state, results):
        pass


def test_controller_downgrades_unsupported_claims(registry):
    state = run_agent(registry, make_ctx(registry, "seat"), reasoner=LyingReasoner())
    hyp = state.hypotheses[0]
    assert hyp.status == "inconclusive"
    assert "EXP-made-up" in hyp.verification_note
    assert state.phase == "escalated"  # no confirmed cause -> no fix generation
    assert state.diagnosis["earliest_incorrect_stage"] == "extraction"  # controller's observation wins


# ---------------------------------------------------------------- Claude reasoner (mocked client)
def _block(kind: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **kw)


class FakeAnthropic:
    """Scripted stand-in for anthropic.Anthropic().messages. No network calls."""

    def __init__(self, script: list[list[SimpleNamespace]]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(copy.deepcopy(kwargs))
        content = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        return SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


def claude_script() -> list[list[SimpleNamespace]]:
    return [
        [_block("text", text="Two extraction fields contradict the note; I will test them separately."),
         _block("tool_use", id="t1", name="record_hypotheses", input={"hypotheses": [
             {"cause_id": "extraction.crown.material.value_incorrect", "stage": "extraction", "statement": "material"},
             {"cause_id": "extraction.crown.status.value_incorrect", "stage": "extraction", "statement": "status"},
             {"cause_id": "qa.missed_error", "stage": "qa", "statement": "qa gap"}]})],
        [_block("tool_use", id="t2", name="run_sandbox_replay",
                input={"label": "material only", "field_overrides": {"crown.material": "all_ceramic"},
                       "tests_hypotheses": ["H-A"]})],
        [_block("text", text="Error remains (D2740). Testing status."),
         _block("tool_use", id="t3", name="run_sandbox_replay",
                input={"label": "status only", "field_overrides": {"crown.status": "prepared"},
                       "tests_hypotheses": ["H-B"]})],
        [_block("tool_use", id="t4", name="submit_diagnosis", input={
            "earliest_incorrect_stage": "extraction", "summary": "status error drives today's failure",
            "verdicts": [
                {"hypothesis_id": "H-B", "status": "supported", "roles": ["sufficient"],
                 "supporting_experiments": ["__STATUS__"], "rationale": "status-only replay matches label"},
                {"hypothesis_id": "H-A", "status": "supported", "roles": ["latent"],
                 "supporting_experiments": [], "rationale": "no citation"},
                {"hypothesis_id": "H-C", "status": "supported", "supporting_observations": ["OBS-02"],
                 "rationale": "QA passed a wrong prediction"}]})],
    ]


class PatchingFake(FakeAnthropic):
    """Substitutes the real experiment id (unknown until runtime) into the scripted verdict."""

    def create(self, **kwargs: Any) -> SimpleNamespace:
        resp = super().create(**kwargs)
        for b in resp.content:
            if b.type == "tool_use" and b.name == "submit_diagnosis":
                msgs = kwargs["messages"]
                exp_id = next(r["content"].split('"experiment_id": "')[1].split('"')[0]
                              for m in reversed(msgs) if m["role"] == "user" and isinstance(m["content"], list)
                              for r in m["content"] if '"label": "status only"' in r["content"])
                for v in b.input["verdicts"]:
                    v["supporting_experiments"] = [exp_id if e == "__STATUS__" else e for e in
                                                   v.get("supporting_experiments", [])]
        return resp


def test_claude_reasoner_tool_loop_with_mocked_client(registry):
    fake = PatchingFake(claude_script())
    reasoner = ClaudeReasoner(fake, model="test-model")
    state = run_agent(registry, make_ctx(registry, "seat"), reasoner=reasoner)
    assert len(fake.calls) == 4 and state.budget.llm_calls == 4
    assert state.budget.tokens_in == 4000 and state.budget.tokens_out == 800 and state.budget.cost_usd > 0
    assert {t["name"] for t in fake.calls[0]["tools"]} >= {"run_sandbox_replay", "submit_diagnosis",
                                                           "create_escalation_report", "get_original_note"}
    assert all(x.origin == "llm" for x in state.hypotheses)
    assert h(state, "extraction.crown.status.value_incorrect").status == "supported"
    # Claimed supported without evidence -> downgraded by the controller.
    material = h(state, "extraction.crown.material.value_incorrect")
    assert material.status == "inconclusive" and material.verification_note
    assert h(state, "qa.missed_error").status == "supported"
    # Every tool_use got a tool_result in the next user turn (API contract).
    second = fake.calls[1]["messages"]
    assert second[-1]["role"] == "user" and second[-1]["content"][0]["tool_use_id"] == "t1"
    assert state.validated_fix is not None  # deterministic fix validation still runs after the LLM diagnosis


def test_claude_reasoner_respects_token_budget(registry):
    script = [[_block("tool_use", id=f"x{i}", name="get_predicted_codes", input={})] for i in range(50)]
    state = run_agent(registry, make_ctx(registry, "seat"), reasoner=ClaudeReasoner(FakeAnthropic(script), "m"),
                      max_tokens=3000)
    assert state.phase == "escalated" and state.escalation["reason"] == "budget_exhausted:tokens"
    assert state.budget.llm_calls == 3


@pytest.mark.parametrize("usage,need,dimension", [
    ({"reasoning_steps": 40}, None, "reasoning_steps"),
    ({"tokens_in": 100_000, "tokens_out": 20_000}, None, "tokens"),
    ({"cost_usd": 1.0}, None, "cost"),
    ({"experiments": 24}, "experiment", "experiments"),
    ({"regression_runs": 10}, "regression", "regression_runs"),
    ({"elapsed_before_resume": 601.0}, None, "deadline"),
])
def test_budget_check_every_dimension(registry, usage, need, dimension):
    from codenta_diag.agent import AgentBudget
    from codenta_diag.agent.budget import BudgetExceeded
    from codenta_diag.agent.state import BudgetUsage
    budget = AgentBudget.from_config(registry.agent_config())
    budget.check(BudgetUsage(), need=need)
    with pytest.raises(BudgetExceeded) as exc:
        budget.check(BudgetUsage(**usage), need=need)
    assert exc.value.dimension == dimension
