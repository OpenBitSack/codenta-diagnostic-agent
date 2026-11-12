"""Escalation reports and human-readable root-cause reports.

Design goal: a reviewer should be able to act in minutes without redoing the agent's work, so every
report lists what was already tested (do-not-repeat) and asks specific, answerable questions.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .state import InvestigationState
    from .tools import ToolContext


def _exp_line(e: dict[str, Any]) -> dict[str, Any]:
    cmp = e.get("comparison") or {}
    return {"experiment_id": e["experiment_id"], "label": e["label"], "predicted_codes": e["predicted_codes"],
            "exact_match": cmp.get("exact_match"), "expected_source": e.get("expected_source")}


def build_escalation(state: "InvestigationState", ctx: "ToolContext", reason: str,
                     questions: list[str]) -> dict[str, Any]:
    key_obs = [o for o in state.observations if o["kind"] not in ("stage_reproduces_from_inputs",)]
    return {
        "reason": reason,
        "questions_for_human": questions,
        "ticket": {"run_id": ctx.run.run_id, "practice_id": ctx.practice_id,
                   "predicted": ctx.run.predicted_codes, "expected": ctx.expected},
        "evidence": [{"id": o["id"], "detail": o["detail"],
                      "quotes": [s["text"] for s in o.get("evidence_spans", [])]} for o in key_obs],
        "hypotheses": [{"id": h.id, "cause_id": h.cause_id, "status": h.status, "rationale": h.rationale}
                       for h in state.hypotheses],
        "already_done_do_not_repeat": [_exp_line(e) for e in state.experiments],
        "rejected_fix_bundles": [{"change_ids": a["change_ids"], "failed_checks": a["failed_checks"],
                                  "regressions": [r["case_id"] for r in a["regressions"]]}
                                 for a in state.fix_attempts if not a["gate_passed"]],
        "budget_used": state.budget.model_dump(exclude={"started_at", "elapsed_before_resume"}),
        "recommended_next_action": {
            "label_documentation_conflict": "Confirm the source of truth for the label; if the note is incomplete, "
                                            "route to documentation follow-up, not a pipeline change.",
            "no_safe_alternative": "Engineer authors a new candidate change; re-run validation with the same suite.",
            "no_candidate_remedy": "Engineer authors a candidate change for the confirmed cause.",
            "expected_codes_unknown": "Dental expert supplies expected codes; investigation resumes automatically.",
        }.get(reason.split(":")[0], "Review evidence and decide whether to extend the budget or close."),
    }


def render_markdown(inv: dict[str, Any]) -> str:
    """Render an investigation (API dict) as a markdown root-cause report."""
    s = inv.get("state") or {}
    lines = [f"# Investigation {inv['id']} — {inv['status']}", ""]
    lines += [f"- Practice: `{inv['practice_id']}`  Run: `{inv['pipeline_run_id']}`  Pipeline: `{inv['pipeline_version']}`",
              f"- Reasoner: `{inv.get('reasoner')}` (deterministic-demo = rule-based policy, NOT an LLM)",
              f"- Predicted: `{inv['predicted_codes']}`  Expected: `{inv['expected_codes']}`",
              f"- Linked reports: {inv.get('report_count')}  Resolution path: `{inv.get('resolution')}`",
              f"- Earliest observable incorrect stage: **{s.get('earliest_incorrect_stage')}**", ""]
    lines += ["## Observations (facts from deterministic checks)", ""]
    for o in s.get("observations", []):
        quotes = "; ".join(f'"{sp["text"]}"' for sp in o.get("evidence_spans", []))
        lines.append(f"- `{o['id']}` [{o['stage']}] {o['detail']}" + (f" — note: {quotes}" if quotes else ""))
    lines += ["", "## Hypotheses and verdicts", "", "| ID | Cause | Status | Roles | Evidence | Rationale |",
              "|---|---|---|---|---|---|"]
    for h in s.get("hypotheses", []):
        ev = ", ".join(h["supporting_experiments"] + h["refuting_experiments"] + h.get("supporting_observations", []))
        lines.append(f"| {h['id']} | `{h['cause_id']}` | **{h['status']}** | {', '.join(h['roles'])} | {ev} | "
                     f"{h['rationale']}{(' ⚠ ' + h['verification_note']) if h.get('verification_note') else ''} |")
    lines += ["", "## Sandbox experiments", "", "| Experiment | Label | Predicted | Matches | Expected source |",
              "|---|---|---|---|---|"]
    for e in s.get("experiments", []):
        cmp = e.get("comparison") or {}
        lines.append(f"| `{e['experiment_id']}` | {e['label']} | `{e['predicted_codes']}` | "
                     f"{cmp.get('exact_match')} | {e.get('expected_source')} |")
    if s.get("fix_attempts"):
        lines += ["", "## Candidate fix bundles (regression suite + quality gate)", ""]
        for a in s["fix_attempts"]:
            verdict = "PASSED" if a["gate_passed"] else "REJECTED"
            lines.append(f"- Attempt {a['attempt']} `{a['change_ids']}` — **{verdict}**; "
                         f"F1 {a['baseline_metrics']['f1']} → {a['candidate_metrics']['f1']}, "
                         f"escaped errors {a['baseline_metrics']['escaped_errors']} → "
                         f"{a['candidate_metrics']['escaped_errors']}, improvements {len(a['improvements'])}, "
                         f"regressions {[r['case_id'] for r in a['regressions']]}")
            if a.get("revision_rationale"):
                lines.append(f"  - Revision: {a['revision_rationale']}")
    if s.get("escalation"):
        esc = s["escalation"]
        lines += ["", "## Escalation", "", f"Reason: `{esc['reason']}`", "", "Questions for a human:"]
        lines += [f"1. {q}" for q in esc["questions_for_human"]]
        lines += ["", f"Recommended next action: {esc['recommended_next_action']}"]
    lines += ["", "## Decision log", ""]
    for d in s.get("decisions", []):
        lines.append(f"- step {d['step']} [{d['phase']}] **{d['action']}** — {d['rationale']}")
    return "\n".join(lines) + "\n"
