"""
TDD tests for ``backend.arch_design_principles`` — DesignPrincipleValidator
validates that ``plans/<plan>/arch-design.md`` declares each
predefined design principle in the ``## Design Principles`` section.

Background
----------
ArchGenerator currently has **no Design Principles HARD-GATE** — the
architecture document may omit key principles (stateless services,
data-logic separation, explicit error boundaries, etc.), causing
downstream tasks to drift from the architectural intent.

This module introduces:

* A pre-defined principle library in ``configs/arch_principles.yaml``
  with at least 5 entries (SOLID subset, 12-factor, layered
  architecture, stateless services, explicit error boundaries).
* :class:`DesignPrincipleValidator` with a classmethod
  ``DesignPrincipleValidator.validate(arch_design_md) -> List[PrincipleCheck]``
  that:
    1. Loads the principle library (yaml if present, else 5 builtin defaults).
    2. Calls an LLM (or a plugged-in ``llm_query_fn``) to judge whether
       each principle is declared in the ``## Design Principles`` section.
    3. If the LLM call fails, **degrades** to a rule-based matcher:
       a principle is treated as "declared" iff any keyword appears
       in the ``## Design Principles`` section's content.
    4. Returns ``[]`` for an empty input — no result, no blocking.

TDD spec
--------
1. ``test_validate_returns_principle_check_list``:
   For a representative ``arch-design.md`` containing a
   ``## Design Principles`` section with multiple keyword hits,
   ``DesignPrincipleValidator.validate(md)`` returns a
   ``List[PrincipleCheck]`` with the predefined principles.
   Each ``PrincipleCheck`` is a dict with the 4 expected keys:
   ``principle``, ``referenced_in``, ``is_consistent``, ``finding``.

2. ``test_llm_unavailable_degrades_to_keyword_match``:
   When the LLM call raises, the validator must still produce a
   ``List[PrincipleCheck]`` based on the rule-based keyword matcher.
   We assert:
     - the LLM is invoked exactly once (or the pluggable function
       fires);
     - the LLM raises (we use ``side_effect=Exception``);
     - the returned list is non-empty and contains a check for the
       "stateless services" principle with ``is_consistent=True``
       because the keyword ``stateless`` appears in the input.

3. ``test_missing_yaml_uses_builtin_defaults``:
   When ``configs/arch_principles.yaml`` is missing, the validator
   falls back to the **builtin** 5-principle default library. The
   returned list must therefore contain exactly 5 PrincipleCheck
   entries (one per builtin principle), even though the on-disk
   yaml does not exist. The builtin defaults include:
   solid_single_responsibility, twelve_factor_config,
   layered_architecture, stateless_services,
   explicit_error_boundaries.

4. ``test_empty_arch_returns_empty_list``:
   An empty / whitespace-only ``arch-design.md`` yields ``[]``
   (no PrincipleCheck produced, no blocking).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


SAMPLE_ARCH_MD = """\
# Architecture Design — Test Project

## 概述

A simple stateless service architecture following SOLID principles.

## 架构决策点列表

### 决策点 1: 单进程 daemon

**[C] 背景：** single-process daemon.
**[P] 问题：** must be simple.
**[A] 行动：** Use a single process.

## Design Principles

