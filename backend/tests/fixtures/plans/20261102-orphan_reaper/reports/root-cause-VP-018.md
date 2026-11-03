# VP-018 根因报告（第 2 轮 · 根因已演进）

~~~json
{
  "plan_id": "20261102-orphan_reaper",
  "vp_id": "VP-018",
  "round": 2,
  "previous_failure_fingerprint": "8418c2747eac028b",
  "failure_fingerprint": "1ac5873d07c66771",
  "root_cause": "服务生命周期层缺一条身份检查：停止路径（service_manager.stop_service → kill_service → os.kill/os.killpg）按 spawn 时记下的 pid/pgid 发信号，从不核对『这个 pid 还是我启的那个进程吗』。宿主被本分支自己放大（server.py 的 ensure_daemon(detach_owner=True) 让守护进程活得比主人久 → 本机 24 个 --daemon 全机每 30s 扫一遍、事件日志无上限已到 1.1GB；再叠 232 个残留执行器、loadavg 40）之后，pid 回收快到停止信号会命中刚起 0.6–2.3 秒的另一台服务。这条误杀又被两层缺陷放大成 8 条红：就绪等待（_wait_ready_with_evidence）从不轮询子进程，看不见『它已经死了』；失败行（_failure_detail）打印的是声明的超时而不是实际等待（实测 1.9s 的用例报『not ready after 30s』）。reaper 已被它自己的 kill_events.jsonl 排除（reason=owner_alive, signal=\"\"）。",
  "paths": [
    "backend/service_manager.py",
    "backend/process_identity.py",
    "backend/orphan_reaper.py",
    "backend/server.py",
    "backend/tests/conftest.py",
    "backend/tests/reaper_daemon_guard.py",
    "backend/tests/unit/test_service_manager.py",
    "backend/tests/unit/test_service_kill_attribution.py",
    "backend/tests/unit/test_orphan_reaper_daemon_budget.py",
    "backend/tests/unit/test_server_orphan_reaper_wiring.py"
  ],
  "files_to_modify": [
    "backend/service_manager.py",
    "backend/process_identity.py",
    "backend/orphan_reaper.py",
    "backend/server.py",
    "backend/tests/conftest.py",
    "backend/tests/reaper_daemon_guard.py",
    "backend/tests/unit/test_service_manager.py",
    "backend/tests/unit/test_service_kill_attribution.py",
    "backend/tests/unit/test_orphan_reaper_daemon_budget.py",
    "backend/tests/unit/test_server_orphan_reaper_wiring.py"
  ],
  "fix_plan": {
    "files_to_modify": [
      "backend/service_manager.py",
      "backend/process_identity.py",
      "backend/orphan_reaper.py",
      "backend/server.py",
      "backend/tests/conftest.py",
      "backend/tests/reaper_daemon_guard.py",
      "backend/tests/unit/test_service_manager.py",
      "backend/tests/unit/test_service_kill_attribution.py",
      "backend/tests/unit/test_orphan_reaper_daemon_budget.py",
      "backend/tests/unit/test_server_orphan_reaper_wiring.py"
    ],
    "layers": [
      "L1 停止路径的身份闸门（service_manager.py）：stop_service/kill_service 在 os.kill(pid) / os.killpg(pgid) 之前必须核对身份——pid 的存活进程启动时间与 runtime.start_epoch 的差 ≤ 2s（复用 process_identity.probe + CREATE_TIME_TOLERANCE），pgid 必须仍由该 pid 领着；核对不上就只报告、不发信号。这条是凶手的正面堵漏。",
      "L2 身份锚点提前记（service_manager.py）：runtime.start_epoch 现在记在 _detached_pgid_of() 之后，而后者最多阻塞 5s（_DETACHED_PGID_TIMEOUT_SECONDS=5.0），锚点最多晚 5 秒；把 start_epoch 的取值挪到 Popen 之后立刻，pgid 的等待不再污染身份窗口。",
      "L3 就绪等待看得见死亡（service_manager.py）：_wait_ready_with_evidence 必须拿到子进程句柄，poll() 到非 None 就立刻结束等待并区分『它死了』与『它慢』；_failure_detail 必须打印实际等待秒数与退出信号，『已答过 200 然后死掉』必须这样写而不是 not ready。",
      "L4 退出原因不被掩盖（service_manager.py）：stop_service 不得为已经死掉的进程置 runtime.stopped；_describe_exit 只有在确实是本轮停掉时才写 stopped by the round。",
      "L5 凶手永远在案（service_manager.py）：kill_service 在没有 plan_dir 时（_ambient_plan_dir() 为空）也要落一条请求记录（含 caller / stack_hint），否则『没有 kill 请求』等于没有证据。",
      "L6 宿主不再被本分支永久劣化（orphan_reaper.py / server.py / tests/conftest.py / tests/reaper_daemon_guard.py）：守护进程的 kill_events.jsonl 必须有上限（轮转或只记动手的裁决 + 跳过计数）；detach_owner=True 改成显式开关而不是默认，守护进程要有租约、主人不在就自己退出；conftest 的回收夹具要按 worker 回收本会话启动的守护进程（含 --survive-owner 那些）。",
      "L7 验证层：只跑这次失败的 8 条 + phase-1 lane 全绿。"
    ],
    "must_not": [
      "不得改动 backend/routes/execution.py（那是上一轮针对旧根因的修复面，本轮不再适用）",
      "不得改 backend/orphan_reaper.py 的 classify 判定阶梯（owner_alive 的跳过是对的，reaper 已被排除）",
      "不得把 os.kill/os.killpg 的兜底路径整条删掉（必须保留发信号的能力，只加身份闸门）",
      "不得放宽任何断言来让红变绿（8 条用例的判定标准原样保留）",
      "不得把宿主清理（杀别人的残留进程）写进产品代码；宿主清理是运维动作，不是本次改动范围"
    ]
  }
}
~~~

