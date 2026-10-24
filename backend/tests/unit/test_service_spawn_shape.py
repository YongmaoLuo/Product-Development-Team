"""Pins for the shape of the service spawn.

What is under test
------------------
The way :mod:`service_manager` creates the process of a declared
service: the argv, the keyword arguments handed to ``Popen``, and the
session the child ends up in.

Why the shape needs a pin
-------------------------
The root-cause document (``docs/development/service-spawn-root-cause.zh.md``)
measured the spawn's shape and found it *not* to be the variable:
``start_new_session`` failed 5/5 with it on and 5/5 with it off.  What
was the variable is what the spawn hands over -- the environment's
``PDT_SECRET_FD_*`` variables -- so the fix changed that and left the
shape alone.  A shape that is not the variable is exactly the kind of
thing that gets "improved" later: someone puts ``start_new_session``
back because it looks like it belongs there, and the service stops
starting.  These tests are the thing that notices.

The shape is read from the document, not copied here
----------------------------------------------------
The document names one mechanism on its ``- 机制：`` line, out of the
four it knows how to talk about.  The expected shape is derived from
that token, so the pin and the document cannot drift apart: if the
mechanism changes, the test follows it and fails on the *new* shape
rather than on a stale copy of the old one.

The document is published in two files, one per language: the Chinese
page carries the contract, and the English page says the same thing in
English.  The pin reads the Chinese one, and checks that the English
one names the same shape.

The pins
--------
1. The spawn's shape is the shape the document names.
2. The declared command reaches the spawn verbatim -- one argv
   element, never re-parsed and never re-quoted.
3. A real spawned child runs in a session of its own.
4. The mechanism this module acts on is the mechanism the document
   names.

Run them with:

    cd backend && .venv/bin/python3 -m pytest tests/unit/test_service_spawn_shape.py
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The document that locked the mechanism in -- the Chinese page,
#: which is the contract the follow-up work reads.
_DOC_PATH = (
    _REPO_ROOT / "docs" / "development" / "service-spawn-root-cause.zh.md"
)

#: The English page of the same document.
_EN_DOC_PATH = (
    _REPO_ROOT / "docs" / "development" / "service-spawn-root-cause.md"
)

#: The line the document names its mechanism on.
_MECHANISM_LINE = re.compile(
    r"^\s*-\s*机制[:：]\s*(\S+)\s*$", re.MULTILINE
)

#: The same line in the document's English half.
_ENGLISH_MECHANISM_LINE = re.compile(
    r"^\s*-\s*Mechanism:\s*(\S+)\s*$", re.MULTILINE
)

#: The four shapes the document may name.
_MECHANISMS: Tuple[str, ...] = (
    "wrapper",
    "stdin-handle",
    "posix-spawn",
    "unchanged",
)

#: The candidate whose row carries the numbers about the session.
_SESSION_CANDIDATE = "start_new_session"


# ---------------------------------------------------------------------------
# Reading the document
# ---------------------------------------------------------------------------


def _doc_text() -> str:
    """The root-cause document's Chinese text."""
    return _DOC_PATH.read_text(encoding="utf-8")


def _en_doc_text() -> str:
    """The same document's English text."""
    return _EN_DOC_PATH.read_text(encoding="utf-8")


def _documented_mechanism() -> str:
    """The mechanism the document names.

    One token, and only one: a document that names two mechanisms has
    not decided what the fix is, and the pins below would be guessing.
    """
    found = _MECHANISM_LINE.findall(_doc_text())
    assert found, (
        f"no '- 机制：<token>' line in {_DOC_PATH.name}"
    )
    assert len(set(found)) == 1, (
        f"the document names more than one mechanism: {found!r}"
    )
    return found[0]


def _candidate_cells(candidate: str) -> List[str]:
    """The count cells the candidate table gives *candidate*.

    The table's rows are ``| candidate | on | off | verdict |``; the
    counts are the two cells in the middle.
    """
    text = _doc_text()
    section = re.search(
        r"^##\s+候选失败率\s*$(.*?)(?=^##\s|\Z)", text,
        re.MULTILINE | re.DOTALL,
    )
    if section is None:
        return []
    for line in section.group(1).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells and cells[0] == candidate:
            return cells[1:3]
    return []


# ---------------------------------------------------------------------------
# Recording what the spawn path asked for
# ---------------------------------------------------------------------------


class _FakeProc:
    """The process object a recorded spawn hands back."""

    pid = 999997
    returncode = None

    def poll(self) -> None:
        return None


