# 暂停与重试的两条负控

这两条负控把「暂停 / 恢复命令遇锁重试」与「暂停意图先落库、再停机」
这两处修复钉住：把修复拿掉，对应的测试必须红；把修复放回去，工作树必须
干净。补丁、命令与红跑输出都写在这一页里，照着做一遍即可复核。

门禁在 `backend/tests/static_gates/test_pause_and_retry_negative_controls.py`：
它读本页的 `## 负控` 段，钉住每条记录必须带的四样东西 —— 还原命令、
红跑用例与失败断言、失败条数、恢复命令与干净工作树。

## 负控

两条负控各自改一处代码：R2 拿掉命令侧的遇锁重试，R1 把停机放回暂停意图
之前。下面的命令在仓库根目录执行；`git apply` 认的是仓库根目录下的路径，
所以 pytest 用子 shell 进 `backend/`，读完还在根目录。

### R2：拿掉命令侧的遇锁重试

补丁把 `backend/plan_scheduler/commands.py` 里暂停命令的生命周期调用重新
从 `retry_on_busy` 里拿出来：遇锁时不再重试，直接把冲突报给调用方。

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

还原之后跑那个用例，它必须红：

```bash
git apply -R /tmp/nc/retry.patch
(cd backend && python3 -m pytest tests/integration/plan_scheduler/test_pause_busy_retry.py::test_pause_retries_locked_then_applies)
git apply /tmp/nc/retry.patch
git diff --stat -- backend/plan_scheduler/commands.py
```

- 失败用例：`test_pause_retries_locked_then_applies`
- 失败断言：`assert result.outcome == "applied"`
- 失败条数：failed=1

```text
tests/integration/plan_scheduler/test_pause_busy_retry.py:297: in test_pause_retries_locked_then_applies
    assert result.outcome == "applied", result.detail
E   AssertionError: database_locked
E   assert 'conflict' == 'applied'

FAILED tests/integration/plan_scheduler/test_pause_busy_retry.py::test_pause_retries_locked_then_applies
1 failed
```

恢复之后 `git diff --stat -- backend/plan_scheduler/commands.py` 无输出，
工作树回到原样。

### R1：把停机放回暂停意图之前

补丁把 `backend/plan_lifecycle_service.py` 里 `stop_plan` 的调用从意图落库
之后挪回之前：停机先发生，暂停意图随后才落盘 —— 顺序正好与修复相反。

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

还原之后跑那个用例，它必须红：

```bash
git apply -R /tmp/nc/intent.patch
(cd backend && python3 -m pytest tests/integration/test_pause_intent_before_stop.py::test_intent_written_before_stop)
git apply /tmp/nc/intent.patch
git diff --stat -- backend/plan_lifecycle_service.py
```

- 失败用例：`test_intent_written_before_stop`
- 失败断言：`assert order == ["write", "stop"]`
- 失败条数：failed=1

```text
tests/integration/test_pause_intent_before_stop.py:173: in test_intent_written_before_stop
    assert order == ["write", "stop"], (
E   AssertionError: the intent must be on disk before the stop starts: ['stop', 'write']
E   assert ['stop', 'write'] == ['write', 'stop']

FAILED tests/integration/test_pause_intent_before_stop.py::test_intent_written_before_stop
1 failed
```

恢复之后 `git diff --stat -- backend/plan_lifecycle_service.py` 无输出，
工作树回到原样。
