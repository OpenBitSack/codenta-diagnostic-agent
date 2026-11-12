# Architecture

> Independent portfolio / interview follow-up prototype — not an official Codenta project. Synthetic data only.

## System overview

```mermaid
flowchart LR
    subgraph PROD["Production prediction pipeline (fixed workflow, NOT an agent) — simulated here"]
        N[Clinical note] --> EX[Extraction LLM stage]
        EX --> CO[Coding stage<br/>rule card + practice policy]
        CO --> QA[QA stage]
        QA --> OUT[Predicted CDT codes]
    end

    OUT -. saved stage traces .-> TS[(Read-only trace store<br/>practice-scoped)]
    QAANALYST[QA analyst / reviewer finds a wrong prediction] --> API

    subgraph DIAG["Diagnostic system (separate service; never writes to production)"]
        API[FastAPI intake<br/>validation · auth · rate limit · backpressure]
        API --> FP{Exact fingerprint<br/>DB UNIQUE constraint}
        FP -- duplicate --> LINK[Attach report to canonical investigation<br/>report_count++ · priority bump]
        FP -- new --> Q[(Persistent job queue<br/>priority · leases · retries · dead-letter)]
        API --> SIM[Similarity retrieval<br/>metadata prefilter + embedding cosine<br/>CANDIDATES ONLY]
        Q --> W[Worker pool<br/>bounded concurrency]
        W --> FAST{Known-issue fast path<br/>identical symptom signature AND<br/>parent's validated fix replays correctly?}
        FAST -- yes --> KI
        FAST -- no --> AG[Bounded diagnostic agent<br/>Observe → Hypothesize → Test → Evaluate → Decide]
        AG <--> TOOLS[Typed tools]
        TOOLS --> TS
        TOOLS --> SB[Sandbox replay engine<br/>versioned config overrides]
        TOOLS --> EV[Regression runner<br/>synthetic eval set + metrics]
        AG --> FIX[Candidate fix generator<br/>+ quality gate + revision loop]
        FIX --> EV
        AG -- budget / ambiguity --> ESC[Escalation report]
        FIX --> KI[(Known issues<br/>confirmed root-cause groups)]
        AG --> CK[(Checkpointed investigation state<br/>audit log)]
    end

    KI --> RQ[Human review queue + dashboard<br/>one decision per change set / question]
    ESC --> RQ
    RQ -->|approve / reject only| CM[Standard change-management process<br/>outside this system]
    CFG[(Versioned config registry<br/>rule cards · schemas · prompts · policies · QA rules · catalog)] --> PROD
    CFG --> SB
```

The arrow from the review queue to change management is the only path toward production, and it
is a human decision. The diagnostic system has no write access to production prompts, schemas,
policies or traces.

## Investigation lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued: intake (new fingerprint)
    queued --> investigating: worker claims job (lease)
    investigating --> investigating: checkpoint each step / crash → lease expiry → resume
    investigating --> linked_known_issue: fast path (signature match + replay of parent's validated fix)
    investigating --> escalated: label/doc conflict · budget exhausted · no safe alternative · grouped with open escalation
    investigating --> fix_validated: diagnosis + bundle passes quality gate
    investigating --> failed: max attempts → dead-letter
    failed --> queued: admin requeue
    fix_validated --> approved: human approves change set
    fix_validated --> rejected: human rejects
    linked_known_issue --> approved: parent approved
    linked_known_issue --> rejected: parent rejected
```

## Agent loop (one investigation)

```mermaid
sequenceDiagram
    participant C as Controller (deterministic, enforces budgets)
    participant R as Reasoner (deterministic-demo OR Claude)
    participant T as Tools
    participant S as Sandbox / Regression
    C->>T: observe (deterministic checks, 0 reasoning steps)
    T-->>C: observations with note evidence spans
    C->>R: state + observations
    R-->>C: record_hypotheses (A material, B status, C coding policy, D QA, ...)
    loop until all testable hypotheses tested or budget exhausted
        R-->>C: tool call (e.g. run_sandbox_replay single:crown.status)
        C->>C: budget check (steps, experiments, regression runs, tokens, cost, deadline)
        C->>S: execute in sandbox
        S-->>C: experiment result (deterministic id, cached)
        C->>C: checkpoint + audit
    end
    R-->>C: submit_diagnosis (verdicts citing experiment / observation ids)
    C->>C: verify citations — unsupported claims are downgraded to inconclusive
    C->>S: candidate bundle → regression suite + target ticket → quality gate
    alt gate fails
        C->>C: attribute regressions → swap remedy / add companion → retry
    else gate passes
        C-->>C: fix_validated (pending human review)
    end
```

## Code map

| Area | Module |
|---|---|
| Simulated production pipeline | `src/codenta_diag/pipeline/` (`extraction.py`, `coding.py`, `qa.py`, `runner.py`, `lexicon.py`) |
| Versioned configuration | `config/` + `src/codenta_diag/config_registry.py` |
| Intake + validation | `src/codenta_diag/intake/` |
| Dedup (fingerprint, similarity, grouping) | `src/codenta_diag/dedup/` |
| Agent (controller, reasoners, tools, fixes, reports) | `src/codenta_diag/agent/` |
| Sandbox replay | `src/codenta_diag/sandbox/` |
| Evaluation + quality gate | `src/codenta_diag/evaluation/` |
| Queue, workers, rate limiting | `src/codenta_diag/jobs/` |
| Persistence | `src/codenta_diag/db/` (SQLAlchemy; SQLite or PostgreSQL) |
| API + dashboard | `src/codenta_diag/api/` |
| Metrics, review | `src/codenta_diag/metrics.py`, `src/codenta_diag/review.py` |
