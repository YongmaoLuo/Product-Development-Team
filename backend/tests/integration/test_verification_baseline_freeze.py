"""审核完备时的基线冻结：写入、等值校验与文档改写后的失效。

背景
----
基线是**机器事实**：它只有一个来源——「被验证对象」章节里那一行
``baseline:``。审核完备的那一刻，这一行被重新解析，``ref`` 被解析成
``commit_sha``，四个字段写进 ``PlanState.verification.baseline``；工作区与
门禁只消费这份记录，不再读文档。

为什么在审核完备时冻结
----------------------
审核是**逐条**的：每一条都被 accept 或带原因的 skip 处置过，审核才算完备。
完备的那一刻是唯一确定「这份文档被审过了」的时刻——早一刻还有条目没处置，
晚一刻文档就可能被改。基线因此冻结在这一刻，并且只冻结一次。

等值校验：冻结下来的基线是既成事实
----------------------------------
候选基线与**任何已记录的**基线逐字段比较，不一致就拒绝这次审核通过
（409 ``baseline_mismatch``），旧值不被覆盖。理由是「已记录」这三个字：
被审过的基线不是可以随时改写的草稿，它是一次批准的事实。

文档改写 → 旧基线失效
----------------------
基线是**为某一份文档**冻结的。文档被改写（``doc_sha256`` 变化）之后，旧基线
不再对应当前文档：它不得继续用于进入验证，且必须在下一次审核通过时按新文档
的基线行重写。这条由「基线记录了它被冻结时那份文档的字节哈希」实现——哈希
对不上，旧基线即失效。

测试数据
--------
真实 git 仓库（``refs/pull/7/head`` 指向第二个提交）与 canonical 验证计划书。
真实 git 是刻意的：``ref`` 的解析结果就是被冻结的事实之一，mock 掉它等于把
要测的东西测没了。审核动作走生产路径（HTTP 端点），冻结因此在「审核完备的
那次写入」上真实发生。
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
from plan_state import PlanState  # noqa: E402
from tests.unit.test_verification_plan_review import CANONICAL_MD  # noqa: E402
from verification_gate import verification_branch_ready  # noqa: E402
from verification_plan_review import doc_sha256  # noqa: E402

client = TestClient(server.app)

#: 验证分支的产物：计划书（相对路径）。
PLAN_DOC = "verification-plan.md"

#: canonical 文档的审核条目数：5 个计划级条目 + 2 个验证点条目。
ITEM_COUNT = 7


# ---------------------------------------------------------------------------
# 计划目录与文档
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir() -> Path:
    """一个空计划目录（还没有计划书）。

    ``server.PLANS_DIR`` 由 conftest 的 autouse fixture 重定向到 tmp_path，
    所以这里建的目录不会落到仓库里的 ``plans/`` 树上。
    """
    path = server.PLANS_DIR / "baseline-freeze-1"
    path.mkdir(parents=True)
    return path


def _doc(repo: Path, ref: str = "refs/pull/7/head") -> str:
    """canonical 文档，基线行指向给定的仓库与 ref。"""
    return CANONICAL_MD.replace(
        "baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7",
        f"baseline: repo={repo} ref={ref} pr=7",
    )


def _write_doc(plan_dir: Path, text: str) -> None:
    """按字节落盘计划书。

    审核状态是按**文件字节**算哈希的，文档必须先落盘再按字节读回。
    """
    (plan_dir / PLAN_DOC).write_bytes(text.encode("utf-8"))


def _doc_sha(plan_dir: Path) -> str:
    """当前计划书的字节哈希（审核者读到的那一版）。"""
    return doc_sha256((plan_dir / PLAN_DOC).read_bytes())


def _post(plan_dir: Path, index: int, **body: object):
    """对第 ``index`` 条提交一次审核动作。

    请求带上当前文档的哈希，等价于审核者**重新读过条目**再动手——文档改写
    之后必须这样：审核不能覆盖一份已经变过的文档。
    """
    body.setdefault("doc_sha256", _doc_sha(plan_dir))
    return client.post(
        f"/api/verification-plan/{plan_dir.name}/review/item/{index}",
        json=body,
    )


def _accept_all_but_last(plan_dir: Path) -> None:
    """把前 ``ITEM_COUNT - 1`` 条处置掉，只留最后一条 pending。"""
    for index in range(ITEM_COUNT - 1):
        resp = _post(plan_dir, index, action="accept")
        assert resp.status_code == 200, resp.text


def _baseline(plan_dir: Path):
    """计划状态里冻结的基线。"""
    return PlanState(plan_dir).get_verification_baseline()


# ---------------------------------------------------------------------------
# 1. 审核完备 → 冻结四字段
# ---------------------------------------------------------------------------


def test_freeze_writes_baseline_on_completion(plan_dir: Path, upstream_repo) -> None:
    """最后一条 accept 让审核完备 → 基线被冻结，四个字段齐了。"""
    _write_doc(plan_dir, _doc(upstream_repo.repo))
    _accept_all_but_last(plan_dir)

    resp = _post(plan_dir, ITEM_COUNT - 1, action="accept")

    assert resp.status_code == 200, resp.text
    assert resp.json()["resolved"] is True

    baseline = _baseline(plan_dir)
    assert baseline == {
        "repo_dir": str(upstream_repo.repo),
        "ref": "refs/pull/7/head",
        "commit_sha": upstream_repo.head_sha(),
        "pr_number": 7,
    }, "基线是四个字段：仓库、ref、冻结时的 commit、PR 号"


def test_baseline_not_written_before_review_is_complete(
    plan_dir: Path, upstream_repo
) -> None:
    """审核不完备时不冻结：基线只来自「审核完备的那次写入」。"""
    _write_doc(plan_dir, _doc(upstream_repo.repo))

    _accept_all_but_last(plan_dir)

    assert _baseline(plan_dir) is None, "还有一条 pending，基线不该被冻结"


# ---------------------------------------------------------------------------
# 2. 与已记录的基线不一致 → 409 baseline_mismatch，旧值不被覆盖
# ---------------------------------------------------------------------------


def test_baseline_mismatch_rejected(plan_dir: Path, upstream_repo) -> None:
    """已记录的基线与候选不一致 → 拒绝这次审核通过，旧值保持不变。"""
    _write_doc(plan_dir, _doc(upstream_repo.repo))
    recorded = {
        "repo_dir": str(upstream_repo.repo),
        "ref": "refs/pull/7/head",
        # 另一个 commit：同一份文档解出来的候选与它逐字段对不上。
        "commit_sha": upstream_repo.base_sha(),
        "pr_number": 7,
    }
    PlanState(plan_dir).set_verification_baseline(recorded)
    _accept_all_but_last(plan_dir)

    resp = _post(plan_dir, ITEM_COUNT - 1, action="accept")

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "baseline_mismatch", body
    assert "commit_sha" in body["detail"], "要说清是哪个字段对不上"

    assert _baseline(plan_dir) == recorded, "旧值不被覆盖"


# ---------------------------------------------------------------------------
# 3. 仓库不存在 / ref 解析不出来 → 409，审核不通过
# ---------------------------------------------------------------------------


def test_unresolvable_ref_blocks_approval(plan_dir: Path, upstream_repo) -> None:
    """ref 解析不出来 → 审核不通过（409），基线不写。"""
    _write_doc(plan_dir, _doc(upstream_repo.repo, ref="refs/pull/999/head"))
    _accept_all_but_last(plan_dir)

    resp = _post(plan_dir, ITEM_COUNT - 1, action="accept")

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "baseline_unresolvable", body
    assert _baseline(plan_dir) is None, "冻结不了就不写基线"


def test_missing_repository_blocks_approval(plan_dir: Path, tmp_path: Path) -> None:
    """仓库目录不存在 → 同样阻止审核通过（409）。"""
    _write_doc(plan_dir, _doc(tmp_path / "absent-repository"))
    _accept_all_but_last(plan_dir)

    resp = _post(plan_dir, ITEM_COUNT - 1, action="accept")

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "baseline_unresolvable"
    assert _baseline(plan_dir) is None


# ---------------------------------------------------------------------------
# 4. 文档改写 → 旧基线失效，门禁关闭
# ---------------------------------------------------------------------------


def test_doc_edit_invalidates_baseline(plan_dir: Path, upstream_repo) -> None:
    """审完之后文档被改 → 旧审核不再覆盖当前文档 → 门禁关闭。"""
    _write_doc(plan_dir, _doc(upstream_repo.repo))
    _accept_all_but_last(plan_dir)
    assert _post(plan_dir, ITEM_COUNT - 1, action="accept").status_code == 200

    assert verification_branch_ready(plan_dir) is True, "审完就该就绪"

    doc = plan_dir / PLAN_DOC
    doc.write_bytes(doc.read_bytes() + "\n追加一行\n".encode("utf-8"))

    assert verification_branch_ready(plan_dir) is False, (
        "文档一改，审核不再覆盖它，门禁随之关闭"
    )


def test_rewritten_document_can_freeze_a_new_baseline(
    plan_dir: Path, upstream_repo
) -> None:
    """文档改写后重新审完 → 基线按新文档的基线行重写，不被旧值挡住。"""
    _write_doc(plan_dir, _doc(upstream_repo.repo))
    _accept_all_but_last(plan_dir)
    assert _post(plan_dir, ITEM_COUNT - 1, action="accept").status_code == 200
    frozen = _baseline(plan_dir)
    assert frozen is not None

    # 改写文档：内容变了（哈希变了），基线行没变。
    doc = plan_dir / PLAN_DOC
    doc.write_bytes(doc.read_bytes() + "\n追加一行\n".encode("utf-8"))

    # 重新审一遍（改写后全部条目回到 pending）。
    _accept_all_but_last(plan_dir)
    resp = _post(plan_dir, ITEM_COUNT - 1, action="accept")

    assert resp.status_code == 200, (
        "旧基线已随文档改写失效，不该挡住重写：%s" % resp.text
    )
    assert _baseline(plan_dir) == frozen, "同一份基线行 → 同一个基线"


# ---------------------------------------------------------------------------
# 5. 计划状态的读写接口
# ---------------------------------------------------------------------------


def test_plan_state_baseline_round_trip(plan_dir: Path) -> None:
    """``set_verification_baseline`` 写进去的，``get_verification_baseline`` 读得回来。"""
    state = PlanState(plan_dir)
    assert state.get_verification_baseline() is None

    baseline = {
        "repo_dir": "/tmp/repo",
        "ref": "refs/pull/7/head",
        "commit_sha": "a" * 40,
        "pr_number": 7,
    }
    state.set_verification_baseline(baseline)

    assert PlanState(plan_dir).get_verification_baseline() == baseline
