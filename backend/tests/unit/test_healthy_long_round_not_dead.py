"""A healthy long round is not dead (2026-11-02).

The negative control for the whole death machinery.

Everything the watchdog does when it declares a verification round
dead — the cleanup that kills the round's processes, the force
release of the plan's lease, the invalidation mark, the terminal
write that closes the plan — is only as good as its ability to tell
a DEAD round from a SLOW one.  A round that is working hard on one
long LLM call writes nothing to its own round log for a long time:
the whole round log can sit untouched for half an hour while the
dispatch it is waiting on is perfectly healthy.  Read that silence as
death and the watchdog kills a round that was about to succeed — the
round's query process dies, the plan is written off as ``failed``,
and the work it did is thrown away.

So the watchdog's evidence set includes ``execution.log``: the
``coding_query_progress`` line the dispatch writes once per interval
(2026-11-01) is a heartbeat from INSIDE the wait, and the round
log's silence stops meaning "dead" the moment it says so.

What this file pins, one test each:

* a long round whose round log is silent but whose ``execution.log``
  carries progress lines is NOT judged dead — no terminal write, no
  cleanup, the plan stays in verification;
* the round's own query process is NOT killed — it is still there
  after the watchdog has looked at it;
* the same round WITHOUT the progress lines IS judged dead once it
  is past the hard ceiling, and its query process is killed with it.
  Without this third test the first two would pass for the wrong
  reason: a watchdog that never killed anything would pass them too.

Hermeticity
------------
The plan directory and the state database live inside the test's own
tree.  The watchdog's two thresholds are **pinned** here rather than
read from the environment: ``server.py`` derives them from
``VERIFICATION_WATCHDOG_STALENESS_SECONDS`` /
``VERIFICATION_HARD_STALE_SECONDS`` (and a deployment's untracked
``.env`` can move them), and a threshold that moved out from under
this file would not fail it — it would quietly turn the "this round
looks stale" half of the setup into "this round was never stale at
all", which is the one thing a negative control must never be.

The only processes are the children this file spawns itself,
and they are spawned in their own session (``start_new_session=True``)
so that a group signal reaches them and only them — a child in the
*test's* group would take the test session down with it.  No test
sleeps: the silence of the round log is produced by back-dating its
mtime, and the liveness of the orchestrator's thread is produced by
a thread that waits on an event.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server as server_mod  # noqa: E402
import round_process_ledger  # noqa: E402
import verification_loop as vl  # noqa: E402


#: The plan the watchdog is watching.
PLAN_ID = "20261102-healthy-long-round"

#: The token of the round that is running in that plan.
ROUND_TOKEN = "verification_loop-1-healthy"

#: The event the dispatch writes into ``execution.log`` once per
#: interval while it waits (2026-11-01).
PROGRESS_EVENT = "coding_query_progress"

#: The watchdog's staleness threshold, PINNED (see the module docstring).
STALENESS_SECONDS = 900.0

#: The watchdog's hard ceiling, PINNED.  Past it, a round with no
#: evidence of life at all is dead.
HARD_CEILING_SECONDS = 3600.0

#: How long the round's own log has been silent.  Past the staleness
#: threshold and well short of the hard ceiling — the window in which a
#: busy round and a dead round look exactly the same unless something
#: else is read.
LOG_SILENCE_SECONDS = 2000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeTool:
    """A stand-in for the orchestrator's coding tool.

    The tool owns at most one subprocess, and the process is reachable
    only through the tool — which is exactly why the round-scoped
    ledger exists.
    """

    def __init__(self, process: Any) -> None:
        self._current_process = process
        self._process_lock = threading.Lock()


def _spawn_query_child() -> subprocess.Popen:
    """A real child process, in a session of its own.

    The child is the round's ``coding_query`` process: the thing that
    stays alive while the round waits, and the thing the cleanup has
    to kill when the round is judged dead.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _write_round_log(plan_dir: Path, age: float) -> Path:
    """A round log that stopped being written ``age`` seconds ago.

    One completed VP and nothing else: no VP is in flight, so the
    watchdog's triage has no "a VP is running" excuse to hold the
    round — what it has is the evidence set.
    """
    logs = plan_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / "verification_1_20261102_030000.log"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "verification_point_id": "VP-010",
            "event_type": "vp_complete",
            "timestamp": "2026-11-02T03:05:00",
        }, ensure_ascii=False) + "\n")
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def _write_progress_line(plan_dir: Path) -> Path:
    """Write one ``coding_query_progress`` line into ``execution.log``.

    A real line, written by the real logger — the watchdog reads the
    file's mtime, but a test that wrote a file the production code
    never writes would be testing a fiction.
    """
    from execution_logger import ExecutionLogger

    logger = ExecutionLogger(PLAN_ID, plans_dir=plan_dir.parent)
    logger.info(
        PROGRESS_EVENT,
        "coding_query_progress elapsed_sec=60 scene=verification",
        data={"elapsed_sec": 60, "scene": "verification", "prompt_chars": 154321},
    )
    return plan_dir / "execution.log"


