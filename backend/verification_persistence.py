"""
Verification Report Persistence Manager
========================================

Implements multi-file persistence strategy for verification reports:
1. Main report file (verification_report.json) — metadata, verification points, overall determination
2. Detailed log files (logs/verification_{round}_{timestamp}.log) — JSON-lines format
3. UI screenshot files (screenshots/verification_{round}_{checkpoint_id}.png)
4. Cleanup strategy — keep last 3 rounds, archive older rounds
"""

import json
import gzip
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any


class VerificationPersistenceManager:
    """
    Manages multi-file persistence for verification reports.

    Persistence strategy:
    - Main report: plans/{id}/verification_report.json (updated after each phase)
    - Logs: plans/{id}/logs/verification_{round}_{timestamp}.log (JSON-lines, per verification point)
    - Screenshots: plans/{id}/screenshots/verification_{round}_{checkpoint_id}.png
    - Cleanup: keep last 3 rounds, archive older rounds to .gz
    """

    def __init__(self, plan_dir: Path):
        """
        Initialize persistence manager.

        Args:
            plan_dir: Plan directory (e.g., plans/my-plan-id)
        """
        self.plan_dir = Path(plan_dir)
        self.logs_dir = self.plan_dir / "logs"
        self.screenshots_dir = self.plan_dir / "screenshots"
        self.report_file = self.plan_dir / "verification_report.json"

        # Create directories
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

        # Current round tracking
        self._current_round = 0
        self._log_file: Optional[Path] = None

    def start_round(self, round_number: int) -> Path:
        """
        Start a new verification round. Creates a new log file.

        Args:
            round_number: Current verification round number (1-indexed)

        Returns:
            Path to the new log file
        """
        self._current_round = round_number

        # Create log file with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"verification_{round_number}_{timestamp}.log"
        self._log_file = self.logs_dir / log_filename

        # Write round start marker
        self._write_log_entry({
            "event": "round_start",
            "round": round_number,
            "timestamp": datetime.now().isoformat()
        })

        # Cleanup old rounds (keep only last 3)
        self._cleanup_old_rounds()

        return self._log_file

    def write_verification_point_log(
        self,
        vp_id: str,
        event_type: str,
        data: Dict[str, Any]
    ):
        """
        Write a verification point log entry immediately.

        This is called after each verification point execution to prevent progress loss.

        Args:
            vp_id: Verification point ID (e.g., "VP-001")
            event_type: Event type (e.g., "pytest_output", "code_review", "llm_call")
            data: Event data to log
        """
        if not self._log_file:
            raise RuntimeError("No log file active. Call start_round() first.")

        entry = {
            "verification_point_id": vp_id,
            "event_type": event_type,
            "timestamp": datetime.now().isoformat(),
            "data": data
        }

        self._write_log_entry(entry)

        # State-change hook: every per-VP log entry that survives
        # represents a VP-level event the operator cares about
        # (vp_start / vp_complete / attempt_* / verdict_*). The
        # notifier rebuilds the verification card on receipt. We
        # deliberately publish on every event_type (not a
        # whitelist) because the fingerprint dedup suppresses the
        # noise — a stream of pytest_output lines collapses to
        # one card push per coalesce window.
        try:
            from notifications.state_events import (
                KIND_VP_STATE_CHANGED,
                publish_safe,
            )
            publish_safe(
                KIND_VP_STATE_CHANGED,
                self.plan_dir.name,
                sub_kind="vp_log",
                vp_id=vp_id,
                event_type=event_type,
            )
        except Exception:
            # Publish path is best-effort; never raise into the
            # verifier's hot loop. (publish_safe already swallows,
            # but the import itself can fail.)
            pass

    def write_screenshot_reference(
        self,
        vp_id: str,
        checkpoint_id: str,
        screenshot_path: str
    ):
        """
        Record a screenshot reference and return relative path for verification_report.json.

        Args:
            vp_id: Verification point ID
            checkpoint_id: Checkpoint ID for screenshot naming
            screenshot_path: Absolute path to screenshot file

        Returns:
            Relative path from plan_dir to screenshot (for embedding in report)
        """
        # Copy screenshot to screenshots directory with proper naming
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_filename = f"verification_{self._current_round}_{checkpoint_id}_{timestamp}.png"
        dest_path = self.screenshots_dir / screenshot_filename

        try:
            shutil.copy2(screenshot_path, dest_path)
        except Exception as e:
            # If copy fails, log the error but continue
            self.write_verification_point_log(
                vp_id,
                "screenshot_error",
                {"error": str(e), "source_path": screenshot_path}
            )
            return None

        # Write log entry
        self.write_verification_point_log(
            vp_id,
            "screenshot_saved",
            {
                "checkpoint_id": checkpoint_id,
                "relative_path": f"screenshots/{screenshot_filename}",
                "absolute_path": str(dest_path)
            }
        )

        # Return relative path for report embedding
        return f"screenshots/{screenshot_filename}"

    def update_report(self, report_data: Dict[str, Any]) -> None:
        """
        Update the main verification report file.

        Called after the entire verification phase completes.

        Args:
            report_data: Complete verification report data
        """
        # Defense-in-depth: an LLM round trip (or a manual override)
        # may return a report that omits ``execution_profile``. The
        # bridge UI / status endpoint keys on the field being present,
        # so inject an empty shell at the write boundary rather than
        # raising. ``generate_verification_report`` normally populates
        # this from ExecutionProfileGenerator; this is the safety net
        # for any code path that writes the report directly.
        if "execution_profile" not in report_data:
            report_data["execution_profile"] = {}

        # Add metadata about referenced files
        report_data["_metadata"] = {
            "current_round": self._current_round,
            "log_file": str(self._log_file.relative_to(self.plan_dir)) if self._log_file else None,
            "screenshots_dir": "screenshots",
            "logs_dir": "logs",
            "generated_at": datetime.now().isoformat()
        }

        # Collect all screenshot references from current round
        screenshots = self._collect_screenshot_references()
        if screenshots:
            report_data["_metadata"]["screenshots"] = screenshots

        # Write report
        with open(self.report_file, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)

    def load_report(self) -> Optional[Dict[str, Any]]:
        """
        Load the main verification report.

        Returns:
            Report data or None if not found
        """
        if not self.report_file.exists():
            return None

        with open(self.report_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_round_logs(self, round_number: int) -> List[Dict[str, Any]]:
        """
        Load all log entries for a specific round.

        Args:
            round_number: Round number to load

        Returns:
            List of log entries
        """
        log_files = sorted(self.logs_dir.glob(f"verification_{round_number}_*.log"))

        entries = []
        for log_file in log_files:
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                entry = json.loads(line)
                                entries.append(entry)
                            except json.JSONDecodeError:
                                pass
            except Exception:
                pass

        return entries

    def get_verification_point_logs(self, vp_id: str) -> List[Dict[str, Any]]:
        """
        Load all log entries for a specific verification point across all rounds.

        Args:
            vp_id: Verification point ID

        Returns:
            List of log entries for this verification point
        """
        all_entries = []
        log_files = sorted(self.logs_dir.glob("verification_*.log"))

        for log_file in log_files:
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                entry = json.loads(line)
                                if entry.get("verification_point_id") == vp_id:
                                    all_entries.append(entry)
                            except json.JSONDecodeError:
                                pass
            except Exception:
                pass

        return all_entries

    def _write_log_entry(self, entry: Dict[str, Any]) -> None:
        """Write a JSON-lines log entry."""
        if not self._log_file:
            return

        with open(self._log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _collect_screenshot_references(self) -> List[str]:
        """Collect all screenshot references from current round."""
        screenshots = []

        if not self._log_file:
            return screenshots

        try:
            with open(self._log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entry = json.loads(line)
                            if entry.get("event_type") == "screenshot_saved":
                                rel_path = entry.get("data", {}).get("relative_path")
                                if rel_path:
                                    screenshots.append(rel_path)
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass

        return screenshots

    def _cleanup_old_rounds(self) -> None:
        """
        Archive logs and screenshots from rounds older than the last 3.

        Keeps current round + 2 previous rounds. Older rounds are compressed to .gz.
        """
        current_round = self._current_round

        # Archive old log files
        log_files = sorted(self.logs_dir.glob("verification_*.log"))
        log_files_by_round = {}

        for log_file in log_files:
            # Extract round number from filename
            parts = log_file.stem.split("_")
            if len(parts) >= 2 and parts[0] == "verification":
                try:
                    round_num = int(parts[1])
                    if round_num not in log_files_by_round:
                        log_files_by_round[round_num] = []
                    log_files_by_round[round_num].append(log_file)
                except ValueError:
                    pass

        # Archive rounds older than current - 2
        for round_num, files in log_files_by_round.items():
            if round_num < current_round - 2:
                for file in files:
                    self._archive_file(file)

        # Archive old screenshot files similarly
        screenshot_files = sorted(self.screenshots_dir.glob("verification_*.png"))
        screenshots_by_round = {}

        for screenshot_file in screenshot_files:
            parts = screenshot_file.stem.split("_")
            if len(parts) >= 2 and parts[0] == "verification":
                try:
                    round_num = int(parts[1])
                    if round_num not in screenshots_by_round:
                        screenshots_by_round[round_num] = []
                    screenshots_by_round[round_num].append(screenshot_file)
                except ValueError:
                    pass

        for round_num, files in screenshots_by_round.items():
            if round_num < current_round - 2:
                for file in files:
                    self._archive_file(file)

    def _archive_file(self, file_path: Path) -> None:
        """
        Compress a file to .gz and remove the original.

        Args:
            file_path: File to archive
        """
        try:
            with open(file_path, "rb") as f_in:
                with gzip.open(f"{file_path}.gz", "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            file_path.unlink()
        except Exception:
            pass  # Silently fail if archiving fails

    def get_requirement_deviations_summary(self) -> List[Dict[str, Any]]:
        """
        Extract requirement deviations summary from verification report.

        This is used by RepairTaskGenerator to get requirement deviations.

        Returns:
            List of requirement deviations
        """
        report = self.load_report()
        if not report:
            return []

        return report.get("requirement_deviations", [])

    def get_verification_points(self) -> List[Dict[str, Any]]:
        """
        Get verification points from the latest report.

        Returns:
            List of verification points
        """
        report = self.load_report()
        if not report:
            return []

        return report.get("verification_results", [])

    def update_round1_stable_vps(self, stable_vps: List[str]) -> None:
        """Write ``round1_stable_vps`` to verification_report.json.

        DP7-3 schema extension: this field records the VP IDs that
        were judged ``stable`` (PASSED) on the first pass of the
        two-phase verification loop. Downstream code (e.g. the
        recheck phase) reads it back via ``load_round1_stable_vps``
        so it can skip exactly those VPs.

        The update is non-destructive — all other fields in the
        report are preserved. If the report file does not exist yet
        we create it with a minimal shell so the field can be
        persisted before the rest of the report is filled in.

        Parameters
        ----------
        stable_vps : list of str
            The list of VP IDs that were PASSED on the first pass.
        """
        report = self.load_report()
        if report is None:
            report = {}
        report["round1_stable_vps"] = list(stable_vps)
        # Defense-in-depth: an LLM round trip (or a manual override)
        # may return a report that omits ``execution_profile``. The
        # bridge UI / status endpoint keys on the field being
        # present, so inject an empty shell at the write boundary
        # rather than raising.
        if "execution_profile" not in report:
            report["execution_profile"] = {}
        # Refresh metadata so downstream readers see the latest
        # generated_at timestamp alongside the new field.
        report["_metadata"] = {
            "current_round": self._current_round,
            "log_file": str(self._log_file.relative_to(self.plan_dir)) if self._log_file else None,
            "screenshots_dir": "screenshots",
            "logs_dir": "logs",
            "generated_at": datetime.now().isoformat(),
        }
        with open(self.report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    def load_round1_stable_vps(self) -> List[str]:
        """Read ``round1_stable_vps`` from verification_report.json.

        Backward-compat contract: a report written before the DP7-3
        schema extension lacks the field. This method MUST return
        ``[]`` in that case rather than raising ``KeyError`` /
        ``AttributeError``, so callers can rely on the round-trip
        always yielding a list.

        Returns
        -------
        list of str
            The VP IDs that were PASSED on the first pass of the
            most-recent two-phase round. Empty list if the field
            is absent or the report does not exist.
        """
        report = self.load_report()
        if not report:
            return []
        value = report.get("round1_stable_vps")
        if not isinstance(value, list):
            return []
        # Filter to strings only — defensive against a malformed
        # report that accidentally stored ints / dicts.
        return [vp_id for vp_id in value if isinstance(vp_id, str)]
