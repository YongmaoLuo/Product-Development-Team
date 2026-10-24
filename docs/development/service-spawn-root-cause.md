# Service spawn root cause

This page is the English version of the account.  The Chinese version is
`service-spawn-root-cause.zh.md`; the two carry the same content, and the
Chinese one is the contract the follow-up work reads (`## 候选失败率`,
`## 根因`, `## 修复形状`).

## Phenomenon

Starting the backend service from a running Python process with the
production parameters fails: the service exits before it is ready, and the
starter is told the service did not come up. The backend's own log holds
the exception:

```text
OSError: [Errno 9] Bad file descriptor
```

It comes from the asyncio event loop's selector: the loop waits on
`select()` for a kqueue descriptor that is no longer valid. The host is
macOS, where asyncio uses kqueue — in other words, the event loop is
waiting on a descriptor that no longer exists.

## Baseline

On the unfixed code the failure rate is the maximum. **These numbers were
measured under the old spawn shape**: production started the service with
`/bin/bash -lc <cmd>` directly, with no wrapper.

| Metric | Value |
|---|---|
| Target | pdt |
| Profile | raw |
| Spawns | 10 |
| Ready | 0 |
| Failed | 10 |
| Failure rate | 1.000 |

The reproducer command (relative paths, so it does not depend on where the
checkout lives):

```bash
cd backend || exit 1
.venv/bin/python3 tests/manual/spawn_repro.py --profile raw --target pdt --runs 10 --ready-timeout 8 --assert-failure-rate 0.5 --json-out .pdt/spawn-repro/baseline.json
```

The command exits 0: the failure-rate assertion (≥ 0.5) holds.

After the fix production moved to the wrapper shape (see `## Fix shape`),
and the reproducer moved with it; the same command measured under the new
shape is in `## Re-measurement after the fix`.

## Candidate failure rate

Every candidate is measured in two cells — the parameter at its production
value (off) and at its alternative (on) — with 5 spawns per cell. **The
table below was measured on the unfixed code, under the old spawn shape** —
the root cause is read out of it: it asks whether the failure follows the
candidate while the defect is still there.

| Candidate | On | Off | Verdict |
|---|---|---|---|
| start_new_session | 5/5 | 5/5 | not implicated |
| stdin | 5/5 | 5/5 | not implicated |
| parent_threads | 5/5 | 5/5 | not implicated |

```bash
cd backend || exit 1
.venv/bin/python3 tests/manual/spawn_repro.py --matrix --target pdt --runs-per-cell 5 --ready-timeout 8 --assert-single-cause --json-out .pdt/spawn-repro/matrix.json
```

That command exits 1 and says that no candidate moved the failure rate, so
the root cause is not in the candidate set.

## Why the original three candidates cannot reproduce it

The original three candidates change *how the process is started*, not *what
the start hands over*.

- `start_new_session`: whether the child gets a session of its own. The
  failure does not depend on it — 5/5 failures with it on and 5/5 with it
  off.
- `stdin`: whether the child's input is devnull or inherited from the
  parent. 5/5 failures either way; the input is not the variable.
- `parent_threads`: whether the parent carries 0 or 8 busy threads. 5/5
  failures either way; the parent's thread count is not the variable.

This is not "occasional": all 10 baseline spawns failed, all 30 matrix
spawns failed, and the failure is deterministic — every run lands on the
same exception. The original three candidates cannot reproduce it not
because they were measured carelessly, but because the root cause is not on
the layer they touch.

## Supplementary candidates

Asking the question as *what does the start hand over* — the environment,
the inherited descriptors, the working directory — adds three more
candidates. **This table was measured on the unfixed code as well, under
the old spawn shape.**

| Candidate | On | Off | Verdict |
|---|---|---|---|
| clear_env | 0/5 | 5/5 | causal |
| close_fds | 5/5 | 5/5 | not implicated |
| cwd | 5/5 | 5/5 | not implicated |

