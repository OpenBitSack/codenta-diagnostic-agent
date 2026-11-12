"""Synthetic load test. Measures THIS machine / THIS configuration only — not an SLA or benchmark.

    python scripts/load_test.py --tickets 1000 --unique-runs 60 --workers 4
    python scripts/load_test.py --hard-limit 20      # exercise backpressure (429s)

Generates synthetic failing runs from a few crown-error templates (varying tooth numbers), files
tickets with a duplicate-heavy distribution through the HTTP API (in-process TestClient), then drains
the queue with the worker pool and reports measured throughput, latency percentiles and how much
work deduplication + known-issue replay avoided.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from codenta_diag.api import create_app
from codenta_diag.api.app import build_container
from codenta_diag.db import Database
from codenta_diag.domain import SourceNote
from codenta_diag.metrics import percentile
from codenta_diag.settings import Settings
from codenta_diag.store import PipelineRecorder

CROWN_TEETH = [2, 3, 4, 5, 12, 13, 14, 15, 18, 19, 20, 29, 30, 31]
TEMPLATES = [  # (template, expected codes at a seat-billing practice, label)
    ("#{t} cracked, pt c/o pain on biting. 1 PA taken. Prepped #{t} for crown, temp placed, impression to lab. "
     "Discussed gold and all-ceramic; pt chose all-ceramic. Also #19 MO composite filling done today. "
     "Seat #{t} in 2 wks.", ["D0220", "D2392"], "interview-pattern"),
    ("#{t} crown seated today. Discussed PFM and gold; pt chose gold.", ["D2790"], "material-only"),
    ("#{t} prepped for full gold crown, temp placed. Seat #{t} in 3 wks.", [], "stage-only"),
    ("#{t} crown seated today. Discussed gold vs PFM; material not documented.", ["D2750"], "label-conflict"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickets", type=int, default=1000)
    ap.add_argument("--unique-runs", type=int, default=60)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--hard-limit", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rnd = random.Random(args.seed)
    tmp = tempfile.mkdtemp(prefix="codenta-load-")
    settings = replace(Settings.from_env(), database_url=os.environ.get("LOAD_DATABASE_URL", f"sqlite:///{tmp}/load.db"),
                       worker_concurrency=args.workers, intake_rate_per_minute=10_000_000,
                       queue_hard_limit=args.hard_limit, queue_soft_limit=min(args.hard_limit, 200))
    container = build_container(settings, Database(settings.database_url))
    recorder = PipelineRecorder(container.db, container.registry)

    runs = []
    for i in range(args.unique_runs):
        tpl, expected, label = TEMPLATES[i % len(TEMPLATES)]
        tooth = CROWN_TEETH[(i // len(TEMPLATES)) % len(CROWN_TEETH)]
        suffix = "" if i < len(TEMPLATES) * len(CROWN_TEETH) else f" Visit ref {i}."
        note = SourceNote(note_id=f"N-L{i:04d}", practice_id="P-SEAT-01", text=tpl.format(t=tooth) + suffix)
        run = recorder.record_run(f"RUN-L{i:04d}", note, "P-SEAT-01@v3")
        runs.append((run, expected, label))

    # Zipf-like duplicate distribution: a few runs get most of the tickets.
    weights = [1 / (k + 1) for k in range(len(runs))]
    picks = rnd.choices(range(len(runs)), weights=weights, k=args.tickets)
    headers = {"X-API-Key": "dev-reporter-seat"}
    status_counts: dict[int, int] = {}
    intake_lat = []
    with TestClient(create_app(settings, container)) as client:
        t0 = time.perf_counter()
        for n, idx in enumerate(picks):
            run, expected, label = runs[idx]
            body = {"ticket_id": f"TCK-L{n:06d}", "practice_id": "P-SEAT-01", "source_note_id": run.note_id,
                    "pipeline_run_id": run.run_id, "predicted_codes": run.predicted_codes, "expected_codes": expected,
                    "error_description": f"synthetic load ticket ({label})", "policy_version": run.policy_ref,
                    "pipeline_version": run.pipeline_version, "severity": rnd.choice(["low", "medium", "high"]),
                    "reporter_id": "load-generator"}
            s = time.perf_counter()
            code = client.post("/v1/issues", json=body, headers=headers).status_code
            intake_lat.append(time.perf_counter() - s)
            status_counts[code] = status_counts.get(code, 0) + 1
        intake_s = time.perf_counter() - t0

        t1 = time.perf_counter()
        processed = container.pool.drain(max_wait_seconds=3600)
        drain_s = time.perf_counter() - t1
        m = client.get("/v1/metrics", headers={"X-API-Key": "dev-admin"}).json()

    out = {
        "disclaimer": "Measured on this machine with SQLite and an in-process TestClient. Not an SLA or benchmark.",
        "config": {"tickets": args.tickets, "unique_runs": args.unique_runs, "workers": args.workers,
                   "hard_limit": args.hard_limit, "reasoner": settings.reasoner, "database": "sqlite (temp file)"},
        "intake": {"status_counts": status_counts, "seconds": round(intake_s, 2),
                   "requests_per_second": round(args.tickets / intake_s, 1),
                   "latency_ms_p50": round(percentile(intake_lat, 50) * 1000, 2),
                   "latency_ms_p95": round(percentile(intake_lat, 95) * 1000, 2)},
        "processing": {"jobs_processed": processed, "drain_seconds": round(drain_s, 2),
                       "investigations_per_second": round(processed / drain_s, 2) if drain_s else None},
        "volume": m["volume"], "outcomes_distinguished": m["outcomes_distinguished"],
        "cost": {k: m["cost"][k] for k in ("reasoning_steps_total", "reasoning_steps_avoided_duplicates",
                                           "reasoning_steps_avoided_known_issue_replay", "cache_hits")},
        "latency_seconds": m["latency_seconds"], "queue": m["queue"],
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
