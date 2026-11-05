"""A refused import is registered as ``import_failed`` — it is not an orphan.

The defect this file nails
--------------------------

``POST /api/plan/import`` refuses material it cannot turn into a runnable
plan: the audit (``validate_tasks``) and the two executor gates
(``validate_workspace`` / ``harden_for_execution``) say no, and the caller
gets a 422 — or a 504 when the model call itself timed out.

Before this change the refusal left the plan's **directory** behind —
``import.json``, and the ``tasks.json`` the generator wrote on its way to
the refusal — and nothing else.  No ``plan_routing`` row, no
``plan_execution`` row.  Every reader that goes through the tables then
answered "no such plan": ``resume`` could not find the plan, the listing
did not show it, and the directory on disk was a folder nobody could
account for.

The rule now is the opposite of "clean up after a failure":

* **the directory is evidence, not garbage** — it is kept, because it
  holds the material that was imported and the failure that stopped it;
* **the plan is registered** in the ``import_failed`` phase, so it can be
  found, listed and read;
* **the reason is attached to the plan** — ``plan_execution.stop_reason``
  carries the original error text, unprocessed, so the reason survives the
  HTTP response the caller saw and threw away;
* **and it is still refused to run** — ``import_failed`` is not
  ``queued``, and the scheduler's start-up scan (``_backfill_queue``)
  only ever collects ``queued``.

How these cases drive the code
-------------------------------

Every case goes through the production entry point: a real request
against the mounted app (``POST /api/plan/import``), never a direct call
into ``routes.fast_lane``.  ``server.create_coding_tool`` is
monkeypatched per case to return a fake that scripts the generator's two
calls.  The plan-state database and the plans tree are redirected at
``tmp_path`` by the autouse ``isolated_plans_dir`` fixture, so nothing
here reads or writes the operator's live ``plans/`` tree or ``state.db``.
"""

from __future__ import annotations

import json
import sqlite3
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
    always reviews the list as it stands at that moment.

    ``error`` makes the synthesis call raise instead of answering.  That
    is how a provider failure reaches the route: the generator does not
    wrap what the tool raises, so the route's own ``except`` clause sees
    it.

    ``on_query`` is an optional hook fired while the synthesis is being
    *asked*.  It exists for the one case where the world has to change
    during the import: the CI adapter gate runs before the model is asked
    and the hardening gate runs after, so a case that wants the second
    gate to see a checkout that is gone has to take the checkout away
    here.  See ``test_hardening_failure_registers_import_failed``.
    """

    def __init__(self, reply=None, error=None, on_query=None):
        self._reply = reply
        self._error = error
        self._on_query = on_query
        self.json_calls = []
        self.query_calls = []

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.json_calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
        })
        if self._on_query is not None:
            self._on_query()
        if self._error is not None:
            raise self._error
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

    The checkout also carries an executable ``scripts/ci_gate.sh``: the
    import lane refuses a project that has no CI adapter (2026-11-05), so
    a fixture without one would be testing the *refusal* branch on the
    happy path.  The execute bit is set explicitly — a file without it
    counts as "no adapter" just as much as a missing file does.
    """
    path = tmp_path / "checkout"
    (path / "backend").mkdir(parents=True)
    for name in ("chart.py", "reporting_view.py", "loader.py", "a.py", "b.py"):
        (path / "backend" / name).write_text("# existing\n", encoding="utf-8")
    (path / "scripts").mkdir(parents=True, exist_ok=True)
    adapter = path / "scripts" / "ci_gate.sh"
    adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    adapter.chmod(0o755)
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


def _unrunnable_payload():
    """A task list the executor could not run: a task with no test command.

    It is refused by the *audit* (``validate_tasks``), not by the
    generator: the generator is happy with it, the audit is the gate that
    says "this list would break at dispatch time".
    """
    return {
        "requirement": "add a daily grouped bar chart to the reporting view",
        "tasks": [
            {
                "id": "T1",
                "title": "add the chart renderer",
                "description": "## 背景\nthe reporting view has no chart",
                "files_to_modify": ["backend/chart.py"],
                # no test_command: nothing says the change can be verified
            },
        ],
    }


