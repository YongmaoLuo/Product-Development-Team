"""End-to-end: a plan's hooks really fan out, and a plan with no hooks
degrades instead of failing.

Background
----------
A human-in-the-loop question is only useful if it reaches a human.  The
HITL subsystem answers that question in three places, and each of them
can be right on its own while the question still never leaves the
building:

* ``hooks/config.py`` decides *who* is asked — the plan's
  ``hitl_config.json`` names the destinations.
* ``hooks/dispatcher.py`` decides *how* the question is delivered — one
  attempt per destination, each on its own clock, none retried.
* ``hitl/service.py`` decides *when* the question is asked — a decision
  row, committed before anybody is told about it, and the outlet told
  afterwards on a thread of its own.

The failure this file exists for is the one none of those three can
catch on its own: a dispatcher that nobody calls delivers nothing, and
a caller that waits for the dispatcher hands a hung webhook the power to
hang the caller.  The unit layer sees neither, because it calls the
dispatcher by hand and has no process boundary to hang across.  The only
place both facts are observable is a real process, a real socket and a
real plan directory.

What is pinned here
-------------------
Three cases, one per fact the product owes a caller:

1. ``test_fanout_nonblocking`` — a question is asked through the real
   HTTP route and the ask comes back inside its budget; the fan-out
   reaches *both* healthy destinations exactly once, each carrying the
   decision's identity and its deadline; a third destination that
   accepts the connection and never answers is abandoned on its own
   clock, appears in the dispatch audit once, and is not tried a
   second time.
2. ``test_empty_hooks_unroutable`` — a plan with no outlet asks
   nobody: no destination sees a request, the delivery report says
   nobody was reached, the dispatch audit says so, and the question is
   not dressed up as answered or defaulted — it is ``unroutable``,
   which waits for nobody and blocks nothing.
3. ``test_creation_binds_destination`` — the binding a plan-creation
   request carries lands in that plan's ``hitl_config.json`` as the
   delivery command it is, the delivery reaches the transport that
   command is pointed at, and a creation request that carries no
   session context lands as an empty hook list.

Who plays the caller
--------------------
Nobody: the product plays it.  ``hitl/service.py::create_decision``
commits the decision row and *then* hands the question to the plan's
outlet, on a worker thread — the shape the architecture prescribes
(``hooks/dispatcher.py`` documents it: "A caller that must answer a
request quickly (a route handler, a decision creation) should
therefore run this on a worker thread rather than on the request's own
thread").

So the fan-out under test is the product's own dispatcher, over real
sockets, driven by the product's own request path, writing the
product's own audit mirror.  What this file plays is the operator's
part: it starts the destinations, asks the question through the real
HTTP route, and reads the three layers back — the response, the
SQLite row and the files in the plan directory.

What the product owes this file
-------------------------------
Two facts, both on the product side of the boundary:

* ``hitl/service.py::create_decision`` commits the decision row and
  returns.  It never calls ``hooks/dispatcher.py::dispatch``, so a
  decision created over HTTP reaches nobody on its own.
* Nothing writes the ``unroutable`` status.  The status is in the
  vocabulary (``hitl/models.py``) and the plan's own text says a plan
  with no hooks has decisions that "are simply unroutable", but the
  store has no transition that produces it — so a question nobody was
  asked stays ``pending`` today.

Both are now the product's behaviour, and both are asserted here
against the real request path: the fan-out leaves the building because
the *service* dispatches, and the empty hook list produces an
``unroutable`` decision because the *store* has the transition.

Why the destination count is checked twice
------------------------------------------
"Exactly one" is a claim about the absence of a second delivery, so the
count is read once the first event has arrived *and* once more
after a pause.  A fan-out that delivered twice would pass a check that
only counted arrivals, and the second delivery is exactly the failure
this case is about.

How the box is built
--------------------
A real ``backend.server`` subprocess (its own SQLite file, its own plans
tree, its own port), plus hook destinations that are real
``http.server`` processes: two that answer, and one that accepts the
connection and then says nothing for thirty seconds.  The destinations
are subprocesses rather than threads because the point of the hanging
one is that it *keeps* the connection open — a thread in the test
process would be a different failure from the one being pinned.

The third case delivers through a *command*, and the command is
pointed at a stub transport written into the test's own work
directory.  The product is told which transport to run through the
environment, so the case neither needs a real one installed nor risks
sending anything to a real group, and what the stub was handed is
recorded in that directory and read back — so the delivery is asserted
from the transport's side as well as from the dispatcher's.

The three layers are asserted at once, because a single layer is a
single layer's opinion: the HTTP response, the SQLite row (read with
``sqlite3`` directly, not through the service), and the files in the
plan directory.  A response that says "delivered" proves nothing; a
row that says "pending" proves nothing; a file that says "hooks: []"
proves nothing.  All three have to say the same thing.
"""

from __future__ import annotations

