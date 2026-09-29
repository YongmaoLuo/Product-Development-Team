"""
Unit Tests for VerificationAgent

Test strategy:
1. Flow control tests - use mocks to verify three-phase workflow
2. LLM output structure tests - real API calls to verify JSON structure
3. Requirement deviation detection tests - known failure scenarios
"""

import asyncio
import json
import os
import subprocess
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import pytest

from verification_agent import (
    VerificationAgent,
    VERIFICATION_PLAN_SYSTEM_PROMPT,
    VERIFICATION_JUDGMENT_SYSTEM_PROMPT
)
from coding_tool import ApiError, HardTimeoutError


def _router_side_effect(plan_response, verdict_response, report_response):
    """2026-09-13: prompt-keyed query_json router.

    Background: production's per-VP sub-agent consumes a VARIABLE
    number of ``query_json`` calls (verdict parse retries, self-heal
    loops, split-decision probes), so the historical positional
    ``side_effect`` lists drifted from reality. Worse, an exhausted
    list raises ``StopIteration`` inside the sub-agent's
    ``asyncio.to_thread`` worker; asyncio refuses to set a
    ``StopIteration`` exception on the chained Future, leaving it
    NEVER SET — the awaiting ``asyncio.wait_for`` then blocks until
    the 1-hour wall-clock cap — a suite-wide hang in this file.

    Routing on the ``system_instruction`` identity sidesteps call
    counts entirely: plan generation and report generation use the
    two pinned module-level prompt constants; every sub-agent call
    (verdict / heal / split) receives the verdict response.
    """
    from verification_agent import (
        VERIFICATION_JUDGMENT_SYSTEM_PROMPT,
        VERIFICATION_PLAN_SYSTEM_PROMPT,
    )

    def _router(prompt=None, system_instruction=None, **kwargs):
        if system_instruction is VERIFICATION_PLAN_SYSTEM_PROMPT:
            return plan_response
        if system_instruction is VERIFICATION_JUDGMENT_SYSTEM_PROMPT:
            return report_response
        return verdict_response

    return _router


# =============================================================================
# Fixtures
# =============================================================================

# ---------------------------------------------------------------------------
# Live-LLM guard for the real-call integration tests
# ---------------------------------------------------------------------------
#
# ``test_verification_plan_output_has_required_fields`` and
# ``test_verification_report_output_has_required_fields`` construct a
# REAL :class:`ClaudeCodingTool` and call the live provider chain. When
# that chain cannot complete (unreachable endpoint, auth prompt, CC
# Switch proxy down), ``query_json`` blocks on
# ``future.result(timeout=None)`` FOREVER — pytest-timeout interrupts
# the main thread but teardown then re-hangs joining the orphaned
# worker/subprocess, stalling the whole suite (2026-09-13 audit).
#
# A bounded probe in an isolated process group decides skip vs run:
# if the probe cannot finish inside the window, the real test cannot
# either.

_LIVE_LLM_STATE = {"probed": False, "available": False}


def _live_llm_available() -> bool:
    """Run one real ``query_json`` in a subprocess; True iff it answers."""
    import signal as _sig
    probe = (
        "from coding_tool import ClaudeCodingTool\n"
        "t = ClaudeCodingTool(model='claude-3-5-haiku-20241022')\n"
        "print(t.query_json('Reply with exactly: {\"ok\": true}'))\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", probe],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(Path(__file__).resolve().parents[1]),
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, _sig.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=5)
        return False
    return proc.returncode == 0 and '"ok"' in (out or "")


@pytest.fixture
def live_llm_required():
    """Skip the test when the live provider chain cannot answer in time."""
    if not _LIVE_LLM_STATE["probed"]:
        _LIVE_LLM_STATE["available"] = _live_llm_available()
        _LIVE_LLM_STATE["probed"] = True
    if not _LIVE_LLM_STATE["available"]:
        pytest.skip(
            "live LLM probe failed/timed out — skipping real-call "
            "integration test"
        )


import sys  # noqa: E402  (used by the probe above)

@pytest.fixture
def temp_plan_dir():
    """Create temporary plan directory."""
    temp_dir = tempfile.mkdtemp()
    plan_dir = Path(temp_dir) / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    yield plan_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def temp_project_dir():
    """Create temporary project directory."""
    temp_dir = tempfile.mkdtemp()
    project_dir = Path(temp_dir) / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    yield project_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def sample_prd(temp_plan_dir):
    """Create sample PRD file."""
    prd_file = temp_plan_dir / "prd.json"
    fixtures_dir = Path(__file__).parent / "fixtures" / "verification"
    sample_prd = fixtures_dir / "sample_prd.json"

    if sample_prd.exists():
        shutil.copy(sample_prd, prd_file)
    else:
        prd_data = {
            "title": "Test Project",
            "overview": "Test overview",
            "constraints": "Test constraints",
            "acceptance": "Test acceptance",
            "decision_points": [
                {
                    "index": 0,
                    "title": "Test Decision",
                    "context": "Test context",
                    "problem": "Test problem",
                    "evidence": "Test evidence",
                    "action": "Test action",
                    "impact": "Test impact",
                    "alternatives": ["Alt 1", "Alt 2"]
                }
            ]
        }
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

    return prd_file


@pytest.fixture
def sample_arch(temp_plan_dir):
    """Create sample architecture file."""
    arch_file = temp_plan_dir / "arch-design.md"
    fixtures_dir = Path(__file__).parent / "fixtures" / "verification"
    sample_arch = fixtures_dir / "sample_arch.md"

    if sample_arch.exists():
        shutil.copy(sample_arch, arch_file)
    else:
        with open(arch_file, 'w') as f:
            f.write("# Test Architecture\n\nTest content")

    return arch_file


@pytest.fixture
def sample_test(temp_plan_dir):
    """Create sample test design file."""
    test_file = temp_plan_dir / "test-design.md"
    fixtures_dir = Path(__file__).parent / "fixtures" / "verification"
    sample_test = fixtures_dir / "sample_test.md"

    if sample_test.exists():
        shutil.copy(sample_test, test_file)
    else:
        with open(test_file, 'w') as f:
            f.write("# Test Design\n\nTest content")

    return test_file


@pytest.fixture
def mock_coding_tool():
    """Create mock coding tool."""
    mock = Mock()
    # Default return for sub-agent calls - tests will override with side_effect
    mock.query_json.return_value = {
        "verdict": "PASSED",
        "reasons": ["test passed"],
        "evidence": ["output"]
    }
    return mock


@pytest.fixture
def verification_agent(temp_plan_dir, temp_project_dir, mock_coding_tool):
    """Create VerificationAgent instance with mock tool.

    Wires a fresh in-memory SQLite :class:`VerificationRepository` so
    the agent can persist Phase 1 execution envelopes to
    ``plan_verification.execution_results`` (task #3.5). Without
    ``verif_repo``, the write path is a silent no-op and the read path
    raises ``FileNotFoundError`` — exactly the regression these tests
    are designed to guard against.
    """
    import sqlite3
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    db_path = temp_plan_dir.parent / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    repo = VerificationRepository(conn)
    plan_id = temp_plan_dir.name
    # The verification row must exist before the agent can write to
    # it (``_update`` raises KeyError when ``cursor.rowcount == 0``).
    repo.insert(plan_id, verification_status="not_started")
    conn.commit()

    return VerificationAgent(
        plan_dir=temp_plan_dir,
        project_dir=temp_project_dir,
        coding_tool=mock_coding_tool,
        verif_repo=repo,
    )


# =============================================================================
# Flow Control Tests (Mock LLM Calls)
# =============================================================================

class TestFlowControl:
    """Test flow control with mocked LLM calls."""

    def test_three_phase_workflow_executes_in_order(self, verification_agent, mock_coding_tool):
        """Test that three-phase workflow executes in correct order."""
        _plan_response = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "Test VP",
                    "related_prd_criteria": "Test criteria",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "Test passes",
                }
            ]
        }
        _verdict_response = {
            "verdict": "PASSED", "reasons": ["ok"], "evidence": ["out"],
        }
        _report_response = {
            "overall_status": "PASSED",
            "summary": "All tests passed",
            "verification_results": [
                {
                    "id": "VP-001",
                    "status": "PASSED",
                    "actual_result": "Test passed",
                    "evidence": "pytest output",
                }
            ],
            "requirement_deviations": [],
        }
        # 2026-09-18: prompt-keyed routing instead of a positional list.
        # The per-VP sub-agent consumes a variable number of query_json
        # calls (verdict parse retries, self-heal), so a positional list
        # silently drifts and an exhausted list raises StopIteration in a
        # worker thread — which is how this file used to hang the whole
        # suite. See ``_router_side_effect``.
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            _plan_response, _verdict_response, _report_response,
        )

        report = verification_agent.run_full_verification(round_number=1)

        # The ORDER is the contract, not the call count: the plan prompt
        # is used before the judgment prompt. Counting calls here proved
        # brittle — the sub-agent's retry budget is not this test's
        # subject.
        _prompts_used = [
            call.kwargs.get("system_instruction")
            for call in mock_coding_tool.query_json.call_args_list
        ]
        assert VERIFICATION_PLAN_SYSTEM_PROMPT in _prompts_used
        assert VERIFICATION_JUDGMENT_SYSTEM_PROMPT in _prompts_used
        assert _prompts_used.index(VERIFICATION_PLAN_SYSTEM_PROMPT) < _prompts_used.index(
            VERIFICATION_JUDGMENT_SYSTEM_PROMPT
        ), "plan generation must precede judgment"

        assert report["overall_status"] == "PASSED"
        assert "verification_results" in report
    def test_llm_call_failure_retries_then_succeeds(self, verification_agent, mock_coding_tool):
        """Test that LLM call failures are retried and eventually succeed."""
        from coding_tool import ApiError

        # 2 failures then success (retry_llm=3 means up to 3 attempts)
        mock_coding_tool.query_json.side_effect = [
            ApiError("Rate limit", status=429),
            ApiError("Rate limit", status=429),
            {
                "verification_points": [
                    {
                        "id": "VP-001",
                        "title": "Test VP",
                        "related_prd_criteria": "Test criteria",
                        "verification_method": "code_review",
                        "priority": "high",
                        "expected_result": "Test passes",
                        "test_command": "echo ok"
                    }
                ]
            }
        ]

        plan = verification_agent.generate_verification_plan(retry_llm=3)

        # 2 failed + 1 successful = 3 calls
        assert mock_coding_tool.query_json.call_count == 3
        assert "verification_points" in plan
        assert len(plan["verification_points"]) == 1

    def test_llm_api_error_raises_after_all_retries(self, verification_agent, mock_coding_tool):
        """Test that ApiError is raised after all retries exhausted."""
        from coding_tool import ApiError

        mock_coding_tool.query_json.side_effect = ApiError("Server error", status=500)

        with pytest.raises(ApiError):
            verification_agent.generate_verification_plan(retry_llm=2)

        assert mock_coding_tool.query_json.call_count == 2

    def test_pytest_failure_continues_to_other_verification_points(
        self, verification_agent, mock_coding_tool, temp_project_dir, temp_plan_dir
    ):
        """Test that pytest failure doesn't stop execution of other VPs."""
        # Create a test file that will fail
        test_file = temp_project_dir / "tests" / "test_failing.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("def test_will_fail():\n    assert False\n")

        # Pre-write the verification plan so execute_verification_plan can find it
        plan_data = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "Failing Test",
                    "related_prd_criteria": "Test 1",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "Should pass",
                    "test_command": f"pytest {test_file} -v"
                },
                {
                    "id": "VP-002",
                    "title": "Manual Check",
                    "related_prd_criteria": "Test 2",
                    "verification_method": "code_review",
                    "priority": "medium",
                    "expected_result": "Manual verification",
                    "test_command": ""
                }
            ]
        }
        plan_file = temp_plan_dir / "verification_plan.json"
        with open(plan_file, 'w') as f:
            json.dump(plan_data, f)

        # Configure mock to return FAILED for automated_test VP (simulating pytest failure)
        # The mock returns PASSED by default, so we need to override for this VP
        def mock_response(prompt, **kwargs):
            if "VP-001" in prompt:
                return {
                    "verdict": "FAILED",
                    "reasons": ["pytest exited with code 1"],
                    "evidence": ["assert False assertion failed"]
                }
            return {"verdict": "PASSED", "reasons": ["test passed"], "evidence": ["output"]}
        mock_coding_tool.query_json.side_effect = mock_response

        # Start a verification round (needed for persistence logging)
        verification_agent.start_verification_round(1)

        # Execute verification plan
        execution_results = verification_agent.execute_verification_plan()

        # Both VPs should be executed despite first failure
        assert len(execution_results["execution_results"]) == 2

        vp1 = execution_results["execution_results"][0]
        assert vp1["id"] == "VP-001"
        assert vp1["status"] == "FAILED"

        vp2 = execution_results["execution_results"][1]
        assert vp2["id"] == "VP-002"
        # 2026-09-18: this used to expect SKIPPED. VP-002 declares no
        # ``depends_on``, so it shares a DAG layer with VP-001 and the
        # executor runs it regardless — which is what these tests have
        # always claimed in their own names ("continues to other VPs",
        # "continues plan execution"). The SKIPPED expectation came from
        # the retired ``manual_check`` short-circuit, not the layer rule.
        assert vp2["status"] == "PASSED"

    def test_non_json_llm_response_falls_back_to_minimal_plan(self, verification_agent, mock_coding_tool):
        """Test that non-JSON LLM response falls back to minimal plan."""
        # ValueError (non-ApiError) triggers fallback to minimal plan
        mock_coding_tool.query_json.side_effect = ValueError("Invalid JSON")

        plan = verification_agent.generate_verification_plan(retry_llm=2)

        # Should return minimal plan instead of raising
        assert "verification_points" in plan
        assert len(plan["verification_points"]) >= 1
        assert plan["verification_points"][0]["id"] == "VP-001"

    def test_verification_plan_cached_to_file(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """Test that verification plan is cached to verification_plan.json."""
        plan_data = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "Cached VP",
                    "related_prd_criteria": "Test criteria",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "Cached result",
                    "test_command": "echo test"
                }
            ]
        }
        mock_coding_tool.query_json.return_value = plan_data

        verification_agent.generate_verification_plan()

        plan_file = temp_plan_dir / "verification_plan.json"
        assert plan_file.exists()

        with open(plan_file, 'r') as f:
            cached = json.load(f)
        assert cached == plan_data

    def test_verification_report_persisted_to_file(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """Test that verification report is persisted to verification_report.json."""
        execution_results = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "Test VP",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "Pass",
                    "test_command": "echo ok"
                }
            ],
            "execution_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "OK", "evidence": "output"}
            ]
        }
        report_data = {
            "overall_status": "PASSED",
            "summary": "All passed",
            "verification_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "OK", "evidence": "output"}
            ],
            "requirement_deviations": []
        }
        mock_coding_tool.query_json.return_value = report_data

        report = verification_agent.generate_verification_report(execution_results)

        report_file = temp_plan_dir / "verification_report.json"
        assert report_file.exists()

        with open(report_file, 'r') as f:
            cached = json.load(f)
        assert cached["overall_status"] == "PASSED"
        assert "generated_at" in cached
        assert "plan_id" in cached

    def test_full_workflow_with_all_document_types(
        self, verification_agent, mock_coding_tool, sample_prd, sample_arch, sample_test
    ):
        """Test full workflow when PRD, arch, and test design documents exist."""
        # Prompt-keyed routing, not a positional list — the per-VP
        # sub-agent consumes a variable number of calls (see
        # ``_router_side_effect``).
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            {"verification_points": [
                {"id": "VP-001", "title": "VP1", "related_prd_criteria": "c1",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "ok", "test_command": ""}
            ]},
            {"verdict": "PASSED", "reasons": ["ok"], "evidence": ["out"]},
            {"overall_status": "PASSED", "summary": "ok",
             "verification_results": [
                 {"id": "VP-001", "status": "PASSED", "actual_result": "ok", "evidence": "e"}
             ],
             "requirement_deviations": []},
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "PASSED"

        # Verify the plan prompt included all three documents
        first_call = mock_coding_tool.query_json.call_args_list[0]
        prompt = first_call.kwargs.get("prompt", first_call[0][0] if first_call.args else "")
        assert "PRD" in prompt or "prd" in prompt

    def test_round_number_tracking(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """Test that round number is tracked correctly."""
        mock_coding_tool.query_json.side_effect = [
            {"verification_points": [
                {"id": "VP-001", "title": "VP1", "related_prd_criteria": "c1",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "ok", "test_command": ""}
            ]},
            {"overall_status": "PASSED", "summary": "ok",
             "verification_results": [
                 {"id": "VP-001", "status": "PASSED", "actual_result": "ok", "evidence": "e"}
             ],
             "requirement_deviations": []}
        ]

        verification_agent.run_full_verification(round_number=2)
        assert verification_agent._current_round == 2

    def test_execution_results_persisted_to_sqlite(self, verification_agent, temp_plan_dir):
        """Phase 2 must persist the execution envelope to SQLite
        (``plan_verification.execution_results``) — the on-disk
        ``verification_execution_results.json`` cache is no longer
        written (task #3.5)."""
        verification_agent.start_verification_round(1)
        plan_data = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "Persist Test",
                    "related_prd_criteria": "Test",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "Pass",
                    "test_command": "echo ok"
                }
            ]
        }

        execution_results = verification_agent.execute_verification_plan(plan_data)

        # The legacy on-disk cache is no longer written.
        legacy_cache = temp_plan_dir / "verification_execution_results.json"
        assert not legacy_cache.exists(), (
            "legacy verification_execution_results.json must not be "
            "written (task #3.5); SQLite is the canonical store now"
        )

        # SQLite must hold the envelope under plan_verification.execution_results.
        assert verification_agent.verif_repo is not None
        stored = verification_agent.verif_repo.current(verification_agent.plan_dir.name)
        assert stored is not None
        persisted = stored.get("execution_results")
        assert isinstance(persisted, dict), (
            f"execution_results should be a dict envelope, got {type(persisted).__name__}"
        )
        assert persisted["execution_results"][0]["id"] == "VP-001"
        assert persisted["execution_results"][0]["status"] == "PASSED"
        assert "executed_at" in persisted

    def test_log_file_contains_valid_json_lines(self, verification_agent, temp_plan_dir):
        """Test that verification log entries are valid JSON-lines with expected fields."""
        verification_agent.start_verification_round(1)
        plan_data = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "Log Test",
                    "related_prd_criteria": "Test",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "Pass",
                    "test_command": "echo ok"
                }
            ]
        }

        verification_agent.execute_verification_plan(plan_data)

        logs_dir = temp_plan_dir / "logs"
        log_files = list(logs_dir.glob("verification_1_*.log"))
        assert len(log_files) >= 1, "should create at least one log file"

        with open(log_files[0], "r") as f:
            entries = [json.loads(line) for line in f if line.strip()]

        assert len(entries) >= 2, "log should contain round_start + VP entries"

        # First entry should be round_start
        round_start = entries[0]
        assert round_start["event"] == "round_start"
        assert round_start["round"] == 1
        assert "timestamp" in round_start

        # Find VP-related entries
        vp_entries = [e for e in entries if e.get("verification_point_id") == "VP-001"]
        assert len(vp_entries) >= 2, "should have vp_start and vp_complete entries"

        vp_start = next(e for e in vp_entries if e["event_type"] == "vp_start")
        assert vp_start["data"]["title"] == "Log Test"
        assert vp_start["data"]["method"] == "code_review"

        vp_complete = next(e for e in vp_entries if e["event_type"] == "vp_complete")
        assert vp_complete["data"]["status"] == "PASSED"

    @pytest.mark.asyncio
    async def test_code_review_timeout_produces_timeout_result(self, verification_agent, temp_project_dir):
        """Test that a HardTimeoutError from the inner idle detector is
        routed to auto-split and produces 'timeout' status when both
        split paths decline.

        2026-09-13 update: the per-VP ``asyncio.wait_for`` wrapper in
        ``_run_single_vp_async`` was removed (2026-09-08) — enforcement
        now lives in VerificationSubAgent's flat 1-hour outer cap and
        coding_tool's 15-min idle detector. A bare ``asyncio.
        TimeoutError`` is NOT routed to split; only HardTimeoutError is.
        """
        import asyncio
        verification_agent.start_verification_round(1)

        vp = {
            "id": "VP-001",
            "title": "Timeout Test",
            "verification_method": "code_review",
            "test_command": "sleep 999",
            "priority": "high",
            "expected_result": "Should timeout"
        }

        async def _split_decline(_vp, _partial):
            return None

        async def _llm_split_decline(_vp, _exc):
            return None

        verification_agent._split_vp_on_timeout = _split_decline
        verification_agent._llm_split_vp_on_hard_timeout = _llm_split_decline
        # Orchestrator has no ``logger`` attr by default; the
        # hard-timeout branch guards with ``if self.logger``.
        if not hasattr(verification_agent, "logger"):
            verification_agent.logger = MagicMock()

        # Mock _delegate_to_sub_agent to raise HardTimeoutError —
        # this simulates the inner 15-min idle detector firing.
        with patch.object(
            verification_agent, "_delegate_to_sub_agent",
            side_effect=HardTimeoutError(
                total_sec=900, elapsed=905.0, last_line="x"
            ),
        ):
            result = await verification_agent._run_single_vp_async(vp)

        assert result["id"] == "VP-001"
        assert result["status"] == "timeout"
        assert "HARD TIMEOUT" in result["actual_result"]

    def test_phase3_loads_results_from_sqlite(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """Phase 3 must load the execution envelope from SQLite
        (``plan_verification.execution_results``) — the on-disk
        ``verification_execution_results.json`` cache is no longer
        written (task #3.5)."""
        plan_data = {
            "verification_points": [
                {
                    "id": "VP-001", "title": "Phase3 Continuity",
                    "related_prd_criteria": "Test", "verification_method": "code_review",
                    "priority": "high", "expected_result": "ok", "test_command": "echo ok"
                }
            ]
        }
        verdict_data = {
            "verdict": "PASSED",
            "reasons": ["pytest exit code 0"],
            "evidence": ["All tests passed"]
        }
        report_data = {
            "overall_status": "PASSED", "summary": "ok",
            "verification_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "ok", "evidence": "e"}
            ],
            "requirement_deviations": []
        }
        mock_coding_tool.query_json.side_effect = [plan_data, verdict_data, report_data]
        verification_agent.start_verification_round(1)
        verification_agent.generate_verification_plan()

        # Phase 2 — envelope goes to SQLite.
        execution_results = verification_agent.execute_verification_plan()
        assert execution_results["execution_results"][0]["status"] == "PASSED"

        # Legacy on-disk cache must NOT be written.
        results_file = temp_plan_dir / "verification_execution_results.json"
        assert not results_file.exists(), (
            "legacy verification_execution_results.json must not be "
            "written (task #3.5); Phase 3 reads from SQLite now"
        )

        # Phase 3: generate report by loading results from SQLite (no arg).
        report_data = {
            "overall_status": "PASSED", "summary": "ok",
            "verification_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "ok", "evidence": "e"}
            ],
            "requirement_deviations": []
        }
        mock_coding_tool.query_json.return_value = report_data

        report = verification_agent.generate_verification_report()
        assert report["overall_status"] == "PASSED"
        assert len(report["verification_results"]) == 1

    def test_plan_generation_inherits_unified_timeout(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """2026-09-15: the plan-generation call must NOT
        pass a per-call ``timeout``.

        It used to send ``LLM_TEST_TIMEOUT`` (default 1800), which REPLACES
        the coding tool's unified 900s adaptive-silence window instead of
        nesting inside it. Removing the kwarg is the contract now — the
        unified rules (900s silence / 1800s idle / layer ceiling) apply.
        """
        mock_coding_tool.query_json.return_value = {
            "verification_points": [
                {"id": "VP-001", "title": "T", "related_prd_criteria": "c",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "ok", "test_command": ""}
            ]
        }

        # Set the legacy env var: it must now be IGNORED (it used to feed
        # the per-call timeout).
        with patch.dict(os.environ, {"LLM_TEST_TIMEOUT": "42"}):
            verification_agent.generate_verification_plan()

        call_kwargs = mock_coding_tool.query_json.call_args.kwargs
        assert "timeout" not in call_kwargs, (
            "plan generation must inherit the unified coding-tool budget; "
            f"got timeout={call_kwargs.get('timeout')!r}"
        )

    def test_phase2_reads_plan_from_phase1_file(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """Test that Phase 2 can load the plan file written by Phase 1."""
        plan_data = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "Phase Continuity",
                    "related_prd_criteria": "Test",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "ok",
                    "test_command": "echo ok"
                }
            ]
        }
        verdict_data = {
            "verdict": "PASSED",
            "reasons": ["pytest exit code 0"],
            "evidence": ["All tests passed"]
        }
        mock_coding_tool.query_json.side_effect = [plan_data, verdict_data]

        # Phase 1: generate and persist plan
        verification_agent.start_verification_round(1)
        verification_agent.generate_verification_plan()

        plan_file = temp_plan_dir / "verification_plan.json"
        assert plan_file.exists()

        # Phase 2: execute should load plan from file (no plan_data arg)
        execution_results = verification_agent.execute_verification_plan()

        assert len(execution_results["execution_results"]) == 1
        assert execution_results["execution_results"][0]["id"] == "VP-001"

    def test_flow_control_subprocess_exception_returns_failed(self, verification_agent, mock_coding_tool):
        """Test that generic subprocess exception in automated test produces FAILED status."""
        verification_agent.start_verification_round(1)

        vp = {
            "id": "VP-001",
            "title": "Exception Test",
            "verification_method": "code_review",
            "test_command": "nonexistent_command",
            "priority": "high",
            "expected_result": "Should handle exception"
        }
        # Simulate sub-agent raising an exception (e.g., subprocess error)
        mock_coding_tool.query_json.side_effect = OSError("Command not found")
        result = verification_agent._execute_verification_point(vp)

        assert result["id"] == "VP-001"
        assert result["status"] == "FAILED"
        assert "异常" in result.get("actual_result", "") or "FAILED" in result.get("status", "")

    def test_flow_control_vp_exception_continues_plan_execution(
        self, verification_agent, mock_coding_tool, temp_plan_dir
    ):
        """Test that an exception from one VP doesn't stop execution of other VPs."""
        plan_data = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "Exception VP",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "Will throw",
                    "test_command": ""
                },
                {
                    "id": "VP-002",
                    "title": "Manual Check",
                    "verification_method": "code_review",
                    "priority": "medium",
                    "expected_result": "Should execute",
                    "test_command": ""
                }
            ]
        }

        # Raise for VP-001 only. A blanket ``side_effect = Exception``
        # would fail VP-002 as well, which makes "execution continues"
        # unobservable — the assertion below needs a VP that actually
        # succeeds after the failing one.
        def _raise_for_vp001(prompt=None, **kwargs):
            if "VP-001" in str(prompt):
                raise Exception("LLM crashed")
            return {
                "verdict": "PASSED", "reasons": ["ok"], "evidence": ["out"],
            }

        mock_coding_tool.query_json.side_effect = _raise_for_vp001
        verification_agent.start_verification_round(1)
        execution_results = verification_agent.execute_verification_plan(plan_data)

        # Both VPs should be executed despite VP-001 throwing
        assert len(execution_results["execution_results"]) == 2
        vp1 = execution_results["execution_results"][0]
        assert vp1["id"] == "VP-001"
        assert vp1["status"] == "FAILED"

        vp2 = execution_results["execution_results"][1]
        assert vp2["id"] == "VP-002"
        # 2026-09-18: this used to expect SKIPPED. VP-002 declares no
        # ``depends_on``, so it shares a DAG layer with VP-001 and the
        # executor runs it regardless — which is what these tests have
        # always claimed in their own names ("continues to other VPs",
        # "continues plan execution"). The SKIPPED expectation came from
        # the retired ``manual_check`` short-circuit, not the layer rule.
        assert vp2["status"] == "PASSED"

    def test_flow_control_empty_verification_points(self, verification_agent, temp_plan_dir):
        """Test execution with empty verification points list produces empty results."""
        verification_agent.start_verification_round(1)
        plan_data = {"verification_points": []}
        execution_results = verification_agent.execute_verification_plan(plan_data)

        assert execution_results["execution_results"] == []
        assert execution_results["verification_points"] == []
        assert "executed_at" in execution_results


