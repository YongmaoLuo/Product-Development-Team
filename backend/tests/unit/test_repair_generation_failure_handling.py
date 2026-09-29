"""Part 3 regression (2026-09-14): a FAILED repair-task generation must
never masquerade as "no repair tasks needed".

Background — round 1:

1. Several VPs failed.
2. Phase-3 report generation died on all three attempts (the first
   reply was unparseable, attempts 2-3 raised a bogus
   ``HardTimeoutError``), so the fallback report was returned — and,
   before Bug B's fix, never written to disk.
3. ``check_cycle_conditions`` consequently handed the repair generator a
   stale failed-VP list, and the generator's own LLM call raised the
   same bogus ``HardTimeoutError``.
4. ``generate_repair_contents`` **swallowed that exception into
   ``[]``**. The auto-loop reads ``[]`` as ``no_repair_tasks`` — i.e.
   "the chain has converged" — and terminalised a plan with three live
   failures and zero repair work.

The two outcomes are not the same thing and must not be collapsed:

  * ``[]`` — nothing to repair (no failed VPs / no new tasks needed).
    Stopping is correct.
  * generation FAILED — the LLM call died or replied with nothing
    usable *while failures were pending*. Stopping is a silent strand.

Contract pinned here, layer by layer:

  * ``RepairTaskGenerator.generate_repair_contents`` raises
    ``RepairGenerationError`` (carrying ``round_number`` /
    ``failed_vp_ids``) instead of returning ``[]`` on both failure
    shapes, and still returns ``[]`` for an empty ``failed_vps``.
  * ``VerificationOrchestrator.check_cycle_conditions`` catches it,
    keeps ``repair_tasks == []``, and surfaces
    ``repair_generation_error`` on the result payload — distinct from
    a successful-but-empty generation, which leaves the field ``None``.
  * ``server._run_auto_verification_loop`` checks that field *before*
    treating the empty list as convergence, and calls
    ``_repair_generation_failed`` (record the reason
    ``repair_generation_failed``, retry the round) rather than the
    chain-closing ``_dead_end_terminal``.

  * 2026-09-18 C3：那个入口现在是一层 try/finally 包装（工作流退出时回收
    计划声明的服务），循环本体是 ``_run_auto_verification_loop_inner``。
    下面锁的都是**本体**，所以 ``inspect.getsource`` 取的是 inner。

2026-09-17 — the parking behaviour changed. ``_repair_generation_failed``
used to roll the routing row back to ``ready`` and hand the plan to
the operator. It now records the failure and lets the auto-loop RETRY the
round ( "生成失败也是要重试的，给一个重试的机会，跟任何
的任务一样"), because a generation failure is a transient provider fault,
not a verdict about the plan.

The rollback had to go with it: ``ready`` is a stage
``POST /api/execution/{plan_id}/start`` will CAS to ``executing``, and
that endpoint has no verification-liveness guard, so leaving the row
there while the loop ran another verification round opened a race where
an operator restart could spawn an executor into a mid-flight
verification.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from repair_generator import (  # noqa: E402
    RepairGenerationError,
    RepairTaskGenerator,
)


# =============================================================================
# Layer 1 — the generator must raise, not swallow
# =============================================================================

def _failed_vp(vp_id: str) -> dict:
    return {
        "id": vp_id,
        "title": f"failure {vp_id}",
        "priority": "high",
        "actual_result": "assert 0 == 1",
        "evidence": "tests/test_x.py::test_y",
    }


class _ScriptedTool:
    """``coding_tool`` double: raise on demand, else return a fixed reply."""

    def __init__(self, exc: Exception | None = None, reply=None):
        self.calls = 0
        self._exc = exc
        self._reply = reply

    def query_json(self, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._reply


def _generator(tmp_path: Path, tool) -> RepairTaskGenerator:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return RepairTaskGenerator(tool, plan_dir, project_dir)


def test_empty_failed_vps_still_returns_empty_without_calling_llm(tmp_path):
    """No failures to repair is the ONE case where ``[]`` is the right
    answer — and it must not cost an LLM call (or raise)."""
    tool = _ScriptedTool()
    gen = _generator(tmp_path, tool)

    assert gen.generate_repair_contents([], round_number=1) == []
    assert tool.calls == 0, "an empty failure list must short-circuit"


def test_llm_exception_raises_repair_generation_error(tmp_path):
    """The exact the observed shape: the LLM call raises (here a
    ``HardTimeoutError``-alike) while failures are pending. Swallowing
    this into ``[]`` is what terminalised the plan."""
    tool = _ScriptedTool(exc=TimeoutError("Hard timeout after 600s"))
    gen = _generator(tmp_path, tool)

    with pytest.raises(RepairGenerationError) as excinfo:
        gen.generate_repair_contents(
            [_failed_vp("VP-021"), _failed_vp("VP-034")], round_number=1
        )

    err = excinfo.value
    assert err.round_number == 1
    assert err.failed_vp_ids == ["VP-021", "VP-034"]
    assert "Hard timeout after 600s" in str(err)
    # The original exception must stay reachable for post-mortems.
    assert isinstance(err.__cause__, TimeoutError)


def test_unusable_reply_raises_repair_generation_error(tmp_path):
    """The model answered, but with nothing the assembler can turn into
    a task. With failures pending that is a generation failure too —
    ``{"tasks": []}`` must not read as convergence."""
    tool = _ScriptedTool(reply={"tasks": []})
    gen = _generator(tmp_path, tool)

    with pytest.raises(RepairGenerationError) as excinfo:
        gen.generate_repair_contents([_failed_vp("VP-036")], round_number=2)

    err = excinfo.value
    assert err.round_number == 2
    assert err.failed_vp_ids == ["VP-036"]
    assert "no usable task" in str(err)


def test_half_formed_reply_also_raises(tmp_path):
    """A task item missing ``description`` is dropped by the parser —
    leaving an empty content list with a failure pending."""
    tool = _ScriptedTool(reply={"tasks": [{"failed_vp_id": "VP-021",
                                          "title": "only a title"}]})
    gen = _generator(tmp_path, tool)

    with pytest.raises(RepairGenerationError):
        gen.generate_repair_contents([_failed_vp("VP-021")], round_number=1)


def test_usable_reply_is_returned_unchanged(tmp_path):
    """Control: a well-formed reply still produces content — the raise
    path must not have swallowed the happy path."""
    tool = _ScriptedTool(reply={"tasks": [{
        "failed_vp_id": "VP-021",
        "title": "fix it",
        "description": "do the thing",
        "acceptance_criteria": "test_x passes",
    }]})
    gen = _generator(tmp_path, tool)

    contents = gen.generate_repair_contents([_failed_vp("VP-021")], round_number=1)
    assert [c["failed_vp_id"] for c in contents] == ["VP-021"]
    assert contents[0]["title"] == "fix it"


# =============================================================================
# Layer 2 — the orchestrator must surface the failure, not hide it
# =============================================================================

def _write_plan_state(plan_dir: Path, phase: str = "executing",
                      verification_round: int = 0, max_rounds: int = 3) -> None:
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": None,
        },
    }
    (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """Hermetic state.db — PlanState persists through plan_routing now."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _failed_vp_reader_patch(failed_vps):
    """Patch whichever failed-VP reader the orchestrator is wired to.

    Returns ``(stack, mock)``. The tree is mid-refactor between the
    inlining reader (``extract_failed_vps_from_report``) and its
    path-based sibling (``extract_failed_vps_with_paths``). Patching
    both — ``create=True`` covers the one that may not exist yet — keeps
    this test pinned to ``check_cycle_conditions``' behaviour rather
    than to which import line happens to be current.
    """
    import contextlib

    stack = contextlib.ExitStack()
    mock = Mock()
    mock.return_value = failed_vps
    for name in (
        "extract_failed_vps_with_paths",
        "extract_failed_vps_from_report",
    ):
        stack.enter_context(
            patch(
                f"verification.verification_report_reader.{name}",
                mock,
                create=True,
            )
        )
    return stack, mock


