"""The heartbeat drives the HITL expiry sweep.

Background
----------
A human-in-the-loop decision is a question the backend cannot answer
by itself.  An ``auto`` question has a way to end without a human:
when its deadline passes, the stored ``default_option`` becomes the
answer and the decision ends ``defaulted``.

*How* that happens is ``hitl.service.expire_due``'s business — it
knows which rows are overdue and what a defaulted decision looks like.
*When* it happens is somebody else's business, and in this backend the
thing that runs every 30 seconds is the ``HeartbeatMonitor``.  Before
this file existed the two were never joined: the deadline could pass
and the decision could sit ``pending`` until something else happened
to look at it.

What is pinned here
-------------------
1. **A tick resolves an overdue question.**  One tick of the monitor is
   enough: the decision ends ``defaulted`` and its answer is the
   ``default_option`` it was created with.
2. **A failing sweep does not take the monitor down.**  The sweep is
   allowed to fail — the database is locked, the disk is unhappy — but
   the failure is logged and the *next* tick still runs.  A monitor
   that died on its first expiry would leave every later decision
   waiting forever, and it would do so silently.
3. **A sweep with nothing to do writes nothing.**  An idle server
   ticks every 30 seconds; a sweep that resolved nothing must leave no
   trace behind.

Why the tick is called directly
--------------------------------
``HeartbeatMonitor._loop`` is a daemon loop that never returns.  What
is under test is one iteration of it, so the test calls the tick
itself: no thread, no sleeping, no wall-clock race.  The deadline is
put in the past by *asking* the question in the past, so the tick's
own clock — the wall clock — finds it overdue without anything having
to wait for it.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from config_paths import resolve_plans_dir, resolve_state_db_path  # noqa: E402
from hitl import service  # noqa: E402
from hitl.models import Decision, DecisionRequest, DecisionStatus  # noqa: E402
from hitl.store import HitlStore  # noqa: E402
from hooks import config_path, write_for_plan  # noqa: E402
from utils.atomic_io import atomic_write_json  # noqa: E402

PLAN_ID = "plan-heartbeat-expire"

#: How long the question waits.  Short enough that the deadline can be
#: put in the past without anybody waiting for it.
TIMEOUT_SEC = 60

#: The answer an overdue question falls back to.
DEFAULT_OPTION = "accept"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_dir() -> Path:
    """The plan's directory — where its ``hitl_config.json`` lives."""
    path = resolve_plans_dir() / PLAN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_config(plan_dir: Path, mode: str = "auto", timeout_sec: int = TIMEOUT_SEC) -> None:
    """Write the plan's HITL configuration, with somebody to ask.

    The mode and the deadline are written by the product's own writer,
    so they are the ones the product would store.  The outlet is then
    given a destination the plan's *origin* cannot express:
    ``write_for_plan`` only ever writes the ``cc_connect`` hook a
    session implies, and a question nobody can be asked is
    ``unroutable`` from the start — the sweep would have nothing to
    resolve.
    """
    document = write_for_plan(plan_dir, {"mode": mode, "timeout_sec": timeout_sec})
    document["hooks"] = [{"kind": "cmd", "command": "true"}]
    atomic_write_json(config_path(plan_dir), document, reraise=True)


def _create_overdue_decision() -> str:
    """A pending ``auto`` question whose deadline has already passed.

    The question is *asked* one timeout in the past, so its deadline —
    one timeout after that — is already behind the wall clock the tick
    reads.  Nothing has to sleep for this question to be overdue.
    """
    plan_dir = _plan_dir()
    _write_config(plan_dir)
    request = DecisionRequest(
        plan_id=PLAN_ID,
        source="execution",
        task_id="T-1",
        question="Ship the change as written?",
        options=["accept", "revise"],
        default_option=DEFAULT_OPTION,
    )
    asked_at = datetime.now(timezone.utc) - timedelta(seconds=TIMEOUT_SEC + 1)
    with HitlStore(resolve_state_db_path()) as store:
        created = service.create_decision(
            request, plan_dir, store=store, now=asked_at
        )
    return created.decision_id


