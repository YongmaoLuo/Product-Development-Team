"""验证计划书的审核条目、独立审核存储与完备性判定。

背景
----
验证计划书有自己的一份审核状态，**不复用** PRD 的 ``review.json``：两份文档
各有各的条目与各自的完备性，塞进同一个文件就没法区分「PRD 审完了」与「计划书
审完了」。审核状态落在 ``verification-plan-review.json``，与
:mod:`verification_plan_doc` 的文档解析彻底分层。

判据
----
本文件钉住五件事：

* 条目提取：canonical 文档 → 5 个计划级条目 + 2 个验证点条目，id 稳定；
* 独立存储：审核文件叫 ``verification-plan-review.json``，不碰 ``review.json``；
* 哈希失效：文档**任意一个字节**变化 → 全部条目回到 ``pending``（审核不能
  覆盖一份已经变过的文档）；
* skip 语义：``skipped`` 必须带非空原因，否则 ``ValueError``；
* 分支相关的 PRD 判定：``skipped`` 只在验证分支算「已解决」，开发分支仍只认
  ``accepted``。

测试数据
--------
canonical 文档与任务 2 的结构校验语料同源：十个章节按模板顺序排列，「被验证对象」
章节含对象描述与唯一一行 ``baseline:``，「验证点清单」章节含两个 VP 块。文档一律
先落盘再按字节读回 —— 哈希是对**文件字节**算的，用字符串拼出来的近似文档测不出
换行归一化这类问题。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_plan_doc import parse_plan_doc  # noqa: E402
from verification_plan_review import (  # noqa: E402
    REVIEW_FILENAME,
    extract_items,
    is_resolved,
    load,
    prd_is_resolved,
    record_action,
)

#: 一份合法的计划书：十个章节按模板顺序排列，两个 VP 块。
CANONICAL_MD = """\
# 验证计划书

本计划书是对被验证对象的人工审核对象，也是机器格式翻译的唯一来源。

## 被验证对象

被验证对象为 product-development-team 仓库 PR #7 的 head，含配额等待的重构。
baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7

## 验证范围

- 配额等待按真实重开时刻睡
- 双档配额耗尽时取最晚重开的窗口

## 排除范围

- 前端交互（本次未改动前端）
- 与配额无关的执行器行为

## 验证方法

- 以单元测试为主，必要时做一次手工核验

## 验证点清单

### VP-001 配额按真实重开时刻睡眠

- 范围：backend/verification_loop.py 的配额等待路径
- 验证方法：单元测试
- 通过判据：
  - C1: 配额耗尽时睡到真实重开时刻
  - C2: 双档耗尽时取最晚重开的窗口
- 证据要求：
  - E1: tests/unit/test_quota_wait.py 的 pytest 输出
- 预期结果：两条判据全部满足，无超时

### VP-002 报告把 skip 条目标注为未验证

- 范围：backend/verification_report.py
- 验证方法：单元测试
- 通过判据：
  - C1: skip 条目显示为「未验证」
- 证据要求：
  - E1: tests/unit/test_report_render.py 的 pytest 输出
- 预期结果：渲染结果与期望一致

## 预期结果

- 全部验证点通过，未发现需要修复的缺陷

## 证据要求

- 每个验证点给出可复跑的命令与原始输出

## 通过判据

- 所有验证点的 C 编号判据全部满足

## 环境与依赖

- Python 3.11 与 pytest 可用

## 风险面

- 上游 PR head 可能移动，导致基线漂移
"""

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
# 工具
# ---------------------------------------------------------------------------


def _plan_dir(tmp_path: Path, *, doc: str | None = CANONICAL_MD) -> Path:
    """建一个 plan 目录，按字节落盘计划书。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    if doc is not None:
        (plan_dir / "verification-plan.md").write_bytes(doc.encode("utf-8"))
    return plan_dir


def _write_prd_review(plan_dir: Path, items: list[dict]) -> None:
    """写 PRD 的 ``review.json``（与计划书审核文件是两个文件）。"""
    (plan_dir / "review.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False),
        encoding="utf-8",
    )


