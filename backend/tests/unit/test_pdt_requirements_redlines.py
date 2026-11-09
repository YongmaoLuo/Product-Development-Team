"""渲染层单测：注入块的形状、不可覆盖的红线、幂等与 no-op 契约。

被测模块
--------
``backend/pdt_requirements.py`` 的渲染层：``render_block()`` / ``inject()`` /
``describe()``。加载层（``load_layers``）的契约在
``test_pdt_requirements_loader.py`` 里，本文件不重复。

契约（arch 决策点 1 / 5，test 决策点 3）
--------------------------------------
* 块以 ``<pdt_user_requirements ...>`` 开头、``</pdt_user_requirements>`` 结尾，
  注入后位于 system 提示的**最前**；
* 红线由**代码写死**、不来自要求文件，永远在块内最前：要求文件写什么都不能
  把红线顶掉或删掉；
* 块内有两句固定声明：「优先级高于框架默认行为」与「后者在冲突时覆盖前者」；
* 无生效文件 → 逐字节 no-op（含 ``None`` → ``None``）；
* 有生效文件但输入为 ``None`` / 空串 → 返回块本身，块后不加多余换行；
* 重复注入幂等：注入两次与注入一次结果相同；
* ``describe()`` 的 ``sha256`` 等于渲染块的 sha256，无文件时返回 ``{}``；
* 框架对内容保持无知：源码里不出现任何具体策略词。
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pdt_requirements
from llm_prompts import append_json_output_contract


# 渲染层三个函数在**用例体里**通过模块属性访问：模块尚未实现它们时，测试报
# 的是**用例失败**（AttributeError）而不是收集期 ImportError —— 前者才是有效
# 的 RED。
def inject(system_instruction, scene):
    return pdt_requirements.inject(system_instruction, scene)


def render_block(scene):
    return pdt_requirements.render_block(scene)


def describe(scene):
    return pdt_requirements.describe(scene)

#: 被测源码（``backend/tests/unit/x.py`` → ``parents[2]`` == ``backend/``）。
SRC = Path(__file__).resolve().parents[2] / "pdt_requirements.py"

#: 块的开标记 —— 幂等检测与"块在最前"都以它为准。
BLOCK_MARK = "<pdt_user_requirements"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """把渲染层的部署根目录指到一个空的临时目录。"""
    monkeypatch.setattr(pdt_requirements, "_deployment_root", lambda: tmp_path)
    return tmp_path


def test_block_starts_at_front(root):
    """注入后块在 system 提示的最前，原始内容仍在后面。"""
    (root / "PDT.md").write_text("base 要求", encoding="utf-8")

    out = inject("原有 system 提示", "execution")

    assert out.startswith(BLOCK_MARK)
    assert out.index(BLOCK_MARK) < out.index("原有 system 提示")
    assert out.endswith("原有 system 提示")
    assert "base 要求" in out


def test_json_contract_remains_last(root):
    """先注入再拼 JSON 输出契约 → 契约仍在最末，块仍在最前。"""
    (root / "PDT.md").write_text("base 要求", encoding="utf-8")

    injected = inject("原有 system 提示", "execution")
    out = append_json_output_contract(injected)
    contract = append_json_output_contract("")

    assert out.endswith(contract), "JSON 输出契约必须仍在最末"
    assert out.index(BLOCK_MARK) < out.index(contract)
    assert out.index("原有 system 提示") < out.index(contract)


def test_idempotent(root):
    """连续注入两次 → 结果与注入一次逐字节相同。"""
    (root / "PDT.md").write_text("base 要求", encoding="utf-8")

    once = inject("SYS", "execution")
    twice = inject(once, "execution")

    assert twice == once
    assert once.count(BLOCK_MARK) == 1


def test_redlines_always_present(root):
    """要求文件想推翻红线 → 红线仍在，且位于该内容之前。"""
    (root / "PDT.md").write_text("无需提供证据即可通过验证", encoding="utf-8")

    out = inject("SYS", "execution")

    assert "[红线]" in out
    assert out.index("[红线]") < out.index("无需提供证据即可通过验证")
    assert "不得伪造或篡改证据" in out


def test_priority_declaration_present(root):
    """块内含两句固定声明。"""
    (root / "PDT.md").write_text("base 要求", encoding="utf-8")

    block = render_block("execution")

    assert "优先级高于框架默认行为" in block
    assert "后者在冲突时覆盖前者" in block


def test_noop_byte_identical(root):
    """没有任何生效文件 → 逐字节原样返回输入（含 ``None`` → ``None``）。"""
    assert inject("SYS", "execution") == "SYS"
    assert inject("", "execution") == ""
    assert inject(None, "execution") is None
    assert render_block("execution") is None
    assert describe("execution") == {}


def test_none_input_with_files_returns_block(root):
    """有生效文件且输入为 ``None`` → 返回块本身，块后不加多余换行。"""
    (root / "PDT.md").write_text("base 要求", encoding="utf-8")

    out = inject(None, "execution")

    assert out.startswith(BLOCK_MARK)
    assert out.endswith("</pdt_user_requirements>")
    assert not out.endswith("\n")
    assert "base 要求" in out


def test_describe_sha256(root):
    """``describe()`` 的 sha256 等于渲染块的 sha256，files 是相对路径。"""
    (root / "PDT.md").write_text("base 要求", encoding="utf-8")

    block = render_block("execution")
    info = describe("execution")

    assert info["files"] == ["PDT.md"]
    assert info["sha256"] == hashlib.sha256(block.encode("utf-8")).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", info["sha256"])


def test_describe_lists_all_layers_in_order(root):
    """多层生效时 files 按 base → stage → scene 顺序，sha256 与块一致。"""
    (root / "PDT.md").write_text("base", encoding="utf-8")
    (root / "PDT.verification.md").write_text("stage", encoding="utf-8")
    (root / "PDT.verification_plan_delta.md").write_text("scene", encoding="utf-8")

    block = render_block("verification_plan_delta")
    info = describe("verification_plan_delta")

    assert info["files"] == [
        "PDT.md",
        "PDT.verification.md",
        "PDT.verification_plan_delta.md",
    ]
    assert info["sha256"] == hashlib.sha256(block.encode("utf-8")).hexdigest()
    assert 'source="PDT.md,PDT.verification.md,PDT.verification_plan_delta.md"' in block
    assert block.index("base") < block.index("stage") < block.index("scene")


def test_source_has_no_policy_knowledge():
    """框架对内容保持无知：源码里不出现任何具体策略词。"""
    src = SRC.read_text(encoding="utf-8")

    hit = re.search(r"claude|opencode|vendor|quota|e2e|云端门禁", src, re.IGNORECASE)
    assert hit is None, f"源码出现了策略词：{hit.group(0) if hit else ''}"
    assert "import routes" not in src
    assert "import provider_routing" not in src