import json
import os
import shlex
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
#: that never runs.  What this file asserts is a wall-clock bound (the
#: creation call comes back in under a second while a destination
#: hangs), not a throughput contract that a shared runner would
#: distort.
pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + bootstrap
# ---------------------------------------------------------------------------

#: ``backend/`` — this file lives in ``backend/tests/e2e/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The repository root, one level above ``backend/``.
PROJECT_ROOT = BACKEND_DIR.parent

# The outlet under test is the product's own module, imported the way
# every other suite in this tree imports one: the package root goes on
# the path first, then the import.
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from hooks.dispatcher import dispatch  # noqa: E402

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

#: The event name a plan's outlet is asked to deliver when a decision
#: is asked.
EVENT = "hitl_decision_created"

#: The three files this suite reads back out of the plan directory.
CONFIG_FILENAME = "hitl_config.json"
HOOK_AUDIT_FILENAME = "hooks_audit.jsonl"
DECISION_AUDIT_FILENAME = "hitl_decisions.jsonl"

#: The timeout the hanging destination is given.  It is far shorter
#: than the destination's own silence (thirty seconds) so that the
#: audit line arrives while the case is still running, and it is
#: longer than the creation call's budget so that a caller which
#: *waited* for the dispatch would visibly blow the one-second bound.
HANG_TIMEOUT_SEC = 5

#: How long the hanging destination stays silent after accepting the
#: connection.
HANG_SEC = 30.0

#: The bound the creation call has to come back within.  A dispatch
#: that blocks its caller for the hanging destination's timeout cannot
#: meet it.
CREATE_BOUND_SEC = 1.0

#: The question the cases ask.  Its text is deliberately plain: the
#: payload travels to the destinations verbatim, and what is under
#: test is the envelope, not the prose.
QUESTION_BODY: Dict[str, Any] = {
    "question": "Ship the hook fan-out as written?",
    "options": ["accept", "revise"],
    "default_option": "accept",
    "task_id": "T-e2e-hooks",
}

# ---------------------------------------------------------------------------
# The stub transport
# ---------------------------------------------------------------------------

#: The stub transport's file name.  It lives in the test's own work
#: directory: the repository is not a place a test writes to.
STUB_FILENAME = "cc-connect-stub"

#: Where the stub records what it was handed.  It is in the work
#: directory too, for the same reason.
CAPTURE_FILENAME = "cc-connect-capture.txt"

#: The variable that pins which transport the delivery runs.  The
#: product reads it; the stub does not.
TRANSPORT_BIN_ENV = "PDT_CC_CONNECT_BIN"

#: The variable that tells the stub where to record what it was
#: handed.  The stub reads it; the product does not.
TRANSPORT_CAPTURE_ENV = "PDT_CC_CONNECT_CAPTURE"

#: How long a poll waits for the delivery to land.  The delivery runs
#: on a thread of its own, so the wait is a real wait — and it is
#: bounded, because an unbounded wait is a hung test.
DELIVERY_TIMEOUT_SEC = 20.0

#: The stub transport.  It stands in for the real one: the product
#: runs ``cc-connect send -p <project> -s <session> --stdin`` with the
#: message on stdin, and the stub records what it was handed.
#:
#: Nothing about the machine this runs on is written into it — the
#: capture path comes from the environment, so the same stub works on
#: every developer's box.
_STUB_SOURCE = '''\
#!/bin/sh
# A stand-in for the real transport.
#
# The product delivers a decision by running
# ``cc-connect send -p <project> -s <session> --stdin`` with the
# message on stdin.  This stub does from the delivery's point of view
# what the real one does: it records the destination it was addressed
# to, and the message it was given.
#
# The capture is appended to rather than truncated, so a second
# delivery would be visible instead of replacing the first.

printf '%s\\n' "$@" >> "$PDT_CC_CONNECT_CAPTURE"
cat >> "$PDT_CC_CONNECT_CAPTURE"
'''


def _write_stub_transport(workdir: Path) -> Path:
    """Write the stub transport into ``workdir`` and return its path.

    The stub is executable: the product runs it directly, the way it
    runs any other command.
    """
    path = workdir / STUB_FILENAME
    path.write_text(_STUB_SOURCE, encoding="utf-8")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# A hook destination — a real http.server in its own process
# ---------------------------------------------------------------------------

