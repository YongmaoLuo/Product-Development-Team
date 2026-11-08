"""Unit tests for the crash guard -- the wiring that catches a crash.

What is under test
------------------
``backend/crash_forensics.py`` has two halves.  The first half (the
report primitives) knows how to turn facts into a JSON document and
put it on disk.  This module tests the second half:
:class:`crash_forensics.CrashGuard`, the object that *notices* a
crash and hands those facts to the primitives.  Three ways for a run
to end badly, and each of them has to leave a report behind: an
uncaught exception (the traceback is the evidence), a signal (the
Python stack is the evidence, and the only way to get it out of a
process that is being torn down is ``faulthandler``), and a plain
non-zero exit (the exit code is the evidence).

The contract the tests pin
--------------------------
* **The report is written once.**  A guard that fires twice -- an
  exception that is caught and then the process dies anyway -- must not
  leave two reports behind; the second trigger answers ``None``.
* **A clean exit writes nothing.**  Exit code zero is not a crash.
* **The guard does not take over the process's ``faulthandler``.**
  ``faulthandler`` is process-global state: pytest installs one, the
  CI watchdog registers a SIGUSR1 handler on it, and a caller that
  enabled it chose a file on purpose.  The guard only enables it when
  the process has none, and hands the process back the way it found it
  on ``close()``.
* **A broken ``faulthandler`` is a missing field, not a missing
  report.**  If the platform has no ``faulthandler``, or enabling it
  raises, the report is still written -- with no stack in it.
* **A trigger before ``install()`` still writes.**  The guard degrades
  to "started at the moment of the crash"; it does not refuse to work.
* **The switch still switches.**  ``PDT_CRASH_FORENSICS=0`` means
  nothing at all is written -- not even the directory.
* **Nothing raises.**  Every one of these methods is called from a
  process that is already in trouble, so a failure inside the guard is
  a missing report, not a second failure.

Every case below is a pure function over a temporary directory: no
server, no database, no subprocess, and no dependence on the wall clock
(the one timestamp the tests need is passed in).
"""

from __future__ import annotations

import faulthandler
import json
import os
from pathlib import Path

import pytest

import crash_forensics
from crash_forensics import FORENSICS_ENV, CrashGuard


def _read_report(path: Path) -> dict:
    """Read back the report a guard wrote."""
    return json.loads(path.read_text(encoding="utf-8"))


def _crash_files(plan_dir: Path) -> list[Path]:
    """Every crash report under ``plan_dir``."""
    return sorted((plan_dir / "logs").glob("executor_crash_*.json"))


def test_exception_writes_traceback(tmp_path: Path, monkeypatch) -> None:
    """An uncaught exception leaves the traceback behind."""
    monkeypatch.delenv(FORENSICS_ENV, raising=False)
    guard = CrashGuard(tmp_path, "p1", argv=["cli.py"])
    guard.install()
    try:
        path = guard.on_exception(RuntimeError("boom"))
    finally:
        guard.close()

    assert path is not None
    report = _read_report(path)
    assert report["killed_by"] == "exception"
    assert "RuntimeError" in report["traceback"]
    # The facts the parent process is the only one to know.
    assert report["plan_id"] == "p1"
    assert report["source"] == "executor"
    assert report["pid"] == os.getpid()
    assert report["argv"] == ["cli.py"]


def test_exit_code_writes_file(tmp_path: Path, monkeypatch) -> None:
    """A non-zero exit leaves the exit code behind."""
    monkeypatch.delenv(FORENSICS_ENV, raising=False)
    guard = CrashGuard(tmp_path, "p1", argv=["cli.py"])
    guard.install()
    try:
        path = guard.on_exit_code(7)
    finally:
        guard.close()

    assert path is not None
    report = _read_report(path)
    assert report["exit_code"] == 7
    assert report["killed_by"] == "exit_code"
    # It was an exit, not a signal: there is no signal to name.
    assert report["signal"] is None
    assert report["signal_name"] is None
    assert report["source"] == "executor"


def test_exit_zero_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    """A clean exit is not a crash and leaves no report behind."""
    monkeypatch.delenv(FORENSICS_ENV, raising=False)
    guard = CrashGuard(tmp_path, "p1")
    guard.install()
    try:
        assert guard.on_exit_code(0) is None
    finally:
        guard.close()

    assert _crash_files(tmp_path) == []
    # Not even the directory: nothing at all was written.
    assert not (tmp_path / "logs").exists()


def test_signal_writes_faulthandler_stack(tmp_path: Path, monkeypatch) -> None:
    """A signal leaves the Python stack behind, and the signal is named."""
    monkeypatch.delenv(FORENSICS_ENV, raising=False)
    guard = CrashGuard(tmp_path, "p1", argv=["cli.py"])
    guard.install()
    try:
        path = guard.on_signal(15)
    finally:
        guard.close()

    assert path is not None
    report = _read_report(path)
    assert report["signal"] == 15
    assert report["signal_name"] == "SIGTERM"
    assert report["killed_by"] == "signal"
    assert report["exit_code"] is None
    # The stack is the whole point of the signal path: without it the
    # report says only "it was killed", which is not an answer.
    assert isinstance(report["faulthandler"], str)
    assert report["faulthandler"].strip()


