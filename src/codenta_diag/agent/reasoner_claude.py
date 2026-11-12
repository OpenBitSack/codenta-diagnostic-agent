"""LLM-backed reasoner (Anthropic Claude tool use).

Claude chooses hypotheses and experiments by calling the SAME tools as the deterministic reasoner.
The controller still enforces budgets, executes tools, and verifies that every claimed verdict cites
experiments that actually ran. Deterministic observation and fix validation are not delegated to
the model. Requires ANTHROPIC_API_KEY and `pip install .[llm]`.

Data handling: this prototype only sends synthetic notes. Sending real clinical notes to any LLM
provider requires an appropriate BAA / data-processing agreement and PHI controls.
"""
from __future__ import annotations

import json
from typing import Any

from ..jobs.ratelimit import TokenBucket
from .reasoner_base import ReasonerOutput, ToolCall
from .state import InvestigationState
from .tools import TOOLS, ToolContext

SYSTEM_PROMPT = """You are a bounded diagnostic agent. A FIXED, multi-stage dental-coding prediction pipeline \
(extraction -> coding -> QA) produced wrong CDT codes that passed automated QA. Find the root cause(s).

Rules:
- Deterministic observations are FACTS about the saved trace. Hypotheses are NOT facts until tested.
- First call record_hypotheses with multiple candidate causes (prompt, schema, model, policy handling, QA).
  Use cause ids like extraction.<group>.<field>.value_incorrect, extraction.schema.<group>.<field>.unrepresentable_value,
  coding.policy.not_applied, input.policy.missing_at_runtime, coding.policy.silent_default_when_missing, qa.missed_error,
  model.capability.
- Test hypotheses with run_sandbox_replay. Prefer cheap experiments (single-field injections) first. One issue can have
  several simultaneous causes: after one correction, check whether the error REMAINS and keep investigating.
- A cause is 'supported' only if you cite experiment ids (or observation ids) that support it. Unsupported claims will be
  downgraded automatically. Mark hypotheses you cannot test in this sandbox as 'untestable'.
- You cannot modify production. Candidate changes are sandbox-only; fix validation runs after you submit.
- Stop as soon as evidence is sufficient: call submit_diagnosis. If no safe conclusion is possible (e.g. the label
  conflicts with the documentation), call create_escalation_report with specific questions a human can answer.
- Be economical: every response costs budget."""


def _truncate(obj: Any, limit: int = 6000) -> str:
    text = json.dumps(obj, default=str)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


PHASE = {"record_hypotheses": "hypothesize", "run_sandbox_replay": "test", "run_targeted_evaluation": "test",
         "submit_diagnosis": "conclude", "create_escalation_report": "escalated"}


class ClaudeReasoner:
    kind = "claude"
    is_llm = True

    def __init__(self, client: Any, model: str, rate_limiter: TokenBucket | None = None,
                 max_tokens_per_call: int = 2048) -> None:
        self.client = client
        self.model = model
        self.rate_limiter = rate_limiter
        self.max_tokens = max_tokens_per_call

    def _initial_message(self, state: InvestigationState, ctx: ToolContext) -> str:
        catalog = [{"id": c["id"], "component": c["component"], "description": c["description"]}
                   for c in ctx.registry.candidate_catalog()]
        return _truncate({
            "investigation_id": state.investigation_id,
            "ticket": {"run_id": ctx.run.run_id, "pipeline_version": ctx.run.pipeline_version,
                       "predicted_codes": ctx.run.predicted_codes, "expected_codes": ctx.expected,
                       "qa_passed": ctx.run.qa_passed},
            "note": ctx.note.text,
            "policy": {"attached_to_run": ctx.run.policy_ref,
                       "registry_current": ctx.registry_policy.ref if ctx.registry_policy else None},
            "extraction_output": ctx.run.trace("extraction").output,
            "deterministic_observations": state.observations,
            "earliest_observable_incorrect_stage": state.earliest_incorrect_stage,
            "sandbox_candidate_catalog": catalog,
            "budget": {"max_reasoning_steps": "see controller", "note": "each of your responses is one step"},
        }, 12000)

    def next_actions(self, state: InvestigationState, ctx: ToolContext) -> ReasonerOutput:
        if not state.llm_messages:
            state.llm_messages = [{"role": "user", "content": self._initial_message(state, ctx)}]
        if self.rate_limiter is not None and not self.rate_limiter.acquire(timeout=60):
            raise RuntimeError("LLM rate limiter: no capacity within 60s (backpressure)")
        resp = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=SYSTEM_PROMPT,
            tools=[t.json_schema() for t in TOOLS.values()], messages=state.llm_messages)
        blocks: list[dict[str, Any]] = []
        actions: list[ToolCall] = []
        text = ""
        for b in resp.content:
            if b.type == "text":
                text += b.text
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                actions.append(ToolCall(b.name, dict(b.input), rationale=text.strip()[:500] or f"LLM chose {b.name}",
                                        phase=PHASE.get(b.name, "test"), call_id=b.id))  # type: ignore[arg-type]
        state.llm_messages.append({"role": "assistant", "content": blocks})
        return ReasonerOutput(actions, tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
                              llm_call=True, text=text)

    def record_results(self, state: InvestigationState, results: list[tuple[ToolCall, dict[str, Any]]]) -> None:
        if state.phase not in ("hypothesize", "test", "conclude"):
            return
        if not results:
            state.llm_messages.append({"role": "user", "content": "Call a tool. When evidence is sufficient call "
                                       "submit_diagnosis, or create_escalation_report if no safe conclusion exists."})
            return
        state.llm_messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.call_id, "content": _truncate(result)}
            for call, result in results if call.call_id]})
