"""``POST /api/plan/import`` — the caller's priority, fixed when the plan joins the line.

What is pinned here
-------------------
The priority of a plan is **what the caller said at the moment it asked
for the plan**, and nothing else.  The request body carries it, the
plan's own artifact keeps it, the queue row the scheduler builds keeps
it, and afterwards neither a configuration file nor an environment
variable can move it.  Four claims are pinned here:

1. **The priority the caller sent is the priority the plan has.**  A
   request that carries ``priority: -5`` leaves a queue row whose
   ``priority`` is ``-5`` — and the plan sorts *ahead* of a plan that
   was imported without one, because the queue's order is
   ``(promote_order is None, promote_order, priority, seq)``.
2. **A request that says nothing gets the default.**  ``priority`` is
   ``0`` when the body does not carry the field.
3. **Nothing but the request moves it.**  Changing the scheduler's
   configuration and the environment *after* the import leaves the queued
   plan's priority alone — and a plan imported afterwards still gets the
   default rather than the number the configuration carries.
4. **A plan is in the line once.**  Importing the same plan again adds
   no second row, and does not rewrite the priority that was fixed the
   first time.

Who writes the row
------------------
The **worker** writes the queue row: the queue is the scheduler's own
fact, and the scheduler's own facts have one writer.  The route does
not write it — it records the caller's number in the plan's own
directory, in the artifact the scheduler reads when it builds the row,
and it hands the plan over to the worker.

That is why the number lives in the plan's own files and not only in
the request: a plan may be imported while the scheduler is switched off
— the default — and the row the scheduler builds when it starts still
has to carry the number the caller gave.  Both are pinned here: the row
the worker builds while the scheduler is running, and the row it builds
for a plan that was imported with the scheduler off.

Why the queue row is read out of the database
---------------------------------------------
The priority is a fact **the scheduler reads**, so it is asserted on the
fact itself: the ``scheduler_queue`` row, read out of the same SQLite
file the scheduler reads, through the store's own read path.  Asserting
on the response body would only prove that the route echoed something
back.

Why this directory has no ``__init__.py``
-----------------------------------------
The production package is called ``plan_scheduler``, and so is this
directory.  A package marker here would make pytest import this
directory as the **top-level** package ``plan_scheduler`` — and the name
would then mean two things at once.  The sibling file
``test_scheduler_actions_api.py`` says the whole of it.

Isolation
---------
The autouse ``isolated_plans_dir`` fixture redirects ``PLANS_DIR`` and
the state database at ``tmp_path``, so this file never reads the
operator's live ``plans/`` tree or ``state.db``.  No real LLM is
involved: ``server.create_coding_tool`` is monkeypatched per-test to
return a fake that scripts the two calls the generator makes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402


client = TestClient(server.app)


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


class FakeCodingTool:
    """Stands in for ``CodingTool``; scripts the two calls the generator makes.

    ``query_json`` is the synthesis and ``query`` is the mandatory
    self-review.  The self-review reply echoes the synthesis payload *by
    reference*, which is what the generator mutates in place while it
    adds ``depends_on`` and resolves ``files_to_modify`` — so the fake
    always reviews the list as it stands at that moment.
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
    exist, and the route runs that gate — so a fixture that pointed
    at a made-up path would be testing the 422 branch on the happy path.
    """
    path = tmp_path / "checkout"
    (path / "backend").mkdir(parents=True)
    for name in ("chart.py", "reporting_view.py", "loader.py", "a.py", "b.py"):
        (path / "backend" / name).write_text("# existing\n", encoding="utf-8")
    return path


@pytest.fixture
def worker(monkeypatch, tmp_path):
    """The scheduler's worker, running against the case's own database.

    The engine is the **production** one — the same builder the server
    uses at start-up — with a scheduler that is switched on, does
    nothing but record its decisions, and ticks once an hour.  It is
    hung on ``app.state.scheduler_engine``, which is the handle the
    route reads, so the route hands the plan to the worker exactly the
    way it does in production.

    The handle is taken away again when the case is over: a worker left
    on the app would be a second scheduler for the next case.
    """
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "scheduler:\n"
        "  enabled: true\n"
        "  dry_run: true\n"
        "  capacity: 4\n"
        "  min_runtime_seconds: 600\n"
        "  tick_seconds: 3600\n"
        "  command_timeout_seconds: 30\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_scheduler_settings_path", lambda: settings)

    runtime = server._build_scheduler_engine()
    engine = runtime.engine
    assert engine is not None, (
        "the production wiring did not build the scheduler engine"
    )
    server.app.state.scheduler_engine = engine
    try:
        yield engine
    finally:
        server.app.state.scheduler_engine = None
        try:
            engine.stop()
        finally:
            if runtime.conn is not None:
                runtime.conn.close()


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


def _open_state_db() -> sqlite3.Connection:
    """Open the case's state database with the production PRAGMA contract."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    migrate(conn)
    return conn


