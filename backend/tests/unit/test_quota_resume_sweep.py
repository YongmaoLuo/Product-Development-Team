"""The resume sweep — one pass over the parked plans.

What the sweep is
-----------------
Requirement R2 says the parked work goes back out by itself within two
minutes; R3 says one plan runs once; R4 says every automatic revival
writes a line.  Those three are properties of a *sweep* — a single pass
that asks "who is parked?", asks "is the quota window open again?", and
then puts the answers together.  This file pins that pass.

The order of the two questions is the load-bearing part
-------------------------------------------------------
**The table is read first, and the probe is sent second.**  A sweep that
probed first would ask the provider quota endpoint on every heartbeat
tick — thirty seconds apart, all night, on a machine with nothing parked
at all.  That is a real cost (a request per tick) paid for no possible
benefit: with nothing parked there is nothing the reading could revive.
So the sweep reads the parked table, and only a table with rows in it
licenses a probe.

The four decisions the sweep makes
---------------------------------
1. **Nothing parked → nothing happens, and no probe is sent.**  The
   probe is not merely unused in that case — it is not *called*, and
   the case below pins that by handing the sweep a probe that records
   its own calls.
2. **The quota has to be back before anything is started.**  A reading
   that is empty, or one where every provider is still exhausted, is
   the answer "the window is still shut" — and the answer is "start
   nothing".  This is the negative control: it is what makes the
   "quota recovered" case mean something.
3. **One plan cannot block the next.**  The caller is a heartbeat
   thread; an exception raised out of one plan's revival would stop the
   sweep for *every* plan.  So a failure is absorbed, logged, and the
   sweep moves on.
4. **A plan is not retried on every tick.**  R2 asks for a revival
   within two minutes; a plan whose revival is *refused* would
   otherwise be retried every thirty seconds, writing a line every
   thirty seconds, forever.  So a plan that was attempted recently is
   left alone until the retry window has passed.

The two log lines
-----------------
R4 fixes the shape of the *success* line::

    quota_recovered_resume plan=<id> round=<n> reason=quota_recovered

That line is what an operator reads to answer "did the automatic
revival happen, and for which round?".  A plan whose revival was
*refused* is a different event, and it gets a different line — one that
says so — because a sweep that reported a refusal in the success
vocabulary would claim a revival that did not happen.

The probe is injected (``probe=``), the clock is injected (``now=``) and
the revival callers are injected (``resume_execution_fn=`` /
``resume_verification_fn=``), so that the contract can be pinned
without a server, a database or a plan directory.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import quota_resume  # noqa: E402
from quota_resume import ParkedPlan  # noqa: E402
from state_machine.db import schema as _schema  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

#: The stamp the seeded rows carry.  A constant, because nothing in this
#: criterion reads a timestamp — and a constant keeps the seed honest.
_STAMP = "2026-11-15T00:00:00Z"


def _state_db() -> sqlite3.Connection:
    """An in-memory state database carrying the *real* schema.

    The real schema is applied on purpose: a hand-written
    ``CREATE TABLE plan_execution (plan_id, exec_status)`` would let
    the sweep drift away from the columns the product actually writes,
    and the drift would only show up in production.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    _schema.migrate(conn)
    return conn


def _seed_execution(
    conn: sqlite3.Connection,
    plan_id: str,
    exec_status: str = "waiting_for_quota",
    stop_reason: str = "waiting_for_quota",
) -> None:
    """Write one ``plan_execution`` row."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_execution "
        "(plan_id, exec_status, stop_reason, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (plan_id, exec_status, stop_reason, _STAMP),
    )


def _seed_verification(
    conn: sqlite3.Connection,
    plan_id: str,
    verification_status: str = "waiting_for_quota",
    verification_stop_reason: str = "waiting_for_quota",
    round: int = 2,
) -> None:
    """Write one ``plan_verification`` row."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_verification "
        "(plan_id, verification_status, round, verification_stop_reason, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (plan_id, verification_status, round, verification_stop_reason, _STAMP),
    )


def _open_window():
    """A provider whose quota window is open again."""
    return SimpleNamespace(exhausted=False)


def _closed_window():
    """A provider whose quota window is still shut."""
    return SimpleNamespace(exhausted=True)


