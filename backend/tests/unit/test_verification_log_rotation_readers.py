"""The verification log readers see the rotated copies, not only the live file.

Background
----------
``backend/bounded_log.py`` gave the plan directory's log writers a bound:
the live log is rotated aside before it would grow past
``MAX_PLAN_LOG_BYTES`` and the rotated copies are numbered ``<name>.1``
… ``<name>.N`` — the **highest number holds the oldest lines**.

Task 3 moved the verification writers onto that bound, and a rotation
moves *old* lines out of the way: the live log keeps the newest lines
and everything before it now lives in a copy.  The three readers below
— ``get_round_logs``, ``get_verification_point_logs`` and
``_collect_screenshot_references`` — read only the live file, so a
rotation silently deletes the entries the reader was looking for: the
verification point that failed three rounds ago is not in the live log
anymore, and the report says nothing about it.

What the tests below pin
------------------------
* each of the three readers finds the entries that were rotated out;
* the order is chronological within one log file (``.3`` → ``.2`` →
  ``.1`` → the live file) and by file name across log files;
* a copy that is not there is skipped, and a copy that is torn costs
  that copy — never the call;
* a live file that is gone does not hide the copies that are left.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import bounded_log
from verification_persistence import VerificationPersistenceManager

# ``server`` first: it wires ``routes.verification`` in at the bottom of
# its own module, and importing the route module first would make the
# two import each other.
import server  # noqa: F401
from routes import verification as routes_verification


#: The round the readers are asked about.
ROUND = 1

#: The verification point the entries belong to.
VP_ID = "VP-001"


def _manager(tmp_path) -> Tuple[VerificationPersistenceManager, Path]:
    """A manager whose current round log is ``verification_1_<ts>.log``.

    The live log's name is the one ``start_round`` produces, so the
    readers' glob and the rotated copies' names agree with the writer's.
    """
    manager = VerificationPersistenceManager(tmp_path / "plan")
    live = manager.logs_dir / "verification_1_20260101_120000.log"
    manager._log_file = live
    return manager, live


def _entry(vp_id: str = VP_ID, marker: str = "") -> Dict[str, Any]:
    """One JSON-lines log entry, tagged with a marker to order it by."""
    return {
        "verification_point_id": vp_id,
        "event_type": "pytest_output",
        "timestamp": "2026-01-01T00:00:00",
        "data": {"marker": marker},
    }


def _screenshot_entry(relative_path: str, marker: str = "shot") -> Dict[str, Any]:
    """One screenshot event, in the shape the writer produces."""
    return {
        "event_type": "screenshot_saved",
        "timestamp": "2026-01-01T00:00:00",
        "data": {"relative_path": relative_path, "marker": marker},
    }


def _write(path: Path, entries: Iterable[Dict[str, Any]]) -> Path:
    """Write ``entries`` as JSON lines into ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )
    return path


def _copy(live: Path, index: int) -> Path:
    """The rotated copy ``<name>.<index>`` of the live log."""
    return live.parent / f"{live.name}.{index}"


def _markers(entries: List[Dict[str, Any]]) -> List[str]:
    """The marker of every entry, in the order the reader returned them."""
    return [
        entry.get("data", {}).get("marker")
        if isinstance(entry.get("data"), dict)
        else None
        for entry in entries
    ]


def _indexes_in(path: Path) -> List[int]:
    """The ``data.index`` of every entry in one log file."""
    indexes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line).get("data")
        if isinstance(data, dict) and isinstance(data.get("index"), int):
            indexes.append(data["index"])
    return indexes


def test_get_round_logs_reads_rotated_copies(tmp_path):
    """The round reader finds the entries that were rotated out."""
    manager, live = _manager(tmp_path)
    _write(live, [_entry(marker="live")])
    _write(_copy(live, 1), [_entry(marker="copy-1")])

    entries = manager.get_round_logs(ROUND)

    assert "copy-1" in _markers(entries), (
        "the rotated copy holds entries the reader must still find"
    )


