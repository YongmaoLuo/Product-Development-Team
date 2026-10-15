"""Regression tests for the BLOCKED verdict handling fix (2026-09-07 plan).

Before the fix:
  - binary-freshness pre-check returned BLOCKED with the freshness detail
    in ``reasons``
  - ``verification_executor._validate_verdict`` rejected BLOCKED (not in
    ALLOWED_STATUSES) and replaced it with a generic
    ``verdict schema rejected: status 'BLOCKED' not in ALLOWED_STATUSES...``
    message
  - ``verification_report.json`` therefore lost the original binary-stale
    detail and every round reported the same generic message
  - the deterministic repair-task generator had no pattern for binary
    staleness, so it emitted 0 repair tasks and the orchestrator hit
    ``no_repair_tasks`` and terminated the round

After the fix:
  - ``ALLOWED_STATUSES`` includes BLOCKED so the schema validator no
    longer overwrites the verdict
  - the BLOCKED verdict carries an explicit ``actual_result`` so the
    freshness detail + rebuild command survive into the report
  - ``_build_execution_results_from_executor`` prefers verdict-level
    ``actual_result`` over a reasons-join
  - ``server.py:_build_verification_progress`` includes BLOCKED in its
    failure-detail filter so the Feishu card renders the binary-stale
    reason verbatim
"""
import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# 1) ALLOWED_STATUSES + _validate_verdict now accept BLOCKED
# ---------------------------------------------------------------------------

def test_blocked_status_is_in_allowed_statuses():
    from verification_executor import ALLOWED_STATUSES
    assert "BLOCKED" in ALLOWED_STATUSES, (
        "BLOCKED must be allowed so binary-stale VPs keep their original "
        "freshness detail instead of being overwritten by a schema-error "
        "message (see Blocker #1F audit, 2026-09-07)."
    )


def test_validate_verdict_accepts_blocked():
    """A BLOCKED verdict with reasons + evidence must pass schema validation."""
    from verification_executor import VerdictSchemaError, VerificationExecutor
    ex = VerificationExecutor.__new__(VerificationExecutor)
    verdict = {
        "status": "BLOCKED",
        "reasons": ["binary stale: rust binary mtime older than source"],
        "evidence": {"binary_freshness": {"kind": "rust", "is_stale": True}},
        "actual_result": "binary stale (rust): ...\nrebuild: cargo build --release",
    }
    ex._validate_verdict(verdict)  # must not raise


# ---------------------------------------------------------------------------
# 2) BLOCKED verdict carries actual_result with rebuild command
# ---------------------------------------------------------------------------

def test_blocked_verdict_has_actual_result_with_rebuild_command():
    """``_build_binary_freshness_blocked_verdict`` must embed the rebuild
    command so the report can carry it forward verbatim."""
    from verification_agent import VerificationAgent
    from binary_freshness import FreshnessReport

    fr = FreshnessReport(
        kind="rust",
        status="FAILED",
        detail="binary mtime older than source",
        binary_path="native_ext/target/release/libnative_ext.dylib",
        newest_source_mtime=1000.0,
        binary_mtime=500.0,
        rebuild_command="cd native_ext && cargo build --release",
    )
    # FreshnessReport is a dataclass — instantiate the agent via __new__
    agent = VerificationAgent.__new__(VerificationAgent)
    verdict = agent._build_binary_freshness_blocked_verdict("VP-001", fr)
    assert verdict["status"] == "BLOCKED"
    assert "actual_result" in verdict, (
        "BLOCKED verdict must carry actual_result so the report keeps "
        "the rebuild command instead of collapsing to a generic "
        "verdict-schema message."
    )
    assert "binary stale (rust)" in verdict["actual_result"]
    assert "cargo build --release" in verdict["actual_result"], (
        "actual_result must include the rebuild command so the repair-"
        "task generator can emit a concrete rebuild step."
    )


# ---------------------------------------------------------------------------
# 3) _build_execution_results_from_executor preserves verdict-level actual_result
# ---------------------------------------------------------------------------

def test_executor_results_preserves_explicit_actual_result():
    """When a verdict explicitly carries ``actual_result``, the executor
    summary must use it instead of joining ``reasons``. This is the path
    that prevents the BLOCKED→FAILED collapse from hiding the binary
    detail in the report JSON."""
    from verification_agent import VerificationAgent
    agent = VerificationAgent.__new__(VerificationAgent)
    agent.verif_repo = None  # bypass persistence branch

    class _StubExecutor:
        def collect_verdicts(self):
            return [{
                "vp_id": "VP-001",
                "status": "BLOCKED",
                "reasons": ["binary stale: rust binary mtime older than source"],
                "actual_result": (
                    "binary stale (rust): binary mtime older than source\n"
                    "rebuild: cargo build --release"
                ),
                "evidence": {"binary_freshness": {"kind": "rust"}},
            }]
        def get(self, key, default=None):
            # Mimics dict-like access for plan_data.get('verification_points', [])
            return default if key != "verification_points" else []
        def _plan_retired_vps(self):
            # Mimics the executor's retired-VP predicate. Nothing here was
            # retired by the plan, so the envelope keeps every VP.
            return {}

    result = agent._build_execution_results_from_executor({}, _StubExecutor())
    results = result["execution_results"] if isinstance(result, dict) else result
    assert len(results) == 1
    r = results[0]
    assert r["id"] == "VP-001"
    assert r["status"] == "BLOCKED"
    assert "cargo build --release" in r["actual_result"], (
        "explicit actual_result must survive into the report (not be "
        "overwritten by a reasons-join)"
    )


def test_executor_results_falls_back_to_reasons_join_when_no_actual_result():
    """Backwards-compat: when verdict has no actual_result, the legacy
    reasons-join behavior must still apply."""
    from verification_agent import VerificationAgent
    agent = VerificationAgent.__new__(VerificationAgent)
    agent.verif_repo = None  # bypass persistence branch

    class _StubExecutor:
        def collect_verdicts(self):
            return [{
                "vp_id": "VP-005",
                "status": "FAILED",
                "reasons": ["exit code 1", "first failing test: foo"],
                "evidence": {},
            }]
        def get(self, key, default=None):
            return default if key != "verification_points" else []
        def _plan_retired_vps(self):
            # Same stub contract as above: the plan retired nothing.
            return {}

    result = agent._build_execution_results_from_executor({}, _StubExecutor())
    results = result["execution_results"] if isinstance(result, dict) else result
    assert results[0]["actual_result"] == "exit code 1\nfirst failing test: foo"


