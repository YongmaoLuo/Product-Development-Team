# Design notes

Decisions that are load-bearing but easy to mistake for incidental
plumbing. Each of these has been the cause of a real failure, which is why
it is written down.

## Two independent completion signals

A task counts as done only when **the agent's own report and its declared
`test_command` exit code agree**. Either alone is insufficient; the
cross-verify step takes the majority verdict.

The temptation is to trust the report, because it reads well and the agent
is the only thing that knows what it was trying to do. The counter-example
is the whole reason the rule exists: a confident summary over a command that
exits non-zero is not done, and a command that exits zero because it was the
*wrong* command is not done either. Neither signal is sufficient alone
because they fail in different directions.

## Empty diff is a warning, not a failure

An earlier gate hard-failed any task that produced no diff. That is wrong
for a whole class of legitimate work: audit-style tasks whose deliverable is
a **finding** — a line number, an import-shape verification, a performance
baseline — rather than a change.

The failure mode was concrete: those tasks were retried until the plan
stranded. They are now detected (declared `verification_only`, or by keyword)
and verified by a **second-pass audit** that compares the specification's
acceptance criteria against the reported answer, instead of relying on the
runtime proof a code change would have given.

The complementary rule: a non-audit task with an empty diff is a **warning**
that operators can grep for, not a silent pass. Removing the hard failure
did not mean removing the signal.

## Bounded loops everywhere

Every retry loop in this system is capped, and each cap has an early exit
that is more interesting than the cap itself:

- **Verification rounds** stop early when two consecutive rounds fail with
  the *identical* set of verification points — repairing is not working, so
  continuing only burns budget to arrive at the same place.
- **Task retries** decompose the task instead of repeating an identical
  attempt after repeated failure, and a loop detector stops a task that
  keeps failing the same way.
- **Subprocess timeouts** kill the whole process group, not just the direct
  child. `subprocess.run(..., timeout=N)` does not do this when the command
  runs through a shell: the direct child is the shell, and the real work
  survives it.

A loop without an early exit is not "more thorough", it is a budget with no
owner.

## `running` is a claim, not evidence

Status fields are written by a thread that may itself have died. A field
saying `running` means "the last writer said so", which is exactly the state
you are in when something has gone wrong.

The system therefore cross-checks the claim against the filesystem: the
modification time of the newest verification log, of the execution results
file, and of the round report. A process that has produced no output for far
longer than any single step should take is reported as stuck rather than
left looking healthy. The same reasoning applies to `/health` (is the
process serving HTTP?) versus `/health/data` (can it actually read its
database?) — a server can be perfectly healthy by the first and dead by the
second.

## The status/reason contract

Several stop conditions are recognised by a **status value**, not by a
reason string. That is deliberate: reason strings are prose, they get
renamed, and a guard comparing them breaks silently on the day someone
improves the wording. This has happened — the convergence check stopped
matching after a rename and stayed broken for as long as that version
shipped, with every round burning the full budget.

When a condition needs to be machine-recognised, give it a value that is
part of a contract and pinned by a test.

## Runtime state is one directory

The state database, its WAL siblings, the boot counter and the shutdown
backups live together under one gitignored directory. They are one unit, and
the reason is not tidiness: the boot counter and the backup directory derive
their location from the database's parent, so a partial move produces
processes reading one file and writing another.

The general form of that bug is **a path re-derived from `__file__` in
several places**. The state-database path was re-derived in nine, two of
which had already drifted — each correctly, on the day it was written. It is
now declared once and enforced by a static gate. See
[Configuration](../operations/configuration.md).

## A gate that reads a file it does not own

The macOS-keychain merge gate decides whether the CI lane runs the
whole-delivery E2E in its strict position by reading two module-level
constants out of that E2E file — the switch's name and the values it accepts
as "on" — instead of keeping its own copy of either spelling. That is the
right call: the E2E file is the definition of what the switch means, and a
gate with a private copy would report the lane strict while the file read
something else entirely.

The cost is that the gate has an input it does not control, and the shape of
that input is worth writing down. The file it parses is reached from the
gate's **own location** — `Path(__file__).resolve().parents[3]` plus a fixed
suffix — not from the working directory and not from an absolute path. So
it is the E2E file in the same tree as the collected gate module. In CI that
means the checkout of the job running the gates, while the lane that actually
collects the E2E checks out the same commit in a different runner: same path,
same commit, two workspaces. A green gate is therefore evidence about the
file *at that commit*, not a byte-for-byte comparison of the two jobs'
checkouts.

What each way of failing looks like, since the distinction is the whole
point of asking:

- **The constant is renamed or removed.** A clean `AssertionError` naming the
  file and the constant. The gate is doing its job.
- **The constant is present but not statically readable** — spelled as an
  expression rather than a literal or a set wrapper. Also a clean
  `AssertionError`, raised by the value reader rather than the finder.
- **The file cannot be parsed at all.** The standard library's `SyntaxError`
  escapes. The report names the E2E file and the offending line, but the top
  frame is the parser inside the standard library, and nothing in it says
  *which* gate was reading the file or *which constant* it was after. A
  reader who has not touched the E2E file sees a syntax error in a file they
  believe nothing parses. The gate passes the path to `ast.parse` as its
  filename, so the name is right — but rendering that exception reduces it to
  a base name and a line number, which is the whole of what a reader gets.
- **The file is gone.** A `FileNotFoundError` from reading it, not the
  existence assertion the gates have for it: three tests read that file and
  only one checks it is there first.

The general form is the one above, stated once: **a reader with an input it
did not write needs its failure messages to name the reader, not just the
input.** The two clean cases do; the two that escape do not, and both escapes
read at first like a defect in the gate's own logic rather than like a
corrupt input to it.

## A contract can be pinned by the file that would break it

The gates above verify the switch's *name* by cross-reference — a rename on
one side only turns them red — and they verify the workflow's value by asking
whether it is **in** the set the E2E file declares. They do not pin the
accepted set itself. A one-sided change to either file is caught; a
coordinated change to both, whether a wholesale rename or a set widened to
admit a loose spelling, stays green.

The accepted values are pinned cell by cell elsewhere: the E2E file's own
truth table asserts what each spelling must decide. So the guarantee exists,
and it sits in the same file that a clobber takes out. The gate's guarantee
and the value's guarantee share a single point of failure by construction —
which is the price of the design, not a defect in it, and worth naming
before someone reads the cross-reference as more than it is.

## Comments explain; they do not narrate

A comment may say what the code does and why. It may not quote a person,
name a sibling checkout, or narrate what happened on one machine — the last
of those reads like evidence while being the one kind of statement a reader
cannot check.

The tests carry the failure history instead, and that is the right place for
it: a gate's docstring is read by exactly the person about to change the
behaviour it protects.
