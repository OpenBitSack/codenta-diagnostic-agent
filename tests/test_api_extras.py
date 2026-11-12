"""Endpoints used by UI clients: identity, scoped run listing, enriched detail, dev demo scenarios."""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from codenta_diag.api import create_app
from codenta_diag.api.app import build_container
from codenta_diag.db import Database

from .conftest import ADMIN, REPORTER, REPORTER_PREP, REVIEWER, get_inv, process, submit

pytestmark = pytest.mark.integration


def test_me(client):
    assert client.get("/v1/me", headers=REPORTER).json() == {
        "principal": "reporter-seat", "role": "reporter", "practices": ["P-SEAT-01"]}
    assert client.get("/v1/me").status_code == 401


def test_pipeline_runs_are_practice_scoped(client):
    seat = client.get("/v1/pipeline-runs", headers=REPORTER).json()
    prep = client.get("/v1/pipeline-runs", headers=REPORTER_PREP).json()
    assert seat and {r["practice_id"] for r in seat} == {"P-SEAT-01"}
    assert [r["run_id"] for r in prep] == ["RUN-2001"]
    run = next(r for r in seat if r["run_id"] == "RUN-1001")
    assert run["predicted_codes"] == ["D0220", "D2392", "D2790"] and run["qa_passed"] is True
    assert run["extraction"]["crown"] == {"tooth": 14, "status": "completed", "material": "gold"}
    assert len(client.get("/v1/pipeline-runs", headers=REVIEWER).json()) == len(seat) + len(prep)


def test_investigation_detail_includes_note_policies_and_reports(client, container, demo_tickets):
    inv_id = submit(client, container, demo_tickets["demo_a"][0])["investigation_id"]
    process(client)
    inv = get_inv(client, inv_id)
    assert inv["note"]["note_id"] == "N-1001" and "pt chose all-ceramic" in inv["note"]["text"]
    assert inv["run_policy"]["ref"] == "P-SEAT-01@v3" and inv["run_policy"]["crown_billing_event"] == "seat"
    assert inv["registry_policy"]["ref"] == "P-SEAT-01@v3"
    assert [r["ticket_id"] for r in inv["reports"]] == ["TCK-A-001"]


def test_demo_scenarios_endpoint(client):
    assert client.get("/v1/dev/demo-scenarios", headers=REVIEWER).status_code == 403
    listed = client.get("/v1/dev/demo-scenarios", headers=ADMIN).json()
    assert [s["id"] for s in listed] == ["a", "b", "c", "d", "e"]
    c = client.post("/v1/dev/demo-scenarios/c", headers=ADMIN).json()
    assert c["tickets"] == 25 and len(c["investigation_ids"]) == 1
    assert get_inv(client, c["investigation_ids"][0])["status"] == "fix_validated"
    e = client.post("/v1/dev/demo-scenarios/e", headers=ADMIN).json()
    second = get_inv(client, e["investigation_ids"][1])
    assert second["resolution"] == "grouped_escalation"
    assert client.post("/v1/dev/demo-scenarios/zzz", headers=ADMIN).status_code == 404


def test_dev_endpoints_hidden_outside_dev(settings):
    keys = {"prod-admin": {"principal": "admin", "role": "admin", "practices": ["*"]}}
    s2 = replace(settings, environment="production", api_keys=keys)
    container = build_container(s2, Database(s2.database_url))
    with TestClient(create_app(s2, container)) as client:
        h = {"X-API-Key": "prod-admin"}
        assert client.get("/v1/dev/demo-scenarios", headers=h).status_code == 404
        assert client.post("/v1/dev/demo-scenarios/a", headers=h).status_code == 404


def test_frontend_route_without_build(client):
    r = client.get("/app")
    assert r.status_code in (200, 503)
