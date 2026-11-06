"""Unit tests for the port's *generate repair tasks* path.

Background
----------
When the terminal round asks a human what to do next, one of the answers
is *generate repair tasks* — "the deliverable is wrong, go and fix it".
That answer used to have a name and no implementation: the executor
called ``port.generate_repair_tasks(...)`` and nothing happened, so the
plan sat in ``verification_failed`` with nobody working on it.

This file pins the port half of the fix.  The chain it has to close is:

1. **The sidecar's failure scene becomes failed-VP records.**
   :func:`failed_vps_from_record` maps the record's ``failed_points``
   onto the shape :class:`repair_generator.RepairTaskGenerator` reads.
2. **The round is the record's round plus one.**  The record says which
   round *stopped*; the repair is the round that comes *after* it.
3. **The rows that reach the database carry no ``status``.**
   :meth:`PlanTaskRepository.add_task` rejects a payload with a field it
   does not know — ``status`` is the runtime mirror's field, not the
   static one — so it is stripped on the way in.
4. **The count that comes back is the number of rows written.**
5. **An empty record manufactures nothing.**  No failed points means no
   repair: the generator is not called at all, because a call that has
   nothing to repair is a call that can only produce an empty repair.

Everything here runs without a model and without the operator's
database: the generator and the writer are injected, and the one test
that touches the database points ``PDT_STATE_DB_PATH`` at a throwaway
file.

Test command::

    pytest backend/tests/unit/test_terminal_decision_port_repair.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


# Ensure backend/ is on sys.path so ``import hitl.terminal_decision_port``
# works regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _port_module():
    """The module under test.

    Imported inside the test bodies so a missing module is a FAILURE
    with the real reason rather than a collection ERROR.
    """
    try:
        from hitl import terminal_decision_port
    except ImportError as exc:  # pragma: no cover - the RED phase
        pytest.fail(f"hitl.terminal_decision_port is not importable: {exc}")
    return terminal_decision_port


#: The sidecar record the terminal round leaves behind: which round
#: stopped, and what failed in it.
_RECORD: Dict[str, Any] = {
    "round_number": 3,
    "failed_points": [
        {
            "id": "VP-3",
            "title": "登录接口",
            "expected_result": "返回 200",
            "failure_class": "deliverable_defect",
            "consecutive_rounds": 3,
            "verdict": "expected 200, got 500",
        },
        {
            "id": "VP-4",
            "title": "登出接口",
            "expected_result": "返回 204",
            "failure_class": "deliverable_defect",
            "consecutive_rounds": 2,
            "verdict": "expected 204, got 404",
        },
    ],
}


def _content_for(vp_id: str) -> Dict[str, Any]:
    """One repair-content row, as the generator would have produced it.

    ``files_to_modify`` is not optional: the assembler refuses a row
    without a declared blast radius, so a content row that omits it
    never becomes a task.
    """
    return {
        "failed_vp_id": vp_id,
        "title": f"修复 {vp_id}",
        "description": f"{vp_id} 连续失败，需要修复根因。",
        "acceptance_criteria": "失败用例转绿",
        "files_to_modify": ["backend/app.py"],
    }


class _FakeGenerator:
    """A repair generator that answers without a model.

    Records every call, so a test can assert both *that* the generator
    was asked and *with what*.
    """

    def __init__(self, contents: List[Dict[str, Any]] = None) -> None:
        self.contents = (
            contents
            if contents is not None
            else [_content_for(point["id"]) for point in _RECORD["failed_points"]]
        )
        self.calls: List[Dict[str, Any]] = []

    def generate_repair_contents(
        self, failed_vps: List[Dict[str, Any]], round_number: int, **kwargs: Any
    ) -> List[Dict[str, Any]]:
        self.calls.append(
            {
                "failed_vps": list(failed_vps),
                "round_number": round_number,
                "kwargs": kwargs,
            }
        )
        return [dict(content) for content in self.contents]


class _RecordingWriter:
    """A task writer that records the rows instead of writing them."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, plan_id: str, rows: List[Dict[str, Any]]) -> None:
        self.calls.append(
            {"plan_id": plan_id, "rows": [dict(row) for row in rows]}
        )


