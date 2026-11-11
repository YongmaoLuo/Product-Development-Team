"""The round's verdict must reach ``plan_verification`` — the
orchestrator's own ``verification_failed`` spelling included.

Background
----------
``/api/plan/{id}/status`` reads the plan's top-level
``verification_status`` from the ``plan_verification`` column, and the
auto-loop writes that column when a round ends.  The recorder accepted
exactly three spellings — ``passed`` / ``failed`` / ``loop_stopped`` —
and returned SILENTLY for anything else.

But the orchestrator does not speak that vocabulary.  Its failure
verdict is ``verification_failed``
(``backend/verification/orchestrator.py``), and the auto-loop forwards
the orchestrator's status verbatim::

    status = result.get("status", "failed")    # -> "verification_failed"
    ...
    _record_round_verdict(status, stop_reason, result)

So every "failed -> dispatch the repairs" round was handed a spelling
the recorder did not know.  The write was dropped, no log line said so,
and the row kept the ``running`` value stamped at round start: the plan
looked like it was still verifying for the whole round, and every
repair round's verdict left the plan behind.

Contract pinned here
--------------------
* ``verification_failed`` is persisted as ``failed`` — the alias is the
  fix — and the row's ``verification_stop_reason`` is the round's own
  reason, verbatim.
* An UNKNOWN status is refused LOUDLY: a WARNING naming the plan, the
  status and the round, and no write at all.  (Before the fix an
  unknown status was as silent as ``running``; a spelling that nobody
  can read must not look like a spelling nobody ever produced.)
* ``running`` is not a verdict: a silent no-op — no write, no log line.
  It is the status a round carries while it is still working.
* A round that was declared dead still refuses to write.  The column
  belongs to the PLAN; a round the watchdog wrote off does not get to
  speak for it.
* The three statuses that already worked still work, unchanged.

Hermeticity
------------
Everything happens inside ``tmp_path``: the plan directory, the project
directory and the state database.  ``PDT_STATE_DB_PATH`` is the seam
``server._state_db_path`` honours, so no write can escape to the
operator's live database.
"""

from __future__ import annotations

import inspect
import logging
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server``
# imports back, so importing the inner one first trips the cycle.
import server as srv  # noqa: E402
import verification_loop as vl  # noqa: E402
import zombie_rounds  # noqa: E402


#: The token of the round that is writing its verdict.
LIVE_TOKEN = "verification_loop-1-ab"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def hermetic_server(tmp_path, monkeypatch):
    """A hermetic plans root + ``state.db``."""
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    db_path = tmp_path / "state.db"

    # The env var is the seam ``server._state_db_path`` honours, so the
    # verdict write cannot escape to the operator's live database.
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(srv, "PLANS_DIR", plans_root)
    monkeypatch.setattr(srv, "_state_db_path", lambda *a, **k: db_path)

    before_state = dict(srv._verification_state)
    before_exec = dict(srv._execution_state)
    yield srv, plans_root, db_path
    srv._verification_state.clear()
    srv._verification_state.update(before_state)
    srv._execution_state.clear()
    srv._execution_state.update(before_exec)


def _seed_running_verification(
    db_path: Path, plan_id: str, plans_root: Path,
) -> None:
    """Seed ``plan_verification`` mid-round — the ``running`` value the
    round start stamped."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(plan_id, "running")
        repo.init_round(plan_id, round_n=1, max_rounds=3)
    finally:
        conn.close()

    (plans_root / plan_id).mkdir(parents=True, exist_ok=True)


def _row(db_path: Path, plan_id: str) -> dict:
    """The plan's ``plan_verification`` row, as the API reads it."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db_path)
    try:
        return VerificationRepository(conn).current(plan_id) or {}
    finally:
        conn.close()


def _warnings(caplog) -> list:
    """The WARNING-and-above records the write produced."""
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# 1. The orchestrator's own failure spelling is persisted
# ---------------------------------------------------------------------------


def test_verification_failed_is_persisted_as_failed(hermetic_server):
    """``verification_failed`` is the orchestrator's failure verdict.

    It used to be dropped on the floor, silently, and the row kept its
    ``running`` value: the plan looked like it was still verifying for
    the whole round, and the round's verdict was lost.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "round-verdict-verification-failed"
    _seed_running_verification(db_path, plan_id, plans_root)

    assert _row(db_path, plan_id)["verification_status"] == "running"

    vl._record_round_verdict_persist(
        plan_id,
        LIVE_TOKEN,
        "verification_failed",
        "same_failure_repeated_after_max_attempts",
        {"results": {}},
    )

    row = _row(db_path, plan_id)
    assert row["verification_status"] == "failed", (
        "the orchestrator's failure verdict was dropped — the row kept "
        "the 'running' value stamped at round start, so the plan looked "
        "like it was still verifying"
    )
    assert row["verification_stop_reason"] == (
        "same_failure_repeated_after_max_attempts"
    ), (
        "the round's own stop reason was not written — the verdict says "
        "nothing about WHY the round ended"
    )


def test_verification_failed_writes_the_normalised_status_in_the_envelope(
    hermetic_server,
):
    """The envelope names the same status the column does.

    ``results.status`` is what ``repair_stale_terminal_state`` reads
    when it has to re-derive the column from the envelope, so the two
    must not disagree.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "round-verdict-envelope"
    _seed_running_verification(db_path, plan_id, plans_root)

    vl._record_round_verdict_persist(
        plan_id, LIVE_TOKEN, "verification_failed", "repair_exhausted",
        {"results": {}},
    )

    envelope = _row(db_path, plan_id).get("results") or {}
    assert envelope.get("status") == "failed", (
        "the envelope names a status the column does not — the two "
        "disagree about what the round's verdict was"
    )


