"""A base-drift-blocked round leaves its trace before it hands the plan off.

Background
----------
A round the base-drift check blocked by a conflict dispatched **nothing**:
the branch had drifted away from its base, merging the base back left
conflicts, and nobody ever looked at the product.  Such a round has no
verdict — it did not happen.

The loop has two hand-off points for such a round — the repair callback
(``_make_repair_on_complete``) and the auto-loop's own round — and both
of them used to dispatch the base merge and return without writing
**anything**.  The round row said nothing, so the next round had no
evidence that the previous one was blocked and the round counter was
bumped for a round that never ran.

What has to be written BEFORE the hand-off:

  * the round row (``plan_verification``) — the stop reason
    ``base_drift_conflicts`` and an envelope naming the repair task the
    conflict became.  The row keeps its status and its number: the
    round is *blocked*, not finished, and it is re-run under the SAME
    number;
  * one audit event in the plan's own ``execution.log``.

TDD contract — 11 test cases
----------------------------
1. ``test_block_is_written_before_handoff`` — the row carries the stop
   reason and the envelope, and keeps its status and its round number.
2. ``test_event_is_written_to_execution_log`` — ``execution.log`` carries
   the ``base_drift_round_blocked`` event and the conflict's evidence.
3. ``test_write_failure_does_not_raise`` — a database that cannot be
   opened is a warning, not a crash: the hand-off must go on.
4. ``test_write_failure_still_writes_the_event`` — the two sinks are
   independent: a row that cannot be written does not silence the
   event.
5. ``test_missing_plan_directory_skips_the_event_only`` — a plan with no
   directory gets no event, but its row is written all the same.
6. ``test_repair_task_id_travels_with_the_record`` — the task id travels
   with the envelope.
7. ``test_blocked_row_is_read_by_the_round_planner`` — the row is what
   the round planner reads, so the blocked round keeps its number.
8. ``test_both_call_sites_call_it`` — both hand-off points record the
   block, and they record it BEFORE they dispatch.
9. ``test_stop_branch_is_untouched`` — the branch with no task to repair
   with records nothing here and still stops the round with
   ``loop_stopped``.
10. ``test_recorder_is_only_called_from_a_repair_branch`` — no third
    caller writes a blocked-round record.
11. ``test_stop_report_is_still_a_stop`` — the recorder does not change
    the branch's own decision.

Boundary conditions
-------------------
* A write that fails never raises: the audit trail is not worth a
  stranded plan.
* A plan whose directory is not there gets no event — but its row is
  still written.
* The ``stop`` branch (no task id) is untouched: it keeps recording
  ``loop_stopped`` through ``_record_terminal`` and does NOT write a
  blocked-round record.
* Nothing here is charged to ``max_rounds``, and
  ``_VerificationLoopOutcome`` keeps its meaning.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# ``backend/`` on sys.path so ``import server`` works no matter which
# entry point runs the suite, and the repository root so the
# ``backend.framework`` package resolves.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
for _path in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import server  # noqa: E402,F401  — imported first: server wires the loop
import verification_loop  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.verification_repository import (  # noqa: E402
    VerificationRepository,
)


PLAN_ID = "20261113-base-drift"

#: The stop reason a base-drift-blocked round carries.
STOP_REASON = verification_loop.BASE_DRIFT_CONFLICTS_STOP_REASON

#: The repair task the conflict became.
REPAIR_TASK_ID = "__base_drift__"

#: The conflict's own evidence, as the loop hands it over.
BLOCKED = {
    "status": STOP_REASON,
    "base_ref": "origin/main",
    "conflict_files": [{"path": "backend/a.py"}],
}


# ---------------------------------------------------------------------------
# Fixtures — a throwaway state.db and a throwaway plans tree
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """A throwaway ``state.db`` with one running round for ``PLAN_ID``."""
    path = tmp_path / "state.db"
    conn = open_db(path)
    try:
        migrate(conn)
        VerificationRepository(conn).insert(
            PLAN_ID, "running", round=1, max_rounds=3,
        )
    finally:
        conn.close()
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **kw: path)
    return path


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    """A throwaway plans tree holding the plan's own directory."""
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "PLANS_DIR", root)
    # The execution logger resolves the plans root when it is built, so
    # the environment has to agree with the patched attribute.
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))
    (root / PLAN_ID).mkdir(parents=True, exist_ok=True)
    return root


