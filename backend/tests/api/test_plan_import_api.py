"""``POST /api/plan/import`` — the fast lane's HTTP contract.

The lane's promise is narrow and has four parts, each pinned here from
the production entry point (a real request against the mounted app, never
a direct call into ``routes.fast_lane.import_plan``):

1. **The caller's material becomes a plan.** Whatever shape it arrived in
   — a string, a bare array, an object with keys this repository has never
   heard of — it is stored verbatim, and a task list obeying PDT's
   constraints comes back out of a *model*. The ``FakeCodingTool`` below
   asserts it was asked: a lane that quietly mapped the caller's fields
   into ``SubTask`` fields itself would pass every other assertion here
   while never calling anything.
2. **A broken answer is refused, per problem.** A 422 whose body lists
   what to fix, and which never reaches ``queued``.
3. **Nothing runs.** The plan stops in ``queued``. Asserted three ways,
   because "did not start execution" is the kind of claim a single green
   assertion cannot support: the plan's phase, the absence of a
   ``plan_execution`` row, and the absence of any entry in the server's
   in-process execution map.
4. **The request guard applies.** A *new* route is exactly where a bypass
   would be added: the header is checked by middleware, so a route that
   escaped the middleware would answer 200 to the one caller that should
   not have reached it.

No real LLM is involved: ``server.create_coding_tool`` is monkeypatched
per-test to return a fake that scripts the two calls the generator makes.

Isolation is the autouse ``isolated_plans_dir`` fixture: ``PLANS_DIR`` and
the state database are both redirected at ``tmp_path``, so this file never
reads the operator's live ``plans/`` tree or ``state.db``.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE  # noqa: E402
from task import UNKNOWN_MODIFICATIONS_SENTINEL, SubTask  # noqa: E402


client = TestClient(server.app)


# ---------------------------------------------------------------------------
# The fake coding tool
# ---------------------------------------------------------------------------


class FakeCodingTool:
    """Stands in for ``CodingTool``; records both calls the generator makes.

    ``query_json`` is the synthesis and ``query`` is the mandatory
    self-review. The self-review reply echoes the synthesis payload *by
    reference*, which is what the generator mutates in place while it adds
    ``depends_on`` and resolves ``files_to_modify`` — so the fake always
    reviews the list as it stands at that moment, and the review comes
    back as the idempotent "reviewed it, found nothing" rewrite the
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

    ``harden_for_execution`` refuses a plan whose ``project_dir`` does not
    exist, and the route runs that gate — so a fixture that pointed at a
    made-up path would be testing the 422 branch on the happy path.
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


@pytest.fixture
def plans_root(tmp_path):
    """The plans tree this test writes into (already redirected)."""
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _post(payload, **overrides):
    body = dict(payload)
    body.update(overrides)
    return client.post("/api/plan/import", json=body)


# ---------------------------------------------------------------------------
# 1 — the caller's material becomes a plan
# ---------------------------------------------------------------------------

#: One payload per shape the request contract promises to accept. The
#: object is made of keys this repository has never heard of on purpose.
SHAPES = [
    ("prose", "把 CSV 报表加上按天分组的柱状图，入口在 /reports"),
    ("bare_array", [
        {"name": "wire the loader", "notes": "reads csv", "files": ["backend/loader.py"]},
        {"name": "render the chart"},
    ]),
    ("arbitrary_object", {
        "ticket": "PROJ-1234",
        "epic": {"name": "reporting", "owner": "someone"},
        "acceptance": ["chart renders", "no N+1 queries"],
    }),
]


