"""DEV ONLY: file the demonstration tickets (A–E) server-side so the UI can run scenarios with one click.

Tickets come from `data/demo_tickets/tickets.yaml`; predicted codes are read from the saved run, so
the tickets pass the same intake validation as any other report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable

import yaml

from .db import Database, PipelineRunRow
from .intake import IntakeService, IssueReportIn, Principal
from .settings import PROJECT_ROOT
from .store import ReadOnlyTraceStore

DEMO_PRINCIPAL = Principal("demo-runner", "admin", ["*"])


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    batches: list[list[dict[str, Any]]] = field(default_factory=list)  # each batch is processed before the next


@lru_cache
def _tickets() -> dict[str, Any]:
    with (PROJECT_ROOT / "data" / "demo_tickets" / "tickets.yaml").open() as fh:
        return yaml.safe_load(fh)


def scenarios() -> dict[str, Scenario]:
    t = _tickets()
    return {s.id: s for s in [
        Scenario("a", "A — Interview crown case",
                 "The original note at a seat-billing practice: two extraction defects, a schema limitation, a QA gap.",
                 [t["demo_a"]]),
        Scenario("b", "B — Prep-billing practice",
                 "Same note at a prep-billing practice; both corrections are needed to reach D2740.",
                 [t["demo_b"]]),
        Scenario("c", "C — 25 duplicate tickets",
                 "25 tickets for the same failing run collapse into one investigation.",
                 [[{**t["demo_a"][0], "ticket_id": f"TCK-C-{i:03d}"} for i in range(25)]]),
        Scenario("d", "D — Similar but different",
                 "Four look-alike crown errors (material, stage, missing policy, overlapping) — not auto-merged.",
                 [t["demo_d"]]),
        Scenario("e", "E — Escalation + grouping",
                 "Labels that need facts the note doesn't document escalate; a second one joins the same question.",
                 [t["escalation"][:1], t["escalation"][1:]]),
    ]}


def _payload(db: Database, ticket: dict[str, Any]) -> IssueReportIn:
    with db.session() as s:
        practice = s.get(PipelineRunRow, ticket["run_id"]).practice_id
    run = ReadOnlyTraceStore(db, practice).get_run(ticket["run_id"])
    return IssueReportIn(
        ticket_id=ticket["ticket_id"], practice_id=run.practice_id, source_note_id=run.note_id,
        pipeline_run_id=run.run_id, predicted_codes=run.predicted_codes, expected_codes=ticket.get("expected_codes"),
        error_description=ticket.get("error_description", ""), policy_version=run.policy_ref,
        pipeline_version=run.pipeline_version, severity=ticket.get("severity", "medium"), reporter_id="demo-runner")


def run_scenario(scenario_id: str, db: Database, intake: IntakeService, process: Callable[[], int]) -> dict[str, Any]:
    sc = scenarios()[scenario_id]
    results = []
    for batch in sc.batches:
        for ticket in batch:
            r = intake.submit(_payload(db, ticket), DEMO_PRINCIPAL)
            results.append({"ticket_id": ticket["ticket_id"], **r.model_dump(mode="json")})
        process()
    return {"scenario": sc.id, "title": sc.title, "tickets": len(results),
            "investigation_ids": list(dict.fromkeys(r["investigation_id"] for r in results)), "results": results}