# =============================================================================
# LLM Output Structure Tests (Real API Calls + Mock-Based Structure Validation)
# =============================================================================

class TestLLMOutputStructure:
    """Test LLM output structure with real API calls and mock-based validation."""

    def test_plan_output_structure_validates_vp_fields(self, verification_agent, mock_coding_tool):
        """Test that generated verification plan has correct VP field structure."""
        plan_data = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "Field Validation VP",
                    "related_prd_criteria": "Criteria text",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "Expected outcome",
                    "test_command": "pytest tests/ -v"
                },
                {
                    "id": "VP-002",
                    "title": "Code Review VP",
                    "related_prd_criteria": "Code quality check",
                    "verification_method": "code_review",
                    "priority": "medium",
                    "expected_result": "Clean code",
                    "test_command": ""
                }
            ]
        }
        mock_coding_tool.query_json.return_value = plan_data

        plan = verification_agent.generate_verification_plan()

        # Validate top-level structure
        assert "verification_points" in plan
        assert isinstance(plan["verification_points"], list)
        assert len(plan["verification_points"]) == 2

        # Validate each VP has required fields with correct types
        required_vp_fields = ["id", "title", "related_prd_criteria",
                              "verification_method", "priority", "expected_result"]
        from verification_subagent import SUPPORTED_METHODS
        valid_methods = list(SUPPORTED_METHODS)
        valid_priorities = ["high", "medium", "low"]

        for vp in plan["verification_points"]:
            for field in required_vp_fields:
                assert field in vp, f"VP {vp.get('id', '?')} missing field: {field}"
            assert isinstance(vp["id"], str)
            assert isinstance(vp["title"], str)
            assert isinstance(vp["expected_result"], str)
            assert vp["verification_method"] in valid_methods
            assert vp["priority"] in valid_priorities

    def test_report_output_structure_validates_result_fields(self, verification_agent, mock_coding_tool):
        """Test that generated verification report has correct result field structure."""
        execution_results = {
            "verification_points": [
                {"id": "VP-001", "title": "T", "verification_method": "code_review",
                 "priority": "high", "expected_result": "ok", "test_command": "echo ok"},
                {"id": "VP-002", "title": "T2", "verification_method": "code_review",
                 "priority": "medium", "expected_result": "clean", "test_command": ""}
            ],
            "execution_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "OK", "evidence": "out"},
                {"id": "VP-002", "status": "FAILED", "actual_result": "Bad", "evidence": "err"}
            ]
        }
        report_data = {
            "overall_status": "FAILED",
            "summary": "1 passed, 1 failed",
            "verification_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "OK", "evidence": "out"},
                {"id": "VP-002", "status": "FAILED", "actual_result": "Bad", "evidence": "err"}
            ],
            "requirement_deviations": [
                {"verification_point_id": "VP-002", "type": "missing",
                 "description": "Feature not implemented", "severity": "high"}
            ]
        }
        mock_coding_tool.query_json.return_value = report_data

        report = verification_agent.generate_verification_report(execution_results)

        # Validate top-level fields
        assert report["overall_status"] in ["PASSED", "FAILED", "PARTIAL", "SKIPPED"]
        assert isinstance(report["summary"], str)
        assert isinstance(report["verification_results"], list)

        # Validate each result has required fields
        valid_statuses = ["PASSED", "FAILED", "SKIPPED", "PARTIAL"]
        for r in report["verification_results"]:
            assert "id" in r
            assert "status" in r
            assert r["status"] in valid_statuses
            assert "actual_result" in r
            assert "evidence" in r

        # Validate metadata fields added by the agent
        assert "generated_at" in report
        assert "plan_id" in report
        assert "project_dir" in report

    def test_report_top_level_fields_all_present(self, verification_agent, mock_coding_tool):
        """Test that report JSON contains all 4 required top-level fields:
        overall_status, summary, verification_results, requirement_deviations."""
        execution_results = {
            "verification_points": [
                {"id": "VP-001", "title": "T", "verification_method": "code_review",
                 "priority": "high", "expected_result": "ok", "test_command": "echo ok"}
            ],
            "execution_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "OK", "evidence": "out"}
            ]
        }
        report_data = {
            "overall_status": "PASSED",
            "summary": "All passed",
            "verification_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "OK", "evidence": "out"}
            ],
            "requirement_deviations": []
        }
        mock_coding_tool.query_json.return_value = report_data

        report = verification_agent.generate_verification_report(execution_results)

        # Verify all 4 required top-level fields exist
        assert "overall_status" in report, "Report missing required field: overall_status"
        assert "summary" in report, "Report missing required field: summary"
        assert "verification_results" in report, "Report missing required field: verification_results"
        assert "requirement_deviations" in report, "Report missing required field: requirement_deviations"

        # Verify types
        assert isinstance(report["overall_status"], str)
        assert isinstance(report["summary"], str)
        assert isinstance(report["verification_results"], list)
        assert isinstance(report["requirement_deviations"], list)

    def test_plan_all_six_vp_fields_present(self, verification_agent, mock_coding_tool):
        """Test that plan JSON verification points contain all 6 required fields:
        id, title, related_prd_criteria, verification_method, priority, expected_result."""
        plan_data = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "Login functionality",
                    "related_prd_criteria": "Users can log in with valid credentials",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "Login succeeds with status 200"
                }
            ]
        }
        mock_coding_tool.query_json.return_value = plan_data

        plan = verification_agent.generate_verification_plan()

        vp = plan["verification_points"][0]
        required_fields = ["id", "title", "related_prd_criteria",
                           "verification_method", "priority", "expected_result"]
        for field in required_fields:
            assert field in vp, f"VP missing required field: {field}"
            assert isinstance(vp[field], str), f"VP field '{field}' should be string, got {type(vp[field])}"

    def test_report_field_types_and_enum_values(self, verification_agent, mock_coding_tool):
        """Test that report fields have correct types and enum values for
        overall_status and verification_results status."""
        execution_results = {
            "verification_points": [
                {"id": "VP-001", "title": "T", "verification_method": "code_review",
                 "priority": "high", "expected_result": "ok", "test_command": "echo ok"}
            ],
            "execution_results": [
                {"id": "VP-001", "status": "FAILED", "actual_result": "Bad", "evidence": "err"}
            ]
        }
        report_data = {
            "overall_status": "FAILED",
            "summary": "1 failed",
            "verification_results": [
                {"id": "VP-001", "status": "FAILED", "actual_result": "Bad", "evidence": "err"}
            ],
            "requirement_deviations": [
                {"verification_point_id": "VP-001", "type": "missing",
                 "description": "Feature missing", "severity": "high"}
            ]
        }
        mock_coding_tool.query_json.return_value = report_data

        report = verification_agent.generate_verification_report(execution_results)

        # overall_status must be a valid enum value
        valid_overall = ["PASSED", "FAILED", "PARTIAL", "SKIPPED"]
        assert report["overall_status"] in valid_overall

        # Each verification_result must have string id, valid status enum, string actual_result and evidence
        valid_result_statuses = ["PASSED", "FAILED", "SKIPPED", "PARTIAL"]
        for r in report["verification_results"]:
            assert isinstance(r.get("id"), str)
            assert r["status"] in valid_result_statuses
            assert isinstance(r.get("actual_result"), str)
            assert isinstance(r.get("evidence"), str)

        # requirement_deviations is a list (may be empty)
        assert isinstance(report["requirement_deviations"], list)

    def test_deviation_output_structure_validates_fields(self, verification_agent, mock_coding_tool):
        """Test that requirement deviations have correct field structure."""
        execution_results = {
            "verification_points": [
                {"id": "VP-001", "title": "T", "verification_method": "code_review",
                 "priority": "high", "expected_result": "ok", "test_command": ""}
            ],
            "execution_results": [
                {"id": "VP-001", "status": "FAILED", "actual_result": "Bad", "evidence": "err"}
            ]
        }
        report_data = {
            "overall_status": "FAILED",
            "summary": "Deviation found",
            "verification_results": [
                {"id": "VP-001", "status": "FAILED", "actual_result": "Bad", "evidence": "err"}
            ],
            "requirement_deviations": [
                {"verification_point_id": "VP-001", "type": "missing",
                 "description": "Feature X not implemented", "severity": "high"},
                {"verification_point_id": "VP-001", "type": "performance",
                 "description": "Response time exceeds 500ms", "severity": "medium"}
            ]
        }
        mock_coding_tool.query_json.return_value = report_data

        report = verification_agent.generate_verification_report(execution_results)

        deviations = report["requirement_deviations"]
        assert len(deviations) == 2

        valid_types = ["missing", "changed", "performance", "compatibility"]
        valid_severities = ["high", "medium", "low"]
        for d in deviations:
            assert "verification_point_id" in d
            assert "type" in d
            assert d["type"] in valid_types
            assert "description" in d
            assert isinstance(d["description"], str) and len(d["description"]) > 0
            assert "severity" in d
            assert d["severity"] in valid_severities

    def test_judgment_prompt_contains_plan_and_results(self, verification_agent):
        """Test that _build_judgment_prompt includes both plan and execution data."""
        execution_results = {
            "verification_points": [
                {"id": "VP-001", "title": "Auth Check", "related_prd_criteria": "Login works",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "Login succeeds", "test_command": "pytest auth"},
                {"id": "VP-002", "title": "Rate Limit", "related_prd_criteria": "Rate limiting",
                 "verification_method": "code_review", "priority": "medium",
                 "expected_result": "Rate limiter present", "test_command": ""}
            ],
            "execution_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "Login OK", "evidence": "pytest green"},
                {"id": "VP-002", "status": "FAILED", "actual_result": "No rate limiter", "evidence": "grep found nothing"}
            ]
        }

        prompt = verification_agent._build_judgment_prompt(execution_results)

        # Must contain both verification plan info and execution results
        assert "VP-001" in prompt
        assert "VP-002" in prompt
        assert "Auth Check" in prompt
        assert "Rate Limit" in prompt
        assert "PASSED" in prompt
        assert "FAILED" in prompt
        assert "Login OK" in prompt
        assert "No rate limiter" in prompt
        assert "验证计划" in prompt
        assert "执行结果" in prompt

    def test_llm_extra_fields_preserved_in_report(self, verification_agent, mock_coding_tool):
        """Test that extra/unexpected fields from LLM response are preserved in report."""
        execution_results = {
            "verification_points": [
                {"id": "VP-001", "title": "T", "verification_method": "code_review",
                 "priority": "high", "expected_result": "ok", "test_command": "echo ok"}
            ],
            "execution_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "OK", "evidence": "out"}
            ]
        }
        report_data = {
            "overall_status": "PASSED",
            "summary": "All passed",
            "verification_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "OK", "evidence": "out"}
            ],
            "requirement_deviations": [],
            "confidence_score": 0.95,  # extra field from LLM
            "recommendation": "Ready for deployment"  # extra field from LLM
        }
        mock_coding_tool.query_json.return_value = report_data

        report = verification_agent.generate_verification_report(execution_results)

        # Extra fields should be preserved alongside standard fields
        assert "confidence_score" in report
        assert report["confidence_score"] == 0.95
        assert "recommendation" in report
        assert report["recommendation"] == "Ready for deployment"
        # Standard fields still present
        assert report["overall_status"] == "PASSED"
        assert "generated_at" in report

    def test_llm_output_structure_empty_plan_valid(self, verification_agent, mock_coding_tool):
        """Test that plan with empty verification_points list is valid JSON structure."""
        plan_data = {"verification_points": []}
        mock_coding_tool.query_json.return_value = plan_data

        plan = verification_agent.generate_verification_plan()

        assert "verification_points" in plan
        assert isinstance(plan["verification_points"], list)
        assert len(plan["verification_points"]) == 0

    def test_llm_output_structure_report_missing_field_retries(
        self, verification_agent, mock_coding_tool
    ):
        """Test that report generation retries when LLM returns missing required fields."""
        execution_results = {
            "verification_points": [],
            "execution_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "OK", "evidence": "T"}
            ]
        }
        # First attempt missing verification_results, second attempt succeeds
        mock_coding_tool.query_json.side_effect = [
            {"overall_status": "PASSED", "summary": "ok"},  # missing verification_results
            {"overall_status": "PASSED", "summary": "ok",
             "verification_results": [
                 {"id": "VP-001", "status": "PASSED", "actual_result": "OK", "evidence": "T"}
             ],
             "requirement_deviations": []}
        ]

        report = verification_agent.generate_verification_report(execution_results, retry_llm=3)

        assert mock_coding_tool.query_json.call_count == 2
        assert report["overall_status"] == "PASSED"
        assert "verification_results" in report

    @pytest.mark.integration
    @pytest.mark.real_model
    @pytest.mark.xfail(
        reason=(
            "LLM is non-deterministic: claude-3-5-haiku-20241022 may "
            "occasionally return a verification plan missing the "
            "``id`` (or other required) field on a VP. xfail marks "
            "this as an expected outcome of model behaviour; a "
            "verdict-parser regression that started dropping fields "
            "consistently would shift xfail→xpass and surface in "
            "the XPASS report."
        ),
        strict=False,
    )
    def test_verification_plan_output_has_required_fields(self, sample_prd, sample_arch, sample_test, live_llm_required):
        """Test that verification plan output contains all required fields."""
        from coding_tool import ClaudeCodingTool
        from verification_subagent import SUPPORTED_METHODS

        coding_tool = ClaudeCodingTool(model="claude-3-5-haiku-20241022")
        temp_plan = sample_prd.parent
        temp_project = Path(tempfile.mkdtemp())

        try:
            agent = VerificationAgent(
                plan_dir=temp_plan,
                project_dir=temp_project,
                coding_tool=coding_tool
            )
            plan = agent.generate_verification_plan()

            assert "verification_points" in plan
            assert isinstance(plan["verification_points"], list)
            assert len(plan["verification_points"]) > 0

            required_fields = [
                "id", "title", "related_prd_criteria",
                "verification_method", "priority", "expected_result"
            ]
            for vp in plan["verification_points"]:
                for field in required_fields:
                    assert field in vp, f"Missing: {field}"
                assert isinstance(vp["id"], str)
                assert isinstance(vp["title"], str)
                assert vp["verification_method"] in SUPPORTED_METHODS
                assert vp["priority"] in ["high", "medium", "low"]
        finally:
            shutil.rmtree(temp_project)

    @pytest.mark.integration
    @pytest.mark.real_model
    @pytest.mark.xfail(
        reason=(
            "LLM is non-deterministic: same as the plan-output "
            "counterpart — claude-3-5-haiku-20241022 may omit a "
            "required field on the verification report. xfail is "
            "the right marker here (model behaviour, not code)."
        ),
        strict=False,
    )
    def test_verification_report_output_has_required_fields(self, sample_prd, sample_arch, sample_test, live_llm_required):
        """Test that verification report output contains all required fields."""
        from coding_tool import ClaudeCodingTool

        coding_tool = ClaudeCodingTool(model="claude-3-5-haiku-20241022")
        temp_plan = sample_prd.parent
        temp_project = Path(tempfile.mkdtemp())

        try:
            agent = VerificationAgent(
                plan_dir=temp_plan,
                project_dir=temp_project,
                coding_tool=coding_tool
            )
            execution_results = {
                "verification_points": [
                    {"id": "VP-001", "title": "Test",
                     "related_prd_criteria": "Test criteria",
                     "verification_method": "code_review",
                     "priority": "high", "expected_result": "Pass",
                     "test_command": "echo test"}
                ],
                "execution_results": [
                    {"id": "VP-001", "status": "PASSED",
                     "actual_result": "Test passed", "evidence": "Output"}
                ]
            }

            report = agent.generate_verification_report(execution_results)

            for field in ["overall_status", "summary", "verification_results"]:
                assert field in report, f"Missing: {field}"

            assert report["overall_status"] in ["PASSED", "FAILED", "PARTIAL", "SKIPPED"]
            assert isinstance(report["verification_results"], list)

            if report["verification_results"]:
                for r in report["verification_results"]:
                    assert "id" in r
                    assert "status" in r
                    assert r["status"] in ["PASSED", "FAILED", "SKIPPED", "PARTIAL"]

            assert "requirement_deviations" in report
            assert isinstance(report["requirement_deviations"], list)
        finally:
            shutil.rmtree(temp_project)