```bash
cd backend || exit 1
.venv/bin/python3 tests/manual/spawn_repro.py --matrix --candidates clear_env,close_fds,cwd --target pdt --runs-per-cell 5 --ready-timeout 8 --assert-single-cause --json-out .pdt/spawn-repro/matrix-extra.json
```

That command exits 0: exactly one candidate moved the failure rate. Only
`clear_env` moved it — a child given a clean environment fails 0/5, a child
given the parent's environment fails 5/5. The other two do not move the
failure rate: the inherited descriptors (`close_fds`) and the working
directory (`cwd`) are not the variable.

The three supplementary candidates measured again under the new shape are
in `## Re-measurement after the fix`.

## Root cause

The parent's environment holds variables of the `PDT_SECRET_FD_*` shape, and
each one names a descriptor. That number only means something **in the
process that published it**. The child inherits the variable, but the number
points at something else in the child: in the parent it is one end of a
pipe, and in the child the number has already been taken by something else.

The backend reads those variables while starting up: `read_secret_fd()` in
`credentials.py` reads from the number in the variable and **closes that
number**, whether or not it read anything. The child's own startup gives
that number to asyncio's selector first. So the startup closes the
descriptor that the event loop's selector is holding, the loop's next
`select()` returns `Bad file descriptor`, and the service exits before it
is ready.

On the starter's side, `clear_env` is the only candidate that moves the
failure rate (0/5 against 5/5): replace the parent's environment and the
child no longer sees the variable, so it cannot close a descriptor that is
not its own.

The backend's own stack points at the line that closes the descriptor:

```text
server.py              feishu_notifier.start()
  feishu_notifier.py     self._client = FeishuClient()
    feishu_client.py       read_secret("feishu_app_secret")
      credentials.py         _resolve_from_inherited_fd(...)
        credentials.py           read_secret_fd(fd)
          credentials.py             os.close(fd)   <-- closes the event loop's selector
```

The negative control: with the two `PDT_SECRET_FD_*` variables removed from
the child's environment, the same start command no longer fails — the
backend comes up and serves. With the variables present, it reproduces.

## Fix shape

- Mechanism: wrapper

What needs fixing is not the shape of the start — the session, the input,
fork versus posix_spawn are all ruled out by the numbers above — but the
environment the start hands over. The service is started through a wrapper:
the wrapper strips the `PDT_SECRET_FD_*` variables — the ones that name a
descriptor — from the environment and then execs the real service.

Three constraints:

- The wrapper must not damage the launcher's own handoff. The launcher
  execs the service directly, where the descriptor numbers are valid; that
  path does not go through the wrapper, so it is not in the blast radius of
  this change.
- The wrapper changes none of the service's start parameters: the session,
  the input, the port and the working directory all keep their production
  values, or the next measurement could not tell a fix from a workaround.
- The bar does not move: the failure rate on the unfixed code is ≥ 0.5, and
  after the fix the same command's failure rate is 0.

## Re-measurement after the fix

Once the fix landed, production's spawn shape became the wrapper: it takes
the `PDT_SECRET_FD_*` variables out of the environment, calls `setsid()`,
and execs the real command. The reproducer moved with it — the argv comes
straight from `service_manager._detached_argv` rather than from a copy of
it — so the reproducer measures the shape production really uses now, not
a shape that no longer exists.

**Baseline** (the same command, a different shape):

| Metric | Value |
|---|---|
| Target | pdt |
| Profile | raw |
| Spawns | 10 |
| Ready | 10 |
| Failed | 0 |
| Failure rate | 0.000 |

```bash
cd backend || exit 1
.venv/bin/python3 tests/manual/spawn_repro.py --profile raw --target pdt --runs 10 --ready-timeout 8 --assert-failure-rate 0 --json-out .pdt/spawn-repro/baseline.json
```

The command exits 0: the failure-rate assertion (= 0) holds — all ten
spawns came up. This is the other half of the acceptance criterion: on the
unfixed code the failure rate is ≥ 0.5, and after the fix the same command
has a failure rate of 0.

**The three-candidate matrix** (5 spawns per cell):

