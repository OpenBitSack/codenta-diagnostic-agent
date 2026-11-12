"""Issue intake: validation against the saved run, idempotency, exact dedup, backpressure.

No LLM is involved in intake. Exact duplicates are linked by a DB unique constraint on the
investigation fingerprint, which is safe under concurrent submissions.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..config_registry import ConfigRegistry
from ..db import Database, InvestigationRow, JobRow, ReportRow
from ..dedup import embed, exact_fingerprint, features_for, rank_candidates
from ..domain import utcnow
from ..store import NotFound, PracticeScopeError, ReadOnlyTraceStore
from .schemas import SEVERITY_PRIORITY, IntakeResult, IssueReportIn


class IntakeError(Exception):
    status_code = 422


class ConflictError(IntakeError):
    status_code = 409


class BackpressureError(IntakeError):
    status_code = 429

    def __init__(self, msg: str, retry_after: int) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


class ForbiddenError(IntakeError):
    status_code = 403


@dataclass
class Principal:
    principal: str
    role: str
    practices: list[str]

    def can_access(self, practice_id: str) -> bool:
        return "*" in self.practices or practice_id in self.practices


class IntakeService:
    def __init__(self, db: Database, registry: ConfigRegistry, soft_limit: int = 200, hard_limit: int = 1000,
                 max_attempts: int = 3) -> None:
        self.db = db
        self.registry = registry
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit
        self.max_attempts = max_attempts

    def _queue_depth(self) -> int:
        from ..jobs.queue import JobQueue
        return JobQueue(self.db).depth()

    def submit(self, report: IssueReportIn, principal: Principal) -> IntakeResult:
        if not principal.can_access(report.practice_id):
            raise ForbiddenError(f"principal {principal.principal} cannot report for {report.practice_id}")

        # Idempotency on ticket_id.
        with self.db.session() as s:
            existing = s.execute(select(ReportRow).where(ReportRow.ticket_id == report.ticket_id)).scalar_one_or_none()
            if existing is not None:
                if existing.pipeline_run_id != report.pipeline_run_id or existing.practice_id != report.practice_id:
                    raise ConflictError(f"ticket {report.ticket_id} already exists with different content")
                inv = s.get(InvestigationRow, existing.investigation_id)
                return IntakeResult(report_id=existing.id, investigation_id=inv.id, duplicate=existing.is_duplicate,
                                    idempotent_replay=True, status=inv.status, report_count=inv.report_count,
                                    message="ticket already received (idempotent replay)")

        # Validate against the saved (read-only) production run.
        store = ReadOnlyTraceStore(self.db, report.practice_id)
        try:
            run = store.get_run(report.pipeline_run_id)
        except (NotFound, PracticeScopeError) as exc:
            raise IntakeError(f"pipeline run not found for practice: {exc}") from exc
        if run.note_id != report.source_note_id:
            raise IntakeError("source_note_id does not match the pipeline run")
        if sorted(run.predicted_codes) != sorted(report.predicted_codes):
            raise IntakeError(f"predicted_codes {report.predicted_codes} do not match saved run {run.predicted_codes}")
        if run.pipeline_version != report.pipeline_version:
            raise IntakeError("pipeline_version does not match the saved run")

        fingerprint = exact_fingerprint(report.practice_id, report.source_note_id, report.pipeline_run_id,
                                        report.predicted_codes)
        with self.db.session() as s:
            known = s.execute(select(InvestigationRow.id).where(InvestigationRow.fingerprint == fingerprint)).first()
        if known is None:
            depth = self._queue_depth()
            if depth >= self.hard_limit and report.severity != "critical":
                raise BackpressureError(f"queue depth {depth} >= hard limit {self.hard_limit}", retry_after=30)
        else:
            depth = 0

        card = self.registry.rule_card()
        features = features_for(report.practice_id, report.policy_version, report.pipeline_version,
                                report.predicted_codes, report.expected_codes, report.error_description, card)
        inv_id = f"INV-{uuid.uuid4().hex[:10]}"
        priority = SEVERITY_PRIORITY[report.severity] + (1 if depth >= self.soft_limit else 0)
        duplicate = False
        try:
            with self.db.session() as s:
                s.add(InvestigationRow(
                    id=inv_id, fingerprint=fingerprint, practice_id=report.practice_id,
                    source_note_id=report.source_note_id, pipeline_run_id=report.pipeline_run_id,
                    pipeline_version=report.pipeline_version, policy_version=report.policy_version,
                    predicted_codes=report.predicted_codes, expected_codes=report.expected_codes,
                    error_description=report.error_description, severity=report.severity, priority=priority,
                    status="queued", features=features, embedding=embed(features)))
                s.flush()
                s.add(JobRow(investigation_id=inv_id, priority=priority, max_attempts=self.max_attempts))
        except IntegrityError:
            duplicate = True

        with self.db.session() as s:
            inv = s.execute(select(InvestigationRow).where(InvestigationRow.fingerprint == fingerprint)).scalar_one()
            if duplicate:
                s.execute(update(InvestigationRow).where(InvestigationRow.id == inv.id)
                          .values(report_count=InvestigationRow.report_count + 1))
                new_priority = min(inv.priority, SEVERITY_PRIORITY[report.severity])
                if inv.report_count + 1 >= 5:
                    new_priority = max(0, new_priority - 1)
                if new_priority != inv.priority:
                    inv.priority = new_priority
                    s.execute(update(JobRow).where(JobRow.investigation_id == inv.id, JobRow.status == "queued")
                              .values(priority=new_priority))
                if report.expected_codes is not None and inv.expected_codes is not None and \
                        sorted(report.expected_codes) != sorted(inv.expected_codes):
                    inv.conflicting_expectations = True
                elif inv.expected_codes is None and report.expected_codes is not None:
                    inv.expected_codes = report.expected_codes
            report_id = f"RPT-{uuid.uuid4().hex[:10]}"
            try:
                s.add(ReportRow(
                    id=report_id, ticket_id=report.ticket_id, practice_id=report.practice_id,
                    source_note_id=report.source_note_id, pipeline_run_id=report.pipeline_run_id,
                    predicted_codes=report.predicted_codes, expected_codes=report.expected_codes,
                    error_description=report.error_description, policy_version=report.policy_version,
                    pipeline_version=report.pipeline_version,
                    stage_traces=[t.model_dump() for t in report.stage_traces] if report.stage_traces else None,
                    severity=report.severity, reporter_id=report.reporter_id, reported_at=report.reported_at,
                    fingerprint=fingerprint, investigation_id=inv.id, is_duplicate=duplicate,
                    created_at=utcnow()))
                s.flush()
            except IntegrityError as exc:  # concurrent replay of the same ticket_id
                raise ConflictError(f"ticket {report.ticket_id} submitted concurrently; retry") from exc
            others = [] if duplicate else [
                {"id": o.id, "features": o.features, "embedding": o.embedding, "status": o.status,
                 "symptom_signature": o.symptom_signature, "root_cause_signature": o.root_cause_signature}
                for o in s.execute(select(InvestigationRow).where(InvestigationRow.id != inv.id)).scalars()]
            similar = rank_candidates({"id": inv.id, "features": inv.features, "embedding": inv.embedding},
                                      others, 0.5, 5) if not duplicate else []
            if not duplicate:
                inv.similar = similar
            status, count = inv.status, inv.report_count + (1 if duplicate else 0)
            inv_ref = inv.id

        self.db.incr("reports_received")
        if duplicate:
            self.db.incr("exact_duplicates_linked")
        if similar:
            self.db.incr("similar_candidates_surfaced")
        self.db.audit(principal.principal, "report_received", inv_ref,
                      {"ticket_id": report.ticket_id, "duplicate": duplicate, "severity": report.severity})
        return IntakeResult(report_id=report_id, investigation_id=inv_ref, duplicate=duplicate, status=status,
                            report_count=count, similar_candidates=similar,
                            message="linked to existing investigation (exact duplicate)" if duplicate
                            else "new investigation queued")
