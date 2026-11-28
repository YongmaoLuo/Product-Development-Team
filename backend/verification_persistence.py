"""
Verification Report Persistence Manager
========================================

Implements multi-file persistence strategy for verification reports:
1. Main report file (verification_report.json) — metadata, verification points, overall determination
2. Derived Markdown view (verification-report.md) — the report, rendered
3. Detailed log files (logs/verification_{round}_{timestamp}.log) — JSON-lines format
4. UI screenshot files (screenshots/verification_{round}_{checkpoint_id}.png)
5. Cleanup strategy — keep last 3 rounds, archive older rounds

一份数据，两份格式
------------------
``verification_report.json`` 是验证结论的**唯一数据源**；
``verification-report.md`` 是它的**派生视图**（渲染器见
:mod:`verification_report_md`）。两份文件由**同一条**写路径产出 ——
:meth:`VerificationPersistenceManager.update_report` —— 因为两个写者就是两
个真相：JSON 说一件事、Markdown 说另一件事，而读者没有办法分辨哪一份是
当前的。

写入顺序与故障隔离
------------------
写路径按这个顺序走，每一步的失败都只影响它自己：

1. **归一化** —— 补齐报告的扩展字段（见 :data:`REPORT_EXTENSION_FIELDS`）。
2. **渲染** —— 从归一化后的报告渲染出 Markdown（纯函数，不碰磁盘）。
3. **落 JSON** —— 先写同目录临时文件，再用 ``os.replace`` 原子替换。
4. **落 Markdown** —— 同样先写临时文件再原子替换。

**为什么渲染排在写 JSON 之前**：渲染是纯计算，它抛异常说明这份报告本身有问
题；这时候 JSON **保持原样**（上一次写的内容原封不动），而不是写进去一份读
者看不懂、渲染器又拒绝渲染的报告。派生视图的失败不允许污染数据源。反过来，
**写** Markdown 失败（磁盘满、只读）不会让 JSON 写不进去 —— JSON 在第 3 步
已经落盘，第 4 步的失败只留下「没有 Markdown」这一个事实。

两份文件都不会出现半截：临时文件 + ``os.replace`` 保证最终路径上要么是旧内
容、要么是完整的新内容，不会是截断的中间态。
"""

import json
import gzip
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Sequence

from bounded_log import append_bounded_line, rotated_log_paths
from framework.clock import utcnow_iso
from plan_artifacts import TASKS_FILENAME
from verification_report_md import (
    _entry_id,
    cost_deferred_entries,
    is_test_mode_cost_skip,
    render as render_markdown,
)

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from config_paths import resolve_state_db_path
except ImportError:  # pragma: no cover
    from backend.config_paths import resolve_state_db_path  # type: ignore


#: The derived Markdown view's filename, next to ``verification_report.json``.
REPORT_MD_FILENAME = "verification-report.md"

#: The report fields this manager guarantees are present in the report.
#:
#: ``plan_item_id`` lives on the verification points (it says which
#: plan-document entry a point traces back to); the other six live on the
#: report itself.  They are listed together because they are one
#: contract: **the readers key on these names**, and a report that omits
#: one of them is a report a reader cannot read.
REPORT_EXTENSION_FIELDS = (
    "plan_item_id",
    "skipped_items",
    "confirmed_defects",
    "repair_records",
    "unresolved",
    "human_decisions",
    "final_verdict",
)

#: Where the report's verdict may live, by priority — the first one that
#: carries text wins.
_VERDICT_KEYS = ("final_verdict", "overall_status", "verdict", "status")

#: The verdict field to read when the verdict itself is a dict.
_VERDICT_TEXT_KEYS = ("verdict", "status", "result", "state", "decision", "value")

#: The fields a plan task may carry the verification-point id in.
_PLAN_TASK_VP_KEYS = ("vp_id", "failed_vp_id")

#: The report fields that are lists, and default to an empty list.
#:
#: ``cost_gate_skipped_test_mode`` is derived (see
#: :func:`_collect_test_mode_skips`), but it is listed here because the
#: readers key on it being present — a reader that has to ask "is this
#: field there?" for every report is a reader that will one day guess.
_REPORT_LIST_FIELDS = (
    "skipped_items",
    "confirmed_defects",
    "unresolved",
    "human_decisions",
    "cost_gate_skipped_test_mode",
)

#: The suffix a rotated copy of a log carries: ``<name>.1`` … ``<name>.N``.
#: The highest number holds the oldest lines.
_ROTATED_COPY_SUFFIX = re.compile(r"\.(\d+)$")


def _log_candidates(live: Path) -> List[Path]:
    """The files one log's entries live in, oldest first.

    A log's rotated copies come first, oldest copy first — the highest
    copy number holds the oldest lines — and the live file comes last,
    because it holds the newest.  That order is the order the lines
    were written in, so a reader that walks the candidates in order
    walks the entries in that order too.

    A candidate that is not there is left out: most plans never rotate
    their log, so most of the copies a reader looks for do not exist.
    One that disappears between this check and the read is still
    handled by the reader itself.
    """
    return [
        candidate
        for candidate in (*rotated_log_paths(live), live)
        if candidate.exists()
    ]


