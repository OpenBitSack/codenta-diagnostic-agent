"""Versioned configuration layer.

Everything domain-specific (codes, extraction fields, policies, QA rules, pipeline stage versions,
candidate changes, agent budgets) is loaded from `config/` so that new CDT codes, policies or error
categories are configuration changes rather than code changes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .settings import PROJECT_ROOT


def _load(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        return yaml.safe_load(fh)


def stable_hash(obj: Any, length: int = 16) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:length]


@dataclass(frozen=True)
class PolicyVersion:
    practice_id: str
    version: str
    crown_billing_event: str
    text: str

    @property
    def ref(self) -> str:
        return f"{self.practice_id}@{self.version}"


class ConfigRegistry:
    def __init__(self, config_dir: Path | None = None) -> None:
        self.dir = config_dir or PROJECT_ROOT / "config"
        self._cache: dict[str, dict] = {}

    def _get(self, rel: str) -> dict[str, Any]:
        if rel not in self._cache:
            self._cache[rel] = _load(self.dir / rel)
        return self._cache[rel]

    def rule_card(self, card_id: str = "interview-v1") -> dict[str, Any]:
        return self._get(f"rule_cards/{card_id}.yaml")

    def extraction_schema(self, schema_id: str) -> dict[str, Any]:
        return self._get(f"extraction_schemas/{schema_id}.yaml")

    def validate_field_override(self, path: str, value: Any) -> None:
        """Raise ValueError unless `path`/`value` is representable in at least one known extraction schema."""
        group, _, name = path.partition(".")
        specs = [s["fields"][group][name] for p in sorted((self.dir / "extraction_schemas").glob("*.yaml"))
                 if name in (s := self.extraction_schema(p.stem))["fields"].get(group, {})]
        if not specs:
            raise ValueError(f"unknown extraction field {path!r}")
        for spec in specs:
            if value is None and spec.get("nullable"):
                return
            if "enum" in spec and value in spec["enum"]:
                return
            if spec.get("type") == "int" and isinstance(value, int) and not isinstance(value, bool):
                return
            if spec.get("type") == "bool" and isinstance(value, bool):
                return
            if spec.get("type") == "list" and isinstance(value, list) and set(value) <= set(spec.get("items", [])):
                return
        allowed = sorted({str(v) for s in specs for v in s.get("enum", s.get("items", []))})
        raise ValueError(f"value {value!r} is not valid for {path!r}" + (f"; allowed: {allowed}" if allowed else ""))

    def lexicon(self, lexicon_id: str = "v1") -> dict[str, Any]:
        return self._get(f"lexicons/{lexicon_id}.yaml")

    def qa_rules(self, qa_id: str) -> list[str]:
        return list(self._get(f"qa_rules/{qa_id}.yaml")["rules"])

    def pipelines(self) -> dict[str, Any]:
        return self._get("pipelines/pipelines.yaml")

    def pipeline_version(self, version: str) -> dict[str, Any]:
        versions = self.pipelines()["versions"]
        if version not in versions:
            raise KeyError(f"Unknown pipeline version {version!r}")
        return dict(versions[version])

    def candidate_catalog(self) -> list[dict[str, Any]]:
        return list(self._get("candidate_changes/catalog-v1.yaml")["changes"])

    def candidate_change(self, change_id: str) -> dict[str, Any]:
        for change in self.candidate_catalog():
            if change["id"] == change_id:
                return change
        raise KeyError(f"Unknown candidate change {change_id!r}")

    def agent_config(self) -> dict[str, Any]:
        return self._get("agent.yaml")

    def practices(self) -> dict[str, Any]:
        return self._get("policies/practices.yaml")["practices"]

    def policy(self, practice_id: str, version: str | None = None) -> PolicyVersion | None:
        practice = self.practices().get(practice_id)
        if practice is None:
            return None
        version = version or practice["current_version"]
        spec = practice["versions"].get(version)
        if spec is None:
            return None
        return PolicyVersion(practice_id, version, spec["crown_billing_event"], spec["text"])

    def policy_from_ref(self, ref: str | None) -> PolicyVersion | None:
        if not ref:
            return None
        practice_id, _, version = ref.partition("@")
        return self.policy(practice_id, version or None)

    def fingerprint(self) -> str:
        """Hash of all loaded config: recorded with every experiment for reproducibility."""
        files = sorted(p for p in self.dir.rglob("*.yaml"))
        return stable_hash({str(p.relative_to(self.dir)): p.read_text() for p in files}, 12)


@lru_cache
def get_registry() -> ConfigRegistry:
    return ConfigRegistry()