# ---------------------------------------------------------------------------
# 2. An unknown status is refused loudly
# ---------------------------------------------------------------------------


def test_unknown_status_logs_warning_and_writes_nothing(
    hermetic_server, caplog,
):
    """A spelling nobody can read must not look like one nobody used.

    The old guard returned silently for everything outside the three
    known statuses, so a typo in a status and a status that means
    "still working" were indistinguishable in the log.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "round-verdict-unknown"
    _seed_running_verification(db_path, plan_id, plans_root)

    with caplog.at_level(logging.WARNING, logger="server"):
        vl._record_round_verdict_persist(
            plan_id, LIVE_TOKEN, "weird", "some_reason", {"results": {}},
        )

    warnings = _warnings(caplog)
    assert warnings, (
        "an unknown status was refused silently — the log cannot tell a "
        "typo apart from a status that was never produced"
    )
    message = warnings[-1].getMessage()
    assert plan_id in message, message
    assert "weird" in message, message
    assert LIVE_TOKEN in message, message

    assert _row(db_path, plan_id)["verification_status"] == "running", (
        "an unknown status was written to plan_verification — the column "
        "now holds a value nothing else in the codebase knows how to read"
    )


# ---------------------------------------------------------------------------
# 3. "running" is not a verdict
# ---------------------------------------------------------------------------


def test_running_is_silent_noop(hermetic_server, caplog):
    """``running`` is what a live round carries.

    It is not a verdict, so it is not written — but it is also not an
    anomaly, so it says nothing.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "round-verdict-running"
    _seed_running_verification(db_path, plan_id, plans_root)

    with caplog.at_level(logging.WARNING, logger="server"):
        vl._record_round_verdict_persist(
            plan_id, LIVE_TOKEN, "running", None, None,
        )

    assert _warnings(caplog) == [], (
        "a round that is still running produced a WARNING — 'running' is "
        "not an anomaly"
    )
    assert _row(db_path, plan_id)["verification_status"] == "running", (
        "the 'running' status was written as a verdict"
    )


# ---------------------------------------------------------------------------
# 4. A dead round still does not write
# ---------------------------------------------------------------------------


def test_invalidated_round_is_refused(hermetic_server):
    """The column belongs to the PLAN.

    A round the watchdog declared dead may still be running: its LLM
    call returns, its code carries on, and it reaches the recorder with
    a verdict.  The plan has already moved past that round, so the
    write is not the plan's to accept.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "round-verdict-dead"
    _seed_running_verification(db_path, plan_id, plans_root)

    zombie_rounds.invalidate(
        plans_root / plan_id,
        LIVE_TOKEN,
        reason="verification_log_stale",
        actor="verification_watchdog",
    )

    vl._record_round_verdict_persist(
        plan_id, LIVE_TOKEN, "failed", "repair_exhausted", {"results": {}},
    )

    assert _row(db_path, plan_id)["verification_status"] == "running", (
        "a round that was declared dead wrote its verdict into "
        "plan_verification — the plan now reports a verdict for a round "
        "it has already written off"
    )


# ---------------------------------------------------------------------------
# 5. The statuses that already worked still work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["passed", "failed", "loop_stopped"])
def test_existing_verdicts_unchanged(hermetic_server, status):
    """The three known spellings are untouched by the alias."""
    server, plans_root, db_path = hermetic_server
    plan_id = f"round-verdict-{status}"
    _seed_running_verification(db_path, plan_id, plans_root)

    vl._record_round_verdict_persist(
        plan_id, LIVE_TOKEN, status, f"reason_{status}", {"results": {}},
    )

    row = _row(db_path, plan_id)
    assert row["verification_status"] == status
    assert row["verification_stop_reason"] == f"reason_{status}"


# ---------------------------------------------------------------------------
# 6. The loop's own recorder reaches the module-level one
# ---------------------------------------------------------------------------


def test_the_loop_delegates_to_the_module_level_recorder():
    """The auto-loop's closure must CALL the module-level recorder.

    Without this, a module-level recorder that nobody calls would pass
    every test above: the bug lives in the closure's body, so the
    closure has to be shown handing its status over.
    """
    source = inspect.getsource(vl._run_auto_verification_loop_inner)
    assert "def _record_round_verdict(" in source, (
        "the auto-loop no longer defines its own round-verdict recorder"
    )
    nested = source.split("def _record_round_verdict(", 1)[1]
    body = nested.split("\n    def ", 1)[0]
    assert "_record_round_verdict_persist(" in body, (
        "the auto-loop's recorder does not delegate to the module-level "
        "one — the alias never reaches the write"
    )
    assert 'status not in {"passed", "failed", "loop_stopped"}' not in body, (
        "the auto-loop's recorder still carries its own status guard — "
        "the alias cannot take effect while that guard is in the way"
    )