#: The destination's source.  It appends one JSON record per request
#: it receives, and can be told to accept the connection and then say
#: nothing for a while — the shape of a webhook that has stopped
#: answering without having closed the socket.
_RECEIVER_SOURCE = '''\
"""A loopback HTTP destination that records what it was sent.

Usage: receive.py <outfile> <port> [hang_seconds]
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

outfile = sys.argv[1]
port = int(sys.argv[2])
hang = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        with open(outfile, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"path": self.path, "body": body.decode("utf-8")}
                )
                + "\\n"
            )
            handle.flush()
        if hang > 0:
            time.sleep(hang)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def _free_port() -> int:
    """Bind an ephemeral loopback port and return its number.

    The kernel hands out a free port and releasing the socket leaves a
    small race window; the readiness polling below tolerates it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK_HOST, 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 20.0) -> None:
    """Block until something accepts connections on ``port``."""
    deadline = time.time() + timeout
    last: Optional[Exception] = None
    while time.time() < deadline:
        try:
            with socket.create_connection((LOOPBACK_HOST, port), timeout=1):
                return
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    raise RuntimeError(
        f"the destination on port {port} did not start in {timeout}s; "
        f"last error: {last!r}"
    )


class _Receiver:
    """A hook destination running in its own process.

    The destination is a subprocess rather than a thread because the
    case that matters most is the one that *keeps* the connection
    open: a thread inside the test process would make a hung
    destination look like a hung test.
    """

    def __init__(self, workdir: Path, name: str, *, hang_sec: float = 0.0):
        self.port = _free_port()
        self.outfile = workdir / f"{name}.jsonl"
        self.outfile.write_text("", encoding="utf-8")
        script = workdir / f"{name}_server.py"
        script.write_text(_RECEIVER_SOURCE, encoding="utf-8")
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-I",  # isolated: the destination's directory is not a
                # source of modules for it
                str(script),
                str(self.outfile),
                str(self.port),
                str(hang_sec),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            _wait_for_port(self.port)
        except Exception:
            self.close()
            raise

    @property
    def url(self) -> str:
        """The URL a plan's ``hooks`` array names this destination by."""
        return f"http://{LOOPBACK_HOST}:{self.port}/hook"

    def records(self) -> List[Dict[str, Any]]:
        """One record per request the destination has received."""
        if not self.outfile.exists():
            return []
        parsed: List[Dict[str, Any]] = []
        for line in self.outfile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                parsed.append(json.loads(line))
        return parsed

    def bodies(self) -> List[Dict[str, Any]]:
        """The JSON bodies the destination has received, in order."""
        return [json.loads(record["body"]) for record in self.records()]

    def close(self) -> None:
        """Terminate the destination and join it."""
        proc = self._proc
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# The server under test
# ---------------------------------------------------------------------------


#: How long a real server gets to answer.  The budget is a *wait*, not
#: a threshold on the product: what has to hold is "the service comes
#: up", and the child either answers or dies — it never answers
#: slowly.  The server binds its port only after its lifespan has run,
#: and the lifespan walks the machine (a state migration, a startup
#: sweep, a credential sweep).  On a loaded runner — the gate runs
#: ``-n 8``, so seven other workers boot their own servers at the same
#: time — that walk is tens of seconds rather than the couple a quiet
#: machine needs.  The larger budget costs time only in the case that
#: is already red.
_SERVER_READY_TIMEOUT = 120.0


def _wait_for_server(base_url: str, timeout: float = _SERVER_READY_TIMEOUT) -> None:
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


def boot_real_server(workdir: Path) -> Tuple[str, subprocess.Popen]:
    """Boot a real ``backend.server`` subprocess against ``workdir``.

    The subprocess gets its own SQLite file, its own plans tree and its
    own port, so everything this suite asserts about is the state a
    real deployment would have written.

    The delivery transport is the stub in ``workdir``: the product is
    pointed at it through ``PDT_CC_CONNECT_BIN``, so the case neither
    needs a real transport installed nor risks sending anything to a
    real group.  What the stub was handed is recorded in
    ``workdir/cc-connect-capture.txt``.
    """
    state_db = workdir / "state.db"
    plans_dir = workdir / "plans"
    backups_dir = workdir / "backups"
    logs_dir = workdir / "logs"
    for directory in (plans_dir, backups_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)

    stub_path = _write_stub_transport(workdir)

    port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "PDT_STATE_DB_PATH": str(state_db),
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_HOST": LOOPBACK_HOST,
            "PDT_PORT": str(port),
            "PDT_ALLOWED_HOSTS": f"{LOOPBACK_HOST},localhost,testserver",
            # Pinned rather than "dry-run": an unrecognised value falls
            # through to the CC Switch probe, which reports a fleet
            # that is not there.
            "PDT_PROVIDER_MODE": "inherit",
            # The coding tool is pinned to a CLI that is not installed,
            # so the one model call this suite can reach (the
            # interview's first question) fails in milliseconds instead
            # of costing a call.  The hook binding is written before
            # that call, so the binding this suite asserts is written
            # either way.
            "PDT_DEFAULT_CODING_TOOL": "vendor-c",
            # The transport the delivery runs, and where it records
            # what it was handed.  The product reads the first of
            # these; the stub reads the second.
            TRANSPORT_BIN_ENV: str(stub_path),
            TRANSPORT_CAPTURE_ENV: str(workdir / CAPTURE_FILENAME),
            "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
        }
    )
    env.pop("ANTHROPIC_BASE_URL", None)
    # The secret handles are inherited file descriptors, and the
    # backend closes the descriptor it is handed.  In a test process
    # that descriptor is frequently an unrelated one — asyncio's kqueue
    # on macOS, for instance — and the server dies on it during
    # startup.  A test must not inherit a live secret handle, so the
    # handles are dropped from the child's environment.
    for name in [key for key in env if key.startswith("PDT_SECRET_FD_")]:
        env.pop(name, None)

    log_handle = (logs_dir / "server.log").open("wb")
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
    return base_url, proc


def teardown_server(proc: subprocess.Popen) -> None:
    """Terminate the server and close its log handle."""
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


@pytest.fixture(scope="module")
def server(tmp_path_factory) -> Tuple[str, Path]:
    """A running backend, and the directory its state lives in.

    The workdir is module-scoped so that one server serves all three
    cases: booting a server is the expensive part, and the plan ids
    keep the cases apart.
    """
    workdir = tmp_path_factory.mktemp("hitl-hooks-e2e")
    base_url, proc = boot_real_server(workdir)
    try:
        yield base_url, workdir
    finally:
        teardown_server(proc)


@pytest.fixture
def receiver(server):
    """A hook destination, closed when the case that made it ends.

    The destinations are subprocesses, so they are reaped here rather
    than left to the interpreter's exit: a case that starts a process
    has to finish it.
    """
    _base_url, workdir = server
    made: List[_Receiver] = []

    def _make(name: str, *, hang_sec: float = 0.0) -> _Receiver:
        made.append(_Receiver(workdir, name, hang_sec=hang_sec))
        return made[-1]

    yield _make

    for destination in made:
        destination.close()


# ---------------------------------------------------------------------------
# HTTP + file + SQLite helpers
# ---------------------------------------------------------------------------


def _post_json(
    base_url: str, path: str, payload: Dict[str, Any], *, timeout: float = 15.0
) -> Tuple[int, str, float]:
    """POST ``payload`` and return ``(status, body, elapsed_seconds)``.

    The elapsed time is the thing one of the cases is about: the
    creation call has to come back while a destination is still
    hanging, so the clock is read around the whole request.
    """
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            REQUEST_HEADER: REQUEST_HEADER_VALUE,
            "Host": LOOPBACK_HOST,
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8") if exc.fp else ""
    elapsed = time.monotonic() - started
    return status, body, elapsed


def _write_config(plan_dir: Path, hooks: List[Dict[str, Any]]) -> Path:
    """Write a plan's hook configuration.

    The document is written the way a plan creation would leave it:
    the schema version, the mode, the timeout, and the destinations.
    The destinations are written here rather than produced by a plan
    creation because a plan creation names a *group* to ask, and this
    suite needs destinations it can watch on a socket.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / CONFIG_FILENAME
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "auto",
                "timeout_sec": 1800,
                "hooks": list(hooks),
            }
        ),
        encoding="utf-8",
    )
    return path


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


def _wait_for_audit(
    plan_dir: Path, count: int, *, timeout: float = 30.0
) -> List[Dict[str, Any]]:
    """Block until the dispatch audit holds ``count`` lines.

    The dispatch runs on a thread of its own, so the audit line for an
    event that involved a hanging destination only appears once that
    destination has been abandoned.  The wait is bounded; the lines
    that are there when the wait gives up are the answer.
    """
    path = plan_dir / HOOK_AUDIT_FILENAME
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = _read_jsonl(path)
        if len(lines) >= count:
            return lines
        time.sleep(0.1)
    return _read_jsonl(path)


def _wait_for_delivery(
    plan_dir: Path, capture: Path, *, timeout: float = DELIVERY_TIMEOUT_SEC
) -> List[Dict[str, Any]]:
    """Block until the delivery has been made and recorded.

    Two things have to be there before the delivery counts as made: the
    dispatch audit has to hold the line for the event, and the
    transport has to have recorded what it was handed.  The wait is for
    both, because either one alone is a single layer's opinion — an
    audit line says the dispatch finished, and the capture says the
    message arrived.

    The delivery runs on a worker thread, so neither is there when the
    request comes back: the request is answered when the question is
    written down, not when the slowest destination answers.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = _read_jsonl(plan_dir / HOOK_AUDIT_FILENAME)
        if lines and capture.exists():
            recorded = capture.read_text(encoding="utf-8", errors="replace")
            if recorded.strip():
                return lines
        time.sleep(0.1)
    return _read_jsonl(plan_dir / HOOK_AUDIT_FILENAME)


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


def _wait_for_bodies(
    receiver: _Receiver, count: int, *, timeout: float = 20.0
) -> List[Dict[str, Any]]:
    """Block until the destination has received ``count`` events."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        bodies = receiver.bodies()
        if len(bodies) >= count:
            return bodies
        time.sleep(0.05)
    return receiver.bodies()


def _event_payload(decision: Dict[str, Any]) -> Dict[str, Any]:
    """The payload the outlet carries for a question that was asked.

    The architecture names the fields: ``plan_id`` / ``decision_id`` /
    ``task_id`` / ``kind`` / ``options`` / ``default_option`` /
    ``deadline_at``.  A decision's *kind* — the plan's four disposition
    kinds, ``out_of_scope`` and its siblings — travels in the
    decision's ``source`` field, so that is what ``kind`` carries here.

    The product builds this payload itself; the copy here is for the
    one place the suite talks to the dispatcher directly — the negative
    control that a destination which is not there is not counted as
    reached.
    """
    return {
        "plan_id": decision["plan_id"],
        "decision_id": decision["decision_id"],
        "task_id": decision["task_id"],
        "kind": decision["source"],
        "options": list(decision["options"]),
        "default_option": decision["default_option"],
        "deadline_at": decision["deadline_at"],
    }


# ---------------------------------------------------------------------------
# 1 — the fan-out really leaves the building
# ---------------------------------------------------------------------------


def test_fanout_nonblocking(server, receiver):
    """A question reaches every destination, and a hung one cannot hold
    the asker hostage.

    Three destinations are configured: two that answer and one that
    accepts the connection and then says nothing.  The facts that have
    to hold together are that the creation call comes back while the
    third destination is still silent, that the two healthy
    destinations each receive the event exactly once, and that the
    silent one is written down as a timeout — once, not retried.
    """
    base_url, workdir = server
    plan_id = "e2e-fanout"
    plan_dir = workdir / "plans" / plan_id

    first = receiver("fanout-first")
    second = receiver("fanout-second")
    hanging = receiver("fanout-hanging", hang_sec=HANG_SEC)

    _write_config(
        plan_dir,
        [
            {"kind": "http", "url": first.url},
            {"kind": "http", "url": second.url},
            {
                "kind": "http",
                "url": hanging.url,
                "timeout_sec": HANG_TIMEOUT_SEC,
            },
        ],
    )

    # 1. The question is asked through the real route, in the real
    #    server, and the ask comes back inside its budget.
    status, body, elapsed = _post_json(
        base_url, f"/api/hitl/{plan_id}/decisions", QUESTION_BODY
    )
    assert status == 200, f"the creation call failed: {body}"
    assert elapsed < CREATE_BOUND_SEC, (
        f"the creation call took {elapsed:.3f}s; it must answer inside "
        f"{CREATE_BOUND_SEC}s"
    )
    decision = json.loads(body)
    assert decision["decision_id"]
    assert decision["status"] == "pending", (
        "a question somebody was asked is reported as "
        f"{decision['status']!r}; it is still waiting for a human"
    )

    # 2. The outlet: the *service* dispatches the event, on a thread of
    #    its own, so the request path never waits for a destination.
    #    The fan-out really leaves the building: the hanging destination
    #    has the request and is not answering it.
    hanging_bodies = _wait_for_bodies(hanging, 1, timeout=20.0)
    assert hanging_bodies, (
        "the hanging destination never received the event; the fan-out "
        "did not leave the building"
    )

    # 3. While the outlet waits on the hang, the creation path still
    #    answers inside its budget: the outlet is not on it.  The second
    #    question is asked of a plan whose *only* destination is the
    #    hanging one, so the two healthy destinations are not asked
    #    twice and their "exactly once" count stays meaningful.
    hang_plan_id = "e2e-fanout-hanging"
    hang_plan_dir = workdir / "plans" / hang_plan_id
    _write_config(
        hang_plan_dir,
        [
            {
                "kind": "http",
                "url": hanging.url,
                "timeout_sec": HANG_TIMEOUT_SEC,
            },
        ],
    )
    status, body, elapsed = _post_json(
        base_url, f"/api/hitl/{hang_plan_id}/decisions", QUESTION_BODY
    )
    assert status == 200, f"the second creation call failed: {body}"
    assert elapsed < CREATE_BOUND_SEC, (
        "a hanging destination delayed the creation API: "
        f"{elapsed:.3f}s"
    )
    hanging_decision = json.loads(body)
    assert hanging_decision["status"] == "pending", (
        "a question that is being delivered is reported as "
        f"{hanging_decision['status']!r}"
    )

    # 4. Both healthy destinations get the event — once each.  The count
    #    is read again after a pause, because "exactly one" is a claim
    #    about a second delivery not happening.
    for destination in (first, second):
        bodies = _wait_for_bodies(destination, 1)
        assert bodies, (
            f"the destination {destination.url} received no event at all; "
            "the fan-out did not leave the building"
        )
        time.sleep(1.5)
        bodies = destination.bodies()
        assert len(bodies) == 1, (
            f"the destination {destination.url} received {len(bodies)} "
            "events; a fan-out is one question asked once"
        )
        event = bodies[0]
        assert event["event"] == EVENT, (
            f"the destination received {event['event']!r}; the outlet's "
            f"event name is {EVENT!r}"
        )
        assert event["decision_id"] == decision["decision_id"], (
            "the event carried a different decision's identity"
        )
        assert event["plan_id"] == plan_id
        assert event["task_id"] == decision["task_id"]
        assert event["kind"] == decision["source"]
        assert event["options"] == list(decision["options"])
        assert event["default_option"] == decision["default_option"]
        assert event["deadline_at"] == decision["deadline_at"], (
            "the event carried no deadline; a human cannot tell how long "
            "the question is still waiting"
        )

    # 5. The dispatch audit: one line for the event, one entry per
    #    destination, and the hung one recorded as a timeout.  The line
    #    only appears once the hanging destination has been abandoned,
    #    which is what "abandoned on its own clock" means.
    audit_lines = _wait_for_audit(plan_dir, 1, timeout=HANG_TIMEOUT_SEC + 20)
    assert len(audit_lines) == 1, (
        f"the dispatch wrote {len(audit_lines)} audit lines for one "
        "event; the event was delivered more than once"
    )
    audit = audit_lines[0]
    assert audit["event"] == EVENT
    assert audit["delivered"] == 2
    assert audit["timed_out"] == 1
    assert audit["skipped"] == 0
    entries = audit["entries"]
    assert len(entries) == 3, (
        f"the audit records {len(entries)} destinations; the plan "
        "configures three and each is attempted exactly once"
    )
    timed_out = [entry for entry in entries if entry["outcome"] == "timed_out"]
    assert len(timed_out) == 1, (
        f"the silent destination is recorded {len(timed_out)} times; a "
        "hung destination is abandoned, not retried"
    )
    assert timed_out[0]["kind"] == "http"
    assert timed_out[0]["timeout_sec"] == HANG_TIMEOUT_SEC
    assert len([entry for entry in entries if entry["outcome"] == "delivered"]) == 2

    # 6. The decision itself is in the database, with the status the
    #    response reported: somebody was asked, so it is still open.
    row = _decision_row(workdir / "state.db", decision["decision_id"])
    assert row is not None, "the decision was not written to the database"
    assert row["status"] == "pending"
    assert row["answer"] is None, (
        "a question somebody was asked was given an answer"
    )
    assert row["plan_id"] == plan_id

    # The negative control: a destination that is not there must not be
    # counted as reached.  Without it, "delivered == 2" could be a
    # constant of the report rather than a measurement of the fan-out.
    dead_port = _free_port()
    dead_plan_dir = workdir / "plans" / "e2e-fanout-unreachable"
    _write_config(
        dead_plan_dir,
        [{"kind": "http", "url": f"http://{LOOPBACK_HOST}:{dead_port}/hook"}],
    )
    dead = dispatch(EVENT, _event_payload(decision), dead_plan_dir)
    assert dead.delivered == 0, (
        "a destination that is not there was reported as reached: "
        f"{dead}"
    )
    assert dead.failed == 1, (
        f"the unreachable destination was not reported as failed: {dead}"
    )


# ---------------------------------------------------------------------------
# 2 — a plan with no hooks asks nobody
# ---------------------------------------------------------------------------


def test_empty_hooks_unroutable(server):
    """A plan with no outlet degrades instead of failing.

    Nobody is asked, so nothing may claim that a human replied: the
    decision is not answered and not defaulted, no destination sees a
    request, and the fact that the event went nowhere is written down
    rather than left implicit.

    The state that means "nobody was asked" is ``unroutable``.  The
    service settles the question there — not answered, not defaulted,
    and not waiting for a human who was never asked — and the
    decision's own audit mirror records both the question and its
    fate.
    """
    base_url, workdir = server
    plan_id = "e2e-unroutable"
    plan_dir = workdir / "plans" / plan_id

    # A plan created without a session context has no outlet: the
    # creation request binds nothing, so the hook list is empty.
    status, body, _ = _post_json(
        base_url,
        "/api/interview/start",
        {
            "requirement": "Land the HITL hook fan-out.",
            "plan_id": plan_id,
        },
        timeout=60.0,
    )
    assert status != 403, (
        f"the request guard refused the creation request ({status}): {body}"
    )
    config_path = plan_dir / CONFIG_FILENAME
    assert config_path.exists(), (
        "a plan created without a session context recorded no "
        "configuration; 'nobody was asked' has to be written down"
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["hooks"] == [], (
        f"a plan created without a session context has hooks: "
        f"{config['hooks']!r}; it must ask nobody"
    )

    # The question is asked...
    status, body, _ = _post_json(
        base_url, f"/api/hitl/{plan_id}/decisions", QUESTION_BODY
    )
    assert status == 200, f"the creation call failed: {body}"
    decision = json.loads(body)

    # ...and nobody was asked, so the question is settled as
    # ``unroutable`` rather than left waiting for a human who was
    # never asked.
    assert decision["status"] == "unroutable", (
        "a decision nobody was asked about is reported as "
        f"{decision['status']!r}; it must be unroutable"
    )

    # The dispatch wrote the audit line: this event went to nobody, and
    # no destination was even attempted.
    audit_lines = _read_jsonl(plan_dir / HOOK_AUDIT_FILENAME)
    assert len(audit_lines) == 1, (
        f"the dispatch wrote {len(audit_lines)} audit lines for one event"
    )
    audit = audit_lines[0]
    assert audit["event"] == EVENT
    assert audit["delivered"] == 0
    assert audit["timed_out"] == 0
    assert audit["skipped"] == 0
    assert audit["entries"] == [], (
        "the audit records destinations for a plan that configures none"
    )

    # The question is not dressed up as answered or defaulted.
    row = _decision_row(workdir / "state.db", decision["decision_id"])
    assert row is not None, "the decision was not written to the database"
    assert row["status"] == "unroutable", (
        f"the stored decision is {row['status']!r}; nobody was asked"
    )
    assert row["answer"] is None, (
        "a decision nobody was asked about was given an answer"
    )
    assert row["answered_by"] is None
    assert row["answered_at"] is None

    # The decision's own mirror records that it was asked — and that it
    # went nowhere.
    decision_audit = _read_jsonl(plan_dir / DECISION_AUDIT_FILENAME)
    assert [record["event"] for record in decision_audit] == [
        "created",
        "unroutable",
    ], (
        "the decision's audit mirror does not record the transition to "
        f"unroutable: {[r['event'] for r in decision_audit]}"
    )


# ---------------------------------------------------------------------------
# 3 — the binding a creation request carries
# ---------------------------------------------------------------------------


def test_creation_binds_destination(server):
    """The binding on a plan-creation request reaches that plan's
    configuration, and the outlet delivers the question to it.

    The plan-creation doors record the origin of the plan next to the
    plan, in the plan's own ``hitl_config.json``: which session it was
    created in, and where a question about it should go.  A plan
    created outside a session has neither, so it asks nobody — which
    is the right answer for a bare ``curl``, and a state the plan
    directory records rather than leaves implicit.

    The binding is a **command**, not a description of a transport: the
    plan's file says what to run, and the dispatcher runs it.  What the
    case pins is that the binding travels all the way to the outlet —
    the project and the session the creation request named are the
    ones the delivery is addressed to — and that a delivery which
    reached its transport leaves the question ``pending``, waiting for
    the human it was addressed to, rather than settling it as though
    nobody had been asked.

    The delivery runs on a worker thread, so the audit line and the
    transport's own record are read after a wait rather than straight
    after the request: the request is answered when the question is
    written down, not when the slowest destination answers.
    """
    base_url, workdir = server

    bound_plan = "e2e-bound"
    status, body, _ = _post_json(
        base_url,
        "/api/interview/start",
        {
            "requirement": "Land the HITL hook fan-out.",
            "plan_id": bound_plan,
            "hook_destination": "grp-e2e",
            "hook_session_id": "sess-e2e",
        },
        timeout=60.0,
    )
    assert status != 403, (
        f"the request guard refused the creation request ({status}): {body}"
    )

    config_path = workdir / "plans" / bound_plan / CONFIG_FILENAME
    assert config_path.exists(), (
        "the creation request's binding was not written to the plan's "
        "configuration; the destination never reached the plan"
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["version"] == 1
    assert config["mode"] == "auto"
    assert config["timeout_sec"] == 1800
    assert config["source"] == "plan_creation", (
        f"the configuration records the source as {config['source']!r}; a "
        "plan created in a session records where it came from"
    )
    assert config["captured_at"], "the configuration records no capture time"

    hooks = config["hooks"]
    assert len(hooks) == 1, (
        "the configuration's hook list does not match the creation "
        f"request: {hooks!r}"
    )
    hook = hooks[0]
    assert hook["kind"] == "cmd", (
        "the configuration's hook is not a delivery command: "
        f"{hook!r}"
    )
    # The command is read back as the argv the dispatcher will run —
    # not as a second implementation of the quoting.
    argv = shlex.split(hook["command"])
    assert argv[3:] == ["--project", "grp-e2e", "--session", "sess-e2e"], (
        "the delivery command does not carry the binding the creation "
        f"request named: {hook['command']!r}"
    )

    # The plan really was created: its routing row is in the database.
    connection = sqlite3.connect(f"file:{workdir / 'state.db'}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT plan_id FROM plan_routing WHERE plan_id = ?", (bound_plan,)
        ).fetchall()
    finally:
        connection.close()
    assert rows, "the creation request did not create a plan"

    # The binding travels to the outlet: the destination the creation
    # request named is the destination the delivery is addressed to.
    bound_plan_dir = workdir / "plans" / bound_plan
    status, body, _ = _post_json(
        base_url, f"/api/hitl/{bound_plan}/decisions", QUESTION_BODY
    )
    assert status == 200, f"the creation call failed: {body}"
    decision = json.loads(body)

    # Somebody can be asked, so the question waits for the answer it
    # was asked for: it is not settled as though nobody had been asked.
    assert decision["status"] == "pending", (
        "a question that reached its destination is reported as "
        f"{decision['status']!r}; it must be pending"
    )

    # The delivery ran on a worker thread, so the audit line and the
    # transport's own record are read after a wait.
    capture = workdir / CAPTURE_FILENAME
    audit_lines = _wait_for_delivery(bound_plan_dir, capture)
    assert len(audit_lines) == 1, (
        f"the dispatch wrote {len(audit_lines)} audit lines for one event"
    )
    entries = audit_lines[0]["entries"]
    assert len(entries) == 1, (
        f"the audit records {len(entries)} destinations; the plan binds "
        "exactly one"
    )
    assert entries[0]["kind"] == "cmd", (
        f"the audit records a delivery of another kind: {entries[0]!r}"
    )
    assert entries[0]["outcome"] == "delivered", (
        "the delivery did not reach the transport: "
        f"{entries[0]!r}"
    )
    assert entries[0]["target"] != "grp-e2e", (
        "the audit records the destination as the target; the target is "
        f"the command that was run: {entries[0]!r}"
    )
    assert "backend.hooks.session_send" in entries[0]["target"], (
        "the audit does not record the command that was run: "
        f"{entries[0]!r}"
    )
    assert "--project grp-e2e --session sess-e2e" in entries[0]["target"], (
        "the audit does not record the binding the creation request "
        f"named: {entries[0]!r}"
    )
    assert audit_lines[0]["delivered"] == 1, (
        "the dispatch did not count the delivery: "
        f"{audit_lines[0]!r}"
    )

    # The transport was handed the destination the creation request
    # named, and the question itself.
    recorded = capture.read_text(encoding="utf-8", errors="replace")
    assert "grp-e2e" in recorded, (
        "the transport was not addressed to the destination the creation "
        f"request named: {recorded!r}"
    )
    assert "sess-e2e" in recorded, (
        "the transport was not addressed to the session the creation "
        f"request named: {recorded!r}"
    )
    assert QUESTION_BODY["question"] in recorded, (
        f"the delivery carried no question: {recorded!r}"
    )

    row = _decision_row(workdir / "state.db", decision["decision_id"])
    assert row is not None, "the decision was not written to the database"
    assert row["status"] == "pending", (
        f"the stored decision is {row['status']!r}; it was delivered, so "
        "it waits for its answer"
    )
    assert row["answer"] is None, (
        "a decision nobody has answered was given an answer"
    )
    assert row["answered_by"] is None
    assert row["answered_at"] is None

    # A creation request that carries no session context binds
    # nothing: the plan asks nobody.
    unbound_plan = "e2e-unbound"
    status, body, _ = _post_json(
        base_url,
        "/api/interview/start",
        {
            "requirement": "Land the HITL hook fan-out.",
            "plan_id": unbound_plan,
        },
        timeout=60.0,
    )
    assert status != 403, (
        f"the request guard refused the creation request ({status}): {body}"
    )

    unbound_path = workdir / "plans" / unbound_plan / CONFIG_FILENAME
    assert unbound_path.exists(), (
        "a plan created without a session context recorded no "
        "configuration; 'nobody was asked' has to be written down"
    )
    unbound = json.loads(unbound_path.read_text(encoding="utf-8"))
    assert unbound["hooks"] == [], (
        f"a plan created without a session context has hooks: "
        f"{unbound['hooks']!r}; it must ask nobody"
    )
    assert unbound["source"] == "", (
        f"a plan created outside a session records the source as "
        f"{unbound['source']!r}"
    )

    # And a question asked of that plan reaches nobody either.
    status, body, _ = _post_json(
        base_url, f"/api/hitl/{unbound_plan}/decisions", QUESTION_BODY
    )
    assert status == 200, f"the creation call failed: {body}"
    unbound_decision = json.loads(body)
    assert unbound_decision["status"] == "unroutable", (
        "a question asked of a plan with no outlet is reported as "
        f"{unbound_decision['status']!r}; it must be unroutable"
    )
    unbound_audit = _read_jsonl(workdir / "plans" / unbound_plan / HOOK_AUDIT_FILENAME)
    assert len(unbound_audit) == 1, (
        f"the dispatch wrote {len(unbound_audit)} audit lines for one event"
    )
    assert unbound_audit[0]["delivered"] == 0, (
        "a plan with no binding delivered the event anyway: "
        f"{unbound_audit[0]!r}"
    )
    assert unbound_audit[0]["entries"] == [], (
        "a plan with no binding has a destination to skip: "
        f"{unbound_audit[0]!r}"
    )