"""HTTP contract: a plan that is not parked for quota never reads as parked.

The criterion this file pins
----------------------------
The capacity-wait PRD says:

    *import 要求 1：等待配额必须由真实的容量事实触发，不能是全局默认状态*
    ("waiting for quota must be triggered by a real capacity fact — it
    must not be a global default state")

Two rounds of probing never produced evidence about that criterion.  The
probe hit a recycled backend::

    GET /api/plan/20261024-vp-quota-normal/status  ->  connection refused

and the four facts the probe wanted to confirm — ``phase='ready'``,
``execution_status='not_started'``, ``tasks.pending=2``,
``tasks.failed=0`` — never arrived.  That is a fact about the *service's*
lifetime, not about the plan: the backend was not running when the
request was made, so the answer says nothing at all about whether a
normal plan is reported as parked.  A one-shot probe against a shared
port is not evidence about a plan.

What the criterion actually needs
---------------------------------
The dangerous shape of a "wait for quota" feature is a **default**: a
plan reads as parked because the code that could not decide parked
everything by default.  Then every plan in the system claims to be
waiting for capacity, and the cards say "⏸ 等待配额" for a plan that is
merely sitting at the manual gate — a state the operator cannot act on
and cannot tell apart from a real capacity wall.

So the criterion has two halves:

  * a plan **with** the park marker reads as parked (the sibling file
    ``test_quota_parked_plan_status_http.py`` pins that half);
  * a plan **without** the park marker keeps its own semantics, and
    ``waiting_for_quota`` appears nowhere in its answer.

The second half is the negative control, and it is the one that fails
silently.  There is no symptom to notice: a plan that wrongly claims to
be waiting for quota still answers 200, still carries counts, still
looks healthy.  Only the parked word itself is wrong — and it is wrong
for EVERY plan at once, because a default is global.

Where the marker lives
----------------------
The park marker is a fact about ONE plan, and it has exactly one home:
the ``plan_execution`` row's ``exec_status``.  Nothing else writes
``waiting_for_quota`` — the phase vocabulary (``backend/plan_state.py``)
merely names the state so the routing row can carry it, and
``POST /api/execution/{id}/start`` merely allows a restart from it.

The status endpoint derives its answer from the rows: ``phase`` comes
from ``plan_routing.current_phase`` (``_build_plan_status``), and
``execution_status`` comes from ``plan_execution.exec_status`` (the
progress endpoint's own derivation reads that same row).  A plan whose
rows say nothing about capacity therefore has nothing to report about
capacity.

The contract this file freezes
------------------------------
``GET /api/plan/{plan_id}/status`` answers with three layers, and the
criterion is about all three:

  * the **snapshot** — ``PlanStatus.to_dict()``: ``phase``,
    ``execution_in_flight``, ``tasks``;
  * the **execution section** — ``execution.execution_status``;
  * the **degradation path** — what the answer says when a section
    cannot be read.  That is where a default is most tempting: a
    section that fails has to answer *something*, and "it must be
    waiting for quota" is the one answer that is always wrong unless a
    marker says so.

Hermeticity
------------
The plans tree and the state database live under the test's own
``tmp_path`` — the autouse ``isolated_plans_dir`` fixture points
``server.PLANS_DIR`` / ``server._state_db_path`` and the
``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH`` env vars at it — so the
operator's live database and ``plans/`` tree are never touched.  The rows
are written through the real repositories, so the endpoint reads back the
shapes production writes.

The in-memory ``_execution_state`` is cleared on purpose, and the test
does not rely on it for anything: the answer has to be readable **from
the durable rows alone**.  A server that happens to hold the plan in
memory would answer from memory, and a default that lives in a
process-wide dict must not be able to colour the answer.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

import server


#: The plan id the failed probes used.  Reusing it keeps the criterion
#: and its evidence about the same plan.
PLAN_ID = "20261024-vp-quota-normal"

#: The plan that carries the park marker — the positive control.
#: Without it, the negative assertions below would also pass on a build
#: where the parked word had been renamed away, i.e. vacuously.
PARKED_CONTROL_ID = "20261024-vp-quota-parked-control"

#: The two tasks of the normal plan.  Both are still ``pending``: the
#: plan is at the manual gate, waiting for a human — not waiting for
#: capacity.
TASK_IDS = ("VP-001", "VP-002")

#: The parked word.  The criterion is about this exact string.
PARKED = "waiting_for_quota"

#: The capacity marker the executor writes when a run parks because every
#: provider had no capacity left (``agent._stop_reason_for_quota_wait``).
#: It goes to the TOP LEVEL of ``tasks.json``, as ``stop_reason`` /
#: ``reason_detail``.
CAPACITY_MARKER = {
    "providers": ["MiniMax"],
    "needs_manual_topup": False,
    "waited_sec": 1800.0,
    "at": "2026-10-25T00:00:00",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_tasks_file(
    plan_dir: Path,
    task_ids: Tuple[str, ...] = TASK_IDS,
    marker: Optional[dict] = None,
) -> None:
    """Write the static task list of the plan.

    Static fields only — ``status`` lives in ``state.db`` (``plan_tasks``)
    since the runtime overlay landed there, and the status endpoint reads
    the runtime state from there too.

    ``marker`` is the capacity marker of a parked run: the top level of
    the file carries ``stop_reason`` / ``reason_detail``, exactly where
    the executor writes them.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": tid, "title": f"task {tid}", "description": f"do {tid}"}
        for tid in task_ids
    ]
    payload: dict[str, Any] = {"tasks": tasks}
    if marker is not None:
        payload["stop_reason"] = PARKED
        payload["reason_detail"] = json.dumps(marker)
    (plan_dir / "tasks.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )


def _seed_plan(
    *,
    project_dir: Path,
    plan_id: str = PLAN_ID,
    phase: Optional[str] = "ready",
    exec_status: Optional[str] = "not_started",
    task_ids: Tuple[str, ...] = TASK_IDS,
    stop_reason: Optional[str] = None,
) -> None:
    """Write the routing row, the execution row and the ``plan_tasks`` rows.

    Every row goes through the repository production writes through, so
    the endpoint reads back exactly the shapes production produces.

    ``phase=None`` writes no routing row and ``exec_status=None`` writes
    no execution row.  Those are not degenerate shapes invented for the
    test — they are the two ways a plan can lack a marker, and a default
    would show up in both of them.

    ``stop_reason`` is the row's "why did the run stop" — the field the
    parked verdict travels on.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = db_open(server._state_db_path())
    try:
        migrate(conn)
        if phase is not None:
            RoutingRepository(conn).insert(plan_id, phase)

        if exec_status is not None:
            row_fields: dict[str, Any] = {
                "exec_status": exec_status,
                "project_dir": str(project_dir),
            }
            if stop_reason is not None:
                row_fields["stop_reason"] = stop_reason
            ExecutionRepository(conn).update_fields(
                plan_id, create_if_missing=True, **row_fields,
            )

        task_repo = PlanTaskRepository(conn)
        for tid in task_ids:
            task_repo.add_task(
                plan_id,
                {"id": tid, "title": f"task {tid}", "description": f"do {tid}"},
            )
            # ``add_task`` already stores "pending"; say it explicitly so
            # the row's meaning does not depend on that default.
            task_repo.update_task(plan_id, tid, {"status": "pending"})
    finally:
        conn.close()


def _client() -> TestClient:
    """A client that does NOT run the application lifespan.

    The lifespan is what restores ``_execution_state`` from the rows at
    boot.  Skipping it is deliberate: the answer has to be readable
    **from the durable rows alone**.  A server that happens to hold the
    plan in memory would answer from memory, and a global default living
    in a process-wide dict must not be able to colour the answer.
    """
    server._execution_state.clear()
    return TestClient(server.app)


def _paths_mentioning(payload: Any, needle: str, path: str = "$") -> List[str]:
    """Every JSON path in ``payload`` whose value mentions ``needle``.

    A blanket search is the point: the criterion is about the parked word
    appearing *anywhere* in the answer, not about one field.  Reporting
    the paths (rather than just "not found") is what makes a failure
    actionable — a leak into ``$.execution.execution_status`` and a leak
    into ``$.summary.state.phase`` are different bugs.
    """
    hits: List[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            hits.extend(_paths_mentioning(value, needle, f"{path}.{key}"))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            hits.extend(_paths_mentioning(value, needle, f"{path}[{index}]"))
    elif isinstance(payload, str) and needle in payload:
        hits.append(f"{path} = {payload!r}")
    return hits


def _assert_not_parked(body: Any, plan_id: str, note: str = "") -> None:
    """The parked word must not be anywhere in this answer."""
    leaks = _paths_mentioning(body, PARKED)
    assert not leaks, (
        f"{PARKED!r} must not appear anywhere in the status of a plan "
        f"with no park marker ({plan_id}{note}); it leaked into: {leaks!r}"
    )


# ---------------------------------------------------------------------------
# The fixture: the plans tree of this test
# ---------------------------------------------------------------------------


@pytest.fixture
def plans_dir(tmp_path: Path) -> Path:
    """The plans tree of this test — never the operator's."""
    path = tmp_path / "plans"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# A normal plan keeps its own semantics
# ---------------------------------------------------------------------------


def test_normal_plan_is_not_reported_as_waiting(plans_dir: Path) -> None:
    """A plan at the manual gate reads as a plan at the manual gate.

    The plan is not parked: its routing row says ``ready`` (tasks
    generated, a human decides when to start) and its execution row says
    ``not_started`` (nothing has run).  The answer the status endpoint
    gives is the operator's — "where is this plan, and what is still left
    to do?" — and it must be that plan's own answer, not the answer of
    some other plan and not a default.

    These are the four facts the failed probe could not get, and they
    are asserted one by one for the same reason: ``phase='ready'``,
    ``execution_status='not_started'``, ``tasks.pending=2`` and
    ``tasks.failed=0`` say "waiting for a human", and a body that said
    ``waiting_for_quota`` instead would send the operator off to fix
    capacity for a plan that has never asked for any.
    """
    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write_tasks_file(plans_dir / PLAN_ID)
    _seed_plan(project_dir=project_dir)

    client = _client()
    resp = client.get(f"/api/plan/{PLAN_ID}/status")

    assert resp.status_code == 200, (
        f"a plan at the manual gate must answer the status endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    # -- the snapshot layer (``PlanStatus.to_dict``) ------------------------
    assert body["phase"] == "ready", (
        f"the status object reported phase {body.get('phase')!r}; the "
        f"routing row says the plan is at the manual gate — a normal plan "
        f"that reads as parked is a plan the operator would chase the "
        f"wrong cause on"
    )
    assert body["phase"] != PARKED, (
        f"a plan with no park marker must not read as {PARKED!r}"
    )

    assert "execution_in_flight" in body, (
        f"the status object must carry 'execution_in_flight'; got keys "
        f"{sorted(body)!r}"
    )
    assert isinstance(body["execution_in_flight"], bool), (
        f"'execution_in_flight' must be a boolean; got "
        f"{body['execution_in_flight']!r}"
    )
    assert body["execution_in_flight"] is False, (
        f"nothing has run for this plan, so nothing can be in flight; got "
        f"{body['execution_in_flight']!r}"
    )

    counts = body.get("tasks")
    assert isinstance(counts, dict), (
        f"the status object must carry the task counts; got {counts!r}"
    )
    assert counts.get("total") == 2, f"unexpected counts: {counts!r}"
    assert counts.get("pending") == 2, (
        f"both tasks are still waiting for a human to start them: {counts!r}"
    )
    assert counts.get("failed") == 0, (
        f"a plan at the manual gate must not have failed anything: {counts!r}"
    )
    assert counts.get("in_progress") == 0, f"unexpected counts: {counts!r}"

    # -- the execution section --------------------------------------------
    execution = body.get("execution")
    assert isinstance(execution, dict), (
        f"the status object must carry the execution section; got "
        f"{execution!r}"
    )
    assert execution["execution_status"] == "not_started", (
        f"the execution section reported "
        f"{execution.get('execution_status')!r}; the execution row says "
        f"nothing has run yet"
    )
    assert execution["execution_status"] != PARKED, (
        f"a plan with no park marker must not read as {PARKED!r}"
    )

    _assert_not_parked(body, PLAN_ID)


# ---------------------------------------------------------------------------
# The parked word is not a default
# ---------------------------------------------------------------------------


def test_waiting_phase_is_not_a_default(
    plans_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``waiting_for_quota`` appears only where a park marker says so.

    The criterion is an "if and only if": the parked state must be
    *reachable* when a real capacity fact parked the plan, and it must
    be *absent* everywhere else.  The first half is pinned by the
    sibling file; this is the second half, and it is the half that
    decides whether the feature is honest.

    Four shapes of "not parked" are checked, because a default would
    leak into all of them and a single shape could miss it:

      * **no execution row** — the plan has no execution record at all,
        so there is no row that could carry a verdict;
      * **an execution row with another verdict** — the marker slot
        exists and says ``not_started``, so a reader that treats "there
        is a row" as "there is a marker" is caught;
      * **no routing row** — the plan's workflow phase is unknown, so
        the phase must not be filled in with the parked one;
      * **a section that cannot be read** — the degradation path, which
        is where a default is most tempting: a section that fails has to
        answer *something*, and the parked verdict is the one answer
        that is always wrong unless a marker says so.  The phase and
        the counts must survive that failure too.

    The positive control at the end is what keeps this test from being
    vacuous: a plan that *does* carry the marker must still read as
    parked.  Without it, the assertions above would also pass on a build
    where the parked word had been renamed away — a green test that
    pins nothing.
    """
    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    unparked_shapes = (
        # (plan_id, phase, exec_status)
        ("20261024-vp-quota-no-exec-row", "ready", None),
        ("20261024-vp-quota-other-verdict", "ready", "not_started"),
        ("20261024-vp-quota-no-routing-row", None, "not_started"),
    )

    client = _client()
    for plan_id, phase, exec_status in unparked_shapes:
        _write_tasks_file(plans_dir / plan_id)
        _seed_plan(
            project_dir=project_dir,
            plan_id=plan_id,
            phase=phase,
            exec_status=exec_status,
        )

        resp = client.get(f"/api/plan/{plan_id}/status")
        assert resp.status_code == 200, (
            f"a plan with no park marker must answer the status endpoint; "
            f"got {resp.status_code} for {plan_id}: {resp.text!r}"
        )
        body = resp.json()

        _assert_not_parked(body, plan_id)
        assert body.get("phase") != PARKED, (
            f"{plan_id}: the phase must be the plan's own, not {PARKED!r}"
        )
        execution = body.get("execution")
        if isinstance(execution, dict):
            assert execution.get("execution_status") != PARKED, (
                f"{plan_id}: the execution verdict must be the row's own, "
                f"not {PARKED!r}"
            )

    # -- the degradation path: a section that cannot be read ----------------
    broken_id = "20261024-vp-quota-broken-section"
    _write_tasks_file(plans_dir / broken_id)
    _seed_plan(project_dir=project_dir, plan_id=broken_id)

    def _boom(_plan_id):
        raise RuntimeError("execution section exploded")

    with monkeypatch.context() as mp:
        mp.setattr(server, "get_execution_progress", _boom)
        resp = client.get(f"/api/plan/{broken_id}/status")

    assert resp.status_code == 200, (
        f"a plan whose execution section cannot be read must still answer; "
        f"got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    _assert_not_parked(body, broken_id, note=" (execution section unreadable)")

    assert body.get("phase") == "ready", (
        f"a section that cannot be read must not take the plan's phase "
        f"with it; got {body.get('phase')!r}"
    )
    counts = body.get("tasks")
    assert isinstance(counts, dict), (
        f"a section that cannot be read must not take the task counts with "
        f"it; got {counts!r}"
    )
    assert counts.get("pending") == 2, f"unexpected counts: {counts!r}"
    assert counts.get("failed") == 0, f"unexpected counts: {counts!r}"

    execution = body.get("execution")
    if isinstance(execution, dict):
        assert execution.get("execution_status") != PARKED, (
            f"a degraded execution section must not invent a parked "
            f"verdict; got {execution.get('execution_status')!r}"
        )

    # -- the positive control: the marker still means parked --------------
    _write_tasks_file(plans_dir / PARKED_CONTROL_ID)
    _seed_plan(
        project_dir=project_dir,
        plan_id=PARKED_CONTROL_ID,
        phase=PARKED,
        exec_status=PARKED,
    )

    resp = client.get(f"/api/plan/{PARKED_CONTROL_ID}/status")
    assert resp.status_code == 200, (
        f"a parked plan must answer the status endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body.get("phase") == PARKED, (
        f"the positive control must read as parked, or the negative "
        f"assertions above prove nothing; got {body.get('phase')!r}"
    )
    execution = body.get("execution")
    assert isinstance(execution, dict), (
        f"the positive control must carry the execution section; got "
        f"{execution!r}"
    )
    assert execution.get("execution_status") == PARKED, (
        f"the positive control's execution verdict must be {PARKED!r}, or "
        f"the negative assertions above prove nothing; got "
        f"{execution.get('execution_status')!r}"
    )


# ---------------------------------------------------------------------------
# Where the phase came from
# ---------------------------------------------------------------------------


def test_vp_008_normal_plan_stays_ready_and_not_started(plans_dir: Path) -> None:
    """The negative control, with the rule that produced it.

    The four facts — ``phase='ready'``, ``execution_status='not_started'``,
    ``tasks.pending=2``, ``tasks.failed=0`` — say "waiting for a human".
    The fifth one says where they came from: ``phase_source='routing'``,
    the routing row's own ``current_phase``.

    That fifth fact is what the previous round was missing.  Asserting
    that a normal plan reads as ``ready`` pins the *answer*; it does not
    pin the *rule* that produces the answer, so the day the routing row
    could not be read the phase could fall to whatever the
    implementation happened to hold — and the operator would be sent
    after a capacity wall that no plan ever hit.  ``phase_source`` makes
    the difference between "the routing row says ready" and "we could
    not read a phase and rested on the default" observable, which is
    the difference between a fact and a guess.
    """
    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write_tasks_file(plans_dir / PLAN_ID)
    _seed_plan(project_dir=project_dir)

    client = _client()
    resp = client.get(f"/api/plan/{PLAN_ID}/status")

    assert resp.status_code == 200, (
        f"a plan at the manual gate must answer the status endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body.get("phase") == "ready", (
        f"a plan at the manual gate must read as ready; got "
        f"{body.get('phase')!r}"
    )
    assert body.get("phase_source") == "routing", (
        f"the phase of a plan whose routing row says 'ready' must come "
        f"from that row; got {body.get('phase_source')!r}"
    )

    execution = body.get("execution")
    assert isinstance(execution, dict), (
        f"the status object must carry the execution section; got "
        f"{execution!r}"
    )
    assert execution.get("execution_status") == "not_started", (
        f"the execution row says nothing has run yet; got "
        f"{execution.get('execution_status')!r}"
    )

    counts = body.get("tasks")
    assert isinstance(counts, dict), (
        f"the status object must carry the task counts; got {counts!r}"
    )
    assert counts.get("pending") == 2, f"unexpected counts: {counts!r}"
    assert counts.get("failed") == 0, f"unexpected counts: {counts!r}"

    _assert_not_parked(body, PLAN_ID)


def test_vp_008_the_phase_source_says_which_tier_answered(plans_dir: Path) -> None:
    """Every answer says where its phase came from.

    Two shapes, one rule:

      * the routing row is there — the phase is that row's own
        ``current_phase`` and the source is ``"routing"``;
      * the routing row is missing — the phase falls to the resting
        default and the source says so (``"default"``).

    The second shape is the one the previous round could not pin: it is
    the branch where the phase used to be whatever the implementation
    held.  A reader that can see ``phase_source='default'`` knows the
    phase is a resting guess, not a recorded fact — and a reader that
    cannot see it has to guess again.
    """
    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    routed_id = "20261024-vp-quota-source-routing"
    _write_tasks_file(plans_dir / routed_id)
    _seed_plan(project_dir=project_dir, plan_id=routed_id, phase="ready")

    unrouted_id = "20261024-vp-quota-source-default"
    _write_tasks_file(plans_dir / unrouted_id)
    _seed_plan(
        project_dir=project_dir, plan_id=unrouted_id, phase=None,
        exec_status="not_started",
    )

    client = _client()

    resp = client.get(f"/api/plan/{routed_id}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("phase") == "ready", body.get("phase")
    assert body.get("phase_source") == "routing", (
        f"the routing row answered; got {body.get('phase_source')!r}"
    )

    resp = client.get(f"/api/plan/{unrouted_id}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("phase") == "ready", (
        f"a plan whose routing row cannot be read rests at the manual "
        f"gate; got {body.get('phase')!r}"
    )
    assert body.get("phase_source") == "default", (
        f"the phase was not read from a record, and the answer must say "
        f"so; got {body.get('phase_source')!r}"
    )
    _assert_not_parked(body, unrouted_id)


def test_phase_source_is_reported(plans_dir: Path) -> None:
    """The tier the answer names is the tier that HOLDS the phase.

    The sibling test above reads the routing row through a row whose
    value happens to equal the resting default (``ready``), so a build
    that ignored the routing row entirely would answer
    ``phase='ready', phase_source='routing'`` and pass it.  The label
    says which tier answered; it does not say that the tier's own
    value is the one that was reported.

    So this test reads a routing row that says something the default
    does NOT say (``executing``).  Now the two answers diverge: a build
    that read the row answers ``executing`` / ``routing``; a build that
    did not answers ``ready`` / ``default`` — and ``default`` is the
    honest answer to "we could not read a phase", so the source label
    is what makes the difference visible instead of silent.

    The source is pinned to the known vocabulary as well, because a tier
    renamed by a typo (``routing-row``) would answer a label no reader
    can interpret while every phase assertion still passed.
    """
    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    routed_id = "20261024-vp-quota-source-holds"
    _write_tasks_file(plans_dir / routed_id)
    _seed_plan(project_dir=project_dir, plan_id=routed_id, phase="executing")

    client = _client()
    resp = client.get(f"/api/plan/{routed_id}/status")

    assert resp.status_code == 200, (
        f"a plan with a routing row must answer the status endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body.get("phase") == "executing", (
        f"the routing row says 'executing'; the answer must carry that "
        f"row's own value, not the resting default; got "
        f"{body.get('phase')!r}"
    )
    assert body.get("phase_source") == "routing", (
        f"the routing tier answered, so it must be the tier named; got "
        f"{body.get('phase_source')!r}"
    )
    assert body.get("phase_source") in {"routing", "execution", "default"}, (
        f"the source must name a tier a reader can interpret; got "
        f"{body.get('phase_source')!r}"
    )
    _assert_not_parked(body, routed_id)

    # -- the other direction: no row holds a phase ------------------------
    unrouted_id = "20261024-vp-quota-source-not-routed"
    _write_tasks_file(plans_dir / unrouted_id)
    _seed_plan(project_dir=project_dir, plan_id=unrouted_id, phase=None)

    resp = client.get(f"/api/plan/{unrouted_id}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body.get("phase_source") == "default", (
        f"no record holds a phase for this plan, so the answer must say "
        f"the phase is a resting default; got {body.get('phase_source')!r}"
    )
    assert body.get("phase") == "ready", (
        f"the resting default is the manual gate; got {body.get('phase')!r}"
    )
    _assert_not_parked(body, unrouted_id)


def test_vp_008_a_parked_execution_row_parks_the_phase(plans_dir: Path) -> None:
    """The parked phase is reachable — and only with both witnesses.

    The routing row is missing here, so the phase cannot come from it.
    The durable execution row's ``stop_reason`` says the run stopped for
    capacity, and the plan's ``tasks.json`` carries the capacity marker
    that says why.  Both are needed: without the marker the row says
    only "the run stopped", which is not a capacity fact.

    This is the positive half of the rule.  Without it, the negative
    assertions elsewhere would also pass on a build where the parked
    phase had been renamed away — a green test that pins nothing.
    """
    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    parked_id = "20261024-vp-quota-source-execution"
    _write_tasks_file(plans_dir / parked_id, marker=CAPACITY_MARKER)
    _seed_plan(
        project_dir=project_dir,
        plan_id=parked_id,
        phase=None,                      # no routing row to answer
        exec_status=PARKED,
        stop_reason=PARKED,              # the row's "why did the run stop"
    )

    client = _client()
    resp = client.get(f"/api/plan/{parked_id}/status")

    assert resp.status_code == 200, (
        f"a parked plan must answer the status endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body.get("phase") == PARKED, (
        f"the execution row says the run is parked for capacity and the "
        f"plan carries the marker; got {body.get('phase')!r}"
    )
    assert body.get("phase_source") == "execution", (
        f"the phase came from the durable execution row; got "
        f"{body.get('phase_source')!r}"
    )

    # -- the same row, without the marker: the PHASE is not parked ----------
    # The execution section still reports the row's own verdict, and that
    # is correct: it is a fact about THIS plan's row, not a default
    # applied to every plan.  What the rule governs is the *phase* — and
    # without the capacity marker there is no capacity fact, so the
    # phase rests at the default.
    unmarked_id = "20261024-vp-quota-source-unmarked"
    _write_tasks_file(plans_dir / unmarked_id)   # no marker
    _seed_plan(
        project_dir=project_dir,
        plan_id=unmarked_id,
        phase=None,
        exec_status=PARKED,
        stop_reason=PARKED,
    )

    resp = client.get(f"/api/plan/{unmarked_id}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body.get("phase") == "ready", (
        f"the row says the run stopped but nothing says it stopped for "
        f"capacity; the phase must rest at the default; got "
        f"{body.get('phase')!r}"
    )
    assert body.get("phase_source") == "default", (
        f"got {body.get('phase_source')!r}"
    )
    assert PARKED not in str(body.get("phase")), (
        f"the phase must not be the parked one; got {body.get('phase')!r}"
    )


# ---------------------------------------------------------------------------
# Where the phase comes from — and what happens when the rows are not there
# ---------------------------------------------------------------------------
#
# The round that produced no evidence produced the SAME answer for the
# normal plan and for the parked plan: ``404 Plan not found``.  Two
# plans, two states, one answer — and that answer is not a fact about
# either plan.  It is a fact about the INSTANCE: the plans tree it was
# looking at did not hold them.  A phase that comes from the instance's
# context instead of from the plan's own rows collapses every plan at
# once, and the collapse is silent — the answer is still a body, still
# a status code, still looks like an answer.
#
# So the rule this section pins is the one the PRD asks for: the phase
# is a fact about the plan's own durable rows, and a plan whose rows
# cannot be read is ANSWERED ("I cannot say") rather than handed a
# default phase — a default is a claim nobody made.


def test_a_plan_with_files_but_no_durable_rows_is_answered_503(
    plans_dir: Path,
) -> None:
    """No rows, no phase: the instance says it cannot answer.

    The plan's files are here — ``tasks.json`` and all — but the durable
    state holds no row for it: neither the routing row nor the execution
    row.  There is nothing to derive a phase from, and the two answers
    an instance could give are both wrong: ``ready`` is a phase nobody
    recorded, and "the plan does not exist" is false, because the plan's
    files are right there.

    So the answer is ``503`` with the data source named: the caller
    learns WHICH store could not answer, and can go and look at it.
    """
    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    plan_id = "20261024-vp-quota-no-rows"
    _write_tasks_file(plans_dir / plan_id)

    client = _client()
    resp = client.get(f"/api/plan/{plan_id}/status")

    assert resp.status_code == 503, (
        f"a plan whose durable rows cannot be read must be answered 503; "
        f"got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert body.get("data_source") == "state_db", (
        f"the answer must name the data source that could not answer; got "
        f"{body!r}"
    )
    _assert_not_parked(body, plan_id)


def test_a_plan_whose_rows_are_here_but_whose_files_are_not_is_answered_503(
    plans_dir: Path,
) -> None:
    """The other half of the collapse: the rows are here, the files are not.

    This is the shape the failed round actually hit — the instance's
    plans tree does not hold the plan, while the durable state does.
    ``Plan not found`` is a FALSE answer here: the plan is there, in
    the store that decides where it is.  And a false answer is worse
    than a refusal, because the caller believes it.

    So the answer is ``503`` with the data source named — not ``404``.
    The negative control at the end is what keeps this honest: a plan
    that is in neither the tree nor the state is still a ``404``.
    """
    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    plan_id = "20261024-vp-quota-wrong-instance"
    _seed_plan(project_dir=project_dir, plan_id=plan_id)
    # The tree holds ANOTHER plan, so the tree itself is searchable —
    # this instance is simply pointed at a tree that does not hold
    # THIS plan.
    _write_tasks_file(plans_dir / "20261024-vp-quota-other-plan")

    client = _client()
    resp = client.get(f"/api/plan/{plan_id}/status")

    assert resp.status_code == 503, (
        f"a plan the durable state knows about must not be answered 404 by "
        f"an instance whose tree does not hold it; got {resp.status_code}: "
        f"{resp.text!r}"
    )
    body = resp.json()
    assert body.get("data_source") == "state_db", (
        f"the answer must name the data source that holds the plan; got "
        f"{body!r}"
    )
    assert body.get("searched_dir") == str(plans_dir), (
        f"the answer must name the tree that was searched; got {body!r}"
    )

    # -- the negative control: a plan in neither the tree nor the state ---
    unknown_id = "20261024-vp-quota-unknown-everywhere"
    resp = client.get(f"/api/plan/{unknown_id}/status")

    assert resp.status_code == 404, (
        f"a plan that is in neither the tree nor the durable state is a "
        f"plan that does not exist; got {resp.status_code}: {resp.text!r}"
    )
    assert resp.json().get("detail") == "Plan not found", resp.text


def test_the_phase_rule_is_a_pure_function_of_the_rows_it_is_handed() -> None:
    """The rule reads the rows it is given — and refuses when it has none.

    This is the function the route's answer comes from, so it is the
    function that has to be honest: it takes the plan's own rows, it
    reads nothing else, and when the rows are not there it says so
    instead of producing a phase.

    The last assertion is the important one: a rule that answers
    "ready" for a plan it knows nothing about is a rule that will
    answer "ready" for EVERY plan it knows nothing about — and a
    default that is global is how a plan ends up claiming to be waiting
    for capacity it never asked for.
    """
    from routes.plans import PlanDataMissing, derive_phase

    # The routing row answers — with its OWN value, not the resting one.
    derived = derive_phase([{"current_phase": "executing"}], None, None)
    assert derived.phase == "executing", (
        f"the routing row's own phase must be the answer; got {derived!r}"
    )
    assert derived.source == "routing", f"got {derived!r}"

    # The execution row answers, when it says the run parked for capacity
    # AND the plan carries the capacity marker that says why.
    derived = derive_phase(
        [],
        {"exec_status": PARKED, "stop_reason": PARKED},
        CAPACITY_MARKER,
    )
    assert derived.phase == PARKED, f"got {derived!r}"

    # The same rows, the same answer — every time.  A rule that reads
    # anything outside its arguments would answer differently the second
    # time it was asked.
    first = derive_phase([{"current_phase": "executing"}], None, None)
    second = derive_phase([{"current_phase": "executing"}], None, None)
    assert first == second, f"{first!r} != {second!r}"

    # No rows at all: the rule REFUSES rather than inventing a phase.
    with pytest.raises(PlanDataMissing) as excinfo:
        derive_phase([], None, None)
    assert excinfo.value.data_source == "state_db", (
        f"the refusal must name the data source that could not answer; got "
        f"{excinfo.value!r}"
    )


def test_the_phase_rule_reads_no_process_state() -> None:
    """Nothing outside the rows reaches the phase.

    The phase rule used to live inside the route, where the process's
    own memory was one step away: an in-process registry of runs, a
    module-level dict of provenances, a clock.  A rule that can see any
    of those answers a question about the PROCESS, not about the plan —
    and the process changes (it restarts, it serves other plans, it
    runs at a different time) while the plan does not.

    So the rule's source is checked for the reads that would make it
    process-dependent — and the module is checked for the second rule
    that a rule like this grows: production must consult ONE phase
    rule, or the two would drift apart and give one plan two answers.
    """
    import inspect

    import routes.plans as rp

    source = inspect.getsource(rp.derive_phase)
    for forbidden in (
        "_execution_state",
        "_LISTING_PROVENANCE",
        "os.environ",
        "datetime.now",
        "time.time",
    ):
        assert forbidden not in source, (
            f"the phase rule must not read {forbidden!r}; it answers a "
            f"question about the process instead of about the plan:\n"
            f"{source}"
        )

    module_source = inspect.getsource(rp)
    assert "plan_status.derive_phase" not in module_source, (
        "the route must derive the phase from the rule it owns, not from a "
        "second rule imported from elsewhere: two rules give one plan two "
        "answers"
    )


def test_the_capacity_marker_has_one_reader() -> None:
    """The marker is parsed in exactly one place.

    The capacity marker is the half of the parked verdict that says
    *why* the run stopped.  It is a free-form "why did this stop" slot
    in ``tasks.json``, so every reader has to agree on what counts as a
    capacity fact — and two readers that disagree produce two answers
    for one plan, one of which says the plan is waiting for capacity
    when it is not.

    So there is one entry point for reading it, and the phase rule goes
    through it.  The last assertion is what makes that checkable: if
    the rule had its own reader, replacing the entry point would not
    change the rule's answer.
    """
    import capacity_wait

    marker = capacity_wait.parse_capacity_marker(
        {"reason_detail": json.dumps(CAPACITY_MARKER)},
    )
    assert marker == CAPACITY_MARKER, f"got {marker!r}"

    # Every shape that is not a capacity fact reads as no marker.
    for payload in (
        None,
        {},
        {"reason_detail": ""},
        {"reason_detail": "{not json at all"},
        {"reason_detail": json.dumps({"reason": "the operator stopped it"})},
        {"reason_detail": json.dumps({})},
    ):
        assert capacity_wait.parse_capacity_marker(payload) is None, (
            f"{payload!r} is not a capacity marker"
        )

    # The rule goes through the entry point: replacing it changes the
    # rule's answer.
    from routes.plans import derive_phase

    original = capacity_wait.parse_capacity_marker
    try:
        capacity_wait.parse_capacity_marker = lambda _payload: None
        derived = derive_phase(
            [],
            {"exec_status": PARKED, "stop_reason": PARKED},
            original({"reason_detail": json.dumps(CAPACITY_MARKER)}),
        )
        assert derived.phase != PARKED, (
            f"the rule must read the marker through the entry point; got "
            f"{derived!r}"
        )
    finally:
        capacity_wait.parse_capacity_marker = original


# ---------------------------------------------------------------------------
# The round's service outlives its starter — the plan still answers
# ---------------------------------------------------------------------------
#
# The control this file pins could not be read on 2026-10-26: the probe
# was refused a connection on port 64469 at 02:25:01, because the
# process that started the service was gone and another instance was
# answering on the same port.  So the round never learned whose answers
# it was reading, and the criterion was never looked at.
#
# The round now names the instance it is asking — its **epoch**: the pid
# and the boot id it read over ``/health/data`` — and compares that name
# before every dispatch.  When the name moves, the answers obtained
# under the old name are not verdicts about the product: they are marked
# ``invalid_env`` and get the round's one second look.
#
# None of that may touch the control's semantics.  The plan is still a
# plan at the manual gate, and it still answers with its own four facts.


class _InstanceService:
    """A real HTTP service that answers ``/health/data`` like the backend.

    The round asks the service who it is over HTTP — the same road a
    verification point's request travels — so the criterion needs a
    service that really answers.  What it publishes about itself is
    configurable, because the point of the criterion is what happens
    when the answer says "I am somebody else".
    """

    def __init__(self, rows: int = 4) -> None:
        self.rows = rows
        self.instance: Optional[dict] = None
        self.requests: List[str] = []
        service = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 - the handler API's own name
                service.requests.append(self.path)
                body: Dict[str, Any] = {
                    "status": "ok",
                    "plan_routing_rows": service.rows,
                }
                if service.instance is not None:
                    body["instance"] = service.instance
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):  # noqa: A003 - silence the log
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def publish(self, instance: Optional[dict]) -> None:
        """Change what the service says about itself."""
        self.instance = instance

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _instance_block(boot_id: str, plans_dir: Path, state_db: Path) -> dict:
    """The ``instance`` block the backend publishes about itself."""
    return {
        "boot_id": boot_id,
        "pid": 4242,
        "pgid": 4242,
        "cwd": str(plans_dir),
        "started_at": "2026-10-26T02:21:51+00:00",
        "plans_dir": str(plans_dir),
        "state_db_path": str(state_db),
        "state_db_inode": int(state_db.stat().st_ino),
        "plan_routing_rows": 4,
    }


def _round_agent(
    plan_dir: Path,
    project_dir: Path,
    port: int,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_runtime: bool = True,
) -> Tuple[Any, Dict[str, Any]]:
    """A verification agent whose ``api`` service is the test's server.

    Everything that answers the round is the product's own code: the
    service manager is stubbed at the one point where it would start a
    process, and the data-plane fingerprint, the instance identity and
    the epoch are all read by the round itself.

    ``with_runtime=False`` is the round that started **nothing**: the
    service manager hands back no process, so the round has no record
    of an instance either — the case in which the round cannot say
    whose answers it would be reading.
    """
    from verification_agent import VerificationAgent

    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )
    plan: Dict[str, Any] = {
        "services": [{
            "name": "api",
            "port": port,
            "start_cmd": "python -m api.server",
            "cwd": ".",
            "health_url": f"http://127.0.0.1:{port}/health",
        }],
        "verification_points": [
            {
                "id": vp_id,
                "title": f"point {vp_id}",
                "verification_method": "api_test",
                "uses_services": ["api"],
                "request": {
                    "method": "GET",
                    "url": "{{svc.api.url}}/items",
                },
                "assertions": [{"status": 200}],
            }
            for vp_id in TASK_IDS
        ],
    }
    agent._normalize_service_declarations(plan)
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None,
    )

    from service_freshness import DataPlaneFingerprint, PortProbe

    monkeypatch.setattr(
        "service_freshness.probe_port",
        lambda port: PortProbe(port=port, listening=True),
    )
    monkeypatch.setattr(
        "service_freshness.probe_data_plane",
        lambda port, **kwargs: DataPlaneFingerprint(
            port=port, pid=4242, routing_rows=4, expected_rows=4,
            status="ok",
        ),
    )
    monkeypatch.setattr(
        "service_freshness.expected_routing_rows", lambda *a, **k: 4,
    )

    import service_manager as sm

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda project, declarations, round_number=0, **kwargs:
            sm.ServiceRuntimeMap(runtimes={
                "api": sm.ServiceRuntime(
                    name="api", port=port, pid=4242, origin="spawned",
                    ready=True, start_cmd="x",
                ),
            } if with_runtime else {}),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    agent.start_verification_round(1)
    return agent, plan


def _spy_api_runner(
    monkeypatch: pytest.MonkeyPatch,
    service: _InstanceService,
    publish_per_dispatch: List[Optional[dict]],
) -> List[str]:
    """Record every dispatch — and hand the port over while it runs.

    This is the production shape: the process that started the service
    is gone, and another instance answers on the same port while the
    round is still running.  ``publish_per_dispatch`` is what the
    service says about itself **after** each dispatch, in order; the
    last one is repeated.
    """
    import verification_api_runner as var

    dispatched: List[str] = []

    class _Result:
        status = "PASSED"
        evidence = {"status_code": 200}

        def to_verdict_dict(self):
            return {
                "status": "PASSED",
                "reasons": ["ok"],
                "evidence": dict(self.evidence),
            }

    def fake_run(vp, **kwargs):
        dispatched.append(str(vp.get("id")))
        if publish_per_dispatch:
            index = min(len(dispatched) - 1, len(publish_per_dispatch) - 1)
            service.publish(publish_per_dispatch[index])
        return _Result()

    monkeypatch.setattr(var, "run_api_verification", fake_run)
    return dispatched


def _run_all_vps(executor) -> list:
    """Run every VP of *executor* through the runner, in order.

    The verdicts are recorded in the executor the way the executor
    records them — so the round's own results envelope is built by the
    round's own code, not written by hand here.
    """
    async def _drive():
        verdicts = []
        for vp in executor.verification_plan["vps"]:
            verdict = await executor.sub_agent_runner(vp)
            executor.record_verdict(str(vp.get("id")), verdict)
            verdicts.append(verdict)
        return verdicts

    return asyncio.run(_drive())


def _evidence_of(result: Dict[str, Any]) -> Dict[str, Any]:
    """One result's evidence, as a dict.

    The round's own writer may hand the evidence over as a JSON string
    — this reads it either way, so the test asks about the evidence and
    not about the shape the round happened to write it in.
    """
    evidence = result.get("evidence")
    if isinstance(evidence, dict):
        return evidence
    if isinstance(evidence, str):
        try:
            loaded = json.loads(evidence)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def test_a_normal_plan_stays_answerable_after_the_round_service_outlives_its_starter(
    plans_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The four facts survive a round whose service changed hands.

    The criterion is a negative control: a plan with no capacity marker
    keeps its own semantics — ``phase='ready'``,
    ``execution_status='not_started'``, ``tasks.pending=2``,
    ``tasks.failed=0`` — and the parked word appears nowhere in its
    answer.

    On 2026-10-26 that control could not be read at all: the probe was
    refused a connection on port 64469 at 02:25:01, because the process
    that started the service was gone and another instance was answering
    on the same port.  So the round now names the instance it is asking
    (its **epoch**) and compares that name before every dispatch.

    This test is that situation, end to end.  The service is taken over
    mid-round, and the round has to

      * know that the instance behind its address changed hands;
      * refuse to treat the answers obtained under the old instance as
        verdicts about the product (``invalid_env``), and give those
        points the round's one second look; and
      * leave the plan's own four facts exactly as they were.

    The last one is the control.  The round's bookkeeping is about
    instances; it is not allowed to say anything about a plan that never
    asked for capacity.
    """
    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write_tasks_file(plans_dir / PLAN_ID)
    _seed_plan(project_dir=project_dir)

    plan_dir = plans_dir / PLAN_ID
    state_db = Path(server._state_db_path())

    service = _InstanceService()
    try:
        agent, plan = _round_agent(
            plan_dir, project_dir, service.port, monkeypatch,
        )
        # The round's own instance — the one its ``ensure`` produced.
        service.publish(_instance_block("boot-1", plan_dir, state_db))
        agent._preflight_service_freshness(plan)

        epoch_before = agent.round_instance_epoch()
        # ... and then another process takes the port mid-round.
        dispatched = _spy_api_runner(
            monkeypatch, service,
            [_instance_block("boot-2", plan_dir, state_db)],
        )

        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan),
        )
        _run_all_vps(executor)
        round_results = agent._build_execution_results_from_executor(
            plan, executor,
        )

        # -- the round knew whose instance it was asking -----------------
        assert epoch_before.get("boot_id") == "boot-1", epoch_before
        assert epoch_before.get("pid") == 4242, epoch_before
        epoch_after = agent.round_instance_epoch()
        assert epoch_after.get("boot_id") == "boot-2", epoch_after
        assert epoch_after.get("token") != epoch_before.get("token"), (
            f"the epoch must be a name, not a constant: {epoch_after!r}"
        )

        # -- the answer under the old instance is not a verdict ---------
        conflicts = agent._detect_round_evidence_conflicts(
            plan, round_results,
        )
        epoch_conflicts = [
            conflict for conflict in conflicts
            if conflict.get("kind") == "instance_epoch_changed"
        ]
        assert len(epoch_conflicts) == 1, conflicts
        assert epoch_conflicts[0]["failed_vps"] == ["VP-001"], (
            f"only the point answered under the old epoch is contradicted; "
            f"got {epoch_conflicts[0]!r}"
        )

        marked = agent._mark_invalid_env_results(round_results, conflicts)
        assert marked == ["VP-001"], marked
        point = round_results["execution_results"][0]
        assert point["status"] == "invalid_env", (
            f"an answer from an instance the round cannot name is not a "
            f"verdict about the product; got {point!r}"
        )
        assert point["status"] != "FAILED", point

        # -- and the contradicted point got the round's one second look --
        agent._reconcile_round_evidence_conflicts(plan, round_results)
        assert dispatched == ["VP-001", "VP-001"], (
            f"the request must not go to an instance the round did not "
            f"start — so the second point never leaves, and the "
            f"contradicted one is looked at again; got {dispatched!r}"
        )

        # The second point was **not** asked: the instance answering the
        # round's address is not the one the round ensured, and an
        # answer from it would be a confident statement about somebody
        # else's data.  It is BLOCKED, never FAILED — FAILED would say
        # "the project answered wrong", and the project was never asked.
        points = {
            str(point.get("id")): point
            for point in round_results["execution_results"]
        }
        assert set(points) == {"VP-001", "VP-002"}, points
        blocked = points["VP-002"]
        assert blocked["status"] == "BLOCKED", blocked
        assert blocked["status"] != "FAILED", blocked
        assert _evidence_of(blocked).get("error_kind") == (
            "service_instance_mismatch"
        ), blocked
    finally:
        service.close()

    # -- the control: the plan still answers with its own four facts -----
    client = _client()
    resp = client.get(f"/api/plan/{PLAN_ID}/status")
    assert resp.status_code == 200, (
        f"the plan must answer the status endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body["phase"] == "ready", body.get("phase")
    execution = body.get("execution")
    assert isinstance(execution, dict), execution
    assert execution["execution_status"] == "not_started", execution
    counts = body.get("tasks")
    assert isinstance(counts, dict), counts
    assert counts.get("pending") == 2, counts
    assert counts.get("failed") == 0, counts

    _assert_not_parked(body, PLAN_ID)


def test_a_round_that_cannot_name_its_instance_dispatches_nothing(
    plans_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round that cannot say whose answers it reads sends no request.

    The epoch is what makes an answer attributable.  A round that talks
    to a service and cannot name the instance behind it — neither by
    the service's own statement nor by the round's own record of the
    process it started — would be reading an address whose owner
    nobody can say.  So it dispatches nothing and says so, instead of
    producing a verdict about a product nobody asked.

    The control is untouched by this: the plan is still a plan at the
    manual gate, with its own four facts, and the parked word is still
    nowhere in its answer.
    """
    import verification_loop as vl

    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write_tasks_file(plans_dir / PLAN_ID)
    _seed_plan(project_dir=project_dir)

    plan_dir = plans_dir / PLAN_ID

    service = _InstanceService()
    try:
        # A round that started nothing and talks to a service that does
        # not identify itself: there is no instance to name, by either
        # road, so the round cannot say whose answers it would read.
        agent, plan = _round_agent(
            plan_dir, project_dir, service.port, monkeypatch,
            with_runtime=False,
        )
        service.publish(None)
        dispatched = _spy_api_runner(monkeypatch, service, [])

        monkeypatch.setattr(
            agent, "generate_verification_plan", lambda: plan,
        )
        agent.set_round_service_preflight(
            lambda: vl._run_service_preflight(
                agent, PLAN_ID, plan_dir,
            ),
        )
        report = agent.run_full_verification()
    finally:
        service.close()

    assert report["overall_status"] == "BLOCKED", report
    assert report["verification_results"] == [], report
    failure = report.get("service_preflight_failed") or {}
    assert failure.get("error_kind") == "instance_epoch_unresolved", failure
    assert dispatched == [], (
        f"a round that cannot name its instance must dispatch nothing; "
        f"got {dispatched!r}"
    )

    # -- the control: the plan still answers with its own four facts -----
    client = _client()
    resp = client.get(f"/api/plan/{PLAN_ID}/status")
    assert resp.status_code == 200, (
        f"the plan must answer the status endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body["phase"] == "ready", body.get("phase")
    execution = body.get("execution")
    assert isinstance(execution, dict), execution
    assert execution["execution_status"] == "not_started", execution
    counts = body.get("tasks")
    assert isinstance(counts, dict), counts
    assert counts.get("pending") == 2, counts
    assert counts.get("failed") == 0, counts

    _assert_not_parked(body, PLAN_ID)


def test_a_service_that_cannot_identify_itself_is_still_asked(
    plans_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service that cannot be **compared** is not blocked.

    "Not comparable" is not "wrong".  Most of the services a plan
    declares are the project's own — an application server, a static
    preview — and they do not answer ``/health/data``, so they cannot
    say who they are.  The round still started them, so it knows which
    instance it asked for, and the round asks them: what it cannot do
    is compare the answer with the instance that gave it, and that is
    not a reason to refuse the question.

    The gate only refuses a round that has **no** name at all.
    """
    import verification_loop as vl

    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write_tasks_file(plans_dir / PLAN_ID)
    _seed_plan(project_dir=project_dir)

    plan_dir = plans_dir / PLAN_ID

    service = _InstanceService()
    try:
        agent, plan = _round_agent(
            plan_dir, project_dir, service.port, monkeypatch,
        )
        # The service does not identify itself ...
        service.publish(None)
        # ... but the round ensured it, so it knows which instance it
        # asked for.
        agent._preflight_service_freshness(plan)

        failure = vl._run_service_preflight(agent, PLAN_ID, plan_dir)
        epoch = agent.round_instance_epoch()
    finally:
        service.close()

    assert failure is None, (
        f"a service the round started itself must not be blocked; got "
        f"{failure!r}"
    )
    assert epoch, (
        f"the round knows which instance it asked for; got {epoch!r}"
    )
    assert epoch.get("token") == "", (
        f"there is nothing to compare when the service does not identify "
        f"itself; got {epoch!r}"
    )
    assert epoch.get("ensured_token"), (
        f"the round's own record of the instance it started is the second "
        f"road to a name; got {epoch!r}"
    )
