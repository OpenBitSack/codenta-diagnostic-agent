# 5-minute demo script

Setup (before the call): `make install`, then `make test` and `make demo` once, so caches are warm and
you know everything passes. Have the dashboard open in a browser: `make serve`, then visit
<http://localhost:8000/dashboard> and log in with `dev-reviewer`.

Say up front: *"This is an independent prototype on synthetic data, built as a follow-up to the
debugging exercise. The investigation you'll see uses a deterministic, scripted reasoner so it runs
without an API key. The same controller runs with Claude as the reasoner, and I'll show where."*

---

**0:00–0:45 — The problem (README top / root-cause report §1)**
* The crown note, the prediction `D0220, D2392, D2790`, QA passed.
* "Swapping D2790 for D2740 is not the fix. Two extraction fields are wrong, one of them latent, the
  schema can't express the visit stage, and QA can't catch extraction errors. The interesting part is
  proving that and not just asserting it."

**0:45–2:00 — Demo A: one investigation (`make demo` output or dashboard detail page)**
* Highlighted note spans → observations are facts; hypotheses A–F are untested claims.
* Walk the experiments: material-only → D2740 (still wrong), status-only → exact match, combined →
  exact, prep counterfactual → material is *latent*, coding refuted, QA v2 would have caught it.
* "Each verdict cites experiment IDs. If a reasoner claims support without evidence, the controller
  downgrades it. That's tested with a deliberately lying reasoner."

**2:00–2:45 — Demo E: the quality gate (same page, 'Candidate fix bundles')**
* Attempt 1 rejected: "last-mentioned material" breaks the PFM-chosen case.
* Attempt 2 rejected: defaulting to seat when policy is missing just moves the error.
* Attempt 3 passes: F1 0.82 → 1.0 on 27 synthetic cases, 0 regressions.
* "The regression suite surfaced a fourth defect, the silent policy default, that isn't visible in
  the ticket at all. And the 1.0 is on a synthetic set, so it shows the mechanics, not accuracy."
* Point at "pending human review" and "NOT deployed".

**2:45–3:15 — Demo B: prep-billing practice**
* Same note, prep policy: both fixes are *necessary* and the result is `D0220, D2392, D2740`.

**3:15–4:15 — Demos C + D: dedup without false merges (`make demo` output, dashboard index)**
* C: 25 duplicate tickets → 1 investigation; measured reasoning steps avoided.
* D: four crown tickets that look alike (wrong material, wrong stage, missing policy, overlapping).
  Similarity flags them as candidates (D-002 scores ≈0.93–0.99 against the interview case) but they get
  separate root causes. The overlapping one (D-004) has the same symptom signature as A, and A's
  validated fix replays correctly on it, so it links as a known issue with 0 reasoning steps.
* Review queue: "5 investigations, 1 decision — they converge on the same change set, so the reviewer
  approves once. Escalations with the same question also fold."

**4:15–5:00 — Scale, safety, what's real**
* `make load`: 1,000 synthetic tickets → 60 canonical → 6–7 full investigations on my laptop.
  "Measured locally, not an SLA."
* Read-only traces, practice scoping, sandbox-only configs, audit without note text, human approval
  required.
* Real vs deterministic: the Claude reasoner is implemented and unit-tested with a mocked client; I
  haven't run it against the live API in this repo. Fix validation is deterministic either way.
* Close: "The pipeline stays a fixed workflow. The agent sits beside it, investigates, and hands
  humans fewer, better decisions."
