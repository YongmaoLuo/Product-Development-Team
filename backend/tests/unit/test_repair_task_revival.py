"""2026-10-13 — a ``failed`` repair task is re-queued while its VP still fails.

The accident
------------
A ``repair-*`` row can reach ``status='failed'`` without ever having
executed: its ``failure_reason`` is an API error about the selected model,
so the run dies in the provider before a single tool call.
``_get_pending_repair_tasks`` — the only thing that feeds the executor queue
— accepted ``status == 'pending'`` and nothing else, so those rows were
unreachable forever. The provider chain was fixed separately; the buried
work was not.

The rules under test
--------------------
1. ``failed`` + ``failed_vp_id`` in this round's failure set → queued.
2. ``failed`` + that VP passed this round → not queued.
3. ``attempt >= 3`` → not queued, and an explicit
   ``repair_task_abandoned_max_attempts`` event says so.
4. A task the breakdown flow already split → not queued (its work is in
   the children; re-queueing the parent runs the same fix twice).

Every test drives :func:`_get_pending_repair_tasks` against a real
``state.db`` — the function the executor's queue actually comes from — and
``test_call_sites_pass_the_rounds_failure_set`` pins the wiring that feeds
it, so the suite cannot pass while the production path still passes
``pending`` only.
"""
from __future__ import annotations

import logging
import sqlite3

import pytest


#: The provider's own ``failure_reason`` shape, as it lands in
#: ``plan_tasks.failure_reason``: the model is rejected before any tool
#: call, so the task never really ran.
API_404 = (
    "[API_ERROR:404] There's an issue with the selected model "
    "(<model-name>). It may not exist or you may not have access to it."
)

PLAN_ID = "test-repair-revival"


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """A real migrated ``state.db``, wired in as ``server._state_db_path``."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    import server
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    return db_path


def add_repair(db_path, task_id, *, status="pending", vp="VP-005",
               round_n=1, failure_reason="", attempt=None,
               task_group="repair-round-1"):
    """Insert one ``plan_tasks`` row with the columns the queue reads."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(db_path)
    try:
        repo = PlanTaskRepository(conn)
        repo.add_task(PLAN_ID, {
            "id": task_id,
            "title": f"repair {vp}",
            "description": "fix it",
            "task_group": task_group,
            "failed_vp_id": vp,
            "round": round_n,
        })
        fields = {"status": status}
        if failure_reason:
            fields["failure_reason"] = failure_reason
        if attempt is not None:
            fields["attempt"] = attempt
        repo.update_task(PLAN_ID, task_id, fields)
    finally:
        conn.close()


def refail(db_path, task_id, failure_reason=""):
    """Stand in for the executor finishing a revived task with a failure."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(db_path)
    try:
        PlanTaskRepository(conn).update_task(
            PLAN_ID, task_id,
            {"status": "failed", "failure_reason": failure_reason},
        )
    finally:
        conn.close()


def row_of(db_path, task_id):
    """``(status, attempt, failure_reason)`` for one task, read back."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        found = conn.execute(
            "SELECT status, attempt, failure_reason FROM plan_tasks "
            "WHERE plan_id = ? AND task_id = ?",
            (PLAN_ID, task_id),
        ).fetchone()
    finally:
        conn.close()
    assert found is not None, f"{task_id} is not in plan_tasks"
    return found


def queue(plan_id=PLAN_ID, failed_vp_ids=None):
    """The production entry: exactly what the auto-loop calls."""
    import server

    return server._get_pending_repair_tasks(plan_id, failed_vp_ids)


def ids(entries):
    return sorted(t["id"] for t in entries)


# ---------------------------------------------------------------------------
# 1. failed + VP still failing -> queued
# ---------------------------------------------------------------------------


def test_failed_row_with_failing_vp_is_requeued(state_db):
    """A failed repair whose VP is still in this round's failure set comes
    back as queued work, counted and re-queued on disk."""
    add_repair(state_db, "repair-r1-01", status="failed",
               failure_reason=API_404)

    assert ids(queue(failed_vp_ids={"VP-005"})) == ["repair-r1-01"]
    assert row_of(state_db, "repair-r1-01")[:2] == ("pending", 1)


