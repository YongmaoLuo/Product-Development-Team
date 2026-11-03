"""A terminal verification round keeps its FIRST stop reason.

Background: a plan had already terminated round 2 with
``base_drift_conflicts``.  A later ``POST /stop`` rewrote
``plan_verification.verification_stop_reason`` to ``user_stopped``,
erasing the real cause of the termination.  The root cause was
:meth:`VerificationRepository.mark_stopped` writing the reason
unconditionally.

Contract exercised here:

  * an open round (``running`` / ``pending``) writes the reason as before;
  * an already-terminal round keeps the reason it was given first and
    only *logs* the attempted overwrite;
  * the early return must not write, must not re-publish ``plan_closed``
    (a second close) and must not re-run the post-write invariant check;
  * a terminal round whose reason is still empty writes normally — there
    is no first reason to preserve yet.
"""

from __future__ import annotations

import logging

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.verification_repository import VerificationRepository


LOGGER_NAME = "state_machine.repositories.verification_repository"


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


@pytest.fixture
def published(monkeypatch):
    """Record every ``publish_safe`` call made through the repository."""
    import notifications.state_events as state_events

    events: list[tuple[str, str, dict]] = []

    def _record(kind, plan_id, **payload):
        events.append((kind, plan_id, payload))

    monkeypatch.setattr(state_events, "publish_safe", _record)
    return events


def _plan_closed(events):
    return [e for e in events if e[0] == "plan_closed"]


# ---------------------------------------------------------------------------
# Open rounds keep the original write semantics.
# ---------------------------------------------------------------------------


def test_open_round_first_stop_writes_reason(repo):
    repo.insert("p1", "running")
    repo.mark_stopped("p1", reason="max_rounds_reached")
    row = repo.current("p1")
    assert row["verification_status"] == "loop_stopped"
    assert row["verification_stop_reason"] == "max_rounds_reached"


def test_pending_round_still_writes_reason(repo):
    repo.insert("p1", "pending")
    repo.mark_stopped("p1", reason="user_stopped")
    row = repo.current("p1")
    assert row["verification_status"] == "loop_stopped"
    assert row["verification_stop_reason"] == "user_stopped"


# ---------------------------------------------------------------------------
# A terminal round keeps the first reason.
# ---------------------------------------------------------------------------


def test_terminal_round_keeps_first_reason(repo):
    repo.insert("p1", "loop_stopped", verification_stop_reason="base_drift_conflicts")
    repo.mark_stopped("p1", reason="user_stopped")
    assert repo.current("p1")["verification_stop_reason"] == "base_drift_conflicts"


def test_terminal_round_logs_kept_and_attempted(repo, caplog):
    repo.insert("p1", "loop_stopped", verification_stop_reason="base_drift_conflicts")
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        repo.mark_stopped("p1", reason="user_stopped")
    messages = [record.getMessage() for record in caplog.records]
    assert any("verification_stop_reason_kept" in m for m in messages)
    assert any(
        "kept=base_drift_conflicts attempted=user_stopped" in m for m in messages
    )


def test_terminal_round_does_not_republish_plan_closed(repo, published):
    repo.insert("p1", "running")
    repo.mark_stopped("p1", reason="max_rounds_reached")
    repo.mark_stopped("p1", reason="user_stopped")
    closed = _plan_closed(published)
    assert len(closed) == 1
    assert closed[0][1] == "p1"


def test_terminal_round_skips_update_and_invariant_check(repo, monkeypatch):
    repo.insert("p1", "loop_stopped", verification_stop_reason="base_drift_conflicts")
    calls: list[str] = []
    monkeypatch.setattr(
        repo, "_update", lambda *a, **k: calls.append("update")
    )
    monkeypatch.setattr(
        repo, "_assert_terminal_state_consistent",
        lambda *a, **k: calls.append("assert"),
    )
    repo.mark_stopped("p1", reason="user_stopped")
    assert calls == []


# ---------------------------------------------------------------------------
# A terminal round without a reason yet writes normally.
# ---------------------------------------------------------------------------


def test_terminal_round_with_empty_reason_still_writes(repo, published):
    repo.insert("p1", "loop_stopped")
    assert repo.current("p1")["verification_stop_reason"] is None
    repo.mark_stopped("p1", reason="user_stopped")
    row = repo.current("p1")
    assert row["verification_status"] == "loop_stopped"
    assert row["verification_stop_reason"] == "user_stopped"
    assert len(_plan_closed(published)) == 1


# ---------------------------------------------------------------------------
# Unchanged failure modes.
# ---------------------------------------------------------------------------


def test_missing_row_raises_key_error(repo):
    with pytest.raises(KeyError):
        repo.mark_stopped("missing", reason="user_stopped")


def test_invalid_status_raises_value_error(repo):
    repo.insert("p1", "running")
    with pytest.raises(ValueError):
        repo.mark_stopped("p1", reason="user_stopped", status="bogus")


def test_invalid_status_raises_value_error_for_terminal_row(repo):
    repo.insert("p1", "loop_stopped", verification_stop_reason="base_drift_conflicts")
    with pytest.raises(ValueError):
        repo.mark_stopped("p1", reason="user_stopped", status="bogus")
