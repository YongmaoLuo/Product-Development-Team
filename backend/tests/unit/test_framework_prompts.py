"""Tests for ``framework.prompts`` — the single home for framework-shared
inline prompt constants.

Architecture context
--------------------
Architecture review finding (2026-09-04): ``verification_agent.py`` did a
lazy ``from agent import INLINE_SPEC_CODE_REVIEW_PROMPT`` inside
``_supplement_spec_code_review`` to avoid a hard coupling. That cycle
breaks if ``agent`` is imported before ``verification_agent`` registers
the symbol, and it forces every test that touches the prompt to import
``agent`` (and all of its transitive imports).

The fix: move the prompt constant into
``framework/prompts.py``. Both ``agent`` and ``verification_agent``
import from there. The cycle is broken because ``framework.prompts``
depends on neither module.

These tests pin the contract for the new module:

  1. ``framework.prompts`` is importable and exports
     ``INLINE_SPEC_CODE_REVIEW_PROMPT``.
  2. The constant is a non-empty ``str``.
  3. The constant contains both ``{task_desc}`` and ``{git_diff_stat}``
     placeholders that ``.format(...)`` consumes at call sites in
     ``agent._inline_spec_code_review`` and
     ``verification_agent._supplement_spec_code_review``.
  4. ``format(task_desc=..., git_diff_stat=...)`` returns a ``str``
     that does NOT still contain any leftover ``{...}`` placeholder.
  5. The constant's body references the four envelope fields the LLM
     is expected to emit (``spec_compliance``, ``code_quality``,
     ``should_block``, ``reason``).
  6. ``framework.prompts`` has no third-party dependencies — only the
     standard library. This guards against the new module accidentally
     pulling in heavy transitive imports (the original problem we
     moved it to break).
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path


PROMPTS_MODULE = "framework.prompts"


def _import_fresh_prompts_module():
    """Import ``framework.prompts`` fresh (no cached bytecode).

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


def test_prompts_module_exports_inline_spec_code_review_prompt():
    """The constant is exported under its expected name."""
    mod = _import_fresh_prompts_module()
    assert hasattr(mod, "INLINE_SPEC_CODE_REVIEW_PROMPT"), (
        "framework.prompts must expose INLINE_SPEC_CODE_REVIEW_PROMPT; "
        f"actual attrs: {sorted(a for a in dir(mod) if not a.startswith('_'))}"
    )


def test_inline_spec_code_review_prompt_is_non_empty_string():
    """The prompt is a non-empty string — not a tuple / None / bytes."""
    mod = _import_fresh_prompts_module()
    prompt = mod.INLINE_SPEC_CODE_REVIEW_PROMPT
    assert isinstance(prompt, str), (
        f"expected str, got {type(prompt).__name__}"
    )
    assert prompt.strip(), "prompt must not be empty / whitespace-only"


def test_inline_spec_code_review_prompt_has_required_placeholders():
    """Both ``{task_desc}`` and ``{git_diff_stat}`` are present and named
    exactly as the two call sites pass them in via ``.format(...)``.
    """
    mod = _import_fresh_prompts_module()
    prompt = mod.INLINE_SPEC_CODE_REVIEW_PROMPT
    assert "{task_desc}" in prompt, (
        "prompt must declare {task_desc} placeholder; "
        "callers pass task_desc=... to .format(...)"
    )
    assert "{git_diff_stat}" in prompt, (
        "prompt must declare {git_diff_stat} placeholder; "
        "callers pass git_diff_stat=... to .format(...)"
    )


def test_inline_spec_code_review_prompt_formats_cleanly():
    """``.format(task_desc=..., git_diff_stat=...)`` produces a string
    with no leftover ``{...}`` placeholders — otherwise ``KeyError`` /
    stray braces leak into the LLM prompt.
    """
    mod = _import_fresh_prompts_module()
    rendered = mod.INLINE_SPEC_CODE_REVIEW_PROMPT.format(
        task_desc="sample task description",
        git_diff_stat=" file.py | 1 +",
    )
    assert isinstance(rendered, str)
    assert "{task_desc}" not in rendered
    assert "{git_diff_stat}" not in rendered


def test_inline_spec_code_review_prompt_enumerates_envelope_fields():
    """The prompt lists the four envelope fields the LLM must emit.

    This protects against a refactor that drops one of them (the LLM
    parser in ``agent._inline_spec_code_review`` and
    ``verification_agent._supplement_spec_code_review`` reads exactly
    these four keys).
    """
    mod = _import_fresh_prompts_module()
    prompt = mod.INLINE_SPEC_CODE_REVIEW_PROMPT
    for field in ("spec_compliance", "code_quality", "should_block", "reason"):
        assert field in prompt, (
            f"prompt must reference envelope field {field!r}; "
            "the LLM is expected to emit it in its JSON response"
        )


def test_prompts_module_only_uses_stdlib():
    """``framework.prompts`` must not import anything that would re-create
    the heavy transitive-import chain ``verification_agent`` was trying
    to avoid by lazy-importing from ``agent``.

    Concretely: every top-level ``import`` in the module must resolve
    to a stdlib module (top-level package name in :data:`sys.stdlib_module_names`).

    Python 3.9 compatibility: ``sys.stdlib_module_names`` only exists on
    3.10+, and the CI PR gate runs a 3.9 venv — fall back to resolving
    each module's origin against the stdlib directory from sysconfig.
    """
    src_path = Path(__file__).resolve().parent.parent.parent / "framework" / "prompts.py"
    assert src_path.exists(), (
        f"expected framework/prompts.py at {src_path}"
    )
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is not None:
        def _is_stdlib(top: str) -> bool:
            return top in stdlib
    else:
        import importlib.util
        import sysconfig

        _stdlib_dir = str(Path(sysconfig.get_path("stdlib")).resolve())

        def _is_stdlib(top: str) -> bool:
            if top in sys.builtin_module_names:
                return True
            try:
                spec = importlib.util.find_spec(top)
            except (ImportError, ValueError):
                return False
            if spec is None:
                return False
            if spec.origin:
                return _stdlib_dir in str(Path(spec.origin).resolve())
            locations = spec.submodule_search_locations or ()
            return any(
                _stdlib_dir in str(Path(loc).resolve()) for loc in locations
            )

    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if not _is_stdlib(top):
                    bad.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".", 1)[0]
            if top and not _is_stdlib(top):
                bad.append(node.module or "")
    assert not bad, (
        "framework/prompts must depend on the standard library only; "
        f"found non-stdlib imports: {bad}"
    )