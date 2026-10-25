"""计划书的只读审核端点。

背景
----
人工审核要看到三样东西：**文档原文**、**结构校验结果**、**条目列表**。前两样
来自 ``GET /api/verification-plan/{plan_id}``，第三样来自
``GET /api/verification-plan/{plan_id}/review/items``。两个端点读的是同一份磁盘
上的文档 —— 审核界面上看到的原文与被审的原文必须是同一份，否则「审的是 A、
看到的是 B」。

基线只有一个来源
----------------
文档里那一行 ``baseline:``。审核通过之后冻结的基线就是它，所以 accept 之前
必须让审核者看见它 —— ``PLAN-OBJECT`` 条目带着它（架构决策点 3：审核对象与派生
视图；test 决策点 5：review item 形状与基线展示）。

结构非法不是接口错误
--------------------
200 + 非空 ``structure_errors``。审核者要看到的是「这份文档哪里不对」，不是
一句 500。

测试数据
--------
canonical 文档与审核状态模块的单元测试同源（十个章节按模板顺序排列、「被验证
对象」章节含唯一一行 ``baseline:``、「验证点清单」章节含两个 VP 块），因此条目
是 5 个计划级 + 2 个验证点 = 7 条。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from tests.unit.test_verification_plan_review import CANONICAL_MD  # noqa: E402
from verification_plan_review import record_action  # noqa: E402

client = TestClient(server.app)

#: 验证分支的产物：计划书（相对路径）。
PLAN_DOC = "verification-plan.md"

#: canonical 文档里的基线（与 ``baseline:`` 行逐字对应）。
EXPECTED_BASELINE = {
    "repo_dir": "/tmp/repo",
    "ref": "refs/pull/7/head",
    "pr_number": 7,
}

#: canonical 文档的预期条目 id（顺序即审核顺序）。
EXPECTED_IDS = [
    "PLAN-OBJECT",
    "PLAN-EXCLUDE",
    "PLAN-METHOD",
    "PLAN-ENV",
    "PLAN-RISK",
    "VP-001",
    "VP-002",
]


# ---------------------------------------------------------------------------
# 计划目录
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir() -> Path:
    """一个空计划目录（还没有计划书）。

    ``server.PLANS_DIR`` 由 conftest 的 autouse fixture 重定向到 tmp_path，
    所以这里建的目录不会落到仓库里的 ``plans/`` 树上。
    """
    path = server.PLANS_DIR / "vp-review-1"
    path.mkdir(parents=True)
    return path


def _write_doc(plan_dir: Path, text: str) -> None:
    """按字节落盘计划书。

    审核状态是按**文件字节**算哈希的，所以文档必须先落盘再按字节读回 ——
    拼出来的近似文档测不出换行归一化这类问题。
    """
    (plan_dir / PLAN_DOC).write_bytes(text.encode("utf-8"))


def _doc_without_last_section() -> str:
    """砍掉「风险面」这一节 —— 十章节契约少一节，其余不动。"""
    head, marker, _ = CANONICAL_MD.partition("## 风险面")
    assert marker, "canonical 文档里没有「风险面」这一节"
    return head


# ---------------------------------------------------------------------------
# 1. GET 文档 → 原文 + 结构校验结果 + 基线
# ---------------------------------------------------------------------------


def test_get_document_returns_content_and_baseline(plan_dir: Path) -> None:
    """canonical 文档 → 200：原文逐字一致、结构无错、基线三字段。"""
    _write_doc(plan_dir, CANONICAL_MD)

    resp = client.get(f"/api/verification-plan/{plan_dir.name}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == CANONICAL_MD, "返回的必须是磁盘上的原文"
    assert body["structure_errors"] == [], "canonical 文档不该有结构错误"
    assert body["baseline"] == EXPECTED_BASELINE, (
        "基线必须逐字来自文档里的 baseline: 行；实际是 %r" % (body["baseline"],)
    )
    # 响应里不许出现计划目录的绝对路径（基线里的仓库路径是文档内容的一部分，
    # 那是另一回事）。
    assert str(plan_dir) not in resp.text


# ---------------------------------------------------------------------------
# 2. GET 条目 → 7 个条目、形状固定、PLAN-OBJECT 带基线
# ---------------------------------------------------------------------------


def test_items_shape_and_plan_object_baseline(plan_dir: Path) -> None:
    """7 个条目；形状固定五键；``PLAN-OBJECT`` 带着同一份基线。"""
    _write_doc(plan_dir, CANONICAL_MD)

    resp = client.get(f"/api/verification-plan/{plan_dir.name}/review/items")

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 7, "5 个计划级条目 + 2 个验证点条目"
    assert [item["id"] for item in items] == EXPECTED_IDS
    assert [item["index"] for item in items] == list(range(7))

    # 形状固定：条目只有这五个键。审核界面按这五个键渲染，多余的字段一律不许
    # 出现（PRD 的条目形状里有个 content 字段，验证点条目不许照抄）。
    for item in items[1:]:
        assert set(item) == {"index", "id", "title", "status", "note"}

    # PLAN-OBJECT 多带一份基线：它是「被验证对象 + 验证范围」那条目，覆盖的
    # 正是文档里那行 baseline: —— accept 之前必须看得到将被冻结的基线。
    assert set(items[0]) == {"index", "id", "title", "status", "note", "baseline"}
    assert items[0]["baseline"] == EXPECTED_BASELINE, (
        "PLAN-OBJECT 带的基线必须与文档里那一份是同一份；实际是 %r"
        % (items[0]["baseline"],)
    )
    # 「同一份」不是自称：两个端点给的是同一份基线，一个来源。
    doc_resp = client.get(f"/api/verification-plan/{plan_dir.name}")
    assert doc_resp.status_code == 200, doc_resp.text
    assert items[0]["baseline"] == doc_resp.json()["baseline"]

    # 没审过的条目一律 pending，且没有原因。
    assert all(item["status"] == "pending" for item in items)
    assert all(item["note"] == "" for item in items)


# ---------------------------------------------------------------------------
# 3. 文档缺失 → 404
# ---------------------------------------------------------------------------


def test_missing_document_404(plan_dir: Path) -> None:
    """目录在、计划书不在 → 404（两个端点都是）。"""
    resp = client.get(f"/api/verification-plan/{plan_dir.name}")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "document_not_found"

    resp = client.get(f"/api/verification-plan/{plan_dir.name}/review/items")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "document_not_found"


def test_missing_plan_404() -> None:
    """计划目录都不在 → 404。"""
    resp = client.get("/api/verification-plan/vp-review-there-is-no-such-plan")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "plan_not_found"


# ---------------------------------------------------------------------------
# 4. 结构非法 → 200 + 非空 structure_errors
# ---------------------------------------------------------------------------


def test_invalid_document_returns_errors(plan_dir: Path) -> None:
    """缺一个章节 → 200，``structure_errors`` 非空（不是 500）。"""
    _write_doc(plan_dir, _doc_without_last_section())

    resp = client.get(f"/api/verification-plan/{plan_dir.name}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["structure_errors"], "结构非法必须逐条给出违规"
    assert any(
        error["code"] == "missing_section" for error in body["structure_errors"]
    ), "缺章节必须被点名；实际是 %r" % (body["structure_errors"],)
    # 基线行仍在文档里 → 审核者在文档有问题时照样看得到被验证的是什么。
    assert body["baseline"] == EXPECTED_BASELINE


# ---------------------------------------------------------------------------
# 5. 已落盘的审核状态要出现在条目里
# ---------------------------------------------------------------------------


def test_items_carry_recorded_review_state(plan_dir: Path) -> None:
    """审过的条目带回状态与原因；没审的仍是 pending。"""
    _write_doc(plan_dir, CANONICAL_MD)
    record_action(plan_dir, 0, "accept")

    resp = client.get(f"/api/verification-plan/{plan_dir.name}/review/items")

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items[0]["status"] == "accepted"
    assert items[1]["status"] == "pending"
