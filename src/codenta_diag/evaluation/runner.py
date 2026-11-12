"""Cached regression runner used by the agent's `run_targeted_evaluation` tool and the fix generator."""
from __future__ import annotations

from ..config_registry import ConfigRegistry, stable_hash
from ..domain import PipelineConfig
from ..sandbox.cache import InMemoryCache, ResultCache
from .dataset import EvalDataset
from .metrics import EvalSummary, evaluate


class RegressionRunner:
    def __init__(self, registry: ConfigRegistry, dataset: EvalDataset, cache: ResultCache | None = None) -> None:
        self.registry = registry
        self.dataset = dataset
        self.cache = cache or InMemoryCache()
        self._fp = registry.fingerprint()

    def summary(self, config: PipelineConfig) -> tuple[EvalSummary, bool]:
        key = "eval:" + stable_hash({"dataset": self.dataset.ref, "components": config.components(),
                                     "config_fp": self._fp}, 24)
        if (hit := self.cache.get(key)) is not None:
            return EvalSummary(**hit).model_copy(update={"config_label": config.version}), True
        summary = evaluate(config, self.dataset, self.registry)
        self.cache.put(key, "eval", summary.model_dump(mode="json"))
        return summary, False
