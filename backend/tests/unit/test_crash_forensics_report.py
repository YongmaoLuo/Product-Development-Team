"""Unit tests for the crash-forensics report primitives.

What is under test
------------------
``backend/crash_forensics.py`` is the reporting half of the answer to
"what happened to the executor?".  When the executor exits non-zero or
is killed by a signal, nothing about the death survives: the plan's
flags come back empty, ``logs/executor_*.log`` stops half way through a
sentence, and there is no traceback anywhere.  The next question --
"was it the task that was running?" -- has no answer to be had.

This module is the primitive that builds, writes and reads back the
scene of the crash.  It is deliberately inert: it is not wired to the
executor yet, it collects no environment variables, reads no
credentials and opens no ``/proc`` file.  What it does is turn facts
that are already known -- an exit code, a pid, a list of log lines --
into one JSON document with a fixed set of keys, put that document on
disk under the plan's directory, and read the newest one back.

The contract the tests pin
--------------------------
* The file name carries a UTC timestamp with microsecond resolution, so
  two crashes in the same second do not collide and the newest file is
  the newest by name.
* ``last_events`` is the raw tail of ``execution.log``.  Raw: the lines
  are the text on disk, not a re-serialised and re-normalised version of
  it -- a forensics document that paraphrases its own evidence is not
  evidence.
* An exit code becomes a signal name when the process was killed, and
  the name is ``SIGKILL`` / ``SIGTERM`` / ``SIGINT`` rather than a
  number somebody has to look up.
* The report carries exactly fifteen top-level keys.  A crash report is
  read by a human under pressure; every key that is not on the list is
  a key nobody knows the meaning of.
* Writing is off-switchable, and when it is off nothing at all is
  written -- not even the directory.

Every case below is a pure function over a temporary directory: no
server, no database, no subprocess, and no dependence on the wall clock
(the one timestamp the tests need is passed in).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from crash_forensics import (
    FORENSICS_ENV,
    build_crash_report,
    crash_report_path,
    derive_active_task_id,
    describe_exit,
    forensics_enabled,
    latest_crash_report,
    tail_execution_events,
    write_crash_report,
)

#: The fifteen keys a crash report is allowed to carry.  Anything else
#: in the document is a key whose meaning nobody has written down.
REPORT_KEYS = {
    "exit_code",
    "signal",
    "signal_name",
    "killed_by",
    "pid",
    "argv",
    "started_at",
    "crashed_at",
    "uptime_seconds",
    "active_task_id",
    "last_events",
    "traceback",
    "faulthandler",
    "source",
    "plan_id",
}


def _write_execution_log(plan_dir: Path, lines: list[str]) -> Path:
    """Write ``lines`` to ``plan_dir/execution.log`` the way the logger does."""
    log_path = plan_dir / "execution.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def test_crash_report_path_is_utc_named(tmp_path: Path) -> None:
    """The report's name carries the crash time in UTC, to the microsecond."""
    moment = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)

    path = crash_report_path(tmp_path, now=moment)

    assert path.name == "executor_crash_20261108T120000000000Z.json"
    assert path.parent == tmp_path / "logs"

    # A moment without a timezone is read as UTC -- the convention the
    # log writer uses -- so the name is the same one either way.
    naive = crash_report_path(tmp_path, now=datetime(2026, 11, 8, 12, 0, 0))
    assert naive.name == "executor_crash_20261108T120000000000Z.json"


def test_tail_returns_last_20_raw_lines(tmp_path: Path) -> None:
    """The tail is the last twenty non-empty lines, exactly as they are on disk."""
    lines = [f"line-{index:02d} payload" for index in range(1, 26)]
    # A line with surrounding whitespace and a line with non-ASCII text:
    # both must survive the read untouched, because the tail is evidence.
    lines[9] = "  line-10 with   spacing  "
    lines[19] = "line-20 payload 中文"
    _write_execution_log(tmp_path, lines)

    tail = tail_execution_events(tmp_path)

    assert len(tail) == 20
    assert tail[-1] == lines[24]
    assert tail == lines[5:]
    assert len(tail_execution_events(tmp_path, limit=3)) == 3


def test_tail_missing_log_is_empty(tmp_path: Path) -> None:
    """A plan with no execution log yields no events, not an error."""
    assert tail_execution_events(tmp_path) == []

    # A log that is there but says nothing is the same answer.
    (tmp_path / "execution.log").write_text("", encoding="utf-8")
    assert tail_execution_events(tmp_path) == []
    (tmp_path / "execution.log").write_text("   \n\n", encoding="utf-8")
    assert tail_execution_events(tmp_path) == []

    # A limit that asks for nothing, and a limit that is not a number,
    # are both answered with nothing rather than with an error.
    assert tail_execution_events(tmp_path, limit=0) == []
    assert tail_execution_events(tmp_path, limit="not a number") == []


def test_describe_exit_maps_signal() -> None:
    """A negative return code is a signal, and the signal has a name."""
    assert describe_exit(-9) == {
        "exit_code": None,
        "signal": 9,
        "signal_name": "SIGKILL",
        "killed_by": "signal",
    }

    # A signal number the platform does not know keeps its number and
    # is named "SIG<n>"; it does not raise.
    assert describe_exit(-999)["signal_name"] == "SIG999"


def test_describe_exit_names_signal_15() -> None:
    """SIGTERM is named, not numbered."""
    described = describe_exit(-15)

    assert described["signal"] == 15
    assert described["signal_name"] == "SIGTERM"
    assert described["killed_by"] == "signal"


def test_describe_exit_names_signal_2() -> None:
    """SIGINT is named, not numbered."""
    described = describe_exit(-2)

    assert described["signal"] == 2
    assert described["signal_name"] == "SIGINT"
    assert described["killed_by"] == "signal"


