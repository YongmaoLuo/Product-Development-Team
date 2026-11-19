"""``release_round`` hands a round number back.

The convergence requirement (§2 / §3): a round that was BLOCKED before
dispatching any verification point — base-drift preflight, service
preflight — must not consume a round number. The repository only had
``init_round``, which overwrites the counter; there was no way back, so
once the number was written it was spent.

``release_round`` is that way back. It rewinds the counter by exactly
one and records WHY in ``executor_state["released_rounds"]``, so the
audit trail survives the rewind instead of vanishing with the number.

What is pinned here:

  * the round number is rewound by exactly one and the cap
    (``max_rounds``) is left alone;
  * the release is idempotent — releasing an already-released round is
    refused instead of decrementing the counter twice;
  * a plan without a row is refused, and no row is created;
  * a round that was RE-RUN to the same number can be released again,
    and both releases are recorded;
  * the rewind stops at 0 — a round number is never written negative;
  * a corrupt / non-object ``executor_state`` is rebuilt as an empty
    object instead of raising into the caller.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def repo_and_conn(tmp_path: Path):
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    try:
        yield VerificationRepository(conn)
    finally:
        conn.close()


def test_release_round_rewinds(repo_and_conn):
    """The number goes back, the cap stays, the reason lands on disk."""
    repo = repo_and_conn
    repo.init_round("p", round_n=3, max_rounds=4)

    assert repo.release_round("p", 3, reason="service_preflight_failed") is True

    row = repo.summary("p")
    assert row is not None
    assert row["round"] == 2
    assert row["max_rounds"] == 4

    released = row["executor_state"]["released_rounds"]
    assert len(released) == 1
    assert released[0]["round"] == 3
    assert released[0]["reason"] == "service_preflight_failed"
    assert released[0]["released_at"]


def test_release_round_idempotent(repo_and_conn):
    """Releasing the same round twice must not decrement twice nor
    duplicate the audit entry."""
    repo = repo_and_conn
    repo.init_round("p", round_n=3, max_rounds=4)

    assert repo.release_round("p", 3, reason="service_preflight_failed") is True
    assert repo.release_round("p", 3, reason="service_preflight_failed") is False

    row = repo.summary("p")
    assert row["round"] == 2
    released = row["executor_state"]["released_rounds"]
    assert len(released) == 1


def test_release_round_missing_plan(repo_and_conn):
    """No row → refuse, and do not create one on the way out."""
    repo = repo_and_conn

    assert repo.release_round("nope", 3, reason="service_preflight_failed") is False
    assert repo.current("nope") is None


def test_release_after_rerun_records_again(repo_and_conn):
    """A round re-run to the same number is a NEW attempt and may be
    released again — both releases are recorded."""
    repo = repo_and_conn
    repo.init_round("p", round_n=3, max_rounds=4)
    assert repo.release_round("p", 3, reason="base_drift_conflicts") is True

    # The round was re-run: the counter is back at 3.
    repo.init_round("p", round_n=3, max_rounds=4)
    assert repo.summary("p")["round"] == 3
    assert repo.release_round("p", 3, reason="base_drift_conflicts") is True

    row = repo.summary("p")
    assert row["round"] == 2
    released = row["executor_state"]["released_rounds"]
    assert len(released) == 2
    assert [entry["round"] for entry in released] == [3, 3]
    assert [entry["reason"] for entry in released] == [
        "base_drift_conflicts",
        "base_drift_conflicts",
    ]


def test_release_round_stops_at_zero(repo_and_conn):
    """The rewind stops at 0 — a round number is never negative."""
    repo = repo_and_conn
    repo.init_round("p", round_n=1, max_rounds=4)

    assert repo.release_round("p", 1, reason="base_drift_conflicts") is True

    row = repo.summary("p")
    assert row["round"] == 0

    assert repo.release_round("p", 0, reason="base_drift_conflicts") is True
    assert repo.summary("p")["round"] == 0


def test_release_round_leaves_verification_status_untouched(repo_and_conn):
    """The status is the task-15 interpreter's business — this method
    only gives the number back."""
    repo = repo_and_conn
    repo.init_round("p", round_n=3, max_rounds=4)
    before = repo.summary("p")["verification_status"]

    repo.release_round("p", 3, reason="base_drift_conflicts")

    assert repo.summary("p")["verification_status"] == before


def test_release_round_rebuilds_non_object_executor_state(repo_and_conn):
    """A corrupt / non-object ``executor_state`` is rebuilt as an empty
    object — the release still lands and nothing raises."""
    repo = repo_and_conn
    repo.init_round("p", round_n=3, max_rounds=4)
    repo._update("p", executor_state=["not", "an", "object"])

    assert repo.release_round("p", 3, reason="base_drift_conflicts") is True

    row = repo.summary("p")
    assert row["round"] == 2
    assert row["executor_state"]["released_rounds"][0]["round"] == 3


def test_release_round_rebuilds_unparsable_executor_state(repo_and_conn):
    """Same for a column that does not even hold valid JSON."""
    repo = repo_and_conn
    repo.init_round("p", round_n=3, max_rounds=4)
    repo._conn.execute(
        "UPDATE plan_verification SET executor_state = ? WHERE plan_id = ?",
        ("{not-json", "p"),
    )

    assert repo.release_round("p", 3, reason="base_drift_conflicts") is True

    row = repo.summary("p")
    assert row["round"] == 2
    assert row["executor_state"]["released_rounds"][0]["round"] == 3