def test_api_error_404_row_is_recovered(state_db):
    """The real incident shape: the provider never connected, the row has
    sat at ``failed`` untouched since. This is the 31 buried tasks."""
    add_repair(state_db, "repair-r1-07", status="failed", vp="VP-011",
               round_n=1, failure_reason=API_404)

    queued = queue(failed_vp_ids={"VP-011"})

    assert ids(queued) == ["repair-r1-07"], (
        "a repair that never ran because the model 404'd must be re-queued "
        "once its VP is still failing"
    )
    assert row_of(state_db, "repair-r1-07")[:2] == ("pending", 1)


def test_failure_reason_survives_the_revive(state_db):
    """The executor feeds ``failure_reason`` into the next prompt
    (``_build_prior_failure_block``); reviving must not erase the evidence
    of what went wrong last time."""
    add_repair(state_db, "repair-r1-01", status="failed",
               failure_reason=API_404)

    queue(failed_vp_ids={"VP-005"})

    assert row_of(state_db, "repair-r1-01")[2] == API_404


# ---------------------------------------------------------------------------
# 2. failed + VP passed -> not queued
# ---------------------------------------------------------------------------


def test_failed_row_whose_vp_passed_is_not_requeued(state_db):
    add_repair(state_db, "repair-r1-01", status="failed", vp="VP-005")
    add_repair(state_db, "repair-r1-02", status="failed", vp="VP-009")

    queued = queue(failed_vp_ids={"VP-009"})

    assert ids(queued) == ["repair-r1-02"]
    assert row_of(state_db, "repair-r1-01")[:2] == ("failed", None), (
        "a repair for a VP that passed must keep its history untouched"
    )


def test_no_failure_set_disables_revival(state_db):
    """``failed_vp_ids=None`` is what the read-only probes pass (the
    operator reset endpoint, the undrained-repair guard). Neither has a
    round verdict, so neither may revive anything."""
    add_repair(state_db, "repair-r1-01", status="failed", failure_reason=API_404)

    assert queue() == []
    assert row_of(state_db, "repair-r1-01")[:2] == ("failed", None)


def test_row_with_no_failed_vp_id_is_never_revived(state_db):
    """The rule stands on its own: no VP, no verdict to test against.

    The docstring here used to justify itself with "breakdown children
    carry no ``failed_vp_id``". They stopped being that on 2026-10-13 —
    ``refiner_structure.inherit_failed_vp`` gives a breakdown child the
    VP of the repair task it was split from — so the justification is
    gone and the rule is unchanged. What still lands here is every row
    written before that change: all 24 breakdown children in the live
    database still read NULL, and nothing backfills them.
    See ``tests/unit/test_breakdown_inherits_failed_vp.py``.
    """
    add_repair(state_db, "repair-r1-01-1", status="failed", vp=None,
               round_n=None, failure_reason="empty_diff_no_changes")

    assert queue(failed_vp_ids={"VP-005"}) == []
    assert row_of(state_db, "repair-r1-01-1")[:2] == ("failed", None)


def test_completed_row_is_not_revived(state_db):
    add_repair(state_db, "repair-r1-01", status="completed")

    assert queue(failed_vp_ids={"VP-005"}) == []
    assert row_of(state_db, "repair-r1-01")[:2] == ("completed", None)


# ---------------------------------------------------------------------------
# 3. attempt cap
# ---------------------------------------------------------------------------


def test_attempt_cap_abandons_and_logs(state_db, caplog):
    """At the cap the row is dropped — loudly, never silently."""
    add_repair(state_db, "repair-r1-01", status="failed",
               failure_reason="empty_diff_no_changes: produced no diff")

    for expected in (1, 2, 3):
        # Between rounds the executor re-fails the task it was given; the
        # revive itself only has to move ``attempt`` forward each time it
        # is offered the row again.
        queue(failed_vp_ids={"VP-005"})
        assert row_of(state_db, "repair-r1-01")[1] == expected
        refail(state_db, "repair-r1-01", "empty_diff_no_changes: produced no diff")

    with caplog.at_level(logging.WARNING, logger="server"):
        assert queue(failed_vp_ids={"VP-005"}) == []

    assert row_of(state_db, "repair-r1-01")[:2] == ("failed", 3)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "repair_task_abandoned_max_attempts" in text
    assert "repair-r1-01" in text
    assert "VP-005" in text
    assert "empty_diff_no_changes" in text


def test_attempt_at_cap_is_never_revived(state_db):
    add_repair(state_db, "repair-r1-01", status="failed", attempt=3)

    assert queue(failed_vp_ids={"VP-005"}) == []
    assert row_of(state_db, "repair-r1-01")[:2] == ("failed", 3)


