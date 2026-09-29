# Execution — running tasks concurrently

Tasks are not run one at a time. The interesting question is not *can* they
run together but **when they may not**, and this page is about how that
question is answered at three different layers.

## Layer 1 — the dependency graph decides what *may* run

`tasks.json` is a graph, not a list. Each task declares what it depends on,
and the executor turns those edges into **dependency layers**: everything in
a layer can start at the same time, and the next layer starts only when the
previous one is done.

This is the coarse answer, and it is the one you can review before anything
runs — it is visible in the task graph itself.

## Layer 2 — the conflict graph decides what *actually* runs together

A dependency layer is not automatically safe. Two tasks can have no
declared dependency on each other and still both edit the same file.

So inside a layer, the executor builds a **conflict graph over each task's
declared `files_to_modify`** and splits the layer into *micro layers* that
never share a file:

- tasks that share a file land in the same connected component and are
  **serialised** across micro layers;
- tasks that do not share a file are coalesced into one micro layer and run
  **in parallel**.

A task that declares `__UNKNOWN_MODIFICATIONS__` means "I have not said
which files I touch" — *not* "I touch everything". Those tasks get a private
conflict key so they serialise against **each other** but not against tasks
that declared their files. (They used to share one sentinel key, which
collapsed every unknown task into a single component and killed all
parallelism for those plans. That is the reason the sentinel is handled
explicitly.)

## Layer 3 — what holds when the plan is wrong

Steps 1 and 2 are both **forward plans**: they are only as good as what the
tasks declared. A task can end up touching a file it did not declare, and
then the graph was silent about a real conflict.

Three mechanisms cover that gap:

**Provider slots.** A separate budget caps how many sub-agents are in flight
at once, per provider. The cap is resolved at dispatch time from the live
provider-capacity map, so a slow or rate-limited provider throttles itself
rather than being discovered as a wall.

**Runtime file locks.** Before a task touches its target files, the executor
takes real OS-level file locks for them and releases them when the task
finishes. This runs on the same path as the task itself, so the lock covers
the whole *read → modify → write* span rather than a single tool call.

**Containment.** The sub-agent runs with an edit/write guard that refuses
writes outside its own working directory, the shared plans directory, and
scratch space. This is a discipline guard rather than a security boundary —
the sub-agent has shell access, and a shell command is not constrained by a
tool-level hook. It exists to catch the accident, and it is worth knowing
that is what it is.

## Why the layering is deliberate

Each layer catches something the one above it cannot:

| Layer | Catches | Blind to |
|---|---|---|
| Dependency graph | declared ordering constraints | files entirely |
| Conflict graph | declared file overlap | undeclared overlap |
| Locks + slots | undeclared overlap, cost | (nothing within its scope) |

The graph is a **forward plan**, the slots bound **cost**, and the locks are
what actually hold when the plan turns out to be wrong. A design with only
the graph would be fast and occasionally wrong; a design with only locks
would be correct and needlessly serial.

## Retries and failure handling

A task that fails is retried with the error context attached, up to a bound.
Repeated failure on the same task triggers sub-task decomposition rather
than another identical attempt, and a loop detector stops a task that keeps
failing the same way instead of letting it consume the whole budget.

Every successful task is a git checkpoint (`[task-{id}]`), so the state of
the tree at any completed task is recoverable.

## Completion is decided by two signals

A task counts as done only when **the agent's own report and its declared
`test_command` exit code agree**. Either signal alone is insufficient — a
confident summary over a failing command is not done, and neither is a
command that exits zero because it was the wrong command.

This is why the empty-diff case is a *warning* rather than a failure:
audit-style tasks whose deliverable is a finding rather than a change are
legitimate, and a gate keyed on "did the file change" would retry them
forever. See [Design notes](../development/design-notes.md).
