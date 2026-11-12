"""Candidate fix generator + regression-driven revision.

Deterministic (no LLM): for every confirmed cause pick the narrowest catalog change, evaluate the
bundle on the regression suite + target ticket, and if the quality gate fails, ATTRIBUTE each problem
case by re-running the deterministic observations on it under the candidate config. Attribution
either swaps a cause to its next (broader) remedy or adds a companion change for a newly exposed
cause. Stops on gate pass, on no new alternative, or on budget -> escalation with evidence.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..domain import SourceNote
from ..pipeline import run_pipeline
from .budget import BudgetExceeded
from .observations import causes_from_observations, observe_run
from .reasoner_base import ToolCall

if TYPE_CHECKING:
    from .controller import AgentController
    from .state import InvestigationState


def _remedies(catalog: list[dict[str, Any]], cause: str) -> list[dict[str, Any]]:
    return sorted((c for c in catalog if cause in c["addresses"]), key=lambda c: (c["blast_radius"], c["id"]))


def _attribute_case(controller: "AgentController", case_id: str, change_ids: list[str]) -> list[str]:
    ctx = controller.ctx
    case = next(c for c in ctx.regression.dataset.cases if c.id == case_id)
    config = ctx.engine.build_config(ctx.base_config, change_ids, "attribution")
    note = SourceNote(note_id=f"eval:{case.id}", practice_id=case.practice_id, text=case.note)
    run = run_pipeline(note, case.run_policy_ref, config, ctx.registry, run_id=f"attr:{case.id}")
    obs = observe_run(note, run, case.expected_codes, ctx.registry)
    return [c for c in causes_from_observations(obs) if not c.startswith("qa.")]


def _attribute_target(controller: "AgentController", change_ids: list[str]) -> list[str]:
    ctx = controller.ctx
    config = ctx.engine.build_config(ctx.base_config, change_ids, "attribution")
    run = run_pipeline(ctx.note, ctx.run.policy_ref, config, ctx.registry, run_id="attr:target")
    obs = observe_run(ctx.note, run, ctx.expected, ctx.registry)
    return [c for c in causes_from_observations(obs) if not c.startswith("qa.")]


def generate_and_validate_fixes(controller: "AgentController", state: "InvestigationState") -> None:
    ctx = controller.ctx
    catalog = ctx.registry.candidate_catalog()
    confirmed = [h for h in state.hypotheses if h.status == "supported"]
    selection: dict[str, int] = {}
    unremediable = []
    for h in confirmed:
        if _remedies(catalog, h.cause_id):
            selection[h.cause_id] = 0
        else:
            unremediable.append(h.cause_id)
    if unremediable:
        controller.escalate(state, "no_candidate_remedy",
                            [f"Confirmed cause(s) {unremediable} have no sandbox candidate change. An engineer must "
                             f"author a candidate (prompt/schema/rule) before automated validation can proceed."])
        return

    tried: list[list[str]] = []
    while True:
        bundle = sorted({_remedies(catalog, c)[i]["id"] for c, i in selection.items()})
        if bundle in tried:
            controller.escalate(state, "no_safe_alternative",
                                ["Every candidate bundle in the catalog failed the quality gate. Review the rejected "
                                 "bundles and their regressions; a new candidate change is needed."])
            return
        tried.append(bundle)
        rationale = ("Narrowest catalog change for each confirmed cause." if len(tried) == 1 else
                     f"Revision {len(tried) - 1}: {state.fix_attempts[-1]['revision_rationale']}")
        try:
            controller._decide(state, "evaluate_candidate_bundle", f"{bundle}: {rationale}", "fix")
            res = controller.execute_tool(state, ToolCall("run_targeted_evaluation",
                                                          {"candidate_change_ids": bundle}, rationale, "fix"))
        except BudgetExceeded as exc:
            controller.escalate(state, f"budget_exhausted:{exc.dimension}",
                                [f"Fix search stopped at the {exc.dimension} limit after {len(tried) - 1} bundle(s). "
                                 f"Review rejected bundles; decide whether to extend budget or author a new change."])
            return
        report = res["regression_report"]
        attempt = {
            "attempt": len(tried),
            "change_ids": bundle,
            "gate_passed": res["gate"]["passed"],
            "gate_checks": res["gate"]["checks"],
            "failed_checks": [c["name"] for c in res["gate"]["checks"] if not c["passed"]],
            "regressions": report["regressions"],
            "critical_regressions": report["critical_regressions"],
            "improvements": [i["case_id"] for i in report["improvements"]],
            "baseline_metrics": report["baseline_metrics"],
            "candidate_metrics": report["candidate_metrics"],
            "deltas": report["deltas"],
            "by_category": res["candidate_by_category"],
            "target": res["target"],
            "dataset": res["dataset"],
            "components": res["components"],
            "revision_rationale": "",
        }
        state.fix_attempts.append(attempt)
        if res["gate"]["passed"]:
            state.validated_fix = attempt
            state.phase = "done"
            controller._decide(state, "fix_validated", f"Bundle {bundle} passed the quality gate: "
                               f"{len(attempt['improvements'])} improvements, 0 regressions. Awaiting human approval.",
                               "fix")
            return

        # ---- revise using regression attribution
        problem_cases = [r["case_id"] for r in report["regressions"]]
        if "false_holds_bounded" in attempt["failed_checks"]:
            cand_cfg = ctx.engine.build_config(ctx.base_config, bundle, "holds")
            cand, _ = ctx.regression.summary(cand_cfg)
            base, _ = ctx.regression.summary(ctx.base_config)
            baseline_holds = {c.case_id for c in base.cases if c.false_hold}
            problem_cases += [c.case_id for c in cand.cases if c.false_hold and c.case_id not in baseline_holds]
        attributed: dict[str, list[str]] = {cid: _attribute_case(controller, cid, bundle) for cid in problem_cases}
        if res["target"] and not res["target"]["exact_match"]:
            attributed["<target ticket>"] = _attribute_target(controller, bundle)

        advanced: set[str] = set()
        notes: list[str] = []
        for case_id, causes in attributed.items():
            for cause in causes:
                if cause in advanced:
                    continue
                if cause in selection:
                    options = _remedies(catalog, cause)
                    current = options[selection[cause]]["id"]
                    if selection[cause] + 1 < len(options):
                        for c2, idx in list(selection.items()):
                            opts2 = _remedies(catalog, c2)
                            if opts2[idx]["id"] == current and idx + 1 < len(opts2):
                                selection[c2] = idx + 1
                                advanced.add(c2)
                        notes.append(f"{case_id}: '{current}' still leaves {cause} -> try "
                                     f"'{options[selection[cause]]['id']}'")
                elif _remedies(catalog, cause):
                    selection[cause] = 0
                    advanced.add(cause)
                    notes.append(f"{case_id}: bundle exposes {cause} -> add companion "
                                 f"'{_remedies(catalog, cause)[0]['id']}'")
        attempt["attribution"] = attributed
        attempt["revision_rationale"] = "; ".join(dict.fromkeys(notes)) or "no attributable cause"
        controller._decide(state, "candidate_rejected",
                           f"Gate failed {attempt['failed_checks']} with regressions "
                           f"{[r['case_id'] for r in report['regressions']]}. {attempt['revision_rationale']}", "fix")
        controller.checkpoint(state)
        if not advanced:
            controller.escalate(state, "no_safe_alternative",
                                [f"Bundle {bundle} failed the gate ({attempt['failed_checks']}) and the regressions "
                                 f"could not be attributed to a cause with an untried remedy. Human review needed."])
            return