def _accept_all(plan_dir: Path) -> None:
    """把全部条目标记为 accepted。"""
    for index in range(len(EXPECTED_IDS)):
        record_action(plan_dir, index, "accept")


# ---------------------------------------------------------------------------
# 1. 条目提取
# ---------------------------------------------------------------------------


def test_extract_five_plan_items_and_vps(tmp_path: Path) -> None:
    """canonical 文档 → 5 个计划级条目 + 2 个 VP 条目，id 稳定。"""
    doc = (tmp_path / "verification-plan.md")
    doc.write_bytes(CANONICAL_MD.encode("utf-8"))

    parsed = parse_plan_doc(doc.read_bytes().decode("utf-8"))
    items = extract_items(parsed)

    assert [item.id for item in items] == EXPECTED_IDS
    assert [item.index for item in items] == list(range(7))
    assert [item.title for item in items][:5] == [
        "被验证对象",
        "排除范围",
        "验证方法",
        "环境与依赖",
        "风险面",
    ]
    # 验证点条目带文档里的标题
    assert items[5].title == "配额按真实重开时刻睡眠"
    assert items[6].title == "报告把 skip 条目标注为未验证"
    # 新提取的条目一律 pending，且没有原因
    assert all(item.status == "pending" for item in items)
    assert all(item.note == "" for item in items)


def test_item_shape_is_fixed_to_five_keys(tmp_path: Path) -> None:
    """条目形状固定为 ``{index, id, title, status, note}``。"""
    doc = tmp_path / "verification-plan.md"
    doc.write_bytes(CANONICAL_MD.encode("utf-8"))
    parsed = parse_plan_doc(doc.read_bytes().decode("utf-8"))

    item = extract_items(parsed)[0]

    assert item.to_dict() == {
        "index": 0,
        "id": "PLAN-OBJECT",
        "title": "被验证对象",
        "status": "pending",
        "note": "",
    }
    # 属性访问与下标访问都成立：条目既要能直接序列化，也要能当字典用
    assert item["id"] == "PLAN-OBJECT" == item.id
    assert dict(item) == item.to_dict()


# ---------------------------------------------------------------------------
# 2. 独立审核存储
# ---------------------------------------------------------------------------


def test_review_file_is_separate_from_prd_review(tmp_path: Path) -> None:
    """审核状态写 ``verification-plan-review.json``，不碰 PRD 的 ``review.json``。"""
    plan_dir = _plan_dir(tmp_path)
    _write_prd_review(plan_dir, [{"index": 0, "title": "DP-0", "status": "accepted", "note": ""}])

    state = record_action(plan_dir, 0, "accept")

    assert REVIEW_FILENAME == "verification-plan-review.json"
    assert (plan_dir / REVIEW_FILENAME).exists()
    # PRD 的审核文件一个字节都没动
    prd_review = json.loads((plan_dir / "review.json").read_text(encoding="utf-8"))
    assert prd_review["items"] == [
        {"index": 0, "title": "DP-0", "status": "accepted", "note": ""}
    ]
    # 计划书审核文件带文档哈希
    stored = json.loads((plan_dir / REVIEW_FILENAME).read_text(encoding="utf-8"))
    assert stored["doc_sha256"] == state.doc_sha256
    assert len(stored["doc_sha256"]) == 64


def test_load_returns_none_without_review_file(tmp_path: Path) -> None:
    """没有审核文件 → 没有状态（不是「全部 pending 的空状态」）。"""
    plan_dir = _plan_dir(tmp_path)

    assert load(plan_dir) is None


def test_record_action_without_review_file_creates_it(tmp_path: Path) -> None:
    """第一次审核动作就落盘，且条目来自当前文档。"""
    plan_dir = _plan_dir(tmp_path)

    state = record_action(plan_dir, 0, "accept")

    assert len(state.items) == 7
    assert state.items[0].status == "accepted"
    assert [item.status for item in state.items[1:]] == ["pending"] * 6
    assert (plan_dir / REVIEW_FILENAME).exists()


