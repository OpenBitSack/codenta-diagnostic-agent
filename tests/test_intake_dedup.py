"""Intake validation, auth/isolation, idempotency, exact dedup (Demo C), concurrency, backpressure."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from codenta_diag.api import create_app
from codenta_diag.api.app import build_container
from codenta_diag.db import Database, InvestigationRow, JobRow
from codenta_diag.intake import IssueReportIn, Principal

from .conftest import ADMIN, REPORTER, REPORTER_PREP, REVIEWER, get_inv, process, submit, ticket_payload

pytestmark = pytest.mark.integration


def test_intake_validation(client, container, demo_tickets):
    t = demo_tickets["demo_a"][0]
    base = ticket_payload(container, t)
    for bad in ({"predicted_codes": ["X123"]}, {"severity": "urgent"}, {"unexpected": 1},
                {"pipeline_version": "1.3"}, {"reported_at": "2999-01-01T00:00:00Z"}):
        assert client.post("/v1/issues", json={**base, **bad}, headers=REPORTER).status_code == 422, bad
    # Report must be consistent with the saved (read-only) run.
    r = client.post("/v1/issues", json={**base, "predicted_codes": ["D0220"]}, headers=REPORTER)
    assert r.status_code == 422 and "do not match saved run" in r.text
    r = client.post("/v1/issues", json={**base, "pipeline_run_id": "RUN-NOPE"}, headers=REPORTER)
    assert r.status_code == 422


def test_auth_and_practice_isolation(client, container, demo_tickets):
    payload = ticket_payload(container, demo_tickets["demo_a"][0])
    assert client.post("/v1/issues", json=payload).status_code == 401
    assert client.post("/v1/issues", json=payload, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/v1/issues", json=payload, headers=REPORTER_PREP).status_code == 403
    assert client.post("/v1/issues", json=payload, headers=REVIEWER).status_code == 403  # reviewers don't file
    created = client.post("/v1/issues", json=payload, headers=REPORTER).json()
    inv_id = created["investigation_id"]
    assert client.get(f"/v1/investigations/{inv_id}", headers=REPORTER).status_code == 200
    assert client.get(f"/v1/investigations/{inv_id}", headers=REPORTER_PREP).status_code == 404
    assert inv_id not in [i["id"] for i in client.get("/v1/investigations", headers=REPORTER_PREP).json()]
    assert client.get("/v1/metrics", headers=REPORTER).status_code == 403
    assert client.post("/v1/admin/process", headers=REVIEWER).status_code == 403


def test_idempotent_ticket_replay(client, container, demo_tickets):
    t = demo_tickets["demo_a"][0]
    first = client.post("/v1/issues", json=ticket_payload(container, t), headers=REPORTER)
    again = client.post("/v1/issues", json=ticket_payload(container, t), headers=REPORTER)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["idempotent_replay"] and again.json()["report_id"] == first.json()["report_id"]
    conflict = client.post("/v1/issues", json=ticket_payload(container, t, pipeline_run_id="RUN-3001",
                                                             source_note_id="N-3001",
                                                             predicted_codes=["D2750"]), headers=REPORTER)
    assert conflict.status_code == 409


def test_demo_c_many_duplicates_one_investigation(client, container, demo_tickets):
    base = demo_tickets["demo_a"][0]
    results = [submit(client, container, {**base, "ticket_id": f"TCK-DUP-{i:03d}"}) for i in range(25)]
    inv_ids = {r["investigation_id"] for r in results}
    assert len(inv_ids) == 1
    assert sum(r["duplicate"] for r in results) == 24
    with container.db.session() as s:
        assert s.scalar(select(func.count()).select_from(JobRow)) == 1
    processed = process(client)["processed_jobs"]
    assert processed == 1  # exactly one agent investigation for 25 tickets
    inv = get_inv(client, inv_ids.pop())
    assert inv["report_count"] == 25 and inv["status"] == "fix_validated"
    assert inv["priority"] == 0  # bumped: many affected tickets
    m = client.get("/v1/metrics", headers=REVIEWER).json()
    assert m["volume"]["tickets_received"] == 25 and m["volume"]["full_agent_investigations"] == 1
    assert m["volume"]["exact_duplicates_linked"] == 24
    assert m["cost"]["reasoning_steps_avoided_duplicates"] == 24 * inv["reasoning_steps"]


def test_concurrent_duplicate_submissions_create_one_investigation(container, demo_tickets):
    principal = Principal("reporter-seat", "reporter", ["P-SEAT-01"])
    base = demo_tickets["demo_a"][0]
    payloads = [IssueReportIn(**ticket_payload(container, {**base, "ticket_id": f"TCK-CC-{i:02d}"}))
                for i in range(16)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda p: container.intake.submit(p, principal), payloads))
    assert len({r.investigation_id for r in results}) == 1
    assert sum(not r.duplicate for r in results) == 1
    with container.db.session() as s:
        inv = s.execute(select(InvestigationRow)).scalar_one()
        assert inv.report_count == 16
        assert s.scalar(select(func.count()).select_from(JobRow)) == 1


def test_different_notes_same_error_category_are_not_exact_duplicates(client, container, demo_tickets):
    a = submit(client, container, demo_tickets["demo_a"][0])
    d4 = submit(client, container, demo_tickets["demo_d"][3])  # same error pattern, different note
    assert a["investigation_id"] != d4["investigation_id"] and not d4["duplicate"]
    assert any(c["investigation_id"] == a["investigation_id"] for c in d4["similar_candidates"])
    cand = next(c for c in d4["similar_candidates"] if c["investigation_id"] == a["investigation_id"])
    assert "not proof" in cand["note"] or "candidate" in cand["note"]


def test_conflicting_expectations_are_flagged(client, container, demo_tickets):
    base = demo_tickets["demo_a"][0]
    first = submit(client, container, base)
    submit(client, container, {**base, "ticket_id": "TCK-A-ALT", "expected_codes": ["D0220", "D2392", "D2740"]})
    assert get_inv(client, first["investigation_id"])["conflicting_expectations"] is True


def test_backpressure_rejects_new_noncritical_work(settings, demo_tickets):
    s2 = replace(settings, queue_hard_limit=1)
    container = build_container(s2, Database(s2.database_url))
    with TestClient(create_app(s2, container)) as client:
        submit(client, container, demo_tickets["demo_d"][0])
        r = client.post("/v1/issues", json=ticket_payload(container, demo_tickets["demo_d"][1]), headers=REPORTER)
        assert r.status_code == 429 and r.headers["Retry-After"] == "30"
        dup = client.post("/v1/issues", json=ticket_payload(container, {**demo_tickets["demo_d"][0],
                                                                        "ticket_id": "TCK-DUP-X"}), headers=REPORTER)
        assert dup.status_code == 200  # duplicates add no work and are always accepted
        crit = client.post("/v1/issues", json=ticket_payload(container, demo_tickets["demo_d"][1], severity="critical"),
                           headers=REPORTER)
        assert crit.status_code == 201


def test_intake_rate_limit(settings, demo_tickets):
    s2 = replace(settings, intake_rate_per_minute=2)
    container = build_container(s2, Database(s2.database_url))
    with TestClient(create_app(s2, container)) as client:
        codes = [client.post("/v1/issues", json=ticket_payload(container, {**demo_tickets["demo_a"][0],
                                                                           "ticket_id": f"TCK-RL-{i}"}),
                             headers=REPORTER).status_code for i in range(4)]
        assert codes[:2] == [201, 200] and codes[2] == 429


def test_admin_dev_pipeline_run_endpoint(client):
    r = client.post("/v1/dev/pipeline-runs", headers=ADMIN, json={
        "run_id": "RUN-9001", "note_id": "N-9001", "practice_id": "P-SEAT-01", "policy_ref": "P-SEAT-01@v3",
        "text": "#2 full gold crown seated today."})
    assert r.status_code == 201 and r.json()["predicted_codes"] == ["D2790"]
    assert client.post("/v1/dev/pipeline-runs", headers=REPORTER, json={}).status_code in (403, 422)
