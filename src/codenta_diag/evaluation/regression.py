"""Baseline-vs-candidate regression comparison and the quality gate."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..domain import CodeComparison
from .metrics import EvalSummary


class CaseDelta(BaseModel):
    case_id: str
    baseline_predicted: list[str]
    candidate_predicted: list[str]
    expected: list[str]
    critical: bool
    categories: list[str]


class RegressionReport(BaseModel):
    baseline: str
    candidate: str
    regressions: list[CaseDelta]       # correct in baseline, wrong in candidate
    improvements: list[CaseDelta]      # wrong in baseline, correct in candidate
    critical_regressions: list[str]
    deltas: dict[str, float]
    baseline_metrics: dict[str, Any]
    candidate_metrics: dict[str, Any]


def _headline(s: EvalSummary) -> dict[str, Any]:
    return {k: getattr(s, k) for k in ("precision", "recall", "f1", "exact_match_rate", "fp", "fn",
                                        "escaped_errors", "false_holds", "policy_violations")}


def compare_summaries(baseline: EvalSummary, candidate: EvalSummary) -> RegressionReport:
    regressions, improvements = [], []
    for b in baseline.cases:
        c = candidate.case(b.case_id)
        delta = CaseDelta(case_id=b.case_id, baseline_predicted=b.predicted, candidate_predicted=c.predicted,
                          expected=b.expected, critical=b.critical, categories=b.categories)
        if b.exact and not c.exact:
            regressions.append(delta)
        elif not b.exact and c.exact:
            improvements.append(delta)
    bm, cm = _headline(baseline), _headline(candidate)
    return RegressionReport(
        baseline=baseline.config_label,
        candidate=candidate.config_label,
        regressions=regressions,
        improvements=improvements,
        critical_regressions=[r.case_id for r in regressions if r.critical],
        deltas={k: round(cm[k] - bm[k], 4) for k in bm},
        baseline_metrics=bm,
        candidate_metrics=cm,
    )


class GateCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class GateDecision(BaseModel):
    passed: bool
    checks: list[GateCheck]

    @property
    def failed(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed]


def apply_quality_gate(report: RegressionReport, target: CodeComparison | None,
                       cfg: dict[str, Any]) -> GateDecision:
    """A fix must not pass simply because it corrects one note."""
    checks = [
        GateCheck(name="no_regressions",
                  passed=len(report.regressions) <= cfg["max_regressions"],
                  detail=f"{len(report.regressions)} regression(s): {[r.case_id for r in report.regressions]}"),
        GateCheck(name="no_critical_regressions", passed=not report.critical_regressions,
                  detail=f"critical: {report.critical_regressions}"),
        GateCheck(name="f1_not_worse", passed=report.deltas["f1"] >= cfg["min_f1_delta"],
                  detail=f"F1 delta {report.deltas['f1']:+.4f}"),
        GateCheck(name="policy_violations_not_increased",
                  passed=report.deltas["policy_violations"] <= cfg["max_policy_violation_delta"],
                  detail=f"policy violation delta {report.deltas['policy_violations']:+.0f}"),
        GateCheck(name="false_holds_bounded",
                  passed=report.deltas["false_holds"] <= cfg["max_false_hold_delta"],
                  detail=f"false-hold delta {report.deltas['false_holds']:+.0f}"),
    ]
    if cfg.get("require_target_fixed") and target is not None:
        checks.append(GateCheck(name="target_ticket_fixed", passed=target.exact_match,
                                detail=f"predicted {target.predicted} vs expected {target.expected}"))
    return GateDecision(passed=all(c.passed for c in checks), checks=checks)
