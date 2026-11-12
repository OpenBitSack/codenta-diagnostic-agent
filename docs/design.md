# Design notes

> Independent portfolio / interview follow-up prototype — not an official Codenta project. Synthetic data only.

## 1. Why the prediction pipeline is not an agent

The production path (extraction → coding → QA) runs on every note, at volume, with a known sequence of
steps. Its value comes from being **predictable, cheap per note, versionable and auditable**. Every
property an agent adds (choosing its own steps, variable tool use, variable cost, open-ended stopping)
is a liability on that path: latency and cost become unbounded, behaviour is harder to regression-test,
and an auditor can't say which steps produced a billing code. A fixed workflow with LLM calls *inside*
well-defined stages is the right shape. This prototype keeps it that way and never lets the diagnostic
system modify it.

## 2. Why a diagnostic agent is justified

Diagnosing a wrong prediction is a different kind of task:

* **The next step depends on the last result.** If correcting the material alone doesn't fix the
  codes, the next useful experiment is the status, then a combination, then a counterfactual policy.
  A fixed script either runs every experiment (wasteful) or misses multi-cause interactions.
* **Multiple simultaneous causes are normal.** The interview case has two extraction defects, a schema
  limitation and a QA gap, and one of the defects is latent under the current policy.
* **It is low volume and high value.** It runs per *incident*, not per note, and dedup reduces the volume
  further. Spending tens of reasoning steps per incident is affordable; per note it would not be.
* **It is sandboxed.** All experiments run against copies of saved traces with versioned config
  overrides, so an agent choosing its own experiments can't harm production.

Even so, the agent is **bounded**. The controller is deterministic code that owns the budget, executes
tools, verifies the reasoner's claims and persists state. The reasoner, whether the deterministic demo
policy or Claude, only proposes the next tool call and the final verdicts.

## 3. Stopping conditions

An investigation stops at the first of:

| Condition | Outcome |
|---|---|
| All testable hypotheses have a verdict backed by experiments/observations, and a fix bundle passes the quality gate | `fix_validated` → human review |
| Identical symptom signature to a parent whose validated fix makes this ticket correct in the sandbox | `linked_known_issue` (0 reasoning steps) |
| Expected codes unknown, or evidence-supported corrections can't reach the label while the label-implied facts aren't documented in the note | `escalated` (`label_documentation_conflict` / `expected_codes_unknown`): the agent does not guess |
| Budget exhausted: reasoning steps (40), experiments (24), regression runs (10), tokens (120k), cost ($1.00), deadline (600 s), all from `config/agent.yaml` | `escalated` with everything already tried listed as "do not repeat" |
| No candidate remedy for a confirmed cause, or every catalog bundle fails the gate | `escalated` (`no_candidate_remedy` / `no_safe_alternative`) |
| No hypothesis supported | `escalated` (`no_confirmed_cause`) |

**Completion criteria** are explicit. Each testable hypothesis is supported, refuted, or inconclusive
with cited evidence. Verdicts that cite non-existent experiments, or claim support without evidence, are
downgraded by the controller. The final fix must improve the target **and** not regress the suite.

## 4. Similarity vs confirmed identity

The system uses three mechanisms and keeps them separate:

1. **Exact fingerprint** (practice, run, note, predicted codes, expected codes, pipeline and policy
   versions) with a DB `UNIQUE` constraint. These are true duplicates: one investigation, N reports.
   Concurrency-safe: 16 simultaneous submissions produce one investigation (tested on SQLite and
   PostgreSQL).
2. **Similarity retrieval** (metadata prefilter + cosine over a hashed feature embedding of error
   direction per procedure family, codes, practice/policy/pipeline and description words). The output is
   a ranked list of **candidates** labelled "candidate (0.5–0.8)" or "strong candidate (≥0.8)". It
   **never links root causes**. Measured in a fresh Demo A + D run, the stage-only crown ticket (D-002)
   scores 0.987 against the interview case at intake and 0.931 after both are investigated, yet its
   confirmed root cause differs (stage only vs material + stage). That is why similarity cannot be
   identity.
3. **Confirmed root-cause grouping.** Known issues are keyed by a confirmed cause id
   (`extraction.crown.status.value_incorrect`, …) and linked only by evidence:
   * `confirmed_by_investigation`: a full investigation's experiments supported the cause.
   * `confirmed_by_counterfactual_replay`: identical *deterministic symptom signature* (the set of
     observed defect kinds, e.g. `extraction_value_contradicts_note:crown.material`) **and** the parent's
     validated fix bundle makes this ticket exactly correct in the sandbox.
   * `pending_human_judgement`: same escalation reason and symptoms as an open escalation, grouped so a
     human answers once. This is explicitly *not* a root-cause claim.

Similarity is used to *rank* fast-path peers. It never substitutes for the replay check.

## 5. Scaling bottlenecks (and what would change)