def test_revival_is_idempotent_within_a_round(state_db):
    """The auto-loop asks twice per verdict (``_pending`` then
    ``_pending_db``). ``attempt`` must advance once, not twice."""
    add_repair(state_db, "repair-r1-01", status="failed")

    queue(failed_vp_ids={"VP-005"})
    queue(failed_vp_ids={"VP-005"})
    queue(failed_vp_ids={"VP-005"})

    assert row_of(state_db, "repair-r1-01")[:2] == ("pending", 1)


# ---------------------------------------------------------------------------
# 4. split parents
# ---------------------------------------------------------------------------


def test_split_parent_is_not_requeued(state_db, caplog):
    """The plan still carries ``repair-r1-01`` at
    ``failed`` beside ``repair-r1-01-1/-2`` — the parent survives the
    breakdown, so revival would run the children's work twice."""
    add_repair(state_db, "repair-r1-01", status="failed", failure_reason=API_404)
    add_repair(state_db, "repair-r1-01-1", status="completed", vp=None,
               round_n=None)
    add_repair(state_db, "repair-r1-01-2", status="failed", vp=None,
               round_n=None, failure_reason="empty_diff_no_changes")

    with caplog.at_level(logging.WARNING, logger="server"):
        queued = queue(failed_vp_ids={"VP-005"})

    assert "repair-r1-01" not in ids(queued)
    assert row_of(state_db, "repair-r1-01")[:2] == ("failed", None), (
        "the parent must stay failed — its work is in its children"
    )
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "repair_task_skipped_split_into_children" in text
    assert "repair-r1-01-1" in text


def test_split_parent_of_a_deeper_chain_is_not_requeued(state_db):
    """A chain three deep (parent -> child -> four grandchildren); the
    outermost parent is still ``failed`` and still has a live VP."""
    add_repair(state_db, "repair-r4-01", status="failed", vp="VP-005")
    add_repair(state_db, "repair-r4-01-5", status="failed", vp=None,
               round_n=None)
    add_repair(state_db, "repair-r4-01-5-1", status="completed", vp=None,
               round_n=None)

    assert ids(queue(failed_vp_ids={"VP-005"})) == []


def test_sibling_task_number_is_not_mistaken_for_a_child(state_db):
    """``R6-10`` is a sibling of ``R6-1``, not its breakdown child. A
    naive ``startswith(tid + "-")`` would retire ``R6-1`` forever."""
    add_repair(state_db, "R6-1", status="failed", vp="VP-005",
               task_group="repair-round-6", round_n=6)
    add_repair(state_db, "R6-10", status="pending", vp="VP-009",
               task_group="repair-round-6", round_n=6)

    assert ids(queue(failed_vp_ids={"VP-005"})) == ["R6-1", "R6-10"]


def test_pending_row_is_still_queued_alongside_a_revived_one(state_db):
    add_repair(state_db, "repair-r1-01", status="failed", failure_reason=API_404)
    add_repair(state_db, "repair-r1-02", status="pending")

    assert ids(queue(failed_vp_ids={"VP-005"})) == [
        "repair-r1-01", "repair-r1-02",
    ]


# ---------------------------------------------------------------------------
# The wiring: the failure set has to actually reach the selector
# ---------------------------------------------------------------------------


def test_round_failed_vp_ids_uses_the_orchestrators_own_verdict(state_db):
    """The criterion is ``VerificationOrchestrator._extract_failed_ids``,
    not a second reader — that method knows about ``timeout`` / ``SPLIT``
    and the ``rounds[-1].results`` fallback. Drive the real method."""
    import verification_loop
    from verification.orchestrator import VerificationOrchestrator

    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {"id": "VP-005", "status": "FAILED"},
            {"id": "VP-006", "status": "PASSED"},
            {"id": "VP-007", "status": "timeout"},
        ],
        "requirement_deviations": [],
    }
    # ``_extract_failed_ids`` reads no instance state, so the real method
    # on a bare instance is the production one, not a stand-in.
    orch = VerificationOrchestrator.__new__(VerificationOrchestrator)
    failed = verification_loop._round_failed_vp_ids(orch, report)

    assert failed == {"VP-005", "VP-007"}

    add_repair(state_db, "repair-r1-01", status="failed", vp="VP-005")
    add_repair(state_db, "repair-r1-03", status="failed", vp="VP-007")
    add_repair(state_db, "repair-r1-04", status="failed", vp="VP-006")

    assert ids(queue(failed_vp_ids=failed)) == ["repair-r1-01", "repair-r1-03"]