@pytest.mark.parametrize("label,content", SHAPES, ids=[s[0] for s in SHAPES])
def test_material_of_any_shape_lands_as_a_queued_plan(
    plans_root, checkout, monkeypatch, state_db_reader, label, content
):
    """The whole point of the lane, in one request per input shape.

    What the caller sent is not a task list — that is the premise. What
    comes back is one, and it is indistinguishable from a generated one.
    """
    tool = _install(monkeypatch, _tasks_payload())

    response = _post(
        {"project_dir": str(checkout), "content": content},
        title="Fast lane import",
    )

    assert response.status_code == 200, response.text
    payload = response.json()

    # A model was asked. This is the assertion that fails first if the
    # lane ever goes back to mapping fields itself.
    assert len(tool.json_calls) == 1
    assert len(tool.query_calls) >= 1
    assert str(plans_root / payload["plan_id"] / "import.json") in (
        tool.json_calls[0]["prompt"]
    )

    # The plan id is the repository's own derivation, not a new format.
    plan_id = payload["plan_id"]
    assert plan_id.startswith("Fast-lane-") or plan_id[:8].isdigit()
    assert "/" not in plan_id and ".." not in plan_id

    # Written to disk, and readable back as task rows.
    tasks_file = plans_root / plan_id / "tasks.json"
    assert tasks_file.exists()
    saved = json.loads(tasks_file.read_text(encoding="utf-8"))
    parsed = [SubTask(**row) for row in saved["tasks"]]
    assert [t.id for t in parsed] == ["T1", "T2"]
    assert parsed[0].files_to_modify == ["backend/chart.py"]
    assert parsed[0].test_command == "pytest -q backend/tests/test_chart.py"
    assert parsed[1].depends_on == ["T1"]
    assert all(t.project_dir == str(checkout) for t in parsed)

    # Parked, not running.
    assert state_db_reader.routing(plan_id)["current_phase"] == "queued"
    assert state_db_reader.execution(plan_id) is None
    assert plan_id not in server._execution_state
    assert server._execution_state == {}


