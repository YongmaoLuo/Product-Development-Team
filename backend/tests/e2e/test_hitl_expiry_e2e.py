"""End-to-end: what a missed deadline does — two ways, in one real process.

Background
----------
A human-in-the-loop question is only finished when somebody answers it,
and sometimes nobody does.  What happens then is not one behaviour but
two, and the difference between them is the whole point of the feature:

* an **auto** question carries a default answer, so when its deadline
  passes the default *becomes* the answer: the decision ends
  ``defaulted`` and the plan carries on;
* a **strict** question has no substitute for a human, so its default
  answer must never be used — not even when the deadline passes.  The
  round stops instead: the question becomes ``paused`` (still
  answerable), and the plan gets a ``pause_state.json`` naming it, so
  the resume path can find the question again instead of guessing which
  one it was.

The two rules are stated in two different modules, and the machinery that
acts on them lives in two different places: ``hitl/service.py`` knows
*how* an overdue question resolves, and the server's heartbeat tick
knows *when*.  ``server.HeartbeatMonitor`` sweeps for overdue ``auto``
questions on every tick, whether or not a plan is in flight;
``verification_loop._lazy_check_verification`` pauses an overdue
``strict`` question and writes the checkpoint.

Why this file exists
--------------------
Every other test of this behaviour injects a clock.  The integration
suite (``tests/integration/test_verification_loop_expire_due.py``)
freezes ``verification_loop._hitl_now`` and moves it past the deadline,
which is the right way to pin the *rule* — and it cannot see the two
things that only exist in a running process:

* **the tick.**  The deadline is noticed by a periodic entry point, not
  by the request that asked the question.  A test that calls the
  transition function directly proves the transition and says nothing
  about whether anything ever calls it.
* **the pair.**  ``auto`` and ``strict`` are two readings of one
  mechanism.  Either one alone is satisfied by a sweep that resolves
  every overdue question the same way — an implementation that forgot
  the difference between them would pass both single-mode cases and
  fail the product.

So this file drives the real thing: a real ``backend.server``
subprocess, a real two-second deadline, and the server's own tick.
Nothing here fakes a clock, and nothing here calls the transition
directly.  The deadline is deliberately **shorter than the tick**
(``server.HEARTBEAT_INTERVAL``, thirty seconds): the case therefore
proves that the expiry is noticed on a tick that lands *after* the
deadline, which is the case a shorter tick would hide.

What is pinned here
-------------------
1. ``test_auto_expires_to_default`` — an ``auto`` question nobody
   answered ends ``defaulted`` with the stored default as its answer,
   a second answer is a conflict (``409``), and the audit mirror holds
   exactly two lines: the question and its defaulting.
2. ``test_strict_expires_to_paused`` — a ``strict`` question nobody
   answered ends ``paused`` with **no** answer at all (the default is
   exactly the answer the backend may not give), the plan's
   ``pause_state.json`` names it and says why, and a human who answers
   afterwards is still taken seriously.
3. ``test_auto_vs_strict_pair`` — both of the above, asked in the same
   process at the same time, asserted in the same case.  This is the
   case the other two cannot replace: one mode passing is not evidence
   that the modes differ.
4. ``test_the_wait_outlives_a_tick_that_lands_after_the_deadline`` —
   the bound the three cases wait for outlives the tick that notices
   the expiry, checked against the product's own tick length.  This is
   the file's premise made executable: the cases do not resolve the
   question themselves, and a wait shorter than a tick-after-the-deadline
   would fail for a reason that has nothing to do with the expiry.

How the strict case gets a round to be interrupted by
------------------------------------------------------
A ``strict`` timeout is only ever noticed for a plan the watchdog is
watching, so the case has to give the server a plan whose verification
round is in flight.  It does that the way a **restart** does: the
``plan_verification`` row says the round is running and the plan
directory is still there, and the server's own startup recovery scan
(``verification_loop._recover_verification_states``) rebuilds the
in-memory state from exactly that.  Nothing is injected into the
running process, and no debug route is switched on — the fixture is
the state a killed backend leaves behind, and everything after it is
the product's own code.

The box
-------
A real ``backend.server`` subprocess with its own SQLite file, its own
plans tree and its own port, exactly as ``tests/e2e/
test_hitl_hooks_e2e.py`` builds one.  The three layers are asserted at
once, because a single layer is a single layer's opinion: the HTTP
response, the SQLite row (read with ``sqlite3`` directly, not through
the service), and the files in the plan directory.  A response that
says ``defaulted`` proves nothing; a row that says ``defaulted``
proves nothing; an audit line that says ``defaulted`` proves nothing.
All three have to say the same thing.

Test command::

    cd backend && ./.venv/bin/python3 -m pytest \
        tests/e2e/test_hitl_expiry_e2e.py -q -p no:xdist

The cases are serial and have to stay that way: each one reads the wall
clock, and the bound this file asserts — a deadline that really
expires, noticed by the server's own tick — is only meaningful on a
machine that is not running another case beside it.  In CI the file
runs in the serial lane (``pytest tests/ -m "time_sensitive or perf"``),
alone on the runner, with the 1800-second budget that lane is the only
one to offer.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest

#: ``backend/`` — this file lives in ``backend/tests/e2e/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The repository root, one level above ``backend/``.
PROJECT_ROOT = BACKEND_DIR.parent

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from hooks import config_path, write_for_plan  # noqa: E402
from utils.atomic_io import atomic_write_json  # noqa: E402

#: The marker this file carries, and why it is ``perf`` and not
#: ``time_sensitive``.
#:
#: The plan's brief for this file asks for ``@pytest.mark.time_sensitive``
#: — the marker the repository reserves for "asserts on wall-clock time,
#: must run alone on the runner".  This file would rather carry it: its
#: cases assert on a deadline that really expires, and a bound measured
#: on a runner that is sharing its CPU with a parallel shard is a bound
#: about the runner, not about the code.
#:
#: But ``backend/pytest.ini`` ships ``-m "not time_sensitive"`` in its
#: ``addopts``, so a ``time_sensitive``-marked file is *deselected* by
#: every plain ``pytest`` invocation — including the command the plan
#: itself uses to run this file, which would then collect nothing and
#: prove nothing.  ``perf`` is the marker the repository routes to the
#: very same lane (``pytest tests/ -m "time_sensitive or perf"``, whose
#: ``--timeout=1800`` is the only budget these can meet on a shared
#: runner), and the sibling file this plan produced for the same reason
#: (``tests/perf/test_hitl_concurrency.py``) carries it for the same
#: reason.
#:
#: So the file lands in exactly the lane the brief asks for — the
#: exclusive serial one, alone on the runner, no xdist — *and* the
#: plan's own command still collects it.
#:
#: Two facts inside the repository pin that choice, and they are worth
#: writing down because the brief asks for the other marker:
#:
#: * the task record for this file lists exactly one command to run it
#:   with — ``pytest tests/e2e/test_hitl_expiry_e2e.py -q -p no:xdist``,
#:   from ``backend/`` — and that command carries no ``-m`` to opt back
#:   in.  Under ``time_sensitive`` it would collect nothing and exit 5,
#:   and the plan reads a non-zero exit as a failed task.
#: * the sibling file this plan produced for the same kind of brief
#:   (``tests/perf/test_hitl_concurrency.py``, whose brief also says
#:   "标注 ``@pytest.mark.time_sensitive``") already carries ``perf``
#:   for exactly this reason.
#:
#: Flipping the marker to ``time_sensitive`` is a one-word change, but
#: the command above has to change with it (``-m time_sensitive``), or
#: the file stops being collected at all.
pytestmark = [pytest.mark.perf]


# ---------------------------------------------------------------------------
# The shape of the question
# ---------------------------------------------------------------------------

#: The question the cases ask.  Its text is deliberately plain: what is
#: under test is the expiry, not the prose.
QUESTION_BODY: Dict[str, Any] = {
    "question": "Ship the change as written?",
    "options": ["accept", "reject"],
    "default_option": "accept",
    "task_id": "T-e2e-hitl-expiry",
}

#: The answer an overdue *auto* question falls back to, and the answer a
#: *strict* question must never be given.
DEFAULT_OPTION = "accept"

#: How long the question waits.  Two seconds — short enough that a case
#: does not spend a minute waiting for the deadline, and **shorter than
#: the server's tick**, which is the point of the whole file.
TIMEOUT_SEC = 2

#: How long a case waits for the expiry to be noticed.
#:
#: The server notices on its heartbeat tick (``server.HEARTBEAT_INTERVAL``
#: — thirty seconds today).  The tick is *not* assumed to fall inside
#: the deadline: the question lives two seconds and the tick lives
#: thirty, so the expiry is noticed at the first tick **after** the
#: deadline, at worst thirty seconds later.  The bound is therefore the
#: deadline plus one tick plus slack for a slow tick and a slow poll.
#:
#: Nothing in this file may assume the tick is quicker than the
#: deadline.  That assumption would make every case pass for the wrong
#: reason: a sweep that ran on the request path would satisfy it, and
#: the sweep that has to exist — the periodic one — would not be
#: exercised at all.
POLL_BOUND_SEC = 42

#: How often the poll asks the server what happened.
POLL_INTERVAL_SEC = 0.25


# ---------------------------------------------------------------------------
# The plan ids
# ---------------------------------------------------------------------------

#: One plan per case, so the cases cannot see each other's decisions and
#: each case's audit mirror says only its own story.
AUTO_PLAN_ID = "e2e-hitl-expiry-auto"
STRICT_PLAN_ID = "e2e-hitl-expiry-strict"
PAIR_AUTO_PLAN_ID = "e2e-hitl-expiry-pair-auto"
PAIR_STRICT_PLAN_ID = "e2e-hitl-expiry-pair-strict"


# ---------------------------------------------------------------------------
# The server under test
# ---------------------------------------------------------------------------

#: The request guard's header.  Every ``/api/*`` call carries it, and
#: every call names a loopback host — the guard is part of the
#: production posture this suite drives the real process through.
REQUEST_HEADER = "X-PDT-Request"
REQUEST_HEADER_VALUE = "1"
LOOPBACK_HOST = "127.0.0.1"

#: The audit mirror the store writes next to the plan it belongs to.
DECISION_AUDIT_FILENAME = "hitl_decisions.jsonl"

#: The pause checkpoint the strict timeout writes.
PAUSE_CHECKPOINT_FILENAME = "pause_state.json"

#: Prefer the checkout's own virtualenv; fall back to the interpreter
#: already running pytest when there is no ``backend/.venv`` (a fresh
#: worktree, a container, CI before the venv bootstrap step).  The
#: fallback is deliberately not a skip: a suite that skips is a suite
#: that asserts nothing.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)


def _free_port() -> int:
    """Bind an ephemeral loopback port and return its number.

    The kernel hands out a free port and releasing the socket leaves a
    small race window; the readiness polling below tolerates it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK_HOST, 0))
        return sock.getsockname()[1]


