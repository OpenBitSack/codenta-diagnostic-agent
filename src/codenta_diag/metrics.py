"""Operational metrics computed from persisted state (no in-memory counters needed for correctness)."""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from .db import CandidateFixRow, Database, InvestigationLinkRow, InvestigationRow, KnownIssueRow, ReportRow
from .jobs import JobQueue


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    k = max(0, math.ceil(p / 100 * len(vals)) - 1)
    return round(vals[k], 3)


def _secs(a: Any, b: Any) -> float | None:
    return None if a is None or b is None else (b - a).total_seconds()


def compute_metrics(db: Database, queue: JobQueue) -> dict[str, Any]:
    with db.session() as s:
        invs = s.execute(select(InvestigationRow)).scalars().all()
        reports = s.execute(select(ReportRow)).scalars().all()
        fixes = s.execute(select(CandidateFixRow)).scalars().all()
        links = s.execute(select(InvestigationLinkRow)).scalars().all()
        kis = {k.id: k for k in s.execute(select(KnownIssueRow)).scalars().all()}

    counts = queue.counts()
    completed = [i for i in invs if i.status not in ("queued", "investigating")]
    full = [i for i in completed if i.resolution in ("full_investigation", "escalated") and i.reasoning_steps > 0]
    diagnosed = [i for i in invs if i.diagnosed_at is not None]
    escalated = [i for i in completed if i.status == "escalated"]

    inv_per_ki: dict[str, set[str]] = defaultdict(set)
    for link in links:
        inv_per_ki[link.known_issue_id].add(link.investigation_id)
    reports_by_inv: dict[str, int] = defaultdict(int)
    for r in reports:
        reports_by_inv[r.investigation_id] += 1
    known_issue_impact = sorted(({
        "known_issue_id": k, "cause_id": kis[k].cause_id, "status": kis[k].status,
        "investigations": len(v), "tickets": sum(reports_by_inv[i] for i in v)} for k, v in inv_per_ki.items()),
        key=lambda x: (-x["tickets"], x["cause_id"]))
    confirmed_invs = {i for k, v in inv_per_ki.items() if kis[k].status == "confirmed" for i in v}

    steps_by_inv = {i.id: i.reasoning_steps for i in invs}
    dup_steps_avoided = sum((i.report_count - 1) * steps_by_inv[i.id] for i in completed if i.report_count > 1)
    replay_steps_avoided = db.counter("reasoning_steps_avoided")

    validated = [f for f in fixes if f.gate_passed]
    coverage = []
    for f in validated:
        covered_kis = {k for k, ki in kis.items() if ki.validated_fix_id == f.id}
        covered_invs = {inv for k in covered_kis for inv in inv_per_ki[k]} | {f.investigation_id}
        coverage.append({
            "fix_id": f.id, "change_ids": f.change_ids, "approval_status": f.approval_status,
            "investigations_covered": len(covered_invs),
            "tickets_covered": sum(reports_by_inv[i] for i in covered_invs),
            "regression_suite_cases_improved": len(f.evaluation.get("improvements", [])),
        })
    reviewed = [f for f in fixes if f.review_seconds is not None]
    tokens = [i.tokens_used for i in full]
    total_cost = sum(i.cost_usd for i in invs)

    return {
        "queue": {"depth": counts.get("queued", 0), "active_investigations": counts.get("running", 0),
                  "dead_letter": counts.get("dead", 0), "done": counts.get("done", 0)},
        "volume": {
            "tickets_received": len(reports),
            "canonical_investigations": len(invs),
            "exact_duplicates_linked": sum(1 for r in reports if r.is_duplicate),
            "full_agent_investigations": len(full),
            "resolved_by_known_issue_replay": sum(1 for i in invs if i.resolution == "known_issue_replay"),
            "escalations_grouped": db.counter("escalations_grouped"),
            "similar_candidates_surfaced": db.counter("similar_candidates_surfaced"),
        },
        "outcomes_distinguished": {
            "diagnosed_incidents": len(diagnosed),
            "validated_fix_proposals": len(validated),
            "distinct_validated_change_sets": len({tuple(f.change_ids) for f in validated}),
            "human_approved_fixes": sum(1 for f in fixes if f.approval_status == "approved"),
            "tickets_linked_to_confirmed_root_causes": sum(reports_by_inv[i] for i in confirmed_invs),
        },
        "known_issue_impact": known_issue_impact,
        "cost": {
            "reasoning_steps_total": sum(steps_by_inv.values()),
            "reasoning_steps_avoided_duplicates": dup_steps_avoided,
            "reasoning_steps_avoided_known_issue_replay": replay_steps_avoided,
            "llm_calls_total": sum(i.llm_calls for i in invs),
            "llm_calls_avoided_note": "in Claude mode one reasoning step = one LLM call; in deterministic-demo mode "
                                      "LLM calls are 0 and 'steps avoided' is the measured proxy",
            "tokens_per_full_investigation_avg": round(sum(tokens) / len(tokens), 1) if tokens else 0,
            "total_cost_usd": round(total_cost, 6),
            "cost_per_diagnosed_incident_usd": round(total_cost / len(diagnosed), 6) if diagnosed else None,
            "cache_hits": db.counter("cache_hits"),
        },
        "latency_seconds": {
            "time_to_diagnosis_p50": percentile([x for i in diagnosed if (x := _secs(i.created_at, i.diagnosed_at)) is not None], 50),
            "time_to_diagnosis_p95": percentile([x for i in diagnosed if (x := _secs(i.created_at, i.diagnosed_at)) is not None], 95),
            "time_to_validated_fix_p50": percentile([x for i in invs if (x := _secs(i.created_at, i.fix_validated_at)) is not None], 50),
            "time_to_validated_fix_p95": percentile([x for i in invs if (x := _secs(i.created_at, i.fix_validated_at)) is not None], 95),
            "investigation_runtime_p50": percentile([x for i in completed if (x := _secs(i.started_at, i.completed_at)) is not None], 50),
            "investigation_runtime_p95": percentile([x for i in completed if (x := _secs(i.started_at, i.completed_at)) is not None], 95),
            "note": "measured on this deployment only; no SLA is implied",
        },
        "human_review": {
            "escalation_rate": round(len(escalated) / len(completed), 3) if completed else None,
            "pending_fix_reviews": sum(1 for f in fixes if f.approval_status == "pending_review"),
            "open_escalations": len(escalated),
            "review_seconds_per_resolved_avg": round(sum(f.review_seconds for f in reviewed) / len(reviewed), 1)
            if reviewed else None,
        },
        "validated_fix_coverage": coverage,
    }