def test_round_failed_vp_ids_degrades_to_revival_off():
    import verification_loop

    class Broken:
        def _extract_failed_ids(self, report):
            raise RuntimeError("boom")

    assert verification_loop._round_failed_vp_ids(Broken(), {}) is None
    assert verification_loop._round_failed_vp_ids(object(), {}) is None
    assert verification_loop._round_failed_vp_ids(Broken(), "not-a-dict") is None


def test_call_sites_pass_the_rounds_failure_set():
    """Pin the wiring. The revival rule is inert unless the auto-loop hands
    the round's failure set to the selector, and the two are edited far
    apart — this is the assertion that fails if only the helper changes."""
    import inspect

    import verification_loop

    source = inspect.getsource(
        verification_loop._run_auto_verification_loop_inner,
    )
    calls = source.count("_get_pending_repair_tasks(")
    assert calls == 5, f"expected the 5 production call sites, found {calls}"
    # Four of them sit after a round verdict exists and must carry it; the
    # fifth is the undrained-repair guard, pinned separately below.
    assert source.count("_round_failed_vp_ids(") == 4, (
        "every call site that has this round's report must pass the "
        "failure set — a revival rule the production path never feeds is "
        "dead code"
    )
    assert source.count("_get_pending_repair_tasks(plan_id)") == 1, (
        "exactly one call site may run without a failure set"
    )


def test_read_only_probe_sites_pass_no_failure_set():
    """The undrained-repair guard runs BEFORE the next round's verdict
    exists. It must stay a plain pending probe, or it would revive on the
    strength of the PREVIOUS round's failures."""
    import inspect

    import verification_loop

    source = inspect.getsource(
        verification_loop._run_auto_verification_loop_inner,
    )
    guard = source.split("def _on_repair_complete", 1)[1]
    assert "_get_pending_repair_tasks(plan_id)" in guard, (
        "the undrained-repair guard must not receive a failure set"
    )
    guard_probe = guard.split("_get_pending_repair_tasks(plan_id)", 1)[1]
    assert ")" in guard_probe and "failed_vp_ids" not in guard_probe[:400]


def test_reset_endpoint_stays_pending_only():
    """``routes/verification.py``'s operator reset supersedes queued repair
    work. It has no round verdict either, so it must not revive."""
    from pathlib import Path

    import server

    routes = Path(server.__file__).with_name("routes") / "verification.py"
    text = routes.read_text(encoding="utf-8")
    assert "_get_pending_repair_tasks(plan_id)" in text
    assert "_round_failed_vp_ids" not in text


# ---------------------------------------------------------------------------
# Section 3 — the breakdown accounting the revival rule leans on
# ---------------------------------------------------------------------------


def test_breakdown_children_inherit_their_parents_depth():
    """``breakdown_count`` was written by nothing at all — 322 NULL rows in
    the live database — so the depth a repair task reached was unknowable.
    The stamp runs on the same list both stores are written from."""
    import agent

    current = [{"id": "repair-r1-01", "breakdown_count": 0}]
    effective = [
        {"id": "repair-r1-01"},
        {"id": "repair-r1-01-1"},
        {"id": "repair-r1-01-2"},
        {"id": "invented-with-no-parent"},
    ]

    stamped = agent._stamp_breakdown_depths(effective, current)

    assert stamped == {"repair-r1-01-1": 1, "repair-r1-01-2": 1}
    assert effective[1]["breakdown_count"] == 1
    assert effective[2]["breakdown_count"] == 1
    assert "breakdown_count" not in effective[3]
    assert "breakdown_count" not in effective[0], "the parent is untouched"


def test_breakdown_depth_accumulates_across_levels():
    import agent

    current = [{"id": "repair-r4-01", "breakdown_count": 0},
               {"id": "repair-r4-01-5", "breakdown_count": 1}]
    effective = [{"id": "repair-r4-01-5-1"}, {"id": "repair-r4-01-5-2"}]

    agent._stamp_breakdown_depths(effective, current)

    assert [e["breakdown_count"] for e in effective] == [2, 2]


# ---------------------------------------------------------------------------
# Section 3 — the repair-split budget
# ---------------------------------------------------------------------------


def _refine_with(agent_stub, task):
    """Drive the real ``_refine_after_failure`` and report what it did."""
    from agent import AutonomousAgent

    AutonomousAgent._refine_after_failure(
        agent_stub, task, "coder response", "result context", "", 1,
    )
    events = [
        c.args[0] for c in agent_stub.logger.error.call_args_list
    ]
    return events