def _wait_for_server(log_path: Path, timeout: float = 90.0) -> str:
    """Block until the server answers ``/health``, and return its base URL.

    The port is read out of the server's own log rather than assumed:
    the server walks forward from ``PDT_PORT`` when the port it was
    given is taken, and a suite that assumed the port it asked for
    would fail for a reason that has nothing to do with the expiry.
    """
    deadline = time.monotonic() + timeout
    last: Optional[Exception] = None
    while time.monotonic() < deadline:
        port = _port_from_log(log_path)
        if port is not None:
            base_url = f"http://{LOOPBACK_HOST}:{port}"
            try:
                with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                    if 200 <= resp.status < 300:
                        return base_url
            except (urllib.error.URLError, OSError) as exc:
                last = exc
        time.sleep(0.1)
    raise RuntimeError(
        f"the server did not start in {timeout}s; last error: {last!r}; "
        f"log tail:\n{_tail(log_path)}"
    )


def _port_from_log(log_path: Path) -> Optional[int]:
    """The port the server says it bound, or ``None`` while it has not.

    Two lines are read: the server's own line, and uvicorn's.  The
    first is a ``print`` — the child's stdout is unbuffered here (see
    ``PYTHONUNBUFFERED`` below), so it lands as soon as it is written;
    the second is uvicorn's own announcement, which is the
    authoritative one.
    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if "Starting uvicorn on" in line:
            _, _, tail = line.partition("Starting uvicorn on")
            _, _, port = tail.strip().rpartition(":")
            if port.isdigit():
                return int(port)
        if "Uvicorn running on" in line:
            _, _, tail = line.partition("Uvicorn running on")
            _, _, port = tail.strip().rpartition(":")
            if port.isdigit():
                return int(port)
    return None


def _tail(path: Path, lines: int = 40) -> str:
    """The last ``lines`` lines of a file, for a failure message."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(content[-lines:])


