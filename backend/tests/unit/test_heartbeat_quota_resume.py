"""The heartbeat drives the quota resume sweep.

Why the heartbeat
-----------------
R2 asks for the parked work to go back out by itself within two
minutes.  The only thing on this server that runs every thirty seconds
is the ``HeartbeatMonitor``, so the sweep has to hang off it — the
requirement is not "the resumer exists", it is "the resumer runs".

Why *outside* ``_check_once``
-----------------------------
``_check_once`` returns immediately when the three registries are
empty, and a server whose only outstanding work is a *parked* plan is
exactly that server: nothing executing, nothing verifying, no
sub-agents — one plan sitting in the table waiting for the quota.  A
sweep mounted inside ``_check_once`` would therefore never run on the
machine that needs it most.  So it hangs off ``_tick``, next to the
human-in-the-loop expiry sweep, which is mounted there for the same
reason.

What is pinned here
-------------------
1. **A tick sweeps, even on an idle server.**  The registries are
   empty and the sweep still runs — once.
2. **A sweep that raises does not take the tick with it.**  Neither
   the liveness sweep nor the HITL expiry sweep may be skipped
   because the resume sweep failed, and the failure has to be logged
   (a silent failure is indistinguishable from "nothing was parked").
3. **``_check_once`` keeps its early exit.**  Mounting the sweep on
   ``_tick`` must not weaken the cost optimisation that an idle
   server walks no plan at all.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import quota_resume  # noqa: E402
import server  # noqa: E402
from server import HeartbeatMonitor  # noqa: E402


@pytest.fixture(autouse=True)
def _idle_server(monkeypatch):
    """Put the server in the state the sweep has to work in.

    The resume sweep must not depend on a plan being in flight, so
    every case starts from an idle server: nothing executing, nothing
    verifying, no sub-agents.

    The HITL expiry sweep is stubbed out: it writes to the state
    database, and the sweep under test here is the *resume* sweep.  A
    case that wants to observe the expiry sweep stubs it itself.
    """
    monkeypatch.setattr(server, "_execution_state", {})
    monkeypatch.setattr(server, "_verification_state", {})
    for plan_id in list(server.sub_agent_registry._by_plan.keys()):
        server.sub_agent_registry.cleanup_for_plan(plan_id)
    monkeypatch.setattr(server, "expire_due", Mock(return_value=0))
    yield


def _monitor() -> HeartbeatMonitor:
    """A monitor that will not tick on its own.

    The interval is huge on purpose: the cases drive the ticks, so a
    monitor that also ticked by itself would turn the assertions into
    a race.
    """
    return HeartbeatMonitor(interval=999.0)


# ---------------------------------------------------------------------------
# 1 — a tick sweeps, even on an idle server
# ---------------------------------------------------------------------------


def test_tick_runs_resume_sweep_when_registries_empty(monkeypatch):
    """The sweep runs on a tick whose registries are all empty.

    This is the case that decides where the sweep is mounted.  A server
    with one parked plan and nothing else in flight has empty
    registries — so a sweep mounted inside ``_check_once`` would never
    run, and the parked plan would wait forever.
    """
    sweep = Mock(return_value=[])
    monkeypatch.setattr(quota_resume, "run_resume_sweep", sweep)

    assert server._execution_state == {}
    assert server._verification_state == {}
    assert server.sub_agent_registry._by_plan == {}

    _monitor()._tick()

    assert sweep.call_count == 1, (
        "the resume sweep must run on every tick, including on a server "
        f"with nothing in flight; got {sweep.call_count} call(s)"
    )


# ---------------------------------------------------------------------------
# 2 — a failing sweep does not take the tick with it
# ---------------------------------------------------------------------------


def test_tick_isolates_sweep_failure(monkeypatch, caplog):
    """A sweep that raises is logged, and the rest of the tick still runs.

    The sweep touches a database another thread may be writing to, so
    it can fail.  What it must not do is take the monitor with it: the
    liveness sweep and the HITL expiry sweep both have work of their
    own, and a tick that stopped at the failure would leave every
    later decision waiting forever — silently, because the loop
    swallows whatever the tick raises.
    """
    monkeypatch.setattr(
        quota_resume,
        "run_resume_sweep",
        Mock(side_effect=RuntimeError("the state database is locked")),
    )

    check_once = Mock()
    expire_due = Mock(return_value=0)
    monkeypatch.setattr(server, "expire_due", expire_due)
    monitor = _monitor()
    monkeypatch.setattr(monitor, "_check_once", check_once)

    with caplog.at_level(logging.ERROR, logger="server"):
        monitor._tick()  # must not raise

    assert check_once.call_count == 1, (
        "the liveness sweep must still run — the resume sweep's failure "
        "must not skip it"
    )
    assert expire_due.call_count == 1, (
        "the HITL expiry sweep must still run — the resume sweep's "
        "failure must not skip it"
    )
    failures = [
        record
        for record in caplog.records
        if record.name == "server" and record.levelno >= logging.ERROR
    ]
    assert failures, (
        "the failure must be logged — a silent failure is "
        "indistinguishable from a sweep that found nothing to do; "
        f"captured {[r.getMessage() for r in caplog.records]!r}"
    )
    assert any(
        "quota resume sweep failed" in record.getMessage()
        for record in failures
    ), (
        "the log record must say which sweep failed; got "
        f"{[r.getMessage() for r in failures]!r}"
    )
    assert any(record.exc_info for record in failures), (
        "the log record must carry the traceback — the reason the sweep "
        "failed is the thing an operator has to read; got "
        f"{[(r.getMessage(), r.exc_info) for r in failures]!r}"
    )
    assert any(
        record.exc_info is not None and "locked" in str(record.exc_info[1])
        for record in failures
    ), (
        "the logged traceback must name the cause of the failure"
    )


# ---------------------------------------------------------------------------
# 3 — the liveness sweep keeps its early exit
# ---------------------------------------------------------------------------


def test_check_once_early_exit_unchanged(monkeypatch):
    """With all three registries empty, ``_check_once`` walks no plan.

    The early exit is a deliberate cost optimisation — an idle server
    must not take three registry locks every thirty seconds — and
    mounting the resume sweep next to it must not weaken it.
    """
    execution_check = Mock()
    verification_check = Mock()
    sub_agent_check = Mock()
    monkeypatch.setattr(server, "_lazy_check_execution", execution_check)
    monkeypatch.setattr(server, "_lazy_check_verification", verification_check)
    monkeypatch.setattr(server, "_lazy_check_sub_agents", sub_agent_check)

    assert server._execution_state == {}
    assert server._verification_state == {}
    assert server.sub_agent_registry._by_plan == {}

    _monitor()._check_once()

    assert execution_check.call_count == 0
    assert verification_check.call_count == 0
    assert sub_agent_check.call_count == 0
