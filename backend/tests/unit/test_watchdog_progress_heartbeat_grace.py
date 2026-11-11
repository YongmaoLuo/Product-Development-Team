"""2026-11-11: a progress heartbeat may not keep a wedged round alive forever.

The 2026-11-09 ``service-readiness-2`` incident: the round was stuck for
48274 s and the watchdog never said a word about it.  The evidence the
watchdog read — ``execution.log``'s mtime — was refreshed every 60 s by
the dispatch's progress heartbeat (``coding_query_progress``), so the
900 s staleness threshold could not fire; and because the threshold
never fired, the 3600 s hard ceiling never fired either.  A round that
had stopped making progress for thirteen hours looked exactly like a
round that was working.

The rule this file pins: **a progress heartbeat counts as liveness
evidence only while it is inside the hard ceiling** — measured from the
round's last REAL event, or from the round's own start when there is
none.  Past that ceiling the heartbeat stops renewing the round's life,
and the staleness ladder fires as it should.

One test each:

* a round whose only evidence is a progress heartbeat and which is
  INSIDE the hard ceiling is not stale — the heartbeat still works;
* the same round PAST the hard ceiling IS stale — the heartbeat has
  stopped renewing the round's life;
* a round whose ``execution.log`` also carries a real event (a
  quota-window probe) is not stale;
* a round whose own round log is fresh is not stale — the discount
  never condemns a round that is plainly writing.

Hermeticity
------------
The plan directory and the state live inside the test's own tree.  The
watchdog's two thresholds are **pinned** rather than read from the
environment: ``server.py`` derives them from
``VERIFICATION_WATCHDOG_STALENESS_SECONDS`` /
``VERIFICATION_HARD_STALE_SECONDS`` (and a deployment's untracked
``.env`` can move them), and a threshold that moved out from under this
file would not fail it — it would quietly turn "this round looks
stale" into "this round was never stale at all", which is the one thing
a negative control must never be.

No test sleeps: the silence is produced by back-dating mtimes, and the
heartbeats are written by the real ``ExecutionLogger``, so the file the
watchdog reads is the file production writes.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock, patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server as server_mod  # noqa: E402
import verification_loop as vl  # noqa: E402
from execution_logger import ExecutionLogger  # noqa: E402


#: The plan the watchdog is watching.
PLAN = "plan-progress-heartbeat-grace"

#: The event the dispatch writes into ``execution.log`` once per interval
#: while it waits on a provider (2026-11-01).  A heartbeat, not progress.
PROGRESS_EVENT = "coding_query_progress"

#: A REAL event: the quota-window probe the dispatch writes while it is
#: parked on a closed quota window.
QUOTA_WAIT_EVENT = "provider_quota_window_wait"

#: The watchdog's staleness threshold, PINNED (see the module docstring).
STALENESS_SECONDS = 900.0

#: The watchdog's hard ceiling, PINNED.  A heartbeat may renew a round's
#: life for at most this long past the round's last real event.
HARD_CEILING_SECONDS = 3600.0

#: How long the round's own log has been silent — past the staleness
#: threshold, so the round log alone would condemn the round.
LOG_SILENCE_SECONDS = 4200.0

#: How long the round has been running, when it has only a heartbeat to
#: show for it: past the hard ceiling, so the heartbeat may not renew
#: its life any more.
ROUND_AGE_PAST_CAP = 48000.0

#: The same, but inside the hard ceiling: the heartbeat still renews the
#: round's life.
ROUND_AGE_WITHIN_CAP = 2000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_round_log(plan_dir: Path, age: float) -> Path:
    """A round log that stopped being written ``age`` seconds ago.

    One completed VP and nothing else: no VP is in flight, so the
    watchdog's triage has no "a VP is running" excuse to hold the round
    — what it has is the evidence set.
    """
    logs = plan_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / "verification_1_20261111_000000.log"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "verification_point_id": "VP-010",
            "event_type": "vp_complete",
            "timestamp": "2026-11-11T00:05:00",
        }, ensure_ascii=False) + "\n")
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def _write_execution_log(plan_dir: Path, events: List[str]) -> Path:
    """Write ``events`` into the plan's ``execution.log``, in that order.

    The real logger writes the lines — the watchdog reads the file
    production writes, and a fixture that invented its own format would
    be testing a fiction.  Each line carries the logger's own ``ts``.
    """
    logger = ExecutionLogger(PLAN, plans_dir=plan_dir.parent)
    for event in events:
        logger.info(
            event,
            f"{event} while the verification round waits",
            data={"scene": "verification"},
        )
    return plan_dir / "execution.log"


def _alive_thread() -> Mock:
    """The orchestrator's thread, alive.

    The watchdog reads ``thread.is_alive()`` first: a thread that has
    already finished is a round dead for a reason that has nothing to do
    with the evidence under test.
    """
    thread = Mock()
    thread.is_alive.return_value = True
    return thread


def _install_running_state(round_age: float) -> None:
    """Put the plan into the state the watchdog is watching."""
    server_mod._verification_state[PLAN] = {
        "verification_status": "running",
        "verification_round": 1,
        "thread": _alive_thread(),
        "started_at": "2026-11-11T00:00:00",
        "started_at_ts": time.time() - round_age,
    }


def _run_lazy_check() -> Mock:
    """Drive the REAL watchdog entry point, terminal write observable.

    ``_lazy_check_verification`` is what the server heartbeat calls once
    per running plan — this is the production decision, not an internal
    helper.
    """
    with patch.object(server_mod, "_persist_verification_terminal") as persist, \
         patch.object(server_mod, "_mark_verification_failed_dead"), \
         patch.object(server_mod, "_update_plan_state_to_terminal"), \
         patch.object(server_mod, "_kill_orphaned_vp_processes",
                      return_value=[]), \
         patch.object(server_mod, "_kill_sub_agent_process"), \
         patch.object(server_mod, "_cleanup_dead_verification_processes"):
        server_mod._lazy_check_verification(PLAN)
    return persist


def _stale_verdicts(caplog) -> List[str]:
    """The watchdog's staleness verdicts, as logged."""
    return [
        record.getMessage() for record in caplog.records
        if "verification_log_stale" in record.getMessage()
    ]