class _Backend:
    """A running backend, and the state it keeps."""

    def __init__(
        self,
        *,
        base_url: str,
        plans_dir: Path,
        state_db: Path,
        log_path: Path,
    ) -> None:
        self.base_url = base_url
        self.plans_dir = plans_dir
        self.state_db = state_db
        self.log_path = log_path


def _seed_running_round(state_db: Path, plan_id: str) -> None:
    """Leave behind the state a backend that was killed mid-round leaves.

    A ``plan_verification`` row that says the round is running, next to
    a plan directory that is still there.  That is what the server's own
    startup recovery scan (``verification_loop.
    _recover_verification_states``) rebuilds the in-memory state from,
    and it is why the watchdog watches the plan at all.

    The row is written through the product's own repository, so the
    shape of the state is the shape the server will read — not a
    hand-rolled approximation of it.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    connection = open_db(state_db)
    try:
        migrate(connection)
        VerificationRepository(connection).insert(
            plan_id,
            verification_status="running",
            round=1,
            max_rounds=3,
            runtime_state={
                "verification_status": "running",
                "verification_round": 1,
            },
            started_at=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        connection.close()


@contextlib.contextmanager
def _backend(
    workdir: Path, *, running_rounds: Sequence[str] = ()
) -> Iterator[_Backend]:
    """Boot a real ``backend.server`` subprocess against ``workdir``.

    The subprocess gets its own SQLite file, its own plans tree and its
    own port, so everything the cases assert about is the state a real
    deployment would have written.
    """
    plans_dir = workdir / "plans"
    logs_dir = workdir / "logs"
    plans_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    state_db = workdir / "state.db"
    log_path = logs_dir / "server.log"

    for plan_id in running_rounds:
        _seed_running_round(state_db, plan_id)

    env = dict(os.environ)
    env.update(
        {
            "PDT_STATE_DB_PATH": str(state_db),
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_HOST": LOOPBACK_HOST,
            "PDT_PORT": str(_free_port()),
            "PDT_ALLOWED_HOSTS": f"{LOOPBACK_HOST},localhost,testserver",
            # Pinned rather than "dry-run": an unrecognised value falls
            # through to the CC Switch probe, which reports a fleet that
            # is not there.
            "PDT_PROVIDER_MODE": "inherit",
            # The coding tool is pinned to a CLI that is not installed,
            # so the one model call this suite could reach fails in
            # milliseconds instead of costing a call.
            "PDT_DEFAULT_CODING_TOOL": "vendor-c",
            # The child's stdout is a file, so Python would block-buffer
            # it and the "Starting uvicorn" line would arrive seconds
            # late — or not at all, if the server never flushed it.
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
        }
    )
    env.pop("ANTHROPIC_BASE_URL", None)
    # The secret handles are inherited file descriptors, and the backend
    # closes the descriptor it is handed.  In a test process that
    # descriptor is frequently an unrelated one — asyncio's kqueue on
    # macOS, for instance — and the server dies on it during startup.
    # A test must not inherit a live secret handle, so the handles are
    # dropped from the child's environment.
    for name in [key for key in env if key.startswith("PDT_SECRET_FD_")]:
        env.pop(name, None)

    log_handle = log_path.open("wb")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "backend.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        base_url = _wait_for_server(log_path)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        log_handle.close()
        raise

    try:
        yield _Backend(
            base_url=base_url,
            plans_dir=plans_dir,
            state_db=state_db,
            log_path=log_path,
        )
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_handle.close()


# ---------------------------------------------------------------------------
# HTTP + file + SQLite helpers
# ---------------------------------------------------------------------------


def _request(
    method: str,
    base_url: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 15.0,
) -> Tuple[int, str]:
    """Make one request and return ``(status, body)``."""
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
    base_url: str, path: str, payload: Dict[str, Any], *, timeout: float = 15.0
) -> Tuple[int, str]:
    """POST ``payload`` and return ``(status, body)``."""
    return _request("POST", base_url, path, payload, timeout=timeout)


def _get_json(
    base_url: str, path: str, *, timeout: float = 15.0
) -> Tuple[int, str]:
    """GET ``path`` and return ``(status, body)``."""
    return _request("GET", base_url, path, None, timeout=timeout)


def _prepare_plan(plans_dir: Path, plan_id: str, mode: str) -> Path:
    """Give the plan its directory and its own HITL configuration.

    The configuration is written through the product's own writer
    (``hooks.write_for_plan``), because that file is the plan's, and
    the plan's configuration — not the caller's request — is what
    decides the mode and the deadline the question is stored with.

    The outlet is then given a destination the plan's *origin* cannot
    express: ``write_for_plan`` only ever writes the ``cc_connect``
    hook a session implies, and a question nobody can be asked is
    ``unroutable`` from the start — it is never ``pending``, so it can
    neither expire nor be paused.  The ``cmd`` destination always
    succeeds: it runs ``true`` and hands it the payload on stdin, on a
    thread of its own.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    written = write_for_plan(
        plan_dir, {"mode": mode, "timeout_sec": TIMEOUT_SEC}
    )
    assert written["mode"] == mode, (
        f"the plan's configuration was written as {written['mode']!r}; "
        f"this case is about {mode!r}"
    )
    assert written["timeout_sec"] == TIMEOUT_SEC
    written["hooks"] = [{"kind": "cmd", "command": "true"}]
    atomic_write_json(config_path(plan_dir), written, reraise=True)
    return plan_dir


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


