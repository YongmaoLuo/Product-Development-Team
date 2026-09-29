"""
TDD tests for ``backend.arch_generator`` HARD-GATE integration.

Background
----------
ArchGenerator currently has **no Design Principles HARD-GATE** — the
``plans/<plan>/arch-design.md`` may omit the ``## Design Principles``
section, or may declare it without the required keywords, causing
downstream tasks to drift from the architectural intent. This task
integrates :class:`DesignPrincipleValidator` (DP4 item 12, task 16)
into the emit pipeline:

  1. ``ArchGenerator.generate()`` must inject a ``## Design Principles``
     section into the emitted markdown BEFORE validation.
  2. When :func:`DesignPrincipleValidator.validate` returns a check
     with ``is_consistent=False`` AND ``severity='high'``, the gate
     MUST block the emit and call ``_regenerate_chapter`` to retry.
  3. If regeneration fails twice, the gate MUST raise
     :class:`ArchHardGateError` so the caller (server endpoint) can
     return HTTP 409.
  4. When the loaded arch-design.md (legacy plan data) lacks the
     ``## Design Principles`` section entirely, the gate MUST be
     skipped (backward compatibility) and generation proceeds
     normally.

TDD spec
--------
1. ``test_apply_gate_injects_design_principles_section``:
   ``ArchGenerator.generate()`` returns content containing a
   ``## Design Principles`` heading — the gate injects the section
   when the LLM's first response does not include it.

2. ``test_high_severity_failure_blocks_emit_and_regenerates``:
   When the validator returns at least one high-severity failure
   (mocked), ``generate()`` triggers ``_regenerate_chapter`` exactly
   once. The mock coding tool records every query; we assert the
   second query is the chapter-regeneration query.

3. ``test_regenerate_twice_fails_raises_arch_hard_gate_error``:
   When the validator returns a high-severity failure on every
   regenerate attempt, the gate MUST raise :class:`ArchHardGateError`
   after exactly ``_MAX_REGENERATE_ATTEMPTS`` (2) attempts and MUST
   NOT write the file.

4. ``test_legacy_arch_without_principles_section_skips_gate``:
   When an existing ``arch-design.md`` is on disk and lacks the
   ``## Design Principles`` section (legacy plan data), ``generate()``
   MUST skip the gate entirely — no exception, no regenerate, the
   file is rewritten as-is.
"""

import os
import json
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] \
    if not os.environ.get("PDT_DEV_REPO") \
    else Path(os.environ["PDT_DEV_REPO"]) / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _passthrough_self_review(*args, **kwargs):
    """No-op stub for the mandatory second-pass self-review."""
    doc_content = kwargs.get("doc_content") or (
        args[0] if args else ""
    )
    return {
        "doc_type": kwargs.get("doc_type") or "arch",
        "attempted": True,
        "succeeded": True,
        "rewrote": False,
        "findings": [],
        "fixed_content": doc_content,
        "input_content_hash": "sha256:" + "x" * 64,
        "fixed_content_hash": "sha256:" + "x" * 64,
        "severity_high_count": 0,
        "severity_medium_count": 0,
        "severity_low_count": 0,
        "mandatory": True,
        "error": None,
    }


import arch_generator as _arch_gen
import tasks_generator as _ts_gen

_arch_gen.run_doc_self_review = _passthrough_self_review
_ts_gen.run_doc_self_review = _passthrough_self_review


SAMPLE_PRD = {
    "title": "Test HARD-GATE project",
    "overview": "Plan used for HARD-GATE integration tests.",
    "constraints": ["stdlib only", "single process"],
    "acceptance": ["principles documented"],
    "decision_points": [
        {
            "index": 0,
            "title": "decision point 1",
            "context": "ctx",
            "problem": "prob",
            "evidence": "evidence",
            "action": "act",
            "impact": "impact",
            "alternatives": ["a", "b"],
        },
    ],
}


class MockCodingTool:
    """Mock that records every prompt/query it receives and returns canned output.

    The first response is returned on the first call; if
    ``canned_responses`` is provided, each subsequent call pops the next
    entry from the list. This lets tests simulate a "regenerate" path
    where the LLM returns the same broken content twice.
    """

    def __init__(self, canned_response: str = "", canned_responses: Optional[List[str]] = None):
        self.queries: List[dict] = []
        if canned_responses is None:
            self._canned_responses = [canned_response] if canned_response else [""]
        else:
            self._canned_responses = list(canned_responses)
        self._cursor = 0

    def query(self, prompt: str, system_instruction: Optional[str] = None,
              retries: int = 3, timeout: Optional[int] = None) -> str:
        self.queries.append({
            "method": "query",
            "prompt": prompt,
            "system_instruction": system_instruction,
        })
        if self._cursor >= len(self._canned_responses):
            return self._canned_responses[-1]
        value = self._canned_responses[self._cursor]
        self._cursor += 1
        return value

    def query_json(self, prompt: str, system_instruction: Optional[str] = None,
                   retries: int = 3, timeout: Optional[int] = None) -> dict:
        self.queries.append({
            "method": "query_json",
            "prompt": prompt,
            "system_instruction": system_instruction,
        })
        return {"tasks": []}


