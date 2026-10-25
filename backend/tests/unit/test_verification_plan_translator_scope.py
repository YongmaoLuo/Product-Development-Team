"""计划书 → 机器格式：审核门禁的 **criteria / scope** 两个维度。

背景
----
``verification-plan.md`` 是人工审核的唯一对象，``verification_plan.json`` 是执行
判据的唯一依据。审核的四项实质内容是：验证点（points）、通过判据（criteria）、
执行范围（scope）、证据（evidence）。前两项由任务 6 落地，本文件钉住后两项。

为什么这两项要单独钉
--------------------
* **criteria** —— 判据编号 ``C1``、``C2`` 是"这条验证点到底要证明什么"。机器
  格式如果引用了计划书里没有的 ``C3``，那这条验证点在证明一件**没被审核过**的
  事；如果漏了 ``C2``，那"审过的判据"有一半没人验。两种都不自己暴露。
* **scope** —— 执行位置（URL / 命令里的路径）必须落在计划书声明的范围里，且不
  得命中排除范围。执行到了范围外，等于"验过的东西"比"审过的东西"多。

两条边界
--------
* **编号顺序不算差异** —— ``C1,C2`` 与 ``C2,C1`` 是同一批判据；
* **判据落在断言名上** —— 断言名用 ``C<n>: <正文>`` 的形式指明它验的是哪条
  判据，所以"每个判据至少一条断言"是可以机械核对的。

形状
----
机器格式里每条验证点的判据引用写在 ``related_prd_criteria``（``C1``、``C2``
这样的编号出现在自由文本里），断言名以 ``C<n>: `` 开头::

    "related_prd_criteria": "C1: 配额耗尽时睡到真实重开时刻；C2: 双档耗尽时取最晚重开的窗口"
    "assertions": [{"name": "C1: 状态码", "status": 200}]

范围写在每条验证点的 ``范围``（``PlanItem.scope``）里；计划级的
``验证范围`` / ``排除范围`` 两个章节给出全局的范围内与排除项。

测试数据
--------
语料是本文件里的两份合法计划书，先落盘再按字节读回，交给
:func:`verification_plan_doc.parse_plan_doc` 解析 —— 校验器消费的是真实文件
字节与真实解析产物。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_ac_coverage  # noqa: E402
import verification_plan_doc  # noqa: E402
import verification_plan_translator as translator  # noqa: E402
from verification_plan_doc import parse_plan_doc  # noqa: E402

# ---------------------------------------------------------------------------
# 语料
# ---------------------------------------------------------------------------

#: 一份合法的计划书：两个验证点。
#:
#: * VP-001 的「范围」是**仓库相对路径**（``backend/verification_loop.py``），
#:   判据有两条（C1、C2），用来测"漏一个 C / 加一个 C"。
#: * VP-002 的「范围」是**URL**（``http://127.0.0.1:9000/report``），用来测
#:   URL 形式的范围比较。
#:
#: 计划级的「排除范围」声明了 ``backend/legacy/`` —— 命中它的执行位置要报
#: scope 差异。
CANONICAL_MD = """\
# 验证计划书

本计划书是对被验证对象的人工审核对象，也是机器格式翻译的唯一来源。

## 被验证对象

被验证对象为示例仓库 PR #7 的 head，含配额等待的重构。
baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7

## 验证范围

- 配额等待按真实重开时刻睡

## 排除范围

- backend/legacy/ 下的旧接口（本次未改动）

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
- 预期结果：两条判据全部满足，无超时

### VP-002 报告把 skip 条目标注为未验证

- 范围：http://127.0.0.1:9000/report
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

#: 单个验证点的计划书 —— 任务书里的输入示例背后那份：只声明了一条判据 ``C1``。
SINGLE_MD = """\
# 验证计划书

本计划书是对被验证对象的人工审核对象，也是机器格式翻译的唯一来源。

## 被验证对象

被验证对象为示例仓库 PR #7 的 head，含登录改造。
baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7

## 验证范围

- 登录接口

## 排除范围

- 单点登录服务（本次未改动）

## 验证方法

- 以接口验收为主

## 验证点清单

### VP-001 登录成功

- 范围：backend/auth.py 的登录路径
- 验证方法：api_test
- 通过判据：
  - C1: 登录成功
- 证据要求：
  - E1: tests/unit/test_login.py 的 pytest 输出
- 预期结果：登录成功

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

#: 判据正文带 ``【标签】`` 的计划书 —— 用来钉住"加了 C 前缀之后，既有的 AC
#: 覆盖标签匹配仍然成立"。
LABELLED_MD = """\
# 验证计划书