def _decision_row(state_db: Path, decision_id: str) -> Optional[sqlite3.Row]:
    """The decision's row, read straight out of SQLite.

    The row is read with ``sqlite3`` rather than through the service,
    so that the assertion is about the database and not about the code
    that wrote it.
    """
    connection = sqlite3.connect(str(state_db))
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.execute(
            "SELECT * FROM plan_decisions WHERE decision_id = ?", (decision_id,)
        )
        return cursor.fetchone()
    finally:
        connection.close()


def _stored_answer(row: sqlite3.Row) -> Any:
    """The answer column of ``row``, decoded.

    The column holds a **JSON-encoded** answer — the store writes it
    that way, so a reader has to decode it to learn the answer.  This
    mirrors the product's own row reader: a column that is not valid
    JSON is returned unchanged, because the fact that something
    undecodable was stored is worth seeing rather than hiding.

    (The audit mirror is different: it carries the answer as it was
    given, not encoded.  That is why the two layers are read by two
    different helpers.)
    """
    raw = row["answer"]
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _wait_for_status(
    backend: _Backend,
    wanted: Dict[str, str],
    *,
    timeout: float = POLL_BOUND_SEC,
) -> Dict[str, Dict[str, Any]]:
    """Block until every plan's decision reaches the status it is after.

    ``wanted`` maps a plan id to the status that plan's question must
    reach.  The wait is a poll of the real HTTP surface: the cases
    observe the same thing an operator's UI would observe, and the
    bound is long enough to cover a tick that lands after the deadline.
    """
    deadline = time.monotonic() + timeout
    seen: Dict[str, Dict[str, Any]] = {}
    while time.monotonic() < deadline:
        seen = {}
        for plan_id in wanted:
            status, body = _get_json(
                backend.base_url, f"/api/hitl/{plan_id}/decisions"
            )
            assert status == 200, (
                f"listing the decisions of {plan_id} failed: {status} {body}"
            )
            decisions = json.loads(body)
            assert decisions, (
                f"the plan {plan_id} has no decision; the question was "
                "never asked"
            )
            seen[plan_id] = decisions[0]
        if all(
            seen[plan_id]["status"] == wanted[plan_id] for plan_id in wanted
        ):
            return seen
        time.sleep(POLL_INTERVAL_SEC)
    raise AssertionError(
        "the expiry was not noticed within "
        f"{timeout}s; wanted {wanted}, saw "
        + ", ".join(
            f"{plan_id}={decision['status']}"
            for plan_id, decision in seen.items()
        )
        + f"; server log tail:\n{_tail(backend.log_path)}"
    )


