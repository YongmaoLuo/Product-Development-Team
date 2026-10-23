"""端到端：state.db 的坏行 → ``/progress`` payload → 卡片。

这条链路上有三层，每一层都可能把坏数据漏出去，所以每层各钉一条：

  * **存储层** —— ``plan_verification`` 这一行里，``progress_state`` 存着
    三个索引列表的**缓存**（写侧顺手写下来的副本），``verdicts`` 存着
    **裁决表**（每个被判过的 VP 一条记录）。两者可以冲突，冲突时真相在
    裁决表：``progress_state`` 里那个 VP 同时出现在 ``completed_vps`` 和
    ``failed_vps`` 里，是历史坏行留下的。
  * **接口层** —— ``GET /api/verification/{id}/progress`` 的三个列表
    **从裁决表派生**，不读缓存（``routes.verification._derive_vp_buckets``）。
  * **展示层** —— 飞书卡片把 ``failed_vps`` 渲染成「❌ 失败 VP」段，把
    ``skipped_vps`` 渲染成「⏭ 跳过 VP」段。接口层漏一个坏 VP 出去，
    运营在卡片上看到的就是一个**不存在的失败**，而且修复环会拿着这份
    失败列表去重跑一个其实早就 SKIPPED 的判据。

负控 B（"改前 ``failed_vps`` 含 VP-003，改后不含且 ``counts.failed``
减一"）写在第一条用例里：同一行坏数据、同一张表，先在**还没有裁决**时
取一次 payload（改动前的行为 —— 缓存列表被原样端出去），补上裁决后再取
一次（改动后的行为）。两次读的是同一份数据，所以这个差不可能是夹具
自己造出来的。

第三条用例起的是**真进程**：``TestClient`` 走的是进程内的 ASGI 调用，
它证明不了「另一个进程读同一张表时看到的是什么」。所以它用 subprocess
拉起一个真的 uvicorn，用真的 HTTP 打一次，然后把进程收掉。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

# ``backend/`` 在 ``sys.path`` 上，``server`` / ``notifications.cards``
# 才 import 得到（与同目录其它用例同一套路）。
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from notifications.cards import build_verification_card  # noqa: E402
from server import app  # noqa: E402


#: 计划 id。三条用例各用各的 tmp 目录与 ``state.db``，所以同一个 id
#: 也不会串数据；这里只把它写成一个常量，让失败时的日志一眼能看出是
#: 哪一个计划。
_PLAN_ID = "20261023-vp-bucket-e2e"

#: 真进程那条用例的端口。任务书指定的就是这个号。
_LIVE_PORT = 8124


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    """A FastAPI TestClient bound to the real ``server.app``."""
    return TestClient(app)


def _write_plan(
    plan_dir: Path,
    *,
    plan_id: str,
    vps: List[Dict[str, Any]],
) -> None:
    """Write the plan's ``verification_plan.json`` (the VP universe)."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(
            {"plan_id": plan_id, "verification_points": vps},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _open_repository(state_db: Path) -> Tuple[Any, Any]:
    """Open the state database and hand back ``(conn, VerificationRepository)``."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    state_db.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(state_db)
    migrate(conn)
    return conn, VerificationRepository(conn)


def _write_row(
    state_db: Path,
    plan_id: str,
    *,
    progress_state: Dict[str, Any],
) -> None:
    """Write the ``plan_verification`` row (progress_state only, no verdict).

    写入走的是执行器用的同一个 repository，所以列的形状与生产一致。
    这一行**故意不带裁决** —— 这是「改动前」的世界：端点只能读缓存的
    三个列表，坏行被原样端出去。
    """
    conn, repo = _open_repository(state_db)
    try:
        repo.insert(
            plan_id,
            verification_status="running",
            progress_state=progress_state,
            verdicts=[],
        )
    finally:
        conn.close()


def _append_verdict(state_db: Path, plan_id: str, verdict: Dict[str, Any]) -> None:
    """Append one verdict to the row (the write side's own entry point)."""
    conn, repo = _open_repository(state_db)
    try:
        repo.append_verdict(plan_id, verdict)
    finally:
        conn.close()


def _read_row(state_db: Path, plan_id: str) -> Dict[str, Any]:
    """Read the stored row back — the fixture's own receipts."""
    conn, repo = _open_repository(state_db)
    try:
        return dict(repo.summary(plan_id) or {})
    finally:
        conn.close()


def _corrupt_progress_state() -> Dict[str, Any]:
    """任务书给定的坏行：同一个 VP 同时在 completed 和 failed 里。"""
    return {
        "current_vp": None,
        "completed_vps": ["VP-003"],
        "failed_vps": ["VP-003"],
        "skipped_vps": [],
        "updated_at": "2026-10-23T10:00:00.000000Z",
    }


def _vp(vp_id: str, title: str = "某个判据") -> Dict[str, Any]:
    return {
        "id": vp_id,
        "title": title,
        "verification_method": "automated_test",
        "layer": 1,
    }


def _get_progress(client: TestClient, plan_id: str) -> Dict[str, Any]:
    response = client.get(f"/api/verification/{plan_id}/progress")
    assert response.status_code == 200, response.text
    return response.json()


def _card_text(card: Dict[str, Any]) -> str:
    """Every text node of a Feishu card, joined.

    飞书卡片 JSON 是嵌套 dict/list，正文都在 ``content`` 键上；把整棵
    树走一遍取出来，断言就不必知道卡片内部的结构。
    """
    chunks: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "content" and isinstance(value, str):
                    chunks.append(value)
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(card)
    return "\n".join(chunks)


def _port_is_free(port: int) -> bool:
    """True when ``port`` can be bound on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port() -> int:
    """Ask the kernel for an unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_healthy(
    port: int,
    proc: subprocess.Popen,
    log_path: Path,
    timeout: float = 90.0,
) -> None:
    """Block until the spawned server answers ``/health`` with 2xx."""
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"the spawned server exited before it became healthy "
                f"(rc={proc.returncode})\n"
                f"--- server log tail ---\n{_log_tail(log_path)}"
            )
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{port}/health", timeout=2
            ) as resp:
                if 200 <= resp.status < 300:
                    return
                last_error = f"status {resp.status}"
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last_error = repr(exc)
        time.sleep(0.25)
    raise AssertionError(
        f"the spawned server did not become healthy on port {port} within "
        f"{timeout}s: {last_error}\n"
        f"--- server log tail ---\n{_log_tail(log_path)}"
    )