def _row(state_db):
    """The plan's verification row, read back through the repository."""
    conn = open_db(state_db)
    try:
        return VerificationRepository(conn).current(PLAN_ID)
    finally:
        conn.close()


def _record(**over):
    """Call the recorder with the standard arguments.

    The fixtures (``state_db`` / ``plans_dir``) are what the recorder
    writes into, so a case that calls this must have asked for them.
    """
    kwargs = {
        "repair_task_id": REPAIR_TASK_ID,
    }
    kwargs.update(over)
    return verification_loop._record_base_drift_round_block(
        PLAN_ID, "round-token-1", BLOCKED, **kwargs,
    )


# ---------------------------------------------------------------------------
# Test 1 — the round row carries the block
# ---------------------------------------------------------------------------


def test_block_is_written_before_handoff(state_db, plans_dir):
    """The row says why the round stopped, and says the round is blocked.

    The stop reason is the base-drift check's own event name, so the row
    and the event log use the same word.  The round's status and its
    number do NOT move: the round is blocked, not finished, and it is
    re-run under the same number.
    """
    _record()

    row = _row(state_db)
    assert row["verification_stop_reason"] == STOP_REASON
    assert row["results"]["status"] == "base_drift_blocked"
    assert row["results"]["stop_reason"] == STOP_REASON
    assert row["results"]["recorded_by"] == "_base_drift_round_block"
    # A blocked round is not a finished round.
    assert row["verification_status"] == "running"
    assert row["round"] == 1


# ---------------------------------------------------------------------------
# Test 2 — the audit event
# ---------------------------------------------------------------------------


def test_event_is_written_to_execution_log(state_db, plans_dir):
    """The plan's own execution log carries the event and the evidence.

    The operator reads ``execution.log``; the conflict's own evidence
    (which base was merged, which files are in conflict) travels with
    the event so the block can be understood without opening a
    database.
    """
    _record()

    log = (plans_dir / PLAN_ID / "execution.log").read_text(encoding="utf-8")
    assert "base_drift_round_blocked" in log
    assert "backend/a.py" in log


# ---------------------------------------------------------------------------
# Test 3 — a failed write never takes the hand-off down
# ---------------------------------------------------------------------------


def test_write_failure_does_not_raise(state_db, plans_dir, monkeypatch):
    """A database that cannot be opened is a warning, not a crash.

    The merge is what unblocks the plan.  A plan whose audit trail has
    a hole is a smaller problem than a plan whose merge never ran, so
    the recorder degrades to a warning and returns.
    """

    def boom(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr("state_machine.db.connection.open", boom)

    _record()  # must not raise


def test_write_failure_still_writes_the_event(state_db, plans_dir, monkeypatch):
    """The event does not depend on the row: one sink failing is not two.

    The audit trail has two destinations and they are independent — the
    row is what a later round reads, the event is what an operator
    reads.  A row that cannot be written must not silence the event.
    """

    def boom(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr("state_machine.db.connection.open", boom)

    _record()

    log = (plans_dir / PLAN_ID / "execution.log").read_text(encoding="utf-8")
    assert "base_drift_round_blocked" in log


def test_missing_plan_directory_skips_the_event_only(state_db, tmp_path,
                                                      monkeypatch):
    """A plan whose directory is not there gets no event — but a row.

    The event lands in the plan's own ``execution.log``; a plan with no
    directory has no log to write to, and creating one would invent a
    plan.  The row, though, lives in the database and is written all
    the same.
    """
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "PLANS_DIR", root)
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))

    _record()

    assert not (root / PLAN_ID / "execution.log").exists()
    assert _row(state_db)["verification_stop_reason"] == STOP_REASON


