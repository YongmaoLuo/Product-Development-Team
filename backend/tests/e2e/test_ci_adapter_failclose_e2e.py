"""CI 适配脚本的 fail-close：导入与启动两条链路的端到端验收（2026-11-05）。

前面的两个任务把「这个项目没有可执行的 ``scripts/ci_gate.sh``」这道门禁
分别钉在了两条链路上：

* 任务 6：``POST /api/plan/import`` —— 缺适配脚本 → 422，计划目录不落地；
* 任务 9：``POST /api/execution/{id}/start`` —— 缺适配脚本 → 400，不 spawn、
  不写 ``plan_execution`` 行。

两份自测都走 ``TestClient``：同一个进程、同一份内存状态、请求与响应都在
一个函数调用里往返。**单测各自绿不等于链路通** —— 中间隔着的还有中间件
（请求守卫）、路由装配、真实的进程边界、真实的 SQLite 文件。这些只有在
**真后端进程 + 真 HTTP 请求**下才会露出来。

所以这一份是端到端的验收：

* 真起一个后端子进程（真的 uvicorn、真的 lifespan、真的端口）；
* 真用 HTTP 打它（Playwright 的 request 上下文 —— 和浏览器那条路走同一
  套客户端栈，请求头也是浏览器会带的那一个）；
* 真看磁盘上的后果（计划目录里有没有多出目录、``plan_execution`` 表里有
  没有多出行）。

现场是真的
----------

* **后端是真的**：一个真的 uvicorn 子进程，一个真的端口；
* **项目是真的**：一个真的临时目录，里面真的没有 ``scripts/ci_gate.sh``；
* **请求是真的**：真的 HTTP 请求，带着真的守卫头（``X-PDT-Request: 1``）。

两个用例
--------

* ``test_import_fails_closed_without_adapter`` —— 导入通道：没有适配脚本的
  项目，导入被拒（422，body 含 ``ci_adapter_missing``），计划目录不落地。
* ``test_start_fails_closed_without_adapter`` —— 启动通道：同一个项目补上
  可执行的适配脚本之后，那个 400 不再发生；而在补上之前，它既不 spawn
  也不写 ``plan_execution`` 行。

为什么两个用例要分成两个项目目录
--------------------------------

启动那个用例会在中途往项目里装上适配脚本。导入那个用例要求项目**一直**
没有适配脚本。共用一个目录就等于让两个用例的先后顺序决定结果 —— 那不是
验收，是运气。分成两个目录，先后顺序就再也进不了这件事。

边界
----

* 端口由内核分配（避开 8000/8011 这些常被占的端口），起服务前再确认一次；
* 断言失败时把 status 与 body 原文打出来 —— 一个只会说「断言失败」的用例，
  读不出到底是 422 变成了 500，还是文案里少了那个前缀；
* 用例结束必须把 uvicorn 子进程停掉 —— 留一个还占着端口的孤儿进程，下一
  次跑就会撞端口。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent

#: The interpreter this suite was installed into.  A checkout with no
#: ``backend/.venv`` (fresh worktree, container) falls back to the
#: interpreter already running pytest — never to a system python, which
#: would not have the dependencies installed.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: Every ``/api/*`` request must carry this header
#: (``backend/request_guard.py``).  Without it the request is refused with
#: 403 **before it reaches a route** — so a missing header here would look
#: exactly like a broken gate, and the test would be measuring the wrong
#: thing.
REQUEST_HEADERS = {"X-PDT-Request": "1"}

#: The plan this file seeds for the start lane.
PLAN_ID = "e2e-ci-adapter-failclose"

#: The CI adapter's path, relative to the project root — the same literal
#: the production gate uses (``backend/ci_adapter.py``).  Written out here
#: on purpose: a test that reads the constant out of the module under test
#: cannot notice the module moving it.
ADAPTER_RELATIVE_PATH = "scripts/ci_gate.sh"

#: The stand-in executor, installed through the named production seam
#: (``PDT_EXECUTOR_CMD``).  It is a real process with a real pid; it is
#: spawned by the **server**, so the server owns it.
EXECUTOR_FIXTURE: Path = (
    BACKEND_DIR / "tests" / "e2e" / "fixtures" / "deterministic_executor.py"
)

#: How long the backend may take to answer its readiness probe.  A boot
#: that has not answered within this budget is a failure, and the failure
#: carries the tail of the server's own output — a boot that died says why
#: in there.
READY_TIMEOUT_SECONDS = 90.0


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind an ephemeral loopback port and hand the number back.

    The kernel picks the port, so the number is free at the moment it is
    returned.  It is the same trick the sibling E2E files use.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _describe(response: Any) -> str:
    """Status + body, verbatim.

    A failure has to say what came back: "the assertion failed" does not
    say whether the gate answered 422 or 500, nor whether the reason text
    lost its ``ci_adapter_missing`` prefix on the way through the
    middleware.
    """
    try:
        body = response.text()
    except Exception as exc:  # noqa: BLE001 — the body may be unreadable
        body = f"<the body could not be read: {exc!r}>"
    return f"status={response.status}\nbody={body}"


def _plan_dirs(plans_dir: Path) -> List[str]:
    """The names of the plan directories under the plans tree."""
    if not plans_dir.exists():
        return []
    return sorted(entry.name for entry in plans_dir.iterdir() if entry.is_dir())


def _install_adapter(project_dir: Path) -> Path:
    """Install an executable CI adapter script in the project."""
    scripts_dir = project_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    adapter = scripts_dir / "ci_gate.sh"
    adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    adapter.chmod(0o755)
    return adapter


def _exec_rows(db_path: Path) -> List[Tuple[Any, ...]]:
    """Every row of ``plan_execution`` — the table that says "this ran"."""
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate

    conn = db_open(db_path)
    try:
        migrate(conn)
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT plan_id, exec_status, exec_pid, stop_reason "
                "FROM plan_execution"
            ).fetchall()
        ]
    finally:
        conn.close()


def _routing_phase(db_path: Path, plan_id: str) -> Optional[str]:
    """The phase the routing row carries right now."""
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = db_open(db_path)
    try:
        migrate(conn)
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


# ---------------------------------------------------------------------------
# The seed: a plan the start lane can be issued against
# ---------------------------------------------------------------------------


def _seed_plan(plans_dir: Path, db_path: Path, plan_id: str = PLAN_ID) -> Path:
    """Write everything the start lane needs, and nothing it must not have.

    * ``tasks.json`` — the endpoint refuses a plan without a task list.
    * ``plan_state.json`` — the legacy phase mirror, ``queued`` (one of the
      phases a start may be issued from).
    * ``import.json`` — the evidence that this plan came through the
      import lane.  The gate only fires for imported plans; a plan without
      this file keeps the old contract ("no pre-run gate").
    * a ``plan_routing`` row in ``queued``.

    **There is no ``plan_execution`` row here, and that is the point.**
    The refusal has to leave that table empty, and a row seeded by the
    fixture would make the assertion vacuous — the table would look
    "unchanged" for the wrong reason.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "t1",
                        "title": "the one task this plan has",
                        "description": "## 背景\nnothing\n\n## 目标\nsomething",
                        "test_command": "true",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": "queued",
                "completed_phases": [],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {"arch_enabled": False, "test_enabled": False},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plan_dir / "import.json").write_text(
        json.dumps({"tasks": [{"title": "an imported task"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    conn = db_open(db_path)
    try:
        # The tables first: the backend opens this database on boot and
        # reads the row out of it, so the schema has to be there before
        # the row is written — not after.
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "queued")
    finally:
        conn.close()

    return plan_dir


# ---------------------------------------------------------------------------
# The backend process
# ---------------------------------------------------------------------------


def _boot(work: Path, port: int) -> subprocess.Popen:
    """Start a real backend against *work*.

    The environment is the whole contract between the seed and the
    server: the plans tree, the state database and the port.  Four more
    are set because the child would otherwise inherit the test process's
    environment and misbehave:

    * ``PDT_PROVIDER_MODE=inherit`` — the boot asks CC Switch for the
      provider chain and **aborts the startup** when it cannot be asked.
      This server exists to answer two questions; it must not need a
      provider fleet to come up.
    * ``PDT_ORPHAN_REAPER=0`` — the boot otherwise walks **every process
      on the machine** to find orphans left behind by a previous life.
      A throwaway server has no orphans to reap: the reaper is off.
    * ``PDT_SCHEDULER_FILE`` — the scheduler picks up ``queued`` plans on
      its own, and a plan started behind the test's back would answer
      questions the test never asked.  The scheduler is switched off
      explicitly rather than by relying on the shipped default.
    * ``PYTEST_CURRENT_TEST`` — the product's own answer to "may this
      process touch the machine?", asked in ``server._orphan_reaper_may_act``
      and in ``state_machine.db.connection``: a *test* process must not
      reap the developer's processes and must never open the operator's
      live ``state.db``.  This child is a test process.

    The child's output goes to a file next to the state, so a boot that
    never became ready can say why.
    """
    env = dict(os.environ)
    env.update(
        {
            "PDT_PLANS_DIR": str(work / "plans"),
            "PDT_STATE_DB_PATH": str(work / "state.db"),
            "PDT_PORT": str(port),
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_ORPHAN_REAPER": "0",
            "PDT_SCHEDULER_FILE": str(work / "scheduler.yaml"),
            # The one substitution this file makes: the executor is a
            # stand-in, installed through the named production seam, and
            # the **server** spawns it (so the server owns the pid and the
            # process ledger records it).  It exits on its own after a
            # second — a stand-in that outlives the test is a stand-in
            # that has to be cleaned up by hand.
            "PDT_EXECUTOR_CMD": "{0} -u {1}".format(
                str(VENV_PYTHON), str(EXECUTOR_FIXTURE)
            ),
            "PDT_E2E_EXIT_AFTER": "1",
            "PYTEST_CURRENT_TEST": (
                "backend/tests/e2e/test_ci_adapter_failclose_e2e.py (setup)"
            ),
            # ``backend/`` because the server's own modules import each
            # other by their flat names (``from config_paths import ...``);
            # the repository root because ``backend/prompts.py`` reaches
            # into ``backend.framework.prompts``.  Both roots have to be
            # importable at once — that is what the production layout
            # asks for.
            "PYTHONPATH": os.pathsep.join(
                [
                    str(BACKEND_DIR),
                    str(PROJECT_ROOT),
                    env.get("PYTHONPATH", ""),
                ]
            ),
        }
    )
    # A stale secret descriptor must never be closed by a process that
    # never owned it.
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)
    env.pop("ANTHROPIC_BASE_URL", None)

    log_path = work / f"server-{port}.log"
    handle = log_path.open("wb")
    proc = subprocess.Popen(
        [
            str(VENV_PYTHON),
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._pdt_log_path = log_path  # type: ignore[attr-defined]
    proc._pdt_log_handle = handle  # type: ignore[attr-defined]
    return proc


def _server_output(proc: subprocess.Popen, limit: int = 2000) -> str:
    """The tail of the backend's own output."""
    path = getattr(proc, "_pdt_log_path", None)
    if path is None:
        return "(no server output was captured)"
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - the file is ours
        return f"(could not read {path}: {exc})"
    return text[-limit:]


def _wait_ready(base_url: str, proc: subprocess.Popen) -> None:
    """Wait until the backend answers its readiness probe.

    The backend is up when the API answers, not when the process exists:
    the process exists for a moment before uvicorn is listening.  A
    backend that dies on the way up says so in its own output, so that
    output is attached to the failure.
    """
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"the backend exited with {proc.returncode} before it was "
                f"ready:\n{_server_output(proc)}"
            )
        try:
            probe = urllib.request.Request(f"{base_url}/api/health")
            for key, value in REQUEST_HEADERS.items():
                probe.add_header(key, value)
            with urllib.request.urlopen(probe, timeout=5) as response:
                if response.status == 200:
                    return
                last = response.status
        except Exception as exc:  # noqa: BLE001 — any failure means "not yet"
            last = exc
        time.sleep(0.2)
    raise AssertionError(
        f"{base_url}/api/health was not ready within {READY_TIMEOUT_SECONDS}s; "
        f"last={last!r}\n{_server_output(proc)}"
    )


def _boot_backend(work: Path) -> Tuple[subprocess.Popen, int, str]:
    """Boot the backend; if the port was taken, take another and retry once.

    ``_free_port`` asks the kernel for a port, so the port is free at the
    moment it is handed back — but the gap between "asked for" and "bound"
    is real, and a squatter in that gap makes the boot die.  That is the
    one failure worth retrying; anything else is a real failure and is
    raised at once.
    """
    last: Optional[BaseException] = None
    for attempt in (1, 2):
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        proc = _boot(work, port)
        try:
            _wait_ready(base_url, proc)
        except AssertionError as exc:
            last = exc
            _teardown(proc)
            output = str(exc).lower()
            taken = (
                "address already in use" in output
                or "errno 48" in output
                or "errno 98" in output
            )
            if attempt == 2 or not taken:
                raise
            continue
        return proc, port, base_url
    raise AssertionError(f"the backend never came up: {last!r}")


def _teardown(proc: Optional[subprocess.Popen]) -> None:
    """Stop the backend and let go of its port.

    ``terminate`` first — uvicorn shuts down gracefully on SIGTERM — and
    ``kill`` only when that did not finish in time.  The port has to be
    free again before the next boot binds it.
    """
    if proc is None:
        return
    handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, AttributeError):
                    proc.kill()
                proc.wait(timeout=10)
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover - the handle is ours
                pass


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_backend(tmp_path_factory) -> Dict[str, Any]:
    """A real backend process, its plans tree and its state database."""
    work = tmp_path_factory.mktemp("ci-adapter-failclose")
    plans_dir = work / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    db_path = work / "state.db"

    # The scheduler is switched off explicitly: it picks up ``queued``
    # plans on its own, and this file seeds exactly one.
    (work / "scheduler.yaml").write_text(
        "scheduler:\n  enabled: false\n  dry_run: false\n",
        encoding="utf-8",
    )

    # Two projects, so the two cases cannot influence each other through
    # the filesystem: the import case needs a project that never has an
    # adapter, the start case installs one halfway through.
    import_project = work / "import-project"
    import_project.mkdir()
    start_project = work / "start-project"
    start_project.mkdir()

    _seed_plan(plans_dir, db_path)

    proc, _port, base_url = _boot_backend(work)
    try:
        yield {
            "base_url": base_url,
            "plans_dir": plans_dir,
            "db_path": db_path,
            "import_project": import_project,
            "start_project": start_project,
            "proc": proc,
        }
    finally:
        _teardown(proc)


@pytest.fixture(scope="module")
def api(live_backend):
    """A Playwright request context pointed at the live backend.

    The request context is the browser's own HTTP client: it carries the
    guard header the same way the UI does, so a request that arrives is
    a request the guard let through.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        context = driver.request.new_context(
            base_url=live_backend["base_url"],
            extra_http_headers=dict(REQUEST_HEADERS),
        )
        try:
            yield context
        finally:
            context.dispose()


# ---------------------------------------------------------------------------
# 1) The import lane: no adapter → refused, nothing left behind
# ---------------------------------------------------------------------------


def test_import_fails_closed_without_adapter(api, live_backend):
    """导入通道：项目没有适配脚本 → 422，计划目录不落地。

    拒绝是 fail-close：跑起来也判不了绿红，就不跑。

    拒绝还必须是**干净**的：一次被拒的导入不能在地上留下半个计划 ——
    一个存在却跑不了的计划，正是这道门禁要防的东西。
    """
    plans_dir = live_backend["plans_dir"]
    project_dir = live_backend["import_project"]
    assert not (project_dir / ADAPTER_RELATIVE_PATH).exists(), (
        "the fixture handed the import case a project that already has an "
        "adapter"
    )
    before = _plan_dirs(plans_dir)

    response = api.post(
        "/api/plan/import",
        data={
            "project_dir": str(project_dir),
            "title": "e2e ci adapter failclose",
            "content": [{"title": "a task that must never be imported"}],
        },
    )

    assert response.status == 422, _describe(response)
    body = response.text()
    assert "ci_adapter_missing" in body, _describe(response)

    after = _plan_dirs(plans_dir)
    assert after == before, (
        "the refused import left a plan directory behind: "
        f"before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# 2) The start lane: no adapter → refused, nothing left behind
# ---------------------------------------------------------------------------


def test_start_fails_closed_without_adapter(api, live_backend):
    """启动通道：项目没有适配脚本 → 400，且什么都不留下。

    拒绝的落点只能是响应体：一次没跑起来的启动不能留下一条「跑过」的
    痕迹（``plan_execution`` 行），也不能把计划的相位挪到 ``executing``
    —— 否则每个读这两张表的消费者都会以为这个计划跑过。

    然后同一个项目补上**可执行**的适配脚本，门禁就该让路：它拦的是
    「没有适配脚本的项目」，不是「导入通道的项目」。
    """
    db_path = live_backend["db_path"]
    project_dir = live_backend["start_project"]
    assert not (project_dir / ADAPTER_RELATIVE_PATH).exists(), (
        "the fixture handed the start case a project that already has an "
        "adapter"
    )
    assert _exec_rows(db_path) == [], "the fixture seeded an execution row"

    refused = api.post(
        f"/api/execution/{PLAN_ID}/start",
        data={"project_dir": str(project_dir)},
    )

    assert refused.status == 400, _describe(refused)
    assert "ci_adapter_missing" in refused.text(), _describe(refused)
    assert _exec_rows(db_path) == [], (
        "the refused start wrote an execution row"
    )
    assert _routing_phase(db_path, PLAN_ID) == "queued", (
        "the refused start moved the plan's phase; a plan that is not "
        "running must not read as 'executing'"
    )

    # The same project, this time with an executable adapter.
    _install_adapter(project_dir)

    accepted = api.post(
        f"/api/execution/{PLAN_ID}/start",
        data={"project_dir": str(project_dir)},
    )

    assert "ci_adapter_missing" not in accepted.text(), _describe(accepted)
    assert accepted.status == 200, _describe(accepted)
