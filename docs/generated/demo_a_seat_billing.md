# Investigation INV-d58d4cd5dd — fix_validated

- Practice: `P-SEAT-01`  Run: `RUN-1001`  Pipeline: `v1.3`
- Reasoner: `deterministic-demo` (deterministic-demo = rule-based policy, NOT an LLM)
- Predicted: `['D0220', 'D2392', 'D2790']`  Expected: `['D0220', 'D2392']`
- Linked reports: 1  Resolution path: `full_investigation`
- Earliest observable incorrect stage: **extraction**

## Observations (facts from deterministic checks)

- `OBS-01` [output] D2790 (Full gold crown) predicted but not expected
- `OBS-02` [qa] all automated QA checks passed although predicted codes are wrong
- `OBS-03` [extraction] extracted crown.material='gold' but note indicates 'all_ceramic' (selection_cue) — note: "pt chose"; "all-ceramic"
- `OBS-04` [extraction] extracted crown.status='completed' but note evidence indicates 'prepared' — note: "Prepped"; "temp placed"; "impression to lab"; "Seat #14 in 2 wks"
- `OBS-05` [extraction] schema v1 crown.status enum ['planned', 'completed'] has no value for 'prepared'
- `OBS-06` [coding] re-running coding on its recorded inputs reproduces the recorded codes (coding is consistent with the facts it was given)

## Hypotheses and verdicts

| ID | Cause | Status | Roles | Evidence | Rationale |
|---|---|---|---|---|---|
| H-A | `extraction.crown.material.value_incorrect` | **supported** | latent | EXP-566652169e, EXP-c58e0807bf | correcting only crown.material -> ['D0220', 'D2392', 'D2740'] (error remains); crown.material does not change today's codes but is necessary under the counterfactual policy (['D0220', 'D2392', 'D2790'] vs derived ['D0220', 'D2392', 'D2740']) -> latent defect. |
| H-B | `extraction.crown.status.value_incorrect` | **supported** | sufficient, necessary | EXP-ca4c34ed19, EXP-566652169e | correcting only crown.status -> ['D0220', 'D2392'] (matches label); crown.status is necessary under the actual policy. |
| H-C | `coding.policy.not_applied` | **refuted** |  | EXP-8236cdb283 | with corrected facts and the correct policy, the production coding stage produces the expected codes ['D0220', 'D2392'] — it applies the policy correctly when given correct facts; it propagated bad inputs. |
| H-D | `qa.missed_error` | **supported** | contributing | EXP-f97ca051fe, OBS-02 | production QA passed the wrong prediction; candidate QA flags it via ['crown_billing_event_note_evidence', 'crown_material_note_evidence'] |
| H-E | `extraction.schema.crown.status.unrepresentable_value` | **supported** | structural | EXP-3c4df85846, OBS-05 | production schema has no enum value for the note's crown.status; only a schema change makes the stage emit the correct value (prompt-only changes cannot). |
| H-F | `model.capability` | **untestable** |  |  | requires replaying the real extraction model or an alternative model; not possible with the deterministic simulated stage in this prototype. |

## Sandbox experiments

| Experiment | Label | Predicted | Matches | Expected source |
|---|---|---|---|---|
| `EXP-8e479c364b` | baseline:recorded-extraction | `['D0220', 'D2392', 'D2790']` | False | ticket_label |
| `EXP-771beae8b1` | baseline:rerun-extraction | `['D0220', 'D2392', 'D2790']` | False | ticket_label |
| `EXP-566652169e` | single:crown.material | `['D0220', 'D2392', 'D2740']` | False | ticket_label |
| `EXP-ca4c34ed19` | single:crown.status | `['D0220', 'D2392']` | True | ticket_label |
| `EXP-8236cdb283` | combined:all-fields | `['D0220', 'D2392']` | True | ticket_label |
| `EXP-d73744c8d2` | counterfactual:prep:all-fields | `['D0220', 'D2392', 'D2740']` | True | derived_counterfactual |
| `EXP-c58e0807bf` | counterfactual:prep:without:crown.material | `['D0220', 'D2392', 'D2790']` | False | derived_counterfactual |
| `EXP-86aa922851` | counterfactual:prep:without:crown.status | `['D0220', 'D2392']` | False | derived_counterfactual |
| `EXP-f97ca051fe` | qa-candidate:qa-v2-independent-policy-and-evidence-checks | `['D0220', 'D2392', 'D2790']` | False | ticket_label |
| `EXP-0ac02e0f72` | component:extraction-prompt-last-mentioned-material | `['D0220', 'D2392', 'D2740']` | False | ticket_label |
| `EXP-f255842964` | component:extraction-prompt-v2-selected-material | `['D0220', 'D2392', 'D2740']` | False | ticket_label |
| `EXP-3c4df85846` | component:extraction-schema-v2-crown-stage | `['D0220', 'D2392']` | True | ticket_label |

