"""The plan-log writer is bounded, and it rotates the way the readers expect.

Background
----------
``logs/repair_*.log`` was a bare file: the executor subprocess's stdout
was redirected into it and nothing ever bounded it, so one long run grew
a single log past 5.4 GB.  A log that only grows is not a log, it is a
disk-filling incident waiting for the next run — and the reader that
has to make sense of it (``pause_state`` looking for a task→session
binding, the verification readers walking the per-round logs) then has
to read gigabytes to find the one line it wanted.

So the writer is bounded: the live log is rotated aside when it would
grow past ``MAX_PLAN_LOG_BYTES``, the rotated copies are numbered
``<name>.1`` … ``<name>.N`` with the **highest number the oldest**, and
the copy that falls past ``PLAN_LOG_BACKUP_COUNT`` is dropped.

The numbering is not this module's invention.  ``ExecutionLogger`` has
rotated ``execution.log`` that way since it was written, and
``pause_state._rotated_log_paths`` reads the rotated copies back in
that order — oldest first, so a binding written after a rotation wins
over the one it replaced.  A writer that numbered its copies
differently would silently lose the bindings a resume needs, so the
layout is pinned here against both of them: one test compares the
paths this module produces with the paths the reader produces, and the
defaults are pinned against ``ExecutionLogger``'s.

What the tests below pin
------------------------
* the live file and every rotated copy stay within the bound — the
  point of the module;
* a single oversized write is capped rather than allowed to blow the
  bound on its own;
* the copies are pruned in the right order, and the oldest content is
  the content that goes;
* the rotated paths are listed oldest-first, and the copies that do
  not exist are listed too (a reader must be able to look for them
  without asking whether they are there first);
* the defaults are read from the module constants **when the call
  happens**, so a test can change the bound without re-importing;
* writers that share a path share a lock, so concurrent writes cannot
  interleave inside a line;
* a write that fails on disk costs that chunk and nothing else: the
  pipe keeps being drained, because a full disk must not turn into a
  deadlocked child.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

import bounded_log
from bounded_log import (
    MAX_PLAN_LOG_BYTES,
    PLAN_LOG_BACKUP_COUNT,
    RotatingLogSink,
    append_bounded_line,
    pump_stream_to_sink,
    rotated_log_paths,
)
from execution_logger import ExecutionLogger
from pause_state import _rotated_log_paths


def _rotated(path: Path, index: int) -> Path:
    """The ``index``-th rotated copy of ``path`` (``<name>.<index>``)."""
    return path.parent / f"{path.name}.{index}"


# ---------------------------------------------------------------------------
# The bound
# ---------------------------------------------------------------------------

def test_rotates_at_limit_and_prunes_oldest(tmp_path):
    """The live log is rotated before it crosses the bound, and the
    oldest copy is the one that goes when the count is exceeded."""
    p = tmp_path / "repair.log"
    limit = 100
    sink = RotatingLogSink(p, max_bytes=limit, backup_count=2)
    lines = [f"{mark}" * 60 + "\n" for mark in ("1", "2", "3", "4")]

    sink.write(lines[0])
    assert p.stat().st_size == len(lines[0])

    # The second line does not fit next to the first: the live file is
    # rotated aside and the new line starts a fresh live file.
    sink.write(lines[1])
    assert p.stat().st_size == len(lines[1])
    assert _rotated(p, 1).read_text() == lines[0]

    sink.write(lines[2])
    assert _rotated(p, 1).read_text() == lines[1]
    assert _rotated(p, 2).read_text() == lines[0]

    # The fourth line pushes the oldest copy past the backup count, so
    # the first line is gone and only the three most recent remain.
    sink.write(lines[3])
    assert _rotated(p, 2).read_text() == lines[1]
    assert _rotated(p, 1).read_text() == lines[2]
    assert p.read_text() == lines[3]
    assert not _rotated(p, 3).exists()

    # Nothing on disk is over the bound — that is the whole point.
    for candidate in (p, _rotated(p, 1), _rotated(p, 2)):
        assert candidate.stat().st_size <= limit


def test_oversized_single_write_is_capped(tmp_path):
    """A single write that is bigger than the bound is truncated to it.

    Without the cap, one write would put the live file over the bound
    on its own — and the bound is what keeps the disk from filling up.
    """
    p = tmp_path / "repair.log"
    limit = 100
    sink = RotatingLogSink(p, max_bytes=limit, backup_count=2)

    sink.write("z" * (limit * 10))
    assert p.stat().st_size <= limit

    # The writer keeps working afterwards, and stays bounded.
    sink.write("tail\n")
    assert p.stat().st_size <= limit
    for candidate in (p, _rotated(p, 1), _rotated(p, 2)):
        if candidate.exists():
            assert candidate.stat().st_size <= limit


def test_empty_write_leaves_the_file_untouched(tmp_path):
    """An empty write changes nothing — not the file, not the copies."""
    p = tmp_path / "repair.log"
    sink = RotatingLogSink(p, max_bytes=100, backup_count=2)
    sink.write("a" * 40 + "\n")

    before = p.stat().st_size
    sink.write("")

    assert p.stat().st_size == before
    assert not _rotated(p, 1).exists()


def test_defaults_are_read_from_the_module_constants_at_call_time(
    tmp_path, monkeypatch
):
    """The default bound is read when the call happens, not at import.

    A test that wants a small bound must not have to re-import the
    module to get it, and a bound that was captured once at import time
    would silently ignore the constant it is supposed to be reading.
    """
    p = tmp_path / "repair.log"
    monkeypatch.setattr(bounded_log, "MAX_PLAN_LOG_BYTES", 100)
    monkeypatch.setattr(bounded_log, "PLAN_LOG_BACKUP_COUNT", 2)

    sink = RotatingLogSink(p)  # no explicit limits
    sink.write("a" * 60 + "\n")
    sink.write("b" * 60 + "\n")
    assert _rotated(p, 1).exists()

    # A later change of the constant is picked up by the next call.
    monkeypatch.setattr(bounded_log, "MAX_PLAN_LOG_BYTES", 50)
    sink.write("c" * 60 + "\n")
    assert p.stat().st_size <= 50


def test_module_defaults_match_the_execution_logger():
    """The defaults are the ones ``ExecutionLogger`` already uses.

    Two writers with two different bounds would produce two different
    layouts for the same kind of file, and a reader that walks the
    rotated copies would have to know which writer produced them.
    """
    assert MAX_PLAN_LOG_BYTES == ExecutionLogger.MAX_BYTES
    assert PLAN_LOG_BACKUP_COUNT == ExecutionLogger.BACKUP_COUNT


# ---------------------------------------------------------------------------
# The layout
# ---------------------------------------------------------------------------

def test_rotated_log_paths_oldest_first(tmp_path, monkeypatch):
    """The rotated copies are listed oldest first, missing ones included.

    Oldest first is the order a reader has to use: a rotation only
    moves *old* lines out of the way, so the live log's line is the
    newest and has to be read last.  A copy that does not exist is
    listed anyway — most plans never rotate at all, and a reader that
    had to ask "is it there?" before looking would need a stat() per
    candidate.
    """
    p = tmp_path / "repair.log"
    monkeypatch.setattr(bounded_log, "PLAN_LOG_BACKUP_COUNT", 2)

    paths = rotated_log_paths(p)
    assert paths == [_rotated(p, 2), _rotated(p, 1)]
    assert not any(candidate.exists() for candidate in paths)

    # An explicit depth overrides the module constant.
    assert rotated_log_paths(p, backup_count=3) == [
        _rotated(p, 3),
        _rotated(p, 2),
        _rotated(p, 1),
    ]


def test_rotation_layout_matches_execution_logger(tmp_path):
    """The names this module produces are the names the reader expects.

    ``pause_state._rotated_log_paths`` is the reader that looks for a
    task→session binding in the rotated copies of ``execution.log``.  If
    the two disagreed on a name, the reader would stop finding the
    oldest bindings the day the writer's layout changed — and it would
    fail silently, because a missing binding looks exactly like a task
    that was never bound.
    """
    p = tmp_path / "execution.log"

    assert rotated_log_paths(p) == _rotated_log_paths(p)
    assert [candidate.name for candidate in rotated_log_paths(p)] == [
        "execution.log.3",
        "execution.log.2",
        "execution.log.1",
    ]


# ---------------------------------------------------------------------------
# The append path
# ---------------------------------------------------------------------------

def test_append_bounded_line_rotates(tmp_path, monkeypatch):
    """``append_bounded_line`` is bounded exactly like the sink.

    The verification writers append one line at a time; they must not
    need a sink object to get the bound.
    """
    p = tmp_path / "verification.log"
    monkeypatch.setattr(bounded_log, "MAX_PLAN_LOG_BYTES", 100)
    monkeypatch.setattr(bounded_log, "PLAN_LOG_BACKUP_COUNT", 2)

    append_bounded_line(p, "a" * 60)
    assert p.read_text() == "a" * 60 + "\n"

    append_bounded_line(p, "b" * 60)
    assert p.read_text() == "b" * 60 + "\n"
    assert _rotated(p, 1).read_text() == "a" * 60 + "\n"
    assert _rotated(p, 1).stat().st_size <= 100


def test_append_bounded_line_and_the_sink_share_one_lock_per_path(
    tmp_path, monkeypatch
):
    """Both writers take the same lock for the same path.

    Two writers on one file without one lock interleave: one rotates
    the file the other is halfway through writing, and the line that
    was being written lands in a rotated copy — or nowhere.
    """
    p = tmp_path / "repair.log"
    requested = []
    real_lock_for = bounded_log._lock_for

    def spy(path):
        requested.append(Path(path))
        return real_lock_for(path)

    monkeypatch.setattr(bounded_log, "_lock_for", spy)

    sink = RotatingLogSink(p, max_bytes=1000, backup_count=1)
    append_bounded_line(p, "hello")

    # Both went through the same path key, so they share the lock.
    assert requested
    assert {str(candidate) for candidate in requested} == {str(p)}
    assert bounded_log._lock_for(p) is bounded_log._lock_for(p)
    assert bounded_log._lock_for(p) is not bounded_log._lock_for(
        tmp_path / "other.log"
    )


def test_concurrent_writers_to_one_path_stay_bounded_and_intact(tmp_path):
    """Writers that share a path are serialised, and the lines survive.

    Every line is written whole: no line is torn by another writer's
    rotation, and no file on disk is over the bound.
    """
    p = tmp_path / "repair.log"
    limit = 200
    payload = "w" * 40 + "\n"
    failures = []

    def worker():
        try:
            sink = RotatingLogSink(p, max_bytes=limit, backup_count=2)
            for _ in range(25):
                sink.write(payload)
        except Exception as exc:  # pragma: no cover - the assert reports it
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    for candidate in [p] + rotated_log_paths(p):
        if not candidate.exists():
            continue
        assert candidate.stat().st_size <= limit
        # Every line is whole: the writers never interleaved.
        assert all(
            line == "w" * 40 for line in candidate.read_text().splitlines()
        )


# ---------------------------------------------------------------------------
# The pump
# ---------------------------------------------------------------------------

def test_pump_discards_a_failed_chunk_and_keeps_reading(tmp_path):
    """A write that fails costs that chunk and nothing else.

    The pipe still has to be drained: a child that is blocked writing
    into a pipe nobody reads is a child that never finishes, and the
    bound on the log is not worth a hang.
    """

    class FlakySink:
        def __init__(self):
            self.calls = 0
            self.kept = []

        def write(self, chunk):
            self.calls += 1
            if self.calls % 2 == 1:
                raise OSError("no space left on device")
            self.kept.append(chunk)

    stream = io.BytesIO(b"x" * 1000)
    sink = FlakySink()
    written = pump_stream_to_sink(stream, sink, chunk_size=100)

    # Every chunk was offered, and the stream was read to EOF.
    assert sink.calls == 10
    assert len(sink.kept) == 5
    assert written == 500
    assert stream.read() == b""


def test_pump_reads_to_eof_when_every_write_fails(tmp_path):
    """Even when the log cannot be written at all, the pipe is drained."""
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("x", encoding="utf-8")
    sink = RotatingLogSink(
        blocker / "repair.log", max_bytes=100, backup_count=1
    )

    stream = io.BytesIO(b"y" * 500)
    written = pump_stream_to_sink(stream, sink, chunk_size=100)

    assert written == 0
    assert stream.read() == b""  # the stream was consumed to EOF