@pytest.fixture(autouse=True)
def _clear_the_retry_ledger():
    """The retry ledger is module state; every case starts with it empty.

    Without this, a case that leaves an attempt behind would put the
    next case's plan into its cooldown and the two cases would depend
    on each other — the failure mode this file is supposed to be
    pinning, not reproducing.
    """
    quota_resume._LAST_ATTEMPT.clear()
    yield
    quota_resume._LAST_ATTEMPT.clear()


# ---------------------------------------------------------------------------
# Nothing parked: no probe is sent
# ---------------------------------------------------------------------------


def test_sweep_skips_probe_when_nothing_parked():
    """An empty table means the sweep never asks the quota endpoint.

    This is the case the background of R2 warns about: a heartbeat that
    probes on every tick drags a request to the provider endpoint every
    thirty seconds, forever, on a machine that has nothing to revive.
    So the sweep reads the table *first*, and the probe is called only
    when the table had rows.

    The probe here records its own calls, so "not called" is an
    observation rather than an inference from the empty result.
    """
    conn = _state_db()
    calls: list = []

    def probe():
        calls.append("probed")
        return {"p1": _open_window()}

    assert quota_resume.run_resume_sweep(conn=conn, probe=probe, now=1000.0) == []
    assert calls == []


# ---------------------------------------------------------------------------
# The revival happens, and it is written down
# ---------------------------------------------------------------------------


def test_sweep_resumes_and_logs(caplog):
    """A parked plan with the window open is revived, and the line is written.

    R4 fixes the shape of the line an operator reads: which plan, which
    round, and why.  All three fields are asserted here — a line that
    carried the plan but not the round would answer "was it revived?"
    without answering "which round?", and the round is exactly what
    tells two revivals of the same plan apart.
    """
    conn = _state_db()
    _seed_execution(conn, "p9")
    revived: list = []

    def resume_execution_fn(plan: ParkedPlan) -> bool:
        revived.append(plan.plan_id)
        return True

    with caplog.at_level(logging.INFO, logger="quota_resume"):
        resumed = quota_resume.run_resume_sweep(
            conn=conn,
            probe=lambda: {"p1": _open_window()},
            now=1000.0,
            resume_execution_fn=resume_execution_fn,
        )

    assert resumed == ["p9"]
    assert revived == ["p9"]
    assert any(
        "quota_recovered_resume plan=p9 round=0 reason=quota_recovered"
        in record.getMessage()
        for record in caplog.records
    )


def test_sweep_resumes_a_parked_verification_round(caplog):
    """A parked verification round is revived through the verification caller.

    The two kinds are not interchangeable: a revival has to go out
    through the start path that owns that kind of work, and the line
    R4 asks for carries the round the revival continued.  So the round
    in the line is the verification's own round, not the execution's
    zero.
    """
    conn = _state_db()
    _seed_verification(conn, "p8", round=3)
    revived: list = []

    def resume_verification_fn(plan: ParkedPlan) -> bool:
        revived.append((plan.plan_id, plan.round))
        return True

    with caplog.at_level(logging.INFO, logger="quota_resume"):
        resumed = quota_resume.run_resume_sweep(
            conn=conn,
            probe=lambda: {"p1": _open_window()},
            now=1000.0,
            resume_verification_fn=resume_verification_fn,
        )

    assert resumed == ["p8"]
    assert revived == [("p8", 3)]
    assert any(
        "quota_recovered_resume plan=p8 round=3 reason=quota_recovered"
        in record.getMessage()
        for record in caplog.records
    )


def test_sweep_revives_only_the_execution_when_both_tables_are_parked():
    """A plan parked in both tables is revived once — as the execution.

    The two tables park different work: the execution and the
    verification round.  Reviving both at once would put two start
    paths on one plan at the same moment, and the verification would
    start against an execution that is still being written.  So the
    execution goes first, and the verification round is picked up by
    the next sweep, when the execution is no longer parked.
    """
    conn = _state_db()
    _seed_execution(conn, "p9")
    _seed_verification(conn, "p9", round=2)
    executions: list = []
    verifications: list = []

    resumed = quota_resume.run_resume_sweep(
        conn=conn,
        probe=lambda: {"p1": _open_window()},
        now=1000.0,
        resume_execution_fn=lambda plan: executions.append(plan.plan_id) or True,
        resume_verification_fn=lambda plan: verifications.append(plan.plan_id) or True,
    )

    assert resumed == ["p9"]
    assert executions == ["p9"]
    assert verifications == []