## Candidate fix bundles (regression suite + quality gate)

- Attempt 1 `['extraction-prompt-last-mentioned-material', 'extraction-schema-v2-crown-stage', 'qa-v2-independent-policy-and-evidence-checks']` — **REJECTED**; F1 0.8214 → 0.918, escaped errors 9 → 0, improvements 8, regressions ['pfm-chosen-over-ceramic', 'undecided-material-prepped-prep-billing', 'missing-policy-seat-practice-prepped']
  - Revision: pfm-chosen-over-ceramic: 'extraction-prompt-last-mentioned-material' still leaves extraction.crown.material.value_incorrect -> try 'extraction-prompt-v2-selected-material'; missing-policy-seat-practice-prepped: bundle exposes input.policy.missing_at_runtime -> add companion 'coding-missing-policy-default-seat'; missing-policy-seat-practice-prepped: bundle exposes coding.policy.silent_default_when_missing -> add companion 'coding-missing-policy-default-seat'
- Attempt 2 `['coding-missing-policy-default-seat', 'extraction-prompt-v2-selected-material', 'extraction-schema-v2-crown-stage', 'qa-v2-independent-policy-and-evidence-checks']` — **REJECTED**; F1 0.8214 → 0.9655, escaped errors 9 → 0, improvements 8, regressions ['missing-policy-prep-practice-seated']
  - Revision: missing-policy-prep-practice-seated: 'coding-missing-policy-default-seat' still leaves input.policy.missing_at_runtime -> try 'pipeline-policy-registry-resolution'
- Attempt 3 `['extraction-prompt-v2-selected-material', 'extraction-schema-v2-crown-stage', 'pipeline-policy-registry-resolution', 'qa-v2-independent-policy-and-evidence-checks']` — **PASSED**; F1 0.8214 → 1.0, escaped errors 9 → 0, improvements 9, regressions []

## Decision log