def _install(monkeypatch, reply=None, error=None, on_query=None):
    """Point ``server.create_coding_tool`` at a fake and hand it back."""
    tool = FakeCodingTool(reply=reply, error=error, on_query=on_query)
    monkeypatch.setattr(server, "create_coding_tool", lambda **kwargs: tool)
    return tool


def _import(body):
    """One import request, through the mounted app."""
    return client.post("/api/plan/import", json=body)


def _queue_plan_ids(db_path: Path):
    """The plan ids in the scheduler's queue, in the order it holds them."""
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            str(row[0])
            for row in conn.execute("SELECT plan_id FROM scheduler_queue")
        ]
    finally:
        conn.close()


def _backfill_queue(db_path: Path, plans_dir: Path):
    """Run the scheduler's start-up scan and return what it collected.

    The scan is the production method (``_backfill_queue``), driven on
    the same database the import wrote to — so what it collects is a
    fact about the phase the import registered, not about a query the
    test made up.
    """
    from plan_scheduler.config import SchedulerConfigLoader
    from plan_scheduler.engine import PlanSchedulerEngine, _stamp
    from plan_scheduler.store import SqliteSchedulerStore
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        engine = PlanSchedulerEngine(
            SchedulerConfigLoader(None),
            store=SqliteSchedulerStore(),
            conn=conn,
            plans_dir=plans_dir,
        )
        return engine._backfill_queue(_stamp(engine._now()))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1 — a refused import is registered, and its directory is kept
# ---------------------------------------------------------------------------


def test_validation_failure_registers_import_failed(
    isolated_plans_dir, checkout, monkeypatch, state_db_reader
):
    """A task list the audit refuses leaves a *registered* plan behind.

    Three facts, each on its own: the caller is told the import failed
    (422), the material that was imported is still on disk (the directory
    is evidence, not garbage), and the plan is registered in the
    ``import_failed`` phase so every reader that goes through the tables
    can find it.

    Before this change the third fact was false: the directory existed
    and the routing row did not, which is exactly the state ``resume``
    answered "no such plan" about.
    """
    _install(monkeypatch, _unrunnable_payload())

    response = _import({
        "project_dir": str(checkout),
        "content": "把 CSV 报表加上按天分组的柱状图",
        "title": "Fast lane refused import",
    })

    assert response.status_code == 422, response.text
    plan_id = response.json()["plan_id"]

    # The directory is kept: the imported material is still there.
    plan_dir = isolated_plans_dir / plan_id
    assert plan_dir.exists()
    assert (plan_dir / "import.json").exists()

    # And the plan is registered in the phase that says "this exists and
    # it is not runnable".
    routing = state_db_reader.routing(plan_id)
    assert routing is not None, (
        "a refused import must leave a routing row behind; without one "
        "every table-driven reader answers 'no such plan'"
    )
    assert routing["current_phase"] == "import_failed"


# ---------------------------------------------------------------------------
# 2 — a generation failure is registered the same way
# ---------------------------------------------------------------------------


def test_generation_failure_registers_import_failed(
    isolated_plans_dir, checkout, monkeypatch, state_db_reader
):
    """The generator's own failure is registered the same way.

    ``TasksGenerationError`` is the generator saying "this list is not a
    plan" — the model mis-fired, or a post-generation gate threw the
    list away.  It is the same refusal as the audit's, one step earlier,
    and it gets the same registration.
    """
    from tasks_generator import TasksGenerationError

    _install(
        monkeypatch,
        error=TasksGenerationError("the model mis-fired"),
    )

    response = _import({
        "project_dir": str(checkout),
        "content": "把 CSV 报表加上按天分组的柱状图",
        "title": "Fast lane generation failure",
    })

    assert response.status_code == 422, response.text
    plan_id = response.json()["plan_id"]

    assert (isolated_plans_dir / plan_id).exists()

    routing = state_db_reader.routing(plan_id)
    assert routing is not None
    assert routing["current_phase"] == "import_failed"


# ---------------------------------------------------------------------------
# 3 — the executor's own gate refuses the list: same registration
# ---------------------------------------------------------------------------