def _log_tail(log_path: Path, limit: int = 2000) -> str:
    """The tail of the spawned server's log — the only place its death explains itself."""
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "<server log unavailable>"


def _http_get_json(url: str) -> Dict[str, Any]:
    """GET ``url`` over real HTTP and decode the JSON body.

    ``/api/`` 下的路径有来源守卫（``request_guard``）：不带
    ``X-PDT-Request`` 头一律 403。所以这里必须像本仓库自己的脚本那样
    把这个头带上 —— 否则断言读到的是 403 的 JSON，不是 payload。
    """
    request = urllib.request.Request(url, headers={"X-PDT-Request": "1"})
    with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310
        assert 200 <= resp.status < 300, resp.status
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 1. 接口层：坏行 → 干净 payload
# ---------------------------------------------------------------------------


def test_corrupted_row_api_returns_clean_payload(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """坏行不再被上报成失败 —— 含负控 B 的前后对照。

    同一行坏数据、同一张表，读两次：

      * **改前**（这一行还没有裁决）：端点只能读 ``progress_state`` 的
        缓存列表，坏行被原样端出去 —— ``failed_vps`` 含 VP-003，
        ``counts.failed`` 是 1。
      * **改后**（补上一条 ``SKIPPED`` 的裁决）：三个列表从裁决表派生
        —— ``failed_vps`` 是空，``skipped_vps`` 是 ``["VP-003"]``，
        ``counts.failed`` 是 0。

    差值就是这条修复的全部作用：``counts.failed`` 减一。
    """
    plan_dir = tmp_path / "plans" / _PLAN_ID
    state_db = tmp_path / "state.db"
    _write_plan(plan_dir, plan_id=_PLAN_ID, vps=[_vp("VP-003")])
    _write_row(state_db, _PLAN_ID, progress_state=_corrupt_progress_state())

    # --- 负控 B 的「改前」读数 -------------------------------------------
    before = _get_progress(client, _PLAN_ID)
    assert before["failed_vps"] == ["VP-003"], (
        "the pre-verdict payload is the control reading: the cached list "
        "is served verbatim. If this fails, the control is gone and the "
        "post-verdict assertion below proves nothing."
    )
    assert before["counts"]["failed"] == 1

    # 坏行确实是坏的 —— 夹具没有偷偷把数据造干净。
    stored = _read_row(state_db, _PLAN_ID)
    assert stored["progress_state"]["failed_vps"] == ["VP-003"]
    assert stored["progress_state"]["completed_vps"] == ["VP-003"]

    # --- 真相到位：裁决说这个 VP 是 SKIPPED ------------------------------
    _append_verdict(state_db, _PLAN_ID, {"vp_id": "VP-003", "status": "SKIPPED"})

    payload = _get_progress(client, _PLAN_ID)

    assert payload["failed_vps"] == []
    assert payload["skipped_vps"] == ["VP-003"]
    assert payload["counts"]["failed"] == 0
    assert payload["counts"]["skipped"] == 1
    # 任务书要求的「减一」。
    assert payload["counts"]["failed"] == before["counts"]["failed"] - 1


# ---------------------------------------------------------------------------
# 2. 展示层：同一份 payload → 卡片
# ---------------------------------------------------------------------------


def test_card_text_has_no_failed_vp(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """卡片上不再有 VP-003 的失败项。

    断言不是「卡片里没有失败段」这种空话 —— 同一份数据先渲染一次
    **改前**的 payload，那张卡片**确实**把 VP-003 渲染成了失败项
    （``🔴 `VP-003```）；换成**改后**的 payload，同一个 VP 出现在跳过段，
    失败段里没有它。两张卡片的差别就是这条修复在用户面前的样子。
    """
    plan_dir = tmp_path / "plans" / _PLAN_ID
    state_db = tmp_path / "state.db"
    _write_plan(plan_dir, plan_id=_PLAN_ID, vps=[_vp("VP-003")])
    _write_row(state_db, _PLAN_ID, progress_state=_corrupt_progress_state())

    stale_payload = _get_progress(client, _PLAN_ID)
    _append_verdict(state_db, _PLAN_ID, {"vp_id": "VP-003", "status": "SKIPPED"})
    payload = _get_progress(client, _PLAN_ID)

    stale_text = _card_text(build_verification_card(_PLAN_ID, stale_payload))
    card_text = _card_text(build_verification_card(_PLAN_ID, payload))

    # 非空对照：卡片**能**渲染这个 VP 的失败项（改前那张卡）。
    assert "VP-003" in stale_text
    assert "🔴 **`VP-003`**" in stale_text, (
        "the control card must show VP-003 as a failure — otherwise the "
        "assertion below would pass on a card that never mentions the VP"
    )

    # 改后：失败段里没有它，跳过段里有它。
    assert "🔴 **`VP-003`**" not in card_text
    assert "❌ 失败 VP" not in card_text
    assert "[VP-003]" in card_text
    assert "⏭ 跳过 VP" in card_text


# ---------------------------------------------------------------------------
# 3. 真进程：另一个进程读同一张表
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_live_server_endpoint_ignores_stale_list(tmp_path: Path) -> None:
    """真的起一个 uvicorn，用真的 HTTP 打一次，再把进程收掉。

    ``TestClient`` 走的是进程内的 ASGI 调用 —— 它证明不了「另一个进程
    读同一张 state.db 时看到的是什么」。这条用例起一个真的服务进程：
    它有自己的 ``PLANS_DIR`` 与 ``state.db``（都指到本用例的 tmp 目录），
    自己读那一行坏数据，自己把 payload 端出来。

    端口：任务书指定 8124。那个号已经被别的服务占着时退回内核分配的
    临时端口 —— 宁可换一个端口，也好过对着别人的服务做断言。
    """
    plans_root = tmp_path / "plans"
    state_db = tmp_path / "state.db"
    plan_dir = plans_root / _PLAN_ID
    _write_plan(plan_dir, plan_id=_PLAN_ID, vps=[_vp("VP-003")])
    _write_row(state_db, _PLAN_ID, progress_state=_corrupt_progress_state())
    _append_verdict(state_db, _PLAN_ID, {"vp_id": "VP-003", "status": "SKIPPED"})

    port = _LIVE_PORT if _port_is_free(_LIVE_PORT) else _free_port()

    # 子进程的环境是**白名单**，不是父进程环境的拷贝。这不是洁癖：
    # 父进程里可能带着 ``PDT_SECRET_FD_*``（密钥以文件描述符的形式
    # 交给子进程读），而那些描述符的编号在子进程里指向的是别的东西 ——
    # asyncio 的事件循环把自己的 self-pipe 放在最低的两个可用 fd 上，
    # 正好撞上。子进程于是死在 ``select()`` 的 ``EBADF`` 上，而且死在
    # 启动之后（凭据那套代码先把 fd 关了），所以健康检查只看到一句
    # 「没起来」。少传这两个变量，子进程就干净了。
    #
    # ``HOME`` 也不传：没有它就没有钥匙串，通知器直接判定「未配置」，
    # 免得这条用例去动操作者的真实密钥链。
    env = {
        "PATH": os.environ.get("PATH", ""),
        # ``prompts.py`` 要 ``backend.framework.prompts``，所以仓库根要
        # 在 sys.path 上；只放 backend/ 的话子进程在 import 期就死了。
        "PYTHONPATH": str(_BACKEND_DIR.parent),
        "PDT_PLANS_DIR": str(plans_root),
        "PDT_STATE_DB_PATH": str(state_db),
    }

    server_log_path = tmp_path / "uvicorn.log"
    server_log = open(server_log_path, "w+b")  # noqa: SIM115
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(_BACKEND_DIR),
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_healthy(port, proc, server_log_path)
        payload = _http_get_json(
            f"http://127.0.0.1:{port}/api/verification/{_PLAN_ID}/progress"
        )

        assert payload["plan_id"] == _PLAN_ID, (
            f"the response came from a server that does not know this plan "
            f"(plan_id={payload.get('plan_id')!r}); port {port} was not ours"
        )
        assert payload["failed_vps"] == []
        assert payload["skipped_vps"] == ["VP-003"]
        assert payload["counts"]["failed"] == 0
        assert payload["counts"]["skipped"] == 1
    finally:
        # 收进程：terminate → wait，超时再 kill → wait。不能只 terminate
        # 就走 —— 那会留下一个还在监听端口的僵尸，下一次跑的时候对着
        # 它做断言。
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
        server_log.close()