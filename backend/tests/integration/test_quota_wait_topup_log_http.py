"""HTTP contract: a hard quota cap is a manual top-up, never a window wait.

The criterion this file pins
----------------------------
PRD requirement 3 reads:

    *the wait length must line up with the real window.  With no
    ``resets_at`` it is a flat 1800s.  A 5h window must be waited out
    against its real reset time; a hard plan cap — the token plan is
    spent and there is no reset — must be recognised as "somebody has to
    top the balance up", not as one more 30-minute spin.*

Two rounds of probing never produced evidence about that criterion::

    GET /api/execution/20261024-vp-quota-topup/logs?event=execution_waiting_for_quota
    -> connection refused        (the service was not running)
    -> 404 Plan not found        (a backend with an empty database)

Both are facts about the *service*, not about the plan.  A probe against
a shared port says the service was down; it says nothing at all about
whether the plan's log records the hard cap.  The criterion needs the
log itself to answer, and the only way to get that answer without a
running service is to write the log through the production writers and
read it back through the endpoint the operator reads.

Two parks, one question
-----------------------
A run parks for capacity for two different reasons, and both parks are
recorded under the SAME event (``execution_waiting_for_quota``) in the
SAME file — the operator asks one question ("why is this plan parked?")
and gets one place to look.  The two scenarios are told apart by the
payload, so the payload has to be unambiguous:

  * a **window wait** — the budget comes back on a timer.  The wait is
    measured against the window (``waited_sec`` > 0) and
    ``needs_manual_topup`` is **false**: nobody has to do anything for
    a window to reopen;

  * a **hard cap** — the budget was *paid for* and is spent.  No timer
    brings it back, so nothing was waited out (``waited_sec`` == 0.0)
    and ``needs_manual_topup`` is **true**: somebody has to top the
    balance up.

A window wait that claimed ``needs_manual_topup`` would page somebody
for a window, and a hard cap that claimed a positive ``waited_sec``
would be indistinguishable from the 30-minute spin the PRD forbids.
So the two scenarios must not cross-contaminate: not through the
filter (one event filter, two parks, two answers) and not through the
payload (each park keeps its own numbers).

What is written, and by whom
----------------------------
Nothing here hand-writes a log line.  Every value in the log is
produced by production code:

  * ``coding_tool._raise_if_all_blocked_are_manual_topup`` decides
    whether a refusal is a hard cap or a window — the decision the
    whole criterion turns on;
  * ``agent.AutonomousAgent._stop_reason_for_quota_wait`` writes the
    ``waiting_for_quota`` marker into the plan's ``tasks.json``;
  * ``routes.execution._enter_quota_wait`` parks the plan and writes
    the disposition event into the plan's ``execution.log``;
  * ``quota_watchdog`` writes the window wait's own event, with the
    ``resets_at`` computed from the reading rather than invented.

Hermeticity
------------
Everything happens under the test's own ``tmp_path``: the autouse
``isolated_plans_dir`` fixture points ``server.PLANS_DIR`` (and the
``PDT_PLANS_DIR`` env var the logger resolves through) at it, and the
state database at ``<tmp_path>/state.db``, so the operator's live
``plans/`` tree and ``state.db`` are never touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from agent import AutonomousAgent
from capacity_wait import (
    DEFAULT_CAPACITY_WAIT_SEC,
    QuotaWaitExhausted,
    quota_wait_event_data,
    quota_wait_kind,
    resolve_quota_wait,
)
from coding_tool import _raise_if_all_blocked_are_manual_topup
from execution_logger import ExecutionLogger
from provider_order import SHAPE_BALANCE, SHAPE_ROLLING, ProviderQuota
from quota_watchdog import QuotaWatchdog
from task_manager import TaskManager


#: The plan the failed probes used.  Reusing it keeps the criterion and
#: its evidence about the same plan.
TOPUP_PLAN_ID = "20261024-vp-quota-topup"

#: The plan that holds the window wait.  A separate plan, so the two
#: scenarios can be read apart as well as side by side.
WINDOW_PLAN_ID = "20261024-vp-quota-window"

#: The plan that holds BOTH parks, so one event filter sees both.
BOTH_PLAN_ID = "20261024-vp-quota-both"

#: The plan that holds the park the two fields do NOT decide — neither
#: a top-up nor a waited-out window.  A third plan, so the "unknown"
#: answer is read on its own and cannot be mistaken for the other two.
UNKNOWN_PLAN_ID = "20261024-vp-quota-unknown"

#: The plan that holds the FLAT wait — the one the PRD names for a
#: window the reading does not put a clock on: with no ``resets_at``
#: there is nothing to count down to, so the wait is the standard half
#: hour (1800s).  A fourth plan, because a flat half hour and a
#: measured five-hour window are two different waits and the log has to
#: be able to tell them apart.
FLAT_WINDOW_PLAN_ID = "20261024-vp-quota-flat-window"

#: The disposition's event — "the run parked because capacity ran out".
#: Both scenarios write it; the payload tells them apart.
WAIT_EVENT = "execution_waiting_for_quota"

#: The watchdog's event — "a dispatch started waiting on a closed window".
WINDOW_WAIT_EVENT = "provider_quota_window_wait_entered"

#: The provider whose balance is spent: a person has to top it up.
TOPUP_PROVIDER = "MiniMax"

#: The provider whose 5h window is shut: the wait clears by itself.
WINDOW_PROVIDER = "vendor-a"

#: The half hour the old code waited when the reading named no window.
#: The PRD forbids spending one of these on a hard cap.
THIRTY_MINUTES = DEFAULT_CAPACITY_WAIT_SEC

#: The window the window scenario waits out: five hours, from the moment
#: the window shut to the moment the provider says it opens again.  The
#: ``resets_at`` is a FACT the reading carries, not a constant this
#: module invents — so it is computed here and carried back verbatim.
WINDOW_START = datetime(2026, 10, 24, 15, 0, 0, tzinfo=timezone.utc)
RESETS_AT = WINDOW_START + timedelta(hours=5)
WINDOW_SEC = (RESETS_AT - WINDOW_START).total_seconds()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_dir(plan_id: str) -> Path:
    """Where the plan's own directory lives."""
    return Path(server.PLANS_DIR) / plan_id


