# Workflow

Seven phases, from a one-line requirement to a verified delivery. Three of
them are reviewable before anything is built, and two of them run
concurrently.

```
requirement
    │
    ▼
[1] Requirement clarification ──► interview.json
    │                              (5 dimensions: background, goals,
    │                               scope, constraints, acceptance)
    ▼
[2] PRD generation ─────────────► prd.json (CPEA decision points)
    │        ▲
    │        └── review / revise loop until every point is accepted
    ▼
[3] Architecture design (opt.) ─► arch-design.md     ┐
    │        ▲                                        │ same
    │        └── review / revise loop                 │ review
    ▼                                                  │ machinery
[4] Test design (opt.) ─────────► test-design.md      ┘
    │
    ▼
[5] Task generation ────────────► tasks.json
    │
    ▼
[6] Autonomous execution ───────► code + execution.log
    │   ⚡ CONCURRENT — dependency layers, then conflict-free micro
    │      layers inside each layer (file-level conflict graph),
    │      bounded by provider slots; runtime file locks as backstop
    │   ▶ runtime state lives in state.db (SQLite), not in the artifacts
    ▼
[7] Verification ───────────────► verification_report.json
    │   ⚡ CONCURRENT — VPs partitioned by method; groups and the VPs
    │      inside them both run in parallel, nested under one shared
    │      plan-wide ceiling
    │
    ├─ ✗ FAILED ──► repair tasks written to state.db ──► back to [6]
    │               (the executor reconciles them into the DAG from
    │                there; tasks.json is never rewritten)
    │               (bounded: stops on pass, round budget exhausted,
    │                the same failure set repeating, or operator stop)
    │
    └─ ✓ PASSED ──► done
```

## The phases

| # | Phase | Produces | Optional |
|---|---|---|---|
| 1 | Requirement clarification | `interview.json` | no |
| 2 | PRD generation | `prd.json` | no |
| 3 | Architecture design | `arch-design.md` | yes |
| 4 | Test design | `test-design.md` | yes |
| 5 | Task generation | `tasks.json` | no |
| 6 | Autonomous execution | code, `execution.log` | no |
| 7 | Verification | `verification_report.json` | no |

Phases are **load-bearing on each other**. Verification compares the running
code against the documents the earlier phases produced — which is why a
change to PRD generation without a matching acceptance test will pass
`pytest` and still be wrong. See [Contributing](development/contributing.md)
for what that means in practice.

## CPEA — what a decision point looks like

Every decision point in the PRD, the architecture document and the test
design is a **CPEA** record:

| Element | What it answers |
|---|---|
| **C**ontext | What is the situation this decision is made in? |
| **P**roblem | What specifically has to be decided? |
| **E**valuation | What were the options, and what is the evidence for each? |
| **A**ction | What was chosen? |

The point of the shape is that a reviewer sees the **evidence behind a
choice**, not just the choice. "We chose X" is not reviewable; "we chose X
because Y and Z, and the alternative would have cost W" is.

## The review loop

The PRD, the architecture document and the test design all run through the
same machinery. Each decision point can be:

- **accepted** — the point passes;
- **rejected** — with a reason, which triggers a **targeted rewrite of that
  point only**. Accepted points are preserved; the refiner does not get to
  re-litigate what you already approved.
- **questioned** — the system answers, and you decide again;
- **skipped** — deferred, to come back to.

A document is only approved when every point is accepted or skipped. After
the optional phases, the system asks explicitly whether you want the
architecture and test design generated or want to go straight to tasks —
that is a real question with a real answer, not a formality.

## Two things that happen concurrently

The two halves that are not review get their own pages, because the
interesting engineering is there:

- **[Execution](architecture/execution.md)** — dependency layers, a
  file-level conflict graph, provider slots, and runtime file locks.
- **[Verification](architecture/verification.md)** — method-partitioned
  parallel verification, and the bounded loop that feeds failures back into
  execution as repair tasks.

## Where the artifacts land

Everything above lives under the plan directory (`plans/{plan_id}/`), plus
whatever the executor writes into the project directory you pointed it at.
The verification logs are split per round
(`logs/verification_{round}_{timestamp}.log`) so a long-running plan can be
read round by round rather than as one growing file.