def _stored(decision_id: str) -> Decision:
    """The stored form of ``decision_id``, read fresh."""
    with HitlStore(resolve_state_db_path()) as store:
        decision = store.get(decision_id)
    assert decision is not None, f"decision {decision_id!r} is not stored"
    return decision


def _monitor() -> "server.HeartbeatMonitor":
    """A monitor that will not tick on its own.

    The interval is huge on purpose: the test drives the ticks, so a
    monitor that also ticks by itself would make the assertions a race.
    """
    return server.HeartbeatMonitor(interval=999.0)


# ---------------------------------------------------------------------------
# 1 — a tick resolves the overdue question
# ---------------------------------------------------------------------------


def test_tick_expires_due() -> None:
    """One tick ends the overdue question with its default answer.

    The deadline is the whole point of the sweep: a question nobody
    answered resolves itself, and the answer it resolves to is the
    ``default_option`` that was stored with it — not an empty answer
    and not somebody else's answer.
    """
    decision_id = _create_overdue_decision()
    assert _stored(decision_id).status == DecisionStatus.PENDING.value

    _monitor()._tick()

    decision = _stored(decision_id)
    assert decision.status == DecisionStatus.DEFAULTED.value, (
        "the tick did not resolve the overdue question — its deadline "
        "passed, so the question must have ended by itself"
    )
    assert decision.answer == DEFAULT_OPTION, (
        "a defaulted decision carries the default_option it was created "
        "with"
    )


# ---------------------------------------------------------------------------
# 2 — a failing sweep does not take the monitor down
# ---------------------------------------------------------------------------


def test_tick_survives_error(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """The sweep may fail; the tick after it still runs.

    The sweep touches a database that another thread may be writing
    to, so it can fail.  What it must not do is take the monitor with
    it: a heartbeat that died on its first expiry would leave every
    later decision waiting forever, and the failure that killed it
    would have been swallowed.
    """
    decision_id = _create_overdue_decision()

    real_expire_due = server.expire_due
    calls: list[object] = []

    def flaky_expire_due(now=None, **kwargs):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("the state database is locked")
        return real_expire_due(now=now, **kwargs)

    monkeypatch.setattr(server, "expire_due", flaky_expire_due)
    monitor = _monitor()

    with caplog.at_level(logging.WARNING, logger="server"):
        monitor._tick()

    assert len(calls) == 1, "the tick did not run the sweep at all"
    assert _stored(decision_id).status == DecisionStatus.PENDING.value, (
        "a sweep that failed must not resolve anything"
    )
    assert any(
        "locked" in record.getMessage() for record in caplog.records
    ), (
        "the failure must be logged — a silent failure is "
        "indistinguishable from a sweep that found nothing to do"
    )

    # The monitor is still alive: the next tick runs the sweep again.
    monitor._tick()

    assert len(calls) == 2, "the monitor must keep ticking after a failed sweep"
    assert _stored(decision_id).status == DecisionStatus.DEFAULTED.value, (
        "the tick after the failure must still resolve the overdue "
        "question — the failed sweep did not take the monitor down"
    )


# ---------------------------------------------------------------------------
# 3 — a sweep with nothing to do writes nothing
# ---------------------------------------------------------------------------


def test_tick_without_a_decision_writes_nothing() -> None:
    """An idle server ticks every 30 seconds; that must leave no trace.

    There is no question to resolve, so nothing is resolved: no row
    changes and no audit line is written.  A sweep that wrote a line
    per tick would fill the mirror of every plan with noise.
    """
    plan_dir = _plan_dir()
    write_for_plan(plan_dir, {"mode": "auto", "timeout_sec": TIMEOUT_SEC})

    _monitor()._tick()

    mirror = plan_dir / "hitl_decisions.jsonl"
    assert not mirror.exists(), (
        "a sweep that resolved nothing must not leave an audit line "
        "behind"
    )