| Candidate | On | Off | Verdict |
|---|---|---|---|
| start_new_session | 0/5 | 0/5 | not implicated |
| stdin | 0/5 | 0/5 | not implicated |
| parent_threads | 0/5 | 0/5 | not implicated |

```bash
cd backend || exit 1
.venv/bin/python3 tests/manual/spawn_repro.py --matrix --target pdt --runs-per-cell 5 --ready-timeout 8 --json-out .pdt/spawn-repro/matrix.json
```

The command exits 0: no candidate moved the failure rate — because the
failure rate is 0 to begin with. Under the old shape these three rows read
5/5 against 5/5, and they were measured while the defect was still there,
which is a different question.

**The supplementary candidates**:

| Candidate | On | Off | Verdict |
|---|---|---|---|
| clear_env | 0/5 | 0/5 | not implicated |
| close_fds | 0/5 | 0/5 | not implicated |
| cwd | 0/5 | 0/5 | not implicated |

```bash
cd backend || exit 1
.venv/bin/python3 tests/manual/spawn_repro.py --matrix --candidates clear_env,close_fds,cwd --target pdt --runs-per-cell 5 --ready-timeout 8 --json-out .pdt/spawn-repro/matrix-extra.json
```

The command exits 0. `clear_env` was causal under the old shape (0/5
against 5/5) and does not move the failure rate under the new one (0/5
against 0/5): the wrapper already takes those variables out at the
handoff, so the whole environment does not have to be swapped. That is
what the fix is for — turning "swap in a clean environment" into "do not
hand the child that number at all".

## Negative control

The reproduction test is green on the fixed code, but a green reproduction
proves very little on its own: the same test is green on a machine where the
defect never appears at all, and the output does not tell the two apart. The
negative control removes that difference — take the fix back out once, and the
same test has to go red; then put the fix back.

The fix is taken out through a patch file, not through `git checkout --` and
not through `git stash`: both of those would take the rest of the batch's
changes with them. The patch is taken from the task-4 change
(`[task-4] 修复 spawn 形状：wrapper 剥掉描述符变量后 exec 服务`):

- Fix patch: `git -C .. show 27d7237 -- backend/service_manager.py > /tmp/service-spawn/fix.patch`
- Revert: `git -C .. apply -R /tmp/service-spawn/fix.patch`
- After the revert: `test_three_consecutive_spawns_all_ready` FAILED — `assert failed == 0` (`failed=3`)
- Restore: `git -C .. apply /tmp/service-spawn/fix.patch`
- After the restore: `git -C .. diff --stat` prints nothing (the working tree is back at HEAD)
- After the restore: 3 passed

The whole command sequence (relative paths, so it does not depend on where the
checkout lives):

```bash
cd backend || exit 1
PY=.venv/bin/python3
git -C .. show 27d7237 -- backend/service_manager.py > /tmp/service-spawn/fix.patch
git -C .. apply -R /tmp/service-spawn/fix.patch
$PY -m pytest tests/integration/test_service_spawn_repro.py -v
git -C .. apply /tmp/service-spawn/fix.patch
$PY -m pytest tests/integration/test_service_spawn_repro.py -v
git -C .. diff --stat
```

The red output of the run after the revert (the first line names the test
case, and the broken assertion follows it):

```text
tests/integration/test_service_spawn_repro.py::test_three_consecutive_spawns_all_ready FAILED
tests/integration/test_service_spawn_repro.py:368: in test_three_consecutive_spawns_all_ready
    assert failed == 0, (
E   AssertionError: batch 1 of 3: failed=3 of 3 spawns
```

`failed=3` is not zero: none of the three spawns of that batch came up. After
the restore the same command is 3 passed, and `git diff --stat` prints nothing
— the fix is back in and the working tree is back at HEAD.

## The second line of defense: not one bit of a descriptor you do not own

The wrapper fixes the **handoff**: the service sees no `PDT_SECRET_FD_*`, so no
descriptor is ever mistaken for a pipe this module published. But the handoff
can still be lost — between the parent publishing the pipe and the child
exec'ing, the pipe may not travel with it, while the **number** is still in the
environment. In the child that number names something the child owns, most
often asyncio's own selector. So the reader needs a second line of defense: a
descriptor it did not publish is not read, not closed, and not have its flags
changed.

