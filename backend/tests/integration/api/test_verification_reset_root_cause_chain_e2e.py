"""E2E: ``POST /api/verification/{plan_id}/reset`` 清根因链 —— 打**真实服务**。

背景
----
``tests/integration/api/test_verification_reset_root_cause_chain.py`` 已经把
``clear_root_cause_chain`` 的三条契约钉在 ``TestClient`` 上。但 ``TestClient``
和被验收的代码**跑在同一个进程里**：``server.PLANS_DIR`` 被 monkeypatch 成
``tmp_path``，``server._state_db_path`` 被换成夹具自己的 ``state.db``，请求走
的是 ASGI 的内存通道，没有 socket、没有端口、没有 ``request_guard``。

那套夹具证明的是**函数**对；它证明不了**服务**对。本文件把同一个契约放到
一个真的 ``server.py`` 进程上跑一遍：

* 真的 ``subprocess.Popen([sys.executable, "server.py"])``；
* 真的 ``GET /health`` 轮询（起来了才算数）；
* 真的 ``urllib.request`` HTTP POST（带 ``X-PDT-Request: 1``，否则
  ``request_guard`` 返 403）；
* 真的文件系统结果 —— 台账文件没了、链点名的报告没了、对照报告还在。

被验收的对象是 **HTTP 端点**，不是 UI：本文件不起任何浏览器。

三个 helper（``_free_port`` / ``_wait_port_released`` / ``_stop_server``）
是从 ``tests/test_server_json_cleanup.py`` **复制**过来的，不是 import ——
import 一个测试模块会执行它全部模块级代码，那条路上还带着一整套别的夹具。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest


# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

#: ``backend/`` —— ``server.py`` 所在目录。
_BACKEND_ROOT = Path(__file__).resolve().parents[3]

#: 本次 E2E 的全部临时产物都落在这里。
_E2E_ROOT = Path("/tmp/root-cause-scope-e2e")
_PLANS_ROOT = _E2E_ROOT / "plans"
_STATE_DB = _E2E_ROOT / "state.db"
_SERVER_LOG = _E2E_ROOT / "server.log"
_RESET_RESPONSE = _E2E_ROOT / "reset-response.json"

_PLAN_ID = "20261030-e2e-scope"
_PLAN_DIR = _PLANS_ROOT / _PLAN_ID
_REPORTS_DIR = _PLAN_DIR / "reports"

#: 台账文件名（相对 plan 目录）。
_CHAIN_FILENAME = "verification_root_cause_chain.json"

#: 台账点名的报告（相对 plan 目录）。
_NAMED_REPORT = "reports/root-cause-VP-3.md"

#: 不在链上的对照报告 —— reset 只能删台账**点名**的，不扫目录。
_KEEP_REPORT = "reports/keep-me.md"

#: 台账原文（任务书「输入示例」逐字照抄）。
_CHAIN_PAYLOAD_TEXT = (
    '{"version": 1, "stage": "BLOCKED_ON_USER", "vps": {"VP-3": '
    '{"stage": "implement", "round": 3, "report_path": '
    '"plans/20261030-e2e-scope/reports/root-cause-VP-3.md", '
    '"blocked_reason": "root_cause_report_without_scope"}}}'
)

#: A directory that shadows ``lark_oapi`` for the spawned child.
_STUB_DIR = _E2E_ROOT / "no-lark-sdk"


# ---------------------------------------------------------------------------
# 从 tests/test_server_json_cleanup.py 复制过来的三个 helper
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Ask the kernel for an unused loopback port, then let it go.

    ``bind(("127.0.0.1", 0))`` is the only allocation that cannot collide
    with a port some other checkout, container or stray listener already
    holds. The socket is closed before the number is returned — the caller
    has to win the race against its own ``Popen``, and holding the socket
    open would make losing it certain.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port_released(port: int, timeout: float = 10.0) -> bool:
    """True once ``port`` can be bound again, False if it is still held."""
    deadline = time.monotonic() + timeout
    while True:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        finally:
            probe.close()


def _stop_server(proc: subprocess.Popen, port: int, timeout: float = 10.0) -> None:
    """Stop a spawned uvicorn deterministically and confirm the port is free.

    ``terminate → wait(timeout) → kill → wait``, then check that ``port``
    is actually released. Two things are load-bearing here and both used
    to be wrong:

    * the sequence is ordered, not best-effort. ``proc.wait()`` reaps, so a
      port check that runs before the reap can see a socket the kernel has
      not finished tearing down;
    * nothing is swallowed. The previous ``except Exception: pass`` around
      this block is what let a SIGTERM-swallowing zombie outlive the test
      that spawned it and answer the next run's health probe — the failure
      this whole port change exists to remove. A teardown that cannot
      account for its child fails loudly instead.
    """
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)
    if not _wait_port_released(port):
        raise AssertionError(
            f"the spawned server (pid={proc.pid}, rc={proc.returncode}) was "
            f"killed but :{port} is still bound {timeout}s later. A listener "
            f"this fixture started would outlive the test that spawned it "
            f"and answer a later run's health probe."
        )


# ---------------------------------------------------------------------------
# 本文件自己的 helper
# ---------------------------------------------------------------------------


def _read_server_log() -> str:
    """The spawned server's output so far.

    A **separate** handle, not the one passed to ``Popen``: the child
    inherits that handle's file description, so the two share one offset
    and a read through the parent's handle would start wherever the child
    happened to be writing. A fresh ``open`` gets its own offset and
    reads the file from the top.
    """
    try:
        with open(_SERVER_LOG, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _bound_port_from_log() -> Optional[int]:
    """The port the server actually bound, as it announced it.

    ``server.py`` backs off to the next port when the requested one is
    taken (``[backend server] Port <p> occupied; using fallback port
    <p2>``). Reading the announcement is what keeps this test talking to
    *its own* server instead of to whoever owns the number we asked for.
    """
    matches = re.findall(
        r"Starting uvicorn on [^\s:]+:(\d+)", _read_server_log(),
    )
    return int(matches[-1]) if matches else None


def _wait_healthy(
    port: int, proc: subprocess.Popen, timeout: float = 30.0,
) -> Optional[int]:
    """Poll ``GET /health`` until the server answers 2xx.

    Returns the port that answered, or ``None`` when the server never came
    up inside the budget. ``None`` is a **failure**, never a skip: a skip
    would report "0 failed" for a layer that was never exercised, which
    is exactly the hole this gate exists to close.
    """
    deadline = time.monotonic() + timeout
    candidate = port
    while time.monotonic() < deadline:
        announced = _bound_port_from_log()
        if announced and announced != candidate:
            candidate = announced
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{candidate}/health", timeout=2,
            ) as response:
                if 200 <= response.status < 300:
                    return candidate
        except (urllib.error.URLError, ConnectionResetError, OSError):
            pass
        # A server that died on startup cannot come up. Without this the
        # loop spends the whole budget on a corpse.
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    return None


def _site_package_dirs() -> List[str]:
    """The third-party package directories this interpreter can see.

    The pytest process runs with ``$HOME`` redirected at a scratch
    directory (the hermetic environment does that), and Python derives
    the **user** site-packages directory from ``$HOME`` — at interpreter
    start-up. The pytest process therefore has the real user
    site-packages on its ``sys.path``, while a child started afterwards
    computes a user-site directory under the redirected ``$HOME`` where
    nothing is installed. The child then dies with ``ModuleNotFoundError:
    No module named 'anyio'`` before it ever binds a port — which reads
    as a health timeout, not as an import error.

    Handing the directories to the child explicitly is what makes the two
    processes see the same third-party packages.
    """
    dirs = []
    for entry in sys.path:
        if entry and entry.endswith(("site-packages", "dist-packages")):
            dirs.append(entry)
    return dirs


def _write_lark_stub() -> None:
    """Shadow ``lark_oapi`` for the child, so it never imports the SDK.

    ``FeishuNotifier.start()`` builds a real ``FeishuClient`` at startup,
    and the client's constructor imports ``lark_oapi`` **before** it asks
    whether Feishu is configured at all. On this machine that import is
    ~1500 modules off a slow volume and costs 20-40 s — the server then
    misses its health budget, and the gate fails for a reason that has
    nothing to do with the endpoint under test.

    Raising ``ImportError`` is the branch the client already handles
    ("the SDK is not installed"): the notifier stays inert, the server
    comes up in seconds, and the reset endpoint behaves exactly as it
    does in production.
    """
    _STUB_DIR.mkdir(parents=True, exist_ok=True)
    (_STUB_DIR / "lark_oapi.py").write_text(
        '"""Blocked on purpose — see the test module\'s docstring."""\n'
        'raise ImportError("lark-oapi is blocked in the reset e2e child")\n',
        encoding="utf-8",
    )


