from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .state import BudgetUsage


class BudgetExceeded(Exception):
    def __init__(self, dimension: str, detail: str) -> None:
        super().__init__(f"budget exhausted: {dimension} ({detail})")
        self.dimension = dimension
        self.detail = detail


@dataclass(frozen=True)
class AgentBudget:
    max_reasoning_steps: int = 40
    max_experiments: int = 24
    max_regression_runs: int = 10
    max_tokens: int = 120_000
    max_cost_usd: float = 1.0
    deadline_seconds: float = 600.0

    @staticmethod
    def from_config(cfg: dict[str, Any], **overrides: Any) -> "AgentBudget":
        return AgentBudget(**{**cfg["budget"], **overrides})

    def elapsed(self, usage: BudgetUsage) -> float:
        return usage.elapsed_before_resume + (time.time() - usage.started_at)

    def check(self, usage: BudgetUsage, *, need: str | None = None) -> None:
        """Raise before an action that would exceed the budget."""
        if self.elapsed(usage) > self.deadline_seconds:
            raise BudgetExceeded("deadline", f"{self.elapsed(usage):.1f}s > {self.deadline_seconds}s")
        if usage.reasoning_steps >= self.max_reasoning_steps:
            raise BudgetExceeded("reasoning_steps", f"{usage.reasoning_steps}/{self.max_reasoning_steps}")
        if usage.tokens_in + usage.tokens_out >= self.max_tokens:
            raise BudgetExceeded("tokens", f"{usage.tokens_in + usage.tokens_out}/{self.max_tokens}")
        if usage.cost_usd >= self.max_cost_usd:
            raise BudgetExceeded("cost", f"${usage.cost_usd:.4f}/${self.max_cost_usd}")
        if need == "experiment" and usage.experiments >= self.max_experiments:
            raise BudgetExceeded("experiments", f"{usage.experiments}/{self.max_experiments}")
        if need == "regression" and usage.regression_runs >= self.max_regression_runs:
            raise BudgetExceeded("regression_runs", f"{usage.regression_runs}/{self.max_regression_runs}")

    def remaining(self, usage: BudgetUsage) -> dict[str, Any]:
        return {
            "reasoning_steps": self.max_reasoning_steps - usage.reasoning_steps,
            "experiments": self.max_experiments - usage.experiments,
            "regression_runs": self.max_regression_runs - usage.regression_runs,
            "tokens": self.max_tokens - usage.tokens_in - usage.tokens_out,
            "seconds": round(self.deadline_seconds - self.elapsed(usage), 1),
        }
