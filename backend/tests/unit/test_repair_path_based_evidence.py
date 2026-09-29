"""Unit tests for path-based verification_report_reader + RepairTaskAssembler fallback.

Covers the 2026-09-10 P2 fix:

  1. ``extract_failed_vps_with_paths`` returns paths (not inlined text)
     so the LLM can ``Read`` evidence on demand instead of being
     truncated to a 1000-char summary.
  2. ``RepairTaskAssembler.assemble`` no longer drops half-formed
     content (empty title / description). Instead it falls back to a
     minimal but actionable task anchored on the evidence paths.

These tests do NOT touch the network / database — they are pure
local-disk unit tests.
"""
import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# extract_failed_vps_with_paths
# ---------------------------------------------------------------------------


def _write_report(plan_dir: Path, verification_results: list) -> Path:
    report_path = plan_dir / "verification_report.json"
    report_path.write_text(
        json.dumps(
            {"verification_results": verification_results},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return report_path


def _write_plan(plan_dir: Path, verification_points: list) -> Path:
    plan_path = plan_dir / "verification_plan.json"
    plan_path.write_text(
        json.dumps(
            {"verification_points": verification_points},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_path


def test_extract_failed_vps_with_paths_returns_paths_not_text(tmp_path):
    from verification.verification_report_reader import (
        extract_failed_vps_with_paths,
    )

    plan_dir = tmp_path
    long_actual = "x" * 5000  # 5K chars of fake pytest output
    long_evidence = "y" * 5000
    _write_report(
        plan_dir,
        [
            {
                "id": "VP-034",
                "title": "VP-034 title",
                "status": "FAILED",
                "actual_result": long_actual,
                "evidence": long_evidence,
            }
        ],
    )

    failed = extract_failed_vps_with_paths(
        plan_dir / "verification_report.json",
        plan_path=None,
    )

    assert len(failed) == 1
    entry = failed[0]
    assert entry["id"] == "VP-034"
    assert entry["title"] == "VP-034 title"
    # The new API does NOT inline 5K of text:
    assert "actual_result" not in entry
    assert "evidence" not in entry
    # Instead it returns a short summary:
    assert "actual_result_summary" in entry
    assert len(entry["actual_result_summary"]) <= 200
    # And a list of paths the LLM can Read:
    assert "evidence_paths" in entry
    assert len(entry["evidence_paths"]) >= 2
    for p in entry["evidence_paths"]:
        assert p.startswith(str(plan_dir.resolve()))


def test_extract_failed_vps_with_paths_surfaces_logs(tmp_path):
    from verification.verification_report_reader import (
        extract_failed_vps_with_paths,
    )

    plan_dir = tmp_path
    (plan_dir / "logs").mkdir()
    (plan_dir / "logs" / "verify_20260910_120000.log").write_text(
        "fake log content", encoding="utf-8"
    )
    _write_report(
        plan_dir,
        [
            {
                "id": "VP-001",
                "status": "FAILED",
                "actual_result": "short",
                "evidence": "short",
            }
        ],
    )

    failed = extract_failed_vps_with_paths(
        plan_dir / "verification_report.json"
    )

    log_paths = [p for p in failed[0]["evidence_paths"] if "logs" in p]
    assert len(log_paths) == 1
    assert log_paths[0].endswith("verify_20260910_120000.log")


def test_extract_failed_vps_with_paths_overlay_plan(tmp_path):
    from verification.verification_report_reader import (
        extract_failed_vps_with_paths,
    )

    plan_dir = tmp_path
    _write_report(
        plan_dir,
        [
            {
                "id": "VP-001",
                "status": "FAILED",
                "actual_result": "fail",
                "evidence": "ev",
            }
        ],
    )
    _write_plan(
        plan_dir,
        [
            {
                "id": "VP-001",
                "title": "Plan title for VP-001",
                "priority": "high",
                "test_command": "pytest tests/test_x.py -v",
                "expected_result": "all green",
            }
        ],
    )

    failed = extract_failed_vps_with_paths(
        plan_dir / "verification_report.json",
        plan_path=plan_dir / "verification_plan.json",
    )

    entry = failed[0]
    assert entry["priority"] == "high"
    assert "test_command_path" in entry
    assert entry["test_command_summary"] == "pytest tests/test_x.py -v"
    assert entry["expected_result_summary"] == "all green"


def test_extract_failed_vps_with_paths_skips_passed_vps(tmp_path):
    from verification.verification_report_reader import (
        extract_failed_vps_with_paths,
    )

    plan_dir = tmp_path
    _write_report(
        plan_dir,
        [
            {"id": "VP-001", "status": "PASSED", "actual_result": "ok"},
            {"id": "VP-002", "status": "FAILED", "actual_result": "bad"},
            {"id": "VP-003", "status": "SKIPPED"},
        ],
    )

    failed = extract_failed_vps_with_paths(
        plan_dir / "verification_report.json"
    )

    ids = [e["id"] for e in failed]
    assert ids == ["VP-002"]


def test_extract_failed_vps_with_paths_missing_file(tmp_path):
    from verification.verification_report_reader import (
        extract_failed_vps_with_paths,
    )

    failed = extract_failed_vps_with_paths(
        tmp_path / "does_not_exist.json"
    )
    assert failed == []


# ---------------------------------------------------------------------------
# RepairTaskAssembler fallback (no longer drops half-formed content)
# ---------------------------------------------------------------------------


def _stub_assembler(round_number, failed_vps, vp_test_commands=None,
                    plan_path=None):
    """Build a RepairTaskAssembler without invoking __init__.

    Every attribute ``assemble()`` dereferences must be set here — the
    helper bypasses ``__init__``, so a new instance attribute added
    there is invisible to these tests until it is mirrored below
    (2026-09-14: ``vp_test_commands`` / ``plan_path``;
    2026-09-20: ``seq_base``).
    """
    from repair_generator import RepairTaskAssembler

    asm = RepairTaskAssembler.__new__(RepairTaskAssembler)
    asm.round = round_number
    asm.failed_vps = {str(vp.get("id", "")): vp for vp in failed_vps if vp.get("id")}
    asm.vp_test_commands = dict(vp_test_commands or {})
    asm.plan_path = str(plan_path or "")
    # ``1`` = this batch is the first for its round, the default
    # ``__init__`` would have set. The shifted-base behaviour is covered
    # in ``test_repair_task_id_uniqueness.py``.
    asm.seq_base = 1
    return asm


def test_assembler_falls_back_when_title_empty():
    asm = _stub_assembler(1, [
        {
            "id": "VP-001",
            "title": "VP-001",
            "priority": "high",
            "actual_result_summary": "245 failed, 2935 passed",
            "evidence_paths": ["/abs/path/verification_report.json#/verification_results/0/actual_result"],
        }
    ])

    # Half-formed content: title missing, description missing.
    contents = [{"failed_vp_id": "VP-001", "title": "", "description": ""}]
    tasks = asm.assemble(contents)

    assert len(tasks) == 1
    t = tasks[0]
    # Not dropped.
    assert t["failed_vp_id"] == "VP-001"
    # Fallback title used.
    assert "VP-001" in t["title"]
    # Fallback description references the evidence paths.
    assert "245 failed" in t["description"] or "证据摘要" in t["description"]
    assert "/abs/path/verification_report.json" in t["description"]


def test_assembler_drops_hallucinated_vp_id():
    asm = _stub_assembler(1, [
        {
            "id": "VP-001",
            "title": "VP-001",
            "priority": "high",
            "actual_result_summary": "x",
            "evidence_paths": [],
        }
    ])
    # LLM invented a VP-999 that was never failed.
    contents = [
        {"failed_vp_id": "VP-001", "title": "ok", "description": "ok"},
        {"failed_vp_id": "VP-999", "title": "ghost", "description": "ghost"},
    ]
    tasks = asm.assemble(contents)
    ids = [t["failed_vp_id"] for t in tasks]
    assert "VP-999" not in ids
    assert "VP-001" in ids


def test_assembler_preserves_explicit_title_and_description():
    asm = _stub_assembler(1, [
        {
            "id": "VP-001",
            "title": "VP-001",
            "priority": "high",
            "actual_result_summary": "x",
            "evidence_paths": ["/p"],
        }
    ])
    contents = [
        {
            "failed_vp_id": "VP-001",
            "title": "Explicit title",
            "description": "Explicit description",
            "priority_hint": "low",
        }
    ]
    tasks = asm.assemble(contents)
    assert tasks[0]["title"] == "Explicit title"
    assert tasks[0]["description"] == "Explicit description"
    assert tasks[0]["priority"] == "low"


# ---------------------------------------------------------------------------
# _build_repair_contents_prompt: both shapes render correctly
# ---------------------------------------------------------------------------


def test_build_repair_contents_prompt_path_based(tmp_path):
    from repair_generator import _build_repair_contents_prompt

    failed_vps = [
        {
            "id": "VP-034",
            "title": "VP-034 title",
            "priority": "high",
            "actual_result_summary": "245 failed",
            "evidence_paths": [
                "/abs/path/verification_report.json#/verification_results/0/actual_result",
                "/abs/path/logs/run.log",
            ],
            "test_command_path": "/abs/path/verification_plan.json#/VP-034/test_command",
            "test_command_summary": "pytest backend/tests/ -q",
        }
    ]
    prompt = _build_repair_contents_prompt(
        failed_vps, 2, tmp_path, tmp_path
    )
    # Path-based markers should appear:
    assert "evidence_paths" in prompt
    assert "evidence_paths (用 Read 工具读取)" in prompt
    assert "/abs/path/verification_report.json" in prompt
    assert "/abs/path/logs/run.log" in prompt
    # Legacy inlined fields should NOT appear:
    assert "evidence: " not in prompt or "evidence_paths" in prompt
    # Round number included:
    assert "Round 2" in prompt


def test_build_repair_contents_prompt_legacy_still_works(tmp_path):
    """Legacy inlined-evidence shape still renders (backwards compat)."""
    from repair_generator import _build_repair_contents_prompt

    failed_vps = [
        {
            "id": "VP-034",
            "title": "VP-034",
            "priority": "medium",
            "actual_result": "inline actual_result text",
            "evidence": "inline evidence text",
            "test_command": "pytest tests/",
            "expected_result": "all green",
        }
    ]
    prompt = _build_repair_contents_prompt(
        failed_vps, 1, tmp_path, tmp_path
    )
    assert "inline actual_result text" in prompt
    assert "inline evidence text" in prompt
    assert "pytest tests/" in prompt
    # Path-based markers absent:
    assert "evidence_paths (用 Read 工具读取)" not in prompt


# ---------------------------------------------------------------------------
# extract_vp_test_commands (2026-09-14)
#
# The path-based reader above redacts a VP's command into a bounded
# ``test_command_summary`` + a JSON-pointer ``test_command_path`` — the
# right shape for a prompt, but NOT runnable. The repair round needs the
# raw string, so ``extract_vp_test_commands`` reads it straight out of
# the plan. These tests pin the raw-vs-summary distinction and the
# never-raise contract.
# ---------------------------------------------------------------------------


def _write_plan(tmp_path, points):
    plan = tmp_path / "verification_plan.json"
    plan.write_text(
        json.dumps({"verification_points": points}, ensure_ascii=False),
        encoding="utf-8",
    )
    return plan


def test_extract_vp_test_commands_returns_the_raw_command(tmp_path):
    from verification.verification_report_reader import (
        extract_failed_vps_with_paths,
        extract_vp_test_commands,
    )

    long_command = "pytest " + " ".join(f"--opt{i}=value{i}" for i in range(40))
    plan = _write_plan(tmp_path, [
        {"id": "VP-1", "title": "t", "priority": "high",
         "test_command": long_command},
    ])
    report = tmp_path / "verification_report.json"
    report.write_text(json.dumps({
        "verification_results": [
            {"id": "VP-1", "status": "FAILED", "actual_result": "a",
             "evidence": "e"},
        ],
    }), encoding="utf-8")

    commands = extract_vp_test_commands(plan)
    assert commands == {"VP-1": long_command}

    # ...whereas the path-based reader only carries a truncated summary.
    failed = extract_failed_vps_with_paths(report, plan)
    assert failed[0]["test_command_summary"] != long_command
    assert "test_command" not in failed[0]


def test_extract_vp_test_commands_skips_blank_and_non_string(tmp_path):
    from verification.verification_report_reader import extract_vp_test_commands

    plan = _write_plan(tmp_path, [
        {"id": "VP-1", "test_command": "   "},
        {"id": "VP-2", "test_command": None},
        {"id": "VP-3", "test_command": ["list", "not", "str"]},
        {"id": "VP-4", "test_command": "pytest -q"},
        {"test_command": "orphan, no id"},
    ])
    assert extract_vp_test_commands(plan) == {"VP-4": "pytest -q"}


def test_extract_vp_test_commands_strips_surrounding_whitespace(tmp_path):
    from verification.verification_report_reader import extract_vp_test_commands

    plan = _write_plan(tmp_path, [
        {"id": "VP-1", "test_command": "\n  pytest -q  \n"},
    ])
    assert extract_vp_test_commands(plan) == {"VP-1": "pytest -q"}


def test_extract_vp_test_commands_is_total_on_bad_input(tmp_path):
    from verification.verification_report_reader import extract_vp_test_commands

    assert extract_vp_test_commands(tmp_path / "missing.json") == {}
    assert extract_vp_test_commands(None) == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert extract_vp_test_commands(broken) == {}

    not_a_dict = tmp_path / "list.json"
    not_a_dict.write_text("[1, 2, 3]", encoding="utf-8")
    assert extract_vp_test_commands(not_a_dict) == {}

    no_points = tmp_path / "empty.json"
    no_points.write_text("{}", encoding="utf-8")
    assert extract_vp_test_commands(no_points) == {}
