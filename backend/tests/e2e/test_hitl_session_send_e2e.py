"""End-to-end: a decision asked in a session reaches that session.

Background
----------
A human-in-the-loop decision is worth nothing if it stops at the
backend.  The chain that carries it out of the building is four links,
and every one of them is covered by a unit test of its own — which is
exactly the problem: each link can be right while the message never
arrives.

1. ``routes/phases.py::start_interview`` records *where* a question
   about this plan should go — the session the plan was created in, and
   the destination to deliver to — in that plan's own
   ``hitl_config.json``.
2. ``hooks/config.py`` turns that origin into a **command**: the
   ``backend.hooks.session_send`` invocation that delivers, written
   down once, when the plan is created.
3. ``hooks/dispatcher.py`` runs that command with the decision event on
   its stdin, and writes down what happened in the plan's
   ``hooks_audit.jsonl``.
4. ``backend/hooks/session_send.py`` reads the event, formats it into
   the message a human reads, and hands it to ``cc-connect send``.

The unit tests call a link directly.  This file calls the *product*: a
real server, over real HTTP, on a real plan directory, and watches the
message arrive.  The failure it exists for is the one no single unit
test can see — a command that names the wrong interpreter, a
dispatcher nobody called, a transport that is not installed, a message
that arrives with the question left out of it.

Who plays the caller
--------------------
The product, through its own HTTP routes:

* ``POST /api/interview/start`` carrying a hook destination and a hook
  session — the plan is created and the binding lands in that plan's
  ``hitl_config.json``.
* ``POST /api/hitl/{plan_id}/decisions`` — the question is asked and the
  delivery runs.

There is no frontend page for this flow, so the case drives the HTTP
routes directly, the way ``test_fastfail_no_ghost_state_ui.py`` drives
the routes it needs.  A browser replay of the same sequence is added
below as optional extra evidence: it runs only where a Playwright
driver is already installed, and every assertion it makes repeats one
the browserless path has already made.

What the case pins
------------------
Four facts, and they have to hold together.  A message that arrives
without the decision's identity is not answerable; a decision that is
not ``pending`` after the delivery is not waiting for an answer; an
audit line that does not say ``delivered`` is the backend's opinion
about itself rather than a fact about the delivery.

1. The message is not empty, and it carries the decision's id.
2. The message carries the question as it was asked, and both of the
   options that were on the table.
3. The plan's ``hooks_audit.jsonl`` says the delivery reached one
   destination, and that destination was a ``cmd`` — the command the
   plan's own configuration named.
4. The decision is still ``pending``: it was asked, it reached
   somebody, and it is waiting for an answer.

And the negative control
------------------------
The case above proves that a message *can* arrive.  It cannot prove
that the report says so only when it did: a dispatcher that counted
"the command ran" as "the message arrived" would pass that case and
lie to every caller — telling it that a human was asked when nobody
was asked anything, and letting it settle the decision as if an answer
were on its way.

The negative control is the same sequence with the transport **not
installed**.  It pins the other three facts: the audit says ``failed``
and counts zero delivered destinations; the decision settles itself as
``unroutable``, because nobody is going to answer a question nobody
was asked; and nothing was sent, because there was nothing to send it
with.  Its own server, its own workdir — the environment that breaks
the delivery belongs to the server, not to a plan.

How the box is built
--------------------
A real ``backend.server`` subprocess: its own SQLite file, its own
plans tree, its own port (handed out by the kernel, never written
down), its own log file.

The delivery transport is a **stub** ``cc-connect``.  The product finds
it through ``PDT_CC_CONNECT_BIN``, so the case does not depend on a
real ``cc-connect`` being installed, and it cannot send anything to a
real group.  The stub does what the real one does from the delivery's
point of view — it reads the message off stdin and writes it down — so
the message the case reads is the message a session would have
received.

The negative control pins that same variable to a path that is not
there, which is what a machine without ``cc-connect`` installed looks
like.

The port is the kernel's to give, and the server is stopped and its
log handle closed in a ``finally``: a test that leaves a server running
is a test that makes the next one fail for the wrong reason.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

#: The e2e layer, and only the e2e layer.  Deliberately *not*
#: ``time_sensitive``: the default pytest options exclude that marker,
#: and a case that the layer's own command cannot collect is a case
#: that never runs.  What this file asserts is that a message arrives
#: and that a decision is still waiting — not a throughput contract
#: that a shared runner would distort.
pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

#: ``backend/`` — this file lives in ``backend/tests/e2e/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The repository root, one level above ``backend/``.  The server is
#: booted from here, so the code it runs is this worktree's code.
PROJECT_ROOT = BACKEND_DIR.parent

# Prefer the checkout's own virtualenv; fall back to the interpreter
# already running pytest when there is no ``backend/.venv`` (a fresh
# worktree, a container, CI before the venv bootstrap step).  The
# fallback is deliberately not a skip: a suite that skips is a suite
# that asserts nothing.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: The request guard's header.  Every ``/api/*`` call carries it, and
#: every call names a loopback host — the guard is part of the
#: production posture this suite drives the real process through.
REQUEST_HEADER = "X-PDT-Request"
REQUEST_HEADER_VALUE = "1"
LOOPBACK_HOST = "127.0.0.1"

#: The plan this suite creates, and the binding its creation carries.
PLAN_ID = "e2e-msg"
DESTINATION = "grp-e2e"
SESSION_ID = "sess-e2e"
REQUIREMENT = "Deliver the HITL decision to the session that asked for it."

#: The plan the negative control creates, and the binding its creation
#: carries.  Its own plan, in its own workdir, on its own server: the
#: environment that makes the delivery fail is a property of the
#: *server*, so the two cases cannot share one.
MISSING_PLAN_ID = "e2e-missing"
MISSING_DESTINATION = "grp-missing"
MISSING_SESSION_ID = "sess-missing"

#: The question, and the answers on the table.
QUESTION = "是否合入？"
OPTIONS = ["accept", "revise"]
DEFAULT_OPTION = "accept"

#: The files this suite reads back out of the workdir and the plan
#: directory.
CONFIG_FILENAME = "hitl_config.json"
HOOK_AUDIT_FILENAME = "hooks_audit.jsonl"
CAPTURE_FILENAME = "capture.txt"
CAPTURE_ARGS_FILENAME = "capture.txt.args"
STUB_FILENAME = "stub-cc-connect"
LOG_FILENAME = "server.log"

#: The first line of every message the product formats.  It is what a
#: reader sees first, and it is how this suite counts how many
#: deliveries arrived.  The message is Markdown, so the title carries
#: its markers — this is the fixed part of the title line, ahead of the
#: plan the decision belongs to.
MESSAGE_HEADER = "**⏳ HITL 决策待处理**"

#: How long a poll waits before it gives up.  The delivery runs on a
#: thread of its own, so the wait is a real wait — and it is bounded,
#: because an unbounded wait is a hung test.
POLL_TIMEOUT_SEC = 20.0

#: How long the server gets to answer ``/health``.
SERVER_BOOT_TIMEOUT_SEC = 60.0

#: Where the optional browser replay leaves its screenshot.  It is
#: evidence, not an assertion: the file is written, and the case reads
#: the delivery back out of the capture the same way the browserless
#: path does.
SCREENSHOT_DIR = Path("/tmp/hitl-e2e")

#: The environment variable that holds the home the run was launched
#: with — the hermetic environment (``_pytest_hermetic_env.py``) points
#: ``$HOME`` at a scratch directory, and Playwright looks for its
#: browser binaries under the real one.
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"

#: The transport that is not installed.  It is a *path* on purpose: the
#: delivery CLI reads a pinned path as a path, and a path that is not
#: there is a configuration error rather than a name to look up on
#: ``PATH``.  The fallback would send the message to whatever
#: ``cc-connect`` happened to be installed — the opposite of what the
#: negative control is about.
MISSING_BINARY = "/nonexistent/cc-connect-xyz"


# ---------------------------------------------------------------------------
# The stub transport
# ---------------------------------------------------------------------------

#: The stub ``cc-connect``.  It stands in for the real transport: the
#: product runs ``cc-connect send -p <project> -s <session> --stdin``
#: with the message on stdin, and the stub records what it was handed.
#:
#: Nothing about the machine this runs on is written into it — the
#: capture path comes from the environment, so the same stub works on
#: every developer's box.
#:
#: The destination is recorded *before* the message is read off stdin:
#: the message is the last thing the stub writes, so a reader that
#: waits for the message knows the destination is already on disk.
_STUB_SOURCE = '''\
#!/bin/sh
# A stand-in for the real ``cc-connect``.
#
# The product delivers a decision by running
# ``cc-connect send -p <project> -s <session> --stdin`` with the
# message on stdin.  This stub does from the delivery's point of view
# what the real one does: it reads the message off stdin and records
# it, together with the destination it was addressed to.
#
# Both files are appended to rather than truncated, so a second
# delivery would be visible instead of replacing the first.

capture="${PDT_CC_CONNECT_CAPTURE:?PDT_CC_CONNECT_CAPTURE is not set}"
args="${capture}.args"

printf '%s\\n' "$*" >> "$args"
cat >> "$capture"
printf '\\n' >> "$capture"
'''


def _write_cc_connect_stub(workdir: Path) -> Path:
    """Write the stub transport into ``workdir`` and return its path.

    The stub is executable: the product runs it directly, the way it
    runs any other command.
    """
    path = workdir / STUB_FILENAME
    path.write_text(_STUB_SOURCE, encoding="utf-8")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# Booting the server
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind an ephemeral loopback port and return its number.

    The kernel hands out a free port and releasing the socket leaves a
    small race window; the readiness polling below tolerates it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK_HOST, 0))
        return sock.getsockname()[1]


def _wait_for_server(base_url: str, timeout: float = SERVER_BOOT_TIMEOUT_SEC) -> None:
    """Block until ``GET /health`` on ``base_url`` answers 2xx."""
    deadline = time.time() + timeout
    last: Optional[Exception] = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        time.sleep(0.2)
    raise RuntimeError(
        f"the server at {base_url} did not start in {timeout}s; last error: {last!r}"
    )


def boot_server(
    workdir: Path, extra_env: Optional[Dict[str, str]] = None
) -> Tuple[str, subprocess.Popen]:
    """Boot a real ``backend.server`` against ``workdir``.

    The subprocess gets its own SQLite file, its own plans tree, its own
    port and its own log file, so everything this suite asserts about
    is the state a real deployment would have written.

    The delivery transport is the stub in ``workdir``: the product is
    pointed at it through ``PDT_CC_CONNECT_BIN``, so the case neither
    needs a real ``cc-connect`` installed nor risks sending anything to
    a real group.  The capture it writes is ``workdir/capture.txt``.

    Parameters
    ----------
    workdir:
        The directory the server's state lives in.  It is created if it
        does not exist.
    extra_env:
        Extra environment for the server process, applied over the
        defaults above.  It is how a caller pins one variable without
        this function having to know about it.

    Returns
    -------
    Tuple[str, subprocess.Popen]
        The base URL the server answers on, and the process.  The
        process carries the log handle, and must be handed to
        :func:`shutdown_server` when the caller is done with it.
    """
    workdir = Path(workdir)
    plans_dir = workdir / "plans"
    logs_dir = workdir / "logs"
    for directory in (plans_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)

    stub_path = _write_cc_connect_stub(workdir)

    port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "PDT_STATE_DB_PATH": str(workdir / "state.db"),
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_HOST": LOOPBACK_HOST,
            "PDT_PORT": str(port),
            "PDT_ALLOWED_HOSTS": f"{LOOPBACK_HOST},localhost,testserver",
            # Pinned rather than "dry-run": an unrecognised value falls
            # through to the CC Switch probe, which reports a fleet that
            # is not there.
            "PDT_PROVIDER_MODE": "inherit",
            # The coding tool is pinned to a CLI that is not installed,
            # so the one model call this suite can reach (the
            # interview's first question) fails in milliseconds instead
            # of costing a call.  The hook binding is written before
            # that call, so the binding this suite asserts is written
            # either way.
            "PDT_DEFAULT_CODING_TOOL": "vendor-c",
            # The delivery transport, and where it records what it was
            # handed.  The product reads the first of these; the stub
            # reads the second.
            "PDT_CC_CONNECT_BIN": str(stub_path),
            "PDT_CC_CONNECT_CAPTURE": str(workdir / CAPTURE_FILENAME),
            "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
        }
    )
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    env.pop("ANTHROPIC_BASE_URL", None)
    # The secret handles are inherited file descriptors, and the backend
    # closes the descriptor it is handed.  In a test process that
    # descriptor is frequently an unrelated one — asyncio's kqueue on
    # macOS, for instance — and the server dies on it during startup.
    # A test must not inherit a live secret handle, so the handles are
    # dropped from the child's environment.
    for name in [key for key in env if key.startswith("PDT_SECRET_FD_")]:
        env.pop(name, None)

    log_path = logs_dir / LOG_FILENAME
    log_handle = log_path.open("wb")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "backend.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    base_url = f"http://{LOOPBACK_HOST}:{port}"
    try:
        _wait_for_server(base_url)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        log_handle.close()
        raise

    proc._pdt_log_handle = log_handle  # type: ignore[attr-defined]
    proc._pdt_log_path = log_path  # type: ignore[attr-defined]
    return base_url, proc


def shutdown_server(proc: subprocess.Popen) -> None:
    """Terminate the server and close its log handle.

    Both halves matter: a server left running keeps the port and the
    plans tree, and a log handle left open keeps a file descriptor
    alive for the rest of the session.
    """
    log_handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _request_json(
    base_url: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    method: str = "POST",
    timeout: float = 60.0,
) -> Tuple[int, str]:
    """Call the server and return ``(status, body)``.

    The guard's header and a loopback ``Host`` travel with every call;
    a request the guard refuses tells this suite nothing about the
    delivery, so the refusal is visible as a 403 rather than as a
    missing message.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers={
            "Content-Type": "application/json",
            REQUEST_HEADER: REQUEST_HEADER_VALUE,
            "Host": LOOPBACK_HOST,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8") if exc.fp else ""


def _post_json(
    base_url: str, path: str, payload: Dict[str, Any], *, timeout: float = 60.0
) -> Tuple[int, str]:
    """POST ``payload`` and return ``(status, body)``."""
    return _request_json(base_url, path, payload, method="POST", timeout=timeout)


def _get_json(
    base_url: str, path: str, *, timeout: float = 30.0
) -> Tuple[int, str]:
    """GET ``path`` and return ``(status, body)``."""
    return _request_json(base_url, path, None, method="GET", timeout=timeout)


# ---------------------------------------------------------------------------
# Waiting for the delivery
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """The parsed records of a jsonl mirror, oldest first."""
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _wait_for_capture(workdir: Path, *, timeout: float = POLL_TIMEOUT_SEC) -> str:
    """Block until the session has been handed a message.

    The delivery runs on a thread of its own, so the wait is a real
    wait — and it is bounded.  When the bound passes, the error names
    the server's log: the reason the message did not arrive is in
    there, and a reader should not have to go looking for it.
    """
    capture = workdir / CAPTURE_FILENAME
    log_path = workdir / "logs" / LOG_FILENAME
    deadline = time.time() + timeout
    while time.time() < deadline:
        if capture.exists():
            text = capture.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                return text
        time.sleep(0.05)
    raise AssertionError(
        "no message reached the session within {0:.0f}s; the server's log "
        "is at {1}".format(timeout, log_path)
    )


def _wait_for_destinations(
    workdir: Path, *, timeout: float = POLL_TIMEOUT_SEC
) -> List[str]:
    """Block until the transport has recorded where it was addressed.

    One line per delivery.  The transport writes this file *before* it
    reads the message off stdin, so a reader that waits for the
    message knows the destination is already on disk — and a reader
    that waits here is never racing the file it is about to read.
    """
    path = workdir / CAPTURE_ARGS_FILENAME
    log_path = workdir / "logs" / LOG_FILENAME
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            lines = [
                line for line in path.read_text(encoding="utf-8").splitlines() if line
            ]
            if lines:
                return lines
        time.sleep(0.05)
    raise AssertionError(
        "the transport recorded no destination within {0:.0f}s; the "
        "server's log is at {1}".format(timeout, log_path)
    )


def _wait_for_audit(
    plan_dir: Path, count: int, *, timeout: float = POLL_TIMEOUT_SEC
) -> List[Dict[str, Any]]:
    """Block until the dispatch audit holds ``count`` lines.

    The audit line is written after the delivery is over, so it can
    arrive after the message itself.  The wait is bounded; the lines
    that are there when the wait gives up are the answer.
    """
    path = plan_dir / HOOK_AUDIT_FILENAME
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = _read_jsonl(path)
        if len(lines) >= count:
            return lines
        time.sleep(0.05)
    return _read_jsonl(path)


def _wait_for_file(path: Path, *, timeout: float = POLL_TIMEOUT_SEC) -> bool:
    """Block until ``path`` exists.  Whether it exists is the answer."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()


def _decision_row(state_db: Path, decision_id: str) -> Optional[sqlite3.Row]:
    """The decision's row, read straight out of SQLite.

    The row is read with ``sqlite3`` rather than through the service,
    so that the assertion is about the database and not about the code
    that wrote it.
    """
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.execute(
            "SELECT * FROM plan_decisions WHERE decision_id = ?", (decision_id,)
        )
        return cursor.fetchone()
    finally:
        connection.close()


def _wait_for_decision_status(
    state_db: Path,
    decision_id: str,
    status: str,
    *,
    timeout: float = POLL_TIMEOUT_SEC,
) -> Optional[sqlite3.Row]:
    """Block until the decision's row reads ``status``.

    The delivery runs on a thread of its own, so the question is
    ``pending`` when the request comes back and settles itself a
    moment later — a question nobody was asked does not stay waiting
    for an answer that is never going to come.  The wait is bounded,
    and the row that is there when the bound passes is the answer.
    """
    deadline = time.time() + timeout
    row: Optional[sqlite3.Row] = None
    while True:
        row = _decision_row(state_db, decision_id)
        if row is not None and row["status"] == status:
            return row
        if time.time() >= deadline:
            return row
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def booted(tmp_path):
    """A running backend, and the directory its state lives in.

    The server is stopped and its log handle closed when the case that
    asked for it ends, whatever the case did.
    """
    workdir = tmp_path
    base_url, proc = boot_server(workdir)
    try:
        yield base_url, workdir
    finally:
        shutdown_server(proc)


@pytest.fixture
def booted_without_transport(tmp_path):
    """A running backend whose delivery transport is not installed.

    Its own server, because the environment that breaks the delivery
    is a property of the server: the transport is pinned to a path that
    is not there, and every case that wants a *working* transport needs
    a server that was booted without that pin.

    The capture path is pinned to this workdir as well, so the case can
    say that nothing was sent by looking at a file it owns.
    """
    workdir = tmp_path
    base_url, proc = boot_server(
        workdir,
        {
            "PDT_CC_CONNECT_BIN": MISSING_BINARY,
            "PDT_CC_CONNECT_CAPTURE": str(workdir / CAPTURE_FILENAME),
        },
    )
    try:
        yield base_url, workdir
    finally:
        shutdown_server(proc)


# ---------------------------------------------------------------------------
# The case
# ---------------------------------------------------------------------------


def test_session_receives_decision_message(booted):
    """A decision asked in a session is delivered to that session.

    The chain is driven from the outside, through the product's own
    routes: a plan is created with a hook destination and a hook
    session, a question is asked about it, and the message that would
    reach the session is read back out of the transport.

    Four facts have to hold together — the message is not empty and
    carries the decision's identity, it carries the question and the
    options, the dispatch audit says one ``cmd`` destination was
    reached, and the decision is still ``pending`` afterwards.
    """
    base_url, workdir = booted
    plan_dir = workdir / "plans" / PLAN_ID

    # 1. The plan is created, and the binding lands in the plan's own
    #    configuration.  The creation call is not expected to succeed:
    #    the coding tool is pinned to a CLI that is not installed, so
    #    the interview's model call fails in milliseconds.  What
    #    matters is that the binding was written *before* that call,
    #    and that the request guard let the request through at all.
    status, body = _post_json(
        base_url,
        "/api/interview/start",
        {
            "requirement": REQUIREMENT,
            "plan_id": PLAN_ID,
            "hook_destination": DESTINATION,
            "hook_session_id": SESSION_ID,
        },
    )
    assert status != 403, (
        f"the request guard refused the creation request ({status}): {body}"
    )

    config_path = plan_dir / CONFIG_FILENAME
    assert _wait_for_file(config_path), (
        "the creation request's binding was not written to the plan's "
        "configuration; the destination never reached the plan"
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["source"] == "plan_creation", (
        f"the configuration records the source as {config['source']!r}; a "
        "plan created in a session records where it came from"
    )
    assert len(config["hooks"]) == 1, (
        f"the configuration binds {len(config['hooks'])} destinations; the "
        "creation request named exactly one"
    )
    hook = config["hooks"][0]
    assert hook["kind"] == "cmd", (
        f"the binding is a {hook['kind']!r} hook; a plan created in a "
        "session binds the command that delivers"
    )
    assert hook["command"], "the command hook carries no command"

    # 2. The question is asked, through the real route, in the real
    #    server.  The delivery runs on a thread of its own, so the
    #    answer comes back before the message has been delivered.
    status, body = _post_json(
        base_url,
        f"/api/hitl/{PLAN_ID}/decisions",
        {
            "question": QUESTION,
            "options": list(OPTIONS),
            "default_option": DEFAULT_OPTION,
        },
    )
    assert status == 200, f"the creation call failed ({status}): {body}"
    decision = json.loads(body)
    decision_id = decision["decision_id"]
    assert decision_id, f"the decision carries no id: {decision!r}"
    assert decision["status"] == "pending", (
        "a question somebody was asked is reported as "
        f"{decision['status']!r}; it is still waiting for a human"
    )

    # 3. The session receives the message.  The stub transport writes
    #    down what the product handed it — which is the message a
    #    session would have received.
    message = _wait_for_capture(workdir)
    assert message.strip(), "the session received an empty message"
    assert decision_id in message, (
        f"the message does not carry the decision's id ({decision_id!r}); "
        "a person reading it cannot say which decision it is about"
    )
    assert QUESTION in message, (
        f"the message does not carry the question ({QUESTION!r}); a "
        "payload that left it out could only say *that* a question was asked"
    )
    for option in OPTIONS:
        assert option in message, (
            f"the message does not carry the option {option!r}; the "
            "answers on the table are what a person replies with"
        )
    assert f"**默认答案**：{DEFAULT_OPTION}" in message, (
        f"the message does not carry the default answer "
        f"({DEFAULT_OPTION!r}); the answer that happens by default is "
        "part of the question"
    )

    # 4. The delivery happened exactly once, and to the destination the
    #    creation request named.
    assert message.count(MESSAGE_HEADER) == 1, (
        "the session received "
        f"{message.count(MESSAGE_HEADER)} messages; a question is asked "
        "once and answered once"
    )
    delivered_to = _wait_for_destinations(workdir)
    assert len(delivered_to) == 1, (
        f"the transport was called {len(delivered_to)} times; a decision "
        "is delivered once"
    )
    assert DESTINATION in delivered_to[0], (
        f"the message went to {delivered_to[0]!r}; the creation request "
        f"named {DESTINATION!r}"
    )
    assert SESSION_ID in delivered_to[0], (
        f"the message went to {delivered_to[0]!r}; the creation request "
        f"named the session {SESSION_ID!r}"
    )

    # 5. The audit says so too, and says what kind of destination it
    #    was: the plan's own configuration named a command, and that is
    #    what was run.
    audit_lines = _wait_for_audit(plan_dir, 1)
    assert len(audit_lines) >= 1, (
        "the dispatch wrote no audit line; nothing says the delivery "
        "happened"
    )
    record = audit_lines[-1]
    assert record["delivered"] == 1, (
        f"the dispatch reported {record['delivered']} delivered "
        f"destinations; the message reached one"
    )
    entries = record["entries"]
    assert len(entries) == 1, (
        f"the audit records {len(entries)} destinations; the plan binds "
        "exactly one"
    )
    assert entries[0]["kind"] == "cmd", (
        f"the audit records a {entries[0]['kind']!r} destination; the "
        "plan's configuration named a command"
    )
    assert entries[0]["outcome"] == "delivered", (
        f"the audit records the destination as "
        f"{entries[0]['outcome']!r}; the message arrived"
    )

    # 6. And the decision is still waiting for an answer.  A decision
    #    that reached nobody is settled as ``unroutable``; one that
    #    reached somebody must not be.
    status, body = _get_json(base_url, f"/api/hitl/{PLAN_ID}/decisions")
    assert status == 200, f"the decision listing failed ({status}): {body}"
    decisions = json.loads(body)
    assert len(decisions) == 1, (
        f"the plan holds {len(decisions)} decisions; exactly one was asked"
    )
    assert decisions[0]["status"] == "pending", (
        "the decision is reported as "
        f"{decisions[0]['status']!r} after the message was delivered; it "
        "is still waiting for an answer"
    )
    assert decisions[0]["answer"] is None, (
        "a decision that was delivered was given an answer"
    )

    row = _decision_row(workdir / "state.db", decision_id)
    assert row is not None, "the decision was not written to the database"
    assert row["status"] == "pending", (
        f"the decision's row says {row['status']!r}; the delivery "
        "reached somebody, so the decision waits"
    )
    assert row["answer"] is None, (
        "a decision that was delivered was given an answer"
    )


# ---------------------------------------------------------------------------
# The negative control
# ---------------------------------------------------------------------------
#
# The case above proves that a message *can* arrive.  It does not
# prove that the report says so only when it did: a dispatcher that
# counted "the command ran" as "the message arrived" would pass that
# case and lie to every caller.  The case below is the other half of
# the contract — the transport is not installed at all — and it pins
# the failure semantics the architecture names: the delivery is
# ``failed``, never ``delivered``, and a question nobody was asked is
# settled as ``unroutable`` rather than left waiting for an answer
# that is never going to come.


def test_missing_binary_is_failed_not_delivered(booted_without_transport):
    """A transport that is not installed is a failure, never a delivery.

    The same sequence as the case above — a plan created in a session,
    a question asked about it — but the transport the product was told
    to use is not there.  Three facts have to hold, and they are the
    ones the positive case cannot show:

    1. the dispatch audit says ``failed``, and says it counts zero
       delivered destinations;
    2. the decision settles itself as ``unroutable`` — nobody was
       asked, so there is no answer to wait for;
    3. nothing was sent: there was nothing to send it with.
    """
    base_url, workdir = booted_without_transport
    plan_dir = workdir / "plans" / MISSING_PLAN_ID
    capture_path = workdir / CAPTURE_FILENAME

    # 1. The plan is created, and the binding lands in the plan's own
    #    configuration — the same binding as the positive case, so the
    #    delivery is attempted in exactly the same way.  The creation
    #    call is not expected to succeed: the coding tool is pinned to
    #    a CLI that is not installed, so the interview's model call
    #    fails in milliseconds.  The binding is written before that
    #    call, so it is there either way.
    status, body = _post_json(
        base_url,
        "/api/interview/start",
        {
            "requirement": REQUIREMENT,
            "plan_id": MISSING_PLAN_ID,
            "hook_destination": MISSING_DESTINATION,
            "hook_session_id": MISSING_SESSION_ID,
        },
    )
    assert status != 403, (
        f"the request guard refused the creation request ({status}): {body}"
    )

    config_path = plan_dir / CONFIG_FILENAME
    assert _wait_for_file(config_path), (
        "the creation request's binding was not written to the plan's "
        "configuration; the destination never reached the plan"
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert len(config["hooks"]) == 1, (
        f"the configuration binds {len(config['hooks'])} destinations; the "
        "creation request named exactly one"
    )
    assert config["hooks"][0]["kind"] == "cmd", (
        f"the binding is a {config['hooks'][0]['kind']!r} hook; a plan "
        "created in a session binds the command that delivers"
    )

    # 2. The question is asked, through the real route, in the real
    #    server.  The delivery runs on a thread of its own, so the
    #    answer comes back before the delivery has finished — the row
    #    may still read ``pending`` here.
    status, body = _post_json(
        base_url,
        f"/api/hitl/{MISSING_PLAN_ID}/decisions",
        {
            "question": QUESTION,
            "options": list(OPTIONS),
            "default_option": DEFAULT_OPTION,
        },
    )
    assert status == 200, f"the creation call failed ({status}): {body}"
    decision = json.loads(body)
    decision_id = decision["decision_id"]
    assert decision_id, f"the decision carries no id: {decision!r}"

    # 3. The dispatch audit says the delivery *failed*.  This is the
    #    assertion the positive case cannot make: the count of
    #    delivered destinations is zero, and the one destination that
    #    was attempted is recorded as ``failed`` — not as "not
    #    delivered", and certainly not as delivered.
    audit_lines = _wait_for_audit(plan_dir, 1)
    assert len(audit_lines) >= 1, (
        "the dispatch wrote no audit line; nothing says the delivery "
        "failed either"
    )
    audit = audit_lines[-1]
    assert audit["delivered"] == 0, (
        f"the dispatch reported {audit['delivered']} delivered "
        "destinations; the transport was not installed, so nothing was "
        "delivered"
    )
    assert audit["timed_out"] == 0, (
        f"the dispatch reported {audit['timed_out']} timed-out "
        "destinations; the delivery failed rather than hanging"
    )
    entries = audit["entries"]
    assert len(entries) == 1, (
        f"the audit records {len(entries)} destinations; the plan binds "
        "exactly one"
    )
    assert entries[0]["kind"] == "cmd", (
        f"the audit records a {entries[0]['kind']!r} destination; the "
        "plan's configuration named a command"
    )
    assert entries[0]["outcome"] == "failed", (
        f"the audit records the destination as "
        f"{entries[0]['outcome']!r}; the transport was not installed, so "
        "the delivery failed — it was not delivered"
    )
    exit_code = entries[0].get("exit_code")
    assert exit_code is not None, (
        "the audit records no exit code; the delivery cannot be called a "
        "failure on the strength of an outcome alone"
    )
    assert exit_code != 0, (
        f"the delivery command exited {exit_code}; a command that ran and "
        "failed is what a missing transport looks like"
    )

    # 4. And the decision settles itself.  A question nobody was asked
    #    must not sit in the table as ``pending``: it would hold the
    #    schedule shut over a question no human ever received.
    row = _wait_for_decision_status(
        workdir / "state.db", decision_id, "unroutable"
    )
    assert row is not None, "the decision was not written to the database"
    assert row["status"] == "unroutable", (
        f"the decision's row says {row['status']!r}; the delivery "
        "reached nobody, so nobody is going to answer it"
    )
    assert row["answer"] is None, (
        "a decision that reached nobody was given an answer"
    )

    status, body = _get_json(base_url, f"/api/hitl/{MISSING_PLAN_ID}/decisions")
    assert status == 200, f"the decision listing failed ({status}): {body}"
    decisions = json.loads(body)
    assert len(decisions) == 1, (
        f"the plan holds {len(decisions)} decisions; exactly one was asked"
    )
    assert decisions[0]["status"] == "unroutable", (
        "the decision is reported as "
        f"{decisions[0]['status']!r} after the delivery failed; nobody "
        "was asked"
    )

    # 5. Nothing was sent, because there was nothing to send it with.
    assert not capture_path.exists(), (
        f"the transport wrote {capture_path}; the transport was not "
        "installed, so nothing could have been written there"
    )


# ---------------------------------------------------------------------------
# The optional browser replay
# ---------------------------------------------------------------------------
#
# The case above is the contract.  The one below is *extra* evidence,
# and it only runs where a browser driver already exists — this
# repository ships no Playwright dependency, and a test is not allowed
# to install one (that would download a browser onto a developer's
# machine).  Where no driver is installed the case skips, and the
# assertions above still stand on their own.


def _driver_env() -> Dict[str, str]:
    """The environment the browser driver runs under.

    ``$HOME`` is pointed back at the home the run was launched with so
    that Playwright can find the browser binaries it installed there.
    """
    env = dict(os.environ)
    real_home = os.environ.get(REAL_HOME_ENV)
    if real_home:
        env["HOME"] = real_home
    return env


def _playwright_interpreter() -> Optional[str]:
    """An interpreter that can import ``playwright``, or ``None``.

    Two candidates, in order: the interpreter running pytest, and the
    interpreter behind a ``playwright`` command on ``PATH`` — the
    command is a Python entry point, so its shebang names an
    interpreter that has the package installed.  Nothing is hard-coded:
    both are read out of the environment.
    """
    candidates = [sys.executable]

    command = shutil.which("playwright")
    if command:
        try:
            first_line = Path(command).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[0]
        except (OSError, IndexError):
            first_line = ""
        if first_line.startswith("#!"):
            parts = shlex.split(first_line[2:].strip())
            interpreter = ""
            if parts:
                if os.path.basename(parts[0]) == "env" and len(parts) > 1:
                    interpreter = parts[1]
                else:
                    interpreter = parts[0]
            if "python" in interpreter:
                candidates.append(interpreter)

    for candidate in candidates:
        try:
            probe = subprocess.run(
                [candidate, "-c", "import playwright.sync_api"],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


#: The driver.  It replays the same sequence through Playwright's
#: request context — the same two calls the browserless path makes —
#: and then leaves a screenshot of the served page behind.
_BROWSER_REPLAY = '''
"""Replay the HITL delivery sequence through a browser's request context."""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot
    launch anything.  The binaries that *are* installed are found here
    instead -- nothing is downloaded, and nothing is hard-coded.
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


base_url, plan_id, destination, session_id, question, screenshot = sys.argv[1:7]

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

headers = {"X-PDT-Request": "1", "Content-Type": "application/json"}

with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)

    def post(path, payload):
        """One call, on a connection pool of its own.

        A fresh context per call: the server answers the creation
        request with an unhandled exception (the interview's model
        call has no provider behind it), and a server that closes the
        connection after an unhandled exception takes the *next* call
        on that pooled connection down with it.

        The body is read here, while the context is still open — a
        response is disposed with the context that fetched it.
        """
        api_context = browser.new_context()
        try:
            response = api_context.request.post(
                base_url + path, headers=headers, data=payload
            )
            return response.status, response.text()
        finally:
            api_context.close()

    # The same two calls the browserless path makes, through the
    # browser's own request context.
    start_status, _ = post(
        "/api/interview/start",
        {
            "requirement": "Deliver the HITL decision to the session.",
            "plan_id": plan_id,
            "hook_destination": destination,
            "hook_session_id": session_id,
        },
    )
    create_status, create_body = post(
        "/api/hitl/" + plan_id + "/decisions",
        {
            "question": question,
            "options": ["accept", "revise"],
            "default_option": "accept",
        },
    )

    try:
        body = json.loads(create_body) if create_status == 200 else {}
    except ValueError:
        body = {}

    # The page the operator would have been looking at, and the
    # evidence that the replay really happened.
    page_context = browser.new_context()
    page = page_context.new_page()
    page.goto(base_url + "/", wait_until="load")
    page.wait_for_selector("#app", timeout=30000)
    page.wait_for_timeout(1000)
    page.screenshot(path=screenshot, full_page=True)
    page_context.close()

    print(json.dumps({
        "start_status": start_status,
        "create_status": create_status,
        "decision_id": body.get("decision_id", ""),
        "status": body.get("status", ""),
        "screenshot": screenshot,
    }))
    browser.close()
