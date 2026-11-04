"""A round the binary-freshness preflight blocked must not cost a round.

Background
----------
The loop can now RECOGNISE a round the binary-freshness preflight
blocked (see ``verification_loop._binary_freshness_blocked_report``),
but recognition alone changes nothing: the loop still has TWO
judgement points where such a round can reach the judgement phase, and
the judgement phase reads "no verdict" as "everything failed".  It then
sends the repair chain after a product nobody ever looked at — and it
does so with the reason ``verification_log_stale``, which names a
symptom (the log went quiet) instead of the cause (the tree was not
fresh).

So both judgement points must treat the freshness-blocked round the way
they already treat a service-blocked one: the chain ends there, with an
**environment** stop reason, and the round is NOT charged to the plan's
round budget.  "Not charged" is not a second budget API: the terminal
write keeps the round the plan was in, so a restart RESUMES that round
instead of starting the next one.

TDD contract — 4 test cases
---------------------------
1. ``test_blocked_round_does_not_advance_the_counter`` — the terminal
   write of a blocked round leaves the round counter where it was.
2. ``test_restart_resumes_the_blocked_round`` — ``/start`` resumes the
   blocked round instead of starting the next one.
3. ``test_post_repair_reentry_does_not_judge_a_binary_blocked_round`` —
   the post-repair re-entry does not judge the blocked round, and the
   terminal verdict IS written down.
4. ``test_auto_loop_records_environment_stop_reason`` — the auto loop's
   branch records the environment stop reason and returns right after.

Boundary conditions
-------------------
* The stop reason is ``binary_freshness_preflight_failed`` — the same
  word the agent stops the round with.
* The status is ``loop_stopped``: the chain is over, but the round did
  not FAIL — it never ran.
* The round counter does not move.  ``complete_round`` writes the
  status and the stop reason, not the round, so the round the plan was
  in survives the terminal write.
* The reason is NOT registered in
  ``server.VERIFICATION_TERMINAL_STOP_REASONS``.  That set survives
  for compatibility only — the STATUS decides whether the chain ends,
  and ``loop_stopped`` is already in ``_TERMINAL_VERIFICATION_STATUSES``.
  The existing ``service_preflight_failed`` is not in that set either:
  that is the semantic baseline this change follows.
* The repair-callback branch records the terminal verdict too.  A bare
  ``return`` would lose the stop reason on that path.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# ``backend/`` on sys.path so ``import server`` works no matter which
# entry point runs the suite, and the repository root so the
# ``backend.framework`` package resolves.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
for _path in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import server  # noqa: E402,F401  — seeds sys.modules["server"] for the loop
import verification_loop as vl  # noqa: E402
from routes.verification import _resolve_start_round  # noqa: E402
from state_machine.repositories.verification_repository import (  # noqa: E402
    VerificationRepository,
)


#: The plan ids used by the cases below.  They are distinct per case
#: so one case cannot see another case's row.
COUNTER_PLAN_ID = "20261101-binary-freshness-counter"
RESUME_PLAN_ID = "20261101-binary-freshness-resume"
REENTRY_PLAN_ID = "20261101-binary-freshness-reentry"


def _blocked_report() -> dict:
    """The report a round the freshness preflight blocked comes back with."""
    return {
        "overall_status": "BLOCKED",
        "verification_results": [],
        "requirement_deviations": [],
        "summary": "binary freshness preflight failed: 3 stale .pyc files",
        "stop_reason": vl.BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND,
        "binary_freshness_preflight_failed": {
            "kind": "stale_pyc",
            "detail": "3 stale .pyc files",
            "stale_pyc_files": ["backend/a.pyc"],
        },
    }


def _state_db():
    """Open the suite's hermetic state DB with the schema applied.

    A FRESH connection per use: the terminal write under test opens its
    own connection, and a connection that still holds a read snapshot
    would read the row as it was before that write.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate as migrate_db

    conn = open_db(str(server._state_db_path()))
    migrate_db(conn)
    return conn


