# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

[中文版](CHANGELOG.zh.md)

## [0.1.1] - 2026-10-03

One subject runs through this release: **a check that reports is not a check
that stops.** Every entry below either makes a gate actually block a merge, or
fixes a place where the automation was answering a different question than the
one it claimed to answer.

### Changed

- **The gates block, and they run in the order that saves work.** GitHub
  counts a skipped required check as passing, and a job downstream of a failed
  one is *skipped* rather than failed — so requiring the downstream jobs never
  required the test suite. Requiring a path-filtered workflow is worse: its
  check stays Pending forever and blocks every merge, with nothing an operator
  can do to satisfy it. One terminal gate now reads the result of every job
  and fails unless all of them succeeded, a `pull_request` trigger may not be
  path-filtered, and `main` carries branch protection. The ~200 static
  contract checks — repository-wide invariants asserted over the tree — now
  run as their own job that every pull-request test job depends on, so a
  broken contract costs one runner instead of twenty.
  ([#25](https://github.com/YongmaoLuo/Product-Development-Team/issues/25),
  [#26](https://github.com/YongmaoLuo/Product-Development-Team/issues/26))

- **What was reviewed is what lands.** Squash merging is disabled, so the
  commits a reviewer approved are the commits that reach `main`. The privacy
  and secret gates scan the commit range rather than only the working tree, so
  a credential that reaches a commit is caught before it reaches the remote.

### Fixed

- **A failing CI job reports red instead of disappearing.** The
  process-cleanup path could signal every process of the same user:
  `os.killpg(1, sig)` is `kill(-1, sig)` in the kernel — a broadcast, not
  "process group 1" — and on a runner that includes the worker that owns the
  step timeout and the log upload, so a job that should have failed hung until
  the platform-wide limit and published nothing. The guard now rejects a
  `pgid` of 1 or less; a value check, not a type check, is what keeps a mocked
  or unresolved pid away from the call.
  ([#20](https://github.com/YongmaoLuo/Product-Development-Team/issues/20))

- **Two probes were answering a different question than they claimed.** The
  port probe asked which process *mentions* a port instead of which process is
  *listening* on it, so a client socket in `CLOSE_WAIT` looked like a listener
  and a clean shutdown could be reported as a failure. The end-to-end defense
  layer had the same shape of bug: it looked for a directory a fresh checkout
  does not contain, which meant it had never run on a pull request at all.
  Both now check what they say they check, and the end-to-end lane runs before
  merge rather than after.
  ([#22](https://github.com/YongmaoLuo/Product-Development-Team/issues/22))

- **A green suite is evidence about the suite, not about the order it ran
  in.** Tests wrote runtime state into two process-wide dictionaries with no
  cleanup between them, so an assertion that counted across them passed
  because of where it happened to sit in the run. Every test now starts from
  empty runtime state, and each parallel job runs its files in a shuffled
  order whose seed is logged — the next run is reproducible, and passing no
  longer depends on being lucky about sequence.
  ([#26](https://github.com/YongmaoLuo/Product-Development-Team/issues/26))

## [0.1.0] - 2026-09-29

The first release, and the repository's initial commit: there is no earlier
state to compare against, and everything in this section is new.

### Added

- **A spec-driven agent harness: one operator, one idea, to a verified
  delivery.** It does the work the dozens of people on a product development
  team would do, and it runs as a loop rather than a pipeline — each round is
  judged against the artifacts the earlier rounds produced, and every step
  advances on the decision points the previous step made. That is what keeps
  the work converging on the right answer instead of drifting to a plausible
  one, and it is what makes the result auditable: you can read *why*, not just
  *what*.

- **Nothing is built before you have reviewed it.** Seven phases: requirement
  clarification, PRD, optional architecture, optional test design, task
  generation, execution, verification. The PRD, the architecture document and
  the test design share one review loop, and every decision point is a
  **CPEA** record (Context, Problem, Evaluation, Action) — the evidence behind
  a choice is on the page with the choice. Each point can be accepted,
  rejected (which rewrites only that point), questioned or deferred, and
  execution does not start until the points are accepted or deferred.

- **Execution runs tasks concurrently, bounded by what is actually safe.**
  Declared dependencies decide what may run together; within a layer, a
  conflict graph over the files each task declares serialises the tasks that
  share a file and runs the rest in parallel. A per-provider cap decides how
  many sub-agents are in flight, and OS-level file locks cover the task that
  turns out to touch a file it did not declare.

- **Verification runs concurrently too, and a failure generates work instead
  of ending the run.** Acceptance criteria become verification points checked
  against the running code — a test command, an HTTP call, a real browser, a
  code review — partitioned by method, with the groups and the points inside
  them running in parallel under one plan-wide ceiling. Failures become repair
  tasks that re-enter execution under the same concurrency rules and the same
  completion check, with no second, weaker path; a task counts as done only
  when the agent's report and its declared test command agree. The loop stops
  on pass, on an exhausted round budget, on the same failure set repeating, or
  on an operator stop.

- **Progress is a state machine in the database, not in the artifacts.** A
  plan's phase is a fact about the schema, and the legal moves between phases
  are transitions rather than assignments. Runtime state lives in one SQLite
  database; the JSON artifacts are the seed and the record, and the task list
  is derived and never rewritten — so a repair round leaves no second copy of
  the task graph behind, and an interrupted run resumes instead of restarting.
  A server refuses to start on a database whose write path is broken, and a
  clean shutdown leaves a restorable copy behind.

- **The boundaries are stated, not implied.** This is a single-user local
  tool: no accounts, no authentication, no per-user separation, and
  sub-agents run with the operator's own privileges — the machine is the
  boundary, not the application. Within that scope, a web page you merely have
  open cannot drive the instance, a hostname resolving to loopback is
  rejected, and files carrying provider credentials are written private and
  redacted once the process that needed them exits. What the repository
  carries is equally deliberate: provider capacity and routing ship as
  templates under `example/` that you copy into a gitignored directory, so no
  deployment's concrete provider names are compiled into anyone else's
  install. See [Security](docs/security.md).

[0.1.1]: https://github.com/YongmaoLuo/Product-Development-Team/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/YongmaoLuo/Product-Development-Team/releases/tag/v0.1.0