def _port(generator: Any, writer: Any, plan_dir: Any = None):
    """A port wired to the fakes above."""
    module = _port_module()
    return module.DefaultTerminalDecisionPort(
        plan_dir_of=lambda plan_id: Path(plan_dir or "/tmp/terminal-port-repair"),
        repair_generator_factory=lambda plan_id: generator,
        task_writer=writer,
    )


# ---------------------------------------------------------------------------
# 1. The record's failed points become failed-VP records
# ---------------------------------------------------------------------------


def test_failed_vps_from_record_maps_fields():
    """Every field of a failed point reaches the generator's shape.

    The mapping is the port's whole contribution to the chain: the
    generator reads ``id`` / ``title`` / ``priority`` / ``actual_result``
    / ``evidence`` / ``expected_result`` / ``failure_class``, and a field
    it cannot find is a field it treats as absent.
    """
    module = _port_module()
    record = {
        "round_number": 3,
        "failed_points": [
            {
                "id": "VP-3",
                "title": "登录接口",
                "expected_result": "返回 200",
                "failure_class": "deliverable_defect",
                "consecutive_rounds": 3,
                "verdict": "expected 200, got 500",
            },
        ],
    }

    assert module.failed_vps_from_record(record) == [
        {
            "id": "VP-3",
            "title": "登录接口",
            "priority": "P1",
            "actual_result": "expected 200, got 500",
            "evidence": [],
            "expected_result": "返回 200",
            "failure_class": "deliverable_defect",
        }
    ]


def test_failed_vps_from_record_defaults_missing_fields():
    """A point that names no verdict and no title still maps.

    The verdict is the failure text; a point that carries none has no
    text to report, and the empty string says exactly that.  The title
    falls back to the id, because a task whose title is empty reads as
    a task nobody wrote.
    """
    module = _port_module()
    record = {
        "round_number": 1,
        "failed_points": [{"id": "VP-9"}],
    }

    mapped = module.failed_vps_from_record(record)

    assert mapped == [
        {
            "id": "VP-9",
            "title": "VP-9",
            "priority": "P1",
            "actual_result": "",
            "evidence": [],
            "expected_result": "",
            "failure_class": "deliverable_defect",
        }
    ]


# ---------------------------------------------------------------------------
# 2. The round the generator is asked for
# ---------------------------------------------------------------------------


def test_round_is_record_round_plus_one():
    """The repair is the round *after* the one that stopped.

    The record says which round stopped; the repair belongs to the next
    one, so a record from round 3 asks the generator for round 4.
    """
    generator = _FakeGenerator()
    writer = _RecordingWriter()
    port = _port(generator, writer)

    port.generate_repair_tasks("p1", _RECORD)

    assert generator.calls, "the generator was never called"
    assert generator.calls[0]["round_number"] == 4


def test_non_numeric_round_is_treated_as_zero():
    """A round that is not a number is round 0 — so the repair is round 1.

    A record whose round is missing (or carries something that is not a
    number) must not become round ``None + 1``; the assembler refuses a
    round below 1, and a repair task with no round cannot be grouped.
    """
    generator = _FakeGenerator()
    writer = _RecordingWriter()
    port = _port(generator, writer)

    port.generate_repair_tasks("p1", {"round_number": "第三轮",
                                      "failed_points": _RECORD["failed_points"]})

    assert generator.calls, "the generator was never called"
    assert generator.calls[0]["round_number"] == 1


# ---------------------------------------------------------------------------
# 3. What reaches the database
# ---------------------------------------------------------------------------