def _ask(base_url: str, plan_id: str) -> Dict[str, Any]:
    """Ask the plan's question through the real HTTP route."""
    status, body = _post_json(
        base_url, f"/api/hitl/{plan_id}/decisions", QUESTION_BODY
    )
    assert status == 200, f"the creation call failed: {status} {body}"
    return json.loads(body)


def _answer(
    base_url: str, plan_id: str, decision_id: str, option: str
) -> Tuple[int, Dict[str, Any]]:
    """Answer the question, and return ``(status, body)``."""
    status, body = _post_json(
        base_url,
        f"/api/hitl/{plan_id}/decisions/{decision_id}/answer",
        {"option": option, "answered_by": "e2e-operator"},
    )
    try:
        parsed = json.loads(body) if body else {}
    except ValueError:
        parsed = {"raw": body}
    return status, parsed


# ---------------------------------------------------------------------------
# 1 — an auto question nobody answered ends on its default
# ---------------------------------------------------------------------------


def test_auto_expires_to_default(tmp_path):
    """An overdue auto question falls back to its default — exactly once.

    The facts that have to hold together are that the question ends
    ``defaulted`` with the *stored default* as its answer, that nobody
    is recorded as having answered it, that a second answer is a
    conflict rather than an overwrite, and that the audit mirror says
    the same story in two lines and no more.
    """
    plan_id = AUTO_PLAN_ID
    _prepare_plan(tmp_path / "plans", plan_id, "auto")

    with _backend(tmp_path) as backend:
        decision = _ask(backend.base_url, plan_id)
        assert decision["status"] == "pending", (
            "a question somebody was asked is reported as "
            f"{decision['status']!r}; it is still waiting for a human"
        )
        assert decision["mode"] == "auto"
        assert decision["timeout_sec"] == TIMEOUT_SEC
        assert decision["deadline_at"], "the question carries no deadline"

        settled = _wait_for_status(backend, {plan_id: "defaulted"})[plan_id]

        # The response, the row and the file all have to agree.
        assert settled["status"] == "defaulted", (
            "an auto question nobody answered must end defaulted; it ended "
            f"{settled['status']!r}"
        )
        assert settled["answer"] == DEFAULT_OPTION, (
            "the default option must become the answer of a defaulted "
            f"question; the stored answer is {settled['answer']!r}"
        )
        assert settled["answered_by"] is None, (
            "nobody answered the question; the default must not be "
            "recorded as if a human had"
        )
        assert settled["answered_at"], "a defaulted question carries no timestamp"

        row = _decision_row(backend.state_db, decision["decision_id"])
        assert row is not None, "the decision was not written to the database"
        assert row["status"] == "defaulted"
        assert _stored_answer(row) == DEFAULT_OPTION, (
            "the row does not carry the default as the answer"
        )
        assert row["answered_by"] is None
        assert row["mode"] == "auto"

        # A second answer is a conflict, never an overwrite.
        status, body = _answer(
            backend.base_url, plan_id, decision["decision_id"], "reject"
        )
        assert status == 409, (
            "answering a settled question must be a conflict; the server "
            f"answered {status} {body}"
        )
        assert body.get("status") == "defaulted", (
            "the conflict must report the status the decision is actually "
            f"in; it reported {body!r}"
        )

        after = _decision_row(backend.state_db, decision["decision_id"])
        assert after is not None
        assert _stored_answer(after) == DEFAULT_OPTION, (
            "the refused answer overwrote the default; a conflict is not "
            "an overwrite"
        )

        # The audit mirror: the question, and the one defaulting.
        lines = _read_jsonl(
            backend.plans_dir / plan_id / DECISION_AUDIT_FILENAME
        )
        assert [line["event"] for line in lines] == ["created", "defaulted"], (
            "the audit mirror must hold the question and its defaulting, "
            f"and nothing else; it holds {[line['event'] for line in lines]!r}"
        )
        assert lines[1]["answer"] == DEFAULT_OPTION
        assert lines[1]["answered_by"] is None


