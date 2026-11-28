# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

[中文版](CHANGELOG.zh.md)

## [Unreleased]

**The keychain read left the process that serves requests.** The two
notification secrets are now resolved once at startup by a launcher that
publishes them onto anonymous pipes and then `exec`s the server, so the
process answering HTTP never runs the keychain tool at all. Beside it, an
opt-in reader that narrows an item's access control list to a binary you
signed yourself — so `/usr/bin/security`, which every process on the
machine may execute, stops being what grants the read.

### Added

- **A pre-generated verification plan coexists with the task list, and is
  consumed as written.** A plan directory that carried
  `verification_plan.json` beside `tasks.json` lost one of the two along
  the way. `backend/plan_artifacts.py` now keeps both artifacts, the plan
  routes serve both, and the verification round consumes the
  pre-generated plan instead of generating its own.

- **The scheduler panel has a 「收尾」 section of its own.** A plan that is
  being finished holds no slot and waits for no capacity, so it was in
  neither the queue nor the running list, and an operator could not see
  that a plan had reached the finishing stage at all. `GET
  /api/scheduler/finishing` (`backend/routes/scheduler.py`) names those
  plans — the phase they are in, 收尾中 or 收尾失败 — and the questions
  they wait on, with the same 延长 / 终止 / 回答 operations the waiting
  table offers. The panel draws it in `frontend/index.html` and
  `frontend/app.js`.

- **The scheduler view has an address of its own.** Clicking the panel
  writes `#/scheduler` into the URL (`frontend/app.js`), so a refresh
  comes back to the panel instead of losing it; a `hashchange` to
  anything else goes back to the plan list.

- **`backend.secret_launcher` reads the keychain once, then becomes the
  server.** It resolves each notification secret, writes the values onto
  anonymous pipes, names the descriptors in the environment, and `exec`s
  `backend.server` — the same PID, so a supervisor's view of the service
  does not change. The server then reads the descriptors it inherited, so
  `credentials.read_secret` reports `"inherited_fd"` and has no keychain
  branch left to take. A deployment that leaves the keychain switched off
  is unaffected and still starts with `python -m backend.server`. See
  [Keychain migration](docs/operations/keychain-migration.md).

- **`tools/pdt-secret-reader/` — a reader you sign, and the rewrite that
  trusts it.** An item filed by `security` names `/usr/bin/security` as
  its trusted application, and that is an Apple-signed binary every
  process on the machine may execute; the default list therefore hands the
  secret to anything that can run one command, and does it silently.
  `setup.sh build` issues a self-signed certificate and a signed reader,
  `adopt --widen` adds that reader to each item's list while keeping what
  is already trusted there, and `adopt --narrow` rewrites those lists with
  the reader alone. Reads go through `PDT_SECRET_READER_PATH` when it is
  set, with the same argv `security` was given, so nothing else about the
  read changes. The procedure, its exit codes, and what it does **not**
  protect against are in its README.

### Changed

- **`secrets verify` now asks the machine, not the process.** The command
  reads the keychain directly, because that is the question an operator
  can act on: a server started without the launcher would report `missing`
  for everything and explain nothing. It still prints `source=keychain`,
  which now means "this machine's keychain holds it" rather than "the
  server will resolve it".

### 判定语义变更

*Judgment semantics changed. The heading is spelled the same in both
languages on purpose: the CI gate reads one string out of both
changelogs, and a section that exists in only one of them is a section
half the readers never see.*

- **A round's close judges the deferred gates by the round's own final
  conclusions.** `backend/verification_executor.py` takes the round's
  execution envelope from `backend/verification_agent.py` at the close
  and reads the current round's ledger before the cross-round verdict
  table, so a stale `BLOCKED` left by an earlier round no longer keeps
  a deferred full gate from being dispatched; when neither ledger holds
  a terminal success the previous behaviour is kept word for word.

- **A deferred full gate runs at the round's close once its blocker is
  green.** `backend/verification_executor.py` re-reads the round's own
  ledger before the close rerun, so a gate layer the process-local table
  forgot (a rebuilt executor, a resumed round) is still dispatched; the
  ledger's reason is kept when the executor cannot see the blocking
  verdict. A gate that was never asked is no longer reported as a skip.

