"""The executor's HITL scheduling gate.

Background
----------
A plan can ask a human a question while it runs.  While that question
is unanswered, the task it is about — and everything downstream of it —
must not be scheduled: the work would run on a premise the human has
not confirmed yet.  The rest of the plan keeps going.

The gate is **computed, never stored**: no task status changes, so
answering the question opens the gate again by itself, and a plan
without an open question schedules exactly what it scheduled before.

What is asserted here
---------------------
Four behaviours, each of which fails differently:

  1. ``test_gate_excludes_downstream`` — an open decision on ``B``
     holds ``B`` and its downstream ``C`` out of the schedule, while
     the other task ``D`` keeps running.  Without the filter the
     executor would run the very work the human was asked about.
  2. ``test_gate_releases_after_answer`` — answering the question
     opens the gate by itself.  No task status is touched on the way
     out, so the release cannot be half-applied.
  3. ``test_gate_failure_open`` — when the service cannot be reached,
     the gate lets everything through and records why.  A gate that
     stopped the plan whenever it could not tell whether a question
     was open would turn a broken lookup into a stuck plan, and a
     stuck plan is worse than a question asked one moment too late.
  4. ``test_gate_holds_the_dispatcher`` — the executor's own
     dispatcher, which rebuilds its layers from the full task list,
     does not run a gated task either.

The graph under test is ``A → B → C`` with ``D`` hanging off ``A``,
and the decision is on ``B``.

Test command::

    pytest backend/tests/integration/test_scheduling_gate.py -v
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from config_paths import resolve_state_db_path
from hitl import service
from hitl.models import DecisionMode, DecisionRequest, DecisionStatus
from hitl.store import HitlStore
from hooks import config_path, write_for_plan
from utils.atomic_io import atomic_write_json

#: The plan under test.  Its directory name is its plan id, which is
#: also the key the decisions are stored under — the same string the
#: executor derives from ``<plans>/<plan_id>/tasks.json``.
PLAN_ID = "plan-hitl-scheduling-gate"

#: A destination that always succeeds, so the question is asked of
#: somebody.  The ``cmd`` transport runs the command and hands it the
#: payload on stdin; ``true`` exits 0 and touches nothing.  The
#: delivery runs on its own thread, so it never holds the decision's
#: creation path.
ASKABLE_HOOKS: List[Dict[str, Any]] = [{"kind": "cmd", "command": "true"}]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(**overrides: Any) -> DecisionRequest:
    """A valid decision request; ``overrides`` replaces single fields."""
    payload: Dict[str, Any] = {
        "plan_id": PLAN_ID,
        "source": "execution",
        "task_id": "B",
        "question": "May task B run?",
        "options": ["accept", "reject"],
        "default_option": "accept",
        "mode": DecisionMode.AUTO.value,
        "timeout_sec": 60,
    }
    payload.update(overrides)
    return DecisionRequest(**payload)


def _task(task_id: str, depends_on: List[str]) -> Dict[str, Any]:
    """One task of the fixture graph."""
    return {
        "id": task_id,
        "title": f"task {task_id}",
        "description": f"task {task_id}",
        "test_command": "true",
        "depends_on": depends_on,
    }


#: The graph every test in this file schedules against: ``A → B → C``
#: with ``D`` hanging off ``A`` as well.
#:
#: ``D`` is deliberately *not* a second root.  A second root would sit
#: in the first layer alone and finish before the gated task's layer
#: was ever reached, which would leave the dispatcher's own layer
#: filter untested — the run would stop on the empty active set
#: instead.  Hanging ``D`` off ``A`` puts it in the same layer as the
#: gated ``B``, so the only thing that can keep the dispatcher from
#: running ``B`` there is the gate itself.
GRAPH: List[Dict[str, Any]] = [
    _task("A", []),
    _task("B", ["A"]),
    _task("C", ["B"]),
    _task("D", ["A"]),
]


def _write_tasks(plan_dir: Path, tasks: List[Dict[str, Any]]) -> None:
    """Write the plan's static task graph into its ``tasks.json``."""
    document = {"requirement": "scheduling gate fixture", "tasks": tasks}
    (plan_dir / "tasks.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


def _write_config(plan_dir: Path, mode: str = "auto", timeout_sec: int = 60) -> None:
    """Write the plan's HITL configuration, with somebody to ask.

    The mode and the deadline are written by the product's own writer,
    so they are the ones the product would store.  The outlet is then
    given a destination the plan's *origin* cannot express:
    ``write_for_plan`` only ever writes the ``cc_connect`` hook a
    session implies, and a question nobody can be asked is
    ``unroutable`` from the start — there is nothing to wait for.
    """
    document = write_for_plan(plan_dir, {"mode": mode, "timeout_sec": timeout_sec})
    document["hooks"] = list(ASKABLE_HOOKS)
    atomic_write_json(config_path(plan_dir), document, reraise=True)


def _git_init(project_dir: Path) -> None:
    """Make ``project_dir`` a git repository.

    The agent holds a ``GitManager``, which opens the directory as a
    repository at construction time; a directory that is not one is a
    construction error, not a scheduling detail.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The plans root and the state database, both inside ``tmp_path``.

    Both are redirected explicitly rather than inherited from the
    session: the decisions this file writes have to be visible to the
    executor's own lookup, and the two must therefore agree on one
    database and one plans root.
    """
    plans = tmp_path / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    return plans


@pytest.fixture
def plan_dir(plan_root: Path) -> Path:
    """The plan's own directory — where its ``tasks.json`` lives."""
    path = plan_root / PLAN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def store(plan_root: Path) -> HitlStore:
    """A store on the redirected database."""
    opened = HitlStore(resolve_state_db_path())
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def agent(plan_dir: Path):
    """An executor bound to the plan, with the fixture graph loaded."""
    from agent import AutonomousAgent

    _git_init(plan_dir)
    _write_tasks(plan_dir, GRAPH)
    return AutonomousAgent(
        requirement="scheduling gate fixture",
        project_dir=plan_dir,
        coding_tool=None,
        config={"max_retries": 1},
        logger=None,
        tasks_file=plan_dir / "tasks.json",
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_gate_excludes_downstream(agent, plan_dir: Path, store: HitlStore) -> None:
    """An open decision holds its task and the downstream out of the schedule.

    The decision is on ``B``, so ``B`` and ``C`` (which depends on it)
    are held back; ``A`` (upstream) and ``D`` (independent) keep
    running.  No task status is touched — the gate is computed, so
    ``B`` is still ``pending`` and will be picked up as soon as the
    question is answered.

    Before the question is asked the gate holds nothing, and the
    schedule is the whole graph: a plan without an open question
    behaves exactly as it did before the gate existed.
    """
    _write_config(plan_dir)

    assert agent._decision_blocked_tasks(agent.plan_id) == set()
    assert [t.id for t in agent._get_active_tasks_for_scheduling()] == [
        "A",
        "B",
        "C",
        "D",
    ]

    service.create_decision(_request(task_id="B"), plan_dir, store=store)

    assert agent._decision_blocked_tasks(agent.plan_id) == {"B", "C"}
    assert [t.id for t in agent._get_active_tasks_for_scheduling()] == ["A", "D"]
    assert {t.id: t.status for t in agent.task_manager.tasks} == {
        "A": "pending",
        "B": "pending",
        "C": "pending",
        "D": "pending",
    }


@pytest.mark.integration
def test_gate_releases_after_answer(
    agent, plan_dir: Path, store: HitlStore
) -> None:
    """Answering the question opens the gate by itself.

    Nothing has to be un-blocked: the gate is recomputed from the
    decision's status, so the answered decision no longer holds
    anything back and the whole graph becomes schedulable again.
    """
    _write_config(plan_dir)
    created = service.create_decision(_request(task_id="B"), plan_dir, store=store)
    assert [t.id for t in agent._get_active_tasks_for_scheduling()] == ["A", "D"]

    status, code = service.answer_decision(
        created.decision_id, "accept", store=store
    )

    assert (status, code) == (DecisionStatus.ANSWERED.value, 200)
    assert agent._decision_blocked_tasks(agent.plan_id) == set()
    assert [t.id for t in agent._get_active_tasks_for_scheduling()] == [
        "A",
        "B",
        "C",
        "D",
    ]
    # The release changed no task status either — it is a gate, not a
    # state transition.
    assert {t.id: t.status for t in agent.task_manager.tasks} == {
        "A": "pending",
        "B": "pending",
        "C": "pending",
        "D": "pending",
    }


@pytest.mark.integration
def test_gate_failure_open(
    agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service that cannot answer lets the plan through.

    The gate is a safety belt, not a brake: a lookup that fails must
    not stop the plan, because "we could not tell whether a question
    is open" is not "a question is open".  The failure is recorded, so
    the operator can see that the gate was not in force.
    """

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the state database is gone")

    monkeypatch.setattr(service, "blocked_task_ids", _boom)

    assert agent._decision_blocked_tasks(agent.plan_id) == set()
    assert [t.id for t in agent._get_active_tasks_for_scheduling()] == [
        "A",
        "B",
        "C",
        "D",
    ]


@pytest.mark.integration
def test_gate_holds_the_dispatcher(
    agent, plan_dir: Path, store: HitlStore
) -> None:
    """The dispatcher itself does not run a task the gate holds.

    The scheduling view is one thing and the executor is another: the
    dispatcher rebuilds its micro-layers from the *full* task list, so
    a gate that only filtered the view would still run the work the
    human was asked about.  This drives the dispatcher with the task
    execution stubbed out and asserts which tasks it picked.

    The run stops with ``hitl_decision_pending`` rather than
    ``success``: the plan is not finished, it is waiting.
    """
    _write_config(plan_dir)
    service.create_decision(_request(task_id="B"), plan_dir, store=store)

    executed: List[str] = []

    async def _fake_run(task, controller, timeout) -> bool:
        executed.append(task.id)
        task.status = "completed"
        return True

    agent._run_task_with_provider_slot = _fake_run
    agent._load_tasks()
    asyncio.run(agent._run_async())

    assert sorted(executed) == ["A", "D"]
    assert agent.task_manager.stop_reason == "hitl_decision_pending"
    assert {t.id: t.status for t in agent.task_manager.tasks} == {
        "A": "completed",
        "B": "pending",
        "C": "pending",
        "D": "completed",
    }