def _last_execution_event(plan_dir: Path) -> Dict[str, Any]:
    """The last event written into the plan's ``execution.log``."""
    path = plan_dir / "execution.log"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert lines, (
        f"the progress line was not written into {path} — the file the "
        "watchdog reads does not exist, so the round below would be "
        "judged on the round log alone"
    )
    return json.loads(lines[-1])


def _seed_running_verification(db_path: Path, plan_id: str) -> None:
    """Seed ``plan_routing`` + ``plan_verification`` mid-verification."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(str(db_path))
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "verification_running")
        VerificationRepository(conn).init_round(plan_id, round_n=1, max_rounds=3)
    finally:
        conn.close()


def _phase(db_path: Path, plan_id: str) -> Any:
    """The plan's routing phase, as the state machine sees it."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _stale_verdicts(caplog) -> List[str]:
    """The watchdog's staleness verdicts, as logged."""
    return [
        record.getMessage() for record in caplog.records
        if "verification_log_stale" in record.getMessage()
    ]


@pytest.fixture
def live_thread():
    """A thread that is alive for the whole test and stops with it.

    The orchestrator's own thread: the watchdog reads
    ``thread.is_alive()`` first, and a thread that has already
    finished would be a round dead for a reason that has nothing to
    do with the evidence under test.  It waits on an event rather
    than sleeping, so no test waits on the clock.
    """
    stop = threading.Event()
    thread = threading.Thread(
        target=lambda: stop.wait(60), name="pdt-fake-orchestrator", daemon=True,
    )
    thread.start()
    yield thread
    stop.set()
    thread.join(timeout=5)


@pytest.fixture
def children():
    """The children this test spawned, killed again when it is over."""
    spawned: List[subprocess.Popen] = []
    yield spawned
    for proc in spawned:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # pragma: no cover - defensive
                pass
        try:
            proc.wait(timeout=5)
        except Exception:  # pragma: no cover - defensive
            pass


@pytest.fixture
def long_round(tmp_path, monkeypatch, live_thread, children):
    """A plan whose verification round is running — and looks healthy.

    The plan directory and the state database live inside the test's
    own tree.  The HITL sweep is stubbed out: it is orthogonal to the
    watchdog's evidence set, and it would otherwise reach for the
    state database of the machine the test runs on.
    """
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(server_mod, "PLANS_DIR", plans_root)
    monkeypatch.setattr(
        server_mod, "_state_db_path", lambda *a, **k: str(db_path),
    )
    # The thresholds are pinned: this file's whole argument is about
    # the gap between them, and a deployment's ``.env`` is allowed to
    # move them.
    monkeypatch.setattr(
        server_mod, "VERIFICATION_WATCHDOG_STALENESS_SECONDS", STALENESS_SECONDS,
    )
    monkeypatch.setattr(
        server_mod, "VERIFICATION_HARD_STALE_SECONDS", HARD_CEILING_SECONDS,
    )
    plan_dir = plans_root / PLAN_ID
    plan_dir.mkdir()
    _seed_running_verification(db_path, PLAN_ID)

    monkeypatch.setattr(vl, "_expire_hitl_decisions", lambda now: 0)
    monkeypatch.setattr(
        vl, "_handle_hitl_strict_timeout", lambda plan_id, now: None,
    )
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN_ID, None)
    round_process_ledger.reset_for_tests()

    server_mod._verification_state[PLAN_ID] = {
        "verification_status": "running",
        "verification_round": 1,
        "round_token": ROUND_TOKEN,
        "thread": live_thread,
        "started_at": "2026-11-02T03:00:00",
        # Well past the startup grace period, so the staleness ladder
        # is allowed to fire at all.
        "started_at_ts": time.time() - 7200.0,
    }

    yield {
        "plan_dir": plan_dir,
        "db_path": db_path,
        "plans_root": plans_root,
        "children": children,
    }

    server_mod._verification_state.pop(PLAN_ID, None)
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN_ID, None)
    round_process_ledger.reset_for_tests()


