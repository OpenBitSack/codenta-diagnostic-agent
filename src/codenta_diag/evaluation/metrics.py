"""Code-level evaluation of one pipeline config over a labeled dataset."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel

from ..config_registry import ConfigRegistry
from ..domain import PipelineConfig, SourceNote, compare_codes
from ..pipeline import run_pipeline
from .dataset import EvalCase, EvalDataset


class CaseResult(BaseModel):
    case_id: str
    predicted: list[str]
    expected: list[str]
    false_positives: list[str]
    false_negatives: list[str]
    exact: bool
    qa_passed: bool
    qa_failed_rules: list[str]
    escaped_error: bool      # wrong AND passed QA -> would have been delivered to the practice
    false_hold: bool         # correct BUT QA failed -> unnecessary human review
    policy_violation: bool   # crown billed at a visit the practice policy does not bill
    categories: list[str]
    critical: bool


class EvalSummary(BaseModel):
    config_label: str
    components: dict[str, str]
    dataset: str
    n_cases: int
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    exact_match_rate: float
    escaped_errors: int
    false_holds: int
    policy_violations: int
    by_category: dict[str, dict[str, Any]]
    cases: list[CaseResult]

    def case(self, case_id: str) -> CaseResult:
        return next(c for c in self.cases if c.case_id == case_id)


def _policy_violation(case: EvalCase, predicted: list[str], registry: ConfigRegistry) -> bool:
    if not case.gold or not case.gold.get("crown_status"):
        return False
    card = registry.rule_card()
    crown_codes = [c for c in predicted if card["codes"].get(c, {}).get("procedure") == "crown"]
    if not crown_codes:
        return False
    policy = registry.policy_from_ref(case.true_policy_ref)
    allowed = card["procedures"]["crown"]["billing"]["event_statuses"][policy.crown_billing_event]
    return case.gold["crown_status"] not in allowed


def evaluate_case(case: EvalCase, config: PipelineConfig, registry: ConfigRegistry) -> CaseResult:
    note = SourceNote(note_id=f"eval:{case.id}", practice_id=case.practice_id, text=case.note)
    run = run_pipeline(note, case.run_policy_ref, config, registry, run_id=f"eval:{case.id}")
    cmp = compare_codes(run.predicted_codes, case.expected_codes)
    qa = run.trace("qa").output
    return CaseResult(
        case_id=case.id,
        predicted=cmp.predicted,
        expected=cmp.expected,
        false_positives=cmp.false_positives,
        false_negatives=cmp.false_negatives,
        exact=cmp.exact_match,
        qa_passed=run.qa_passed,
        qa_failed_rules=[c["rule_id"] for c in qa["checks"] if not c["passed"]],
        escaped_error=(not cmp.exact_match) and run.qa_passed,
        false_hold=cmp.exact_match and not run.qa_passed,
        policy_violation=_policy_violation(case, run.predicted_codes, registry),
        categories=case.categories,
        critical=case.critical,
    )


def evaluate(config: PipelineConfig, dataset: EvalDataset, registry: ConfigRegistry) -> EvalSummary:
    results = [evaluate_case(c, config, registry) for c in dataset.cases]
    tp = fp = fn = 0
    for r in results:
        p, e = Counter(r.predicted), Counter(r.expected)
        tp += sum((p & e).values())
        fp += sum((p - e).values())
        fn += sum((e - p).values())
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    cats: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "exact": 0})
    for r in results:
        for cat in r.categories:
            cats[cat]["n"] += 1
            cats[cat]["exact"] += int(r.exact)
    for v in cats.values():
        v["exact_rate"] = round(v["exact"] / v["n"], 3)
    return EvalSummary(
        config_label=config.version,
        components=config.components(),
        dataset=dataset.ref,
        n_cases=len(results),
        tp=tp, fp=fp, fn=fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        exact_match_rate=round(sum(r.exact for r in results) / len(results), 4),
        escaped_errors=sum(r.escaped_error for r in results),
        false_holds=sum(r.false_hold for r in results),
        policy_violations=sum(r.policy_violation for r in results),
        by_category=dict(sorted(cats.items())),
        cases=results,
    )
