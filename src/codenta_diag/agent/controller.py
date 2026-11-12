"""Bounded diagnostic agent loop: Observe -> Hypothesize -> Test -> Evaluate -> Decide.

The controller owns everything that must be trustworthy regardless of which reasoner is plugged in:
budgets, tool execution, checkpointing, audit records, and verification that every "supported"
claim cites experiments that actually ran. Reasoners (deterministic demo or Claude) only choose
the next action.
"""
from __future__ import annotations

import logging
import string
import time
from typing import Any, Callable

from pydantic import ValidationError

from .budget import AgentBudget, BudgetExceeded
from .fixes import generate_and_validate_fixes
from .observations import earliest_incorrect_stage, observations_payload, symptom_signature
from .reasoner_base import Reasoner, ToolCall
from .report import build_escalation
from .state import Decision, Hypothesis, InvestigationState, ToolCallRecord
from .tools import TOOLS, NoArgs, InspectArgs, SimilarArgs, ToolContext

log = logging.getLogger(__name__)

Checkpoint = Callable[[InvestigationState], None]
Audit = Callable[[str, dict[str, Any]], None]

OBSERVE_TOOLS: list[tuple[str, Any]] = [
    ("get_original_note", NoArgs()),
    ("get_practice_policy", NoArgs()),
    ("get_pipeline_traces", NoArgs()),
    ("get_predicted_codes", NoArgs()),
    ("get_expected_codes", NoArgs()),
    ("find_similar_incidents", SimilarArgs(limit=5)),
    ("inspect_stage_output", InspectArgs(stage="extraction")),
    ("inspect_stage_output", InspectArgs(stage="coding")),
    ("inspect_stage_output", InspectArgs(stage="qa")),
]


def _summarize(result: dict[str, Any]) -> dict[str, Any]:
    """Audit-safe summary: never copies note text into logs/audit."""
    if "experiment_id" in result:
        cmp = result.get("comparison") or {}
        return {"experiment_id": result["experiment_id"], "predicted_codes": result["predicted_codes"],
                "exact_match": cmp.get("exact_match"), "cached": result.get("cached")}
    if "gate" in result:
        return {"bundle": result["candidate_change_ids"], "gate_passed": result["gate"]["passed"],
                "regressions": [r["case_id"] for r in result["regression_report"]["regressions"]]}
    if "text" in result:
        return {"note_id": result.get("note_id"), "chars": len(result["text"])}
    return {k: v for k, v in result.items() if k in ("stage", "predicted_codes", "expected_codes", "error")}