# ---------------------------------------------------------------------------
# 3. 哈希失效
# ---------------------------------------------------------------------------


def test_hash_change_resets_all_items(tmp_path: Path) -> None:
    """文档任意字节变化 → 全部条目回到 pending。"""
    plan_dir = _plan_dir(tmp_path)
    _accept_all(plan_dir)

    state = load(plan_dir)
    assert state is not None
    assert all(item.status == "accepted" for item in state.items)

    # 只改一个字节：把「风险面」那段的一个字换掉
    doc = plan_dir / "verification-plan.md"
    doc.write_bytes(doc.read_bytes().replace("基线漂移".encode("utf-8"), "基线漂移!".encode("utf-8")))

    after = load(plan_dir)
    assert after is not None
    assert [item.status for item in after.items] == ["pending"] * 7
    assert [item.note for item in after.items] == [""] * 7


def test_line_ending_change_resets_all_items(tmp_path: Path) -> None:
    """CRLF 与 LF 的差别也是字节差别 —— 哈希不做换行归一化。"""
    plan_dir = _plan_dir(tmp_path, doc=CANONICAL_MD.replace("\n", "\r\n"))
    _accept_all(plan_dir)

    doc = plan_dir / "verification-plan.md"
    doc.write_bytes(doc.read_bytes().replace(b"\r\n", b"\n"))

    after = load(plan_dir)
    assert after is not None
    assert [item.status for item in after.items] == ["pending"] * 7


def test_recording_after_document_change_keeps_other_items_pending(tmp_path: Path) -> None:
    """文档变了之后落一次动作：其余条目不会带着旧决定复活。"""
    plan_dir = _plan_dir(tmp_path)
    _accept_all(plan_dir)

    doc = plan_dir / "verification-plan.md"
    doc.write_bytes(doc.read_bytes().replace(b"pr=7", b"pr=8"))

    state = record_action(plan_dir, 0, "accept")

    assert state.items[0].status == "accepted"
    assert [item.status for item in state.items[1:]] == ["pending"] * 6


def test_is_resolved_is_false_after_document_change(tmp_path: Path) -> None:
    """持有旧状态对象时，哈希对不上照样不算完备。"""
    plan_dir = _plan_dir(tmp_path)
    _accept_all(plan_dir)

    stale = load(plan_dir)
    assert stale is not None
    assert is_resolved(stale, CANONICAL_MD) is True

    doc = plan_dir / "verification-plan.md"
    changed = doc.read_bytes().replace(b"pr=7", b"pr=8")
    doc.write_bytes(changed)

    assert is_resolved(stale, changed.decode("utf-8")) is False


# ---------------------------------------------------------------------------
# 4. 完备性判定
# ---------------------------------------------------------------------------


def test_is_resolved_requires_every_item_and_matching_hash(tmp_path: Path) -> None:
    """pending 阻断；accepted / skipped 且哈希一致才算完备。"""
    plan_dir = _plan_dir(tmp_path)

    pending_state = load(plan_dir)
    assert pending_state is None  # 还没审过

    state = record_action(plan_dir, 0, "accept")
    assert is_resolved(state, CANONICAL_MD) is False

    for index in range(1, 7):
        state = record_action(plan_dir, index, "accept")
    assert is_resolved(state, CANONICAL_MD) is True

    # skip 也算已解决
    skipped = record_action(plan_dir, 3, "skip", note="本次不验环境")
    assert skipped.items[3].status == "skipped"
    assert is_resolved(skipped, CANONICAL_MD) is True


def test_skip_requires_note(tmp_path: Path) -> None:
    """skip 没有非空原因 → ValueError，条目状态不变。"""
    plan_dir = _plan_dir(tmp_path)

    with pytest.raises(ValueError):
        record_action(plan_dir, 0, "skip")

    with pytest.raises(ValueError):
        record_action(plan_dir, 0, "skip", note="   ")

    state = record_action(plan_dir, 0, "skip", note="本次不验")
    assert state.items[0].status == "skipped"
    assert state.items[0].note == "本次不验"


