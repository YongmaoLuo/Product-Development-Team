"""Unit tests for :func:`backend.verification.verification_report_reader.extract_failed_vps_from_report`.

Why this module exists
----------------------
2026-09-07: the repair generator was refactored to read
``verification_report.json`` directly via this local-code reader,
replacing the three-step LLM chain that round-tripped the failure
back through PRD context. These tests pin the reader's contract:
it must work against the actual production schema (which uses
``verification_results`` — not ``verification_points`` — as the
per-VP key, confirmed against the real on-disk report).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.verification.verification_report_reader import (  # noqa: E402
    extract_failed_vps_from_report,
)


class TestExtractFailedVPsHappyPath(unittest.TestCase):
    """The reader returns one entry per FAILED VP, with plan-side
    priority / test_command / expected_result overlaid onto the
    report-side actual_result / evidence."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.plan_dir = Path(self.tmpdir.name)

    def _write_report(self, report: dict) -> Path:
        p = self.plan_dir / "verification_report.json"
        p.write_text(json.dumps(report), encoding="utf-8")
        return p

    def _write_plan(self, plan: dict) -> Path:
        p = self.plan_dir / "verification_plan.json"
        p.write_text(json.dumps(plan), encoding="utf-8")
        return p

    def test_returns_one_entry_per_failed_vp(self):
        report = {
            "verification_results": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "ok",
                 "evidence": "e"},
                {"id": "VP-002", "status": "FAILED",
                 "actual_result": "broken", "evidence": "traceback"},
                {"id": "VP-003", "status": "FAILED",
                 "actual_result": "timeout", "evidence": "60s elapsed"},
            ],
        }
        failed = extract_failed_vps_from_report(
            self._write_report(report),
            self._write_plan({"verification_points": []}),
        )
        self.assertEqual(len(failed), 2)
        self.assertEqual([f["id"] for f in failed], ["VP-002", "VP-003"])

    def test_actual_result_and_evidence_are_preserved(self):
        report = {
            "verification_results": [
                {"id": "VP-002", "status": "FAILED",
                 "actual_result": "pytest collected 0 items",
                 "evidence": "exit code 5"},
            ],
        }
        failed = extract_failed_vps_from_report(
            self._write_report(report), None,
        )
        self.assertEqual(failed[0]["actual_result"], "pytest collected 0 items")
        self.assertEqual(failed[0]["evidence"], "exit code 5")

    def test_priority_comes_from_plan(self):
        report = {
            "verification_results": [
                {"id": "VP-002", "status": "FAILED",
                 "actual_result": "x", "evidence": "y"},
            ],
        }
        plan = {
            "verification_points": [
                {"id": "VP-002", "priority": "high",
                 "title": "Full pytest baseline",
                 "test_command": "pytest tests/",
                 "expected_result": "all green"},
            ],
        }
        failed = extract_failed_vps_from_report(
            self._write_report(report), self._write_plan(plan),
        )
        self.assertEqual(failed[0]["priority"], "high")
        self.assertEqual(failed[0]["test_command"], "pytest tests/")
        self.assertEqual(failed[0]["expected_result"], "all green")

    def test_priority_defaults_to_medium_when_plan_missing(self):
        report = {
            "verification_results": [
                {"id": "VP-002", "status": "FAILED",
                 "actual_result": "x", "evidence": "y"},
            ],
        }
        failed = extract_failed_vps_from_report(
            self._write_report(report), None,
        )
        self.assertEqual(failed[0]["priority"], "medium")

    def test_plan_overlay_is_best_effort(self):
        """A corrupt plan file should not crash the reader."""
        report = {
            "verification_results": [
                {"id": "VP-002", "status": "FAILED",
                 "actual_result": "x", "evidence": "y"},
            ],
        }
        report_path = self._write_report(report)
        bad_plan = self.plan_dir / "verification_plan.json"
        bad_plan.write_text("{not valid json", encoding="utf-8")
        failed = extract_failed_vps_from_report(report_path, bad_plan)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["priority"], "medium")


