# Verification — the parallel verifier and the loop back

Verification here is not a report produced at the end. It is a **loop with a
return edge**: a failure does not stop the pipeline, it *generates work*.

Two things make it worth its own page — the verifier runs concurrently, and
what it does with a failure is the shape of the whole system.

## What a verification point is

Verification reads the PRD, the architecture document and the test design,
and turns them into a plan of **verification points**. Each point has an id,
a method, a priority and an expected result, and each one is checked against
the running code rather than against the executor's summary.

| Method | How the point is checked |
|---|---|
| `automated_test` | a command whose exit code decides |
| `code_review` | an LLM review of the relevant code |
| `ui_validation` | a real browser driving the UI |
| `api_test` | an HTTP request and assertions on the response |
| `manual_check` | needs a human — reported, not silently passed |

## It runs in parallel, with two nested bounds

Points are **partitioned by method** into groups. Groups run concurrently,
and the points inside a group run concurrently too.

Both levels are bounded, and the second bound is the one that matters:

!!! note "The per-group cap alone would be a lie"
    `parallelism_cap` throttles *one group's* own fan-out. With several
    groups running at once, per-group caps **multiply** (cap × groups) and
    quietly exceed the number an operator thinks they configured.

    So a single `plan_semaphore` is created once per round and shared by
    every group. That shared ceiling — not the per-group knob — is the real
    fleet limit.

Each group brackets its run with a `group_started` / `group_completed` pair
on the JSONL log, carrying the method, the point count and the per-group
pass/fail counts, so progress is readable while the round is running instead
of only at the end.

## What happens when a round fails

This is the return edge:

```
   ┌───────────────────────────────────────────────────────┐
   │                                                       │
   ▼                                                       │
[Execution] ──► [Verification round N] ──► PASSED ──► done │
                        │                                  │
                        └── FAILED ────────────────────────┘
                             │
                             ├─ requirement deviations + failed points
                             ├─ turned into repair tasks
                             └─ written into state.db  ──► back to Execution
```

The crucial detail is **where the repair tasks go**. They are written
straight into the state database (`state.db`, through `PlanTaskRepository`),
and the executor reconciles them out of there into the DAG on its next load
— so it picks them up the way it picks up any other task:

- the same concurrency rules apply (dependency layers, conflict graph,
  provider slots, file locks);
- the same dual-signal completion check applies;
- the same git checkpointing applies.

There is no second, weaker execution path for repairs. A repair task is a
task.

That destination is deliberate, so it is worth stating what it rules out:
**execution state lives in the database, not in the artifact files.** The
JSON under the plan directory is the *seed* and the *record* — `tasks.json`
is the graph task generation produced, and nothing rewrites it to carry
runtime results. A repair round therefore leaves no second copy of the task
graph behind for a later reader to reconcile against the first, and no
half-written file to lose if the process dies mid-round.

Sub-tasks a breakdown adds take the same route for the same reason: the
refiner writes them to the database too, so a mid-round split is a row, not
a second version of the graph. Both are one mechanism — *new work appears
in `state.db`, and the executor reconciles it into the DAG on its next
load* — and keeping it to one mechanism is what keeps a crashed round
recoverable rather than ambiguous.

Repair generation is also **automatic** — there is no confirmation gate
between "verification failed" and "the executor is fixing it". If repair
generation itself fails (a provider hiccup, an empty response), that is
recorded and retried on the next round rather than parking the plan and
waiting for a human.

## The loop is bounded on four sides

| Stop condition | Meaning |
|---|---|
| `passed` | Acceptance criteria hold. Done. |
| `max_rounds_reached` | The round budget is exhausted. |
| `same_failure_repeated` | Two consecutive rounds failed with the **identical set** of verification points. |
| `user_stopped` | An operator stopped it. |

`same_failure_repeated` is the one worth understanding. If round N and round
N+1 fail on exactly the same points, repairing is not working. Continuing
would burn the remaining budget to reach the same place, so the loop stops
early and says why.

!!! warning "This condition is checked by status, not by reason string"
    The auto-loop recognises convergence by the orchestrator returning
    `status == "loop_stopped"` — **not** by comparing the `stop_reason`
    string. An earlier version compared the string, and when that string was
    renamed the guard silently stopped matching: the convergence check never
    fired for as long as that version shipped, every round burned the full
    budget, and the recorded reason was overwritten by
    `max_rounds_reached`. If you touch either side of this contract, they
    move together.

## Reading a stopped loop

A plan that ends in `loop_stopped` is not the same as one that failed. It
means the system ran out of *ways to make progress* — either the budget, or
the identical-failure check — and stopped rather than thrashing. The
[`GET /api/verification/{plan_id}/status`](../operations/running.md) response
carries `stop_reason`, and the per-round logs under
`plans/{plan_id}/logs/` say which points were failing at each round.

Not every "still running" is running: a verification round that has produced
no log output for a long stretch is far more likely to be stuck than slow,
which is what the watchdog ticks are for. See
[Design notes](../development/design-notes.md).

## Restarting a stopped loop

`POST /api/verification/{plan_id}/reset` resets the round counters and
clears the current round's artifacts **without** touching the verification
point list, and then `/start` runs again. Pass
`{"clear_verification_plan": true}` if you want the next round to
re-generate the points as well — that is a deliberate second step because
regenerating the plan changes what "passing" means.