def test_unknown_action_is_rejected(tmp_path: Path) -> None:
    """未知动作 → ValueError，不落盘。"""
    plan_dir = _plan_dir(tmp_path)

    with pytest.raises(ValueError):
        record_action(plan_dir, 0, "revise")

    assert not (plan_dir / REVIEW_FILENAME).exists()


def test_reset_action_clears_status_and_note(tmp_path: Path) -> None:
    """reset 回到 pending，原因一并清掉。"""
    plan_dir = _plan_dir(tmp_path)

    state = record_action(plan_dir, 0, "skip", note="先跳过")
    assert state.items[0].status == "skipped"

    state = record_action(plan_dir, 0, "reset")
    assert state.items[0].status == "pending"
    assert state.items[0].note == ""


def test_record_action_on_missing_document_raises(tmp_path: Path) -> None:
    """没有计划书就没有可审核的对象。"""
    plan_dir = _plan_dir(tmp_path, doc=None)

    with pytest.raises(FileNotFoundError):
        record_action(plan_dir, 0, "accept")


# ---------------------------------------------------------------------------
# 5. PRD 审核的分支相关判定
# ---------------------------------------------------------------------------


def test_prd_skip_counts_resolved_only_for_verification_branch(tmp_path: Path) -> None:
    """验证分支：skipped 算已解决；开发分支：仍只认 accepted。"""
    skipped_items = [
        {"index": 0, "title": "DP-0", "status": "skipped", "note": "本次不适用"},
        {"index": 1, "title": "DP-1", "status": "accepted", "note": ""},
    ]

    verification_dir = _plan_dir(tmp_path / "verification")
    _write_prd_review(verification_dir, skipped_items)
    assert prd_is_resolved(verification_dir) is True

    dev_dir = tmp_path / "dev"
    dev_dir.mkdir()
    (dev_dir / "tasks.json").write_text("{}", encoding="utf-8")
    _write_prd_review(dev_dir, skipped_items)
    assert prd_is_resolved(dev_dir) is False


def test_prd_is_resolved_false_without_review_file(tmp_path: Path) -> None:
    """没有 PRD 审核文件 → 没审过 → 不算已解决。"""
    plan_dir = _plan_dir(tmp_path)

    assert prd_is_resolved(plan_dir) is False


def test_prd_is_resolved_requires_every_item(tmp_path: Path) -> None:
    """还有 pending 条目 → 不算已解决。"""
    plan_dir = _plan_dir(tmp_path)
    _write_prd_review(
        plan_dir,
        [
            {"index": 0, "title": "DP-0", "status": "accepted", "note": ""},
            {"index": 1, "title": "DP-1", "status": "pending", "note": ""},
        ],
    )

    assert prd_is_resolved(plan_dir) is False


def test_prd_all_accepted_is_resolved_on_both_branches(tmp_path: Path) -> None:
    """全部 accepted：验证分支与开发分支都算已解决（开发分支行为不变）。"""
    items = [{"index": 0, "title": "DP-0", "status": "accepted", "note": ""}]

    verification_dir = _plan_dir(tmp_path / "verification")
    _write_prd_review(verification_dir, items)
    assert prd_is_resolved(verification_dir) is True

    dev_dir = tmp_path / "dev"
    dev_dir.mkdir()
    (dev_dir / "tasks.json").write_text("{}", encoding="utf-8")
    _write_prd_review(dev_dir, items)
    assert prd_is_resolved(dev_dir) is True


def test_record_action_rejects_unknown_item_index(tmp_path: Path) -> None:
    """条目序号不存在 → ValueError，不落盘。"""
    plan_dir = _plan_dir(tmp_path)

    with pytest.raises(ValueError):
        record_action(plan_dir, 99, "accept")

    assert not (plan_dir / REVIEW_FILENAME).exists()