def test_signal_is_written_once(tmp_path: Path, monkeypatch) -> None:
    """A second trigger answers None and leaves no second report."""
    monkeypatch.delenv(FORENSICS_ENV, raising=False)
    guard = CrashGuard(tmp_path, "p1")
    guard.install()
    try:
        first = guard.on_signal(15)
        second = guard.on_signal(15)
    finally:
        guard.close()

    assert first is not None
    assert second is None
    assert len(_crash_files(tmp_path)) == 1


def test_close_restores_faulthandler_state(tmp_path: Path, monkeypatch) -> None:
    """The process's ``faulthandler`` state is the same after close()."""
    monkeypatch.delenv(FORENSICS_ENV, raising=False)
    before = faulthandler.is_enabled()

    guard = CrashGuard(tmp_path, "p1")
    guard.install()
    after_install = faulthandler.is_enabled()
    guard.close()
    after_close = faulthandler.is_enabled()

    assert after_install == before
    assert after_close == before


def test_disabled_env_suppresses_all_triggers(tmp_path: Path, monkeypatch) -> None:
    """With the switch off, no trigger writes anything."""
    monkeypatch.setenv(FORENSICS_ENV, "0")
    guard = CrashGuard(tmp_path, "p1")
    guard.install()
    try:
        assert guard.on_exception(RuntimeError("boom")) is None
        assert guard.on_signal(15) is None
        assert guard.on_exit_code(7) is None
    finally:
        guard.close()

    assert _crash_files(tmp_path) == []
    assert list(tmp_path.rglob("executor_crash_*")) == []


def test_only_the_first_successful_trigger_writes(tmp_path: Path, monkeypatch) -> None:
    """A guard writes one report, whichever trigger comes first."""
    monkeypatch.delenv(FORENSICS_ENV, raising=False)
    guard = CrashGuard(tmp_path, "p1")
    guard.install()
    try:
        first = guard.on_exit_code(3)
        second = guard.on_exception(RuntimeError("boom"))
        third = guard.on_signal(15)
    finally:
        guard.close()

    assert first is not None
    assert second is None
    assert third is None
    assert len(_crash_files(tmp_path)) == 1


def test_install_enables_faulthandler_when_the_process_has_none(
    tmp_path: Path, monkeypatch
) -> None:
    """With no ``faulthandler`` in the process, the guard installs one."""
    calls: list[str] = []
    monkeypatch.setattr(faulthandler, "is_enabled", lambda: False)
    monkeypatch.setattr(
        faulthandler, "enable", lambda *args, **kwargs: calls.append("enable")
    )
    monkeypatch.setattr(
        faulthandler, "disable", lambda *args, **kwargs: calls.append("disable")
    )

    guard = CrashGuard(tmp_path, "p1")
    guard.install()
    assert calls == ["enable"]

    guard.close()
    assert calls == ["enable", "disable"]


def test_install_leaves_an_existing_faulthandler_alone(
    tmp_path: Path, monkeypatch
) -> None:
    """A ``faulthandler`` the process already has is not touched."""
    calls: list[str] = []
    monkeypatch.setattr(faulthandler, "is_enabled", lambda: True)
    monkeypatch.setattr(
        faulthandler, "enable", lambda *args, **kwargs: calls.append("enable")
    )
    monkeypatch.setattr(
        faulthandler, "disable", lambda *args, **kwargs: calls.append("disable")
    )

    guard = CrashGuard(tmp_path, "p1")
    guard.install()
    guard.close()

    assert calls == []


def test_broken_faulthandler_degrades_but_still_writes(
    tmp_path: Path, monkeypatch
) -> None:
    """A platform without ``faulthandler`` loses the stack, not the report."""
    monkeypatch.delenv(FORENSICS_ENV, raising=False)
    monkeypatch.setattr(faulthandler, "is_enabled", lambda: False)

    def _explode(*args, **kwargs):
        raise RuntimeError("no faulthandler on this platform")

    monkeypatch.setattr(faulthandler, "enable", _explode)

    guard = CrashGuard(tmp_path, "p1")
    guard.install()
    try:
        path = guard.on_signal(15)
    finally:
        guard.close()

    assert path is not None
    report = _read_report(path)
    assert report["killed_by"] == "signal"
    assert report["signal_name"] == "SIGTERM"
    assert report["faulthandler"] is None


def test_trigger_without_install_still_writes(tmp_path: Path, monkeypatch) -> None:
    """A trigger before ``install()`` writes, with the moment of the crash."""
    monkeypatch.delenv(FORENSICS_ENV, raising=False)
    guard = CrashGuard(tmp_path, "p1")

    path = guard.on_exit_code(9)

    assert path is not None
    report = _read_report(path)
    assert report["exit_code"] == 9
    assert report["pid"] == os.getpid()
    # Started at the moment of the crash: it ran for no time at all.
    assert report["started_at"] is not None
    assert report["uptime_seconds"] == 0.0


def test_close_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Closing twice is not an error."""
    monkeypatch.delenv(FORENSICS_ENV, raising=False)
    guard = CrashGuard(tmp_path, "p1")
    guard.install()
    guard.close()
    guard.close()


def test_internal_failure_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    """A failure inside the guard is a missing report, not a second failure."""

    def _explode(*args, **kwargs):
        raise OSError("the disk is on fire")

    monkeypatch.delenv(FORENSICS_ENV, raising=False)
    monkeypatch.setattr(crash_forensics, "write_crash_report", _explode)

    guard = CrashGuard(tmp_path, "p1")
    guard.install()
    try:
        assert guard.on_exit_code(7) is None
    finally:
        guard.close()