def _queue_rows(plan_id: str) -> List[Dict[str, Any]]:
    """The queue rows the database holds for ``plan_id``.

    Read through the store's own read path — the same rows the panel,
    the admission check and the restart recovery read — so a case cannot
    disagree with production about what the queue holds.
    """
    from plan_scheduler.store import SqliteSchedulerStore

    conn = _open_state_db()
    try:
        return [
            row
            for row in SqliteSchedulerStore().list_queue(conn)
            if str(row.get("plan_id")) == plan_id
        ]
    finally:
        conn.close()


def _queue_order() -> List[str]:
    """The plan ids the queue holds, in the queue's own order."""
    from plan_scheduler.store import SqliteSchedulerStore

    conn = _open_state_db()
    try:
        return [
            str(row["plan_id"]) for row in SqliteSchedulerStore().list_queue(conn)
        ]
    finally:
        conn.close()


def _wait_for_row(plan_id: str, timeout: float = 10.0) -> List[Dict[str, Any]]:
    """Wait for the worker to put the plan in the line, and return its row.

    The row is the worker's to write, and the worker writes it in its
    own turn — so the case waits for it rather than expecting the
    request that asked for the plan to have written it.
    """
    deadline = time.monotonic() + timeout
    while True:
        rows = _queue_rows(plan_id)
        if rows:
            return rows
        if time.monotonic() >= deadline:
            return rows
        time.sleep(0.02)


def _intent(plan_id: str) -> Optional[int]:
    """The priority the plan's own artifact carries, if it carries one.

    Read out of the plan's own directory: the number there is the
    caller's, and it is the number the scheduler copies into the queue
    row.
    """
    from plan_scheduler.engine import SCHEDULER_INTENT_FILE

    path = server._plan_dir(plan_id) / SCHEDULER_INTENT_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return int(data["priority"])


# ---------------------------------------------------------------------------
# 1 — the priority the caller sent is the priority the plan has
# ---------------------------------------------------------------------------


def test_import_persists_priority(
    isolated_plans_dir, checkout, monkeypatch, worker
):
    """The number in the body is the number the queue row carries.

    The row is read out of the database rather than out of the response,
    because the row is what the scheduler sorts by: the priority is a
    fact about the plan's place in the line, not a thing the route says
    back to the caller.

    The order is asserted too, because a priority nobody sorts by is a
    number in a column: ``-5`` has to put the plan *ahead* of a plan
    imported without a priority, and the queue's comparator is the only
    thing that may decide that.
    """
    _install(monkeypatch, _tasks_payload())
    prioritised = _import({
        "project_dir": str(checkout),
        "content": "the urgent one",
        "title": "Urgent plan",
        "priority": -5,
    })
    assert prioritised.status_code == 200, prioritised.text
    prioritised_id = prioritised.json()["plan_id"]

    _install(monkeypatch, _tasks_payload())
    ordinary = _import({
        "project_dir": str(checkout),
        "content": "the ordinary one",
        "title": "Ordinary plan",
    })
    assert ordinary.status_code == 200, ordinary.text
    ordinary_id = ordinary.json()["plan_id"]

    # The number the caller sent is the number the plan's own artifact
    # carries — it is the plan's, and the worker copies it into the row.
    assert _intent(prioritised_id) == -5
    assert _intent(ordinary_id) == 0

    # The priority is on the row, exactly as it was sent.
    rows = _wait_for_row(prioritised_id)
    assert len(rows) == 1, f"expected one queue row, found {rows}"
    assert rows[0]["priority"] == -5

    # The default is the default.
    assert _wait_for_row(ordinary_id)[0]["priority"] == 0

    # And the queue's own order puts the prioritised plan first.
    assert _queue_order() == [prioritised_id, ordinary_id]


