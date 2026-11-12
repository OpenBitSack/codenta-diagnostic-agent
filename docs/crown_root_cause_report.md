# Root-cause report — interview crown case

> Independent prototype analysis on **synthetic data**. The production pipeline here is a deterministic
> simulation built to reproduce the trace shared in the interview exercise; it is not Codenta's system,
> prompts, or data. The investigation below was performed by the **deterministic-demo reasoner** (a
> scripted, rule-based investigation policy — not an LLM). Machine-generated reports from the actual
> runs are in [`generated/demo_a_seat_billing.md`](generated/demo_a_seat_billing.md) and
> [`generated/demo_b_prep_billing.md`](generated/demo_b_prep_billing.md) (regenerate with `make reports`;
> experiment IDs are deterministic hashes, investigation IDs are random).

## 1. The case

**Note:** "#14 cracked, pt c/o pain on biting. 1 PA taken. Prepped #14 for crown, temp placed, impression
to lab. Discussed gold and all-ceramic; pt chose all-ceramic. Also #19 MO composite filling done today.
Seat #14 in 2 wks."

| | Value |
|---|---|
| Practice policy | crowns billed at **seat** (`P-SEAT-01@v3`) |
| Prediction | `D0220, D2392, D2790` |
| Extraction (crown) | `status: completed`, `material: gold` |
| QA | passed |
| Expected | `D0220, D2392` (crown not billable today) |
| Expected under prep billing | `D0220, D2392, D2740` |

Rule card facts used: D2790 = full gold crown, D2740 = all-ceramic crown; crowns are billed once at prep or
seat per practice policy; crown material must reflect what the patient **selected**, not options discussed.

## 2. Short answer

There are **two independent extraction defects plus a QA gap**. The coding stage is not at fault.

| Hypothesis | Verdict | Role | Deciding evidence |
|---|---|---|---|
| A — material extracted wrong (`gold` vs selected `all_ceramic`) | **supported** | **latent** at a seat-billing practice; **necessary** at a prep-billing practice | Note quote "pt chose … all-ceramic"; replay `single:crown.material` → `D0220, D2392, D2740` (still wrong today); under the prep counterfactual, leaving material uncorrected yields D2790 instead of D2740 |
| B — status extracted wrong (`completed` vs `prepared`) | **supported** | **sufficient + necessary** for today's error | Note quotes "Prepped", "temp placed", "impression to lab", "Seat #14 in 2 wks"; replay `single:crown.status` → `D0220, D2392` (exact match) |
| B′ — schema cannot represent the stage | **supported** | **structural** | Schema v1 `crown.status ∈ {planned, completed}` has no "prepared"; only the schema-v2 candidate makes extraction emit the right stage (prompt-only candidates cannot) |
| C — coding misapplied the policy | **refuted** | — | Re-running coding on its recorded inputs reproduces the codes; with corrected facts and the correct policy, production coding produces exactly `D0220, D2392` |
| D — QA missed the error | **supported** | **contributing** | QA v1 passed a wrong prediction; candidate QA v2 (independent note-evidence checks) flags it via `crown_billing_event_note_evidence` and `crown_material_note_evidence` |
| Model capability | **untestable** here | — | Requires replaying the real model or an alternative model; not possible with the simulated stage |

**Why "change D2790 → D2740" is not a fix:** correcting only the material produces D2740 *today*, at a
seat-billing practice, for a crown that was only prepped — still a billing error, just a different one.
The timing error comes from the status field, and the status field is wrong because the schema cannot
express "prepared". The material error is latent today and becomes visible only under prep billing,
which is exactly what Demo B shows.

## 3. How the investigation proceeded (14 reasoning steps, 12 experiments, 0 LLM calls)

1. **Observe (deterministic, no reasoning step):** compared predicted vs expected (D2790 is a false
   positive), checked QA (passed), re-read the note with a phrase lexicon and recorded evidence spans,
   inspected the schema enum, and re-ran coding on its recorded inputs (reproduces → coding is
   consistent with the facts it was given). Earliest observable incorrect stage: **extraction**.
2. **Hypothesize:** six hypotheses generated from observations. Observations are facts; hypotheses are
   explicitly untested.
