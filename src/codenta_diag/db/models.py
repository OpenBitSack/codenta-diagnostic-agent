"""Relational model. Portable across SQLite (dev/tests) and PostgreSQL (docker-compose / AWS RDS)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..domain import utcnow


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON, datetime: DateTime()}


# ---------------------------------------------------------------- production-style (read-only to the agent)
class SourceNoteRow(Base):
    __tablename__ = "source_notes"
    note_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    practice_id: Mapped[str] = mapped_column(String(32), index=True)
    text: Mapped[str] = mapped_column(Text)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)


class PipelineRunRow(Base):
    __tablename__ = "pipeline_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    practice_id: Mapped[str] = mapped_column(String(32), index=True)
    note_id: Mapped[str] = mapped_column(String(64), index=True)
    pipeline_version: Mapped[str] = mapped_column(String(32))
    policy_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


# ---------------------------------------------------------------- diagnostic system
class ReportRow(Base):
    """One incoming failure ticket. Many reports may link to one canonical investigation."""
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(64), unique=True)  # idempotency key
    practice_id: Mapped[str] = mapped_column(String(32), index=True)
    source_note_id: Mapped[str] = mapped_column(String(64))
    pipeline_run_id: Mapped[str] = mapped_column(String(64), index=True)
    predicted_codes: Mapped[list[Any]] = mapped_column(JSON)
    expected_codes: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    error_description: Mapped[str] = mapped_column(Text, default="")
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pipeline_version: Mapped[str] = mapped_column(String(32))
    stage_traces: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    severity: Mapped[str] = mapped_column(String(16))
    reporter_id: Mapped[str] = mapped_column(String(64))
    reported_at: Mapped[datetime] = mapped_column()
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), index=True)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class InvestigationRow(Base):
    """Canonical investigation per exact-duplicate fingerprint (DB-enforced uniqueness)."""
    __tablename__ = "investigations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    practice_id: Mapped[str] = mapped_column(String(32), index=True)
    source_note_id: Mapped[str] = mapped_column(String(64))
    pipeline_run_id: Mapped[str] = mapped_column(String(64))
    pipeline_version: Mapped[str] = mapped_column(String(32))
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    predicted_codes: Mapped[list[Any]] = mapped_column(JSON)
    expected_codes: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    error_description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(16))
    priority: Mapped[int] = mapped_column(Integer, default=2)
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")
    report_count: Mapped[int] = mapped_column(Integer, default=1)
    conflicting_expectations: Mapped[bool] = mapped_column(Boolean, default=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    embedding: Mapped[list[Any]] = mapped_column(JSON, default=list)
    similar: Mapped[list[Any]] = mapped_column(JSON, default=list)
    state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # checkpoint
    symptom_signature: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    root_cause_signature: Mapped[str | None] = mapped_column(String(512), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(48), nullable=True)
    reasoner: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reasoning_steps: Mapped[int] = mapped_column(Integer, default=0)
    experiments_run: Mapped[int] = mapped_column(Integer, default=0)
    regression_runs: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    diagnosed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    fix_validated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class JobRow(Base):
    """Persistent job queue (stand-in for SQS). One job per investigation -> no duplicate processing."""
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), unique=True)
    status: Mapped[str] = mapped_column(String(16), index=True, default="queued")  # queued|running|done|dead
    priority: Mapped[int] = mapped_column(Integer, default=2)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_run_at: Mapped[datetime] = mapped_column(default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_jobs_claim", "status", "priority", "next_run_at"),)


class KnownIssueRow(Base):
    """A CONFIRMED root cause (or a shared escalation awaiting human judgement)."""
    __tablename__ = "known_issues"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    cause_id: Mapped[str] = mapped_column(String(200), unique=True)
    stage: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24))  # confirmed | needs_human | resolved
    parent_investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    validated_fix_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    history: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class InvestigationLinkRow(Base):
    __tablename__ = "investigation_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), index=True)
    known_issue_id: Mapped[str] = mapped_column(ForeignKey("known_issues.id"), index=True)
    link_type: Mapped[str] = mapped_column(String(48))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (UniqueConstraint("investigation_id", "known_issue_id"),)


class CandidateFixRow(Base):
    __tablename__ = "candidate_fixes"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), index=True)
    change_ids: Mapped[list[Any]] = mapped_column(JSON)
    gate_passed: Mapped[bool] = mapped_column(Boolean)
    evaluation: Mapped[dict[str, Any]] = mapped_column(JSON)
    approval_status: Mapped[str] = mapped_column(String(24))  # pending_review|approved|rejected|not_submitted
    reviewer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CacheEntryRow(Base):
    __tablename__ = "cache_entries"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditLogRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(48))
    investigation_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CounterRow(Base):
    __tablename__ = "counters"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)
