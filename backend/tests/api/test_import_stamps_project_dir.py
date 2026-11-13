"""The import lane puts the request's checkout on every task it produces.

The contradiction this file pins
--------------------------------
``POST /api/plan/import`` requires a ``project_dir`` and promises a plan
that can run.  The start path (``POST /api/execution/{id}/start``) refuses
a start that carries no directory — a plan whose tasks carry none can
therefore never be admitted, and the scheduler keeps retrying that
admission, so the plan sits at the head of the line and never starts.

The lane used to leave that gap open.  ``validate_workspace`` clears the
directory of any task that points into the repository hosting this server
(the framework refuses to let a task work in the checkout that runs it),
and the task was then queued with no directory at all — while the lane's
own warning promised that "the run's working directory will be used",
which is exactly what the start path refuses to do.

So the lane now stamps the checkout the request named onto every task that
has none, and refuses the import if a task still has none.

What is pinned here
-------------------
The real endpoint, through ``TestClient`` against the mounted app, with
the same fake coding tool the import's own test file uses — the lane must
ask the model, and the model must not be asked for a directory it has
already been given.

The checkout is placed **inside** the repository that hosts the server.
That is the condition under which the directory was cleared, and it is
the one an operator hits when the project they import into lives in the
same repository as PDT itself.  The repository root is redirected at a
tmp directory for the test, so the test never writes into the operator's
checkout and never depends on where PDT happens to be installed.
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

# The route module is imported **after** ``server``, and that order is not
# a style choice: ``server`` imports the route module for its request
# models, and the route module imports ``server`` for the shared helpers.
# Importing the route module first trips that circular import.
from routes.fast_lane import _stamp_project_dir  # noqa: E402

# The import lane's own test file already owns the stub that scripts the
# two calls the generator makes, the payload helper and the checkout
# fixture.  They are used here rather than written a second time: a second
# stub is a second thing to keep in step with the generator.
from tests.api.test_plan_import_api import (  # noqa: E402
    _install,
    _tasks_payload,
    checkout,
    no_probe,
    plans_root,
)

client = TestClient(server.app)


@pytest.fixture
def checkout_inside_the_server_repo(tmp_path, monkeypatch, checkout):
    """The checkout, placed inside the repository that hosts the server.

    ``server._PROJECT_ROOT`` is the repository the backend runs out of, and
    it is the directory a task is not allowed to work in.  Redirecting it
    at this test's own tmp root keeps the test hermetic — no write lands
    in the operator's checkout — while reproducing the production
    condition exactly: the imported checkout is inside the repository
    hosting the server.
    """
    monkeypatch.setattr(server, "_PROJECT_ROOT", tmp_path)
    return checkout


def _post(project_dir, **overrides):
    body = {"project_dir": str(project_dir), "content": "do the thing"}
    body.update(overrides)
    return client.post("/api/plan/import", json=body)


def test_every_imported_task_names_the_checkout_the_request_named(
    checkout_inside_the_server_repo, monkeypatch, plans_root
):
    """The plan the import returns is a plan the start path can start.

    Every task names the checkout, and names the one the request named —
    not a sibling, not an empty string.  A task with an empty directory
    is a task the executor has nowhere to work in, and the start path
    refuses the whole plan when it cannot find one.
    """
    tool = _install(monkeypatch, _tasks_payload())

    response = _post(checkout_inside_the_server_repo)

    assert response.status_code == 200, response.text
    payload = response.json()

    # The model was asked — the lane's promise is that a model produced
    # this list, not that the lane mapped the caller's fields onto tasks.
    assert len(tool.json_calls) == 1

    tasks = payload["tasks"]
    assert tasks, "the import produced no tasks to check"

    missing = [t.get("id") for t in tasks if not str(t.get("project_dir") or "").strip()]
    assert not missing, (
        "these tasks carry no project_dir, so the plan cannot be started: "
        f"{missing}"
    )

    wrong = [
        (t.get("id"), t.get("project_dir"))
        for t in tasks
        if t.get("project_dir") != str(checkout_inside_the_server_repo)
    ]
    assert not wrong, (
        "these tasks do not name the checkout the request named: "
        f"{wrong}"
    )


def test_the_persisted_plan_carries_the_checkout_on_every_task(
    checkout_inside_the_server_repo, monkeypatch, plans_root
):
    """The file the executor reads is the file that carries the checkout.

    The response is built from what was written, so this is the same
    claim read from the other side — but it is the side that matters: the
    scheduler resolves the plan's directory from this file, and a file
    that names no directory is a plan the scheduler can never admit.
    """
    _install(monkeypatch, _tasks_payload())

    response = _post(checkout_inside_the_server_repo)

    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]
    saved = json.loads(
        (plans_root / plan_id / "tasks.json").read_text(encoding="utf-8")
    )

    assert saved["tasks"], "the plan was written with no tasks"
    for task in saved["tasks"]:
        assert task.get("project_dir") == str(checkout_inside_the_server_repo), (
            f"task {task.get('id')!r} does not name the checkout the request "
            f"named: {task.get('project_dir')!r}"
        )


def test_the_import_does_not_promise_a_working_directory_it_cannot_use(
    checkout_inside_the_server_repo, monkeypatch, plans_root
):
    """The warning the lane used to emit is gone, and it had to be gone.

    "the run's working directory will be used" is what the lane said when
    it could not resolve a task's directory.  The start path refuses a
    start without one, so the promise was the opposite of the truth: the
    caller read it and believed the plan would run.

    The lane resolves the directory now, so there is nothing to warn
    about — the warning is not reworded, it is gone.
    """
    _install(monkeypatch, _tasks_payload())

    response = _post(checkout_inside_the_server_repo)

    assert response.status_code == 200, response.text
    warnings = response.json()["warnings"]
    assert not any("working directory" in w for w in warnings), warnings
    assert not any("no project_dir could be resolved" in w for w in warnings), (
        warnings
    )


def test_a_task_that_already_names_a_directory_keeps_it():
    """The stamp fills a gap; it does not overrule a fact.

    The stamp exists to put the request's checkout back onto tasks the
    workspace gate emptied.  A task that names a directory of its own is
    a fact the model produced and the audit already checked against the
    request's — overwriting it would silently retarget that task at a
    checkout it was never asked to work in.

    This is pinned at the stamp rather than through the endpoint because
    the endpoint cannot carry this input: ``validate_tasks`` refuses an
    import whose task names a directory other than the request's, so a
    task with a directory of its own never reaches the stamp through
    ``POST /api/plan/import``.  The stamp's own behaviour — leave a
    directory that is already there alone — is the contract, and it is
    only observable here.
    """
    tasks_data = {
        "project_dir": "",
        "tasks": [
            {"id": "T1", "title": "the task names no directory"},
            {
                "id": "T2",
                "title": "the task names a directory of its own",
                "project_dir": "/tmp/elsewhere",
            },
        ],
    }

    missing = _stamp_project_dir(tasks_data, "/tmp/the-request-checkout")

    by_id = {task["id"]: task for task in tasks_data["tasks"]}

    # The gap is filled: the task that named no directory gets the
    # checkout the request named.
    assert by_id["T1"]["project_dir"] == "/tmp/the-request-checkout"

    # The fact is kept: the task that named a directory of its own still
    # names it.  This is the assertion the case exists for.
    assert by_id["T2"]["project_dir"] == "/tmp/elsewhere", (
        f"the stamp overwrote a directory the task already named: "
        f"{by_id['T2']['project_dir']!r}"
    )

    # Nothing is left without a directory, so no import is refused over a
    # directory that could not be resolved.
    assert missing == []

    # The plan's own directory is filled the same way.
    assert tasks_data["project_dir"] == "/tmp/the-request-checkout"
