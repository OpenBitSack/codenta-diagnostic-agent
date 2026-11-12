# Security and data handling

> Synthetic data only. No real patient data, no access to Codenta systems.

| Requirement | Implementation | Verified by |
|---|---|---|
| Input validation | Pydantic models with `extra="forbid"`, regex-validated IDs and CDT codes, enum severities, length limits, `reported_at` not in the future; reports must match the saved run (predicted codes, practice, versions) or are rejected with 422; manual sandbox overrides validated against known extraction schemas | `tests/test_intake_dedup.py::test_intake_validation`, `tests/test_demos.py::test_manual_sandbox_replay_endpoint` |
| Authentication (prototype) | API keys mapped to principal, role (`reporter`, `reviewer`, `admin`) and practice scope; dashboard uses an HttpOnly, SameSite=strict cookie; dev keys refused unless `CODENTA_ENV=dev` | `test_auth_and_practice_isolation`, `test_dev_keys_refused_outside_dev`, `test_dashboard_flow` |
| Authentication (production design) | OIDC (e.g. Cognito / corporate IdP) with short-lived JWTs; practice scope and role as claims; service-to-service via IAM roles; keys never stored in config files | design only |
| Practice-level isolation | Every read goes through `ReadOnlyTraceStore(practice_scope)`; `ToolContext` refuses note/run from another practice; API returns 404 (not 403) for out-of-scope resources to avoid existence leaks; review queue, list endpoints and dashboard are filtered by scope; fixes can only be reviewed within scope | `tests/test_security.py` (scope, cross-practice context, review-queue scoping) |
| Minimal sensitive information in logs | Logs and audit records contain IDs, actions and tool names — never note text. Note text lives only in the trace store and investigation state | `test_audit_log_records_actions_without_note_text`, `test_logs_do_not_contain_note_text`; live server log grep |
| Audit records | Every intake, tool call, experiment, escalation, outcome, review decision and queue event is written to `audit_log` with actor, action, investigation id, timestamp | `test_audit_log_records_actions_without_note_text` |
| Read-only production traces | `ReadOnlyTraceStore` exposes only `get_note` / `get_run`; production runs are byte-identical before/after an investigation | `test_trace_store_has_no_write_methods`, `test_investigation_never_mutates_production_runs_or_config` |
| Sandbox-only experimental changes | Candidate changes produce a new `PipelineConfig` with `sandbox=True` and a distinct version label; the production config and the config registry fingerprint are unchanged after investigations | `test_sandbox_configs_are_flagged`, `test_investigation_never_mutates_production_runs_or_config` |
| No automatic production changes | There is no code path that writes prompts, schemas, policies or rule cards. "Approve" records a human decision and returns "NOT deployed" | `test_human_review_approve_folds_identical_change_sets` |
| Human approval mandatory | Only gate-passing fixes can be approved; only reviewers/admins can approve; rejected bundles cannot be approved | same + `test_review_queue_scoped_for_practice_reviewers` |

## LLM-specific considerations (Claude mode)

* Only the minimum context is sent: the note under investigation, its traces, observations and tool
  results. With real PHI this would require a BAA with the model provider and a data-processing review.
* The model cannot call anything outside the typed tool registry; arguments are validated by Pydantic
  before execution, and practice scope is enforced by the tool context, not by the prompt.
* Model verdicts are verified against executed experiments; a model cannot "declare" a root cause.
* Token and cost budgets per investigation; a shared rate limiter across workers.