## 0. 一句话结论

上一轮针对旧根因（`argv[0]` 身份改写 / 慢 settle / 停放写两处）的方案**已经生效且本轮不再出现**：round 2 的 8 条失败里没有一条是那三类。
本轮红的是**另一条链**，而且这条链的中心不是 reaper：

> **服务生命周期层没有一条「这个 pid 还是我启的那个进程吗」的检查。**
> 停止路径按 spawn 时记下的 pid/pgid 直接发信号；宿主被本分支自己放大到 pid 回收很快之后，一次迟到 1–2 秒的停止就打死了**一台刚起来 0.6–2.3 秒的、属于另一个用例的服务**。
> 这次误杀又被两层缺陷放大成 8 条红、并抹掉线索：
> **就绪等待从不看子进程**（看不见「它已经死了」），**失败行打印声明的超时而不是实际等待**（1.9 秒的用例报「not ready after 30s」）。

叠加在这条链上的土壤是**本分支自己引入的**：`backend/server.py` 的 `ensure_daemon(config, detach_owner=True)` 把守护进程的**默认「与主人同命」换成了豁免**，于是它**活得比它的主人久**，测试套件里每个用例的 `tmp_path` 都会漏一个；实测本机 **24 个** `--daemon` 存活（十分钟后再测 **29 个**，还在长），各自每 30 秒全机扫一遍、每个进程写一条裁决，事件日志**无上限**、最大的已到 **1.1 GB**；再叠上 232 个残留 `deterministic_executor.py`，宿主 loadavg 一度到 **40**，墙钟与 pid 回收都失真。**上一轮只把这批 daemon 当成「跑前 21 → 跑后 21」的环境读数（一条硬约束），这一轮有 diff 证据说明它们是本分支的 `detach_owner=True` 造成的、而且还在长大。**

## 1. 本轮证据（全部在当前树 HEAD 上实跑）

### 1.0 确定性复现步骤

#### 1.0.1 环境事实（来自 `plans/20261102-orphan_reaper/context/round-6.json`，不重新探索）

| 项 | 值 |
|---|---|
| worktree | `worktrees/reaper-leak`（分支 `fix/reaper-daemon-leak`） |
| base_commit | `5165c6045e54bbf45edcc9b157b997008ad72fd5` |
| 改动面 | 22 个文件，+6495 / −53（`backend/orphan_reaper.py` +1002、`backend/routes/execution.py` +304、`backend/server.py` ±12、`backend/tests/conftest.py` +62 …） |
| `focused_test_command` | `null`（本任务没有定向命令） |
| 本轮失败产物 | `plans/20261102-orphan_reaper/vp_artifacts/VP-018/phase-1.xml`（8 条 failure）、`phase-2.xml` / `phase-3.xml`（通过）、`ci_output.json`（exit_code=1，duration 892.67s） |

#### 1.0.2 复现命令（在 worktree 的仓库根执行）

~~~bash
PDT_GATE_JUNIT_DIR=/tmp/vp018_verify bash scripts/run_tests.sh --lane unit
~~~

本轮实跑（第二次复现，2026-11-03 02:00–02:06）的真实输出尾部：

~~~
FAILED backend/tests/unit/test_quota_wait_wall_clock_c.py::TestQuotaWaitIsNotChargedToTheBudget::test_a_wait_many_times_the_budget_still_returns_the_answer
FAILED backend/tests/unit/test_quota_wait_wall_clock_c.py::TestQuotaWaitIsNotChargedToTheBudget::test_the_wait_itself_is_not_capped
FAILED backend/tests/unit/test_quota_wait_wall_clock_c.py::TestQuotaWaitIsNotChargedToTheBudget::test_query_is_bounded_the_same_way
FAILED backend/tests/unit/test_quota_wait_wall_clock_c.py::TestTheBudgetStillBoundsWork::test_work_done_after_the_wait_is_still_charged
FAILED backend/tests/unit/test_service_manager.py::test_health_url_drives_readiness
FAILED backend/tests/unit/test_verification_service_freshness.py::test_reensure_adopted_only_after_health_200

=== gate summary ===
phase 1/3 (parallel):             FAILED (exit 1)
phase 2/3 (e2e + integration):    not selected
phase 3/3 (time_sensitive + perf): not selected
host measurability:               not measured (the wall-clock phase was not selected)
gate budget: 1800s
gate elapsed: 344s
gate: FAIL (exit 1) — a phase failed
~~~

第一次复现（2026-11-03 01:42–01:45，日志 `/tmp/vp018_gate_unit.log`）的收尾行：

~~~
= 6 failed, 11228 passed, 39 skipped, 1 xfailed, 77 warnings, 10 subtests passed in 163.74s (0:02:43) =
gate: FAIL (exit 1) — a phase failed
~~~

#### 1.0.3 从 junit 里读出「实测时长 vs 失败行写的时长」

~~~bash
python3 - <<'PY'
import glob, xml.etree.ElementTree as ET
for f in sorted(glob.glob('/tmp/vp018_verify/*.xml')):
    for tc in ET.parse(f).getroot().iter('testcase'):
        for fail in list(tc.findall('failure')) + list(tc.findall('error')):
            print(f"{tc.get('classname')}::{tc.get('name')}  dur={tc.get('time')}  {(fail.get('message') or '')[:180]}")
PY
~~~

真实输出：

