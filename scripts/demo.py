"""Run demonstration scenarios A-E end-to-end through the HTTP API against a fresh database.

    python scripts/demo.py                 # all scenarios, deterministic-demo reasoner, temp SQLite DB
    python scripts/demo.py --only a,c      # subset
    python scripts/demo.py --only a,b --report-dir docs/generated

Everything printed here is measured from this run. The reasoner is the clearly labelled
deterministic demo implementation unless CODENTA_REASONER=claude and ANTHROPIC_API_KEY are set.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from codenta_diag.api import create_app
from codenta_diag.api.app import build_container
from codenta_diag.db import Database, PipelineRunRow
from codenta_diag.settings import PROJECT_ROOT, Settings
from codenta_diag.store import ReadOnlyTraceStore

HEADERS = {"P-SEAT-01": {"X-API-Key": "dev-reporter-seat"}, "P-PREP-02": {"X-API-Key": "dev-reporter-prep"}}
REVIEWER = {"X-API-Key": "dev-reviewer"}
ADMIN = {"X-API-Key": "dev-admin"}


def hr(title: str) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")


class Demo:
    def __init__(self, client: TestClient, db: Database) -> None:
        self.client, self.db = client, db
        with (PROJECT_ROOT / "data" / "demo_tickets" / "tickets.yaml").open() as fh:
            self.tickets = yaml.safe_load(fh)
        self.failures: list[str] = []

    def check(self, cond: bool, label: str) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        if not cond:
            self.failures.append(label)

    def payload(self, t: dict[str, Any], **kw: Any) -> dict[str, Any]:
        with self.db.session() as s:
            practice = s.get(PipelineRunRow, t["run_id"]).practice_id
        run = ReadOnlyTraceStore(self.db, practice).get_run(t["run_id"])
        p = {"ticket_id": t["ticket_id"], "practice_id": run.practice_id, "source_note_id": run.note_id,
             "pipeline_run_id": run.run_id, "predicted_codes": run.predicted_codes,
             "expected_codes": t.get("expected_codes"), "error_description": t.get("error_description", ""),
             "policy_version": run.policy_ref, "pipeline_version": run.pipeline_version,
             "severity": t.get("severity", "medium"), "reporter_id": "qa-analyst-demo"}
        p.update(kw)
        return p

    def submit(self, t: dict[str, Any], **kw: Any) -> dict[str, Any]:
        p = self.payload(t, **kw)
        r = self.client.post("/v1/issues", json=p, headers=HEADERS[p["practice_id"]])
        r.raise_for_status()
        return r.json()

    def process(self) -> int:
        return self.client.post("/v1/admin/process", headers=ADMIN).json()["processed_jobs"]

    def inv(self, inv_id: str) -> dict[str, Any]:
        return self.client.get(f"/v1/investigations/{inv_id}", headers=REVIEWER).json()

    @staticmethod
    def summarize(inv: dict[str, Any]) -> None:
        st = inv["state"] or {}
        print(f"  investigation {inv['id']}  status={inv['status']}  resolution={inv['resolution']}  "
              f"reasoner={inv['reasoner']}")
        print(f"  predicted={inv['predicted_codes']}  expected={inv['expected_codes']}")
        print(f"  budget: steps={inv['reasoning_steps']} experiments={inv['experiments_run']} "
              f"regression_runs={inv['regression_runs']} llm_calls={inv['llm_calls']} tokens={inv['tokens_used']}")
        for h in st.get("hypotheses", []):
            print(f"   {h['label']:>4} {h['status']:<13} {','.join(h.get('roles', [])):<28} {h['cause_id']}")
        for a in st.get("fix_attempts", []):
            print(f"   fix attempt: {a['change_ids']} gate_passed={a['gate_passed']}"
                  + (f" failed={a['failed_checks']} regressions={[r['case_id'] for r in a['regressions']]}"
                     if not a["gate_passed"] else ""))
            if a.get("revision_rationale"):
                print(f"      revision: {a['revision_rationale']}")
        if st.get("escalation"):
            print(f"   escalation: {st['escalation'].get('reason')}")

    # ------------------------------------------------------------------ scenarios
    def demo_a(self) -> str:
        hr("DEMO A — original interview crown case (seat-billing practice P-SEAT-01)")
        r = self.submit(self.tickets["demo_a"][0])
        self.process()
        inv = self.inv(r["investigation_id"])
        self.summarize(inv)
        hs = {h["cause_id"]: h for h in inv["state"]["hypotheses"]}
        self.check(hs["extraction.crown.material.value_incorrect"]["status"] == "supported",
                   "hypothesis A (material) supported by replay")
        self.check(hs["extraction.crown.status.value_incorrect"]["status"] == "supported",
                   "hypothesis B (status) supported by replay")
        self.check(hs["coding.policy.not_applied"]["status"] == "refuted",
                   "hypothesis C (coding policy) refuted: coding applies the policy it is given")
        self.check(hs["qa.missed_error"]["status"] == "supported", "hypothesis D (QA) supported")
        self.check(inv["status"] == "fix_validated", "a fix bundle passed the regression quality gate")
        self.check(any(not a["gate_passed"] for a in inv["state"]["fix_attempts"]),
                   "narrower bundles were rejected by the gate before the passing one (also Demo E)")
        return r["investigation_id"]

    def demo_b(self) -> str:
        hr("DEMO B — same note at a prep-billing practice (P-PREP-02) must produce D2740")
        r = self.submit(self.tickets["demo_b"][0])
        self.process()
        inv = self.inv(r["investigation_id"])
        self.summarize(inv)
        st = inv["state"]
        combined = next(e for e in st["experiments"] if e["label"] == "combined:all-fields")
        self.check(combined["predicted_codes"] == ["D0220", "D2392", "D2740"],
                   f"combined correction replay -> {combined['predicted_codes']}")
        single_mat = next(e for e in st["experiments"] if e["label"] == "single:crown.material")
        self.check("D2740" not in single_mat["predicted_codes"],
                   f"material-only correction is NOT sufficient (D2790->D2740 alone): {single_mat['predicted_codes']}")
        return r["investigation_id"]

    def demo_c(self, n: int = 25) -> str:
        hr(f"DEMO C — {n} duplicate tickets for the same run -> one investigation")
        base = self.tickets["demo_a"][0]
        results = [self.submit({**base, "ticket_id": f"TCK-C-{i:03d}"}) for i in range(n)]
        ids = {r["investigation_id"] for r in results}
        processed = self.process()
        inv = self.inv(next(iter(ids)))
        m = self.client.get("/v1/metrics", headers=REVIEWER).json()
        print(f"  tickets={n} canonical_investigations={len(ids)} jobs_processed_now={processed} "
              f"report_count={inv['report_count']} priority={inv['priority']}")
        print(f"  reasoning steps avoided by exact dedup (measured): {m['cost']['reasoning_steps_avoided_duplicates']}")
        self.check(len(ids) == 1, "all duplicates map to one canonical investigation")
        return next(iter(ids))

    def demo_d(self) -> list[str]:
        hr("DEMO D — similar-looking crown issues with different root causes are NOT auto-merged")
        ids = []
        for t in self.tickets["demo_d"]:
            r = self.submit(t)
            cands = [(c["investigation_id"], c["score"]) for c in r["similar_candidates"]]
            print(f"  {t['ticket_id']} -> {r['investigation_id']} similar_candidates={cands}")
            ids.append(r["investigation_id"])
        self.process()
        sigs = {}
        for t, i in zip(self.tickets["demo_d"], ids):
            inv = self.inv(i)
            print(f"\n  --- {t['ticket_id']}: {t['error_description']}")
            self.summarize(inv)
            sigs[t["ticket_id"]] = (inv["root_cause_signature"], inv["resolution"])
        self.check(len({s[0] for k, s in sigs.items() if k != "TCK-D-004"}) == 3,
                   "D-001 (material), D-002 (stage), D-003 (missing policy) get three different root causes")
        return ids

    def demo_e(self) -> None:
        hr("DEMO E — gate rejects a regressing fix; agent revises or escalates")
        print("  (gate rejections + revision are visible in Demo A's fix attempts above)")
        r1 = self.submit(self.tickets["escalation"][0])
        self.process()
        r2 = self.submit(self.tickets["escalation"][1])
        self.process()
        for r in (r1, r2):
            inv = self.inv(r["investigation_id"])
            self.summarize(inv)
        e2 = self.inv(r2["investigation_id"])
        self.check(self.inv(r1["investigation_id"])["status"] == "escalated",
                   "label requires a fact the note does not document -> escalated, not guessed")
        self.check((e2["state"].get("escalation") or {}).get("grouped_with") == r1["investigation_id"],
                   "second escalation with identical symptoms grouped (one human answer)")

    def review_and_metrics(self) -> None:
        hr("HUMAN REVIEW QUEUE + METRICS")
        q = self.client.get("/v1/review-queue", headers=REVIEWER).json()
        for item in q:
            print(f"  [{item['kind']}] investigations={item['investigations']} severity={item['severity']} "
                  f"tickets={item['affected_tickets']} linked={item['linked_investigations']}\n      {item['summary']}")
        m = self.client.get("/v1/metrics", headers=REVIEWER).json()
        print(f"  human decisions needed: {len(q)} for {m['volume']['tickets_received']} tickets")
        print(json.dumps({k: m[k] for k in ("volume", "outcomes_distinguished", "known_issue_impact", "cost",
                                            "human_review")}, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="a,b,c,d,e")
    ap.add_argument("--report-dir", help="write generated markdown reports for demos A/B here")
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="codenta-demo-")
    settings = replace(Settings.from_env(), database_url=os.environ.get("DEMO_DATABASE_URL",
                                                                        f"sqlite:///{tmp}/demo.db"),
                       intake_rate_per_minute=10_000)
    container = build_container(settings, Database(settings.database_url))
    print(f"database: {settings.database_url}\nreasoner: {settings.reasoner} "
          f"({'DETERMINISTIC DEMO — scripted, no LLM' if settings.reasoner == 'deterministic' else 'Claude via API'})")
    with TestClient(create_app(settings, container)) as client:
        demo = Demo(client, container.db)
        steps = set(args.only.split(","))
        reports = {}
        if "a" in steps:
            reports["demo_a_seat_billing"] = demo.demo_a()
        if "b" in steps:
            reports["demo_b_prep_billing"] = demo.demo_b()
        if "c" in steps:
            demo.demo_c()
        if "d" in steps:
            demo.demo_d()
        if "e" in steps:
            demo.demo_e()
        demo.review_and_metrics()
        if args.report_dir:
            out = Path(args.report_dir)
            out.mkdir(parents=True, exist_ok=True)
            for name, inv_id in reports.items():
                md = client.get(f"/v1/investigations/{inv_id}/report.md", headers=REVIEWER).text
                (out / f"{name}.md").write_text(md)
                print(f"wrote {out / f'{name}.md'}")
    hr("RESULT: " + ("all checks passed" if not demo.failures else f"{len(demo.failures)} FAILED: {demo.failures}"))
    return 1 if demo.failures else 0


if __name__ == "__main__":
    sys.exit(main())
