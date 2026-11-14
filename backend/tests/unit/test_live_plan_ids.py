"""The live plan set is one answer for "what is running right now".

Background
----------
Two readers wanted the same answer and each computed it for itself:

* the scheduler's facts reader, which asks the database which plans are
  holding compute, and
* the active endpoint, which asks the two in-memory state tables.

The two rules drifted apart.  The database's rows go quiet when a
verification round stops refreshing them, while the round is very much
still running; the in-memory tables keep resting records forever (a
finished execution keeps its entry, a failed verification keeps its
verdict).  So the capacity panel said a slot was free while the workflow
was working on it — "used" and "active" did not agree, and neither was
wrong on its own terms.

The fix is a single helper: :func:`server._live_plan_ids` names the plans
that are alive right now, and both readers ask it.  It is a **set**, so a
plan that is executing *and* verifying is one plan — it holds one slot,
not two.

What these cases pin
--------------------
* a running verification is alive;
* a terminal verification is not — the record stays in the table, the
  plan is not working;
* a plan on both sides appears **once**;
* an empty pair of tables answers with an empty set rather than raising;
* the active endpoint reads the helper (it does not compute its own set);
* the facts reader is handed the helper, so the union actually happens.

Why the in-memory tables and not a database
--------------------------------------------
The claim being pinned is a claim about what the process knows right now:
which of its own records say "working".  A row in the state database is
the *history* of that claim, and the whole point of the helper is that
the process's own answer is fresher than the history.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

import pytest

import server


#: The backend package — the module that owns the helper and the tables.
_BACKEND_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """Both state tables start empty and are left empty.

    They are process globals, so a case that leaves a record behind
    would decide the next case's answer.
    """
    server._execution_state.clear()
    server._verification_state.clear()
    yield
    server._execution_state.clear()
    server._verification_state.clear()


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


def test_live_ids_include_running_verification() -> None:
    """A verification round that is cycling is alive.

    The record is the shape the auto-loop writes: a status, and the
    thread that drives the round.  A round with no thread is still a
    round — the thread is bookkeeping, the status is the claim.
    """
    server._verification_state["p-x"] = {
        "verification_status": "running",
        "thread": None,
    }

    assert "p-x" in server._live_plan_ids()


def test_live_ids_exclude_terminal_verification() -> None:
    """A failed round is not alive, even though its record stays.

    The record is a cache, not a set: a plan that failed keeps its
    verdict so the card can show it.  Counting that record as running
    is how a server with nothing working reported three active plans
    once already.
    """
    server._verification_state["p-x"] = {
        "verification_status": "failed",
        "thread": None,
    }

    assert "p-x" not in server._live_plan_ids()


def test_live_ids_dedup() -> None:
    """A plan on both sides is one plan.

    It holds one slot, not two.  The answer is a set, so the two sides
    cannot report it twice — and the scheduler, which counts plans
    against a capacity limit, must not charge it twice.
    """
    server._execution_state["p-x"] = {"status": "running", "ended_at": None}
    server._verification_state["p-x"] = {
        "verification_status": "running",
        "thread": None,
    }

    live = server._live_plan_ids()

    assert live == {"p-x"}
    assert len(live) == 1


def test_live_ids_empty_state() -> None:
    """Two empty tables answer with an empty set.

    The common case — a server that has just started, or one that has
    just finished everything — must answer, not raise.
    """
    assert server._live_plan_ids() == set()


def test_live_ids_include_running_execution() -> None:
    """An execution whose subprocess is alive is alive."""
    server._execution_state["p-x"] = {"status": "running", "ended_at": None}

    assert "p-x" in server._live_plan_ids()


def test_live_ids_exclude_finished_execution() -> None:
    """An execution that has ended is not alive.

    Its record stays in the table forever, so "the record exists" and
    "something is running" are two different questions.
    """
    server._execution_state["p-x"] = {
        "status": "completed",
        "ended_at": "2026-11-01T10:00:00",
    }

    assert "p-x" not in server._live_plan_ids()


def test_live_ids_exclude_resting_verification_statuses() -> None:
    """Every resting status is a resting status.

    ``passed`` / ``failed`` / ``loop_stopped`` / ``not_started`` are the
    states a verification record rests in; only ``running`` /
    ``repairing`` / ``rerunning`` mean the round is driving the plan.
    """
    for status in ("passed", "failed", "loop_stopped", "not_started"):
        server._verification_state["p-x"] = {
            "verification_status": status,
            "thread": None,
        }

        assert "p-x" not in server._live_plan_ids(), status


# ---------------------------------------------------------------------------
# The wiring
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "get_active_tasks lives in backend/routes/plans.py, which this "
        "task's files_to_modify does not declare — the write was refused "
        "by the containment guard. The endpoint still derives its set from "
        "the same two running predicates the helper unions, so the two "
        "answers agree; what is missing is the endpoint *asking* the "
        "helper. Add backend/routes/plans.py to the declaration and this "
        "case passes — strict, so it fails loudly once it does."
    ),
)
def test_active_endpoint_reads_the_live_helper(monkeypatch) -> None:
    """The active endpoint must ask the helper, not compute its own set.

    The helper is emptied here while the state tables still hold a
    working plan.  An endpoint that computed its own set would report
    that plan; an endpoint that asks the helper reports nothing.  The
    second answer is the one this change is about: the two readers must
    not be able to disagree.

    This case is **expected to fail** for now, and it says exactly what
    is left to do.  ``get_active_tasks`` was extracted out of
    ``server.py`` into ``backend/routes/plans.py`` on 2026-09-25 — the
    task that asked for this change named ``backend/server.py`` as the
    file to edit, so the endpoint's own file was never declared, and
    the write was refused by the containment guard.  The endpoint still
    derives its set from the two running predicates, which is the same
    rule the helper unions — so the two answers agree (pinned by
    :func:`test_active_endpoint_agrees_with_the_live_helper`) — but the
    endpoint does not *ask* the helper yet.

    The case is strict: the day ``backend/routes/plans.py`` is added to
    the declaration and the endpoint calls ``_server._live_plan_ids()``,
    this case starts passing, and the strict marker turns that into a
    failure that says "this is done — remove the marker".
    """
    server._execution_state["p-exec"] = {"status": "running", "ended_at": None}
    server._verification_state["p-ver"] = {
        "verification_status": "running",
        "thread": None,
    }

    monkeypatch.setattr(server, "_live_plan_ids", lambda: set())

    payload = server.get_active_tasks()

    assert payload["total_active"] == 0
    assert payload["details"] == []


def test_active_endpoint_agrees_with_the_live_helper() -> None:
    """The endpoint and the helper must name the same plans.

    The endpoint counts *agents* — a plan that is executing and
    verifying at the same time drives two — but the *plans* it names
    are the helper's plans.  Both sides read the same two running
    predicates over the same two tables, so they cannot disagree; this
    case is what keeps that true.
    """
    server._execution_state["p-exec"] = {"status": "running", "ended_at": None}
    server._execution_state["p-done"] = {
        "status": "completed",
        "ended_at": "2026-11-01T10:00:00",
    }
    server._verification_state["p-ver"] = {
        "verification_status": "running",
        "thread": None,
    }
    server._verification_state["p-failed"] = {
        "verification_status": "failed",
        "thread": None,
    }

    live = server._live_plan_ids()
    payload = server.get_active_tasks()

    assert live == {"p-exec", "p-ver"}
    assert {detail["plan_id"] for detail in payload["details"]} == live


def test_active_endpoint_reports_the_live_plans() -> None:
    """The endpoint still says what is running, and still says it twice.

    The endpoint counts *agents*, not plans: a plan that is executing
    and verifying at the same time drives two agents, and the panel
    that shows the parallel-agent count is right to say so.  What
    changed is where the set comes from, not what the endpoint reports.
    """
    server._execution_state["p-exec"] = {"status": "running", "ended_at": None}
    server._verification_state["p-ver"] = {
        "verification_status": "running",
        "thread": None,
    }

    payload = server.get_active_tasks()

    assert payload["execution_running"] == 1
    assert payload["verification_running"] == 1
    assert payload["total_active"] == 2
    assert {detail["plan_id"] for detail in payload["details"]} == {
        "p-exec",
        "p-ver",
    }


def test_scheduler_engine_injects_the_live_helper() -> None:
    """The facts reader must be handed the helper.

    Without the callback the reader answers from the database's rows
    alone, and the union this change exists for never happens: a round
    that has gone quiet still vanishes from the active set.  The engine
    is not callable from a unit test — it opens the database and starts
    a worker — so the wiring is pinned structurally.
    """
    tree = ast.parse((_BACKEND_DIR / "server.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PlanFactsReader"
    ]

    assert len(calls) == 1, (
        f"expected exactly one PlanFactsReader construction, found {len(calls)}"
    )

    keyword = next(
        (kw for kw in calls[0].keywords if kw.arg == "live_plan_ids"), None
    )
    assert keyword is not None, (
        "PlanFactsReader is built without live_plan_ids=..., so the reader "
        "answers from the database's rows alone and the live set is never "
        "unioned with them."
    )
    assert isinstance(keyword.value, ast.Name), (
        f"live_plan_ids= must be the helper itself, got {ast.dump(keyword.value)}"
    )
    assert keyword.value.id == "_live_plan_ids"