本计划书是对被验证对象的人工审核对象，也是机器格式翻译的唯一来源。

## 被验证对象

被验证对象为示例仓库 PR #7 的 head，含凭据门禁。
baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7

## 验证范围

- 凭据写入路径

## 排除范围

- 外部凭据服务（本次未改动）

## 验证方法

- 以静态门禁为主

## 验证点清单

### VP-001 凭据键名门禁

- 范围：backend/credentials.py 的写入路径
- 验证方法：code_review
- 通过判据：
  - C1: 【静态门禁】secret 键名不得出现在任何 os.environ 写入中
- 证据要求：
  - E1: tests/unit/test_credentials.py 的 pytest 输出
- 预期结果：静态门禁通过

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

#: ``LABELLED_MD`` 里那条判据的正文（也就是 PRD 侧的验收标准原文）。
LABELLED_ACCEPTANCE = "【静态门禁】secret 键名不得出现在任何 os.environ 写入中"

#: 仓库事实。``services`` 是计划级的事实（端口只在这里出现）。
REPO_FACTS: Dict[str, Any] = {
    "services": [
        {
            "name": "api",
            "port": 8080,
            "start_cmd": "python -m app.server",
            "cwd": ".",
        }
    ],
    "repo_root": "/tmp/repo",
}


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class StubTool:
    """最小 LLM 替身：把预先备好的回应交回给翻译器。"""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: List[Dict[str, Any]] = []

    def query_json(
        self,
        prompt: str,
        system_instruction: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {"prompt": prompt, "system_instruction": system_instruction}
        )
        return self.payload


# ---------------------------------------------------------------------------
# 便捷封装
# ---------------------------------------------------------------------------


def _parsed(tmp_path: Path, markdown: str = CANONICAL_MD) -> verification_plan_doc.ParsedPlan:
    """把语料落盘再读回，交给真正的解析器。"""
    path = tmp_path / "verification-plan.md"
    path.write_bytes(markdown.encode("utf-8"))
    return parse_plan_doc(path.read_bytes().decode("utf-8"))


def _plan(*points: Dict[str, Any]) -> Dict[str, Any]:
    """机器格式的顶层对象。"""
    return {"verification_points": list(points)}


def _vp(
    vp_id: str,
    criteria: Optional[str] = None,
    assertions: Optional[Sequence[Dict[str, Any]]] = None,
    evidence_refs: Sequence[str] = ("E1",),
    **extra: Any,
) -> Dict[str, Any]:
    """机器格式里的一条验证点。

    ``criteria`` 是 ``related_prd_criteria`` 的原文；``assertions`` 是断言数组。
    ``evidence_refs`` 决定 ``expected_result`` 的规范化引用串前缀。
    """
    point: Dict[str, Any] = {
        "id": vp_id,
        "expected_result": f"{','.join(evidence_refs)} | 预期结果正文",
    }
    if criteria is not None:
        point["related_prd_criteria"] = criteria
    if assertions is not None:
        point["assertions"] = list(assertions)
    point.update(extra)
    return point


def _find(
    divergences: Sequence[Any],
    dimension: str,
    item_id: str = "",
) -> List[Any]:
    """挑出指定维度（可选指定验证点）的差异。"""
    return [
        divergence
        for divergence in divergences
        if divergence.dimension == dimension
        and (not item_id or divergence.item_id == item_id)
    ]


def _details(divergences: Sequence[Any]) -> List[str]:
    """差异列表的 detail 序列。"""
    return [divergence.detail for divergence in divergences]


# ---------------------------------------------------------------------------
# 1. criteria 维度：判据编号
# ---------------------------------------------------------------------------


