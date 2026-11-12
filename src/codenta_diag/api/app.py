from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..agent.report import render_markdown
from ..config_registry import ConfigRegistry, get_registry
from ..db import (CandidateFixRow, Database, InvestigationLinkRow, InvestigationRow, KnownIssueRow,
                  PipelineRunRow, ReportRow)
from ..demo_scenarios import run_scenario, scenarios
from ..domain import SourceNote
from ..evaluation import load_dataset
from ..intake import IntakeError, IntakeResult, IntakeService, IssueReportIn, Principal
from ..intake.service import BackpressureError
from ..jobs import JobQueue, TokenBucket
from ..jobs.worker import WorkerPool
from ..metrics import compute_metrics
from ..reasoners import make_reasoner_factory
from ..review import ReviewError, review_fix, review_queue
from ..sandbox import ReplayRequest
from ..service import InvestigationService
from ..settings import PROJECT_ROOT, Settings, get_settings
from ..store import PipelineRecorder, ReadOnlyTraceStore, seed_production_store
from .auth import require
from .dashboard import router as dashboard_router

log = logging.getLogger(__name__)
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


@dataclass
class Container:
    settings: Settings
    db: Database
    registry: ConfigRegistry
    intake: IntakeService
    queue: JobQueue
    service: InvestigationService
    pool: WorkerPool
    intake_limits: dict[str, TokenBucket]


def build_container(settings: Settings, db: Database | None = None, **service_kwargs: Any) -> Container:
    db = db or Database(settings.database_url)
    db.create_all()
    registry = get_registry()
    seed_production_store(db, registry)
    queue = JobQueue(db, settings.job_lease_seconds, settings.retry_base_seconds, settings.job_max_attempts)
    service = InvestigationService(db, registry, load_dataset(),
                                   reasoner_factory=service_kwargs.pop("reasoner_factory", None)
                                   or make_reasoner_factory(settings), **service_kwargs)
    return Container(settings=settings, db=db, registry=registry,
                     intake=IntakeService(db, registry, settings.queue_soft_limit, settings.queue_hard_limit,
                                          settings.job_max_attempts),
                     queue=queue, service=service, pool=WorkerPool(queue, service, settings.worker_concurrency),
                     intake_limits={})


def inv_to_dict(inv: InvestigationRow, include_state: bool = True) -> dict[str, Any]:
    d = {c: getattr(inv, c) for c in (
        "id", "status", "practice_id", "source_note_id", "pipeline_run_id", "pipeline_version", "policy_version",
        "predicted_codes", "expected_codes", "severity", "priority", "report_count", "conflicting_expectations",
        "similar", "symptom_signature", "root_cause_signature", "resolution", "reasoner", "reasoning_steps",
        "experiments_run", "regression_runs", "llm_calls", "tokens_used", "cost_usd")}
    for t in ("created_at", "started_at", "diagnosed_at", "fix_validated_at", "completed_at"):
        v = getattr(inv, t)
        d[t] = v.isoformat() + "Z" if v else None
    if include_state:
        d["state"] = inv.state
    return d


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    notes: str = Field("", max_length=2000)
    review_seconds: float | None = Field(None, ge=0)


class ManualReplayIn(BaseModel):
    label: str = Field(..., max_length=120)
    from_stage: Literal["extraction", "coding"] = "coding"
    field_overrides: dict[str, Any] = Field(default_factory=dict)
    candidate_change_ids: list[str] = Field(default_factory=list)
    billing_event_override: Literal["prep", "seat"] | None = None


class DevRunIn(BaseModel):
    run_id: str = Field(..., pattern=r"^[A-Za-z0-9_.:-]{3,64}$")
    note_id: str = Field(..., pattern=r"^[A-Za-z0-9_.:-]{3,64}$")
    practice_id: str = Field(..., pattern=r"^P-[A-Z0-9-]{2,30}$")
    policy_ref: str | None = None
    text: str = Field(..., max_length=4000, description="SYNTHETIC note text only")


