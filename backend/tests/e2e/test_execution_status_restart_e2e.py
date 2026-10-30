"""真实重启之后，status 的日志仍然非空 —— 端到端验收 F。

前面的三个任务修的是同一件事的两半
------------------------------------

* 任务 1：``backend/execution_log_tail.py`` —— 从 ``execution.log`` 的尾部
  读出若干行，并把每一行归一化成 ``[<带时区的 ISO 戳>] <正文>``。
* 任务 2：``GET /api/execution/{id}/status`` —— 进程内存里没有日志时，从
  磁盘上那份日志回填。
* 任务 3：``started_at`` —— 写的时候带时区，读的时候也带时区。

这三个任务的自测都走 ``TestClient``：同一个进程、同一份内存里的
``_execution_state``、一个从没重启过的后端。而它们要修的现场恰恰是
**重启之后**：进程内存里那份 ``deque`` 随进程一起没了，答案只能从磁盘
上捞。``TestClient`` 走不到那里 —— 它连一次真实的进程退出都没有发生过。

所以这一份是真进程的验收：真的起一个 ``backend.server``，真的把它杀掉，
真的再起一个，然后问第二个进程"这个计划在干什么"。第一个进程知道的任何
事都不算数了 —— 它已经不在了。

被钉住的两件事
--------------

* **日志非空，且末行是新鲜的。** ``logs`` 里有行；最后一行的时间戳距
  现在不到 5 分钟。回填不能只把三小时前的旧行端上来装作新鲜，也不能端
  一堆没有时间戳的裸文本 —— 读者要靠那个戳判断"这个 run 还在动吗"。
* **``started_at`` 带时区。** 重启之后这个字段仍然说得出它指的是哪
  一瞬间（``utcoffset()`` 不是 ``None``），而不是一个猜不出时区含义的
  裸字符串；而且它指的是**我们写进去的那一瞬间**（离现在不到 5 分钟），
  不是被当成 UTC 读偏了 8 小时的另一个瞬间。

为什么 seed 的 ``exec_pid`` 是测试进程自己的 pid
-------------------------------------------------

后端启动时会扫描计划目录，把每一行的 run 恢复进内存；对 ``running`` 的
行还要探一次 pid：进程没了就判 ``failed``。所以这一行得挂在一个**活着**
的进程上，否则后端一启动就把这次 run 判成崩溃，测到的就成了"重启后 run
死了"，而不是"重启后日志还在"。测试进程自己就是那个活着的进程。

为什么 seed 和后端必须指向同一个 state.db
-------------------------------------------

行写进 A 库、后端读 B 库，断言就会把"读不到行"误判成"回填失效"。
所以 seed 用的就是后端那个 ``PDT_STATE_DB_PATH``：同一个路径，同一个
文件。seed 先 ``migrate`` 建表、再写行 —— 免得后端启动时那张表还不存在。

复用说明
--------

本文件里的 ``_free_port`` / ``_seed`` / ``_boot`` / ``_wait_ready`` /
``_teardown`` / ``_http_json`` 是给后面的任务（任务 5）复用的：按**文件
路径** importlib 进来即可（``backend/tests/e2e/`` 没有 ``__init__.py``，
跨模块 import 要靠 pytest 的 rootdir  gymnastics，太脆）。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent

# The backend subprocess has to run on the interpreter this suite was
# installed into.  A checkout with no ``backend/.venv`` (fresh worktree,
# container) falls back to the interpreter already running pytest — never
# to a system python, which would not have the dependencies installed.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: Every ``/api/*`` request must carry this header (``backend/request_guard.py``).
REQUEST_HEADERS = {
    "X-PDT-Request": "1",
    "Content-Type": "application/json",
}

#: The plan this file seeds.  A leaf name, nothing exotic: the endpoint
#: validates it (``server._validated_plan_id``) before it resolves a path.
PLAN_ID = "p1"

#: How long the backend may take to answer its readiness probe.  A boot
#: that has not answered within this budget is a failure, and the failure
#: carries the tail of the server's own output — a boot that died says why
#: in there, and a boot that hung says where it stopped.
READY_TIMEOUT_SECONDS = 90.0

#: The shape of a line a reader may parse: a bracket, then an ISO-8601
#: stamp.  The offset is part of the stamp — a reader cannot tell which
#: instant a naive string names.
LINE_SHAPE_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
STAMP_RE = re.compile(r"^\[(?P<stamp>[^\]]+)\]")

#: How fresh the last log line has to be.  The seeded line is written
#: half a minute ago, so anything older than this means the answer was
#: built from the wrong thing.
FRESH_WINDOW_SECONDS = 300.0


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


def _http_json(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    """One HTTP request against the backend; ``(status, decoded body)``.

    The guard header goes with every call: ``/api/*`` is guarded, and a
    request without the header is refused with 403 before it reaches a
    route.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in REQUEST_HEADERS.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, raw.decode("utf-8", "replace")


def _parse_stamp(text: str) -> datetime:
    """Parse the stamp at the head of a normalised line.

    The stamp is written with an explicit offset, so it names one instant.
    ``Z`` is spelled out as ``+00:00`` first: ``fromisoformat`` only
    learned to read a trailing ``Z`` in 3.11, and this file must not care
    which interpreter runs it.
    """
    match = STAMP_RE.match(text)
    if match is None:
        raise AssertionError(f"the line carries no leading stamp: {text!r}")
    stamp = match.group("stamp").strip()
    if stamp.endswith(("Z", "z")):
        stamp = stamp[:-1] + "+00:00"
    return datetime.fromisoformat(stamp)


def _naive_utc(moment: datetime) -> str:
    """A UTC instant as the naive string the log writer uses.

    The writer stamps its entries with ``datetime.utcnow().isoformat()``:
    a naive string that means UTC **by convention**, not by declaration.
    Seeding the log with exactly that shape is what makes the reader's
    "naive means UTC" rule the thing under test.
    """
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


# ---------------------------------------------------------------------------
# The seed: the row and the log file the restarted backend has to find
# ---------------------------------------------------------------------------


def _seed(work: Path, plan_id: str) -> None:
    """Write everything the restarted backend has to find on disk.

    Two artefacts, one per source of truth:

    * the ``plan_execution`` row in ``work/state.db`` — the durable
      record of the run, and the row the boot-time recovery scan reads;
    * ``work/plans/<plan_id>/execution.log`` — the activity, in the exact
      shape ``backend/execution_logger.py`` writes it (one JSON object per
      line, ``ts`` a naive UTC stamp).

    The pid on the row is **this** process: the boot scan probes the pid
    of a ``running`` row and judges a missing process to be a crash, so
    the row has to hang on something alive.  The start time is written the
    way the pre-fix writer wrote it — ``datetime.now().isoformat()``,
    naive and local — because that is the shape the reader has to make
    sense of after the restart.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    db_path = work / "state.db"
    conn = db_open(db_path)
    try:
        # The tables first: the backend opens this database on boot and
        # reads the row out of it, so the schema has to be there before
        # the row is written — not after.
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "executing")
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            exec_status="running",
            exec_pid=os.getpid(),
            project_dir=str(work / "target"),
            started_at=datetime.now().isoformat(),
        )
    finally:
        conn.close()

    plan_dir = work / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    with (plan_dir / "execution.log").open("a", encoding="utf-8") as handle:
        for offset, event, message in (
            (120, "task_started", "e2e seed"),
            (30, "task_completed", "e2e seed"),
        ):
            entry = {
                "ts": _naive_utc(now - timedelta(seconds=offset)),
                "level": "INFO",
                "event": event,
                "message": message,
            }
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# The backend process
# ---------------------------------------------------------------------------


def _boot(work: Path, port: int) -> subprocess.Popen:
    """Start a real ``backend.server`` process against ``work``.

    The three environment variables are the whole contract between the
    seed and the server: the plans tree, the state database and the port.
    Four more are set because the child would otherwise inherit the test
    process's environment and misbehave:

    * ``PDT_PROVIDER_MODE=inherit`` — the boot asks CC Switch for the
      provider chain and **aborts the startup** when it cannot be asked.
      This server exists to answer one status question; it must not need
      a provider fleet to come up.
    * ``PYTHONPATH`` — ``backend/`` has to be importable, because the
      server's own modules import each other by their flat names
      (``from config_paths import ...``).
    * ``PDT_ORPHAN_REAPER=0`` — the boot otherwise walks **every process
      on the machine** to find orphans left behind by a previous life.
      On a busy box that scan takes a minute, and the request guard's
      self-check — which stops an instance that is not listening within
      60 s — kills the boot before it ever answers.  A throwaway server
      has no orphans to reap: the reaper is off.
    * ``PYTEST_CURRENT_TEST`` — the product's own answer to "may this
      process touch the machine?", asked in
      ``server._orphan_reaper_may_act`` and in
      ``state_machine.db.connection``: a *test* process must not reap the
      developer's processes and must never open the operator's live
      ``state.db``.  This child is a test process; saying so keeps it
      from doing either, whether or not the parent happened to be pytest.

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
            "PYTEST_CURRENT_TEST": (
                "backend/tests/e2e/test_execution_status_restart_e2e.py (setup)"
            ),
            "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
        }
    )
    # A stale secret descriptor must never be closed by a process that
    # never owned it (the sibling E2E file carries the full account).
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)
    env.pop("ANTHROPIC_BASE_URL", None)

    log_path = work / f"server-{port}.log"
    handle = log_path.open("wb")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "backend.server"],
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
            status, body = _http_json("GET", f"{base_url}/api/health")
            if status == 200:
                return
            last = (status, body)
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last = exc
        time.sleep(0.2)
    raise AssertionError(
        f"{base_url}/api/health was not ready within {READY_TIMEOUT_SECONDS}s; "
        f"last={last!r}\n{_server_output(proc)}"
    )


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
                proc.kill()
                proc.wait(timeout=10)
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover - the handle is ours
                pass


# ---------------------------------------------------------------------------
# The fixture: boot, kill, boot again
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def restarted_status(tmp_path_factory) -> Dict[str, Any]:
    """The status answer of a backend that has been restarted.

    The sequence is the whole point: a first boot (which recovers the run
    into its memory), a kill (which takes that memory with it), and a
    second boot on the same plans tree and the same state database.  The
    answer comes from the second process — it can only know what is on
    disk.
    """
    work = tmp_path_factory.mktemp("status-restart-e2e")
    _seed(work, PLAN_ID)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    first = _boot(work, port)
    try:
        _wait_ready(base_url, first)
    finally:
        _teardown(first)

    second = _boot(work, port)
    try:
        _wait_ready(base_url, second)
        status, body = _http_json(
            "GET", f"{base_url}/api/execution/{PLAN_ID}/status"
        )
        assert status == 200, f"the status endpoint answered {status}: {body!r}"
        assert isinstance(body, dict), f"the status answer is not an object: {body!r}"
        yield body
    finally:
        _teardown(second)


# ---------------------------------------------------------------------------
# The two assertions
# ---------------------------------------------------------------------------


def test_logs_survive_real_backend_restart(restarted_status: Dict[str, Any]):
    """The logs are still there after a real restart, and they are fresh.

    The first boot's memory is gone — the process that held it was killed.
    What the second process answers has to come from the log file on
    disk, in the one shape a reader can parse, and the last line has to
    be recent enough to say "this run is still going".
    """
    logs = restarted_status.get("logs")
    assert isinstance(logs, list), f"logs is not a list: {logs!r}"
    assert logs, "the status answer carries no logs after the restart"

    last = logs[-1]
    assert isinstance(last, str), f"the last log line is not text: {last!r}"
    assert LINE_SHAPE_RE.match(last), (
        f"the last log line does not open with an ISO-8601 stamp: {last!r}"
    )

    stamp = _parse_stamp(last)
    assert stamp.utcoffset() is not None, (
        f"the stamp names no offset, so it names no instant: {last!r}"
    )
    age = abs((datetime.now(timezone.utc) - stamp).total_seconds())
    assert age < FRESH_WINDOW_SECONDS, (
        f"the last log line is {age:.0f}s old, which is not fresh: {last!r}"
    )


def test_started_at_has_offset_after_restart(restarted_status: Dict[str, Any]):
    """``started_at`` still says which instant it names, after the restart.

    The row was written the way the pre-fix writer wrote it: a naive
    local stamp.  Read as UTC it would be eight hours off, and a reader
    that computes an age from it would call a two-minute-old run
    eight-hour-old.  So the answer has to carry an offset, and the
    instant it names has to be the one that was written.
    """
    started_at = restarted_status.get("started_at")
    assert started_at, f"the status answer carries no start time: {started_at!r}"
    assert isinstance(started_at, str), (
        f"the start time is not text: {started_at!r}"
    )

    parsed = datetime.fromisoformat(started_at)
    assert parsed.utcoffset() is not None, (
        f"the start time names no offset, so it names no instant: {started_at!r}"
    )

    age = abs((datetime.now(timezone.utc) - parsed).total_seconds())
    assert age < FRESH_WINDOW_SECONDS, (
        f"the start time is {age:.0f}s away from now, so it names a "
        f"different instant than the one that was written: {started_at!r}"
    )
