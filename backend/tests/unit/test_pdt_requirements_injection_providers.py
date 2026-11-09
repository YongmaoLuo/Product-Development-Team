"""provider 入口接入单测：四个 provider × 两个入口 = **八个**入口都注入。

被测对象
--------
``backend/coding_tool.py`` 的八个入口：

* ``ClaudeCodingTool.query`` / ``ClaudeCodingTool.query_json``
* ``VendorCCodingTool.query`` / ``VendorCCodingTool.query_json``
* ``OpenCodeCodingTool.query`` / ``OpenCodeCodingTool.query_json``
* ``ScriptedCodingTool.query`` / ``ScriptedCodingTool.query_json``

契约（arch 决策点 2，test 决策点 2）
------------------------------------
* 八个入口的**首条可执行语句**都必须调用 ``_apply_pdt_requirements`` ——
  早于任何 dispatch，早于 scripted 的早退；
* ``OpenCodeCodingTool`` 的 scene 由**构造函数**注入，由 ``create_coding_tool``
  转发（现状是工厂对 opencode 分支把 scene 丢了）；
* ``ScriptedCodingTool`` 把**注入后**的 system_instruction 记在
  ``last_system_instruction`` 上 —— scripted 模式下没有真的 provider 可观测，
  这个属性就是唯一的观测点；``query`` 与 ``query_json`` 都写；
* 未知 scene 只吃 base 层，不报错（安全回落）。

为什么读源码而不调用真方法
--------------------------
"第一句就注入"是**源码形状**的契约：真调用会花钱，而注入是否发生只能靠
system 提示的内容判断，那得先真的发一次请求。这里读 AST，所以不碰网络。

注意不能用 ``tests/app_source.py`` 的 ``find_def("query")``：它只返回
**第一个**同名定义（``CodingTool`` 的抽象桩），会检查错方法体。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool
import pdt_requirements


#: 被测源码（``backend/tests/unit/x.py`` → ``parents[2]`` == ``backend/``）。
SRC = Path(__file__).resolve().parents[2] / "coding_tool.py"

#: 八个入口 —— 四个 provider × ``query`` / ``query_json``。
ENTRYPOINTS: list[tuple[str, str]] = [
    ("ClaudeCodingTool", "query"),
    ("ClaudeCodingTool", "query_json"),
    ("VendorCCodingTool", "query"),
    ("VendorCCodingTool", "query_json"),
    ("OpenCodeCodingTool", "query"),
    ("OpenCodeCodingTool", "query_json"),
    ("ScriptedCodingTool", "query"),
    ("ScriptedCodingTool", "query_json"),
]


@pytest.fixture
def deploy_root(tmp_path, monkeypatch):
    """把部署根目录指到一个空的临时目录（PDT 要求文件放这里）。"""
    root = tmp_path / "deploy"
    root.mkdir()
    monkeypatch.setattr(pdt_requirements, "_deployment_root", lambda: root)
    return root


@pytest.fixture
def script_dir(tmp_path):
    """脚本目录：``registry.json`` + 同名的 ``<key>.txt`` 响应文件。

    契约：缺 ``registry.json`` 或缺同名响应文件时 ``ScriptedCodingTool``
    直接抛 ``ScriptedLLMError`` —— 双重保险不静默降级。
    """
    directory = tmp_path / "script"
    directory.mkdir()
    (directory / "registry.json").write_text(
        '{"prompts": {"hello": "hello"}}', encoding="utf-8"
    )
    (directory / "hello.txt").write_text("ok", encoding="utf-8")
    return directory


@pytest.fixture
def json_script_dir(tmp_path):
    """同上，但响应是 JSON —— ``query_json`` 的答案得能解析。"""
    directory = tmp_path / "script_json"
    directory.mkdir()
    (directory / "registry.json").write_text(
        '{"prompts": {"hello": "hello"}}', encoding="utf-8"
    )
    (directory / "hello.txt").write_text('{"ok": true}', encoding="utf-8")
    return directory


def _source() -> str:
    return SRC.read_text(encoding="utf-8")


def _body_without_docstring(func: ast.AST) -> list:
    """返回函数体语句列表，跳过 docstring。"""
    body = list(getattr(func, "body", []))
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _entrypoints() -> dict:
    """返回 ``{(类名, 方法名): 跳过 docstring 后的第一条语句源码}``。

    遍历 ``tree.body`` 里的 ``ast.ClassDef`` —— 只看模块顶层的类，不走
    ``ast.walk``，所以嵌套类里的同名方法不会混进来。
    """
    src = _source()
    tree = ast.parse(src)
    out: dict = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = _body_without_docstring(item)
            if not body:
                continue
            out[(node.name, item.name)] = ast.get_source_segment(src, body[0]) or ""
    return out


def test_all_eight_entrypoints_call_helper():
    """八个入口的首条语句都调用 ``_apply_pdt_requirements``。"""
    eps = _entrypoints()

    missing = [f"{cls}.{method}" for cls, method in ENTRYPOINTS if (cls, method) not in eps]
    assert not missing, f"这些入口在 {SRC} 里找不到: {missing}"

    for cls, method in ENTRYPOINTS:
        src = eps[(cls, method)]
        assert "_apply_pdt_requirements" in src, (
            f"{cls}.{method} 的首条语句没有注入: {src!r}"
        )


def test_opencode_scene_attribute():
    """OpenCode 的 scene 由构造函数注入。"""
    tool = coding_tool.OpenCodeCodingTool(scene="verification")

    assert tool.scene == "verification"


def test_factory_forwards_scene_to_opencode(monkeypatch):
    """工厂把 scene 转发给 opencode 分支（现状是丢弃）。"""
    monkeypatch.setattr(coding_tool, "scripted_mode_enabled", lambda: False)

    tool = coding_tool.create_coding_tool("opencode", scene="execution")

    assert isinstance(tool, coding_tool.OpenCodeCodingTool)
    assert tool.scene == "execution"


def test_scripted_records_injected(deploy_root, script_dir):
    """有生效的要求文件时，scripted 记录的是**注入后**的值。"""
    (deploy_root / "PDT.md").write_text("base 要求", encoding="utf-8")

    tool = coding_tool.ScriptedCodingTool(script_dir=str(script_dir), scene="prd")
    tool.query("hello", system_instruction="SYS")

    recorded = tool.last_system_instruction
    assert recorded.startswith("<pdt_user_requirements "), (
        f"记录的 system_instruction 不是注入块: {recorded!r}"
    )
    assert "base 要求" in recorded
    assert "SYS" in recorded


def test_scripted_negative_control(deploy_root, script_dir):
    """没有生效的要求文件时，逐字节原样记录入参。"""
    tool = coding_tool.ScriptedCodingTool(script_dir=str(script_dir), scene="prd")
    tool.query("hello", system_instruction="SYS")

    recorded = tool.last_system_instruction
    assert recorded == "SYS"
    assert recorded.encode("utf-8") == b"SYS"


def test_scripted_query_json_records_injected(deploy_root, json_script_dir):
    """``query_json`` 也写 ``last_system_instruction``（注入后的值）。"""
    (deploy_root / "PDT.md").write_text("base 要求", encoding="utf-8")

    tool = coding_tool.ScriptedCodingTool(script_dir=str(json_script_dir), scene="prd")
    tool.query_json("hello", system_instruction="SYS")

    recorded = tool.last_system_instruction
    assert recorded.startswith("<pdt_user_requirements ")
    assert "base 要求" in recorded


def test_unknown_scene_base_only(deploy_root, script_dir):
    """未知 scene 只吃 base 层，不报错。"""
    (deploy_root / "PDT.md").write_text("base 要求", encoding="utf-8")
    (deploy_root / "PDT.execution.md").write_text("execution 要求", encoding="utf-8")

    tool = coding_tool.ScriptedCodingTool(script_dir=str(script_dir), scene="no-such-scene")
    tool.query("hello", system_instruction="SYS")

    recorded = tool.last_system_instruction
    assert recorded.startswith("<pdt_user_requirements ")
    assert "base 要求" in recorded
    assert "execution 要求" not in recorded, "未知 scene 不该吃到阶段层"