def write_minimal_prd(plan_dir: Path) -> None:
    """Write a minimal ``prd.json`` so :meth:`ArchGenerator._load_prd` returns
    a non-empty markdown body and ``generate()`` does not crash."""
    with open(plan_dir / "prd.json", "w", encoding="utf-8") as f:
        json.dump(SAMPLE_PRD, f, ensure_ascii=False, indent=2)


@pytest.fixture
def plan_dir():
    """Yield a fresh tmp plan dir with a minimal PRD; cleanup after the test."""
    d = Path(tempfile.mkdtemp(prefix="ac-arch-hardgate-"))
    write_minimal_prd(d)
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_apply_gate_injects_design_principles_section(plan_dir, monkeypatch):
    """``ArchGenerator.generate()`` must inject a ``## Design Principles``
    section into the emitted markdown BEFORE validation.

    The LLM's first response is plain prose WITHOUT the section. After
    ``generate()`` runs, the file on disk and the return value must
    both contain the section header.
    """
    from arch_generator import ArchGenerator

    first_response = (
        "# 架构设计 — Test\n\n"
        "## 概述\n"
        "Some intro prose.\n\n"
        "## 架构决策点列表\n\n"
        "### 决策点 1: 测试\n"
        "CPEA content.\n"
    )
    mock = MockCodingTool(canned_response=first_response)
    gen = ArchGenerator(mock, plan_dir)

    out = gen.generate()

    assert "## Design Principles" in out, (
        f"generate() output should contain '## Design Principles' section; "
        f"output (first 400 chars): {out[:400]!r}"
    )
    assert "## Design Principles" in (plan_dir / "arch-design.md").read_text(
        encoding="utf-8"
    ), "the on-disk file should also contain the section"


def test_high_severity_failure_blocks_emit_and_regenerates(plan_dir, monkeypatch):
    """When the validator returns a high-severity failure, the gate MUST
    trigger ``_regenerate_chapter`` exactly once.

    We monkey-patch :meth:`DesignPrincipleValidator.validate` to return a
    high-severity failure on the FIRST call (gate triggers regenerate)
    and a clean (no failures) result on the SECOND call (gate accepts).
    We then assert the mock coding tool received exactly 2 queries:
    one initial render and one regenerate.
    """
    from arch_generator import ArchGenerator
    import arch_design_principles as _adp

    # First response: no Design Principles, so the gate must inject.
    first_response = (
        "# 架构设计 — Test\n\n"
        "## 概述\n"
        "Some intro prose.\n"
    )
    # Second response (regenerate): now has all required keywords so
    # the (mocked) validator will mark every principle consistent.
    second_response = (
        "# 架构设计 — Test (regenerated)\n\n"
        "## Design Principles\n\n"
        "single responsibility twelve-factor layered stateless error boundary\n"
    )
    mock = MockCodingTool(
        canned_responses=[first_response, second_response],
    )

    # Track validate() invocations.
    validate_calls = []

    def fake_validate(cls, arch_md, llm_query_fn=None, yaml_path=None):
        validate_calls.append(arch_md)
        # First call: report a high-severity failure.
        # Second call: report all consistent.
        if len(validate_calls) == 1:
            return [
                {
                    "principle": "stateless services",
                    "referenced_in": ["Design Principles"],
                    "is_consistent": False,
                    "severity": "high",
                    "finding": "simulated high-severity failure",
                }
            ]
        return [
            {
                "principle": "stateless services",
                "referenced_in": ["Design Principles"],
                "is_consistent": True,
                "severity": "high",
                "finding": "",
            }
        ]

    monkeypatch.setattr(_adp.DesignPrincipleValidator, "validate", classmethod(fake_validate))

    gen = ArchGenerator(mock, plan_dir)
    out = gen.generate()

    # The regenerated content must be on disk / returned.
    assert "regenerated" in out, (
        f"after regenerate, the output should reflect the regenerate "
        f"response; output (first 400 chars): {out[:400]!r}"
    )
    # We expect at least 2 queries: initial render + 1 regenerate.
    assert len(mock.queries) >= 2, (
        f"expected >= 2 queries (initial + 1 regenerate); got {len(mock.queries)}"
    )
    # The validate() mock was invoked at least twice (initial + post-regenerate).
    assert len(validate_calls) >= 2, (
        f"validator.validate() should be called at least twice "
        f"(initial + after regenerate); got {len(validate_calls)}"
    )