def test_criteria_partial_and_extra_rejected(tmp_path: Path) -> None:
    """漏一个 C 或加一个 C → 各产生 criteria 差异。"""
    parsed = _parsed(tmp_path)

    # 漏：计划书声明了 C1、C2，机器格式只提了 C1。
    partial = translator.validate_equivalence(
        parsed,
        _plan(
            _vp(
                "VP-001",
                criteria="C1: 配额耗尽时睡到真实重开时刻",
            ),
            _vp("VP-002", criteria="C1 | skip 条目显示为「未验证」"),
        ),
    )
    missing = _find(partial, "criteria", "VP-001")
    assert missing, "漏掉的判据必须报出来"
    assert "C2" in missing[0].detail
    assert "C1" not in missing[0].detail

    # 加：机器格式引用了计划书里没有的 C3。
    extra = translator.validate_equivalence(
        parsed,
        _plan(
            _vp(
                "VP-001",
                criteria="C1: 配额耗尽时睡到真实重开时刻；C2: 双档耗尽时取最晚重开的窗口；C3: 计划书里没有的判据",
            ),
            _vp("VP-002", criteria="C1 | skip 条目显示为「未验证」"),
        ),
    )
    unexpected = _find(extra, "criteria", "VP-001")
    assert unexpected, "多出来的判据必须报出来"
    assert any("C3" in divergence.detail for divergence in unexpected)


def test_criteria_extra_number_carried_by_an_assertion(tmp_path: Path) -> None:
    """断言名指向计划书里没有的判据 → criteria 差异（"C9 not in plan"）。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(
            _vp(
                "VP-001",
                criteria="C1 | 配额耗尽时睡到真实重开时刻",
                assertions=[
                    {"name": "C1: 状态码", "status": 200},
                    {"name": "C2: 双档耗尽时取最晚重开的窗口", "status": 200},
                    {"name": "C9: 计划书里没有的判据", "status": 200},
                ],
            ),
            _vp("VP-002", criteria="C1 | skip 条目显示为「未验证」"),
        ),
    )

    unexpected = _find(divergences, "criteria", "VP-001")
    assert [divergence.detail for divergence in unexpected] == ["C9 not in plan"]


def test_assertion_names_cover_all_criteria(tmp_path: Path) -> None:
    """每个 C 至少一条断言 → 零差异。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(
            _vp(
                "VP-001",
                assertions=[
                    {"name": "C1: 状态码", "status": 200},
                    {"name": "C2: 响应体里的窗口", "json_path": "$.window"},
                ],
            ),
            _vp("VP-002", criteria="C1 | skip 条目显示为「未验证」"),
        ),
    )

    assert divergences == []


def test_assertion_without_criterion_prefix_is_rejected(tmp_path: Path) -> None:
    """断言名缺 ``Cx:`` 前缀 → criteria 差异。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(
            _vp(
                "VP-001",
                criteria="C1 | 配额耗尽时睡到真实重开时刻；C2 | 双档耗尽时取最晚重开的窗口",
                assertions=[{"name": "状态码", "status": 200}],
            ),
            _vp("VP-002", criteria="C1 | skip 条目显示为「未验证」"),
        ),
    )

    divergences_of_criteria = _find(divergences, "criteria", "VP-001")
    assert divergences_of_criteria
    assert any("Cx" in divergence.detail for divergence in divergences_of_criteria)


def test_criterion_order_does_not_matter(tmp_path: Path) -> None:
    """判据编号换顺序 → 零差异（集合相等即可）。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(
            _vp(
                "VP-001",
                assertions=[
                    {"name": "C2: 响应体里的窗口", "json_path": "$.window"},
                    {"name": "C1: 状态码", "status": 200},
                ],
            ),
            _vp("VP-002", criteria="C1 | skip 条目显示为「未验证」"),
        ),
    )

    assert divergences == []


# ---------------------------------------------------------------------------
# 2. scope 维度：执行位置 vs 声明范围
# ---------------------------------------------------------------------------


def test_scope_and_exclusion_enforced(tmp_path: Path) -> None:
    """范围外的 URL / 命中排除路径 → scope 差异。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(
            _vp(
                "VP-001",
                criteria="C1 | 配额耗尽时睡到真实重开时刻；C2 | 双档耗尽时取最晚重开的窗口",
                assertions=[
                    {"name": "C1: 状态码", "status": 200},
                    {"name": "C2: 响应体里的窗口", "json_path": "$.window"},
                ],
                # 命令跑到了排除范围里
                test_command="pytest backend/legacy/test_old.py",
            ),
            _vp(
                "VP-002",
                criteria="C1 | skip 条目显示为「未验证」",
                # 声明的范围是 http://127.0.0.1:9000/report，这里跑到了 /admin
                target_url="http://127.0.0.1:9000/admin",
            ),
        ),
    )

    excluded = _find(divergences, "scope", "VP-001")
    assert [divergence.detail for divergence in excluded] == [
        "test_command in excluded scope"
    ]

    outside = _find(divergences, "scope", "VP-002")
    assert [divergence.detail for divergence in outside] == [
        "target_url outside range"
    ]


def test_scope_in_range_produces_no_divergence(tmp_path: Path) -> None:
    """执行位置落在声明范围内 → 零差异。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(
            _vp(
                "VP-001",
                criteria="C1 | 配额耗尽时睡到真实重开时刻；C2 | 双档耗尽时取最晚重开的窗口",
                assertions=[
                    {"name": "C1: 状态码", "status": 200},
                    {"name": "C2: 响应体里的窗口", "json_path": "$.window"},
                ],
                test_command="python backend/verification_loop.py",
            ),
            _vp(
                "VP-002",
                criteria="C1 | skip 条目显示为「未验证」",
                target_url="http://127.0.0.1:{{svc.api.port}}/report",
            ),
        ),
    )

    assert divergences == []