# ---------------------------------------------------------------------------
# The negative control: the window is still shut
# ---------------------------------------------------------------------------


def test_sweep_gated_by_quota_recovery():
    """A reading in which every provider is exhausted starts nothing.

    This is the negative control for the whole criterion.  Without it,
    "the sweep revives a parked plan" would be a statement about the
    sweep's wiring rather than about its *gate*: the sweep has to
    notice that the window is still shut, and the answer to that
    question has to be "start nothing".

    The empty reading is the other half of the same answer, and it is
    checked here too — an empty reading is not a recovery.
    """
    conn = _state_db()
    _seed_execution(conn, "p9")
    revived: list = []

    def resume_execution_fn(plan: ParkedPlan) -> bool:
        revived.append(plan.plan_id)
        return True

    assert (
        quota_resume.run_resume_sweep(
            conn=conn,
            probe=lambda: {"p1": _closed_window()},
            now=1000.0,
            resume_execution_fn=resume_execution_fn,
        )
        == []
    )
    assert revived == []

    assert (
        quota_resume.run_resume_sweep(
            conn=conn,
            probe=lambda: {},
            now=1000.0,
            resume_execution_fn=resume_execution_fn,
        )
        == []
    )
    assert revived == []


# ---------------------------------------------------------------------------
# The retry window
# ---------------------------------------------------------------------------


def test_sweep_respects_retry_cooldown():
    """A plan attempted inside the retry window is left alone.

    R2 asks for a revival within two minutes; the sweep runs every
    thirty seconds.  Without a retry window, a plan whose revival is
    refused would be retried on every single tick and would write a
    line on every single tick — the log becomes noise and the noise
    hides the one line that matters.

    So the second pass over the same plan, at the same moment, does not
    call the revival caller at all.
    """
    conn = _state_db()
    _seed_execution(conn, "p9")
    revived: list = []

    def resume_execution_fn(plan: ParkedPlan) -> bool:
        revived.append(plan.plan_id)
        return True

    first = quota_resume.run_resume_sweep(
        conn=conn,
        probe=lambda: {"p1": _open_window()},
        now=1000.0,
        resume_execution_fn=resume_execution_fn,
    )
    second = quota_resume.run_resume_sweep(
        conn=conn,
        probe=lambda: {"p1": _open_window()},
        now=1000.0,
        resume_execution_fn=resume_execution_fn,
    )

    assert first == ["p9"]
    assert second == []
    assert revived == ["p9"]


def test_sweep_retries_after_the_retry_window():
    """Past the retry window the plan is tried again.

    The other half of the cooldown: a window that never opened would
    strand the plan forever, and R2 would never be satisfied.  So the
    window has an end, and past it the sweep tries again.
    """
    conn = _state_db()
    _seed_execution(conn, "p9")
    revived: list = []

    def resume_execution_fn(plan: ParkedPlan) -> bool:
        revived.append(plan.plan_id)
        return True

    quota_resume.run_resume_sweep(
        conn=conn,
        probe=lambda: {"p1": _open_window()},
        now=1000.0,
        resume_execution_fn=resume_execution_fn,
    )
    later = quota_resume.run_resume_sweep(
        conn=conn,
        probe=lambda: {"p1": _open_window()},
        now=1000.0 + quota_resume.MIN_RETRY_SEC,
        resume_execution_fn=resume_execution_fn,
    )

    assert later == ["p9"]
    assert revived == ["p9", "p9"]


# ---------------------------------------------------------------------------
# One plan cannot block the next
# ---------------------------------------------------------------------------


def test_sweep_isolates_one_failure(caplog):
    """A plan whose revival blows up does not stop the next plan.

    The caller is a heartbeat thread that sweeps every parked plan.  An
    exception raised out of one plan's revival would unwind that thread
    and stop the sweep for *every* plan — one broken plan would keep
    the whole fleet parked.

    So the failure is absorbed and logged, and the sweep carries on to
    the next plan.
    """
    conn = _state_db()
    _seed_execution(conn, "p1")
    _seed_execution(conn, "p2")

    def resume_execution_fn(plan: ParkedPlan) -> bool:
        if plan.plan_id == "p1":
            raise RuntimeError("the executor could not be spawned")
        return True

    with caplog.at_level(logging.ERROR, logger="quota_resume"):
        resumed = quota_resume.run_resume_sweep(
            conn=conn,
            probe=lambda: {"p1": _open_window()},
            now=1000.0,
            resume_execution_fn=resume_execution_fn,
        )

    assert resumed == ["p2"]
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


