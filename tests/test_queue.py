"""Queue resilience: crash/resume from checkpoint, retries + dead-letter, lease recovery, no double
claims under concurrency, priority ordering, rate limiting."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from codenta_diag.db import JobRow
from codenta_diag.domain import utcnow
from codenta_diag.jobs import TokenBucket

from .conftest import ADMIN, get_inv, hyp, process, submit

pytestmark = pytest.mark.integration


def job_for(container, inv_id):
    with container.db.session() as s:
        return s.execute(select(JobRow).where(JobRow.investigation_id == inv_id)).scalar_one()


def test_worker_crash_resumes_from_checkpoint(client, container, demo_tickets):
    inv_id = submit(client, container, demo_tickets["demo_a"][0])["investigation_id"]
    container.service.crash_after_step = 5
    process(client)
    inv = get_inv(client, inv_id)
    assert inv["status"] == "fix_validated"
    assert inv["state"]["resumed_count"] == 1
    assert any(d["action"] == "resume" for d in inv["state"]["decisions"])
    job = job_for(container, inv_id)
    assert job.attempts == 2 and job.status == "done" and "simulated worker crash" in job.last_error
    # the resumed run reaches the same diagnosis without repeating completed experiments
    labels = [e["label"] for e in inv["state"]["experiments"]]
    assert len(labels) == len(set(labels))
    assert hyp(inv, "extraction.crown.status.value_incorrect")["status"] == "supported"
    assert hyp(inv, "coding.policy.not_applied")["status"] == "refuted"


def test_retries_then_dead_letter_then_manual_requeue(client, container, demo_tickets, monkeypatch):
    inv_id = submit(client, container, demo_tickets["demo_a"][0])["investigation_id"]
    real_run = container.service.run

    def broken(*_a, **_k):
        raise ConnectionError("simulated dependency outage")
    monkeypatch.setattr(container.service, "run", broken)
    process(client)
    job = job_for(container, inv_id)
    assert job.status == "dead" and job.attempts == container.settings.job_max_attempts
    assert get_inv(client, inv_id)["status"] == "failed"
    m = client.get("/v1/metrics", headers=ADMIN).json()
    assert m["queue"]["dead_letter"] == 1
    monkeypatch.setattr(container.service, "run", real_run)
    assert client.post(f"/v1/admin/jobs/{job.id}/requeue", headers=ADMIN).status_code == 200
    assert client.post(f"/v1/admin/jobs/{job.id}/requeue", headers=ADMIN).status_code == 409
    process(client)
    assert get_inv(client, inv_id)["status"] == "fix_validated"


def test_backoff_delays_retry(container, client, demo_tickets, monkeypatch):
    inv_id = submit(client, container, demo_tickets["demo_d"][0])["investigation_id"]
    job_id, _ = container.queue.claim("w1")
    assert container.queue.fail(job_id, "boom") == "queued"
    job = job_for(container, inv_id)
    assert job.next_run_at > utcnow() and container.queue.claim("w2") is None  # not runnable until backoff elapses
    time.sleep(container.settings.retry_base_seconds * 1.5)
    assert container.queue.claim("w2") is not None


def test_expired_lease_is_recovered(client, container, demo_tickets):
    inv_id = submit(client, container, demo_tickets["demo_d"][1])["investigation_id"]
    job_id, _ = container.queue.claim("worker-that-dies")
    assert container.queue.recover_expired_leases() == 0
    with container.db.session() as s:
        s.execute(update(JobRow).where(JobRow.id == job_id).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
    assert container.queue.recover_expired_leases() == 1
    assert not container.queue.heartbeat(job_id, "worker-that-dies")  # stale owner can't extend the lease
    process(client)
    assert get_inv(client, inv_id)["status"] == "fix_validated"


def test_concurrent_claims_never_double_assign(client, container, demo_tickets):
    tickets = demo_tickets["demo_d"] + demo_tickets["demo_a"] + demo_tickets["escalation"]
    for t in tickets:
        submit(client, container, t)
    with ThreadPoolExecutor(max_workers=12) as pool:
        claims = list(pool.map(lambda i: container.queue.claim(f"w{i}"), range(24)))
    claimed = [c[0] for c in claims if c]
    assert len(claimed) == len(tickets) and len(set(claimed)) == len(claimed)


def test_priority_order(client, container, demo_tickets):
    low = submit(client, container, demo_tickets["demo_d"][0], severity="low")["investigation_id"]
    crit = submit(client, container, demo_tickets["demo_d"][1], severity="critical")["investigation_id"]
    first = container.queue.claim("w")
    assert first[1] == crit and container.queue.claim("w")[1] == low


def test_concurrent_pool_processes_independent_investigations(client, container, demo_tickets):
    ids = [submit(client, container, t)["investigation_id"] for t in demo_tickets["demo_d"][:3]]
    assert process(client)["processed_jobs"] == 3
    assert all(get_inv(client, i)["status"] == "fix_validated" for i in ids)


def test_token_bucket():
    b = TokenBucket(rate_per_minute=600, burst=3)
    assert [b.try_acquire() for _ in range(4)] == [True, True, True, False]
    assert 0 < b.retry_after() <= 0.1
    assert b.acquire(timeout=1.0)
