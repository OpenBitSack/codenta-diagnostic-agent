"""End-to-end demonstration scenarios through the HTTP API (deterministic-demo reasoner, fresh DB per test)."""
from __future__ import annotations

import re

import pytest

from .conftest import ADMIN, REPORTER, REPORTER_PREP, REVIEWER, get_inv, hyp, process, submit

pytestmark = pytest.mark.integration

MATERIAL = "extraction.crown.material.value_incorrect"
STATUS = "extraction.crown.status.value_incorrect"
SCHEMA = "extraction.schema.crown.status.unrepresentable_value"
CODING = "coding.policy.not_applied"
QA = "qa.missed_error"
FULL_BUNDLE = ["extraction-prompt-v2-selected-material", "extraction-schema-v2-crown-stage",
               "pipeline-policy-registry-resolution", "qa-v2-independent-policy-and-evidence-checks"]


def run_ticket(client, container, ticket):
    r = submit(client, container, ticket)
    process(client)
    return get_inv(client, r["investigation_id"])


def test_demo_a_interview_case_multi_cause(client, container, demo_tickets):
    inv = run_ticket(client, container, demo_tickets["demo_a"][0])
    assert inv["status"] == "fix_validated" and inv["reasoner"] == "deterministic-demo"
    assert inv["llm_calls"] == 0 and inv["tokens_used"] == 0
    assert hyp(inv, MATERIAL)["status"] == "supported" and "latent" in hyp(inv, MATERIAL)["roles"]
    assert hyp(inv, STATUS)["status"] == "supported" and "sufficient" in hyp(inv, STATUS)["roles"]
    assert hyp(inv, CODING)["status"] == "refuted"
    assert hyp(inv, QA)["status"] == "supported"
    assert hyp(inv, "model.capability")["status"] == "untestable"
    # every supported verdict cites executed experiments or deterministic observations
    exp_ids = {e["experiment_id"] for e in inv["state"]["experiments"]}
    for h in inv["state"]["hypotheses"]:
        assert set(h["supporting_experiments"]) | set(h["refuting_experiments"]) <= exp_ids
        if h["status"] == "supported":
            assert h["supporting_experiments"] or h.get("supporting_observations")
        if h["status"] == "refuted":
            assert h["refuting_experiments"]
    attempts = inv["state"]["fix_attempts"]
    assert not attempts[0]["gate_passed"] and attempts[-1]["gate_passed"]
    assert attempts[-1]["change_ids"] == FULL_BUNDLE
    assert attempts[-1]["target"]["exact_match"] and attempts[-1]["regressions"] == []
    assert inv["candidate_fixes"][-1]["approval_status"] == "pending_review"
    assert {l["link_type"] for l in inv["known_issue_links"]} == {"confirmed_by_investigation"}
    md = client.get(f"/v1/investigations/{inv['id']}/report.md", headers=REVIEWER).text
    assert "deterministic" in md.lower() and "D2790" in md


def test_demo_b_prep_policy_requires_both_corrections(client, container, demo_tickets):
    inv = run_ticket(client, container, demo_tickets["demo_b"][0])
    exps = {e["label"]: e for e in inv["state"]["experiments"]}
    assert exps["combined:all-fields"]["predicted_codes"] == ["D0220", "D2392", "D2740"]
    assert "D2740" not in exps["single:crown.material"]["predicted_codes"]
    assert "D2740" not in exps["single:crown.status"]["predicted_codes"]
    assert "necessary" in hyp(inv, MATERIAL)["roles"] and "necessary" in hyp(inv, STATUS)["roles"]
    assert inv["status"] == "fix_validated"


def test_demo_d_similar_but_different_issues_are_not_merged(client, container, demo_tickets):
    ids = [submit(client, container, t)["investigation_id"] for t in demo_tickets["demo_d"]]
    assert len(set(ids)) == 4
    process(client)
    d1, d2, d3, d4 = (get_inv(client, i) for i in ids)
    sig = lambda inv: set((inv["root_cause_signature"] or "").split("|"))  # noqa: E731
    assert MATERIAL in sig(d1) and STATUS not in sig(d1)
    assert STATUS in sig(d2) and MATERIAL not in sig(d2)
    assert "input.policy.missing_at_runtime" in sig(d3) and "coding.policy.silent_default_when_missing" in sig(d3)
    assert MATERIAL not in sig(d3) and STATUS not in sig(d3)
    assert {MATERIAL, STATUS} <= sig(d4)  # overlapping errors, both confirmed
    assert len({frozenset(sig(x)) for x in (d1, d2, d3, d4)}) == 4
    for inv in (d1, d2, d3, d4):
        assert inv["resolution"] == "full_investigation"  # no similarity-based shortcut
        assert all(l["link_type"] == "confirmed_by_investigation" for l in inv["known_issue_links"])
    sim = client.get(f"/v1/investigations/{ids[3]}/similar", headers=REVIEWER).json()
    assert sim["candidates"] and all("candidate" in c["note"] for c in sim["candidates"])


