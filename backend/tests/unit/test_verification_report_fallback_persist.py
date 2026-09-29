"""Bug B regression (2026-09-14): the Phase-3 fallback report must be
persisted, or repair generation runs on the PREVIOUS round's evidence.

Chain of events in round 1:

1. ``snapshot_round_results`` + ``clear_round_results`` run at round
   start, moving the old round's ``verification_results`` into
   ``rounds[N-1]`` and emptying the top-level field.
2. Round 1 executes and 3 VPs fail (VP-021 / VP-034 / VP-036).
3. All three Phase-3 judgment attempts die (two bogus
   ``HardTimeoutError``s after the unparseable first reply), so
   ``generate_verification_report`` returns
   ``_generate_minimal_report(execution_results)``.
4. That function built the right dict and returned it **without writing
   anything**. ``verification_report.json`` therefore still described
   the 2026-09-08 round — 41 PASSED / 1 FAILED (VP-034).
5. ``extract_failed_vps_with_paths`` fed the stale list to
   ``RepairTaskGenerator``, so two real failures would have been
   dropped even if the repair LLM call had succeeded.

The tests below pin both halves of the contract: the fallback writes a
report whose failed-VP set matches the round that just ran, and the
reader the repair generator actually uses sees all of them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


class _FailingTool:
    """``coding_tool`` stand-in whose every LLM call blows up."""

    def __init__(self, exc: Exception | None = None):
        self.calls = 0
        self._exc = exc or RuntimeError("simulated provider outage")

    def query_json(self, **kwargs):
        self.calls += 1
        raise self._exc


def _stale_previous_round_report() -> dict:
    """The exact shape an earlier plan had on disk at repair time."""
    return {
        "overall_status": "FAILED",
        "summary": "42 个验证点中 41 个 PASSED，VP-034 FAILED",
        "verification_results": [],  # cleared at round start
        "requirement_deviations": [],
        "generated_at": "2026-09-08T16:44:36.659533",
        "plan_id": "stale-plan",
        "rounds": [
            {
                "round": 0,
                "results": [
                    {"id": "VP-034", "status": "FAILED", "title": "全量 pytest 基线"},
                ],
                "snapshot_at": "2026-09-08T14:05:37.651223Z",
            }
        ],
    }


def _current_round_execution_results() -> dict:
    """Round-1 verdicts: VP-021 / VP-034 / VP-036 failed."""
    return {
        "verification_points": [
            {"id": "VP-021", "title": "server.py 规模", "verification_method": "automated_test"},
            {"id": "VP-034", "title": "全量 pytest 基线", "verification_method": "automated_test"},
            {"id": "VP-036", "title": "冷启动性能", "verification_method": "automated_test"},
            {"id": "VP-001", "title": "健康检查", "verification_method": "automated_test"},
        ],
        "execution_results": [
            {"id": "VP-021", "status": "FAILED", "title": "server.py 规模"},
            {"id": "VP-034", "status": "FAILED", "title": "全量 pytest 基线"},
            {"id": "VP-036", "status": "FAILED", "title": "冷启动性能"},
            {"id": "VP-001", "status": "PASSED", "title": "健康检查"},
        ],
    }


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    plan = tmp_path / "20260101-fallback"
    plan.mkdir()
    (plan / "verification_report.json").write_text(
        json.dumps(_stale_previous_round_report()), encoding="utf-8"
    )
    return plan


def _agent(plan_dir: Path, tool):
    from verification_agent import VerificationAgent

    return VerificationAgent(
        plan_dir=plan_dir, project_dir=plan_dir, coding_tool=tool
    )


def test_fallback_report_is_persisted_with_current_round_verdicts(plan_dir):
    """All judgment attempts fail → the deterministic fallback still
    lands on disk, carrying THIS round's settled verdicts."""
    tool = _FailingTool()
    agent = _agent(plan_dir, tool)

    report = agent.generate_verification_report(
        execution_results=_current_round_execution_results(), retry_llm=2
    )

    assert tool.calls == 2, "both Phase-3 attempts should have failed"
    assert report["overall_status"] == "FAILED"

    on_disk = json.loads(
        (plan_dir / "verification_report.json").read_text(encoding="utf-8")
    )
    failed_on_disk = [
        r["id"]
        for r in on_disk.get("verification_results", [])
        if r.get("status") == "FAILED"
    ]
    assert failed_on_disk == ["VP-021", "VP-034", "VP-036"], (
        "the persisted report must describe the round that just ran; "
        f"got {failed_on_disk!r} — a stale file here is Bug B"
    )
    # The per-round audit trail owned by snapshot_round_results must
    # survive the fallback write.
    assert [entry.get("round") for entry in on_disk.get("rounds", [])] == [0]
    # generated_at must be refreshed (proves the file was rewritten,
    # not merely left untouched).
    assert on_disk["generated_at"] != "2026-09-08T16:44:36.659533"


def test_repair_generator_reader_sees_every_failed_vp_after_fallback(plan_dir):
    """The reader ``check_cycle_conditions`` feeds to
    ``generate_repair_contents`` must see all three failures — this is
    the actual downstream consequence of Bug B."""
    from verification.verification_report_reader import (
        extract_failed_vps_with_paths,
    )

    agent = _agent(plan_dir, _FailingTool())
    agent.generate_verification_report(
        execution_results=_current_round_execution_results(), retry_llm=1
    )

    failed = extract_failed_vps_with_paths(
        plan_dir / "verification_report.json",
        plan_dir / "verification_plan.json",  # absent: the reader tolerates it
    )
    assert {vp["id"] for vp in failed} == {"VP-021", "VP-034", "VP-036"}, (
        f"repair generation would be handed {sorted(vp['id'] for vp in failed)} "
        f"— the stale-report regression is back"
    )


def test_fallback_write_failure_does_not_raise(plan_dir, monkeypatch):
    """A disk error while persisting the fallback must degrade to a
    warning — the caller still gets a usable report dict."""
    agent = _agent(plan_dir, _FailingTool())

    def _boom(_report_data):
        raise OSError("disk full")

    monkeypatch.setattr(agent.persistence, "update_report", _boom)

    report = agent.generate_verification_report(
        execution_results=_current_round_execution_results(), retry_llm=1
    )
    assert report["overall_status"] == "FAILED"
    assert report["verification_results"], "fallback must still carry results"