def test_get_verification_point_logs_reads_rotated_copies(tmp_path):
    """The verification-point reader finds the entries that were rotated out."""
    manager, live = _manager(tmp_path)
    _write(live, [_entry(vp_id=VP_ID, marker="live")])
    _write(_copy(live, 2), [_entry(vp_id="VP-002", marker="copy-2")])

    entries = manager.get_verification_point_logs("VP-002")

    assert _markers(entries) == ["copy-2"], (
        "the rotated copy holds the verification point's entries"
    )


def test_collect_screenshot_references_reads_rotated_copies(tmp_path):
    """The screenshot collector finds the screenshots that were rotated out."""
    manager, live = _manager(tmp_path)
    _write(live, [])
    _write(
        _copy(live, 1),
        [_screenshot_entry("screenshots/verification_1_shot.png")],
    )

    screenshots = manager._collect_screenshot_references()

    assert screenshots == ["screenshots/verification_1_shot.png"], (
        "the rotated copy holds the screenshot references"
    )


def test_readers_skip_missing_and_corrupt_copies(tmp_path):
    """A copy that is not there and a copy that is torn cost that copy — not the call.

    The rotated copies are the writer's overflow, not the writer's
    contract: a plan that never rotates has none of them, and a copy
    that was torn by a kill holds half a line.  Neither may end the
    read — the entries the live file still holds are the answer.
    """
    manager, live = _manager(tmp_path)
    _write(
        live,
        [
            _entry(vp_id=VP_ID, marker="live"),
            _screenshot_entry("screenshots/live.png"),
        ],
    )
    _copy(live, 2).write_bytes(b"\xff\xfe not json at all\n")

    entries = manager.get_round_logs(ROUND)
    assert _markers(entries) == ["live", "shot"]

    vp_entries = manager.get_verification_point_logs(VP_ID)
    assert _markers(vp_entries) == ["live"]

    screenshots = manager._collect_screenshot_references()
    assert screenshots == ["screenshots/live.png"]


def test_readers_read_the_copies_oldest_first(tmp_path):
    """Within one log file the order is ``.3`` → ``.2`` → ``.1`` → the live file.

    The highest copy number holds the oldest lines, and the live file
    holds the newest — a rotation only moves old lines out of the way,
    so this order is the order the lines were written in.
    """
    manager, live = _manager(tmp_path)
    _write(live, [_entry(marker="live")])
    _write(_copy(live, 1), [_entry(marker="copy-1")])
    _write(_copy(live, 2), [_entry(marker="copy-2")])
    _write(_copy(live, 3), [_entry(marker="copy-3")])

    entries = manager.get_round_logs(ROUND)

    assert _markers(entries) == ["copy-3", "copy-2", "copy-1", "live"]


def test_readers_read_copies_when_the_live_file_is_gone(tmp_path):
    """A live file that is gone does not hide the copies that are left.

    The round cleanup archives the live log of an old round and removes
    it, and the copies it rotated out are still on disk — they hold the
    entries the reader is looking for.
    """
    manager, live = _manager(tmp_path)
    _write(_copy(live, 1), [_entry(marker="copy-1")])

    entries = manager.get_round_logs(ROUND)

    assert _markers(entries) == ["copy-1"], (
        "the copies of a log whose live file is gone still hold entries"
    )


def test_round_logs_are_ordered_by_file_name(tmp_path):
    """Across log files the order is the file name order."""
    manager, live = _manager(tmp_path)
    later = manager.logs_dir / "verification_1_20260101_130000.log"
    _write(later, [_entry(marker="later-file")])
    _write(live, [_entry(marker="earlier-file")])

    entries = manager.get_round_logs(ROUND)

    assert _markers(entries) == ["earlier-file", "later-file"]


