"""Tests for ``prompts`` (the top-level backend module) — the single home for
framework-shared inline prompt constants.

Architecture context
--------------------
The backend's agent loop and verification loop both pass inline prompt
strings to the LLM. Historically these were scattered:

  * ``framework.prompts.INLINE_SPEC_CODE_REVIEW_PROMPT`` — the
    adversarial self-review template invoked by
    :meth:`agent.AutonomousAgent._inline_spec_code_review` and
    :meth:`verification_agent.VerificationAgent._supplement_spec_code_review`.
    Already centralised in :mod:`framework.prompts` (cycle-breaking
    leaf module) by the 2026-09-04 architecture review.

  * ``verification_agent.VERIFICATION_PLAN_SYSTEM_PROMPT`` and
    ``verification_agent.VERIFICATION_JUDGMENT_SYSTEM_PROMPT`` — the
    verification phase-1 planning and phase-3 judgment system prompts,
    previously defined inline at module top of
    :mod:`verification_agent`.

This new module hoists all three into a single canonical home at the
backend root level (paralleling :mod:`backend.config_paths`). New code
SHOULD import from :mod:`prompts`; :mod:`framework.prompts` is
preserved as a leaf re-export so existing ``from framework.prompts
import INLINE_SPEC_CODE_REVIEW_PROMPT`` call sites keep working.

These tests pin the contract for the new module:

  1. ``prompts`` is importable and exposes the three constants.
  2. Each constant is a non-empty ``str``.
  3. ``INLINE_SPEC_CODE_REVIEW_PROMPT`` is the same string as
     :data:`framework.prompts.INLINE_SPEC_CODE_REVIEW_PROMPT` (single
     source of truth — no drift between the two access paths).
  4. ``VERIFICATION_PLAN_SYSTEM_PROMPT`` references every
     ``verification_subagent.SUPPORTED_METHODS`` value (derived, not
     hard-coded, so the prompt cannot offer a method the registry has no
     template for) and the ``verification_points`` JSON shape.
  5. ``VERIFICATION_JUDGMENT_SYSTEM_PROMPT`` references the four
     verdict enum values
     (``PASSED|FAILED|SKIPPED|PARTIAL``) and the
     ``requirement_deviations`` JSON shape.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


PROMPTS_MODULE = "prompts"
FRAMEWORK_PROMPTS_MODULE = "framework.prompts"


def _import_fresh_prompts_module():
    """Import ``prompts`` fresh (no cached bytecode).

    Some test runners keep module-level bytecode around between test
    files. Re-importing via ``importlib`` (after dropping the cached
    entry) keeps these tests independent of execution order.
    """
    sys.modules.pop(PROMPTS_MODULE, None)
    return importlib.import_module(PROMPTS_MODULE)


def test_prompts_module_is_importable():
    """The new module exists and imports without error."""
    mod = _import_fresh_prompts_module()
    assert mod is not None


def test_prompts_module_exports_three_constants():
    """The module exposes the three framework inline prompt constants."""
    mod = _import_fresh_prompts_module()
    expected = (
        "INLINE_SPEC_CODE_REVIEW_PROMPT",
        "VERIFICATION_PLAN_SYSTEM_PROMPT",
        "VERIFICATION_JUDGMENT_SYSTEM_PROMPT",
    )
    for name in expected:
        assert hasattr(mod, name), (
            f"backend.prompts must expose {name}; "
            f"actual attrs: {sorted(a for a in dir(mod) if not a.startswith('_'))}"
        )


def test_prompts_constants_are_non_empty_strings():
    """Each constant is a non-empty string — not a tuple / None / bytes."""
    mod = _import_fresh_prompts_module()
    for name in (
        "INLINE_SPEC_CODE_REVIEW_PROMPT",
        "VERIFICATION_PLAN_SYSTEM_PROMPT",
        "VERIFICATION_JUDGMENT_SYSTEM_PROMPT",
    ):
        value = getattr(mod, name)
        assert isinstance(value, str), (
            f"{name} must be a str, got {type(value).__name__}"
        )
        assert value.strip(), f"{name} must not be empty / whitespace-only"


def test_inline_spec_code_review_prompt_matches_framework_prompts():
    """``prompts.INLINE_SPEC_CODE_REVIEW_PROMPT`` matches ``framework.prompts``.

    Single source of truth — the two access paths must point to the
    exact same string. A future edit to one without the other would
    silently let the inline review and the supplement review drift
    apart, defeating the entire point of consolidating them.
    """
    backend_mod = _import_fresh_prompts_module()
    framework_mod = importlib.import_module(FRAMEWORK_PROMPTS_MODULE)
    assert (
        backend_mod.INLINE_SPEC_CODE_REVIEW_PROMPT
        == framework_mod.INLINE_SPEC_CODE_REVIEW_PROMPT
    ), (
        "prompts.INLINE_SPEC_CODE_REVIEW_PROMPT must be the same object "
        "(or string-equal) to framework.prompts.INLINE_SPEC_CODE_REVIEW_PROMPT; "
        "drift here breaks the single-source-of-truth contract."
    )


def test_verification_plan_system_prompt_enumerates_methods_and_shape():
    """The verification plan prompt declares the verification_method enum
    and the ``verification_points`` JSON shape the LLM is expected to
    emit.
    """
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_PLAN_SYSTEM_PROMPT
    # 2026-09-18: the enum is derived from SUPPORTED_METHODS rather than
    # hard-coded, so the prompt and the code that builds the templates
    # cannot drift apart — a method the registry cannot build a template
    # for must not be offered to the planner.
    from verification_subagent import SUPPORTED_METHODS

    for method in SUPPORTED_METHODS:
        assert method in prompt, (
            f"VERIFICATION_PLAN_SYSTEM_PROMPT must reference verification "
            f"method {method!r}; the LLM is expected to emit it in its "
            f"JSON response."
        )
    # Retired methods must NOT be advertised — that is how the planner
    # would keep producing VPs nothing can execute.
    for retired in ("automated_test", "manual_check"):
        assert retired not in prompt, (
            f"VERIFICATION_PLAN_SYSTEM_PROMPT must not offer the retired "
            f"method {retired!r} to the planner."
        )
    # The JSON shape the LLM is expected to emit.
    assert "verification_points" in prompt, (
        "VERIFICATION_PLAN_SYSTEM_PROMPT must reference the "
        "'verification_points' JSON key the LLM is expected to emit."
    )


def test_verification_judgment_system_prompt_enumerates_verdicts_and_shape():
    """The verification judgment prompt declares the four-verdict enum
    and the ``requirement_deviations`` JSON shape.
    """
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_JUDGMENT_SYSTEM_PROMPT
    # The four verdict enum values the verification report schema
    # requires.
    for verdict in ("PASSED", "FAILED", "SKIPPED", "PARTIAL"):
        assert verdict in prompt, (
            f"VERIFICATION_JUDGMENT_SYSTEM_PROMPT must reference verdict "
            f"{verdict!r}; the LLM is expected to emit it in its JSON "
            f"response."
        )
    # The JSON shape the LLM is expected to emit.
    assert "requirement_deviations" in prompt, (
        "VERIFICATION_JUDGMENT_SYSTEM_PROMPT must reference the "
        "'requirement_deviations' JSON key the LLM is expected to emit."
    )


def test_prompts_module_resolves_to_backend_path():
    """``prompts`` resolves to ``backend/prompts.py`` (not framework or
    any other copy).

    Guard against a future split where ``prompts`` accidentally resolves
    to ``framework/prompts.py`` because of a stale sys.path entry — the
    two modules have different responsibilities and consumers must
    reach the backend-level aggregator.
    """
    mod = _import_fresh_prompts_module()
    expected = (Path(__file__).resolve().parent.parent.parent / "prompts.py").resolve()
    actual = Path(mod.__file__).resolve()
    assert actual == expected, (
        f"prompts module must resolve to backend/prompts.py at "
        f"{expected}, but it resolved to {actual}"
    )

# ---------------------------------------------------------------------------
# 两阶段示例的形状（2026-09-18, D4）
#
# 这一组钉的是那次「Phase 2 凭空消失」反向升级的根因：
#   * 提示词里唯一的示例 VP 带着 `"verification_phase": 1,
#     "phase_order": 1` —— 自相矛盾（规则说 phase_order 只在 Phase 2 填），
#     而它是模型见过的唯一形状；
#   * 一个 Phase 2 的示例都没有，模型不知道全量关卡长什么样。
# ---------------------------------------------------------------------------


def _example_plan_block(prompt: str) -> dict:
    """Extract the ``输出格式`` JSON example and parse it.

    Parsing is itself part of the contract: an example the model cannot
    even read as JSON is worse than no example.
    """
    import json

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


def test_prompt_example_is_parseable_json():
    mod = _import_fresh_prompts_module()
    example = _example_plan_block(mod.VERIFICATION_PLAN_SYSTEM_PROMPT)
    assert example["verification_points"], "示例必须至少有一个 VP"
    assert example["services"], "示例必须演示计划级 services 声明"


def test_phase_one_example_has_no_phase_order():
    """``phase_order`` 只对 Phase 2 有意义的规则，示例自己得先守。"""
    mod = _import_fresh_prompts_module()
    example = _example_plan_block(mod.VERIFICATION_PLAN_SYSTEM_PROMPT)
    phase_one = [
        vp for vp in example["verification_points"]
        if vp.get("verification_phase") == 1
    ]
    assert phase_one, "示例里必须有一条 Phase 1 的 VP"
    for vp in phase_one:
        assert "phase_order" not in vp, (
            f"Phase 1 的示例 VP {vp.get('id')} 不得带 phase_order —— "
            f"规则说该字段只在 Phase 2 填，示例自相矛盾会让模型照抄"
        )


def test_phase_two_examples_cover_both_gates():
    """Phase 2 必须示范两种关卡：``full_ci``(order 1) 与 ``e2e``(order 2)。"""
    mod = _import_fresh_prompts_module()
    example = _example_plan_block(mod.VERIFICATION_PLAN_SYSTEM_PROMPT)
    gates = [
        vp for vp in example["verification_points"]
        if vp.get("verification_phase") == 2
    ]
    by_order = {vp.get("phase_order"): vp.get("verification_method") for vp in gates}
    assert by_order.get(1) == "full_ci", (
        "phase_order=1 的示例必须是 full_ci（全量 Nightly CI）"
    )
    assert by_order.get(2) == "e2e", (
        "phase_order=2 的示例必须是 e2e（全量端到端）"
    )


def test_phase_two_examples_carry_their_required_fields():
    mod = _import_fresh_prompts_module()
    example = _example_plan_block(mod.VERIFICATION_PLAN_SYSTEM_PROMPT)
    by_order = {
        vp.get("phase_order"): vp
        for vp in example["verification_points"]
        if vp.get("verification_phase") == 2
    }
    assert by_order[1].get("ci_entry"), "full_ci 的示例必须带 ci_entry"
    assert by_order[2].get("target_url"), "e2e 的示例必须带 target_url"


def test_prompt_states_the_phase_two_completeness_rule():
    """项目里有入口就必须有关卡 —— 这条规则得写在提示词里。"""
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_PLAN_SYSTEM_PROMPT
    assert "什么时候必须有 Phase 2" in prompt
    assert "ci_local.py" in prompt
    assert "playwright.config" in prompt
    assert "白跑" in prompt, "要写明缺口会让这一轮白跑，否则模型照省略"


def test_prompt_no_longer_describes_phase_two_as_optional():
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_PLAN_SYSTEM_PROMPT
    assert "全量关卡，可选" not in prompt


def test_prompt_no_longer_talks_about_phase_two_commands():
    """``test_command`` 已经不存在；旧措辞会把模型引向不存在的字段。"""
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_PLAN_SYSTEM_PROMPT
    assert "Phase 2 的命令" not in prompt


def test_prompt_pins_methods_to_phases():
    mod = _import_fresh_prompts_module()
    prompt = mod.VERIFICATION_PLAN_SYSTEM_PROMPT
    assert "Phase 1 只能用" in prompt
    assert "full_ci" in prompt and "Phase 2 专用" in prompt


# ---------------------------------------------------------------------------
# api_test 断言规范表格 —— 由 verification_api_runner 的词汇表派生，
# 不是手抄。教 LLM 一个运行器不认的 spell，等于把它送进"永远判不了"。
# ---------------------------------------------------------------------------


def _api_test_table_section(prompt: str) -> str:
    """Slice out the api_test 断言规范 table from the planning prompt.

    Anchored on the section heading the prompt already names
    (``api_test 断言规范``); without an anchor a future re-order of
    surrounding prose would shift the slice and turn this test into a
    different contract.
    """
    anchor = "api_test 断言规范"
    start = prompt.index(anchor)
    end = prompt.find("**VP 的判定依据", start)
    assert end > start, (
        "could not find the closing '**VP 的判定依据' after the api_test "
        "table section — the anchor contract this test relies on has drifted"
    )
    return prompt[start:end]


def test_api_test_table_covers_every_subject():
    """Every canonical subject must appear in the api_test spec table.

    Pinning the spec table against ``verification_api_runner.SUBJECT_KEYS``
    means a future vocabulary change in the runner automatically drives a
    matching update in the prompt — a future contributor who adds a new
    subject to the runner and forgets the prompt is told here, not by a
    silently-passing plan.
    """
    from verification_api_runner import SUBJECT_KEYS

    mod = _import_fresh_prompts_module()
    section = _api_test_table_section(mod.VERIFICATION_PLAN_SYSTEM_PROMPT)

    for subject in SUBJECT_KEYS:
        # The body's example uses the literal subject key (e.g.
        # ``{"json_path": ...}``); presence of that token in the
        # section is the minimum contract.
        assert subject in section, (
            f"api_test spec table is missing subject {subject!r}; the "
            f"prompt teaches the LLM a smaller vocabulary than the runner "
            f"actually accepts ({list(SUBJECT_KEYS)}) — the two have drifted. "
            f"Add a row to _API_TEST_SUBJECT_DISPLAY in prompts.py or remove "
            f"the subject from verification_api_runner.SUBJECT_KEYS."
        )


def test_api_test_table_uses_self_contained_marker_consistently():
    """Subjects in SELF_CONTAINED_SUBJECTS get the 自带期望值 footer.

    A row whose subject is self-contained but lacks the marker would
    leave the LLM thinking it still has to write a comparator — making
    ``{\"status\": 200}`` get emitted as ``{\"status\": 200, \"equals\": 200}``
    and bounce off the schema validator.
    """
    from verification_api_runner import SELF_CONTAINED_SUBJECTS, SUBJECT_KEYS

    mod = _import_fresh_prompts_module()
    section = _api_test_table_section(mod.VERIFICATION_PLAN_SYSTEM_PROMPT)

    for subject in SUBJECT_KEYS:
        # Find the row whose leftmost cell the subject name corresponds to.
        # The simple heuristic: every row is "| <name> | `...{subject}...` | ... |"
        # so the bare ``{"<subject>":`` token is the unique marker for that
        # row's spelling.
        spelling_marker = f'"{subject}":'
        assert spelling_marker in section, (
            f"could not find the row for subject {subject!r} in the api_test "
            f"spec table; the spelling marker {spelling_marker!r} was not "
            f"present"
        )
        row_start = section.index(spelling_marker)
        row_end = section.find("\n", row_start)
        row = section[row_start:row_end if row_end != -1 else None]

        if subject in SELF_CONTAINED_SUBJECTS:
            assert "自带期望值" in row, (
                f"subject {subject!r} is in SELF_CONTAINED_SUBJECTS so its "
                f"row must carry the 自带期望值 footer; the row was: {row!r}"
            )
        else:
            assert "必须" in row and "带比较符" in row, (
                f"subject {subject!r} is NOT self-contained so its row must "
                f"teach '**必须**带比较符'; the row was: {row!r}"
            )


def test_api_test_table_lists_every_comparator():
    """Every canonical comparator must appear in the api_test comparators line.

    Same rationale as the subjects test — the prompt is the only place
    the LLM learns which spellings are valid, and a missing comparator
    means a plan that uses it (e.g. ``length_lte``) is generated by a
    vocabulary the validator will reject.
    """
    from verification_api_runner import COMPARATOR_KEYS

    mod = _import_fresh_prompts_module()
    section = _api_test_table_section(mod.VERIFICATION_PLAN_SYSTEM_PROMPT)

    for comparator in COMPARATOR_KEYS:
        assert comparator in section, (
            f"comparator {comparator!r} is in COMPARATOR_KEYS but is "
            f"missing from the api_test spec table comparators line; add "
            f"it to verification_api_runner.COMPARATOR_KEYS first, then "
            f"re-export from prompts._api_test_comparator_table."
        )


# ---------------------------------------------------------------------------
# 2026-09-27 (repair-r4-01-2): api_test 断言规范表格只能教 SUBJECT_KEYS ∪
# COMPARATOR_KEYS 里的键。表格里出现词汇表外的键 = 教模型一个永远判不了的
# 拼写。用一份契约性的 regex 抽取钉住这一行。
# ---------------------------------------------------------------------------


def test_api_test_table_keys_are_all_in_vocabulary():
    """Every key appearing in the api_test spec table must be in vocabulary.

    The api_test spec table is the only place the LLM learns the
    canonical spellings; if the table teaches a key (subject or
    comparator) the runner rejects, the LLM will generate a VP the
    framework flags as ``永远判不了``. The vocabulary is the union of
    :data:`verification_api_runner.SUBJECT_KEYS` (the four main
    subjects) and :data:`verification_api_runner.COMPARATOR_KEYS` (the
    comparators); anything else in the table is drift.
    """
    import re

    import verification_api_runner as runner

    mod = _import_fresh_prompts_module()
    keys = set(re.findall(r'"([a-z_]+)":', mod._API_TEST_TABLE))
    vocabulary = set(runner.SUBJECT_KEYS) | set(runner.COMPARATOR_KEYS)
    assert keys <= vocabulary, (
        f"api_test spec table teaches keys {sorted(keys - vocabulary)} "
        f"which are not in SUBJECT_KEYS ∪ COMPARATOR_KEYS; remove the "
        f"spellings from _API_TEST_SUBJECT_DISPLAY in prompts.py, or "
        f"add them back to verification_api_runner vocabulary first. "
        f"Table keys seen: {sorted(keys)}."
    )