# ---------------------------------------------------------------------------
# Test 4 — the repair task travels with the record
# ---------------------------------------------------------------------------


def test_repair_task_id_travels_with_the_record(state_db, plans_dir):
    """The envelope names the task the conflict became.

    The id is what tells a later reader WHICH repair the block belongs
    to — the merge that is about to run under the executor's own scene.
    """
    _record()

    assert _row(state_db)["results"]["base_drift_repair_task_id"] == (
        REPAIR_TASK_ID
    )


# ---------------------------------------------------------------------------
# Test 5 — the row is what the round planner reads
# ---------------------------------------------------------------------------


def test_blocked_row_is_read_by_the_round_planner(state_db, plans_dir):
    """The row this writes is the evidence the round planner reads.

    A blocked round is re-run under the SAME number: it dispatched
    nothing, so it spent no budget.  The planner recognises the block
    from the row — a row without the reason would bump the counter for
    a round that never ran.
    """
    _record()

    row = _row(state_db)
    assert verification_loop._round_was_blocked_by_base_drift(row) is True

    next_round, max_rounds, exhausted = verification_loop._plan_next_round(row)
    assert next_round == 1, (
        "a blocked round is re-run under the same number, not a new one"
    )
    assert max_rounds == 3, "the plan's budget is untouched"
    assert exhausted is False


# ---------------------------------------------------------------------------
# Test 6 — both hand-off points record the block
# ---------------------------------------------------------------------------


def _iter_production_modules():
    """Yield every production ``.py`` module under ``backend/``."""
    for path in sorted(_BACKEND_DIR.rglob("*.py")):
        relative = path.relative_to(_BACKEND_DIR)
        if relative.parts[0] == "tests":
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        yield path


def _is_repair_test(node) -> bool:
    """Is this the test ``_base_drift_round_action(...) == "repair"``?"""
    if not isinstance(node, ast.Compare):
        return False
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
        return False
    if len(node.comparators) != 1:
        return False
    comparator = node.comparators[0]
    if not isinstance(comparator, ast.Constant) or comparator.value != "repair":
        return False
    left = node.left
    return (
        isinstance(left, ast.Call)
        and isinstance(left.func, ast.Name)
        and left.func.id == "_base_drift_round_action"
    )


def _repair_branches():
    """Every ``if _base_drift_round_action(...) == "repair"`` in production.

    Returns a list of ``(path, ast.If)`` pairs.  The whole production
    tree is scanned: a third hand-off point somewhere else would skip
    the record just as silently as one here.
    """
    found = []
    for path in _iter_production_modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and _is_repair_test(node.test):
                found.append((path, node))
    return found


def _calls(node, name):
    """The calls of ``name`` inside ``node`` (a plain name or an attribute)."""
    found = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name) and func.id == name:
            found.append(child)
        elif isinstance(func, ast.Attribute) and func.attr == name:
            found.append(child)
    return found


def test_both_call_sites_call_it():
    """Both hand-off points record the block BEFORE they dispatch.

    The record has to be on the row when the merge runs — the repair
    that follows re-enters verification, and the round that comes back
    has to find the evidence that this round was blocked.
    """
    branches = _repair_branches()

    assert len(branches) == 2, (
        f"expected exactly 2 base-drift repair branches, found "
        f"{len(branches)}: {[str(p) for p, _ in branches]}"
    )

    for path, branch in branches:
        records = _calls(branch, "_record_base_drift_round_block")
        dispatches = _calls(branch, "_run_repair_execution_async")

        assert len(records) == 1, (
            f"{path}:{branch.lineno} must record the blocked round exactly "
            f"once, found {len(records)} calls"
        )
        assert len(dispatches) == 1, (
            f"{path}:{branch.lineno} must dispatch the executor exactly once, "
            f"found {len(dispatches)} calls"
        )
        assert records[0].lineno < dispatches[0].lineno, (
            f"{path}:{branch.lineno} must record the blocked round BEFORE "
            f"it hands the plan to the executor"
        )


