"""Server-rendered human review dashboard (Jinja2). One process, no frontend build, testable via TestClient.

Optimised for scarce expert time: the index page is a single prioritised decision queue (one item
per root cause, not per ticket); the detail page puts the evidence, experiments and before/after
regression numbers on one screen so a reviewer does not need to repeat the agent's work.
"""
from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..db import CandidateFixRow, InvestigationLinkRow, InvestigationRow, KnownIssueRow, ReportRow
from ..intake import Principal
from ..metrics import compute_metrics
from ..review import ReviewError, review_fix, review_queue
from .auth import principal_from_key

router = APIRouter(prefix="/dashboard", include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
COOKIE = "codenta_key"


def _principal(request: Request) -> Principal | None:
    p = principal_from_key(request, request.cookies.get(COOKIE))
    return p if p and p.role in ("reviewer", "admin") else None


def highlight(text: str, spans: list[dict[str, Any]]) -> str:
    """Escape note text and wrap evidence spans in <mark> (spans come from deterministic checks)."""
    marks = sorted({(s["start"], s["end"]) for s in spans if 0 <= s["start"] < s["end"] <= len(text)})
    out, pos = [], 0
    for start, end in marks:
        if start < pos:
            continue
        out.append(html.escape(text[pos:start]))
        out.append(f"<mark>{html.escape(text[start:end])}</mark>")
        pos = end
    out.append(html.escape(text[pos:]))
    return "".join(out)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, api_key: str = Form(...)) -> Any:
    p = principal_from_key(request, api_key)
    if p is None or p.role not in ("reviewer", "admin"):
        return templates.TemplateResponse(request, "login.html", {"error": "invalid reviewer key"}, status_code=401)
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(COOKIE, api_key, httponly=True, samesite="strict")
    return resp


@router.get("", response_class=HTMLResponse)
def index(request: Request) -> Any:
    p = _principal(request)
    if p is None:
        return RedirectResponse("/dashboard/login", status_code=303)
    c = request.app.state.c
    with c.db.session() as s:
        invs = [i for i in s.execute(select(InvestigationRow).order_by(InvestigationRow.created_at.desc())).scalars()
                if p.can_access(i.practice_id)]
    return templates.TemplateResponse(request, "index.html", {
        "principal": p, "metrics": compute_metrics(c.db, c.queue), "queue": review_queue(c.db, p.practices),
        "investigations": invs, "reasoner": c.settings.reasoner})


@router.get("/investigations/{inv_id}", response_class=HTMLResponse)
def detail(inv_id: str, request: Request) -> Any:
    p = _principal(request)
    if p is None:
        return RedirectResponse("/dashboard/login", status_code=303)
    c = request.app.state.c
    with c.db.session() as s:
        inv = s.get(InvestigationRow, inv_id)
        if inv is None or not p.can_access(inv.practice_id):
            return HTMLResponse("not found", status_code=404)
        fixes = s.execute(select(CandidateFixRow).where(CandidateFixRow.investigation_id == inv_id)).scalars().all()
        links = s.execute(select(InvestigationLinkRow).where(
            InvestigationLinkRow.investigation_id == inv_id)).scalars().all()
        kis = {k.id: k for k in s.execute(select(KnownIssueRow)).scalars().all()}
        all_links = s.execute(select(InvestigationLinkRow)).scalars().all()
        reports = s.execute(select(ReportRow).where(ReportRow.investigation_id == inv_id)).scalars().all()
    ctx = c.service.build_context(inv)
    state = inv.state or {}
    spans = [sp for o in state.get("observations", []) for sp in o.get("evidence_spans", [])]
    group_sizes = {k: len({l.investigation_id for l in all_links if l.known_issue_id == k}) for k in kis}
    status_next = {
        "fix_validated": "Review the validated fix bundle below and approve or reject it for the release process.",
        "escalated": "Answer the escalation questions below; the agent has stopped automated attempts.",
        "linked_known_issue": "No action: covered by the parent investigation's validated fix.",
        "approved": "Approved. Hand off to the standard change/deployment process (not automated here).",
        "rejected": "Rejected by reviewer. Revise candidate changes or close.",
    }.get(inv.status, "Investigation in progress.")
    return templates.TemplateResponse(request, "investigation.html", {
        "principal": p, "inv": inv, "state": state, "fixes": fixes, "links": links, "kis": kis,
        "group_sizes": group_sizes, "reports": reports, "note_html": highlight(ctx.note.text, spans),
        "policy": ctx.run_policy, "registry_policy": ctx.registry_policy, "next_action": status_next,
        "opened_at": time.time()})


@router.post("/fixes/{fix_id}/review")
def review(fix_id: str, request: Request, decision: str = Form(...), notes: str = Form(""),
           opened_at: float = Form(0.0)) -> Any:
    p = _principal(request)
    if p is None:
        return RedirectResponse("/dashboard/login", status_code=303)
    c = request.app.state.c
    seconds = round(time.time() - opened_at, 1) if opened_at else None
    try:
        result = review_fix(c.db, fix_id, p, decision, notes, seconds)
    except (ReviewError, LookupError) as exc:
        return HTMLResponse(f"review failed: {html.escape(str(exc))}", status_code=409)
    return RedirectResponse(f"/dashboard/investigations/{result['investigation_id']}", status_code=303)
