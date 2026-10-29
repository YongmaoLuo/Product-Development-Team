#!/usr/bin/env python3
"""scripts/scheduler_dryrun_diff.py — replay a dry run, judge it, write diff.md.

Why this script exists
----------------------
The native scheduler replaces the old cron scheduler, but only after a
24 hour dry run proves the two behave the same.  The evidence of that
run is three files: ``ticks.jsonl`` (the decisions the new engine
recorded), ``windows.json`` (the 48 half-hour windows the comparison is
made in) and ``diff.md`` (the comparison itself, written out so a human
can read it).

A 24 hour wait cannot sit in a pull-request gate, so the *comparison* is
replayed against a recorded run instead: this script reads the first two
files, hands the windows to the judge, and writes the third.  What it
regresses in CI is the judge and the classifier — the two pieces of
logic that decide whether the switch may happen — and it does so
against real recorded data rather than against a mock.

The judgement is not this script's own
--------------------------------------
The criterion — 48 windows, at least 45 of them identical, the rest
allowed-only, no forbidden difference anywhere — lives in
:func:`plan_scheduler.dryrun.evaluate_windows` and
:func:`plan_scheduler.dryrun.classify_difference`, and this script
calls them.  Re-implementing the criterion here would let the tool and
the production gate judge differently, and a comparison tool that
judges differently from the gate is worse than no tool at all.

What this script is not
-----------------------
It is not the engine and it does not run one.  It reads two files and
writes one.  It never opens the scheduler database, never starts the
worker, never touches a plan — a comparison tool that wrote into the
run it is judging would stop being evidence.  For the same reason it
loads the two pure functions straight from ``dryrun.py`` instead of
importing the scheduler package: the script's import graph stays
exactly the two functions and never reaches the engine runtime.

Exit codes
----------
0
    The replay passed — the criterion holds.
1
    The replay did not pass: a forbidden difference, too few windows, too
    few identical ones, or a window that was never compared with the old
    engine — an uncompared window is not evidence, and a run that
    compared nothing may not switch.
2
    The replay could not be run: an input file is missing, unreadable or
    malformed.  The missing path is printed — on stdout, so a gate that
    only captures the report still sees why it stopped, and on stderr,
    so the diagnostic is where diagnostics belong.

Usage
-----
::

    python3 scripts/scheduler_dryrun_diff.py \\
        --ticks /tmp/pdt-native-scheduler/dryrun/20261027/ticks.jsonl \\
        --windows /tmp/pdt-native-scheduler/dryrun/20261027/windows.json \\
        --out /tmp/pdt-native-scheduler/dryrun/20261027/diff.md
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: The replay passed.
EXIT_PASS = 0

#: The replay ran and the verdict is no.
EXIT_FAIL = 1

#: The replay could not be run at all.
EXIT_INPUT = 2

#: The length of one comparison window — the old engine's cron window.
WINDOW_MINUTES = 30

#: The keys a window may carry its differences under.  These are the
#: keys the judge reads (see ``plan_scheduler.dryrun``); the report
#: needs them to point at the exact place in the recorded file a
#: difference came from.
DIFFERENCE_KEYS = (
    "differences",
    "diffs",
    "difference",
    "diff",
    "diff_kinds",
    "kinds",
)

#: The keys a difference (or the window it belongs to) may carry its
#: written explanation under.  The criterion asks for every allowed
#: difference to be explained in writing, so the report has to find the
#: explanation — and has to say so when there is none.
EXPLANATION_KEYS = (
    "explanation",
    "explanation_ref",
    "note",
    "notes",
    "note_ref",
    "comment",
    "justification",
    "rationale",
    "why",
    "doc",
    "doc_ref",
)

#: The keys a window may carry its start time under.
WINDOW_START_KEYS = ("start", "start_ts", "window_start", "from", "begin", "ts")

#: The four conclusions a window can reach.
CONCLUSION_IDENTICAL = "一致"
CONCLUSION_ALLOWED = "仅含允许差异"
CONCLUSION_FORBIDDEN = "含不允许差异"
CONCLUSION_UNCOMPARED = "未对照"


class InputError(Exception):
    """An input file is missing, unreadable or malformed."""


@dataclass
class DifferenceNote:
    """One difference of one window, and where its explanation lives."""

    kind: str
    classification: str
    location: str
    explanation: Optional[str]


@dataclass
class WindowNote:
    """One window of the replay and what the judge made of it."""

    index: int
    start: Optional[str]
    conclusion: str
    tick_count: Optional[int]
    differences: List[DifferenceNote]


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------


def _load_judge() -> Any:
    """Load the two pure functions that make the judgement.

    ``plan_scheduler.dryrun`` is a leaf module: it imports nothing but
    the standard library.  It is loaded straight from its file so that
    this script's import graph stays exactly those two functions and
    never reaches the engine, the scheduler store or the plan
    lifecycle — and so the script runs on any Python 3, without the
    backend's dependencies installed.

    Returns:
        The ``plan_scheduler.dryrun`` module.
    """
    backend = Path(__file__).resolve().parents[1] / "backend"
    module_path = backend / "plan_scheduler" / "dryrun.py"

    if module_path.is_file():
        spec = importlib.util.spec_from_file_location(
            "plan_scheduler_dryrun", module_path
        )
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(spec.name, None)
                raise
            return module

    # Fall back to the package import for a layout where the module is
    # not a file next to this script (an installed package, say).
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from plan_scheduler import dryrun  # noqa: E402  (deliberate late import)

    return dryrun


# ---------------------------------------------------------------------------
# Reading the recorded run
# ---------------------------------------------------------------------------


def _read_ticks(path: Path) -> List[Dict[str, Any]]:
    """Read the recorded tick decisions.

    One JSON object per line.  A blank line is skipped; a line that is
    not a JSON object is an input error, because a tick log that cannot
    be parsed is not a tick log.

    Args:
        path: the ``ticks.jsonl`` file.

    Returns:
        The recorded tick decisions, in the order they were recorded.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"无法读取 ticks 文件: {path} ({exc})") from exc

    records: List[Dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise InputError(f"{path}:{number}: 不是合法的 JSON 行 ({exc})") from exc
        if not isinstance(record, dict):
            raise InputError(f"{path}:{number}: tick 记录必须是 JSON 对象")
        records.append(record)
    return records


def _read_windows(path: Path) -> Tuple[List[Any], str]:
    """Read the window records, and the key they sit under.

    The windows are a list, either at the top level of the file or
    under a ``windows`` key.  The key is what the report's explanation
    locations are built from, so a location can be pasted back into the
    file it came from.

    Args:
        path: the ``windows.json`` file.

    Returns:
        The window records and the key they were found under (empty for
        a top-level list).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"无法读取 windows 文件: {path} ({exc})") from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: 不是合法的 JSON ({exc})") from exc

    if isinstance(payload, list):
        return list(payload), ""
    if isinstance(payload, dict):
        for key in ("windows", "records", "entries"):
            value = payload.get(key)
            if isinstance(value, list):
                return list(value), key
    raise InputError(f'{path}: 找不到窗口列表（期望顶层数组或 "windows" 数组）')


# ---------------------------------------------------------------------------
# Reading one window
# ---------------------------------------------------------------------------


def _difference_entries(window: Any) -> List[Tuple[str, Any]]:
    """The differences a window lists, with the key they were listed under.

    Args:
        window: one window record.

    Returns:
        One ``(key, entry)`` pair per difference the window carries.
    """
    if isinstance(window, dict):
        for key in DIFFERENCE_KEYS:
            value = window.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                return [(key, value)]
            if isinstance(value, (list, tuple)):
                return [(key, entry) for entry in value]
            return []
        return []

    if isinstance(window, str):
        return [("difference", window)]
    if isinstance(window, (list, tuple)):
        return [("difference", entry) for entry in window]
    return []


def _kind_of(entry: Any) -> str:
    """The name of a difference's kind, for the report to show.

    Args:
        entry: one difference entry.

    Returns:
        The kind's name, or a placeholder when the entry names none.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for key in ("kind", "type"):
            value = entry.get(key)
            if isinstance(value, str):
                return value
    return "(未命名)"


def _explanation_of(entry: Any, window: Any) -> Optional[str]:
    """The written explanation of a difference, if there is one.

    The explanation may sit on the difference itself or on the window
    the difference belongs to.  A difference without one is reported as
    missing rather than dropped: the criterion asks for every allowed
    difference to be explained in writing, and a difference nobody
    explained is not an explained difference.

    Args:
        entry: one difference entry.
        window: the window the difference belongs to.

    Returns:
        The explanation, or ``None`` when there is none.
    """
    for source in (entry, window):
        if not isinstance(source, dict):
            continue
        for key in EXPLANATION_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (list, tuple)) and value:
                first = value[0]
                if isinstance(first, str) and first.strip():
                    return first.strip()
    return None


