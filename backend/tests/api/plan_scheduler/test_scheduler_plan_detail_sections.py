"""``GET /api/scheduler/plan/{plan_id}/detail`` — the structured sections.

What is pinned here
-------------------
The detail endpoint already answers with the card's flattened **lines**:
sentences a renderer prints.  A panel that *draws* the plan's state — a
status header with a colour and an elapsed time, a phase banner, a
progress bar, a list of the tasks that are running right now — needs the
same facts as **structure**, and a sentence has to be parsed back into
facts before it can be drawn.

So the endpoint answers with both: the ``lines`` stay exactly as they
were, and a ``sections`` list is added beside them.  The sections are
built by the card's own builder
(:func:`notifications.cards.build_progress_sections`), so the panel and
the Feishu card cannot disagree about the same plan — and the panel does
not grow a second phrasing of the card's facts.

What is pinned here:

1. **The sections are the card's.**  They are the ones
   ``cards.build_progress_sections`` builds from the very same plan
   status the endpoint read, and they arrive in the card's order.
2. **One fact, one spelling.**  The elapsed time is the same number in
   the top level and in the status section, and the phase's label is
   the same word in both.  A response that carried two spellings of one
   fact is a response the panel has to guess between.
3. **Reading is reading.**  The endpoint writes nothing: the plan's
   files are byte-for-byte the same before the call and after it.

The plan status the endpoint reads is **handed to it** by these tests
(``stub_status``), so that the payload carries exactly the facts a test
is about — a run that started and ended, four tasks, one failed
verification round.  The read-only test does **not** stub it: it reads a
real plan off a real directory, because "reading writes nothing" is a
claim about the real path.

The request carries ``X-PDT-Request: 1`` — the guard every ``/api/*``
request has to satisfy, and the header the bundled UI sends.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: it is the application, and it is what puts the
# routers on the app.  ``routes.plans`` is imported **after** it — the
# two modules import each other, and the application's own import order
# is the one that resolves the cycle.
import server  # noqa: E402
from notifications import cards  # noqa: E402
from routes import plans as routes_plans  # noqa: E402
from routes import scheduler as routes_scheduler  # noqa: E402

client = TestClient(server.app)

#: The endpoint under test.
DETAIL_PATH = "/api/scheduler/plan/{plan_id}/detail"

#: The header every ``/api/*`` request carries (``request_guard``).
GUARD_HEADERS = {"X-PDT-Request": "1"}


@pytest.fixture(autouse=True)
def _isolated_plans_dir(monkeypatch, tmp_path):
    """Point ``PLANS_DIR`` at a tmp tree, and keep the listing cache cold."""
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    (tmp_path / "plans").mkdir(parents=True, exist_ok=True)
    server.invalidate_plans_cache()
    yield
    server.invalidate_plans_cache()


@pytest.fixture
def seed_plan():
    """Seed a plan directory (and its routing row) — and clean it up after.

    The routing table lives in the session-level hermetic ``state.db``,
    so a row written here outlives the test unless it is deleted again.
    The database path is captured at **setup** time: a test that seeds
    no routing row never migrates that database, and the teardown must
    not open a file that is not there.
    """
    from server import _state_db_path

    db_path = _state_db_path(None)
    seeded: List[str] = []

    def _seed(
        plan_id: str,
        *,
        files: Optional[Dict[str, str]] = None,
        phase: Optional[str] = None,
    ) -> str:
        from server import PLANS_DIR

        plan_dir = PLANS_DIR / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)
        for name, content in (files or {}).items():
            (plan_dir / name).write_text(content, encoding="utf-8")
        if phase:
            from state_machine.db.connection import open as open_db
            from state_machine.db.schema import migrate
            from state_machine.repositories.routing_repository import (
                RoutingRepository,
            )

            conn = open_db(db_path)
            try:
                migrate(conn)
                repo = RoutingRepository(conn)
                if repo.find(plan_id) is None:
                    repo.insert(plan_id, phase)
            finally:
                conn.close()
        seeded.append(plan_id)
        return plan_id

    yield _seed

    if seeded:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate

        conn = open_db(db_path)
        try:
            # The schema may not exist yet: a test that seeds no routing
            # row never migrated that database.
            migrate(conn)
            for plan_id in seeded:
                conn.execute(
                    "DELETE FROM plan_routing WHERE plan_id = ?", (plan_id,)
                )
            conn.commit()
        finally:
            conn.close()


@pytest.fixture
def stub_status(monkeypatch):
    """Hand the endpoint the plan status the test wants it to read.

    The endpoint asks ``routes.plans.get_plan_status`` for the plan's
    snapshot; this replaces that one call with the payload the test
    hands in, so that the facts under test (the run's stamps, the task
    counts, the verification round) are the ones the test wrote down.
    """
    def _stub(payload: Dict[str, Any]) -> None:
        monkeypatch.setattr(
            routes_plans,
            "get_plan_status",
            lambda plan_id: dict(payload),
        )

    return _stub


def _plan_status_payload(**overrides: Any) -> Dict[str, Any]:
    """A plan-status-shaped payload — the body of ``/api/plan/{id}/status``.

    It carries every kind of fact the sections are built from: a phase,
    the task counts, a run that started and ended, and a verification
    round that failed.
    """
    payload: Dict[str, Any] = {
        "plan_id": "p1",
        "current_phase": "executing",
        "summary": {
            "state": {"current_phase": "executing"},
            "tasks": {
                "total": 4,
                "completed": 1,
                "in_progress": 1,
                "pending": 2,
                "failed": 0,
            },
        },
        "execution": {
            "started_at": "2026-11-14T10:00:00",
            "ended_at": "2026-11-14T10:01:30",
            "tasks": [
                {"id": "1-1", "title": "写门禁", "status": "in_progress"},
                {"id": "1-2", "title": "跑测试", "status": "pending"},
            ],
        },
        "verification": {
            "verification_status": "failed",
            "verification_round": 2,
            "max_rounds": 4,
            "stop_reason": "same_failure_repeated_after_max_attempts",
            "failed_vps": ["VP-2"],
            "vps": [{"id": "VP-2", "title": "端口"}],
        },
    }
    payload.update(overrides)
    return payload


def _detail(plan_id: str):
    """Ask for one plan's detail."""
    return client.get(
        DETAIL_PATH.format(plan_id=plan_id), headers=dict(GUARD_HEADERS)
    )