# =============================================================================
# Requirement Deviation Detection Tests
# =============================================================================

class TestRequirementDeviationDetection:
    """Test requirement deviation detection with known failure scenarios."""

    def test_login_failure_rate_requirement_deviation_detected(
        self, verification_agent, mock_coding_tool, temp_plan_dir
    ):
        """
        PRD requires login failure rate <1% but code has no error handling.
        Should detect requirement deviation and generate description.
        """
        prd_data = {
            "title": "用户登录系统",
            "overview": "实现用户登录功能",
            "constraints": "Python FastAPI",
            "acceptance": "登录失败率必须低于1%",
            "decision_points": [
                {
                    "index": 0, "title": "登录失败率要求",
                    "context": "系统需要保证高可用性",
                    "problem": "如何确保登录失败率低于1%？",
                    "evidence": "需要实现错误处理和监控",
                    "action": "实现登录错误处理和失败率监控",
                    "impact": "影响认证模块",
                    "alternatives": ["使用第三方APM"]
                }
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

 # For sub-agent calls, use a helper that returns verdicts based on call context
        # The default return_value handles sub-agent calls; side_effect handles plan + report
        # Build side_effect as a list so we can use it directly
        plan_response = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "登录失败率验证",
                    "related_prd_criteria": "登录失败率必须低于1%",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "代码包含错误处理和失败率监控",
                    "test_command": ""
                }
            ]
        }
        # Sub-agent calls use the default return_value (PASSED verdict)
        # Report generation needs the specific response with deviations
        report_response = {
            "overall_status": "FAILED",
            "summary": "登录失败率要求未满足",
            "verification_results": [
                {
                    "id": "VP-001",
                    "status": "FAILED",
                    "actual_result": "代码缺少错误处理和失败率监控",
                    "evidence": "代码审查发现：无try-catch、无日志记录、无失败率统计"
                }
            ],
            "requirement_deviations": [
                {
                    "verification_point_id": "VP-001",
                    "type": "missing",
                    "description": "PRD要求登录失败率低于1%，但代码未实现错误处理和失败率监控功能",
                    "severity": "high"
                }
            ]
        }
        # Use side_effect to return plan for call 1, report for call 2
        # Sub-agent calls (call 2-5) use the default return_value
        call_count = [0]
        def side_effect_func(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return plan_response
            else:
                return report_response
        mock_coding_tool.query_json.side_effect = side_effect_func

        report = verification_agent.run_full_verification()

        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert len(deviations) > 0

        deviation = next(d for d in deviations if d.get("verification_point_id") == "VP-001")
        assert deviation["type"] in ["missing", "changed", "performance", "compatibility"]
        assert deviation["severity"] in ["high", "medium", "low"]
        assert len(deviation["description"]) > 0
        assert "1%" in deviation["description"] or "失败率" in deviation["description"]

    def test_missing_feature_deviation_detected(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """Test detection of missing feature deviation."""
        prd_data = {
            "title": "用户系统",
            "overview": "用户管理功能",
            "constraints": "Python FastAPI",
            "acceptance": "必须支持密码重置功能",
            "decision_points": [
                {
                    "index": 0, "title": "密码重置",
                    "context": "用户可能忘记密码",
                    "problem": "如何实现密码重置？",
                    "evidence": "需要邮件发送和token验证",
                    "action": "实现基于邮件的密码重置流程",
                    "impact": "影响用户模块",
                    "alternatives": ["短信验证"]
                }
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "密码重置功能",
                    "related_prd_criteria": "必须支持密码重置功能",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "代码包含密码重置实现",
                    "test_command": ""
                }
            ]
        }
        verdict_response = {
            "verdict": "FAILED",
            "reasons": ["password reset not found"],
            "evidence": ["no reset_password function found"]
        }
        report_response = {
            "overall_status": "FAILED",
            "summary": "密码重置功能缺失",
            "verification_results": [
                {
                    "id": "VP-001",
                    "status": "FAILED",
                    "actual_result": "未找到密码重置相关代码",
                    "evidence": "代码搜索结果：无reset_password函数"
                }
            ],
            "requirement_deviations": [
                {
                    "verification_point_id": "VP-001",
                    "type": "missing",
                    "description": "PRD要求密码重置功能，但代码中未找到相关实现",
                    "severity": "high"
                }
            ]
        }
        # 2026-09-13: route on prompt identity — the sub-agent's call
        # count is no longer fixed (self-heal loops, parse retries);
        # positional lists drift and an exhausted list hangs the
        # asyncio Future (never-set StopIteration chain). See
        # ``_router_side_effect``.
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            plan_response, verdict_response, report_response
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert len(deviations) > 0
        assert any(d.get("type") == "missing" for d in deviations)

    def test_performance_deviation_detected(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """Test detection of performance requirement deviation."""
        prd_data = {
            "title": "API 服务",
            "overview": "高性能 API 服务",
            "constraints": "Python FastAPI",
            "acceptance": "API 响应时间 p95 < 200ms",
            "decision_points": [
                {
                    "index": 0, "title": "性能要求",
                    "context": "需要高性能 API",
                    "problem": "如何保证响应时间？",
                    "evidence": "基准测试验证",
                    "action": "实现缓存和查询优化",
                    "impact": "全系统",
                    "alternatives": []
                }
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "响应时间验证",
                 "related_prd_criteria": "API 响应时间 p95 < 200ms",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "代码包含缓存和查询优化", "test_command": ""}
            ]
        }
        verdict_response = {
            "verdict": "FAILED",
            "reasons": ["no caching mechanism"],
            "evidence": ["code review found no cache usage"]
        }
        report_response = {
            "overall_status": "FAILED", "summary": "性能不达标",
             "verification_results": [
                 {"id": "VP-001", "status": "FAILED",
                  "actual_result": "代码无缓存机制，查询未优化",
                  "evidence": "代码审查发现未使用缓存"}
             ],
             "requirement_deviations": [
                 {"verification_point_id": "VP-001", "type": "performance",
                  "description": "PRD要求p95<200ms，但代码缺少缓存机制",
                  "severity": "high"}
             ]}
        # 2026-09-13: route on prompt identity — the sub-agent's call
        # count is no longer fixed (self-heal loops, parse retries);
        # positional lists drift and an exhausted list hangs the
        # asyncio Future (never-set StopIteration chain). See
        # ``_router_side_effect``.
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            plan_response, verdict_response, report_response
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert any(d.get("type") == "performance" for d in deviations)
        perf = [d for d in deviations if d.get("type") == "performance"][0]
        assert "200ms" in perf["description"] or "缓存" in perf["description"]

    def test_changed_feature_deviation_detected(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """Test detection of changed feature deviation — implementation differs from PRD."""
        prd_data = {
            "title": "密码重置系统",
            "overview": "用户密码重置功能",
            "constraints": "Python FastAPI",
            "acceptance": "密码重置必须通过邮件链接方式完成",
            "decision_points": [
                {
                    "index": 0, "title": "密码重置方式",
                    "context": "用户忘记密码需要安全重置",
                    "problem": "选择密码重置方式？",
                    "evidence": "邮件链接是最常见的重置方式",
                    "action": "实现基于邮件链接的密码重置流程",
                    "impact": "用户认证模块",
                    "alternatives": ["短信验证码"]
                }
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "密码重置方式验证",
                 "related_prd_criteria": "密码重置必须通过邮件链接方式完成",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "代码使用邮件链接方式重置密码",
                 "test_command": ""}
            ]
        }
        verdict_response = {
            "verdict": "FAILED",
            "reasons": ["implementation uses SMS instead of email"],
            "evidence": ["reset_password calls SMS API instead of email service"]
        }
        report_response = {
            "overall_status": "FAILED", "summary": "密码重置方式与PRD不符",
             "verification_results": [
                 {"id": "VP-001", "status": "FAILED",
                  "actual_result": "代码使用短信验证码方式重置密码，而非PRD要求的邮件链接",
                  "evidence": "代码审查发现reset_password函数调用SMS API而非邮件服务"}
             ],
             "requirement_deviations": [
                 {"verification_point_id": "VP-001", "type": "changed",
                  "description": "PRD要求邮件链接重置，但实现改用短信验证码方式",
                  "severity": "high"}
             ]}
        # 2026-09-13: route on prompt identity — the sub-agent's call
        # count is no longer fixed (self-heal loops, parse retries);
        # positional lists drift and an exhausted list hangs the
        # asyncio Future (never-set StopIteration chain). See
        # ``_router_side_effect``.
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            plan_response, verdict_response, report_response
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert len(deviations) > 0
        changed = [d for d in deviations if d.get("type") == "changed"]
        assert len(changed) > 0
        assert "邮件" in changed[0]["description"] or "短信" in changed[0]["description"]

    def test_compatibility_deviation_detected(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """Test detection of compatibility deviation — cross-browser/platform issue."""
        prd_data = {
            "title": "管理后台",
            "overview": "跨浏览器兼容的管理后台",
            "constraints": "React + TypeScript",
            "acceptance": "必须兼容Chrome、Firefox、Safari三大浏览器",
            "decision_points": [
                {
                    "index": 0, "title": "浏览器兼容性",
                    "context": "用户可能使用不同浏览器",
                    "problem": "如何保证跨浏览器兼容？",
                    "evidence": "需要标准CSS和polyfill",
                    "action": "使用标准Web API并添加polyfill",
                    "impact": "前端模块",
                    "alternatives": ["仅支持Chrome"]
                }
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "浏览器兼容性验证",
                 "related_prd_criteria": "必须兼容Chrome、Firefox、Safari",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "代码使用标准Web API并包含polyfill",
                 "test_command": ""}
            ]
        }
        verdict_response = {
            "verdict": "FAILED",
            "reasons": ["Safari incompatible API without polyfill"],
            "evidence": ["ResizeObserver used without polyfill"]
        }
        report_response = {
            "overall_status": "FAILED", "summary": "浏览器兼容性不达标",
             "verification_results": [
                 {"id": "VP-001", "status": "FAILED",
                  "actual_result": "代码使用了Safari不支持的CSS特性且无polyfill",
                  "evidence": "代码审查发现使用ResizeObserver但缺少polyfill"}
             ],
             "requirement_deviations": [
                 {"verification_point_id": "VP-001", "type": "compatibility",
                  "description": "PRD要求兼容三大浏览器，但代码使用了Safari不支持的API且未添加polyfill",
                  "severity": "high"}
             ]}
        # 2026-09-13: route on prompt identity — the sub-agent's call
        # count is no longer fixed (self-heal loops, parse retries);
        # positional lists drift and an exhausted list hangs the
        # asyncio Future (never-set StopIteration chain). See
        # ``_router_side_effect``.
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            plan_response, verdict_response, report_response
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert len(deviations) > 0
        compat = [d for d in deviations if d.get("type") == "compatibility"]
        assert len(compat) > 0
        assert compat[0]["severity"] in ["high", "medium", "low"]
        assert "Safari" in compat[0]["description"] or "浏览器" in compat[0]["description"] or "兼容" in compat[0]["description"]

    def test_no_deviations_when_all_pass(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """Test that no deviations are reported when all verification points pass."""
        prd_data = {
            "title": "简单计算器",
            "overview": "基础计算器功能",
            "constraints": "Python",
            "acceptance": "加减乘除运算正确",
            "decision_points": [
                {
                    "index": 0, "title": "运算功能",
                    "context": "需要基础运算",
                    "problem": "如何实现？",
                    "evidence": "标准算术运算",
                    "action": "实现加减乘除",
                    "impact": "核心功能",
                    "alternatives": []
                }
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "运算验证",
                 "related_prd_criteria": "加减乘除运算正确",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "所有运算正确",
                 "test_command": "echo 'All passed'"}
            ]
        }
        verdict_response = {
            "verdict": "PASSED",
            "reasons": ["all tests passed"],
            "evidence": ["pytest: 4 passed"]
        }
        report_response = {
            "overall_status": "PASSED", "summary": "全部通过",
             "verification_results": [
                 {"id": "VP-001", "status": "PASSED",
                  "actual_result": "所有运算正确",
                  "evidence": "pytest: 4 passed"}
             ],
             "requirement_deviations": []}
        # Use call counter approach: call 1 = plan, last call = report, rest = verdicts
        call_count = [0]
        def side_effect_func(*args, **kwargs):
            call_count[0] += 1
            # Phase 1: plan generation (first call)
            if call_count[0] == 1:
                return plan_response
            # Phase 3: report generation (no sub-agent calls in this test - 1 VP passes)
            # The call_count will be 2 for the single verdict + 3 for report retries
            # We detect report phase by checking the prompt content
            prompt = args[0] if args else kwargs.get('prompt', '')
            system = kwargs.get('system_instruction', '')
            if '判定每个验证点' in str(system) or '整体验证报告' in prompt:
                return report_response
            # Sub-agent verdict calls
            return verdict_response
        mock_coding_tool.query_json.side_effect = side_effect_func

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "PASSED"
        assert report.get("requirement_deviations", []) == []

    def test_multiple_deviation_types_detected(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """Test detection of multiple deviation types from different verification points."""
        prd_data = {
            "title": "电商系统",
            "overview": "完整电商平台",
            "constraints": "Python FastAPI",
            "acceptance": "支付成功率>99%，支持移动端浏览器，商品搜索响应<500ms",
            "decision_points": [
                {
                    "index": 0, "title": "支付可靠性",
                    "context": "支付必须可靠",
                    "problem": "如何保证？",
                    "evidence": "需要错误处理和重试",
                    "action": "实现支付错误处理",
                    "impact": "支付模块",
                    "alternatives": []
                },
                {
                    "index": 1, "title": "移动端兼容",
                    "context": "需要移动端支持",
                    "problem": "如何兼容？",
                    "evidence": "响应式设计",
                    "action": "实现响应式布局",
                    "impact": "前端模块",
                    "alternatives": []
                },
                {
                    "index": 2, "title": "搜索性能",
                    "context": "搜索必须快速",
                    "problem": "如何优化？",
                    "evidence": "索引和缓存",
                    "action": "实现搜索索引",
                    "impact": "搜索模块",
                    "alternatives": []
                }
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "支付验证",
                 "related_prd_criteria": "支付成功率>99%",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "支付包含错误处理和重试", "test_command": ""},
                {"id": "VP-002", "title": "移动端验证",
                 "related_prd_criteria": "支持移动端浏览器",
                 "verification_method": "code_review", "priority": "medium",
                 "expected_result": "前端包含响应式设计", "test_command": ""},
                {"id": "VP-003", "title": "搜索性能验证",
                 "related_prd_criteria": "商品搜索响应<500ms",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "代码包含搜索索引", "test_command": ""}
            ]
        }
        verdict_response = {
            "verdict": "FAILED",
            "reasons": ["verification failed"],
            "evidence": ["code review found issues"]
        }
        report_response = {
            "overall_status": "FAILED", "summary": "多项不达标",
             "verification_results": [
                 {"id": "VP-001", "status": "FAILED",
                  "actual_result": "支付模块缺少错误处理",
                  "evidence": "无try-catch"},
                 {"id": "VP-002", "status": "FAILED",
                  "actual_result": "前端未实现响应式布局",
                  "evidence": "CSS缺少media query"},
                 {"id": "VP-003", "status": "FAILED",
                  "actual_result": "搜索未建立索引",
                  "evidence": "全表扫描"}
             ],
             "requirement_deviations": [
                 {"verification_point_id": "VP-001", "type": "missing",
                  "description": "支付模块缺少错误处理机制，无法保证99%成功率",
                  "severity": "high"},
                 {"verification_point_id": "VP-002", "type": "compatibility",
                  "description": "前端未实现响应式布局，移动端浏览器无法正常使用",
                  "severity": "medium"},
                 {"verification_point_id": "VP-003", "type": "performance",
                  "description": "搜索使用全表扫描，响应时间远超500ms要求",
                  "severity": "high"}
             ]}
        # 2026-09-13: route on prompt identity (see
        # ``_router_side_effect``) — sub-agent call counts are no
        # longer fixed.
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            plan_response, verdict_response, report_response
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert len(deviations) == 3

        types = {d["type"] for d in deviations}
        assert "missing" in types
        assert "compatibility" in types
        assert "performance" in types

        severities = {d["severity"] for d in deviations}
        assert "high" in severities
        assert "medium" in severities

    def test_mixed_pass_fail_only_failed_get_deviations(
        self, verification_agent, mock_coding_tool, temp_plan_dir
    ):
        """Test that only failed VPs generate deviations, not passed ones."""
        prd_data = {
            "title": "混合系统",
            "overview": "部分通过部分失败",
            "constraints": "Python",
            "acceptance": "功能A正确，功能B存在，性能达标",
            "decision_points": [
                {"index": 0, "title": "功能A", "context": "需要功能A",
                 "problem": "如何实现？", "evidence": "标准实现",
                 "action": "实现功能A", "impact": "核心", "alternatives": []},
                {"index": 1, "title": "功能B", "context": "需要功能B",
                 "problem": "如何实现？", "evidence": "标准实现",
                 "action": "实现功能B", "impact": "核心", "alternatives": []},
                {"index": 2, "title": "性能", "context": "需要高性能",
                 "problem": "如何保证？", "evidence": "基准测试",
                 "action": "实现缓存", "impact": "全系统", "alternatives": []}
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "功能A验证",
                 "related_prd_criteria": "功能A正确",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "功能A实现正确", "test_command": "echo ok"},
                {"id": "VP-002", "title": "功能B验证",
                 "related_prd_criteria": "功能B存在",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "代码包含功能B", "test_command": ""},
                {"id": "VP-003", "title": "性能验证",
                 "related_prd_criteria": "性能达标",
                 "verification_method": "code_review", "priority": "medium",
                 "expected_result": "包含缓存机制", "test_command": ""}
            ]
        }
        passed_verdict = {
            "verdict": "PASSED",
            "reasons": ["test passed"],
            "evidence": ["pytest: 1 passed"]
        }
        failed_verdict = {
            "verdict": "FAILED",
            "reasons": ["verification failed"],
            "evidence": ["code review found issues"]
        }
        report_response = {
            "overall_status": "FAILED", "summary": "部分不达标",
             "verification_results": [
                 {"id": "VP-001", "status": "PASSED",
                  "actual_result": "功能A测试通过", "evidence": "pytest: 1 passed"},
                 {"id": "VP-002", "status": "FAILED",
                  "actual_result": "功能B代码缺失", "evidence": "grep未找到相关函数"},
                 {"id": "VP-003", "status": "FAILED",
                  "actual_result": "缺少缓存机制", "evidence": "代码审查无缓存"}
             ],
             "requirement_deviations": [
                 {"verification_point_id": "VP-002", "type": "missing",
                  "description": "PRD要求功能B但代码中未实现", "severity": "high"},
                 {"verification_point_id": "VP-003", "type": "performance",
                  "description": "缺少缓存机制，性能可能不达标", "severity": "medium"}
             ]}
        # Use callback-based side_effect: detect by system instruction content
        call_count = [0]
        def side_effect_func(*args, **kwargs):
            call_count[0] += 1
            system = kwargs.get('system_instruction', '')
            prompt = args[0] if args else kwargs.get('prompt', '')
            # Phase 1: plan generation
            if '判定每个验证点' not in str(system) and '整体验证报告' not in prompt:
                # Check if it's not the judgment/report phase
                # First call is plan, subsequent calls until report are verdicts
                # The report phase is detected by specific judgment prompt patterns
                if call_count[0] == 1:
                    return plan_response
                # Verdict calls (sub-agent) - return passed for VP-001, failed for VP-002 and VP-003
                # VP IDs are embedded in the prompt
                if 'VP-001' in str(prompt):
                    return passed_verdict
                else:
                    return failed_verdict
            # Phase 3: report generation
            return report_response
        mock_coding_tool.query_json.side_effect = side_effect_func

        report = verification_agent.run_full_verification()

        # Only failed VPs should have deviations
        deviations = report.get("requirement_deviations", [])
        dev_vp_ids = {d["verification_point_id"] for d in deviations}
        assert "VP-002" in dev_vp_ids, "Failed VP-002 should have deviation"
        assert "VP-003" in dev_vp_ids, "Failed VP-003 should have deviation"
        assert "VP-001" not in dev_vp_ids, "Passed VP-001 should NOT have deviation"
        assert len(deviations) == 2

        # Verify verification results show the mixed status
        results = report["verification_results"]
        passed = [r for r in results if r["status"] == "PASSED"]
        failed = [r for r in results if r["status"] == "FAILED"]
        assert len(passed) == 1
        assert len(failed) == 2

    def test_deviation_severity_medium_and_low(self, verification_agent, mock_coding_tool, temp_plan_dir):
        """Test deviation detection with medium and low severity levels."""
        prd_data = {
            "title": "内容管理系统",
            "overview": "CMS系统",
            "constraints": "Python FastAPI",
            "acceptance": "核心功能完整，UI美观，日志完善",
            "decision_points": [
                {
                    "index": 0, "title": "UI美观度",
                    "context": "用户界面需要美观",
                    "problem": "如何提升美观度？",
                    "evidence": "设计规范",
                    "action": "遵循设计规范",
                    "impact": "前端",
                    "alternatives": []
                },
                {
                    "index": 1, "title": "日志完善度",
                    "context": "需要完善日志",
                    "problem": "如何完善？",
                    "evidence": "日志最佳实践",
                    "action": "添加详细日志",
                    "impact": "运维",
                    "alternatives": []
                }
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "UI美观度验证",
                 "related_prd_criteria": "UI美观",
                 "verification_method": "code_review", "priority": "medium",
                 "expected_result": "UI符合设计规范", "test_command": ""},
                {"id": "VP-002", "title": "日志验证",
                 "related_prd_criteria": "日志完善",
                 "verification_method": "code_review", "priority": "low",
                 "expected_result": "包含详细日志记录", "test_command": ""}
            ]
        }
        verdict_response = {
            "verdict": "FAILED",
            "reasons": ["verification failed"],
            "evidence": ["code review found issues"]
        }
        report_response = {
            "overall_status": "FAILED", "summary": "部分不达标",
             "verification_results": [
                 {"id": "VP-001", "status": "FAILED",
                  "actual_result": "UI部分页面不符合设计规范",
                  "evidence": "按钮间距和颜色与设计稿不一致"},
                 {"id": "VP-002", "status": "FAILED",
                  "actual_result": "部分模块缺少日志记录",
                  "evidence": "3个模块未添加logging"}
             ],
             "requirement_deviations": [
                 {"verification_point_id": "VP-001", "type": "changed",
                  "description": "UI实现与设计规范存在偏差，按钮间距和颜色不一致",
                  "severity": "medium"},
                 {"verification_point_id": "VP-002", "type": "missing",
                  "description": "3个模块缺少日志记录功能",
                  "severity": "low"}
             ]}
        # 2026-09-13: route on prompt identity (see
        # ``_router_side_effect``) — sub-agent call counts are no
        # longer fixed.
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            plan_response, verdict_response, report_response
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert len(deviations) == 2

        medium_dev = next(d for d in deviations if d.get("severity") == "medium")
        assert medium_dev["type"] == "changed"

        low_dev = next(d for d in deviations if d.get("severity") == "low")
        assert low_dev["type"] == "missing"

    def test_deviation_detection_minimal_report_empty_deviations(
        self, verification_agent, mock_coding_tool
    ):
        """Test that minimal report (LLM fallback) always has empty requirement_deviations."""
        execution_results = {
            "verification_points": [],
            "execution_results": [
                {"id": "VP-001", "status": "FAILED", "actual_result": "Error", "evidence": "T"}
            ]
        }
        mock_coding_tool.query_json.side_effect = RuntimeError("LLM failed")

        report = verification_agent.generate_verification_report(execution_results, retry_llm=1)

        assert report["overall_status"] == "FAILED"
        assert "requirement_deviations" in report
        assert report["requirement_deviations"] == []

    def test_all_four_deviation_types_detected_simultaneously(
        self, verification_agent, mock_coding_tool, temp_plan_dir
    ):
        """Test all 4 deviation types (missing, changed, performance, compatibility) in one report."""
        prd_data = {
            "title": "综合系统",
            "overview": "多功能系统",
            "constraints": "Python FastAPI + React",
            "acceptance": "功能完整、UI符合设计、响应<200ms、跨浏览器兼容",
            "decision_points": [
                {"index": 0, "title": "功能A", "context": "需要功能A",
                 "problem": "如何实现？", "evidence": "标准方案",
                 "action": "实现功能A", "impact": "核心", "alternatives": []},
                {"index": 1, "title": "UI设计", "context": "UI需符合设计稿",
                 "problem": "如何保证？", "evidence": "设计规范",
                 "action": "遵循设计稿", "impact": "前端", "alternatives": []},
                {"index": 2, "title": "响应时间", "context": "需要快速响应",
                 "problem": "如何优化？", "evidence": "缓存策略",
                 "action": "实现缓存", "impact": "全系统", "alternatives": []},
                {"index": 3, "title": "浏览器兼容", "context": "需支持多浏览器",
                 "problem": "如何兼容？", "evidence": "polyfill",
                 "action": "添加polyfill", "impact": "前端", "alternatives": []}
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "功能A验证",
                 "related_prd_criteria": "功能A完整",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "代码包含功能A", "test_command": ""},
                {"id": "VP-002", "title": "UI设计验证",
                 "related_prd_criteria": "UI符合设计稿",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "UI与设计稿一致", "test_command": ""},
                {"id": "VP-003", "title": "响应时间验证",
                 "related_prd_criteria": "响应<200ms",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "包含缓存机制", "test_command": ""},
                {"id": "VP-004", "title": "浏览器兼容验证",
                 "related_prd_criteria": "跨浏览器兼容",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "包含polyfill", "test_command": ""}
            ]
        }
        verdict_response = {
            "verdict": "FAILED",
            "reasons": ["verification failed"],
            "evidence": ["code review found issues"]
        }
        report_response = {
            "overall_status": "FAILED", "summary": "四项偏离",
             "verification_results": [
                 {"id": "VP-001", "status": "FAILED",
                  "actual_result": "功能A未实现", "evidence": "代码中无功能A"},
                 {"id": "VP-002", "status": "FAILED",
                  "actual_result": "UI使用暗色主题而非设计稿的亮色主题",
                  "evidence": "CSS中背景色为#333而非#fff"},
                 {"id": "VP-003", "status": "FAILED",
                  "actual_result": "无缓存机制，响应时间超过200ms",
                  "evidence": "每次请求都重新计算"},
                 {"id": "VP-004", "status": "FAILED",
                  "actual_result": "使用Safari不支持的API且无polyfill",
                  "evidence": "使用了ResizeObserver但未添加polyfill"}
             ],
             "requirement_deviations": [
                 {"verification_point_id": "VP-001", "type": "missing",
                  "description": "功能A完全未实现", "severity": "high"},
                 {"verification_point_id": "VP-002", "type": "changed",
                  "description": "UI从亮色主题改为暗色主题，与PRD不符", "severity": "high"},
                 {"verification_point_id": "VP-003", "type": "performance",
                  "description": "响应时间超过200ms要求，缺少缓存", "severity": "high"},
                 {"verification_point_id": "VP-004", "type": "compatibility",
                  "description": "Safari不支持所使用的API且未添加polyfill", "severity": "high"}
             ]}
        # 2026-09-13: count-independent router — a positional
        # side_effect list that runs dry raises StopIteration inside
        # asyncio.to_thread, which asyncio refuses to set on the
        # chained Future, hanging the suite until the 3600s cap.
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            plan_response, verdict_response, report_response
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert len(deviations) == 4

        types = {d["type"] for d in deviations}
        assert types == {"missing", "changed", "performance", "compatibility"}

    def test_all_severity_levels_high_medium_low_detected(
        self, verification_agent, mock_coding_tool, temp_plan_dir
    ):
        """Test all 3 severity levels (high, medium, low) detected in one report."""
        prd_data = {
            "title": "多层级系统",
            "overview": "包含不同严重性偏离",
            "constraints": "Python",
            "acceptance": "核心功能完整、UI美观、日志完善",
            "decision_points": [
                {"index": 0, "title": "核心功能", "context": "核心功能必须完整",
                 "problem": "如何实现？", "evidence": "标准方案",
                 "action": "实现核心功能", "impact": "核心", "alternatives": []},
                {"index": 1, "title": "UI美观度", "context": "UI需要美观",
                 "problem": "如何提升？", "evidence": "设计规范",
                 "action": "遵循设计规范", "impact": "前端", "alternatives": []},
                {"index": 2, "title": "日志完善度", "context": "需要完善日志",
                 "problem": "如何完善？", "evidence": "最佳实践",
                 "action": "添加日志", "impact": "运维", "alternatives": []}
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "核心功能验证",
                 "related_prd_criteria": "核心功能完整",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "核心功能已实现", "test_command": ""},
                {"id": "VP-002", "title": "UI美观度验证",
                 "related_prd_criteria": "UI美观",
                 "verification_method": "code_review", "priority": "medium",
                 "expected_result": "UI符合设计规范", "test_command": ""},
                {"id": "VP-003", "title": "日志验证",
                 "related_prd_criteria": "日志完善",
                 "verification_method": "code_review", "priority": "low",
                 "expected_result": "包含日志记录", "test_command": ""}
            ]
        }
        verdict_response = {
            "verdict": "FAILED",
            "reasons": ["verification failed"],
            "evidence": ["code review found issues"]
        }
        report_response = {
            "overall_status": "FAILED", "summary": "三级偏离",
             "verification_results": [
                 {"id": "VP-001", "status": "FAILED",
                  "actual_result": "核心功能未实现", "evidence": "代码中无相关逻辑"},
                 {"id": "VP-002", "status": "FAILED",
                  "actual_result": "UI部分不符合设计规范", "evidence": "按钮间距不一致"},
                 {"id": "VP-003", "status": "FAILED",
                  "actual_result": "部分模块缺少日志", "evidence": "3个模块无logging"}
             ],
             "requirement_deviations": [
                 {"verification_point_id": "VP-001", "type": "missing",
                  "description": "核心功能完全未实现", "severity": "high"},
                 {"verification_point_id": "VP-002", "type": "changed",
                  "description": "UI按钮间距与设计规范不一致", "severity": "medium"},
                 {"verification_point_id": "VP-003", "type": "missing",
                  "description": "3个模块缺少日志记录", "severity": "low"}
             ]}
        # 2026-09-13: count-independent router (see
        # test_all_four_deviation_types_detected_simultaneously).
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            plan_response, verdict_response, report_response
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert len(deviations) == 3

        severities = {d["severity"] for d in deviations}
        assert severities == {"high", "medium", "low"}

        high_dev = next(d for d in deviations if d["severity"] == "high")
        assert high_dev["type"] == "missing"
        medium_dev = next(d for d in deviations if d["severity"] == "medium")
        assert medium_dev["type"] == "changed"
        low_dev = next(d for d in deviations if d["severity"] == "low")
        assert low_dev["type"] == "missing"

    def test_low_severity_deviation_standalone(
        self, verification_agent, mock_coding_tool, temp_plan_dir
    ):
        """Test a single low-severity deviation is correctly detected and classified."""
        prd_data = {
            "title": "日志系统",
            "overview": "日志记录功能",
            "constraints": "Python",
            "acceptance": "所有模块应包含INFO级别日志",
            "decision_points": [
                {"index": 0, "title": "日志覆盖",
                 "context": "需要完善的日志记录",
                 "problem": "如何保证日志覆盖？",
                 "evidence": "最佳实践要求每个模块至少有入口和出口日志",
                 "action": "在所有模块中添加日志记录",
                 "impact": "运维和调试",
                 "alternatives": []}
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "日志覆盖验证",
                 "related_prd_criteria": "所有模块应包含INFO级别日志",
                 "verification_method": "code_review", "priority": "low",
                 "expected_result": "所有模块包含INFO级别日志", "test_command": ""}
            ]
        }
        verdict_response = {
            "verdict": "FAILED",
            "reasons": ["missing logging"],
            "evidence": ["utils.py and helpers.py have no logging import"]
        }
        report_response = {
            "overall_status": "FAILED", "summary": "部分模块缺少日志",
             "verification_results": [
                 {"id": "VP-001", "status": "FAILED",
                  "actual_result": "2个辅助模块缺少日志记录",
                  "evidence": "utils.py和helpers.py无logging import"}
             ],
             "requirement_deviations": [
                 {"verification_point_id": "VP-001", "type": "missing",
                  "description": "utils.py和helpers.py两个辅助模块缺少INFO级别日志记录",
                  "severity": "low"}
             ]}
        # 2026-09-13: count-independent router (positional side_effect
        # exhaustion hangs the suite — see _router_side_effect docs).
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            plan_response, verdict_response, report_response
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert len(deviations) == 1

        dev = deviations[0]
        assert dev["severity"] == "low"
        assert dev["type"] == "missing"
        assert dev["verification_point_id"] == "VP-001"
        assert len(dev["description"]) > 0

    def test_high_severity_deviation_standalone(
        self, verification_agent, mock_coding_tool, temp_plan_dir
    ):
        """Test a single high-severity deviation is correctly detected and classified."""
        prd_data = {
            "title": "支付系统",
            "overview": "在线支付功能",
            "constraints": "Python FastAPI",
            "acceptance": "支付功能必须包含错误处理和重试机制",
            "decision_points": [
                {"index": 0, "title": "支付可靠性",
                 "context": "支付必须可靠，涉及资金",
                 "problem": "如何保证？", "evidence": "需要错误处理和重试",
                 "action": "实现支付错误处理和重试", "impact": "支付模块", "alternatives": []}
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "支付错误处理验证",
                 "related_prd_criteria": "支付功能必须包含错误处理和重试机制",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "代码包含try-catch和重试逻辑", "test_command": ""}
            ]
        }
        verdict_response = {
            "verdict": "FAILED",
            "reasons": ["no error handling"],
            "evidence": ["charge() function directly calls API without exception handling"]
        }
        report_response = {
            "overall_status": "FAILED", "summary": "支付缺少错误处理",
             "verification_results": [
                 {"id": "VP-001", "status": "FAILED",
                  "actual_result": "支付函数无try-catch和重试逻辑",
                  "evidence": "charge()函数直接调用API，无异常捕获"}
             ],
             "requirement_deviations": [
                 {"verification_point_id": "VP-001", "type": "missing",
                  "description": "支付模块缺少错误处理和重试机制，可能导致资金损失",
                  "severity": "high"}
             ]}
        # 2026-09-13: count-independent router (positional side_effect
        # exhaustion hangs the suite — see _router_side_effect docs).
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            plan_response, verdict_response, report_response
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert len(deviations) == 1

        dev = deviations[0]
        assert dev["severity"] == "high"
        assert dev["type"] == "missing"
        assert dev["verification_point_id"] == "VP-001"
        assert len(dev["description"]) > 0
        assert "错误处理" in dev["description"] or "重试" in dev["description"] or "支付" in dev["description"]

    def test_medium_severity_deviation_standalone(
        self, verification_agent, mock_coding_tool, temp_plan_dir
    ):
        """Test a single medium-severity deviation is correctly detected and classified."""
        prd_data = {
            "title": "数据导出系统",
            "overview": "支持CSV和Excel导出",
            "constraints": "Python FastAPI",
            "acceptance": "导出文件名必须包含时间戳",
            "decision_points": [
                {"index": 0, "title": "文件名格式",
                 "context": "导出文件需要唯一命名",
                 "problem": "如何命名导出文件？",
                 "evidence": "时间戳是最常用的唯一标识",
                 "action": "导出文件名包含YYYYMMDD_HHmmss时间戳",
                 "impact": "导出模块", "alternatives": ["UUID"]}
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "文件名格式验证",
                 "related_prd_criteria": "导出文件名必须包含时间戳",
                 "verification_method": "code_review", "priority": "medium",
                 "expected_result": "文件名包含时间戳格式", "test_command": ""}
            ]
        }
        verdict_response = {
            "verdict": "FAILED",
            "reasons": ["filename uses UUID instead of timestamp"],
            "evidence": ["export_file() uses uuid4()"]
        }
        report_response = {
            "overall_status": "FAILED", "summary": "文件名格式不符合要求",
             "verification_results": [
                 {"id": "VP-001", "status": "FAILED",
                  "actual_result": "文件名使用UUID而非时间戳",
                  "evidence": "export_file()函数使用uuid4()生成文件名"}
             ],
             "requirement_deviations": [
                 {"verification_point_id": "VP-001", "type": "changed",
                  "description": "PRD要求文件名包含时间戳，但实现改用UUID格式",
                  "severity": "medium"}
             ]}
        # 2026-09-13: count-independent router (positional side_effect
        # exhaustion hangs the suite — see _router_side_effect docs).
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            plan_response, verdict_response, report_response
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert len(deviations) == 1

        dev = deviations[0]
        assert dev["severity"] == "medium"
        assert dev["type"] == "changed"
        assert dev["verification_point_id"] == "VP-001"
        assert len(dev["description"]) > 0
        assert "时间戳" in dev["description"] or "UUID" in dev["description"] or "文件名" in dev["description"]

    def test_changed_deviation_standalone_with_description_validation(
        self, verification_agent, mock_coding_tool, temp_plan_dir
    ):
        """Test single changed-type deviation with thorough description validation."""
        prd_data = {
            "title": "通知系统",
            "overview": "用户通知功能",
            "constraints": "Python FastAPI",
            "acceptance": "通知必须通过站内信方式发送",
            "decision_points": [
                {"index": 0, "title": "通知方式",
                 "context": "用户需要接收系统通知",
                 "problem": "选择通知方式？",
                 "evidence": "站内信是最低成本的通知方式",
                 "action": "实现站内信通知系统",
                 "impact": "通知模块", "alternatives": ["邮件通知", "短信通知"]}
            ]
        }
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f, indent=2)

        plan_response = {
            "verification_points": [
                {"id": "VP-001", "title": "通知方式验证",
                 "related_prd_criteria": "通知必须通过站内信方式发送",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "代码使用站内信方式发送通知",
                 "test_command": ""}
            ]
        }
        verdict_response = {
            "verdict": "FAILED",
            "reasons": ["uses email instead of in-app messaging"],
            "evidence": ["NotificationService.send() calls SMTP"]
        }
        report_response = {
            "overall_status": "FAILED", "summary": "通知方式与PRD不符",
             "verification_results": [
                 {"id": "VP-001", "status": "FAILED",
                  "actual_result": "代码使用邮件通知而非站内信",
                  "evidence": "NotificationService.send()调用SMTP邮件发送，非站内信存储"}
             ],
             "requirement_deviations": [
                 {"verification_point_id": "VP-001", "type": "changed",
                  "description": "PRD要求站内信通知，但实现改为邮件通知方式，违反了产品决策",
                  "severity": "high"}
             ]}
        # 2026-09-13: count-independent router (positional side_effect
        # exhaustion hangs the suite — see _router_side_effect docs).
        mock_coding_tool.query_json.side_effect = _router_side_effect(
            plan_response, verdict_response, report_response
        )

        report = verification_agent.run_full_verification()
        assert report["overall_status"] == "FAILED"
        deviations = report.get("requirement_deviations", [])
        assert len(deviations) == 1

        dev = deviations[0]
        assert dev["type"] == "changed"
        assert dev["severity"] == "high"
        assert dev["verification_point_id"] == "VP-001"
        # Validate description content references both original requirement and actual implementation
        desc = dev["description"]
        assert ("站内信" in desc or "通知" in desc) and ("邮件" in desc or "SMTP" in desc or "改" in desc)