def test_import_defaults_to_zero(
    isolated_plans_dir, checkout, monkeypatch, worker
):
    """A body that says nothing about priority gets the default.

    The default is ``0`` and not "whatever the queue row happened to
    have": the queue row is created by this import, so ``0`` is the
    number the caller asked for by not asking.
    """
    _install(monkeypatch, _tasks_payload())

    response = _import({
        "project_dir": str(checkout),
        "content": "把 CSV 报表加上按天分组的柱状图",
        "title": "Fast lane default priority",
    })

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]

    assert _intent(plan_id) == 0
    rows = _wait_for_row(plan_id)
    assert len(rows) == 1, f"expected one queue row, found {rows}"
    assert rows[0]["priority"] == 0


def test_extreme_priority_is_kept_verbatim(
    isolated_plans_dir, checkout, monkeypatch, worker
):
    """A priority that is a very large number is kept as it was sent.

    The number is stored as it was given — not clamped, not rounded, not
    refused: the queue's column is an integer, and the caller's number
    is an integer.  The extremes here are the ones the repository's own
    queue cases use (``2 ** 62`` and its negative), so this case says
    about the number the same thing the rest of the scheduler says
    about it.
    """
    extremes = {"the lowest": -(2 ** 62), "the highest": 2 ** 62}

    plan_ids = {}
    for label, value in extremes.items():
        _install(monkeypatch, _tasks_payload())
        response = _import({
            "project_dir": str(checkout),
            "content": f"the {label} import",
            "title": f"Extreme priority {label}",
            "priority": value,
        })
        assert response.status_code == 200, response.text
        plan_ids[label] = response.json()["plan_id"]

    for label, value in extremes.items():
        assert _intent(plan_ids[label]) == value
        rows = _wait_for_row(plan_ids[label])
        assert len(rows) == 1, f"expected one queue row, found {rows}"
        assert rows[0]["priority"] == value


def test_the_route_does_not_write_the_queue(
    isolated_plans_dir, checkout, monkeypatch
):
    """The import route leaves the scheduler's own tables alone.

    The queue is the scheduler's own fact, and the scheduler's own
    facts are written by the scheduler's worker and by nobody else.  So
    an import that happens while the scheduler is **switched off** — the
    default — writes no queue row at all: there is no worker to write
    one.

    What it does write is the caller's number, in the plan's own
    directory.  That number is the plan's, and the worker reads it from
    there when it builds the row — which is what the next case pins.
    """
    _install(monkeypatch, _tasks_payload())

    response = _import({
        "project_dir": str(checkout),
        "content": "the import nobody is waiting for",
        "title": "Plan imported with the scheduler off",
        "priority": -5,
    })
    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]

    assert _intent(plan_id) == -5, (
        "the caller's number was not recorded in the plan's own artifact"
    )
    assert _queue_rows(plan_id) == [], (
        "the route wrote the scheduler's queue itself: the queue is the "
        "scheduler's own fact and the worker is its only writer"
    )


# ---------------------------------------------------------------------------
# 2 — the scheduler that starts later still builds the row the caller asked for
# ---------------------------------------------------------------------------


def test_a_scheduler_started_later_keeps_the_priority(
    isolated_plans_dir, checkout, monkeypatch, tmp_path
):
    """A plan imported while the scheduler was off keeps its priority.

    The scheduler is switched off by default, so a plan is very often
    imported while there is no worker at all.  The number the caller
    gave must not be lost in the meantime: the row the scheduler builds
    when it starts carries it.

    This is the case the plan's own artifact exists for.  The request is
    gone by the time the scheduler starts; what is left is the plan, and
    the number in the plan's own directory.
    """
    _install(monkeypatch, _tasks_payload())
    imported = _import({
        "project_dir": str(checkout),
        "content": "the import that had to wait",
        "title": "Plan imported before the scheduler",
        "priority": -5,
    })
    assert imported.status_code == 200, imported.text
    plan_id = imported.json()["plan_id"]

    # No worker yet: nothing is in the line.
    assert _queue_rows(plan_id) == []

    # The scheduler starts.
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "scheduler:\n"
        "  enabled: true\n"
        "  dry_run: true\n"
        "  capacity: 4\n"
        "  min_runtime_seconds: 600\n"
        "  tick_seconds: 3600\n"
        "  command_timeout_seconds: 30\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_scheduler_settings_path", lambda: settings)
    runtime = server._build_scheduler_engine()
    engine = runtime.engine
    assert engine is not None, (
        "the production wiring did not build the scheduler engine"
    )
    try:
        rows = _wait_for_row(plan_id)
    finally:
        engine.stop()
        if runtime.conn is not None:
            runtime.conn.close()

    assert len(rows) == 1, f"expected one queue row, found {rows}"
    assert rows[0]["priority"] == -5, (
        "the row the scheduler built did not carry the priority the caller "
        "gave: {0!r}".format(rows)
    )


