"""``/progress`` 的三个索引桶改为**从 verdicts 派生**（负控 B）。

背景
----
``_verification_progress_body`` 原先直接信任 ``progress_state`` 里存下的
``completed_vps`` / ``failed_vps`` / ``skipped_vps`` 三个列表。那三个列表是
**缓存**，不是真相：历史坏行（例如同一个 VP 同时出现在 failed 和 completed
里）会被原样返回给卡片，于是「失败数」凭空多一个，修复环拿着这份输入去重跑
一个其实早就 SKIPPED 的判据。

真相在 ``plan_verification.verdicts``（裁决表）里 —— 写侧
``VerificationExecutor._backfill_index_lists_from_verdicts`` 早就以它为准，
而规则已由 :mod:`verification_buckets` 抽成了唯一的纯函数。

契约
----
  * verdicts 非空 → 三个列表**完全**由 verdicts 派生，``progress_state`` 里
    的三个列表一律不信（负控 B）；
  * verdicts 为空（真正的旧行）→ 回退到 ``progress_state`` 的三个列表，
    仅限迁移窗口；
  * DEFERRED 不进任何终态桶 → 仍然是 pending；
  * 计划作废（``obsolete`` / ``superseded_by``）的 FAILED 沿用写侧的规则。

五个用例分别钉住：坏行被裁决覆盖、计数随之变化、三桶两两互斥、旧行保持旧
行为、DEFERRED 仍是 pending。第一个用例之外另有一条直接钉住任务书给定的
输入 / 输出示例。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# ``backend/`` 在 ``sys.path`` 上，``server`` / ``routes.verification`` /
# ``verification_buckets`` 才 import 得到（与同目录其它用例同一套路）。
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from server import app  # noqa: E402
from routes import verification as routes_verification  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    """A FastAPI TestClient bound to the real ``server.app``."""
    return TestClient(app)


def _write_plan(
    plan_dir: Path,
    *,
    plan_id: str,
    vps: List[Dict[str, Any]],
) -> None:
    """Write the plan's ``verification_plan.json`` (the VP universe)."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(
            {"plan_id": plan_id, "verification_points": vps},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_row(
    plan_id: str,
    *,
    progress_state: Dict[str, Any],
    verdicts: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Write the ``plan_verification`` row: progress_state + verdicts.

    The row is written through the same repository the executor uses:
    ``insert`` for the row itself, then ``append_verdict`` once per
    verdict — so the stored ``verdicts`` column has exactly the shape
    production produces (``[{"vp_id", "status", ...}, ...]``).
    """
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    db_path = server._state_db_path(None)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(
            plan_id,
            verification_status="running",
            progress_state=progress_state,
            verdicts=[],
        )
        for verdict in verdicts or []:
            repo.append_verdict(plan_id, verdict)
    finally:
        conn.close()


def _progress_state(
    *,
    completed: Optional[List[str]] = None,
    failed: Optional[List[str]] = None,
    skipped: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """A ``progress_state`` payload (the three cached index lists)."""
    return {
        "current_vp": None,
        "completed_vps": list(completed or []),
        "failed_vps": list(failed or []),
        "skipped_vps": list(skipped or []),
        "updated_at": "2026-10-23T10:00:00.000000Z",
    }


def _vp(vp_id: str, title: str = "some criterion") -> Dict[str, Any]:
    return {
        "id": vp_id,
        "title": title,
        "verification_method": "automated_test",
        "layer": 1,
    }


def _get_progress(client: TestClient, plan_id: str) -> Dict[str, Any]:
    response = client.get(f"/api/verification/{plan_id}/progress")
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 任务书给定的输入 / 输出示例
# ---------------------------------------------------------------------------


def test_derive_vp_buckets_matches_documented_example() -> None:
    """任务书给的输入 → 输出必须逐字对得上。"""
    buckets = routes_verification._derive_vp_buckets(
        {
            "verdicts": [
                {"vp_id": "VP-3", "status": "SKIPPED"},
                {"vp_id": "VP-1", "status": "PASSED"},
            ]
        },
        [],
        {"failed_vps": ["VP-3"], "completed_vps": ["VP-1"]},
    )
    assert buckets == {
        "completed_vps": ["VP-1"],
        "failed_vps": [],
        "skipped_vps": ["VP-3"],
    }


# ---------------------------------------------------------------------------
# 负控 B：坏行被裁决覆盖
# ---------------------------------------------------------------------------


def test_corrupt_failed_list_overridden_by_verdict(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """同一个 VP 同时在 failed / completed 里，裁决说 SKIPPED。

    坏行里 ``VP-X`` 同时出现在 ``failed_vps`` 和 ``completed_vps``。
    裁决表说它是 SKIPPED —— 所以它**只在** skipped 里，两个终态桶都不
    该再出现它。
    """
    plan_id = "20261023-derive-corrupt"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(plan_dir, plan_id=plan_id, vps=[_vp("VP-X")])
    _write_row(
        plan_id,
        progress_state=_progress_state(
            completed=["VP-X"],
            failed=["VP-X"],
        ),
        verdicts=[{"vp_id": "VP-X", "status": "SKIPPED"}],
    )

    payload = _get_progress(client, plan_id)

    assert payload["skipped_vps"] == ["VP-X"]
    assert payload["failed_vps"] == []
    assert payload["completed_vps"] == []


def test_counts_follow_derived_buckets(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """``counts`` 必须跟着派生出来的桶走，不是跟着坏行走。

    坏行说 ``VP-A`` 和 ``VP-X`` 都失败了；裁决说 ``VP-A`` 真的失败、
    ``VP-X`` 是 SKIPPED。所以 ``counts.failed`` 只有一个（VP-A），
    ``counts.skipped`` 有一个（VP-X）。
    """
    plan_id = "20261023-derive-counts"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(
        plan_dir,
        plan_id=plan_id,
        vps=[_vp("VP-A"), _vp("VP-X")],
    )
    _write_row(
        plan_id,
        progress_state=_progress_state(failed=["VP-A", "VP-X"]),
        verdicts=[
            {"vp_id": "VP-A", "status": "FAILED"},
            {"vp_id": "VP-X", "status": "SKIPPED"},
        ],
    )

    payload = _get_progress(client, plan_id)
    counts = payload["counts"]

    assert counts["failed"] == 1
    assert counts["skipped"] == 1
    assert counts["completed"] == 0
    assert counts["total"] == 2
    assert counts["pending"] == 0


def test_lists_mutually_exclusive(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """三个列表两两无交集。

    一个 VP 只能落在一个桶里 —— 坏行里「既失败又完成」的那条不许在两个
    桶里同时出现。
    """
    plan_id = "20261023-derive-exclusive"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(
        plan_dir,
        plan_id=plan_id,
        vps=[_vp("VP-A"), _vp("VP-B"), _vp("VP-X")],
    )
    _write_row(
        plan_id,
        progress_state=_progress_state(
            completed=["VP-B", "VP-X"],
            failed=["VP-A", "VP-X"],
        ),
        verdicts=[
            {"vp_id": "VP-A", "status": "FAILED"},
            {"vp_id": "VP-B", "status": "PASSED"},
            {"vp_id": "VP-X", "status": "SKIPPED"},
        ],
    )

    payload = _get_progress(client, plan_id)
    completed = set(payload["completed_vps"])
    failed = set(payload["failed_vps"])
    skipped = set(payload["skipped_vps"])

    assert completed & failed == set()
    assert completed & skipped == set()
    assert failed & skipped == set()
    assert completed == {"VP-B"}
    assert failed == {"VP-A"}
    assert skipped == {"VP-X"}


def test_legacy_row_without_verdicts_uses_stored_lists(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """没有裁决的旧行 → 保持旧行为（读 progress_state 的三个列表）。

    迁移窗口内已经落库的行没有 ``verdicts``，这时三个列表仍然是唯一的
    可用信息，必须原样返回。
    """
    plan_id = "20261023-derive-legacy"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(
        plan_dir,
        plan_id=plan_id,
        vps=[_vp("VP-1"), _vp("VP-2"), _vp("VP-3")],
    )
    _write_row(
        plan_id,
        progress_state=_progress_state(
            completed=["VP-1"],
            failed=["VP-2"],
            skipped=["VP-3"],
        ),
        verdicts=[],
    )

    payload = _get_progress(client, plan_id)

    assert payload["completed_vps"] == ["VP-1"]
    assert payload["failed_vps"] == ["VP-2"]
    assert payload["skipped_vps"] == ["VP-3"]
    assert payload["counts"]["completed"] == 1
    assert payload["counts"]["failed"] == 1
    assert payload["counts"]["skipped"] == 1


def test_deferred_stays_pending(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """DEFERRED 不在任何终态桶里，仍然是 pending。

    DEFERRED 的语义是「这一轮没验，下一轮要重跑」—— 把它塞进 failed 会
    凭空制造一个修复任务，塞进 skipped 会让它永远不再跑。
    """
    plan_id = "20261023-derive-deferred"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(plan_dir, plan_id=plan_id, vps=[_vp("VP-D")])
    _write_row(
        plan_id,
        # 坏行：把 DEFERRED 的 VP 记成了失败。派生之后一律不信。
        progress_state=_progress_state(failed=["VP-D"]),
        verdicts=[{"vp_id": "VP-D", "status": "DEFERRED"}],
    )

    payload = _get_progress(client, plan_id)

    assert "VP-D" not in payload["completed_vps"]
    assert "VP-D" not in payload["failed_vps"]
    assert "VP-D" not in payload["skipped_vps"]
    assert payload["pending_vps"] == ["VP-D"]
    assert payload["counts"]["pending"] == 1
    assert payload["counts"]["failed"] == 0
    vp_entry = next(v for v in payload["vps"] if v["id"] == "VP-D")
    assert vp_entry["status"] == "pending"


# ---------------------------------------------------------------------------
# 边界条件：verdicts 非空时，缓存里的列表**整个**不信
# ---------------------------------------------------------------------------


def test_verdicts_ignore_stored_lists_entirely(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """缓存里那些**没有裁决**的条目也一律不认。

    「完全忽略」是字面的：只要这一行有裁决，``progress_state`` 的三个
    列表里多出来的条目（``VP-GHOST`` 这种连计划里都没有的）不能回到
    响应里 —— 否则坏行还是能凭空造出一个失败。
    """
    plan_id = "20261023-derive-ghost"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(plan_dir, plan_id=plan_id, vps=[_vp("VP-1")])
    _write_row(
        plan_id,
        progress_state=_progress_state(
            completed=["VP-1"],
            failed=["VP-GHOST"],
            skipped=["VP-GHOST-2"],
        ),
        verdicts=[{"vp_id": "VP-1", "status": "PASSED"}],
    )

    payload = _get_progress(client, plan_id)

    assert payload["completed_vps"] == ["VP-1"]
    assert payload["failed_vps"] == []
    assert payload["skipped_vps"] == []
    assert payload["counts"]["failed"] == 0
    assert payload["counts"]["skipped"] == 0


def test_retired_failed_verdict_keeps_write_side_rules(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """计划作废的 VP 沿用写侧的规则。

    ``obsolete``（计划不要这个判据了）的 FAILED 不进任何桶；
    ``superseded_by``（父 VP 被子 VP 取代）的 FAILED 进 skipped —— 否则
    下一轮会把拆分过的那条大 VP 重跑一遍。
    """
    plan_id = "20261023-derive-retired"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(
        plan_dir,
        plan_id=plan_id,
        vps=[
            {**_vp("VP-OBS"), "obsolete": True},
            {**_vp("VP-SUP"), "superseded_by": ["VP-SUP-1"]},
        ],
    )
    _write_row(
        plan_id,
        progress_state=_progress_state(),
        verdicts=[
            {"vp_id": "VP-OBS", "status": "FAILED"},
            {"vp_id": "VP-SUP", "status": "FAILED"},
        ],
    )

    payload = _get_progress(client, plan_id)

    assert "VP-OBS" not in payload["failed_vps"]
    assert payload["skipped_vps"] == ["VP-SUP"]
    assert payload["counts"]["failed"] == 0