def test_the_stored_payload_is_exactly_what_the_caller_sent(
    plans_root, checkout, monkeypatch, state_db_reader
):
    """Stored, not interpreted.

    A writer that coerced the bare array into ``{"tasks": [...]}``, or
    dropped the object's unknown keys, would still hand the model
    something readable — and the caller would never learn that half of
    what they sent did not arrive.
    """
    content = {"ticket": "PROJ-7", "weird_key": {"nested": [1, 2]}}
    _install(monkeypatch, _tasks_payload())

    response = _post(
        {"project_dir": str(checkout), "content": content},
        title="verbatim",
    )

    assert response.status_code == 200, response.text
    stored = json.loads(
        (plans_root / response.json()["plan_id"] / "import.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored == content


def test_every_content_alias_is_the_same_slot(plans_root, checkout, monkeypatch):
    """``content`` is documented; the other three are habits. Which one a
    caller reaches for is not a requirement, so none of them is refused."""
    for field in ("content", "tasks", "raw", "requirement"):
        _install(monkeypatch, _tasks_payload())

        response = _post({"project_dir": str(checkout), field: "do the thing"})

        assert response.status_code == 200, (field, response.text)
        assert response.json()["source_field"] == field


def test_the_import_prompt_carries_the_whole_task_schema(
    plans_root, checkout, monkeypatch
):
    """Import mode changes what the model is shown, not what it owes.

    A lane that shipped a trimmed schema would produce plans the executor
    refuses, and the failure would land at dispatch time rather than here.
    """
    tool = _install(monkeypatch, _tasks_payload())

    response = _post(
        {"project_dir": str(checkout), "content": "do the thing"},
        title="schema",
    )

    assert response.status_code == 200, response.text
    system = tool.json_calls[0]["system_instruction"]
    assert "上游输入**不是 PRD**" in system
    assert f"- `{checkout}`" in system
    assert "不要输出 `project_dir` 字段" not in system


def test_the_response_echoes_the_rows_that_were_written(plans_root, checkout, monkeypatch):
    """What the caller reads back is what the executor will read."""
    _install(monkeypatch, _tasks_payload())

    response = _post({"project_dir": str(checkout), "content": "do the thing"})

    assert response.status_code == 200, response.text
    payload = response.json()
    saved = json.loads(
        (plans_root / payload["plan_id"] / "tasks.json").read_text(encoding="utf-8")
    )

    assert payload["tasks"] == saved["tasks"]


def test_a_report_task_is_imported_with_its_deliverable(plans_root, checkout, monkeypatch):
    """The lane must not flatten a report task into a code task.

    A report task's whole meaning is the pair (``deliverable="report"``,
    ``report_path``); dropping the path would leave a task the executor
    can never complete.
    """
    _install(monkeypatch, _tasks_payload(tasks=[{
        "id": "R1",
        "title": "assemble the evidence",
        "description": "read-only investigation",
        "deliverable": "report",
        "report_path": "/tmp/fast-lane-evidence.md",
    }]))

    response = _post({"project_dir": str(checkout), "content": "investigate"})

    assert response.status_code == 200, response.text
    saved = json.loads(
        (plans_root / response.json()["plan_id"] / "tasks.json").read_text(
            encoding="utf-8"
        )
    )
    row = saved["tasks"][0]

    assert row["deliverable"] == "report"
    assert row["report_path"] == "/tmp/fast-lane-evidence.md"


def test_a_task_whose_files_were_never_resolved_lands_as_a_warning(
    plans_root, checkout, monkeypatch
):
    """``__UNKNOWN_MODIFICATIONS__`` means "meant to change something,
    never said what" — the executor can still work around it, so it is a
    warning the caller reads, not a 422."""
    _install(monkeypatch, _tasks_payload(tasks=[{
        "id": "T1",
        "title": "do the thing",
        "description": "the caller never said which files",
        "files_to_modify": list(UNKNOWN_MODIFICATIONS_SENTINEL),
        "test_command": "pytest -q",
    }]))

    response = _post({"project_dir": str(checkout), "content": "do the thing"})

    assert response.status_code == 200, response.text
    assert any(
        "files_to_modify was never resolved" in warning
        for warning in response.json()["warnings"]
    )


def test_a_second_import_of_the_same_title_does_not_overwrite_the_first(
    plans_root, checkout, monkeypatch
):
    _install(monkeypatch, _tasks_payload())
    first = _post({"project_dir": str(checkout), "content": "a"}, title="same")
    _install(monkeypatch, _tasks_payload())
    second = _post({"project_dir": str(checkout), "content": "b"}, title="same")

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["plan_id"] != second.json()["plan_id"]
    assert (plans_root / first.json()["plan_id"] / "tasks.json").exists()
    assert (plans_root / second.json()["plan_id"] / "tasks.json").exists()
    assert any(
        "already exists" in warning for warning in second.json()["warnings"]
    )


# ---------------------------------------------------------------------------
# 2 — a refused import never becomes a plan
# ---------------------------------------------------------------------------


def _assert_not_queued(plan_id, state_db_reader):
    """``queued`` is the one phase the lane may park a plan in, so its
    absence is the whole "nothing happened" claim, read from the store."""
    routing = state_db_reader.routing(plan_id)
    assert routing is None or routing["current_phase"] != "queued"
    assert state_db_reader.execution(plan_id) is None


def test_a_model_that_returned_no_tasks_is_refused(
    plans_root, checkout, monkeypatch, state_db_reader
):
    _install(monkeypatch, {"tasks": []})

    response = _post({"project_dir": str(checkout), "content": "do the thing"})

    assert response.status_code == 422
    assert "0 tasks" in response.json()["detail"]
    _assert_not_queued(response.json()["plan_id"], state_db_reader)


def test_a_generated_list_that_fails_the_audit_is_refused(
    plans_root, checkout, monkeypatch, state_db_reader
):
    """Generation succeeded; the graph is what the executor cannot run."""
    _install(monkeypatch, _tasks_payload(tasks=[
        {
            "id": "A", "title": "a", "description": "d",
            "files_to_modify": ["backend/a.py"], "test_command": "pytest -q",
            "depends_on": ["B"],
        },
        {
            "id": "B", "title": "b", "description": "d",
            "files_to_modify": ["backend/b.py"], "test_command": "pytest -q",
            "depends_on": ["A"],
        },
    ]))

    response = _post({"project_dir": str(checkout), "content": "do the thing"})

    assert response.status_code == 422
    assert response.json()["problems"] == ["dependency cycle between tasks: A, B"]
    _assert_not_queued(response.json()["plan_id"], state_db_reader)


def test_a_generated_task_with_no_test_command_is_refused(
    plans_root, checkout, monkeypatch, state_db_reader
):
    _install(monkeypatch, _tasks_payload(tasks=[{
        "id": "T1", "title": "a", "description": "d",
        "files_to_modify": ["backend/a.py"],
    }]))

    response = _post({"project_dir": str(checkout), "content": "do the thing"})

    assert response.status_code == 422
    assert any(
        "needs a test_command" in problem
        for problem in response.json()["problems"]
    )
    _assert_not_queued(response.json()["plan_id"], state_db_reader)


def test_a_generated_task_naming_another_checkout_is_refused(
    plans_root, checkout, monkeypatch, state_db_reader
):
    _install(monkeypatch, _tasks_payload(tasks=[{
        "id": "T1", "title": "a", "description": "d",
        "files_to_modify": ["backend/a.py"], "test_command": "pytest -q",
        "project_dir": "/somewhere/else",
    }]))

    response = _post({"project_dir": str(checkout), "content": "do the thing"})

    assert response.status_code == 422
    assert any(
        "differs from the request's" in problem
        for problem in response.json()["problems"]
    )
    _assert_not_queued(response.json()["plan_id"], state_db_reader)


def test_a_generated_task_pointing_nowhere_is_refused(
    plans_root, checkout, monkeypatch, state_db_reader
):
    """``harden_for_execution`` is the last gate, and this route runs it.

    Every task targets the checkout the request named — so nothing about
    the list itself is wrong — but the checkout is not on this machine.
    That is a plan the executor would refuse at start, and finding it out
    at import is the point of the lane.
    """
    _install(monkeypatch, _tasks_payload())
    shutil.rmtree(checkout)

    response = _post({"project_dir": str(checkout), "content": "do the thing"})

    assert response.status_code == 422
    assert response.json()["error"] == "Generated tasks are not runnable"
    assert "project_dir does not exist" in str(response.json()["error_payload"])
    _assert_not_queued(response.json()["plan_id"], state_db_reader)


def test_a_list_without_a_project_dir_is_refused(plans_root, monkeypatch):
    _install(monkeypatch, _tasks_payload())

    response = client.post("/api/plan/import", json={"content": "do the thing"})

    assert response.status_code == 400
    assert any(
        "project_dir is required" in problem
        for problem in response.json()["problems"]
    )
    assert list(plans_root.iterdir()) == []


def test_a_request_without_any_material_is_refused(plans_root, checkout, monkeypatch):
    _install(monkeypatch, _tasks_payload())

    response = client.post(
        "/api/plan/import", json={"project_dir": str(checkout)}
    )

    assert response.status_code == 400
    assert any(
        "content is required" in problem
        for problem in response.json()["problems"]
    )
    assert list(plans_root.iterdir()) == []


# ---------------------------------------------------------------------------
# 3 — the guard, and the "nothing runs" contract
# ---------------------------------------------------------------------------


def test_the_import_route_is_behind_the_request_guard(plans_root, checkout, monkeypatch):
    """No ``X-PDT-Request``, no import — the middleware, not the handler.

    Asserted on the mounted route rather than on a helper, because the
    failure this guards against is precisely a route registered outside
    the guard's reach.
    """
    _install(monkeypatch, _tasks_payload())
    raw = TestClient(server.app)
    raw.headers.pop(REQUEST_HEADER, None)

    response = raw.post(
        "/api/plan/import",
        json={"project_dir": str(checkout), "content": "do the thing"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
    assert list(plans_root.iterdir()) == []


def test_the_route_is_registered_on_the_app_not_only_importable(plans_root):
    """A module that is never mounted answers none of the above."""
    paths = [route.path for route in server.iter_app_routes()]

    assert "/api/plan/import" in paths


def test_the_import_path_never_reaches_the_executor_entry_point():
    """The "does not start execution" claim, stated as a source contract.

    The behavioural assertions above prove this run started nothing; this
    one survives the day a runtime assertion is loosened, because it fails
    the moment someone wires ``start_execution`` (or the spawner behind
    it) into the import route.
    """
    source = (_BACKEND_DIR / "routes" / "fast_lane.py").read_text(encoding="utf-8")

    for forbidden in ("start_execution", "_spawn_executor_subprocess", "subprocess"):
        assert forbidden not in source, (
            f"routes/fast_lane.py references {forbidden!r}; the import lane "
            "must stop at queued and never reach the executor"
        )