def test_fast_path_links_identical_symptoms_after_replay(client, container, demo_tickets):
    a = run_ticket(client, container, demo_tickets["demo_a"][0])
    d4 = run_ticket(client, container, demo_tickets["demo_d"][3])
    assert d4["status"] == "linked_known_issue" and d4["resolution"] == "known_issue_replay"
    assert d4["reasoning_steps"] == 0 and d4["experiments_run"] == 1 and d4["reasoner"] == "none"
    assert d4["symptom_signature"] == a["symptom_signature"]
    assert d4["state"]["fast_path"]["parent"] == a["id"]
    links = {l["link_type"] for l in d4["known_issue_links"]}
    assert links == {"confirmed_by_counterfactual_replay"}
    replay = d4["state"]["experiments"][0]
    assert replay["comparison"]["exact_match"] and replay["request"]["candidate_change_ids"] == FULL_BUNDLE
    m = client.get("/v1/metrics", headers=REVIEWER).json()
    assert m["volume"]["resolved_by_known_issue_replay"] == 1
    assert m["cost"]["reasoning_steps_avoided_known_issue_replay"] == a["reasoning_steps"]


def test_fast_path_does_not_link_different_symptoms(client, container, demo_tickets):
    run_ticket(client, container, demo_tickets["demo_a"][0])
    d2 = run_ticket(client, container, demo_tickets["demo_d"][1])  # high similarity, different symptoms
    assert d2["resolution"] == "full_investigation" and d2["reasoning_steps"] > 0


def test_fast_path_disabled_runs_full_investigation(settings, demo_tickets):
    from fastapi.testclient import TestClient

    from codenta_diag.api import create_app
    from codenta_diag.api.app import build_container
    from codenta_diag.db import Database
    container = build_container(settings, Database(settings.database_url), fast_path=False)
    with TestClient(create_app(settings, container)) as client:
        run_ticket(client, container, demo_tickets["demo_a"][0])
        d4 = run_ticket(client, container, demo_tickets["demo_d"][3])
        assert d4["resolution"] == "full_investigation"


def test_demo_e_escalation_and_grouping(client, container, demo_tickets):
    e1 = run_ticket(client, container, demo_tickets["escalation"][0])
    assert e1["status"] == "escalated" and e1["reasoner"] == "deterministic-demo"
    esc = e1["state"]["escalation"]
    assert esc["reason"].startswith("label_documentation_conflict")
    assert esc["questions_for_human"] and esc["already_done_do_not_repeat"]
    assert esc["evidence"]
    e2 = run_ticket(client, container, demo_tickets["escalation"][1])
    assert e2["status"] == "escalated" and e2["resolution"] == "grouped_escalation"
    assert e2["state"]["escalation"]["grouped_with"] == e1["id"] and e2["reasoning_steps"] == 0
    assert {l["link_type"] for l in e2["known_issue_links"]} == {"pending_human_judgement"}
    queue = client.get("/v1/review-queue", headers=REVIEWER).json()
    escalations = [q for q in queue if q["kind"] == "escalation"]
    assert len(escalations) == 1 and escalations[0]["affected_tickets"] == 2


def test_demo_e_gate_rejection_then_revision_recorded(client, container, demo_tickets):
    inv = run_ticket(client, container, demo_tickets["demo_a"][0])
    rejected = [a for a in inv["state"]["fix_attempts"] if not a["gate_passed"]]
    assert rejected and all(a["failed_checks"] and a["revision_rationale"] for a in rejected)
    assert any(r["case_id"] == "pfm-chosen-over-ceramic" for r in rejected[0]["regressions"])
    assert len(inv["candidate_fixes"]) == len(inv["state"]["fix_attempts"])
    assert sum(f["approval_status"] == "not_submitted" for f in inv["candidate_fixes"]) == len(rejected)


def test_budget_exhaustion_escalates_with_evidence(settings, demo_tickets):
    from fastapi.testclient import TestClient

    from codenta_diag.api import create_app
    from codenta_diag.api.app import build_container
    from codenta_diag.db import Database
    container = build_container(settings, Database(settings.database_url), budget_overrides={"max_regression_runs": 1})
    with TestClient(create_app(settings, container)) as client:
        inv = run_ticket(client, container, demo_tickets["demo_a"][0])
        assert inv["status"] == "escalated"
        esc = inv["state"]["escalation"]
        assert esc["reason"] == "budget_exhausted:regression_runs"
        assert esc["rejected_fix_bundles"]
        assert hyp(inv, STATUS)["status"] == "supported"  # diagnosis kept even though fix search stopped


