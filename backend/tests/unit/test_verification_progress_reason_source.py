"""``/progress`` 的失败原因改为**与失败桶同源**（verdicts 优先，报告仅回退）。

背景
----
``/api/verification/{plan_id}/progress`` 的失败桶早就改成从
``plan_verification.verdicts``（裁决表）派生 —— 那是真相。但**失败原因**
却另起一路：从 ``verification_report.json`` 里抓 ``actual_result``。

两路数据源不同步，于是 2026-10-23 观测到：``counts.failed == 11``，
但 ``vps[]`` 里的失败项**没有** ``actual_result``，飞书卡片只剩一行
``🔴 [VP-001] · 标题``。根因是报告被后续判定阶段重写成了「17 条全
PASSED」—— 报告里一条失败原因都匹配不到，而失败桶（来自裁决表）还是
11 条。操作员看得见失败数，看不见失败原因。

契约
----
  * 失败原因与失败桶**同源**：优先从裁决（verdicts）取；
  * ``verification_report.json`` 降级为**回退**：裁决给不出原因时才用；
  * 裁决有 ``reasons`` 没有 ``actual_result`` → 用 ``reasons`` 以 ``；`` 拼接；
  * 裁决两者皆空 → 回退报告；裁决表整个为空（真正的旧行）→ 完全走回退；
  * 非 failed 的 VP **不得**带上 ``actual_result`` / ``evidence_tail``；
  * 截断契约不变：``actual_result`` ≤ 500、``evidence_tail`` ≤ 300。

五个用例分别钉住：报告里没有原因时裁决的原因照样冒出来、报告退居回退、
非失败项不带这两个字段、超长原因被截断、BLOCKED 仍然落在失败桶里且带
原因。另有三个用例钉住边界：证据的两种形状、真正的旧行走回退、裁决压过
报告里同名的失败条目。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# ``backend/`` 在 ``sys.path`` 上，``server`` / ``routes.verification``
# 才 import 得到（与同目录其它用例同一套路）。
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from server import app  # noqa: E402


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


def _write_report(
    plan_dir: Path,
    *,
    results: List[Dict[str, Any]],
) -> None:
    """Write ``verification_report.json`` (the **fallback** source)."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_report.json").write_text(
        json.dumps({"verification_results": results}, ensure_ascii=False),
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


def _vp_entry(payload: Dict[str, Any], vp_id: str) -> Dict[str, Any]:
    """The ``vps[]`` entry of one VP."""
    return next(v for v in payload["vps"] if v["id"] == vp_id)


# ---------------------------------------------------------------------------
# 1. 报告里没有原因时，裁决的原因照样冒出来
# ---------------------------------------------------------------------------


def test_verdict_reason_surfaces_when_report_lacks_it(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """报告被重写成「全 PASSED」→ 失败原因仍然来自裁决。

    这就是 2026-10-23 观测到的现场：失败桶（裁决派生）有 11 条，
    报告里一条失败原因都匹配不到。修复前这条 VP 的 ``actual_result``
    是空的，卡片只剩 ``🔴 [VP-001] · 标题``。
    """
    plan_id = "20261023-reason-verdict"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(plan_dir, plan_id=plan_id, vps=[_vp("VP-001")])
    # 报告被后续判定阶段重写：一条失败原因都没有。
    _write_report(
        plan_dir,
        results=[{"id": "VP-001", "status": "PASSED", "actual_result": "all good"}],
    )
    _write_row(
        plan_id,
        progress_state=_progress_state(failed=["VP-001"]),
        verdicts=[
            {
                "vp_id": "VP-001",
                "status": "FAILED",
                "reasons": ["suite 3 failed"],
                "evidence": {"pytest": "3 failed, 12 passed"},
            }
        ],
    )

    payload = _get_progress(client, plan_id)
    entry = _vp_entry(payload, "VP-001")

    assert entry["status"] == "failed"
    assert entry["actual_result"] == "suite 3 failed"
    assert entry["evidence_tail"]
    assert payload["counts"]["failed"] == 1


# ---------------------------------------------------------------------------
# 2. 裁决给不出原因 → 回退报告
# ---------------------------------------------------------------------------


def test_report_is_fallback_when_verdict_has_no_reason(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """裁决的 ``reasons`` 与 ``actual_result`` 都空 → 用报告里的原因。"""
    plan_id = "20261023-reason-fallback"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(plan_dir, plan_id=plan_id, vps=[_vp("VP-001")])
    _write_report(
        plan_dir,
        results=[
            {
                "id": "VP-001",
                "status": "FAILED",
                "actual_result": "报告里的原因",
                "evidence": "report evidence",
            }
        ],
    )
    _write_row(
        plan_id,
        progress_state=_progress_state(failed=["VP-001"]),
        verdicts=[
            {"vp_id": "VP-001", "status": "FAILED", "reasons": [], "evidence": {}}
        ],
    )

    payload = _get_progress(client, plan_id)
    entry = _vp_entry(payload, "VP-001")

    assert entry["status"] == "failed"
    assert entry["actual_result"] == "报告里的原因"


# ---------------------------------------------------------------------------
# 3. 非 failed 的 VP 不带这两个字段
# ---------------------------------------------------------------------------


def test_non_failed_vp_has_no_reason_fields(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """PASSED 的 VP 不得出现 ``actual_result`` / ``evidence_tail``。

    失败原因字段是失败桶的私有载荷：挂到 PASSED 的 VP 上，卡片会把
    「通过」渲染成「失败」。
    """
    plan_id = "20261023-reason-nonfailed"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(plan_dir, plan_id=plan_id, vps=[_vp("VP-001"), _vp("VP-002")])
    # 报告里有一条 VP-001 的失败条目 —— 也不该泄漏到 PASSED 的 VP 上。
    _write_report(
        plan_dir,
        results=[
            {"id": "VP-001", "status": "FAILED", "actual_result": "报告里的原因"}
        ],
    )
    _write_row(
        plan_id,
        progress_state=_progress_state(completed=["VP-001"], failed=["VP-002"]),
        verdicts=[
            {
                "vp_id": "VP-001",
                "status": "PASSED",
                "reasons": ["all green"],
                "evidence": {"pytest": "12 passed"},
            },
            {
                "vp_id": "VP-002",
                "status": "FAILED",
                "reasons": ["boom"],
                "evidence": {},
            },
        ],
    )

    payload = _get_progress(client, plan_id)
    passed = _vp_entry(payload, "VP-001")

    assert passed["status"] == "completed"
    assert "actual_result" not in passed
    assert "evidence_tail" not in passed


# ---------------------------------------------------------------------------
# 4. 原因截断到 500
# ---------------------------------------------------------------------------


def test_reason_truncated_to_500(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """超长的原因被截断到 500 字符（卡片契约不变）。"""
    plan_id = "20261023-reason-truncate"
    plan_dir = isolated_plans_dir / plan_id
    long_reason = "R" * 900
    _write_plan(plan_dir, plan_id=plan_id, vps=[_vp("VP-001")])
    _write_row(
        plan_id,
        progress_state=_progress_state(failed=["VP-001"]),
        verdicts=[
            {
                "vp_id": "VP-001",
                "status": "FAILED",
                "reasons": [long_reason],
                "evidence": {},
            }
        ],
    )

    payload = _get_progress(client, plan_id)
    entry = _vp_entry(payload, "VP-001")

    assert entry["actual_result"]
    assert len(entry["actual_result"]) == 500


# ---------------------------------------------------------------------------
# 5. BLOCKED 仍然落在失败桶里，并且带原因
# ---------------------------------------------------------------------------


def test_blocked_verdict_still_in_failed_bucket(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """BLOCKED 的裁决 → ``status == "failed"``、计入 ``counts.failed``、原因非空。

    这里断言的是**接口响应**（不是纯函数 ``bucket_for_status``）——
    读侧看到的是同一个失败桶。
    """
    plan_id = "20261023-reason-blocked"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(plan_dir, plan_id=plan_id, vps=[_vp("VP-001")])
    _write_row(
        plan_id,
        progress_state=_progress_state(failed=["VP-001"]),
        verdicts=[
            {
                "vp_id": "VP-001",
                "status": "BLOCKED",
                "reasons": ["BLOCKED — service not ready"],
                "evidence": [],
            }
        ],
    )

    payload = _get_progress(client, plan_id)
    entry = _vp_entry(payload, "VP-001")

    assert entry["status"] == "failed"
    assert payload["counts"]["failed"] == 1
    assert payload["failed_vps"] == ["VP-001"]
    assert entry["actual_result"]


# ---------------------------------------------------------------------------
# 边界：证据的两种形状
# ---------------------------------------------------------------------------


def test_evidence_dict_is_stringified_not_crashed(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """``evidence`` 是 ``dict`` → 转成字符串再截断（直接切片会 TypeError）。"""
    plan_id = "20261023-reason-evidence-dict"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(plan_dir, plan_id=plan_id, vps=[_vp("VP-001")])
    _write_row(
        plan_id,
        progress_state=_progress_state(failed=["VP-001"]),
        verdicts=[
            {
                "vp_id": "VP-001",
                "status": "FAILED",
                "reasons": ["boom"],
                "evidence": {"pytest": "x" * 900},
            }
        ],
    )

    payload = _get_progress(client, plan_id)
    entry = _vp_entry(payload, "VP-001")

    assert entry["actual_result"] == "boom"
    assert isinstance(entry["evidence_tail"], str)
    assert entry["evidence_tail"]
    assert len(entry["evidence_tail"]) == 300


def test_evidence_list_is_joined(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """``evidence`` 是 ``list[str]`` → 用 ``；`` 拼接后截断。"""
    plan_id = "20261023-reason-evidence-list"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(plan_dir, plan_id=plan_id, vps=[_vp("VP-001")])
    _write_row(
        plan_id,
        progress_state=_progress_state(failed=["VP-001"]),
        verdicts=[
            {
                "vp_id": "VP-001",
                "status": "FAILED",
                "reasons": ["boom"],
                "evidence": ["line one", "line two"],
            }
        ],
    )

    payload = _get_progress(client, plan_id)
    entry = _vp_entry(payload, "VP-001")

    assert entry["evidence_tail"] == "line one；line two"


# ---------------------------------------------------------------------------
# 边界：真正的旧行（verdicts 为空）→ 完全走回退
# ---------------------------------------------------------------------------


def test_legacy_row_without_verdicts_falls_back_to_report(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """裁决表为空的旧行 → 原因完全来自报告（迁移窗口内保持旧行为）。"""
    plan_id = "20261023-reason-legacy"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(plan_dir, plan_id=plan_id, vps=[_vp("VP-001")])
    _write_report(
        plan_dir,
        results=[
            {
                "id": "VP-001",
                "status": "FAILED",
                "actual_result": "旧行的原因",
                "evidence": "old evidence",
            }
        ],
    )
    _write_row(
        plan_id,
        progress_state=_progress_state(failed=["VP-001"]),
        verdicts=[],
    )

    payload = _get_progress(client, plan_id)
    entry = _vp_entry(payload, "VP-001")

    assert entry["status"] == "failed"
    assert entry["actual_result"] == "旧行的原因"
    assert entry["evidence_tail"] == "old evidence"


# ---------------------------------------------------------------------------
# 边界：裁决压过报告里同名的失败条目
# ---------------------------------------------------------------------------


def test_verdict_wins_over_report_entry(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """报告里也有这条 VP 的失败条目 → 仍然以裁决为准。

    报告是**回退**，不是平级来源：两份数据源不一致时，失败桶认的那份
    （裁决）才是原因。
    """
    plan_id = "20261023-reason-priority"
    plan_dir = isolated_plans_dir / plan_id
    _write_plan(plan_dir, plan_id=plan_id, vps=[_vp("VP-001")])
    _write_report(
        plan_dir,
        results=[
            {
                "id": "VP-001",
                "status": "FAILED",
                "actual_result": "报告里的旧原因",
                "evidence": "report evidence",
            }
        ],
    )
    _write_row(
        plan_id,
        progress_state=_progress_state(failed=["VP-001"]),
        verdicts=[
            {
                "vp_id": "VP-001",
                "status": "FAILED",
                "reasons": ["裁决里的原因"],
                "evidence": {"pytest": "1 failed"},
            }
        ],
    )

    payload = _get_progress(client, plan_id)
    entry = _vp_entry(payload, "VP-001")

    assert entry["actual_result"] == "裁决里的原因"
    assert entry["evidence_tail"] != "report evidence"
