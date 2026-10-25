"""计划书 → 机器格式：审核门禁的**等价性校验**。

背景
----
``verification-plan.md`` 是人工审核的唯一对象，``verification_plan.json`` 是执行
判据的唯一依据。审核门禁的全部价值建立在"执行的就是审过的"之上 —— 所以两份
产物必须**机械可比对**，而不是靠人眼再扫一遍。

本文件钉住两个维度：

* **points** —— 验证点集合：机器格式多一条、少一条、改了名，都是漂移；
* **evidence** —— 证据编号：机器格式必须引用计划书声明过的**每一个** E 编号。

两条边界：

* **顺序不算差异** —— 验证点换顺序、E 编号换顺序，都不产生差异（集合相等）；
* **重复 id 是失败** —— 同一批验证点里出现两个同 id 的条目，比对本身没有意义。

形状
----
机器格式里每条验证点的 ``expected_result`` 以**规范化引用串**开头：证据编号用
逗号分隔，紧跟一个 ``" | "``，之后才是预期结果正文::

    "E1,E2 | 两条判据全部满足，无超时"

编号的顺序不算内容（集合相等即可），但正文之前必须有这个前缀 —— 前缀不在，
这条验证点就等于"一条证据都没引用"。

测试数据
--------
语料是本文件里的一份合法计划书（十章节、两个验证点），先落盘再按字节读回，
交给 :func:`verification_plan_doc.parse_plan_doc` 解析 —— 校验器消费的是真实
文件字节与真实解析产物。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_plan_doc  # noqa: E402
import verification_plan_translator as translator  # noqa: E402
from verification_plan_doc import parse_plan_doc  # noqa: E402

# ---------------------------------------------------------------------------
# 语料
# ---------------------------------------------------------------------------

#: 一份合法的计划书：两个验证点。
#:
#: VP-001 声明了两条证据（E1、E2），VP-002 声明了一条（E1）—— 证据集合不一样
#: 的两个验证点，才能把"少引一条 E"这件事测出来。
CANONICAL_MD = """\
# 验证计划书

本计划书是对被验证对象的人工审核对象，也是机器格式翻译的唯一来源。

## 被验证对象

被验证对象为示例仓库 PR #7 的 head，含配额等待的重构。
baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7

## 验证范围

- 配额等待按真实重开时刻睡

## 排除范围

- 前端交互（本次未改动前端）

## 验证方法

- 以接口验收与浏览器实测为主

## 验证点清单

### VP-001 配额按真实重开时刻睡眠

- 范围：backend/verification_loop.py 的配额等待路径
- 验证方法：api_test
- 通过判据：
  - C1: 配额耗尽时睡到真实重开时刻
  - C2: 双档耗尽时取最晚重开的窗口
- 证据要求：
  - E1: tests/unit/test_quota_wait.py 的 pytest 输出
  - E2: tests/unit/test_quota_window.py 的 pytest 输出
- 预期结果：两条判据全部满足，无超时

### VP-002 报告把 skip 条目标注为未验证

- 范围：backend/verification_report.py
- 验证方法：ui_validation
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

#: 语料里两个验证点的标题。
TITLE_VP_001 = "配额按真实重开时刻睡眠"
TITLE_VP_002 = "报告把 skip 条目标注为未验证"


# ---------------------------------------------------------------------------
# 便捷封装
# ---------------------------------------------------------------------------


def _parsed(tmp_path: Path) -> verification_plan_doc.ParsedPlan:
    """把语料落盘再读回，交给真正的解析器。"""
    path = tmp_path / "verification-plan.md"
    path.write_bytes(CANONICAL_MD.encode("utf-8"))
    return parse_plan_doc(path.read_bytes().decode("utf-8"))


def _vp(vp_id: str, refs: Sequence[str], title: Optional[str] = None) -> Dict[str, Any]:
    """机器格式里的一条验证点。

    ``expected_result`` 按规范形状拼：``"<E1,E2> | <正文>"``。
    """
    point: Dict[str, Any] = {
        "id": vp_id,
        "expected_result": f"{','.join(refs)} | 预期结果正文",
    }
    if title is not None:
        point["title"] = title
    return point


def _plan(*points: Dict[str, Any]) -> Dict[str, Any]:
    """机器格式的顶层对象。"""
    return {"verification_points": list(points)}


def _find(
    divergences: Sequence[Any],
    dimension: str,
    item_id: str,
) -> Optional[Any]:
    """在差异列表里找第一条指定维度、指定验证点的差异。"""
    for divergence in divergences:
        if divergence.dimension == dimension and divergence.item_id == item_id:
            return divergence
    return None


