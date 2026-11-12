"""Investigation service: runs one canonical investigation end-to-end and persists the outcome."""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from sqlalchemy import select

from .agent import AgentBudget, AgentController, DeterministicReasoner, InvestigationState, ToolContext
from .agent.reasoner_base import Reasoner
from .config_registry import ConfigRegistry
from .db import CandidateFixRow, Database, DbCache, InvestigationRow
from .dedup import rank_candidates
from .dedup.grouping import link_by_replay, record_confirmed_causes, record_escalation_group
from .domain import utcnow
from .evaluation import EvalDataset
from .evaluation.runner import RegressionRunner
from .pipeline import production_config
from .sandbox import ReplayRequest, SandboxReplayEngine
from .store import ReadOnlyTraceStore

log = logging.getLogger(__name__)

TERMINAL = {"fix_validated", "escalated", "linked_known_issue", "approved", "rejected", "failed"}
ReasonerFactory = Callable[[], Reasoner]


class InvestigationService:
    def __init__(self, db: Database, registry: ConfigRegistry, dataset: EvalDataset,
                 reasoner_factory: ReasonerFactory | None = None, budget_overrides: dict[str, Any] | None = None,
                 fast_path: bool | None = None) -> None:
        self.db = db
        self.registry = registry
        self.dataset = dataset
        self.reasoner_factory = reasoner_factory or DeterministicReasoner
        self.cache = DbCache(db)
        self.engine = SandboxReplayEngine(registry, self.cache)
        self.regression = RegressionRunner(registry, dataset, self.cache)
        cfg = registry.agent_config()
        self.budget = AgentBudget.from_config(cfg, **(budget_overrides or {}))
        self.gate_cfg = cfg["quality_gate"]
        self.fast_path = cfg["fast_path"]["enabled"] if fast_path is None else fast_path
        self.min_similarity = cfg["fast_path"]["min_similarity"]
        self.crash_after_step: int | None = None  # test hook: simulate a worker crash mid-investigation

    # ------------------------------------------------------------------ context
    def _similar_fn(self, inv_id: str) -> Callable[[int], list[dict[str, Any]]]:
        def fn(limit: int) -> list[dict[str, Any]]:
            with self.db.session() as s:
                rows = s.execute(select(InvestigationRow)).scalars().all()
                target = next(r for r in rows if r.id == inv_id)
                return rank_candidates(self._sim_dict(target), [self._sim_dict(r) for r in rows],
                                       self.min_similarity, limit)
        return fn

    @staticmethod
    def _sim_dict(r: InvestigationRow) -> dict[str, Any]:
        return {"id": r.id, "features": r.features, "embedding": r.embedding, "status": r.status,
                "symptom_signature": r.symptom_signature, "root_cause_signature": r.root_cause_signature}

    def build_context(self, inv: InvestigationRow) -> ToolContext:
        store = ReadOnlyTraceStore(self.db, inv.practice_id)
        run = store.get_run(inv.pipeline_run_id)
        note = store.get_note(inv.source_note_id)
        return ToolContext(
            investigation_id=inv.id, practice_id=inv.practice_id, note=note, run=run,
            expected=inv.expected_codes, run_policy=self.registry.policy_from_ref(run.policy_ref),
            registry_policy=self.registry.policy(inv.practice_id),
            base_config=production_config(self.registry, run.pipeline_version), registry=self.registry,
            engine=self.engine, regression=self.regression, gate_cfg=self.gate_cfg,
            similar_fn=self._similar_fn(inv.id))

    # ------------------------------------------------------------------ persistence helpers
    def _checkpoint_fn(self, inv_id: str, heartbeat: Callable[[], Any]) -> Callable[[InvestigationState], None]:
        def checkpoint(state: InvestigationState) -> None:
            with self.db.session() as s:
                inv = s.get(InvestigationRow, inv_id)
                inv.state = state.model_dump(mode="json")
                inv.symptom_signature = state.symptom_signature or None
                inv.reasoning_steps = state.budget.reasoning_steps
                inv.experiments_run = state.budget.experiments
                inv.regression_runs = state.budget.regression_runs
                inv.llm_calls = state.budget.llm_calls
                inv.tokens_used = state.budget.tokens_in + state.budget.tokens_out
                inv.cost_usd = state.budget.cost_usd
                if state.diagnosis and inv.diagnosed_at is None:
                    inv.diagnosed_at = utcnow()
            heartbeat()
            if self.crash_after_step is not None and state.budget.reasoning_steps >= self.crash_after_step:
                self.crash_after_step = None
                raise RuntimeError("simulated worker crash (test hook)")
        return checkpoint

    def _audit_fn(self, inv_id: str) -> Callable[[str, dict[str, Any]], None]:
        def audit(action: str, details: dict[str, Any]) -> None:
            self.db.audit("diagnostic-agent", action, inv_id, details)
        return audit

    # ------------------------------------------------------------------ fast path
    def _try_fast_path(self, inv: InvestigationRow, ctx: ToolContext, state: InvestigationState,
                       checkpoint: Callable[[InvestigationState], None]) -> str | None:
        if not state.symptom_signature or ctx.expected is None:
            return None
        with self.db.session() as s:
            rows = s.execute(select(InvestigationRow).where(
                InvestigationRow.symptom_signature == state.symptom_signature,
                InvestigationRow.id != inv.id,
                InvestigationRow.status.in_(["fix_validated", "approved", "linked_known_issue", "escalated"]))
            ).scalars().all()
            peers = [(r.id, r.status, r.state or {}, self._sim_dict(r)) for r in rows]
        me = self._sim_dict(inv)
        me["embedding"] = inv.embedding
        ranked = {c["investigation_id"]: c["score"] for c in rank_candidates(me, [p[3] for p in peers], 0.0, 50)}
        peers.sort(key=lambda p: -ranked.get(p[0], 0.0))

        for peer_id, status, peer_state, _ in peers:
            fix = peer_state.get("validated_fix") or (peer_state.get("fast_path") or {}).get("fix")
            if fix:
                exp = self.engine.replay(ctx.note, ctx.run, ctx.base_config, ReplayRequest(
                    label=f"fast-path:replay-validated-fix-of:{peer_id}", from_stage="extraction",
                    candidate_change_ids=fix["change_ids"]), ctx.expected)
                record = exp.model_dump(mode="json")
                record["tests_hypotheses"] = []
                state.experiments.append(record)
                state.budget.experiments += 1
                if exp.comparison and exp.comparison.exact_match:
                    parent = (peer_state.get("fast_path") or {}).get("parent", peer_id)
                    with self.db.session() as s:
                        linked = link_by_replay(s, inv.id, parent, exp.experiment_id, ranked.get(peer_id))
                    state.fast_path = {"parent": parent, "fix": fix, "experiment": exp.experiment_id,
                                       "similarity": ranked.get(peer_id)}
                    state.diagnosis = {"resolution": "known_issue_replay", "parent_investigation": parent,
                                       "experiment": exp.experiment_id, "known_issues": linked,
                                       "reasoner": "none (deterministic replay)",
                                       "summary": "Identical deterministic symptom signature and the parent's "
                                                  "validated fix makes this ticket correct in the sandbox."}
                    state.phase = "done"
                    checkpoint(state)
                    self.db.incr("full_investigations_avoided")
                    self.db.incr("reasoning_steps_avoided", int((peer_state.get("budget") or {}).get(
                        "reasoning_steps", 0)))
                    return "linked_known_issue"
            elif status == "escalated":
                state.escalation = {**(peer_state.get("escalation") or {}), "grouped_with": peer_id,
                                    "note": "Grouped with an open escalation that has an identical deterministic "
                                            "symptom signature; one human answer applies to both."}
                state.phase = "escalated"
                checkpoint(state)
                with self.db.session() as s:
                    record_escalation_group(s, inv.id, state.model_dump(mode="json"))
                self.db.incr("escalations_grouped")
                self.db.incr("reasoning_steps_avoided", int((peer_state.get("budget") or {}).get(
                    "reasoning_steps", 0)))
                return "escalated"
        return None

    # ------------------------------------------------------------------ main
    def run(self, inv_id: str, heartbeat: Callable[[], Any] = lambda: None) -> str:
        with self.db.session() as s:
            inv = s.get(InvestigationRow, inv_id)
            if inv is None:
                raise LookupError(inv_id)
            if inv.status in TERMINAL:
                return inv.status
            inv.status = "investigating"
            inv.started_at = inv.started_at or utcnow()
            saved_state = inv.state
        ctx = self.build_context(inv)
        reasoner = self.reasoner_factory()
        checkpoint = self._checkpoint_fn(inv_id, heartbeat)
        controller = AgentController(ctx, reasoner, self.budget, checkpoint, self._audit_fn(inv_id))
        state = InvestigationState(**saved_state) if saved_state else controller.new_state()
        if state.phase == "observe":
            controller.observe(state)
            with self.db.session() as s:
                row = s.get(InvestigationRow, inv_id)
                row.embedding = self._embed_with_symptoms(row, state.symptom_signature)
        if self.fast_path and state.phase == "hypothesize" and not state.hypotheses:
            fast = self._try_fast_path(inv, ctx, state, checkpoint)
            if fast:
                return self._finish(inv_id, fast, "known_issue_replay" if fast == "linked_known_issue"
                                    else "grouped_escalation", reasoner.kind)
        state = controller.run(state)
        return self._persist_outcome(inv_id, state, reasoner.kind)

    def _embed_with_symptoms(self, row: InvestigationRow, signature: str) -> list[float]:
        from .dedup.similarity import embed
        return embed(row.features, signature)

    def _persist_outcome(self, inv_id: str, state: InvestigationState, reasoner_kind: str) -> str:
        sdump = state.model_dump(mode="json")
        fix_id = None
        with self.db.session() as s:
            for attempt in state.fix_attempts:
                fid = f"FIX-{uuid.uuid4().hex[:8]}"
                s.add(CandidateFixRow(id=fid, investigation_id=inv_id, change_ids=attempt["change_ids"],
                                      gate_passed=attempt["gate_passed"], evaluation=attempt,
                                      approval_status="pending_review" if attempt["gate_passed"] else "not_submitted"))
                if attempt["gate_passed"]:
                    fix_id = fid
            if state.phase == "done" and state.validated_fix:
                status, resolution = "fix_validated", "full_investigation"
                record_confirmed_causes(s, inv_id, sdump, fix_id)
            else:
                status, resolution = "escalated", "escalated"
                record_confirmed_causes(s, inv_id, sdump, None)
                record_escalation_group(s, inv_id, sdump)
            inv = s.get(InvestigationRow, inv_id)
            inv.root_cause_signature = "|".join(sorted(h.cause_id for h in state.hypotheses
                                                       if h.status == "supported")) or None
            if status == "fix_validated":
                inv.fix_validated_at = utcnow()
        self.db.incr(f"investigations_{status}")
        return self._finish(inv_id, status, resolution, reasoner_kind)

    def _finish(self, inv_id: str, status: str, resolution: str, reasoner_kind: str) -> str:
        with self.db.session() as s:
            inv = s.get(InvestigationRow, inv_id)
            inv.status = status
            inv.resolution = resolution
            inv.reasoner = reasoner_kind if resolution in ("full_investigation", "escalated") else "none"
            inv.completed_at = utcnow()
            if inv.diagnosed_at is None and status in ("fix_validated", "linked_known_issue"):
                inv.diagnosed_at = utcnow()
        self.db.audit("diagnostic-agent", f"investigation_{status}", inv_id, {"resolution": resolution})
        return status