# ---------------------------------------------------------------------------
# 2 — a strict question nobody answered stops the round
# ---------------------------------------------------------------------------


def test_strict_expires_to_paused(tmp_path):
    """An overdue strict question is paused — never answered.

    The default option of a strict question is exactly the answer the
    backend may not give in a human's place, so the case asserts that
    the answer stays empty, that the pause is written down with the
    reason and the question that caused it, and that a human who
    answers afterwards is still taken seriously.
    """
    plan_id = STRICT_PLAN_ID
    plans_dir = tmp_path / "plans"
    _prepare_plan(plans_dir, plan_id, "strict")

    # A round in flight: the state a backend that was killed mid-round
    # leaves behind, and the state the server's own recovery scan
    # rebuilds the watchdog's watch list from.
    with _backend(tmp_path, running_rounds=[plan_id]) as backend:
        decision = _ask(backend.base_url, plan_id)
        assert decision["status"] == "pending"
        assert decision["mode"] == "strict", (
            "the plan's configuration decides the mode; the question was "
            f"stored as {decision['mode']!r}"
        )
        assert decision["timeout_sec"] == TIMEOUT_SEC

        settled = _wait_for_status(backend, {plan_id: "paused"})[plan_id]

        assert settled["status"] == "paused", (
            "a strict question nobody answered must be paused; it ended "
            f"{settled['status']!r}"
        )
        assert settled["answer"] is None, (
            "a strict decision must never fall back to its default option; "
            f"the default is the one answer the backend is not allowed to "
            f"give, and the answer is {settled['answer']!r}"
        )
        assert settled["answered_by"] is None
        assert settled["answered_at"] is None, (
            "a paused question was not answered by anybody"
        )

        row = _decision_row(backend.state_db, decision["decision_id"])
        assert row is not None
        assert row["status"] == "paused"
        assert row["answer"] is None, (
            "the row carries an answer; a strict question nobody answered "
            "has none"
        )

        # The pause is written down, and it names the question that
        # caused it — the resume path finds the question through it.
        checkpoint_path = backend.plans_dir / plan_id / PAUSE_CHECKPOINT_FILENAME
        assert checkpoint_path.exists(), (
            "a strict timeout must leave a pause checkpoint; without one "
            "the resume path has nothing to pick the round up from"
        )
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert checkpoint["reason"] == "hitl_strict_timeout", (
            "the checkpoint does not say why the round stopped: "
            f"{checkpoint.get('reason')!r}"
        )
        assert checkpoint["kind"] == "verification"
        assert checkpoint["decision_id"] == decision["decision_id"], (
            "the checkpoint does not name the question that caused the "
            "pause; the resume path would have to guess"
        )

        # The audit mirror: the question, the pause — and no defaulting.
        lines = _read_jsonl(
            backend.plans_dir / plan_id / DECISION_AUDIT_FILENAME
        )
        assert [line["event"] for line in lines] == ["created", "paused"], (
            "the audit mirror must hold the question and its pause, and "
            f"nothing else; it holds {[line['event'] for line in lines]!r}"
        )
        assert lines[1]["note"] == "hitl_strict_timeout"
        assert lines[1]["answer"] is None

        # The pause is the end of the *waiting*, not of the question: a
        # human who answers afterwards must be taken seriously.
        status, body = _answer(
            backend.base_url, plan_id, decision["decision_id"], "accept"
        )
        assert status == 200, (
            "a paused question is still answerable; the server answered "
            f"{status} {body}"
        )
        assert body.get("status") == "answered"
        assert body.get("answer") == "accept"

        answered = _decision_row(backend.state_db, decision["decision_id"])
        assert answered is not None
        assert answered["status"] == "answered"
        assert _stored_answer(answered) == "accept"


