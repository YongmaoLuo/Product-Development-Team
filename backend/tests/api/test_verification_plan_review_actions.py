"""计划书的审核动作端点。

背景
----
计划书的审核是**逐条**做的：每一条要么 accept，要么 skip（必须带原因），要么
reset / revise 退回待审。这个端点是那条链路的唯一写入口：
``POST /api/verification-plan/{plan_id}/review/item/{item_index}``。

为什么逐条
----------
一道门禁不是「这份文档整体通过 / 不通过」，而是「每一条都被处置过」：accept
过的、带原因 skip 过的都算数，pending 阻断。所以动作必须落在**条目**上，不能
落在文档上 —— 文档只有一个状态，条目有七个。

skip 必须带原因
--------------
没有原因的 skip 无法归因：报告里出现「未验证」时，得能说出「为什么没验」。所以
缺 note 的 skip 是 400 ``skip_note_required``，而且**不写盘** —— 被拒绝的请求不
落状态。

reset 与 revise 都退回待审，区别在 note
--------------------------------------
``reset`` 是「这条先不算数」，原因一并清空；``revise`` 是「这条要改」，退回待审
但**留着** note —— 那条 note 是后续 refine 要用的反馈，丢了就没人知道要改什么。

文档变了要重读
--------------
审核状态与文档**字节**绑定（``doc_sha256``）。文档被改过之后，之前那份审核针对
的是另一份文档，不能算数：请求带着的哈希对不上当前文档 → 409
``document_changed``，要求重读条目再操作。

测试数据
--------
canonical 文档与任务 18 的只读端点同源（十个章节按模板顺序排列、「被验证对象」
章节含唯一一行 ``baseline:``、「验证点清单」章节含两个 VP 块），因此条目是
5 个计划级 + 2 个验证点 = 7 条。文档一律先落盘再按字节读回 —— 哈希是对**文件
字节**算的。
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
from verification_plan_review import doc_sha256  # noqa: E402

client = TestClient(server.app)

#: 验证分支的产物：计划书（相对路径）。
PLAN_DOC = "verification-plan.md"

#: canonical 文档里的基线行。``/tmp/repo`` 是个**占位路径**，不是真的仓库。
_BASELINE_LINE = "baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7"

#: canonical 文档的字节哈希 —— 审核者「读到的那一版」。
CANONICAL_SHA = doc_sha256(CANONICAL_MD.encode("utf-8"))


def _doc(repo: Path) -> str:
    """canonical 文档，基线行指向给定的仓库。

    基线行里的仓库路径必须指向一个**真实存在**的仓库：审核完备的那一刻
    基线会被冻结，冻结要解析 ``ref``（``refs/pull/7/head``）—— 仓库不在，
    解析不出来，这次审核通过就被拒（``409 baseline_unresolvable``）。写死
    ``/tmp/repo`` 的那份文档只在宿主机上恰好有那个目录时才对：CI 上没有。
    """
    return CANONICAL_MD.replace(
        _BASELINE_LINE,
        f"baseline: repo={repo} ref=refs/pull/7/head pr=7",
    )


# ---------------------------------------------------------------------------
# 计划目录
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir() -> Path:
    """一个空计划目录（还没有计划书）。

    ``server.PLANS_DIR`` 由 conftest 的 autouse fixture 重定向到 tmp_path，
    所以这里建的目录不会落到仓库里的 ``plans/`` 树上。
    """
    path = server.PLANS_DIR / "vp-review-actions-1"
    path.mkdir(parents=True)
    return path


def _write_doc(plan_dir: Path, text: str) -> None:
    """按字节落盘计划书。

    审核状态是按**文件字节**算哈希的，所以文档必须先落盘再按字节读回 ——
    拼出来的近似文档测不出换行归一化这类问题。
    """
    (plan_dir / PLAN_DOC).write_bytes(text.encode("utf-8"))


def _post(plan_dir: Path, index: int, **body: object):
    """对第 ``index`` 条提交一次审核动作。"""
    return client.post(
        f"/api/verification-plan/{plan_dir.name}/review/item/{index}",
        json=body,
    )


# ---------------------------------------------------------------------------
# 1. accept / skip
# ---------------------------------------------------------------------------


def test_accept_and_skip_actions(plan_dir: Path, upstream_repo) -> None:
    """accept → accepted；skip（带原因）→ skipped + 原因留着。

    响应一次给全量条目：审核界面刷新一次就够，不必再拉一次。

    最后一条 accept 让审核**完备**，基线就在那一刻被冻结 —— 所以这一条
    需要一个真的 git 仓库（``upstream_repo``）：基线行里写死 ``/tmp/repo``
    的文档在宿主机上没有那个目录，冻结会失败（``409
    baseline_unresolvable``）。
    """
    doc = _doc(upstream_repo.repo)
    _write_doc(plan_dir, doc)
    doc_sha = doc_sha256(doc.encode("utf-8"))

    resp = _post(plan_dir, 0, action="accept")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "doc_sha256", "resolved"}, (
        "响应形状固定三键；实际是 %r" % (sorted(body),)
    )
    assert body["doc_sha256"] == doc_sha
    assert body["resolved"] is False, "还有 6 条没处置，不算审完"
    assert body["items"][0]["status"] == "accepted"
    assert body["items"][1]["status"] == "pending"

    resp = _post(plan_dir, 1, action="skip", note="本机无对应服务")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"][1]["status"] == "skipped"
    assert body["items"][1]["note"] == "本机无对应服务", "skip 的原因必须留着"
    assert body["items"][0]["status"] == "accepted", "上一条的状态不受影响"
    assert body["resolved"] is False, "还有 5 条没处置"

    # 全部处置过 → 这份审核算完备（skip 也算数：它带着原因）。
    for index in range(2, 7):
        resp = _post(plan_dir, index, action="accept")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resolved"] is True, "七条都处置过了，这份审核算完备"
    assert all(
        item["status"] in {"accepted", "skipped"} for item in body["items"]
    )


# ---------------------------------------------------------------------------
# 2. skip 缺原因 → 400，且不写盘
# ---------------------------------------------------------------------------


def test_skip_without_note_rejected(plan_dir: Path) -> None:
    """skip 没带原因 → 400 ``skip_note_required``，条目仍是 pending。"""
    _write_doc(plan_dir, CANONICAL_MD)

    resp = _post(plan_dir, 0, action="skip")

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "skip_note_required"

    # 被拒绝的请求不写盘：条目仍是 pending。
    items = client.get(
        f"/api/verification-plan/{plan_dir.name}/review/items"
    ).json()["items"]
    assert items[0]["status"] == "pending"
    assert items[0]["note"] == ""


# ---------------------------------------------------------------------------
# 3. reset / revise → 退回待审
# ---------------------------------------------------------------------------


def test_reset_and_revise_return_to_pending(plan_dir: Path) -> None:
    """reset 清空原因退回 pending；revise 退回 pending 但留着 refine 反馈。"""
    _write_doc(plan_dir, CANONICAL_MD)
    assert _post(plan_dir, 0, action="accept").status_code == 200
    assert (
        _post(plan_dir, 1, action="skip", note="本机无对应服务").status_code == 200
    )

    resp = _post(plan_dir, 0, action="reset")

    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["status"] == "pending", "reset 之后这条不算数了"
    assert item["note"] == "", "reset 把原因一并清掉"

    resp = _post(plan_dir, 1, action="revise", note="把基线写清楚")

    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][1]
    assert item["status"] == "pending", "revise 之后这条要重新审"
    assert item["note"] == "把基线写清楚", "revise 的 note 是 refine 反馈，不能丢"


# ---------------------------------------------------------------------------
# 4. 文档已变 → 409 document_changed
# ---------------------------------------------------------------------------


def test_stale_hash_returns_409(plan_dir: Path) -> None:
    """审核者读到的那一版已经不是当前这一版 → 409，要求重读条目。"""
    _write_doc(plan_dir, CANONICAL_MD)

    changed = CANONICAL_MD + "\n追加一行\n"
    _write_doc(plan_dir, changed)

    resp = _post(plan_dir, 0, action="accept", doc_sha256=CANONICAL_SHA)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "document_changed"
    assert body["doc_sha256"] == doc_sha256(changed.encode("utf-8")), (
        "409 要把当前这一版的哈希给回去，客户端才知道该带什么重试"
    )

    # 不带哈希时靠已落盘的审核状态判陈旧：先记一次动作，文档再变。
    _write_doc(plan_dir, CANONICAL_MD)
    assert _post(plan_dir, 0, action="accept").status_code == 200
    _write_doc(plan_dir, changed)

    resp = _post(plan_dir, 1, action="accept")

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "document_changed"


# ---------------------------------------------------------------------------
# 5. 边界：未知动作 / 越界序号
# ---------------------------------------------------------------------------


def test_unknown_action_and_out_of_range_index(plan_dir: Path) -> None:
    """动作不认识 → 400；条目序号不存在 → 404。两条都不写盘。"""
    _write_doc(plan_dir, CANONICAL_MD)

    resp = _post(plan_dir, 0, action="approve")

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "unknown_action"

    resp = _post(plan_dir, 99, action="accept")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "item_not_found"

    items = client.get(
        f"/api/verification-plan/{plan_dir.name}/review/items"
    ).json()["items"]
    assert all(item["status"] == "pending" for item in items), (
        "被拒绝的请求不写盘"
    )
