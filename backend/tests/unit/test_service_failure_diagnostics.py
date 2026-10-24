"""Pins for the diagnostics a service that did not come up must report.

What is under test
------------------
``backend/service_manager.py`` -- :func:`service_manager.start_service`
and the private helpers that dress up its two failure branches.

Why the detail needs four things
---------------------------------
The plan material (§3.4) asks for all of them whenever a declared
service fails to start:

  1. the child's **exit code**, when it has already exited;
  2. the **first error** in its log -- the first line carrying
     ``Traceback`` / ``Error`` / ``Exception``, plus the five lines
     before it -- because a service that dies during startup writes
     its traceback early and then floods the log with shutdown noise,
     so the tail alone hides the cause;
  3. the child's **pid / pgid**, so the operator knows which process
     to look at;
  4. the **descriptor count** it held, because the failure this was
     written for is an event loop dying of ``Bad file descriptor``.

The shape is fixed, so a reader (human or log scraper) can find the
same four items in every failure.

The degradations are pinned too, because a diagnostic that raises is
worse than no diagnostic: an unreadable log must read ``(none)`` and
an empty tail ``(empty)``, and a descriptor count that could not be
taken must be **omitted** -- a missing ``lsof`` is not a count of
zero, and reporting it as zero would name a descriptor exhaustion
that was never observed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402


def _decl(**overrides) -> ServiceDeclaration:
    base = dict(
        name="chart",
        port=3000,
        start_cmd="npx next dev -p 3000",
        cwd=".",
    )
    base.update(overrides)
    return ServiceDeclaration(**base)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "tmp").mkdir()
    return tmp_path


def _log_path(project: Path, name: str = "chart") -> Path:
    """Where ``start_service`` writes the service's log."""
    return project / "tmp" / f"pdt_service_{name}.log"


class _FakeProc:
    """A child that is either still running or already gone."""

    def __init__(self, pid: int, rc=None):
        self.pid = pid
        self._rc = rc

    def poll(self):
        return self._rc


def _stub_spawn(monkeypatch, proc, *, ready: bool):
    """Replace the spawn and the readiness probe with fixed answers.

    ``_detached_pgid_of`` is stubbed rather than ``_process_group_of``:
    the real one waits up to five seconds for a child that does not
    exist, and a test that sleeps is a test that hides a regression.

    ``lsof`` is let through to the real ``Popen``. The descriptor
    count is taken with ``subprocess.run``, which builds its own
    ``Popen`` -- so a stub that answered every spawn would turn the
    count into a stubbed answer, and the test would prove nothing
    about the count it claims to check.
    """
    real_popen = sm.subprocess.Popen

    def _popen(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "lsof":
            return real_popen(argv, *args, **kwargs)
        return proc

    monkeypatch.setattr(sm.subprocess, "Popen", _popen)
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: ready)
    monkeypatch.setattr(sm, "_detached_pgid_of", lambda pid: proc.pid)


# ---------------------------------------------------------------------------
# 1. The not-ready detail carries all four diagnostics
# ---------------------------------------------------------------------------


def test_not_ready_detail_has_pid_pgid_fds_and_first_error(project, monkeypatch):
    _log_path(project).write_text(
        "vp fixture ready\n"
        "Started server process [1]\n"
        "Traceback (most recent call last):\n"
        "  File \"app.py\", line 1, in <module>\n"
        "OSError: [Errno 9] Bad file descriptor\n"
    )
    _stub_spawn(monkeypatch, _FakeProc(24057), ready=False)
    monkeypatch.setattr(sm, "_fd_count", lambda pid: 37)

    runtime = sm.start_service(_decl(), project)

    assert runtime.ready is False
    assert "pid 24057" in runtime.detail
    assert "pgid 24057" in runtime.detail
    assert "fds 37" in runtime.detail
    # The error is already inside the tail window, so it is said once
    # and not repeated as a separate "first error at line N" clause.
    assert "first error at line" not in runtime.detail
    assert runtime.detail.count("OSError: [Errno 9] Bad file descriptor") == 1
    assert "OSError: [Errno 9] Bad file descriptor" in runtime.detail


# ---------------------------------------------------------------------------
# 2. A child that already exited says so
# ---------------------------------------------------------------------------


def test_exited_detail_carries_rc(project, monkeypatch):
    """The port answered but our child was gone -- its exit code is
    the one fact that says *why* it is gone."""
    _log_path(project).write_text(
        "Traceback (most recent call last):\n"
        "OSError: [Errno 9] Bad file descriptor\n"
    )
    _stub_spawn(monkeypatch, _FakeProc(24058, rc=9), ready=True)
    monkeypatch.setattr(sm, "_fd_count", lambda pid: None)

    runtime = sm.start_service(_decl(), project)

    assert runtime.ready is False
    assert "rc=9" in runtime.detail


def test_not_ready_detail_says_rc_none_while_the_child_lives(project, monkeypatch):
    _stub_spawn(monkeypatch, _FakeProc(24059), ready=False)
    monkeypatch.setattr(sm, "_fd_count", lambda pid: 12)

    runtime = sm.start_service(_decl(), project)

    assert "rc=None" in runtime.detail


# ---------------------------------------------------------------------------
# 3. The excerpt is the first marker and the five lines before it
# ---------------------------------------------------------------------------


def test_first_error_excerpt_picks_first_marker(tmp_path):
    log = tmp_path / "svc.log"
    lines = [f"info {index}" for index in range(1, 11)]
    lines += [
        "Traceback (most recent call last):",
        "  File \"app.py\", line 1, in <module>",
        "OSError: [Errno 9] Bad file descriptor",
        "ValueError: a later error",
    ]
    log.write_text("\n".join(lines) + "\n")

    found = sm._first_error_excerpt(log.read_text().splitlines(), 5)

    assert found is not None
    line_number, excerpt = found
    assert line_number == 11
    parts = excerpt.split(" | ")
    assert len(parts) == 6, excerpt
    assert parts[0] == "info 6"
    assert parts[-1] == "Traceback (most recent call last):"
    assert "ValueError" not in excerpt


# ---------------------------------------------------------------------------
# 4. An unreadable log degrades instead of raising
# ---------------------------------------------------------------------------


def test_missing_log_degrades_to_none(tmp_path, project, monkeypatch):
    missing = tmp_path / "not-there.log"

    assert sm._diagnosable_log(missing) == "(empty)"

    _stub_spawn(monkeypatch, _FakeProc(24060), ready=False)
    monkeypatch.setattr(sm, "_fd_count", lambda pid: 12)

    runtime = sm.start_service(_decl(), project)

    assert "(empty)" in runtime.detail
    assert "first error" not in runtime.detail


# ---------------------------------------------------------------------------
# 5. A descriptor count that could not be taken is None, and is omitted
# ---------------------------------------------------------------------------


def test_fd_count_none_when_lsof_missing(tmp_path, project, monkeypatch):
    """No ``lsof`` on the PATH means "not measurable", not "zero"."""
    empty_bin = tmp_path / "no-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    assert sm._fd_count(os.getpid()) is None

    _stub_spawn(monkeypatch, _FakeProc(24061), ready=False)

    runtime = sm.start_service(_decl(), project)

    assert "fds " not in runtime.detail
    assert "pid 24061" in runtime.detail
