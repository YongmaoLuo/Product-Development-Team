"""
API tests for the three ``decision_point/add`` endpoints.

Uses FastAPI TestClient. Mocks ``create_coding_tool`` so we don't hit
any LLM. Verifies the full contract: 200 / 404 / 400 / 422, review.json
registration, plan_state rollback, file integrity.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from server import app, PLANS_DIR  # noqa: E402


client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Create a temp plan dir with all three docs and route
    PLANS_DIR to it for the duration of the test."""
    plans_root = tmp_path / "plans"
    plan_id = "test-add-api"
    pd = plans_root / plan_id
    pd.mkdir(parents=True)

    # arch-design.md with 2 existing DPs
    (pd / "arch-design.md").write_text(
        "# 架构设计\n\n## 决策点列表\n\n"
        "### 决策点 1: 技术栈\n\n**[A] 行动：** Python。\n\n"
        "### 决策点 2: 存储\n\n**[A] 行动：** SQLite。\n\n"
        "## 技术栈总览\n\n- Python\n",
        encoding="utf-8",
    )
    (pd / "test-design.md").write_text(
        "# 测试设计\n\n## 测试策略决策点列表\n\n"
        "### 决策点 1: 单元测试\n\n**[A] 行动：** pytest。\n\n"
        "## 测试矩阵\n\n- x\n",
        encoding="utf-8",
    )
    (pd / "prd.json").write_text(
        json.dumps(
            {
                "title": "T",
                "decision_points": [
                    {
                        "title": "目标",
                        "context": "",
                        "problem": "",
                        "evidence": "",
                        "action": "",
                        "impact": "",
                        "alternatives": [],
                        "category": "requirement",
                        "index": 0,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (pd / "interview.json").write_text(
        json.dumps({"product_form": {"form": "software"}}), encoding="utf-8"
    )

    # Plan state so transition_to works
    (pd / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": "arch_review",
                "completed_phases": [],
                "flags": {},
                "verification": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(server_module_or_globals := sys.modules["server"], "PLANS_DIR", plans_root)
    return pd, plan_id


def _mock_coding_tool_with_text(text: str) -> MagicMock:
    ct = MagicMock()
    ct.query.return_value = text
    return ct


ARCH_LLM_RESPONSE = (
    "### 决策点 99: 监控告警\n\n"
    "**[C] 背景：** 无监控。\n\n"
    "**[P] 问题：** 异常不可见。\n\n"
    "**[E] 评估：** 用 stdout JSON。\n\n"
    "**[A] 行动：** stdout JSON + cron 巡检。\n\n"
    "影响范围：运维。\n"
)

TEST_LLM_RESPONSE = (
    "### 决策点 99: E2E 测试\n\n"
    "**[C] 背景：** 无 e2e。\n\n"
    "**[P] 问题：** 缺端到端。\n\n"
    "**[E] 评估：** puppeteer。\n\n"
    "**[A] 行动：** 引入 puppeteer。\n\n"
    "测试类型：E2E\n"
)

PRD_LLM_RESPONSE = json.dumps(
    [
        {
            "title": "验收",
            "context": "C",
            "problem": "P",
            "evidence": "E",
            "action": "A",
            "impact": "I",
            "alternatives": [],
            "category": "acceptance_criterion",
        }
    ],
    ensure_ascii=False,
)


# ---------------------------------------------------------------------------
# Arch endpoint
# ---------------------------------------------------------------------------


def test_arch_add_decision_point_success(plan_dir):
    pd, plan_id = plan_dir
    with patch("server.create_coding_tool", return_value=_mock_coding_tool_with_text(ARCH_LLM_RESPONSE)):
        r = client.post(
            f"/api/arch/{plan_id}/decision_point/add",
            json={"requirement": "缺监控告警", "count": 1},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"][0]["title"] == "监控告警"
    assert body["added"][0]["index"] == 2
    assert body["no_gap_reason"] is None

    # File got the new DP inserted before 技术栈总览
    updated = (pd / "arch-design.md").read_text(encoding="utf-8")
    assert "### 决策点 3: 监控告警" in updated
    assert updated.index("### 决策点 3: 监控告警") < updated.index("## 技术栈总览")

    # review.json got the new index as pending
    review = json.loads((pd / "arch-review.json").read_text(encoding="utf-8"))
    by_idx = {it["index"]: it for it in review["items"]}
    assert by_idx[2]["status"] == "pending"


def test_arch_add_decision_point_404(plan_dir):
    _, _ = plan_dir
    # Wrong plan id
    r = client.post(
        "/api/arch/does-not-exist/decision_point/add",
        json={"requirement": "x", "count": 1},
    )
    assert r.status_code == 404


def test_arch_add_decision_point_400_empty_requirement(plan_dir):
    _, plan_id = plan_dir
    r = client.post(
        f"/api/arch/{plan_id}/decision_point/add",
        json={"requirement": "  ", "count": 1},
    )
    assert r.status_code == 400


def test_arch_add_decision_point_400_count_out_of_range(plan_dir):
    _, plan_id = plan_dir
    r = client.post(
        f"/api/arch/{plan_id}/decision_point/add",
        json={"requirement": "x", "count": 5},
    )
    assert r.status_code == 400


def test_arch_add_decision_point_rolls_back_arch_approved(plan_dir):
    """End-to-end rollback check.

    PlanState is SQLite-backed so we can't seed ``arch_approved`` via
    ``plan_state.json``. Instead we patch ``PlanState.get_state`` to
    report ``arch_approved`` and assert ``transition_to`` is invoked
    with ``arch_refining`` followed by ``arch_review``.
    """
    pd, plan_id = plan_dir
    from plan_state import PlanState

    calls: list[str] = []
    real_transition = PlanState.transition_to

    def spy_transition(self, phase):
        calls.append(phase)
        return real_transition(self, phase)

    with patch.object(PlanState, "get_state", return_value={"current_phase": "arch_approved", "completed_phases": [], "flags": {}, "verification": {}}), patch(
        "server.create_coding_tool", return_value=_mock_coding_tool_with_text(ARCH_LLM_RESPONSE)
    ), patch.object(PlanState, "transition_to", spy_transition):
        r = client.post(
            f"/api/arch/{plan_id}/decision_point/add",
            json={"requirement": "缺监控告警", "count": 1},
        )
    assert r.status_code == 200
    assert calls == ["arch_refining", "arch_review"]


# ---------------------------------------------------------------------------
# Test endpoint
# ---------------------------------------------------------------------------


def test_test_add_decision_point_success(plan_dir):
    pd, plan_id = plan_dir
    with patch("server.create_coding_tool", return_value=_mock_coding_tool_with_text(TEST_LLM_RESPONSE)):
        r = client.post(
            f"/api/test/{plan_id}/decision_point/add",
            json={"requirement": "缺 E2E", "count": 1},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"][0]["title"] == "E2E 测试"
    assert body["added"][0]["index"] == 1
    updated = (pd / "test-design.md").read_text(encoding="utf-8")
    assert "### 决策点 2: E2E 测试" in updated


# ---------------------------------------------------------------------------
# PRD endpoint
# ---------------------------------------------------------------------------


def test_prd_add_decision_point_success(plan_dir):
    pd, plan_id = plan_dir
    with patch("server.create_coding_tool", return_value=_mock_coding_tool_with_text(PRD_LLM_RESPONSE)):
        r = client.post(
            f"/api/prd/{plan_id}/decision_point/add",
            json={"requirement": "补验收标准", "count": 1},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"][0]["title"] == "验收"
    assert body["added"][0]["index"] == 1
    prd = json.loads((pd / "prd.json").read_text(encoding="utf-8"))
    assert len(prd["decision_points"]) == 2
    assert prd["decision_points"][1]["title"] == "验收"
    assert prd["decision_points"][1]["index"] == 1


def test_prd_add_decision_point_404_when_no_prd(plan_dir, tmp_path_factory):
    # Build a separate plan with no prd.json
    _, plan_id = plan_dir
    empty_plan = plan_dir[0].parent / "no-prd-plan"
    empty_plan.mkdir()
    with patch("server.create_coding_tool", return_value=_mock_coding_tool_with_text(PRD_LLM_RESPONSE)):
        r = client.post(
            f"/api/prd/no-prd-plan/decision_point/add",
            json={"requirement": "x", "count": 1},
        )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# NO_GAP path
# ---------------------------------------------------------------------------


def test_arch_add_decision_point_no_gap_returns_no_write(plan_dir):
    pd, plan_id = plan_dir
    original = (pd / "arch-design.md").read_text(encoding="utf-8")
    with patch(
        "server.create_coding_tool",
        return_value=_mock_coding_tool_with_text("NO_GAP\n已被决策点 1 覆盖"),
    ):
        r = client.post(
            f"/api/arch/{plan_id}/decision_point/add",
            json={"requirement": "技术栈选择", "count": 1},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == []
    assert body["no_gap_reason"] and "决策点 1" in body["no_gap_reason"]
    assert (pd / "arch-design.md").read_text(encoding="utf-8") == original