# ---------------------------------------------------------------------------
# 3 — the two of them, side by side, in one process
# ---------------------------------------------------------------------------


def test_auto_vs_strict_pair(tmp_path):
    """The two modes end differently — asked together, asserted together.

    One mode passing is not evidence that the modes differ: a sweep
    that resolved every overdue question the same way would satisfy
    either case on its own.  So this case asks an ``auto`` question and
    a ``strict`` question in the same process, at the same time, and
    asserts both answers in the same body — the one place where "the
    modes are different" is actually a claim about the code.

    The wait is shared: the server notices both deadlines on the same
    tick, and the case polls both questions until each has reached the
    state its mode promises.
    """
    auto_id = PAIR_AUTO_PLAN_ID
    strict_id = PAIR_STRICT_PLAN_ID
    plans_dir = tmp_path / "plans"
    _prepare_plan(plans_dir, auto_id, "auto")
    _prepare_plan(plans_dir, strict_id, "strict")

    with _backend(tmp_path, running_rounds=[strict_id]) as backend:
        auto_decision = _ask(backend.base_url, auto_id)
        strict_decision = _ask(backend.base_url, strict_id)
        assert auto_decision["mode"] == "auto"
        assert strict_decision["mode"] == "strict"

        settled = _wait_for_status(
            backend, {auto_id: "defaulted", strict_id: "paused"}
        )

        # --- the auto half ---
        auto = settled[auto_id]
        assert auto["status"] == "defaulted", (
            "the auto question did not fall back to its default; it ended "
            f"{auto['status']!r}"
        )
        assert auto["answer"] == DEFAULT_OPTION, (
            "the auto question's answer is not its default option: "
            f"{auto['answer']!r}"
        )
        assert auto["answered_by"] is None

        status, body = _answer(
            backend.base_url, auto_id, auto_decision["decision_id"], "reject"
        )
        assert status == 409, (
            "a defaulted question must refuse a second answer; the server "
            f"answered {status} {body}"
        )

        # --- the strict half ---
        strict = settled[strict_id]
        assert strict["status"] == "paused", (
            "the strict question did not stop the round; it ended "
            f"{strict['status']!r}"
        )
        assert strict["answer"] is None, (
            "the strict question was answered by the backend; the default "
            f"option is the one answer it may never be given, and the "
            f"answer is {strict['answer']!r}"
        )

        checkpoint = json.loads(
            (
                backend.plans_dir / strict_id / PAUSE_CHECKPOINT_FILENAME
            ).read_text(encoding="utf-8")
        )
        assert checkpoint["reason"] == "hitl_strict_timeout"
        assert checkpoint["decision_id"] == strict_decision["decision_id"]

        status, body = _answer(
            backend.base_url, strict_id, strict_decision["decision_id"], "accept"
        )
        assert status == 200, (
            "a paused question is still answerable; the server answered "
            f"{status} {body}"
        )

        # --- the pair, side by side ---
        auto_row = _decision_row(backend.state_db, auto_decision["decision_id"])
        strict_row = _decision_row(
            backend.state_db, strict_decision["decision_id"]
        )
        assert auto_row is not None and strict_row is not None
        assert auto_row["status"] == "defaulted"
        assert strict_row["status"] == "answered"
        assert _stored_answer(auto_row) == DEFAULT_OPTION
        assert auto_row["answered_by"] is None, (
            "the auto question records an answerer; nobody answered it"
        )
        assert strict_row["answered_by"] == "e2e-operator", (
            "the strict question records no answerer; a human answered it"
        )


