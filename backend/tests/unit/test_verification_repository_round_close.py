"""``complete_round`` does two separable things, and only one of them
belongs on a repair round.

The method records the round's verdict AND fires ``plan_closed``, which
every subscriber reads as "this plan is finished". A round that ended
FAILED and is about to hand off to a repair executor has a verdict but
does NOT have a finished plan — announcing a terminal there tells the
notifier (and any future subscriber, and anyone reading
``/api/debug/notifications``) that the plan closed seconds before an
executor starts on it, and forces a card push past the coalesce window
for a state that is about to be superseded.

``publish_closed`` splits the two. Default ``True`` keeps every existing
call site's behaviour byte-identical; the repair path passes ``False``.

What is pinned here:

  * the verdict is written either way — the column is the whole point;
  * ``plan_closed`` fires by default;
  * ``publish_closed=False`` writes the column and stays silent;
  * the post-write consistency assertion runs in BOTH cases, because
    silently skipping it would make the suppressed variant a weaker
    check than the one it was derived from.
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


def test_publish_closed_defaults_to_true(repo_and_conn, monkeypatch):
    repo = repo_and_conn
    seen: list = []
    monkeypatch.setattr(
        repo, "_publish_plan_closed",
        lambda plan_id, **kw: seen.append((plan_id, kw)),
    )
    repo.init_round("p", round_n=1, max_rounds=4)

    repo.complete_round("p", {}, status="failed")

    assert len(seen) == 1, seen
    assert seen[0][0] == "p"
    assert seen[0][1]["status"] == "failed"


def test_suppressed_publish_still_writes_the_verdict(repo_and_conn, monkeypatch):
    """The column is the point of the call. Suppressing the event must
    not turn it into a no-op."""
    repo = repo_and_conn
    seen: list = []
    monkeypatch.setattr(
        repo, "_publish_plan_closed",
        lambda plan_id, **kw: seen.append((plan_id, kw)),
    )
    repo.init_round("p", round_n=1, max_rounds=4)
    assert repo.current("p")["verification_status"] == "running"

    repo.complete_round("p", {}, status="failed", publish_closed=False)

    assert seen == [], "plan_closed fired for a round whose plan continues"
    assert repo.current("p")["verification_status"] == "failed"


def test_suppressed_publish_still_runs_the_consistency_assertion(
    repo_and_conn, monkeypatch,
):
    """Otherwise the repair path would get a weaker integrity check than
    the terminal path it shares a method with."""
    repo = repo_and_conn
    monkeypatch.setattr(repo, "_publish_plan_closed", lambda *a, **k: None)
    calls: list = []
    monkeypatch.setattr(
        repo, "_assert_terminal_state_consistent",
        lambda plan_id, expected_status=None: calls.append(expected_status),
    )
    repo.init_round("p", round_n=1, max_rounds=4)

    repo.complete_round("p", {}, status="passed", publish_closed=False)

    assert calls == ["passed"]


def test_stop_reason_is_written_in_both_modes(repo_and_conn, monkeypatch):
    repo = repo_and_conn
    monkeypatch.setattr(repo, "_publish_plan_closed", lambda *a, **k: None)

    repo.init_round("p", round_n=2, max_rounds=4)
    repo.complete_round(
        "p", {}, status="loop_stopped",
        stop_reason="max_rounds_reached", publish_closed=False,
    )

    row = repo.current("p")
    assert row["verification_status"] == "loop_stopped"
    assert row["verification_stop_reason"] == "max_rounds_reached"