class AgentController:
    def __init__(self, ctx: ToolContext, reasoner: Reasoner, budget: AgentBudget,
                 checkpoint: Checkpoint | None = None, audit: Audit | None = None) -> None:
        self.ctx = ctx
        self.reasoner = reasoner
        self.budget = budget
        self._checkpoint = checkpoint or (lambda s: None)
        self._audit = audit or (lambda a, d: None)

    # ------------------------------------------------------------------ state helpers
    def new_state(self) -> InvestigationState:
        return InvestigationState(investigation_id=self.ctx.investigation_id, reasoner=self.reasoner.kind)

    def checkpoint(self, state: InvestigationState) -> None:
        state.budget.elapsed_at_checkpoint = round(self.budget.elapsed(state.budget), 3)
        self._checkpoint(state)

    def _decide(self, state: InvestigationState, action: str, rationale: str, phase: str | None = None) -> None:
        state.decisions.append(Decision(step=state.budget.reasoning_steps, phase=phase or state.phase,
                                        action=action, rationale=rationale))

    # ------------------------------------------------------------------ deterministic observe phase
    def observe(self, state: InvestigationState) -> None:
        for name, args in OBSERVE_TOOLS:
            result = TOOLS[name].fn(self.ctx, args)  # type: ignore[misc]
            state.tool_calls.append(ToolCallRecord(step=0, tool=name, args=args.model_dump(),
                                                   result_summary=_summarize(result)))
            self._audit("tool_call", {"tool": name, "args": args.model_dump()})
        obs = self.ctx.observations()
        state.observations = observations_payload(obs)
        state.symptom_signature = symptom_signature(obs)
        state.earliest_incorrect_stage = earliest_incorrect_stage(obs)
        state.phase = "hypothesize"
        self._decide(state, "observe", f"{len(obs)} deterministic observations; earliest observable incorrect "
                     f"stage: {state.earliest_incorrect_stage}. No reasoning step spent.", "observe")
        self.checkpoint(state)

    # ------------------------------------------------------------------ tool execution
    def execute_tool(self, state: InvestigationState, call: ToolCall) -> dict[str, Any]:
        spec = TOOLS.get(call.name)
        if spec is None or spec.fn is None:
            return {"error": f"unknown or non-executable tool {call.name!r}"}
        try:
            args = spec.args_model(**call.args)
        except ValidationError as exc:
            return {"error": f"invalid arguments: {exc.errors(include_url=False)}"}
        if spec.cost in ("experiment", "regression"):
            self.budget.check(state.budget, need=spec.cost)
        result = spec.fn(self.ctx, args)
        if spec.cost == "experiment":
            state.budget.experiments += 1
            result["tests_hypotheses"] = call.args.get("tests_hypotheses", [])
            result["step"] = state.budget.reasoning_steps
            state.experiments.append(result)
        elif spec.cost == "regression":
            state.budget.regression_runs += 1
        state.tool_calls.append(ToolCallRecord(step=state.budget.reasoning_steps, tool=call.name,
                                               args={k: v for k, v in call.args.items() if k != "expected_override"},
                                               result_summary=_summarize(result)))
        self._audit("tool_call", {"tool": call.name, "label": call.args.get("label"),
                                  "result": _summarize(result)})
        return result

    # ------------------------------------------------------------------ control tools
    def _record_hypotheses(self, state: InvestigationState, call: ToolCall) -> dict[str, Any]:
        added = []
        origin = "llm" if self.reasoner.is_llm else "deterministic_policy"
        for h in call.args.get("hypotheses", []):
            if state.hypothesis(h["cause_id"]):
                continue
            label = string.ascii_uppercase[len(state.hypotheses) % 26]
            hyp = Hypothesis(id=f"H-{label}", label=label, cause_id=h["cause_id"], stage=h["stage"],
                             statement=h["statement"], origin=origin)
            state.hypotheses.append(hyp)
            added.append(hyp.id)
        state.phase = "test"
        return {"recorded": added}

    def _apply_verdicts(self, state: InvestigationState, verdicts: list[dict[str, Any]]) -> None:
        obs_ids = {o["id"] for o in state.observations}
        exp_ids = {e["experiment_id"] for e in state.experiments}
        for v in verdicts:
            h = state.hypothesis(v["hypothesis_id"])
            if h is None:
                continue
            sup = [e for e in v.get("supporting_experiments", []) if e in exp_ids]
            ref = [e for e in v.get("refuting_experiments", []) if e in exp_ids]
            sup_obs = [o for o in v.get("supporting_observations", []) if o in obs_ids]
            status = v["status"]
            note = ""
            if status == "supported" and not (sup or sup_obs):
                status, note = "inconclusive", "claimed supported without citing an executed experiment or observation"
            if status == "refuted" and not ref:
                status, note = "inconclusive", "claimed refuted without citing an executed experiment"
            bogus = set(v.get("supporting_experiments", []) + v.get("refuting_experiments", [])) - exp_ids
            if bogus:
                note = (note + "; " if note else "") + f"ignored non-existent experiment ids {sorted(bogus)}"
            h.status = status
            h.roles = list(v.get("roles", [])) if status == "supported" else []
            h.supporting_experiments, h.refuting_experiments = sup, ref
            h.supporting_observations = sup_obs
            h.rationale = v.get("rationale", "")
            h.verification_note = note

    def _submit(self, state: InvestigationState, call: ToolCall) -> None:
        self._apply_verdicts(state, call.args.get("verdicts", []))
        confirmed = [h for h in state.hypotheses if h.status == "supported"]
        state.diagnosis = {
            "earliest_incorrect_stage": state.earliest_incorrect_stage,
            "earliest_stage_claimed_by_reasoner": call.args.get("earliest_incorrect_stage"),
            "confirmed_causes": [{"hypothesis": h.id, "cause_id": h.cause_id, "roles": h.roles} for h in confirmed],
            "refuted": [h.cause_id for h in state.hypotheses if h.status == "refuted"],
            "untestable": [h.cause_id for h in state.hypotheses if h.status == "untestable"],
            "inconclusive": [h.cause_id for h in state.hypotheses if h.status == "inconclusive"],
            "summary": call.args.get("summary", ""),
            "reasoner": self.reasoner.kind,
        }
        state.phase = "fix" if confirmed else "escalated"
        if not confirmed:
            state.escalation = build_escalation(state, self.ctx, "no_confirmed_cause",
                                                ["No hypothesis is supported by experiments. Review the evidence."])

    def escalate(self, state: InvestigationState, reason: str, questions: list[str]) -> None:
        state.escalation = build_escalation(state, self.ctx, reason, questions)
        state.phase = "escalated"
        self._decide(state, "escalate", f"{reason}: {questions[0] if questions else ''}", "escalated")
        self._audit("escalated", {"reason": reason})

    # ------------------------------------------------------------------ main loop
    def run(self, state: InvestigationState | None = None) -> InvestigationState:
        state = state or self.new_state()
        if state.budget.reasoning_steps or state.experiments:
            state.resumed_count += 1
            state.budget.elapsed_before_resume = state.budget.elapsed_at_checkpoint
            state.budget.started_at = time.time()
            self._decide(state, "resume", f"resumed from checkpoint at step {state.budget.reasoning_steps} "
                         f"with {len(state.experiments)} experiments preserved")
        if state.phase == "observe":
            self.observe(state)

        while state.phase in ("hypothesize", "test", "conclude"):
            try:
                self.budget.check(state.budget)
            except BudgetExceeded as exc:
                self.escalate(state, f"budget_exhausted:{exc.dimension}",
                              [f"Investigation stopped at the {exc.dimension} limit ({exc.detail}). Decide whether "
                               f"to extend the budget or assign to an engineer; see evidence collected so far."])
                break
            out = self.reasoner.next_actions(state, self.ctx)
            state.budget.reasoning_steps += 1
            state.budget.tokens_in += out.tokens_in
            state.budget.tokens_out += out.tokens_out
            state.budget.llm_calls += int(out.llm_call)
            state.budget.cost_usd = round(state.budget.cost_usd + self._cost(out.tokens_in, out.tokens_out), 6)
            if not out.actions:
                self._decide(state, "no_action", out.text[:300] or "reasoner returned no action")
            results: list[tuple[ToolCall, dict[str, Any]]] = []
            for call in out.actions:
                self._decide(state, call.name if call.name != "run_sandbox_replay" else
                             f"experiment:{call.args.get('label')}", call.rationale, call.phase)
                if call.name == "submit_diagnosis":
                    self._submit(state, call)
                    results.append((call, {"accepted": True, "phase": state.phase}))
                    break
                if call.name == "create_escalation_report":
                    if call.args.get("verdicts"):
                        self._apply_verdicts(state, call.args["verdicts"])
                    self.escalate(state, call.args.get("reason", "reasoner_escalated"),
                                  call.args.get("questions_for_human", []))
                    results.append((call, {"accepted": True}))
                    break
                if call.name == "record_hypotheses":
                    results.append((call, self._record_hypotheses(state, call)))
                    continue
                try:
                    results.append((call, self.execute_tool(state, call)))
                except BudgetExceeded as exc:
                    self.escalate(state, f"budget_exhausted:{exc.dimension}",
                                  [f"Stopped before '{call.name}' at the {exc.dimension} limit ({exc.detail})."])
                    break
            self.reasoner.record_results(state, results)
            self.checkpoint(state)

        if state.phase == "fix":
            generate_and_validate_fixes(self, state)
            self.checkpoint(state)
        return state

    def _cost(self, tin: int, tout: int) -> float:
        llm = self.ctx.registry.agent_config()["llm"]
        return tin / 1e6 * llm["input_usd_per_mtok"] + tout / 1e6 * llm["output_usd_per_mtok"]
