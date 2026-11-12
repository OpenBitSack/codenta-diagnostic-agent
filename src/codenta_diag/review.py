"""Human review: prioritized queue + approve/reject. Approval NEVER deploys anything."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select

from .db import CandidateFixRow, Database, InvestigationLinkRow, InvestigationRow
from .domain import utcnow
from .intake import Principal

SEVERITY_WEIGHT = {"critical": 8, "high": 4, "medium": 2, "low": 1}
SEVERITY_ORDER = ["critical", "high", "medium", "low"]


class ReviewError(Exception):
    status_code = 409


def _max_severity(invs: list[InvestigationRow]) -> str:
    return min((i.severity for i in invs), key=SEVERITY_ORDER.index)


def review_queue(db: Database, practices: list[str] | None = None) -> list[dict[str, Any]]:
    """One item per decision a human must make, ranked by (severity x affected tickets).

    Folding rules (each reduces decisions, never merges root causes):
      * known-issue-replay children fold into their parent investigation;
      * pending fixes with an identical change set fold into one decision (the reviewer approves a
        change set; each investigation keeps its own confirmed root causes);
      * escalations linked to the same `pending_human_judgement` group fold into one question.
    """
    with db.session() as s:
        invs = s.execute(select(InvestigationRow)).scalars().all()
        fixes = s.execute(select(CandidateFixRow).where(CandidateFixRow.approval_status == "pending_review")).scalars().all()
        esc_links = s.execute(select(InvestigationLinkRow).where(
            InvestigationLinkRow.link_type == "pending_human_judgement")).scalars().all()
    by_id = {i.id: i for i in invs}
    children: dict[str, list[InvestigationRow]] = defaultdict(list)
    for i in invs:
        st = i.state or {}
        parent = (st.get("fast_path") or {}).get("parent") or (st.get("escalation") or {}).get("grouped_with")
        if parent:
            children[parent].append(i)

    def visible(inv: InvestigationRow) -> bool:
        return practices is None or "*" in practices or inv.practice_id in practices

    def family(inv: InvestigationRow) -> list[InvestigationRow]:
        return [inv, *children.get(inv.id, [])]

    items = []
    by_change_set: dict[tuple[str, ...], list[CandidateFixRow]] = defaultdict(list)
    for f in fixes:
        if visible(by_id[f.investigation_id]):
            by_change_set[tuple(f.change_ids)].append(f)
    for change_set, group in by_change_set.items():
        group.sort(key=lambda f: by_id[f.investigation_id].created_at)
        members = [m for f in group for m in family(by_id[f.investigation_id])]
        tickets = sum(m.report_count for m in members)
        severity = _max_severity(members)
        ev = group[0].evaluation
        causes = sorted({c for f in group for c in (by_id[f.investigation_id].root_cause_signature or "").split("|") if c})
        items.append({
            "kind": "fix_review", "fix_id": group[0].id, "fix_ids": [f.id for f in group],
            "change_ids": list(change_set), "investigation_id": group[0].investigation_id,
            "investigations": [f.investigation_id for f in group],
            "linked_investigations": [m.id for m in members if m.id not in {f.investigation_id for f in group}],
            "root_causes_addressed": causes, "severity": severity, "affected_tickets": tickets,
            "score": SEVERITY_WEIGHT[severity] * tickets,
            "summary": f"Bundle {list(change_set)}: F1 {ev['baseline_metrics']['f1']} -> {ev['candidate_metrics']['f1']}, "
                       f"escaped errors {ev['baseline_metrics']['escaped_errors']} -> "
                       f"{ev['candidate_metrics']['escaped_errors']}, regressions {len(ev['regressions'])}; "
                       f"validated independently by {len(group)} investigation(s)",
            "decision_needed": "Approve this sandbox-validated change set for the standard release process?",
        })

    esc_group: dict[str, str] = {l.investigation_id: l.known_issue_id for l in esc_links}
    by_question: dict[str, list[InvestigationRow]] = defaultdict(list)
    for inv in invs:
        st = inv.state or {}
        if inv.status != "escalated" or (st.get("escalation") or {}).get("grouped_with") or not visible(inv):
            continue
        by_question[esc_group.get(inv.id, inv.id)].append(inv)
    for group_key, group in by_question.items():
        group.sort(key=lambda i: i.created_at)
        primary = group[0]
        members = [m for g in group for m in family(g)]
        tickets = sum(m.report_count for m in members)
        severity = _max_severity(members)
        esc = (primary.state or {}).get("escalation") or {}
        items.append({
            "kind": "escalation", "investigation_id": primary.id, "investigations": [g.id for g in group],
            "escalation_group": group_key if group_key != primary.id else None,
            "linked_investigations": [m.id for m in members if m.id != primary.id],
            "severity": severity, "affected_tickets": tickets, "score": SEVERITY_WEIGHT[severity] * tickets,
            "summary": esc.get("reason", ""), "decision_needed": (esc.get("questions_for_human") or [""])[0],
        })
    return sorted(items, key=lambda x: -x["score"])


def review_fix(db: Database, fix_id: str, principal: Principal, decision: str, notes: str,
               review_seconds: float | None) -> dict[str, Any]:
    """Approve/reject a change set. The decision applies to every pending fix with the identical change
    set (they were folded into one review item), and to known-issue-replay children."""
    if decision not in ("approve", "reject"):
        raise ReviewError("decision must be 'approve' or 'reject'")
    new_status = "approved" if decision == "approve" else "rejected"
    with db.session() as s:
        fix = s.get(CandidateFixRow, fix_id)
        if fix is None:
            raise LookupError(fix_id)
        inv = s.get(InvestigationRow, fix.investigation_id)
        if not principal.can_access(inv.practice_id):
            raise LookupError(fix_id)
        if fix.approval_status != "pending_review":
            raise ReviewError(f"fix {fix_id} is {fix.approval_status}, not pending_review")
        if not fix.gate_passed:
            raise ReviewError("only gate-passing fixes can be approved")
        same = [f for f in s.execute(select(CandidateFixRow).where(
            CandidateFixRow.approval_status == "pending_review")).scalars()
            if f.change_ids == fix.change_ids and principal.can_access(s.get(InvestigationRow, f.investigation_id).practice_id)]
        now = utcnow()
        inv_ids = []
        for f in same:
            f.approval_status = new_status
            f.reviewer_id = principal.principal
            f.review_notes = notes[:2000]
            f.review_seconds = review_seconds if f.id == fix_id else None
            f.reviewed_at = now
            parent = s.get(InvestigationRow, f.investigation_id)
            parent.status = new_status
            inv_ids.append(parent.id)
        for child in s.execute(select(InvestigationRow).where(InvestigationRow.status == "linked_known_issue")).scalars():
            if ((child.state or {}).get("fast_path") or {}).get("parent") in inv_ids:
                child.status = new_status
                inv_ids.append(child.id)
        fix_ids = [f.id for f in same]
    for inv_id in inv_ids:
        db.audit(principal.principal, f"fix_{new_status}", inv_id, {"fix_id": fix_id, "applies_to_fixes": fix_ids,
                                                                    "review_seconds": review_seconds})
    return {"fix_id": fix_id, "approval_status": new_status, "investigation_id": inv_ids[0], "applies_to_fixes": fix_ids,
            "investigations_updated": inv_ids,
            "deployment": "NOT deployed. Approved change sets go through the standard change-management / "
                          "deployment process outside this system." if decision == "approve" else None}
