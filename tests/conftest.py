from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml
from fastapi.testclient import TestClient

from codenta_diag.api import create_app
from codenta_diag.api.app import Container, build_container
from codenta_diag.config_registry import ConfigRegistry
from codenta_diag.db import Database
from codenta_diag.settings import PROJECT_ROOT, Settings

INTERVIEW_NOTE = ("#14 cracked, pt c/o pain on biting. 1 PA taken. Prepped #14 for crown, temp placed, impression "
                  "to lab. Discussed gold and all-ceramic; pt chose all-ceramic. Also #19 MO composite filling done "
                  "today. Seat #14 in 2 wks.")
REPORTER = {"X-API-Key": "dev-reporter-seat"}
REPORTER_PREP = {"X-API-Key": "dev-reporter-prep"}
REVIEWER = {"X-API-Key": "dev-reviewer"}
ADMIN = {"X-API-Key": "dev-admin"}


@pytest.fixture(scope="session")
def registry() -> ConfigRegistry:
    return ConfigRegistry()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """SQLite per test by default; set CODENTA_TEST_DATABASE_URL to run the suite against PostgreSQL."""
    url = os.getenv("CODENTA_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    if not url.startswith("sqlite"):
        db = Database(url)
        db.drop_all()
        db.engine.dispose()
    return replace(Settings(), database_url=url, worker_concurrency=4, retry_base_seconds=0.05)


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    c = build_container(settings, Database(settings.database_url))
    yield c
    c.db.engine.dispose()


@pytest.fixture
def client(settings: Settings, container: Container) -> Iterator[TestClient]:
    app = create_app(settings, container)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def demo_tickets() -> dict[str, Any]:
    with (PROJECT_ROOT / "data" / "demo_tickets" / "tickets.yaml").open() as fh:
        return yaml.safe_load(fh)


def ticket_payload(container: Container, ticket: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Build an intake payload for a seeded run (predicted codes come from the saved run)."""
    from codenta_diag.store import ReadOnlyTraceStore
    from codenta_diag.db import PipelineRunRow

    with container.db.session() as s:
        practice = s.get(PipelineRunRow, ticket["run_id"]).practice_id
    run = ReadOnlyTraceStore(container.db, practice).get_run(ticket["run_id"])
    payload = {
        "ticket_id": ticket["ticket_id"], "practice_id": run.practice_id, "source_note_id": run.note_id,
        "pipeline_run_id": run.run_id, "predicted_codes": run.predicted_codes,
        "expected_codes": ticket.get("expected_codes"), "error_description": ticket.get("error_description", ""),
        "policy_version": run.policy_ref, "pipeline_version": run.pipeline_version,
        "severity": ticket.get("severity", "medium"), "reporter_id": "qa-analyst-1",
    }
    payload.update(overrides)
    return payload


def submit(client: TestClient, container: Container, ticket: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload = ticket_payload(container, ticket, **overrides)
    headers = REPORTER if payload["practice_id"] == "P-SEAT-01" else REPORTER_PREP
    r = client.post("/v1/issues", json=payload, headers=headers)
    assert r.status_code in (200, 201), r.text
    return r.json()


def process(client: TestClient) -> dict[str, Any]:
    r = client.post("/v1/admin/process", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def get_inv(client: TestClient, inv_id: str) -> dict[str, Any]:
    r = client.get(f"/v1/investigations/{inv_id}", headers=REVIEWER)
    assert r.status_code == 200, r.text
    return r.json()


def hyp(inv: dict[str, Any], cause_id: str) -> dict[str, Any]:
    return next(h for h in inv["state"]["hypotheses"] if h["cause_id"] == cause_id)
