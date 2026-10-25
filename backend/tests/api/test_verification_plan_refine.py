"""计划书的改写端点：``POST /api/verification-plan/{plan_id}/refine``。

背景
----
审核者对计划书提的反馈要**改写整篇文档**，而不是改一个字段。改写走这个端点：
``POST /api/verification-plan/{plan_id}/refine``。

改写让旧审核失效
----------------
审核状态与文档**字节**绑定。文档一改，字节就变了，之前的审核针对的是另一份
文档 —— 所以改写之后**全部条目回到 pending**，一条都不算数。这不是保守，是
必需：让旧审核继续覆盖新内容，等于允许「审的是 A、跑的是 B」。

基线随之失效，门禁关闭
----------------------
基线是**为某一份文档**冻结的（它记着自己被冻结时那份文档的字节哈希）。改写
之后哈希对不上，旧基线不再有效，于是验证门禁（``verification_branch_ready``）
关闭：改写过的计划书必须重新审一遍才能开始验证。

先校验后落盘
------------
模型给的改写结果不满足十章节契约时 → 422，**旧文档原样保留**。非法文档落到
磁盘上只会变成下一个读它的人踩的坑。

反馈不能为空
------------
空反馈不值得花一次模型调用：400 ``feedback_required``，模型一次都没被问过，
文档一个字节都没动。

测试数据
--------
canonical 文档与任务 18/19 同源（十个章节按模板顺序排列、「被验证对象」章节含
唯一一行 ``baseline:``、「验证点清单」章节含两个 VP 块），因此条目是
5 个计划级 + 2 个验证点 = 7 条。改写后的文档内容变了，但**验证点 id 与基线行
不变** —— 改写不改验证点的集合，也不改基线。模型是替身：每次被问都返回同一份
文档，所以本文件不碰任何 provider，也不联网。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from plan_state import PlanState  # noqa: E402
from tests.unit.test_verification_plan_review import CANONICAL_MD  # noqa: E402
from verification_gate import verification_branch_ready  # noqa: E402
from verification_plan_review import doc_sha256  # noqa: E402

client = TestClient(server.app)

#: 验证分支的产物：计划书（相对路径）。
PLAN_DOC = "verification-plan.md"

#: 审核状态文件（计划书自己的那一份，与 PRD 的 ``review.json`` 不是同一个）。
REVIEW_FILE = "verification-plan-review.json"

#: canonical 文档的审核条目数：5 个计划级条目 + 2 个验证点条目。
ITEM_COUNT = 7

#: 审核者的反馈。
FEEDBACK = "补充并发场景的证据要求"


# ---------------------------------------------------------------------------
# 脚本化的改写工具
# ---------------------------------------------------------------------------


class _ScriptedTool:
    """改写工具的替身：每次被问都返回同一份文档。

    ``prompts`` 留着被问过的提示词 —— 「被拒绝的请求不许调用模型」这条契约
    就是拿它来断的。
    """

    def __init__(self, document: str = "") -> None:
        self.document = document
        self.prompts: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.document


@pytest.fixture
def scripted_tool(monkeypatch: pytest.MonkeyPatch) -> _ScriptedTool:
    """把 ``server.create_coding_tool`` 换成脚本化替身。"""
    tool = _ScriptedTool()
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: tool)
    return tool


# ---------------------------------------------------------------------------
# 计划目录与文档
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir() -> Path:
    """一个空计划目录（还没有计划书）。

    ``server.PLANS_DIR`` 由 conftest 的 autouse fixture 重定向到 tmp_path，
    所以这里建的目录不会落到仓库里的 ``plans/`` 树上。
    """
    path = server.PLANS_DIR / "vp-refine-1"
    path.mkdir(parents=True)
    return path


def _write_doc(plan_dir: Path, text: str) -> None:
    """按字节落盘计划书。

    审核状态是按**文件字节**算哈希的，所以文档必须先落盘再按字节读回。
    """
    (plan_dir / PLAN_DOC).write_bytes(text.encode("utf-8"))


def _refined_doc(base: str = CANONICAL_MD) -> str:
    """改写后的文档：内容变了，验证点 id 与基线行不变。

    反馈是「补充并发场景的证据要求」，所以「验证方法」这一节多一条。改写不改
    验证点的集合（VP-001 / VP-002），也不动基线行。
    """
    old = "- 以单元测试为主，必要时做一次手工核验"
    assert old in base, "canonical 文档里没有「验证方法」这一行"
    return base.replace(
        old,
        old + "\n- 并发场景：两个验证点同时跑，证据里要带双方的输出",
    )


def _invalid_doc(base: str = CANONICAL_MD) -> str:
    """砍掉「风险面」这一节 —— 十章节契约少一节。"""
    head, marker, _ = base.partition("## 风险面")
    assert marker, "canonical 文档里没有「风险面」这一节"
    return head


#: 塞进「验证点清单」章节的一个新验证点块（结构本身合法）。
NEW_VP_BLOCK = """
### VP-003 模型自己加的验证点