# ---------------------------------------------------------------------------
# A refused revival is not a revival
# ---------------------------------------------------------------------------


def test_sweep_writes_a_warning_when_the_revival_is_refused(caplog):
    """A refused revival is reported as a refusal, in its own vocabulary.

    The success line says a revival happened.  A start path that
    refuses the plan (it is already running, it is archived, it has no
    usable working directory) means no revival happened — so it gets
    its own line, and the success line is not written for it.

    Mixing the two would be worse than either: an operator reading
    ``quota_recovered_resume`` would conclude the plan was running when
    it is not.
    """
    conn = _state_db()
    _seed_execution(conn, "p9")

    with caplog.at_level(logging.INFO, logger="quota_resume"):
        resumed = quota_resume.run_resume_sweep(
            conn=conn,
            probe=lambda: {"p1": _open_window()},
            now=1000.0,
            resume_execution_fn=lambda plan: False,
        )

    assert resumed == []
    messages = [record.getMessage() for record in caplog.records]
    assert any("quota_resume_skipped plan=p9 kind=execution" in line for line in messages)
    assert not any("quota_recovered_resume" in line for line in messages)


# ---------------------------------------------------------------------------
# The default probe
# ---------------------------------------------------------------------------


def test_default_probe_uses_the_shared_watchdog(monkeypatch):
    """Without a fixture, the probe is the shared quota watchdog's.

    The watchdog is the one place in the product that reads the
    provider quota payload, and it merges concurrent readers into one
    request.  A second reader would be a second opinion about the
    same window — and the window is a fact, not an opinion.

    The scene is named so that the probe is legible in the log next to
    the other reads of the same payload.
    """
    import quota_watchdog

    seen: list = []

    class _StubWatchdog:
        def probe(self, *, scene="", **kwargs):
            seen.append(scene)
            return {"p1": _open_window()}

    monkeypatch.setattr(quota_watchdog, "get_shared_quota_watchdog", lambda: _StubWatchdog())
    monkeypatch.delenv("PDT_QUOTA_FIXTURE", raising=False)

    reading = quota_resume._default_probe()

    assert seen == ["quota_resume_sweep"]
    assert quota_resume.quota_recovered(reading) is True


def test_default_probe_reads_the_fixture_file(monkeypatch, tmp_path):
    """With a fixture, the reading comes from the file on disk.

    The fixture exists so that an end-to-end case can *change* the
    quota reading — write the file, sweep, watch the plan start — with
    no provider endpoint involved.  The file says whether the window is
    open, and the sweep reads it the same way it reads any other
    reading.
    """
    path = tmp_path / "quota.json"
    path.write_text(json.dumps({"exhausted": False}), encoding="utf-8")
    monkeypatch.setenv("PDT_QUOTA_FIXTURE", str(path))

    reading = quota_resume._default_probe()

    assert isinstance(reading, dict)
    assert quota_resume.quota_recovered(reading) is True

    path.write_text(json.dumps({"exhausted": True}), encoding="utf-8")
    assert quota_resume.quota_recovered(quota_resume._default_probe()) is False


def test_default_probe_never_raises_on_a_bad_fixture(monkeypatch, tmp_path):
    """A broken fixture is an empty reading, not an exception.

    The sweep runs inside a heartbeat thread.  A raise here would kill
    that thread — and with it the revival of every parked plan — over a
    file that happens to be truncated.  So every way the file can be
    wrong answers the empty reading: no reading, no recovery, no
    revival, and the next tick asks again.
    """
    monkeypatch.setenv("PDT_QUOTA_FIXTURE", str(tmp_path / "absent.json"))
    assert quota_resume._default_probe() == {}

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json at all", encoding="utf-8")
    monkeypatch.setenv("PDT_QUOTA_FIXTURE", str(corrupt))
    assert quota_resume._default_probe() == {}

    not_an_object = tmp_path / "list.json"
    not_an_object.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    monkeypatch.setenv("PDT_QUOTA_FIXTURE", str(not_an_object))
    assert quota_resume._default_probe() == {}