3. **Reproduce:** `baseline:recorded-extraction` and `baseline:rerun-extraction` both reproduce
   `D0220, D2392, D2790`, so the trace is self-consistent and the failure reproduces end to end.
4. **Isolate:** single-field corrections from the recorded extraction (coding + QA re-run in the sandbox).
   Material alone → D2740 (still wrong); status alone → exact match.
5. **Combine:** correcting both → exact match.
6. **Test the coding hypothesis:** the combined experiment uses the correct policy; production coding gets
   the right answer → C refuted.
7. **Counterfactual policy:** replay under prep billing with both fixes (→ `D0220, D2392, D2740`, matching
   the expected-under-prep codes), then leave each field out. Without the material fix → D2790 (wrong).
   This is what separates **latent** from **irrelevant**.
8. **QA candidate:** run QA v2 on the original failing output → it fails the prediction (would have held it).
9. **Component tests:** run the extraction stage with each catalog candidate alone. Both prompt candidates
   fix the material but not the stage; only schema v2 fixes the stage.
10. **Submit diagnosis.** The controller verified every verdict cites an executed experiment or
    observation (claims without evidence are downgraded to `inconclusive` — tested with an adversarial
    reasoner in `tests/test_agent.py`).

## 4. Fix search and regression validation (Demo E behaviour)

Fixes are evaluated as bundles on the 27-case synthetic regression set **plus** the target ticket, then
checked by the quality gate (no regressions, no critical regressions, F1 not worse, policy violations not
increased, false holds bounded, target fixed).

| Attempt | Bundle | Gate | Why |
|---|---|---|---|
| 1 | last-mentioned-material prompt + schema v2 + QA v2 | **rejected** | Regressions: `pfm-chosen-over-ceramic` ("last mentioned" picks the wrong material when the selection comes first), `undecided-material-prepped-prep-billing`, `missing-policy-seat-practice-prepped` (schema v2 exposes the missing-policy silent default) |
| 2 | selected-material prompt + schema v2 + QA v2 + default-to-seat when policy missing | **rejected** | Regression: `missing-policy-prep-practice-seated` (defaulting to seat is just a different wrong default) |
| 3 | selected-material prompt + schema v2 + **registry policy resolution with hold-when-missing** + QA v2 | **passed** | F1 0.8214 → 1.0, escaped errors 9 → 0, policy violations 2 → 0, 0 regressions, target fixed |

The revision was driven by **attribution**, not by trial and error: for each regressed case the agent
re-ran the deterministic observations under the candidate config and identified which cause was still
present (or newly exposed). It then swapped that cause's remedy for the next one in the catalog, or added
a companion change. A fourth, latent defect surfaced this way: the production pipeline **silently
defaults to prep billing when the practice policy is missing at runtime**. It is not visible in the
interview ticket, but the regression suite exposed it as soon as the status field became correct.

The passing bundle is **pending human review**. Approval records a decision only; nothing is deployed.

## 5. Recommended actions (for a human owner)

1. Extend the extraction schema with an explicit procedure stage (`planned | prepared | seated`) and
   change the extraction instructions to capture the **selected** material, with `null` when no selection
   is documented (and a `materials_discussed` list for context).
2. Resolve the practice policy from the authoritative registry when it is missing at runtime, and
   **hold** rather than default when it cannot be resolved.
3. Add QA checks that re-read the note independently of extraction (stage evidence, material-selection
   evidence, policy present). QA that only validates the extraction's internal consistency cannot catch
   extraction errors.
4. Keep the counterfactual policy test in the regression suite: one note, both billing policies.

## 6. Caveats

* The F1 of 1.0 is on a 27-case synthetic set designed alongside the simulated pipeline. It demonstrates
  the validation mechanics, **not** real-world accuracy.
* The simulated extraction stage is deterministic, so "model capability" cannot be tested. With the real
  LLM stage, repeated sampling and alternative-model replays would be needed.
* Three rule interpretations not on the interview card are documented as synthetic assumptions in
  `config/rule_cards/interview-v1.yaml` (additional PA images, undocumented material → hold, posterior
  tooth set).
