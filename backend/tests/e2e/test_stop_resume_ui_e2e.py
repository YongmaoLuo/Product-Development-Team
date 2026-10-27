"""E2E: the session binding is on disk while a real dispatch is in flight.

The chain this file walks is the one an operator actually exercises::

    server --POST /api/execution/<plan>/start--> executor (a real
    ``backend/cli.py`` subprocess) --spawn--> claude (a real child
    process)

Every link in that chain is real here.  The only thing that is replaced
is the *model*: ``claude`` itself is a fake that reads its stdin, emits
the ``system`` / ``init`` event, records what it was asked for, and then
stays alive forever.  It never answers, so the dispatch stays blocked --
which is the state this file is about.

Why "blocked" is the interesting state
--------------------------------------

The binding (``task_session_bound`` in the plan's ``execution.log``) is
the only durable record of which session a task is bound to.  The stop
path reads it out of the log (``pause_state.collect_task_sessions``) to
re-open that session on a resume.  When the record was only written
*after* the dispatch returned, a dispatch that blocked -- a provider
that hangs, a CLI that waits -- left the log with no binding at all:
the stop could only answer ``no_session_id`` and fell back to a full
re-run of the plan.

So the fact proved here is narrow and sharp: **while the dispatch is
still in flight, the log already carries the binding** -- and it carries
the id the dispatch was actually asked for.

What this file asserts
----------------------

Three tests, one chain, three vantage points:

* ``test_binding_exists_while_real_dispatch_is_in_flight`` -- start ->
  executor -> claude child -> the binding is in the log while the
  dispatch is still running;
* ``test_stop_then_restart_resumes_the_same_session`` -- the same chain
  plus the stop: stop -> the checkpoint records ``session_resume`` ->
  the next start dispatches ``--resume <the same id>``;
* ``test_stop_then_start_resumes_the_same_session`` -- the same chain
  through the **served page**: the operator clicks 停止执行, the
  checkpoint records ``session_resume``, and the operator clicks 开始
  执行 again -- the next dispatch re-opens the same session.

The first two are HTTP-level: they read the plan's own
``execution.log``, the plan's pause checkpoint, and the state of a
running process, none of which a browser can show.  The third one is
the operator's own path, so it drives the real page in a real browser
(through the same ``sys.argv`` driver script the sibling UI file
uses).  It is also the one that asserts the stop leaves **no orphan**:
for that the fake ``claude`` has to be a live one -- keeping its
stdout busy, the way the real CLI does -- because the stop opens by
closing the child's stdout, and a child that never writes again
cannot be closed that way.

Isolation
---------

Every test boots its own server against its own ``tmp_path`` (its own
state DB, its own plans tree) and tears it down in a ``finally``, so a
leaked server cannot influence the next test and a failed assertion
cannot leave a listener behind.  The port is picked by the kernel
(``_free_port``) -- never a hard-coded 8000/8001.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
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

# Prefer the project venv: the backend subprocess this test boots must run
# on the interpreter this suite was installed into.  A checkout with no
# ``backend/.venv`` (fresh worktree, container) falls back to the
# interpreter already running pytest, so the server really boots.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: Every ``/api/*`` request must carry this header (``backend/request_guard.py``).
REQUEST_HEADERS = {
    "X-PDT-Request": "1",
    "Content-Type": "application/json",
}

#: The plan this file drives.  The date prefix keeps it out of the
#: archived-plan guard (``classify_plan`` reads a ``YYYYMMDD-*`` dirname
#: as the plan's creation date, and anything on or before the
#: 2026-08-05 cutoff is archived and read-only).
PLAN_ID = "20261026-stop-resume-e2e"

#: The one task the seeded plan carries.  Its id is what the binding
#: event names.
TASK_ID = "T1"

#: How long the fake ``claude`` may take to show up (the executor has to
#: boot, run the pre-flight and reach the dispatch first).
DISPATCH_TIMEOUT = 180.0

#: The SIGTERM grace the server gets, in seconds.
#:
#: The default (10 s) is not enough room for the stop of *this* chain,
#: and the difference is not cosmetic.  The executor's stop sequence
#: opens by closing the child's stdout; with a real CLI that ends the
#: child's write and the child leaves.  The *silent* fake the first two
#: tests in this file use never writes again, so the close blocks on the
#: lock the dispatch's reader thread holds, the sequence wedges, and the
#: executor leaves only when its own wait runs out -- at ``0.9 x
#: grace``.  The server escalates to SIGKILL at ``grace`` and a
#: SIGKILLed executor is recorded as ``full_rerun`` instead of
#: ``session_resume``: the resume would silently stop resuming.  So the
#: margin that matters is the last ``0.1 x grace``, and it has to be
#: wide enough for a loaded machine.  30 s buys three seconds of
#: margin; the stop of the silent fake costs ~27 s.  The *live* fake
#: (the one the page walk uses) never reaches that: its stream stays
#: busy, the close completes, and the stop costs ~1.5 s.
STOP_GRACE_SECONDS = "30"


# ---------------------------------------------------------------------------
# Read-only reuse of the sibling E2E file
# ---------------------------------------------------------------------------


def _load_reused_helpers():
    """Load ``test_fastfail_no_ghost_state_ui.py`` **without** modifying it.

    Two helpers are reused as they are: ``_free_port()`` (the kernel picks
    a free port) and ``_playwright_interpreter()`` (the interpreter that
    can import playwright).  ``backend/tests/e2e/`` has no ``__init__.py``,
    so a plain cross-module import would depend on pytest's rootdir-based
    ``sys.path`` juggling; loading by **file path** avoids that without
    copying a single line of it.
    """
    path = Path(__file__).with_name("test_fastfail_no_ghost_state_ui.py")
    spec = importlib.util.spec_from_file_location("pdt_reused_stop_resume_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the reused helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REUSED = _load_reused_helpers()

#: Bind an ephemeral loopback port (the kernel picks it).
_free_port = _REUSED._free_port
#: An interpreter that can import ``playwright``, or ``None``.
_playwright_interpreter = _REUSED._playwright_interpreter
#: The environment the browser driver runs under (``$HOME`` points back at
#: the home the run was launched with, so Playwright finds its browsers).
_driver_env = _REUSED._driver_env


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _decode(raw: bytes) -> Any:
    """JSON when the body is JSON, the text otherwise."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def _http(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Tuple[int, Any]:
    """One HTTP request against the test server; ``(status, body)``."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in REQUEST_HEADERS.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())


def _wait_for_plan_listed(
    base_url: str,
    plan_id: str,
    timeout: float = 30.0,
) -> List[Dict[str, Any]]:
    """Wait until the served plan listing shows ``plan_id``.

    The server caches the plan listing for two seconds
    (``backend/server.py``'s ``_PLANS_CACHE``).  The health check that
    waits for the server to come up reads the listing *before* the plan
    is seeded, so a listing that is read right after the seed is
    answered from that cache -- and answers empty.  The page reads the
    listing exactly once, so the test waits for a listing that knows
    about the plan before the browser opens the page.
    """
    deadline = time.time() + timeout
    body: Any = []
    while True:
        status, body = _http("GET", f"{base_url}/api/plans", timeout=30)
        assert status == 200, (
            f"the plan listing answered {status}: {body!r}"
        )
        plans = body if isinstance(body, list) else (body or {}).get("plans", [])
        if any(plan.get("id") == plan_id for plan in plans):
            return plans
        if time.time() >= deadline:
            raise AssertionError(
                f"the plan listing never showed {plan_id!r} within {timeout}s; "
                f"it answered {body!r}"
            )
        time.sleep(0.5)


def _wait_for_api(base_url: str, path: str, timeout: float = 60.0) -> int:
    """Poll ``GET base_url + path`` until it answers 200; return the status.

    The backend is up when the API answers, not when the process exists.
    """
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        try:
            status, body = _http("GET", f"{base_url}{path}", timeout=5)
            if status == 200:
                return status
            last = (status, body)
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last = exc
        time.sleep(0.2)
    raise RuntimeError(
        f"{base_url}{path} did not answer 200 within {timeout}s; last={last!r}"
    )


# ---------------------------------------------------------------------------
# The fake ``claude``
# ---------------------------------------------------------------------------
#
# The fake is a *child process*, not a stubbed function: the whole point
# of this file is the process chain (server -> executor -> claude), and a
# function call would collapse the two links that matter.
#
# It ignores SIGTERM on purpose.  A real ``claude`` does not ignore
# SIGTERM, but the stop chain has to cope with a child that does, and a
# fake that died politely on SIGTERM would hide exactly the case the
# stop path is built for.

_FAKE_CLAUDE = r'''#!{interpreter}
"""A fake ``claude`` CLI: it never answers, it just stays alive.

The real CLI is replaced by this one so an end-to-end test can hold a
dispatch open for as long as it needs -- with no model, no provider and
no network.

What it does, in order:

1. reads stdin to EOF (the parent writes the prompt and closes stdin);
2. emits the ``system`` / ``init`` event that carries the session id --
   the id it was handed by ``--session-id`` (a new session) or
   ``--resume`` (an existing one);
3. appends one JSON line to ``<work>/claude_invocations.jsonl`` -- the
   record the test reads to learn what the dispatch was asked for;
4. ignores SIGTERM and sleeps forever.

Step 4 is the point: the dispatch stays blocked, so the test can look
at the state of a *running* dispatch rather than of a finished one.

``KEEPALIVE`` turns the fake into a *live* CLI: it keeps writing to
its stdout while it works, the way the real one does.  The stop
sequence opens by closing the child's stdout -- with a stream that is
still in use the close completes, the child writes into a broken pipe
and leaves.  A child that never writes again cannot be closed that
way: the close waits for a read that never returns, so the sequence
wedges and the child survives the stop as an orphan.  A test that has
to see the stop *reap* its child needs the live fake.
"""

#: Whether the fake keeps its stdout busy (see the note above).
KEEPALIVE = {keepalive}

import json
import os
import signal
import sys
import time
from pathlib import Path


def _session_id(argv):
    """The session id the dispatch asked for, or ``""``."""
    for index, arg in enumerate(argv):
        if arg in ("--session-id", "--resume") and index + 1 < len(argv):
            return argv[index + 1]
    return ""


def main():
    argv = sys.argv[1:]
    session_id = _session_id(argv)

    # The parent writes the prompt and closes stdin; reading to EOF is as
    # far along as the dispatch gets before it blocks on the answer.
    try:
        sys.stdin.read()
    except Exception:
        pass

    sys.stdout.write(json.dumps({
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
    }) + "\n")
    sys.stdout.flush()

    record = {
        "argv": argv,
        "session_id": session_id,
        "resumed": "--resume" in argv,
        "pid": os.getpid(),
        "cwd": os.getcwd(),
    }
    log_path = Path(__file__).resolve().parent.parent / "claude_invocations.jsonl"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    # A child that outlives its stop is an orphan; ignoring SIGTERM is
    # the hard case the stop chain has to win.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.5)
        if KEEPALIVE:
            # The stream stays busy, so the stop's first stage -- close
            # the child's stdout -- finds a read that returns.  The
            # child then writes into a broken pipe and leaves, which is
            # what the stop sequence is built around.
            sys.stdout.write(
                json.dumps({"type": "system", "subtype": "keepalive"}) + "\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
'''


def _install_fake_claude(work: Path, keepalive: bool = False) -> Path:
    """Write the fake ``claude`` into ``<work>/fake_bin``; return that dir.

    The directory is what goes on ``PATH`` for the server (and therefore
    for the executor it spawns).  The fake writes its invocation record
    to ``<work>/claude_invocations.jsonl`` -- the parent of its own
    directory -- so the test can find it without passing a path through
    the environment.

    ``keepalive`` makes the fake a *live* CLI: it keeps writing to its
    stdout while it works.  The default (``False``) is the silent fake
    the other tests in this file use -- it never writes again, so the
    stop sequence wedges on the closed pipe and the child is left to
    the teardown.  A test that asserts the stop *reaps* its child
    needs the live one: the real CLI keeps its stream busy, dies on
    the broken pipe, and leaves no orphan.
    """
    bin_dir = work / "fake_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    interpreter = sys.executable or "/usr/bin/env python3"
    script = bin_dir / "claude"
    # A plain replacement, not ``str.format``: the fake's own body is full
    # of JSON braces, and ``format`` would read every one of them as a
    # field name.
    script.write_text(
        _FAKE_CLAUDE.replace("{interpreter}", interpreter).replace(
            "{keepalive}", "True" if keepalive else "False"
        ),
        encoding="utf-8",
    )
    # The executor runs it as a plain command: it has to be executable.
    script.chmod(0o755)
    return bin_dir


def _invocations(work: Path) -> List[Dict[str, Any]]:
    """Every fake-``claude`` invocation recorded so far.

    One record per line of ``<work>/claude_invocations.jsonl``; an
    unwritten file means "the dispatch never reached the CLI".
    """
    path = work / "claude_invocations.jsonl"
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _wait_for_invocations(
    work: Path,
    timeout: float = DISPATCH_TIMEOUT,
    count: int = 1,
) -> List[Dict[str, Any]]:
    """Block until the fake ``claude`` has been invoked at least *count* times.

    ``count=1`` waits for the first dispatch; ``count=2`` waits for the
    dispatch that follows a resume, so the second record is the one the
    resumed run produced.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        records = _invocations(work)
        if len(records) >= count:
            return records
        time.sleep(0.2)
    return []


def _pid_alive(pid: int) -> bool:
    """Is ``pid`` still a live process?"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# The plan the server sees
# ---------------------------------------------------------------------------


def _make_checkout(work: Path) -> Path:
    """A real git checkout for the plan's task to work on."""
    checkout = work / "checkout"
    (checkout / "backend").mkdir(parents=True, exist_ok=True)
    (checkout / "backend" / "chart.py").write_text("# placeholder\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(checkout), check=False)
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=False)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.email=e2e@example.invalid",
            "-c",
            "user.name=pdt-e2e",
            "commit",
            "-qm",
            "e2e checkout",
        ],
        check=False,
    )
    return checkout


