from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .state import InvestigationState, Phase
from .tools import ToolContext


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    rationale: str = ""
    phase: Phase = "test"
    call_id: str | None = None   # LLM tool_use id, when applicable


@dataclass
class ReasonerOutput:
    actions: list[ToolCall]
    tokens_in: int = 0
    tokens_out: int = 0
    llm_call: bool = False
    text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Reasoner(Protocol):
    kind: str
    is_llm: bool

    def next_actions(self, state: InvestigationState, ctx: ToolContext) -> ReasonerOutput: ...

    def record_results(self, state: InvestigationState, results: list[tuple[ToolCall, dict[str, Any]]]) -> None: ...
