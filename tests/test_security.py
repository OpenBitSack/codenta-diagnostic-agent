"""Security properties: practice isolation, read-only traces, sandbox-only changes, minimal sensitive logging."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from codenta_diag.agent import ToolContext
from codenta_diag.config_registry import ConfigRegistry
from codenta_diag.db import AuditLogRow, PipelineRunRow
from codenta_diag.pipeline import production_config
from codenta_diag.settings import Settings
from codenta_diag.store import NotFound, PracticeScopeError, ReadOnlyTraceStore

from .conftest import INTERVIEW_NOTE, REPORTER, REVIEWER, get_inv, process, submit


def test_trace_store_enforces_practice_scope(container):
    store = ReadOnlyTraceStore(container.db, "P-PREP-02")
    with pytest.raises(PracticeScopeError):
        store.get_run("RUN-1001")
    with pytest.raises(PracticeScopeError):
        store.get_note("N-1001")
    with pytest.raises(NotFound):
        store.get_run("RUN-NOPE")
    assert store.get_run("RUN-2001").practice_id == "P-PREP-02"


def test_trace_store_has_no_write_methods():
    public = {n for n in dir(ReadOnlyTraceStore) if not n.startswith("_")}
    assert public <= {"get_note", "get_run"}


def test_tool_context_rejects_cross_practice_data(container):
    store = ReadOnlyTraceStore(container.db, "P-SEAT-01")
    run, note = store.get_run("RUN-1001"), store.get_note("N-1001")
    with pytest.raises(PermissionError):
        ToolContext(investigation_id="x", practice_id="P-PREP-02", note=note, run=run, expected=None,
                    run_policy=None, registry_policy=None, base_config=production_config(container.registry),
                    registry=container.registry, engine=container.service.engine,
                    regression=container.service.regression, gate_cfg={})


def test_investigation_never_mutates_production_runs_or_config(client, container, demo_tickets):
    with container.db.session() as s:
        before = {r.run_id: json.dumps(r.payload, sort_keys=True)
                  for r in s.execute(select(PipelineRunRow)).scalars()}
    registry_fp = ConfigRegistry().fingerprint()
    prod = production_config(container.registry).components()
    submit(client, container, demo_tickets["demo_a"][0])
    process(client)
    with container.db.session() as s:
        after = {r.run_id: json.dumps(r.payload, sort_keys=True)
                 for r in s.execute(select(PipelineRunRow)).scalars()}
    assert before == after
    assert ConfigRegistry().fingerprint() == registry_fp
    assert production_config(container.registry).components() == prod
    assert production_config(container.registry).sandbox is False


def test_sandbox_configs_are_flagged(container):
    cfg = container.service.engine.build_config(production_config(container.registry),
                                                ["extraction-schema-v2-crown-stage"], "t")
    assert cfg.sandbox is True and cfg.version != production_config(container.registry).version


def test_audit_log_records_actions_without_note_text(client, container, demo_tickets):
    inv_id = submit(client, container, demo_tickets["demo_a"][0])["investigation_id"]
    process(client)
    with container.db.session() as s:
        rows = s.execute(select(AuditLogRow)).scalars().all()
    actions = {r.action for r in rows}
    assert {"report_received", "tool_call", "investigation_fix_validated"} <= actions
    assert all(r.actor and r.ts for r in rows)
    assert sum(r.investigation_id == inv_id and r.action == "tool_call" for r in rows) >= 5
    blob = json.dumps([{"a": r.action, "d": r.details} for r in rows])
    for fragment in ("pt chose all-ceramic", "cracked", INTERVIEW_NOTE[:40]):
        assert fragment not in blob


def test_logs_do_not_contain_note_text(client, container, demo_tickets, caplog):
    caplog.set_level("DEBUG")
    submit(client, container, demo_tickets["demo_a"][0])
    process(client)
    assert "pt chose all-ceramic" not in caplog.text


def test_dev_keys_refused_outside_dev(monkeypatch):
    monkeypatch.setenv("CODENTA_ENV", "production")
    monkeypatch.delenv("CODENTA_API_KEYS_JSON", raising=False)
    with pytest.raises(RuntimeError):
        Settings.from_env()


def test_reporter_cannot_read_other_practice_reports(client, container, demo_tickets):
    r = submit(client, container, demo_tickets["demo_a"][0])
    assert client.get(f"/v1/issues/{r['report_id']}", headers=REPORTER).status_code == 200
    assert client.get(f"/v1/issues/{r['report_id']}", headers={"X-API-Key": "dev-reporter-prep"}).status_code == 404
    inv = get_inv(client, r["investigation_id"])
    assert inv["practice_id"] == "P-SEAT-01"


def test_review_queue_scoped_for_practice_reviewers(settings, demo_tickets):
    from fastapi.testclient import TestClient

    from codenta_diag.api import create_app
    from codenta_diag.api.app import build_container
    from codenta_diag.db import Database
    keys = {**settings.api_keys, "prep-reviewer": {"principal": "prep-reviewer", "role": "reviewer",
                                                   "practices": ["P-PREP-02"]}}
    s2 = replace(settings, api_keys=keys)
    container = build_container(s2, Database(s2.database_url))
    with TestClient(create_app(s2, container)) as client:
        a = submit(client, container, demo_tickets["demo_a"][0])
        process(client)
        q = client.get("/v1/review-queue", headers={"X-API-Key": "prep-reviewer"}).json()
        assert q == []
        assert client.get(f"/v1/investigations/{a['investigation_id']}",
                          headers={"X-API-Key": "prep-reviewer"}).status_code == 404
        fix_id = client.get("/v1/review-queue", headers=REVIEWER).json()[0]["fix_id"]
        assert client.post(f"/v1/fixes/{fix_id}/review", json={"decision": "approve"},
                           headers={"X-API-Key": "prep-reviewer"}).status_code == 404
