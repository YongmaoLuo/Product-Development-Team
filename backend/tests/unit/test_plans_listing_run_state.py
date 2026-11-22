"""The plan listing must not report a runtime phase for a plan that never ran.

What this pins
--------------
``GET /api/plans`` answers two fields per plan:

* ``status`` — a heuristic read off the plan directory (``tasks.json``
  present means ``ready``), overridden to ``running`` when the execution
  store holds a live run (a status of ``running`` with a positive pid);
* ``current_phase`` — the authority, ``plan_routing.current_phase``.

Those two must agree, and for one phase they cannot be read
independently.  ``executing`` does not describe a plan's ARTEFACTS, it
describes a PROCESS: "the executor is running this plan right now".  So
the execution store can refute it, and the listing has to check it
against the store before repeating it.

The shape that made them disagree is a plan directory carrying a legacy
``plan_state.json``: ``plan_state.PlanState._load_state`` imports that
file's ``current_phase`` into ``plan_routing`` verbatim, once, and never
reconciles it.  A fixture left behind by a test run therefore carries
``executing`` forever, and the listing answered::

    {"id": "resume-test-1790215922380", "status": "ready",
     "pid": null, "current_phase": "executing"}

— ``ready`` (no pid, no execution row) and ``executing`` in the same
entry.  Anything that counts ``current_phase`` to answer "how many plans
are running?" over-counts by every one of those.

What the test does
------------------
1. Materialise a plan in the exact shape of those leftovers: a directory
   with ``plan_state.json`` (phase ``executing``), ``tasks.json`` and
   ``execution.json``, a ``plan_routing`` row stuck at ``executing``, and
   **no** ``plan_execution`` row.
2. Read the listing through the production route (``GET /api/plans``).
3. Assert the entry says ``status=ready`` and does NOT say
   ``current_phase=executing``.
4. Assert the rule is not "executing is never reported": a plan that
   DOES have an execution record keeps its ``executing`` phase.
5. Assert the two readers agree: ``GET /api/plan/{id}/status`` — the
   single-plan snapshot — must report the same phase as the listing.
   It used to repeat the routing row instead, so the same plan read
   ``ready`` in one answer and ``executing`` in the other.

6. Assert the third verdict is read too: a plan that carries a
   ``plan_verification`` row is judged by the **workflow's** verdict
   alone.  The run's verdict (``failed`` / ``stopped``) is about a
   process, and a dead process says nothing about a verification loop
   that is still going — so such a plan must stay on the board.  A plan
   without that row keeps the old filtering, and a plan whose routing
   phase is explicitly ``verification_passed`` is still hidden.

Scope, stated honestly
----------------------
This reads the readers — the listing and the single-plan status endpoint.
It does not repair the stale routing row — the routing table is the phase
authority, and rewriting it is a data migration, not a reader's job.  What
it pins is that no reader repeats a phase the execution store can refute,
and that the two readers do not answer the same question differently.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server import app, invalidate_plans_cache

pytestmark = pytest.mark.unit

client = TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_listing(monkeypatch, tmp_path):
    """Point the listing at this test's own plans tree."""
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    (tmp_path / "plans").mkdir(parents=True, exist_ok=True)
    invalidate_plans_cache()
    yield
    invalidate_plans_cache()


def _seed_plan(plan_id: str, *, phase: str = "executing") -> None:
    """Materialise a plan directory in the shape the leaked fixtures had.

    ``plan_state.json`` carries the legacy phase, ``tasks.json`` is what
    makes the listing's artifact heuristic answer ``ready``, and
    ``execution.json`` is the on-disk twin of the (absent) execution
    record.
    """
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": phase,
            "completed_phases": ["interview", "prd_generation", "task_generation"],
            "review_rounds": {"prd": 1, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8",
    )
    (plan_dir / "execution.json").write_text(
        json.dumps({
            "status": "running", "started_at": "2026-09-24T02:12:02Z",
            "ended_at": None, "pid": None, "stop_reason": None,
            "project_dir": "/tmp/nonexistent",
        }),
        encoding="utf-8",
    )


def _set_routing_phase(plan_id: str, phase: str) -> None:
    """Write the routing row the legacy migration would have written."""
    from server import _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(_state_db_path(None))
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        if repo.find(plan_id) is None:
            repo.insert(plan_id, phase)
        else:
            repo.write_plan_state(
                plan_id,
                phase=phase,
                completed_phases=[],
                review_rounds={},
                flags={},
                last_updated=None,
            )
    finally:
        conn.close()


