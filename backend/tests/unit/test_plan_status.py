"""``PlanStatus`` — one plan, one answer (2026-09-23).

The bug these tests pin: a production plan had six live
answers to "where is it now" (``plan_routing.current_phase`` =
``completed``, its ``verification`` mirror = ``failed``,
``plan_verification`` = ``running``, ``plan_tasks`` = a repair task in
progress, ``/api/system/active`` = running, and the operator's card =
"⏸ 暂停（上游阻塞）"). The truth was the tasks table. Nothing was broken
about any one store — they were written by different code paths at
different moments, and the card re-derived its verdict from three
endpoints fetched at three instants.

Two layers are tested here:

* :func:`find_divergences` — pure, so the invariants are cheap to pin
  exhaustively, including the shapes that must NOT fire.
* :func:`server._build_plan_status` — the assembly, against a hermetic
  ``state.db``, so "the snapshot says X" is checked against real rows
  rather than against a dict the test wrote for itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# find_divergences — pure
# ---------------------------------------------------------------------------


def _status(**over):
    from plan_status import PlanStatus

    base = dict(
        plan_id="plan-x",
        phase="executing",
        verification_status="running",
        verification_round=1,
        verification_max_rounds=4,
        execution_in_flight=True,
        verification_in_flight=True,
        tasks={"total": 5, "completed": 2, "failed": 0,
               "in_progress": 1, "pending": 2, "skipped": 0},
    )
    base.update(over)
    return PlanStatus(**base)


def _codes(status):
    from plan_status import find_divergences

    return [d.code for d in find_divergences(status)]


def test_the_0921_shape_is_reported():
    """``phase="completed"`` while the repair execution is live.

    This is the incident verbatim: the dispatcher wrote ``executing``,
    the caller promoted it to ``completed``, and the card read
    "⏸ 暂停（上游阻塞）" next to a running ``repair-r1-01``.
    """
    codes = _codes(_status(
        phase="completed",
        verification_status="failed",
        execution_in_flight=True,
    ))

    assert "finished_phase_with_live_execution" in codes, (
        "a finished phase with a live execution is exactly the shape that "
        "lied to the operator for 80 minutes"
    )


def test_a_running_plan_is_not_reported():
    """The anti-vacuity control: the normal case must stay silent."""
    assert _codes(_status()) == []


def test_a_clean_finish_is_not_reported():
    assert _codes(_status(
        phase="completed",
        verification_status="passed",
        execution_in_flight=False,
        verification_in_flight=False,
    )) == []


def test_a_stalled_plan_is_not_reported():
    """``⏸ 暂停（上游阻塞）`` is a legitimate resting state.

    Firing here would make the alarm useless for the one case the card
    was built to show.
    """
    assert _codes(_status(
        phase="completed",
        verification_status="failed",
        execution_in_flight=False,
        verification_in_flight=False,
        tasks={"total": 33, "completed": 22, "failed": 1,
               "in_progress": 0, "pending": 10, "skipped": 0},
    )) == []


def test_phase_and_verdict_disagree_is_reported():
    """``verification_running`` but the verdict already landed."""
    codes = _codes(_status(
        phase="verification_running",
        verification_status="failed",
        execution_in_flight=False,
        verification_in_flight=False,
    ))

    assert "phase_and_verdict_disagree" in codes


def test_a_matching_phase_and_verdict_is_not_reported():
    assert _codes(_status(
        phase="verification_failed",
        verification_status="failed",
        execution_in_flight=False,
        verification_in_flight=False,
    )) == []


def test_finished_is_false_while_an_execution_runs():
    """``finished`` is the derived property consumers key off — it must not
    be true just because the phase string looks terminal."""
    st = _status(phase="completed", execution_in_flight=True)
    assert st.finished is False
    assert _status(phase="completed", execution_in_flight=False).finished is True


def test_task_count_helpers_survive_junk():
    st = _status(tasks={"total": "17", "completed": None, "failed": 3})
    assert st.count("total") == 17
    assert st.count("completed") == 0
    assert st.finished_tasks == 3


# ---------------------------------------------------------------------------
# _build_plan_status — assembled from real rows
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """Hermetic ``state.db`` wired into ``server``."""
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db = tmp_path / "state.db"
    conn = open_db(db)
    migrate(conn)
    conn.close()
    # ``_open_state_machine`` calls ``_state_db_path(request)``.
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db)
    return db


def _seed_routing(db: Path, plan_id: str, phase: str) -> None:
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, version, completed_phases, review_rounds, "
            " flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan_id, phase, 0, "[]", "{}", "{}", "null",
             "2026-09-23T00:00:00", "2026-09-23T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_verification(db: Path, plan_id: str, *, status: str,
                       round_n: int = 1, max_rounds: int = 4) -> None:
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db)
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.init_round(plan_id, round_n=round_n, max_rounds=max_rounds)
        repo.complete_round(plan_id, {}, status=status, stop_reason=None)
        conn.commit()
    finally:
        conn.close()


def _seed_tasks(db: Path, plan_id: str, rows) -> None:
    """Seed ``plan_tasks``.

    ``add_task`` validates that identity must not carry progress — a
    ``status`` key in the identity payload raises
    ``TaskProgressValidationError`` — so identity and runtime state go in
    through their two separate doors, exactly as the runtime does.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(db)
    try:
        migrate(conn)
        repo = PlanTaskRepository(conn)
        for row in rows:
            fields = dict(row)
            task_id = str(fields.pop("id"))
            status = fields.pop("status", None)
            repo.add_task(plan_id, {"id": task_id, **fields})
            if status is not None:
                repo.update_task(plan_id, task_id, {"status": status})
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def clean_execution_state():
    """Restore ``_execution_state`` / ``_verification_state`` after a test."""
    import server

    exec_before = dict(server._execution_state)
    verif_before = dict(server._verification_state)
    yield
    server._execution_state.clear()
    server._execution_state.update(exec_before)
    server._verification_state.clear()
    server._verification_state.update(verif_before)