- **A gate that could not answer no longer cites the previous round's
  junit.** `backend/verification_ci_runner.py` decides freshness from the
  report's own timestamp and fails the gate INCONCLUSIVE when this round
  wrote none, instead of attaching a stale report from an earlier round as
  its evidence; stale reports are cleared first, and a cleanup failure is
  recorded rather than swallowed. `backend/verification_plan_delta.py`
  recognises the script names inside a command (including Chinese
  punctuation and absolute paths), so a command that only moved to another
  script is no longer read as "no change". **No verdict changed**: a gate
  that really passed still passes, and one that really failed still fails.

- **A paused plan is only stopped when its processes really are.** The
  repair-round pause in `backend/verification_loop.py` reads the plan's
  process ledger after the stop and reports `pause_kill_incomplete` when
  anything of the plan is still alive, instead of treating the executor's
  exit as proof that the work stopped — a plan never says "stopped" while
  part of it still runs. **No verdict changed**: what a round reports
  about its verification points is unchanged.

- **A red CI gate names the lane that failed.** `scripts/ci_gha.sh`
  prints the failing job(s) of the run before it emits the FAILED
  verdict — the job list is already in hand — so a red whole-repo gate
  carries locatable evidence instead of an exit code alone. **No verdict
  changed**: the gate's pass/fail decision is the same.

- **A terminal failure announces its own review once.** When execution or
  verification settles on a terminal failure, `backend/verification_loop.py`
  announces a failure review after the terminal phase is written — once per
  plan and round, and a failed announcement never raises. The decision it
  creates has no deadline and is not settled by the timeout window. **No
  verdict changed**: the status the round reports is the one it always
  reported.

- **A round reclaims the services it declared from its own ledger.**
  `backend/verification_agent.py` hands the round's plan directory and
  service ledger to the declared-service reclamation, so a listener left
  behind by an earlier round is recognised as the service it is instead of
  turning the whole round into environment-blocked. **No verdict changed**:
  a service that really is foreign is still refused.

- **The scheduler card tells the finishing stage apart from a running
  verification.** While the finishing stage ran, the card said 「验证中」
  the whole time; a plan parked at a failed finishing stage stopped
  pushing entirely, so the card froze on its last pre-failure content and
  nobody was told the plan was waiting for a human. The card now has a
  label and a header for each of the two phases (「收尾中」 /
  「收尾失败（待决定是否续跑）」), and each transition publishes a state
  event so the card is refreshed when it happens. **No verdict changed**:
  verification still reports whatever it reported, `verification_status`
  still reads `running` throughout the stage, and the plan keeps its
  scheduler slot.

- **`_ran_the_declared_entry` command exemption (`88d898f9`).** A
  command-kind verification point asks one question — does *this* command
  exit 0 — and a verdict that records that same command together with the
  exit code the framework read has answered it by construction. The file
  names in its output are the log of what ran, not the subject of the
  question, so `verdict_subject_mismatch` stopped comparing them
  (`backend/verification_evidence.py`). Before this change, a command
  point whose declared entry had just exited 0 was downgraded to FAILED
  for "answering another question" — a false red produced by the
  framework's own bookkeeping. The exemption landed inside an unrelated
  pull request and was recorded nowhere; this entry is the backfill.

- **`scripts/ci_judgment_surface.sh` is the one entry point for the three
  judgment-surface checks, and `.github/workflows/ci.yml` runs it as a
  gate.** The judgment surface is described by three artifacts that are
  allowed to drift apart: the manifest of the functions that decide
  PASS/FAIL, the decision tables that manifest names, and the behavioural
  goldens frozen from what those functions return today. Each of the three
  had its own checker, and three separate entry points are three ways for
  the chain to rot — one link can stop running while the other two keep
  reporting green, and the reader of a CI log has to know which of the three
  to believe. The script now runs all three in order and answers with one
  number: the census check is advisory, because a manifest that no longer
  matches the code warns; the static gate that says the named tables are
  really there and the behavioural goldens are the ones that block. The
  workflow runs the script as its own `judgment-surface-consistency` job,
  and the merge gate lists that job among the ones it waits for — so the
  three checks are no longer a step inside a lane whose name says nothing
  about the judgment surface.