- **stateless services**: 状态不存储在服务进程内。
- **layered architecture**: 严格分层，每层只依赖下一层。
- **explicit error boundaries**: 跨层调用使用显式 Result/Either 类型。
- **twelve-factor config**: 配置全部通过环境变量注入。
- **single responsibility (SOLID)**: 每个模块只负责一件事。
"""


def test_validate_returns_principle_check_list():
    """A realistic ``arch-design.md`` produces a non-empty
    :class:`List`[:class:`PrincipleCheck`] with the 4 expected fields.

    The validator must:
      * load the principle library (yaml or builtin defaults);
      * parse the ``## Design Principles`` section;
      * for each principle, record whether the keyword / concept
        is referenced (and in which sub-section);
      * return a list of dicts, each carrying the 4 contract keys.
    """
    from arch_design_principles import DesignPrincipleValidator

    # Use the built-in defaults (no yaml access) — both branches
    # must produce a List[PrincipleCheck] with the same shape.
    result = DesignPrincipleValidator.validate(
        SAMPLE_ARCH_MD,
        llm_query_fn=None,
    )

    assert isinstance(result, list), (
        f"validate() should return a list, got {type(result).__name__}"
    )
    assert len(result) >= 1, (
        f"validate() should return at least 1 PrincipleCheck for a populated "
        f"arch-design.md; got {len(result)}"
    )
    for i, check in enumerate(result):
        assert isinstance(check, dict), (
            f"PrincipleCheck #{i} should be a dict, got {type(check).__name__}"
        )
        for required_key in ("principle", "referenced_in", "is_consistent", "finding"):
            assert required_key in check, (
                f"PrincipleCheck #{i} missing required key {required_key!r}; "
                f"got keys={list(check.keys())}"
            )
        assert isinstance(check["principle"], str), (
            f"PrincipleCheck #{i}['principle'] should be str, "
            f"got {type(check['principle']).__name__}"
        )
        assert isinstance(check["referenced_in"], list), (
            f"PrincipleCheck #{i}['referenced_in'] should be list, "
            f"got {type(check['referenced_in']).__name__}"
        )
        assert isinstance(check["is_consistent"], bool), (
            f"PrincipleCheck #{i}['is_consistent'] should be bool, "
            f"got {type(check['is_consistent']).__name__}"
        )
        assert isinstance(check["finding"], str), (
            f"PrincipleCheck #{i}['finding'] should be str, "
            f"got {type(check['finding']).__name__}"
        )


def test_llm_unavailable_degrades_to_keyword_match():
    """When the LLM call raises, the validator must fall back to a
    rule-based keyword matcher and still return a meaningful list.

    Specifically, for an ``arch-design.md`` whose
    ``## Design Principles`` section contains the keyword
    ``stateless``, the resulting PrincipleCheck for
    ``stateless services`` must have ``is_consistent == True`` even
    though the LLM call failed.
    """
    from arch_design_principles import DesignPrincipleValidator

    llm_query_fn = MagicMock(side_effect=Exception("simulated LLM outage"))
    arch_md = (
        "# Architecture\n\n"
        "## Design Principles\n\n"
        "- **stateless services**: services hold no local state.\n"
        "- **layered architecture**: strict layers.\n"
    )

    result = DesignPrincipleValidator.validate(
        arch_md,
        llm_query_fn=llm_query_fn,
    )

    # The LLM plug was attempted (it raised). The fallback path
    # ran and produced a non-empty list.
    assert llm_query_fn.call_count == 1, (
        f"llm_query_fn should be invoked exactly once; got {llm_query_fn.call_count}"
    )
    assert isinstance(result, list), (
        f"validate() should return a list after LLM failure; got {type(result).__name__}"
    )
    assert len(result) >= 1, (
        f"keyword fallback should produce at least one check; got {len(result)}"
    )

    # 'stateless services' is in the builtin default library and the
    # keyword 'stateless' is present in the input. The fallback must
    # have marked it consistent.
    stateless_checks = [
        c for c in result if c["principle"] == "stateless services"
    ]
    assert stateless_checks, (
        f"no PrincipleCheck for 'stateless services' in fallback result; "
        f"principles returned: {[c['principle'] for c in result]}"
    )
    assert stateless_checks[0]["is_consistent"] is True, (
        f"'stateless services' should be marked consistent by the keyword "
        f"fallback (since 'stateless' appears in the input); got {stateless_checks[0]}"
    )


def test_missing_yaml_uses_builtin_defaults(tmp_path, monkeypatch):
    """When ``configs/arch_principles.yaml`` is missing or unreadable,
    the validator falls back to the builtin default 5-principle library.

    The builtin library, by contract, contains exactly 5 entries
    (solid_single_responsibility, twelve_factor_config,
    layered_architecture, stateless_services,
    explicit_error_boundaries) — every one must appear in the returned
    list.

    We simulate "missing yaml" by patching :func:`DesignPrincipleValidator._default_yaml_path`
    to point inside ``tmp_path`` (a directory we control and where
    no yaml exists). The validator must NOT raise — it returns a
    :class:`List`[:class:`PrincipleCheck`] with 5 entries.
    """
    from arch_design_principles import DesignPrincipleValidator

    # Patch the yaml path to a non-existent file under tmp_path.
    missing_yaml = tmp_path / "does_not_exist.yaml"
    monkeypatch.setattr(
        DesignPrincipleValidator,
        "_default_yaml_path",
        classmethod(lambda cls: missing_yaml),
        raising=False,
    )

    # No declared principles in this arch-design.md — every builtin
    # default will produce a PrincipleCheck with is_consistent=False
    # (nothing referenced). We do NOT assert is_consistent values;
    # we only assert the 5 builtin principles are present.
    arch_md = "# Architecture\n\nNo principles declared.\n"

    result = DesignPrincipleValidator.validate(
        arch_md,
        llm_query_fn=None,
    )

    assert isinstance(result, list), (
        f"validate() should return a list when yaml is missing; got {type(result).__name__}"
    )
    assert len(result) == 5, (
        f"expected exactly 5 builtin default principles when yaml is missing; "
        f"got {len(result)}: {[c['principle'] for c in result]}"
    )
    builtin_principles = {c["principle"] for c in result}
    expected_principles = {
        "solid single responsibility",
        "twelve factor config",
        "layered architecture",
        "stateless services",
        "explicit error boundaries",
    }
    assert builtin_principles == expected_principles, (
        f"builtin default principle set mismatch; "
        f"expected {expected_principles}, got {builtin_principles}"
    )


def test_empty_arch_returns_empty_list():
    """An empty or whitespace-only ``arch-design.md`` yields ``[]``.

    The validator does NOT block on empty input — it returns an
    empty list so the caller can decide whether the document is
    actually missing (out of scope) versus just lacks a
    ``## Design Principles`` section.
    """
    from arch_design_principles import DesignPrincipleValidator

    # Empty string.
    result_empty = DesignPrincipleValidator.validate("", llm_query_fn=None)
    assert result_empty == [], (
        f"empty string should produce an empty list; got {result_empty!r}"
    )

    # Whitespace-only string.
    result_ws = DesignPrincipleValidator.validate("   \n\n  \t  \n", llm_query_fn=None)
    assert result_ws == [], (
        f"whitespace-only string should produce an empty list; got {result_ws!r}"
    )