def _register_round_query_process(
    plan_dir: Path, children: List[subprocess.Popen],
) -> subprocess.Popen:
    """Give the running round a query process, the way the loop does.

    The round is opened in the ledger, the tool that owns the process
    is registered into it, and the child is handed back so the test
    can ask whether it is still there afterwards.
    """
    proc = _spawn_query_child()
    children.append(proc)
    round_process_ledger.begin_round(plan_dir, ROUND_TOKEN)
    round_process_ledger.register_tool(plan_dir, _FakeTool(proc))
    return proc


def _run_watchdog() -> None:
    """Drive the production watchdog entry point."""
    server_mod._lazy_check_verification(PLAN_ID)


# ---------------------------------------------------------------------------
# 1. A progressing long round is not dead
# ---------------------------------------------------------------------------


def test_a_progressing_long_round_is_not_judged_dead(long_round, caplog):
    """The round log is silent, but the round is not dead.

    The round's own log has been quiet for 2000 s — past the 900 s
    staleness threshold, so on that evidence alone it looks like a
    round that stopped making progress.  But the dispatch it is
    waiting on writes a ``coding_query_progress`` line into
    ``execution.log`` once a minute, and that line says the round is
    very much alive.  The watchdog reads the whole evidence set, so
    the silence of one file does not condemn it.

    What must NOT happen: a terminal write (the plan is not closed),
    a cleanup (the round is not killed), and a staleness verdict (the
    ladder did not fire — the progress line is what answered it).
    """
    plan_dir = long_round["plan_dir"]

    # The precondition, asserted rather than assumed: this round's
    # silence alone WOULD have condemned it, and the window it sits in
    # is the one the whole file is about.
    assert STALENESS_SECONDS < LOG_SILENCE_SECONDS < HARD_CEILING_SECONDS, (
        "the round log's silence must be past the staleness threshold and "
        f"short of the hard ceiling: threshold={STALENESS_SECONDS} "
        f"silence={LOG_SILENCE_SECONDS} ceiling={HARD_CEILING_SECONDS}"
    )

    _write_round_log(plan_dir, age=LOG_SILENCE_SECONDS)
    _write_progress_line(plan_dir)

    # The heartbeat really is the event the dispatch writes, in the
    # file the watchdog reads.
    event = _last_execution_event(plan_dir)
    assert event.get("event") == PROGRESS_EVENT, (
        f"the heartbeat must be the progress event: {event!r}"
    )

    with caplog.at_level(logging.WARNING, logger="server"):
        with patch.object(
            server_mod, "_persist_verification_terminal",
            wraps=server_mod._persist_verification_terminal,
        ) as persist, patch.object(
            server_mod, "_mark_verification_failed_dead",
        ) as mark_dead:
            _run_watchdog()

    persist.assert_not_called(), (
        "a round that writes progress lines must not be written off — "
        f"the watchdog called the terminal write: {persist.call_args_list!r}"
    )
    mark_dead.assert_not_called(), (
        "a healthy round must not be marked dead in memory"
    )
    assert _phase(long_round["db_path"], PLAN_ID) == "verification_running", (
        "the plan must stay in verification — a healthy round must not "
        "move its plan to failed"
    )
    stale_verdicts = _stale_verdicts(caplog)
    assert not stale_verdicts, (
        "the staleness ladder must not fire on a round that is writing "
        f"progress: {stale_verdicts!r}"
    )


# ---------------------------------------------------------------------------
# 2. The round's query process is not killed
# ---------------------------------------------------------------------------