def test_rows_written_without_status():
    """No row carries ``status`` on the way into the database.

    ``add_task`` **rejects** a payload with a field it does not know, so
    a row that kept the assembler's ``status='pending'`` would take the
    whole write down with it.
    """
    generator = _FakeGenerator()
    writer = _RecordingWriter()
    port = _port(generator, writer)

    port.generate_repair_tasks("p1", _RECORD)

    assert writer.calls, "the writer was never called"
    rows = writer.calls[0]["rows"]
    assert rows, "no rows reached the writer"
    for row in rows:
        assert "status" not in row, f"row still carries status: {row!r}"


def test_returns_row_count():
    """The count is the number of rows written."""
    generator = _FakeGenerator()
    writer = _RecordingWriter()
    port = _port(generator, writer)

    assert port.generate_repair_tasks("p1", _RECORD) == 2
    assert len(writer.calls[0]["rows"]) == 2


# ---------------------------------------------------------------------------
# 4. Nothing to repair means nothing is generated
# ---------------------------------------------------------------------------


def test_empty_record_skips_generator():
    """An empty failure list manufactures no repair at all.

    Not "a repair with nothing in it": the generator is not called, and
    the writer is not called either.  A call that has nothing to repair
    can only produce an empty repair, and an empty repair task in the
    queue is work the executor has to pick up and throw away.
    """
    generator = _FakeGenerator()
    writer = _RecordingWriter()
    port = _port(generator, writer)

    count = port.generate_repair_tasks(
        "p1", {"round_number": 3, "failed_points": []}
    )

    assert count == 0
    assert generator.calls == [], "the generator was called for an empty record"
    assert writer.calls == [], "the writer was called for an empty record"


def test_missing_record_returns_zero():
    """A record that is not there is not an error — it is nothing to do."""
    generator = _FakeGenerator()
    writer = _RecordingWriter()
    port = _port(generator, writer)

    assert port.generate_repair_tasks("p1", None) == 0
    assert generator.calls == []
    assert writer.calls == []


def test_writer_failure_propagates():
    """A queueing failure is not swallowed here.

    The port is not the layer that records failures — the executor is
    (arch decision point 5: *read it or degrade, never raise*).  If the
    port swallowed the failure, the executor would never hear about it
    and the plan would sit in ``verification_failed`` with nobody
    working on it and nobody told.
    """
    generator = _FakeGenerator()

    def _failing_writer(plan_id, rows):
        raise RuntimeError("state.db is locked")

    port = _port(generator, _failing_writer)

    with pytest.raises(RuntimeError, match="state.db is locked"):
        port.generate_repair_tasks("p1", _RECORD)


# ---------------------------------------------------------------------------
# 5. The default writer really writes
# ---------------------------------------------------------------------------


def test_default_writer_persists_rows_to_state_db(tmp_path, monkeypatch):
    """The default writer puts the rows into ``state.db``'s ``plan_tasks``.

    This is the end of the chain the port exists for: the executor's
    ``_get_pending_repair_tasks`` reads ``plan_tasks``, so a row that
    never reached the table is a repair nobody runs.  The database here
    is a throwaway file — ``PDT_STATE_DB_PATH`` is pointed at one — so
    the operator's own database is never in reach.
    """
    module = _port_module()
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))

    generator = _FakeGenerator()
    port = module.DefaultTerminalDecisionPort(
        plan_dir_of=lambda plan_id: tmp_path,
        repair_generator_factory=lambda plan_id: generator,
    )

    count = port.generate_repair_tasks("p1", _RECORD)
    assert count == 2

    from state_machine.db.connection import open as open_db
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(db_path)
    try:
        rows = PlanTaskRepository(conn).load_all("p1")
    finally:
        conn.close()

    assert len(rows) == 2, f"the rows did not reach plan_tasks: {rows!r}"
    for task_id, row in rows.items():
        assert task_id.startswith("repair-"), f"not a repair task id: {task_id!r}"
        # ``add_task`` owns the runtime status — a new row starts pending.
        assert row.get("status") == "pending", f"unexpected status: {row!r}"