- 范围：模型自己加的
- 验证方法：单元测试
- 通过判据：
  - C1: 模型自己加的判据
- 证据要求：
  - E1: 模型自己加的证据
- 预期结果：模型自己加的预期
"""


def _doc_with_extra_vp(base: str = CANONICAL_MD) -> str:
    """多了一个验证点的文档（结构合法，验证点集合变了）。"""
    marker = "\n## 预期结果\n"
    assert marker in base, "canonical 文档里没有「预期结果」这一节"
    return base.replace(marker, NEW_VP_BLOCK + marker)


def _post_refine(plan_dir: Path, feedback: object = FEEDBACK):
    """提交一次改写。"""
    return client.post(
        f"/api/verification-plan/{plan_dir.name}/refine",
        json={"feedback": feedback},
    )


def _post_review(plan_dir: Path, index: int, **body: object):
    """对第 ``index`` 条提交一次审核动作（走生产端点）。"""
    body.setdefault(
        "doc_sha256", doc_sha256((plan_dir / PLAN_DOC).read_bytes())
    )
    return client.post(
        f"/api/verification-plan/{plan_dir.name}/review/item/{index}",
        json=body,
    )


def _stored_review(plan_dir: Path) -> dict:
    """已落盘的审核状态。"""
    path = plan_dir / REVIEW_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. 改写成功 → 全部条目回到 pending
# ---------------------------------------------------------------------------


def test_refine_rewrites_and_resets_items(
    plan_dir: Path, scripted_tool: _ScriptedTool
) -> None:
    """文档被改写 → 旧审核不再算数 → 全部条目回到 pending。"""
    _write_doc(plan_dir, CANONICAL_MD)
    # 先处置两条，制造「已经审过」的状态。
    assert _post_review(plan_dir, 0, action="accept").status_code == 200
    assert (
        _post_review(plan_dir, 1, action="skip", note="本机无对应服务")
        .status_code
        == 200
    )

    scripted_tool.document = _refined_doc()

    resp = _post_refine(plan_dir)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document_path"] == PLAN_DOC, (
        "文档路径必须是**相对路径**；实际是 %r" % body["document_path"]
    )
    expected_sha = doc_sha256(_refined_doc().encode("utf-8"))
    assert body["doc_sha256"] == expected_sha, "响应给的是新文档那一版的哈希"

    assert len(body["items"]) == ITEM_COUNT
    assert all(item["status"] == "pending" for item in body["items"]), (
        "文档一改，之前的审核针对的是另一份文档，一条都不算数"
    )
    assert all(item["note"] == "" for item in body["items"]), (
        "旧原因针对的是旧文档，不能留在新条目上"
    )

    # 新文档落盘，旧文档没了。
    assert (plan_dir / PLAN_DOC).read_bytes() == _refined_doc().encode("utf-8")

    # 落盘的审核状态也退回待审，并绑定新文档的哈希。
    stored = _stored_review(plan_dir)
    assert stored["doc_sha256"] == expected_sha
    assert all(item["status"] == "pending" for item in stored["items"])

    # 反馈被带进了提示词。
    assert scripted_tool.prompts, "改写要问一次模型"
    assert FEEDBACK in scripted_tool.prompts[0]


# ---------------------------------------------------------------------------
# 2. 改写结果非法 → 422，旧文档原样保留
# ---------------------------------------------------------------------------


def test_refine_invalid_doc_keeps_old_file(
    plan_dir: Path, scripted_tool: _ScriptedTool
) -> None:
    """模型给的改写缺一节 → 422，磁盘上仍是旧文档。"""
    _write_doc(plan_dir, CANONICAL_MD)
    before = (plan_dir / PLAN_DOC).read_bytes()
    scripted_tool.document = _invalid_doc()

    resp = _post_refine(plan_dir)

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "verification_plan_invalid"
    assert body["errors"], "必须逐条给出违规"

    assert (plan_dir / PLAN_DOC).read_bytes() == before, (
        "非法改写不许落盘：旧文档必须原样保留"
    )


# ---------------------------------------------------------------------------
# 2b. 改写新增了验证点 → 422，旧文档原样保留
# ---------------------------------------------------------------------------


def test_refine_cannot_add_vp_ids(plan_dir: Path, scripted_tool: _ScriptedTool) -> None:
    """改写不得新增验证点 —— 审核条目按 id 归因，少一条验证点是静默的。

    这份文档**结构合法**（十章节齐、编号连续），所以拦下它的不是结构校验，
    而是改写特有的那条约束：验证点 id 集合不变。
    """
    _write_doc(plan_dir, CANONICAL_MD)
    before = (plan_dir / PLAN_DOC).read_bytes()
    scripted_tool.document = _doc_with_extra_vp()

    resp = _post_refine(plan_dir)

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "verification_plan_invalid"
    codes = [error["code"] for error in body["errors"]]
    assert "vp_ids_changed" in codes, "要说清是验证点集合变了：%r" % (codes,)

    assert (plan_dir / PLAN_DOC).read_bytes() == before, (
        "验证点集合变了就不该落盘：旧文档必须原样保留"
    )


# ---------------------------------------------------------------------------
# 3. 反馈为空 → 400，且不调用模型
# ---------------------------------------------------------------------------


def test_empty_feedback_rejected(
    plan_dir: Path, scripted_tool: _ScriptedTool
) -> None:
    """反馈是空白 → 400，文档一个字节都没动，模型一次都没被问过。"""
    _write_doc(plan_dir, CANONICAL_MD)
    before = (plan_dir / PLAN_DOC).read_bytes()

    resp = _post_refine(plan_dir, feedback="   ")

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "feedback_required"

    assert (plan_dir / PLAN_DOC).read_bytes() == before
    assert scripted_tool.prompts == [], "空反馈不值得花一次模型调用"

    # 完全没有反馈字段，同样被拒绝。
    resp = _post_refine(plan_dir, feedback=None)
    assert resp.status_code == 400, resp.text
    assert scripted_tool.prompts == []


# ---------------------------------------------------------------------------
# 4. 改写 → 基线失效，门禁关闭
# ---------------------------------------------------------------------------


def test_refine_invalidates_baseline(
    plan_dir: Path, scripted_tool: _ScriptedTool, upstream_repo
) -> None:
    """审完之后改写 → 旧基线不再有效 → 验证门禁关闭。"""
    doc = CANONICAL_MD.replace(
        "baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7",
        f"baseline: repo={upstream_repo.repo} ref=refs/pull/7/head pr=7",
    )
    _write_doc(plan_dir, doc)

    # 审完全部条目 → 基线被冻结（真实 git 仓库，ref 解析得出来）。
    for index in range(ITEM_COUNT):
        assert _post_review(plan_dir, index, action="accept").status_code == 200

    assert PlanState(plan_dir).get_verification_baseline() is not None, (
        "审完了，基线该被冻下来"
    )
    assert verification_branch_ready(plan_dir) is True, "审完就该就绪"

    scripted_tool.document = _refined_doc(doc)

    resp = _post_refine(plan_dir)

    assert resp.status_code == 200, resp.text
    assert PlanState(plan_dir).get_verification_baseline() is None, (
        "基线是为某一份文档冻结的：文档一改，旧基线不再有效"
    )
    assert verification_branch_ready(plan_dir) is False, (
        "改写之后必须重新审一遍才能开始验证"
    )


# ---------------------------------------------------------------------------
# 5. 边界：计划不存在 / 计划书不存在 → 404
# ---------------------------------------------------------------------------


def test_unknown_plan_and_missing_document_return_404(
    plan_dir: Path, scripted_tool: _ScriptedTool
) -> None:
    """计划不在 → 404；计划在但计划书不在 → 404。两条都不调用模型。"""
    resp = client.post(
        "/api/verification-plan/there-is-no-such-plan/refine",
        json={"feedback": FEEDBACK},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "plan_not_found"

    resp = _post_refine(plan_dir)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "document_not_found"

    assert scripted_tool.prompts == []
