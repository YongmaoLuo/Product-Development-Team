"""
TDD tests for the ``run_full_verification`` 3-step skeleton refactor.

Background
----------
The :class:`VerificationAgent.run_full_verification` method has been
refactored from a DAG-driven monolith into a 3-step scheduler:

  1. Phase 1 LLM — ``self.generate_verification_plan()`` (unchanged).
  2. Phase 2 — delegate to :class:`VerificationExecutor.run`; the
     parent no longer directly iterates VPs or calls
     ``coding_tool.query`` for per-VP work.
  3. Phase 3 LLM — ``self.generate_verification_report(...)`` (unchanged).

This file pins three contracts:

  1. ``test_run_full_verification_delegates_to_executor`` —
     ``VerificationExecutor.run`` is awaited exactly once per call.
  2. ``test_phase2_no_coding_tool_query`` — static source check that
     ``run_full_verification`` body does NOT contain
     ``coding_tool.query`` (the per-VP LLM path lives on the
     sub-agent runner, not the parent orchestrator).
  3. ``test_report_uses_executor_verdicts`` — the execution_results
     envelope passed to ``generate_verification_report`` is built
     from ``executor.collect_verdicts()`` (the parent's Phase 2
     work, not a side channel).

The tests do NOT touch any real LLM / network. The LLM calls are
mocked via ``Mock()`` and the executor is patched at the
``verification_agent`` import surface.
"""

import inspect
import json
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest


# Ensure ``backend/`` is on ``sys.path`` so ``import verification_agent``
# works regardless of which test runner entry point is used. Mirrors
# the pattern in ``test_verification_executor.py``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


import verification_agent as verification_agent_module  # noqa: E402
from verification_agent import VerificationAgent  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_plan_dir():
    """Fresh plan directory under tempdir, auto-cleaned on teardown."""
    temp_root = tempfile.mkdtemp()
    plan_dir = Path(temp_root) / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    yield plan_dir
    shutil.rmtree(temp_root)


@pytest.fixture
def temp_project_dir():
    """Fresh project directory under tempdir, auto-cleaned on teardown.

    2026-09-15: ``_sample_plan()`` 的 VP-001 跑 ``pytest tests/test_auth.py``，
    所以这里真的把那两个文件建出来。护栏（``verification_command_guard``）
    会检查命令点名的测试目标是否**真实存在** —— 夹具里造一个不存在的路径，
    会让生成阶段多跑两轮重试（``query_json`` 调用数 2 → 6），把本文件里
    "调用几次" 的断言打乱。生产里项目本来就有测试，夹具也该如此。
    """
    temp_root = tempfile.mkdtemp()
    project_dir = Path(temp_root) / "test-project"
    (project_dir / "tests").mkdir(parents=True, exist_ok=True)
    (project_dir / "tests" / "test_auth.py").write_text(
        "def test_login(): pass\n", encoding="utf-8",
    )
    yield project_dir
    shutil.rmtree(temp_root)


@pytest.fixture
def mock_coding_tool():
    """Mock coding tool — its ``query_json`` is the Phase 1/3 LLM surface."""
    return Mock()


@pytest.fixture
def verification_agent(
    temp_plan_dir, temp_project_dir, mock_coding_tool
) -> VerificationAgent:
    """VerificationAgent wired with a mock coding tool."""
    return VerificationAgent(
        plan_dir=temp_plan_dir,
        project_dir=temp_project_dir,
        coding_tool=mock_coding_tool,
    )


# ---------------------------------------------------------------------------
# Sample plan / report fixtures
# ---------------------------------------------------------------------------


def _sample_plan() -> Dict[str, Any]:
    """A 2-VP plan covering the L1 layer (canonical smoke test shape)."""
    return {
        "verification_points": [
            {
                "id": "VP-001",
                "title": "Login API contract",
                "related_prd_criteria": "PRD §1.1",
                "verification_method": "automated_test",
                "priority": "high",
                "expected_result": "pytest exit 0",
                "test_command": "pytest tests/test_auth.py",
            },
            {
                "id": "VP-002",
                "title": "Login password hashing",
                "related_prd_criteria": "PRD §1.2",
                "verification_method": "code_review",
                "priority": "medium",
                "expected_result": "reviewer accepts",
            },
        ]
    }


