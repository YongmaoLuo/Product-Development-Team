"""The negative control for the round-verdict normalisation — executable.

Background
----------
``backend/tests/unit/test_round_verdict_persistence.py`` pins what the
recorder DOES with each status.  What no such suite can show on its own
is that the alias is what makes the difference: a test file that passes
whether or not ``_ROUND_VERDICT_STATUS_ALIASES`` exists proves nothing
about the alias.

This plan's acceptance rule is "every fix carries at least one negative
control", and until now that control was a sentence in a report.  This
file is that sentence, run.

The control is executable without touching production code, because the
two things it needs are already reachable:

* the alias table is a module-level dict, so ``monkeypatch.setattr`` can
  empty it — the recorder reads the global at call time, so the
  recorder sees the patched table;
* the recorder imports ``VerificationRepository`` INSIDE its body, at
  call time, so patching the attribute on the source module is exactly
  what the recorder sees.  No production seam is needed, and none was
  added.

With the alias removed, the very same call that used to write
``failed`` writes NOTHING — and says so in the log instead of dropping
the verdict on the floor.

Contract pinned here
--------------------
* ``verification_failed`` is persisted as ``failed``, and the row's
  ``verification_stop_reason`` is the round's own reason, verbatim.
* an unknown status is refused LOUDLY — a WARNING naming the plan, the
  status and the round — and writes nothing.
* ``running`` is not a verdict: a silent no-op, no write and no log line.
* a round that was declared dead refuses to write.
* the three statuses that already worked still work, unchanged.
* the alias table maps exactly one spelling, and REMOVING it stops the
  write — the negative control itself.

Hermeticity
------------
Everything happens inside ``tmp_path``: the plan directory, the project
directory and the state database.  ``PDT_STATE_DB_PATH`` is the seam
``server._state_db_path`` honours, so no write can escape to the
operator's live database.
"""

from __future__ import annotations

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

from state_machine.repositories import (  # noqa: E402
    verification_repository as verification_repository_module,
)


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


def _spy_repository(monkeypatch) -> list:
    """Record what the recorder asks the repository to write.

    The spy is a subclass of the REAL repository, so the write it
    performs is the real one: the row still ends up in the database.
    Only the call is observed on the way through.  That matters — a
    stand-in that swallowed the write could not tell "the recorder
    asked for the wrong status" apart from "the recorder asked for
    nothing at all", and the negative control is exactly about that
    difference.

    Patching the attribute on the SOURCE module is what the recorder
    sees, because the recorder imports the class inside its own body,
    at call time.
    """
    calls: list = []
    real = verification_repository_module.VerificationRepository

    class _SpyRepository(real):
        def complete_round(
            self,
            plan_id,
            results,
            status="failed",
            stop_reason=None,
            publish_closed=True,
        ):
            calls.append(
                {
                    "plan_id": plan_id,
                    "results": results,
                    "status": status,
                    "stop_reason": stop_reason,
                    "publish_closed": publish_closed,
                }
            )
            return super().complete_round(
                plan_id,
                results,
                status=status,
                stop_reason=stop_reason,
                publish_closed=publish_closed,
            )

    monkeypatch.setattr(
        verification_repository_module,
        "VerificationRepository",
        _SpyRepository,
    )
    return calls


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
# 6. The alias table says exactly one thing
# ---------------------------------------------------------------------------


def test_alias_table_maps_only_verification_failed():
    """The table maps the orchestrator's spelling and nothing else.

    A table that grew a second entry would change what the column
    accepts, and every consumer of that column reads the column, not
    the table — so the table itself is pinned here.
    """
    assert vl._ROUND_VERDICT_STATUS_ALIASES == {
        "verification_failed": "failed",
    }, (
        "the alias table changed: the round-verdict vocabulary is now "
        "wider or narrower than the one entry this fix is about"
    )


# ---------------------------------------------------------------------------
# 7. With the alias in place, the write carries the NORMALISED status
# ---------------------------------------------------------------------------


def test_alias_present_writes_failed(hermetic_server, monkeypatch):
    """The recorder asks the repository for ``failed``.

    Not for ``verification_failed`` — that spelling is what the
    orchestrator produced, and the column does not speak it.  The
    normalisation happens in the recorder, on the way to the write.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "round-verdict-alias-present"
    _seed_running_verification(db_path, plan_id, plans_root)

    calls = _spy_repository(monkeypatch)

    vl._record_round_verdict_persist(
        plan_id,
        LIVE_TOKEN,
        "verification_failed",
        "same_failure_repeated_after_max_attempts",
        {"results": {}},
    )

    assert len(calls) == 1, (
        "the recorder did not write the round's verdict at all — "
        f"{len(calls)} writes reached the repository"
    )
    assert calls[0]["status"] == "failed", (
        "the repository was asked to write a status the column does not "
        f"speak: {calls[0]['status']!r}"
    )
    assert calls[0]["results"].get("status") == "failed", (
        "the envelope and the column disagree about the round's verdict"
    )
    assert calls[0]["stop_reason"] == (
        "same_failure_repeated_after_max_attempts"
    ), "the round's own stop reason did not reach the write"


# ---------------------------------------------------------------------------
# 8. THE NEGATIVE CONTROL: remove the alias, the write disappears
# ---------------------------------------------------------------------------


def test_removing_the_alias_stops_the_write(
    hermetic_server, monkeypatch, caplog,
):
    """Take the alias away and the same call writes NOTHING.

    This is the negative control the acceptance rule asks for, as an
    executable case instead of a sentence: with the alias table
    emptied, ``verification_failed`` is no longer a spelling the
    recorder knows, so it refuses the write — loudly, because a
    spelling nobody can read must not look like one nobody ever
    produced.

    If this test ever passes with the alias still in place, the
    control has stopped controlling anything.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "round-verdict-alias-removed"
    _seed_running_verification(db_path, plan_id, plans_root)

    calls = _spy_repository(monkeypatch)

    # The control itself: the alias table, emptied.
    monkeypatch.setattr(vl, "_ROUND_VERDICT_STATUS_ALIASES", {})

    with caplog.at_level(logging.WARNING, logger="server"):
        vl._record_round_verdict_persist(
            plan_id,
            LIVE_TOKEN,
            "verification_failed",
            "same_failure_repeated_after_max_attempts",
            {"results": {}},
        )

    assert calls == [], (
        "the verdict was written even though the alias table was "
        f"emptied — {len(calls)} write(s) reached the repository"
    )
    assert _row(db_path, plan_id)["verification_status"] == "running", (
        "the row changed even though the alias table was emptied — the "
        "write is not gated on the alias at all, so the alias is not "
        "what makes the difference"
    )

    messages = [r.getMessage() for r in _warnings(caplog)]
    assert any("unknown round verdict" in m for m in messages), (
        "the refused write said nothing — the log must show that the "
        "spelling was refused, not that it was never seen: "
        f"{messages!r}"
    )