class TestExtractFailedVPsSchemaContract(unittest.TestCase):
    """The schema invariant — ``verification_results``, NOT
    ``verification_points``. Cross-checked against the actual
    production report at
    plans/2026-09-04/verification_report.json
    on 2026-09-07."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.plan_dir = Path(self.tmpdir.name)

    def test_reads_verification_results_not_verification_points(self):
        report_path = self.plan_dir / "verification_report.json"
        # Production uses ``verification_results`` (top-level key
        # for per-VP entries). ``verification_points`` belongs to
        # ``verification_plan.json``, NOT to the report.
        report_path.write_text(json.dumps({
            "verification_results": [
                {"id": "VP-A", "status": "FAILED",
                 "actual_result": "x", "evidence": "y"},
            ],
            # verification_points should NOT be present on a report;
            # including it as a tripwire to catch fixture-side drift.
            "_deprecated_verification_points": [],
        }), encoding="utf-8")
        failed = extract_failed_vps_from_report(report_path, None)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["id"], "VP-A")

    def test_skipped_entries_are_not_returned(self):
        report_path = self.plan_dir / "verification_report.json"
        report_path.write_text(json.dumps({
            "verification_results": [
                {"id": "VP-A", "status": "FAILED", "actual_result": "x", "evidence": "y"},
                {"id": "VP-B", "status": "SKIPPED", "actual_result": "n/a", "evidence": ""},
                {"id": "VP-C", "status": "PASSED", "actual_result": "ok", "evidence": "e"},
                {"id": "VP-D", "status": "PARTIAL", "actual_result": "p", "evidence": "p"},
            ],
        }), encoding="utf-8")
        failed = extract_failed_vps_from_report(report_path, None)
        # Only FAILED entries come back.
        self.assertEqual([f["id"] for f in failed], ["VP-A"])


class TestExtractFailedVPsDefensive(unittest.TestCase):
    """All I/O is wrapped — missing / corrupt / empty files degrade
    to an empty list, never raise."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.plan_dir = Path(self.tmpdir.name)

    def test_missing_report_returns_empty(self):
        missing = self.plan_dir / "verification_report.json"
        failed = extract_failed_vps_from_report(missing, None)
        self.assertEqual(failed, [])

    def test_corrupt_report_returns_empty(self):
        bad = self.plan_dir / "verification_report.json"
        bad.write_text("not json", encoding="utf-8")
        failed = extract_failed_vps_from_report(bad, None)
        self.assertEqual(failed, [])

    def test_empty_results_returns_empty(self):
        empty = self.plan_dir / "verification_report.json"
        empty.write_text(json.dumps({"verification_results": []}), encoding="utf-8")
        failed = extract_failed_vps_from_report(empty, None)
        self.assertEqual(failed, [])

    def test_missing_results_key_returns_empty(self):
        # Old / wrong-shape reports that omit verification_results
        # entirely should not crash.
        empty = self.plan_dir / "verification_report.json"
        empty.write_text(json.dumps({"other_key": []}), encoding="utf-8")
        failed = extract_failed_vps_from_report(empty, None)
        self.assertEqual(failed, [])

    def test_skips_entries_without_id(self):
        """Entries with empty/missing id are dropped — without an id
        we cannot route them to a plan-side priority lookup, and the
        assembler cannot stamp a `failed_vp_id` field."""
        path = self.plan_dir / "verification_report.json"
        path.write_text(json.dumps({
            "verification_results": [
                {"status": "FAILED", "actual_result": "x", "evidence": "y"},
                {"id": "", "status": "FAILED", "actual_result": "x", "evidence": "y"},
                {"id": "VP-A", "status": "FAILED", "actual_result": "x", "evidence": "y"},
            ],
        }), encoding="utf-8")
        failed = extract_failed_vps_from_report(path, None)
        self.assertEqual([f["id"] for f in failed], ["VP-A"])

    def test_non_dict_entries_are_skipped(self):
        path = self.plan_dir / "verification_report.json"
        path.write_text(json.dumps({
            "verification_results": [
                "not a dict",  # type: ignore[list-item]
                42,  # type: ignore[list-item]
                {"id": "VP-A", "status": "FAILED",
                 "actual_result": "x", "evidence": "y"},
            ],
        }), encoding="utf-8")
        failed = extract_failed_vps_from_report(path, None)
        self.assertEqual([f["id"] for f in failed], ["VP-A"])


