"""计划书 → 机器格式的翻译：**验证点集合零漂移**。

背景
----
``verification-plan.md`` 是人工审核的唯一对象，``verification_plan.json`` 是
机器执行的唯一依据。翻译这一步只做一件事：把文档里定死的验证点集合原样搬进
机器格式，让模型去补**执行细节**（请求、断言、命令、CI 入口……）。

危险的不是模型把某个字段写错 —— 那只影响一条验证点。危险的是模型**动了验证点
集合本身**：多一条看起来很正常，少一条看起来像"本来就该少"。这两种漂移都不会
自己暴露，所以翻译阶段对集合的态度是零容忍：多一条、少一条、改个 id、改个标
题，一律抛 :class:`TranslationError`，机器格式不落盘。

判据
----
本文件钉住：

* 两个验证点的文档 → 输出的 id 集合与文档**逐条相等**（且保持文档顺序）；
* 模型新增 / 删除 / 改名 / 重复验证点 → 抛 ``TranslationError``；
* 模型只被允许补八个执行字段，别的字段一律以文档为准；
* 翻译过程**不写任何文件** —— "抛错 + JSON 不落盘"是同一件事的两面。

测试数据
--------
语料是本文件里的一份合法计划书（十章节、两个验证点），先落盘再按字节读回，
交给 :func:`verification_plan_doc.parse_plan_doc` 解析 —— 翻译器消费的是真实
文件字节与真实解析产物。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_plan_doc  # noqa: E402
from verification_plan_doc import parse_plan_doc  # noqa: E402
from verification_plan_translator import (  # noqa: E402
    ALLOWED_LLM_FIELDS,
    TranslationError,
    translate,
)

# ---------------------------------------------------------------------------
# 语料
# ---------------------------------------------------------------------------

#: 一份合法的计划书：两个验证点，一个 api_test、一个 ui_validation。
#:
#: 每条验证点的「验证方法」写的是机器格式的方法名 —— 翻译器把它**原样**搬进
#: ``verification_method``，不做任何再分类（模型的分类不等于人工审核过的分类）。
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

#: 仓库事实。``services`` 是计划级的事实（端口只在这里出现），直接进机器格式。
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
    """最小 LLM 替身：把预先备好的回应交回给翻译器。

    刻意写成普通类而不是 ``Mock``：``Mock`` 会把任何调用都吞掉，翻译器因此
    可能"通过"一条它其实根本没调过模型的路径。
    """

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


class PositionalOnlyTool:
    """只吃位置参数的模型句柄 —— 老接口 / 别的 provider 的形状。"""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls = 0

    def query_json(self, prompt: str) -> Any:
        self.calls += 1
        return self.payload


class CallableTool:
    """只吃一个提示词的可调用对象。"""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls = 0

    def __call__(self, prompt: str) -> Any:
        self.calls += 1
        return self.payload


class FailingTool:
    """调不通的模型句柄。"""

    def query_json(self, prompt: str, system_instruction: Any = None, **kwargs: Any) -> Any:
        raise RuntimeError("provider 不可用")


class TypeErroringTool:
    """签名吃得下关键字参数，但内部炸了 ``TypeError``。

    这是"按签名决定怎么调"与"调一次、炸了就重试一次"的分水岭：后者会把**同一次
    翻译**变成两次模型调用。
    """

    def __init__(self) -> None:
        self.calls = 0

    def query_json(
        self,
        prompt: str,
        system_instruction: Any = None,
        timeout: Any = None,
    ) -> Any:
        self.calls += 1
        raise TypeError("内部炸了")


def _llm_points(*points: Dict[str, Any]) -> Dict[str, Any]:
    """把若干条验证点包成模型回应的形状。"""
    return {"verification_points": list(points)}


def _doc_vp_001() -> Dict[str, Any]:
    """模型给 VP-001 补的执行细节（api_test）。"""
    return {
        "id": "VP-001",
        "uses_services": ["api"],
        "request": {
            "method": "GET",
            "url": "{{svc.api.url}}/api/quota",
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码", "status": 200},
            {"name": "至少返回一条记录", "json_path": "$.items", "length_gte": 1},
        ],
    }


def _doc_vp_002() -> Dict[str, Any]:
    """模型给 VP-002 补的执行细节（ui_validation）。"""
    return {
        "id": "VP-002",
        "uses_services": ["api"],
        "target_url": "http://127.0.0.1:{{svc.api.port}}/report",
    }


def _parse(tmp_path: Path) -> verification_plan_doc.ParsedPlan:
    """把语料落盘再读回，交给真正的解析器。"""
    path = tmp_path / "verification-plan.md"
    path.write_bytes(CANONICAL_MD.encode("utf-8"))
    return parse_plan_doc(path.read_bytes().decode("utf-8"))


def _translate_ok(tmp_path: Path, payload: Any) -> Dict[str, Any]:
    """用给定回应跑一次翻译（成功路径的便捷封装）。"""
    return translate(_parse(tmp_path), REPO_FACTS, StubTool(payload))


# ---------------------------------------------------------------------------
# 1. 验证点集合零漂移
# ---------------------------------------------------------------------------


def test_translate_preserves_vp_set(tmp_path: Path) -> None:
    """文档里的两个验证点 → 机器格式里还是这两个，且顺序不变。"""
    plan = _translate_ok(tmp_path, _llm_points(_doc_vp_001(), _doc_vp_002()))

    points = plan["verification_points"]
    assert [vp["id"] for vp in points] == ["VP-001", "VP-002"]
    assert {vp["id"] for vp in points} == {"VP-001", "VP-002"}


def test_translate_rejects_added_vp(tmp_path: Path) -> None:
    """模型新增一条没被审核过的验证点 → 抛错（新增的那条不落进结果）。"""
    added = dict(_doc_vp_002())
    added["id"] = "VP-003"

    with pytest.raises(TranslationError) as excinfo:
        _translate_ok(tmp_path, _llm_points(_doc_vp_001(), _doc_vp_002(), added))

    assert "VP-003" in str(excinfo.value)


def test_translate_rejects_removed_vp(tmp_path: Path) -> None:
    """模型少给一条验证点 → 抛错（少的那条不许悄悄消失）。"""
    with pytest.raises(TranslationError) as excinfo:
        _translate_ok(tmp_path, _llm_points(_doc_vp_001()))

    assert "VP-002" in str(excinfo.value)


def test_translate_rejects_renamed_vp(tmp_path: Path) -> None:
    """模型把验证点改名（id 变了）→ 集合漂移 → 抛错。"""
    renamed = dict(_doc_vp_002())
    renamed["id"] = "VP-009"

    with pytest.raises(TranslationError) as excinfo:
        _translate_ok(tmp_path, _llm_points(_doc_vp_001(), renamed))

    message = str(excinfo.value)
    assert "VP-009" in message and "VP-002" in message


def test_translate_rejects_retitled_vp(tmp_path: Path) -> None:
    """模型改了验证点的标题 → 人工审核过的标题被替换 → 抛错。"""
    retitled = dict(_doc_vp_001())
    retitled["title"] = "模型自己编的标题"

    with pytest.raises(TranslationError) as excinfo:
        _translate_ok(tmp_path, _llm_points(retitled, _doc_vp_002()))

    assert "VP-001" in str(excinfo.value)


def test_translate_rejects_duplicate_vp_ids(tmp_path: Path) -> None:
    """模型把同一条验证点给了两遍 → 抛错。"""
    with pytest.raises(TranslationError) as excinfo:
        _translate_ok(
            tmp_path, _llm_points(_doc_vp_001(), _doc_vp_002(), _doc_vp_001())
        )

    assert "VP-001" in str(excinfo.value)


def test_translate_rejects_vp_without_id(tmp_path: Path) -> None:
    """模型给的验证点没有 id → 无法与文档对齐 → 抛错。"""
    anonymous = dict(_doc_vp_001())
    anonymous.pop("id")

    with pytest.raises(TranslationError):
        _translate_ok(tmp_path, _llm_points(anonymous, _doc_vp_002()))


# ---------------------------------------------------------------------------
# 2. 字段边界：只许补八个执行字段
# ---------------------------------------------------------------------------


def test_translate_copies_allowed_execution_fields(tmp_path: Path) -> None:
    """八个执行字段原样搬进机器格式。"""
    plan = _translate_ok(tmp_path, _llm_points(_doc_vp_001(), _doc_vp_002()))

    vp1, vp2 = plan["verification_points"]
    for field in ("uses_services", "request", "assertions"):
        assert vp1[field] == _doc_vp_001()[field]
    assert vp2["target_url"] == _doc_vp_002()["target_url"]
    assert vp2["uses_services"] == ["api"]


def test_translate_keeps_document_identity_fields(tmp_path: Path) -> None:
    """id / 标题 / 验证方法 / 判据 / 预期结果一律以文档为准。"""
    plan = _translate_ok(tmp_path, _llm_points(_doc_vp_001(), _doc_vp_002()))

    vp1 = plan["verification_points"][0]
    assert vp1["id"] == "VP-001"
    assert vp1["title"] == "配额按真实重开时刻睡眠"
    assert vp1["verification_method"] == "api_test"
    assert "C1: 配额耗尽时睡到真实重开时刻" in vp1["related_prd_criteria"]
    assert vp1["expected_result"] == "两条判据全部满足，无超时"


def test_translate_discards_fields_outside_the_allow_list(tmp_path: Path) -> None:
    """模型顺手写的别的字段（身份字段之外）一律丢掉，文档说了算。"""
    noisy = dict(_doc_vp_001())
    noisy["priority"] = "high"
    noisy["verification_phase"] = 2
    noisy["expected_result"] = "模型改写的预期结果"
    noisy["verification_method"] = "code_review"

    plan = _translate_ok(tmp_path, _llm_points(noisy, _doc_vp_002()))

    vp1 = plan["verification_points"][0]
    assert "priority" not in vp1
    assert "verification_phase" not in vp1
    assert vp1["expected_result"] == "两条判据全部满足，无超时"
    assert vp1["verification_method"] == "api_test"


def test_allowed_llm_fields_are_exactly_the_eight() -> None:
    """允许补的字段集合本身被钉住 —— 多一个就是模型能改文档了。"""
    assert set(ALLOWED_LLM_FIELDS) == {
        "uses_services",
        "request",
        "assertions",
        "target_url",
        "test_command",
        "ci_entry",
        "ci_timeout_seconds",
        "phase_order",
    }


# ---------------------------------------------------------------------------
# 3. 计划级字段
# ---------------------------------------------------------------------------


def test_translate_copies_services_from_repo_facts(tmp_path: Path) -> None:
    """服务声明是仓库事实，原样进机器格式 —— 端口只在这里出现一次。"""
    plan = _translate_ok(tmp_path, _llm_points(_doc_vp_001(), _doc_vp_002()))

    assert plan["services"] == REPO_FACTS["services"]
    assert plan["services"][0]["port"] == 8080


def test_translate_output_has_exactly_two_top_level_keys(tmp_path: Path) -> None:
    """机器格式的顶层只有两个键 —— 多出来的都是没被审过的东西。"""
    plan = _translate_ok(tmp_path, _llm_points(_doc_vp_001(), _doc_vp_002()))

    assert set(plan) == {"services", "verification_points"}


def test_translate_result_is_json_serialisable(tmp_path: Path) -> None:
    """翻译产物能直接落盘成 JSON（调用方要写的就是它）。"""
    plan = _translate_ok(tmp_path, _llm_points(_doc_vp_001(), _doc_vp_002()))

    assert json.loads(json.dumps(plan, ensure_ascii=False)) == plan


# ---------------------------------------------------------------------------
# 4. 副作用与坏输入
# ---------------------------------------------------------------------------


def test_translate_writes_no_file(tmp_path: Path, monkeypatch) -> None:
    """翻译不落盘：跑完之后目录里一个文件都不该多。

    "抛错 + JSON 不落盘"是同一件事的两面 —— 调用方在 ``translate`` 返回之后才
    写文件，所以只要翻译本身不写，抛错就一定意味着什么都没落盘。
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    plan = _translate_ok(tmp_path, _llm_points(_doc_vp_001(), _doc_vp_002()))

    assert plan["verification_points"]
    assert list(workdir.iterdir()) == []


