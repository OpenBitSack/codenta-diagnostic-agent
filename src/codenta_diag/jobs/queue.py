"""Persistent, DB-backed job queue (SQS stand-in for local dev).

* Claim is an atomic conditional UPDATE (`... WHERE id=? AND status='queued'`), so two workers can
  never run the same job; on PostgreSQL this is equivalent to SELECT ... FOR UPDATE SKIP LOCKED.
* Leases + heartbeats: a crashed worker's job is re-queued when its lease expires, and the
  investigation resumes from its last checkpoint.
* Retries use exponential backoff with jitter; after `max_attempts` the job is dead-lettered.
* Priority: severity-derived, bumped when many duplicate reports are attached.
"""
from __future__ import annotations

import random
from datetime import timedelta

from sqlalchemy import func, select, update

from ..db import Database, InvestigationRow, JobRow
from ..domain import utcnow


class JobQueue:
    def __init__(self, db: Database, lease_seconds: int = 120, retry_base_seconds: float = 2.0,
                 max_attempts: int = 3) -> None:
        self.db = db
        self.lease_seconds = lease_seconds
        self.retry_base = retry_base_seconds
        self.max_attempts = max_attempts

    def depth(self) -> int:
        with self.db.session() as s:
            return s.scalar(select(func.count()).select_from(JobRow).where(JobRow.status == "queued")) or 0

    def counts(self) -> dict[str, int]:
        with self.db.session() as s:
            rows = s.execute(select(JobRow.status, func.count()).group_by(JobRow.status)).all()
        return {status: n for status, n in rows}

    def claim(self, worker_id: str) -> tuple[int, str] | None:
        now = utcnow()
        with self.db.session() as s:
            candidates = s.execute(
                select(JobRow.id, JobRow.investigation_id)
                .where(JobRow.status == "queued", JobRow.next_run_at <= now)
                .order_by(JobRow.priority, JobRow.id).limit(8)).all()
        for job_id, inv_id in candidates:
            with self.db.session() as s:
                res = s.execute(
                    update(JobRow).where(JobRow.id == job_id, JobRow.status == "queued")
                    .values(status="running", lease_owner=worker_id, attempts=JobRow.attempts + 1,
                            lease_expires_at=now + timedelta(seconds=self.lease_seconds), updated_at=now))
                if res.rowcount == 1:
                    return job_id, inv_id
        return None

    def heartbeat(self, job_id: int, worker_id: str) -> bool:
        now = utcnow()
        with self.db.session() as s:
            res = s.execute(update(JobRow).where(JobRow.id == job_id, JobRow.lease_owner == worker_id,
                                                 JobRow.status == "running")
                            .values(lease_expires_at=now + timedelta(seconds=self.lease_seconds), updated_at=now))
            return res.rowcount == 1

    def complete(self, job_id: int) -> None:
        with self.db.session() as s:
            s.execute(update(JobRow).where(JobRow.id == job_id)
                      .values(status="done", lease_owner=None, lease_expires_at=None, updated_at=utcnow()))

    def fail(self, job_id: int, error: str) -> str:
        """Returns the new status: 'queued' (retry scheduled) or 'dead' (dead-lettered)."""
        now = utcnow()
        with self.db.session() as s:
            job = s.get(JobRow, job_id)
            if job.attempts >= job.max_attempts:
                job.status = "dead"
                inv = s.get(InvestigationRow, job.investigation_id)
                inv.status = "failed"
            else:
                delay = self.retry_base * (2 ** (job.attempts - 1)) * (1 + random.random() * 0.25)
                job.status = "queued"
                job.next_run_at = now + timedelta(seconds=delay)
            job.last_error = error[:2000]
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            status = job.status
        self.db.audit("queue", "job_dead_lettered" if status == "dead" else "job_retry_scheduled",
                      details={"job_id": job_id, "error": error[:300]})
        return status

    def recover_expired_leases(self) -> int:
        now = utcnow()
        with self.db.session() as s:
            res = s.execute(update(JobRow).where(JobRow.status == "running", JobRow.lease_expires_at < now)
                            .values(status="queued", lease_owner=None, lease_expires_at=None, updated_at=now))
            n = res.rowcount
        if n:
            self.db.audit("queue", "expired_leases_recovered", details={"count": n})
        return n

    def requeue_dead(self, job_id: int) -> bool:
        with self.db.session() as s:
            job = s.get(JobRow, job_id)
            if job is None or job.status != "dead":
                return False
            job.status, job.attempts, job.next_run_at = "queued", 0, utcnow()
            inv = s.get(InvestigationRow, job.investigation_id)
            inv.status = "queued"
        return True

    def next_delayed_run(self) -> float | None:
        with self.db.session() as s:
            nxt = s.scalar(select(func.min(JobRow.next_run_at)).where(JobRow.status == "queued"))
        return None if nxt is None else max(0.0, (nxt - utcnow()).total_seconds())