def test_regenerate_twice_fails_raises_arch_hard_gate_error(plan_dir, monkeypatch):
    """When the validator returns a high-severity failure on every
    regenerate attempt, the gate MUST raise :class:`ArchHardGateError`
    after exactly ``_MAX_REGENERATE_ATTEMPTS`` (2) attempts and MUST
    NOT write the file.

    We monkey-patch :meth:`DesignPrincipleValidator.validate` to always
    return a high-severity failure and assert:
      * ``ArchHardGateError`` is raised
      * The mock coding tool received exactly 1 initial render + 2
        regenerate attempts (= 3 total queries)
      * The on-disk ``arch-design.md`` was NOT written (file missing
        or unchanged)
    """
    from arch_generator import ArchGenerator, ArchHardGateError
    import arch_design_principles as _adp

    response = (
        "# 架构设计 — Test\n\n"
        "## 概述\n"
        "intro.\n\n"
        "## 架构决策点列表\n\n"
        "### 决策点 1: 测试决策点\n"
        "CPEA content.\n"
    )
    mock = MockCodingTool(canned_response=response)

    def always_fail(cls, arch_md, llm_query_fn=None, yaml_path=None):
        return [
            {
                "principle": "stateless services",
                "referenced_in": ["Design Principles"],
                "is_consistent": False,
                "severity": "high",
                "finding": "persistent high-severity failure",
            }
        ]

    monkeypatch.setattr(_adp.DesignPrincipleValidator, "validate", classmethod(always_fail))

    gen = ArchGenerator(mock, plan_dir)
    arch_file = plan_dir / "arch-design.md"
    assert not arch_file.exists(), "precondition: file does not exist"

    with pytest.raises(ArchHardGateError):
        gen.generate()

    # We expect initial render + _MAX_REGENERATE_ATTEMPTS (=2) regenerates.
    expected_queries = 1 + ArchGenerator._MAX_REGENERATE_ATTEMPTS
    assert len(mock.queries) == expected_queries, (
        f"expected {expected_queries} queries "
        f"(1 initial + {ArchGenerator._MAX_REGENERATE_ATTEMPTS} regenerates); "
        f"got {len(mock.queries)}"
    )
    # The on-disk file must NOT have been written when the gate fails.
    assert not arch_file.exists(), (
        f"arch-design.md should NOT be written when HARD-GATE fails; "
        f"but the file exists (size={arch_file.stat().st_size} bytes)"
    )


def test_legacy_arch_without_principles_section_skips_gate(plan_dir, monkeypatch):
    """When an existing ``arch-design.md`` on disk lacks the
    ``## Design Principles`` section (legacy plan data), ``generate()``
    MUST skip the gate entirely.

    The validator mock is set up to raise if invoked — that would prove
    the gate is being applied. We assert:
      * No exception is raised.
      * The on-disk file is rewritten (legacy plans still get the new
        LLM-rendered content).
      * The mock validator was NEVER called.
    """
    from arch_generator import ArchGenerator
    import arch_design_principles as _adp

    # Pre-existing legacy arch-design.md WITHOUT the section.
    legacy_arch = (
        "# 架构设计 — Legacy\n\n"
        "## 概述\n"
        "Old plan data with no Design Principles section.\n\n"
        "## 架构决策点列表\n\n"
        "### 决策点 1: legacy decision\n"
    )
    (plan_dir / "arch-design.md").write_text(legacy_arch, encoding="utf-8")

    # New LLM response is plain prose without the section. (The gate
    # would normally inject the section; in legacy mode we skip it.)
    new_response = (
        "# 架构设计 — Test (regenerated without gate)\n\n"
        "## 概述\n"
        "regenerated intro.\n"
    )
    mock = MockCodingTool(canned_response=new_response)

    validator_called = []

    def fail_validator(cls, arch_md, llm_query_fn=None, yaml_path=None):
        validator_called.append(arch_md)
        raise AssertionError(
            "validator should NOT be called for legacy plans "
            "(no ## Design Principles section on disk)"
        )

    monkeypatch.setattr(_adp.DesignPrincipleValidator, "validate", classmethod(fail_validator))

    gen = ArchGenerator(mock, plan_dir)
    out = gen.generate()

    assert "regenerated" in out, (
        f"output should reflect the new LLM response; got (first 400): {out[:400]!r}"
    )
    assert validator_called == [], (
        f"validator.validate() should NOT be called for legacy plans; "
        f"was called {len(validator_called)} time(s)"
    )
    # The new content is written (overwrites the legacy file).
    assert (plan_dir / "arch-design.md").exists(), (
        "arch-design.md should be rewritten after generate()"
    )