def test_translate_writes_no_file_on_drift(tmp_path: Path, monkeypatch) -> None:
    """漂移导致抛错时，同样不落盘。"""
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    added = dict(_doc_vp_002())
    added["id"] = "VP-003"
    with pytest.raises(TranslationError):
        _translate_ok(tmp_path, _llm_points(_doc_vp_001(), _doc_vp_002(), added))

    assert list(workdir.iterdir()) == []


def test_translate_rejects_non_json_response(tmp_path: Path) -> None:
    """模型回的不是 JSON → 抛错（不是静默产出半份计划）。"""
    with pytest.raises(TranslationError):
        _translate_ok(tmp_path, "这不是 JSON")


def test_translate_rejects_response_without_verification_points(tmp_path: Path) -> None:
    """模型回的对象里没有验证点 → 抛错。"""
    with pytest.raises(TranslationError):
        _translate_ok(tmp_path, {"services": []})


def test_translate_passes_the_document_skeleton_to_the_model(tmp_path: Path) -> None:
    """模型必须看得见文档给的骨架，否则它无从知道要给哪条验证点补什么。"""
    tool = StubTool(_llm_points(_doc_vp_001(), _doc_vp_002()))

    translate(_parse(tmp_path), REPO_FACTS, tool)

    assert len(tool.calls) == 1
    prompt = tool.calls[0]["prompt"]
    assert "VP-001" in prompt and "VP-002" in prompt
    assert "配额按真实重开时刻睡眠" in prompt


