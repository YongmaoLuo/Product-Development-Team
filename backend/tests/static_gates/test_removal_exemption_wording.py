"""Static gate: the off-topic VP removal exemption is worded as an exemption.

Why this gate exists
--------------------
既有硬规则是「绝不修改已有验证点、废弃只标记不删除」。A1′ 要真删**主题外**的
验证点（与本次计划主题无关的那一条），就得有一条措辞受限的豁免；没有它，规则
自相矛盾 —— 而规则自相矛盾时，读到它的模型与实现永远各取更宽松的那一半。

豁免一旦写成「可以删除」，它就不再是豁免，而是把规则改掉了。所以豁免的措辞
必须被**钉住**，钉的方式是文字而不是意图：

1. **三个限定词必须同时出现** —— ``llm_irrelevant``（判定值）、``审计已落盘``
   （证据已持久化）、``主题外``（范围）。少一个，豁免就退化成一个口子：只写
   「主题外」而不写判定值，判定失灵时的兜底也会被当成移除理由；只写判定值而
   不写审计，移除就没有证据，而「已归档」只是一句可以被抹掉的声明。
2. **不许出现无条件措辞** —— 「无条件删除」「全部清理」这类写法是把规则改掉，
   不是给它开豁免。
3. **规则主体必须还在** —— 「绝不修改已有验证点」这句原文必须保留。豁免是
   **追加**的一段，不是对原句的改写。

What it pins
------------
三处必须带豁免措辞的文本：

* ``backend/verification_plan_delta.py`` —— 规则模块本体。检查它的三种形态：
  模块 docstring、``DELTA_SYSTEM_PROMPT``、以及 ``build_delta_prompt`` 渲染出来
  的用户提示词（模型真正读到的那一份）。
* ``backend/prompts.py`` —— 验证相关的提示词常量。
* ``docs/architecture/verification.zh.md`` —— 架构文档里的豁免小节。

文件缺失 → **门禁失败**，不是 skip：豁免的措辞散了，整条 A1′ 就没有护栏，
而「跳过」会让这道门看起来是绿的。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

import pytest

#: Repository root — this file lives at
#: ``backend/tests/static_gates/test_removal_exemption_wording.py``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_DIR = _REPO_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

#: The three places that must carry the exemption wording.
_DELTA_MODULE = _BACKEND_DIR / "verification_plan_delta.py"
_PROMPTS_MODULE = _BACKEND_DIR / "prompts.py"
_ARCH_DOC = _REPO_ROOT / "docs" / "architecture" / "verification.zh.md"

#: The three qualifiers. 缺一不可。
REQUIRED_QUALIFIERS: tuple[str, ...] = ("llm_irrelevant", "审计已落盘", "主题外")

#: Wording that would turn the exemption into a blanket removal licence.
#: Built from fragments so this file's own source never carries the literal
#: phrases — the same reason the sibling attribution gate builds its phrase
#: list from fragments.
FORBIDDEN_WORDING: tuple[str, ...] = ("无条件" + "删除", "全部" + "清理")

#: The rule body the exemption is appended to. 它必须原样还在。
RULE_BODY = "绝不修改已有验证点"

#: The three targets, in the order the brief lists them.
_TARGETS: tuple[str, ...] = ("verification_plan_delta", "prompts", "arch_doc")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    """Read *path* — failing, not skipping, when it does not exist."""
    assert path.exists(), (
        f"{path.relative_to(_REPO_ROOT)} is missing — the removal exemption "
        "must be written down. A missing file is a missing guard, not a "
        "reason to skip this gate."
    )
    return path.read_text(encoding="utf-8")


def _delta_text() -> str:
    """The delta module's text in the three shapes it takes.

    The static source, the system-prompt constant and the rendered user
    prompt are three different surfaces: the model reads the last two,
    a reader of the file reads the first.  The exemption has to reach
    all of them, so all three are checked together.
    """
    source = _read(_DELTA_MODULE)

    from verification_plan_delta import DELTA_SYSTEM_PROMPT, build_delta_prompt

    rendered = build_delta_prompt({"verification_points": []}, [], [], 1)
    return "\n".join((source, DELTA_SYSTEM_PROMPT, rendered))


def _text_for(target: str) -> str:
    """The text of one target."""
    if target == "verification_plan_delta":
        return _delta_text()
    if target == "prompts":
        return _read(_PROMPTS_MODULE)
    if target == "arch_doc":
        return _read(_ARCH_DOC)
    raise AssertionError(f"unknown target {target!r}")


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", _TARGETS)
def test_exemption_qualifiers_present(target: str) -> None:
    """三处文本都同时出现三个限定词。

    缺任何一个，豁免就不再是豁免：没有判定值，判定失灵时的兜底会被当成
    移除理由；没有审计，「已归档」只是一句声明；没有范围，改判据也会被
    当成主题清理。
    """
    text = _text_for(target)
    missing = [qualifier for qualifier in REQUIRED_QUALIFIERS if qualifier not in text]
    assert not missing, (
        f"{target} 没有同时写全豁免的三个限定词 "
        f"{list(REQUIRED_QUALIFIERS)!r}，缺 {missing!r}。豁免必须同时限定："
        "判定值 llm_irrelevant、证据审计已落盘、范围主题外。"
    )


@pytest.mark.parametrize("target", _TARGETS)
def test_no_unconditional_removal_wording(target: str) -> None:
    """反向断言：没有「无条件删除」「全部清理」类措辞。"""
    text = _text_for(target)
    hits = [wording for wording in FORBIDDEN_WORDING if wording in text]
    assert not hits, (
        f"{target} 出现了无条件移除类措辞 {hits!r}。豁免是**追加**在既有规则"
        "之上的一段，不是把「绝不修改已有验证点、废弃只标记不删除」改写成"
        "「可以删除」。"
    )


def test_rule_body_unchanged() -> None:
    """「绝不修改已有验证点」原文仍在 —— 豁免没有改写规则主体。

    模型读到的是 :data:`DELTA_SYSTEM_PROMPT`，所以那句原文必须还在那份
    提示词里，而不是只活在某个 docstring 里。
    """
    source = _read(_DELTA_MODULE)
    assert RULE_BODY in source, (
        f"{_DELTA_MODULE.name} 里找不到规则主体的原文 {RULE_BODY!r}。"
        "豁免是追加的一段，不允许改写既有规则。"
    )

    from verification_plan_delta import DELTA_SYSTEM_PROMPT

    assert RULE_BODY in DELTA_SYSTEM_PROMPT, (
        f"DELTA_SYSTEM_PROMPT 里找不到规则主体的原文 {RULE_BODY!r} —— "
        "模型读到的是这份提示词，原文必须还在里面。"
    )


# ---------------------------------------------------------------------------
# The gate is not vacuous
# ---------------------------------------------------------------------------


def test_the_gate_actually_reads_three_targets() -> None:
    """三处文本都要真的读到东西 —— 否则上面的断言可能是在空文本上通过的。

    The three targets exist and each yields non-empty text.  A target
    that silently resolved to ``""`` would make every qualifier check
    above fail loudly (they are all required), but the *forbidden*
    wording check would pass vacuously — so this pins the shape.
    """
    texts: Dict[str, str] = {target: _text_for(target) for target in _TARGETS}
    empty = [target for target, text in texts.items() if not text.strip()]
    assert not empty, f"these targets resolved to empty text: {empty!r}"
