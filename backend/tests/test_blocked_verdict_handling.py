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
  - ``repair_generator.collect_failure_evidence`` includes BLOCKED rows
  - ``repair_generator._build_deterministic_repair_tasks`` emits a
    concrete rebuild task when ``actual_result`` mentions binary staleness
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

    result = agent._build_execution_results_from_executor({}, _StubExecutor())
    results = result["execution_results"] if isinstance(result, dict) else result
    assert results[0]["actual_result"] == "exit code 1\nfirst failing test: foo"


# ---------------------------------------------------------------------------
# 4) _build_deterministic_repair_tasks emits binary-stale pattern
# ---------------------------------------------------------------------------

class FakeCodingTool:
    def query_json(self, prompt, system_instruction, timeout=1800):
        return {"tasks": []}


def _make_gen(plan_dir: Path, project_dir: Path):
    from repair_generator import RepairTaskGenerator

    class _G:
        pass

    g = _G()
    g.plan_dir = plan_dir
    g.project_dir = project_dir
    g.coding_tool = FakeCodingTool()
    g.persistence = None
    g._build_deterministic_repair_tasks = (
        RepairTaskGenerator._build_deterministic_repair_tasks.__get__(g)
    )
    g._get_next_repair_task_id = (
        RepairTaskGenerator._get_next_repair_task_id.__get__(g)
    )
    return g


def test_binary_stale_evidence_emits_rebuild_repair_task(tmp_path):
    plan_dir = tmp_path / "p"; plan_dir.mkdir()
    project_dir = tmp_path / "exec"; project_dir.mkdir()
    gen = _make_gen(plan_dir, project_dir)

    evidence = [{
        "vp_id": "VP-001",
        "actual_result": (
            "binary stale (rust): rust binary mtime older than source\n"
            "rebuild: cd native_ext && cargo build --release"
        ),
        "evidence": json.dumps({
            "binary_freshness": {
                "kind": "rust",
                "is_stale": True,
                "rebuild_command": "cd native_ext && cargo build --release",
            }
        }),
        "test_command": (
            "bash -c \"source venv1/bin/activate && curl -s 'http://127.0.0.1:8080/api/items/1min/EXAMPLE'\""
        ),
    }]

    tasks = gen._build_deterministic_repair_tasks(evidence, round_number=2)
    assert len(tasks) == 1
    t = tasks[0]
    assert "binary stale" in t["title"].lower() or "binary" in t["title"].lower()
    # The actual_result was the source of the rebuild command — it
    # must surface in either the description or test_command.
    desc_and_cmd = t["description"] + " " + t["test_command"]
    assert "cargo build" in desc_and_cmd, (
        "rebuild command must be extracted from the BLOCKED verdict and "
        "placed into either the description (human-readable) or the "
        "test_command (executable) — otherwise the operator gets no "
        "concrete hint on how to fix the BLOCKED state."
    )
    # Must NOT blame the test_command (the binary is fresh, the test
    # command is correct).
    assert "items" not in t["description"] or "binary" in t["description"].lower(), (
        "description must focus on the binary, not on the curl/items "
        "test_command (which is unrelated to the BLOCKED state)."
    )


def test_blocked_with_no_rebuild_command_still_emits_generic_rebuild_task(tmp_path):
    """Even when rebuild_command is missing from the verdict, the
    deterministic pattern must still emit a task (otherwise we fall
    back to the LLM-driven path which hallucinates)."""
    plan_dir = tmp_path / "p"; plan_dir.mkdir()
    project_dir = tmp_path / "exec"; project_dir.mkdir()
    gen = _make_gen(plan_dir, project_dir)

    evidence = [{
        "vp_id": "VP-006",
        "actual_result": "binary stale (python): python module mtime older than source",
        "evidence": "{}",
        "test_command": "",
    }]

    tasks = gen._build_deterministic_repair_tasks(evidence, round_number=1)
    assert len(tasks) == 1, (
        "binary-stale pattern must emit at least one deterministic task "
        "even without an explicit rebuild command, so the LLM-driven "
        "path is short-circuited."
    )


def test_unrelated_failure_does_not_match_binary_stale_pattern(tmp_path):
    """A real test failure (no binary staleness) must NOT match the
    binary-stale pattern, otherwise we'd recommend a rebuild when the
    real fix is in the code."""
    plan_dir = tmp_path / "p"; plan_dir.mkdir()
    project_dir = tmp_path / "exec"; project_dir.mkdir()
    gen = _make_gen(plan_dir, project_dir)

    evidence = [{
        "vp_id": "VP-005",
        "actual_result": "AssertionError: expected 1, got 2",
        "evidence": "traceback from test_foo.py",
        "test_command": "pytest backend/tests/test_foo.py",
    }]

    tasks = gen._build_deterministic_repair_tasks(evidence, round_number=1)
    assert tasks == [], (
        "real test failures must NOT trigger the binary-stale pattern."
    )


# ---------------------------------------------------------------------------
# 5) collect_failure_evidence now picks up BLOCKED rows
# ---------------------------------------------------------------------------

def test_collect_failure_evidence_picks_up_blocked_rows(tmp_path):
    """BLOCKED rows (e.g. binary-stale VPs) must surface in
    ``collect_failure_evidence`` so the LLM-driven repair generator and
    the orchestrator's same-failure-repeated check see them."""
    plan_dir = tmp_path / "p"; plan_dir.mkdir()
    project_dir = tmp_path / "exec"; project_dir.mkdir()

    (plan_dir / "verification_report.json").write_text(json.dumps({
        "verification_results": [
            {
                "id": "VP-001",
                "status": "BLOCKED",
                "actual_result": (
                    "binary stale (rust): rust binary mtime older than source\n"
                    "rebuild: cargo build --release"
                ),
            },
            {
                "id": "VP-005",
                "status": "PASSED",
                "actual_result": "",
            },
        ],
    }))

    from repair_generator import RepairTaskGenerator
    gen = RepairTaskGenerator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=FakeCodingTool(),
    )

    evidence = gen.collect_failure_evidence(_DummyContext())
    vp_ids = [e["vp_id"] for e in evidence]
    assert "VP-001" in vp_ids, (
        "BLOCKED rows must be collected as failure evidence so the "
        "orchestrator can emit a concrete rebuild repair task. "
        "Pre-fix: collect_failure_evidence only filtered status == "
        "'FAILED' so BLOCKED rows were silently dropped."
    )


class _DummyContext:
    """Stand-in for the requirement_context dict the real code expects."""
    pass