def test_verification_point_logs_filter_the_rotated_copies(tmp_path):
    """A copy holds the entries of every point — the reader still filters it.

    The round log holds one line per verification point, so a rotated
    copy holds the lines of all of them.  Reading the copy must not
    turn it into a second, unfiltered answer: the reader is asked for
    one point and gets that point.
    """
    manager, live = _manager(tmp_path)
    _write(live, [_entry(vp_id=VP_ID, marker="live")])
    _write(
        _copy(live, 1),
        [
            _entry(vp_id="VP-002", marker="other-point"),
            _entry(vp_id=VP_ID, marker="wanted"),
            _entry(vp_id="VP-003", marker="another-point"),
        ],
    )

    entries = manager.get_verification_point_logs(VP_ID)

    assert _markers(entries) == ["wanted", "live"], (
        "the copy contributes this point's entries and nothing else"
    )


def test_readers_find_the_entries_the_real_writer_rotated_out(tmp_path, monkeypatch):
    """The reader and the writer agree on the layout.

    The copies in the tests above were written by hand, so they pin the
    reader against a layout the reader itself chose.  This one rotates
    the log the way the plan does — through the manager's own write
    path and the bounded writer underneath it — so a writer that
    renumbered its copies, or a reader that read them in the wrong
    order, is caught here rather than in a plan that ran for hours.
    """
    monkeypatch.setattr(bounded_log, "MAX_PLAN_LOG_BYTES", 300)

    manager = VerificationPersistenceManager(tmp_path / "plan")
    live = manager.start_round(1)

    for index in range(10):
        manager.write_verification_point_log(
            VP_ID, "pytest_output", {"index": index, "filler": "x" * 40}
        )

    rotated = live.parent / f"{live.name}.1"
    assert rotated.exists(), "the writer must have rotated the round log"

    live_indexes = _indexes_in(live)
    copy_indexes = _indexes_in(rotated)
    assert copy_indexes, "the rotated copy must hold entries of its own"

    entries = manager.get_round_logs(ROUND)
    indexes = [
        entry["data"]["index"]
        for entry in entries
        if isinstance(entry.get("data"), dict)
        and isinstance(entry["data"].get("index"), int)
    ]

    assert set(copy_indexes) <= set(indexes), (
        "the entries the writer rotated out must come back"
    )
    assert set(live_indexes) <= set(indexes), (
        "the entries the live log holds must come back too"
    )
    assert indexes == sorted(indexes), (
        "the copies are older than the live file, so they come first"
    )


# ---------------------------------------------------------------------------
# The route-side readers
# ---------------------------------------------------------------------------
#
# The readers above live in ``verification_persistence`` and answer
# "what did this round log say".  The three below live in
# ``routes/verification.py`` and answer the questions the progress
# endpoint asks: which VPs are running *right now*, when did this one
# start, and what is the newest round's state.
#
# They used to read the live file only, so a rotation deleted their
# answers — and what a rotation deletes is the *old* half of the log:
# the ``vp_start`` of a VP that is still running.  The consequence is
# not a missing row: ``running_vp_ids`` comes back empty, the card
# falls back to a stale terminal verdict, and the verification death
# sequence computes an empty in-flight set and kills nothing.

#: The round log the route-side readers are pointed at.
_ROUTE_LOG = "verification_2_20260101_130000.log"


def _plan(tmp_path) -> Path:
    """A plan directory with a ``logs/`` directory in it."""
    plan = tmp_path / "plan"
    (plan / "logs").mkdir(parents=True)
    return plan


def _vp_event(event_type: str, vp_id: str, ts: str) -> Dict[str, Any]:
    """One ``vp_start`` / ``vp_complete`` entry, in the writer's shape."""
    return {
        "event_type": event_type,
        "verification_point_id": vp_id,
        "timestamp": ts,
    }