def _section(sections: List[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    """The section of ``kind`` — ``None`` when there is no such section."""
    for section in sections:
        if section.get("kind") == kind:
            return section
    return None


def _plan_files_bytes(plan_id: str) -> Dict[str, bytes]:
    """Every file of one plan, read as bytes — the plan's own content."""
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / plan_id
    return {
        str(path.relative_to(plan_dir)): path.read_bytes()
        for path in sorted(plan_dir.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# 1 — the sections are the card's
# ---------------------------------------------------------------------------


def test_detail_returns_sections(stub_status):
    """The answer carries the card's structured sections.

    The sections are the ones ``cards.build_progress_sections`` builds
    from the very same plan status — the same builder the Feishu card
    renders, so the panel and the card cannot say two different things
    about the same plan.
    """
    stub_status(_plan_status_payload())

    response = _detail("p1")

    assert response.status_code == 200, response.text
    body = response.json()

    assert isinstance(body["sections"], list), body
    assert body["sections"], body

    kinds = [section["kind"] for section in body["sections"]]
    assert kinds[0] == "status", kinds
    assert "phase" in kinds, kinds
    assert "task_progress" in kinds, kinds

    # The sections are the card's: the same builder, fed the same
    # status, produces exactly these sections.
    expected = cards.build_progress_sections(
        _plan_status_payload(),
        body["running_sec"],
        body["phase_label"],
    )
    assert body["sections"] == expected, body

    # The lines are still there, and still the card's own.
    assert body["lines"], body
    assert body["lines"] == cards.build_progress_lines(
        _plan_status_payload()
    )["lines"], body


def test_status_section_carries_running_sec(stub_status):
    """The status section carries how long the run has been going.

    The run started at 10:00:00 and ended at 10:01:30 — ninety seconds.
    The number is in the status section (where the card puts it) and in
    the top level (where the panel reads it), and it is the same number
    in both.
    """
    stub_status(_plan_status_payload())

    response = _detail("p1")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["running_sec"] == 90, body

    status_section = body["sections"][0]
    assert status_section["kind"] == "status", status_section
    assert status_section["running_sec"] == 90, status_section


def test_task_progress_section_is_numeric(stub_status):
    """The progress bar's percentage is a number, not a sentence.

    One of four tasks is done, so the bar is at 25 percent — and the
    panel can draw it without parsing a sentence.
    """
    stub_status(_plan_status_payload())

    response = _detail("p1")

    assert response.status_code == 200, response.text
    body = response.json()

    progress = _section(body["sections"], "task_progress")
    assert progress is not None, body

    percent = progress["percent"]
    assert isinstance(percent, int), progress
    assert 0 <= percent <= 100, progress
    assert percent == 25, progress

    assert progress["counts"]["total"] == 4, progress
    assert progress["counts"]["completed"] == 1, progress


def test_verification_section_fields(stub_status):
    """The verification section carries the round's facts.

    The round's status, its number, its bound, why it stopped and the
    VPs that failed it — the five facts the panel shows about the round.
    """
    stub_status(_plan_status_payload())

    response = _detail("p1")

    assert response.status_code == 200, response.text
    body = response.json()

    verification = _section(body["sections"], "verification")
    assert verification is not None, body

    assert verification["status"] == "failed", verification
    assert verification["round"] == 2, verification
    assert verification["max_rounds"] == 4, verification
    assert verification["stop_reason"] == (
        "same_failure_repeated_after_max_attempts"
    ), verification
    assert verification["failed_vps"] == [
        {"id": "VP-2", "title": "端口"}
    ], verification


def test_phase_label_is_single_source(stub_status):
    """One phase, one label — in the top level and in the sections.

    The label is the shared table's (``routes.plans._phase_label``), and
    it is passed to the card's builder explicitly, so the response
    cannot answer "executing" at the top level and "▶️ 执行中" in the
    status section: two spellings of one fact, and a panel that has to
    guess which one to draw.
    """
    stub_status(_plan_status_payload())

    response = _detail("p1")

    assert response.status_code == 200, response.text
    body = response.json()

    status_section = body["sections"][0]
    assert status_section["kind"] == "status", body
    assert status_section["phase_label"] == body["phase_label"], body
    assert body["phase_label"] == routes_plans._phase_label("executing"), body


# ---------------------------------------------------------------------------
# 2 — the elapsed time, read the one way
# ---------------------------------------------------------------------------


def test_running_sec_reads_the_execution_block():
    """The run's stamps are read off the execution block."""
    assert routes_scheduler._detail_running_sec({
        "execution": {
            "started_at": "2026-11-14T10:00:00",
            "ended_at": "2026-11-14T10:01:30",
        },
    }) == 90


def test_running_sec_falls_back_to_the_summary_block():
    """A payload whose execution block has no stamps reads the summary's."""
    assert routes_scheduler._detail_running_sec({
        "summary": {
            "execution": {
                "started_at": "2026-11-14T10:00:00",
                "ended_at": "2026-11-14T10:01:30",
            },
        },
    }) == 90


def test_running_sec_is_unknown_without_a_start():
    """A plan whose run never started has no elapsed time — not zero."""
    assert routes_scheduler._detail_running_sec({}) is None
    assert routes_scheduler._detail_running_sec({"execution": {}}) is None


def test_running_sec_is_unknown_for_an_unreadable_stamp():
    """A stamp no parser accepts is unknown, not a wrong number."""
    assert routes_scheduler._detail_running_sec({
        "execution": {"started_at": "yesterday"},
    }) is None
    assert routes_scheduler._detail_running_sec({
        "execution": {
            "started_at": "2026-11-14T10:00:00",
            "ended_at": "tomorrow",
        },
    }) is None


def test_running_sec_clamps_a_negative_duration():
    """A run that ended before it started reads as zero, not as a debt."""
    assert routes_scheduler._detail_running_sec({
        "execution": {
            "started_at": "2026-11-14T10:01:30",
            "ended_at": "2026-11-14T10:00:00",
        },
    }) == 0


def test_running_sec_counts_to_now_when_the_run_has_not_ended():
    """A run that is still going is measured against the current time."""
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)

    seconds = routes_scheduler._detail_running_sec({
        "execution": {"started_at": "2026-01-01T00:00:00"},
    })

    expected = int((datetime.now(timezone.utc) - started).total_seconds())
    assert seconds is not None
    assert abs(seconds - expected) <= 2, (seconds, expected)


# ---------------------------------------------------------------------------
# 3 — reading writes nothing
# ---------------------------------------------------------------------------


def test_detail_stays_read_only(seed_plan):
    """Opening a plan does not change a byte of it.

    The endpoint is a **read**: it asks the plan's status for where the
    plan is and hands the card's wording on.  Every file of the plan is
    byte-for-byte the same before the call and after it — a detail
    endpoint that wrote to the plan would be a second writer of the
    plan's state.
    """
    plan_id = seed_plan(
        "plan-detail-sections-read-only",
        files={
            "tasks.json": "[]",
            "summary.json": '{"tasks": {"total": 1, "completed": 1}}',
        },
        phase="executing",
    )

    before = _plan_files_bytes(plan_id)

    response = _detail(plan_id)
    assert response.status_code == 200, response.text

    after = _plan_files_bytes(plan_id)

    assert before, "the plan has files to compare"
    assert after == before, (before, after)


def test_unknown_plan_404(seed_plan):
    """A plan that is not there is still the same 404.

    The panel asks about a plan that is not there; the answer says so
    instead of handing back an empty detail, and the wording is the one
    the panel already knows.
    """
    # One registered plan, so the tree this instance serves is searchable
    # and the state database holds rows of its own — the situation in
    # which "not found" is a fact this instance may state.
    seed_plan(
        "plan-detail-sections-known",
        files={"tasks.json": "[]"},
        phase="executing",
    )

    response = _detail("plan-detail-sections-unknown")

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "plan not found"}