def _child_env(port: int) -> Dict[str, str]:
    """The environment the spawned server runs with.

    ``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH`` / ``PDT_PORT`` are the task's
    three: the first two point the server at *this run's* tree (without
    them the server answers about the developer's real plans/ and real
    state.db), the third pins the port.

    Two more keys are not optional in practice:

    ``PYTHONPATH``
        must carry the **repo root**, this interpreter's site-packages,
        and the stub directory (in that order). The repo root is needed
        because ``prompts.py`` does ``from backend.framework.prompts
        import ...`` — a child whose sys.path holds only ``backend/``
        dies at import time. The site-packages are needed because of the
        redirected ``$HOME`` (see :func:`_site_package_dirs`). The stub
        directory comes first so the child never imports the Feishu SDK
        (see :func:`_write_lark_stub`).

    ``PDT_ORPHAN_REAPER=0``
        switches off the orphan reaper. Its boot scan is a **machine-wide**
        sweep (``ps -E`` per process on macOS) that costs ~20 s and, worse,
        the reaper's job is to *kill* orphans — a test child must not go
        around killing the developer's other processes, and must not leave
        a detached daemon behind after the test ends.

    The child's **credentials** are taken away as well. A test server that
    inherited the operator's Feishu app secret would build a real
    ``FeishuClient`` at startup — and importing ``lark_oapi`` costs ~40 s
    on this machine, which reads as "the server never came up" and blows
    the health budget. Worse than slow: a test server that *can* push
    cards would push them to the operator's chat.
    """
    env = {
        **os.environ,
        "PDT_PLANS_DIR": str(_PLANS_ROOT),
        "PDT_STATE_DB_PATH": str(_STATE_DB),
        "PDT_PORT": str(port),
        "PDT_DISABLE_TEMP_SWEEP": os.environ.get("PDT_DISABLE_TEMP_SWEEP", "1"),
        "PDT_ORPHAN_REAPER": "0",
        "PYTHONFAULTHANDLER": "1",
        # The suite pins ``PDT_PROVIDER_MODE=cc-switch`` for the pytest
        # process (it is a deployment property, not a test property), and
        # the child inherits it. On a runner without CC Switch — CI — the
        # server then dies at startup asking :4000 for the provider chain.
        # This case exercises /reset, not provider routing; inherit mode
        # is the mode that starts a server with no CC Switch present.
        "PDT_PROVIDER_MODE": "inherit",
        # The keychain is fail-closed: anything but "0"/"false" leaves it
        # switched off. The child must not go looking in the operator's
        # keychain for a secret it has no business reading.
        "PDT_DISABLE_KEYCHAIN_SECRETS": "1",
    }
    # The secret may arrive over an inherited descriptor (the launcher's
    # hand-off) or in the environment. Neither reaches the test child.
    for key in list(env):
        if key.startswith("PDT_SECRET_FD_") or key == "FEISHU_APP_SECRET":
            env.pop(key)
    python_path = [str(_STUB_DIR), str(_BACKEND_ROOT.parent)]
    inherited = os.environ.get("PYTHONPATH")
    if inherited:
        python_path.extend(part for part in inherited.split(os.pathsep) if part)
    python_path.extend(_site_package_dirs())
    env["PYTHONPATH"] = os.pathsep.join(python_path)
    return env