def _cycle_result(tmp_path: Path, gen_side_effect) -> dict:
    """Run ``check_cycle_conditions`` once on a scripted failure round."""
    from verification import VerificationOrchestrator

    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write_plan_state(plan_dir)

    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {"id": "VP-021", "status": "FAILED", "title": "server.py 规模"},
        ],
        "requirement_deviations": [],
    }

    reader_stack, _ = _failed_vp_reader_patch([
        {"id": "VP-021", "title": "server.py 规模", "priority": "high"},
    ])
    with reader_stack, \
         patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"), \
         patch("repair_generator.RepairTaskAssembler") as MockAssembler:
        MockAssembler.return_value.assemble.return_value = []

        orch = VerificationOrchestrator(plan_dir, project_dir, object())
        orch.verification_agent.run_full_verification.return_value = report
        orch.repair_generator.generate_repair_contents.side_effect = (
            gen_side_effect
        )

        orch.start_verification_cycle(round_number=1)
        return orch.check_cycle_conditions(report, round_number=1)


def test_check_cycle_conditions_surfaces_generation_error(tmp_path, state_db):
    """A raising generator must produce an empty ``repair_tasks`` AND a
    populated ``repair_generation_error`` — the auto-loop keys off the
    latter to tell "broke" from "converged"."""
    result = _cycle_result(
        tmp_path,
        RepairGenerationError(
            "repair-content LLM call failed for round 1: Hard timeout",
            round_number=1,
            failed_vp_ids=["VP-021"],
        ),
    )

    assert result["repair_tasks"] == []
    assert result["repair_generation_error"], (
        "check_cycle_conditions swallowed RepairGenerationError — the "
        "auto-loop would read repair_tasks=[] as no_repair_tasks and "
        "terminalise the plan"
    )
    assert "Hard timeout" in result["repair_generation_error"]
    assert result["status"] == "verification_failed"