def _seed_plan(work: Path, plan_id: str = PLAN_ID) -> Path:
    """Create the plan directory and the rows the start route needs.

    Two things have to be on disk before ``POST /api/execution/<id>/start``
    can do anything:

    * ``plans/<plan_id>/tasks.json`` -- the route answers 404 without it.
      The one task carries ``test_command: "false"``: the pre-flight runs
      it, it exits non-zero, and the task therefore falls through to the
      *dispatch* path (a pre-flight that exits 0 would mark the task
      completed and never reach the model at all).  ``false`` is also
      instant, so the test does not sit through the 60 s pre-flight cap.
    * the state-machine rows -- ``plan_routing`` (the start CASes the
      phase from ``queued``) and ``plan_execution`` (the row the run
      state is written into).

    Returns the plan directory.
    """
    plan_dir = work / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "requirement": "prove the binding exists while a dispatch is in flight",
                "tasks": [
                    {
                        "id": TASK_ID,
                        "title": "hold the dispatch open",
                        "description": "The fake claude never answers.",
                        "test_command": "false",
                        "files_to_modify": [],
                        "depends_on": [],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(work / "state.db")
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "queued")
        ExecutionRepository(conn).insert(plan_id, exec_status="not_started")
        conn.commit()
    finally:
        conn.close()
    return plan_dir


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


def _boot_server(work: Path, fake_bin: Path) -> Tuple[str, subprocess.Popen]:
    """Boot a real ``backend.server`` subprocess; return ``(base_url, proc)``.

    The environment is the interesting part:

    * ``PATH`` starts with the fake ``claude``'s directory, so the
      executor's dispatch runs the fake and not a real CLI;
    * ``PDT_PROVIDER_MODE=inherit`` -- no CC Switch, no provider chain,
      no credentials.  The dispatch inherits whatever the parent process
      has, which for the fake CLI is nothing at all;
    * ``PDT_LLM_MODE`` is deliberately **not** set: in ``scripted`` mode
      the coding tool answers from a fixture and never spawns a CLI, so
      the process this file is about would not exist.
    """
    plans_dir = work / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()

    env = dict(os.environ)
    env.update(
        {
            "PDT_STATE_DB_PATH": str(work / "state.db"),
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_HOST": "127.0.0.1",
            "PDT_PORT": str(port),
            "PDT_DEBUG_ROUTES": "1",
            "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_PROVIDER_CAPACITY_FILE": str(work / "absent.yaml"),
            "PDT_DISABLE_FALSIFIABILITY_PROBE": "1",
            "PDT_STOP_GRACE_SECONDS": STOP_GRACE_SECONDS,
            "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        }
    )
    env.pop("ANTHROPIC_BASE_URL", None)
    # A throwaway test server has no secret to receive, and a stale
    # descriptor number must never be closed by a process that never
    # owned it.
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)

    log_handle = (work / "server.log").open("wb")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "backend.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_api(base_url, "/api/plans", timeout=60.0)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        log_handle.close()
        raise
    proc._pdt_log_handle = log_handle  # type: ignore[attr-defined]
    return base_url, proc


def _kill_leftover_executors(plan_id: str) -> None:
    """Kill an executor that outlived the server (it is found by its id).

    The executor's command line carries the plan id, so the id is
    enough to find it.
    """
    if not plan_id:
        return
    try:
        listing = subprocess.run(
            ["pgrep", "-f", plan_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    for line in listing.stdout.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except OSError:
                break
            time.sleep(0.5)
            if not _pid_alive(pid):
                break


def _kill_fake_claudes(work: Path) -> None:
    """Kill any fake ``claude`` that outlived the stop.

    The fake ignores SIGTERM on purpose, so this has to escalate to
    SIGKILL.  The pids come from the invocation record -- the file the
    fake itself wrote -- rather than from a process-table scan, so the
    test kills exactly the children it started.
    """
    for record in _invocations(work):
        pid = record.get("pid")
        if not isinstance(pid, int):
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except OSError:
                break
            time.sleep(0.5)
            if not _pid_alive(pid):
                break


def _teardown(base_url: str, proc: subprocess.Popen, plan_id: str, work: Path) -> None:
    """Leave nothing behind: the executor, the fake children, the server.

    The order is deliberate, and it is not the polite one.  The
    executor is killed FIRST, by its plan id: it is the process that
    owns the fake ``claude`` child, and the server's own shutdown would
    otherwise stop the executions it knows about *politely* -- for as
    long as the stop grace allows, which with this file's grace is
    half a minute.  Killing the executor first keeps the teardown
    bounded at a couple of seconds.

    The fake ignores SIGTERM -- that is its contract -- so it is
    killed with SIGKILL, and its pids come from the record the fake
    itself wrote rather than from a process-table scan: the test kills
    exactly the children it started.

    The stop *endpoint* is not called here.  It is exercised on purpose
    by the test that is about stopping, and a teardown that stopped the
    plan as well would pay the grace a second time for nothing.

    ``base_url`` is accepted so that the teardown has the same shape as
    the boot it undoes; this teardown does not call the API.
    """
    _kill_leftover_executors(plan_id)
    _kill_fake_claudes(work)

    log_handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Reading the binding out of the execution log
# ---------------------------------------------------------------------------


def _bindings(log_path: Path, task_id: str) -> List[Dict[str, Any]]:
    """Every ``task_session_bound`` event the log carries for ``task_id``."""
    if not log_path.exists():
        return []
    found: List[Dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("event") != "task_session_bound":
            continue
        if str(entry.get("task_id")) != task_id:
            continue
        found.append(entry)
    return found


# ---------------------------------------------------------------------------
# Reading the stop's checkpoint
# ---------------------------------------------------------------------------


def _checkpoint(plan_dir: Path) -> Dict[str, Any]:
    """The plan's pause checkpoint, or ``{}`` when there is none.

    ``pause_state.PAUSE_CHECKPOINT_FILENAME`` is the file the stop
    writes; it is named here rather than imported so this file stays
    importable without the backend on ``sys.path``.
    """
    path = plan_dir / "pause_state.json"
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


def _interrupted(checkpoint: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    """The checkpoint's entry for the interrupted task, or ``{}``."""
    entries = checkpoint.get("interrupted_tasks")
    if not isinstance(entries, list):
        return {}
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("task_id")) == task_id:
            return entry
    return {}


# ---------------------------------------------------------------------------
# The browser pass
# ---------------------------------------------------------------------------
#
# The operator's path is the one the page offers: pick the plan, start it,
# stop it, start it again.  The two facts that only a page can show are
# the badge the operator reads and the screenshots of what they saw --
# everything else (the checkpoint, the dispatch's command line, the state
# of the child process) is read from the disk by the test itself.
#
# The browser runs in its own process because the interpreter that runs
# pytest is not necessarily the one that has Playwright installed; the
# driver script is handed its parameters through ``sys.argv`` and reports
# what it saw as one JSON line on stdout -- the same shape the sibling
# UI file uses.

#: The badge the page shows for a stopped plan (``frontend/app.js``'s
#: ``setExecBadge``: ``stopped`` -> 已停止).
STOPPED_BADGE = "已停止"

#: The driver subprocess' own budget.  It has to stay below the suite's
#: per-test timeout, because the server boot and the teardown sit on
#: either side of it.
_DRIVER_TIMEOUT_SECONDS = 240

_STOP_RESUME_DRIVER = r'''
"""Drive the served page: stop the execution, then start it again.

Run as::

    python drive_stop_resume.py <url> <plan_id> <checkout> <work> <shots>

Every step is a click on the page, so the frontend's own handlers run.
The driver reports what the page showed; the assertions live in the test
that runs it.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot launch
    anything.  The binaries that *are* installed are found here instead --
    nothing is downloaded, and nothing is hard-coded: the cache lives
    under the home the run was launched with.
    """
    home = Path.home()
    roots = [
        home / "Library" / "Caches" / "ms-playwright",
        home / ".cache" / "ms-playwright",
    ]
    patterns = (
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-linux*/chrome",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern)):
                if candidate.exists():
                    return str(candidate)
    return None


def wait_until(predicate, timeout, message):
    """Block until *predicate* holds; raise when it does not."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(message)


url, plan_id, checkout, work_dir, shots_dir = sys.argv[1:6]

work = Path(work_dir)
shots = Path(shots_dir)
shots.mkdir(parents=True, exist_ok=True)

#: The file the fake claude appends to every time it is invoked.
invocations = work / "claude_invocations.jsonl"
#: The file the stop writes the resume point into.
checkpoint = work / "plans" / plan_id / "pause_state.json"


def invocation_count():
    """How many times the fake claude has been invoked so far."""
    if not invocations.exists():
        return 0
    text = invocations.read_text(encoding="utf-8", errors="replace")
    return len([line for line in text.splitlines() if line.strip()])


executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

report = {"page_errors": [], "console_errors": [], "dialogs": []}

with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    def on_page_error(exc):
        report["page_errors"].append(str(exc))

    def on_console(message):
        if message.type == "error":
            report["console_errors"].append(message.text)

    def on_dialog(dialog):
        report["dialogs"].append(dialog.message)
        dialog.dismiss()

    page.on("pageerror", on_page_error)
    page.on("console", on_console)
    page.on("dialog", on_dialog)

    card = '.plan-card[data-plan="%s"]' % plan_id

    # --- 1: the plan list ---------------------------------------------
    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)
    page.wait_for_selector(card, timeout=30000)
    report["plan_card_visible"] = page.is_visible(card)
    page.screenshot(path=str(shots / "1-plan.png"), full_page=True)

    # --- 2: into the task view ----------------------------------------
    page.click(card)
    page.wait_for_selector("#tasks-view:not(.hidden)", timeout=30000)
    page.wait_for_selector("#btn-start-execution", state="visible", timeout=30000)
    report["tasks_view_visible"] = page.is_visible("#tasks-view")
    report["start_button_visible"] = page.is_visible("#btn-start-execution")

    # --- 3: start the execution ---------------------------------------
    page.click("#btn-start-execution")
    page.wait_for_selector("#btn-confirm-execute", state="visible", timeout=15000)
    page.fill("#project-dir-input", checkout)
    page.click("#btn-confirm-execute")
    wait_until(
        lambda: invocation_count() >= 1,
        120,
        "the first dispatch never reached the claude CLI",
    )
    report["first_invocation_seen"] = True
    page.screenshot(path=str(shots / "2-dispatch.png"), full_page=True)

    # --- 4: stop it ----------------------------------------------------
    # The badge is watched from here on.  The page's own stop handler
    # sets it to 已停止 when the stop answers; a poll that was already
    # in flight when the stop started can repaint it afterwards with
    # the status it read (the plan was still running then).  Every value
    # the badge is given is recorded, so the test can assert on what
    # the operator saw instead of on one reading that a late poll can
    # race.
    page.evaluate(
        "() => {"
        " const b = document.querySelector('#execution-status-badge');"
        " window.__badgeValues = [];"
        " const record = (value) => {"
        "   if (value) { window.__badgeValues.push(value); }"
        " };"
        " record(b ? b.textContent.trim() : '');"
        " new MutationObserver((records) => {"
        "   for (const r of records) {"
        "     for (const node of r.addedNodes || []) {"
        "       if (node.textContent) { record(node.textContent.trim()); }"
        "     }"
        "   }"
        " }).observe(b, {childList: true, characterData: true, subtree: true});"
        "}"
    )
    page.click("#btn-stop-execution")
    wait_until(
        lambda: checkpoint.exists(),
        120,
        "the stop wrote no pause checkpoint",
    )
    # The checkpoint is read HERE, in the moment the brief describes.
    # The resume that follows *consumes* it -- a resume is single-shot
    # (backend/pause_state.py's ``consume_pause_checkpoint`` reads the
    # file and deletes it) -- so by the time the walk is over the file
    # is gone, and what it said is what the walk is about.
    report["checkpoint"] = json.loads(checkpoint.read_text(encoding="utf-8"))
    page.wait_for_function(
        "() => (window.__badgeValues || []).indexOf('已停止') !== -1",
        timeout=30000,
    )
    report["badge_values"] = page.evaluate("() => window.__badgeValues || []")
    report["badge_after_stop"] = page.evaluate(
        "() => { const b = document.querySelector('#execution-status-badge');"
        " return b ? b.textContent.trim() : ''; }"
    )
    page.screenshot(path=str(shots / "3-stopped.png"), full_page=True)

    # --- 5: start it again ---------------------------------------------
    # 重新执行 is what brings the confirm button back: the row that holds
    # it is hidden once a run has started (frontend/app.js), and the stop
    # does not show it again.
    page.click("#btn-start-execution")
    page.click("#btn-restart-execution")
    page.wait_for_selector("#btn-confirm-execute", state="visible", timeout=15000)
    page.fill("#project-dir-input", checkout)
    page.click("#btn-confirm-execute")
    wait_until(
        lambda: invocation_count() >= 2,
        120,
        "the resumed dispatch never reached the claude CLI",
    )
    report["second_invocation_seen"] = True
    page.screenshot(path=str(shots / "4-resumed.png"), full_page=True)

    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


class BrowserUnavailable(RuntimeError):
    """The browser pass could not run here: no driver, or no browser.

    Raised only for the two cases where *this machine* cannot drive a
    page -- the driver is not installed, or its browser binaries are
    not.  It is not a failure of the chain under test, so the caller
    falls back to the HTTP-level walk of the same chain.
    """


def _run_stop_resume_driver(
    interpreter: str,
    script: Path,
    base_url: str,
    plan_id: str,
    checkout: Path,
    work: Path,
    shots: Path,
) -> Dict[str, Any]:
    """Run the browser pass; return the report it printed."""
    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{base_url}/",
            plan_id,
            str(checkout),
            str(work),
            str(shots),
        ],
        capture_output=True,
        text=True,
        timeout=_DRIVER_TIMEOUT_SECONDS,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        if "Executable doesn't exist" in combined or (
            "playwright install" in combined
        ):
            raise BrowserUnavailable(combined)
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _wait_for_pid_gone(pid: int, timeout: float = 10.0) -> None:
    """Wait until ``os.kill(pid, 0)`` says the pid names no process.

    Polled rather than checked once: a corpse the executor has not
    reaped yet still holds its table entry, so a single check could
    report an orphan that is really a zombie about to be collected.
    """
    deadline = time.time() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if time.time() >= deadline:
            raise AssertionError(
                f"the claude child (pid {pid}) is still alive {timeout}s "
                "after the stop; the stop left an orphan behind"
            )
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_binding_exists_while_real_dispatch_is_in_flight(tmp_path):
    """The binding is in the log while the dispatch is still running.

    The whole chain is real: the server spawns the executor, the
    executor spawns the ``claude`` child, and the child never answers.
    The binding must therefore already be on disk -- written *before*
    the dispatch, not after it returned.

    What is asserted:

    * the fake ``claude`` was invoked (the chain reached the model);
    * the execution log carries a ``task_session_bound`` event for the
      task, with a non-empty session id;
    * that id is the one the dispatch was actually asked for (the
      ``--session-id`` value the child saw on its command line);
    * the child is still alive -- the dispatch is in flight, not over.
    """
    work = tmp_path
    fake_bin = _install_fake_claude(work)
    base_url, proc = _boot_server(work, fake_bin)
    try:
        _seed_plan(work, PLAN_ID)
        checkout = _make_checkout(work)

        status, body = _http(
            "POST",
            f"{base_url}/api/execution/{PLAN_ID}/start",
            {"project_dir": str(checkout)},
            timeout=120,
        )
        assert status == 200, (
            f"starting the execution answered {status}: {body!r}"
        )

        invocations = _wait_for_invocations(work, timeout=DISPATCH_TIMEOUT)
        assert invocations, (
            "the executor never invoked the claude CLI; the dispatch did "
            f"not happen. server log:\n"
            f"{(work / 'server.log').read_text(encoding='utf-8', errors='replace')[-4000:]}"
        )
        invocation = invocations[0]
        assert invocation["session_id"], (
            f"the claude child was invoked without a session id: {invocation!r}"
        )
        assert "--session-id" in invocation["argv"], (
            "a fresh dispatch must CREATE the session under ``--session-id``; "
            f"the child saw {invocation['argv']!r}"
        )

        log_path = work / "plans" / PLAN_ID / "execution.log"
        bindings = _bindings(log_path, TASK_ID)
        assert bindings, (
            "no ``task_session_bound`` event for the task while the "
            f"dispatch was in flight; the log at {log_path} carries "
            f"{_bindings(log_path, '__any__')} and "
            f"{log_path.read_text(encoding='utf-8', errors='replace')[-4000:] if log_path.exists() else '(no log)'}"
        )
        binding = bindings[-1]
        bound_session = (binding.get("data") or {}).get("session_id")
        assert bound_session, (
            f"the binding event carries no session id: {binding!r}"
        )
        assert bound_session == invocation["session_id"], (
            "the binding names a different session than the dispatch asked "
            f"for: bound={bound_session!r} asked={invocation['session_id']!r}"
        )

        assert _pid_alive(invocation["pid"]), (
            f"the claude child (pid {invocation['pid']}) is gone; the "
            "dispatch was not in flight any more, so the assertion above "
            "proves nothing about a running dispatch"
        )
    finally:
        _teardown(base_url, proc, PLAN_ID, work)


def test_stop_then_restart_resumes_the_same_session(tmp_path):
    """stop -> checkpoint says ``session_resume`` -> the next start resumes it.

    This is the other half of the stop/resume story, walked through the
    same real chain: the server stops the plan, the stop lands on the
    executor's process group, and the checkpoint the stop writes records
    the interrupted task with the session it was bound to.  The next
    start reads that checkpoint and dispatches with
    ``--resume <the same id>`` -- the conversation continues instead of
    starting over.

    What is asserted:

    * the stop answers ``stopped`` and reports the interrupted task;
    * the checkpoint records the task with ``strategy ==
      "session_resume"`` and the session id the dispatch was asked for
      (a hard kill would say ``full_rerun`` instead -- that is the
      difference this assertion pins down);
    * the next start answers ``resumed`` and dispatches the child with
      ``--resume <that same id>``, and that child is alive.

    What is *not* asserted is that the first child is gone after the
    stop.  It is not: the fake ignores SIGTERM and never writes to its
    stdout again, so the executor's stop sequence wedges on the closed
    pipe and the child is left to the teardown.  A real CLI would die
    on the broken pipe, which is what the sequence is built around; the
    fake cannot, so the assertion would be about the fake rather than
    about the chain.

    The facts asserted here are the checkpoint's contents and the flags
    on the child's command line; neither is visible on a page, which is
    why this file talks to the API directly.
    """
    work = tmp_path
    fake_bin = _install_fake_claude(work)
    base_url, proc = _boot_server(work, fake_bin)
    try:
        _seed_plan(work, PLAN_ID)
        checkout = _make_checkout(work)

        status, body = _http(
            "POST",
            f"{base_url}/api/execution/{PLAN_ID}/start",
            {"project_dir": str(checkout)},
            timeout=120,
        )
        assert status == 200, (
            f"starting the execution answered {status}: {body!r}"
        )

        invocations = _wait_for_invocations(work, timeout=DISPATCH_TIMEOUT)
        assert invocations, (
            "the executor never invoked the claude CLI; the dispatch did "
            f"not happen. server log:\n"
            f"{(work / 'server.log').read_text(encoding='utf-8', errors='replace')[-4000:]}"
        )
        first = invocations[0]
        session_id = first["session_id"]
        assert session_id, (
            f"the claude child was invoked without a session id: {first!r}"
        )

        # --- the stop -------------------------------------------------
        stop_status, stop_body = _http(
            "POST",
            f"{base_url}/api/execution/{PLAN_ID}/stop",
            {},
            timeout=120,
        )
        assert stop_status == 200, (
            f"stopping the execution answered {stop_status}: {stop_body!r}"
        )
        assert stop_body.get("status") == "stopped", (
            f"the stop did not report a stopped plan: {stop_body!r}"
        )
        assert stop_body.get("interrupted_task_count") == 1, (
            f"the stop reported no interrupted task: {stop_body!r}"
        )

        # --- the checkpoint the stop wrote ----------------------------
        checkpoint = _checkpoint(work / "plans" / PLAN_ID)
        interrupted = _interrupted(checkpoint, TASK_ID)
        assert interrupted, (
            f"the checkpoint names no interrupted task: {checkpoint!r}"
        )
        assert interrupted.get("strategy") == "session_resume", (
            "the stop did not record the interrupted task as resumable; "
            f"the checkpoint says {interrupted!r}"
        )
        assert interrupted.get("session_id") == session_id, (
            "the checkpoint names a different session than the dispatch "
            f"was asked for: checkpoint={interrupted.get('session_id')!r} "
            f"asked={session_id!r}"
        )

        # --- the next start resumes that session ----------------------
        restart_status, restart_body = _http(
            "POST",
            f"{base_url}/api/execution/{PLAN_ID}/start",
            {"project_dir": str(checkout)},
            timeout=120,
        )
        assert restart_status == 200, (
            f"restarting the execution answered {restart_status}: "
            f"{restart_body!r}"
        )
        assert restart_body.get("resumed") is True, (
            f"the restart did not resume the paused execution: "
            f"{restart_body!r}"
        )
        assert restart_body.get("resume_strategy") == "session_resume", (
            f"the restart did not pick the session_resume strategy: "
            f"{restart_body!r}"
        )

        resumed = _wait_for_invocations(work, timeout=DISPATCH_TIMEOUT, count=2)
        assert len(resumed) >= 2, (
            "the resumed execution never invoked the claude CLI; the "
            f"dispatch did not happen. invocations: {resumed!r}"
        )
        second = resumed[1]
        assert second.get("resumed") is True, (
            f"the second dispatch did not resume a session: {second!r}"
        )
        assert "--resume" in second["argv"], (
            "the resumed dispatch must RE-OPEN the session under "
            f"``--resume``; the child saw {second['argv']!r}"
        )
        assert second["session_id"] == session_id, (
            "the resumed dispatch re-opened a different session than the "
            f"one it was asked to continue: resumed={second['session_id']!r} "
            f"original={session_id!r}"
        )
        assert _pid_alive(second["pid"]), (
            f"the resumed claude child (pid {second['pid']}) is gone; the "
            "resumed dispatch was not in flight"
        )
    finally:
        _teardown(base_url, proc, PLAN_ID, work)


# ---------------------------------------------------------------------------
# The operator's path, through the served page
# ---------------------------------------------------------------------------


def _assert_stop_resume_chain_over_http(
    base_url: str,
    work: Path,
    checkout: Path,
) -> Dict[str, Any]:
    """The same chain without a browser: stop -> checkpoint -> start.

    This is the fallback for a machine that has no Playwright driver.
    It asserts the chain the browser pass asserts -- the checkpoint
    records ``session_resume``, the next start dispatches ``--resume
    <the same id>`` (and not ``--session-id``), and the stopped child is
    gone -- minus the two facts only a page can show: the badge and the
    screenshots.
    """
    status, body = _http(
        "POST",
        f"{base_url}/api/execution/{PLAN_ID}/start",
        {"project_dir": str(checkout)},
        timeout=120,
    )
    assert status == 200, f"starting the execution answered {status}: {body!r}"

    invocations = _wait_for_invocations(work, timeout=DISPATCH_TIMEOUT)
    assert invocations, (
        "the executor never invoked the claude CLI; the dispatch did "
        f"not happen. server log:\n"
        f"{(work / 'server.log').read_text(encoding='utf-8', errors='replace')[-4000:]}"
    )
    first = invocations[0]
    session_id = first["session_id"]
    assert session_id, f"the claude child was invoked without a session id: {first!r}"

    stop_status, stop_body = _http(
        "POST",
        f"{base_url}/api/execution/{PLAN_ID}/stop",
        {},
        timeout=120,
    )
    assert stop_status == 200, f"stopping the execution answered {stop_status}: {stop_body!r}"
    assert stop_body.get("status") == "stopped", (
        f"the stop did not report a stopped plan: {stop_body!r}"
    )

    checkpoint = _checkpoint(work / "plans" / PLAN_ID)
    interrupted = _interrupted(checkpoint, TASK_ID)
    assert interrupted, f"the checkpoint names no interrupted task: {checkpoint!r}"
    assert interrupted.get("strategy") == "session_resume", (
        "the stop did not record the interrupted task as resumable; "
        f"the checkpoint says {interrupted!r}"
    )
    assert interrupted.get("session_id") == session_id, (
        "the checkpoint names a different session than the dispatch was "
        f"asked for: checkpoint={interrupted.get('session_id')!r} "
        f"asked={session_id!r}"
    )

    _wait_for_pid_gone(first["pid"])

    restart_status, restart_body = _http(
        "POST",
        f"{base_url}/api/execution/{PLAN_ID}/start",
        {"project_dir": str(checkout)},
        timeout=120,
    )
    assert restart_status == 200, (
        f"restarting the execution answered {restart_status}: "
        f"{restart_body!r}"
    )
    assert restart_body.get("resume_strategy") == "session_resume", (
        f"the restart did not pick the session_resume strategy: "
        f"{restart_body!r}"
    )

    resumed = _wait_for_invocations(work, timeout=DISPATCH_TIMEOUT, count=2)
    assert len(resumed) >= 2, (
        "the resumed execution never invoked the claude CLI; the dispatch "
        f"did not happen. invocations: {resumed!r}"
    )
    second = resumed[1]
    assert "--resume" in second["argv"], (
        "the resumed dispatch must RE-OPEN the session under ``--resume``; "
        f"the child saw {second['argv']!r}"
    )
    assert second["argv"][second["argv"].index("--resume") + 1] == session_id, (
        "the resumed dispatch re-opened a different session than the one "
        f"it was asked to continue: {second['argv']!r}"
    )
    assert "--session-id" not in second["argv"], (
        "the resumed dispatch must not CREATE a session: "
        f"the child saw {second['argv']!r}"
    )
    return {"session_id": session_id, "first_pid": first["pid"]}


def test_stop_then_start_resumes_the_same_session(tmp_path):
    """The operator's path: stop on the page, start again, same session.

    The first two tests in this file walk the chain through the API.
    This one walks it the way the operator does -- on the served page:
    pick the plan, start it, stop it, start it again -- and asserts that
    the conversation continues instead of starting over.

    What is asserted:

    * the plan card is on the page and the task view opens when it is
      clicked (the operator got to the plan);
    * the first dispatch bound the task to a session, and the binding
      in the execution log names the very id the dispatch asked for;
    * the stop leaves a resume point behind: the checkpoint records the
      interrupted task with ``strategy == "session_resume"`` and the
      session id the dispatch was bound to;
    * the stopped ``claude`` child is gone -- the stop left no orphan
      behind (``os.kill(pid, 0)`` raises ``ProcessLookupError``);
    * the page's execution badge reads 已停止 after the stop;
    * the second start dispatches ``--resume <the same id>`` and does
      **not** dispatch ``--session-id`` (a new session would throw the
      conversation away);
    * the page raised no error and showed no dialog, and the four
      screenshots of the walk are on disk.

    The browser is driven by a script that is handed its parameters
    through ``sys.argv`` and reports what it saw as one JSON line --
    the same shape the sibling UI file uses.  When this machine has no
    Playwright driver (or no browser for it), the walk falls back to
    the HTTP-level chain: the same facts minus the two only a page can
    show (the badge and the screenshots).

    The fake ``claude`` here is the *live* one (it keeps its stdout
    busy, the way the real CLI does).  That is what makes "no orphan"
    a fact about the chain: the stop opens by closing the child's
    stdout, the close finds a stream that is still in use, the child
    writes into a broken pipe and leaves.  The silent fake the other
    tests in this file use cannot be closed that way -- the close
    waits for a read that never returns, the sequence wedges, and the
    child outlives the stop.  Asserting that the orphan is gone with
    the silent fake would assert something about the fake, not about
    the stop.
    """
    work = tmp_path
    fake_bin = _install_fake_claude(work, keepalive=True)
    base_url, proc = _boot_server(work, fake_bin)
    try:
        _seed_plan(work, PLAN_ID)
        _wait_for_plan_listed(base_url, PLAN_ID)
        checkout = _make_checkout(work)

        interpreter = _playwright_interpreter()
        report: Optional[Dict[str, Any]] = None
        shots = work / "shots"
        if interpreter is None:
            print(
                "[stop-resume-e2e] no Playwright driver on this machine; "
                "walking the same chain over HTTP instead"
            )
        else:
            script = work / "drive_stop_resume.py"
            script.write_text(_STOP_RESUME_DRIVER, encoding="utf-8")
            try:
                report = _run_stop_resume_driver(
                    interpreter, script, base_url, PLAN_ID, checkout, work, shots,
                )
            except BrowserUnavailable as exc:
                print(
                    "[stop-resume-e2e] the browser pass is unavailable here "
                    f"({exc}); walking the same chain over HTTP instead"
                )
                report = None

        if report is None:
            # The degraded path: the same chain, without the page.
            _assert_stop_resume_chain_over_http(base_url, work, checkout)
            return

        # --- the operator got to the plan ------------------------------
        assert report.get("plan_card_visible") is True, (
            "the plan card never appeared on the page; the operator could "
            f"not even pick the plan. report={report!r}"
        )
        assert report.get("tasks_view_visible") is True, (
            "clicking the plan card did not open the task view; the "
            f"operator never reached 开始执行. report={report!r}"
        )
        assert report.get("start_button_visible") is True, (
            f"the 开始执行 button is not on the page. report={report!r}"
        )

        # --- the first dispatch bound the session ----------------------
        invocations = _invocations(work)
        assert len(invocations) >= 1, (
            "the executor never invoked the claude CLI; the dispatch did "
            f"not happen. invocations={invocations!r}"
        )
        first = invocations[0]
        session_id = first["session_id"]
        assert session_id, (
            f"the claude child was invoked without a session id: {first!r}"
        )
        log_path = work / "plans" / PLAN_ID / "execution.log"
        bindings = _bindings(log_path, TASK_ID)
        assert bindings, (
            f"no ``task_session_bound`` event for the task in {log_path}; "
            f"the log carries {_bindings(log_path, '__any__')!r}"
        )
        bound_session = (bindings[-1].get("data") or {}).get("session_id")
        assert bound_session == session_id, (
            "the binding names a different session than the dispatch asked "
            f"for: bound={bound_session!r} asked={session_id!r}"
        )

        # --- the stop left a resume point, and no orphan ---------------
        # The checkpoint is read out of the walk, at the moment the
        # stop had written it: the resume that follows consumes it
        # (delete-after-read makes a resume single-shot), so the file
        # is gone by the time the walk is over.
        checkpoint = report.get("checkpoint") or {}
        interrupted = _interrupted(checkpoint, TASK_ID)
        assert interrupted, (
            f"the checkpoint names no interrupted task: {checkpoint!r}"
        )
        assert interrupted.get("strategy") == "session_resume", (
            "the stop did not record the interrupted task as resumable; "
            f"the checkpoint says {interrupted!r}"
        )
        assert interrupted.get("session_id") == session_id, (
            "the checkpoint names a different session than the dispatch "
            f"was asked for: checkpoint={interrupted.get('session_id')!r} "
            f"asked={session_id!r}"
        )
        _wait_for_pid_gone(first["pid"])
        with pytest.raises(ProcessLookupError):
            os.kill(first["pid"], 0)

        # The badge the operator reads.  The assertion is on the values
        # the badge was *given*, not on one reading of it: a poll that
        # was in flight when the stop started can repaint the badge
        # with the status it read (the plan was still running then),
        # and the value the stop put there is the one the brief is
        # about.
        badge_values = report.get("badge_values") or []
        assert STOPPED_BADGE in badge_values, (
            f"the execution badge never read {STOPPED_BADGE!r} after the "
            f"stop; the page showed {badge_values!r} "
            f"(it reads {report.get('badge_after_stop')!r} now)"
        )

        # --- the second start continues the same conversation ----------
        invocations = _invocations(work)
        assert len(invocations) >= 2, (
            "the resumed execution never invoked the claude CLI; the "
            f"dispatch did not happen. invocations={invocations!r}"
        )
        second = invocations[1]
        assert second.get("resumed") is True, (
            f"the second dispatch did not resume a session: {second!r}"
        )
        assert "--resume" in second["argv"], (
            "the resumed dispatch must RE-OPEN the session under "
            f"``--resume``; the child saw {second['argv']!r}"
        )
        assert second["argv"][second["argv"].index("--resume") + 1] == session_id, (
            "the resumed dispatch re-opened a different session than the "
            f"one it was asked to continue: {second['argv']!r}"
        )
        assert "--session-id" not in second["argv"], (
            "the resumed dispatch must not CREATE a session -- that would "
            f"throw the conversation away: the child saw {second['argv']!r}"
        )

        # --- the page did not blow up on the way -----------------------
        assert report.get("page_errors") == [], (
            f"the page raised errors during the walk: {report['page_errors']!r}"
        )
        assert report.get("dialogs") == [], (
            f"the page showed dialogs during the walk: {report['dialogs']!r}"
        )

        # --- the screenshots of the walk are on disk -------------------
        for name in ("1-plan.png", "2-dispatch.png", "3-stopped.png", "4-resumed.png"):
            shot = shots / name
            assert shot.exists() and shot.stat().st_size > 0, (
                f"the screenshot {name} was not written to {shots}"
            )
    finally:
        _teardown(base_url, proc, PLAN_ID, work)