'''


def test_browser_replays_the_same_delivery(booted):
    """The same sequence, replayed through a browser's request context.

    Optional by construction: it needs a Playwright driver that is
    already installed, and it skips when there is none.  It makes no
    assertion the case above has not already made — it repeats them
    through a different client, and leaves a screenshot behind.
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; "
            "the default (browserless) path carries the assertions"
        )

    base_url, workdir = booted
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    screenshot = str(SCREENSHOT_DIR / "test_browser_replays_the_same_delivery.png")

    driver_script = workdir / "drive_replay.py"
    driver_script.write_text(_BROWSER_REPLAY, encoding="utf-8")

    result = subprocess.run(
        [
            interpreter,
            str(driver_script),
            base_url,
            PLAN_ID,
            DESTINATION,
            SESSION_ID,
            QUESTION,
            screenshot,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        if "Executable doesn't exist" in combined or (
            "playwright install" in combined
        ):
            pytest.skip(
                "the Playwright driver is installed but its browser "
                "binaries are not; the default path carries the "
                "assertions"
            )
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["create_status"] == 200, (
        f"the replayed creation call failed: {payload!r}"
    )
    assert payload["decision_id"], f"the replayed decision carries no id: {payload!r}"

    # The replayed delivery reached the session too.
    message = _wait_for_capture(workdir)
    assert payload["decision_id"] in message, (
        "the replayed message does not carry the decision's id; the "
        f"message was {message!r}"
    )
    assert Path(screenshot).exists(), (
        f"the browser replay left no screenshot at {screenshot}"
    )


def test_browser_replays_the_missing_binary_delivery(booted_without_transport):
    """The negative control, replayed through a browser's request context.

    Optional by construction, exactly like the case above: it needs a
    Playwright driver that is already installed, and it skips when there
    is none.  It carries no assertion the browserless path has not
    already made — it repeats the *sequence* through a different
    client, and leaves a screenshot behind as evidence that the replay
    really happened.

    The screenshot is filed under its own name, next to the positive
    case's: the two are evidence of different outcomes and must not be
    mistaken for one another.
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; "
            "the default (browserless) path carries the assertions"
        )

    base_url, workdir = booted_without_transport
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    screenshot = str(SCREENSHOT_DIR / "negative.png")

    driver_script = workdir / "drive_negative_replay.py"
    driver_script.write_text(_BROWSER_REPLAY, encoding="utf-8")

    result = subprocess.run(
        [
            interpreter,
            str(driver_script),
            base_url,
            MISSING_PLAN_ID,
            MISSING_DESTINATION,
            MISSING_SESSION_ID,
            QUESTION,
            screenshot,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        if "Executable doesn't exist" in combined or (
            "playwright install" in combined
        ):
            pytest.skip(
                "the Playwright driver is installed but its browser "
                "binaries are not; the default path carries the "
                "assertions"
            )
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["create_status"] == 200, (
        f"the replayed creation call failed: {payload!r}"
    )
    assert payload["decision_id"], (
        f"the replayed decision carries no id: {payload!r}"
    )

    # The replayed delivery went nowhere either: the transport is not
    # installed, so nothing was written down.
    assert not (workdir / CAPTURE_FILENAME).exists(), (
        "the replayed delivery wrote a capture; the transport was not "
        "installed, so nothing could have been written there"
    )
    assert Path(screenshot).exists(), (
        f"the browser replay left no screenshot at {screenshot}"
    )