def test_the_snapshot_reports_the_0921_state(state_db, clean_execution_state):
    """Every field the card needs, from one read."""
    import server

    # A realistic plan id: the system only ever produces
    # ``derive_plan_id`` output ([A-Za-z0-9._-]), and server.py rejects
    # anything else before it touches ``plans/{plan_id}``.
    plan_id = "20260101-example-plan"
    _seed_routing(state_db, plan_id, "completed")
    _seed_verification(state_db, plan_id, status="failed", round_n=1)
    _seed_tasks(state_db, plan_id, [
        {"id": "1", "title": "done", "status": "completed"},
        {"id": "2", "title": "done too", "status": "completed"},
        {"id": "14-5", "title": "broke", "status": "failed"},
        {"id": "repair-r1-01", "title": "fix VP-001", "status": "in_progress"},
        {"id": "repair-r1-02", "title": "fix VP-002", "status": "pending"},
    ])
    # A repair execution is live — the record the dispatcher seeds.
    server._execution_state[plan_id] = {
        "status": "running", "ended_at": None, "_source": "repair_execution",
    }

    st = server._build_plan_status(plan_id)

    assert st.phase == "completed"
    assert st.verification_status == "failed"
    assert st.verification_round == 1
    assert st.verification_max_rounds == 4
    assert st.execution_in_flight is True
    assert st.tasks == {"total": 5, "completed": 2, "failed": 1,
                        "in_progress": 1, "pending": 1, "skipped": 0}
    assert st.current_task == {"id": "repair-r1-01", "title": "fix VP-001"}, (
        "the card's 🔄 当前任务 line is what contradicted the ⏸ header"
    )
    assert st.next_task is not None and st.next_task["id"] == "repair-r1-02"
    assert [d.code for d in st.divergences] == [
        "finished_phase_with_live_execution"
    ], st.to_dict()
    assert st.consistent is False


def test_superseded_rows_are_not_counted_as_work(state_db, clean_execution_state):
    """``drop_terminal_db_orphans`` drops them from every card section, so
    the status object must agree about how big the plan is."""
    import server

    plan_id = "plan-superseded"
    _seed_routing(state_db, plan_id, "executing")
    _seed_verification(state_db, plan_id, status="running")
    _seed_tasks(state_db, plan_id, [
        {"id": "1", "title": "real", "status": "completed"},
        {"id": "2", "title": "gone", "status": "superseded"},
    ])

    st = server._build_plan_status(plan_id)

    assert st.tasks["total"] == 1, st.tasks
    assert st.tasks["completed"] == 1


def test_a_clean_plan_reports_no_divergences(state_db, clean_execution_state):
    import server

    plan_id = "plan-clean"
    _seed_routing(state_db, plan_id, "completed")
    _seed_verification(state_db, plan_id, status="passed")
    _seed_tasks(state_db, plan_id, [
        {"id": "1", "title": "done", "status": "completed"},
    ])

    st = server._build_plan_status(plan_id)

    assert st.divergences == ()
    assert st.consistent is True
    assert st.execution_in_flight is False
    assert st.execution_in_flight is False