class TestExtractFailedVPsRealReport(unittest.TestCase):
    """Cross-check against the actual production report layout."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.plan_dir = Path(self.tmpdir.name)

    def test_real_shape_from_plan_20260101(self):
        """The 2026-09-07 production report has the shape produced
        by ``VerificationPersistenceManager.update_report``. The
        reader must return one entry for VP-034 (the only FAILED
        VP) with the correct actual_result / evidence."""
        report_path = self.plan_dir / "verification_report.json"
        report_path.write_text(json.dumps({
            "overall_status": "FAILED",
            "verification_results": [
                {"id": "VP-033", "status": "PASSED",
                 "actual_result": "ok", "evidence": "e"},
                {"id": "VP-034", "status": "FAILED",
                 "actual_result": "全量 pytest 基线验证（PRD 要求每 stream 完成后 1482 全绿）在 1800 秒内未完成",
                 "evidence": "tests 总数已扩至 3198 (远超原 1482)，单次 pytest collection+run 超时"},
                {"id": "VP-035", "status": "PASSED",
                 "actual_result": "ok", "evidence": "e"},
            ],
        }), encoding="utf-8")
        failed = extract_failed_vps_from_report(report_path, None)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["id"], "VP-034")
        self.assertIn("1800", failed[0]["actual_result"])


class TestExtractFailedVPsRoundSnapshotFallback(unittest.TestCase):
    """A round that snapshots its results before re-reading them:

    The previous reader only consulted ``verification_results`` and
    silently returned ``[]`` when ``snapshot_round_results`` +
    ``clear_round_results`` had emptied the top-level array at round
    start (the failures then live in ``rounds[-1].results``).

    Production trace:
      - Round 0 ends: verification_results holds every VP (some FAILED)
      - Round 1 starts: snapshot_round_results moves data into
                        ``rounds[0].results`` and clears the
                        top-level field
      - Round 1 ends: top-level empty (executor wrote to a different
                      store or the round aborted before write)
      - check_cycle_conditions: extract_failed_vps_from_report → []
      - auto-loop: _record_terminal("failed", "no_repair_tasks")
      - operator sees: stuck plan with no repair tasks despite 4
                       documented FAILED VPs

    These tests pin the fix: when the top-level field is empty the
    reader MUST fall back to ``rounds[-1].results`` so the failed set
    is still discoverable.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.plan_dir = Path(self.tmpdir.name)

    def test_falls_back_to_rounds_last_results_when_top_level_empty(self):
        """The production shape: top-level
        ``verification_results`` is empty, ``rounds[-1].results``
        holds the FAILED VPs. Reader must return all of them.
        """
        report_path = self.plan_dir / "verification_report.json"
        report_path.write_text(json.dumps({
            "overall_status": "FAILED",
            "verification_results": [],  # empty after snapshot+clear
            "rounds": [
                {
                    "round": 0,
                    "results": [
                        {"id": "VP-006", "status": "FAILED",
                         "actual_result": "select_entering_consolidation 函数体 diff 非空",
                         "evidence": "Per-line diff 全部以 + 开头"},
                        {"id": "VP-010", "status": "FAILED",
                         "actual_result": "filter 匹配到 0 个测试",
                         "evidence": "filter 'test_metric_original_interval_immutable' 不存在"},
                        {"id": "VP-023", "status": "FAILED",
                         "actual_result": "Nightly CI 120s 超时",
                         "evidence": "asyncio.TimeoutError"},
                        {"id": "VP-027", "status": "FAILED",
                         "actual_result": "Rust 覆盖率 84.48% < 90%",
                         "evidence": "cargo tarpaulin 输出"},
                    ],
                    "snapshot_at": "2026-09-12T01:55:08Z",
                },
            ],
        }), encoding="utf-8")
        failed = extract_failed_vps_from_report(report_path, None)
        ids = sorted(f["id"] for f in failed)
        self.assertEqual(ids, ["VP-006", "VP-010", "VP-023", "VP-027"])

    def test_top_level_wins_when_both_present(self):
        """When both top-level and rounds[-1].results are populated
        the reader must dedupe (top-level wins as the most recent
        verdict). This guards against the brief window between
        generate_verification_report writing top-level and
        clear_round_results running.
        """
        report_path = self.plan_dir / "verification_report.json"
        report_path.write_text(json.dumps({
            "overall_status": "FAILED",
            "verification_results": [
                {"id": "VP-006", "status": "FAILED",
                 "actual_result": "TOP-LEVEL verdict", "evidence": "e"},
            ],
            "rounds": [
                {
                    "round": 0,
                    "results": [
                        {"id": "VP-006", "status": "FAILED",
                         "actual_result": "ROUND-0 verdict", "evidence": "e"},
                    ],
                },
            ],
        }), encoding="utf-8")
        failed = extract_failed_vps_from_report(report_path, None)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["id"], "VP-006")
        # Top-level wins (most recent verdict)
        self.assertEqual(failed[0]["actual_result"], "TOP-LEVEL verdict")

    def test_dedupes_across_top_level_and_round_snapshot(self):
        """Same VP in both top-level and round snapshot must NOT be
        duplicated. Defensive case for the snapshot+clear race.
        """
        report_path = self.plan_dir / "verification_report.json"
        report_path.write_text(json.dumps({
            "overall_status": "FAILED",
            "verification_results": [{"id": "VP-001", "status": "FAILED",
                                      "actual_result": "a"}],
            "rounds": [{"round": 0, "results": [
                {"id": "VP-001", "status": "FAILED", "actual_result": "a"},
                {"id": "VP-002", "status": "FAILED", "actual_result": "b"},
            ]}],
        }), encoding="utf-8")
        failed = extract_failed_vps_from_report(report_path, None)
        ids = [f["id"] for f in failed]
        # VP-001 dedup'd, VP-002 picked up from round snapshot
        self.assertEqual(ids.count("VP-001"), 1)
        self.assertIn("VP-002", ids)

    def test_no_rounds_no_top_level_returns_empty(self):
        """Defensive: report has neither top-level nor rounds — return [].
        """
        report_path = self.plan_dir / "verification_report.json"
        report_path.write_text(json.dumps({
            "overall_status": "FAILED",
            "verification_results": [],
            "rounds": [],
        }), encoding="utf-8")
        failed = extract_failed_vps_from_report(report_path, None)
        self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()