def test_check_cycle_conditions_marks_successful_empty_as_convergence(
    tmp_path, state_db
):
    """Control: a generator that *succeeds* with nothing to report leaves
    ``repair_generation_error`` at ``None``. That is the only shape
    entitled to the terminal ``no_repair_tasks`` verdict."""
    result = _cycle_result(tmp_path, lambda *a, **k: [])

    assert result["repair_tasks"] == []
    assert result["repair_generation_error"] is None


def test_check_cycle_conditions_keeps_repair_tasks_and_no_error(tmp_path, state_db):
    """Control: the happy path is untouched."""
    result = _cycle_result(
        tmp_path, lambda *a, **k: [{"failed_vp_id": "VP-021", "title": "fix"}]
    )

    assert result["repair_generation_error"] is None
    assert result["status"] == "verification_failed"


# =============================================================================
# Layer 3 — the server must park the plan, not close the chain
# =============================================================================

def _seed_plan(tmp_path, monkeypatch, *, stage: str, pending_tasks: int):
    """Seed a hermetic state.db with a plan mid-verification.

    Returns ``(server, plan_id, db_path)``.
    """
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    conn = open_db(db_path)
    migrate(conn)
    plan_id = "20260101-repair-gen-failed"

    RoutingRepository(conn).insert(plan_id, stage)
    VerificationRepository(conn).insert(plan_id, verification_status="failed")
    for idx in range(pending_tasks):
        PlanTaskRepository(conn).add_task(
            plan_id,
            {"id": f"1-{idx + 1}", "title": f"task {idx + 1}"},
        )
    conn.close()
    return server, plan_id, db_path


def _routing_stage(db_path: Path, plan_id: str) -> str | None:
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(db_path)
    try:
        row = RoutingRepository(conn).find(plan_id)
        return row["current_phase"] if row else None
    finally:
        conn.close()


def _verification_row(db_path: Path, plan_id: str) -> dict:
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db_path)
    try:
        return VerificationRepository(conn).current(plan_id) or {}
    finally:
        conn.close()


def _recorded_stop_reason(row: dict) -> str | None:
    """The reason as operators actually read it.

    ``_persist_verification_terminal`` records it in the ``results``
    envelope (``{"status", "stop_reason", "recorded_by"}``); the
    ``verification_stop_reason`` column is left NULL on this path — an
    earlier plan's row has it NULL too, and
    ``_reconcile_orphaned_verification_stages`` resolves the reason in
    Python for exactly that reason. Asserting on the column would pin
    the wrong shape.
    """
    results = row.get("results") or {}
    if isinstance(results, str):
        results = json.loads(results)
    return results.get("stop_reason")