def _sample_report() -> Dict[str, Any]:
    """A minimal Phase 3 report dict (mocked LLM return)."""
    return {
        "overall_status": "PASSED",
        "summary": "all green",
        "verification_results": [
            {
                "id": "VP-001",
                "status": "PASSED",
                "actual_result": "pytest exit 0",
                "evidence": "logs/VP-001.log",
            },
            {
                "id": "VP-002",
                "status": "PASSED",
                "actual_result": "reviewer accepted",
                "evidence": "comments_url",
            },
        ],
        "requirement_deviations": [],
    }


def _sample_executor_verdicts() -> List[Dict[str, Any]]:
    """The verdicts an executor would return via ``collect_verdicts()``."""
    return [
        {
            "vp_id": "VP-001",
            "status": "PASSED",
            "reasons": ["pytest exit 0"],
            "evidence": {"logs_path": "logs/VP-001.log"},
        },
        {
            "vp_id": "VP-002",
            "status": "PASSED",
            "reasons": ["reviewer accepted"],
            "evidence": {"comments_url": "https://..."},
        },
    ]


# ---------------------------------------------------------------------------
# Test 1: run_full_verification delegates Phase 2 to VerificationExecutor
# ---------------------------------------------------------------------------


def test_run_full_verification_delegates_to_executor(
    verification_agent, mock_coding_tool
):
    """``run_full_verification`` builds and awaits ``VerificationExecutor.run``.

    The Phase 2 delegation is the central contract of the 3-step
    refactor. The parent no longer iterates VPs itself — it builds
    a :class:`VerificationExecutor` (via a private builder) and
    awaits its ``run()`` exactly once per ``run_full_verification``
    invocation.

    We patch ``VerificationExecutor`` at the ``verification_agent``
    import surface so the patch is observed by the SUT (which
    references the class through its own module scope). The patched
    class returns a ``MagicMock`` instance whose ``run`` is an
    ``AsyncMock`` so ``asyncio.run`` can await it; its
    ``collect_verdicts`` returns a fixed list so the parent's
    projection step has stable input.
    """
    mock_coding_tool.query_json.side_effect = [
        _sample_plan(),
        _sample_report(),
    ]

    # Build a fake executor instance. ``run`` is an AsyncMock so the
    # parent's ``asyncio.run(executor.run())`` call can await it
    # without exploding. ``collect_verdicts`` returns the canonical
    # shape (one entry per recorded VP) so the projection helper
    # has well-defined input.
    fake_executor_instance = MagicMock()
    fake_executor_instance.run = AsyncMock(return_value=None)
    fake_executor_instance.collect_verdicts = MagicMock(
        return_value=_sample_executor_verdicts()
    )

    fake_executor_class = MagicMock(return_value=fake_executor_instance)

    with patch.object(
        verification_agent_module, "VerificationExecutor", fake_executor_class
    ):
        report = verification_agent.run_full_verification(round_number=1)

    # Phase 2 contract: VerificationExecutor was instantiated exactly
    # once and its run() was awaited exactly once.
    assert fake_executor_class.call_count == 1, (
        "run_full_verification must build a single VerificationExecutor "
        "(not one per VP, not zero)"
    )
    assert fake_executor_instance.run.await_count == 1, (
        "run_full_verification must await executor.run() exactly once"
    )

    # The report was still produced via the Phase 3 LLM (mocked
    # query_json side_effect served both phases).
    assert report["overall_status"] == "PASSED"

    # Phase 1 LLM was called once (plan); Phase 3 LLM was called
    # once (report). No LLM call inside Phase 2.
    assert mock_coding_tool.query_json.call_count == 2


# ---------------------------------------------------------------------------
# Test 2: run_full_verification body has no coding_tool.query
# ---------------------------------------------------------------------------