class _Spawn:
    """What the spawn path handed to the process-creation call."""

    def __init__(self) -> None:
        self.argv: Optional[List[str]] = None
        self.kwargs: Dict[str, Any] = {}
        self.posix_spawn_calls: List[Tuple[Any, ...]] = []

    @property
    def spawned_argv(self) -> List[str]:
        """The argv, whichever way the process was created."""
        if self.argv is not None:
            return self.argv
        if self.posix_spawn_calls:
            return list(self.posix_spawn_calls[-1][1])
        return []


def _install_recorders(monkeypatch) -> _Spawn:
    """Record what the spawn path asks for, without starting anything."""
    record = _Spawn()

    def _fake_popen(argv, **kwargs):
        record.argv = list(argv)
        record.kwargs = dict(kwargs)
        return _FakeProc()

    def _fake_posix_spawn(path, argv, env, **kwargs):
        record.posix_spawn_calls.append(
            (path, list(argv), dict(kwargs))
        )
        # ``os.posix_spawn`` answers with the new process' pid.
        return _FakeProc.pid

    monkeypatch.setattr(sm.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(os, "posix_spawn", _fake_posix_spawn)
    return record


def _decl(**overrides) -> ServiceDeclaration:
    base = dict(
        name="spawn-shape",
        port=3000,
        start_cmd="npx next dev -p 3000",
        cwd=".",
    )
    base.update(overrides)
    return ServiceDeclaration(**base)


def _free_port() -> int:
    """A port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_recording(
    monkeypatch, tmp_path, **overrides
) -> Tuple[_Spawn, Any]:
    """Start a service with the spawn path recorded rather than run.

    ``wait_until_ready`` is stubbed so the readiness probe does not
    decide the outcome, and ``_await_exit`` is stubbed because it waits
    two seconds for a process that -- being a recording -- never exits.
    """
    record = _install_recorders(monkeypatch)
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: True)
    monkeypatch.setattr(
        sm, "_await_exit", lambda proc, timeout: (False, None)
    )
    decl = _decl(**overrides)
    runtime = sm.start_service(decl, tmp_path)
    return record, runtime


# ---------------------------------------------------------------------------
# The shapes the four mechanisms have
# ---------------------------------------------------------------------------


def _wrapper_code_lines(source: str) -> List[str]:
    """The wrapper's own lines -- comments dropped.

    A commented-out ``os.setsid()`` is not a call to it, so the pins
    look at lines the wrapper *runs*, not at lines it mentions.
    """
    return [
        line.strip() for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _assert_wrapper_shape(record: _Spawn, docstring: str) -> None:
    """``wrapper``: a wrapper program that makes the session itself."""
    argv = record.spawned_argv
    assert argv, "the spawn path created no process at all"
    assert "start_new_session" not in record.kwargs, (
        "Popen was asked for a new session as well: the wrapper calls "
        "setsid() itself, and a child that is already a group leader "
        "gets EPERM from its own setsid()"
    )
    assert argv[0] == (sys.executable or "/usr/bin/python3"), (
        f"the wrapper does not run under this interpreter: {argv[0]!r}"
    )
    assert argv[1] == "-c", (
        f"the wrapper is not a program of its own: {argv[1:2]!r}"
    )
    lines = _wrapper_code_lines(argv[2])
    assert "os.setsid()" in lines, (
        f"the wrapper does not call os.setsid(), so the child shares "
        f"this process' session: {argv[2]!r}"
    )
    assert any(
        sm._SECRET_FD_ENV_PREFIX in line for line in lines
    ), (
        "the wrapper does not know about the descriptor variables, so "
        "the child inherits a descriptor number that is not its own"
    )
    assert any(
        line.startswith("del os.environ") or "environ.pop(" in line
        for line in lines
    ), (
        "the wrapper names the descriptor variables but does not remove "
        "them from the environment it hands on"
    )


def _assert_stdin_handle_shape(record: _Spawn, docstring: str) -> None:
    """``stdin-handle``: the child's input is an already-open handle."""
    stdin = record.kwargs.get("stdin")
    assert stdin is not None, "the spawn path gave the child no stdin"
    assert stdin is not subprocess.DEVNULL, (
        "the child's input is /dev/null, not an open handle"
    )
    assert hasattr(stdin, "fileno"), (
        f"the child's input is not a handle: {stdin!r}"
    )
    assert not getattr(stdin, "closed", False), (
        "the handle the child was given is already closed"
    )
    assert stdin.fileno() >= 0, (
        "the handle the child was given has no file descriptor"
    )


def _assert_posix_spawn_shape(record: _Spawn, docstring: str) -> None:
    """``posix-spawn``: ``os.posix_spawn`` is asked for a new session."""
    assert record.posix_spawn_calls, (
        "the document locks in posix-spawn, but the spawn still goes "
        "through subprocess.Popen"
    )
    _path, _argv, kwargs = record.posix_spawn_calls[-1]
    assert kwargs.get("setsid") is True, (
        f"os.posix_spawn was not asked for a new session: {kwargs!r}"
    )


def _assert_unchanged_shape(record: _Spawn, docstring: str) -> None:
    """``unchanged``: the shape stays, and the numbers say why."""
    assert record.kwargs.get("start_new_session") is True, (
        "the shape was to stay as it was, but Popen was not asked for "
        "a new session"
    )
    cells = _candidate_cells(_SESSION_CANDIDATE)
    assert cells, (
        f"the document's candidate table has no {_SESSION_CANDIDATE} row"
    )
    for cell in cells:
        assert cell in docstring, (
            f"this test does not cite the table number {cell!r} for "
            f"{_SESSION_CANDIDATE}, so it does not say what the shape "
            "is allowed to be"
        )
    assert _SESSION_CANDIDATE in docstring, (
        f"this test does not name {_SESSION_CANDIDATE} in its docstring"
    )


#: What each mechanism's spawn has to look like.
_SHAPE_ASSERTERS: Dict[str, Callable[[_Spawn, str], None]] = {
    "wrapper": _assert_wrapper_shape,
    "stdin-handle": _assert_stdin_handle_shape,
    "posix-spawn": _assert_posix_spawn_shape,
    "unchanged": _assert_unchanged_shape,
}


# ---------------------------------------------------------------------------
# 1. The spawn's shape is the shape the document names
# ---------------------------------------------------------------------------


def test_popen_shape_matches_documented_mechanism(tmp_path, monkeypatch):
    """The spawn's shape is the one the root-cause document locked in.

    The document's candidate table measured the session: with
    ``start_new_session`` on the service failed 5/5 times, and with it
    off it failed 5/5 times -- the session is not the variable, so the
    fix changed *what the spawn hands over* (the environment) and left
    the shape alone.  That is the shape this pins: a
    ``start_new_session`` that quietly comes back, or a wrapper whose
    ``setsid()`` disappears, is the regression the table says cannot
    happen.

    The expected shape is read from the document's ``- 机制：`` line
    rather than written out here, so the pin follows the document
    instead of keeping a second copy of it.
    """
    mechanism = _documented_mechanism()
    assert mechanism in _SHAPE_ASSERTERS, (
        f"no shape is pinned for mechanism {mechanism!r}"
    )

    record, _runtime = _start_recording(
        monkeypatch, tmp_path, port=_free_port()
    )

    _SHAPE_ASSERTERS[mechanism](
        record, test_popen_shape_matches_documented_mechanism.__doc__ or ""
    )


# ---------------------------------------------------------------------------
# 2. The declared command reaches the spawn verbatim
# ---------------------------------------------------------------------------


def test_argv_preserves_start_cmd_verbatim(tmp_path, monkeypatch):
    """The declared command is handed over as it stands.

    One argv element, never re-parsed and never re-quoted: a command
    that is re-parsed on the way to the spawn changes what it means
    (quoting, ``&&``, ``$VAR``), and a service that starts is a service
    that started *the command that was declared*.
    """
    start_cmd = "npx next dev -p 3000 --label 'a b'"

    record, _runtime = _start_recording(
        monkeypatch, tmp_path, port=_free_port(), start_cmd=start_cmd
    )

    argv = record.spawned_argv
    assert argv, "the spawn path created no process at all"
    assert len(shlex.split(start_cmd)) > 1, (
        "the command under test has to have more than one token, or "
        "a re-parsed argv would look the same as a verbatim one"
    )
    assert argv[-1] == start_cmd, (
        f"the declared command was not handed over verbatim: {argv[-1]!r}"
    )
    assert all(start_cmd not in part for part in argv[:-1]), (
        f"the declared command appears more than once in the argv: {argv!r}"
    )


# ---------------------------------------------------------------------------
# 3. A real child runs in a session of its own
# ---------------------------------------------------------------------------


def _kill_group(pgid: Optional[int], pid: Optional[int]) -> None:
    """Tear a spawned child down: its process group, then the child."""
    if not pid:
        return
    try:
        own_group = os.getpgid(0)
    except OSError:
        own_group = None
    if pgid and pgid > 1 and pgid != own_group:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.waitpid(pid, 0)
    except (ChildProcessError, OSError):
        pass


def test_child_runs_in_its_own_session(tmp_path, monkeypatch):
    """A real spawn: the child ends up in a session of its own.

    The shape is only worth anything if it produces the session it
    promises: a service that shares the backend's session dies with
    the backend, which is the whole reason the service is spawned
    detached.

    Two things are checked, and neither of them can be satisfied by a
    skip:

    * The production path's own bookkeeping.  ``start_service`` records
      the child's process group only once the child leads a group of
      its own, so ``pgid == pid`` is the production path saying the
      separation happened.  This does not depend on the child still
      being alive when the assertion runs.
    * A real child (``sleep 2``), started through the argv the
      production path uses and watched while it is alive.

    Two things can keep the session from being *observed*, and only
    one of them is a reason to skip: a platform without
    ``os.getsid``/``os.getpid``, and a child that exited cleanly
    before it could be looked at.  A child that died on the way, or
    one that never left this process' session, is a failure -- a skip
    there would hide exactly the regression this test exists for.
    """
    if not hasattr(os, "getsid") or not hasattr(os, "getpid"):
        pytest.skip(
            "this platform has no os.getsid/os.getpid, so a child's "
            "session cannot be inspected"
        )

    # The readiness probe is not what is under test, and it must not
    # decide the outcome: stub it so the spawn path returns at once.
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: False)

    decl = _decl(
        name="sleeper", port=_free_port(), start_cmd="sleep 2",
    )

    # --- the production path -------------------------------------------
    runtime = sm.start_service(decl, tmp_path)
    try:
        assert runtime.pid, (
            f"the spawn produced no pid: {runtime.detail!r}"
        )
        assert runtime.pgid == runtime.pid, (
            "the spawned service did not lead a process group of its "
            f"own (pgid={runtime.pgid!r}, pid={runtime.pid!r}); it "
            "shares this process' group, so it would not outlive the "
            f"process that started it. Log: {_log_tail(tmp_path, decl)!r}"
        )
    finally:
        _kill_group(runtime.pgid, runtime.pid)

    # --- a real child, watched while it is alive ------------------------
    proc = subprocess.Popen(
        sm._detached_argv("sleep 2"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        own_sid = os.getsid(0)
        deadline = time.monotonic() + 1.5
        sid: Optional[int] = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break          # the child died; reported below
            try:
                candidate = os.getsid(proc.pid)
            except (ProcessLookupError, PermissionError):
                break
            if candidate != own_sid:
                sid = candidate
                break
            time.sleep(0.01)

        if sid is None:
            code = proc.poll()
            if code:
                raise AssertionError(
                    f"the spawned child died (exit code {code}) before "
                    "it could be seen in a session of its own"
                )
            if code == 0:
                pytest.skip(
                    "the spawned child exited before its session could "
                    "be observed"
                )
            raise AssertionError(
                "the spawned child never left this process' session, so "
                "it would not outlive the process that started it"
            )

        assert sid != own_sid, (
            "the child shares this process' session, so it would not "
            "outlive the process that started it"
        )
    finally:
        _kill_group(proc.pid, proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass


def _log_tail(project_dir: Path, decl: ServiceDeclaration) -> str:
    """The tail of the service's log file, for a failure message."""
    try:
        content = Path(
            project_dir / "tmp" / f"pdt_service_{decl.name}.log"
        ).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return " | ".join(content[-15:])


# ---------------------------------------------------------------------------
# 4. The mechanism acted on is the mechanism the document names
# ---------------------------------------------------------------------------


def test_mechanism_token_matches_doc():
    """The mechanism this module pins is the one the document names."""
    token = _documented_mechanism()

    assert token in _MECHANISMS, (
        f"mechanism {token!r} is not one of {_MECHANISMS!r}"
    )
    assert set(_SHAPE_ASSERTERS) == set(_MECHANISMS), (
        "every mechanism the document may name needs a pinned shape; "
        f"pinned: {sorted(_SHAPE_ASSERTERS)!r}"
    )
    assert token in _SHAPE_ASSERTERS, (
        f"no shape is pinned for mechanism {token!r}"
    )

    # The document says it twice -- once in Chinese, once in English --
    # and the two versions have to name the same shape.
    english = _ENGLISH_MECHANISM_LINE.findall(_en_doc_text())
    if english:
        assert set(english) == {token}, (
            f"the English page names {english!r}, the Chinese page "
            f"names {token!r}"
        )
