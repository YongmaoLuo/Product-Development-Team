"""Tests for the narrow ``VerificationRepository.set_state`` writer.

``set_state`` exists because the verification state has exactly ONE
authoritative write entry point (arch decision point 1) and the
``plan_routing.verification`` mirror no longer carries writes (arch
decision point 3). The writer therefore has to touch ``plan_verification``
and NOTHING else — no whole-row overwrite (``insert``), no forced
``running`` status (``init_round``), no terminal settlement
(``complete_round``).

The four writable fields are ``status`` / ``round`` / ``max_rounds`` /
``stop_reason``; every one of them is OPTIONAL, and ``None`` means
"leave this column alone" — never "clear it".
"""

from __future__ import annotations

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.verification_repository import VerificationRepository


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "state.db"
    conn = open_db(path)
    migrate(conn)
    conn.close()
    return path


def _repo(db_path):
    conn = open_db(db_path)
    return conn, VerificationRepository(conn)


def test_set_state_updates_existing_row(db_path):
    """Only ``round`` changes — the other three columns stay put."""
    conn, repo = _repo(db_path)
    repo.insert(
        "p1",
        "not_started",
        round=0,
        max_rounds=3,
        verification_stop_reason="keep_me",
    )

    repo.set_state("p1", round_n=2)

    row = repo.current("p1")
    assert row["round"] == 2
    assert row["verification_status"] == "not_started"
    assert row["max_rounds"] == 3
    assert row["verification_stop_reason"] == "keep_me"
    conn.close()


def test_set_state_inserts_when_absent(db_path):
    """No row yet → one is created; ``status`` defaults to ``not_started``."""
    conn, repo = _repo(db_path)
    assert repo.current("p1") is None

    repo.set_state("p1", round_n=2)

    row = repo.current("p1")
    assert row is not None
    assert row["verification_status"] == "not_started"
    assert row["round"] == 2
    assert row["max_rounds"] == 0
    assert row["verification_stop_reason"] is None
    conn.close()


def test_set_state_noop_when_all_none(db_path):
    """Every argument ``None`` → not one statement is sent to SQLite."""
    conn, repo = _repo(db_path)
    repo.insert("p1", "not_started", round=1, max_rounds=3)
    before = repo.current("p1")

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        repo.set_state("p1")
    finally:
        conn.set_trace_callback(None)

    assert statements == []
    after = repo.current("p1")
    assert after["updated_at"] == before["updated_at"]
    assert after["round"] == 1
    assert after["max_rounds"] == 3
    assert after["verification_status"] == "not_started"
    conn.close()


def test_set_state_keeps_stop_reason_when_none(db_path):
    """``stop_reason=None`` means "don't touch", not "clear"."""
    conn, repo = _repo(db_path)
    repo.insert(
        "p1",
        "not_started",
        round=0,
        max_rounds=3,
        verification_stop_reason="max_rounds_reached",
    )

    repo.set_state("p1", status="running", round_n=1)

    row = repo.current("p1")
    assert row["verification_stop_reason"] == "max_rounds_reached"
    assert row["verification_status"] == "running"
    assert row["round"] == 1
    conn.close()


def test_set_state_explicit_empty_string_clears_stop_reason(db_path):
    """Clearing is an explicit empty string — ``None`` cannot clear."""
    conn, repo = _repo(db_path)
    repo.insert("p1", "not_started", verification_stop_reason="user_stopped")

    repo.set_state("p1", stop_reason="")

    row = repo.current("p1")
    assert row["verification_stop_reason"] == ""
    conn.close()


def test_set_state_routes_through_update(db_path, monkeypatch):
    """The write goes through ``_update``, so the writeable-column guard
    still applies — this method must not hand-roll its own UPDATE."""
    conn, repo = _repo(db_path)
    repo.insert("p1", "not_started")

    seen: dict[str, object] = {}
    original = repo._update

    def spy(plan_id, **fields):
        seen["plan_id"] = plan_id
        seen["fields"] = dict(fields)
        return original(plan_id, **fields)

    monkeypatch.setattr(repo, "_update", spy)
    repo.set_state("p1", status="running", round_n=3, max_rounds=4, stop_reason="why")

    assert seen["plan_id"] == "p1"
    assert seen["fields"] == {
        "verification_status": "running",
        "round": 3,
        "max_rounds": 4,
        "verification_stop_reason": "why",
    }
    conn.close()


def test_set_state_does_not_publish_vp_event(db_path, monkeypatch):
    """This is not a VP state change — no event is published."""
    conn, repo = _repo(db_path)
    repo.insert("p1", "not_started")

    calls: list[tuple] = []
    monkeypatch.setattr(
        repo, "_publish_vp_event", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    repo.set_state("p1", status="running", round_n=1)

    assert calls == []
    conn.close()