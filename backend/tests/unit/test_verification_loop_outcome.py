"""The auto-verification loop has two kinds of exit, and conflating them
stamped a running plan ``completed`` (2026-09-23).

Live incident
-------------
Round 1 of the verification loop had just finished failing and generated
repair tasks; the loop dispatched ``_run_repair_execution_async`` and
returned.
Three lines further up the stack decided what that return meant::

    _run_auto_verification_loop(...)          # returned the instant the
                                              # repair subprocess spawned
    ps = PlanState(plan_dir)
    if ps.get_current_phase() == "executing":
        ps.transition_to("completed")         # ← fired here

``_run_repair_execution_async`` sets ``executing`` microseconds before
that read, so the guard saw exactly what it was looking for and promoted
the plan to ``completed`` while ``repair-r1-01`` was visibly running on
the same Feishu card. The card's terminal branch then chose:

    ⏸ 暂停（上游阻塞） · a production plan

with a ``⏳1`` in the body — the operator's report was "0921的这个plan
也暂停了".

The same confusion hit ``_reap_managed_services`` in the wrapper's
``finally``: it tore down the plan's dev servers because "the loop
returned", which for this exit means "the loop handed off", not "the
workflow ended".

Why the existing tests missed it
--------------------------------
The loop's docstring counted "a dozen ``return`` paths" and every caller
assumed all of them were terminal. That was true when
``_run_repair_execution`` blocked inline; the 2026-09-11 v14 refactor
made the dispatch async and no caller was revisited. Nothing in the
suite drove the loop to a repair dispatch and then asked what the caller
concluded — ``test_routing_stage_partial_completion`` re-implements the
CAS it claims to test, which proves the statement works but never that
it is reachable from here.

These tests drive the real decisions
------------------------------------
``_settle_phase_after_verification_loop`` was extracted from the two
call sites so the distinction is one function against a real
``plan_routing`` row, and ``_plan_execution_in_flight`` is exercised
against the record ``_run_repair_execution_async`` actually writes.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fresh_state_db(tmp_path: Path, monkeypatch) -> Path:
    """A hermetic ``state.db`` with ``server._state_db_path`` patched to it."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    import server

    db = tmp_path / "state.db"
    conn = open_db(db)
    migrate(conn)
    conn.close()
    monkeypatch.setattr(server, "_state_db_path", lambda: db)
    return db


def _seed_routing(db: Path, plan_id: str, phase: str) -> None:
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, version, completed_phases, review_rounds, "
            " flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan_id, phase, 0, "[]", "{}", "{}", "null",
             "2026-09-23T00:00:00", "2026-09-23T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def _read_phase(db: Path, plan_id: str):
    from state_machine.db.connection import open as open_db

    conn = open_db(db)
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


@pytest.fixture
def clean_execution_state(monkeypatch):
    """Undo any ``_execution_state`` rows a test seeds."""
    import server

    before = dict(server._execution_state)
    yield
    with server._execution_state_lock:
        server._execution_state.clear()
        server._execution_state.update(before)


# ---------------------------------------------------------------------------
# _VerificationLoopOutcome
# ---------------------------------------------------------------------------


def test_a_fresh_outcome_is_terminal():
    from server import _VerificationLoopOutcome

    assert _VerificationLoopOutcome().terminal is True


def test_a_handed_off_outcome_is_not_terminal():
    from server import _VerificationLoopOutcome

    outcome = _VerificationLoopOutcome()
    outcome.handed_off = True
    assert outcome.terminal is False


# ---------------------------------------------------------------------------
# The wrapper's finally: reap, or defer
# ---------------------------------------------------------------------------


def _drive_wrapper(tmp_path, monkeypatch, *, handed_off: bool):
    """Run the real wrapper over a stub loop body; return (calls, outcome)."""
    import server

    reap_calls: list = []
    monkeypatch.setattr(
        server, "_reap_managed_services",
        lambda plan_id, plan_dir, reason: reap_calls.append(reason),
    )

    def fake_inner(plan_id, plan_dir, project_dir, **kwargs):
        kwargs["outcome"].handed_off = handed_off
        return None

    monkeypatch.setattr(server, "_run_auto_verification_loop_inner", fake_inner)
    outcome = server._run_auto_verification_loop("plan-x", tmp_path, tmp_path)
    return reap_calls, outcome


def test_wrapper_reaps_when_the_loop_ended(tmp_path, monkeypatch):
    """A terminal exit still reaps — the anti-vacuity control."""
    reap_calls, outcome = _drive_wrapper(tmp_path, monkeypatch, handed_off=False)

    assert outcome.terminal is True
    assert reap_calls == ["verification_loop_exit"], (
        "a loop that reached a verdict must still release the plan's services"
    )


def test_wrapper_defers_the_reap_when_a_repair_execution_is_in_flight(
    tmp_path, monkeypatch,
):
    """The reap targeted services the repair round still needed."""
    reap_calls, outcome = _drive_wrapper(tmp_path, monkeypatch, handed_off=True)

    assert outcome.terminal is False
    assert reap_calls == [], (
        "the plan's dev servers were reaped while its repair execution was "
        "still running; the reap belongs to whoever finishes the chain"
    )