| Bottleneck | Prototype | Production direction |
|---|---|---|
| Reasoning cost per incident | dedup + known-issue replay avoid most full investigations (load test: 1,000 tickets → 6–7 full investigations) | same, plus a cheaper model for routine steps and caching of tool results across investigations |
| LLM rate limits | shared token bucket (`llm_calls_per_minute`), per-call token caps, budget per investigation | provider-side quotas per tenant; queue priority by severity × affected tickets |
| Regression runs (most expensive tool) | cached by config fingerprint + dataset version; each bundle evaluated once across all investigations | parallel evaluation workers; incremental eval on impacted cases first |
| Similarity search | Python cosine over all investigations (O(n) per intake) | pgvector `vector(256)` + HNSW index; metadata filters in SQL |
| Queue | DB-backed with atomic claims and leases (SKIP LOCKED-equivalent) | SQS + DLQ, or keep Postgres-backed queue up to moderate volume |
| SQLite write concurrency | WAL + busy timeout; fine for local and tests | PostgreSQL (suite and demo verified against PostgreSQL 16 via embedded `pgserver`) |
| Intake | per-principal rate limit, soft/hard queue limits with 429 + Retry-After; duplicates always accepted because they add no work; critical severity bypasses the soft limit | API gateway throttling, WAF |

Concurrency effect observed in the load test: when several identical-symptom tickets start at the
same time, the fast path can't use a parent that hasn't finished yet, so a few redundant full
investigations run (5–6 validated fix proposals for 1 distinct change set across runs). A per-signature advisory
lock or a short "await in-flight parent" delay would remove most of these.

## 6. Measuring SLAs (without claiming one)

The system records the timestamps an SLA needs: `created_at` (first report), `started_at`,
`diagnosed_at`, `fix_validated_at`, `completed_at`, plus human `reviewed_at` and `review_seconds`.
`/v1/metrics` exposes p50/p95 time-to-diagnosis, time-to-validated-fix and investigation runtime,
queue depth, dead-letter count and escalation rate. To set an SLA you would:

1. Pick the user-facing promise, e.g. "a diagnosis or escalation for a high-severity incident within
   N hours of first report". Measure from the *first* report, because duplicates must not reset the
   clock.
2. Track it per severity and per resolution path. Fast-path and full investigations have very
   different distributions.
3. Track the human part separately (time in review queue, review seconds). Automated diagnosis
   latency and human approval latency have different owners.
4. Alert on queue depth / oldest-job age, not only on percentiles.

The load-test numbers in the README are measurements of one laptop run, not an SLA.

## 7. Optimizing human review

Expert time is the scarcest resource, so the review flow is designed to minimize decisions per root
cause and the time per decision:

* **One decision per change set.** Pending fixes with an identical change set fold into one queue
  item. In the full demo, 5 independently validated investigations and the fast-path child of one
  of them produce a single fix decision. Approving it records the decision for all of them.
* **One answer per escalation question.** Escalations with the same reason + symptom signature fold
  together.
* **Ranked by severity × affected tickets**, including folded duplicates and children.
* **Evidence on one screen:** highlighted note spans (`<mark>`), each hypothesis with its verdict, role
  and cited experiments, every experiment's codes, the rejected bundles and why, before/after metrics
  per category, and the do-not-repeat list for escalations.
* **Specific questions** in escalation reports (e.g. "the label implies `pfm`, which the note does not
  document; is the label correct?") instead of "please investigate".
* **Measured:** review seconds per decision are recorded from the dashboard and averaged in metrics.

## 8. Extensibility

Everything specific to the domain lives in versioned YAML under `config/` and is fingerprinted into
cache keys:

* **New CDT codes / procedures:** add to a rule card (`config/rule_cards/`). Anything not from the
  authoritative card must be listed under `synthetic_assumptions`.
* **New practice policies or versions:** `config/policies/practices.yaml`.
* **New extraction fields:** a new schema version (`config/extraction_schemas/`); the observation layer
  generalises over `group.field` paths.
* **New QA rules:** `config/qa_rules/` plus a rule function.
* **New candidate fixes:** `config/candidate_changes/` (`overrides`, `addresses` cause ids,
  `blast_radius`). The fix generator picks them up automatically, narrowest first.
* **New pipeline versions:** `config/pipelines/pipelines.yaml`.
* **New reasoner:** implement the two-method `Reasoner` protocol (`next_actions`, `record_results`).
  The controller, tools, budgets and verification are unchanged.

## Synthetic demo assumptions (rules not on the interview card)

Listed in `config/rule_cards/interview-v1.yaml`:

1. **additional-pa-images:** the card defines D0220 for one PA image. Additional images are flagged
   as unsupported rather than coded.
2. **ambiguous-material:** if the selected crown material is not documented, no crown code is produced
   and the case is held for review.
3. **posterior-teeth:** posterior = premolars and molars (1–5, 12–21, 28–32 in universal numbering).

The legacy schema value `completed` is treated by coding as "seated" (`event_statuses.seat`). This is a
modelling choice for the simulated pipeline that reproduces the interview trace.