def test_describe_exit_maps_positive_code() -> None:
    """A positive return code is an exit code, and no signal at all."""
    assert describe_exit(7) == {
        "exit_code": 7,
        "signal": None,
        "signal_name": None,
        "killed_by": "exit_code",
    }

    # The clean exit is the other end of the same family: an exit code
    # of zero, and no signal and no way of dying.
    assert describe_exit(0) == {
        "exit_code": 0,
        "signal": None,
        "signal_name": None,
        "killed_by": None,
    }

    # A return code the parent never learned is neither of them.
    assert describe_exit(None) == {
        "exit_code": None,
        "signal": None,
        "signal_name": None,
        "killed_by": None,
    }


def test_derive_active_task_id_reads_last_task_line() -> None:
    """The newest line that names a task wins; the others are skipped."""
    events = [
        json.dumps({"event": "task_started", "task_id": "3-1"}),
        json.dumps({"event": "task_progress", "task_id": "3-2"}),
        json.dumps({"event": "task_progress", "task_id": "3-3"}),
    ]

    assert derive_active_task_id(events) == "3-3"

    # A line that is not JSON, and a JSON line that names no task, are
    # skipped rather than ending the search.
    assert derive_active_task_id(["not json at all", json.dumps({"event": "x"})]) is None

    # The search runs backwards, so the lines it has to step over are
    # the ones behind the answer: a blank line, a line that is not JSON,
    # a JSON line that is not an object, and a task id that is empty.
    assert derive_active_task_id(
        [
            json.dumps({"task_id": "4-2"}),
            json.dumps({"task_id": None}),
            json.dumps([1, 2]),
            "   ",
            "not json at all",
        ]
    ) == "4-2"
    assert derive_active_task_id(
        [json.dumps({"task_id": "4-1"}), json.dumps({"task_id": "   "})]
    ) == "4-1"

    # A task id that is not a string is still a task.
    assert derive_active_task_id([json.dumps({"task_id": 7})]) == "7"

    # Nothing to read is not an answer.
    assert derive_active_task_id([]) is None
    assert derive_active_task_id(None) is None

    # A line that is not a line of text at all is not a task record.
    assert derive_active_task_id([{"task_id": "6-1"}]) is None


def test_report_keys_are_whitelisted(tmp_path: Path) -> None:
    """The report carries the fifteen whitelisted keys and nothing else."""
    report = build_crash_report(plan_dir=tmp_path, source="parent", plan_id="p1")

    assert set(report) == REPORT_KEYS

    # The same holds with no fields at all, and a field that is not on
    # the whitelist does not become a key of its own.
    assert set(build_crash_report()) == REPORT_KEYS
    assert set(build_crash_report(credential="secret")) == REPORT_KEYS

    # A field the caller states is the field the report carries; the
    # derivation only fills in what was not stated.
    stated = build_crash_report(
        last_events=[json.dumps({"task_id": "5-1"})],
        active_task_id="5-1",
        source="parent",
        plan_id="p1",
        pid=1234,
        argv=["cli.py"],
    )
    assert stated["last_events"] == [json.dumps({"task_id": "5-1"})]
    assert stated["active_task_id"] == "5-1"
    assert (stated["pid"], stated["argv"], stated["source"], stated["plan_id"]) == (
        1234,
        ["cli.py"],
        "parent",
        "p1",
    )
    # An events field that is not a list of lines is not events.
    assert build_crash_report(last_events=None)["last_events"] == []


def test_write_creates_logs_dir_and_roundtrips(tmp_path: Path, monkeypatch) -> None:
    """Writing makes the directory, lands the file, and reads back equal."""
    # The switch is off-switchable, so the environment is made explicit
    # here: with the variable absent, writing is on.
    monkeypatch.delenv(FORENSICS_ENV, raising=False)
    report = build_crash_report(source="parent", plan_id="p1", exit_code=1)

    path = write_crash_report(tmp_path, report)

    assert path is not None
    assert path.exists()
    assert path.parent == tmp_path / "logs"
    assert latest_crash_report(tmp_path) == report

    # A report that cannot be written is a missing report, not a second
    # failure: the write answers None and raises nothing.
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "logs").write_text("a file where a directory belongs", encoding="utf-8")
    assert write_crash_report(blocked, report) is None

    # The newest report is the one read back; a report that cannot be
    # parsed is skipped rather than hiding the one before it.  A plan of
    # its own, so the report written above does not take part.
    newest_dir = tmp_path / "newest"
    logs = newest_dir / "logs"
    logs.mkdir(parents=True)
    older = logs / "executor_crash_20260101T000000000000Z.json"
    newer = logs / "executor_crash_20260102T000000000000Z.json"
    older.write_text(json.dumps({"n": 1}), encoding="utf-8")
    newer.write_text(json.dumps({"n": 2}), encoding="utf-8")
    assert latest_crash_report(newest_dir) == {"n": 2}
    newer.write_text("{not json", encoding="utf-8")
    assert latest_crash_report(newest_dir) == {"n": 1}
    older.write_text("{not json", encoding="utf-8")
    assert latest_crash_report(newest_dir) is None
    assert latest_crash_report(tmp_path / "no-such-plan") is None


def test_disabled_env_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    """With the switch off, nothing is written -- not even the directory."""
    for value in ("0", "false", "off"):
        monkeypatch.setenv(FORENSICS_ENV, value)
        report = build_crash_report(source="parent", plan_id="p1")

        assert forensics_enabled() is False, value
        assert write_crash_report(tmp_path, report) is None, value
        assert list(tmp_path.rglob("*")) == [], value
