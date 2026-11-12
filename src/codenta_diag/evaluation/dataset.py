from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ..settings import PROJECT_ROOT

DEFAULT_DATASET = PROJECT_ROOT / "data" / "eval" / "dental_eval_v1.yaml"


class EvalCase(BaseModel):
    id: str
    note: str
    practice_id: str
    run_policy_ref: str | None
    true_policy_ref: str
    expected_codes: list[str]
    gold: dict[str, Any] | None = None
    categories: list[str] = Field(default_factory=list)
    critical: bool = False


class EvalDataset(BaseModel):
    id: str
    version: int
    rule_card: str
    cases: list[EvalCase]

    @property
    def ref(self) -> str:
        return f"{self.id}@v{self.version}"


@lru_cache
def load_dataset(path: Path = DEFAULT_DATASET) -> EvalDataset:
    with Path(path).open() as fh:
        raw = yaml.safe_load(fh)
    ds = EvalDataset(**raw)
    ids = [c.id for c in ds.cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids in evaluation dataset")
    return ds