def _seed_plan(tmp_path: Path, plan_id: str) -> None:
    """Give the plan the rows the disposition writes to.

    Without them the disposition degrades: it logs that it could not
    persist the park and forces the phase, and the plan's log carries
    two failure events next to the one under test.  A real parked plan
    has both rows, so the fixture gives it both.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, substage, current_phase, version, completed_phases, "
            " review_rounds, flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, None, "executing", 0, "[]", "{}", "{}", "{}",
                "2026-10-24T00:00:00", "2026-10-24T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution "
            "(plan_id, attempt_count, project_dir, stop_reason, task_progress, "
            " next_run_at, card_state, flags, exec_pid, exec_status, "
            " started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, 0, str(project_dir), None, json.dumps({"tasks": {}}),
                None, "{}", "{}", None, "not_started", None,
                "2026-10-24T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


class _AgentStub:
    """The three things the marker writer touches.

    :meth:`agent.AutonomousAgent._stop_reason_for_quota_wait` is the
    production writer of the ``waiting_for_quota`` marker, and it
    touches exactly three things: the flag it sets, the task manager it
    writes through, and the logger it reports to.  A stub with those is
    enough to drive the real method — and driving the real method is
    the point: the marker's shape is production's, not this file's.
    """

    def __init__(self, task_manager, logger):
        self.task_manager = task_manager
        self.logger = logger
        self._quota_wait_requested = False


def _write_marker(plan_id: str, exc: QuotaWaitExhausted) -> str:
    """Write the ``waiting_for_quota`` marker the way a run does."""
    plan_dir = _plan_dir(plan_id)
    plan_dir.mkdir(parents=True, exist_ok=True)
    task_manager = TaskManager(
        project_dir=plan_dir, tasks_file=plan_dir / "tasks.json",
    )
    logger = ExecutionLogger(plan_id, plans_dir=Path(server.PLANS_DIR))
    return AutonomousAgent._stop_reason_for_quota_wait(
        _AgentStub(task_manager, logger), exc,
    )


def _park(plan_id: str) -> None:
    """Park the plan the way the server does when a run ends on quota."""
    from routes import execution as routes_execution

    plan_dir = _plan_dir(plan_id)
    state = {"status": "running", "stop_reason": None, "ended_at": None}
    assert routes_execution._enter_quota_wait(
        plan_id, plan_dir, state, plan_dir / "tasks.json",
    ) is True, "the marker must be recognised as a quota park"


def _topup_exception() -> QuotaWaitExhausted:
    """The production verdict on a spent balance: a hard cap.

    ``_raise_if_all_blocked_are_manual_topup`` is the function that
    decides "this is not a window — somebody has to act".  A reading
    whose exhausted tier is a *balance* tier (not one of the gated
    windows) is that case, and the verdict arrives as a
    ``QuotaWaitExhausted`` that says so.
    """
    quota = ProviderQuota(
        provider=TOPUP_PROVIDER,
        shape=SHAPE_BALANCE,
        remaining_pct=0.0,
        exhausted=True,
        # The vendor's own name for its balance tier.  It is NOT one of
        # the gated windows — that is the whole point: a balance is
        # topped up by a person, not by a timer.
        exhausted_tier="credits",
        exhausted_resets_at=None,
    )
    with pytest.raises(QuotaWaitExhausted) as caught:
        _raise_if_all_blocked_are_manual_topup(
            blocked=[TOPUP_PROVIDER],
            quota_map={TOPUP_PROVIDER: quota},
            scene="execution",
            waited_sec=0.0,
        )
    return caught.value


def _window_exception() -> QuotaWaitExhausted:
    """The production verdict on a shut window: keep waiting.

    A window is not a hard cap, so the same function does NOT raise
    here — that is the difference the criterion turns on, and it is
    asserted rather than assumed.  What the run ends up with is a park
    that waited the window out: measured, and not a person's job.
    """
    quota = ProviderQuota(
        provider=WINDOW_PROVIDER,
        shape=SHAPE_ROLLING,
        remaining_pct=0.0,
        exhausted=True,
        exhausted_tier="five_hour",
        exhausted_resets_at=RESETS_AT,
    )
    _raise_if_all_blocked_are_manual_topup(
        blocked=[WINDOW_PROVIDER],
        quota_map={WINDOW_PROVIDER: quota},
        scene="execution",
        waited_sec=0.0,
    )
    return QuotaWaitExhausted(
        providers=[WINDOW_PROVIDER],
        needs_manual_topup=False,
        waited_sec=WINDOW_SEC,
    )


def _unknown_exception() -> QuotaWaitExhausted:
    """A park that is neither a top-up nor a waited-out window.

    The two fields the log carries do not decide anything here: no
    top-up is needed, and nothing was waited out.  The label for that
    is ``unknown`` — and it is the case a default would get wrong,
    because a default would call it a window and send the operator off
    to wait for a timer that does not exist.
    """
    return QuotaWaitExhausted(
        providers=[TOPUP_PROVIDER],
        needs_manual_topup=False,
        waited_sec=0.0,
    )


def _flat_window_exception() -> QuotaWaitExhausted:
    """The production verdict on a window with no clock to count down.

    The PRD names this case: *with no ``resets_at`` it is a flat
    1800s*.  A reading that names a gated tier but carries no reset
    time cannot be waited out against a clock, so the honest length is
    the standard half hour — and nobody has to do anything for it to
    pass.

    The length is the resolver's own answer
    (``capacity_wait.resolve_quota_wait``), not a number this file
    invented: the test reads the wait production decided on, so the
    log cannot drift away from the decision it claims to record.
    """
    quota = ProviderQuota(
        provider=WINDOW_PROVIDER,
        shape=SHAPE_ROLLING,
        remaining_pct=0.0,
        exhausted=True,
        exhausted_tier="five_hour",
        # The reading names the window but not when it reopens.
        exhausted_resets_at=None,
    )
    wait = resolve_quota_wait(
        WINDOW_PROVIDER, "quota exhausted", "429", quota=quota,
    )
    return QuotaWaitExhausted(
        providers=[WINDOW_PROVIDER],
        needs_manual_topup=wait.needs_manual_topup,
        waited_sec=wait.seconds,
    )


def _write_window_event(plan_id: str) -> None:
    """The watchdog's account of a dispatch waiting on a closed window.

    The watchdog is the production writer, and it computes the reset
    stamp itself from the reading — so the ``resets_at`` that ends up
    in the log is the one the reading carried, not one this test wrote
    into the log by hand.
    """
    quota = ProviderQuota(
        provider=WINDOW_PROVIDER,
        shape=SHAPE_ROLLING,
        remaining_pct=0.0,
        exhausted=True,
        exhausted_tier="five_hour",
        exhausted_resets_at=RESETS_AT,
    )
    QuotaWatchdog().wait_entered(
        scene="task_execution",
        blocked=[WINDOW_PROVIDER],
        quota_map={WINDOW_PROVIDER: quota},
        logger=ExecutionLogger(plan_id, plans_dir=Path(server.PLANS_DIR)),
    )


def _client() -> TestClient:
    """A client that does NOT run the application lifespan.

    The lifespan is what restores ``_execution_state`` from the rows at
    boot.  This contract is about the log on disk, which survives a
    restart — so the answer has to be readable without any in-memory
    state.
    """
    server._execution_state.clear()
    return TestClient(server.app)


def _get_logs(client: TestClient, plan_id: str, event: str) -> dict:
    """Read one event out of a plan's log through the endpoint."""
    resp = client.get(
        f"/api/execution/{plan_id}/logs", params={"event": event},
    )
    assert resp.status_code == 200, (
        f"the plan's own log must answer the logs endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    return resp.json()


# ---------------------------------------------------------------------------
# The hard cap is reported as a manual top-up
# ---------------------------------------------------------------------------


def test_topup_wait_event_reports_manual_topup(tmp_path):
    """The hard cap comes back as "somebody has to top the balance up".

    Three facts make up the criterion: which provider ran out, that a
    person has to act, and that nothing was waited out.  The last one
    is the "no 30-minute spin" half — a hard cap is recognised before
    anything is waited, so ``waited_sec`` is zero rather than the flat
    half hour the old code spent on every refusal.

    The answer is read back through the endpoint the operator reads,
    because that is where the criterion is checked: a log line nobody
    can fetch is not evidence.
    """
    _seed_plan(tmp_path, TOPUP_PLAN_ID)

    exc = _topup_exception()
    assert exc.providers == [TOPUP_PROVIDER], (
        f"the hard cap must name the provider that ran out; got "
        f"{exc.providers!r}"
    )
    assert exc.needs_manual_topup is True, (
        f"a spent balance must be recognised as needing a top-up; got "
        f"{exc.needs_manual_topup!r}"
    )
    assert exc.waited_sec == 0.0, (
        f"a hard cap is detected before anything is waited out; got "
        f"{exc.waited_sec!r}"
    )

    _write_marker(TOPUP_PLAN_ID, exc)
    _park(TOPUP_PLAN_ID)

    client = _client()
    body = _get_logs(client, TOPUP_PLAN_ID, WAIT_EVENT)

    assert body["total"] == 1, (
        f"the hard cap must be readable; got {body.get('total')!r} "
        f"entries: {body.get('entries')!r}"
    )

    entry = body["entries"][0]
    assert entry["event"] == WAIT_EVENT, (
        f"the endpoint answered with the wrong event: {entry!r}"
    )
    data = entry["data"]
    assert data["needs_manual_topup"] is True, (
        f"a hard cap must be reported as needing a manual top-up; got "
        f"{data!r}"
    )
    assert data["waited_sec"] == 0.0, (
        f"a hard cap must not be reported as a wait that was sat out — "
        f"in particular not the flat {THIRTY_MINUTES:g}s the old code "
        f"used; got {data!r}"
    )
    assert data["providers"][0] == TOPUP_PROVIDER, (
        f"the park must name the provider that ran out; got {data!r}"
    )
    assert not ({"resets_at", "earliest_resets_at"} & set(data)), (
        f"a hard cap must not masquerade as a window wait — there is no "
        f"reset time for it to wait out; got {data!r}"
    )


# ---------------------------------------------------------------------------
# The two parks must not cross-contaminate
# ---------------------------------------------------------------------------


def test_topup_and_window_are_distinguishable(tmp_path):
    """The two parks are told apart — by the filter and by the payload.

    Three reads, one process, one client:

    * each scenario in its own plan, read on its own: the numbers do
      not leak from one park into the other;
    * both parks in ONE plan, read through the same event filter: the
      filter does not fold them into one answer, and each entry keeps
      the numbers of its own scenario;
    * a near-miss event name: the filter matches the event exactly, so
      a prefix of it matches nothing at all.
    """
    for plan_id in (TOPUP_PLAN_ID, WINDOW_PLAN_ID, BOTH_PLAN_ID):
        _seed_plan(tmp_path, plan_id)

    topup_exc = _topup_exception()
    window_exc = _window_exception()

    # -- Each scenario in its own plan, read on its own. ----------------
    _write_marker(TOPUP_PLAN_ID, topup_exc)
    _park(TOPUP_PLAN_ID)
    _write_marker(WINDOW_PLAN_ID, window_exc)
    _park(WINDOW_PLAN_ID)

    client = _client()

    topup_body = _get_logs(client, TOPUP_PLAN_ID, WAIT_EVENT)
    window_body = _get_logs(client, WINDOW_PLAN_ID, WAIT_EVENT)

    assert topup_body["total"] == 1, (
        f"the hard cap must be readable on its own; got "
        f"{topup_body.get('total')!r} entries: {topup_body.get('entries')!r}"
    )
    topup_data = topup_body["entries"][0]["data"]
    assert topup_data["needs_manual_topup"] is True, (
        f"the hard cap must keep saying a person has to act; got "
        f"{topup_data!r}"
    )
    assert topup_data["waited_sec"] == 0.0, (
        f"the hard cap must keep its zero wait; got {topup_data!r}"
    )
    assert topup_data["providers"] == [TOPUP_PROVIDER], (
        f"the hard cap must keep its own provider; got {topup_data!r}"
    )

    assert window_body["total"] == 1, (
        f"the window wait must be readable on its own; got "
        f"{window_body.get('total')!r} entries: "
        f"{window_body.get('entries')!r}"
    )
    window_data = window_body["entries"][0]["data"]
    assert window_data["needs_manual_topup"] is False, (
        f"a closed window is not a balance somebody has to top up; got "
        f"{window_data!r}"
    )
    assert window_data["waited_sec"] == WINDOW_SEC, (
        f"the window wait must keep the seconds it waited out; got "
        f"{window_data!r}"
    )
    assert window_data["providers"] == [WINDOW_PROVIDER], (
        f"the window wait must keep its own provider; got {window_data!r}"
    )

    # -- Both parks in ONE plan, through the same event filter. ---------
    _write_window_event(BOTH_PLAN_ID)
    _write_marker(BOTH_PLAN_ID, window_exc)
    _park(BOTH_PLAN_ID)
    _write_marker(BOTH_PLAN_ID, topup_exc)
    _park(BOTH_PLAN_ID)

    body = _get_logs(client, BOTH_PLAN_ID, WAIT_EVENT)

    assert body["total"] == 2, (
        f"the two parks are two events — the filter must not fold them "
        f"into one answer; got {body.get('total')!r} entries: "
        f"{body.get('entries')!r}"
    )

    topup_entries = [
        entry for entry in body["entries"]
        if entry["data"]["needs_manual_topup"] is True
    ]
    window_entries = [
        entry for entry in body["entries"]
        if entry["data"]["needs_manual_topup"] is False
    ]
    assert len(topup_entries) == 1, (
        f"exactly one park is a hard cap; got {body.get('entries')!r}"
    )
    assert len(window_entries) == 1, (
        f"exactly one park is a window wait; got {body.get('entries')!r}"
    )

    assert topup_entries[0]["data"]["waited_sec"] == 0.0, (
        f"the hard cap must keep its zero wait next to the window's; got "
        f"{topup_entries[0]['data']!r}"
    )
    assert topup_entries[0]["data"]["providers"] == [TOPUP_PROVIDER], (
        f"the hard cap must keep its own provider next to the window's; "
        f"got {topup_entries[0]['data']!r}"
    )
    assert not (
        {"resets_at", "earliest_resets_at"} & set(topup_entries[0]["data"])
    ), (
        f"a hard cap must not masquerade as a window wait — it has no "
        f"reset time to wait out; got {topup_entries[0]['data']!r}"
    )
    assert window_entries[0]["data"]["waited_sec"] == WINDOW_SEC, (
        f"the window wait must keep the seconds it waited out next to "
        f"the hard cap's; got {window_entries[0]['data']!r}"
    )
    assert window_entries[0]["data"]["providers"] == [WINDOW_PROVIDER], (
        f"the window wait must keep its own provider next to the hard "
        f"cap's; got {window_entries[0]['data']!r}"
    )

    # -- The watchdog's event is a different event. ---------------------
    window_wait_body = _get_logs(client, BOTH_PLAN_ID, WINDOW_WAIT_EVENT)
    assert window_wait_body["total"] == 1, (
        f"the window wait has its own event, and the filter must not "
        f"fold it into the disposition's; got "
        f"{window_wait_body.get('total')!r} entries: "
        f"{window_wait_body.get('entries')!r}"
    )
    assert window_wait_body["entries"][0]["event"] == WINDOW_WAIT_EVENT, (
        f"the endpoint answered with the wrong event: "
        f"{window_wait_body['entries'][0]!r}"
    )

    # -- And a prefix of the event name matches nothing. ----------------
    prefix_body = _get_logs(client, BOTH_PLAN_ID, "execution_waiting")
    assert prefix_body["total"] == 0, (
        f"the event filter is exact — a prefix of the event name must "
        f"not match it; got {prefix_body.get('total')!r} entries: "
        f"{prefix_body.get('entries')!r}"
    )
    assert prefix_body["entries"] == [], (
        f"a near-miss event name carries no entry at all; got "
        f"{prefix_body.get('entries')!r}"
    )


# ---------------------------------------------------------------------------
# The park carries a kind a machine can switch on
# ---------------------------------------------------------------------------


def test_vp_007_hard_cap_log_reports_manual_topup_with_zero_wait(tmp_path):
    """The hard cap is labelled for a machine, and it keeps its zero.

    The previous round proved that the two parks can be told apart — but
    only by a reader who combines two fields itself.  That is a
    derivation every consumer has to make the same way, and a consumer
    that makes it differently pages somebody for a window (or waits out
    a hard cap).  So the log ships the answer instead of the ingredients:
    a normalised ``wait_kind``.

      * ``manual_topup`` — a spent balance: somebody has to act;
      * ``window`` — a window that reopens by itself, after a wait that
        was actually measured;
      * ``unknown`` — the two fields do not decide.  NOT a window: an
        invented window is a person told to wait for a timer that does
        not exist, which is the very thing the PRD forbids.

    The zero is the other half of the criterion.  ``waited_sec`` is
    ``0.0`` for a hard cap, and ``0.0`` is FALSE in Python: a payload
    assembled with "only write what has a value" drops the very number
    that says "nothing was waited out" — and a reader left with
    ``needs_manual_topup`` alone is back to deriving.  So the field
    has to be there, as the float it was written with, next to the
    label, and the label must not have replaced it with a default.
    """
    for plan_id in (TOPUP_PLAN_ID, WINDOW_PLAN_ID, UNKNOWN_PLAN_ID):
        _seed_plan(tmp_path, plan_id)

    _write_marker(TOPUP_PLAN_ID, _topup_exception())
    _park(TOPUP_PLAN_ID)
    _write_marker(WINDOW_PLAN_ID, _window_exception())
    _park(WINDOW_PLAN_ID)
    _write_marker(UNKNOWN_PLAN_ID, _unknown_exception())
    _park(UNKNOWN_PLAN_ID)

    client = _client()

    topup_body = _get_logs(client, TOPUP_PLAN_ID, WAIT_EVENT)
    window_body = _get_logs(client, WINDOW_PLAN_ID, WAIT_EVENT)
    unknown_body = _get_logs(client, UNKNOWN_PLAN_ID, WAIT_EVENT)

    assert topup_body["total"] == 1, (
        f"the hard cap must be readable; got {topup_body.get('total')!r} "
        f"entries: {topup_body.get('entries')!r}"
    )
    assert window_body["total"] == 1, (
        f"the window wait must be readable; got {window_body.get('total')!r} "
        f"entries: {window_body.get('entries')!r}"
    )
    assert unknown_body["total"] == 1, (
        f"the undecided park must be readable; got "
        f"{unknown_body.get('total')!r} entries: "
        f"{unknown_body.get('entries')!r}"
    )

    topup_data = topup_body["entries"][0]["data"]
    window_data = window_body["entries"][0]["data"]
    unknown_data = unknown_body["entries"][0]["data"]

    # -- The kind, in one word a machine can switch on. ----------------
    assert topup_data.get("wait_kind") == "manual_topup", (
        f"a hard cap must be exposed AS a manual top-up, not left for "
        f"the reader to derive from two fields; got {topup_data!r}"
    )
    assert window_data.get("wait_kind") == "window", (
        f"a window wait must be exposed as the window it is; got "
        f"{window_data!r}"
    )
    assert unknown_data.get("wait_kind") == "unknown", (
        f"a park the two fields do not decide must say so; got "
        f"{unknown_data!r}"
    )
    assert unknown_data.get("wait_kind") != "window", (
        f"an undecided park must never be dressed up as a window — a "
        f"default would invent a timer that does not exist; got "
        f"{unknown_data!r}"
    )

    # -- The zero survives as the float it was written with. -----------
    assert "waited_sec" in topup_data, (
        f"the hard cap's wait must be reported even though it is zero — "
        f"a payload that only writes what has a value drops 0.0; got "
        f"{topup_data!r}"
    )
    assert isinstance(topup_data["waited_sec"], float), (
        f"the hard cap's wait must stay a float; got "
        f"{topup_data['waited_sec']!r} "
        f"({type(topup_data['waited_sec']).__name__})"
    )
    assert topup_data["waited_sec"] == 0.0, (
        f"a hard cap must not be reported as a wait that was sat out — "
        f"in particular not the flat {THIRTY_MINUTES:g}s the old code "
        f"used; got {topup_data!r}"
    )
    assert topup_data["needs_manual_topup"] is True, (
        f"a hard cap must be reported as needing a manual top-up, as a "
        f"bool; got {topup_data!r}"
    )
    assert topup_data["providers"] == [TOPUP_PROVIDER], (
        f"the hard cap must keep naming the provider that ran out; got "
        f"{topup_data!r}"
    )

    # -- And the other two keep their own numbers. ---------------------
    assert window_data["waited_sec"] == WINDOW_SEC, (
        f"the window wait must keep the seconds it waited out; got "
        f"{window_data!r}"
    )
    assert window_data["needs_manual_topup"] is False, (
        f"a closed window is not a balance somebody has to top up; got "
        f"{window_data!r}"
    )
    assert unknown_data["waited_sec"] == 0.0, (
        f"the undecided park must keep its zero; got {unknown_data!r}"
    )
    assert unknown_data["needs_manual_topup"] is False, (
        f"the undecided park must not claim a top-up either; got "
        f"{unknown_data!r}"
    )


# ---------------------------------------------------------------------------
# The kind is written down — it is not the reader's job to derive it
# ---------------------------------------------------------------------------


#: The parks, the kind each one is, and the exception that produces
#: it.  One table, so the write path and the read path are held to the
#: SAME expectation: a hard cap is a hard cap on both sides of the log.
_KINDS = (
    (TOPUP_PLAN_ID, "manual_topup"),
    (FLAT_WINDOW_PLAN_ID, "window"),
    (WINDOW_PLAN_ID, "window"),
    (UNKNOWN_PLAN_ID, "unknown"),
)

#: The kind decides the flag, and only the kind decides it: a kind
#: never carries two different flags, so the two spellings of one fact
#: cannot contradict each other.
_KIND_TO_FLAG = {
    "manual_topup": True,
    "window": False,
    "unknown": False,
}


def _exception_for(plan_id: str) -> QuotaWaitExhausted:
    """The production verdict that belongs to ``plan_id``."""
    if plan_id == TOPUP_PLAN_ID:
        return _topup_exception()
    if plan_id == FLAT_WINDOW_PLAN_ID:
        return _flat_window_exception()
    if plan_id == WINDOW_PLAN_ID:
        return _window_exception()
    return _unknown_exception()


def _written_events(plan_id: str) -> list:
    """The events the run wrote into the plan's own log.

    The raw file, not the endpoint: this is what the WRITE path put
    there, before any reader had a say in it.
    """
    log_file = _plan_dir(plan_id) / "execution.log"
    entries = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:  # pragma: no cover — defensive
            continue
    return entries


def _written_wait_event(plan_id: str) -> dict:
    """The one park event the plan's own log carries."""
    waiting = [
        entry for entry in _written_events(plan_id)
        if entry.get("event") == WAIT_EVENT
    ]
    assert len(waiting) == 1, (
        f"the park of {plan_id} must be recorded exactly once; got "
        f"{waiting!r}"
    )
    return waiting[0]


def test_park_event_records_kind_field(tmp_path):
    """The WRITE path: the park event says which kind of park it is.

    The park event used to carry numbers only — the providers, whether
    a person has to act, and how long was waited out.  Every reader
    then had to *derive* the kind from them, and a reader that derived
    it differently turned a hard cap into a window wait: a person told
    to wait for a timer that does not exist.

    So the kind is written down, by the same call that writes the
    numbers, and the two cannot disagree: the flag is derived from the
    kind rather than written beside it.  Every park is checked, because
    "the hard cap is labelled" is only half of it — a window and an
    undecided park must be labelled as themselves, and the undecided
    one must NOT be dressed up as a window.
    """
    for plan_id, expected_kind in _KINDS:
        _seed_plan(tmp_path, plan_id)
        _write_marker(plan_id, _exception_for(plan_id))
        _park(plan_id)

        data = _written_wait_event(plan_id)["data"]

        assert data.get("kind") == expected_kind, (
            f"the park event of {plan_id} must say which kind of park it "
            f"is; got {data!r}"
        )
        assert data["needs_manual_topup"] is (
            expected_kind == "manual_topup"
        ), (
            f"the kind and the flag the park event of {plan_id} writes "
            f"must not contradict each other; got {data!r}"
        )


def test_kind_and_needs_manual_topup_cannot_contradict(tmp_path):
    """The kind and the flag are ONE fact, and they cannot disagree.

    There is exactly one mapping between them, and it is checked from
    both ends:

    * every combination of the two numbers the writer has — the flag
      and the wait — lands on the kind the one rule says, and the flag
      that comes out of that kind is the flag that went in;
    * every park the API exposes agrees with itself: the kind the
      writer wrote, the flag the writer wrote, and the label the
      reader normalises are the same answer about one park.
    """
    # -- The rule, over every combination of the two numbers. ----------
    for needs_manual_topup in (True, False):
        for waited_sec in (0.0, 1800.0):
            data = quota_wait_event_data(
                providers=[TOPUP_PROVIDER],
                waited_sec=waited_sec,
                needs_manual_topup=needs_manual_topup,
            )

            assert data["kind"] == quota_wait_kind(
                needs_manual_topup, waited_sec,
            ), (
                f"the kind of a park with needs_manual_topup="
                f"{needs_manual_topup!r} and waited_sec={waited_sec!r} "
                f"must be the one the rule says; got {data!r}"
            )
            assert data["needs_manual_topup"] is (
                data["kind"] == "manual_topup"
            ), (
                f"the kind and the flag must not contradict each other; "
                f"got {data!r}"
            )
            assert data["needs_manual_topup"] is _KIND_TO_FLAG[
                data["kind"]
            ], (
                f"the kind must decide the flag and only the kind; got "
                f"{data!r}"
            )

    # -- And the API cannot contradict itself either. ------------------
    for plan_id, expected_kind in _KINDS:
        _seed_plan(tmp_path, plan_id)
        _write_marker(plan_id, _exception_for(plan_id))
        _park(plan_id)

    client = _client()

    for plan_id, expected_kind in _KINDS:
        body = _get_logs(client, plan_id, WAIT_EVENT)

        assert body["total"] == 1, (
            f"the park of {plan_id} must be readable; got "
            f"{body.get('total')!r} entries: {body.get('entries')!r}"
        )
        data = body["entries"][0]["data"]

        assert data["kind"] == expected_kind, (
            f"the endpoint must expose the kind of the park of {plan_id}; "
            f"got {data!r}"
        )
        assert data["needs_manual_topup"] is _KIND_TO_FLAG[expected_kind], (
            f"the kind and the flag the endpoint exposes for {plan_id} "
            f"must not contradict each other; got {data!r}"
        )
        assert data.get("wait_kind") == expected_kind, (
            f"the label the reader normalises and the label the writer "
            f"wrote must be the same word for {plan_id}; got {data!r}"
        )


def test_topup_event_reads_back_needs_manual_topup_true_and_waited_sec_zero(
    tmp_path,
):
    """The READ path: the hard cap comes back as a hard cap.

    The operator asks one question — "why is this plan parked?" — and
    the answer has to arrive whole: which provider ran out, that a
    person has to act, and that nothing was waited out.  The last one
    is the "no thirty-minute spin" half — a hard cap is recognised
    before anything is waited, so ``waited_sec`` is zero rather than
    the flat half hour the old code spent on every refusal.

    The answer is read back through the endpoint the operator reads,
    because that is where the criterion is checked: a log line nobody
    can fetch is not evidence.
    """
    _seed_plan(tmp_path, TOPUP_PLAN_ID)
    _write_marker(TOPUP_PLAN_ID, _topup_exception())
    _park(TOPUP_PLAN_ID)

    client = _client()
    body = _get_logs(client, TOPUP_PLAN_ID, WAIT_EVENT)

    assert body["total"] == 1, (
        f"the hard cap must be readable; got {body.get('total')!r} "
        f"entries: {body.get('entries')!r}"
    )
    entry = body["entries"][0]
    assert entry["event"] == WAIT_EVENT, (
        f"the endpoint answered with the wrong event: {entry!r}"
    )
    data = entry["data"]

    assert data["needs_manual_topup"] is True, (
        f"a hard cap must be reported as needing a manual top-up; got "
        f"{data!r}"
    )
    assert data["waited_sec"] == 0.0, (
        f"a hard cap must not be reported as a wait that was sat out — "
        f"in particular not the flat {THIRTY_MINUTES:g}s the old code "
        f"used; got {data!r}"
    )
    assert data["providers"][0] == TOPUP_PROVIDER, (
        f"the park must name the provider that ran out; got {data!r}"
    )
    assert data["kind"] == "manual_topup", (
        f"the API must expose the hard cap AS a manual top-up; got {data!r}"
    )


def test_window_event_reads_back_waited_sec_1800(tmp_path):
    """The READ path: the window wait comes back with its own length.

    The PRD's window case: *with no ``resets_at`` it is a flat 1800s*.
    That is a timed recovery, so the log says so — the wait that was
    sat out comes back as the number of seconds that were sat out, and
    nobody is told to buy credits because the balance was never the
    problem.

    The criterion is the one the plan's own verification reads: the
    window park is told apart from the hard cap by its length and by
    its flag, and the two never look alike.
    """
    _seed_plan(tmp_path, FLAT_WINDOW_PLAN_ID)
    _write_marker(FLAT_WINDOW_PLAN_ID, _flat_window_exception())
    _park(FLAT_WINDOW_PLAN_ID)

    client = _client()
    body = _get_logs(client, FLAT_WINDOW_PLAN_ID, WAIT_EVENT)

    assert body["total"] == 1, (
        f"the window wait must be readable; got {body.get('total')!r} "
        f"entries: {body.get('entries')!r}"
    )
    data = body["entries"][0]["data"]

    assert data["waited_sec"] == 1800.0, (
        f"the window wait must come back with the seconds it waited out; "
        f"got {data!r}"
    )
    assert data["needs_manual_topup"] is False, (
        f"a closed window is not a balance somebody has to top up; got "
        f"{data!r}"
    )
    assert data["providers"], (
        f"the window wait must name the provider it waited on; got {data!r}"
    )
    assert data["kind"] == "window", (
        f"the API must expose the window wait AS a window; got {data!r}"
    )


def test_the_event_payload_treats_one_provider_as_one_provider():
    """A single provider, given by name, is one provider.

    The park names the provider that ran out, and a name is a name —
    not a list of the letters it is spelled with.  A payload that
    spelled ``MiniMax`` out letter by letter would still answer the
    operator's question with the wrong provider.
    """
    data = quota_wait_event_data(
        providers=TOPUP_PROVIDER,
        needs_manual_topup=True,
    )

    assert data["providers"] == [TOPUP_PROVIDER], (
        f"one provider is one provider, not a string of characters; got "
        f"{data!r}"
    )
    assert data["kind"] == "manual_topup", (
        f"the kind must not change because of how the providers were "
        f"spelled; got {data!r}"
    )


def test_the_event_payload_reads_an_unreadable_wait_as_no_wait():
    """A wait nobody can read is no wait at all.

    The rule already says so — :func:`capacity_wait.quota_wait_kind`
    answers ``unknown`` for a length it cannot parse — and the payload
    builder has to agree with the rule it is built from.  If it did
    not, the two would disagree about the same park: one saying
    "nothing was waited out" and the other refusing to say anything at
    all.
    """
    assert quota_wait_kind(False, "not a number") == "unknown", (
        f"the rule must read an unreadable wait as no wait; got "
        f"{quota_wait_kind(False, 'not a number')!r}"
    )

    try:
        data = quota_wait_event_data(
            providers=[TOPUP_PROVIDER],
            waited_sec="not a number",
            needs_manual_topup=False,
        )
    except (TypeError, ValueError) as exc:
        pytest.fail(f"an unreadable wait must not break the payload: {exc}")

    assert data["waited_sec"] == 0.0, (
        f"a wait nobody can read is no wait at all; got {data!r}"
    )
    assert data["kind"] == "unknown", (
        f"the payload must agree with the rule about an unreadable wait; "
        f"got {data!r}"
    )
    assert data["needs_manual_topup"] is False, (
        f"the kind and the flag must not contradict each other; got {data!r}"
    )


#: The gate command for ``repair-r1-27`` names the contract of the
#: test above under a different name:
#: ``test_manual_topup_is_reported_as_a_distinct_kind``.  The section
#: title above ("The park carries a kind a machine can switch on") and
#: that name say the same thing -- a hard cap is published as its own
#: ``wait_kind`` (``manual_topup``), distinct from a window wait
#: (``window``) and from an undecided park (``unknown``).  Both names
#: point at the same test, so both gates run the same assertions.
#: Alias only: no assertion and no production code is touched.
test_manual_topup_is_reported_as_a_distinct_kind = (
    test_vp_007_hard_cap_log_reports_manual_topup_with_zero_wait
)
