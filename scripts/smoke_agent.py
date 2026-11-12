"""In-memory smoke run of the agent on the interview case (no DB, no API). Usage:
    python scripts/smoke_agent.py [seat|prep]
"""
from __future__ import annotations

import sys

from codenta_diag.agent import AgentBudget, AgentController, DeterministicReasoner, ToolContext
from codenta_diag.agent.report import render_markdown
from codenta_diag.config_registry import ConfigRegistry
from codenta_diag.domain import SourceNote
from codenta_diag.evaluation import load_dataset
from codenta_diag.evaluation.runner import RegressionRunner
from codenta_diag.pipeline import production_config, run_pipeline
from codenta_diag.sandbox import SandboxReplayEngine

NOTE = ("#14 cracked, pt c/o pain on biting. 1 PA taken. Prepped #14 for crown, temp placed, impression to lab. "
        "Discussed gold and all-ceramic; pt chose all-ceramic. Also #19 MO composite filling done today. "
        "Seat #14 in 2 wks.")


def main(mode: str = "seat") -> None:
    reg = ConfigRegistry()
    practice, policy, expected = (("P-SEAT-01", "P-SEAT-01@v3", ["D0220", "D2392"]) if mode == "seat" else
                                  ("P-PREP-02", "P-PREP-02@v1", ["D0220", "D2392", "D2740"]))
    note = SourceNote(note_id="N-1001", practice_id=practice, text=NOTE)
    cfg = production_config(reg)
    run = run_pipeline(note, policy, cfg, reg, run_id="RUN-1001")
    ctx = ToolContext(investigation_id="INV-smoke", practice_id=practice, note=note, run=run, expected=expected,
                      run_policy=reg.policy_from_ref(policy), registry_policy=reg.policy(practice), base_config=cfg,
                      registry=reg, engine=SandboxReplayEngine(reg),
                      regression=RegressionRunner(reg, load_dataset()),
                      gate_cfg=reg.agent_config()["quality_gate"])
    ctl = AgentController(ctx, DeterministicReasoner(), AgentBudget.from_config(reg.agent_config()))
    state = ctl.run()
    inv = {"id": "INV-smoke", "status": state.phase, "practice_id": practice, "pipeline_run_id": run.run_id,
           "pipeline_version": run.pipeline_version, "predicted_codes": run.predicted_codes,
           "expected_codes": expected, "reasoner": state.reasoner, "report_count": 1, "resolution": "smoke",
           "state": state.model_dump(mode="json")}
    print(render_markdown(inv))
    print("budget:", state.budget.model_dump(exclude={"started_at"}))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "seat")