def _location(
    windows_path: Path, prefix: str, position: int, key: str, entry_position: int
) -> str:
    """Where a difference sits in the recorded file.

    Args:
        windows_path: the file the difference was read from.
        prefix: the key the windows sit under (empty for a top-level list).
        position: the window's index in the file.
        key: the key the difference was listed under.
        entry_position: the difference's index within the window.

    Returns:
        A pointer into the recorded file, e.g.
        ``windows.json#/windows/3/differences/0``.
    """
    parts = [part for part in (prefix, str(position), key) if part]
    pointer = "#/" + "/".join(parts)
    if entry_position >= 0:
        pointer = f"{pointer}/{entry_position}"
    return f"{windows_path.name}{pointer}"


def _parse_timestamp(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, or return ``None``.

    Args:
        value: the timestamp text.

    Returns:
        The moment, timezone-aware, or ``None`` when it is not one.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _window_start(window: Any) -> Optional[datetime]:
    """The moment a window starts, when the record says so."""
    if not isinstance(window, dict):
        return None
    for key in WINDOW_START_KEYS:
        value = window.get(key)
        if isinstance(value, str):
            parsed = _parse_timestamp(value)
            if parsed is not None:
                return parsed
    return None


def _tick_time(tick: Dict[str, Any]) -> Optional[datetime]:
    """The moment a tick was recorded, when the record says so."""
    value = tick.get("ts")
    if not isinstance(value, str):
        return None
    return _parse_timestamp(value)


def _ticks_in(bucket: datetime, ticks: List[Dict[str, Any]]) -> int:
    """How many recorded ticks fall into the window that starts at ``bucket``."""
    end = bucket + timedelta(minutes=WINDOW_MINUTES)
    count = 0
    for tick in ticks:
        moment = _tick_time(tick)
        if moment is None:
            continue
        if bucket <= moment < end:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Judging the replay
# ---------------------------------------------------------------------------


def _conclusion(judge: Any, window: Any) -> str:
    """What the judge makes of one window.

    The judge is asked about the window on its own, so the answer is
    that window's bucket: identical, allowed-only, forbidden — or
    *uncompared*, which is what a window nobody compared with the old
    engine is.  The verdict of the whole run is a separate question,
    asked of all the windows at once.

    Args:
        judge: the ``plan_scheduler.dryrun`` module.
        window: one window record.

    Returns:
        The window's conclusion.
    """
    conclusion = judge.classify_window(window)
    if conclusion == judge.FORBIDDEN:
        return CONCLUSION_FORBIDDEN
    if conclusion == judge.ALLOWED_ONLY:
        return CONCLUSION_ALLOWED
    if conclusion == judge.UNCOMPARED:
        return CONCLUSION_UNCOMPARED
    return CONCLUSION_IDENTICAL


def _analyse(
    judge: Any,
    windows: List[Any],
    ticks: List[Dict[str, Any]],
    windows_path: Path,
    prefix: str,
) -> List[WindowNote]:
    """Judge every window and note what the report has to say about it.

    Args:
        judge: the ``plan_scheduler.dryrun`` module.
        windows: the window records.
        ticks: the recorded tick decisions.
        windows_path: the file the windows were read from.
        prefix: the key the windows sit under.

    Returns:
        One note per window, oldest first.
    """
    notes: List[WindowNote] = []
    for position, window in enumerate(windows):
        differences: List[DifferenceNote] = []
        for entry_position, (key, entry) in enumerate(_difference_entries(window)):
            differences.append(
                DifferenceNote(
                    kind=_kind_of(entry),
                    classification=judge.classify_difference(entry),
                    location=_location(
                        windows_path, prefix, position, key, entry_position
                    ),
                    explanation=_explanation_of(entry, window),
                )
            )

        start = _window_start(window)
        notes.append(
            WindowNote(
                index=position + 1,
                start=start.strftime("%Y-%m-%dT%H:%M:%SZ") if start else None,
                conclusion=_conclusion(judge, window),
                tick_count=_ticks_in(start, ticks) if start else None,
                differences=differences,
            )
        )
    return notes


# ---------------------------------------------------------------------------
# Writing the report
# ---------------------------------------------------------------------------


def _tick_span(ticks: List[Dict[str, Any]]) -> Optional[str]:
    """The time span the recorded ticks cover."""
    moments = [moment for moment in (_tick_time(tick) for tick in ticks) if moment]
    if not moments:
        return None
    first = min(moments).strftime("%Y-%m-%dT%H:%M:%SZ")
    last = max(moments).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{first} ~ {last}"


def _total(verdict: Any) -> int:
    """How many windows the verdict judged, all four kinds together."""
    return (
        verdict.consistent
        + verdict.allowed_only
        + verdict.forbidden
        + getattr(verdict, "uncompared", 0)
    )


def _render_report(
    judge: Any,
    verdict: Any,
    notes: List[WindowNote],
    ticks: List[Dict[str, Any]],
    ticks_path: Path,
    windows_path: Path,
) -> str:
    """Write the comparison out as markdown.

    Args:
        judge: the ``plan_scheduler.dryrun`` module.
        verdict: the verdict of the whole replay.
        notes: one note per window.
        ticks: the recorded tick decisions.
        ticks_path: the file the ticks were read from.
        windows_path: the file the windows were read from.

    Returns:
        The report, as markdown.
    """
    passed = bool(verdict.passed)
    lines: List[str] = []

    lines.append("# dry-run 对拍报告")
    lines.append("")
    lines.append("## 概要")
    lines.append("")
    span = _tick_span(ticks)
    tick_note = f"（回放 {len(ticks)} 条 tick"
    tick_note += f"，{span}）" if span else "）"
    lines.append(f"- ticks: `{ticks_path.name}`{tick_note}")
    lines.append(f"- windows: `{windows_path.name}`（{len(notes)} 个周期）")
    lines.append(f"- 判定: **{'PASSED' if passed else 'FAILED'}**")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 周期总数 | {_total(verdict)} |")
    lines.append(f"| 完全一致 | {verdict.consistent} |")
    lines.append(f"| 仅含允许差异 | {verdict.allowed_only} |")
    lines.append(f"| 含不允许差异 | {verdict.forbidden} |")
    lines.append(f"| 未对照 | {verdict.uncompared} |")
    lines.append("")
    lines.append(
        "判定口径：周期数 ≥ {min_windows}，其中完全一致 ≥ {min_consistent}，"
        "其余周期只含允许差异，不允许差异为 0。".format(
            min_windows=judge.MIN_WINDOWS, min_consistent=judge.MIN_CONSISTENT
        )
    )
    lines.append("")
    lines.append("## 逐周期结论")
    lines.append("")

    for note in notes:
        title = f"### 周期 {note.index}"
        if note.start:
            title += f" · {note.start}"
        title += f" · {note.conclusion}"
        lines.append(title)
        lines.append("")
        ticks_note = "—" if note.tick_count is None else str(note.tick_count)
        lines.append(f"- 回放 tick：{ticks_note}")
        allowed = sum(1 for item in note.differences if item.classification == "allowed")
        forbidden = sum(
            1 for item in note.differences if item.classification == "forbidden"
        )
        if allowed:
            lines.append(f"- 允许差异：{allowed} 条（逐条说明见下节）")
        if forbidden:
            lines.append(f"- 不允许差异：{forbidden} 条（逐条说明见下节）")
        lines.append("")

    lines.append("## 允许差异（逐条书面解释）")
    lines.append("")
    allowed_lines = _difference_lines(notes, "allowed")
    lines.extend(allowed_lines or ["- 无"])
    lines.append("")
    lines.append("## 不允许差异")
    lines.append("")
    forbidden_lines = _difference_lines(notes, "forbidden")
    lines.extend(forbidden_lines or ["- 无"])
    lines.append("")

    missing = sum(
        1
        for note in notes
        for item in note.differences
        if item.classification == "allowed" and not item.explanation
    )
    if missing:
        lines.append(
            f"> 有 {missing} 条允许差异没有书面解释——判定口径要求逐条书面解释，"
            "请补齐后再进入切换门禁。"
        )
        lines.append("")

    total = _total(verdict)
    lines.append("## 结论")
    lines.append("")
    lines.append(
        "{verdict} —— {total} 个周期中 {consistent} 个完全一致，"
        "{allowed} 个周期仅含允许差异，含不允许差异 {forbidden} 个，"
        "未对照 {uncompared} 个。".format(
            verdict="PASSED" if passed else "FAILED",
            total=total,
            consistent=verdict.consistent,
            allowed=verdict.allowed_only,
            forbidden=verdict.forbidden,
            uncompared=getattr(verdict, "uncompared", 0),
        )
    )
    lines.append("")
    if getattr(verdict, "uncompared", 0):
        lines.append(
            "> 有 {0} 个周期没有对照侧（未对照）——未对照的周期不构成证据，"
            "本轮不能进入切换门禁。".format(getattr(verdict, "uncompared", 0))
        )
        lines.append("")

    return "\n".join(lines)


def _difference_lines(notes: List[WindowNote], classification: str) -> List[str]:
    """One line per difference of the given classification.

    Args:
        notes: one note per window.
        classification: ``"allowed"`` or ``"forbidden"``.

    Returns:
        The lines, in window order.
    """
    lines: List[str] = []
    for note in notes:
        for item in note.differences:
            if item.classification != classification:
                continue
            label = "解释位置" if classification == "allowed" else "位置"
            explanation = item.explanation or "未找到书面解释（需人工补齐）"
            lines.append(
                "- [{tag}] 周期 {index} · `{kind}` · {label} `{location}` · 解释：{explanation}".format(
                    tag=classification,
                    index=note.index,
                    kind=item.kind,
                    label=label,
                    location=item.location,
                    explanation=explanation,
                )
            )
    return lines


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def _report_input_error(message: str) -> None:
    """Print an input error on both channels.

    The gate reads stdout — that is where the verdict goes — and the
    log reads stderr, which is where diagnostics go.  The message names
    the file that is missing, so either channel is enough to act on.
    """
    print(message)
    print(f"scheduler_dryrun_diff: {message}", file=sys.stderr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Replay the recorded run, judge it, and write the comparison out.

    Args:
        argv: the command line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when the replay passed, ``1`` when it did not, and ``2``
        when it could not be run at all.
    """
    parser = argparse.ArgumentParser(
        prog="scheduler_dryrun_diff.py",
        description="回放一次 dry-run 对拍，判定并写出 diff.md",
    )
    parser.add_argument("--ticks", required=True, help="回放的 tick 记录（JSONL）")
    parser.add_argument("--windows", required=True, help="48 个窗口的记录（JSON）")
    parser.add_argument("--out", required=True, help="写出的对照报告 diff.md")
    args = parser.parse_args(argv)

    ticks_path = Path(args.ticks)
    windows_path = Path(args.windows)
    out_path = Path(args.out)

    missing = [path for path in (ticks_path, windows_path) if not path.is_file()]
    if missing:
        for path in missing:
            _report_input_error(f"缺少输入文件: {path}")
        return EXIT_INPUT

    try:
        ticks = _read_ticks(ticks_path)
        windows, prefix = _read_windows(windows_path)
    except InputError as exc:
        _report_input_error(str(exc))
        return EXIT_INPUT

    try:
        judge = _load_judge()
    except Exception as exc:  # pragma: no cover - the module is in the repo
        _report_input_error(f"无法加载判定器: {exc}")
        return EXIT_INPUT

    try:
        verdict = judge.evaluate_windows(windows)
        notes = _analyse(judge, windows, ticks, windows_path, prefix)
    except Exception as exc:
        # A window record the judge cannot read is an input error, not a
        # verdict: the replay did not run, so it has no answer to give.
        _report_input_error(f"窗口记录无法判定: {exc}")
        return EXIT_INPUT

    report = _render_report(
        judge, verdict, notes, ticks, ticks_path, windows_path
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    total = _total(verdict)
    print(
        "{verdict}: {total} 个周期, 完全一致 {consistent}, 仅含允许差异 {allowed}, "
        "含不允许差异 {forbidden}, 未对照 {uncompared}".format(
            verdict="PASSED" if verdict.passed else "FAILED",
            total=total,
            consistent=verdict.consistent,
            allowed=verdict.allowed_only,
            forbidden=verdict.forbidden,
            uncompared=getattr(verdict, "uncompared", 0),
        )
    )
    print(f"对照报告: {out_path}")
    return EXIT_PASS if verdict.passed else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