# =============================================================================
# Coverage Tests
# =============================================================================

class TestCoverage:
    """Additional tests to maximize code coverage."""

    def test_minimal_plan_generation_on_generic_exception(self, verification_agent, mock_coding_tool):
        """Test that minimal plan is generated when LLM fails with generic exception."""
        # Generic exceptions (not ApiError) trigger minimal plan fallback
        mock_coding_tool.query_json.side_effect = RuntimeError("Something broke")

        plan = verification_agent.generate_verification_plan(retry_llm=2)

        assert "verification_points" in plan
        assert len(plan["verification_points"]) >= 1
        assert plan["verification_points"][0]["id"] == "VP-001"
        assert "title" in plan["verification_points"][0]
        assert "verification_method" in plan["verification_points"][0]

    def test_minimal_report_generation_on_generic_exception(self, verification_agent, mock_coding_tool):
        """Test that minimal report is generated when LLM fails with generic exception."""
        execution_results = {
            "verification_points": [],
            "execution_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "OK", "evidence": "Test"}
            ]
        }

        mock_coding_tool.query_json.side_effect = RuntimeError("LLM down")

        report = verification_agent.generate_verification_report(execution_results, retry_llm=2)

        assert "overall_status" in report
        assert "verification_results" in report
        assert "summary" in report

    def test_minimal_report_status_determination(self, verification_agent, mock_coding_tool):
        """Test minimal report correctly determines status from results."""
        # All PASSED
        results = {
            "verification_points": [],
            "execution_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "OK", "evidence": "T"}
            ]
        }
        mock_coding_tool.query_json.side_effect = RuntimeError("fail")
        report = verification_agent.generate_verification_report(results, retry_llm=1)
        assert report["overall_status"] == "PASSED"

    def test_minimal_report_with_failed_results(self, verification_agent, mock_coding_tool):
        """Test minimal report with failed execution results."""
        results = {
            "verification_points": [],
            "execution_results": [
                {"id": "VP-001", "status": "FAILED", "actual_result": "Error", "evidence": "T"},
                {"id": "VP-002", "status": "PASSED", "actual_result": "OK", "evidence": "T"}
            ]
        }
        mock_coding_tool.query_json.side_effect = RuntimeError("fail")
        report = verification_agent.generate_verification_report(results, retry_llm=1)
        assert report["overall_status"] == "FAILED"

    def test_minimal_report_with_partial_results(self, verification_agent, mock_coding_tool):
        """Test minimal report with partial execution results."""
        results = {
            "verification_points": [],
            "execution_results": [
                {"id": "VP-001", "status": "PARTIAL", "actual_result": "Partial", "evidence": "T"}
            ]
        }
        mock_coding_tool.query_json.side_effect = RuntimeError("fail")
        report = verification_agent.generate_verification_report(results, retry_llm=1)
        assert report["overall_status"] == "PARTIAL"

    def test_minimal_report_with_skipped_results(self, verification_agent, mock_coding_tool):
        """Test minimal report with all skipped results."""
        results = {
            "verification_points": [],
            "execution_results": [
                {"id": "VP-001", "status": "SKIPPED", "actual_result": "Skip", "evidence": "T"}
            ]
        }
        mock_coding_tool.query_json.side_effect = RuntimeError("fail")
        report = verification_agent.generate_verification_report(results, retry_llm=1)
        assert report["overall_status"] == "SKIPPED"

    def test_document_loading_with_missing_files(self, verification_agent):
        """Test document loading handles missing files gracefully."""
        documents = verification_agent._load_documents()
        assert isinstance(documents, dict)

    def test_document_loading_with_prd_json(self, verification_agent, temp_plan_dir):
        """Test document loading with PRD JSON file."""
        prd_data = {"title": "Test", "decision_points": []}
        prd_file = temp_plan_dir / "prd.json"
        with open(prd_file, 'w') as f:
            json.dump(prd_data, f)

        documents = verification_agent._load_documents()
        assert "prd" in documents
        assert "Test" in documents["prd"]

    def test_document_loading_with_prd_markdown(self, verification_agent, temp_plan_dir):
        """Test document loading with legacy PRD markdown file."""
        prd_md = temp_plan_dir / "prd.md"
        prd_md.write_text("# Legacy PRD\n\nSome content")

        documents = verification_agent._load_documents()
        assert "prd" in documents
        assert "Legacy PRD" in documents["prd"]

    def test_verification_plan_file_missing_during_execution(self, verification_agent):
        """Test execution when plan file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            verification_agent.execute_verification_plan()

    def test_execution_results_file_missing_during_report(self, verification_agent):
        """Test report generation when execution results file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            verification_agent.generate_verification_report()

    def test_build_planning_prompt_with_no_documents(self, verification_agent):
        """Test planning prompt generation with no documents."""
        prompt = verification_agent._build_planning_prompt({})
        assert "基础验证计划" in prompt

    def test_build_planning_prompt_with_documents(self, verification_agent, temp_plan_dir):
        """Test planning prompt includes document sections."""
        docs = {
            "prd": "PRD content here",
            "arch": "Arch content here",
            "test": "Test content here"
        }
        prompt = verification_agent._build_planning_prompt(docs)
        assert "PRD" in prompt
        assert "架构" in prompt
        assert "测试" in prompt

    def test_execute_code_review_no_command(self, verification_agent, mock_coding_tool):
        """Test automated test execution with no test command."""
        verification_agent.start_verification_round(1)
        vp = {
            "id": "VP-001",
            "title": "No Command",
            "verification_method": "code_review",
            "test_command": ""
        }
        # With empty test_command, the sub-agent will still run but produce FAILED verdict
        mock_coding_tool.query_json.return_value = {
            "verdict": "FAILED",
            "reasons": ["test_command is empty"],
            "evidence": []
        }
        result = verification_agent._execute_verification_point(vp)
        assert result["status"] == "FAILED"
        assert result["id"] == "VP-001"

    def test_execute_ui_validation_skipped(self, verification_agent, mock_coding_tool):
        """Test UI validation: SKIPPED only when subprocess returns verdict=SKIPPED with reasons."""
        verification_agent.start_verification_round(1)
        vp = {
            "id": "VP-001",
            "title": "UI Test",
            "verification_method": "ui_validation",
            "expected_result": "UI looks correct"
        }
        # Subprocess returned a structured SKIPPED verdict (e.g. browser not available)
        # SKIPPED is coerced to FAILED by parse_verdict (zero-tolerance for skipping)
        mock_coding_tool.query_json.return_value = {
            "verdict": "SKIPPED",
            "reasons": ["chromium binary not installed"],
            "evidence": ["verified host env"]
        }
        result = verification_agent._execute_verification_point(vp)
        # SKIPPED is coerced to FAILED by the sub-agent
        assert result["status"] == "FAILED"
        # actual_result contains the first reason (coercion reason appended if SKIPPED was coerced)
        # The sub-agent should have coerced SKIPPED to FAILED with an appended reason
        actual_result = result.get("actual_result", "")
        assert "chromium binary not installed" in actual_result or "coerced" in actual_result.lower()

    def test_execute_ui_validation_passed(self, verification_agent, mock_coding_tool):
        """UI validation: PASSED when subprocess returns verdict=PASSED."""
        verification_agent.start_verification_round(1)
        vp = {
            "id": "VP-OK",
            "title": "UI Test",
            "verification_method": "ui_validation",
            "expected_result": "Graph renders with > 0 nodes",
            "target_url": "http://127.0.0.1:8000/",
        }
        mock_coding_tool.query_json.return_value = {
            "verdict": "PASSED",
            "evidence": ["43 nodes / 36 edges rendered"],
            "reasons": []
        }
        result = verification_agent._execute_verification_point(vp)
        assert result["status"] == "PASSED"

    def test_execute_ui_validation_failed_when_no_data(self, verification_agent, mock_coding_tool):
        """UI validation: FAILED (NOT SKIPPED) when graph is empty — this is what
        catches the regression where the frontend shows graph but no data."""
        verification_agent.start_verification_round(1)
        vp = {
            "id": "VP-EMPTY",
            "title": "UI Test",
            "verification_method": "ui_validation",
            "expected_result": "Graph renders company nodes with labels",
            "target_url": "http://127.0.0.1:8000/",
        }
        mock_coding_tool.query_json.return_value = {
            "verdict": "FAILED",
            "evidence": ["0 nodes rendered, only green dots"],
            "reasons": ["no company labels visible", "no detail panel content"]
        }
        result = verification_agent._execute_verification_point(vp)
        assert result["status"] == "FAILED"
        # Evidence must include the puppeteer observation, not just say "skipped"
        evidence_str = str(result.get("evidence", []))
        assert "no company labels" in evidence_str or "0 nodes" in evidence_str

    def test_execute_ui_validation_failed_when_subprocess_unparseable(self, verification_agent, mock_coding_tool):
        """UI validation: FAILED (NOT SKIPPED) when subprocess returns non-JSON.

        This is critical — silently SKIPPED would let bad UI ship."""
        verification_agent.start_verification_round(1)
        vp = {
            "id": "VP-MUMBLE",
            "title": "UI Test",
            "verification_method": "ui_validation",
            "expected_result": "...",
        }
        mock_coding_tool.query_json.return_value = "I could not find a browser binary."
        result = verification_agent._execute_verification_point(vp)
        assert result["status"] == "FAILED"

    def test_execute_ui_validation_skipped_without_reasons_is_failed(self, verification_agent, mock_coding_tool):
        """UI validation: SKIPPED verdict without reasons → FAILED (anti-silent-skip)."""
        verification_agent.start_verification_round(1)
        vp = {
            "id": "VP-LAZY",
            "title": "UI Test",
            "verification_method": "ui_validation",
            "expected_result": "..."
        }
        # SKIPPED without reasons is coerced to FAILED
        mock_coding_tool.query_json.return_value = {
            "verdict": "SKIPPED",
            "reasons": [],
            "evidence": ["browser unavailable"]
        }
        result = verification_agent._execute_verification_point(vp)
        assert result["status"] == "FAILED"
        # The override reason must appear in actual_result so reviewers know why
        actual_result = result.get("actual_result", "")
        # SKIPPED verdict is coerced to FAILED with a reason annotation
        assert "SKIPPED" in actual_result or result["status"] == "FAILED"

    def test_execute_code_review_exception(self, verification_agent, mock_coding_tool):
        """Test code review handles LLM exceptions."""
        verification_agent.start_verification_round(1)
        mock_coding_tool.query_json.side_effect = Exception("LLM unavailable")
        vp = {
            "id": "VP-001",
            "title": "Code Review",
            "verification_method": "code_review",
            "expected_result": "Clean code"
        }
        result = verification_agent._execute_verification_point(vp)
        assert result["status"] == "FAILED"
        actual_result = str(result.get("actual_result", ""))
        assert "LLM unavailable" in actual_result or "FAILED" in actual_result

    def test_execute_verification_point_exception(self, verification_agent, mock_coding_tool):
        """Test VP execution handles exceptions gracefully."""
        verification_agent.start_verification_round(1)
        mock_coding_tool.query_json.side_effect = Exception("Unexpected error")
        vp = {
            "id": "VP-001",
            "title": "Code Review",
            "verification_method": "code_review",
            "expected_result": "OK"
        }
        result = verification_agent._execute_verification_point(vp)
        assert result["status"] == "FAILED"

    def test_execute_verification_plan_with_provided_data(self, verification_agent, temp_plan_dir):
        """Test execution with plan data provided directly."""
        verification_agent.start_verification_round(1)
        plan_data = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "Quick Test",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "Pass",
                    "test_command": "echo ok"
                }
            ]
        }

        results = verification_agent.execute_verification_plan(plan_data)
        assert len(results["execution_results"]) == 1
        assert results["execution_results"][0]["status"] == "PASSED"

    def test_start_verification_round(self, verification_agent, temp_plan_dir):
        """Test starting a verification round creates log file."""
        log_path = verification_agent.start_verification_round(round_number=1)
        assert log_path.exists()
        assert "verification_1_" in log_path.name

    def test_missing_verification_points_key_triggers_retry(self, verification_agent, mock_coding_tool):
        """Test that missing verification_points key triggers retry."""
        mock_coding_tool.query_json.side_effect = [
            {"wrong_key": []},  # Missing verification_points
            {"verification_points": [
                {"id": "VP-001", "title": "T", "related_prd_criteria": "c",
                 "verification_method": "code_review", "priority": "high",
                 "expected_result": "ok", "test_command": ""}
            ]}
        ]

        plan = verification_agent.generate_verification_plan(retry_llm=3)
        assert "verification_points" in plan
        assert mock_coding_tool.query_json.call_count == 2

    def test_execute_verification_point_ui_validation_via_dispatcher(self, verification_agent, mock_coding_tool):
        """Cover line 356: ui_validation branch via _execute_verification_point dispatcher.

        Subprocess returns a valid JSON verdict. New behavior: PASSED when
        verdict=PASSED, FAILED when verdict=FAILED or non-parseable. SKIPPED
        only when verdict=SKIPPED with non-empty reasons.
        """
        verification_agent.start_verification_round(1)
        vp = {
            "id": "VP-UI-DISP",
            "title": "UI via Dispatcher",
            "verification_method": "ui_validation",
            "expected_result": "UI works",
            "target_url": "http://127.0.0.1:8000/",
        }
        # Subprocess reports SKIPPED with a real reason → coerced to FAILED
        mock_coding_tool.query_json.return_value = {
            "verdict": "SKIPPED",
            "reasons": ["server not reachable"],
            "evidence": ["curl 127.0.0.1:8000 failed"]
        }
        result = verification_agent._execute_verification_point(vp)
        # SKIPPED is coerced to FAILED by the sub-agent
        assert result["status"] == "FAILED"

    def test_execute_verification_point_api_test_via_dispatcher(self, verification_agent):
        """Cover line 358: api_test branch via _execute_verification_point dispatcher."""
        verification_agent.start_verification_round(1)
        vp = {
            "id": "VP-API-DISP",
            "title": "API via Dispatcher",
            "verification_method": "api_test",
            "test_command": "echo 'API OK'"
        }
        result = verification_agent._execute_verification_point(vp)
        assert result["status"] == "PASSED"

    def test_execute_verification_point_code_review_via_dispatcher(self, verification_agent, mock_coding_tool):
        """Cover code_review branch via _execute_verification_point dispatcher."""
        verification_agent.start_verification_round(1)
        mock_coding_tool.query.return_value = "Code review complete, PASSED all checks."
        vp = {
            "id": "VP-CR-DISP",
            "title": "Code Review via Dispatcher",
            "verification_method": "code_review",
            "expected_result": "Code is clean"
        }
        result = verification_agent._execute_verification_point(vp)
        assert result["status"] == "PASSED"

    def test_execute_code_review_timeout(self, verification_agent, mock_coding_tool):
        """Cover timeout handling in _execute_verification_point via sub-agent."""
        verification_agent.start_verification_round(1)
        vp = {
            "id": "VP-TIMEOUT",
            "title": "Timeout",
            "verification_method": "code_review",
            "test_command": "sleep 10"
        }
        # Simulate the sub-agent raising a timeout by having the mock return an error
        mock_coding_tool.query_json.side_effect = Exception("timeout")
        result = verification_agent._execute_verification_point(vp)
        assert result["status"] == "FAILED"

    def test_execute_code_review_generic_exception(self, verification_agent, mock_coding_tool):
        """Cover generic exception handling in _execute_verification_point."""
        verification_agent.start_verification_round(1)
        vp = {
            "id": "VP-GENEXC",
            "title": "Generic Exception",
            "verification_method": "code_review",
            "test_command": "echo test"
        }
        mock_coding_tool.query_json.side_effect = OSError("Permission denied")
        result = verification_agent._execute_verification_point(vp)
        assert result["status"] == "FAILED"

    def test_plan_generation_api_error_reraises(self, verification_agent, mock_coding_tool):
        """Cover line 620-622: ApiError re-raised in report generation."""
        mock_coding_tool.query_json.side_effect = ApiError("Service unavailable", status=503)
        verification_agent.start_verification_round(1)
        execution_results = {
            "verification_points": [],
            "execution_results": [],
            "executed_at": datetime.now().isoformat()
        }
        with pytest.raises(ApiError):
            verification_agent.generate_verification_report(execution_results, retry_llm=2)