def _collect_test_mode_skips(report: Dict[str, Any]) -> List[str]:
    """The verification points the cost gate held back because the run is a test.

    The judgement is the Markdown view's judgement: ``cost_deferred_entries``
    finds the ``DEFERRED`` entries, ``is_test_mode_cost_skip`` picks the ones
    marked ``test``.  Both live in :mod:`verification_report_md` and are used
    here on purpose — two implementations of "which points did the test-mode
    cost gate skip" would drift, and the report would end up claiming a skip
    the Markdown does not show (or the other way round).

    The same verification point can show up in several fields of the report
    (the plan side writes ``cost``, the execution side writes ``reasons``),
    so the ids are de-duplicated — one point, one entry, however many records
    mention it.

    :param report: The report (the JSON one).
    :return: The verification-point ids, de-duplicated and sorted; empty when
        nothing was skipped.
    """
    vp_ids = set()
    for entry in cost_deferred_entries(report):
        if not is_test_mode_cost_skip(entry):
            continue
        vp_id = _entry_id(entry)
        if vp_id:
            vp_ids.add(vp_id)
    return sorted(vp_ids)


def _discard(path: Optional[Path]) -> None:
    """Delete a staging file, ignoring the fact that it may not exist."""
    if path is None:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass


def _write_text_file(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` — the one place staging files are written.

    Split out as a module-level function so the failure it can raise is
    injectable: the write path's "no half-written file" guarantee is
    only observable if the write can be made to fail.
    """
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _plan_task_id(task: Dict[str, Any]) -> str:
    """A plan task's id — ``task_id`` in the database, ``id`` in tasks.json."""
    for key in ("task_id", "id"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list, tuple)):
            return str(value).strip()
    return ""


def _plan_task_vp_id(task: Dict[str, Any]) -> str:
    """The verification point a plan task repairs."""
    for key in _PLAN_TASK_VP_KEYS:
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


#: The name of the durable audit file inside a plan's log directory.
#:
#: It sits next to the per-round logs and holds the events that must
#: not be lost: the ones that record a decision about the *plan* rather
#: than about one verification point (a refused criterion replacement,
#: a delta that could not be written). The per-round log is tied to a
#: round; this file is not — and its name deliberately does not start
#: with ``verification_``, so the per-round cleanup (``logs/`` globbing
#: ``verification_*``) never mistakes it for a round artifact.
AUDIT_LOG_FILENAME = "audit_events.jsonl"


def write_audit_event(
    plan_dir: Path,
    vp_id: str,
    event_type: str,
    data: Dict[str, Any],
) -> bool:
    """Append one audit event to the plan's durable audit trail.

    :meth:`VerificationPersistenceManager.write_verification_point_log`
    needs an open round log; an event that must not be lost cannot
    depend on that, so this writes to a file of its own inside the
    plan's log directory. The entry carries the same fields as a
    round-log entry, so a reader does not have to know which of the
    two it is looking at.

    Args:
        plan_dir: The plan's directory (``plans/{id}``).
        vp_id: The verification point id the event belongs to — a
            synthetic id for an event that is about the plan.
        event_type: The event name (e.g. ``plan_delta_persist_failed``).
        data: The event payload.

    Returns:
        ``True`` when the event reached the disk, ``False`` when it
        could not be written. The caller decides what an unwritable
        audit trail means; the event itself is never raised.

    The file is written through :func:`bounded_log.append_bounded_line`,
    so it is bounded: an audit trail that records every event of every
    round would otherwise grow without a ceiling, and a reader that has
    to find one line in it has to read all of it.
    """
    try:
        logs_dir = Path(plan_dir) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "verification_point_id": vp_id,
            "event_type": event_type,
            "timestamp": datetime.now().isoformat(),
            "data": data,
        }
        append_bounded_line(
            logs_dir / AUDIT_LOG_FILENAME,
            json.dumps(entry, ensure_ascii=False, default=str) + "\n",
        )
        return True
    except OSError:
        return False


class VerificationPersistenceManager:
    """
    Manages multi-file persistence for verification reports.

    Persistence strategy:
    - Main report: plans/{id}/verification_report.json (updated after each phase)
    - Derived view: plans/{id}/verification-report.md (rendered on every report write)
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
        self.report_md_file = self.plan_dir / REPORT_MD_FILENAME

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

    def write_audit_event(
        self, vp_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Write one audit event to the plan's durable audit trail.

        The sibling of :meth:`write_verification_point_log` for the
        events that must not be lost: it does not need an open round
        log, so it can be called from a path that runs before (or
        after) the round's own log exists.

        Args:
            vp_id: Verification point ID (a synthetic id for an event
                that is about the plan rather than about one point).
            event_type: Event type (e.g. ``plan_delta_persist_failed``).
            data: Event data to log.

        Returns:
            ``True`` when the event reached the disk.
        """
        return write_audit_event(self.plan_dir, vp_id, event_type, data)

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
        Update the main verification report file — and its Markdown view.

        Called after the entire verification phase completes.  This is
        the **only** writer of both files: the JSON holds the data, the
        Markdown is the view of it, and two writers would mean two
        truths about the same round.

        The order of the steps is the fault isolation:

        1. normalise — fill in the report's extension fields;
        2. render — produce the Markdown (pure computation, no disk);
        3. write the JSON — staged in a temp file, then atomically
           replaced;
        4. write the Markdown — staged, then atomically replaced.

        A renderer that raises leaves the JSON on disk exactly as it was
        (step 2 happens before step 3), and the exception reaches the
        caller — a silent failure would leave it believing the new
        report is on disk.  A Markdown that cannot be written does not
        take the JSON with it: the JSON is already on disk by then, and
        the failure leaves no half-written Markdown behind.

        Args:
            report_data: Complete verification report data

        Raises:
            Exception: Whatever the renderer raised, or whatever the
                disk raised while staging the files.
        """
        report = self._normalize_report(report_data)

        # Stage the JSON.  The final path is only touched by the
        # ``os.replace`` below, so a failure before that leaves the
        # previous report exactly as it was.
        staged_json = self._stage_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            self.report_file,
        )
        try:
            markdown = self._render_markdown(report)
        except BaseException:
            _discard(staged_json)
            raise

        # The JSON is on disk from here on; the Markdown cannot take it
        # away again.
        self._commit(staged_json, self.report_file)

        staged_md = self._stage_text(markdown, self.report_md_file)
        self._commit(staged_md, self.report_md_file)

    # ------------------------------------------------------------------
    # The write path's helpers
    # ------------------------------------------------------------------

    def _normalize_report(self, report_data: Dict[str, Any]) -> Dict[str, Any]:
        """Fill in the report's extension fields and metadata.

        The report is written by an LLM round trip (and by the fallback
        path, and by hand), so it may arrive missing any of the fields
        the readers key on.  Rather than raising on the first missing
        key, every field this manager guarantees is filled in here — at
        the write boundary, which is the only place that knows what the
        report is supposed to contain.

        The report is normalised **in place** (and returned), so the
        caller sees the same report that landed on disk.

        Args:
            report_data: The incoming report

        Returns:
            The normalised report (the same object that was passed in)
        """
        if not isinstance(report_data, dict):
            report_data = {}

        # Defense-in-depth: an LLM round trip (or a manual override)
        # may return a report that omits ``execution_profile``. The
        # bridge UI / status endpoint keys on the field being present,
        # so inject an empty shell at the write boundary rather than
        # raising. ``generate_verification_report`` normally populates
        # this from ExecutionProfileGenerator; this is the safety net
        # for any code path that writes the report directly.
        if "execution_profile" not in report_data:
            report_data["execution_profile"] = {}

        # The extension fields.  ``plan_item_id`` belongs to the
        # verification points; the rest belong to the report.
        self._normalize_plan_item_ids(report_data)
        for field in _REPORT_LIST_FIELDS:
            if not isinstance(report_data.get(field), list):
                report_data[field] = []
        # Which verification points the cost gate held back because this
        # run is a rehearsal.  Derived here (rather than left to the
        # caller) so the JSON carries the same answer the Markdown view
        # shows — and carries it even when there is nothing to skip, so
        # the readers never have to ask whether the field is there.
        report_data["cost_gate_skipped_test_mode"] = _collect_test_mode_skips(report_data)
        report_data["cost_gate_skipped_test_mode_count"] = len(
            report_data["cost_gate_skipped_test_mode"]
        )
        if "final_verdict" not in report_data:
            report_data["final_verdict"] = self._derive_final_verdict(report_data)
        report_data["repair_records"] = self._build_repair_records(report_data)

        # Add metadata about referenced files
        report_data["_metadata"] = {
            "current_round": self._current_round,
            "log_file": str(self._log_file.relative_to(self.plan_dir)) if self._log_file else None,
            "screenshots_dir": "screenshots",
            "logs_dir": "logs",
            "report_md": REPORT_MD_FILENAME,
            "generated_at": datetime.now().isoformat()
        }

        # Collect all screenshot references from current round
        screenshots = self._collect_screenshot_references()
        if screenshots:
            report_data["_metadata"]["screenshots"] = screenshots

        return report_data

    @staticmethod
    def _normalize_plan_item_ids(report: Dict[str, Any]) -> None:
        """Give every verification point its ``plan_item_id``.

        The Markdown view's "计划书条目" column is keyed on
        ``plan_item_id`` — the id of the plan-document entry the point
        traces back to.  A point that does not carry one falls back to
        its own id, which is what the renderer does too; writing it out
        makes the join explicit instead of implicit.
        """
        for field in ("verification_points", "verification_results"):
            points = report.get(field)
            if not isinstance(points, (list, tuple)):
                continue
            for point in points:
                if not isinstance(point, dict):
                    continue
                plan_item_id = point.get("plan_item_id")
                if isinstance(plan_item_id, str) and plan_item_id.strip():
                    point["plan_item_id"] = plan_item_id.strip()
                    continue
                vp_id = point.get("id") or point.get("vp_id")
                point["plan_item_id"] = str(vp_id).strip() if vp_id else ""

    @staticmethod
    def _derive_final_verdict(report: Dict[str, Any]) -> str:
        """The report's verdict, taken from whichever field carries it.

        ``final_verdict`` is the name the readers key on; the older
        spellings (``overall_status`` and friends) are what the LLM
        round trip actually produces.  Deriving it here means the report
        carries one name for one fact.

        Nothing is inferred from the verification points: a report
        without a verdict says it has none (an empty string), because
        "the points all passed" is a conclusion this layer has no
        business drawing.
        """
        for key in _VERDICT_KEYS:
            value = report.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for text_key in _VERDICT_TEXT_KEYS:
                    text = value.get(text_key)
                    if isinstance(text, str) and text.strip():
                        return text.strip()
        return ""

    def _render_markdown(self, report: Dict[str, Any]) -> str:
        """Render the report's Markdown view.

        The renderer is a pure function of its arguments (see
        :mod:`verification_report_md`): the same report in, the same
        bytes out.  The repair rows come from the report itself, so the
        Markdown never says anything the JSON does not.
        """
        repair_rows = report.get("repair_records")
        if not isinstance(repair_rows, (list, tuple)):
            repair_rows = []
        return render_markdown(report, repair_rows=repair_rows)

    def _stage_text(self, text: str, target: Path) -> Path:
        """Write ``text`` to a sibling temp file and return its path.

        The temp file lives next to its target so the final swap is a
        rename within one filesystem — POSIX-atomic.  A reader of the
        final path sees either the old file or the complete new one,
        never a half-written one.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        os.close(handle)
        staged = Path(tmp_name)
        try:
            _write_text_file(staged, text)
        except BaseException:
            _discard(staged)
            raise
        return staged

    def _commit(self, staged: Path, target: Path) -> None:
        """Move a staged file onto its final path, atomically."""
        try:
            os.replace(staged, target)
        except BaseException:
            _discard(staged)
            raise

    # ------------------------------------------------------------------
    # The repair records
    # ------------------------------------------------------------------

    def _build_repair_records(self, report: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Join the confirmed defects with the plan tasks' commits.

        A *repair record* says which repair task fixed which defect and
        — the part that cannot be reconstructed from the report alone —
        which commit it landed in.  The defect lives in the report
        (``confirmed_defects[]``), the commit lives on the plan task
        (``plan_tasks.commit_sha``), and the join key is the
        verification point id.

        Both sides are optional, and neither one is allowed to hide the
        other: a defect whose repair task has not been written yet still
        gets a record (with an empty commit), and a repair task that
        names a verification point gets a record even when the report's
        defect list has not caught up with it yet.
        """
        defects = report.get("confirmed_defects")
        if not isinstance(defects, (list, tuple)):
            defects = []
        tasks = self._collect_plan_tasks()

        by_id: Dict[str, Dict[str, Any]] = {}
        by_vp: Dict[str, Dict[str, Any]] = {}
        for task in tasks:
            task_id = _plan_task_id(task)
            if task_id and task_id not in by_id:
                by_id[task_id] = task
            vp_id = _plan_task_vp_id(task)
            if vp_id and vp_id not in by_vp:
                by_vp[vp_id] = task

        records: List[Dict[str, Any]] = []
        seen = set()

        def _add(vp_id: str, task_id: str, task: Optional[Dict[str, Any]]) -> None:
            key = task_id or f"vp:{vp_id}"
            if key in seen:
                return
            seen.add(key)
            records.append(self._repair_record(vp_id, task_id, task))

        for defect in defects:
            if not isinstance(defect, dict):
                continue
            vp_id = str(defect.get("vp_id") or "").strip()
            task_id = str(defect.get("repair_task_id") or "").strip()
            task = by_id.get(task_id) if task_id else None
            if task is None and vp_id:
                task = by_vp.get(vp_id)
            if task is not None:
                task_id = task_id or _plan_task_id(task)
                vp_id = vp_id or _plan_task_vp_id(task)
            _add(vp_id, task_id, task)

        for task in tasks:
            vp_id = _plan_task_vp_id(task)
            if not vp_id:
                continue
            _add(vp_id, _plan_task_id(task), task)

        records.sort(key=lambda record: (record["vp_id"], record["repair_task_id"]))
        return records

    @staticmethod
    def _repair_record(
        vp_id: str,
        task_id: str,
        task: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """One repair record, as it lands in the report."""
        task = task or {}
        return {
            "vp_id": vp_id,
            "repair_task_id": task_id,
            # The same value under the name the Markdown renderer reads
            # (``_REPAIR_ID_KEYS``); ``repair_task_id`` is the name the
            # report's defect vocabulary uses.
            "task_id": task_id,
            "commit_sha": str(task.get("commit_sha") or ""),
            "status": str(task.get("status") or ""),
            "title": str(task.get("title") or ""),
        }

    def _collect_plan_tasks(self) -> List[Dict[str, Any]]:
        """The plan's tasks, from wherever they can be read.

        Two sources, both optional:

        * ``state.db``'s ``plan_tasks`` table — the single source of
          truth for a task's runtime state, and the only place a
          repair task's ``commit_sha`` and its verification point id
          are both recorded;
        * ``tasks.json`` — the dispatcher's input, for the plans whose
          rows never reached the database.

        A plan that has neither simply has no repairs to join.
        """
        tasks = self._plan_tasks_from_state_db()
        tasks.extend(self._plan_tasks_from_tasks_json())
        return tasks

    def _plan_tasks_from_state_db(self) -> List[Dict[str, Any]]:
        """The plan's task rows, read from ``state.db``."""
        try:
            from config_paths import resolve_state_db_path
            from state_machine.db.connection import open as open_db
            from state_machine.repositories.plan_task_repository import (
                PlanTaskRepository,
            )

            db_path = resolve_state_db_path()
            if not db_path.exists():
                return []
            conn = open_db(db_path)
            try:
                rows = PlanTaskRepository(conn).load_all(self.plan_dir.name)
            finally:
                conn.close()
        except Exception:
            # Best-effort: a plan whose database cannot be read has no
            # repair records to show, which is not a reason to fail the
            # report write.
            return []
        # ``load_all`` keys the rows by task id and does not repeat it
        # inside the row; put it back so both sources look the same.
        return [
            {"task_id": task_id, **dict(row)}
            for task_id, row in rows.items()
            if isinstance(row, dict)
        ]

    def _plan_tasks_from_tasks_json(self) -> List[Dict[str, Any]]:
        """The plan's task rows, read from ``tasks.json``."""
        path = self.plan_dir / TASKS_FILENAME
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return []
        if isinstance(payload, dict):
            payload = payload.get("tasks")
        if not isinstance(payload, list):
            return []
        return [dict(entry) for entry in payload if isinstance(entry, dict)]

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

    def _log_bases(self, pattern: str) -> List[Path]:
        """The live log files matching ``pattern``, in file-name order.

        A log file's rotated copies are found next to it, and the live
        file is the one the glob matches.  A copy whose live file is
        gone — archived by the round cleanup, or deleted — still holds
        entries, so the copies are looked for as well and the live file
        they belong to is rebuilt from the copy's name.

        The order is the file-name order, because the file name carries
        the round and the timestamp: two log files of the same round are
        two moments of that round, and the earlier one holds the earlier
        entries.
        """
        bases = {path for path in self.logs_dir.glob(pattern) if path.is_file()}

        for copy_path in self.logs_dir.glob(f"{pattern}.*"):
            match = _ROTATED_COPY_SUFFIX.search(copy_path.name)
            if match is None:
                continue
            suffix_length = match.end() - match.start()
            base = copy_path.with_name(copy_path.name[:-suffix_length])
            if not base.exists():
                bases.add(base)

        return sorted(bases)

    def _read_log_entries(self, path: Path) -> List[Dict[str, Any]]:
        """The entries of one JSON-lines log file, the bad lines skipped.

        A file that cannot be read costs that file and nothing else: the
        round log is written while the plan runs, and a copy that was
        torn by a kill holds half a line.  Neither may end the read —
        the entries the other files hold are still the answer.
        """
        entries: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(entry, dict):
                        entries.append(entry)
        except Exception:
            pass

        return entries

    def get_round_logs(self, round_number: int) -> List[Dict[str, Any]]:
        """
        Load all log entries for a specific round.

        The rotated copies of each log file are read too, oldest copy
        first: a rotation moves the *old* entries out of the live file,
        and the entries a reader is looking for are often exactly the
        ones that were rotated out.

        Args:
            round_number: Round number to load

        Returns:
            List of log entries
        """
        entries: List[Dict[str, Any]] = []
        for log_file in self._log_bases(f"verification_{round_number}_*.log"):
            for candidate in _log_candidates(log_file):
                entries.extend(self._read_log_entries(candidate))

        return entries

    def get_verification_point_logs(self, vp_id: str) -> List[Dict[str, Any]]:
        """
        Load all log entries for a specific verification point across all rounds.

        The rotated copies of each log file are read too, oldest copy
        first — see :meth:`get_round_logs`.

        Args:
            vp_id: Verification point ID

        Returns:
            List of log entries for this verification point
        """
        all_entries: List[Dict[str, Any]] = []
        for log_file in self._log_bases("verification_*.log"):
            for candidate in _log_candidates(log_file):
                for entry in self._read_log_entries(candidate):
                    if entry.get("verification_point_id") == vp_id:
                        all_entries.append(entry)

        return all_entries

    def _write_log_entry(self, entry: Dict[str, Any]) -> None:
        """Write a JSON-lines log entry.

        The write goes through :func:`bounded_log.append_bounded_line`,
        so the round log is bounded: a round that logs every sub-agent
        line would otherwise grow a single file past any ceiling, and
        the reader of that file has to read all of it to find the one
        line it wanted.

        A log that cannot be written costs that line and nothing else.
        The round has to finish whatever happens to its log, so the
        write failure is swallowed here rather than ending the
        verification.
        """
        if not self._log_file:
            return

        try:
            append_bounded_line(
                self._log_file,
                json.dumps(entry, ensure_ascii=False) + "\n",
            )
        except OSError:
            pass

    def _collect_screenshot_references(self) -> List[str]:
        """Collect all screenshot references from current round.

        The rotated copies of the round log are read too, oldest copy
        first — a rotation moves the *old* screenshots out of the live
        file, and a screenshot the report does not reference is a
        screenshot nobody can look at.
        """
        screenshots: List[str] = []

        if not self._log_file:
            return screenshots

        for candidate in _log_candidates(self._log_file):
            for entry in self._read_log_entries(candidate):
                if entry.get("event_type") != "screenshot_saved":
                    continue
                data = entry.get("data")
                rel_path = data.get("relative_path") if isinstance(data, dict) else None
                if rel_path:
                    screenshots.append(rel_path)

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


# ---------------------------------------------------------------------------
# 只追加的验证结果行（``plan_verification_result``）
# ---------------------------------------------------------------------------
#: 结果行的来源：框架修复这条链上的**首次**结论 —— 缺陷被登记时的那一轮。
#:
#: 它是这条链的基线：后来的每一次重跑都是拿它比的。没有它，「修了之后是好了
#: 还是换了个样子」就没有可比的起点。
RESULT_SOURCE_INITIAL = "initial"

#: 结果行的来源：修复合入之后，在含修复的树上重跑的那一次。
RESULT_SOURCE_FRAMEWORK_FIX_RERUN = "framework_fix_rerun"

#: 结果行的来源：已修复的签名又被撞上（延迟命中）之后的那一次重跑。
#:
#: 它与 :data:`RESULT_SOURCE_FRAMEWORK_FIX_RERUN` 的区别不在**做了什么**
#: （都是重跑），而在**为什么**：这一轮是修复合入**之后**才撞上的。
RESULT_SOURCE_LATE_HIT_RERUN = "late_hit_rerun"

#: 结果行的来源词表。集合外的词不是「新语义」，是拼错了。
RESULT_SOURCES = frozenset(
    {
        "verification_loop",
        RESULT_SOURCE_INITIAL,
        RESULT_SOURCE_FRAMEWORK_FIX_RERUN,
        RESULT_SOURCE_LATE_HIT_RERUN,
        "manual",
    }
)

#: 结果行的结论词表。
RESULT_VERDICTS = frozenset({"passed", "failed", "skipped", "unknown"})

#: 各种写法的结论 → 结果行真正写的那一个。
_VERDICT_ALIASES = {
    "passed": "passed",
    "pass": "passed",
    "ok": "passed",
    "success": "passed",
    "verification_passed": "passed",
    "failed": "failed",
    "fail": "failed",
    "failure": "failed",
    "error": "failed",
    "verification_failed": "failed",
    "skipped": "skipped",
    "skip": "skipped",
    "deferred": "skipped",
    "unknown": "unknown",
    "inconclusive": "unknown",
    "not_run": "unknown",
}


def normalize_result_verdict(verdict: Any) -> str:
    """把各种写法的结论归一成 :data:`RESULT_VERDICTS` 里的那一个。

    ``None`` 归成 ``unknown`` —— 「没跑出结论」是一个真实的结论，不是「不知道
    是哪个词」。别的写法认不出来就**抛错**：拼错的结论看起来和没人写过的结论
    一模一样，而那会把一次失败记成一次「不知道」。

    Raises:
        ValueError: 这个写法不在词表里。
    """
    if verdict is None:
        return "unknown"
    text = " ".join(str(verdict).split()).lower()
    resolved = _VERDICT_ALIASES.get(text)
    if resolved is None:
        raise ValueError(
            f"unknown verification verdict {verdict!r}; expected one of "
            f"{sorted(RESULT_VERDICTS)}"
        )
    return resolved


# ---------------------------------------------------------------------------
# 框架缺陷挂起的守卫 —— 「这个计划此刻被挂住了吗」的**唯一**判据
# ---------------------------------------------------------------------------
#: 一个被框架缺陷挂住的计划被拒绝时带的错误词。它与修复生成器用的同一个词
#: （``repair_generator.SKIPPED_BLOCKED_ON_FRAMEWORK_FIX``）、与读模型投影出来
#: 的同一个词（``framework_fix.read_model.BLOCKED``）是同一个词 —— 一件事一个
#: 词。
BLOCKED_ON_FRAMEWORK_FIX = "blocked_on_framework_fix"

#: 守卫**读不到**挂起状态时带的错误词。它刻意不是上面那个：守卫并不知道这个
#: 计划有没有被挂起，说它被挂住了是「把猜测打扮成答案」。
FRAMEWORK_FIX_GUARD_UNAVAILABLE = "framework_fix_guard_unavailable"


class FrameworkFixGuardError(RuntimeError):
    """守卫的拒绝。两个子类都带一个能映射成 HTTP 状态码的错误词。"""

    #: 这条拒绝带的错误词。
    error: str = FRAMEWORK_FIX_GUARD_UNAVAILABLE

    #: 这条拒绝对应的 HTTP 状态码。
    status_code: int = 500

    def __init__(self, message: str = "", *, fingerprint: str = "") -> None:
        super().__init__(message or self.error)
        #: 挂住这个计划的缺陷指纹（读得到才有）。
        self.fingerprint = str(fingerprint or "")


class FrameworkFixGuardUnavailable(FrameworkFixGuardError):
    """挂起状态读不出来 —— 守卫给不出答案，所以不放行。"""

    error = FRAMEWORK_FIX_GUARD_UNAVAILABLE
    status_code = 500


class BlockedOnFrameworkFix(FrameworkFixGuardError):
    """这个计划被框架缺陷挂住了 —— 这次写入被拒。"""

    error = BLOCKED_ON_FRAMEWORK_FIX
    status_code = 409


def framework_fix_hold_block(plan_id: str) -> Optional[Dict[str, Any]]:
    """这个计划此刻的挂起块 —— 没挂起返回 ``None``。

    挂起的载体住在计划的 ``framework_fix`` 块里：``hold`` 标记，以及
    ``paused_by``（挂到了哪个修复计划上）。判据取自读模型
    （``framework_fix.read_model._is_held``）—— **这里是它唯一的调用点**，
    所以第二个入口不可能自己重写一份判据然后写错。

    相位那个载体（``current_phase == "blocked_on_framework_fix"``）不参与判定：
    相位是挂起的**投影**，块才是挂起本身的落点。

    Args:
        plan_id: 计划号。

    Returns:
        挂起块（字典）；这个计划没被挂起时是 ``None``。

    Raises:
        FrameworkFixGuardUnavailable: 挂起状态**读不出来**。「不知道」不是
            「没挂起」—— 读失败当成没挂起，守卫就成了摆设。
    """
    from framework_fix.read_model import _is_held
    from plan_state import _read_routing_framework_fix

    read = _read_routing_framework_fix(plan_id)
    if read is None:
        raise FrameworkFixGuardUnavailable(
            f"the framework-fix hold state of plan {plan_id!r} could not be read"
        )
    block = read[1]
    if not isinstance(block, dict):
        return None
    if _is_held({}, block):
        return block
    return None


def framework_fix_is_held(plan_id: str) -> bool:
    """这个计划此刻被框架缺陷挂住了吗。

    Raises:
        FrameworkFixGuardUnavailable: 挂起状态读不出来。
    """
    return framework_fix_hold_block(plan_id) is not None


def refuse_verdict_when_held(plan_id: str) -> None:
    """这个计划被挂起了吗 —— 是的话把这次写入拒掉。

    给落盘层用：调用方拿到的是一个**异常**，不是一个答复。它映射成 409 时说
    的就是「这个计划被框架缺陷挡着」；验证循环接住它、记一行日志、不落行 ——
    那条 ``skipped`` 结论于是没有结清任何东西。

    Args:
        plan_id: 计划号。

    Raises:
        BlockedOnFrameworkFix: 这个计划被框架缺陷挂住了。
        FrameworkFixGuardUnavailable: 挂起状态读不出来（同样不放行）。
    """
    hold = framework_fix_hold_block(plan_id)
    if hold is None:
        return
    fingerprint = str(hold.get("fingerprint") or "")
    # The message carries the plan and the defect on purpose.  The
    # verification loop catches this exception and logs it — so the
    # message is the only place the operator learns **which** defect is
    # holding the plan.  A refusal that does not say which defect is
    # holding it is a refusal nobody can act on.
    raise BlockedOnFrameworkFix(
        f"plan {plan_id!r} is held by the framework defect "
        f"{fingerprint or '(fingerprint unknown)'}; this write is refused",
        fingerprint=fingerprint,
    )


def _result_db_path(db_path: Optional[Path] = None) -> Path:
    """结果行落在哪个库。不给就用应用现在用的那个。"""
    if db_path is not None:
        return Path(db_path)
    return Path(resolve_state_db_path())


def _migrate_result_table(conn: sqlite3.Connection) -> None:
    """迁移是幂等的补列步骤 —— 列不存在时写不进去。

    表本身是「新增」而不是「改形状」，所以迁移按**表在不在**来判断，而不是
    按版本号：版本号只是形状变更的缓存，而这张表在不在才是更好的账本。
    """
    try:  # pragma: no cover - 两条路径等价，取先成功的那条
        from state_machine.db.schema import migrate
    except ImportError:  # pragma: no cover
        from backend.state_machine.db.schema import migrate  # type: ignore

    migrate(conn)


def append_verification_result(
    *,
    plan_id: str,
    vp_id: str,
    tree_fingerprint: str,
    verdict: str,
    round_no: int = 1,
    base_main_commit: str = "",
    clean: bool = True,
    source: str = "verification_loop",
    fix_plan_id: Optional[str] = None,
    evidence_path: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> str:
    """追加一行验证结果，返回它的行 id。

    **只追加，永不更新。** 这张表的全部不变式就是这一句：一次重跑写一行，旧行
    原地不动。「这棵树过了吗」这个问题由**这棵树**的那几行回答，而不是由「最后
    一行」回答 —— 后者会把一次失败抹掉。

    每一行都绑着一棵树（``tree_fingerprint``）：同一个验证点在不同的树上结论
    可以不同，两条结论都留着，各自说清自己是在哪棵树上得出的。

    Args:
        plan_id: 结论属于哪个计划。
        vp_id: 哪个验证点。
        tree_fingerprint: 结论是在哪棵树上得出的（``sha256:`` 前缀）。
        verdict: 结论。各种写法都认（见 :func:`normalize_result_verdict`）。
        round_no: 第几轮。
        base_main_commit: 基准 main 的 commit。
        clean: 当时工作区是不是干净的。
        source: 这一行的来源（见 :data:`RESULT_SOURCES`）。
        fix_plan_id: 这次重跑是为哪个修复计划跑的。
        evidence_path: 证据文件的位置（原文不入库）。
        db_path: 落在哪个库。``None`` 表示应用现在用的那个。

    Returns:
        这一行的 ``result_id``。

    Raises:
        ValueError: 参数不合法（空计划号 / 空验证点 / 空指纹 / 词表外的词）。
        BlockedOnFrameworkFix: 结论是 ``skipped``，而这个计划正被框架缺陷挂住
            —— 挂起中的验证点不许被 skip 结清。
        FrameworkFixGuardUnavailable: 挂起状态读不出来（同样不放行）。
    """
    plan = str(plan_id or "").strip()
    vp = str(vp_id or "").strip()
    tree = str(tree_fingerprint or "").strip()
    if not plan:
        raise ValueError("plan_id must be a non-empty string")
    if not vp:
        raise ValueError("vp_id must be a non-empty string")
    if not tree:
        raise ValueError("tree_fingerprint must be a non-empty string")
    resolved = normalize_result_verdict(verdict)
    origin = str(source or "").strip()
    if origin not in RESULT_SOURCES:
        raise ValueError(
            f"unknown result source {source!r}; expected one of "
            f"{sorted(RESULT_SOURCES)}"
        )
    number = int(round_no)
    if number < 0:
        raise ValueError("round_no must not be negative")

    # 挂起中的验证点不许被一条 ``skipped`` 结论结清。一条 skipped 结论说的
    # 是「这个验证点不用验了」—— 而它正被挂住就是因为没验出来。拒绝发生
    # 在落行之前，所以这一行根本不会存在：验证点仍然是欠账。
    #
    # 与 skip 无关的结论保持原语义：挂起不改变「这一轮实际观察到了什么」。
    if resolved == "skipped":
        refuse_verdict_when_held(plan)

    result_id = uuid.uuid4().hex
    conn = sqlite3.connect(str(_result_db_path(db_path)))
    try:
        _migrate_result_table(conn)
        conn.execute(
            "INSERT INTO plan_verification_result ("
            "  result_id, plan_id, round, vp_id, tree_fingerprint,"
            "  base_main_commit, clean, verdict, source, fix_plan_id,"
            "  evidence_path, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result_id,
                plan,
                number,
                vp,
                tree,
                str(base_main_commit or ""),
                1 if clean else 0,
                resolved,
                origin,
                fix_plan_id,
                evidence_path,
                utcnow_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return result_id


def read_verification_results(
    *,
    plan_id: Optional[str] = None,
    vp_id: Optional[str] = None,
    tree_fingerprint: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """读验证结果行 —— 按写入顺序。

    按 ``rowid`` 排而不是按时间戳：同一微秒里落两行的事在这里同样会发生，
    而 ``result_id`` 是随机的 —— 按它排等于随机排历史。

    Args:
        plan_id: 只读这个计划的行。
        vp_id: 只读这个验证点的行。
        tree_fingerprint: 只读这棵树的行。
        db_path: 读哪个库。``None`` 表示应用现在用的那个。

    Returns:
        结果行（字典），最早的在前。
    """
    clauses: List[str] = []
    params: List[Any] = []
    if plan_id is not None:
        clauses.append("plan_id = ?")
        params.append(str(plan_id))
    if vp_id is not None:
        clauses.append("vp_id = ?")
        params.append(str(vp_id))
    if tree_fingerprint is not None:
        clauses.append("tree_fingerprint = ?")
        params.append(str(tree_fingerprint))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    conn = sqlite3.connect(str(_result_db_path(db_path)))
    conn.row_factory = sqlite3.Row
    try:
        _migrate_result_table(conn)
        rows = conn.execute(
            "SELECT * FROM plan_verification_result" + where + " ORDER BY rowid",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def read_vp_results(
    *,
    plan_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """读出**逐验证点**的结论 —— 每个验证点一行。

    追加表里同一个验证点可以有很多行：每棵树一行，每跑一次重跑再一行。
    这里**每个验证点只给一行** —— 最新那棵树上的那行，也就是最后写进去的
    那行（表按 ``rowid`` 排序，所以「最后写进去」就是「最新的那棵树」）。

    历史没有消失：``history_count`` 说这个验证点一共留了几行。「这棵树过了
    吗」由这棵树的那几行回答；「这个验证点最近怎么样」由这一行回答 —— 两个
    问题，两个答案，谁也不冒充谁。

    Args:
        plan_id: 只读这个计划的行。``None`` 表示所有计划。
        db_path: 读哪个库。``None`` 表示应用现在用的那个。

    Returns:
        结论列表（字典）。没有结果时是空列表 —— 不是缺字段，是「还没有
        结论」。
    """
    rows = read_verification_results(plan_id=plan_id, db_path=db_path)

    latest: Dict[str, Dict[str, Any]] = {}
    counts: Dict[str, int] = {}
    for row in rows:
        vp_id = str(row.get("vp_id") or "").strip()
        if not vp_id:
            continue
        counts[vp_id] = counts.get(vp_id, 0) + 1
        # 后写的那行覆盖先写的那行 —— 留下的就是最后写进去的那条。
        latest[vp_id] = row

    results: List[Dict[str, Any]] = []
    for vp_id, row in latest.items():
        results.append(
            {
                "vp_id": vp_id,
                "status": str(row.get("verdict") or "unknown"),
                "tree_fingerprint": str(row.get("tree_fingerprint") or ""),
                "round": int(row.get("round") or 0),
                "history_count": counts.get(vp_id, 1),
            },
        )
    return results
