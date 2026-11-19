"""The watchdog must be able to tell "slow" from "dead" (2026-11-02).

Why this file exists
--------------------
A dispatch that waits on a provider writes one heartbeat line per
interval into the plan's ``execution.log`` (see
``coding_tool._QueryProgressReporter``).  The watchdog did not read
it: ``_verification_liveness_probe`` only knew about live processes,
fresh ``/tmp`` artefacts and registry handles.  A round that was
*waiting* — busy, healthy, and simply slow — therefore looked exactly
like a round that was *wedged*, and nothing in the record said which.

The contracts pinned here:

1. A fresh heartbeat IS liveness evidence: the probe reports the round
   alive and names the heartbeat as the reason.
2. A plan that never wrote a heartbeat is NOT kept alive by the
   feature that reads it — the heartbeat is extra evidence, never a
   new requirement.
3. The two situations leave DIFFERENT evidence.  "Healthy but slow"
   (a heartbeat that exists but has aged out) and "really dead" (no
   heartbeat at all) must be tellable apart from the stale record
   alone, because that record is what an operator reads after the
   fact.

The clock is frozen in these tests.  The heartbeat's age is a
wall-clock measurement, and asserting an exact string against a moving
clock is a race against the CI runner rather than a contract.  The
freeze lands on a whole second and the heartbeat's stamp does too: the
age is measured by subtracting an ISO stamp from the clock, and a stamp
that carries a fraction of a second can come back a fraction of a
microsecond off — which is enough to render a 60s heartbeat as "59s".
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: it seeds ``sys.modules['server']`` before it imports
# ``verification_loop`` (see the wiring at the bottom of server.py), and
# importing the leaf module first would leave it half-initialised.
import server as _server  # noqa: E402
import verification_loop  # noqa: E402


#: The staleness budget the watchdog uses in these tests.  A heartbeat
#: is judged against the SAME number, so the two answers are comparable.
THRESHOLD_SEC = 900.0

#: The hard cap.  Everything in these tests stays well below it, so the
#: watchdog records the stale judgement and HOLDS the round instead of
#: stamping it terminal — no database rows are written on that path.
HARD_CAP_SEC = 3600.0

#: How old the "healthy but slow" heartbeat is: past the threshold, so
#: the round is still judged stale, but the heartbeat is on record.
SLOW_HEARTBEAT_AGE_SEC = 1000.0

#: The rendered prefix a heartbeat line carries in the log.
HEARTBEAT_MARKER = "[CODING_QUERY_PROGRESS]"


class _FrozenClock:
    """A ``time`` stand-in whose ``time()`` does not move.

    The probe measures the heartbeat's age against the wall clock, so
    the age is only reproducible if the clock is.  This replaces the
    ``time`` module *as the module under test sees it* — the real
    module is untouched for everything else in the process.
    """

    def __init__(self, frozen: float) -> None:
        self._frozen = frozen

    def time(self) -> float:
        return self._frozen

    def monotonic(self) -> float:
        return self._frozen

    def sleep(self, seconds: float) -> None:  # pragma: no cover
        time.sleep(seconds)


@pytest.fixture
def frozen_clock(monkeypatch):
    """Freeze the clock the watchdog and the probe read.

    The freeze lands on a WHOLE second on purpose.  A heartbeat's age
    is measured by subtracting the heartbeat's own stamp from this
    clock, and the stamp travels through an ISO string with
    microsecond resolution -- so a clock that sits inside a second can
    lose (or gain) a fraction of a microsecond on that round trip.
    That is enough to render a 60s heartbeat as "59s": the same plan,
    the same heartbeat, a different answer depending on when the test
    ran.  On a whole second every age in this file is exact.
    """
    frozen = math.floor(time.time())
    monkeypatch.setattr(verification_loop, "time", _FrozenClock(frozen))
    monkeypatch.setattr("server.VERIFICATION_WATCHDOG_STALENESS_SECONDS", THRESHOLD_SEC)
    monkeypatch.setattr("server.VERIFICATION_HARD_STALE_SECONDS", HARD_CAP_SEC)
    return frozen


@pytest.fixture
def plan_dir():
    """An (empty) plan directory under the isolated plans root."""
    plan_id = "vp-heartbeat-liveness"
    directory = _server.PLANS_DIR / plan_id
    directory.mkdir(parents=True, exist_ok=True)
    return plan_id, directory


@pytest.fixture(autouse=True)
def clean_watchdog_state():
    """Leave no watchdog state behind for the next test."""
    _server._verification_state.clear()
    _server._WATCHDOG_STATS["actions"].clear()
    _server._WATCHDOG_STATS["per_plan_last_action_ts"].clear()
    verification_loop._VERIFICATION_STALE_EVENTS.clear()
    yield
    _server._verification_state.clear()
    _server._WATCHDOG_STATS["actions"].clear()
    _server._WATCHDOG_STATS["per_plan_last_action_ts"].clear()
    verification_loop._VERIFICATION_STALE_EVENTS.clear()


def _write_heartbeat(directory: Path, *, age_sec: float, now: float) -> None:
    """Write an ``execution.log`` whose last line is a heartbeat.

    The line is written the way the reporter writes it: a JSON entry
    whose ``ts`` is a NAIVE UTC stamp (the convention
    ``execution_logger`` writes and ``execution_log_tail`` parses).

    The stamp sits on a whole second.  The probe reads the stamp back
    as a float and subtracts it from the clock, and an ISO string
    carries microseconds -- so a stamp with a fraction can come back a
    fraction of a microsecond off, which is enough to push a 60s
    heartbeat's age under 60 and have it rendered as "59s".  A whole
    second survives the round trip exactly, so the age the probe
    reports is the age this helper asked for.
    """
    written_at = math.floor(now - age_sec)
    stamp = (
        datetime.fromtimestamp(written_at, tz=timezone.utc)
        .replace(tzinfo=None)
        .isoformat()
    )
    entry = {
        "ts": stamp,
        "level": "INFO",
        "event": "coding_query_progress",
        "message": (
            f'{HEARTBEAT_MARKER} '
            '{"elapsed_sec": 300.0, "phase": "elapsed"}'
        ),
        "data": {"elapsed_sec": 300.0, "phase": "elapsed"},
    }
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / "execution.log"
    with open(log_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.utime(log_file, (written_at, written_at))


def _write_stale_round_log(directory: Path, *, now: float, age_sec: float) -> None:
    """Write a round log that stopped ``age_sec`` ago."""
    logs = directory / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_file = logs / "verification_round1.log"
    log_file.write_text("early log line\n", encoding="utf-8")
    stamp = now - age_sec
    os.utime(log_file, (stamp, stamp))


def _run_watchdog_tick(plan_id: str) -> None:
    """Run one watchdog tick for a running round with a live thread.

    The thread is alive, so the watchdog's thread-death rung does not
    fire and the tick reaches the staleness rung.  The round log is
    stale but younger than the hard cap, so the watchdog records the
    stale judgement and holds the round.
    """
    stop_event = threading.Event()
    thread = threading.Thread(
        target=stop_event.wait, args=(30,), daemon=True
    )
    thread.start()
    try:
        past_iso = (datetime.now() - timedelta(minutes=5)).isoformat()
        _server._verification_state[plan_id] = {
            "plan_id": plan_id,
            "verification_status": "running",
            "verification_round": 1,
            "verification_max_rounds": 3,
            "results": {},
            "repair_tasks": [],
            "started_at": past_iso,
            "updated_at": past_iso,
            "orchestrator": None,
            "stop_reason": None,
            "thread": thread,
        }
        _server._lazy_check_verification(plan_id)
    finally:
        stop_event.set()
        thread.join(timeout=2.0)


def _stale_events(plan_id: str) -> list:
    """The stale records recorded for ``plan_id``, oldest first."""
    return [
        event
        for event in verification_loop._VERIFICATION_STALE_EVENTS
        if event.get("plan_id") == plan_id
    ]


# ---------------------------------------------------------------------------
# 1. A fresh heartbeat is liveness evidence
# ---------------------------------------------------------------------------


def test_fresh_heartbeat_keeps_alive(plan_dir, frozen_clock):
    """A heartbeat 60s old keeps the round alive — and is named as the reason.

    The heartbeat is the evidence that separates "the round is waiting on
    a provider" from "the round is wedged".  Without it a busy-but-slow
    round and a dead round produce the same silence, and the watchdog
    cannot tell them apart.

    The detail string names the signal and its age, so the operator can
    see WHAT kept the round alive — not merely that something did.
    """
    plan_id, directory = plan_dir
    _write_heartbeat(directory, age_sec=60.0, now=frozen_clock)

    alive, detail = verification_loop._verification_liveness_probe(plan_id, [])

    assert alive is True, (
        f"a heartbeat 60s old is liveness evidence; the probe said {detail!r}"
    )
    assert detail == "coding_query_progress:60s", (
        f"the probe must name the heartbeat and its age; got {detail!r}"
    )


# ---------------------------------------------------------------------------
# 1a. The reported age does not depend on where the clock sits
# ---------------------------------------------------------------------------


def test_the_reported_age_is_stable_for_any_clock(plan_dir, monkeypatch):
    """The age the probe reports must not depend on the clock's fraction.

    The heartbeat's stamp travels through an ISO string with
    microsecond resolution, and the probe reads that stamp back as a
    float before subtracting it from the clock.  A clock that sits
    inside a second can lose (or gain) a fraction of a microsecond on
    that round trip -- which is enough to push a 60s heartbeat's age
    under 60 and have it rendered as "59s".  The same plan, the same
    heartbeat, a different answer, depending on when the test ran.

    This pins the property that ends that race: the age the probe
    reports is the age the helper asked for, whatever the clock says.
    """
    plan_id, directory = plan_dir
    # An instant whose sub-second fraction does not survive the round
    # trip: the stamp is written at ...5923977 and read back as
    # ...592398, so the heartbeat reads a fraction of a microsecond
    # newer than it was written and its measured age lands just under
    # 60s -- one second short once the probe renders it.
    frozen = 1794831968.5923977
    monkeypatch.setattr(verification_loop, "time", _FrozenClock(frozen))

    _write_heartbeat(directory, age_sec=60.0, now=frozen)

    alive, detail = verification_loop._verification_liveness_probe(plan_id, [])
    assert alive is True, (
        f"a heartbeat 60s old is liveness evidence; the probe said {detail!r}"
    )
    assert detail == "coding_query_progress:60s", (
        f"the reported age must not depend on the clock's fraction; got {detail!r}"
    )


# ---------------------------------------------------------------------------
# 2. No heartbeat → still stale
# ---------------------------------------------------------------------------


def test_no_heartbeat_still_stale(plan_dir, frozen_clock):
    """A plan with no heartbeat at all is still judged stale.

    The heartbeat is EXTRA evidence.  If its absence kept a round alive,
    the feature would protect exactly the rounds it knows nothing about
    — the ones that are genuinely dead.
    """
    plan_id, directory = plan_dir
    # A round log that stopped 1000s ago and no execution.log at all.
    _write_stale_round_log(directory, now=frozen_clock, age_sec=1000.0)

    alive, _detail = verification_loop._verification_liveness_probe(plan_id, [])
    assert alive is False, (
        "no heartbeat and no other signal must not read as alive"
    )

    _run_watchdog_tick(plan_id)

    events = _stale_events(plan_id)
    assert len(events) == 1, (
        f"the watchdog must record exactly one stale judgement; got {events!r}"
    )
    event = events[0]
    assert event["heartbeat_seen"] is False, (
        "a plan with no heartbeat must say so on the record"
    )
    assert event["last_heartbeat_age_sec"] is None, (
        "there is no heartbeat age to report when no heartbeat was seen"
    )
    assert event["threshold_sec"] == THRESHOLD_SEC


# ---------------------------------------------------------------------------
# 3. "Healthy but slow" and "really dead" are different evidence
# ---------------------------------------------------------------------------


def test_healthy_slow_vs_dead_distinguished(plan_dir, frozen_clock):
    """A slow round and a dead round leave different records.

    This is the whole point of the feature.  A heartbeat that exists but
    has aged past the threshold means "something was writing and stopped"
    — a round that was slow and then stopped.  No heartbeat at all means
    "nothing was writing" — a round that never had a pulse.  Both are
    judged stale, and an operator reading the record afterwards must be
    able to tell which one happened.
    """
    plan_id, directory = plan_dir

    # (a) healthy but slow: the heartbeat is on record but has aged out.
    _write_heartbeat(directory, age_sec=SLOW_HEARTBEAT_AGE_SEC, now=frozen_clock)
    _write_stale_round_log(directory, now=frozen_clock, age_sec=SLOW_HEARTBEAT_AGE_SEC)

    alive, _detail = verification_loop._verification_liveness_probe(plan_id, [])
    assert alive is False, (
        "a heartbeat older than the threshold is not liveness evidence — "
        "the round is still stale"
    )

    _run_watchdog_tick(plan_id)
    slow_events = _stale_events(plan_id)
    assert len(slow_events) == 1, slow_events
    slow = slow_events[0]

    # (b) really dead: no heartbeat at all.
    (directory / "execution.log").unlink()
    _run_watchdog_tick(plan_id)
    dead_events = _stale_events(plan_id)
    assert len(dead_events) == 2, dead_events
    dead = dead_events[1]

    # The two records must be tellable apart.
    assert slow["heartbeat_seen"] is True
    assert dead["heartbeat_seen"] is False
    assert slow["last_heartbeat_age_sec"] == pytest.approx(
        SLOW_HEARTBEAT_AGE_SEC, abs=1.0
    )
    assert dead["last_heartbeat_age_sec"] is None
    assert slow["threshold_sec"] == dead["threshold_sec"] == THRESHOLD_SEC

    # And the age must be the REAL age, not the threshold and not zero.
    assert slow["last_heartbeat_age_sec"] > slow["threshold_sec"], (
        "a heartbeat older than the threshold must report its real age"
    )