# ---------------------------------------------------------------------------
# 3 — nothing but the request moves the priority
# ---------------------------------------------------------------------------


def test_config_change_does_not_affect_queued(
    isolated_plans_dir, checkout, monkeypatch, tmp_path, worker
):
    """A queued plan's priority is not a setting anybody can change.

    The priority is fixed when the plan joins the line: it is the
    caller's intent, expressed once, at the moment the plan was asked
    for.  A scheduler configuration that carries a ``priority`` key, and
    an environment variable that carries one too, are both *not* sources
    of it — the configuration has no such setting, and the environment
    is not read for one at all.

    So the case changes both after the import and asserts that the
    queued plan's priority did not move — and that a plan imported
    *afterwards* gets the default rather than the number the
    configuration carries.
    """
    _install(monkeypatch, _tasks_payload())
    first = _import({
        "project_dir": str(checkout),
        "content": "the first import",
        "title": "Fixed priority plan",
        "priority": -5,
    })
    assert first.status_code == 200, first.text
    first_id = first.json()["plan_id"]

    # The configuration changes: a scheduler section that carries a
    # priority the scheduler does not know, plus an environment
    # variable that carries one as well.
    config = tmp_path / "config.yaml"
    config.write_text(
        "scheduler:\n"
        "  enabled: false\n"
        "  capacity: 4\n"
        "  priority: 99\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_scheduler_settings_path", lambda: config)
    monkeypatch.setenv("PDT_SCHEDULER_PRIORITY", "99")

    # The queued plan keeps the priority it was given.
    rows = _wait_for_row(first_id)
    assert len(rows) == 1, f"expected one queue row, found {rows}"
    assert rows[0]["priority"] == -5

    # And a plan imported after the change gets the default, not 99.
    _install(monkeypatch, _tasks_payload())
    second = _import({
        "project_dir": str(checkout),
        "content": "the second import",
        "title": "Plan imported after the change",
    })
    assert second.status_code == 200, second.text
    second_id = second.json()["plan_id"]
    assert _wait_for_row(second_id)[0]["priority"] == 0


# ---------------------------------------------------------------------------
# 4 — a plan is in the line once
# ---------------------------------------------------------------------------


def test_reimport_does_not_duplicate(
    isolated_plans_dir, checkout, monkeypatch, worker
):
    """Importing the same plan again adds no row and moves no priority.

    The queue holds one row per plan — ``plan_id`` is its primary key —
    and the priority is fixed when the plan joins the line.  A repeated
    import of the same plan therefore changes nothing: not the number of
    rows, and not the priority the first import fixed.

    The case makes the second import land on the **same** plan (the id
    the first import was given) by pinning the id the lane allocates,
    which is the only way to say "the same plan" through this door.
    """
    _install(monkeypatch, _tasks_payload())
    first = _import({
        "project_dir": str(checkout),
        "content": "the first import",
        "title": "Re-imported plan",
        "priority": -5,
    })
    assert first.status_code == 200, first.text
    plan_id = first.json()["plan_id"]

    rows = _wait_for_row(plan_id)
    assert len(rows) == 1, f"expected one queue row, found {rows}"
    assert rows[0]["priority"] == -5
    first_seq = rows[0]["seq"]

    # The same plan, imported again — this time asking for a different
    # priority, which must not rewrite the one that was fixed.
    from routes import fast_lane

    monkeypatch.setattr(
        fast_lane, "_allocate_plan_id", lambda seed: (plan_id, None),
    )
    _install(monkeypatch, _tasks_payload())
    second = _import({
        "project_dir": str(checkout),
        "content": "the first import",
        "title": "Re-imported plan",
        "priority": 7,
    })
    assert second.status_code == 200, second.text
    assert second.json()["plan_id"] == plan_id

    # The number the plan carries is the one the first import fixed.
    assert _intent(plan_id) == -5

    rows = _wait_for_row(plan_id)
    assert len(rows) == 1, f"expected one queue row, found {rows}"
    assert rows[0]["priority"] == -5
    assert rows[0]["seq"] == first_seq
