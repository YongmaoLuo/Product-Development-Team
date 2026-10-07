# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

[中文版](CHANGELOG.zh.md)

## [0.1.2] - 2026-10-07

**What a run may touch, and what a green report proves.** Three capabilities
are new in this release: notification secrets that can be read from the
macOS keychain instead of the environment, coding sub-agents that can be
confined to an OS sandbox, and a verification step that can no longer pass
while an acceptance criterion has no verification point. The rest are fixes
to what you see while a plan runs, plus the repository's own checks, which
now block a merge rather than merely report on it.

### Added

- **Notification secrets can come from the macOS keychain instead of the
  environment.** The Feishu app secret and the Telegram bot token can live in
  a keychain of this project's own — not the login keychain, which holds
  every credential the account has ever saved — and reach the process that
  needs them over a file descriptor, so the value is in no environment and in
  no `ps eew` output. Off by default and macOS-only: a deployment that
  changes nothing keeps reading `backend/.env` exactly as before, and on
  Linux the keychain is not consulted at all. Enabled, it does not fall back:
  a missing item is reported missing rather than quietly read from a
  plaintext variable, because a silent downgrade to a value any process on
  the machine can read is worse than a clear failure.
  `backend/.venv/bin/python3 -m backend.cli secrets verify` reports which
  source each secret actually came from — until it says `source=keychain`,
  the migration is not done. See
  [Keychain migration](docs/operations/keychain-migration.md).
  ([#34](https://github.com/YongmaoLuo/Product-Development-Team/pull/34))

- **Coding sub-agents can run inside an OS sandbox.** Sub-agents are spawned
  with their confirmation prompts off, which is exactly when the filesystem
  needs a boundary that is not a prompt: point `PDT_SANDBOX_PROFILE` at a
  Seatbelt profile (a template ships as
  `example/sandbox_profile.sb.example`) and every sub-agent runs inside it.
  Nothing configured means nothing changes — the identical command line as
  before. A profile that is configured but cannot be applied stops the run
  instead of spawning anyway: a sandbox that quietly turns itself off is
  worse than none, because the configuration still says it is on. See
  [Configuration](docs/operations/configuration.md).
  ([#34](https://github.com/YongmaoLuo/Product-Development-Team/pull/34))

- **Verification can no longer pass while an acceptance criterion has no
  verification point.** A criterion could be dropped during test design and
  nothing would notice: no verification point judged it, and the report still
  said PASSED, so the omission was invisible from the outside. The plan is
  now cross-checked against the PRD's acceptance criteria; an uncovered one
  sends the planner back to add a point for it, and a gap that survives the
  retry is recorded on the plan rather than lost inside a green report. The
  match is deliberately narrow — only criteria carrying an explicit
  `【label】` are checked, and an unlabelled sentence is skipped rather than
  guessed at — because a false accusation against a healthy plan costs more
  than a miss.
  ([#34](https://github.com/YongmaoLuo/Product-Development-Team/pull/34))

### Changed

- **A merge is blocked by the checks that ran, not by the ones that were
  skipped.** GitHub treats a skipped check as a pass, so marking the checks
  *after* the tests as mandatory never made the tests mandatory; one check
  now reads the result of every check on the pull request and fails unless
  all of them succeeded. The repository's static contract checks run as their
  own step ahead of the test suites, so a broken contract costs one machine
  instead of twenty, and the check that guards merging no longer runs on
  manual runs — where it was red by construction. `main` also stopped
  re-running the whole suite after a merge: the pull request already ran it,
  on the tree that lands.
  ([#23](https://github.com/YongmaoLuo/Product-Development-Team/pull/23),
  [#24](https://github.com/YongmaoLuo/Product-Development-Team/pull/24),
  [#30](https://github.com/YongmaoLuo/Product-Development-Team/pull/30),
  [#31](https://github.com/YongmaoLuo/Product-Development-Team/pull/31))

- **The privacy and secret scans now cover history, and what a merge copies
  into it.** A check that reads only the working tree cannot see a commit
  that added a private identifier and a later commit that removed it: the
  tree is clean while the commit stays fetchable by its SHA. The scans now
  walk every commit in a range, and they cover the three fields GitHub copies
  verbatim into `main`'s history — the branch name, the pull-request title
  and its body. A pre-push hook ships alongside
  (`scripts/install_git_hooks.sh`) so a failure lands before the push, when
  fixing it is a rewrite rather than a history problem.
  ([#2](https://github.com/YongmaoLuo/Product-Development-Team/pull/2),
  [#30](https://github.com/YongmaoLuo/Product-Development-Team/pull/30))

- **Installing the backend goes through one locked dependency set.**
  `uv sync --project backend` replaces the pip steps, and
  `backend/pyproject.toml` with `backend/uv.lock` pins the whole transitive
  tree — a fresh clone installs what CI tested. CI installs with `--locked`,
  so a manifest and a lock that have drifted apart fail instead of being
  silently re-resolved into whatever the index offers that day. Python 3.11
  is now a structural requirement (`>=3.11,<3.12`): uv refuses to build the
  environment on an interpreter this tree has never been tested on.
  ([#35](https://github.com/YongmaoLuo/Product-Development-Team/pull/35),
  [#36](https://github.com/YongmaoLuo/Product-Development-Team/pull/36))

- **Credential-bearing scratch files have a home of their own.** The
  sub-agent settings file carries the routed provider's key; it was already
  written private and redacted once the process that needed it exited, but it
  landed in the system temp directory — a different path on every machine,
  and collected by nothing. It now lives under `~/.pdt-scratch`: one
  predictable location per user, private by mode, with each dispatch's
  directory removed once it is 30 days old — long enough for the post-mortem
  the redacted copy is kept for.
  ([#11](https://github.com/YongmaoLuo/Product-Development-Team/pull/11))

### Fixed

- **A plan's progress cards say what is running at every moment.** The Feishu
  cards, and the Telegram messages beside them, could go silent — a bare
  "verifying" with no name while the round was planning or judging — freeze
  on a stale body, contradict themselves after a repair round, or compute an
  elapsed time in the wrong timezone, so a ten-minute window could read as
  eight hours; Telegram also opened a new message for a state that had not
  changed. Current activity is now derived from the execution log whenever no
  task or verification point can name it, header and body are reconciled
  against the same source, and an unchanged card is updated in place.
  ([#33](https://github.com/YongmaoLuo/Product-Development-Team/pull/33))

- **A task that was split no longer comes back as an empty row.** When the
  executor split a task into its sub-tasks, a later bookkeeping write could
  re-create the removed parent with nothing in it — no status, no title — and
  the scheduler would stop with "No schedulable micro-layer found" while
  every real sub-task sat pending. The guard that keeps removed tasks out now
  covers every write path, and a write it skips is logged rather than dropped
  in silence.
  ([#29](https://github.com/YongmaoLuo/Product-Development-Team/pull/29))

- **Three ways a task could fail for reasons that were not its own.** Prose
  in a sub-agent's report could be parsed as a file path and written to disk
  — one run died on `File name too long` — and a captured target that is not
  a usable in-project path is now skipped as prose instead. A task whose
  declared test command demands a clean tree (a restore or revert task, whose
  success condition is an empty diff) was failed by the empty-diff check for
  producing exactly the tree it was asked to produce; it is recognised now.
  And a task the executor split into children could survive as a failed
  parent beside children that had all completed.
  ([#34](https://github.com/YongmaoLuo/Product-Development-Team/pull/34))

- **Stopping a service can no longer be misreported, and a start is only a
  start if it is ours.** The check for "is anything on this port" asked which
  process *mentioned* the port instead of which process was *listening* on
  it: a client connection that had already closed could make an empty port
  look occupied, and a clean stop could be reported as a failure. A service
  was also recorded as started whenever anything answered on its port — even
  a process this tool had not spawned. The check now asks for the listener,
  and a start counts only when the listener is the process that was started.
  ([#21](https://github.com/YongmaoLuo/Product-Development-Team/pull/21))

- **A cleanup can no longer become a broadcast.** The code that stops a
  subprocess and everything it spawned accepted ids that were not real
  processes; the value 1 does not mean "the first group" to the kernel — it
  means the signal goes to every process of the same user, which on your
  machine is everything you own. The id is now checked before the call: 0 and
  1 are refused, along with the placeholder values that used to slip past a
  type check.
  ([#14](https://github.com/YongmaoLuo/Product-Development-Team/pull/14),
  [#21](https://github.com/YongmaoLuo/Product-Development-Team/pull/21))

- **A green test run is evidence about the code, not about where or in what
  order it happened to run.** Tests used to leave runtime state behind for
  whichever test came next, so an assertion could pass because of its
  position in the run; every test now starts from empty state, and each
  parallel suite executes its files in a shuffled order whose seed is logged,
  so the next run is reproducible. Those suites were also cut to a size that
  finishes inside the time budget of the machine running them. And the
  concurrency stress test can no longer report two tasks holding one file:
  its overlap window was measured slightly past the moment the lock was
  released, so the violation it printed was an artifact of the measurement
  rather than of the lock.
  ([#7](https://github.com/YongmaoLuo/Product-Development-Team/pull/7),
  [#24](https://github.com/YongmaoLuo/Product-Development-Team/pull/24),
  [#32](https://github.com/YongmaoLuo/Product-Development-Team/pull/32))

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

[0.1.2]: https://github.com/YongmaoLuo/Product-Development-Team/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/YongmaoLuo/Product-Development-Team/releases/tag/v0.1.0