# ---------------------------------------------------------------------------
# The callers' three lines: promote to ``completed``, or not
# ---------------------------------------------------------------------------


def test_a_handoff_does_not_stamp_the_plan_completed(tmp_path, monkeypatch):
    """The exact defect, at the exact decision that produced it.

    Pre-fix this read ``assert phase == "completed"``: the loop had
    returned, the phase was ``executing`` (because the dispatcher had
    just written it), so the fallback fired on a plan that was starting
    round 1 of repairs.
    """
    import server

    # A realistic plan id: the system only ever produces
    # ``derive_plan_id`` output ([A-Za-z0-9._-]), and server.py rejects
    # anything else before it touches ``plans/{plan_id}``.
    plan_id = "20260101-example-plan"
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir()
    db = _fresh_state_db(tmp_path, monkeypatch)
    _seed_routing(db, plan_id, "executing")

    outcome = server._VerificationLoopOutcome()
    outcome.handed_off = True
    server._settle_phase_after_verification_loop(plan_dir, outcome)

    assert _read_phase(db, plan_id) == "executing", (
        "a plan whose repair execution has just been dispatched must stay "
        "``executing`` — ``completed`` here is what made the Feishu card "
        "read '⏸ 暂停（上游阻塞）' while repair-r1-01 was on the same card"
    )


def test_a_terminal_loop_still_settles_an_executing_plan(tmp_path, monkeypatch):
    """The fallback itself must survive — it covers a mocked/skipped loop."""
    import server

    plan_id = "plan-terminal"
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir()
    db = _fresh_state_db(tmp_path, monkeypatch)
    _seed_routing(db, plan_id, "executing")

    server._settle_phase_after_verification_loop(
        plan_dir, server._VerificationLoopOutcome(),
    )

    assert _read_phase(db, plan_id) == "completed"


def test_settle_tolerates_a_stubbed_loop_returning_none(tmp_path, monkeypatch):
    """A replaced/stubbed loop returns no outcome; that must not crash.

    Several suites monkeypatch ``_run_auto_verification_loop`` with a
    bare ``lambda *a, **k: None``. The first cut of this guard assumed a
    real outcome and raised ``AttributeError: 'NoneType'`` out of the
    executor watcher, which the watcher logs as
    ``executor_watcher_crashed`` — a strictly worse failure than the
    stale phase it was fixing.
    """
    import server

    plan_id = "plan-stubbed-loop"
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir()
    db = _fresh_state_db(tmp_path, monkeypatch)
    _seed_routing(db, plan_id, "executing")

    server._settle_phase_after_verification_loop(plan_dir, None)

    assert _read_phase(db, plan_id) == "completed"


def test_settle_leaves_a_terminal_verdict_alone(tmp_path, monkeypatch):
    """A failed round must not be rewritten to ``completed``."""
    import server

    plan_id = "plan-verdict-failed"
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir()
    db = _fresh_state_db(tmp_path, monkeypatch)
    _seed_routing(db, plan_id, "failed")

    server._settle_phase_after_verification_loop(
        plan_dir, server._VerificationLoopOutcome(),
    )

    assert _read_phase(db, plan_id) == "failed"


# ---------------------------------------------------------------------------
# _plan_execution_in_flight, against the record the dispatcher writes
# ---------------------------------------------------------------------------


class _FakeProcess:
    """``process.wait()`` blocks until ``release()``, like a real executor."""

    def __init__(self, returncode: int = 0):
        self.pid = 987654
        self.returncode = returncode
        self._done = threading.Event()

    def release(self) -> None:
        self._done.set()

    def wait(self, timeout=None):
        self._done.wait(timeout=timeout or 10)
        return self.returncode

    def poll(self):
        return self.returncode if self._done.is_set() else None


def test_a_dispatched_repair_execution_reads_as_in_flight(
    tmp_path, monkeypatch, clean_execution_state,
):
    """``_run_repair_execution_async`` is what ``outcome.handed_off`` reads.

    The two dispatch sites call ``_plan_execution_in_flight(plan_id)``
    immediately after the dispatch. This drives a real dispatch (with the
    subprocess spawn stubbed) and pins both ends of that predicate.
    """
    import server

    _fresh_state_db(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path)
    plan_id = "plan-repair-dispatch"
    (tmp_path / plan_id).mkdir()

    proc = _FakeProcess()
    monkeypatch.setattr(
        server, "_spawn_executor_subprocess", lambda **kwargs: (proc, None),
    )

    result = server._run_repair_execution_async(plan_id, tmp_path)

    assert result["status"] == "started"
    assert server._plan_execution_in_flight(plan_id) is True, (
        "the dispatcher seeds the record as running, so the loop's return "
        "is a hand-off — reading it as terminal is the defect under test"
    )

    # The watcher thread clears the record once the subprocess exits, so a
    # repair round that has finished no longer blocks the reap.
    proc.release()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not server._plan_execution_in_flight(plan_id):
            break
        time.sleep(0.02)

    assert server._plan_execution_in_flight(plan_id) is False, (
        "a finished repair execution must stop reading as in flight, or "
        "every later terminal exit would defer its reap forever"
    )


def test_an_idle_plan_is_not_in_flight(clean_execution_state):
    import server

    assert server._plan_execution_in_flight("never-dispatched") is False