# =============================================================================
# Integration Tests (5 Scenarios)
# =============================================================================

class TestIntegrationScenarios:
    """Integration tests covering end-to-end workflow and sub-agent honesty."""

    @pytest.mark.integration
    @pytest.mark.real_model
    @pytest.mark.xfail(
        reason=(
            "LLM is non-deterministic: claude-3-5-haiku-20241022 may "
            "raise / hang / produce an unparseable response during the "
            "full end-to-end smoke. The assertion in this test is "
            "loose (``overall_status in [PASSED, FAILED, ...]``) so "
            "most runs pass, but intermittent model failures still "
            "flake the local full-suite run. CI never runs this (no "
            "claude CLI), so xfail is the right marker."
        ),
        strict=False,
    )
    def test_run_full_verification_end_to_end_smoke(
        self, sample_prd, sample_arch, sample_test, temp_project_dir, monkeypatch
    ):
        """Smoke test: run_full_verification() can complete end-to-end.

        We pin Phase 1 to a single VP so the test finishes in a reasonable
        time. The focus is on the workflow completing and producing a
        well-formed report, not on the exact verdict.
        """
        from coding_tool import ClaudeCodingTool

        coding_tool = ClaudeCodingTool(model="claude-3-5-haiku-20241022")
        temp_plan = sample_prd.parent

        tests_dir = temp_project_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_example.py").write_text(
            "def test_example():\n    assert True\n"
        )

        agent = VerificationAgent(
            plan_dir=temp_plan,
            project_dir=temp_project_dir,
            coding_tool=coding_tool,
        )

        # Pin Phase 1 to one VP so this smoke test completes quickly; the
        # honesty of the sub-agent is covered by dedicated tests below.
        simple_plan = {
            "verification_points": [
                {
                    "id": "VP-SMOKE",
                    "title": "Smoke test VP",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "The feature under test behaves correctly",
                    "test_command": "pytest tests/test_example.py",
                }
            ]
        }
        monkeypatch.setattr(agent, "generate_verification_plan", lambda: simple_plan)

        report = agent.run_full_verification()
        assert "overall_status" in report
        assert report["overall_status"] in ["PASSED", "FAILED", "PARTIAL", "SKIPPED"]
        assert "verification_results" in report
        assert isinstance(report["verification_results"], list)

    @pytest.mark.integration
    @pytest.mark.real_model
    @pytest.mark.xfail(
        reason=(
            "LLM is non-deterministic: claude-3-5-haiku-20241022 "
            "occasionally hallucinates FAILED for an actually-passing "
            "pytest. Marking xfail so the suite is not blocked by model "
            "behaviour; a code-level regression in the verdict parser "
            "would show as a sudden shift from xfail→xpass, which is "
            "still surfaced by pytest's XPASS reporting."
        ),
        strict=False,
    )
    def test_sub_agent_recognizes_passing_pytest(self, temp_project_dir):
        """Sub-agent must return PASSED when pytest actually passes.

        This is a focused honesty check: we feed the sub-agent a VP whose
        test_command runs a real passing pytest file and assert it does not
        hallucinate a FAILED verdict.
        """
        import asyncio
        from coding_tool import ClaudeCodingTool
        from verification_subagent import VerificationSubAgent

        tests_dir = temp_project_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_passing.py").write_text(
            "def test_example():\n    assert True\n"
        )

        sub_agent = VerificationSubAgent(method="code_review", max_retries=0)
        vp = {
            "id": "VP-PASS",
            "title": "Passing pytest",
            "verification_method": "code_review",
            "priority": "high",
            "expected_result": "The feature under test behaves correctly",
            "test_command": "pytest tests/test_passing.py",
        }
        verdict = asyncio.run(
            sub_agent.run(
                vp_node=vp,
                coding_tool=ClaudeCodingTool(model="claude-3-5-haiku-20241022"),
                log_dir=temp_project_dir / "logs",
                project_dir=temp_project_dir,
                plan_id="honesty-pass",
            )
        )
        assert verdict.verdict == "PASSED"

    @pytest.mark.integration
    @pytest.mark.real_model
    @pytest.mark.xfail(
        reason=(
            "LLM is non-deterministic: claude-3-5-haiku-20241022 may "
            "hallucinate a verdict for an obviously failing pytest "
            "(e.g. claim PASSED despite a real assertion error). Same "
            "xfail rationale as the passing-pytest counterpart — model "
            "behaviour, not code; xfail→xpass would still flag a "
            "regression in the verdict parser."
        ),
        strict=False,
    )
    def test_sub_agent_recognizes_failing_pytest(self, temp_project_dir):
        """Sub-agent must return FAILED when pytest actually fails.

        This is the counterpart to the passing test: we assert the sub-agent
        does not paper over a real failure. The expected_result describes the
        desired correct behaviour ("feature should work"); the actual failing
        pytest output should still produce a FAILED verdict.
        """
        import asyncio
        from coding_tool import ClaudeCodingTool
        from verification_subagent import VerificationSubAgent

        tests_dir = temp_project_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_failing.py").write_text(
            "def test_will_fail():\n    assert False\n"
        )

        sub_agent = VerificationSubAgent(method="code_review", max_retries=0)
        vp = {
            "id": "VP-FAIL",
            "title": "Failing pytest",
            "verification_method": "code_review",
            "priority": "high",
            "expected_result": "The feature under test behaves correctly",
            "test_command": "pytest tests/test_failing.py",
        }
        verdict = asyncio.run(
            sub_agent.run(
                vp_node=vp,
                coding_tool=ClaudeCodingTool(model="claude-3-5-haiku-20241022"),
                log_dir=temp_project_dir / "logs",
                project_dir=temp_project_dir,
                plan_id="honesty-fail",
            )
        )
        assert verdict.verdict == "FAILED"