- **`backend/verification_loop.py` gains a bounded resume entry for the
  finishing stage.** A plan parked in `postprocess_failed` used to have no
  way out: the phase existed, but nothing could trigger it. A human can now
  call the resume endpoint, which continues only the items the progress file
  records as unfinished or failed — an item whose outward action already
  succeeded is not performed again. Nothing resumes on its own, and the
  number of resumes is capped by a fixed retry budget
  (`backend/postprocess/retry_budget.py`): a resume repeats an outward
  action (open a PR, push), and an unbounded loop of those is exactly what
  this human-gated stage exists to prevent.

- **`.github/workflows/ci.yml` now caps a pull request at five commits.**
  The cap turns "squash before opening" into a check instead of a habit:
  past five commits the branch's history stops reading as one decision,
  and the per-commit CI record that makes a merge auditable is the first
  thing to go. The job reads `pull_request.commits` — the number the pull
  request itself shows — and joins the merge gate's `needs`.

- **A failure point blocks only under the dual condition.** `decide()` in
  `backend/framework_fix/classifier.py` is the whole of the new rule: a
  failure blocks when it is acceptance-critical **and** either the failure
  was reproduced as a framework defect (`framework_deterministic`,
  `framework_timeout`) or the change went out of scope. Both halves have
  to hold — the first one alone, or the second one alone, is not a
  framework defect. A failure that is not acceptance-critical never
  blocks, whatever else is true of it. A reproduction that came back
  `flake` or `not_reproducible` does not block either, and it outranks
  the out-of-scope half: a run that has just been shown to be flaky is
  not held on the strength of a change that also wandered. The
  out-of-scope half stands on its own — a change that reached outside the
  plan's scope blocks without anyone having to reproduce anything first.
  A failure that was reproduced but cannot be classified is `unclassified`
  and is registered as evidence; two such rounds in a row are `exhausted`
  and go to a human; a failure that has not been reproduced yet is
  `needs_repro` and does not block yet. What comes out is a
  `FailureDecision`, whose `blocking` flag is true exactly when the
  disposition is `blocking` — and never `not_blocking`, which is what a
  non-critical failure and a flaky reproduction both produce.

- **A plan now has a kind, and the executor reads it in exactly one
  place.** `plan_kind` (`backend/plan_state.py`) says what a plan is:
  `normal` for an ordinary business plan, `framework_fix` for a plan that
  exists to repair the framework itself. The executor (`backend/agent.py`)
  carries exactly one judgment seam, `plan_start_decision()`: a
  `framework_fix` plan skips the wait for a scheduled batch and starts its
  current task directly, because the point of such a plan is to reach that
  task now. Nothing else about execution changed — no second execution
  path, no new scheduling mechanism, no second judgment. A plan that says
  nothing about its kind is treated as `normal` and behaves exactly as
  before, and a plan whose kind cannot be read raises instead of being
  guessed at: guessing `normal` would silently change what that plan is
  doing, and the plan itself would never know.

- **Verification results are appended per tree, and the status endpoint
  now says which tree a verdict came from.** A result row is written once
  and never updated: the same verification point run again on another tree
  appends another row to the append-only table `plan_verification_result`
  (`backend/state_machine/db/schema.py`), so what was tried on which code
  is kept rather than overwritten — a failure that overwrote its own
  earlier row would leave the ledger one row short. Reading those rows
  back is `read_vp_results()` (`backend/verification_persistence.py`),
  and the status response (`backend/routes/verification.py`) now carries
  `vp_results`: one row per verification point, the row from the most
  recent tree, with that point's `tree_fingerprint` and the number of
  rows it has accumulated as `history_count`. No conclusions yet is an
  empty list rather than a missing field, because "no verdict yet" is
  itself an answer. A plan held for a framework defect may not start or
  reset a verification round — that round is waiting for the framework to
  be repaired, not for another attempt — and the refusal is `409` with
  `blocked_on_framework_fix` and the fingerprint of the defect it is held
  on. A guard that cannot read the hold state refuses with `500` instead
  of letting the round through: a guard that reads "could not tell" as
  "not held" is not a guard.

