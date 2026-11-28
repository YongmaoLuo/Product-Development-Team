"""The framework-fix guard on the verification surface.

What this file pins
-------------------
* ``GET /api/verification/{id}/status`` answers the **per verification point**
  results — one entry per VP, each carrying the tree fingerprint its verdict
  was reached on, and each carrying how many rows the append-only table
  keeps for that VP;
* a plan that is **held** by a framework defect cannot start a verification
  round — 409, and the answer names the defect it is held on;
* the same held plan cannot be reset either — 409, the same answer;
* a plan that is **not** held behaves exactly as before: the status still
  answers every field it always answered, ``vp_results`` is empty, and the
  reset works;
* a guard that cannot read the hold state **fails closed** — 500, and the
  start does not go through.

The data is real
----------------
The hold is written through the production writer (``PlanState.enter_block``
— the same call the framework-fix lane makes), the results are written
through the production writer (``verification_persistence.
append_verification_result``), and both land in the same migrated SQLite
file the endpoints read (the ``state_db`` fixture).  Nothing here is a
double: a guard that only holds against a fake database is not a guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from server import app

client = TestClient(app)


#: The fingerprint the held plans in this file are held on.  It is a
#: **framework defect** fingerprint (the one the plan is blocked on), not a
#: tree fingerprint.
HELD_FINGERPRINT = "sha256:" + "f" * 64

#: The error word a held plan's refusal carries.
BLOCKED = "blocked_on_framework_fix"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def plans_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A plans tree of this test's own."""
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("server.PLANS_DIR", root)
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))
    return root


def _plan_dir(
    plans_root: Path,
    plan_id: str,
    plan_sqlite_seeder: Any,
) -> Path:
    """A plan in this test's tree, materialised in both stores."""
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": plan_id, "current_phase": "executing"}),
        encoding="utf-8",
    )
    plan_sqlite_seeder(plan_dir, plan_id, phase="executing")
    return plan_dir


def _hold(plan_dir: Path) -> None:
    """Hold the plan on a framework defect — through the production writer."""
    from plan_state import PlanState

    PlanState(plan_dir).enter_block(
        plan_dir.name,
        fingerprint=HELD_FINGERPRINT,
        fix_plan_id="fx-fix-0001",
    )


def _append_result(
    plan_id: str,
    vp_id: str,
    *,
    tree: str,
    verdict: str,
    round_no: int = 1,
    db_path: Path,
) -> str:
    """Append one verification result row — through the production writer."""
    from verification_persistence import append_verification_result

    return append_verification_result(
        plan_id=plan_id,
        vp_id=vp_id,
        tree_fingerprint=tree,
        verdict=verdict,
        round_no=round_no,
        db_path=db_path,
    )


def _status(plan_id: str) -> Dict[str, Any]:
    """The status answer, or a failure that names what came back."""
    resp = client.get(f"/api/verification/{plan_id}/status")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# TDD 规格
# ---------------------------------------------------------------------------
def test_vp_results_shape(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
) -> None:
    """状态接口逐个验证点给出结论，每条结论带着它那棵树的指纹。"""
    plan_id = "p1"
    _plan_dir(plans_root, plan_id, plan_sqlite_seeder)
    _append_result(
        plan_id,
        "VP-3",
        tree="sha256:" + "a" * 64,
        verdict="failed",
        db_path=state_db,
    )

    body = _status(plan_id)

    results = body["vp_results"]
    assert len(results) == 1, f"一个验证点就该有一条结论；实际 {results!r}"
    entry = results[0]
    assert entry["vp_id"] == "VP-3", f"结论得说清是哪个验证点；实际 {entry!r}"
    assert entry["status"] == "failed", f"结论本身要带出来；实际 {entry!r}"
    assert entry["tree_fingerprint"] == "sha256:" + "a" * 64, (
        "结论必须绑在它得出时那棵树上 —— 没有树就没法说「这棵树过了吗」；"
        f"实际 {entry!r}"
    )


