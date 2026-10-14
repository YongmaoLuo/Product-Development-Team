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

## It runs in parallel, bounded by the fleet

Points are **partitioned by method** into groups. Groups run concurrently,
and the points inside a group run concurrently too.

Both levels are bounded, and both are bounded by the **same number**: how
many verification points the verification scene's providers can serve right
now.

!!! note "The width comes from the providers, not from a typed number"
    That width is the sum, over the scene's own chain, of each provider's
    declared cap scaled by how much of its 5-hour window is left — the
    arithmetic execution already uses for its own pools, read from the same
    quota payload the dispatch path is handed
    (`dynamic_provider_concurrency.fleet_capacity`). One provider declaring
    ten yields ten; two yield twenty; one down to 40% of its window yields
    four; a provider that has been **shut loses its share**, and when the
    whole chain is shut the answer is `0` — nobody is dispatched into a
    window that is closed.

    It is read **every time the gate binds**, not fixed when the round
    starts. A window that closes mid-round shrinks the fleet while the
    round is running, and a gate that froze its width at construction could
    not see it.

`parallelism_cap` (still in `verification.yaml`, still `4`) is the
**fallback**: it applies only when nothing is derivable — the verification
scene's chain could not be resolved at all. It is deliberately not a
"nothing available → run 4" rule; a measured `0` is a real answer from the
meters and is used as one.

A second, plan-wide bound sits above the groups so their fan-outs cannot
multiply past the number of slots the providers actually have. While the
scene resolves it reads the **same live figure** as each group; when the
chain cannot be resolved it falls back to the declared capacity ceiling
(`provider_capacity.configured_capacity_ceiling`).

Each group brackets its run with a `group_started` / `group_completed` pair
on the JSONL log, carrying the method, the point count and the per-group
pass/fail counts, so progress is readable while the round is running instead
of only at the end.

## Waiting on a closed quota window

When the scene has no dispatchable provider because its **quota windows are
shut** — not because the chain is unresolvable, and not because a local pool
is full — the dispatch does not fail. It parks the caller's work clock (so a
wait is not charged to the budget that exists to catch a wedged call), logs
that it is waiting along with which providers are shut and the soonest moment
any of them comes back, and probes the quota payload on a fixed five-minute
beat **with no upper limit on how long it keeps asking**. How long a window
stays shut is the provider's fact; a caller that gave up on its own schedule
would not get the window back, only a failure for a wait it declared over on
a figure it invented.

There is **one** watchdog for the whole process, not one per waiting
dispatch. The payload fans out to every provider in a single request, so a
watchdog per waiter would spend one request per waiter per interval to learn
one thing. Asking at the same moment is merged: one caller reads, the others
wait for it and take its answer. Measured over a closed window with eight
verification points asleep on it: **8 requests per tick before, 1 after**.

A wait is not a stall. A verification point asleep on a closed window has no
subprocess, no stdout and no progress to report, so the sub-agent watchdog's
stale sweep skips it — otherwise a wait long enough to cross the threshold
would spend a kill path on a process that does not exist and then throw the
handle away, disarming the watchdog for the subprocess that does start when
the window reopens.

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