def _reset_e2e_tree() -> None:
    """Start every case from an empty tree.

    The first case **deletes** the chain and the named report; a second
    case that reused them would be asserting on its own leftovers — and
    would pass for the wrong reason. Each case rebuilds its own fixtures,
    so the order of the two cases does not matter.
    """
    if _PLAN_DIR.exists():
        shutil.rmtree(_PLAN_DIR)
    for suffix in ("", "-wal", "-shm"):
        sidecar = Path(f"{_STATE_DB}{suffix}")
        if sidecar.exists():
            sidecar.unlink()


def _seed_chain_fixture() -> None:
    """Write the ledger and the reports it names.

    The ledger is the task's input payload, verbatim; the named report is
    a root-cause report **without** scope (the state that makes a round
    park on ``BLOCKED_ON_USER``); ``keep-me.md`` is the control — a report
    in the same directory that the ledger does not name.
    """
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (_PLAN_DIR / _CHAIN_FILENAME).write_text(
        _CHAIN_PAYLOAD_TEXT, encoding="utf-8",
    )
    (_REPORTS_DIR / "root-cause-VP-3.md").write_text(
        "# 根因\n连接池耗尽。\n", encoding="utf-8",
    )
    (_REPORTS_DIR / "keep-me.md").write_text(
        "不在链上的报告\n", encoding="utf-8",
    )


