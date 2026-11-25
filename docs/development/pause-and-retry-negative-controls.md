# The two negative controls of the pause and the retry

These two negative controls pin down two fixes — the pause/resume
command that retries when it meets a lock, and the pause intent that is
written to disk before the process is stopped.  Take either fix away and
the matching test has to go red; put it back and the working tree has to
be clean again.  The patch, the commands and the red run are all written
down on this page, so a reader can repeat the whole thing and check it.

The gate lives in
`backend/tests/static_gates/test_pause_and_retry_negative_controls.py`:
it reads the `## Negative controls` section of this page and pins down
the four things every record has to carry — the command that takes the
fix back out, the red run's failing case and failing assertion, the
failure count, and the command that puts the fix back together with a
clean working tree.

## Negative controls

The two negative controls each change one piece of code: R2 takes the
busy-retry back out of the command side, R1 puts the stop back in front
of the pause intent.  The commands below run from the repository root;
`git apply` reads paths relative to the repository root, so pytest runs
in a subshell inside `backend/` and the shell is still in the root
afterwards.

### R2: take the busy-retry out of the command side

The patch takes the pause command's lifecycle call in
`backend/plan_scheduler/commands.py` back out of `retry_on_busy`: a
locked database is no longer retried, and the conflict is reported to
the caller straight away.

```bash
cat > /tmp/nc/retry.patch <<'PATCH'
diff --git a/backend/plan_scheduler/commands.py b/backend/plan_scheduler/commands.py
index 423d4093..b5500b2e 100644
--- a/backend/plan_scheduler/commands.py
+++ b/backend/plan_scheduler/commands.py
@@ -650,7 +650,7 @@ class CommandExecutor:
             return NOT_FOUND, "no_such_plan"
 
         try:
-            result = self._lifecycle.pause_plan(plan_id)
+            result = retry_on_busy(lambda: self._lifecycle.pause_plan(plan_id))
         except Exception as exc:  # noqa: BLE001 — the type decides
             if self._is_not_found(exc):
                 return NOT_FOUND, "no_such_plan"
PATCH
```

After the fix is taken back out, run that case — it has to go red:

```bash
git apply -R /tmp/nc/retry.patch
(cd backend && python3 -m pytest tests/integration/plan_scheduler/test_pause_busy_retry.py::test_pause_retries_locked_then_applies)
git apply /tmp/nc/retry.patch
git diff --stat -- backend/plan_scheduler/commands.py
```

- Failing case: `test_pause_retries_locked_then_applies`
- Failing assertion: `assert result.outcome == "applied"`
- Failure count: `failed=1`

```text
tests/integration/plan_scheduler/test_pause_busy_retry.py:297: in test_pause_retries_locked_then_applies
    assert result.outcome == "applied", result.detail
E   AssertionError: database_locked
E   assert 'conflict' == 'applied'

FAILED tests/integration/plan_scheduler/test_pause_busy_retry.py::test_pause_retries_locked_then_applies
1 failed
```

After the restore, `git diff --stat -- backend/plan_scheduler/commands.py`
prints nothing and the working tree is back to what it was.

### R1: put the stop back in front of the pause intent

The patch moves the call to `stop_plan` in
`backend/plan_lifecycle_service.py` from after the intent is written to
before it: the stop happens first and the pause intent is written
afterwards — the opposite order from the fix.

```bash
cat > /tmp/nc/intent.patch <<'PATCH'
diff --git a/backend/plan_lifecycle_service.py b/backend/plan_lifecycle_service.py
index 504106eb..55fa7383 100644
--- a/backend/plan_lifecycle_service.py
+++ b/backend/plan_lifecycle_service.py
@@ -744,7 +744,6 @@ class PlanLifecycleService:
         # 落库失败时不动手停：停完之后没有证据的意图，等于把一次崩溃
         # 说成一次暂停。答案带自己的名字，操作员看得见「有人要停它，
         # 但没停成」。
-        stopped = self.stop_plan(plan_id, requeue=False)
         try:
             pause_state.write_pause_intent(plan_dir, reason="user_pause")
         except OSError:
@@ -768,5 +767,6 @@ class PlanLifecycleService:
         #
         # 意图已经落库了，所以这一句抛出去的时候意图**留在盘上**：停到
         # 哪一步算哪一步（修复轮就是靠这个意图把「暂停」与「崩溃」分开）。
+        stopped = self.stop_plan(plan_id, requeue=False)
         if stopped.dispatched is not None:
             return stopped
PATCH
```

After the fix is taken back out, run that case — it has to go red:

```bash
git apply -R /tmp/nc/intent.patch
(cd backend && python3 -m pytest tests/integration/test_pause_intent_before_stop.py::test_intent_written_before_stop)
git apply /tmp/nc/intent.patch
git diff --stat -- backend/plan_lifecycle_service.py
```

- Failing case: `test_intent_written_before_stop`
- Failing assertion: `assert order == ["write", "stop"]`
- Failure count: `failed=1`

```text
tests/integration/test_pause_intent_before_stop.py:173: in test_intent_written_before_stop
    assert order == ["write", "stop"], (
E   AssertionError: the intent must be on disk before the stop starts: ['stop', 'write']
E   assert ['stop', 'write'] == ['write', 'stop']

FAILED tests/integration/test_pause_intent_before_stop.py::test_intent_written_before_stop
1 failed
```

After the restore, `git diff --stat -- backend/plan_lifecycle_service.py`
prints nothing and the working tree is back to what it was.