@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    """A running verification whose plan dir holds only what the test writes."""
    plans_root = tmp_path / "plans"
    plan_dir = plans_root / PLAN
    plan_dir.mkdir(parents=True)
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(server_mod, "PLANS_DIR", plans_root)
    monkeypatch.setattr(server_mod, "_VP_TMP_ROOT", tmp_root)
    # The liveness probe shells out to ``pgrep``; default it to "no
    # matching process" so the no-VP-in-flight branch is deterministic.
    monkeypatch.setattr(
        server_mod, "_vp_artifact_processes", lambda vp_id: []
    )
    monkeypatch.setattr(
        server_mod, "VERIFICATION_WATCHDOG_STALENESS_SECONDS", STALENESS_SECONDS,
    )
    monkeypatch.setattr(
        server_mod, "VERIFICATION_HARD_STALE_SECONDS", HARD_CEILING_SECONDS,
    )
    monkeypatch.setattr(vl, "_expire_hitl_decisions", lambda now: 0)
    monkeypatch.setattr(vl, "_handle_hitl_strict_timeout", lambda plan_id, now: None)
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN, None)
    yield plan_dir
    server_mod._verification_state.pop(PLAN, None)
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN, None)


# ---------------------------------------------------------------------------
# 1. Inside the ceiling the heartbeat still works
# ---------------------------------------------------------------------------


def test_progress_only_within_cap_is_not_stale(plan_env, caplog):
    """A heartbeat inside the hard ceiling is still liveness evidence.

    The round's own log has been silent for 70 min — past the staleness
    threshold, so on that evidence alone the round looks dead.  But the
    dispatch is heart-beating and the round is only 33 min old, which is
    inside the hard ceiling: the heartbeat is allowed to renew the
    round's life, and the round must NOT be condemned.

    The assertion is on the VERDICT, not only on the terminal write: the
    watchdog's triage would hold a round that the ladder condemned but
    that is still inside the hard ceiling, so "the plan was not closed"
    alone would pass for the wrong reason.
    """
    _write_round_log(plan_env, age=LOG_SILENCE_SECONDS)
    _write_execution_log(plan_env, [PROGRESS_EVENT])
    _install_running_state(round_age=ROUND_AGE_WITHIN_CAP)

    with caplog.at_level(logging.WARNING, logger="server"):
        persist = _run_lazy_check()

    stale_verdicts = _stale_verdicts(caplog)
    assert not stale_verdicts, (
        "a round inside the hard ceiling must not be judged stale — the "
        f"ladder fired: {stale_verdicts!r}"
    )
    persist.assert_not_called(), (
        "a round that is heart-beating inside the hard ceiling must not be "
        f"written off: {persist.call_args_list!r}"
    )


# ---------------------------------------------------------------------------
# 2. Past the ceiling the heartbeat stops renewing the round's life
# ---------------------------------------------------------------------------


