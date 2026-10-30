"""Unit tests for the execution-log tail reader and the line normaliser.

What is under test
------------------
``backend/execution_log_tail.py`` provides the two capabilities the
status endpoint needs and did not have:

* a bounded read of the tail of ``plans/{plan_id}/execution.log``, and
* one line shape for whatever comes back, whichever source it came from.

The line shape is the point.  The log file stamps its timestamps as
naive UTC (the writer calls ``datetime.utcnow()``), while the
in-process lines carry whatever the console printer wrote.  A reader
that compared those two by string would read one instant as two
different moments, so every line leaves this module in the same shape:
``[<ISO-8601 with an explicit UTC offset>] <body>``.

The failure this module exists to prevent
------------------------------------------
``GET /api/execution/{plan_id}/status`` answers with an empty ``logs``
list whenever the process that started the run is not the process
answering the request.  The activity is on disk all the same; nothing
was reading it.  A missing log therefore has to degrade to an empty
list rather than to an error: the endpoint that renders a card cannot
fail because a file is absent, and it must not fail because a file
cannot be read.

Every case below is a pure function over a temporary file — no server,
no database, no subprocess.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from execution_log_tail import (
    MAX_TAIL_BYTES,
    MAX_TAIL_LINES,
    normalize_log_line,
    normalize_status_logs,
    read_status_log_tail,
    read_tail_lines,
)


#: A JSON line exactly as the logger writes one, with the naive-UTC
#: timestamp the writer produces.
DISK_LINE = json.dumps(
    {
        "ts": "2026-10-28T08:35:13.816773",
        "level": "INFO",
        "event": "task_completed",
        "message": "task 3 done",
    }
)

#: The same event as the console printer writes it.
MEM_LINE = "[2026-10-28T10:35:13] [INFO] task_completed: task 3 done"


def _write_lines(path: Path, count: int, prefix: str = "line") -> None:
    """Write ``count`` numbered lines, one per line, ending with a newline."""
    path.write_text(
        "".join(f"{prefix} {i}\n" for i in range(count)),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Reading the tail
# ---------------------------------------------------------------------------


def test_read_tail_returns_last_200_lines(tmp_path: Path) -> None:
    """A 300-line log yields exactly its last 200 lines."""
    log = tmp_path / "execution.log"
    _write_lines(log, 300)

    lines = read_tail_lines(log)

    assert len(lines) == 200
    # The tail is the END of the file, not an arbitrary window: the
    # first returned line is the 101st and the last is the 300th.
    assert lines[0] == "line 100"
    assert lines[-1] == "line 299"


def test_read_tail_drops_partial_first_line(tmp_path: Path) -> None:
    """A log past the byte cap yields whole lines only, never a fragment.

    The reader seeks backwards from the end of the file, so the seek
    lands inside a line.  That fragment is not a line and must not be
    handed to a caller that parses one line at a time.
    """
    log = tmp_path / "execution.log"
    padding = "x" * 1000
    _write_lines(log, 400, prefix=padding)
    assert log.stat().st_size > MAX_TAIL_BYTES

    lines = read_tail_lines(log)

    assert 0 < len(lines) <= MAX_TAIL_LINES
    # Every returned line is whole: the full marker, then its number.
    whole_line = re.compile(r"x{1000} \d+")
    for line in lines:
        assert whole_line.fullmatch(line), line
    # And the tail really is the end of the file.
    assert lines[-1] == f"{padding} 399"


def test_missing_or_unreadable_file_returns_empty(tmp_path: Path) -> None:
    """A log that cannot be read degrades to an empty list, never an error."""
    missing = tmp_path / "does-not-exist.log"
    assert read_tail_lines(missing) == []
    assert read_status_log_tail(missing) == []

    unreadable = tmp_path / "locked.log"
    unreadable.write_text("secret\n", encoding="utf-8")
    unreadable.chmod(0o000)
    try:
        # Root ignores the mode bits, so the permission case only means
        # anything for an unprivileged user.
        if os.geteuid() != 0:
            assert read_tail_lines(unreadable) == []
            assert read_status_log_tail(unreadable) == []
    finally:
        unreadable.chmod(0o600)


def test_empty_file_returns_empty_list(tmp_path: Path) -> None:
    """An empty log is an empty list, not a single empty line."""
    log = tmp_path / "execution.log"
    log.write_text("", encoding="utf-8")

    assert read_tail_lines(log) == []
    assert read_status_log_tail(log) == []


def test_a_directory_is_not_a_log(tmp_path: Path) -> None:
    """A path that is not a file degrades to an empty list."""
    directory = tmp_path / "plans"
    directory.mkdir()

    assert read_tail_lines(directory) == []
    assert read_status_log_tail(directory) == []


def test_tail_limits_are_honoured(tmp_path: Path) -> None:
    """``max_lines`` bounds how many lines come back."""
    log = tmp_path / "execution.log"
    _write_lines(log, 50)

    lines = read_tail_lines(log, max_lines=5)

    assert lines == ["line 45", "line 46", "line 47", "line 48", "line 49"]


def test_default_limits_match_the_contract() -> None:
    """The published constants are the ones the endpoint advertises."""
    assert MAX_TAIL_LINES == 200
    assert MAX_TAIL_BYTES == 262144


# ---------------------------------------------------------------------------
# Normalising one line
# ---------------------------------------------------------------------------


def test_json_line_normalized_to_utc_bracket() -> None:
    """A JSON log line becomes ``[naive-UTC-as-UTC] [LEVEL] event: message``."""
    assert normalize_log_line(DISK_LINE) == (
        "[2026-10-28T08:35:13.816773+00:00] [INFO] task_completed: task 3 done"
    )


def test_raw_line_timestamp_normalized() -> None:
    """A console line keeps its body and gains an explicit UTC offset."""
    assert normalize_log_line(MEM_LINE) == (
        "[2026-10-28T10:35:13+00:00] [INFO] task_completed: task 3 done"
    )
    # A space instead of the ``T`` separator is the same instant.
    assert normalize_log_line("[2026-10-28 10:35:13] hello") == (
        "[2026-10-28T10:35:13+00:00] hello"
    )


def test_line_without_timestamp_kept_verbatim() -> None:
    """A line that carries no timestamp is kept as it is, not dropped.

    A traceback is evidence: the reader must be able to see that the
    run died and how, so a line with no timestamp in it still comes
    back — with its text intact.
    """
    raw = "Traceback (most recent call last):"

    assert normalize_log_line(raw) == raw


def test_explicit_offsets_are_converted_to_utc() -> None:
    """``Z`` and ``+08:00`` are the same instant as the naive UTC they name."""
    assert normalize_log_line(
        '{"ts": "2026-10-28T08:35:13Z", "level": "INFO", '
        '"event": "e", "message": "m"}'
    ) == "[2026-10-28T08:35:13+00:00] [INFO] e: m"
    assert normalize_log_line(
        '{"ts": "2026-10-28T16:35:13+08:00", "level": "INFO", '
        '"event": "e", "message": "m"}'
    ) == "[2026-10-28T08:35:13+00:00] [INFO] e: m"


def test_json_line_with_missing_fields_is_padded_not_raised() -> None:
    """A JSON line missing fields is padded with empty strings."""
    line = json.dumps({"ts": "2026-10-28T08:35:13", "message": "only a message"})

    normalised = normalize_log_line(line)

    assert normalised.startswith("[2026-10-28T08:35:13+00:00] ")
    assert "only a message" in normalised


def test_body_newlines_are_flattened_to_spaces() -> None:
    """One line in, one line out — the caller parses line by line."""
    line = json.dumps(
        {
            "ts": "2026-10-28T08:35:13",
            "level": "ERROR",
            "event": "task_failed",
            "message": "first line\nsecond line",
        }
    )

    normalised = normalize_log_line(line)

    assert "\n" not in normalised
    assert normalised == (
        "[2026-10-28T08:35:13+00:00] [ERROR] task_failed: first line second line"
    )


def test_a_stamped_body_that_carries_a_break_keeps_its_stamp() -> None:
    """A break inside the body does not cost the line its timestamp."""
    normalised = normalize_log_line("[2026-10-28T10:35:13] first\nsecond")

    assert normalised == "[2026-10-28T10:35:13+00:00] first second"


def test_a_limit_that_is_not_a_number_returns_empty(tmp_path: Path) -> None:
    """A limit the reader cannot honour is not worth failing the caller over."""
    log = tmp_path / "execution.log"
    _write_lines(log, 10)

    assert read_tail_lines(log, max_lines=None) == []
    assert read_tail_lines(log, max_bytes=None) == []


# ---------------------------------------------------------------------------
# Normalising what the endpoint already holds
# ---------------------------------------------------------------------------


def test_inline_lines_take_precedence_in_normalize_status_logs() -> None:
    """The lines the endpoint already holds are the ones it keeps.

    The in-process lines are the freshest view of the run, so they win
    over anything that could be read from disk; what this function
    owes them is the same line shape as the disk fallback, not a
    replacement of them.
    """
    inline = [MEM_LINE]

    normalised = normalize_status_logs(inline)

    assert normalised == ["[2026-10-28T10:35:13+00:00] [INFO] task_completed: task 3 done"]


def test_normalize_status_logs_on_empty_input_is_empty() -> None:
    """No lines in means no lines out."""
    assert normalize_status_logs([]) == []
    assert normalize_status_logs(()) == []
    assert normalize_status_logs(None) == []


# ---------------------------------------------------------------------------
# The whole path: read the tail and normalise it
# ---------------------------------------------------------------------------


def test_read_status_log_tail_normalises_the_disk_lines(tmp_path: Path) -> None:
    """The disk fallback comes back in the same shape as the inline lines."""
    log = tmp_path / "execution.log"
    log.write_text(DISK_LINE + "\n" + MEM_LINE + "\n", encoding="utf-8")

    assert read_status_log_tail(log) == [
        "[2026-10-28T08:35:13.816773+00:00] [INFO] task_completed: task 3 done",
        "[2026-10-28T10:35:13+00:00] [INFO] task_completed: task 3 done",
    ]