# ---------------------------------------------------------------------------
# 4 — the premise the three cases above rest on
# ---------------------------------------------------------------------------


def test_the_wait_outlives_a_tick_that_lands_after_the_deadline():
    """The bound the three cases wait for covers the tick that notices them.

    Nothing in the three cases above resolves the question.  The expiry
    is noticed by the server's own periodic tick — the heartbeat sweeps
    the overdue ``auto`` questions, and the verification loop pauses the
    overdue ``strict`` ones — and the tick may land anywhere in the
    interval *after* the deadline.  So the bound the cases wait for has
    to outlive a tick that lands after the deadline; a shorter wait
    would fail for a reason that has nothing to do with the expiry.

    The tick's length is read from the product
    (``server.HEARTBEAT_INTERVAL``) rather than restated here, so a
    tick that grew is caught here instead of arriving as a flake in the
    three cases.  The second assertion is this file's own premise: the
    deadline is deliberately shorter than the tick, so what the cases
    observe is a *later* tick's work and not the request's.
    """
    # ``server`` reaches into the ``backend`` package (``prompts`` imports
    # ``backend.framework.prompts``), so the repository root has to be
    # importable however pytest was invoked.
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    import server

    tick = server.HEARTBEAT_INTERVAL
    assert TIMEOUT_SEC < tick, (
        "this file's premise no longer holds against the product: the "
        f"deadline ({TIMEOUT_SEC}s) is not shorter than the tick ({tick}s), "
        "so the cases no longer show that a tick *after* the deadline did "
        "the work.  Shorten the question's timeout, or re-state the claim."
    )
    assert TIMEOUT_SEC + tick <= POLL_BOUND_SEC, (
        "the wait no longer covers a tick that lands after the deadline: "
        f"a {TIMEOUT_SEC}s deadline plus a {tick}s tick exceeds the "
        f"{POLL_BOUND_SEC}s bound.  The three cases would fail for a "
        "reason that has nothing to do with the expiry."
    )