def _add_execution_record(plan_id: str) -> None:
    """Give the plan the execution row a real run would have left."""
    from server import _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import ExecutionRepository

    conn = open_db(_state_db_path(None))
    try:
        migrate(conn)
        ExecutionRepository(conn).insert(
            plan_id, exec_status="running", exec_pid=4242,
        )
    finally:
        conn.close()


def _entry(plan_id: str) -> dict:
    """The listing entry for *plan_id*, read through the production route.

    The listing is cached by design (same ``PLANS_DIR``, same
    ``include_terminal``), so the cache is dropped before each read —
    otherwise a second plan seeded into the same tree would never appear.
    """
    entries = _entries()
    assert plan_id in entries, (
        f"{plan_id} is missing from the listing: {entries!r}"
    )
    return entries[plan_id]


def _entries() -> dict:
    """Every entry the default listing answers, keyed by plan id.

    The default listing is the one that hides terminal plans, so this
    is the view a reader of the sidebar sees.
    """
    invalidate_plans_cache()
    response = client.get("/api/plans")
    assert response.status_code == 200, response.text
    return {e["id"]: e for e in response.json()}


def test_a_plan_with_no_execution_record_is_not_reported_as_executing():
    """The contract: ``executing`` needs an execution record behind it."""
    never_ran = "resume-test-1790215922380"
    _seed_plan(never_ran)
    _set_routing_phase(never_ran, "executing")

    entry = _entry(never_ran)

    # The plan has tasks and no execution: the listing says ``queued``
    # (the single waiting phase; ``ready`` is only its legacy input alias
    # and must not be emitted).
    assert entry["status"] == "queued", entry
    assert entry["pid"] is None, entry
    # And it must not ALSO claim the executor is running it.
    assert entry["current_phase"] != "executing", (
        f"{never_ran} has no execution record, so ``executing`` is not a "
        f"phase it can be in — the listing answered {entry!r}"
    )
    # The two answers have to agree with each other.
    assert entry["current_phase"] == entry["status"], entry

    # The rule is not "``executing`` is never reported": a plan that DOES
    # have an execution record keeps it.
    running = "resume-test-1790215922382"
    _seed_plan(running)
    _set_routing_phase(running, "executing")
    _add_execution_record(running)

    running_entry = _entry(running)
    assert running_entry["current_phase"] == "executing", running_entry


def test_the_status_endpoint_agrees_with_the_listing_about_the_phase():
    """One plan, one phase — whichever endpoint you ask.

    The listing learned the rule above; ``GET /api/plan/{id}/status`` did
    not, and it kept repeating the routing row.  So the same plan read
    ``ready`` in one answer and ``executing`` in the other: one plan, two
    answers, and no way for a consumer to tell which one is the lie.

    Both readers now call the same judgement
    (``plan_status.resolve_phase``) with the same fallback, so they
    cannot disagree.  This pins that they do not.
    """
    never_ran = "resume-test-1790215922384"
    _seed_plan(never_ran)
    _set_routing_phase(never_ran, "executing")

    entry = _entry(never_ran)

    response = client.get(f"/api/plan/{never_ran}/status")
    assert response.status_code == 200, response.text
    payload = response.json()

    # The two endpoints must answer the same question the same way.
    assert payload["phase"] == entry["current_phase"], (
        f"the listing says current_phase={entry['current_phase']!r} and the "
        f"status endpoint says phase={payload['phase']!r} for the same plan "
        f"(no execution record, routing row says 'executing')"
    )
    # And the shared answer is not the refuted runtime claim.
    assert payload["phase"] != "executing", payload


# ---------------------------------------------------------------------------
# The third verdict: a plan that carries a verification row.
# ---------------------------------------------------------------------------