def _post_reset(port: int, payload: Dict[str, Any]) -> Tuple[int, str]:
    """The user action under test: a real HTTP POST against a real server.

    ``X-PDT-Request: 1`` is mandatory — ``request_guard`` refuses every
    ``/api/*`` request without it (403), and the refusal would otherwise
    read as "the endpoint is broken".

    A 4xx/5xx is returned as ``(status, body)`` instead of raising: the
    point of the assertion is to *see* the status, not to blow up inside
    ``urlopen`` with an opaque ``HTTPError``.
    """
    request = urllib.request.Request(  # noqa: S310
        f"http://127.0.0.1:{port}/api/verification/{_PLAN_ID}/reset",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-PDT-Request": "1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def live_server():
    """A real ``server.py`` process on an ephemeral port.

    Yields the port it answered on. The child is always reaped in the
    ``finally`` — including when an assertion inside a test fails, which
    is exactly when a leaked listener would poison the next run.
    """
    _E2E_ROOT.mkdir(parents=True, exist_ok=True)
    _write_lark_stub()
    _reset_e2e_tree()

    port = _free_port()
    log_handle = open(_SERVER_LOG, "w", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "server.py"],
        cwd=str(_BACKEND_ROOT),
        env=_child_env(port),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    live_port: Optional[int] = None
    try:
        live_port = _wait_healthy(port, proc, timeout=30.0)
        if live_port is None:
            if proc.poll() is None:
                proc.send_signal(signal.SIGABRT)
                time.sleep(1.0)
            pytest.fail(
                f"the spawned backend (pid={proc.pid}, rc={proc.poll()}) did "
                f"not answer GET /health on :{port} within 30s. A server "
                f"that never comes up must fail the gate, not skip it — a "
                f"skip reads as '0 failed' for a layer nobody exercised.\n"
                f"Server output:\n{_read_server_log()[-4000:]}"
            )
        yield live_port
    finally:
        _stop_server(proc, live_port or port)
        log_handle.close()


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_reset_clears_root_cause_chain_against_live_server(live_server):
    """真的服务 + ``clear_root_cause_chain=true`` → 台账与点名的报告都消失。

    对照报告 ``keep-me.md`` 必须还在：清理是**点名**的，不是扫目录的。
    """
    _seed_chain_fixture()

    status, raw = _post_reset(
        live_server,
        {
            "clear_root_cause_chain": True,
            "clear_artifacts": False,
            "supersede_repair_tasks": False,
        },
    )
    _RESET_RESPONSE.write_text(raw, encoding="utf-8")

    assert status == 200, raw
    cleared = json.loads(raw)["cleared"]["root_cause_chain"]
    assert _CHAIN_FILENAME in cleared, cleared
    assert _NAMED_REPORT in cleared, cleared
    assert set(cleared) == {_CHAIN_FILENAME, _NAMED_REPORT}, cleared

    assert not (_PLAN_DIR / _CHAIN_FILENAME).exists(), (
        "台账还在，重跑时会继续占着验证点"
    )
    assert not (_PLAN_DIR / _NAMED_REPORT).exists(), (
        "台账点名的报告还在，reader 仍会判 scope 缺失"
    )
    assert (_PLAN_DIR / _KEEP_REPORT).exists(), (
        "reset 只能删台账点名的报告，不能扫整个 reports 目录"
    )
    assert _read_server_log().strip(), (
        f"{_SERVER_LOG} 必须是空的 —— 那说明这个服务根本没跑起来"
    )


@pytest.mark.e2e
def test_reset_default_keeps_chain_against_live_server(live_server):
    """不带这个字段的 reset（``{}``）把台账和报告都留着。

    清台账是一个**决定**，不是 reset 的副作用 —— 真实服务上也得是这样。
    """
    _seed_chain_fixture()

    status, raw = _post_reset(live_server, {})
    _RESET_RESPONSE.write_text(raw, encoding="utf-8")

    assert status == 200, raw
    assert json.loads(raw)["cleared"]["root_cause_chain"] == []
    assert (_PLAN_DIR / _CHAIN_FILENAME).exists(), "默认不该清台账"
    assert (_PLAN_DIR / _NAMED_REPORT).exists(), "默认不该清报告"
