"""真进程上的任务重置：端到端验收（2026-11-13）。

为什么要有这一份
----------------
任务 1-8 的用例都在 ``TestClient`` 里跑：同一个进程、同一份内存里的
``_execution_state``、一个从没退出过的后端。它们证明的是**路由与仓储**
的契约 —— 「这个端点会写哪些字段」「这条拒绝长什么样」。

它们没有证明的是另一件事：**一次真的重置能走完**。一个真的
``backend/server.py`` 进程、真的 ``state.db``、真的 ``plans/`` 目录，
被**另一个进程**用 HTTP 打一次 —— 这条路上还有三段 ``TestClient``
走不到：

* 进程边界：子进程读的是**磁盘**，不是父进程的那份内存；
* 环境解析：``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH`` / ``PDT_PORT`` /
  ``PDT_SCHEDULER_FILE`` 这四个变量，服务端是不是真的认；
* 就绪：服务起来了没有 —— 端口在听、``/api/*`` 答 200。

这一份把那三段补上。

钉住的六件事
------------
每条断言对应一个 KPI，全部落在**可复跑的代码**里（截图只是附加证据）：

1. ``POST /api/execution/{plan_id}/tasks/{task_id}/reset`` → 200，
   ``task.status == "pending"``、``cleared.status == "failed"``；
2. ``GET /api/execution/{plan_id}/progress`` → 那条任务是 ``pending``
   （活进程读到的是**新行**，不是它启动时读到的那一行）；
3. 另开一个连接读磁盘上的 ``plan_tasks`` → ``status='pending'``、
   ``attempt=0``、``failure_reason`` / ``end_ts`` / ``commit_sha`` 全 NULL；
4. ``plans/<plan_id>/task_reset_log.json`` 恰好一条，``from_status`` 是
   ``failed``、``to_status`` 是 ``pending``、``caller`` 是 ``playwright``、
   ``reason`` 是 ``e2e acceptance``；
5. 再发一次同样的请求 → 200 且 ``noop is True``，审计**仍然**只有一条；
6. ``GET /api/plan/{plan_id}/summary`` 的 ``current_phase`` 前后都是
   ``failed`` —— 这个接口**不放行计划**。

证据从哪来
----------
复跑命令（在 ``backend/`` 目录下）::

    python3 -m pytest tests/e2e/test_task_reset_live_server_e2e.py -v

这一份还留了**浏览器那一半**的证据：同一个 ``server.py`` 进程、同一个
种子，用 Playwright 在页面里发同一条请求（``X-PDT-Caller: playwright``），
截图落在 ``/tmp/pdt-e2e-reset/reset.png``。截图**不作为断言依据** ——
它证明的是「真实浏览器发的那一次，落进的是同一个审计文件」。

为什么子进程必须拿到这五个环境变量
------------------------------------
* ``PDT_PLANS_DIR`` —— 计划目录的根。不给就写进仓库的 ``plans/``，
  那是有静态门禁盯着的地方。
* ``PDT_STATE_DB_PATH`` —— 状态库的路径（``server._state_db_path``
  的第一优先级）。不给就开的是**操作者的** ``state.db``。
* ``PDT_PORT`` —— 端口。默认 8000 上可能已经有服务在跑。
* ``PDT_SCHEDULER_FILE`` —— 指向 ``scheduler: {enabled: false}`` 的
  那个文件，调度器不许在这个进程里替我们启动什么。
* ``AC_DISABLE_KEYCHAIN_SECRETS=1`` —— 不许去碰钥匙串。

另外还剔除了继承来的 ``PDT_DEBUG_ROUTES``（调试路由不归这条链路管），
并把 ``PDT_ORPHAN_REAPER`` 关掉：这个进程没有孤儿可收，而收孤儿要
扫遍整台机器。

共享一个进程
------------
5 条用例共用一个 server（``scope="module"``）。5 次冷启动加 5 次就绪
轮询会把时长预算吃光，而这里要验的**不是**启动有多快。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterator

import httpx
import pytest

from tests.fixtures import task_reset_env as env

pytestmark = [pytest.mark.e2e]

#: ``backend/`` —— ``server.py`` 在这儿，子进程的工作目录也是这儿。
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: 这条计划与这条任务。种子和断言用的是同一对 id。
PLAN_ID = env.DEFAULT_PLAN_ID
TASK_ID = "1"

#: 重置请求的体。``reason`` 会原样落进审计条目。
RESET_BODY: Dict[str, Any] = {"reason": "e2e acceptance"}

#: 重置请求的头。``X-PDT-Request`` 是 CSRF 守卫（缺了就是 403），
#: ``X-PDT-Caller`` 是**调用方身份**（守卫那个头的值恒为 "1"，
#: 拿它当身份会让所有调用方都记成同一个名字）。
RESET_HEADERS: Dict[str, str] = {
    "X-PDT-Request": "1",
    "X-PDT-Caller": "playwright",
    "Content-Type": "application/json",
}

#: 只读查询也要带守卫头 —— ``/api/*`` 全部受 ``backend/request_guard.py`` 管。
READ_HEADERS: Dict[str, str] = {"X-PDT-Request": "1"}

#: 审计日志的文件名（落在 plan 目录下）。
RESET_LOG_FILENAME = "task_reset_log.json"


# ---------------------------------------------------------------------------
# The live server
# ---------------------------------------------------------------------------


def _server_output(log_path: Path, limit: int = 4000) -> str:
    """服务进程自己的输出（就绪失败时它说明为什么）。"""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - the file is ours
        return "(no server output was captured)"
    return text[-limit:]


def _client(base_url: str) -> httpx.Client:
    """一个只去本机的 HTTP 客户端。

    ``trust_env=False``：``HTTP_PROXY`` 之类的环境变量不该把一个
    本机回环请求送去某个代理 —— 那会把「服务答不答」量成代理的 502。
    ``Host`` 头是 ``127.0.0.1:<port>``，请求守卫认它。
    """
    return httpx.Client(base_url=base_url, timeout=30.0, trust_env=False)


@pytest.fixture(scope="module")
def live_server(tmp_path_factory) -> Iterator[Dict[str, Any]]:
    """一个真的 ``server.py`` 进程，整个模块共用。

    三件事按这个顺序发生：

    1. ``live_server_env`` 在 ``tmp_path`` 下摆好 ``plans/``、
       ``state.db`` 的位置与 ``scheduler.yaml``，并挑一个**空闲端口**；
    2. 进程起起来（``Popen``），种子跟着种进那棵 ``plans/`` 和那份
       ``state.db``；
    3. ``wait_for_server`` 等它真的开始回答。

    种子在 ``Popen`` 之后、就绪之前：**服务还没开始服务**，所以种子里
    建表（``migrate``）与服务端启动时自己建表不会撞在一起；等
    ``wait_for_server`` 返回，服务端读到的已经是种好的那份状态。

    收尾：``terminate()`` → ``wait(15)`` → 还活着就 ``kill()``。
    """
    work = tmp_path_factory.mktemp("e2e-reset")
    child_env, base_url = env.live_server_env(work)
    plans_dir = Path(child_env["PDT_PLANS_DIR"])
    db_path = Path(child_env["PDT_STATE_DB_PATH"])
    plan_dir = plans_dir / PLAN_ID

    log_path = work / "server.log"
    handle = log_path.open("wb")
    proc = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=str(BACKEND_DIR),
        env=child_env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    try:
        # 种子：一条失败过的任务 + 一行执行记录。
        #
        # ``plan_execution`` 那一行是**必须**的：``/progress`` 的 404 门槛
        # 就是「没有执行记录」，少这一行那个端点根本不会回答计划。
        # 用 ``stopped`` 而不是 ``running`` —— 后者会撞上「活着的执行器
        # 持有这条任务」的守卫。
        env.seed_plan(
            plans_dir,
            db_path,
            plan_id=PLAN_ID,
            task_id=TASK_ID,
            status="failed",
            attempt=3,
            commit_sha="a" * 40,
            phase="failed",
            project_dir=str(plan_dir),
        )
        env.seed_execution(
            db_path,
            PLAN_ID,
            exec_status="stopped",
            project_dir=str(plan_dir),
        )

        try:
            env.wait_for_server(base_url)
        except RuntimeError as exc:
            raise RuntimeError(
                f"{exc}\n--- server output ---\n{_server_output(log_path)}"
            ) from exc

        yield {
            "base_url": base_url,
            "plans_dir": plans_dir,
            "db_path": db_path,
            "plan_dir": plan_dir,
            "log_path": log_path,
            # 重置**之前**的相位 —— 断言 6 的「前」就是这一读。
            "phase_before": _current_phase(base_url),
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
        handle.close()


def _current_phase(base_url: str) -> str:
    """``GET /api/plan/{plan_id}/summary`` 里的 ``current_phase``。

    相位住在 ``summary`` 的 ``state`` 块里 —— 那是 ``PlanState`` 从
    ``plan_routing`` 那一行读出来的答案。
    """
    with _client(base_url) as client:
        response = client.get(
            f"/api/plan/{PLAN_ID}/summary", headers=READ_HEADERS
        )
    assert response.status_code == 200, response.text
    return str(response.json()["state"]["current_phase"])


@pytest.fixture(scope="module")
def first_reset(live_server) -> httpx.Response:
    """整个模块里**唯一一次**真重置。

    放在夹具里而不是某条用例里，是为了让 5 条用例**各自都能单跑**：
    「再发一次是 no-op」那条用例自己跑的时候，前面那次重置也得发生过
    —— 否则它量到的是第一次调用，不是第二次。

    请求与浏览器里那一次**逐字相同**（同一个体、同一个 ``caller``），
    所以无论哪一次先落地，审计条目都是同一组字段。
    """
    with _client(live_server["base_url"]) as client:
        return client.post(
            f"/api/execution/{PLAN_ID}/tasks/{TASK_ID}/reset",
            json=RESET_BODY,
            headers=RESET_HEADERS,
        )


def _reset_log(live_server) -> list:
    """读审计日志的原始 list。"""
    path = live_server["plan_dir"] / RESET_LOG_FILENAME
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, list), raw
    return raw


# ---------------------------------------------------------------------------
# 1) 主路径
# ---------------------------------------------------------------------------


def test_live_server_reset_returns_pending(first_reset):
    """断言 1：HTTP 200，任务回到 ``pending``，清掉的是 ``failed``。

    这是整个接口存在的原因，而且这一条是在**真的服务进程**上量的：
    请求是另一个进程用 HTTP 打的，响应里的 ``cleared`` 是重置**之前**
    那一行的原值。
    """
    assert first_reset.status_code == 200, first_reset.text
    body = first_reset.json()
    assert body["task"]["status"] == "pending", body
    assert body["cleared"]["status"] == "failed", body
    # 输出示例里还带着这两项：不是 no-op，且确实清掉了重试预算。
    assert body["noop"] is False, body
    assert body["cleared"]["attempt"] == 3, body


# ---------------------------------------------------------------------------
# 2) 活进程读到的是新行
# ---------------------------------------------------------------------------


def test_live_server_progress_shows_pending_after_reset(live_server, first_reset):
    """断言 2：``/progress`` 里那条任务是 ``pending``。

    这一条量的是**进程边界**：服务端在别的进程里启动过，它读到的是
    磁盘上那一行 —— 重置写进去的那一行，不是它启动时读到的那一行。

    ``first_reset`` 在这里是被**依赖**的，不是被断言的：没有那次重置，
    这一条量到的是一条还没跑过的任务（``pending`` 但不是被重置出来的
    ``pending``）。
    """
    with _client(live_server["base_url"]) as client:
        response = client.get(
            f"/api/execution/{PLAN_ID}/progress", headers=READ_HEADERS
        )

    assert response.status_code == 200, response.text
    body = response.json()
    tasks = {str(task.get("id")): task for task in body.get("tasks", [])}
    assert TASK_ID in tasks, body
    assert tasks[TASK_ID].get("status") == "pending", tasks[TASK_ID]


# ---------------------------------------------------------------------------
# 3) 磁盘上的那一行
# ---------------------------------------------------------------------------


def test_live_server_sqlite_row_is_cleared(live_server, first_reset):
    """断言 3：另开一个连接读 ``state.db``，五个记账字段都清干净了。

    走的是**裸 SQL**：``read_raw_row`` 自己开一条连接，绕开服务进程
    手里那条。这一条证明的是「写**落盘**了」，而不是「服务进程自己
    看得见」。

    ``first_reset`` 是被**依赖**的 —— 那一行得先被重置过，才有东西可清。
    """
    row = env.read_raw_row(live_server["db_path"], PLAN_ID, TASK_ID)
    assert row is not None, (
        f"重置必须在库里留下 plan {PLAN_ID!r} 的 task {TASK_ID!r} 这一行"
    )
    assert row["status"] == "pending", row
    assert row["attempt"] == 0, row
    assert row["failure_reason"] is None, row
    assert row["end_ts"] is None, row
    assert row["commit_sha"] is None, row


# ---------------------------------------------------------------------------
# 4) 审计
# ---------------------------------------------------------------------------


def test_live_server_writes_one_audit_entry(live_server, first_reset):
    """断言 4：审计文件恰好一条，四個字段都对得上。

    审计写在**计划目录**里，由服务进程写。它是「谁、为什么、把什么
    从什么状态改成了什么状态」的唯一落点 —— 没有它，一次重置就是一次
    不明不白的擦除。

    ``first_reset`` 是被**依赖**的 —— 审计条目是那一次重置写下的。
    """
    entries = _reset_log(live_server)
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["from_status"] == "failed", entry
    assert entry["to_status"] == "pending", entry
    assert entry["caller"] == "playwright", entry
    assert entry["reason"] == "e2e acceptance", entry
    assert entry["task_id"] == TASK_ID, entry


# ---------------------------------------------------------------------------
# 5) 第二次调用是 no-op；6) 相位不变
# ---------------------------------------------------------------------------


def test_live_server_second_call_is_a_noop(live_server, first_reset):
    """断言 5：再发一次是 no-op，审计不增长。

    断言 6：计划的相位**前后都是** ``failed`` —— 这个接口重置的是一条
    任务，不放行整个计划。要放行计划得自己走
    ``POST /api/plan/{plan_id}/state``。

    ``first_reset`` 是被**依赖**的 —— 这一条量的是**第二次**调用，
    得先有第一次。
    """
    # 「前」：重置之前读的那一次（夹具里读的）。
    assert live_server["phase_before"] == "failed", live_server["phase_before"]

    with _client(live_server["base_url"]) as client:
        response = client.post(
            f"/api/execution/{PLAN_ID}/tasks/{TASK_ID}/reset",
            json=RESET_BODY,
            headers=RESET_HEADERS,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["noop"] is True, body

    # no-op 不写审计 —— 「一次没有发生的重置」留在审计里比没有更糟。
    entries = _reset_log(live_server)
    assert len(entries) == 1, entries

    # 「后」：再读一次相位，还是 failed。
    assert _current_phase(live_server["base_url"]) == "failed"