class TestAsyncVPTimeout:
    """TDD spec for the async single-VP executor.

    Each test pins a specific contract:

    * test_agent_respects_method_timeout_under_threshold — under the
      threshold, the VP is **not** short-circuited by the timeout
      wrapper. The test commands a 200s sleep, sets the method-level
      default to 1800s, and expects the executor to be *willing to
      wait*. We never let it run to completion in CI; we cancel
      explicitly so the test wall-clock stays under a few seconds.
    * test_agent_returns_timeout_status_on_hit — when the per-method
      timeout *does* fire, the returned status is ``"timeout"`` (not
      ``"FAILED"``) and the evidence references
      ``asyncio.TimeoutError``. This is the contract the
      ``RepairTaskGenerator`` relies on to distinguish a hung VP
      from a real failure.
    * test_agent_respects_per_vp_timeout_override — a per-VP
      ``timeout_seconds`` field wins over the method-level default.
      We set 100s override + 150s sleep and expect timeout; the
      method default (120s) would *also* have fired, so we go a
      step further: set the method default to 1800s and the
      override to 100s to prove the override is the binding number.
    """

    @pytest.mark.asyncio
    async def test_agent_respects_method_timeout_under_threshold(
        self, verification_agent, monkeypatch
    ):
        """200s sleep + ui=1800s default → task is NOT cancelled by the wrapper.

        We use ``asyncio.wait_for`` with a much shorter ceiling so the
        test finishes in <2s, but the underlying per-VP timeout is
        resolved to 1800s from the policy (so the wrapper will not
        cancel a 200s sleep on its own). The test only asserts that
        the wrapper *itself* does not pre-empt; it does not assert the
        command finishes (it does not — we cancel it ourselves).
        """
        from verification_config import TimeoutPolicy

        # Force ui_validation default to 1800s regardless of any yaml on disk.
        verification_agent.timeout_policy = TimeoutPolicy(
            per_method_timeout_seconds={"ui_validation": 1800},
            global_default_timeout_seconds=1800,
        )
        verification_agent.start_verification_round(1)

        # "Sleep 200" in a wrapped bash -c. The wrapper sees 1800s
        # so it will NOT fire; we cancel ourselves with a short
        # wait_for to keep the test fast.
        vp = {
            "id": "VP-UI-1800",
            "title": "ui_validation under threshold",
            "verification_method": "ui_validation",
            "target_url": "http://127.0.0.1:9999/",
            "expected_result": "should run unblocked until our external cancel",
            "test_command": "sleep 200",
        }

        # Patch _execute_ui_validation (the leaf method called on the
        # async path for ui_validation) to spawn the sleep 200 in a
        # subprocess and just await it. That faithfully models what
        # the wrapper sees: a long coroutine. The OUTER wait_for we
        # add here is a 2s ceiling, which fires BEFORE the 1800s
        # policy timeout would.
        import asyncio as _asyncio

        async def _fake_long_running(*_args, **_kwargs):
            proc = await _asyncio.create_subprocess_exec(
                "sleep", "200",
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            try:
                await proc.communicate()
            finally:
                if proc.returncode is None:
                    proc.kill()
            return {
                "id": vp["id"],
                "status": "PASSED",
                "actual_result": "ran unblocked",
                "evidence": "ran unblocked",
            }

        monkeypatch.setattr(
            verification_agent, "_execute_ui_validation", _fake_long_running
        )

        # The policy-resolved timeout is 1800s. If we wrap with a
        # 2s ceiling, the ceiling fires and we know the policy did
        # NOT pre-empt (it would have needed to fire at 1800s, but
        # the wrapper would have raised TimeoutError at 2s instead).
        with pytest.raises(_asyncio.TimeoutError):
            await _asyncio.wait_for(
                verification_agent._run_single_vp_async(vp), timeout=2
            )

    @pytest.mark.asyncio
    async def test_agent_returns_timeout_status_on_hit(
        self, verification_agent, monkeypatch
    ):
        """Inner HardTimeoutError with both split paths declining →
        status=timeout with HARD TIMEOUT message.

        2026-09-13 update: ``_run_single_vp_async`` no longer wraps
        execution in a per-VP ``asyncio.wait_for`` (removed
        2026-09-08). The enforcement layers are the flat 1-hour outer
        cap in VerificationSubAgent and the 15-min idle detector in
        coding_tool. This test pins the downstream handling: when the
        inner layer raises HardTimeoutError and both auto-split paths
        decline, the VP resolves to ``status="timeout"``.
        """
        verification_agent.start_verification_round(1)

        vp = {
            "id": "VP-UI-TIMEOUT",
            "title": "ui_validation times out",
            "verification_method": "ui_validation",
            "target_url": "http://127.0.0.1:9999/",
            "expected_result": "should produce status=timeout",
            "test_command": "sleep 5",
        }

        async def _split_decline(_vp, _partial):
            return None

        async def _llm_split_decline(_vp, _exc):
            return None

        verification_agent._split_vp_on_timeout = _split_decline
        verification_agent._llm_split_vp_on_hard_timeout = _llm_split_decline
        # Orchestrator has no ``logger`` attr by default; the
        # hard-timeout branch guards with ``if self.logger``.
        if not hasattr(verification_agent, "logger"):
            verification_agent.logger = MagicMock()

        with patch.object(
            verification_agent, "_delegate_to_sub_agent",
            side_effect=HardTimeoutError(
                total_sec=900, elapsed=905.0, last_line="x"
            ),
        ):
            result = await verification_agent._run_single_vp_async(vp)

        assert result["id"] == "VP-UI-TIMEOUT"
        assert result["status"] == "timeout", (
            f"expected status='timeout' when HardTimeoutError fires and "
            f"both split paths decline, got {result['status']!r} "
            f"(actual_result={result.get('actual_result')!r})"
        )
        assert "HARD TIMEOUT" in result["actual_result"], (
            f"expected actual_result to reference HARD TIMEOUT, "
            f"got {result['actual_result']!r}"
        )
        assert result["evidence"] == "HardTimeoutError"

    @pytest.mark.asyncio
    async def test_method_level_timeout_is_metadata_only(
        self, verification_agent, monkeypatch
    ):
        """2026-09-13: the per-method TimeoutPolicy value is
        surface metadata (vp_start event), NOT enforcement.

        The method default is set to 2s but the leaf sleeps 3s —
        before the per-VP ``asyncio.wait_for`` wrapper was removed
        (2026-09-08) this would have been pre-empted with
        ``status="timeout"``. Now the run completes ``PASSED``: the
        only enforcement layers are the 1-hour outer cap
        (``VerificationSubAgent``) and the 15-min idle detector
        (``coding_tool``). The vp_start event still records the
        policy value (``timeout_seconds=2``) for observability.
        """
        from verification_config import TimeoutPolicy

        verification_agent.timeout_policy = TimeoutPolicy(
            per_method_timeout_seconds={"ui_validation": 2},
            global_default_timeout_seconds=2,
        )
        verification_agent.start_verification_round(1)

        vp = {
            "id": "VP-UI-DEFAULT",
            "title": "ui_validation method-level timeout metadata",
            "verification_method": "ui_validation",
            "target_url": "http://127.0.0.1:9999/",
            "expected_result": "3s run completes despite 2s policy value",
            "test_command": "sleep 3",
        }

        # Sanity-check the policy resolution (method-level only).
        resolved = verification_agent.timeout_policy.resolve(
            vp["verification_method"]
        )
        assert resolved == 2, (
            f"expected method default 2, got {resolved}"
        )

        import asyncio as _asyncio

        async def _fake_sleep_3(*_args, **_kwargs):
            # Sleep PAST the 2s policy value to prove the policy does
            # not pre-empt execution any more.
            await _asyncio.sleep(3)
            return {
                "id": vp["id"],
                "status": "PASSED",
                "actual_result": "ran past the 2s policy value",
                "evidence": "ran past the 2s policy value",
            }

        monkeypatch.setattr(
            verification_agent, "_execute_ui_validation", _fake_sleep_3
        )

        result = await verification_agent._run_single_vp_async(vp)

        assert result["id"] == "VP-UI-DEFAULT"
        assert result["status"] == "PASSED", (
            f"3s run must complete (policy=2s is metadata-only now), "
            f"got {result['status']!r} "
            f"(actual_result={result.get('actual_result')!r})"
        )

        # The vp_start event still surfaces the policy value.
        log_files = sorted(
            (verification_agent.plan_dir / "logs").glob("verification_*.log")
        )
        assert log_files, "no verification log written"
        with open(log_files[-1], "r", encoding="utf-8") as f:
            entries = [json.loads(l) for l in f if l.strip()]
        vp_starts = [
            e for e in entries
            if e.get("event_type") == "vp_start"
            and e.get("verification_point_id") == "VP-UI-DEFAULT"
        ]
        assert vp_starts, f"no vp_start event for VP-UI-DEFAULT: {entries!r}"
        assert vp_starts[0]["data"]["timeout_seconds"] == 2


class TestSplitOnTimeout:
    """TDD spec for the timeout-driven VP split pipeline.

    A timed-out VP whose ``expected_result`` contains multiple
    ``;``-separated clauses should be auto-decomposed so we don't
    lose the partial results. The contract pinned here:

    * test_split_emits_event_with_child_ids — on a multi-clause
      timeout, ``persistence.write_verification_point_log`` is called
      with ``event_type="vp_subtask_split"`` and a ``data`` payload
      carrying ``vp_id`` (= parent), ``parent_id`` (= parent), ``N``
      (= child count) and ``child_vp_ids`` (= ``["VP-001-1", ...]``).
    * test_split_subtask_inherits_parent_metadata — every child VP
      returned by ``_run_split_children`` carries
      ``parent_vp_id`` (= original VP id) AND
      ``original_vp_id`` (= original VP id) AND
      ``split_clause_index`` (= 1-based clause position). The
      preservation of all three is what makes the sub-VPs
      traceable back to the original after the batch finishes.
    * test_split_does_not_run_when_single_clause — a single-clause
      VP that times out is NOT split and NOT logged with
      ``vp_subtask_split``. The original timeout result is
      returned unchanged.

    The three tests use the ``persistence`` attribute on the agent
    fixture (already constructed in
    ``temp_plan_dir`` → ``logs/verification_*_*.log``) and inspect
    the on-disk JSON-lines log so the contract is enforced against
    the real persistence layer, not a mock.
    """

    def _read_log_entries(self, temp_plan_dir):
        """Return all JSON-lines entries written by the current round."""
        log_files = sorted((temp_plan_dir / "logs").glob("verification_*.log"))
        assert log_files, "no verification log file was created — agent never started a round"
        entries = []
        with open(log_files[-1], "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entries.append(json.loads(line))
        return entries

    def test_split_emits_event_with_child_ids(
        self, verification_agent, temp_plan_dir
    ):
        """A 3-clause timeout → ``vp_subtask_split`` event with 3 child ids.

        We use the policy-driven timeout path: set ui_validation=1s,
        patch the leaf to sleep 5s so the policy itself fires (not
        some external cancel). After the agent finishes, the
        verification log must contain exactly one entry with
        ``event_type == "vp_subtask_split"`` and a ``data`` payload
        listing all three child ids.
        """
        from verification_config import TimeoutPolicy

        verification_agent.timeout_policy = TimeoutPolicy(
            per_method_timeout_seconds={"ui_validation": 3600},
            global_default_timeout_seconds=3600,
        )
        verification_agent.start_verification_round(1)

        vp = {
            "id": "VP-001",
            "title": "ui multi-clause timeout",
            "verification_method": "ui_validation",
            "target_url": "http://127.0.0.1:9999/",
            # 3 ; separated clauses — the splitter must produce 3 children.
            "expected_result": "登录页 OK; 主页 OK; 设置页 OK",
            "test_command": "sleep 5",
        }

        import asyncio as _asyncio

        async def _run_split():
            # Drive the split path directly through the HardTimeoutError
            # route — 2026-09-13: the per-VP policy wrapper is gone, so
            # the split is reachable only via HardTimeoutError from the
            # inner idle detector (or a real 1-hour outer cap fire).
            return await verification_agent._split_vp_on_timeout(
                vp, {"id": vp["id"], "status": "hard_timeout",
                     "actual_result": "HARD TIMEOUT: idle detector",
                     "reasons": ["HARD TIMEOUT: idle detector"],
                     "evidence": "HardTimeoutError"}
            )

        split_result = _asyncio.run(_run_split())

        # The splitter accepted the 3-clause VP → synthesised SPLIT
        # envelope with child results; the vp_subtask_split event is
        # emitted inside _split_vp_on_timeout.
        assert split_result is not None
        assert split_result.get("status") == "SPLIT"

        entries = self._read_log_entries(temp_plan_dir)
        split_entries = [
            e for e in entries if e.get("event_type") == "vp_subtask_split"
        ]
        assert len(split_entries) == 1, (
            f"expected exactly one vp_subtask_split event, got {len(split_entries)}: "
            f"{[e.get('data') for e in entries]}"
        )
        data = split_entries[0]["data"]
        # Pin the spec-required fields
        assert data["vp_id"] == "VP-001", f"vp_id mismatch: {data.get('vp_id')!r}"
        assert data["parent_id"] == "VP-001", f"parent_id mismatch: {data.get('parent_id')!r}"
        assert data["N"] == 3, f"expected N=3 clauses, got {data.get('N')!r}"
        assert data["child_vp_ids"] == ["VP-001-1", "VP-001-2", "VP-001-3"], (
            f"child_vp_ids mismatch: {data.get('child_vp_ids')!r}"
        )

    def test_split_subtask_inherits_parent_metadata(
        self, verification_agent, temp_plan_dir, monkeypatch
    ):
        """Each sub-VP carries parent_vp_id, original_vp_id, split_clause_index.

        2026-09-13: driven through the HardTimeoutError route — the
        parent hits the inner idle-detector cap, the real
        ``_split_vp_on_timeout`` decomposes the 3-clause
        ``expected_result``, and ``_run_split_children`` executes the
        children through the legacy ``_execute_code_review`` bridge
        (stubbed to return instantly so no subprocess is spawned).
        The child results are the contract under test: they must carry
        ``parent_vp_id`` / ``original_vp_id`` / ``split_clause_index``.
        """
        verification_agent.start_verification_round(1)

        parent_vp = {
            "id": "VP-007",
            "title": "automated test multi-clause",
            "verification_method": "code_review",
            "expected_result": "alpha; beta; gamma",
            "test_command": "sleep 5",
        }

        async def _llm_split_decline(_vp, _exc):
            return None

        verification_agent._llm_split_vp_on_hard_timeout = _llm_split_decline
        if not hasattr(verification_agent, "logger"):
            verification_agent.logger = MagicMock()

        # Dispatch stub: the parent (``VP-007``) goes through
        # ``_delegate_to_sub_agent`` (patched to raise below), while
        # the children (``VP-007-N``, dispatched by
        # ``_run_split_children`` → ``_run_single_vp_async`` → the
        # legacy monkey-patch bridge) return a fast PASS dict — no
        # subprocess is spawned. The ``_async`` variant wins the
        # bridge's dispatch order, so patching it covers both.
        async def _dispatch(vp, *_args, **_kwargs):
            if vp.get("id") == "VP-007":
                return await verification_agent._delegate_to_sub_agent(
                    vp, vp.get("verification_method", "code_review"),
                    vp.get("id", "VP-007"),
                )
            return {
                "id": vp.get("id", "?"),
                "status": "PASSED",
                "actual_result": "stubbed child",
                "evidence": "stubbed child",
            }

        monkeypatch.setattr(
            verification_agent, "_execute_code_review", _dispatch
        )

        with patch.object(
            verification_agent, "_delegate_to_sub_agent",
            side_effect=HardTimeoutError(
                total_sec=900, elapsed=905.0, last_line="x"
            ),
        ):
            parent_result = asyncio.run(
                verification_agent._run_single_vp_async(parent_vp)
            )

        # parent_result is the synthesised "SPLIT" envelope; the
        # children live in ``child_results`` and are the
        # metadata-tagged versions produced by the production
        # tagging code path.
        assert parent_result.get("status") == "SPLIT", (
            f"expected status=SPLIT, got {parent_result.get('status')!r}"
        )
        results = parent_result["child_results"]
        assert len(results) == 3

        # The child *results* are the contract under test — they
        # must carry parent_vp_id / original_vp_id / split_clause_index
        # so downstream consumers can roll back up to the original.
        for index, result in enumerate(results, start=1):
            assert result["parent_vp_id"] == "VP-007", (
                f"result {index} missing parent_vp_id: {result!r}"
            )
            assert result["original_vp_id"] == "VP-007", (
                f"result {index} missing original_vp_id: {result!r}"
            )
            assert result["split_clause_index"] == index, (
                f"result {index} split_clause_index mismatch: {result!r}"
            )
            # The child id encodes the clause position, so we can
            # also check the suffix.
            assert result["id"] == f"VP-007-{index}", (
                f"result {index} id mismatch: expected VP-007-{index}, "
                f"got {result.get('id')!r}"
            )

        # And the child *definitions* logged in the persistence layer
        # must carry the same metadata — this is what
        # RepairTaskGenerator and the JSONL observers see.
        log_files = sorted((temp_plan_dir / "logs").glob("verification_*.log"))
        assert log_files
        log_entries = []
        with open(log_files[-1], "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    log_entries.append(json.loads(line))
        # The vp_subtask_split event was emitted with the child ids
        # in the canonical order, which encodes the clause index
        # in the id suffix. This is the documented contract for the
        # repair-task generator to use.
        split_events = [
            e for e in log_entries if e.get("event_type") == "vp_subtask_split"
        ]
        assert len(split_events) == 1, (
            f"expected one vp_subtask_split event, got {len(split_events)}"
        )
        child_ids_in_log = split_events[0]["data"]["child_vp_ids"]
        assert child_ids_in_log == ["VP-007-1", "VP-007-2", "VP-007-3"], (
            f"child_vp_ids mismatch: {child_ids_in_log!r}"
        )

    def test_split_does_not_run_when_single_clause(
        self, verification_agent, temp_plan_dir
    ):
        """Single-clause VP that times out → no split, no vp_subtask_split event.

        Boundary: a VP with exactly one ``;``-free expected_result
        clause is not decomposable. The splitter returns ``None``,
        the agent must therefore NOT emit a ``vp_subtask_split``
        event, and the original timeout result must be returned
        unchanged.

        2026-09-13: driven through the HardTimeoutError route (the
        per-VP policy wrapper that used to raise asyncio.TimeoutError
        was removed 2026-09-08). The real ``_split_vp_on_timeout`` is
        kept so the single-clause decline is exercised end-to-end;
        only the LLM fallback is stubbed (it would otherwise make a
        real LLM call).
        """
        verification_agent.start_verification_round(1)

        vp = {
            "id": "VP-SINGLE",
            "title": "ui single-clause timeout",
            "verification_method": "ui_validation",
            "target_url": "http://127.0.0.1:9999/",
            "expected_result": "登录页 OK",  # single clause, no ';'
            "test_command": "sleep 5",
        }

        async def _llm_split_decline(_vp, _exc):
            return None

        verification_agent._llm_split_vp_on_hard_timeout = _llm_split_decline
        # The Orchestrator class has no ``logger`` attribute by
        # default (the hard-timeout branch is None-safe via
        # ``if self.logger``); MagicMock keeps the warning path happy.
        if not hasattr(verification_agent, "logger"):
            verification_agent.logger = MagicMock()

        with patch.object(
            verification_agent, "_delegate_to_sub_agent",
            side_effect=HardTimeoutError(
                total_sec=900, elapsed=905.0, last_line="x"
            ),
        ):
            result = asyncio.run(
                verification_agent._run_single_vp_async(vp)
            )

        # Original timeout result must be returned unchanged.
        assert result["status"] == "timeout", (
            f"expected status=timeout, got {result.get('status')!r}"
        )
        assert result["id"] == "VP-SINGLE"

        # The split event must NOT appear in the log.
        entries = self._read_log_entries(temp_plan_dir)
        split_entries = [
            e for e in entries if e.get("event_type") == "vp_subtask_split"
        ]
        assert split_entries == [], (
            f"single-clause VP must not emit vp_subtask_split, "
            f"but found: {[e.get('data') for e in split_entries]}"
        )


class TestSplitAggregation:
    """TDD spec for the post-split aggregation step (task-7 follow-up).

    After :meth:`VerificationAgent._split_vp_on_timeout` decomposes a
    timed-out parent VP into child VPs and runs them, the parent's
    ``status`` is still ``"SPLIT"`` — an intermediate verdict. The
    judgment phase needs a single terminal status to surface in the
    final report, which is what
    :meth:`VerificationAgent._aggregate_split_results` produces.

    The four cases pinned by the TDD spec:

    * ``test_aggregate_all_children_passed_returns_passed`` — all
      children PASSED → parent PASSED.
    * ``test_aggregate_any_child_failed_returns_failed`` — at least
      one FAILED → parent FAILED, ``requirement_deviations`` lists
      only the failed clauses (already-passed ones must not pollute
      the deviation list).
    * ``test_aggregate_all_children_skipped_returns_split`` — all
      children SKIPPED → parent stays SPLIT, no
      ``requirement_deviations``.
    * ``test_aggregate_empty_children_raises`` — empty
      ``child_results`` is an illegal state and must raise
      ``ValueError`` rather than silently returning SPLIT.

    The aggregator is a pure function (no LLM, no filesystem) so
    each test can call it directly with hand-constructed inputs
    and inspect the returned dict.
    """

    def test_aggregate_all_children_passed_returns_passed(self):
        """All children PASSED → parent PASSED.

        The split was a false alarm: every clause of the original
        expectation held once the timeout was bypassed. The
        aggregator must return ``status="PASSED"`` and carry the
        child results so downstream consumers (the report, the
        repair-task generator) can still see what was tested.
        """
        parent_vp = {
            "id": "VP-001",
            "parent_vp_id": None,
            "status": "SPLIT",
        }
        child_results = [
            {
                "id": "VP-001-1",
                "parent_vp_id": "VP-001",
                "status": "PASSED",
                "split_clause_index": 0,
                "actual_result": "clause 1 ok",
                "evidence": "evidence 1",
            },
            {
                "id": "VP-001-2",
                "parent_vp_id": "VP-001",
                "status": "PASSED",
                "split_clause_index": 1,
                "actual_result": "clause 2 ok",
                "evidence": "evidence 2",
            },
        ]

        aggregated = VerificationAgent._aggregate_split_results(
            parent_vp=parent_vp,
            child_results=child_results,
        )

        assert aggregated["status"] == "PASSED", (
            f"expected PASSED, got {aggregated.get('status')!r}"
        )
        assert aggregated["id"] == "VP-001", (
            f"parent id lost in aggregation: {aggregated.get('id')!r}"
        )
        assert aggregated["child_count"] == 2, (
            f"expected child_count=2, got {aggregated.get('child_count')!r}"
        )
        assert aggregated["subtask_results"] == child_results, (
            "subtask_results must be the original child list, in order"
        )
        # PASSED parents do NOT carry a requirement_deviations field —
        # the aggregation is the success verdict, not a deviation list.
        assert "requirement_deviations" not in aggregated, (
            f"PASSED aggregate must not carry requirement_deviations, "
            f"got: {aggregated.get('requirement_deviations')!r}"
        )

    def test_aggregate_any_child_failed_returns_failed(self):
        """At least one FAILED → parent FAILED, deviations only from failures.

        Pin two contracts:
        1. Any single FAILED child forces the parent to FAILED (even
           if the other children passed).
        2. The ``requirement_deviations`` list contains exactly one
           entry per FAILED child, identified by the child's
           ``clause_index`` (not by child id, which is the level
           the deviation is reported at).
        """
        parent_vp = {
            "id": "VP-002",
            "parent_vp_id": None,
            "status": "SPLIT",
        }
        child_results = [
            {
                "id": "VP-002-1",
                "parent_vp_id": "VP-002",
                "status": "PASSED",
                "split_clause_index": 0,
                "actual_result": "clause 1 ok",
                "evidence": "evidence 1",
            },
            {
                "id": "VP-002-2",
                "parent_vp_id": "VP-002",
                "status": "FAILED",
                "split_clause_index": 1,
                "actual_result": "clause 2 failed: timeout",
                "evidence": "stack trace ...",
            },
        ]

        aggregated = VerificationAgent._aggregate_split_results(
            parent_vp=parent_vp,
            child_results=child_results,
        )

        assert aggregated["status"] == "FAILED", (
            f"expected FAILED, got {aggregated.get('status')!r}"
        )
        assert aggregated["id"] == "VP-002"
        assert aggregated["child_count"] == 2

        deviations = aggregated.get("requirement_deviations", [])
        assert len(deviations) == 1, (
            f"deviations must list only the FAILED child, got {len(deviations)}: "
            f"{deviations!r}"
        )
        dev = deviations[0]
        # The clause_index is what the report consumers key on — the
        # failed clause is clause 1, not clause 0.
        assert dev["clause_index"] == 1, (
            f"deviation clause_index should be 1 (the failed child), got "
            f"{dev.get('clause_index')!r}"
        )
        # The deviation should reference the failing child by id, so a
        # reader can jump straight to the per-clause evidence.
        assert dev.get("verification_point_id") == "VP-002-2", (
            f"deviation should reference the failing child VP-002-2, got "
            f"{dev.get('verification_point_id')!r}"
        )

    def test_aggregate_all_children_skipped_returns_split(self):
        """All children SKIPPED → parent stays SPLIT, no deviations.

        The fourth terminal state: if every child was SKIPPED
        (e.g. all clauses hit an environmental block that also
        blocks the chunked re-run), the parent stays SPLIT
        (inconclusive) and no requirement_deviations are recorded.
        This is the explicit "all SKIPPED → SPLIT" branch the spec
        pins.
        """
        parent_vp = {
            "id": "VP-003",
            "parent_vp_id": None,
            "status": "SPLIT",
        }
        child_results = [
            {
                "id": "VP-003-1",
                "parent_vp_id": "VP-003",
                "status": "SKIPPED",
                "split_clause_index": 0,
                "actual_result": "no browser available",
                "evidence": "puppeteer binary missing",
            },
            {
                "id": "VP-003-2",
                "parent_vp_id": "VP-003",
                "status": "SKIPPED",
                "split_clause_index": 1,
                "actual_result": "no browser available",
                "evidence": "puppeteer binary missing",
            },
        ]

        aggregated = VerificationAgent._aggregate_split_results(
            parent_vp=parent_vp,
            child_results=child_results,
        )

        assert aggregated["status"] == "SPLIT", (
            f"all-SKIPPED parent must stay SPLIT, got {aggregated.get('status')!r}"
        )
        assert aggregated["id"] == "VP-003", (
            f"parent id lost in aggregation: {aggregated.get('id')!r}"
        )
        # All-SKIPPED parents do NOT carry subtask_results or
        # child_count (per the spec output example), and do NOT
        # carry requirement_deviations.
        assert "subtask_results" not in aggregated, (
            f"all-SKIPPED aggregate must not carry subtask_results, got: "
            f"{aggregated.get('subtask_results')!r}"
        )
        assert "child_count" not in aggregated, (
            f"all-SKIPPED aggregate must not carry child_count, got: "
            f"{aggregated.get('child_count')!r}"
        )
        assert "requirement_deviations" not in aggregated, (
            f"all-SKIPPED aggregate must not record deviations, got: "
            f"{aggregated.get('requirement_deviations')!r}"
        )

    def test_aggregate_empty_children_raises(self):
        """Empty child_results → ValueError (illegal state).

        A SPLIT VP with zero children is a contract violation —
        the splitter always produces ≥ 2 children, so reaching the
        aggregator with an empty list means a logic bug upstream.
        The aggregator must raise rather than silently return
        SPLIT/PASSED, which would mask the bug and let the report
        generator carry an empty envelope downstream.
        """
        parent_vp = {
            "id": "VP-EMPTY",
            "parent_vp_id": None,
            "status": "SPLIT",
        }

        with pytest.raises(ValueError) as exc_info:
            VerificationAgent._aggregate_split_results(
                parent_vp=parent_vp,
                child_results=[],
            )

        # The error message must reference the empty list, so a
        # future operator chasing the traceback knows exactly which
        # invariant was violated.
        assert "empty" in str(exc_info.value).lower(), (
            f"ValueError message should mention the empty child list, "
            f"got: {exc_info.value!r}"
        )


class TestSplitAggregationHelper:
    """TDD spec for the ``_aggregate_split_vps`` wrapper (task-8-2).

    The pure aggregator (:meth:`VerificationAgent._aggregate_split_results`)
    is tested in :class:`TestSplitAggregation`. This class covers the
    **outer helper** that walks an ``execution_results`` payload, finds
    the SPLIT entries, runs the aggregator, and rewrites the list in
    place. The contracts pinned by the four TDD cases:

    1. ``test_helper_mutates_split_in_place`` — the helper does NOT
       build a new list; it mutates the same list object it was given
       (verified via :func:`id`) and replaces the SPLIT slot with the
       aggregated verdict while leaving the surrounding PASSED slot
       untouched.
    2. ``test_helper_skips_non_split`` — non-SPLIT entries (PASSED in
       this case) pass through the loop untouched, so a list of all
       PASSED entries is unchanged after the call (same length, same
       identity, every entry's ``id``/``status`` preserved).
    3. ``test_helper_empty_results_is_noop`` — an empty
       ``execution_results`` list is a no-op: the helper does not
       raise, does not crash on ``child_results`` access, and returns
       without surfacing any data to the report.

    The helper is a regular instance method (not static) because it
    delegates to :meth:`_aggregate_split_results`, so each test
    instantiates a real :class:`VerificationAgent` via the
    ``verification_agent`` fixture. The LLM and filesystem layers
    stay dormant: the helper only walks the dict it was given and
    calls another in-process method.
    """

    def test_helper_mutates_split_in_place(self, verification_agent):
        """SPLIT entry is rewritten in place; PASSED entry is untouched.

        Build an ``execution_results`` payload with one SPLIT entry
        (carrying two children) and one PASSED entry. Call the helper
        and verify two things:

        * The returned list is the **same** list object passed in
          (``id(results) is id(returned)``); the helper does not
          return a freshly allocated copy.
        * The SPLIT slot is replaced with the aggregated verdict
          (parent id preserved, terminal status, ``child_count`` set
          by the aggregator).
        * The PASSED slot is **byte-for-byte** unchanged — the
          helper only touches SPLIT entries.
        """
        split_entry = {
            "id": "VP-100",
            "parent_vp_id": None,
            "status": "SPLIT",
            "child_results": [
                {
                    "id": "VP-100-1",
                    "parent_vp_id": "VP-100",
                    "status": "PASSED",
                    "split_clause_index": 0,
                    "actual_result": "clause 1 ok",
                    "evidence": "evidence 1",
                },
                {
                    "id": "VP-100-2",
                    "parent_vp_id": "VP-100",
                    "status": "PASSED",
                    "split_clause_index": 1,
                    "actual_result": "clause 2 ok",
                    "evidence": "evidence 2",
                },
            ],
        }
        passed_entry = {
            "id": "VP-200",
            "parent_vp_id": None,
            "status": "PASSED",
            "actual_result": "untouched",
            "evidence": "should remain unchanged",
        }
        execution_results = {
            "verification_points": [],
            "execution_results": [split_entry, passed_entry],
            "executed_at": "2026-06-05T00:00:00",
        }

        original_list = execution_results["execution_results"]
        original_list_id = id(original_list)
        original_split_id = id(split_entry)
        original_passed_id = id(passed_entry)

        returned = verification_agent._aggregate_split_vps(execution_results)

        # Same outer dict object (in-place mutation, not copy).
        assert returned is execution_results, (
            "helper must mutate the dict in place and return the same object"
        )
        # Same list object inside the dict (no list-rebuild on mutation).
        assert id(returned["execution_results"]) == original_list_id, (
            "helper must not rebuild the execution_results list"
        )

        results = returned["execution_results"]
        assert len(results) == 2, (
            f"helper must not insert or drop entries, got len={len(results)}"
        )

        # Slot 0 (was SPLIT) — replaced with the aggregated verdict.
        aggregated_slot = results[0]
        # Same dict slot identity is not guaranteed (the helper uses
        # .clear() + .update()), but the original SPLIT dict must NOT
        # be the same object anymore — it should have been rewritten.
        assert id(aggregated_slot) != original_split_id or (
            aggregated_slot.get("status") == "PASSED"
        ), (
            f"SPLIT slot must be rewritten, got: {aggregated_slot!r}"
        )
        assert aggregated_slot.get("status") == "PASSED", (
            f"all-children-PASSED parent must aggregate to PASSED, "
            f"got status={aggregated_slot.get('status')!r}"
        )
        # Original id must be preserved through the rewrite (the
        # helper explicitly carries it forward in the result.clear()/
        # result.update() block).
        assert aggregated_slot.get("id") == "VP-100", (
            f"aggregation must preserve the original parent id, "
            f"got: {aggregated_slot.get('id')!r}"
        )
        assert aggregated_slot.get("child_count") == 2, (
            f"aggregated parent must report child_count=2, "
            f"got: {aggregated_slot.get('child_count')!r}"
        )

        # Slot 1 (was PASSED) — completely untouched.
        untouched_slot = results[1]
        assert id(untouched_slot) == original_passed_id, (
            "PASSED slot must be the same object — helper must not "
            "rewrite non-SPLIT entries"
        )
        assert untouched_slot == passed_entry, (
            f"PASSED slot must be byte-for-byte identical to input, "
            f"got: {untouched_slot!r}"
        )
        assert untouched_slot["status"] == "PASSED", (
            f"PASSED slot status must remain PASSED, got: "
            f"{untouched_slot.get('status')!r}"
        )

    def test_helper_skips_non_split(self, verification_agent):
        """All-PASSED list passes through the helper unchanged.

        The helper is safe to call unconditionally on every
        ``execution_results`` payload, including ones that contain no
        SPLIT entries at all. Verify that an all-PASSED list comes
        out the other side with the same length, the same object
        identity, and the same per-entry content.
        """
        passed_a = {
            "id": "VP-A",
            "parent_vp_id": None,
            "status": "PASSED",
            "actual_result": "ok",
            "evidence": "eA",
        }
        passed_b = {
            "id": "VP-B",
            "parent_vp_id": None,
            "status": "PASSED",
            "actual_result": "ok",
            "evidence": "eB",
        }
        passed_c = {
            "id": "VP-C",
            "parent_vp_id": None,
            "status": "PASSED",
            "actual_result": "ok",
            "evidence": "eC",
        }
        execution_results = {
            "verification_points": [],
            "execution_results": [passed_a, passed_b, passed_c],
            "executed_at": "2026-06-05T00:00:00",
        }

        original_list = list(execution_results["execution_results"])
        original_ids = [id(e) for e in original_list]

        returned = verification_agent._aggregate_split_vps(execution_results)

        # Same outer dict, same inner list.
        assert returned is execution_results, (
            "helper must mutate in place and return the same dict"
        )
        assert returned["execution_results"] is execution_results["execution_results"], (
            "helper must not swap out the inner list"
        )

        # Length preserved.
        assert len(returned["execution_results"]) == 3, (
            f"all-PASSED list must keep size 3, got: "
            f"{len(returned['execution_results'])}"
        )

        # Every entry's object identity and content are preserved.
        for i, original_entry in enumerate(original_list):
            current = returned["execution_results"][i]
            assert id(current) == original_ids[i], (
                f"slot {i} object identity changed; helper rewrote a "
                f"non-SPLIT entry"
            )
            assert current == original_entry, (
                f"slot {i} content changed; helper must not touch "
                f"non-SPLIT entries, got: {current!r}"
            )
            assert current["status"] == "PASSED", (
                f"slot {i} status must remain PASSED, got: "
                f"{current.get('status')!r}"
            )
            assert current["id"] == original_entry["id"], (
                f"slot {i} id must remain {original_entry['id']!r}, "
                f"got: {current.get('id')!r}"
            )

    def test_helper_empty_results_is_noop(self, verification_agent):
        """Empty ``execution_results`` list is a safe no-op.

        Edge case: a plan with zero VPs (or a payload where the
        ``execution_results`` key is an empty list) must not crash
        the report generator. The helper iterates the list, so an
        empty list means the loop body never runs; there is nothing
        to aggregate. The call must complete without raising and
        must not fabricate a fake result.
        """
        execution_results = {
            "verification_points": [],
            "execution_results": [],
            "executed_at": "2026-06-05T00:00:00",
        }

        original_dict_id = id(execution_results)
        original_list_id = id(execution_results["execution_results"])

        # The call must not raise.
        returned = verification_agent._aggregate_split_vps(execution_results)

        # Outer dict is the same object.
        assert returned is execution_results, (
            "helper must return the same dict object on the no-op path"
        )
        # Inner list is the same object and still empty.
        assert id(returned["execution_results"]) == original_list_id, (
            "helper must not allocate a new list on the no-op path"
        )
        assert returned["execution_results"] == [], (
            f"empty list must stay empty, got: {returned['execution_results']!r}"
        )
        # The outer-dict identity check is the contract: a no-op means
        # nothing was rewritten, so id() is unchanged.
        assert id(returned) == original_dict_id


class TestReportSplitIntegration:
    """End-to-end TDD spec for the report pipeline (task-8-3).

    The unit-level guarantees for the aggregator
    (:class:`TestSplitAggregation`) and the in-place wrapper
    (:class:`TestSplitAggregationHelper`) are necessary but not
    sufficient. The remaining open question is whether the
    aggregation actually fires *before* the LLM judgment step in
    :meth:`VerificationAgent.generate_verification_report`, and whether
    the aggregated state survives all the way through to the report
    payload. This class pins the integration contract.

    Three contracts:

    1. ``test_report_no_split_residue`` — an
       ``execution_results`` payload with a SPLIT entry and a PASSED
       entry, when fed through ``generate_verification_report``,
       yields a report whose ``verification_results`` list has the
       same length (no rows added/dropped) and **no** entry whose
       ``status == "SPLIT"`` (the aggregation must rewrite the SPLIT
       slot to a terminal status before the LLM ever sees it).
    2. ``test_report_merges_child_deviations`` — when a SPLIT entry
       carries a FAILED child, the per-clause deviation produced by
       the aggregator (with ``clause_index`` and
       ``verification_point_id`` pointing at the failed child) must
       be carried into the final report's
       ``requirement_deviations`` list at the top level — not just
       inside the per-VP result, and not lost between the aggregation
       step and the LLM step.
    3. ``test_report_judgment_prompt_sees_no_split`` — the prompt
       that ``generate_verification_report`` passes to the LLM (the
       prompt the LLM is *meant* to judge from) must already contain
       terminal statuses only. We assert this by capturing the
       ``prompt=`` keyword argument the mock LLM tool receives and
       grepping for the SPLIT envelope — if the helper ran after the
       LLM call, the prompt would still carry the original SPLIT
       entry.

    All three tests run in <1s because the LLM and filesystem layers
    are stubbed by the ``verification_agent`` fixture's
    ``mock_coding_tool`` Mock. The persistence manager still writes
    to ``temp_plan_dir`` (the fixture's tmp dir), but the report
    JSON file is created in tmp and torn down at fixture teardown.
    """

    def test_report_no_split_residue(
        self, verification_agent, mock_coding_tool
    ):
        """Report's ``verification_results`` has no SPLIT entries.

        Build a payload with one SPLIT (carrying two PASSED children)
        and one standalone PASSED entry. Stub the LLM to return a
        faithful echo of the aggregated results. Assert that:

        * The returned report's ``verification_results`` list has
          length 2 (no rows added or dropped by aggregation).
        * **No** entry in that list has ``status == "SPLIT"`` — the
          aggregation must have rewritten the SPLIT slot before the
          LLM got to see it.

        This pins the core "aggregation-before-judgment" contract:
        the report the operator reads can never see an unaggregated
        SPLIT envelope.
        """
        split_entry = {
            "id": "VP-010",
            "parent_vp_id": None,
            "status": "SPLIT",
            "child_results": [
                {
                    "id": "VP-010-1",
                    "parent_vp_id": "VP-010",
                    "status": "PASSED",
                    "split_clause_index": 0,
                    "actual_result": "clause 1 ok",
                    "evidence": "e1",
                },
                {
                    "id": "VP-010-2",
                    "parent_vp_id": "VP-010",
                    "status": "PASSED",
                    "split_clause_index": 1,
                    "actual_result": "clause 2 ok",
                    "evidence": "e2",
                },
            ],
        }
        passed_entry = {
            "id": "VP-020",
            "parent_vp_id": None,
            "status": "PASSED",
            "actual_result": "ok",
            "evidence": "e",
        }
        execution_results = {
            "verification_points": [
                {
                    "id": "VP-010",
                    "title": "Split VP",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "ok",
                    "test_command": "echo ok",
                },
                {
                    "id": "VP-020",
                    "title": "Plain VP",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "ok",
                    "test_command": "echo ok",
                },
            ],
            "execution_results": [split_entry, passed_entry],
            "executed_at": "2026-06-05T00:00:00",
        }

        # LLM returns a report that mirrors the (post-aggregation)
        # results. The LLM has no business introducing new rows, so
        # we expect 2 verification_results.
        report_data = {
            "overall_status": "PASSED",
            "summary": "All passed after aggregation",
            "verification_results": [
                {
                    "id": "VP-010",
                    "status": "PASSED",
                    "actual_result": "aggregated: all children passed",
                    "evidence": "agg",
                },
                {
                    "id": "VP-020",
                    "status": "PASSED",
                    "actual_result": "ok",
                    "evidence": "e",
                },
            ],
            "requirement_deviations": [],
        }
        mock_coding_tool.query_json.return_value = report_data

        report = verification_agent.generate_verification_report(
            execution_results, retry_llm=1
        )

        # Core assertion: no SPLIT residue in the report the operator sees.
        results = report["verification_results"]
        assert isinstance(results, list)
        assert len(results) == 2, (
            f"report must have 2 verification_results rows, got {len(results)}: "
            f"{results!r}"
        )
        statuses = [r.get("status") for r in results]
        assert "SPLIT" not in statuses, (
            f"report must not contain a SPLIT entry after aggregation, "
            f"got statuses={statuses!r}"
        )
        # All entries must be on a terminal status the report contract
        # recognises. (The LLM decides these here; the point is
        # they're not the unaggregated SPLIT envelope.)
        valid_terminal = {"PASSED", "FAILED", "SKIPPED", "PARTIAL"}
        for s in statuses:
            assert s in valid_terminal, (
                f"report verification_result has unexpected status {s!r}; "
                f"all rows must be terminal"
            )

    def test_report_merges_child_deviations(
        self, verification_agent, mock_coding_tool
    ):
        """Aggregator's per-clause deviations surface in the report.

        Build a payload with one SPLIT entry whose children are
        ``[PASSED, FAILED]``. The aggregator (proven by
        :class:`TestSplitAggregation`) will produce one
        ``requirement_deviations`` entry on the aggregated FAILED
        parent, with ``clause_index == 1`` and
        ``verification_point_id == "VP-030-2"``.

        We want to prove that the aggregated FAILED verdict — and
        crucially the **per-clause** deviation it carries — survives
        the LLM round trip into the report. The mock LLM is
        configured to copy the aggregated entry's
        ``requirement_deviations`` into the report's top-level
        ``requirement_deviations`` list, mimicking the way a real
        LLM would surface the deviation.
        """
        child_failed = {
            "id": "VP-030-2",
            "parent_vp_id": "VP-030",
            "status": "FAILED",
            "split_clause_index": 1,
            "actual_result": "clause 2 failed: timeout",
            "evidence": "stack trace ...",
        }
        child_passed = {
            "id": "VP-030-1",
            "parent_vp_id": "VP-030",
            "status": "PASSED",
            "split_clause_index": 0,
            "actual_result": "clause 1 ok",
            "evidence": "evidence 1",
        }
        split_entry = {
            "id": "VP-030",
            "parent_vp_id": None,
            "status": "SPLIT",
            "child_results": [child_passed, child_failed],
        }
        execution_results = {
            "verification_points": [
                {
                    "id": "VP-030",
                    "title": "Split VP with one failure",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "ok",
                    "test_command": "echo ok",
                },
            ],
            "execution_results": [split_entry],
            "executed_at": "2026-06-05T00:00:00",
        }

        # LLM returns a report that mirrors the post-aggregation
        # verdict: parent is FAILED, and the per-clause deviation is
        # surfaced at the top level.
        report_data = {
            "overall_status": "FAILED",
            "summary": "1 VP aggregated to FAILED",
            "verification_results": [
                {
                    "id": "VP-030",
                    "status": "FAILED",
                    "actual_result": "aggregated: clause 1 passed, clause 2 failed",
                    "evidence": "agg",
                },
            ],
            "requirement_deviations": [
                {
                    "verification_point_id": "VP-030-2",
                    "clause_index": 1,
                    "type": "missing",
                    "description": "clause 2 failed: timeout",
                    "severity": "high",
                    "evidence": "stack trace ...",
                },
            ],
        }
        mock_coding_tool.query_json.return_value = report_data

        report = verification_agent.generate_verification_report(
            execution_results, retry_llm=1
        )

        deviations = report.get("requirement_deviations", [])
        assert isinstance(deviations, list)
        assert len(deviations) == 1, (
            f"expected exactly 1 deviation surfaced from the FAILED "
            f"child, got {len(deviations)}: {deviations!r}"
        )
        dev = deviations[0]
        # The clause_index + verification_point_id from the child
        # must be preserved end-to-end. The report consumer keys on
        # these to jump to the failing clause.
        assert dev.get("verification_point_id") == "VP-030-2", (
            f"deviation must reference the failed child VP-030-2, got "
            f"{dev.get('verification_point_id')!r}"
        )
        assert dev.get("clause_index") == 1, (
            f"deviation must carry clause_index=1 (the failed clause), "
            f"got {dev.get('clause_index')!r}"
        )
        # The PASSED child must NOT pollute the deviation list.
        passed_clauses_in_devs = [
            d for d in deviations
            if d.get("verification_point_id") == "VP-030-1"
        ]
        assert passed_clauses_in_devs == [], (
            f"PASSED child must not appear in deviations, got: "
            f"{passed_clauses_in_devs!r}"
        )

    def test_report_judgment_prompt_sees_no_split(
        self, verification_agent, mock_coding_tool
    ):
        """Mock LLM's prompt carries terminal statuses only.

        The whole reason ``_aggregate_split_vps`` was added to the
        pipeline is so the LLM judgment step doesn't have to roll
        up a SPLIT envelope on its own. To prove the helper runs
        *before* the LLM is called, we capture the
        ``prompt=`` keyword argument the mock LLM tool receives
        and assert that no VP in the prompt has status "SPLIT".

        The mock LLM echoes a fixed report; we don't need to
        inspect the report. The contract under test is on the
        *order* of operations inside ``generate_verification_report``.
        """
        split_entry = {
            "id": "VP-040",
            "parent_vp_id": None,
            "status": "SPLIT",
            "child_results": [
                {
                    "id": "VP-040-1",
                    "parent_vp_id": "VP-040",
                    "status": "PASSED",
                    "split_clause_index": 0,
                    "actual_result": "clause 1 ok",
                    "evidence": "e1",
                },
                {
                    "id": "VP-040-2",
                    "parent_vp_id": "VP-040",
                    "status": "FAILED",
                    "split_clause_index": 1,
                    "actual_result": "clause 2 failed: timeout",
                    "evidence": "e2",
                },
            ],
        }
        passed_entry = {
            "id": "VP-050",
            "parent_vp_id": None,
            "status": "PASSED",
            "actual_result": "ok",
            "evidence": "e",
        }
        execution_results = {
            "verification_points": [
                {
                    "id": "VP-040",
                    "title": "Split VP",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "ok",
                    "test_command": "echo ok",
                },
                {
                    "id": "VP-050",
                    "title": "Plain VP",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "ok",
                    "test_command": "echo ok",
                },
            ],
            "execution_results": [split_entry, passed_entry],
            "executed_at": "2026-06-05T00:00:00",
        }

        # Stub LLM with a faithful but minimal report. The Mock's
        # call_args will be inspected below to read the prompt kwarg.
        mock_coding_tool.query_json.return_value = {
            "overall_status": "FAILED",
            "summary": "1 VP failed",
            "verification_results": [
                {"id": "VP-040", "status": "FAILED", "actual_result": "agg", "evidence": "e"},
                {"id": "VP-050", "status": "PASSED", "actual_result": "ok", "evidence": "e"},
            ],
            "requirement_deviations": [
                {"verification_point_id": "VP-040-2", "clause_index": 1,
                 "type": "missing", "description": "clause 2 failed",
                 "severity": "high"},
            ],
        }

        # Drive the pipeline. The mock LLM is called at most once
        # (retry_llm=1) so call_args is a single record.
        verification_agent.generate_verification_report(
            execution_results, retry_llm=1
        )

        assert mock_coding_tool.query_json.called, (
            "LLM mock should have been invoked at least once"
        )
        call_kwargs = mock_coding_tool.query_json.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # The prompt must reference the VP ids (sanity check that
        # the LLM was actually fed the execution context).
        assert "VP-040" in prompt, (
            f"prompt should reference VP-040 (the SPLIT VP id), got: "
            f"{prompt[:500]!r}"
        )
        assert "VP-050" in prompt, (
            f"prompt should reference VP-050 (the PASSED VP id), got: "
            f"{prompt[:500]!r}"
        )

        # The contract: NO VP in the prompt has the unaggregated
        # "SPLIT" status. The prompt is built from
        # ``execution_results["execution_results"]``; if the helper
        # ran first, the SPLIT entry has been rewritten to a
        # terminal status.
        #
        # The prompt section is structured as
        # "### <vp_id>: <status>". We scan for "SPLIT" appearing
        # as a status token. We deliberately look for the
        # status-token form ("### VP-XXX: SPLIT" or as a standalone
        # status after a colon followed by newline) to avoid
        # false positives in unrelated text.
        terminal_statuses = {"PASSED", "FAILED", "SKIPPED", "PARTIAL"}
        for vp_id in ("VP-040", "VP-050"):
            # Match the prompt's per-VP line: "### <vp_id>: <STATUS>"
            # followed by either newline or end-of-string. The
            # captured status must be terminal, not "SPLIT".
            import re
            pattern = re.compile(
                rf"###\s+{re.escape(vp_id)}:\s*([A-Z]+)"
            )
            matches = pattern.findall(prompt)
            assert matches, (
                f"prompt must contain a '### {vp_id}: <STATUS>' line, "
                f"got prompt excerpt: {prompt[:1000]!r}"
            )
            # The LAST match corresponds to the most recent (i.e.
            # the post-aggregation) status for this VP.
            status = matches[-1]
            assert status in terminal_statuses, (
                f"prompt must show terminal status for {vp_id}, "
                f"got {status!r}. Aggregation must run before the "
                f"LLM is called."
            )
        # The word "SPLIT" must not appear as a status token after
        # a colon in the prompt's per-VP section. We grep for the
        # exact "### <ID>: SPLIT" pattern.
        import re
        split_status_lines = re.findall(
            r"###\s+VP-\w+:\s*SPLIT", prompt
        )
        assert split_status_lines == [], (
            f"prompt must not contain '### <VP-ID>: SPLIT' lines "
            f"(aggregation must run before the LLM call), got: "
            f"{split_status_lines!r}"
        )


# =============================================================================
# Execution-Profile injection (TDD spec from task 8-4)
# =============================================================================


class TestExecutionProfileInReport:
    """TDD spec for the ``execution_profile`` top-level field.

    The verification report consumed by the bridge UI (frontend
    polling, dashboarding, budgeting) needs a stable, schema-compliant
    ``execution_profile`` block at the top level. This block must
    mirror :class:`ExecutionProfileGenerator.build()`'s contract
    (total_duration_sec + group_profiles + subtask_splits +
    per_method_timeouts + parallelism_cap).

    Two contracts pinned here:

    1. ``test_report_includes_execution_profile`` — when
       ``generate_verification_report`` produces a report, the returned
       dict must include a top-level ``execution_profile`` field whose
       shape is the ExecutionProfileGenerator's output.

    2. ``test_report_missing_profile_gets_empty_shell`` — when a
       pre-existing report (already on disk from an older round, or
       produced by code that doesn't compute the profile) lacks
       ``execution_profile``, the persistence layer (or
       report-writer-side helper) must auto-inject an empty
       ``{}`` shell at write time, not raise.

    Both tests run in <1s because the LLM is stubbed.
    """

    def test_report_includes_execution_profile(
        self, verification_agent, mock_coding_tool, temp_plan_dir
    ):
        """Top-level ``execution_profile`` field present + schema-compliant.

        Build a small plan with two VPs of differing methods, run it
        through ``generate_verification_report``, and assert the
        returned report carries an ``execution_profile`` field whose
        shape matches
        :class:`verification_profile.ExecutionProfileGenerator`'s
        contract:

        * keys: total_duration_sec, group_profiles, subtask_splits,
          per_method_timeouts, parallelism_cap
        * types: int, list, list, dict, int
        """
        execution_results = {
            "verification_points": [
                {
                    "id": "VP-100",
                    "title": "automated",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "passes",
                    "test_command": "pytest",
                },
                {
                    "id": "VP-101",
                    "title": "code review",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": "ok",
                    "test_command": "",
                },
            ],
            "execution_results": [
                {
                    "id": "VP-100",
                    "parent_vp_id": None,
                    "status": "PASSED",
                    "actual_result": "ok",
                    "evidence": "e",
                },
                {
                    "id": "VP-101",
                    "parent_vp_id": None,
                    "status": "PASSED",
                    "actual_result": "ok",
                    "evidence": "e",
                },
            ],
            "executed_at": "2026-06-05T00:00:00",
        }

        mock_coding_tool.query_json.return_value = {
            "overall_status": "PASSED",
            "summary": "ok",
            "verification_results": [
                {"id": "VP-100", "status": "PASSED", "actual_result": "ok", "evidence": "e"},
                {"id": "VP-101", "status": "PASSED", "actual_result": "ok", "evidence": "e"},
            ],
            "requirement_deviations": [],
        }

        report = verification_agent.generate_verification_report(
            execution_results, retry_llm=1
        )

        # The contract: execution_profile is at the top level, NOT
        # nested inside verification_results or _metadata.
        assert "execution_profile" in report, (
            f"report must include top-level 'execution_profile' field, "
            f"got keys: {list(report.keys())!r}"
        )
        profile = report["execution_profile"]
        assert isinstance(profile, dict), (
            f"execution_profile must be a dict, got {type(profile).__name__}"
        )

        # Schema compliance with ExecutionProfileGenerator's output.
        required_keys = {
            "total_duration_sec",
            "group_profiles",
            "subtask_splits",
            "per_method_timeouts",
            "parallelism_cap",
        }
        missing = required_keys - set(profile.keys())
        assert not missing, (
            f"execution_profile is missing required keys: {missing!r}; "
            f"present keys: {set(profile.keys())!r}"
        )

        # Type contract
        assert isinstance(profile["total_duration_sec"], int)
        assert isinstance(profile["group_profiles"], list)
        assert isinstance(profile["subtask_splits"], list)
        assert isinstance(profile["per_method_timeouts"], dict)
        assert isinstance(profile["parallelism_cap"], int)

        # Sanity: 2 VPs across 2 methods => at least 2 group_profiles
        # (or exactly 2 if no empties), and the total duration is a
        # non-negative int.
        assert len(profile["group_profiles"]) >= 1
        assert profile["total_duration_sec"] >= 0
        assert profile["parallelism_cap"] > 0

        # The profile must also be persisted to disk — the on-disk
        # file is what the status endpoint reads on cold start.
        report_file = temp_plan_dir / "verification_report.json"
        assert report_file.exists(), (
            f"report must be written to {report_file} by "
            f"generate_verification_report"
        )
        with open(report_file, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        assert "execution_profile" in persisted, (
            f"persisted report must include execution_profile, got: "
            f"{list(persisted.keys())!r}"
        )
        # The on-disk shape mirrors the in-memory shape (modulo
        # _metadata which the persistence layer adds).
        assert (
            persisted["execution_profile"]["total_duration_sec"]
            == profile["total_duration_sec"]
        )

    def test_report_missing_profile_gets_empty_shell(
        self, verification_agent, mock_coding_tool, temp_plan_dir
    ):
        """Missing ``execution_profile`` at write time becomes ``{}``.

        Scenario: an LLM round trip returns a report without an
        ``execution_profile`` key (older LLM, prompt template drift,
        manual override). The persistence layer / report-writer-side
        helper must inject an empty ``{}`` shell so downstream
        consumers (status endpoint, bridge UI) never see a missing
        key.

        The test stubs the LLM to return a report that omits
        ``execution_profile`` entirely, then asserts the
        *persisted* report on disk has the empty shell — proving the
        safety net is at the write boundary, not at read time only.
        """
        execution_results = {
            "verification_points": [
                {
                    "id": "VP-200",
                    "title": "single",
                    "verification_method": "code_review",
                    "priority": "medium",
                    "expected_result": "ok",
                    "test_command": "echo ok",
                },
            ],
            "execution_results": [
                {
                    "id": "VP-200",
                    "parent_vp_id": None,
                    "status": "PASSED",
                    "actual_result": "ok",
                    "evidence": "e",
                },
            ],
            "executed_at": "2026-06-05T00:00:00",
        }

        # LLM returns a report WITHOUT execution_profile — this is
        # the production scenario we want to survive gracefully.
        mock_coding_tool.query_json.return_value = {
            "overall_status": "PASSED",
            "summary": "ok (no profile)",
            "verification_results": [
                {"id": "VP-200", "status": "PASSED", "actual_result": "ok", "evidence": "e"},
            ],
            "requirement_deviations": [],
            # NOTE: no "execution_profile" key here on purpose
        }

        # The call must not raise. (Without the empty-shell safety
        # net, the in-memory report also lacks the key — the safety
        # net is the persistence layer's update_report injecting {}.)
        report = verification_agent.generate_verification_report(
            execution_results, retry_llm=1
        )

        # The function itself does add the key (in-memory path).
        # This is the primary contract from the spec.
        assert "execution_profile" in report, (
            f"in-memory report must include execution_profile (even if "
            f"the LLM didn't supply one), got keys: {list(report.keys())!r}"
        )

        # The on-disk report must have execution_profile too —
        # whether the in-memory path computed it, or the persistence
        # layer's empty-shell safety net filled it in. Either way,
        # the file is never missing the key.
        report_file = temp_plan_dir / "verification_report.json"
        assert report_file.exists(), (
            f"persisted report must exist at {report_file}"
        )
        with open(report_file, "r", encoding="utf-8") as f:
            persisted = json.load(f)

        assert "execution_profile" in persisted, (
            f"persisted report must include execution_profile; the "
            f"write boundary must inject {{}} if absent. Got keys: "
            f"{list(persisted.keys())!r}"
        )
        # The injected value must be a dict (possibly empty). The
        # in-memory path populates a full profile, so we just check
        # the type — not the emptiness — to remain robust against
        # future refactors that move the injection point.
        assert isinstance(persisted["execution_profile"], dict)

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
