"""端到端验收：真 server 进程 + 真裁决 → ``/progress`` 的失败原因可读。

背景
----
缺陷 4 改的是 ``/progress`` 的**字段语义**（失败原因改为与失败桶同源），
前三个缺陷改的是框架内部行为。任务 1-7 之后需要一条**端到端**验收：
真进程、真 HTTP、真数据，把三件事一次说清——

  * ``counts.failed == 2`` —— 一条 FAILED + 一条 BLOCKED。BLOCKED 仍在
    失败桶里（桶规则不变：验不下去也得有人去修）；
  * 每个 failed VP 的 ``actual_result`` **非空** —— 原因可读；
  * 被 block 的那一条 ``actual_result`` 含 ``BLOCKED`` —— 分得清「验不
    下去」和「验了没过」。

为什么一定要起真进程
--------------------
``TestClient`` 走的是进程内的 ASGI 调用，它证明不了「**另一个进程**读
同一张 ``state.db`` 时看到的是什么」—— 而失败原因的来源（裁决表）恰恰
是那份跨进程的数据。所以这里起一个真的 uvicorn 子进程，用真的 HTTP 打
一次，然后把进程收掉。

判定依据只有 httpx 拿到的响应 JSON：不用浏览器、不用截图、不用任何 MCP
工具 —— 那是本仓库里另一条路（前端 E2E），不是这条的证据。

进程纪律
--------
server 子进程是 **function 级 fixture**：每个用例各自起停，不共享 module
级实例。回收在 ``finally`` 里做（terminate → wait(10s) → 超时 kill →
wait），回收后断言端口已释放 —— 这条用例不得留下任何常驻进程。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import httpx
import pytest

#: ``backend/`` 在 ``sys.path`` 上，``state_machine.*`` / ``server`` 才
#: import 得到（与同目录其它用例同一套路）。
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: 计划 id。每个用例各有各的 tmp 目录与 ``state.db``，所以同一个 id
#: 也不会串数据；写成一个常量是为了失败时的日志一眼能认出是哪一个计划。
_PLAN_ID = "20261023-e2e-progress"

#: 真进程的请求头。``/api/`` 下的路径有来源守卫（``request_guard``）：
#: 不带 ``X-PDT-Request`` 一律 403 —— 读到的是守卫的 JSON，不是 payload。
_REQUEST_HEADERS = {"X-PDT-Request": "1"}

#: 服务就绪的等待上限（秒）。超时即断言失败，并把服务日志的尾巴打出来。
_READY_TIMEOUT = 30.0

#: 进程回收的等待上限（秒）。
_STOP_TIMEOUT = 10.0

#: server 在日志里报出自己绑定的端口。端口被抢走时 server 会自己往后
#: 退，所以**真正监听的端口**要从它嘴里问，不能只信自己申请的那个。
_BOUND_PORT_RE = re.compile(r"Starting uvicorn on \S+:(\d+)")

#: 两条种子裁决。形状与生产写侧一致：``{"vp_id", "status", "reasons",
#: "evidence"}``（见 ``verification_executor.REQUIRED_VERDICT_KEYS``）。
#:
#: 第二条的 reason 走 ``blocked_reason()`` 的统一前缀 —— 那是任务 7 的
#: 产出点之一，「验不下去」必须读得出来。
_FAILED_VERDICT: Dict[str, Any] = {
    "vp_id": "VP-001",
    "status": "FAILED",
    "reasons": ["suite 3 failed"],
    "evidence": {"pytest": "3 failed, 12 passed"},
}
_BLOCKED_VERDICT: Dict[str, Any] = {
    "vp_id": "VP-002",
    "status": "BLOCKED",
    "reasons": ["BLOCKED — service not ready"],
    "evidence": {"service": "verification service never became ready"},
}


# ---------------------------------------------------------------------------
# 夹具：计划文件 + 种子裁决
# ---------------------------------------------------------------------------


def _write_plan(plan_dir: Path) -> None:
    """Write the plan's ``verification_plan.json`` (the VP universe)."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(
            {
                "plan_id": _PLAN_ID,
                "verification_points": [
                    {
                        "id": "VP-001",
                        "title": "第一个判据",
                        "method": "automated_test",
                        "layer": 1,
                    },
                    {
                        "id": "VP-002",
                        "title": "第二个判据",
                        "method": "automated_test",
                        "layer": 1,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _seed_verdicts(state_db: Path) -> None:
    """Write the ``plan_verification`` row and the two verdicts.

    走的是执行器用的同一个 repository，所以列的形状与生产一致：
    ``insert`` 建行（``progress_state`` 非空，否则端点按「还没开始验证」
    回答 404），``append_verdict`` 一条一条追加裁决。
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    state_db.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(state_db)
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(
            _PLAN_ID,
            verification_status="running",
            progress_state={
                "current_vp": None,
                "completed_vps": [],
                "failed_vps": [],
                "skipped_vps": [],
                "updated_at": "2026-10-23T10:00:00Z",
            },
            verdicts=[],
        )
        repo.append_verdict(_PLAN_ID, dict(_FAILED_VERDICT))
        repo.append_verdict(_PLAN_ID, dict(_BLOCKED_VERDICT))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 夹具：真的 server 子进程
# ---------------------------------------------------------------------------


class _LiveServer:
    """One real server subprocess, and the port it is actually listening on."""

    def __init__(
        self,
        proc: subprocess.Popen,
        requested_port: int,
        log_path: Path,
        log_handle: Any = None,
    ) -> None:
        self.proc = proc
        self.requested_port = requested_port
        self.port = requested_port
        self.log_path = log_path
        self.log_handle = log_handle
        self._stopped = False

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:{0}".format(self.port)

    def stop(self) -> None:
        """Reclaim the process — terminate, wait, kill, wait.

        幂等：夹具的 ``finally`` 与用例里的显式回收都会调它，第二次是
        空操作。
        """
        if self._stopped:
            return
        self._stopped = True
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=_STOP_TIMEOUT)
        if self.log_handle is not None:
            try:
                self.log_handle.close()
            except OSError:
                pass

    def progress(self) -> Dict[str, Any]:
        """GET ``/api/verification/{id}/progress`` over real HTTP."""
        response = httpx.get(
            "{0}/api/verification/{1}/progress".format(self.base_url, _PLAN_ID),
            headers=_REQUEST_HEADERS,
            timeout=10.0,
        )
        assert response.status_code == 200, response.text
        return response.json()


def _acquire_port() -> int:
    """Ask the kernel for a free loopback port.

    端口一律现取，**不写死常量**：写死的号会被别的服务占着，而对着别人
    的服务做断言等于什么都没验。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _child_env(plans_root: Path, state_db: Path, port: int) -> Dict[str, str]:
    """The environment the server subprocess runs with.

    白名单，不是父进程环境的拷贝 —— 与同目录 ``test_vp_bucket_progress_e2e``
    起真 server 的做法一致。四条理由，每条都对应一个「不带就出事」的地方：

    * **不带 ``PDT_SECRET_FD_*``。** 那些变量把密钥以文件描述符的形式
      交出去，而描述符的编号在子进程里指向的是别的东西（asyncio 的
      事件循环把自己的 self-pipe 放在最低的两个可用 fd 上，正好撞上），
      子进程会死在 ``select()`` 的 ``EBADF`` 上 —— 而且死在启动之后。
    * **不带 ``HOME``。** 没有它就没有钥匙串，通知器直接判定「未配置」，
      这条用例不会去动操作者的真实密钥链。
    * **``PYTHONPATH`` 带上仓库根。** ``backend/prompts.py`` 要
      ``backend.framework.prompts``，只放 ``backend/`` 的话子进程在
      import 期就死了。
    * **``PDT_PROVIDER_MODE=inherit``。** 服务启动时要问 CC Switch 要
      provider 链（``server.load_providers``），而 pytest 的 hermetic
      环境默认把这个模式钉成 ``cc-switch`` —— 那台机器上没有 CC Switch
      时，服务在启动期就死在 ``api_unreachable`` 上，端口永远不监听。
      inherit 模式是这套代码自己留的「没有 provider 集群」的配置，与
      ``/progress`` 读的是哪张表毫无关系，却决定了服务能不能起来。
    * **``PDT_DISABLE_TEMP_SWEEP=1``。** 服务启动时的凭据清扫是**机器级**
      的：它会走真实的临时目录，把认出来的凭据改写掉（生产环境正是为
      此而存在 —— 启动这一刻不会有我们自己的东西在跑）。pytest 进程自己
      带着这个开关（``_pytest_hermetic_env`` 里的 ``setdefault``），但那是
      **进程内**的：白名单 env 不继承父进程，于是子 server 照样做机器级
      清扫，lifespan 被拖到 18~21s，就绪等待（30s）差点不够用。父进程已经
      有值就原样透传 —— 谁定的谁说了算，父进程说 ``"0"`` 就让子进程扫。
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(
            part
            for part in (str(_REPO_ROOT), os.environ.get("PYTHONPATH", ""))
            if part
        ),
        "PDT_PLANS_DIR": str(plans_root),
        "PDT_STATE_DB_PATH": str(state_db),
        "PDT_PORT": str(port),
        "PDT_HOST": "127.0.0.1",
        "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "PDT_PROVIDER_MODE": "inherit",
        "PDT_DISABLE_TEMP_SWEEP": os.environ.get("PDT_DISABLE_TEMP_SWEEP", "1"),
    }


def _log_tail(log_path: Path, limit: int = 4000) -> str:
    """The tail of the spawned server's log — the only place its death explains itself."""
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "<server log unavailable>"


def _bound_port(log_path: Path) -> Optional[int]:
    """The port the server says it bound (it backs off when the port is taken)."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _BOUND_PORT_RE.search(text)
    if not match:
        return None
    return int(match.group(1))


def _wait_until_ready(server: _LiveServer, timeout: float = _READY_TIMEOUT) -> None:
    """Block until the spawned server answers ``/api/plans`` with 2xx."""
    deadline = time.monotonic() + timeout
    last_error = "no request was attempted"
    while time.monotonic() < deadline:
        if server.proc.poll() is not None:
            raise AssertionError(
                "the spawned server exited before it became ready "
                "(rc={0})\n--- server log tail ---\n{1}".format(
                    server.proc.returncode, _log_tail(server.log_path)
                )
            )
        bound = _bound_port(server.log_path)
        if bound is not None:
            server.port = bound
        try:
            response = httpx.get(
                "{0}/api/plans".format(server.base_url),
                headers=_REQUEST_HEADERS,
                timeout=2.0,
            )
        except httpx.HTTPError as exc:
            last_error = repr(exc)
        else:
            if 200 <= response.status_code < 300:
                return
            last_error = "status {0}".format(response.status_code)
        time.sleep(0.2)
    raise AssertionError(
        "the spawned server did not become ready within {0}s ({1})\n"
        "--- server log tail ---\n{2}".format(
            timeout, last_error, _log_tail(server.log_path)
        )
    )


def _spawn_server(
    port: int,
    plans_root: Path,
    state_db: Path,
    log_path: Path,
) -> _LiveServer:
    """Start the server as a real subprocess and wait until it is ready."""
    log = open(log_path, "w+b")  # noqa: SIM115 — closed by the caller's stop()
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-u", "server.py"],
        cwd=str(_BACKEND_DIR),
        env=_child_env(plans_root, state_db, port),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    server = _LiveServer(proc, port, log_path, log_handle=log)
    try:
        _wait_until_ready(server)
    except AssertionError:
        server.stop()
        raise
    return server


def _start_live_server(
    plans_root: Path,
    state_db: Path,
    log_path: Path,
) -> _LiveServer:
    """Start the server, retrying once if the port was taken mid-flight.

    取端口与释放之间存在竞态：内核把号交出来的那一刻它还是空的，等
    server 真去 bind 时可能已经被别人拿走了。那时候 server 自己会往后
    退（bind 失败 → 换号），所以**真正监听的端口**要从它的日志里读。
    这里在撞上这个竞态时重试一次，重新向内核要一个号 —— 任何时候都
    不退回写死的端口。
    """
    server: Optional[_LiveServer] = None
    for attempt in (1, 2):
        port = _acquire_port()
        server = _spawn_server(port, plans_root, state_db, log_path)
        if server.port == port:
            return server
        # 撞上了竞态：申请的号在 server bind 之前被拿走了。
        if attempt == 1:
            server.stop()
    # 第二次也撞上了。server 已经在内核给的另一个号上监听了 —— 那个号是
    # 空闲的，也不是写死的常量，所以照样能用。
    assert server is not None
    return server


def _assert_port_released(port: int, timeout: float = 15.0) -> None:
    """The port is free again: nothing answers on it, and it can be re-bound."""
    deadline = time.monotonic() + timeout
    last_error = "the port was never checked"
    while time.monotonic() < deadline:
        still_listening = False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                still_listening = True
        except OSError:
            # 连接被拒 —— 进程已经不在了，正文要的就是这个。
            pass
        if still_listening:
            last_error = "something is still listening on port {0}".format(port)
            time.sleep(0.2)
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))
        except OSError as exc:
            last_error = "port {0} could not be re-bound: {1}".format(port, exc)
            time.sleep(0.2)
            continue
        return
    raise AssertionError(
        "the server's port {0} was not released: {1}".format(port, last_error)
    )


@pytest.fixture
def live_server(tmp_path: Path, register_child_process) -> Iterator[_LiveServer]:
    """A real server subprocess, seeded with real verdicts.

    Function 级：每个用例各自起停，不共享 module 级实例。夹具的
    ``finally`` 负责回收 —— 就算用例自己先崩了，进程也不会留在机器上。
    """
    plans_root = tmp_path / "plans"
    state_db = tmp_path / "state.db"
    _write_plan(plans_root / _PLAN_ID)
    _seed_verdicts(state_db)
    log_path = tmp_path / "server.log"
    server = _start_live_server(plans_root, state_db, log_path)
    register_child_process(server.proc)
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def _failed_vps(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [vp for vp in payload["vps"] if vp.get("status") == "failed"]


def _vp_entry(payload: Dict[str, Any], vp_id: str) -> Dict[str, Any]:
    for vp in payload["vps"]:
        if vp.get("id") == vp_id:
            return vp
    raise AssertionError(
        "VP {0} is missing from the progress payload: {1}".format(
            vp_id, payload.get("vps")
        )
    )


@pytest.mark.integration
def test_live_progress_reports_reason_for_every_failed_vp(
    live_server: _LiveServer,
) -> None:
    """每个 failed VP 的 ``actual_result`` 非空 —— 原因可读。

    真进程 + 真 HTTP：响应 JSON 是唯一的判定依据。
    """
    payload = live_server.progress()

    failed = _failed_vps(payload)
    assert len(failed) == 2, (
        "both seeded verdicts (FAILED / BLOCKED) must show up as failed VPs: "
        "{0}".format(payload.get("vps"))
    )
    for vp in failed:
        reason = vp.get("actual_result")
        assert reason, (
            "VP {0} is failed but carries no reason — the operator sees a "
            "count and nothing else".format(vp.get("id"))
        )
        assert str(reason).strip(), (
            "VP {0} has a blank reason".format(vp.get("id"))
        )


@pytest.mark.integration
def test_live_blocked_reason_reads_blocked(live_server: _LiveServer) -> None:
    """被 block 的那一条，原因里读得出 ``BLOCKED``。

    「验不下去」和「验了没过」是两种语义，混在同一个失败桶里没人分得
    出来 —— 前缀就是给读的人看的。
    """
    payload = live_server.progress()

    blocked = _vp_entry(payload, "VP-002")
    assert blocked.get("status") == "failed", (
        "a BLOCKED verdict belongs in the failed bucket: {0}".format(blocked)
    )
    reason = blocked.get("actual_result") or ""
    assert "BLOCKED" in reason, (
        "the blocked VP must say so in its reason, got {0!r}".format(reason)
    )
    assert "service not ready" in reason, (
        "the reason must keep the detail, not only the marker: {0!r}".format(
            reason
        )
    )


@pytest.mark.integration
def test_live_failed_count_matches_buckets(live_server: _LiveServer) -> None:
    """``counts.failed == 2`` —— 一条 FAILED + 一条 BLOCKED 都在失败桶里。"""
    payload = live_server.progress()

    assert payload["counts"]["failed"] == 2, payload.get("counts")
    assert payload["counts"]["total"] == 2, payload.get("counts")
    assert sorted(payload["failed_vps"]) == ["VP-001", "VP-002"], payload.get(
        "failed_vps"
    )
    # 失败桶的两条在逐条列表里也是 failed —— 计数与列表同源。
    assert [vp["id"] for vp in _failed_vps(payload)] == ["VP-001", "VP-002"]


@pytest.mark.integration
def test_live_server_process_is_reclaimed(live_server: _LiveServer) -> None:
    """子进程在用例结束前退出，端口可重新绑定 —— 不留常驻进程。"""
    port = live_server.port

    assert live_server.proc.poll() is None, (
        "the server subprocess must be alive while the test is using it"
    )

    live_server.stop()

    assert live_server.proc.poll() is not None, (
        "the server subprocess must be gone after the teardown"
    )
    _assert_port_released(port)