def test_repair_generation_failed_records_but_does_not_move_the_stage(
    tmp_path, monkeypatch
):
    """The dead-end shape.

    The failure must be RECORDED (operators grep for the reason) and the
    chain must stay open — but the routing row must stay where the plan
    actually is. Rolling it to ``ready`` would let
    ``POST /api/execution/{plan_id}/start`` spawn an executor while the
    auto-loop is still running verification rounds.
    """
    stage = "verification_repairing"
    server, plan_id, db_path = _seed_plan(
        tmp_path, monkeypatch, stage=stage, pending_tasks=3
    )

    server._repair_generation_failed(plan_id, "repair-content LLM call failed")

    assert _routing_stage(db_path, plan_id) == stage, (
        f"the stage moved off {stage} — the auto-loop is about to run "
        f"another verification round, and ready is a stage "
        f"/execution/start will CAS to executing with no liveness guard"
    )
    row = _verification_row(db_path, plan_id)
    assert row.get("verification_status") == "failed"
    assert _recorded_stop_reason(row) == "repair_generation_failed", (
        "the stop reason must name the real cause so operators can grep "
        "for it instead of guessing at 'no_repair_tasks'"
    )


def test_repair_generation_failed_is_a_noop_on_the_stage_when_running(
    tmp_path, monkeypatch
):
    """Same contract when the plan's stage is ``verification_running``
    (the auto-loop's normal stage at that point)."""
    stage = "verification_running"
    server, plan_id, db_path = _seed_plan(
        tmp_path, monkeypatch, stage=stage, pending_tasks=3
    )

    server._repair_generation_failed(plan_id, "LLM returned no usable task")

    assert _routing_stage(db_path, plan_id) == stage
    assert _recorded_stop_reason(_verification_row(db_path, plan_id)) \
        == "repair_generation_failed"


def test_repair_generation_failed_never_raises_on_missing_db(
    tmp_path, monkeypatch
):
    """Best-effort parking: a broken/absent state.db must degrade to
    logged errors, not propagate into the verification thread."""
    import server

    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "nope" / "x.db"))
    server._repair_generation_failed("no-such-plan", "boom")  # must not raise


def test_repair_generation_failed_reason_is_not_chain_ending():
    """``_repair_generation_failed`` records the failure on
    ``plan_verification`` for visibility, but its reason is deliberately
    absent from ``VERIFICATION_TERMINAL_STOP_REASONS`` so the routing
    stage is advanced only by the explicit ``ready`` rollback —
    never by the terminal helper."""
    import server

    assert "repair_generation_failed" not in server.VERIFICATION_TERMINAL_STOP_REASONS


def test_auto_loop_distinguishes_failure_from_convergence():
    """Source-level pin on the auto-loop branch: the empty-repair-tasks
    path must consult ``repair_generation_error`` BEFORE falling through
    to ``_dead_end_terminal``.

    The loop body itself needs a full executor/orchestrator harness to
    drive; wiring it end-to-end here would test more fixture than
    production. This asserts the ordering that makes the difference —
    a regression that removes the check would put the dead-end call
    first and reopen the stranding.
    """
    import inspect

    import server

    src = inspect.getsource(server._run_auto_verification_loop_inner)
    # 2026-09-14: the guard gained ``and not _pure_split`` — a round whose
    # only work is a VP split also has an empty repair list, but it must
    # NOT reach the dead-end (the split children still need a round).
    # The ordering this test protects is unchanged.
    branch = src.index("if not repair_tasks and not _pure_split:")
    check = src.index('result.get("repair_generation_error")', branch)
    dead_end = src.index("_dead_end_terminal(", branch)
    park = src.index("_repair_generation_failed(plan_id, _gen_error)", branch)

    assert check < park < dead_end, (
        "the auto-loop must test repair_generation_error, record the "
        "failure via _repair_generation_failed, and only then reach the "
        "chain-closing _dead_end_terminal"
    )


def _statement_after_call(src: str, marker: str) -> str:
    """Return the first real statement after the call starting at ``marker``.

    Scans past the call's own arguments (balanced parens) and past any
    intervening comment/blank lines, so the assertion targets the
    CONTROL FLOW rather than a character window that a longer comment
    can silently push the target out of.
    """
    start = src.index(marker)
    depth = 0
    i = start
    while True:
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    for line in src[i:].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped
    return ""