def test_its_query_process_is_not_killed(long_round, caplog):
    """The round's own query process survives the watchdog's look.

    The process is the round's ``coding_query`` child — the LLM call
    the round is waiting on.  It is reachable only through the tool
    that owns it, which is why the round-scoped ledger exists: when a
    round IS judged dead, this is the process that has to die with it
    (2026-11-01, the 1h46m incident).  When the round is healthy,
    this is the process that has to keep running.

    The cleanup is spied on with ``wraps``, so the assertion is about
    the real function: it was never entered.  And the process itself
    is asked directly, because a process that is still there is the
    only proof that matters.

    The round must not merely survive the kill — it must not be
    JUDGED dead either, so the watchdog's verdict is checked here as
    well: a round that was written off and then spared the kill would
    still be a round the plan had given up on.
    """
    plan_dir = long_round["plan_dir"]
    _write_round_log(plan_dir, age=LOG_SILENCE_SECONDS)
    _write_progress_line(plan_dir)
    proc = _register_round_query_process(plan_dir, long_round["children"])

    with caplog.at_level(logging.WARNING, logger="server"):
        with patch.object(
            server_mod, "_persist_verification_terminal",
            wraps=server_mod._persist_verification_terminal,
        ) as persist, patch.object(
            server_mod, "_cleanup_dead_verification_processes",
            wraps=server_mod._cleanup_dead_verification_processes,
        ) as cleanup:
            _run_watchdog()

    persist.assert_not_called(), (
        "a healthy round must not be written off — the watchdog called "
        f"the terminal write: {persist.call_args_list!r}"
    )
    cleanup.assert_not_called(), (
        "a healthy round must not be cleaned up — the watchdog called "
        f"the cleanup: {cleanup.call_args_list!r}"
    )
    stale_verdicts = _stale_verdicts(caplog)
    assert not stale_verdicts, (
        "a healthy round must not be judged stale — the ladder fired: "
        f"{stale_verdicts!r}"
    )
    assert _phase(long_round["db_path"], PLAN_ID) == "verification_running", (
        "the plan must stay in verification — a healthy round must not "
        "move its plan to failed"
    )
    assert proc.poll() is None, (
        "the round's query process must still be running — it was killed "
        f"(exit code {proc.poll()!r})"
    )
    os.kill(proc.pid, 0)  # raises ProcessLookupError only if it is gone
    assert round_process_ledger.tools_for_round(plan_dir, ROUND_TOKEN), (
        "the round must still own its tool — a killed round is not a "
        "round that is merely quiet"
    )


# ---------------------------------------------------------------------------
# 3. The control group: the same round without progress IS dead
# ---------------------------------------------------------------------------


def test_the_same_round_without_progress_is_judged_dead(long_round):
    """Without the progress lines, the same round dies.

    This is the control that gives the two tests above their meaning.
    A watchdog that never killed anything would pass them; this test
    fails for it.  The round here is identical in every respect —
    same plan, same round token, same live thread, same silent round
    log — except that nothing writes to ``execution.log``, and its
    silence has run past the hard ceiling.

    The verdict must be written, the plan must be closed, the cleanup
    must have run, and the round's own query process must be killed
    with it.
    """
    plan_dir = long_round["plan_dir"]
    silence = HARD_CEILING_SECONDS + 100.0
    assert silence >= HARD_CEILING_SECONDS, (
        "the control round's silence must have passed the hard ceiling: "
        f"silence={silence} ceiling={HARD_CEILING_SECONDS}"
    )
    _write_round_log(plan_dir, age=silence)
    # No execution.log at all: this round writes no progress line.
    proc = _register_round_query_process(plan_dir, long_round["children"])

    with patch.object(
        server_mod, "_persist_verification_terminal",
        wraps=server_mod._persist_verification_terminal,
    ) as persist, patch.object(
        server_mod, "_cleanup_dead_verification_processes",
        wraps=server_mod._cleanup_dead_verification_processes,
    ) as cleanup:
        _run_watchdog()

    persist.assert_called_once(), (
        "a round that is silent past the hard ceiling and writes no "
        "progress must be written off"
    )
    args, kwargs = persist.call_args
    assert args[1] == "failed", (
        f"the verdict must be a failure: {args!r}"
    )
    assert args[2] == "verification_log_stale", (
        f"the verdict must name the reason: {args!r}"
    )
    assert kwargs.get("round_token") is None, (
        "the watchdog names no round — its own verdict may not be "
        f"refused: {kwargs!r}"
    )
    cleanup.assert_called_once(), (
        "a dead round must be cleaned up — the cleanup never ran: "
        f"{cleanup.call_args_list!r}"
    )
    assert _phase(long_round["db_path"], PLAN_ID) == "failed", (
        "the plan must learn that its round died"
    )
    assert proc.poll() is not None, (
        "the round's query process must be killed with the round — it is "
        f"still running (exit code {proc.poll()!r})"
    )