def test_hardening_failure_registers_import_failed(
    isolated_plans_dir, checkout, monkeypatch, state_db_reader
):
    """The last gate refuses the list — and the plan is registered too.

    ``validate_workspace`` / ``harden_for_execution`` are the gates the
    executor's own start path runs.  A list they refuse is refused one
    step later than the audit refuses one, and it gets the same
    registration: the caller gets the 422, and the plan is registered
    as failed instead of being an orphan directory.
    """
    import shutil

    # The checkout the request named is not on this machine: every task
    # targets it, so the list itself is fine — the world is not.  It is
    # taken away **while the model is being asked**, because the CI
    # adapter gate runs before that and needs the project to be complete,
    # and the hardening gate runs after and needs the project to be gone.
    # Taking it away up front would be refused by the adapter gate
    # instead — a different gate, and this case is named after the one
    # that has to fire.
    _install(
        monkeypatch,
        _tasks_payload(),
        on_query=lambda: shutil.rmtree(checkout, ignore_errors=True),
    )

    response = _import({
        "project_dir": str(checkout),
        "content": "把 CSV 报表加上按天分组的柱状图",
        "title": "Fast lane hardening failure",
    })

    assert response.status_code == 422, response.text
    assert response.json()["error"] == "Generated tasks are not runnable"

    plan_id = response.json()["plan_id"]
    assert (isolated_plans_dir / plan_id).exists()

    routing = state_db_reader.routing(plan_id)
    assert routing is not None
    assert routing["current_phase"] == "import_failed"


# ---------------------------------------------------------------------------
# 4 — a timeout is the same story
# ---------------------------------------------------------------------------


def test_timeout_registers_import_failed(
    isolated_plans_dir, checkout, monkeypatch, state_db_reader
):
    """The model call timed out — the plan is registered, not orphaned.

    The timeout is a different status (504) and a different reason, but
    it is the same shape of event: the import did not produce a plan,
    and the directory is on disk.
    """
    _install(monkeypatch, error=TimeoutError("the provider did not answer"))

    response = _import({
        "project_dir": str(checkout),
        "content": "把 CSV 报表加上按天分组的柱状图",
        "title": "Fast lane timeout",
    })

    assert response.status_code == 504, response.text
    plan_id = response.json()["plan_id"]

    assert (isolated_plans_dir / plan_id).exists()

    routing = state_db_reader.routing(plan_id)
    assert routing is not None
    assert routing["current_phase"] == "import_failed"

    execution = state_db_reader.execution(plan_id)
    assert execution is not None
    assert "the provider did not answer" in execution["stop_reason"]


# ---------------------------------------------------------------------------
# 5 — the reason is attached to the plan, unprocessed
# ---------------------------------------------------------------------------


def test_stop_reason_keeps_original_text(
    isolated_plans_dir, checkout, monkeypatch, state_db_reader
):
    """``stop_reason`` carries the error text as it was written.

    Not a summary of it, not a re-worded version of it: the sentence the
    audit produced, verbatim.  The HTTP response is the first copy of the
    reason and the caller throws it away; the row is the copy that
    stays with the plan.
    """
    _install(monkeypatch, _unrunnable_payload())

    response = _import({
        "project_dir": str(checkout),
        "content": "把 CSV 报表加上按天分组的柱状图",
    })

    assert response.status_code == 422, response.text
    plan_id = response.json()["plan_id"]

    # The problems the audit reported — the same sentences the caller saw.
    problems = response.json()["problems"]
    assert problems, "the refusal must say what was wrong"

    execution = state_db_reader.execution(plan_id)
    assert execution is not None, (
        "the reason has to be attached to the plan; a plan with no "
        "execution row has nowhere to keep it"
    )
    stop_reason = execution["stop_reason"]
    assert stop_reason, "stop_reason must not be empty"

    for problem in problems:
        assert problem in stop_reason, (
            f"the original problem text {problem!r} is missing from "
            f"stop_reason {stop_reason!r}"
        )


# ---------------------------------------------------------------------------
# 6 — a registration that could not land: logged, and the refusal stands
# ---------------------------------------------------------------------------