def test_scope_paths_are_normalized_to_repo_relative(tmp_path: Path) -> None:
    """路径归一化成仓库相对路径后比较：``./backend/...`` 与 ``backend/...`` 等价。"""
    parsed = _parsed(tmp_path)

    divergences = translator.validate_equivalence(
        parsed,
        _plan(
            _vp(
                "VP-001",
                criteria="C1 | 配额耗尽时睡到真实重开时刻；C2 | 双档耗尽时取最晚重开的窗口",
                assertions=[
                    {"name": "C1: 状态码", "status": 200},
                    {"name": "C2: 响应体里的窗口", "json_path": "$.window"},
                ],
                test_command="python ./backend/verification_loop.py",
            ),
            _vp("VP-002", criteria="C1 | skip 条目显示为「未验证」"),
        ),
    )

    assert _find(divergences, "scope") == []


# ---------------------------------------------------------------------------
# 3. 与既有机制的关系
# ---------------------------------------------------------------------------


def test_existing_ac_coverage_labels_still_match(tmp_path: Path) -> None:
    """判据加了 ``C1:`` 前缀之后，既有的 ``【标签】`` 覆盖匹配仍然成立。"""
    parsed = _parsed(tmp_path, LABELLED_MD)

    translated = translator.translate(
        parsed,
        REPO_FACTS,
        StubTool({"verification_points": [{"id": "VP-001"}]}),
    )

    claim = translated["verification_points"][0]["related_prd_criteria"]
    assert claim.startswith("C1: ")
    assert "【静态门禁】" in claim

    assert (
        verification_ac_coverage.find_uncovered_criteria(
            translated, [LABELLED_ACCEPTANCE]
        )
        == []
    )


# ---------------------------------------------------------------------------
# 4. 任务书里的输入 / 输出示例
# ---------------------------------------------------------------------------


def test_brief_example_shape(tmp_path: Path) -> None:
    """任务书给的输入示例 → 输出示例。"""
    parsed = _parsed(tmp_path, SINGLE_MD)

    plan_json = {
        "verification_points": [
            {
                "id": "VP-001",
                "related_prd_criteria": "C1 | 登录成功",
                "assertions": [{"name": "C2: status", "expected": 200}],
                "target_url": "http://127.0.0.1:9000/admin",
            }
        ]
    }

    divergences = translator.validate_equivalence(parsed, plan_json)

    assert _details(_find(divergences, "criteria")) == ["C2 not in plan"]
    assert _details(_find(divergences, "scope")) == ["target_url outside range"]


def test_brief_example_with_evidence_prefix_is_exact(tmp_path: Path) -> None:
    """补上证据引用前缀之后，差异列表恰好是输出示例里的两条。"""
    parsed = _parsed(tmp_path, SINGLE_MD)

    plan_json = {
        "verification_points": [
            {
                "id": "VP-001",
                "related_prd_criteria": "C1 | 登录成功",
                "assertions": [{"name": "C2: status", "expected": 200}],
                "target_url": "http://127.0.0.1:9000/admin",
                "expected_result": "E1 | 登录成功",
            }
        ]
    }

    divergences = translator.validate_equivalence(parsed, plan_json)

    assert divergences == [
        translator.Divergence(
            dimension="criteria", item_id="VP-001", detail="C2 not in plan"
        ),
        translator.Divergence(
            dimension="scope", item_id="VP-001", detail="target_url outside range"
        ),
    ]


if __name__ == "__main__":  # pragma: no cover - 手动跑
    raise SystemExit(pytest.main([__file__, "-v"]))