def test_a_plan_that_was_never_started_reads_as_empty(
    tmp_path, state_db, monkeypatch, clean_execution_state,
):
    """No routing row, no tasks, no execution — a 404-able plan, not a crash."""
    import server

    st = server._build_plan_status("plan-that-does-not-exist")

    assert st.phase == ""
    assert st.tasks["total"] == 0
    assert st.divergences == ()


def test_the_snapshot_never_raises_on_a_corrupt_db(
    tmp_path, monkeypatch, clean_execution_state,
):
    """An operator asking "where is this plan" is the wrong moment for a 500."""
    import server

    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a sqlite file at all")
    monkeypatch.setattr(server, "_state_db_path", lambda: broken)

    st = server._build_plan_status("plan-whatever")

    assert st.phase == ""
    assert st.divergences == ()


def test_to_dict_round_trips_the_divergences(state_db):
    import server

    st = _status(phase="completed", execution_in_flight=True)
    payload = st.to_dict()

    assert payload["divergences"] == []
    assert set(payload) >= {
        "plan_id", "phase", "verification_status", "verification_round",
        "verification_max_rounds", "verification_stop_reason",
        "execution_in_flight", "verification_in_flight", "tasks",
        "current_task", "next_task", "divergences",
    }


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_the_endpoint_404s_for_an_unknown_plan(tmp_path, monkeypatch):
    import server
    from fastapi import HTTPException

    monkeypatch.setattr(server, "PLANS_DIR", tmp_path)

    with pytest.raises(HTTPException) as exc:
        server.get_plan_status("no-such-plan")

    assert exc.value.status_code == 404


def test_the_endpoint_serves_the_snapshot(tmp_path, monkeypatch, state_db,
                                          clean_execution_state):
    import server

    plan_id = "plan-endpoint"
    (tmp_path / plan_id).mkdir()
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path)
    _seed_routing(state_db, plan_id, "executing")
    _seed_verification(state_db, plan_id, status="running")
    _seed_tasks(state_db, plan_id, [
        {"id": "1", "title": "work", "status": "in_progress"},
    ])

    payload = server.get_plan_status(plan_id)

    assert payload["plan_id"] == plan_id
    assert payload["phase"] == "executing"
    assert payload["tasks"]["total"] == 1
    assert payload["divergences"] == []


# ---------------------------------------------------------------------------
# _plan_status_payload — one read serves the whole card
# ---------------------------------------------------------------------------


def test_the_payload_carries_the_three_body_sections(
    tmp_path, monkeypatch, state_db, clean_execution_state,
):
    """The notifier makes ONE read; the body still needs its sections.

    Before 2026-09-23 ``_rebuild_card`` called three endpoints at three
    instants and re-derived a verdict from the mix. The sections are
    still here — they are gathered in one server-side pass now, so they
    describe one moment.
    """
    import server

    plan_id = "plan-sections"
    (tmp_path / plan_id).mkdir()
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path)
    _seed_routing(state_db, plan_id, "executing")
    _seed_verification(state_db, plan_id, status="running")
    _seed_tasks(state_db, plan_id, [
        {"id": "1", "title": "work", "status": "in_progress"},
    ])

    payload = server._plan_status_payload(plan_id)

    for key in ("summary", "execution", "verification"):
        assert key in payload, key
    # The verdict fields are the top level, not buried in a section.
    assert payload["phase"] == "executing"
    assert payload["verification_status"] == "running"
    # ``summary`` is the real endpoint body, so it carries the task
    # counts the body renders from. (Its exact arithmetic is that
    # endpoint's business, not this composer's.)
    assert "tasks" in (payload["summary"] or {})


def test_a_failing_section_degrades_to_none_not_500(
    tmp_path, monkeypatch, state_db, clean_execution_state,
):
    """An operator asking "where is this plan" must still get an answer."""
    import server

    plan_id = "plan-broken-section"
    (tmp_path / plan_id).mkdir()
    monkeypatch.setattr(server, "PLANS_DIR", tmp_path)
    _seed_routing(state_db, plan_id, "executing")
    _seed_verification(state_db, plan_id, status="running")

    def _boom(_plan_id):
        raise RuntimeError("section exploded")

    monkeypatch.setattr(server, "get_execution_progress", _boom)

    payload = server._plan_status_payload(plan_id)

    assert payload["execution"] is None, "a broken section must not be invented"
    assert payload["phase"] == "executing", (
        "the verdict must survive a body section failing"
    )