def test_human_review_approve_folds_identical_change_sets(client, container, demo_tickets):
    a = run_ticket(client, container, demo_tickets["demo_a"][0])
    d1 = run_ticket(client, container, demo_tickets["demo_d"][0])
    queue = client.get("/v1/review-queue", headers=REVIEWER).json()
    fix_items = [q for q in queue if q["kind"] == "fix_review"]
    assert len(fix_items) == 1 and set(fix_items[0]["investigations"]) == {a["id"], d1["id"]}
    fix_id = fix_items[0]["fix_id"]
    assert client.post(f"/v1/fixes/{fix_id}/review", json={"decision": "approve"}, headers=REPORTER).status_code == 403
    r = client.post(f"/v1/fixes/{fix_id}/review", json={"decision": "approve", "notes": "ok", "review_seconds": 42},
                    headers=REVIEWER)
    assert r.status_code == 200
    body = r.json()
    assert body["approval_status"] == "approved" and "NOT deployed" in body["deployment"]
    assert set(body["investigations_updated"]) == {a["id"], d1["id"]}
    assert get_inv(client, a["id"])["status"] == "approved" and get_inv(client, d1["id"])["status"] == "approved"
    assert client.post(f"/v1/fixes/{fix_id}/review", json={"decision": "approve"}, headers=REVIEWER).status_code == 409
    rejected_fix = next(f["id"] for f in get_inv(client, a["id"])["candidate_fixes"] if not f["gate_passed"])
    assert client.post(f"/v1/fixes/{rejected_fix}/review", json={"decision": "approve"},
                       headers=REVIEWER).status_code == 409
    m = client.get("/v1/metrics", headers=REVIEWER).json()
    assert m["outcomes_distinguished"]["human_approved_fixes"] == 2
    assert m["outcomes_distinguished"]["distinct_validated_change_sets"] == 1
    assert m["human_review"]["review_seconds_per_resolved_avg"] == 42


def test_review_propagates_to_fast_path_children(client, container, demo_tickets):
    a = run_ticket(client, container, demo_tickets["demo_a"][0])
    d4 = run_ticket(client, container, demo_tickets["demo_d"][3])
    fix_id = next(f["id"] for f in a["candidate_fixes"] if f["gate_passed"])
    client.post(f"/v1/fixes/{fix_id}/review", json={"decision": "reject", "notes": "needs PM sign-off"},
                headers=REVIEWER)
    assert get_inv(client, d4["id"])["status"] == "rejected"


def test_metrics_distinguish_outcomes(client, container, demo_tickets):
    run_ticket(client, container, demo_tickets["demo_a"][0])
    run_ticket(client, container, demo_tickets["escalation"][0])
    m = client.get("/v1/metrics", headers=REVIEWER).json()
    od = m["outcomes_distinguished"]
    assert od["diagnosed_incidents"] == 1 and od["validated_fix_proposals"] == 1 and od["human_approved_fixes"] == 0
    assert m["human_review"]["open_escalations"] == 1 and m["human_review"]["escalation_rate"] == 0.5
    assert "no SLA" in m["latency_seconds"]["note"]
    assert m["latency_seconds"]["investigation_runtime_p50"] is not None


def test_manual_sandbox_replay_endpoint(client, container, demo_tickets):
    inv_id = submit(client, container, demo_tickets["demo_b"][0])["investigation_id"]
    body = {"label": "reviewer-check", "from_stage": "coding",
            "field_overrides": {"crown.material": "all_ceramic", "crown.status": "prepared"}}
    r = client.post(f"/v1/investigations/{inv_id}/sandbox-replay", json=body, headers=REVIEWER)
    assert r.status_code == 200 and r.json()["predicted_codes"] == ["D0220", "D2392", "D2740"]
    assert client.post(f"/v1/investigations/{inv_id}/sandbox-replay", json=body, headers=REPORTER_PREP).status_code == 403
    for bad in ({**body, "candidate_change_ids": ["does-not-exist"]},
                {**body, "field_overrides": {"crown.material": "all-ceramic"}},
                {**body, "field_overrides": {"crown.colour": "white"}}):
        r = client.post(f"/v1/investigations/{inv_id}/sandbox-replay", json=bad, headers=REVIEWER)
        assert r.status_code == 422, bad


def test_dashboard_flow(client, container, demo_tickets):
    inv = run_ticket(client, container, demo_tickets["demo_a"][0])
    assert client.get("/dashboard", follow_redirects=False).status_code == 303
    assert client.post("/dashboard/login", data={"api_key": "dev-reporter-seat"}).status_code == 401
    r = client.post("/dashboard/login", data={"api_key": "dev-reviewer"}, follow_redirects=False)
    assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower()
    index = client.get("/dashboard")
    assert index.status_code == 200 and inv["id"] in index.text and "NOT an LLM" in index.text
    detail = client.get(f"/dashboard/investigations/{inv['id']}")
    assert detail.status_code == 200
    page = detail.text
    assert "<mark>" in page
    for needle in ("D2790", "fix_validated", MATERIAL, "pfm-chosen-over-ceramic", "Approve"):
        assert needle in page, needle
    fix_id = next(f["id"] for f in inv["candidate_fixes"] if f["gate_passed"])
    opened = re.search(r'name="opened_at" value="([0-9.]+)"', page)
    r = client.post(f"/dashboard/fixes/{fix_id}/review",
                    data={"decision": "approve", "notes": "LGTM", "opened_at": opened.group(1) if opened else "0"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert get_inv(client, inv["id"])["status"] == "approved"


def test_admin_endpoints_require_admin(client):
    assert client.post("/v1/admin/process", headers=REPORTER).status_code == 403
    assert client.post("/v1/admin/process", headers=ADMIN).status_code == 200