def test_progress_only_past_cap_is_stale(plan_env, caplog):
    """The 2026-11-09 incident, as a test.

    The round has been running for 13 hours and the only thing it has
    written is the heartbeat the dispatch writes once a minute.  The
    round's own log fell silent 70 min ago — past the staleness
    threshold AND past the hard ceiling, so there is nothing left to
    hold the round: it is dead, and it must be written off.

    Without the ceiling on the heartbeat this round is alive forever:
    ``execution.log``'s mtime is refreshed every 60 s, so the threshold
    never fires and the ceiling above it never fires either.
    """
    _write_round_log(plan_env, age=LOG_SILENCE_SECONDS)
    _write_execution_log(plan_env, [PROGRESS_EVENT])
    _install_running_state(round_age=ROUND_AGE_PAST_CAP)

    with caplog.at_level(logging.WARNING, logger="server"):
        persist = _run_lazy_check()

    persist.assert_called_once(), (
        "a round that has only been heart-beating for 13 hours must be "
        f"written off: {persist.call_args_list!r}"
    )
    assert persist.call_args[0][2] == "verification_log_stale", (
        "the round is stale, not merely quiet — the stop reason names the "
        f"wrong thing: {persist.call_args[0][2]!r}"
    )


# ---------------------------------------------------------------------------
# 3. A real event still counts
# ---------------------------------------------------------------------------


def test_progress_plus_quota_wait_probe_is_not_stale(plan_env, caplog):
    """A round whose log carries a REAL event is not stale.

    The dispatch parked on a closed quota window and wrote a probe line
    (a real event, not a heartbeat); the heartbeat kept going while it
    waited.  The real event is the anchor the heartbeat is measured
    against, and it is recent — so the heartbeat is inside the ceiling
    and the round is alive.

    This is the 2026-10-14 incident's shape, and it must keep working:
    the fix is about heartbeats, not about the quota wait.
    """
    _write_round_log(plan_env, age=LOG_SILENCE_SECONDS)
    _write_execution_log(plan_env, [QUOTA_WAIT_EVENT, PROGRESS_EVENT])
    _install_running_state(round_age=ROUND_AGE_PAST_CAP)

    with caplog.at_level(logging.WARNING, logger="server"):
        persist = _run_lazy_check()

    stale_verdicts = _stale_verdicts(caplog)
    assert not stale_verdicts, (
        "a round that wrote a real event must not be judged stale — the "
        f"ladder fired: {stale_verdicts!r}"
    )
    persist.assert_not_called(), (
        "a round parked on a quota window writes real events; it must not "
        f"be written off: {persist.call_args_list!r}"
    )


# ---------------------------------------------------------------------------
# 4. A fresh round log is never condemned
# ---------------------------------------------------------------------------


def test_fresh_round_log_is_not_stale(plan_env, caplog):
    """The round's own log is fresh — the round is plainly writing.

    Nothing here may condemn it, however old the round is and whatever
    its heartbeat looks like: the round log is the strongest evidence
    there is, and the heartbeat's ceiling is only ever a discount on the
    heartbeat, never a verdict of its own.
    """
    _write_round_log(plan_env, age=10.0)
    _write_execution_log(plan_env, [PROGRESS_EVENT])
    _install_running_state(round_age=ROUND_AGE_PAST_CAP)

    with caplog.at_level(logging.WARNING, logger="server"):
        persist = _run_lazy_check()

    stale_verdicts = _stale_verdicts(caplog)
    assert not stale_verdicts, (
        "a round whose own log is fresh must not be judged stale — the "
        f"ladder fired: {stale_verdicts!r}"
    )
    persist.assert_not_called(), (
        "a round that is writing its own log must not be written off: "
        f"{persist.call_args_list!r}"
    )


# ---------------------------------------------------------------------------
# The interface the watchdog reads
# ---------------------------------------------------------------------------


def test_execution_log_liveness_ts_splits_progress_from_real_events(plan_env):
    """The two timestamps the watchdog reads out of ``execution.log``.

    ``(last_non_progress_ts, last_progress_ts)`` — the newest real
    event and the newest heartbeat, as epochs.  Either may be ``None``.
    """
    _write_execution_log(plan_env, [QUOTA_WAIT_EVENT, PROGRESS_EVENT])

    non_progress_ts, progress_ts = vl._execution_log_liveness_ts(
        plan_env, time.time() - 100.0,
    )

    assert non_progress_ts is not None, (
        "the quota-window probe is a real event and must be reported"
    )
    assert progress_ts is not None, (
        "the heartbeat must be reported as the progress timestamp"
    )
    assert progress_ts >= non_progress_ts, (
        "the heartbeat was written after the probe, so its timestamp is "
        f"the later one: progress={progress_ts!r} non_progress={non_progress_ts!r}"
    )


def test_execution_log_liveness_ts_of_a_missing_log_is_empty(plan_env):
    """No ``execution.log`` at all — nothing to report, and no verdict.

    A plan whose execution log was never written (or whose lines cannot
    be read) falls back to the behaviour that existed before: the
    watchdog's other evidence decides.
    """
    assert vl._execution_log_liveness_ts(plan_env, None) == (None, None), (
        "a missing execution.log must report no timestamps at all"
    )