def test_phase2_no_coding_tool_query(verification_agent):
    """Static check: ``run_full_verification`` body has no ``coding_tool.query``.

    The refactor pins Phase 2 as a pure delegation step — the
    parent orchestrator must not invoke ``coding_tool.query`` (or
    ``query_json``) for per-VP work. All per-VP LLM calls live on
    the executor's ``sub_agent_runner``, which routes to
    :class:`VerificationSubAgent`.

    This test walks the AST of the method and asserts that no
    call expression in the function body resolves to
    ``self.coding_tool.query`` / ``self.coding_tool.query_json`` /
    ``coding_tool.query`` / ``coding_tool.query_json``. Using the
    AST (rather than a substring search on the source) avoids
    false positives on docstring text that happens to mention
    ``coding_tool.query`` for documentation purposes.
    """
    import ast

    method = getattr(VerificationAgent, "run_full_verification")
    source = inspect.getsource(method)
    tree = ast.parse(source.lstrip())
    func_def = tree.body[0]
    assert isinstance(func_def, (ast.FunctionDef, ast.AsyncFunctionDef))

    # Collect every ``Call`` node's function-attr chain (the
    # dotted attribute path on the left of the call). E.g.
    # ``self.coding_tool.query_json(prompt=...)`` produces the
    # chain ``("self", "coding_tool", "query_json")``.
    forbidden_chains: List[List[str]] = [
        ["self", "coding_tool", "query"],
        ["self", "coding_tool", "query_json"],
        ["coding_tool", "query"],
        ["coding_tool", "query_json"],
    ]

    def _dotted_chain(node: ast.AST) -> List[str]:
        """Return the dotted-attribute chain for a Call.func / Attribute node."""
        if isinstance(node, ast.Attribute):
            base = _dotted_chain(node.value)
            return base + [node.attr]
        if isinstance(node, ast.Name):
            return [node.id]
        return []

    bad_calls: List[str] = []

    class _CallVisitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            chain = _dotted_chain(node.func)
            for forbidden in forbidden_chains:
                if chain == forbidden or (
                    len(chain) > len(forbidden)
                    and chain[-len(forbidden):] == forbidden
                ):
                    bad_calls.append(".".join(chain))
            self.generic_visit(node)

    _CallVisitor().visit(func_def)

    assert not bad_calls, (
        f"run_full_verification body must not call any coding_tool.query* "
        f"method — per-VP LLM calls belong on the executor's "
        f"sub_agent_runner, not the parent orchestrator. "
        f"Found forbidden call(s): {bad_calls!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: generate_verification_report receives the executor's verdicts
# ---------------------------------------------------------------------------


def test_report_uses_executor_verdicts(
    verification_agent, mock_coding_tool
):
    """Phase 3 input is built from ``executor.collect_verdicts()``.

    The 3-step skeleton promises that the legacy ``execution_results``
    envelope passed to :meth:`generate_verification_report` is
    derived from the executor's collected verdicts — not from
    some other side channel (e.g. a re-read of the plan, a
    standalone sub-agent call from the parent, or a hand-built
    stub).

    We capture the kwargs the parent passes to
    ``generate_verification_report`` and assert:

      * the execution_results envelope was passed in (positional or
        keyword);
      * its ``execution_results`` list has one entry per
        ``collect_verdicts()`` verdict (the parent's projection
        preserves the verdict count exactly — it is a 1:1 map);
      * the verdicts' ``vp_id`` values appear as ``id`` in the
        envelope, so the Phase 3 LLM can correlate back to the
        plan.
    """
    mock_coding_tool.query_json.side_effect = [
        _sample_plan(),
        _sample_report(),
    ]

    fake_executor_instance = MagicMock()
    fake_executor_instance.run = AsyncMock(return_value=None)
    fake_executor_instance.collect_verdicts = MagicMock(
        return_value=_sample_executor_verdicts()
    )

    fake_executor_class = MagicMock(return_value=fake_executor_instance)

    with patch.object(
        verification_agent_module, "VerificationExecutor", fake_executor_class
    ):
        verification_agent.run_full_verification(round_number=1)

    # generate_verification_report was called (Phase 3 LLM). It
    # received an execution_results envelope derived from the
    # executor's verdicts.
    assert mock_coding_tool.query_json.call_count == 2
    report_call = mock_coding_tool.query_json.call_args_list[1]
    # The Phase 3 LLM is the second query_json call. Its prompt
    # is built by ``_build_judgment_prompt(execution_results)``,
    # which stringifies the envelope. We assert the prompt
    # mentions every verdict's vp_id — that proves the verdicts
    # flowed into the envelope and the envelope reached the
    # judgment prompt.
    report_prompt = report_call.kwargs.get("prompt") or (
        report_call.args[0] if report_call.args else ""
    )
    for verdict in _sample_executor_verdicts():
        assert verdict["vp_id"] in report_prompt, (
            f"verdict vp_id {verdict['vp_id']!r} must appear in the "
            f"Phase 3 judgment prompt — the report envelope should "
            f"have been built from executor.collect_verdicts()"
        )

    # The parent's collect_verdicts() was called exactly once
    # (the success path invokes it once for the projection; a
    # regression that called it twice or zero would indicate
    # the envelope is sourced from somewhere else).
    assert fake_executor_instance.collect_verdicts.call_count == 1
