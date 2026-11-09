"""注入层单测：``coding_tool`` 的四个入口把部署级要求注入到 system 提示。

被测对象
--------
``backend/coding_tool.py`` 的 ``CodingTool._apply_pdt_requirements()``，以及
Claude / Vendor C 两组入口（``query`` 与 ``query_json``）。

契约（arch 决策点 2，test 决策点 2）
------------------------------------
* 注入必须落在**方法的第一条可执行语句**：早于 scripted 早退，早于配额清理。
  早于 scripted 早退是有意的 —— scripted 模式也必须看到同一份要求，否则
  同一份要求在两种模式下生效范围不同；
* scene 的取法：per-call scene > 实例 scene > base（两层都没有时仍按 base
  注入，不报错）；
* 没有生效的要求文件 → 逐字节 no-op（``None`` → ``None``）；
* 入口签名不许变：Vendor C 只给构造函数加 ``scene``，由
  ``create_coding_tool`` 转发。

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
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool
import pdt_requirements


#: 被测源码（``backend/tests/unit/x.py`` → ``parents[2]`` == ``backend/``）。
SRC = Path(__file__).resolve().parents[2] / "coding_tool.py"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """把部署根目录指到一个空的临时目录。"""
    monkeypatch.setattr(pdt_requirements, "_deployment_root", lambda: tmp_path)
    return tmp_path


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


def _statements(cls: str, method: str) -> list:
    """返回某个方法的全部语句源码（跳过 docstring）。"""
    src = _source()
    tree = ast.parse(src)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != cls:
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method:
                return [ast.get_source_segment(src, s) or "" for s in _body_without_docstring(item)]
    raise AssertionError(f"{cls}.{method} 在 {SRC} 里不存在")


def _helper(self_obj, system_instruction, scene=None):
    """直接调用未绑定的方法 —— 不构造真实 provider。"""
    return coding_tool.CodingTool._apply_pdt_requirements(self_obj, system_instruction, scene)


def test_helper_uses_instance_scene(root):
    """scene=None → 用实例上的 scene。"""
    (root / "PDT.md").write_text("base 要求", encoding="utf-8")
    (root / "PDT.prd.md").write_text("prd 要求", encoding="utf-8")

    out = _helper(types.SimpleNamespace(scene="prd"), "SYS")

    assert "PDT.prd.md" in out, "实例 scene 没被用上"


def test_helper_per_call_scene_wins(root):
    """显式 scene 覆盖实例上的 scene。"""
    (root / "PDT.md").write_text("base 要求", encoding="utf-8")
    (root / "PDT.execution.md").write_text("execution 要求", encoding="utf-8")
    (root / "PDT.prd.md").write_text("prd 要求", encoding="utf-8")

    out = _helper(types.SimpleNamespace(scene="prd"), "SYS", "execution")

    assert "PDT.execution.md" in out, "per-call scene 没被用上"
    assert "PDT.prd.md" not in out, "实例 scene 不该压过 per-call scene"


def test_helper_noop_byte_identical(root):
    """无生效文件 → 逐字节原样返回入参，不新增换行。"""
    out = _helper(types.SimpleNamespace(scene="prd"), "SYS")

    assert out == "SYS"
    assert out.encode("utf-8") == b"SYS"


def test_claude_entrypoints_inject():
    """Claude 的 query / query_json 首条语句都注入。"""
    eps = _entrypoints()

    for method in ("query", "query_json"):
        src = eps[("ClaudeCodingTool", method)]
        assert "_apply_pdt_requirements" in src, f"ClaudeCodingTool.{method} 的首条语句没有注入"


def test_vendorc_entrypoints_inject():
    """Vendor C 的 query / query_json 首条语句都注入。"""
    eps = _entrypoints()

    for method in ("query", "query_json"):
        src = eps[("VendorCCodingTool", method)]
        assert "_apply_pdt_requirements" in src, f"VendorCCodingTool.{method} 的首条语句没有注入"


def test_injection_precedes_scripted_early_return():
    """注入早于 scripted 早退 —— scripted 模式也必须看到同一份要求。"""
    for method in ("query", "query_json"):
        stmts = _statements("ClaudeCodingTool", method)
        assert "_apply_pdt_requirements" in stmts[0]

        scripted = [i for i, s in enumerate(stmts) if "scripted_mode_enabled" in s]
        assert scripted, f"ClaudeCodingTool.{method} 里找不到 scripted 早退"
        assert min(scripted) > 0, "scripted 早退必须排在注入之后"


def test_vendorc_scene_attribute():
    """Vendor C 的 scene 由构造函数注入。"""
    tool = coding_tool.VendorCCodingTool(scene="verification")

    assert tool.scene == "verification"


def test_factory_forwards_scene_to_vendorc(monkeypatch):
    """工厂把 scene 转发给 vendor-c 分支（现状是丢弃）。"""
    monkeypatch.setattr(coding_tool, "scripted_mode_enabled", lambda: False)

    tool = coding_tool.create_coding_tool("vendor-c", scene="execution")

    assert isinstance(tool, coding_tool.VendorCCodingTool)
    assert tool.scene == "execution"


def test_none_input_noop(root):
    """无生效文件时 ``None`` 进、``None`` 出。"""
    assert pdt_requirements.inject(None, "prd") is None
    assert _helper(types.SimpleNamespace(scene="prd"), None) is None