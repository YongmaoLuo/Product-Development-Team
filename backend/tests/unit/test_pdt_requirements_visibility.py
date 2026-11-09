"""可见性单测：日志暴露本轮生效的要求文件与渲染块哈希。

被测对象
--------
``backend/coding_tool.py`` 的 ``CodingTool._log_query_context()``（从
``ClaudeCodingTool`` 上移到基类）以及 Claude 两个入口的调用点。

契约（arch 决策点 3：可见性）
------------------------------
* 日志 data 暴露**生效文件相对路径**与**渲染块 sha256**，且与
  ``pdt_requirements.describe()`` 完全一致 —— 线上据此判断某轮到底吃到了
  哪些要求，而不是靠猜；
* 没有生效文件 → **不写**这两个键（不写一对空值：空值会被当成"生效了但
  是空的"）；
* scene 的取法：per-call scene > 实例 scene（``getattr(self, "scene", None)``）；
  两者都没有 → 不抛 AttributeError（基类没有 ``scene`` 属性）；
* Claude 的 ``query`` / ``query_json`` 必须把 per-call scene **原样**传给
  日志 —— 否则日志里的哈希可能与真正注入的那块不一致，"可见性"就成了假象；
* 方法在基类上 → 四个 provider 都能继承调用（Vendor C / OpenCode /
  Scripted 本轮不新增调用点）。

为什么用 ``SimpleNamespace`` 当 ``self``
----------------------------------------
``_log_query_context`` 只用到 ``self.logger`` / ``self._estimate_tokens`` /
``getattr(self, "scene", None)`` 三样，用一个假对象替身就能测真实方法体，
不必构造真 provider（那要连网络）。

为什么读 AST 查调用点
---------------------
"调用点传了 ``scene=scene``"是**源码形状**的契约：真调用会花钱，而传没传
只能从源码判断。这里读 AST，所以不碰网络。
"""

from __future__ import annotations

import ast
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool
import pdt_requirements


#: 被测源码（``backend/tests/unit/x.py`` → ``parents[2]`` == ``backend/``）。
SRC = Path(__file__).resolve().parents[2] / "coding_tool.py"


class _FakeLogger:
    """最小 logger 替身：只记 ``info`` 事件。"""

    def __init__(self):
        self.records: list[dict] = []

    def info(self, event, message, data=None):
        self.records.append(
            {"event": event, "message": message, "data": dict(data or {})}
        )

    def debug(self, *args, **kwargs):  # pragma: no cover —— 本用例不触发
        pass

    def warning(self, *args, **kwargs):  # pragma: no cover —— 本用例不触发
        pass

    def error(self, *args, **kwargs):  # pragma: no cover —— 本用例不触发
        pass


@pytest.fixture
def root(tmp_path, monkeypatch):
    """把部署根目录指到一个空的临时目录。"""
    monkeypatch.setattr(pdt_requirements, "_deployment_root", lambda: tmp_path)
    return tmp_path


def _self_obj(logger, scene=None):
    """造一个只带日志所需属性的假 ``self``。

    ``scene`` 缺省时**不**放这个属性 —— "对象没有 scene 属性"是契约里的一
    条，不能被替身顺手补上。
    """
    obj = types.SimpleNamespace(
        logger=logger,
        _estimate_tokens=lambda t: len(t or "") // 4,
    )
    if scene is not None:
        obj.scene = scene
    return obj


def _log(obj, prompt="p", system="s", phase="query", scene=None):
    """调用基类上的方法（不构造真 provider）。"""
    coding_tool.CodingTool._log_query_context(
        obj, prompt, system, phase=phase, scene=scene
    )
    assert obj.logger.records, "没有日志输出"
    return obj.logger.records[-1]


def _calls(cls: str, method: str, callee: str) -> list:
    """返回某个方法体里所有 ``callee(...)`` 调用节点。"""
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != cls:
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method:
                return [
                    n
                    for n in ast.walk(item)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == callee
                ]
    raise AssertionError(f"{cls}.{method} 在 {SRC} 里不存在")


def _has_scene_kwarg(call: ast.Call) -> bool:
    """调用里有没有把**本轮的** ``scene`` 按关键字传下去。"""
    for kw in call.keywords:
        if kw.arg != "scene":
            continue
        value = kw.value
        if isinstance(value, ast.Name) and value.id == "scene":
            return True
    return False


def test_fields_absent_without_files(root):
    """无生效文件 → data 里不出现那两个键（不是空值，是没有）。"""
    logger = _FakeLogger()

    record = _log(_self_obj(logger))

    assert "pdt_requirements_files" not in record["data"]
    assert "pdt_requirements_sha256" not in record["data"]