def _dimensions(divergences: Sequence[Any]) -> List[str]:
    """差异列表的维度序列。"""
    return [divergence.dimension for divergence in divergences]


# ---------------------------------------------------------------------------
# 1. points 维度：验证点集合
# ---------------------------------------------------------------------------


def test_equivalence_points_add_delete_rename(tmp_path: Path) -> None:
    """增 / 删 / 改名各产生 points 差异。"""
    parsed = _parsed(tmp_path)

    # 删：机器格式少了 VP-002。
    deleted = translator.validate_equivalence(
        parsed, _plan(_vp("VP-001", ["E1", "E2"]))
    )
    missing = _find(deleted, "points", "VP-002")
    assert missing is not None
    assert missing.detail == "missing VP"

    # 增：机器格式多了没被审核过的 VP-003。
    added = translator.validate_equivalence(
        parsed,
        _plan(
            _vp("VP-001", ["E1", "E2"]),
            _vp("VP-002", ["E1"]),
            _vp("VP-003", ["E1"]),
        ),
    )
    unexpected = _find(added, "points", "VP-003")
    assert unexpected is not None
    assert unexpected.detail == "added VP"

    # 改名：VP-002 被改成 VP-009 —— 既算少了一条，也算多了一条。
    renamed = translator.validate_equivalence(
        parsed, _plan(_vp("VP-001", ["E1", "E2"]), _vp("VP-009", ["E1"]))
    )
    assert _find(renamed, "points", "VP-002") is not None
    assert _find(renamed, "points", "VP-009") is not None


def test_equivalence_points_order_change_is_not_a_divergence(tmp_path: Path) -> None:
    """验证点换顺序不是差异 —— 集合相等就行。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(_vp("VP-002", ["E1"]), _vp("VP-001", ["E1", "E2"])),
    )

    assert divergences == []


def test_equivalence_duplicate_vp_id_is_a_failure(tmp_path: Path) -> None:
    """同一批验证点出现两个同 id 的条目 → points 差异（比对没有意义）。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(
            _vp("VP-001", ["E1", "E2"]),
            _vp("VP-001", ["E1", "E2"]),
            _vp("VP-002", ["E1"]),
        ),
    )

    duplicate = _find(divergences, "points", "VP-001")
    assert duplicate is not None
    assert "duplicate" in duplicate.detail


def test_equivalence_points_dimension_states_are_isolated(tmp_path: Path) -> None:
    """points 维度的四种状态，只报 points —— 不掺别的维度。

    四个维度正交：坏了一个维度，报出来的差异维度就该**恰好**是那一个。
    """
    parsed = _parsed(tmp_path)

    # 完整 → 零差异。
    assert translator.validate_equivalence(
        parsed,
        _plan(
            _vp("VP-001", ["E1", "E2"], title=TITLE_VP_001),
            _vp("VP-002", ["E1"], title=TITLE_VP_002),
        ),
    ) == []

    # 增一条 / 删一条 / 改名 → 只有 points。
    added = translator.validate_equivalence(
        parsed,
        _plan(
            _vp("VP-001", ["E1", "E2"]),
            _vp("VP-002", ["E1"]),
            _vp("VP-003", ["E1"]),
        ),
    )
    deleted = translator.validate_equivalence(
        parsed, _plan(_vp("VP-001", ["E1", "E2"]))
    )
    renamed = translator.validate_equivalence(
        parsed, _plan(_vp("VP-001", ["E1", "E2"]), _vp("VP-009", ["E1"]))
    )

    assert _dimensions(added) == ["points"]
    assert _dimensions(deleted) == ["points"]
    assert _dimensions(renamed) == ["points", "points"]


# ---------------------------------------------------------------------------
# 2. evidence 维度：证据编号
# ---------------------------------------------------------------------------