def _agent_stub():
    from unittest.mock import MagicMock

    stub = MagicMock()
    stub._refinement_count = 0
    stub.plan_id = None
    stub.task_manager = MagicMock()
    stub.task_manager.tasks = []
    stub.refiner = MagicMock()
    # "Refiner returned no changes" — the cheapest path through the rest
    # of the method, so these tests assert on the budget and nothing else.
    stub.refiner.refine.side_effect = lambda **kwargs: kwargs["tasks"]
    return stub


def _repair_task(task_id="repair-r1-01-2", breakdown_count=0):
    from task import SubTask

    return SubTask(
        id=task_id,
        title="fix VP-005",
        description="d",
        test_command="pytest -q",
        task_group="repair-round-1",
        breakdown_count=breakdown_count,
    )


def test_repair_split_stops_at_the_depth_cap():
    """A repair task that has already been split twice is not split again.
    Before this, nothing bounded the depth: ``EXECUTOR_BREAKDOWN_MAX`` lost
    its only enforcement site with ``_breakdown_task`` and the run-wide cap
    of 8 is shared across every failure and reset by every new round."""
    from agent import REPAIR_MAX_BREAKDOWN_DEPTH

    assert REPAIR_MAX_BREAKDOWN_DEPTH == 2
    stub = _agent_stub()

    events = _refine_with(
        stub, _repair_task(breakdown_count=REPAIR_MAX_BREAKDOWN_DEPTH),
    )

    assert "repair_breakdown_budget_exhausted" in events
    stub.refiner.refine.assert_not_called()
    assert stub._refinement_count == 0, (
        "an exhausted breakdown budget must not spend the run-wide "
        "refinement budget either"
    )


def test_repair_split_still_allowed_below_the_cap():
    """Negative control: the budget is a depth, not a blanket refusal."""
    from agent import REPAIR_MAX_BREAKDOWN_DEPTH

    stub = _agent_stub()
    task = _repair_task(breakdown_count=REPAIR_MAX_BREAKDOWN_DEPTH - 1)

    events = _refine_with(stub, task)

    assert "repair_breakdown_budget_exhausted" not in events
    stub.refiner.refine.assert_called_once()


def test_non_repair_task_is_unaffected_by_the_repair_budget():
    """The cap is scoped to repair tasks on purpose — widening it to the
    whole plan would change behaviour this change is not about."""
    from task import SubTask

    stub = _agent_stub()
    task = SubTask(
        id="12", title="ship it", description="d",
        test_command="pytest -q", breakdown_count=9,
    )

    events = _refine_with(stub, task)

    assert "repair_breakdown_budget_exhausted" not in events
    stub.refiner.refine.assert_called_once()