~~~
tests.unit.test_quota_wait_wall_clock_c.TestQuotaWaitIsNotChargedToTheBudget::test_a_wait_many_times_the_budget_still_returns_the_answer  dur=8.815  TimeoutError: All providers failed after 2 timeout retries (max=2). Providers attempted: ['Vendor A Pro', 'Vendor A', 'Vendor A API']. Original error: Query timed out after 1.0 sec
tests.unit.test_quota_wait_wall_clock_c.TestQuotaWaitIsNotChargedToTheBudget::test_the_wait_itself_is_not_capped  dur=11.932  TimeoutError: All providers failed after 2 timeout retries (max=2). Providers attempted: ['Vendor A Pro', 'Vendor A', 'Vendor A API']. Original error: Query timed out after 1.0 sec
tests.unit.test_quota_wait_wall_clock_c.TestQuotaWaitIsNotChargedToTheBudget::test_query_is_bounded_the_same_way  dur=4.133  AssertionError: 2
assert 2 == 1
 +  where 2 = _launches(PosixPath('/private/var/folders/4w/nt19ql0d1y731n4zz_9pv60m0000gn/T/pytest-of-yongmaoluo/pytest-48754/popen-gw11/test_query_
tests.unit.test_quota_wait_wall_clock_c.TestTheBudgetStillBoundsWork::test_work_done_after_the_wait_is_still_charged  dur=7.055  AssertionError: the call ran 7.04s -- 1.50s of wait plus 3 attempts at a 1.0s budget. It waited the CLI out instead of billing it.
assert 7.0444316659122705 < ((1.5 + (3 * 1.0)) +
tests.unit.test_service_manager::test_health_url_drives_readiness  dur=3.047  AssertionError: started (pid 39137, pgid 39137) but not ready after 62s; rc=-15; log tail: 127.0.0.1 - - [03/Nov/2026 02:06:13] "GET / HTTP/1.1" 200 - | 127.0.0.1 - - [03/Nov/2026
tests.unit.test_verification_service_freshness::test_reensure_adopted_only_after_health_200  dur=65.655  AssertionError: started (pid 40151, pgid 40151) but not ready after 60s; rc=-15; (empty)
assert False is True
 +  where False = ServiceRuntime(name='api-well', port=20834, declared
~~~

（上面这段输出里的临时路径是 pytest 自己的 tmp 目录，与本报告的结论无关。）

#### 1.0.4 复现的前置条件（不满足就不是「确定性」复现）

1. **宿主必须先清干净**：24–29 个 `--daemon` reaper、232 个残留 `deterministic_executor.py`。
   不清的话，gate 自己的 `host measurability` 探针会判「宿主不可测」（退出码 3 而不是 1），
   而且失败集合会随宿主负载漂移 —— **这正是 §1.1 要证明的那件事**。
2. **必须跑 lane 1（`--lane unit`）**：CI 这一轮红的是 phase 1，phase 2/3 是过的。
3. 复现的**判定标准不是「哪几条红」**，而是 §1.2 的两个读数：
   失败行里的秒数 == junit 记的实测 duration，且 `rc=-15` 不再出现。

### 1.1 失败面又变了 —— 指纹演进本身就是证据

| 轮次 | 失败面 | 来源 |
|---|---|---|
| round 1 | phase 1 五条 + phase 2 八条 = **13 条** | `verification_failure_history.json` |
| round 2 (CI) | phase 1 **8 条**，phase 2/3 全过 | `verification_failure_history.json` + `vp_artifacts/VP-018/phase-1.xml` |
| 本地复跑 A | **6 条**，其中 **5 条**正是 CI 这轮的 8 条之一 | `/tmp/vp018_gate_unit.log` |
| 本地复跑 B | **6 条**，**集合不同**：4 条变成 `test_quota_wait_wall_clock_c` 的纯墙钟失败 | 本报告 §1.2 |

复跑 B 里那 4 条的原文是 `TimeoutError: ... Query timed out after 1.0 sec` 与
`AssertionError: the call ran 7.04s -- 1.50s of wait plus 3 attempts at a 1.0s budget. It waited the CLI out instead of billing it.`
—— 这是**墙钟被拉长 4 倍**的直接读数，与服务无关。
本地复跑 A 的 6 条原文（`/tmp/vp018_gate_unit.log`，收尾行 `= 6 failed, 11228 passed, 39 skipped, 1 xfailed, 77 warnings, 10 subtests passed in 163.74s (0:02:43) =`，`gate: FAIL (exit 1) — a phase failed`）：

~~~
FAILED backend/tests/unit/test_service_kill_attribution.py::test_a_parked_execution_is_still_answerable_after_the_owning_process_exits
FAILED backend/tests/unit/test_service_address_events.py::test_start_service_records_the_allocation_it_made
FAILED backend/tests/unit/test_service_restart_preserves_fixtures.py::test_the_topup_wait_log_stays_answerable_after_the_round_service_outlives_its_starter
FAILED backend/tests/unit/test_service_process_watcher.py::test_exit_event_carries_port
FAILED backend/tests/unit/test_service_manager.py::test_start_service_end_to_end
FAILED backend/tests/unit/test_service_manager.py::test_stopping_a_relocated_service_never_reaches_the_test_session
~~~

**其中 5 条就是 CI 这轮 8 条里的同一条**（第 4 条 `test_exit_event_carries_port` 是 round 1 的那条节点，本轮 CI 换成了 `test_health_data_still_answers_after_the_round_records_its_service`）。

⇒ **失败集合是宿主状态的函数，不是代码的函数**；这排除了「某一个确定性的代码路径错了」这种解释，把矛头指向「一条被宿主放大的竞态」。

### 1.2 CI 这一轮的 8 条失败：真实时长 vs 失败行里写的时长

`vp_artifacts/VP-018/phase-1.xml`（框架自己跑出来的 junit，**duration 是框架记的实测值**）：

| # | node id | 实测 duration | 失败行原文里的 N | 差 |
|---|---|---|---|---|
| 1 | `tests.integration.test_quota_parked_plan_status_http::test_vp_004_plan_status_reports_parked_phase_and_counts` | 5.408s | `urlopen error [Errno 61] Connection refused` | — |
| 2 | `tests.unit.test_service_address_events::test_start_service_records_the_allocation_it_made` | 20.676s | `not ready after 20s; rc=-15` | ≈0 |
| 3 | `tests.unit.test_service_kill_attribution::test_a_parked_execution_is_still_answerable_after_the_owning_process_exits` | **2.305s** | **`not ready after 30s; rc=-15`** | **≈ −28s** |
| 4 | `tests.unit.test_service_manager::test_start_service_end_to_end` | 36.571s | `not ready after 35s; rc=-15` | ≈ +1.5s |
| 5 | `tests.unit.test_service_process_watcher::test_health_data_still_answers_after_the_round_records_its_service` | 53.795s | `not ready after 53s; rc=-15` | ≈ +0.8s |
| 6 | `tests.unit.test_service_manager::test_stopping_a_relocated_service_never_reaches_the_test_session` | 40.632s | `the stop signalled group 44188 (signal 15), which is not the service's own group 47547` | — |
| 7 | `tests.unit.test_service_kill_attribution::test_the_round_s_own_reap_names_itself_as_the_caller` | **1.908s** | **`not ready after 30s; rc=-15`** | **≈ −28s** |
| 8 | `tests.unit.test_service_restart_preserves_fixtures::test_the_topup_wait_log_stays_answerable_after_the_round_service_outlives_its_starter` | 30.929s | `not ready after 30s; rc=-15; log tail: vp_quota fixture ready` | ≈ +0.9s |

**7 条里的 7 条都是 `rc=-15`（SIGTERM）**，没有一条是「服务起不来」或「端口被占」。
**第 3、7 条的实测时长是 1.9s / 2.3s，失败行却写「not ready after 30s」** —— 差了 28 秒。这就是「失败行在说谎」的第一个硬读数（机制见 §1.7）。

第 6 条（relocated-stop）是**二阶后果**，不是独立缺陷：它记下的 44188 是**第一次搬迁尝试**的进程组，47547 是第二次的；第一次尝试之所以失败，是因为它撞上了同一条误杀链（见 §2 的 L1→L3）。

### 1.3 负控 A —— reaper 不是凶手（它自己的日志自证）

`backend/orphan_reaper.py` 的 `_cleanup_pass` 每次扫描都把**全部裁决**写进 `kill_events.jsonl`（`write_events(events_path(config.plans_root), reaped)`，`backend/orphan_reaper.py:4846`）。
在失败窗口里，那批死掉的服务 pid（87908 / 88537 / 92759 等）在 reaper 自己的日志里的裁决是：

~~~
{"reason": "owner_alive", "signal": ""}
~~~

`signal: ""` = reaper **没有对它发过任何信号**。
⇒ **reaper 这条路径被排除。**（上一轮的方案是针对 reaper/argv[0] 的；本轮不适用。）

### 1.4 负控 B —— 凶手在仓内，而且只有一条路能产生这个记录

死者的退出事件（`service_process_events.jsonl`）原文：

~~~
{"event": "service_process_exited", "exit_signal": 15, "pid": 87453,
 "reason": "stopped by the round", "returncode": -15, "uptime_sec": 2.31,
 "ts": "2026-11-03T01:44:15.474347"}
~~~

`reason` 由 `backend/service_manager.py:5401-5404` 的 `_describe_exit` 决定（原文照抄）：

~~~python
def _describe_exit(returncode: Optional[int], stopped: bool) -> str:
    """One line saying what the exit was, in words."""
    if stopped:
        return "stopped by the round"
~~~

而 `runtime.stopped = True` 全仓只有两处：

~~~
backend/service_manager.py:1474   （kill_service 内部）
backend/service_manager.py:1633   （stop_service 内部）
~~~

（`grep -rn "\.stopped = True" backend/ --include="*.py"`，排除测试后只有这两行。）

⇒ **`"stopped by the round"` 只可能由 `service_manager.kill_service` / `stop_service` 产生。**
⇒ 凶手是**仓内的停止路径**，不是 reaper、不是外部进程、不是 OOM。
⇒ 另外：`uptime_sec: 2.31` —— 死亡发生在服务启动后 **2.31 秒**，也就是**测试还在跑的时候**，不是测试收尾时。

### 1.5 负控 C —— 停止路径没有身份闸门（而 reaping 路径有）

`backend/service_manager.py:1493-1500`（`kill_service` 内的 `_signal_pids`；`os.kill(pid, sig)` 在 `:1496`）。
**裸 pid，没有任何身份核对**（原文照抄）：

~~~python
    def _signal_pids() -> None:
        for pid in targets:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                continue
~~~

`backend/service_manager.py:1620`（`stop_service` 内部）——
**停的时候现读一个可能已被回收的 pid 的当前组**（原文照抄）：

~~~python
    pgid = runtime.pgid or _process_group_of(targets[0])
~~~

对比 **reaping 路径是有闸门的**：`backend/service_manager.py:5923-5939` 的 `_pid_reused(entry)` 用 2 秒容差比较「记下的启动时间」与「活着的启动时间」，`backend/service_manager.py:6344` 在 reaping 前调用它。
⇒ **同一个模块里，「我能不能杀这个 pid」有两个答案**：reaping 路径有答案，停止路径没有。
而 `stop_service` 的目标 `runtime.pid` 是 **spawn 时**记下的（`backend/service_manager.py:2671`），中间可能隔了几秒甚至跨过一个 pid 回收。

### 1.6 负控 D —— 身份锚点自己就晚了 5 秒

`backend/service_manager.py:2671-2682`（原文照抄；**身份锚点 `start_epoch` 记在 `_detached_pgid_of()` 之后**，而后者最多阻塞 5s）：

~~~python
    runtime.pid = proc.pid
    runtime.pgid = _detached_pgid_of(proc.pid)
    # Our own clock, not the system's. `proc` was created by the line
    # above, so we know when it started without asking anyone, and that
    # matters twice over: the `ps -o lstart=` probe is unavailable or
    # racy on a machine that restricts subprocesses (the unit test
    # environment does), and a `None` here silently disables the PID-reuse
    # guard in `_pid_reused` — a recycled PID would then be signalled as
    # if it were still ours. The comparison tolerates 2 seconds and
    # `ps lstart` only has 1-second granularity, so a wall-clock reading
    # taken microseconds after the fork is well inside it.
    runtime.start_epoch = time.time()
~~~

`_DETACHED_PGID_TIMEOUT_SECONDS = 5.0`（`backend/service_manager.py:1156`），`_detached_pgid_of`（1177-1199）在等子进程 `setsid()` 之前一直循环。
⇒ **记录下来的身份比真实启动时刻最多晚 5 秒**；而复用判定用的是 2 秒容差。
⇒ 就算把 §1.5 的闸门照抄到停止路径，锚点本身也是歪的 —— **两处必须一起改**。
（旁证：reaper 自己的日志里，同一个 pid 92759 在 `01:45:03` 记 `pgid: 80978`、在 `01:45:05` 记 `pgid: 92759` —— `setsid()` 落在这 2–5 秒窗口里。）

### 1.7 负控 E —— 就绪等待看不见「它已经死了」，失败行打印的是声明超时

`backend/service_manager.py:2224-2277`：

~~~python
while True:
    if _service_is_answering(decl):
        evidence = data_plane_evidence(decl.port)
        if _data_plane_is_acceptable(evidence, expected_rows, decl):
            return True, evidence
    if time.monotonic() >= deadline:
        return False, evidence
    time.sleep(_READY_POLL_INTERVAL)
~~~

**这个循环里没有子进程。** 子进程死了，它照样把整个声明预算烧完（本地复跑 B 实测：一条 `dur=65.655s` 的用例，服务在 ~2 秒时就死了，失败行写「not ready after 60s」）。

`backend/service_manager.py:3535-3568` 的 `_failure_detail` 打印的是 `timeout_seconds` 参数，而两个调用点传的都是 **`decl.ready_timeout_seconds`**（`backend/service_manager.py:2779` 与 `:2806`）—— **声明值，不是实际等了多少秒**。

还有第三种形状：`if ready:` 分支里 `_await_exit(proc, timeout=2.0)`（`backend/service_manager.py:2772`）发现子进程在 2 秒内死了 → `runtime.ready = False` + 同一条「not ready after Ns」。**此时这台服务其实答过 200** —— 本地复跑 B 的失败尾巴里就留着 `127.0.0.1 - - [03/Nov/2026 02:06:13] "GET / HTTP/1.1" 200 -`。
⇒ **三种形状都被归到同一句「not ready after Ns」上**：慢、死了、答过又死了。下一轮读报告的人只能从错误的那一层开始查。

### 1.8 负控 F —— 停止请求根本没落盘，「没有 kill 请求」等于没有证据

`backend/service_manager.py:1465-1467`（`kill_service`）：

~~~python
recorded = False
if plan_dir is not None:
    recorded = record_kill_event(Path(plan_dir), request)
~~~

`plan_dir` 为空时取 `_ambient_plan_dir()`（`backend/service_manager.py:1537`），而它读的是 ContextVar，**在 `address_event_context` 之外是 None**。
本轮 8 条失败涉及的用例，其收尾都走 `sm.stop_service(runtime, None, grace_seconds=5.0)`（例如 `backend/tests/unit/test_service_kill_attribution.py:359`）—— **不传 plan_dir**。
⇒ **停止请求一条都没落盘。**
⇒ 上一轮我据此推断过「仓内没有发过信号」——**那是错的**（请求是被丢掉的，不是没发生）。本报告把它作为**证据链的必修项**写进 L5。

### 1.9 负控 G —— 宿主是被本分支永久放大的（`git diff` vs base）

~~~
$ git diff 5165c6045e54bbf45edcc9b157b997008ad72fd5..HEAD -- backend/server.py
-        return orphan_reaper.ensure_daemon(config)
+        return orphan_reaper.ensure_daemon(config, detach_owner=True)
...
-                orphan_reaper.ensure_daemon(config)
+                orphan_reaper.ensure_daemon(config, detach_owner=True)
~~~

（`backend/server.py:1629` 与 `:1764`。）**这是本分支自己的改动**：守护进程被豁免于「主人不在就退出」的规则，**活得比服务器久**。每启一次后端就漏一个。

实测当前状态：

~~~
$ ps -Ao pid,etime,args | grep "[o]rphan_reaper" | grep -- "--daemon" | wc -l
24                              ← 02:04 实测
$ ps -Ao pid,etime,args | grep "[o]rphan_reaper" | grep -- "--daemon" | wc -l
29                              ← 02:13 再测：**十分钟内又多了 5 个**
$ ps -Ao pid,etime,args | grep "[o]rphan_reaper" | grep -- "--daemon" | awk '{print $2}' | head -4
05:02:56
05:02:54
04:32:37
07:00:23                      ← 最老的一个已经跑了 7 小时
$ ps -Ao pid,args | grep -c "[d]eterministic_executor.py"
232
$ uptime
load averages: 40.19 19.42 11.07
~~~

（**02:20 复测**：`--daemon` **28** 个、`deterministic_executor.py` **232** 个不变、
`load averages: 4.29 7.38 9.96`。）
⇒ **常驻变的是「daemon 与事件日志」，loadavg 那一波是我自己跑门禁造成的尖峰**，不是恒定的 40。
两者性质不同：**daemon 与 1.1 GB 日志是永久劣化**（尖峰过去它们还在长），
**loadavg 尖峰是一次性的**（跑完门禁就回落）。

**这 5 个新长出来的是谁漏的？本轮我自己跑的那遍门禁。**
`ensure_daemon` 是「同一个 plans root 一个」的单例（`backend/orphan_reaper.py:3728-3729`），
而测试套件里**每个用例的 `tmp_path` 是一个新的 plans root** —— 于是每个启过后端的用例漏一个 daemon。
漏的对象就是本轮正在被测的服务。

`ensure_daemon` 的 docstring 自己写明了默认与豁免的关系（`backend/orphan_reaper.py:3705-3711`）：

> "**Who the daemon belongs to** is the caller's decision, and the default is the safe one. By default the daemon is *attached*: it is told that this process is its owner (pid **and** start time …), and it leaves when that owner is gone — a daemon that outlives the process that asked for it has nothing left to do. ``detach_owner=True`` is the exemption."

⇒ 本分支把**默认**换成了**豁免**，而且换在服务器启动与重保活两条路径上（`backend/server.py:1629` / `:1764`）。

它们的日志：

~~~
$ find /private/tmp /tmp -name "kill_events.jsonl" -type f -size +1M -exec ls -l {} \; | awk '{printf "%.1f MB\t%s\n", $5/1048576, $9}' | sort -rn | head -4
1121.9 MB	/private/tmp/bootsample/plans/.orphan_reaper/kill_events.jsonl
1055.7 MB	/private/tmp/vp016-repro2/plans/.orphan_reaper/kill_events.jsonl
1040.1 MB	/private/tmp/bootmeasure2/plans-0/.orphan_reaper/kill_events.jsonl
1040.0 MB	/private/tmp/bootmeasure2/plans-1/.orphan_reaper/kill_events.jsonl
~~~

无上限的来源在代码里写得很清楚（`backend/orphan_reaper.py:4475-4478`）：

> "The log is appended to, never rewritten: a reaper that runs every thirty seconds must not lose the record of what it did the last thirty seconds ago."

⇒ 每 30 秒、每台机器上的每个进程一条裁决，**永不停、永不轮转**。
（尾巴里那条 `reason: "env_unreadable"` 的裁决，argv 是本会话自己的一条 shell —— 它也每 30 秒被记一次。）

CI 侧同一件事的读数（`verification_failure_history.json` 原文）：

> `host measurability: the host cannot measure wall-clock (sleep(0.05)=0.192s, loadavg=4.73)`

⇒ 宿主墙钟慢 4 倍；**pid 回收与信号到达的相对时序全部被拉长**，§1.4 的误杀正需要这个土壤。

## 2. 完整因果链

~~~
[L6 土壤 · 本分支引入]
  backend/server.py:1629/:1764  ensure_daemon(detach_owner=True)
      → 守护进程活得比主人久，每启一次后端漏一个
      → 实测 24 个 --daemon 存活（最老 7 小时）+ 232 个残留执行器
      → 每 30 秒全机扫一遍、每进程一条裁决、kill_events.jsonl 无上限（最大 1.1 GB）
      → 宿主 loadavg 40、墙钟慢 4 倍、pid 回收变快
                    │
                    ▼
[L1 凶手 · 停止路径没有身份闸门]
  stop_service(backend/service_manager.py:1620)  →  pgid = runtime.pgid or _process_group_of(targets[0])
  kill_service (backend/service_manager.py:1496) →  os.kill(pid, sig)      ← 裸 pid，不核对身份
      → 一次迟到 1–2 秒的停止，命中已被回收给另一台服务的 pid
      → 受害者：刚起 0.6–2.3 秒的另一台服务（rc=-15）
                    │
[L2 锚点歪了]                                     ▼
  runtime.start_epoch 记在 _detached_pgid_of() 之后（最多晚 5 秒）
      → 就算补上闸门，2 秒容差的比对也是拿歪锚点去比
                    │
                    ▼
[L3 误杀被放大成「就绪超时」]
  _wait_ready_with_evidence (2224-2277) 从不看子进程
      → 服务 1.8 秒就死了，等待仍烧满声明预算（实测 65.655s）
  _failure_detail (3535-3568) 打印 decl.ready_timeout_seconds
      → 1.9 秒的用例报「not ready after 30s」；答过 200 又死的也报同一句
                    │
                    ▼
[L4 结论被掩盖]
  stop_service 无条件 runtime.stopped = True（1633）
  _describe_exit (5401) → 「stopped by the round」
      → 别人的停止，被记成「这一轮停的」
                    │
                    ▼
[L5 线索被丢掉]
  kill_service 只在 plan_dir 已知时落请求（1465-1467）；_ambient_plan_dir() 在测试里是 None
      → 这一轮没有 kill 请求可查；下一轮又只能靠推断
                    │
                    ▼
[8 条红]（§1.2）—— 6 条服务类 + 1 条连接被拒 + 1 条 relocated-stop（二阶后果）
~~~

**这一轮的指纹（`1ac5873d07c66771`）与上一轮（`8418c2747eac028b`）完全不同**：上一轮是 `argv[0]` 身份改写 + 慢 settle + 停放写两处；本轮一条都不是。

## 3. 修复方案（一次覆盖所有层）

### L1 停止路径的身份闸门 —— `backend/service_manager.py`（凶手正面堵漏）

* `kill_service._signal_pids()`：在 `os.kill(pid, sig)` 之前核对身份 ——
  用 `process_identity.probe(pid)` 取活进程的 `create_time`，与 `runtime.start_epoch` 的差必须 ≤ `CREATE_TIME_TOLERANCE`（2.0，`backend/process_identity.py:65`）；
  核对不上 ⇒ **不发信号**，只把「pid 已被回收，未发信号」写进返回值与事件。
* `stop_service` 的 pgid 选取：只有在**该 pid 仍在领着这个组**时才 `os.killpg`；
  `runtime.pgid` 为空时不再现读 `_process_group_of(targets[0])` 去猜一个陌生人的组。
* 复用 `backend/process_identity.py` 已有的 `probe` / `matches` / `CREATE_TIME_TOLERANCE`，
  **不要**在 service_manager 里再写第二份身份规则（该文件注释里已经写明「一个问题只能有一个答案」）。

### L2 身份锚点提前 —— `backend/service_manager.py`

* `runtime.start_epoch = time.time()` 挪到 `Popen` 成功之后**立刻**赋值（2671 行之后），
  `_detached_pgid_of()` 的等待不再污染身份窗口。
* 若 `start_epoch` 记不准，宁可记 `None` 并让停止路径拒绝发信号，也不要记一个偏 5 秒的值。

### L3 就绪等待看得见死亡 + 失败行说实话 —— `backend/service_manager.py`

* `_wait_ready_with_evidence` 增加子进程句柄（或一个 `is_alive` 回调）：
  `proc.poll()` 非 None ⇒ 立刻结束等待，并返回「它死了」这个**新的**结论，
  不再烧完剩余预算。
* `_failure_detail` 打印**实际等待秒数**（`time.monotonic()` 差）与**退出信号**
  （`rc=-15` ⇒ 「killed by SIGTERM」），不再打印 `decl.ready_timeout_seconds`。
* 「答过 200 然后死掉」写成它自己的形状，不与「没起来」共用一句话。

### L4 退出原因不被掩盖 —— `backend/service_manager.py`

* `stop_service` 只在**它确实停掉了一个活着的进程**时才置 `runtime.stopped = True`；
  对已经死掉的进程，退出原因必须回到「killed by signal N」或「exited with code N」。
* `_describe_exit` 的分支顺序照旧，但前提由 L4 保证。

### L5 凶手永远在案 —— `backend/service_manager.py`

* `kill_service` 在 `plan_dir` 为空时也要落一条请求记录：回落到 `_ambient_plan_dir()`，
  再不行就落到 spawn registry / 服务日志目录，**记录里必须带 `caller` 与 `stack_hint`**。
* 判定「谁杀了它」从此有据可查，而不是靠「日志里没有 ⇒ 没发生过」这种反向推断。

### L6 宿主不再被本分支永久劣化 —— `backend/orphan_reaper.py` / `backend/server.py` / 测试夹具

* `backend/orphan_reaper.py`
  * `kill_events.jsonl` 必须有上限：按大小轮转，或只写「动手过的裁决」+ 一条滚动的跳过计数
    （现在每 30 秒给本会话自己的 shell 也记一条 `env_unreadable`，这是 1.1 GB 的来源）。
  * 守护进程要有**租约/心跳**：主人不在（且没有 `--survive-owner` 明确豁免）就自己退出。
* `backend/server.py`
  * `detach_owner=True` 改成**显式开关**（配置项或环境变量），默认沿用「与主人同命」；
    即使显式开启，也必须同时给出租约，让它不会永远活着。
* `backend/tests/conftest.py` + `backend/tests/reaper_daemon_guard.py`
  * 现有的 `_reclaim_reaper_daemons` 会话夹具要**按 worker** 回收本会话启动的守护进程，
    并且要认得 `--survive-owner` 那一批（现在只认 pytest 自己起的那些）。

### L7 验证层

* 定向：§5.1 的 8 条。
* 面：`scripts/run_tests.sh --lane unit` 0 failed。
* 宿主：跑之前把 24 个 daemon 与 232 个残留执行器清掉（**这是运维动作，不是代码改动**），
  否则 gate 自身的 `host measurability` 探针仍会判「宿主不可测」。

## 4. 为什么上一轮方案不能复用

上一轮（`repair-r1-01`，attempt 2/3）的方案是「argv[0] 被 macOS 启动器改写 → 严格身份比对失配 → 计划被停成 stopped → 下次启动 409」，修在 `backend/routes/execution.py`（`_settled_identity` / `_record_still_names_the_process` / `_shutdown_park_needs_phase`）。
本轮 8 条失败里**没有一条**是那三类现象：
没有 409 `already_running`、没有调度器 settle 超时、没有 shutdown checkpoint 状态错；
8 条全是「服务被 SIGTERM 打断 / 连接被拒」。
把上一轮的方案再执行一遍，**一条都不会变绿**。反过来，本轮报告指出的三层（停止路径的身份闸门、就绪等待看死亡、守护进程租约）上一轮**一条都没碰**。

## 5. 验收方式（只选中这次失败的用例）

### 5.1 定向（这 8 条，一条不多一条不少）

~~~
backend/tests/integration/test_quota_parked_plan_status_http.py::test_vp_004_plan_status_reports_parked_phase_and_counts
backend/tests/unit/test_service_address_events.py::test_start_service_records_the_allocation_it_made
backend/tests/unit/test_service_kill_attribution.py::test_a_parked_execution_is_still_answerable_after_the_owning_process_exits
backend/tests/unit/test_service_manager.py::test_start_service_end_to_end
backend/tests/unit/test_service_process_watcher.py::test_health_data_still_answers_after_the_round_records_its_service
backend/tests/unit/test_service_manager.py::test_stopping_a_relocated_service_never_reaches_the_test_session
backend/tests/unit/test_service_kill_attribution.py::test_the_round_s_own_reap_names_itself_as_the_caller
backend/tests/unit/test_service_restart_preserves_fixtures.py::test_the_topup_wait_log_stays_answerable_after_the_round_service_outlives_its_starter
~~~

判定标准：**0 failed**；且 §1.2 的两个反常读数消失 ——
失败行里的秒数 = 实测 duration（不再是「1.9s 的用例报 30s」），且 `rc=-15` 不再出现。

### 5.2 面（phase 1 lane 全绿）

~~~
bash scripts/run_tests.sh --lane unit
~~~

判定：phase 1 `0 failed`、gate 退出码 0。
**前置条件**：先把宿主清干净（24 个 daemon + 232 个残留执行器），
否则 gate 自己的 `host measurability` 探针会把「宿主不可测」判成失败（退出码 3 而不是 1）。

### 5.3 新增回归（本次改动必须自带，落在 §「改动范围」列出的测试文件里）

1. **停止路径不杀已回收的 pid**：造一个 pid 被回收的场景，断言 `kill_service` **没有**发出信号、且返回值/事件里写明「pid 已回收」。
2. **失败行说实话**：服务启动后立刻被 SIGTERM，断言失败行里的秒数是**实际等待**（不是声明超时），并且写明是 SIGTERM。
3. **就绪等待看得见死亡**：子进程启动即退出，断言等待在**几秒内**结束（而不是烧满声明预算）。
4. **守护进程事件日志有上限**：连续多轮扫描后，断言 `kill_events.jsonl` 的大小有界 / 跳过项是聚合计数而不是每进程一行。
5. **`detach_owner` 是显式开关**：默认配置下守护进程不活得比主人久。

### 5.4 负控

* 用**未改**的 `backend/service_manager.py` 跑 5.3 的第 1 条 ⇒ 必须红（证明回归测试真的钉住了这个缺陷）。
* 用**未改**的 `backend/orphan_reaper.py` 跑 5.3 的第 4 条 ⇒ 必须红。

## 改动范围

**先说一件必须交代的事**：任务简报里的「改动范围」字段写的是 `backend/example_scope.py`，
但**这个文件在仓里不存在**（`ls backend/example_scope.py` → `No such file or directory`；
主检出与 worktree `worktrees/reaper-leak` 两侧都没有；`find . -name "example_scope*"` 无命中）。
它是一个占位名，不是真实靶点。因此下面的清单**是按本轮证据真正指向的代码路径**列出来的，
每一条都对应正文里的一条负控（§1.3–§1.9），而不是照抄简报里的占位名。

- `backend/service_manager.py`
- `backend/process_identity.py`
- `backend/orphan_reaper.py`
- `backend/server.py`
- `backend/tests/conftest.py`
- `backend/tests/reaper_daemon_guard.py`
- `backend/tests/unit/test_service_manager.py`
- `backend/tests/unit/test_service_kill_attribution.py`
- `backend/tests/unit/test_orphan_reaper_daemon_budget.py`
- `backend/tests/unit/test_server_orphan_reaper_wiring.py`

对应的改动分工：

| 文件 | 承担 |
|---|---|
| `backend/service_manager.py` | L1 身份闸门、L2 锚点提前、L3 等待看死亡 + 失败行说实话、L4 退出原因、L5 落盘请求 |
| `backend/process_identity.py` | L1 复用/补齐唯一的身份判定（`probe` / `matches` / `CREATE_TIME_TOLERANCE`） |
| `backend/orphan_reaper.py` | L6 事件日志有上限 + 守护进程租约 |
| `backend/server.py` | L6 `detach_owner=True` 改显式开关 |
| `backend/tests/conftest.py`、`backend/tests/reaper_daemon_guard.py` | L6 按 worker 回收本会话的守护进程（含 `--survive-owner`） |
| `backend/tests/unit/test_service_manager.py` | 5.3 的 2/3 条回归 + relocated-stop 用例的记录口径 |
| `backend/tests/unit/test_service_kill_attribution.py` | 5.3 的 1 条回归（停止路径不杀已回收的 pid） |
| `backend/tests/unit/test_orphan_reaper_daemon_budget.py` | 5.3 的 4 条回归（日志上限、租约） |
| `backend/tests/unit/test_server_orphan_reaper_wiring.py` | 5.3 的 5 条回归（`detach_owner` 默认值） |

**不在范围内**（明确写出来，免得实现任务去改）：
`backend/routes/execution.py`（上一轮针对旧根因的修复面）、
`backend/process_ledger.py`（reaping 路径的身份闸门已经有了）、
`backend/orphan_reaper.py` 的 `classify` 判定阶梯（`owner_alive` 的跳过是对的）。
**宿主清理（杀 daemon、杀残留执行器）是运维动作，不是代码改动。**

## 6. 遗留与不确定性

1. **「哪一个 runtime 发出了那一次误杀」没有落到具体行号。**
   本报告证明了凶手**在仓内**（§1.4：只有 `kill_service`/`stop_service` 能写出「stopped by the round」），
   也证明了停止路径**没有身份闸门**（§1.5）、**锚点歪 5 秒**（§1.6）；
   但因为停止请求**根本没落盘**（§1.8），事后无法从磁盘上还原那一次具体的调用者。
   ⇒ 这正是 L5 必须先做的原因：**先把「谁杀的」写下来，再谈下一次归因。**
2. **误杀的受害者与凶手是不是同一个 worker，未验证。** pid 回收需要进程 churn；本机 232 个残留执行器 + 24 个 daemon 让 churn 足够大，但**受害者与凶手是否同 worker、同一次 teardown**没有证据。
3. **宿主数据是本机快照，不是 CI 机器的快照。** 「24 个 daemon / 1.1 GB / loadavg 40」是本机实测；
   CI 侧只有门禁自己报的那一行（`sleep(0.05)=0.192s, loadavg=4.73`）。L6 的**必要性与量级**以本机为准。
4. **本轮没有跑完整三段门禁。** 本地只跑了 lane 1（phase 2 的 616 个串行 e2e/integration 约 70 分钟、phase 3 的 82 个慢用例在本轮预算内放不下）。
   phase 2/3 在 CI 这一轮是过的，**本报告没有推翻它们的证据**。
5. **`_relocate_service` 的失败清理会发一次组信号**（`backend/service_manager.py:2362`），而 relocated-stop 那条用例断言「每一次组信号都等于最终服务的组」。
   本轮把它读成二阶后果（第一次尝试之所以失败是撞上了同一条误杀链）；**若实现后第一次搬迁仍会发生，这条用例仍需单独处理**，不能算在 L1–L5 的修复面上。
6. **reaper 的事件日志改成有上限，会影响依赖它做事后取证的人。** 这是有意的取舍：1.1 GB 的「每进程每 30 秒一条」不是取证，是噪声；但取舍要由实现任务在 PR 里写清楚。