def test_generation_failure_retries_instead_of_parking():
    """2026-09-17 : "生成失败也是要重试的，给一个重试的
    机会，跟任何的任务一样".

    A generation failure is a transient provider fault, not a verdict
    about the plan, so the branch ``continue``s the round loop. Parking
    (a ``return`` after the bookkeeping call) is the regression.
    """
    import inspect

    import server

    src = inspect.getsource(server._run_auto_verification_loop_inner)
    nxt = _statement_after_call(
        src, "_repair_generation_failed(plan_id, _gen_error)",
    )
    assert nxt == "continue", (
        f"the statement after _repair_generation_failed is {nxt!r}, not "
        f"``continue`` — the auto-loop is parking again instead of retrying"
    )


def test_the_dead_end_routes_to_executing_not_to_a_park():
    """2026-09-17: a dead end that has already reached ``ready`` should
    move straight to ``executing`` — the executor dispatches whatever
    work exists, and with none left the chain flows on to
    ``verification``.

    So the dead end dispatches the executor and returns. Whether there is
    work is the executor's question; ``_on_repair_complete`` hands the
    chain back to verification either way. That is what makes the loop
    closed — ``executing`` always flows to ``verification``, and only the
    round budget or a repeated failure set can end the chain.
    """
    import inspect

    import server

    src = inspect.getsource(server._run_auto_verification_loop_inner)
    branch_start = src.index("if not repair_tasks and not _pure_split:")
    branch = src[branch_start:src.index("if _pending_db:", branch_start)]

    assert "confirm_repair_and_rerun()" in branch, (
        "the dead end does not route the plan to executing — the executor "
        "never gets a chance to drain remaining work"
    )
    assert "rollback_to_ready=False" in branch, (
        "the dead end still parks the routing row on ready while the "
        "round routes to executing — /execution/start CASes that stage with "
        "no verification-liveness guard"
    )
    assert "chain_ending=False" in branch
    assert _statement_after_call(branch, "_run_repair_execution_async(") == "return", (
        "the dead end must return after dispatching — the chain resumes "
        "from the executor's on_complete callback, not from `for round_num`"
    )


def test_the_loop_tail_reports_a_budget_stop_not_an_impossible_exit():
    """Exhausting the round range is now a normal outcome.

    The tail used to say it was unreachable and reported
    ``exited_loop_unexpectedly`` — which, once the dead-end branches
    started continuing, told operators to hunt for a control-flow bug
    that does not exist.
    """
    import inspect

    import server

    src = inspect.getsource(server._run_auto_verification_loop_inner)
    assert '_record_terminal("failed", "exited_loop_unexpectedly")' not in src, (
        "the tail still reports an impossible exit instead of the budget stop"
    )
    assert src.rstrip().endswith('_record_terminal("loop_stopped", "max_rounds_reached")')


def test_dead_end_does_not_park_on_ready_when_the_loop_continues(
    tmp_path, monkeypatch
):
    """The auto-loop's dead-end branch must NOT roll back to ``ready``.

    That branch now ``continue``s into another verification round. Leaving
    the routing row on ``ready`` while a round runs is a race:
    ``POST /api/execution/{plan_id}/start`` CASes ``ready`` →
    ``executing`` and has no verification-liveness guard, so an operator
    restart spawns an executor into a verification that is already live.

    The rollback itself is still correct for the OPERATOR-facing dead end
    (a parked, resumable plan) — hence the flag rather than a removal.
    """
    server, plan_id, db_path = _seed_plan(
        tmp_path, monkeypatch, stage="verification_running", pending_tasks=3
    )

    server._dead_end_terminal(
        plan_id, "no_repair_tasks",
        chain_ending=False, rollback_to_ready=False,
    )

    assert _routing_stage(db_path, plan_id) == "verification_running", (
        "the row moved off the verification family while the auto-loop is "
        "about to run another round"
    )
    # The verdict is still recorded for visibility.
    assert _recorded_stop_reason(_verification_row(db_path, plan_id)) == "no_repair_tasks"


def test_dead_end_still_parks_for_the_operator_by_default(tmp_path, monkeypatch):
    """Control: the operator-facing dead end keeps its resumable rollback."""
    server, plan_id, db_path = _seed_plan(
        tmp_path, monkeypatch, stage="verification_repairing", pending_tasks=3
    )

    server._dead_end_terminal(plan_id, "no_repair_tasks", chain_ending=False)

    assert _routing_stage(db_path, plan_id) == "ready"