- **The base-drift check now names its base per plan
  (`backend/verification_agent.py`).** The base a branch is measured
  against is a property of the **repository**, not of the deployment:
  the remote may be called `origin` or `github`, the default branch may
  be `main` or `master`. One environment variable cannot carry that,
  and the default `origin/main` did not resolve in every repository —
  where it did not, the check reported a failure every round and
  measured no drift at all, which reads in the log exactly like a
  branch that was never behind anything.

  Each plan now states its own base in
  `plans/<plan_id>/base_drift.json`, and the environment stays the
  fallback for every plan that says nothing. The layering is plan file
  over environment over default, so **a plan without that file behaves
  exactly as it did before** — same base, same mode, same result. The
  file never raises: a missing file, a broken one and a key of the
  wrong type each fall back to the layer underneath, key by key,
  because a typo in the configuration must not disable the check nor
  break the round that reads it. The alternative — guessing a list of
  candidate remote names — is what this replaces: it would have
  measured the wrong thing, or nothing, while looking as though it had
  worked.

- **A coding tool now has to be told which checkout it is about, and the
  failure fingerprint is decided by the payload.** `ClaudeCodingTool`
  (`backend/coding_tool.py`) takes its working root as a required
  keyword-only argument and refuses to be built without one — an unnamed
  root would run the child in the backend server's own directory, a
  different tree than the one the call is about. Every construction site
  names a root now: the verification orchestrator passes the plan's
  project directory into the tool it builds
  (`backend/verification_agent.py`), and the summarizer that decides a
  stuck attempt's verdict is handed the same root
  (`backend/verification_subagent.py`). The terminal-decision judge and
  the HITL port resolve that root instead of guessing one: the judge
  investigates in the tree it was told about and gets no tool at all
  when it cannot name one (`backend/verification/terminal_analysis.py`),
  while the port resolves the plan's registered project directory first
  and the plan's own directory second
  (`backend/hitl/terminal_decision_port.py`). The failure fingerprint
  follows the same one-authority-per-question rule: when the grader's own
  `failed_assertions` payload is present, the payload decides the
  fingerprint and the report's prose is not hashed around it, so one
  defect fingerprints the same way however the prose is reworded; the
  prose (`first_error_line`) takes part only when there is no payload to
  hash (`backend/verification/failure_fingerprint.py`).

- **A declared squash step may stage the product diff.**
  `backend/postprocess/executor.py` recognises the declared squash entry
  (`git reset --soft origin/main`, matched verbatim, and the entry's own
  `target` has to agree with it) and lets that one entry skip the
  porcelain criterion of the product-code guard: staging the branch diff
  is exactly what the entry is for, and the entry that follows it
  commits the result. **The content criterion is unchanged** —
  `reset --soft` does not touch file contents, so the sha256 comparison over
  the product-code paths still catches a real content change, and a
  rewrite during that entry is still red. **No verdict changed for any
  other entry**: a squash onto another base, a `git reset --mixed` or
  `--hard`, and any entry that writes product code are judged as
  before.

- **A Phase-2 gate layer that was deferred over a Phase-1 conclusion is
  re-decided against that conclusion's final state, and it runs in the same
  round once its blocker turns green.** `backend/verification_executor.py`
  keeps the deferred gate layer in the round's verdict ledger — the ledger
  is the truth about "this round a gate layer was deferred and has not run
  yet" — so a layer deferred while its blocker was still `BLOCKED` is found
  again even when the executor is rebuilt or the process is replaced, and
  the deferral reason is re-worded to name the blocker that blocks *now*:
  a reason never keeps naming a blocker that has already turned green.
  `backend/verification_agent.py` re-decides and dispatches those layers at
  the round's close — after the last thing that can still change a
  conclusion, and before the report is built — so a blocker that turns green
  later in the round still lets the gate layer run in that same round. A
  blocker that is still `FAILED` or `BLOCKED`, or has no conclusion at all,
  keeps the layer deferred exactly as before. **A verdict did change**: a
  round whose gate layer stayed deferred over a stale Phase-1 conclusion
  used to be reported as `SKIPPED`, and now carries the gate's own
  conclusion.