- step 0 [observe] **observe** — 6 deterministic observations; earliest observable incorrect stage: extraction. No reasoning step spent.
- step 1 [hypothesize] **record_hypotheses** — Generated 6 hypotheses from deterministic observations (observations are facts; hypotheses are untested).
- step 2 [test] **experiment:baseline:recorded-extraction** — Reproduce the failure from saved stage outputs before testing anything (cheap; confirms the trace is self-consistent).
- step 3 [test] **experiment:baseline:rerun-extraction** — Re-run the extraction stage with production versions to check the failure is reproducible end-to-end.
- step 4 [test] **experiment:single:crown.material** — Cheapest test of hypothesis A: inject the note-supported value of crown.material only.
- step 5 [test] **experiment:single:crown.status** — Previous correction left the error in place (predicted ['D0220', 'D2392', 'D2740']); continue with hypothesis B: correct only crown.status.
- step 6 [test] **experiment:combined:all-fields** — Correct all evidence-contradicted fields together to test whether extraction errors fully explain the failure.
- step 7 [test] **experiment:counterfactual:prep:all-fields** — Coding is verified correct on corrected facts; ask whether the conclusions depend on the policy by replaying under a PREP-billing counterfactual.
- step 8 [test] **experiment:counterfactual:prep:without:crown.material** — Under PREP billing, is crown.material necessary? (separates latent from necessary causes)
- step 9 [test] **experiment:counterfactual:prep:without:crown.status** — Under PREP billing, is crown.status necessary? (separates latent from necessary causes)
- step 10 [test] **experiment:qa-candidate:qa-v2-independent-policy-and-evidence-checks** — Run the candidate QA rules on the ORIGINAL failing output: would an independent check have caught it?
- step 11 [test] **experiment:component:extraction-prompt-last-mentioned-material** — Re-run the extraction stage with candidate 'extraction-prompt-last-mentioned-material' alone to see which fields a {'extraction_prompt': 'extract-last-mentioned'} change can fix (prompt vs schema).
- step 12 [test] **experiment:component:extraction-prompt-v2-selected-material** — Re-run the extraction stage with candidate 'extraction-prompt-v2-selected-material' alone to see which fields a {'extraction_prompt': 'extract-v2-selected'} change can fix (prompt vs schema).
- step 13 [test] **experiment:component:extraction-schema-v2-crown-stage** — Re-run the extraction stage with candidate 'extraction-schema-v2-crown-stage' alone to see which fields a {'extraction_schema': 'v2'} change can fix (prompt vs schema).
- step 14 [conclude] **submit_diagnosis** — All testable hypotheses have been tested; evidence is sufficient to conclude.
- step 14 [fix] **evaluate_candidate_bundle** — ['extraction-prompt-last-mentioned-material', 'extraction-schema-v2-crown-stage', 'qa-v2-independent-policy-and-evidence-checks']: Narrowest catalog change for each confirmed cause.
- step 14 [fix] **candidate_rejected** — Gate failed ['no_regressions', 'no_critical_regressions', 'false_holds_bounded'] with regressions ['pfm-chosen-over-ceramic', 'undecided-material-prepped-prep-billing', 'missing-policy-seat-practice-prepped']. pfm-chosen-over-ceramic: 'extraction-prompt-last-mentioned-material' still leaves extraction.crown.material.value_incorrect -> try 'extraction-prompt-v2-selected-material'; missing-policy-seat-practice-prepped: bundle exposes input.policy.missing_at_runtime -> add companion 'coding-missing-policy-default-seat'; missing-policy-seat-practice-prepped: bundle exposes coding.policy.silent_default_when_missing -> add companion 'coding-missing-policy-default-seat'
- step 14 [fix] **evaluate_candidate_bundle** — ['coding-missing-policy-default-seat', 'extraction-prompt-v2-selected-material', 'extraction-schema-v2-crown-stage', 'qa-v2-independent-policy-and-evidence-checks']: Revision 1: pfm-chosen-over-ceramic: 'extraction-prompt-last-mentioned-material' still leaves extraction.crown.material.value_incorrect -> try 'extraction-prompt-v2-selected-material'; missing-policy-seat-practice-prepped: bundle exposes input.policy.missing_at_runtime -> add companion 'coding-missing-policy-default-seat'; missing-policy-seat-practice-prepped: bundle exposes coding.policy.silent_default_when_missing -> add companion 'coding-missing-policy-default-seat'
- step 14 [fix] **candidate_rejected** — Gate failed ['no_regressions', 'no_critical_regressions'] with regressions ['missing-policy-prep-practice-seated']. missing-policy-prep-practice-seated: 'coding-missing-policy-default-seat' still leaves input.policy.missing_at_runtime -> try 'pipeline-policy-registry-resolution'
- step 14 [fix] **evaluate_candidate_bundle** — ['extraction-prompt-v2-selected-material', 'extraction-schema-v2-crown-stage', 'pipeline-policy-registry-resolution', 'qa-v2-independent-policy-and-evidence-checks']: Revision 2: missing-policy-prep-practice-seated: 'coding-missing-policy-default-seat' still leaves input.policy.missing_at_runtime -> try 'pipeline-policy-registry-resolution'
- step 14 [fix] **fix_validated** — Bundle ['extraction-prompt-v2-selected-material', 'extraction-schema-v2-crown-stage', 'pipeline-policy-registry-resolution', 'qa-v2-independent-policy-and-evidence-checks'] passed the quality gate: 9 improvements, 0 regressions. Awaiting human approval.