That defense used to leak on the flags. `_drain_published_pipe` switches the
pipe to non-blocking mode with `os.set_blocking(fd, False)`; on macOS that fd
is often the kqueue descriptor — `fstat` says it is a FIFO (so "it is a pipe",
the necessary condition, passes) while `fcntl` on it answers ENOTTY, so
`os.set_blocking` raises.

**Raising is not proof that nothing changed.** The kernel applies F_SETFL and
only then reports ENOTTY, so a descriptor belonging to somebody else is left
permanently in non-blocking mode. Measured on macOS:

```text
fresh kqueue fd          F_GETFL=2   O_NONBLOCK=False
os.set_blocking(fd, False) → OSError: [Errno 25] Inappropriate ioctl for device
after                     F_GETFL=6   O_NONBLOCK=True    ← somebody else's fd, changed
os.set_blocking(fd, True)  → OSError: [Errno 25] Inappropriate ioctl for device
after                     F_GETFL=2   O_NONBLOCK=False   ← the restore still works
```

How it is isolated: the read-only snapshot and the handling of a candidate
descriptor are separated. `os.get_blocking(fd)` goes through F_GETFL, only
reads, and a failure there returns None straight away — that step really did
touch nothing. But **once** `os.set_blocking(fd, False)` has been attempted
(whether it succeeded or raised), **every** exit has to go through the restore,
so the `finally` that restores is lifted from "wrapping the read loop only" to
"covering that call". The restore raises ENOTTY on a kqueue as well and still
clears O_NONBLOCK — so the restore is possible, and the gap was in the code,
never in the kernel.

The criterion (VP-019): when the determination does not hold, return None /
raise ValueError, and do not read the fd, do not close it, and do not change
its flags. Pinned in `backend/tests/unit/test_credentials_fd_payload.py`:
`test_drain_leaves_a_foreign_descriptor_flags_untouched`,
`test_reader_leaves_a_foreign_descriptor_flags_untouched` and
`test_reader_restores_the_flags_of_a_pipe_it_did_not_publish`.

## Acceptance: one command re-runs every clause of VP-019

The HEAD this section was written against:
`63b96593d0bcb2a68b973d1f5faa4e41d08e7fb7` — the commit that brought this section
and `backend/tests/unit/test_credentials_fd_protocol.py` into the tree. The
criterion, the clause table and the negative-control transcript have not changed
since that commit; everything this section has gained since is explanatory prose.

The criterion (VP-019), which does not change:

> only when the fd is a FIFO (decided by fstat) and the non-blocking read
> reaches EOF because the write end is closed does it return the payload,
> closing the fd before parsing it; when the determination does not hold it
> returns None / raises ValueError, and neither reads nor closes the fd and
> does not change its flags; the comment says that on macOS a kqueue
> descriptor is a FIFO to fstat as well (necessary, not sufficient), and
> points at docs/development/service-spawn-root-cause.md; the PDT_SECRET_FD_*
> variable names are not removed; the review has to give file:line references

The criterion used to live in the prose of a review, and prose is not
something you can re-run: as soon as HEAD moved, the previous round had
nothing to run and fell back to an earlier execution. This section makes it
an artifact of the code instead — one command, one run, and the three sides
of **clause ↔ test ↔ source location** line up, so the protocol no longer has
to be pieced together out of scattered sentences.

### The acceptance command

```bash
cd backend && .venv/bin/python -m pytest tests/unit/test_credentials_fd_protocol.py -v
```

pytest and nothing else. **No** inline `python3 -c` probe: the previous task's
own pre-flight command (`pytest ... && python3 -c '...'`) failed before the
executor touched anything, and the task was still called completed. A
criterion that has to be re-runnable cannot hang off a one-off command that
depends on the working directory.

### Clause ↔ test ↔ source

