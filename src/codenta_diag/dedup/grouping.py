"""Confirmed root-cause grouping.

Tickets are grouped ONLY by evidence:
  * `confirmed_by_investigation`       a full investigation's sandbox experiments supported the cause
  * `confirmed_by_counterfactual_replay` identical deterministic symptom signature AND the parent's
                                         validated fix makes this ticket correct in the sandbox
  * `pending_human_judgement`          identical symptom signature to an open escalation; grouped so a
                                         human answers once (not a root-cause claim)
Similarity scores never create links on their own.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..config_registry import stable_hash
from ..db import InvestigationLinkRow, KnownIssueRow
from ..domain import utcnow


def _upsert_known_issue(s: Session, cause_id: str, stage: str, title: str, status: str,
                        parent_id: str) -> KnownIssueRow:
    """Concurrency-safe get-or-create; the returned row is locked (FOR UPDATE on PostgreSQL) so concurrent
    investigations can't lose each other's history entries."""
    insert = pg_insert if s.get_bind().dialect.name == "postgresql" else sqlite_insert
    s.execute(insert(KnownIssueRow).values(
        id=f"KI-{uuid.uuid4().hex[:8]}", cause_id=cause_id, stage=stage, title=title, status=status,
        parent_investigation_id=parent_id, history=[], created_at=utcnow(),
    ).on_conflict_do_nothing(index_elements=["cause_id"]))
    return s.execute(select(KnownIssueRow).where(KnownIssueRow.cause_id == cause_id)
                     .with_for_update().execution_options(populate_existing=True)).scalar_one()


def _link(s: Session, inv_id: str, ki: KnownIssueRow, link_type: str, evidence: dict[str, Any]) -> None:
    exists = s.execute(select(InvestigationLinkRow).where(
        InvestigationLinkRow.investigation_id == inv_id,
        InvestigationLinkRow.known_issue_id == ki.id)).scalar_one_or_none()
    if exists is None:
        s.add(InvestigationLinkRow(investigation_id=inv_id, known_issue_id=ki.id, link_type=link_type,
                                   evidence=evidence))
        ki.history = [*ki.history, {"ts": utcnow().isoformat(), "investigation": inv_id, "link": link_type}]


def record_confirmed_causes(s: Session, inv_id: str, state: dict[str, Any], fix_id: str | None) -> list[str]:
    linked = []
    # Lock known issues in a global order (by cause_id) so concurrent investigations cannot deadlock.
    for h in sorted(state.get("hypotheses", []), key=lambda h: h["cause_id"]):
        if h["status"] != "supported":
            continue
        ki = _upsert_known_issue(s, h["cause_id"], h["stage"], h["statement"], "confirmed", inv_id)
        if fix_id and ki.validated_fix_id is None:
            ki.validated_fix_id = fix_id
        _link(s, inv_id, ki, "confirmed_by_investigation",
              {"hypothesis": h["id"], "roles": h["roles"],
               "experiments": h["supporting_experiments"], "observations": h.get("supporting_observations", [])})
        linked.append(ki.id)
    return linked


def link_by_replay(s: Session, inv_id: str, parent_id: str, experiment_id: str, similarity: float | None) -> list[str]:
    parent_links = s.execute(select(InvestigationLinkRow).where(
        InvestigationLinkRow.investigation_id == parent_id,
        InvestigationLinkRow.link_type.in_(["confirmed_by_investigation", "confirmed_by_counterfactual_replay"]))
    ).scalars().all()
    linked = []
    for ki_id in sorted({pl.known_issue_id for pl in parent_links}):
        ki = s.execute(select(KnownIssueRow).where(KnownIssueRow.id == ki_id).with_for_update()
                       .execution_options(populate_existing=True)).scalar_one()
        _link(s, inv_id, ki, "confirmed_by_counterfactual_replay",
              {"via_investigation": parent_id, "experiment": experiment_id, "similarity": similarity})
        linked.append(ki.id)
    return linked


def record_escalation_group(s: Session, inv_id: str, state: dict[str, Any]) -> str:
    esc = state.get("escalation") or {}
    reason = esc.get("reason", "unknown").split(":")[0]
    cause_id = f"escalation.{reason}.{stable_hash(state.get('symptom_signature', ''), 10)}"
    ki = _upsert_known_issue(s, cause_id, "human", f"Shared escalation: {reason} "
                             f"(symptoms: {state.get('symptom_signature', '')})", "needs_human", inv_id)
    _link(s, inv_id, ki, "pending_human_judgement", {"reason": esc.get("reason")})
    return ki.id
