"""``mark_round_blocked`` records a blocked round WITHOUT ending it.

Background: a verification round that ends on a base-drift conflict must
leave a persistent trace on the row.  Without one, the next round has no
evidence that the previous round was blocked, and the round counter is
bumped for a round that never really ran.  The trace has to be:

  * **write-once** — the same semantics ``mark_stopped`` already has
    (a terminal round keeps the reason it was given first); and
  * **non-terminal** — the round is blocked, not finished, so
    ``verification_status`` must not move.  A blocked round is expected to
    be re-run under the SAME round number.

Contract exercised here:

  * the reason lands on ``verification_stop_reason`` while
    ``verification_status`` / ``round`` / ``max_rounds`` stay untouched;
  * the optional ``results`` envelope is stored verbatim, and
    ``results=None`` writes no envelope at all;
  * a terminal row that already carries a reason keeps it;
  * a missing row raises ``KeyError``;
  * the write is NOT mistaken for a half-written terminal persist by
    ``repair_stale_terminal_state``;
  * ``mark_round_blocked`` and ``mark_stopped`` share ONE write-once
    judgement — a row stopped first keeps the first reason.
"""

from __future__ import annotations

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.verification_repository import VerificationRepository


BLOCKED_ENVELOPE = {
    "status": "base_drift_blocked",
    "recorded_by": "_base_drift_round_block",
}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "state.db"
    conn = open_db(path)
    migrate(conn)
    conn.close()
    return path


@pytest.fixture
def repo(db_path):
    conn = open_db(db_path)
    try:
        yield VerificationRepository(conn)
    finally:
        conn.close()


def test_marks_reason_and_keeps_status(repo):
    """The reason lands; the round is still running and keeps its number."""
    repo.insert("p1", "running", round=2, max_rounds=5)

    repo.mark_round_blocked("p1", reason="base_drift_conflicts")

    row = repo.current("p1")
    assert row["verification_stop_reason"] == "base_drift_conflicts"
    assert row["verification_status"] == "running"
    assert row["round"] == 2
    assert row["max_rounds"] == 5


def test_results_envelope_is_written(repo):
    """The envelope is stored verbatim, so a later round can read it back."""
    repo.insert("p1", "running")

    repo.mark_round_blocked(
        "p1", reason="base_drift_conflicts", results=dict(BLOCKED_ENVELOPE),
    )

    assert repo.current("p1")["results"] == BLOCKED_ENVELOPE


def test_results_none_leaves_the_column_untouched(repo):
    """``results=None`` must not write (nor clear) the column."""
    repo.insert("p1", "running", results={"status": "previous"})

    repo.mark_round_blocked("p1", reason="base_drift_conflicts")

    assert repo.current("p1")["results"] == {"status": "previous"}


def test_terminal_reason_is_write_once(repo):
    """A terminal round keeps the reason it was given first."""
    repo.insert(
        "p1", "loop_stopped", verification_stop_reason="base_drift_conflicts",
    )

    repo.mark_round_blocked("p1", reason="user_stopped")

    row = repo.current("p1")
    assert row["verification_stop_reason"] == "base_drift_conflicts"
    assert row["verification_status"] == "loop_stopped"


def test_missing_row_raises_keyerror(repo):
    with pytest.raises(KeyError):
        repo.mark_round_blocked("missing", reason="base_drift_conflicts")


def test_not_healed_by_stale_terminal_repair(repo):
    """The blocked trace must not be read as a half-written terminal verdict.

    ``repair_stale_terminal_state`` re-derives ``verification_status``
    from a ``results`` envelope stamped by ``_persist_verification_terminal``.
    The blocked envelope is stamped by ``_base_drift_round_block``, so the
    repair must leave the row alone.
    """
    repo.insert("p1", "running")

    repo.mark_round_blocked(
        "p1", reason="base_drift_conflicts", results=dict(BLOCKED_ENVELOPE),
    )

    assert repo.repair_stale_terminal_state("p1") is None
    assert repo.current("p1")["verification_status"] == "running"


def test_shares_write_once_semantics_with_mark_stopped(repo):
    """One judgement, two callers: the first reason wins either way."""
    repo.insert("only", "running")
    repo.insert("both", "running")

    repo.mark_stopped("only", reason="base_drift_conflicts")
    repo.mark_stopped("both", reason="base_drift_conflicts")
    repo.mark_round_blocked(
        "both", reason="user_stopped", results=dict(BLOCKED_ENVELOPE),
    )

    only_row = repo.current("only")
    both_row = repo.current("both")
    assert both_row["verification_stop_reason"] == only_row["verification_stop_reason"]
    assert both_row["verification_stop_reason"] == "base_drift_conflicts"
    assert both_row["verification_status"] == only_row["verification_status"]
    assert both_row["results"] == only_row["results"]