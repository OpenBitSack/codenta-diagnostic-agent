from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEV_API_KEYS: dict[str, dict] = {
    "dev-reporter-seat": {"principal": "reporter-seat", "role": "reporter", "practices": ["P-SEAT-01"]},
    "dev-reporter-prep": {"principal": "reporter-prep", "role": "reporter", "practices": ["P-PREP-02"]},
    "dev-reviewer": {"principal": "reviewer-1", "role": "reviewer", "practices": ["*"]},
    "dev-admin": {"principal": "admin-1", "role": "admin", "practices": ["*"]},
}


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./codenta_diag.db"
    config_dir: Path = PROJECT_ROOT / "config"
    data_dir: Path = PROJECT_ROOT / "data"
    reasoner: str = "deterministic"  # "deterministic" | "claude"
    llm_model: str = "claude-sonnet-4-5"
    anthropic_api_key: str | None = None
    worker_concurrency: int = 4
    queue_soft_limit: int = 200
    queue_hard_limit: int = 1000
    intake_rate_per_minute: int = 600
    llm_calls_per_minute: int = 50
    job_max_attempts: int = 3
    job_lease_seconds: int = 120
    retry_base_seconds: float = 2.0
    environment: str = "dev"
    api_keys: dict[str, dict] = field(default_factory=lambda: dict(DEV_API_KEYS))

    @staticmethod
    def from_env() -> "Settings":
        keys = DEV_API_KEYS
        if raw := os.getenv("CODENTA_API_KEYS_JSON"):
            keys = json.loads(raw)
        env = os.getenv("CODENTA_ENV", "dev")
        if env != "dev" and keys is DEV_API_KEYS:
            raise RuntimeError("Dev API keys are not allowed outside CODENTA_ENV=dev")
        return Settings(
            database_url=os.getenv("CODENTA_DATABASE_URL", "sqlite:///./codenta_diag.db"),
            reasoner=os.getenv("CODENTA_REASONER", "deterministic"),
            llm_model=os.getenv("CODENTA_LLM_MODEL", "claude-sonnet-4-5"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            worker_concurrency=int(os.getenv("CODENTA_WORKER_CONCURRENCY", "4")),
            queue_soft_limit=int(os.getenv("CODENTA_QUEUE_SOFT_LIMIT", "200")),
            queue_hard_limit=int(os.getenv("CODENTA_QUEUE_HARD_LIMIT", "1000")),
            environment=env,
            api_keys=keys,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