def test_split_writes_the_depth_to_both_stores(tmp_path):
    """Drive the real ``_apply_refiner_structure`` — the one place a
    breakdown actually happens — and check that ``breakdown_count`` lands
    in ``plan_tasks`` *and* in ``tasks.json``.

    Testing :func:`_stamp_breakdown_depths` alone proves nothing about the
    wiring, which is how a whole accounting feature stays dead.
    """
    from unittest.mock import MagicMock

    from agent import AutonomousAgent
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    from task_manager import TaskManager

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    manager = TaskManager(project_dir)
    manager.set_tasks(
        [{
            "id": "repair-r1-01", "title": "fix VP-005",
            "description": "d", "test_command": "pytest -q",
            "depends_on": [], "task_group": "repair-round-1",
        }],
        requirement="req",
    )

    stub = MagicMock()
    stub.task_manager = manager
    stub.plan_id = PLAN_ID
    stub._TERMINAL_REPAIR_TASK_GROUP_PREFIX = "repair"
    stub._get_task_progress_repository = MagicMock(
        return_value=PlanTaskRepository(open_db(db_path)),
    )
    # A recording logger, not a MagicMock: the depth-stamp event is
    # emitted inside a ``try/except Exception: pass`` (a logger must
    # never kill a refinement), and a mock would swallow a broken
    # payload silently — which is how a ``dict`` slice reached this
    # test suite in the first place and made the event a no-op.
    events: list = []
    stub.logger = MagicMock()
    stub.logger.info.side_effect = (
        lambda name, msg, **kw: events.append((name, kw))
    )

    current = [t.model_dump() for t in manager.tasks]
    # What the refiner answers: the parent split into two children.
    refined = [
        {"id": "repair-r1-01-1", "title": "a", "description": "d",
         "test_command": "pytest -q", "depends_on": [],
         "task_group": "repair-round-1"},
        {"id": "repair-r1-01-2", "title": "b", "description": "d",
         "test_command": "pytest -q", "depends_on": [],
         "task_group": "repair-round-1"},
    ]

    class _FailedTask:
        id = "repair-r1-01"

    assert AutonomousAgent._apply_refiner_structure(
        stub, current, refined, _FailedTask(),
    )

    conn = open_db(db_path)
    try:
        depths = dict(conn.execute(
            "SELECT task_id, breakdown_count FROM plan_tasks "
            "WHERE plan_id = ? ORDER BY task_id",
            (PLAN_ID,),
        ).fetchall())
    finally:
        conn.close()
    assert depths["repair-r1-01-1"] == 1
    assert depths["repair-r1-01-2"] == 1

    # And the event that says so actually fired, with the real children
    # in it. A broken payload would raise inside the ``except: pass``
    # and leave ``events`` empty while the DB asserts stayed green.
    stamped = [kw for name, kw in events if name == "refine_breakdown_depth_stamped"]
    assert stamped, f"depth-stamp event never fired; events={[n for n, _ in events]}"
    assert stamped[0]["data"]["children"] == {
        "repair-r1-01-1": 1, "repair-r1-01-2": 1,
    }

    # The parent row is gone — this is the 2026-10-05 shape where the
    # breakdown deletes it, as opposed to the live plans where it lingers
    # at ``failed``.
    assert "repair-r1-01" not in depths

    # ``save_tasks`` strips ``breakdown_count`` on purpose (same as
    # ``status`` / ``failure_reason`` — runtime state belongs in SQLite),
    # so the row above is the ONLY copy. Asserted explicitly because it is
    # the reason the reload hydration exists.
    import json

    on_disk = json.loads(manager.tasks_file.read_text(encoding="utf-8"))
    by_id = {t["id"]: t for t in on_disk["tasks"]}
    assert "breakdown_count" not in by_id["repair-r1-01-1"]


def _loaded_tasks(tmp_path, *, status, breakdown_count):
    """Drive the real ``_load_tasks`` over a tasks.json + state.db pair."""
    from unittest.mock import MagicMock

    from agent import AutonomousAgent
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    from task_manager import TaskManager
    from task_repository import TaskRepository

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    project_dir = tmp_path / PLAN_ID
    project_dir.mkdir()
    manager = TaskManager(project_dir)
    manager.set_tasks(
        [{
            "id": "repair-r1-01-1", "title": "a", "description": "d",
            "test_command": "pytest -q", "depends_on": [],
            "task_group": "repair-round-1",
            "files_to_modify": ["__NO_FILE_CHANGES__"],
        }],
        requirement="req",
    )

    repo = PlanTaskRepository(open_db(db_path))
    repo.add_task(PLAN_ID, {
        "id": "repair-r1-01-1", "title": "a", "description": "d",
        "task_group": "repair-round-1",
    })
    repo.update_task(
        PLAN_ID, "repair-r1-01-1",
        {"status": status, "breakdown_count": breakdown_count},
    )

    stub = MagicMock()
    stub.task_manager = manager
    stub.plan_id = PLAN_ID
    stub.logger = None
    stub._session_task_completed_counts = {}
    stub._get_task_progress_repository = MagicMock(return_value=repo)
    stub.task_repository = TaskRepository(project_dir / "tasks.json")

    loaded = AutonomousAgent._load_tasks(stub)
    return {t.id: t.breakdown_count for t in loaded}


def test_breakdown_depth_survives_a_reload(tmp_path):
    """``save_tasks`` strips ``breakdown_count`` from ``tasks.json``, so
    ``plan_tasks`` is the only copy that outlives the run. Without the
    reload hydration a breakdown child comes back at depth 0 in the next
    executor process and ``REPAIR_MAX_BREAKDOWN_DEPTH`` resets every
    round — the budget would look enforced and bound nothing."""
    assert _loaded_tasks(tmp_path, status="pending", breakdown_count=2) == {
        "repair-r1-01-1": 2,
    }


def test_absent_depth_stays_at_the_model_default(tmp_path):
    """A row with no depth keeps SubTask's default of 0 — the hydrate must
    not invent a number."""
    assert _loaded_tasks(tmp_path, status="pending", breakdown_count=None) == {
        "repair-r1-01-1": 0,
    }
