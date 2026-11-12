"""Similarity-based CANDIDATE retrieval (never proof of a shared root cause).

Stage 1: structured metadata pre-filter (same pipeline version or overlapping procedure families).
Stage 2: cosine similarity over a local hashed feature embedding.

Production path: store `embedding` in a pgvector `vector(256)` column with an HNSW/IVFFlat index and
replace the in-Python cosine with `ORDER BY embedding <=> :q`. Swapping the hashed embedding for a
text-embedding model is possible but unnecessary for structured features like codes and stages.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any

DIM = 256
STOPWORDS = {"the", "and", "but", "was", "for", "per", "not", "should", "billed", "today", "patient", "with"}
WEIGHTS = {"fp": 1.5, "fn": 1.5, "fpp": 3.0, "fnp": 3.0, "proc": 2.0, "sym": 2.5, "policy": 1.0,
           "practice": 0.75, "pipeline": 0.75, "w": 0.5}


def features_for(practice_id: str, policy_version: str | None, pipeline_version: str, predicted: list[str],
                 expected: list[str] | None, description: str, card: dict[str, Any]) -> dict[str, Any]:
    exp = expected or []
    fp = sorted(set(predicted) - set(exp)) if expected is not None else []
    fn = sorted(set(exp) - set(predicted))
    def proc(codes: list[str]) -> list[str]:
        return sorted({card["codes"][c]["procedure"] for c in codes if c in card["codes"]})

    words = sorted({w for w in re.findall(r"[a-z][a-z-]{2,}", description.lower()) if w not in STOPWORDS})
    # fpp/fnp = error direction per procedure family ("a crown was wrongly billed"), which generalises
    # across specific codes better than exact code identity.
    return {"practice": practice_id, "policy": policy_version or "none", "pipeline": pipeline_version,
            "fp": fp, "fn": fn, "fpp": proc(fp), "fnp": proc(fn), "proc": proc(fp + fn), "w": words[:40]}


def _tokens(features: dict[str, Any], symptoms: str | None = None) -> list[tuple[str, float]]:
    toks: list[tuple[str, float]] = []
    for key in ("practice", "policy", "pipeline"):
        toks.append((f"{key}:{features[key]}", WEIGHTS[key]))
    for key in ("fp", "fn", "fpp", "fnp", "proc", "w"):
        toks += [(f"{key}:{v}", WEIGHTS[key]) for v in features.get(key, [])]
    if symptoms:
        toks += [(f"sym:{s}", WEIGHTS["sym"]) for s in symptoms.split("|") if s]
    return toks


def embed(features: dict[str, Any], symptoms: str | None = None) -> list[float]:
    vec = [0.0] * DIM
    for tok, w in _tokens(features, symptoms):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % DIM] += w if (h >> 8) & 1 else -w
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 6) for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def metadata_prefilter(f: dict[str, Any], g: dict[str, Any]) -> bool:
    return f.get("pipeline") == g.get("pipeline") or bool(set(f.get("proc", [])) & set(g.get("proc", [])))


def shared_features(f: dict[str, Any], g: dict[str, Any]) -> list[str]:
    out = []
    for key in ("practice", "policy", "pipeline"):
        if f.get(key) == g.get(key):
            out.append(f"{key}={f[key]}")
    for key in ("fp", "fn", "fpp", "fnp"):
        common = sorted(set(f.get(key, [])) & set(g.get(key, [])))
        if common:
            out.append(f"{key}={common}")
    return out


def rank_candidates(target: dict[str, Any], others: list[dict[str, Any]], min_score: float,
                    limit: int) -> list[dict[str, Any]]:
    """`target`/`others`: dicts with id, features, embedding, status, symptom_signature, root_cause_signature."""
    scored = []
    for o in others:
        if o["id"] == target["id"] or not metadata_prefilter(target["features"], o["features"]):
            continue
        score = round(cosine(target["embedding"], o["embedding"]), 3)
        if score < min_score:
            continue
        scored.append({
            "investigation_id": o["id"], "score": score,
            "band": "strong candidate (>=0.8)" if score >= 0.8 else "candidate (0.5-0.8)",
            "shared": shared_features(target["features"], o["features"]),
            "status": o.get("status"), "root_cause_signature": o.get("root_cause_signature"),
            "same_symptom_signature": bool(target.get("symptom_signature")) and
                                      target.get("symptom_signature") == o.get("symptom_signature"),
            "note": "retrieval candidate only; shared root cause requires replay evidence",
        })
    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]
