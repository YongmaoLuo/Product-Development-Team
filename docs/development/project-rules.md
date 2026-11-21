# Project Rules

Rules learned from real failures. Each one names the failure it prevents, so a
reader can judge the edge cases instead of following it blindly. The Chinese
mirror is `project-rules.zh.md`.

## Tests and product semantics

1. **Changing a contract means updating every test that asserts the old one.**
   When a change alters an observable contract — a phase name, an endpoint's
   answer, a synchronous call becoming a background one — grep the test tree for
   the old contract before merging and update each assertion. CI green on the
   changed paths is not evidence: the stale assertions live in files the change
   never touched.
   *Failure it prevents (2026-11-21):* the stop flow became asynchronous; one
   test in `tests/api/test_plan_lifecycle_equivalence.py` was updated and its
   neighbour was missed. The missed one asserted the phase had already reached
   `stopped` and failed four out of five local runs.

2. **A test that asserts the result of an asynchronous operation waits for the
   terminal condition.**
   Read the terminal state — phase landed, record cleared, file present —
   through a bounded poll (the file's own `_wait_for_phase`, 30 s), never
   immediately after the request returns. The request answers `stopping`; only
   the background layer knows when the plan has stopped.
   *Failure it prevents (2026-11-21):* `test_execution_status_stopping.py`
   asserted the stop record was cleared the instant the phase changed; the
   finalize clears it after the reap and the requeue, so the assertion lost the
   race under CI load.

3. **A test does not depend on module identity.**
   `server.py` ends with `sys.modules.setdefault("server", sys.modules[__name__])`,
   so whichever module loaded it first owns the name, and log records carry that
   module's `__name__`. Filter by `server.__name__`, never by the literal
   `"server"`.
   *Failure it prevents (2026-11-21):* `test_heartbeat_tick_isolation.py` went
   red whenever `test_partial_completion_path.py` shared its process, because
   the latter loads `server.py` under the alias
   `partial_completion_path_test_server`.

## CI sharding and ordering

4. **Shard membership is positional; the execution order is deliberately
   random.** Files are sliced with `NR % count == index`, then shuffled per run
   with the seed `run_id.run_attempt.shard` (`ci.yml`). Adding any test file
   moves every later file to a different shard, so the same test can gain a
   different neighbour on an unrelated pull request.

5. **The shuffle is a canary, not a bug.** A red caused by order dependence is
   a test-hygiene defect: fix the test, do not disable the shuffle. The run's
   `execution order seed` is printed, so any failure is reproducible.

6. **One file is not one process.** `-n 8` starts eight workers, but under the
   default `--dist load` every worker imports every collected file. Only
   per-file invocation isolates module-level side effects.

## Pull requests

7. **One pull request per piece of work.** Fixes, squashes and conflict
   resolutions go to the existing PR branch with `git push --force-with-lease`.
   Do not open a second pull request for the same change. `main` is protected
   and is never force-pushed.

8. **Every commit on the branch carries a green CI run.** An intermediate red
   commit is rebuilt (amend/squash) and force-pushed; it is never merged into
   history.

9. **Green means the merge gate is green.** Every PR job passes; skips are
   allowed, failures are not.

10. **Merging needs the owner's approval** unless the owner has explicitly said
    green is enough for that pull request.

11. **A merge is verified, not assumed.** After merging, `git pull --ff-only` in
    the main checkout and confirm `git diff --stat <old-main>..main` contains
    exactly the PR's files. A merge that silently removes main's commits is the
    failure mode this check exists for.

12. **PR metadata carries no plan ids, internal codenames or AI attribution.**

## Verification flow

13. **GitHub Actions is the verification environment.** Local runs are
    targeted; the full gate runs on push. A public-repo pull request does not
    need the local CI container.