def _write_log(path: Path, entries: Iterable[Dict[str, Any]]) -> Path:
    """Write ``entries`` as JSON lines into ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )
    return path


def _touch(path: Path, mtime: float) -> Path:
    """Pin ``path``'s modification time, so the readers' order is decided.

    The readers order the files by modification time (the round counter
    resets on restart, so the time is the only monotonic signal).  A
    test that leaves the times to the filesystem decides nothing.
    """
    os.utime(path, (mtime, mtime))
    return path


def _copy_of(live: Path, index: int) -> Path:
    """The rotated copy ``<name>.<index>`` of a live round log."""
    return live.parent / f"{live.name}.{index}"


def test_read_all_vp_starts_sees_the_rotated_copies(tmp_path):
    """A ``vp_start`` the rotation moved aside is still a running VP.

    This reader is what fills ``running_vp_ids`` on the progress
    endpoint.  A VP whose start line was rotated out of the live log
    is invisible to it, so the card reports nothing in flight while a
    sub-agent is running.
    """
    plan = _plan(tmp_path)
    live = plan / "logs" / _ROUTE_LOG
    _write_log(live, [_vp_event("vp_start", "VP-LIVE", "2026-01-01T13:00:00")])
    _write_log(
        _copy_of(live, 1),
        [_vp_event("vp_start", "VP-ROT", "2026-01-01T12:59:00")],
    )

    starts = routes_verification._read_all_vp_starts(plan)

    assert starts.get("VP-ROT") == "2026-01-01T12:59:00", (
        "the rotated copy holds a vp_start the reader must still see"
    )
    assert starts.get("VP-LIVE") == "2026-01-01T13:00:00", (
        "the live log's vp_start must still be seen too"
    )


def test_read_latest_vp_start_sees_the_rotated_copies(tmp_path):
    """``current_vp.started_at`` comes from a rotated copy, not only the live file."""
    plan = _plan(tmp_path)
    live = plan / "logs" / _ROUTE_LOG
    _write_log(live, [_vp_event("vp_start", "VP-LIVE", "2026-01-01T13:00:00")])
    _write_log(
        _copy_of(live, 1),
        [_vp_event("vp_start", "VP-ROT", "2026-01-01T12:59:00")],
    )

    started_at = routes_verification._read_latest_vp_start(plan, "VP-ROT")

    assert started_at == "2026-01-01T12:59:00", (
        "the rotated copy holds the vp_start the reader must still find"
    )


def test_read_all_vp_starts_lets_the_latest_write_win(tmp_path):
    """The newest write of a VP wins, and the newest write is in the live file.

    The reader scans the files oldest first, so a VP that started
    before the rotation and started again after it is reported with
    the later timestamp.
    """
    plan = _plan(tmp_path)
    live = plan / "logs" / _ROUTE_LOG
    _write_log(live, [_vp_event("vp_start", "VP-A", "2026-01-01T13:00:00")])
    _write_log(_copy_of(live, 1), [_vp_event("vp_start", "VP-A", "2026-01-01T12:00:00")])
    _touch(_copy_of(live, 1), 1000.0)
    _touch(live, 2000.0)

    starts = routes_verification._read_all_vp_starts(plan)

    assert starts.get("VP-A") == "2026-01-01T13:00:00", (
        "the live file holds the newest write of this VP"
    )


def test_read_latest_round_state_merges_the_copies_and_the_live_log(tmp_path):
    """A round split across a copy and the live file is one round.

    The ``vp_start`` of a VP is written early and its ``vp_complete``
    late, so a rotation can land exactly between the two.  A reader
    that reads one file sees half a VP: it looks like a VP that
    started and never finished, which is a VP that is running.  The
    round's files have to be read together, oldest first.
    """
    plan = _plan(tmp_path)
    live = plan / "logs" / _ROUTE_LOG
    _write_log(live, [_vp_event("vp_complete", "VP-LIVE", "2026-01-01T13:00:00")])
    _write_log(
        _copy_of(live, 1),
        [_vp_event("vp_start", "VP-ROT", "2026-01-01T12:59:00")],
    )

    activity, _judgment, _last = routes_verification._read_latest_round_state(plan)

    assert activity.get("VP-ROT", {}).get("start") == "2026-01-01T12:59:00", (
        "the copy's vp_start must be visible"
    )
    assert activity.get("VP-LIVE", {}).get("complete") == "2026-01-01T13:00:00", (
        "the live log's vp_complete must be visible"
    )


def test_read_latest_round_state_reads_the_copies_oldest_first(tmp_path):
    """Inside one round the copies come before the live file.

    The highest copy number holds the oldest lines and the live file
    holds the newest, so the last event the reader saw is the newest
    one.  Reading the files in the other order would report the
    round's oldest VP event as its most recent one.
    """
    plan = _plan(tmp_path)
    live = plan / "logs" / _ROUTE_LOG
    _write_log(live, [])
    _write_log(_copy_of(live, 1), [_vp_event("vp_complete", "VP-A", "2026-01-01T12:30:00")])
    _write_log(_copy_of(live, 2), [_vp_event("vp_start", "VP-A", "2026-01-01T12:00:00")])

    activity, _judgment, last_vp = routes_verification._read_latest_round_state(plan)

    assert activity.get("VP-A", {}).get("start") == "2026-01-01T12:00:00", (
        "the oldest copy's vp_start must be visible"
    )
    assert activity.get("VP-A", {}).get("complete") == "2026-01-01T12:30:00", (
        "the newer copy's vp_complete must be visible"
    )
    assert last_vp.get("event_type") == "vp_complete", (
        "the newest event of the round is the one the reader saw last"
    )


def test_readers_still_read_the_copies_when_the_live_file_is_gone(tmp_path):
    """A live file that is gone does not hide the copies that are left.

    The per-round cleanup archives the live log of an old round, and
    the copies it rotated out are still on disk — they hold the
    entries the reader is looking for.
    """
    plan = _plan(tmp_path)
    live = plan / "logs" / _ROUTE_LOG
    _write_log(
        _copy_of(live, 1),
        [_vp_event("vp_start", "VP-ROT", "2026-01-01T12:59:00")],
    )

    starts = routes_verification._read_all_vp_starts(plan)
    assert starts.get("VP-ROT") == "2026-01-01T12:59:00", (
        "the copies of a log whose live file is gone still hold entries"
    )

    assert routes_verification._read_latest_vp_start(plan, "VP-ROT") == (
        "2026-01-01T12:59:00"
    ), "the copies of a log whose live file is gone are still readable"

    activity, _judgment, _last = routes_verification._read_latest_round_state(plan)
    assert activity.get("VP-ROT", {}).get("start") == "2026-01-01T12:59:00", (
        "the round is still readable when only its copies are left"
    )


def test_read_latest_round_state_does_not_mix_earlier_rounds(tmp_path):
    """Only the newest round's files are read.

    The round counter resets when a verification restarts, so the
    newest round is the one whose files were written last — and the
    earlier round's files are not part of it.  A VP that ran in the
    earlier round is not running now.
    """
    plan = _plan(tmp_path)
    older = plan / "logs" / "verification_1_20260101_100000.log"
    newer = plan / "logs" / _ROUTE_LOG
    _write_log(older, [_vp_event("vp_start", "VP-OLD", "2026-01-01T10:00:00")])
    _write_log(newer, [_vp_event("vp_start", "VP-NEW", "2026-01-01T13:00:00")])
    _touch(older, 1000.0)
    _touch(newer, 2000.0)

    activity, _judgment, _last = routes_verification._read_latest_round_state(plan)

    assert "VP-NEW" in activity, "the newest round's VPs are the current ones"
    assert "VP-OLD" not in activity, (
        "the earlier round's VPs must not be mixed into the newest round"
    )


def test_route_readers_are_unchanged_when_nothing_rotated(tmp_path):
    """A plan that never rotated reads exactly what it read before.

    The negative control: the readers were changed to see the copies,
    not to see something else.  With one live log and no copies, the
    three readers answer what the live log says.
    """
    plan = _plan(tmp_path)
    live = plan / "logs" / _ROUTE_LOG
    _write_log(
        live,
        [
            _vp_event("vp_start", "VP-A", "2026-01-01T13:00:00"),
            _vp_event("vp_complete", "VP-A", "2026-01-01T13:05:00"),
        ],
    )

    starts = routes_verification._read_all_vp_starts(plan)
    assert starts == {"VP-A": "2026-01-01T13:00:00"}

    assert routes_verification._read_latest_vp_start(plan, "VP-A") == (
        "2026-01-01T13:00:00"
    )

    activity, _judgment, last_vp = routes_verification._read_latest_round_state(plan)
    assert activity == {
        "VP-A": {"start": "2026-01-01T13:00:00", "complete": "2026-01-01T13:05:00"}
    }
    assert last_vp == {
        "event_type": "vp_complete",
        "vp_id": "VP-A",
        "ts": "2026-01-01T13:05:00",
    }


def test_the_round_log_family_is_the_copies_oldest_first_then_the_live_file(tmp_path):
    """The family of one round log is ``.3`` → ``.2`` → ``.1`` → the live file.

    The order is the order the lines were written in: the highest copy
    number holds the oldest lines and the live file holds the newest.
    A reader that walked the family the other way would report the
    round's oldest event as its most recent one.
    """
    plan = _plan(tmp_path)
    live = plan / "logs" / _ROUTE_LOG
    for index in (1, 2, 3):
        _write_log(_copy_of(live, index), [])
    _write_log(live, [])

    families = routes_verification._round_log_families(plan / "logs")

    assert len(families) == 1, "one round log is one family"
    assert [path.name for path in families[0]] == [
        f"{_ROUTE_LOG}.3",
        f"{_ROUTE_LOG}.2",
        f"{_ROUTE_LOG}.1",
        _ROUTE_LOG,
    ], "the copies come oldest first and the live file comes last"


def test_the_route_readers_see_every_entry_the_bounded_writer_kept(tmp_path, monkeypatch):
    """The readers and the real writer agree: nothing that is on disk is lost.

    The copies in the tests above were written by hand, so they pin the
    readers against a layout the readers themselves chose.  This one
    rotates the round log the way the plan does — through the
    persistence manager's own write path and the bounded writer
    underneath it — and then asks the route-side readers for the
    ``vp_start`` of every VP.

    The bound is dropped so that a rotation really happens.  The
    entries the writer dropped past the bound are gone for good — that
    is the bound working — but every entry it *kept* is still on disk,
    and every one of them has to come back.
    """
    monkeypatch.setattr(bounded_log, "MAX_PLAN_LOG_BYTES", 2000)

    plan = tmp_path / "plan"
    manager = VerificationPersistenceManager(plan)
    live = manager.start_round(2)

    for index in range(60):
        manager.write_verification_point_log(
            f"VP-{index:03d}",
            "vp_start",
            {
                "title": "t" * 60,
                "method": "auto",
                "priority": "p",
                "expected_result": "r" * 60,
                "timeout_seconds": 3600,
            },
        )

    assert (live.parent / f"{live.name}.1").exists(), (
        "the writer must have rotated the round log"
    )

    on_disk = {
        entry["verification_point_id"]
        for path in sorted(live.parent.glob(f"{live.name}*"))
        for entry in (
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if entry.get("event_type") == "vp_start"
    }
    assert on_disk, "the writer must have kept some entries"

    starts = routes_verification._read_all_vp_starts(plan)

    assert set(starts) == on_disk, (
        "every vp_start the writer kept must come back — a reader that "
        "loses one of them loses a VP that is running"
    )