def test_equivalence_evidence_dimension_states_are_isolated(tmp_path: Path) -> None:
    """evidence 维度的四种状态，只报 evidence —— 不掺别的维度。"""
    parsed = _parsed(tmp_path)

    # 完整 → 零差异。
    assert translator.validate_equivalence(
        parsed, _plan(_vp("VP-001", ["E1", "E2"]), _vp("VP-002", ["E1"]))
    ) == []

    # 增一条 E / 删一条 E / 改 E 的名 → 只有 evidence。
    added = translator.validate_equivalence(
        parsed, _plan(_vp("VP-001", ["E1", "E2", "E3"]), _vp("VP-002", ["E1"]))
    )
    deleted = translator.validate_equivalence(
        parsed, _plan(_vp("VP-001", ["E1"]), _vp("VP-002", ["E1"]))
    )
    renamed = translator.validate_equivalence(
        parsed, _plan(_vp("VP-001", ["E1", "E3"]), _vp("VP-002", ["E1"]))
    )

    assert _dimensions(added) == ["evidence"]
    assert _dimensions(deleted) == ["evidence"]
    assert _dimensions(renamed) == ["evidence", "evidence"]


def test_evidence_set_equal_is_order_insensitive(tmp_path: Path) -> None:
    """E 编号重排 → 零差异（集合相等即可，顺序不算内容）。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(
            _vp("VP-001", ["E2", "E1"]),
            _vp("VP-002", ["E1"]),
        ),
    )

    assert divergences == []


def test_missing_evidence_reference_detected(tmp_path: Path) -> None:
    """少引一条 E → evidence 差异。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(
            _vp("VP-001", ["E1"]),
            _vp("VP-002", ["E1"]),
        ),
    )

    divergence = _find(divergences, "evidence", "VP-001")
    assert divergence is not None
    assert "E2" in divergence.detail
    assert "E1" not in divergence.detail.replace("VP-001", "")


def test_extra_evidence_reference_detected(tmp_path: Path) -> None:
    """机器格式引用了计划书没声明的 E → evidence 差异。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(
            _vp("VP-001", ["E1", "E2", "E9"]),
            _vp("VP-002", ["E1"]),
        ),
    )

    divergence = _find(divergences, "evidence", "VP-001")
    assert divergence is not None
    assert "E9" in divergence.detail


def test_evidence_reference_prefix_is_required(tmp_path: Path) -> None:
    """``expected_result`` 没有规范化引用串前缀 → 等于一条证据都没引用。"""
    parsed = _parsed(tmp_path)

    plan = _plan(
        {"id": "VP-001", "expected_result": "两条判据全部满足，无超时"},
        _vp("VP-002", ["E1"]),
    )
    divergences = translator.validate_equivalence(parsed, plan)

    divergence = _find(divergences, "evidence", "VP-001")
    assert divergence is not None
    assert "E1" in divergence.detail


def test_missing_vp_is_not_compared_on_evidence(tmp_path: Path) -> None:
    """机器格式里没有的验证点，比不了证据 —— 只报 points 差异。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed, _plan(_vp("VP-001", ["E1", "E2"]))
    )

    assert _dimensions(divergences) == ["points"]
    assert divergences[0].item_id == "VP-002"


# ---------------------------------------------------------------------------
# 3. 两个维度合起来的形状
# ---------------------------------------------------------------------------


def test_equivalence_reports_points_and_evidence_together(tmp_path: Path) -> None:
    """文档 VP-001(E1,E2)/VP-002(E1)，机器格式只有 VP-001 且只引 E1 → 两处差异。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed, _plan(_vp("VP-001", ["E1"]))
    )

    assert [(d.dimension, d.item_id) for d in divergences] == [
        ("points", "VP-002"),
        ("evidence", "VP-001"),
    ]
    assert divergences[0].detail == "missing VP"
    assert divergences[1].detail == "E2 not referenced"


def test_equivalence_of_matching_plan_is_empty(tmp_path: Path) -> None:
    """机器格式与计划书逐条相等 → 零差异（门禁放行）。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(
            _vp("VP-001", ["E1", "E2"], title=TITLE_VP_001),
            _vp("VP-002", ["E1"], title=TITLE_VP_002),
        ),
    )

    assert divergences == []


def test_equivalence_divergence_is_a_report_not_an_exception(tmp_path: Path) -> None:
    """有差异时返回差异列表 —— 不抛错，抛不抛由调用方决定。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed, _plan(_vp("VP-001", ["E1"]))
    )

    assert isinstance(divergences, list)
    assert divergences
    assert all(
        isinstance(d.dimension, str) and isinstance(d.item_id, str)
        for d in divergences
    )


def test_equivalence_rejects_non_plan_payload(tmp_path: Path) -> None:
    """机器格式里没有验证点数组 → points 差异（不是静默放行）。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(parsed, {"services": []})

    assert _dimensions(divergences) == ["points"]


if __name__ == "__main__":  # pragma: no cover - 手动跑
    raise SystemExit(pytest.main([__file__, "-v"]))