- **The whole-repo gate judges the current tree, not the branch
  history.** `scripts/ci_gha.sh` used to push HEAD itself to its
  throw-away probe branch, and pushing a ref runs the repository's
  pre-push hook, which scans every commit of the pushed ref that is not
  on a remote yet — so an old commit in the branch history that the
  current tree no longer contains could keep the gate from pushing its
  probe branch at all, and the gate went red without a lane ever
  having run. The probe branch now points at a single commit built from
  the tree of HEAD and parented on `origin/main`, so the hook scans the
  net change of the current tree against the base, and the branch
  history never enters the scan range. The history checks are not lost:
  the pre-push hook guards the real pushes of a branch, and ci.yml's
  `commit-range-privacy` job runs on pull requests — the gate dispatches
  `workflow_dispatch`, which never runs that job. **No verdict
  changed**: the gate still answers 0 / 1 / 3 exactly as before; what it
  pushes is what changed.

- **A verdict output that does not conform is no longer filed as a code
  failure.** A sub-agent answer that does not satisfy the verdict
  contract established nothing about the deliverable, so it is carried as
  `UNKNOWN` with the `infra` failure class instead of `FAILED`
  (`backend/verification_subagent.py`); a report entry that came back
  failed without ever claiming a deliverable defect is restored to
  `UNKNOWN` instead of buying a repair task for something nobody verified
  (`backend/verification_agent.py`); and the "no conclusion" status has a
  bucket of its own rather than being folded into the failure counts
  (`backend/verification_buckets.py`). The repair batch's ending is
  decided by one classifier, and a death of the executor is recorded
  beside that verdict instead of being written over it — so "the code is
  still wrong" and "there is still work to run" no longer collapse into
  one another (`backend/verification_loop.py`). The convergence summary
  reads the same ledger and sorts every plan into `passed`,
  `user_stopped`, `env_blocked`, `infra_flake`, `code_defect` or
  `unclassified` (`backend/verification/convergence_metrics.py`,
  `backend/routes/verification.py`). **A verdict did change**: a round
  that produced no conclusion used to be reported as a failure of the
  deliverable, and it is now reported as the absence of one.

- **A verification point may not take a number before its definition is
  complete.** `backend/verification_plan_delta.py` checks a delta entry
  before its id is handed out: it needs a title and a supported verification
  method, and it needs an expected result for every method but the
  whole-repository gate — for that one the criterion is the exit code of
  its entry, so a missing description is not a missing criterion. The
  method's own payload gaps (a bare `json_path`, a `full_ci` without its
  `ci_entry`) are reported beside the missing fields, and the definitions
  are written back into `verification_plan.json`, so the next round has
  something to dispatch. An id whose definition never landed used to run
  never, while the failure it had earned was carried forward as if it were
  real. **No verdict changed**: a gate that really passed still passes.

- **A whole-repository gate counts only when this round produced it.**
  `backend/verification_executor.py` marks every verdict with
  `executed_this_round`; a Phase 2 full-gate verdict that was hydrated from
  the ledger instead of produced this round is superseded and reported
  `DEFERRED` — the verdict it replaced, and the commit that verdict judged,
  are kept as audit trail — instead of being carried into the report as this
  round's PASSED. **A verdict did change**: a gate conclusion an earlier
  round earned no longer stands in for this round's.

- **A conclusion says which commit it judged.**
  `backend/verification_agent.py` and `backend/verification_executor.py`
  stamp every verdict with `judged_head`, and
  `backend/verification_ci_runner.py` writes the CI gate's `head_sha` /
  `tree_sha` / `started_at` / `project_dir` into the artifact and its
  `judged_head` into the verdict. An artifact whose `head_sha` is missing,
  or no longer the current HEAD, is not reused: the gate runs again, and the
  result records which old conclusion was ignored. **No verdict changed**: a
  gate that really passed on this tree still passes.

- **The lines a pull request proposes are measured, not only the tree.**
  `.github/workflows/ci.yml` runs the changed-line coverage floor over the
  lines a pull request touches, beside the whole-tree floor, and fetches the
  base branch with full history — so that comparison is a measurement rather
  than an assumption.



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