| Clause | Test | Symbol | Source |
| --- | --- | --- | --- |
| C1 | `test_clause_c1_fifo_is_decided_by_fstat` | `_is_pipe` | `backend/credentials.py:711` |
| C2 | `test_clause_c2_fifo_alone_is_not_sufficient` | `_is_pipe` | `backend/credentials.py:707` |
| C3 | `test_clause_c3_payload_is_returned_only_after_eof` | `_drain_published_pipe` | `backend/credentials.py:717` |
| C4 | `test_clause_c4_fd_is_closed_before_the_payload_is_parsed` | `read_secret_fd` | `backend/credentials.py:1261` |
| C5 | `test_clause_c5_unpublished_descriptor_raises_value_error` | `read_secret_fd` | `backend/credentials.py:1261` |
| C6 | `test_clause_c6_unpublished_descriptor_is_not_read` | `_drain_published_pipe` | `backend/credentials.py:717` |
| C7 | `test_clause_c7_unpublished_descriptor_is_not_closed` | `read_secret_fd` | `backend/credentials.py:1261` |
| C8 | `test_clause_c8_flags_of_a_live_writer_pipe_are_unchanged` | `_drain_published_pipe` | `backend/credentials.py:789` |
| C9 | `test_clause_c9_flags_of_a_regular_file_are_unchanged` | `_is_pipe` | `backend/credentials.py:695` |
| C10 | `test_clause_c10_flags_of_a_kqueue_descriptor_are_unchanged` | `_drain_published_pipe` | `backend/credentials.py:789` |
| C11 | `test_clause_c11_the_pre_fix_shape_changed_those_flags` | `_drain_published_pipe` | `backend/credentials.py:789` |
| C12 | `test_clause_c12_kqueue_is_a_fifo_to_fstat_and_the_comment_says_so` | `_is_pipe` | `backend/credentials.py:711` |
| C13 | `test_clause_c13_the_source_points_at_the_root_cause_document` | `_drain_published_pipe` | `backend/credentials.py:717` |
| C14 | `test_clause_c14_a_failed_f_setfl_still_takes_effect_and_the_source_says_so` | `_drain_published_pipe` | `backend/credentials.py:751` |
| C15 | `test_clause_c15_pdt_secret_fd_variable_names_are_not_removed` | `secret_fd_env_var` | `backend/credentials.py:1145` |
| C16 | `test_clause_c16_drain_never_touches_descriptor_flags` | `_drain_published_pipe` | `backend/credentials.py:789` |
| C17 | `test_clause_c17_the_guard_rejects_the_shape_it_exists_to_reject` | `_drain_published_pipe` | `backend/credentials.py:789` |
| C18 | `test_clause_c18_the_acceptance_table_matches_this_module` | `test_clause_c18_the_acceptance_table_matches_this_module` | `backend/tests/unit/test_credentials_fd_protocol.py:1133` |
| C19 | `test_clause_c19_drain_never_attempts_to_change_the_flags` | `_drain_published_pipe` | `backend/credentials.py:789` |
| C20 | `test_clause_c20_no_read_can_block` | `_drain_published_pipe` | `backend/credentials.py:802` |

What the clauses are, against the wording of the criterion:

- **C1** "only when the fd is a FIFO (decided by fstat)" — the kind is decided
  by `os.fstat` and `stat.S_ISFIFO`.
- **C2** "**and**" — a FIFO is only necessary: a pipe whose writer is still
  attached is refused as well.
- **C3** "the non-blocking read reaches EOF because the write end is closed"
  — the switch to non-blocking mode comes before the read.
- **C4** "closing the fd before parsing it" — the close happens before the
  parse, and happens even when the parse fails.
- **C5** "return None / raise ValueError" — None from the inside, ValueError
  from the outside.
- **C6** "neither reads" — see "A known boundary" below.
- **C7** "nor closes" — the descriptor is still there.
- **C8 / C9 / C10** "does not change its flags" — a pipe with a live writer, a
  regular file, and a kqueue descriptor.
- **C11** the negative control: the shape from before the fix really did
  change the flags (`2 -> 6`), so this clause is not empty talk.
- **C12** "the comment says that on macOS a kqueue descriptor is a FIFO to
  fstat as well (necessary, not sufficient)".