# ---------------------------------------------------------------------------
# Test 7 — the stop branch is untouched
# ---------------------------------------------------------------------------


def _statement_lists(tree):
    """Yield every statement list of the tree."""
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            value = getattr(node, field, None)
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                yield value


def _stop_paths():
    """The statements that follow every repair branch — the stop path.

    The repair branch is an ``if`` without an ``else``: the code that
    follows it, in the same block, is what runs when the conflict
    became no task.
    """
    paths = []
    for path in _iter_production_modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        repair_ifs = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.If) and _is_repair_test(node.test)
        ]
        if not repair_ifs:
            continue
        for statements in _statement_lists(tree):
            for index, statement in enumerate(statements):
                if statement in repair_ifs:
                    paths.append((path, statements[index + 1:]))
    return paths


def _records_loop_stopped(statements) -> bool:
    """Does this code stop the round with ``loop_stopped``?"""
    for statement in statements:
        for call in ast.walk(statement):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not (isinstance(func, ast.Name) and func.id == "_record_terminal"):
                continue
            if len(call.args) < 2:
                continue
            status, reason = call.args[0], call.args[1]
            if not (isinstance(status, ast.Constant) and status.value == "loop_stopped"):
                continue
            if isinstance(reason, ast.Name) and reason.id == (
                "BASE_DRIFT_CONFLICTS_STOP_REASON"
            ):
                return True
    return False


def test_stop_branch_is_untouched():
    """The branch with no task to repair with records nothing here.

    A conflict that became no task ends the round with
    ``loop_stopped`` — the existing path, unchanged.  It must NOT write
    a blocked-round record: that record says "this round is blocked and
    will be re-run under the same number", and a round that stopped is
    not re-run under the same number.
    """
    paths = _stop_paths()

    assert paths, "the stop path of the base-drift branch was not found"

    for path, statements in paths:
        assert not _calls_statements(statements, "_record_base_drift_round_block"), (
            f"{path}: the stop path must not record a blocked round"
        )
        assert _records_loop_stopped(statements), (
            f"{path}: the stop path must still stop the round with "
            f"loop_stopped"
        )


def _calls_statements(statements, name):
    """The calls of ``name`` inside a list of statements."""
    found = []
    for statement in statements:
        found.extend(_calls(statement, name))
    return found


def test_recorder_is_only_called_from_a_repair_branch():
    """No third caller writes a blocked-round record.

    Every call to the recorder in the production tree sits inside a
    repair branch.  A call anywhere else — the stop path, an unrelated
    round — would write a row that says "re-run this round" for a round
    that is not going to be re-run.
    """
    inside = 0
    for path, branch in _repair_branches():
        inside += len(_calls(branch, "_record_base_drift_round_block"))

    everywhere = 0
    for path in _iter_production_modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        everywhere += len(_calls(tree, "_record_base_drift_round_block"))

    assert everywhere == inside, (
        "the blocked-round recorder is called from somewhere other than a "
        f"base-drift repair branch ({everywhere} calls in the tree, "
        f"{inside} inside a repair branch)"
    )


def test_stop_report_is_still_a_stop():
    """A conflict with no task id is still a stop.

    The recorder does not change the branch's own decision: the report
    that carries no task id maps to ``"stop"``, exactly as before.
    """
    report = {
        "stop_reason": STOP_REASON,
        "base_drift_conflicts": {"status": "conflicts"},
    }

    assert verification_loop._base_drift_round_action(report) == "stop"