def _read_row(plan_id: str) -> dict:
    """The plan's ``plan_verification`` row, read on a fresh connection."""
    conn = _state_db()
    try:
        return VerificationRepository(conn).summary(plan_id) or {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. The blocked round does not cost the plan a round
# ---------------------------------------------------------------------------


def test_blocked_round_does_not_advance_the_counter():
    """A round blocked by the environment does not consume the budget.

    The plan was in round 2 of 3.  The round was blocked before it
    dispatched anything, so it measured nothing — and a round that
    measured nothing must not be counted as a round that ran.  The
    counter therefore stays at 2: the plan still has its third round
    ahead of it, and the blocked round is the one a restart resumes.
    """
    conn = _state_db()
    try:
        VerificationRepository(conn).init_round(COUNTER_PLAN_ID, 2, 3)
    finally:
        conn.close()

    vl._persist_verification_terminal(
        COUNTER_PLAN_ID,
        "loop_stopped",
        vl.BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND,
    )

    row = _read_row(COUNTER_PLAN_ID)

    assert row.get("round") == 2, (
        f"the blocked round was charged to the budget: {row!r}"
    )
    assert row.get("verification_status") == "loop_stopped", row
    assert row.get("verification_stop_reason") == (
        vl.BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND
    ), row


# ---------------------------------------------------------------------------
# 2. A restart resumes the blocked round
# ---------------------------------------------------------------------------


def test_restart_resumes_the_blocked_round():
    """``/start`` picks the blocked round back up — it does not skip it.

    The round counter did not move, and the row says ``loop_stopped``,
    so the round is an INTERRUPTED one: it is the round a restart must
    resume.  If the restart started round 3 instead, the blocked round's
    verdicts, report and repair tasks would be filed under a round that
    never happened.
    """
    conn = _state_db()
    try:
        VerificationRepository(conn).init_round(RESUME_PLAN_ID, 2, 3)
    finally:
        conn.close()

    vl._persist_verification_terminal(
        RESUME_PLAN_ID,
        "loop_stopped",
        vl.BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND,
    )

    row = _read_row(RESUME_PLAN_ID)

    assert _resolve_start_round(row, None) == (2, True), row


# ---------------------------------------------------------------------------
# 3. The post-repair re-entry does not judge a blocked round
# ---------------------------------------------------------------------------


def test_post_repair_reentry_does_not_judge_a_binary_blocked_round(
    tmp_path: Path, monkeypatch,
):
    """The re-entry ends at the blocked round — and says why.

    The post-repair re-entry is the same chain the auto loop runs, and
    it hands whatever the round returned to the judgement phase.  A
    round blocked by the freshness preflight carries no verdict, and
    the judgement phase reads "no verdict" as a failure: it would
    spawn repair tasks for a product nobody ever looked at.

    So the re-entry stops at the blocked round.  And it stops with the
    verdict written down: a bare ``return`` would leave the plan's
    ``stop_reason`` empty on this path, and the operator would see a
    plan that stopped without saying what stopped it.
    """
    from config_paths import resolve_plans_dir

    plan_dir = resolve_plans_dir() / REENTRY_PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    judged: list[str] = []

    class _Orch:
        def start_verification_cycle(self, **kwargs):
            return _blocked_report()

        def check_cycle_conditions(self, report, round_number=1):
            judged.append("judged")
            return {
                "status": "verification_failed",
                "repair_tasks": [{"id": "VP-999", "title": "not a real failure"}],
            }

    monkeypatch.setattr(
        vl._server, "VerificationOrchestrator", lambda *a, **k: _Orch(),
    )
    monkeypatch.setattr(vl._server, "create_coding_tool", lambda *a, **k: None)

    callback = vl._make_repair_on_complete(REENTRY_PLAN_ID, project_dir)
    callback(0)

    assert judged == [], (
        "the blocked round was judged: the judgement phase reads it as a "
        "failure and sends the repair chain after a product nobody ever "
        "looked at"
    )

    row = _read_row(REENTRY_PLAN_ID)
    assert row.get("verification_status") == "loop_stopped", row
    assert row.get("verification_stop_reason") == (
        vl.BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND
    ), (
        "the re-entry ended without writing the terminal verdict down — "
        "the plan stopped without saying what stopped it"
    )


# ---------------------------------------------------------------------------
# 4. The auto loop records the environment stop reason and returns
# ---------------------------------------------------------------------------


def _calls(node: ast.AST, name: str) -> bool:
    """Does this expression call the function called ``name``?"""
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == name
        for inner in ast.walk(node)
    )


def test_auto_loop_records_environment_stop_reason():
    """The auto loop's branch ends the chain with the environment reason.

    The branch has to do BOTH halves: record the terminal verdict (so
    the plan says what stopped it) and return (so the judgement phase
    never sees the blocked round).  A branch that records but falls
    through would judge the round; a branch that returns without
    recording would stop the chain without a reason.
    """
    source = Path(vl.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_run_auto_verification_loop_inner"
    )

    branches = [
        node for node in ast.walk(function)
        if isinstance(node, ast.If)
        and _calls(node.test, "_binary_freshness_blocked_report")
    ]

    assert len(branches) == 1, (
        "the auto loop must recognise the freshness-blocked round exactly "
        f"once — found {len(branches)}"
    )
    branch = branches[0]
    assert not branch.orelse, (
        "the blocked round must not fall through to the judgement phase"
    )
    assert len(branch.body) >= 2, branch.body

    last, before = branch.body[-1], branch.body[-2]

    assert isinstance(last, ast.Return), (
        "the blocked round's branch must end the chain with a return"
    )
    assert isinstance(before, ast.Expr) and isinstance(before.value, ast.Call), (
        "the branch must record the terminal verdict before it returns"
    )
    call = before.value
    assert isinstance(call.func, ast.Name), call.func
    assert call.func.id == "_record_terminal", call.func.id
    assert len(call.args) == 2, call.args
    status, reason = call.args
    assert isinstance(status, ast.Constant), status
    assert status.value == "loop_stopped", status.value
    assert isinstance(reason, ast.Name), reason
    assert reason.id == "BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND", reason.id
