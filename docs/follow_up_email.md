# Follow-up email (draft)

**To:** Jon, Melvin
**Subject:** Follow-up on the crown-coding debugging exercise: a small diagnostic-agent prototype

Hi Jon and Melvin,

Thanks again for the conversation and for the debugging exercise. I kept thinking about the crown
case afterwards, so I built a small prototype to explore how I'd approach this class of problem at
production scale. It's an independent project on synthetic data only. I haven't used anything from
Codenta's systems, prompts, or data, and it's not meant to represent your actual pipeline.

- Code: [GITHUB_REPOSITORY_URL]
- 5-minute walkthrough: [DEMO_VIDEO_URL]

**What it does**

- **Multi-cause root-cause analysis.** For the crown note it shows, with sandbox replays, that the
  error has two extraction causes: the stage (prepped, not seated) and the selected material
  (all-ceramic, not gold). The material error is latent under seat billing and only shows up under prep
  billing. The schema can't represent "prepared", the coding stage is refuted as a cause, and QA
  missed the error because it never re-reads the note. That's why changing D2790 to D2740 alone isn't
  enough.
- **A bounded diagnostic agent, separate from the pipeline.** The prediction pipeline stays a fixed
  workflow. The agent runs only per incident, inside a sandbox, with hard budgets (steps, experiments,
  tokens, cost, deadline) and explicit stopping and escalation rules. Every conclusion has to cite an
  executed experiment. The demo uses a clearly labelled deterministic reasoner; a Claude-based reasoner
  plugs into the same controller.
- **Regression-validated fixes.** Candidate fixes are evaluated as bundles against a synthetic regression
  set plus the original ticket. The quality gate rejected two plausible fixes that broke other cases.
  The agent attributed the regressions and revised the bundle until one passed. Nothing is deployed;
  a human approves or rejects.
- **Similar-issue deduplication without false merges.** Exact duplicates collapse into one investigation.
  Similar-looking tickets are surfaced as candidates only, and grouping under a known issue requires
  replay evidence. Four crown tickets that look alike but have different causes stay separate.
- **Reduced human workload.** Reviewers see one decision per change set or escalation question, ranked
  by severity × affected tickets, with the evidence on one screen. In a local synthetic load test,
  1,000 tickets resulted in 60 canonical investigations, of which only 6–7 needed the full agent loop.
  These are measurements from my laptop, not a benchmark.

The README covers what's implemented, what's tested, and what's still conceptual (for example, AWS
deployment is a design note only, and I haven't run the Claude reasoner against the live API in this
repo).

I'd be glad to walk through it or hear where you'd take it differently.

Best regards,
[YOUR_NAME]
[YOUR_PHONE] · [YOUR_LINKEDIN_URL]
