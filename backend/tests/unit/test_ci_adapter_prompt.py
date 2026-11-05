"""The planning prompt must point ``ci_entry`` at the project's CI adapter.

Architecture context
--------------------
The CI acceptance contract (2026-11-05) is a **single script call**:
every project ships ``<project>/scripts/ci_gate.sh`` and the framework
only checks that the script exists and honours the exit-code contract
(``0`` = green, ``1`` = red, ``3`` = "the machine cannot decide").  The
framework neither parses the script nor cares whether it runs the gate
locally, in the cloud, or inside a container.

The planning prompt used to say "写**仓库的 CI 总入口**" and demonstrated
an inline compound command (``cargo test --workspace && pytest tests/``),
which contradicts that contract: the model copies the inline form into
the generated plan, and ``validate_vp`` then rejects it as a
non-conforming ``ci_entry``.

These tests pin the two halves of the fix:

  1. the worked example writes ``bash scripts/ci_gate.sh``;
  2. the prompt states that the adapter script is provided by the project.

The rest of the contract is pinned here too — the 0/1/3 exit codes, the
``ci_timeout_seconds`` floor of 7200, the "入口不得收窄" rule and the set
of runtime field names — because the point of this change is to
*redirect* the prompt, not to loosen it.
"""

from __future__ import annotations

import importlib
import json
import sys


PROMPTS_MODULE = "prompts"

#: The one form the prompt may demonstrate for ``ci_entry``.
ADAPTER_ENTRY = "bash scripts/ci_gate.sh"

#: The runtime fields of a ``full_ci`` VP.  The adapter contract changes
#: what the prompt tells the model to write — it must not add, drop or
#: rename a single field the runtime reads.
FULL_CI_FIELDS = frozenset(
    {
        "id",
        "title",
        "related_prd_criteria",
        "verification_method",
        "verification_phase",
        "phase_order",
        "priority",
        "ci_entry",
        "ci_timeout_seconds",
        "expected_result",
    }
)


def _import_fresh_prompts_module():
    """Import ``prompts`` fresh (no cached bytecode)."""
    sys.modules.pop(PROMPTS_MODULE, None)
    return importlib.import_module(PROMPTS_MODULE)


def _example_plan_block(prompt: str) -> dict:
    """Extract the ``输出格式`` JSON example and parse it.

    Parsing is part of the contract: an example the model cannot read as
    JSON is worse than no example.
    """
    anchor = "输出格式（纯 JSON）："
    start = prompt.index(anchor) + len(anchor)
    start = prompt.index("{", start)
    depth = 0
    for index in range(start, len(prompt)):
        if prompt[index] == "{":
            depth += 1
        elif prompt[index] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(prompt[start:index + 1])
    raise AssertionError("unterminated JSON example in the planning prompt")


def _full_ci_example(prompt: str) -> dict:
    example = _example_plan_block(prompt)
    gates = [
        vp
        for vp in example["verification_points"]
        if vp.get("verification_method") == "full_ci"
    ]
    assert gates, "示例里必须有一条 full_ci 的 VP"
    return gates[0]


def test_prompt_example_uses_adapter_script():
    """The worked example points at the project's CI adapter script.

    Not "any CI entry" — the *one* form the runtime accepts: a single
    call to the adapter script the project provides.
    """
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_PLAN_SYSTEM_PROMPT
    gate = _full_ci_example(prompt)
    assert gate["ci_entry"] == ADAPTER_ENTRY, (
        f"full_ci 示例的 ci_entry 必须是 {ADAPTER_ENTRY!r}，"
        f"实际是 {gate['ci_entry']!r}"
    )
    assert ADAPTER_ENTRY in prompt


def test_prompt_says_project_owns_the_adapter():
    """The prompt says the adapter script belongs to the project.

    The framework only checks that the script exists and honours the
    exit-code contract; it does not parse the script and does not judge
    where it runs.  The model has to know that, otherwise it keeps
    inventing inline commands.
    """
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_PLAN_SYSTEM_PROMPT
    assert "适配脚本由项目提供" in prompt, (
        "提示词必须说明适配脚本由项目提供 —— 否则模型会以为框架自带一个"
    )
    assert "框架不解析" in prompt, (
        "提示词必须说明框架不解析脚本内容"
    )
    assert "容器" in prompt, (
        "提示词必须说明框架不判断适配脚本在本地 / 云端 / 容器里怎么跑"
    )


def _full_ci_spec_paragraph(prompt: str) -> str:
    """The ``full_ci`` bullet of the field-spec list."""
    start = prompt.index("   - full_ci: **Phase 2 专用")
    end = prompt.index("\n4. ", start)
    return prompt[start:end]


def test_prompt_drops_the_inline_compound_example():
    """The old inline demo must be gone — it is what the model copies."""
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_PLAN_SYSTEM_PROMPT
    spec = _full_ci_spec_paragraph(prompt)
    assert "（示例：`bash scripts/ci_gate.sh`）" in spec, (
        "full_ci 字段规范必须示范项目适配脚本这一种形态"
    )
    assert "python scripts/ci_local.py" not in spec, (
        "full_ci 字段规范仍在示范另一条内联脚本调用，而不是项目适配脚本"
    )
    assert "pytest tests/" not in spec, (
        "full_ci 字段规范仍在示范内联复合命令，与单条脚本契约冲突"
    )


def test_prompt_keeps_the_exit_code_contract():
    """0 / 1 / 3 must stay exactly as they were."""
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_PLAN_SYSTEM_PROMPT
    assert "退出码契约" in prompt
    assert "0 = 绿" in prompt
    assert "1 = " in prompt
    assert "3 = 机器答不了" in prompt


def test_prompt_keeps_the_budget_floor():
    """``ci_timeout_seconds`` floor of 7200 stays."""
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_PLAN_SYSTEM_PROMPT
    assert "≥ 7200" in prompt
    assert _full_ci_example(prompt)["ci_timeout_seconds"] >= 7200


def test_prompt_keeps_the_no_narrowing_rule():
    """The "入口不得收窄" paragraph stays — the adapter must run the
    whole-repo gate, not a selected subset."""
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_PLAN_SYSTEM_PROMPT
    assert "入口不得收窄" in prompt


def test_prompt_does_not_change_runtime_field_names():
    """The adapter contract changes the *value*, never the field set."""
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_PLAN_SYSTEM_PROMPT
    gate = _full_ci_example(prompt)
    assert set(gate) == set(FULL_CI_FIELDS), (
        f"full_ci 示例的字段集变了：多出 {sorted(set(gate) - FULL_CI_FIELDS)}，"
        f"少了 {sorted(FULL_CI_FIELDS - set(gate))}"
    )