# ---------------------------------------------------------------------------
# 5. 模型句柄的形状
# ---------------------------------------------------------------------------


def test_translate_accepts_positional_only_tool(tmp_path: Path) -> None:
    """只吃位置参数的句柄也能用，且**只调一次**（签名不匹配不该变成两次调用）。"""
    tool = PositionalOnlyTool(_llm_points(_doc_vp_001(), _doc_vp_002()))

    plan = translate(_parse(tmp_path), REPO_FACTS, tool)

    assert tool.calls == 1
    assert [vp["id"] for vp in plan["verification_points"]] == ["VP-001", "VP-002"]


def test_translate_accepts_plain_callable_tool(tmp_path: Path) -> None:
    """只吃一个提示词的可调用对象也能用。"""
    tool = CallableTool(_llm_points(_doc_vp_001(), _doc_vp_002()))

    plan = translate(_parse(tmp_path), REPO_FACTS, tool)

    assert tool.calls == 1
    assert [vp["id"] for vp in plan["verification_points"]] == ["VP-001", "VP-002"]


def test_translate_reports_tool_failure(tmp_path: Path) -> None:
    """模型调不通 → 抛错（原异常挂在 ``__cause__`` 上）。"""
    with pytest.raises(TranslationError) as excinfo:
        translate(_parse(tmp_path), REPO_FACTS, FailingTool())

    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_translate_does_not_call_the_model_twice(tmp_path: Path) -> None:
    """模型内部抛 ``TypeError`` 时**不许**再叫一遍模型。

    一次翻译 = 一次模型调用。按"炸了就换个签名再试一次"的写法，同一次翻译会变成
    两次模型调用 —— 既烧钱，也让"模型被叫了几次"这件事失去可核对性。
    """
    tool = TypeErroringTool()

    with pytest.raises(TranslationError):
        translate(_parse(tmp_path), REPO_FACTS, tool)

    assert tool.calls == 1


def test_translate_accepts_json_string_response(tmp_path: Path) -> None:
    """模型回的是 JSON 字符串（带 markdown 围栏）也能解析。"""
    payload = "```json\n" + json.dumps(
        _llm_points(_doc_vp_001(), _doc_vp_002()), ensure_ascii=False
    ) + "\n```"

    plan = _translate_ok(tmp_path, payload)

    assert [vp["id"] for vp in plan["verification_points"]] == ["VP-001", "VP-002"]