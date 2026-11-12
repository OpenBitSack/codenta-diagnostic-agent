from __future__ import annotations

from typing import Callable

from .agent.reasoner_base import Reasoner
from .agent.reasoner_deterministic import DeterministicReasoner
from .jobs.ratelimit import TokenBucket
from .settings import Settings


def make_reasoner_factory(settings: Settings) -> Callable[[], Reasoner]:
    if settings.reasoner == "deterministic":
        return DeterministicReasoner
    if settings.reasoner == "claude":
        if not settings.anthropic_api_key:
            raise RuntimeError("CODENTA_REASONER=claude requires ANTHROPIC_API_KEY")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("install the llm extra: pip install '.[llm]'") from exc
        from .agent.reasoner_claude import ClaudeReasoner

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3)
        bucket = TokenBucket(settings.llm_calls_per_minute)
        return lambda: ClaudeReasoner(client, settings.llm_model, bucket)
    raise ValueError(f"unknown reasoner {settings.reasoner!r}")