def create_app(settings: Settings | None = None, container: Container | None = None,
               run_workers: bool = False) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()
        thread = None
        if run_workers:
            thread = threading.Thread(target=app.state.c.pool.run_forever, args=(stop,), daemon=True)
            thread.start()
        yield
        stop.set()
        if thread:
            thread.join(timeout=5)

    app = FastAPI(title="Codenta AI — Coding Pipeline Diagnostic Agent (prototype)",
                  description="Independent interview follow-up prototype. Synthetic data only.", version="0.1.0",
                  lifespan=lifespan)
    app.state.settings = settings
    app.state.c = container or build_container(settings)

    def c(request: Request) -> Container:
        return request.app.state.c

    @app.exception_handler(IntakeError)
    async def _intake_err(_: Request, exc: IntakeError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, BackpressureError) else None
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code, headers=headers)

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, Any]:
        return {"ok": True, "reasoner": c(request).settings.reasoner, "queue_depth": c(request).queue.depth()}

    # ------------------------------------------------------------ intake
    @app.post("/v1/issues", response_model=IntakeResult, status_code=201)
    def submit_issue(report: IssueReportIn, request: Request,
                     p: Principal = Depends(require("reporter", "admin"))) -> JSONResponse:
        ct = c(request)
        bucket = ct.intake_limits.setdefault(p.principal, TokenBucket(ct.settings.intake_rate_per_minute))
        if not bucket.try_acquire():
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(bucket.retry_after())})
        result = ct.intake.submit(report, p)
        code = 200 if (result.duplicate or result.idempotent_replay) else 201
        return JSONResponse(result.model_dump(mode="json"), status_code=code)

    @app.get("/v1/issues/{report_id}")
    def get_issue(report_id: str, request: Request,
                  p: Principal = Depends(require("reporter", "reviewer", "admin"))) -> dict[str, Any]:
        with c(request).db.session() as s:
            r = s.get(ReportRow, report_id)
            if r is None or not p.can_access(r.practice_id):
                raise HTTPException(404, "not found")
            inv = s.get(InvestigationRow, r.investigation_id)
            return {"report_id": r.id, "ticket_id": r.ticket_id, "investigation_id": inv.id,
                    "investigation_status": inv.status, "is_duplicate": r.is_duplicate,
                    "report_count": inv.report_count}

    # ------------------------------------------------------------ investigations
    def _get_inv(request: Request, inv_id: str, p: Principal) -> InvestigationRow:
        with c(request).db.session() as s:
            inv = s.get(InvestigationRow, inv_id)
        if inv is None or not p.can_access(inv.practice_id):
            raise HTTPException(404, "not found")
        return inv

    @app.get("/v1/investigations")
    def list_investigations(request: Request, status: str | None = None,
                            p: Principal = Depends(require("reporter", "reviewer", "admin"))) -> list[dict[str, Any]]:
        with c(request).db.session() as s:
            q = select(InvestigationRow).order_by(InvestigationRow.created_at)
            if status:
                q = q.where(InvestigationRow.status == status)
            rows = s.execute(q).scalars().all()
        return [inv_to_dict(r, include_state=False) for r in rows if p.can_access(r.practice_id)]

    @app.get("/v1/me")
    def me(p: Principal = Depends(require("reporter", "reviewer", "admin"))) -> dict[str, Any]:
        return {"principal": p.principal, "role": p.role, "practices": p.practices}

    @app.get("/v1/pipeline-runs")
    def list_runs(request: Request,
                  p: Principal = Depends(require("reporter", "reviewer", "admin"))) -> list[dict[str, Any]]:
        """Saved production runs visible to the caller (read-only), used to pick a run when filing a ticket."""
        with c(request).db.session() as s:
            rows = [(r.run_id, r.practice_id) for r in s.execute(
                select(PipelineRunRow).order_by(PipelineRunRow.run_id)).scalars() if p.can_access(r.practice_id)]
        out = []
        for run_id, practice in rows:
            store = ReadOnlyTraceStore(c(request).db, practice)
            run = store.get_run(run_id)
            out.append({"run_id": run.run_id, "practice_id": run.practice_id, "note_id": run.note_id,
                        "note_text": store.get_note(run.note_id).text, "policy_ref": run.policy_ref,
                        "pipeline_version": run.pipeline_version, "predicted_codes": run.predicted_codes,
                        "qa_passed": run.qa_passed, "extraction": run.trace("extraction").output})
        return out

    @app.get("/v1/investigations/{inv_id}")
    def get_investigation(inv_id: str, request: Request,
                          p: Principal = Depends(require("reporter", "reviewer", "admin"))) -> dict[str, Any]:
        inv = _get_inv(request, inv_id, p)
        d = inv_to_dict(inv)
        ctx = c(request).service.build_context(inv)
        d["note"] = {"note_id": ctx.note.note_id, "text": ctx.note.text, "synthetic": ctx.note.synthetic}
        d["run_policy"] = asdict(ctx.run_policy) | {"ref": ctx.run_policy.ref} if ctx.run_policy else None
        d["registry_policy"] = (asdict(ctx.registry_policy) | {"ref": ctx.registry_policy.ref}
                                if ctx.registry_policy else None)
        with c(request).db.session() as s:
            d["reports"] = [{"report_id": r.id, "ticket_id": r.ticket_id, "severity": r.severity,
                             "is_duplicate": r.is_duplicate, "error_description": r.error_description}
                            for r in s.execute(select(ReportRow).where(ReportRow.investigation_id == inv_id)
                                               .order_by(ReportRow.created_at)).scalars()]
            d["candidate_fixes"] = [{"id": f.id, "change_ids": f.change_ids, "gate_passed": f.gate_passed,
                                     "approval_status": f.approval_status}
                                    for f in s.execute(select(CandidateFixRow).where(
                                        CandidateFixRow.investigation_id == inv_id)).scalars()]
            d["known_issue_links"] = [{"known_issue_id": l.known_issue_id, "link_type": l.link_type,
                                       "evidence": l.evidence}
                                      for l in s.execute(select(InvestigationLinkRow).where(
                                          InvestigationLinkRow.investigation_id == inv_id)).scalars()]
        return d

    @app.get("/v1/investigations/{inv_id}/report.md", response_class=PlainTextResponse)
    def investigation_report(inv_id: str, request: Request,
                             p: Principal = Depends(require("reporter", "reviewer", "admin"))) -> str:
        return render_markdown(inv_to_dict(_get_inv(request, inv_id, p)))

    @app.get("/v1/investigations/{inv_id}/similar")
    def similar(inv_id: str, request: Request,
                p: Principal = Depends(require("reviewer", "admin"))) -> dict[str, Any]:
        inv = _get_inv(request, inv_id, p)
        return {"investigation_id": inv_id, "candidates": c(request).service._similar_fn(inv.id)(10),
                "note": "similarity is a retrieval signal; it never links root causes on its own"}

    @app.get("/v1/known-issues")
    def known_issues(request: Request, p: Principal = Depends(require("reviewer", "admin"))) -> list[dict[str, Any]]:
        with c(request).db.session() as s:
            kis = s.execute(select(KnownIssueRow)).scalars().all()
            links = s.execute(select(InvestigationLinkRow)).scalars().all()
        out = []
        for k in kis:
            ls = [l for l in links if l.known_issue_id == k.id]
            out.append({"id": k.id, "cause_id": k.cause_id, "stage": k.stage, "title": k.title, "status": k.status,
                        "parent_investigation_id": k.parent_investigation_id, "validated_fix_id": k.validated_fix_id,
                        "linked_investigations": [{"investigation_id": l.investigation_id, "link_type": l.link_type}
                                                  for l in ls], "history": k.history})
        return out

    # ------------------------------------------------------------ human review
    @app.get("/v1/review-queue")
    def get_review_queue(request: Request, p: Principal = Depends(require("reviewer", "admin"))) -> list[dict[str, Any]]:
        return review_queue(c(request).db, p.practices)

    @app.post("/v1/fixes/{fix_id}/review")
    def post_review(fix_id: str, body: ReviewIn, request: Request,
                    p: Principal = Depends(require("reviewer", "admin"))) -> dict[str, Any]:
        try:
            return review_fix(c(request).db, fix_id, p, body.decision, body.notes, body.review_seconds)
        except LookupError:
            raise HTTPException(404, "fix not found")
        except ReviewError as exc:
            raise HTTPException(409, str(exc))

    # ------------------------------------------------------------ ops
    @app.get("/v1/metrics")
    def metrics(request: Request, p: Principal = Depends(require("reviewer", "admin"))) -> dict[str, Any]:
        return compute_metrics(c(request).db, c(request).queue)

    @app.post("/v1/admin/process")
    def process(request: Request, p: Principal = Depends(require("admin"))) -> dict[str, Any]:
        processed = c(request).pool.drain()
        return {"processed_jobs": processed, "queue": c(request).queue.counts()}

    @app.post("/v1/admin/jobs/{job_id}/requeue")
    def requeue(job_id: int, request: Request, p: Principal = Depends(require("admin"))) -> dict[str, Any]:
        if not c(request).queue.requeue_dead(job_id):
            raise HTTPException(409, "job is not dead-lettered")
        return {"requeued": job_id}

    @app.post("/v1/investigations/{inv_id}/sandbox-replay")
    def manual_replay(inv_id: str, body: ManualReplayIn, request: Request,
                      p: Principal = Depends(require("reviewer", "admin"))) -> dict[str, Any]:
        inv = _get_inv(request, inv_id, p)
        ctx = c(request).service.build_context(inv)
        try:
            for path, value in body.field_overrides.items():
                ctx.registry.validate_field_override(path, value)
            res = ctx.engine.replay(ctx.note, ctx.run, ctx.base_config, ReplayRequest(**body.model_dump()),
                                    ctx.expected)
        except (KeyError, ValueError) as exc:
            raise HTTPException(422, str(exc))
        c(request).db.audit(p.principal, "manual_sandbox_replay", inv_id, {"experiment": res.experiment_id})
        return res.model_dump(mode="json")

    @app.post("/v1/dev/pipeline-runs", status_code=201)
    def dev_run(body: DevRunIn, request: Request, p: Principal = Depends(require("admin"))) -> dict[str, Any]:
        """DEV ONLY: simulate the production pipeline processing a synthetic note."""
        if c(request).settings.environment != "dev":
            raise HTTPException(404, "not available")
        rec = PipelineRecorder(c(request).db, c(request).registry)
        run = rec.record_run(body.run_id, SourceNote(note_id=body.note_id, practice_id=body.practice_id,
                                                     text=body.text), body.policy_ref)
        return {"run_id": run.run_id, "predicted_codes": run.predicted_codes, "qa_passed": run.qa_passed,
                "pipeline_version": run.pipeline_version}

    @app.get("/v1/dev/demo-scenarios")
    def list_scenarios(request: Request, p: Principal = Depends(require("admin"))) -> list[dict[str, Any]]:
        if c(request).settings.environment != "dev":
            raise HTTPException(404, "not available")
        return [{"id": s.id, "title": s.title, "description": s.description,
                 "tickets": sum(len(b) for b in s.batches)} for s in scenarios().values()]

    @app.post("/v1/dev/demo-scenarios/{scenario_id}")
    def run_demo_scenario(scenario_id: str, request: Request, p: Principal = Depends(require("admin"))) -> dict[str, Any]:
        """DEV ONLY: file a demo scenario's synthetic tickets and process the queue."""
        ct = c(request)
        if ct.settings.environment != "dev":
            raise HTTPException(404, "not available")
        if scenario_id not in scenarios():
            raise HTTPException(404, "unknown scenario")
        ct.db.audit(p.principal, "demo_scenario_run", details={"scenario": scenario_id})
        return run_scenario(scenario_id, ct.db, ct.intake, ct.pool.drain)

    app.include_router(dashboard_router)
    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built single-page frontend (frontend/dist) at /app, with client-side routing fallback."""
    dist = FRONTEND_DIST.resolve()
    if not (dist / "index.html").exists():
        @app.get("/app", include_in_schema=False)
        @app.get("/app/{path:path}", include_in_schema=False)
        def frontend_missing(path: str = "") -> PlainTextResponse:
            return PlainTextResponse("No frontend build found at frontend/dist. Use /dashboard or the API (/docs).",
                                     status_code=503)
        return

    @app.get("/app", include_in_schema=False)
    @app.get("/app/{path:path}", include_in_schema=False)
    def frontend(path: str = "") -> FileResponse:
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/app")