def test_registration_failure_is_logged_and_the_refusal_stands(
    isolated_plans_dir, checkout, monkeypatch, state_db_reader, caplog
):
    """A registration that could not happen is logged — and swallowed.

    ``_seed_plan_routing_phase`` swallows its own failures (SQLite
    unavailable → a warning, no row), so "the plan was registered" is a
    claim that has to be **read back** rather than assumed.  When it does
    not hold, the operator gets an error in the log — and the caller
    still gets the 422, because the import did fail.  A registration
    that could not happen is not a different answer to the caller; it is
    a plan that is missing from the registry, and that has to be visible
    to whoever looks after the service.
    """
    monkeypatch.setattr(
        server, "_seed_plan_routing_phase", lambda plan_id, phase: None,
    )
    _install(monkeypatch, _unrunnable_payload())

    response = _import({
        "project_dir": str(checkout),
        "content": "把 CSV 报表加上按天分组的柱状图",
    })

    # The refusal stands — the import failed, and that is what the
    # caller is told.
    assert response.status_code == 422, response.text
    assert response.json()["problems"]

    plan_id = response.json()["plan_id"]

    # The routing row did not land, and the log says so.
    assert state_db_reader.routing(plan_id) is None
    errors = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "ERROR"
    ]
    assert any(
        "import_failed_phase_not_registered" in message
        for message in errors
    ), f"a registration that did not land must be logged: {errors}"

    # The reason is still attached to the plan: that write does not
    # depend on the phase row.
    execution = state_db_reader.execution(plan_id)
    assert execution is not None
    assert execution["stop_reason"]


# ---------------------------------------------------------------------------
# 7 — a registered failure is never pulled into the line
# ---------------------------------------------------------------------------


def test_import_failed_not_in_queue(
    isolated_plans_dir, checkout, monkeypatch, state_db_reader
):
    """``import_failed`` is not ``queued``, so the scan never collects it.

    The scheduler's start-up scan (``_backfill_queue``) is what puts a
    waiting plan into the line, and it only ever collects ``queued``.
    A registered failure is registered *as a failure* — being findable
    must not make it runnable.

    The control is in the same case: a plan that imported **successfully**
    is in the same database, in the same phase family, and the scan does
    collect it.  Without that control, "the failed plan is not in the
    queue" would also be true of a scan that collects nothing at all.
    """
    # The control: a plan that made it all the way to ``queued``.
    _install(monkeypatch, _tasks_payload())
    good = _import({
        "project_dir": str(checkout),
        "content": "做一个能跑的图表页",
        "title": "Fast lane good import",
    })
    assert good.status_code == 200, good.text
    good_id = good.json()["plan_id"]
    assert state_db_reader.routing(good_id)["current_phase"] == "queued"

    # The case: an import that was refused.
    _install(monkeypatch, _unrunnable_payload())
    bad = _import({
        "project_dir": str(checkout),
        "content": "做一个跑不了的图表页",
        "title": "Fast lane refused import",
    })
    assert bad.status_code == 422, bad.text
    bad_id = bad.json()["plan_id"]
    assert state_db_reader.routing(bad_id)["current_phase"] == "import_failed"

    # The scan runs on the database the two imports wrote to.
    collected = _backfill_queue(state_db_reader.db_path, isolated_plans_dir)
    queued = _queue_plan_ids(state_db_reader.db_path)

    assert good_id in collected, (
        "the control proves the scan works at all: a queued plan has to "
        f"be collected (collected={collected})"
    )
    assert good_id in queued
    assert bad_id not in collected
    assert bad_id not in queued


# ---------------------------------------------------------------------------
# 7 — the negative control: a good import is untouched by any of this
# ---------------------------------------------------------------------------


def test_successful_import_still_queued(
    isolated_plans_dir, checkout, monkeypatch, state_db_reader
):
    """The success path is unchanged: ``queued``, on disk, with its tasks.

    The registration of failures must not reach the success path — a
    plan that imported fine is still a plan the scheduler collects, with
    its task list on disk and nothing in the execution table (nothing
    ran).
    """
    _install(monkeypatch, _tasks_payload())

    response = _import({
        "project_dir": str(checkout),
        "content": "把 CSV 报表加上按天分组的柱状图",
        "title": "Fast lane successful import",
    })

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]

    plan_dir = isolated_plans_dir / plan_id
    assert plan_dir.exists()
    tasks = json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))
    assert [row["id"] for row in tasks["tasks"]] == ["T1", "T2"]

    routing = state_db_reader.routing(plan_id)
    assert routing is not None
    assert routing["current_phase"] == "queued"

    # Nothing ran, so there is no execution row to speak of.
    assert state_db_reader.execution(plan_id) is None
