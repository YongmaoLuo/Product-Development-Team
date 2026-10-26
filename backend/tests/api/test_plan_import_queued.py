"""``POST /api/plan/import`` lands the imported plan in ``queued``.

The fast lane is the short way in: the caller already has *something* —
a requirement, notes, a rough list — and the lane turns it into a plan
that the rest of PDT can pick up.  For that to work the imported plan
has to land in the phase the **scheduler** collects from, which is
``queued``.  A plan parked in the old ``ready`` vocabulary is invisible
to the scheduler: it is a plan that never runs and nobody is told why.

Three claims are pinned here, from the production entry point (a real
request against the mounted app, never a direct call into
``routes.fast_lane``):

1. **The import lands in ``queued``** and nothing ran — asserted on the
   routing row, on the absence of a ``plan_execution`` row, and on the
   absence of the plan in the server's in-process execution map.
2. **A plan directory that already exists is never overwritten.** The
   conflict is resolved by giving the second import its own plan id and
   telling the caller, not by writing two plans' history into one
   folder.
3. **A refused import leaves nothing behind.** A model that answered
   with an empty task list produces no plan: no routing row, no
   execution row.

No real LLM is involved: ``server.create_coding_tool`` is monkeypatched
per-test to return a fake that scripts the two calls the generator
makes.  Isolation is the autouse ``isolated_plans_dir`` fixture:
``PLANS_DIR`` and the state database are both redirected at
``tmp_path``, so this file never reads the operator's live ``plans/``
tree or ``state.db``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402


client = TestClient(server.app)


# ---------------------------------------------------------------------------
# The fake coding tool
# ---------------------------------------------------------------------------


class FakeCodingTool:
    """Stands in for ``CodingTool``; scripts the two calls the generator makes.

    ``query_json`` is the synthesis and ``query`` is the mandatory
    self-review.  The self-review reply echoes the synthesis payload *by
    reference*, which is what the generator mutates in place while it
    adds ``depends_on`` and resolves ``files_to_modify`` — so the fake
    always reviews the list as it stands at that moment, and the review
    comes back as the idempotent "reviewed it, found nothing" rewrite the
    generator already accepts.
    """

    def __init__(self, reply):
        self._reply = reply
        self.json_calls = []
        self.query_calls = []

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.json_calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
        })
        return self._reply

    def query(self, prompt: str) -> str:
        self.query_calls.append(prompt)
        return json.dumps(
            {"findings": [], "fixed_content": self._reply},
            ensure_ascii=False,
        )


@pytest.fixture(autouse=True)
def no_probe(monkeypatch):
    """The RED probe would run the generated test commands for real."""
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")


@pytest.fixture
def checkout(tmp_path):
    """A real directory for the tasks to target.

    ``harden_for_execution`` refuses a plan whose ``project_dir`` does
    not exist, and the route runs that gate — so a fixture that pointed
    at a made-up path would be testing the 422 branch on the happy
    path.
    """
    path = tmp_path / "checkout"
    (path / "backend").mkdir(parents=True)
    for name in ("chart.py", "reporting_view.py", "loader.py", "a.py", "b.py"):
        (path / "backend" / name).write_text("# existing\n", encoding="utf-8")
    return path


def _tasks_payload(**overrides):
    """A task list a model would plausibly emit, and one that passes."""
    payload = {
        "requirement": "add a daily grouped bar chart to the reporting view",
        "tasks": [
            {
                "id": "T1",
                "title": "add the chart renderer",
                "description": "## 背景\nthe reporting view has no chart",
                "files_to_modify": ["backend/chart.py"],
                "test_command": "pytest -q backend/tests/test_chart.py",
            },
            {
                "id": "T2",
                "title": "wire the renderer into the view",
                "description": "## 目标\nmount the renderer",
                "files_to_modify": ["backend/reporting_view.py"],
                "test_command": "pytest -q",
                "depends_on": ["T1"],
            },
        ],
    }
    payload.update(overrides)
    return payload


def _install(monkeypatch, reply):
    """Point ``server.create_coding_tool`` at a fake and hand it back."""
    tool = FakeCodingTool(reply)
    monkeypatch.setattr(server, "create_coding_tool", lambda **kwargs: tool)
    return tool


def _import(body):
    """One import request, through the mounted app."""
    return client.post("/api/plan/import", json=body)


# ---------------------------------------------------------------------------
# 1 — the import lands in ``queued``, and nothing ran
# ---------------------------------------------------------------------------


def test_import_lands_in_queued(
    isolated_plans_dir, checkout, monkeypatch, state_db_reader, caplog
):
    """The imported plan is in the phase the scheduler collects from.

    ``queued`` is asserted on the routing row — the table the scheduler
    reads — and the "nothing ran" claim is asserted three ways, because
    a single green assertion cannot support it: the phase itself, the
    absence of a ``plan_execution`` row, and the absence of the plan in
    the server's in-process execution map.

    The deprecation warning is part of the same claim.  ``ready`` is the
    historical alias of ``queued``: reading one is legitimate (old rows
    still carry it) but **writing** one is not, and ``plan_state`` says
    so out loud.  A brand-new import is not legacy data, so it must not
    produce that warning at all — before this change the lane seeded
    ``ready`` and let the transition normalise it, so every fresh
    import logged the deprecation on its way in.
    """
    _install(monkeypatch, _tasks_payload())

    response = _import({
        "project_dir": str(checkout),
        "content": "把 CSV 报表加上按天分组的柱状图",
        "title": "Fast lane queued import",
    })

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]

    routing = state_db_reader.routing(plan_id)
    assert routing is not None
    assert routing["current_phase"] == "queued"

    # Nothing ran.
    assert state_db_reader.execution(plan_id) is None
    assert plan_id not in server._execution_state

    # The write path spoke the current vocabulary the whole way in.
    legacy = [
        record.getMessage()
        for record in caplog.records
        if "deprecated phase vocabulary" in record.getMessage()
    ]
    assert legacy == [], (
        "a fresh import must not write the legacy 'ready' vocabulary: "
        f"{legacy}"
    )


# ---------------------------------------------------------------------------
# 2 — a plan directory that already exists is never overwritten
# ---------------------------------------------------------------------------


def test_duplicate_import_still_conflicts(
    isolated_plans_dir, checkout, monkeypatch, state_db_reader
):
    """Two imports of the same material are two plans, not one mess.

    The lane resolves the collision by giving the second import its own
    plan id (``<id>-2``), leaving the first plan's directory exactly as
    it was, and telling the caller that a directory of that name was
    already taken.  The alternative — writing the second import into
    the first plan's directory — would leave one folder holding two
    plans' worth of history, which is a plan nobody can read.

    Both plans come out whole: each has its own task list and its own
    routing row, both parked in ``queued``.
    """
    _install(monkeypatch, _tasks_payload())
    first = _import({
        "project_dir": str(checkout),
        "content": "the first import",
        "title": "Same title",
    })
    _install(monkeypatch, _tasks_payload())
    second = _import({
        "project_dir": str(checkout),
        "content": "the second import",
        "title": "Same title",
    })

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    first_id = first.json()["plan_id"]
    second_id = second.json()["plan_id"]
    assert first_id != second_id

    # The first plan was not touched: its directory still holds exactly
    # its own task list.
    first_tasks = json.loads(
        (isolated_plans_dir / first_id / "tasks.json").read_text(
            encoding="utf-8"
        )
    )
    assert [row["id"] for row in first_tasks["tasks"]] == ["T1", "T2"]
    assert (isolated_plans_dir / second_id / "tasks.json").exists()

    # The caller is told about the collision rather than guessing.
    assert any(
        "already exists" in warning
        for warning in second.json()["warnings"]
    )

    # Neither plan is half-written: both are whole plans in ``queued``.
    assert state_db_reader.routing(first_id)["current_phase"] == "queued"
    assert state_db_reader.routing(second_id)["current_phase"] == "queued"


# ---------------------------------------------------------------------------
# 3 — a seed that could not write its row is not a failed import
# ---------------------------------------------------------------------------


def test_a_failed_seed_still_reports_queued(
    isolated_plans_dir, checkout, monkeypatch, state_db_reader
):
    """The import survives a routing row it could not write itself.

    ``_seed_plan_routing_phase`` swallows its own failures (SQLite
    unavailable → a warning, no row), so the lane has to cope with a
    plan whose routing row was never written by the seed. The on-disk
    inference reads ``tasks.json``'s existence as ``queued`` — the same
    answer — so the plan is still parked where the scheduler looks for
    it, and the caller gets its plan instead of a 500.

    A 500 here would be the worst of both worlds: the plan is on disk
    (its task list is written) but the caller was told the import
    failed, and the next import of the same material would find the
    directory already taken.
    """
    def _seed_that_cannot_write(plan_id, phase):
        """Stand in for a seed that failed: no row, no exception."""
        return None

    monkeypatch.setattr(
        server, "_seed_plan_routing_phase", _seed_that_cannot_write,
    )
    _install(monkeypatch, _tasks_payload())

    response = _import({
        "project_dir": str(checkout),
        "content": "做一个图表页",
    })

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]

    # The plan exists on disk and is parked in the phase the scheduler
    # collects from.
    assert (isolated_plans_dir / plan_id / "tasks.json").exists()
    routing = state_db_reader.routing(plan_id)
    assert routing is not None
    assert routing["current_phase"] == "queued"


# ---------------------------------------------------------------------------
# 4 — a refused import leaves nothing behind
# ---------------------------------------------------------------------------


def test_empty_task_list_is_refused(
    isolated_plans_dir, checkout, monkeypatch, state_db_reader
):
    """A model that answered with no tasks produces no plan.

    The refusal happens before the plan is marked, so there is no
    routing row to find: a plan that was never born has no phase, and a
    plan with a phase but no tasks would be a plan the scheduler picks
    up and cannot run.
    """
    _install(monkeypatch, {"tasks": []})

    response = _import({
        "project_dir": str(checkout),
        "content": "做一个图表页",
    })

    assert response.status_code == 422
    assert "0 tasks" in response.json()["detail"]

    plan_id = response.json()["plan_id"]
    assert state_db_reader.routing(plan_id) is None
    assert state_db_reader.execution(plan_id) is None