def test_vp_results_latest_tree_and_history(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
) -> None:
    """同一个验证点有多行 → 给最新那棵树的那行，历史行数留在计数里。"""
    plan_id = "p1"
    _plan_dir(plans_root, plan_id, plan_sqlite_seeder)
    _append_result(
        plan_id,
        "VP-3",
        tree="sha256:" + "0" * 64,
        verdict="failed",
        round_no=1,
        db_path=state_db,
    )
    _append_result(
        plan_id,
        "VP-3",
        tree="sha256:" + "1" * 64,
        verdict="passed",
        round_no=2,
        db_path=state_db,
    )

    body = _status(plan_id)

    results = body["vp_results"]
    assert len(results) == 1, f"一个验证点只给一行；实际 {results!r}"
    entry = results[0]
    assert entry["tree_fingerprint"] == "sha256:" + "1" * 64, (
        "最新那棵树上的那行才是当前的结论；实际 {entry!r}".format(entry=entry)
    )
    assert entry["status"] == "passed", f"实际 {entry!r}"
    assert entry["history_count"] == 2, (
        "历史行不许被吃掉 —— 一次失败抹掉一次，账就不准了；实际 "
        f"{entry!r}"
    )


def test_vp_results_empty_without_results(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
) -> None:
    """没有结果 → 空列表（不是缺字段）。"""
    plan_id = "p1"
    _plan_dir(plans_root, plan_id, plan_sqlite_seeder)

    body = _status(plan_id)

    assert body["vp_results"] == [], f"没有结果就是空列表；实际 {body!r}"


def test_start_blocked_409(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
) -> None:
    """被挂起的计划起不了验证 —— 409，并说清挂在哪个缺陷上。"""
    plan_id = "p1"
    plan_dir = _plan_dir(plans_root, plan_id, plan_sqlite_seeder)
    _hold(plan_dir)

    resp = client.post(f"/api/verification/{plan_id}/start", json={})

    assert resp.status_code == 409, f"被挂起的计划不许起验证；实际 {resp.text}"
    body = resp.json()
    assert body["error"] == BLOCKED, f"拒绝得说清是框架缺陷挡的；实际 {body!r}"
    assert body["fingerprint"] == HELD_FINGERPRINT, (
        f"拒绝得带上挂住它的那个缺陷的指纹；实际 {body!r}"
    )


def test_reset_blocked_409(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
) -> None:
    """重置也一样拒绝 —— 挂起期间不许把状态抹了重跑。"""
    plan_id = "p1"
    plan_dir = _plan_dir(plans_root, plan_id, plan_sqlite_seeder)
    _hold(plan_dir)

    resp = client.post(f"/api/verification/{plan_id}/reset", json={})

    assert resp.status_code == 409, f"被挂起的计划不许重置；实际 {resp.text}"
    body = resp.json()
    assert body["error"] == BLOCKED, f"拒绝得说清是框架缺陷挡的；实际 {body!r}"
    assert body["fingerprint"] == HELD_FINGERPRINT, (
        f"拒绝得带上挂住它的那个缺陷的指纹；实际 {body!r}"
    )


def test_guard_failure_fails_closed(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """守卫读不到挂起状态 → 500，不放行。"""
    plan_id = "p1"
    _plan_dir(plans_root, plan_id, plan_sqlite_seeder)

    import plan_state

    monkeypatch.setattr(
        plan_state, "_read_routing_framework_fix", lambda _plan_id: None,
    )

    resp = client.post(f"/api/verification/{plan_id}/start", json={})

    assert resp.status_code == 500, (
        f"守卫读不到状态就不能放行 —— 放行等于把守卫变成摆设；实际 {resp.text}"
    )


def test_normal_plan_unchanged(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
) -> None:
    """没被挂起的计划：老字段一个不少，``vp_results`` 是空的，重置照常。"""
    plan_id = "p2"
    _plan_dir(plans_root, plan_id, plan_sqlite_seeder)

    body = _status(plan_id)

    for key in (
        "plan_id",
        "current_phase",
        "verification_status",
        "verification_round",
        "verification_max_rounds",
        "results",
        "repair_tasks",
        "execution_profile",
        "verification_points",
        "started_at",
        "updated_at",
    ):
        assert key in body, f"老字段 {key!r} 不许消失；实际 {body!r}"
    assert body["vp_results"] == [], f"没有结果就是空列表；实际 {body!r}"

    resp = client.post(f"/api/verification/{plan_id}/reset", json={})
    assert resp.status_code == 200, (
        f"没被挂起的计划照常能重置；实际 {resp.text}"
    )