def _seed_verifying_plan(plan_id: str) -> None:
    """A plan directory in the shape a plan in a verification loop has."""
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(
        json.dumps({"verification_points": [
            {
                "id": "VP-001", "title": "验证点一",
                "verification_method": "api_test",
            },
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (plan_dir / "interview.json").write_text(
        json.dumps({
            "dimensions": {"goals": f"验证收敛性种子计划 {plan_id}"},
            "created_at": "2026-11-01T00:00:00Z",
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def _add_execution_verdict(plan_id: str, exec_status: str) -> None:
    """The execution row a finished — or dead — run leaves behind."""
    from server import _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(_state_db_path(None))
    try:
        migrate(conn)
        ExecutionRepository(conn).insert(
            plan_id,
            exec_status=exec_status,
            stop_reason="process_died_unexpectedly",
        )
    finally:
        conn.close()


def _add_verification_row(plan_id: str) -> None:
    """The verification row a plan in a verification loop carries.

    The row is the **evidence that a verification loop exists** — not
    what the loop concluded.  Its status is ``pending``: the round was
    released, the loop has not finished.
    """
    from server import _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(_state_db_path(None))
    try:
        migrate(conn)
        VerificationRepository(conn).insert(
            plan_id, "pending", round=2, max_rounds=4,
        )
    finally:
        conn.close()


def test_a_plan_in_verification_outlives_the_verdict_of_a_dead_executor():
    """A verification row keeps a plan on the board.

    The shape this pins
    -------------------
    A plan whose executor died: the run's verdict on the execution row
    is ``failed``, and the recovery scan moved the routing phase to
    ``failed`` as well.  On its own that verdict ends the plan, and the
    default listing hides it.

    But a plan that carries a ``plan_verification`` row is in — or has
    been in — a **verification loop**, which is a separate piece of work
    with its own progress.  The run's verdict is about a *process*; it
    says nothing about that loop.  A plan in a verification round, a
    plan whose verification can be started, and the result of a
    verification loop that stopped are all things the panel must still
    be able to show — so the run's verdict alone must not hide them.

    Two controls keep the rule from growing: a plan with **no**
    verification row keeps the old filtering, and a plan whose routing
    phase says the workflow has concluded (``verification_passed``) is
    still hidden.
    """
    verifying = "verify-listed-1790215922386"
    _seed_verifying_plan(verifying)
    _set_routing_phase(verifying, "failed")
    _add_execution_verdict(verifying, "failed")
    _add_verification_row(verifying)

    entries = _entries()
    assert verifying in entries, (
        f"{verifying} carries a verification row, so the panel must be able "
        f"to show it; the default listing answered {sorted(entries)!r}"
    )

    # Control 1: the same shape without the verification row is a plan
    # that is simply over — it stays hidden.
    over = "verify-absent-1790215922388"
    _seed_verifying_plan(over)
    _set_routing_phase(over, "failed")
    _add_execution_verdict(over, "failed")

    entries = _entries()
    assert over not in entries, (
        f"{over} has no verification row and its run is over, so it must "
        f"stay out of the default listing; it answered {entries[over]!r}"
    )

    # Control 2: a verification row does not resurrect a plan whose
    # workflow has already concluded.
    passed = "verify-passed-1790215922390"
    _seed_verifying_plan(passed)
    _set_routing_phase(passed, "verification_passed")
    _add_execution_verdict(passed, "failed")
    _add_verification_row(passed)

    entries = _entries()
    assert passed not in entries, (
        f"{passed} reached verification_passed, so it is over; it must stay "
        f"out of the default listing; it answered {entries[passed]!r}"
    )


# ---------------------------------------------------------------------------
# 2026-11-22: 桶由相位 / 验证行推出，不再由「目录里有没有 tasks.json」猜
# ---------------------------------------------------------------------------
#
# 事故（线上实测）：一条**已经停下**的计划在 ``GET /api/plans`` 里是
# ``status=queued``（前端翻成「排队中」），同一份响应里还有一条
# ``status=running``。原因不是相位渲染错了 —— 是桶**猜**出来的：
# ``_artifact_status`` 只看 ``tasks.json`` 在不在，于是任何有 tasks.json、
# 执行行又没说 running 的计划都被报成 ``queued``，包括失败的、停止的。
#
# 下面钉住：桶跟着**相位 / 验证行**走；没有相位时才回落到既有启发式。


def _terminal_entries() -> dict:
    """Every entry the ``include_terminal=true`` listing answers.

    终局计划（``failed`` / ``stopped``）默认被过滤掉 —— 那套判据是另一条
    规则（``_plan_is_terminal``），本次不动它。事故现场读的就是带
    ``include_terminal=true`` 的那份列表，所以这里也用它。
    """
    invalidate_plans_cache()
    response = client.get("/api/plans?include_terminal=true")
    assert response.status_code == 200, response.text
    return {e["id"]: e for e in response.json()}


def test_the_listing_bucket_is_derived_from_the_phase():
    """纯函数：相位 → 桶。逐条钉住映射表。"""
    from plan_status import listing_status_bucket

    # 等待相位（``ready`` 是历史别名）。
    assert listing_status_bucket("queued") == "queued"
    assert listing_status_bucket("ready") == "queued"
    # 「正在跑」的三个相位。
    assert listing_status_bucket("executing") == "running"
    assert listing_status_bucket("verification_running") == "running"
    assert listing_status_bucket("waiting_for_quota") == "running"
    # 终局相位。
    assert listing_status_bucket("failed") == "failed"
    assert listing_status_bucket("verification_failed") == "failed"
    assert listing_status_bucket("stopped") == "stopped"
    assert listing_status_bucket("verification_loop_stopped") == "stopped"
    assert listing_status_bucket("user_stopping") == "stopped"
    assert listing_status_bucket("completed") == "completed"
    assert listing_status_bucket("verification_passed") == "completed"


def test_the_bucket_for_a_repairing_phase_asks_the_verification_row():
    """修复相位自己不说是死是活 —— 它问验证行。

    ``verification_repairing`` 只说「修复任务在等着跑」：是操作者停的
    （``stopped``）、验证循环自己停的（``stopped``）、修复没跑成
    （``failed``），还是正要去跑（``running``），只有验证行知道。
    """
    from plan_status import listing_status_bucket

    assert listing_status_bucket("verification_repairing") == "running"
    assert listing_status_bucket(
        "verification_repairing", verification_status="loop_stopped",
    ) == "stopped"
    assert listing_status_bucket(
        "verification_repairing", verification_stop_reason="user_stopped",
    ) == "stopped"
    assert listing_status_bucket(
        "verification_repairing",
        verification_stop_reason="user_force_terminal",
    ) == "stopped"
    assert listing_status_bucket(
        "verification_repairing", verification_status="failed",
    ) == "failed"
    assert listing_status_bucket(
        "verification_repairing", verification_status="running",
    ) == "running"


def test_the_bucket_says_nothing_for_an_empty_or_unknown_phase():
    """空相位 / 未知相位 → ``None``：调用方回落既有启发式。

    启发式的桶（``reviewed`` / ``prd`` / ``interviewed`` / ``new``）**不是**
    相位，纯函数不许替它们说话。
    """
    from plan_status import listing_status_bucket

    assert listing_status_bucket("") is None
    assert listing_status_bucket(None) is None
    assert listing_status_bucket("reviewed") is None
    assert listing_status_bucket("verification") is None


def test_a_failed_plan_is_not_listed_as_queued():
    """事故的负控：一条失败的计划（有 ``tasks.json``）不再显示「排队中」。

    这条计划有 ``tasks.json``，所以旧的启发式会说 ``queued``（前端翻成
    「排队中」）。相位是 ``failed`` —— 权威说是失败，桶就必须是 ``failed``。
    """
    plan_id = "bucket-failed-1790215922392"
    _seed_plan(plan_id, phase="failed")
    _set_routing_phase(plan_id, "failed")

    entries = _terminal_entries()
    assert plan_id in entries, (
        f"{plan_id} is missing from the listing: {sorted(entries)!r}"
    )
    entry = entries[plan_id]
    assert entry["status"] != "queued", (
        f"a failed plan was listed as queued: {entry!r}"
    )
    assert entry["status"] == "failed", entry
    assert entry["current_phase"] == "failed", entry


def test_a_stopped_repair_is_listed_as_stopped():
    """停在修复相位、验证行写着「操作者停过」→ 桶是 ``stopped``。

    这就是事故那一条计划的形状：停止窗口里另一条路径把相位挪回
    ``verification_repairing``，而验证行记着「操作者停过」。
    """
    from server import _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    plan_id = "bucket-stopped-1790215922394"
    _seed_plan(plan_id, phase="verification_repairing")
    _set_routing_phase(plan_id, "verification_repairing")

    conn = open_db(_state_db_path(None))
    try:
        migrate(conn)
        VerificationRepository(conn).insert(
            plan_id,
            verification_status="loop_stopped",
            verification_stop_reason="user_stopped",
        )
    finally:
        conn.close()

    entry = _terminal_entries()[plan_id]
    assert entry["status"] == "stopped", entry
    assert entry["current_phase"] == "verification_repairing", entry


def test_a_plan_without_a_phase_keeps_the_artifact_fallback():
    """没有相位 → 既有启发式回落不变（有 ``tasks.json`` → ``queued``）。

    桶的推导只接管**有相位**的计划；没有记录下来的相位就没有权威可依，
    启发式原样留着。
    """
    plan_id = "bucket-nophase-1790215922396"
    from server import PLANS_DIR

    # 只有目录、只有 ``tasks.json``：没有 routing 行，也就没有任何相位。
    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8",
    )

    entry = _terminal_entries()[plan_id]
    assert entry["status"] == "queued", (
        "a plan with no routing row must keep the artifact fallback: "
        f"{entry!r}"
    )
