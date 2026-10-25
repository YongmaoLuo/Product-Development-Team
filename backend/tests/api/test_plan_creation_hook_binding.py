"""Plan creation binds a plan to the session that created it.

A human-in-the-loop decision is only useful if somebody is actually
asked, and *who* is asked is a property of the plan rather than of the
backend: a plan imported from a Claude Code session asks the group that
session lives in, and a plan created by a bare ``curl`` has no session
and therefore nobody to ask.

Both plan-creation entry points — ``POST /api/plan/import`` (the fast
lane) and ``POST /api/interview/start`` (the interview) — therefore
record that origin in the plan's own ``hitl_config.json``.  The reader
and the writer of that file live in ``hooks.config`` and nowhere else;
these routes are the *callers* that hand the origin over.

What this file pins:

1. ``test_import_writes_hook`` — the fast lane records the origin it
   was given, and the file lands in the plan's own directory.
2. ``test_import_without_origin_empty`` — a plan created without an
   origin asks nobody: the hook list is empty, and the creation still
   succeeds.
3. ``test_interview_writes_hook`` — the interview entry point records
   it too.
4. ``test_interview_without_origin_empty`` — and it degrades the same
   way the fast lane does when there is no origin to record.
5. ``test_hook_write_failure_does_not_fail_creation`` — a hook
   configuration that cannot be persisted is logged, not fatal: the
   plan is still created.
6. ``test_interview_hook_write_failure_does_not_fail_creation`` — the
   same, on the interview door: the failure is a property of the
   binding, not of the door.

The assertions read the file back through ``hooks.config.load_for_plan``
— the reader of record — and check the file's existence directly, so a
writer that stored the hooks somewhere else fails here rather than in
production.

No model takes part in either route: the coding tool and the interviewer
are replaced per test.
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
from hooks.config import HITL_CONFIG_FILENAME, load_for_plan  # noqa: E402


client = TestClient(server.app)


# ---------------------------------------------------------------------------
# The fakes: neither route is allowed to call a model
# ---------------------------------------------------------------------------


class FakeCodingTool:
    """Stands in for ``CodingTool``; scripts the two calls the generator makes.

    ``query_json`` is the synthesis and ``query`` is the mandatory
    self-review.  The self-review reply echoes the synthesis payload, so
    the generator finds nothing to fix and keeps the list as it is.
    """

    def __init__(self, reply):
        self._reply = reply

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        return self._reply

    def query(self, prompt: str) -> str:
        return json.dumps(
            {"findings": [], "fixed_content": self._reply},
            ensure_ascii=False,
        )


class FakeInterviewer:
    """Stands in for ``Interviewer``: one question, and nothing else."""

    def __init__(self, coding_tool, plan_dir):
        self.coding_tool = coding_tool
        self.plan_dir = plan_dir

    def start(self, requirement):
        return [{"dimension": "goal", "question": "what exactly?"}]

    def is_complete(self):
        return False

    def get_dimensions(self):
        return []


@pytest.fixture
def checkout(tmp_path):
    """A real directory for the tasks to target.

    ``harden_for_execution`` refuses a plan whose ``project_dir`` does not
    exist, and the import route runs that gate — so a fixture pointing
    at a made-up path would be testing the 422 branch on the happy path.
    """
    path = tmp_path / "checkout"
    (path / "backend").mkdir(parents=True)
    for name in ("chart.py", "reporting_view.py"):
        (path / "backend" / name).write_text("# existing\n", encoding="utf-8")
    return path


def _tasks_payload():
    """A task list a model would plausibly emit, and one that passes."""
    return {
        "requirement": "add a daily grouped bar chart to the reporting view",
        "tasks": [
            {
                "id": "T1",
                "title": "add the chart renderer",
                "description": "## 背景\nthe reporting view has no chart",
                "files_to_modify": ["backend/chart.py"],
                "test_command": "pytest -q backend/tests/test_chart.py",
            },
        ],
    }


def _install_coding_tool(monkeypatch, reply):
    """Point ``server.create_coding_tool`` at a fake."""
    tool = FakeCodingTool(reply)
    monkeypatch.setattr(server, "create_coding_tool", lambda **kwargs: tool)
    return tool


@pytest.fixture
def interview(monkeypatch):
    """Make ``POST /api/interview/start`` answer without a model."""
    monkeypatch.setattr(server, "create_coding_tool", lambda **kwargs: object())
    monkeypatch.setattr("routes.phases.Interviewer", FakeInterviewer)


# ---------------------------------------------------------------------------
# The fast lane
# ---------------------------------------------------------------------------


def test_import_writes_hook(isolated_plans_dir, checkout, monkeypatch):
    """The fast lane records the origin the caller handed it.

    The hook is a *plan* property, so it lands in the plan's own
    directory next to the rest of that plan's state — not in a registry
    somewhere else.
    """
    _install_coding_tool(monkeypatch, _tasks_payload())

    response = client.post(
        "/api/plan/import",
        json={
            "project_dir": str(checkout),
            "content": "add a chart to the reporting view",
            "title": "hook binding",
            "hook_destination": "grp-1",
            "hook_session_id": "s-1",
        },
    )

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]
    plan_dir = isolated_plans_dir / plan_id

    assert (plan_dir / HITL_CONFIG_FILENAME).exists(), (
        f"the hook configuration must be written into the plan's own "
        f"directory: {plan_dir!s}"
    )
    assert load_for_plan(plan_dir)["hooks"] == [
        {"kind": "cc_connect", "destination": "grp-1", "session_id": "s-1"}
    ]


def test_import_without_origin_empty(isolated_plans_dir, checkout, monkeypatch):
    """A plan created outside a session asks nobody — and is still a plan.

    No session and no destination is not an error: it is the answer for a
    plan created by a bare ``curl``, and the plan is created all the same.
    """
    _install_coding_tool(monkeypatch, _tasks_payload())

    response = client.post(
        "/api/plan/import",
        json={
            "project_dir": str(checkout),
            "content": "add a chart to the reporting view",
            "title": "no origin",
        },
    )

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]
    plan_dir = isolated_plans_dir / plan_id

    assert (plan_dir / HITL_CONFIG_FILENAME).exists(), (
        f"the absence of an origin is written down, not left implicit: "
        f"{plan_dir!s}"
    )
    assert load_for_plan(plan_dir)["hooks"] == []


# ---------------------------------------------------------------------------
# The interview
# ---------------------------------------------------------------------------


def test_interview_writes_hook(isolated_plans_dir, interview):
    """The interview entry point records the origin too.

    Two doors into the same plan must answer the same question the same
    way: a plan created by the interview asks the same group as a plan
    created by the fast lane, because both were created by the same
    session.
    """
    response = client.post(
        "/api/interview/start",
        json={
            "requirement": "add a chart to the reporting view",
            "hook_destination": "grp-1",
            "hook_session_id": "s-1",
        },
    )

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]
    plan_dir = isolated_plans_dir / plan_id

    assert (plan_dir / HITL_CONFIG_FILENAME).exists(), (
        f"the hook configuration must be written into the plan's own "
        f"directory: {plan_dir!s}"
    )
    assert load_for_plan(plan_dir)["hooks"] == [
        {"kind": "cc_connect", "destination": "grp-1", "session_id": "s-1"}
    ]


def test_interview_without_origin_empty(isolated_plans_dir, interview):
    """The interview door degrades the way the fast lane degrades.

    A plan started outside a session asks nobody, and is still a plan.
    The two doors must answer this the same way: a caller that omits the
    two fields gets a plan whose hook list is empty, not an error and not
    a plan that was never created.
    """
    response = client.post(
        "/api/interview/start",
        json={"requirement": "add a chart to the reporting view"},
    )

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]
    plan_dir = isolated_plans_dir / plan_id

    assert (plan_dir / HITL_CONFIG_FILENAME).exists(), (
        f"the absence of an origin is written down, not left implicit: "
        f"{plan_dir!s}"
    )
    assert load_for_plan(plan_dir)["hooks"] == []


# ---------------------------------------------------------------------------
# A binding that cannot be written is not a failed plan
# ---------------------------------------------------------------------------


def test_hook_write_failure_does_not_fail_creation(
    isolated_plans_dir, checkout, monkeypatch, caplog
):
    """A hook configuration that cannot be persisted is logged, not fatal.

    A plan whose hook configuration cannot be written is still a plan,
    and a plan with no hooks is a state the plan already knows how to be
    in.  A failed plan creation, on the other hand, is a plan that does
    not exist — so the write is reported loudly and the creation
    continues.
    """
    import routes.fast_lane as fast_lane

    def _boom(*args, **kwargs):
        raise RuntimeError("the disk is on fire")

    monkeypatch.setattr(fast_lane, "write_for_plan", _boom)
    _install_coding_tool(monkeypatch, _tasks_payload())

    response = client.post(
        "/api/plan/import",
        json={
            "project_dir": str(checkout),
            "content": "add a chart to the reporting view",
            "title": "unwritable hook",
            "hook_destination": "grp-1",
            "hook_session_id": "s-1",
        },
    )

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]
    assert (isolated_plans_dir / plan_id / "tasks.json").exists(), (
        "the plan must exist even when its hook configuration cannot be "
        "written"
    )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "hitl_hook_binding_failed" in logged, (
        f"the failed binding must be logged; captured log: {logged!r}"
    )


def test_interview_hook_write_failure_does_not_fail_creation(
    isolated_plans_dir, interview, monkeypatch, caplog
):
    """The interview door degrades the same way the fast lane degrades.

    The failure is a property of the *binding*, not of the door: a plan
    whose hook configuration cannot be persisted is still a plan, on
    either door.  So the interview must answer 200 and log the failure
    rather than refusing to create the plan.
    """
    import routes.fast_lane as fast_lane

    def _boom(*args, **kwargs):
        raise RuntimeError("the disk is on fire")

    monkeypatch.setattr(fast_lane, "write_for_plan", _boom)

    response = client.post(
        "/api/interview/start",
        json={
            "requirement": "add a chart to the reporting view",
            "hook_destination": "grp-1",
            "hook_session_id": "s-1",
        },
    )

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]
    assert (isolated_plans_dir / plan_id).is_dir(), (
        "the plan must exist even when its hook configuration cannot be "
        "written"
    )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "hitl_hook_binding_failed" in logged, (
        f"the failed binding must be logged; captured log: {logged!r}"
    )