- **C13** "and points at docs/development/service-spawn-root-cause.md" — and
  the document it points at has to exist.
- **C14** "a failed F_SETFL still takes effect" — that fact is written where
  the restore is.
- **C15** "the PDT_SECRET_FD_* variable names are not removed".
- **C16 / C17** the structural guard: every `os.set_blocking(fd, False)` has
  to sit inside a `try` whose `finally` restores the flags. A behaviour test
  cannot tell that somebody narrowed the `finally` back to the read loop
  only; an AST guard can.
- **C18** "the review has to give file:line references" — the table above is
  what the test guards.

The table itself is guarded by
`test_clause_c18_the_acceptance_table_matches_this_module`: a row too many,
a row too few, a symbol that does not exist, a line number outside the
function, or the two language versions of this document disagreeing —
all of them are red.

### Negative control

Put the `backend/credentials.py` of `49f93be~1` back in place, and C10,
C11, C14 and C16 have to go red:

```bash
TMP=$(mktemp -d)
git show 49f93be~1:backend/credentials.py > "$TMP/credentials.py"
cat > "$TMP/pre_fix_plugin.py" <<'PY'
import importlib.util, os, sys
_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.py")
_spec = importlib.util.spec_from_file_location("credentials", _path)
_module = importlib.util.module_from_spec(_spec)
sys.modules["credentials"] = _module
_spec.loader.exec_module(_module)
PY
cd backend && PYTHONPATH="$TMP" .venv/bin/python -m pytest \
    tests/unit/test_credentials_fd_protocol.py -v -p pre_fix_plugin
```

Transcript (HEAD = the commit this section was written against; abridged — pytest's own
`assert` expansion lines and the closing summary line are left out, the summary line
carries the counts of one run, which is a record rather than a criterion):

```text
FAILED tests/unit/test_credentials_fd_protocol.py::test_clause_c10_flags_of_a_kqueue_descriptor_are_unchanged
E   AssertionError: the reader changed the flags of a descriptor it does not own: 2 -> 6
FAILED tests/unit/test_credentials_fd_protocol.py::test_clause_c11_the_pre_fix_shape_changed_those_flags
E   AssertionError: the shape in the tree changes the flags of a descriptor it does not own
FAILED tests/unit/test_credentials_fd_protocol.py::test_clause_c14_a_failed_f_setfl_still_takes_effect_and_the_source_says_so
E   AssertionError: the source no longer says that a failed F_SETFL still takes effect
FAILED tests/unit/test_credentials_fd_protocol.py::test_clause_c16_every_nonblocking_switch_is_covered_by_a_restore
E   AssertionError: the non-blocking switch is not covered by a restoring finally:
E     line 39 of the function: os.set_blocking(..., False) is not covered by a finally that restores the flags
```

That run left exactly those four red; every other clause was green.

The C16 assertion is the structural one, and it is why the guard exists: in
the pre-fix shape `os.set_blocking(fd, False)` sat in the same `try` — one
with **no** `finally` — as the read-only snapshot, and the restore was in a
`finally` that covered the read loop only. The two shapes behave identically
for every descriptor whose `fcntl` works; only a descriptor whose `fcntl`
fails, such as a kqueue descriptor, tells them apart.

### A known boundary

The criterion says "neither reads nor closes the fd". That holds for a
descriptor ruled out by the FIFO test (a regular file: not one byte of the
file offset moved) and for an *empty* pipe with a live writer (the read comes
back EAGAIN, nothing is consumed, and the owner of the pipe can still write
and read it). It does not hold for a foreign pipe that *already carries
data*: "is the writer still attached" is only answerable by reading, and
bytes read out of a pipe cannot be put back. So such a pipe is read once and
then refused (None / ValueError), and the data is consumed.

That is a property of pipes rather than a choice this module made, so it is
written down here instead of being left for the next reader of the criterion
to trip over. The production failure took the other road: a kqueue
descriptor is a FIFO to `fstat` and useless to `fcntl`, so the reader backed
out before it had read anything at all.