def test_fields_present_with_files(root):
    """有生效文件 → files 是相对路径、sha256 与 ``describe()`` 一致。"""
    (root / "PDT.md").write_text("base 要求", encoding="utf-8")
    logger = _FakeLogger()

    record = _log(_self_obj(logger), phase="query", scene="prd")

    described = pdt_requirements.describe("prd")
    assert record["data"]["pdt_requirements_files"] == described["files"]
    assert record["data"]["pdt_requirements_files"] == ["PDT.md"]
    assert record["data"]["pdt_requirements_sha256"] == described["sha256"]
    # 相对路径，不带部署根目录前缀
    assert not Path(record["data"]["pdt_requirements_files"][0]).is_absolute()
    # 既有的键还在（向后兼容）
    for key in (
        "phase",
        "prompt_tokens_est",
        "system_tokens_est",
        "total_tokens_est",
        "prompt_chars",
        "system_chars",
    ):
        assert key in record["data"], f"既有键 {key} 丢了"


def test_scene_falls_back_to_instance(root):
    """没传 scene → 用实例上的 ``scene``。"""
    (root / "PDT.prd.md").write_text("prd 要求", encoding="utf-8")
    logger = _FakeLogger()

    record = _log(_self_obj(logger, scene="prd"))

    assert record["data"]["pdt_requirements_files"] == ["PDT.prd.md"]


def test_no_scene_attribute(root):
    """对象连 ``scene`` 属性都没有 → 不抛 AttributeError。"""
    logger = _FakeLogger()
    obj = _self_obj(logger)
    assert not hasattr(obj, "scene")

    record = _log(obj)

    # 没有生效文件 → 那两个键不写；关键是**没有**抛异常。
    assert "pdt_requirements_files" not in record["data"]


def test_hook_available_on_base(root, tmp_path):
    """方法在基类上 → 四个 provider 都能继承调用。"""
    assert "_log_query_context" in coding_tool.CodingTool.__dict__
    # Claude 不再自己定义一份（否则"上移"只是复制）
    assert "_log_query_context" not in coding_tool.ClaudeCodingTool.__dict__
    # 既有调用语法不变：继承而来，仍能解析
    assert (
        coding_tool.ClaudeCodingTool._log_query_context
        is coding_tool.CodingTool._log_query_context
    )

    script_dir = tmp_path / "responses"
    script_dir.mkdir()
    (script_dir / "registry.json").write_text(
        json.dumps({"prompts": {}}, ensure_ascii=False), encoding="utf-8"
    )
    logger = _FakeLogger()
    tool = coding_tool.ScriptedCodingTool(script_dir=str(script_dir), logger=logger)

    tool._log_query_context("p", "s", phase="query", scene="prd")

    assert logger.records, "ScriptedCodingTool 上调用基类方法没有产出日志"


def test_call_sites_pass_scene():
    """Claude 的 query / query_json 两条路径都把 per-call scene 原样传给日志。

    两条路径的落点不同：``query`` 自己就记日志；``query_json`` 把活交给
    ``_query_json_internal``（超时重试会**再进**这个方法，所以日志留在那里
    才覆盖得到重试）。两条都要传 ``scene=scene`` —— 传的是别的 scene，
    日志里的哈希就跟真正注入的那块对不上了。
    """
    calls = _calls("ClaudeCodingTool", "query", "_log_query_context")
    assert calls, "ClaudeCodingTool.query 里找不到 _log_query_context 调用"
    for call in calls:
        assert _has_scene_kwarg(call), (
            "ClaudeCodingTool.query 的 _log_query_context 没传 scene=scene"
        )

    calls = _calls("ClaudeCodingTool", "_query_json_internal", "_log_query_context")
    assert calls, "ClaudeCodingTool._query_json_internal 里找不到 _log_query_context 调用"
    for call in calls:
        assert _has_scene_kwarg(call), (
            "ClaudeCodingTool._query_json_internal 的 _log_query_context 没传 scene=scene"
        )

    # query_json 把本轮的 scene 原样交给 _query_json_internal —— 少了这一跳，
    # 上面那个 scene=scene 拿到的是 None。
    forwards = _calls("ClaudeCodingTool", "query_json", "_query_json_internal")
    assert forwards, "ClaudeCodingTool.query_json 里找不到 _query_json_internal 调用"
    assert any(_has_scene_kwarg(call) for call in forwards), (
        "ClaudeCodingTool.query_json 没把 scene 传给 _query_json_internal"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
