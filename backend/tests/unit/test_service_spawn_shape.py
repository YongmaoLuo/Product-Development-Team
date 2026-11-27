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
5. A probe the spawn path makes is not the service's own spawn: the
   capture holds the **service's** spawn, even after the start path has
   run a probe of its own.  The CI gate's own spawn registry does not
   turn into a second spawn, because the recorder tells the service's
   spawn apart from every other subprocess by its shape, not by the
   order the calls came in.

The pid scope of the signals this file sends
--------------------------------------------
This file signals processes, so it has to say which ones.  The
``pid_scope`` of every signal sent from here is **the child process
this test spawned, and that child's own process group** -- never a
machine-wide scan, and never a pid this file did not start.

Two things pin that scope down:

* the pids and pgids that reach ``_kill_group`` are the ones the
  spawn path handed back in the runtime it had just created for this
  test -- ``runtime.pid`` / ``runtime.pgid`` for the declared
  service, and the pid of the ``Popen`` handle this test opened for
  the child it watches.  Nothing else can get in: those values are
  not read out of a process table, they are the handles of the
  processes this test started;
* ``_kill_group`` refuses to signal this process' own group.  It
  compares the candidate pgid against ``os.getpgid(0)`` and skips the
  group kill when the two are the same, so a mis-read pgid costs
  the stray child its life and never this process' siblings.

The reaper's vocabulary appears here -- the spawn registry this file
names, and the verdicts that registry holds -- because the spawn
path really does talk to the reaper.  That vocabulary is not a
licence to signal anything: the two pids above are the whole scope.

Run them with:

    cd backend && .venv/bin/python3 -m pytest tests/unit/test_service_spawn_shape.py
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
import process_identity as identity  # noqa: E402
import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402
from tests.unit.test_service_manager import _free_port  # noqa: E402

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


def _is_the_service_spawn(argv: Any) -> bool:
    """Is this argv the spawn of the declared service?

    The recorder stands in for ``subprocess.Popen``, and *every*
    subprocess in this process goes through ``Popen`` -- including the
    ``ps`` probe that writing the spawn registry costs.  That probe is
    one call to ``Popen`` with a very different argv, and a recorder
    that read "the call it caught" would read the probe instead of the
    spawn: the pin would then fail on every run that merely happens to
    be a CI gate run, and the failure would say that the spawn's shape
    changed when nothing about it did.

    The spawn is therefore told apart by its shape.  The detached
    wrapper runs under this interpreter and is a program of its own --
    that is the shape ``service_manager._detached_argv`` builds, and it
    is the shape no other subprocess in this process has.  The probes
    are the production's business, and the production is not what this
    test is about: they are handed to the real ``Popen`` rather than
    recorded as the service's spawn.  Their own argv gives them away
    anyway -- it is the ``ps`` binary's own path (an absolute one,
    because the reaper resolves ``ps`` once and keeps it), and its last
    element is the pid that was probed.
    """
    argv = list(argv or [])
    return (
        len(argv) > 2
        and argv[0] == (sys.executable or "/usr/bin/python3")
        and argv[1] == "-c"
        and argv[2] == sm._DETACHED_WRAPPER_SOURCE
    )


class _FakeProc:
    """The process object a recorded spawn hands back.

    It answers the whole ``subprocess`` protocol, not just ``poll``:
    ``subprocess.run`` — which is how a census is taken — builds a
    process through the same ``Popen`` this recorder replaces, and it
    needs ``communicate``, ``poll`` and the context-manager protocol to
    finish.  A recorder that only answers ``poll`` turns a census into
    an exception, and an exception on the spawn path is worse than the
    census: it unwinds the caller that asked for it.
    """

    pid = 999997
    returncode = None

    def __init__(self, argv: Optional[List[str]] = None) -> None:
        self.argv = list(argv or [])
        #: ``subprocess.run`` reads this back when it builds its result.
        self.args = list(argv or [])

    def poll(self) -> None:
        return None

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self, input=None, timeout=None):
        return ("", "")

    def __enter__(self) -> "_FakeProc":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _Spawn:
    """What the spawn path handed to the process-creation call."""

    def __init__(self) -> None:
        self.calls: List[Tuple[List[str], Dict[str, Any]]] = []
        self.posix_spawn_calls: List[Tuple[Any, ...]] = []
        #: The command the service was declared with, when the caller knows it.
        self.declared = ""
        #: The spawns that were **not** the service's own -- the probes
        #: the spawn path makes around it.  Kept apart from the
        #: service's spawn so that a probe is never read as the service.
        self.probe_argvs: List[List[str]] = []
        #: Every service spawn the capture took, in order.
        self.wrapper_spawns: List[List[str]] = []

    def service_call(self, declared: str = "") -> Optional[Tuple[List[str], Dict[str, Any]]]:
        """The call that started the **declared** service.

        The spawn path is allowed to start more than one process -- a
        helper of its own, a probe, anything -- and a recorder that
        takes "the last one" is a recorder that will one day watch a
        helper and call it the service.  That is not a hypothetical:
        the census ``ps`` the registry used to run was exactly such a
        helper, and every assertion about the service's argv was
        really an assertion about the census.

        The service is the spawn that carries the declared command, so
        that is what is selected.  When the declared command is not
        known, the shape is the other way of telling -- the spawn that
        is the service's own and not one of the path's probes.  A
        third way, "the last call", is not used: it is the one that
        watches a helper and calls it the service.
        """
        wanted = declared or self.declared
        if wanted:
            for argv, kwargs in self.calls:
                if argv and argv[-1] == wanted:
                    return argv, kwargs
        spawns = self.spawns
        if spawns:
            return spawns[-1]
        return None

    @property
    def spawns(self) -> List[Tuple[List[str], Dict[str, Any]]]:
        """The calls that are the service's own spawn, picked by shape.

        Every call the recorder caught is kept -- the probe included --
        because a recorder that threw the others away would be hiding
        them instead of telling them apart.
        """
        return [
            call for call in self.calls if _is_the_service_spawn(call[0])
        ]

    @property
    def spawned_argv(self) -> List[str]:
        """The argv, whichever way the process was created."""
        call = self.service_call()
        if call is not None:
            return list(call[0])
        if self.posix_spawn_calls:
            return list(self.posix_spawn_calls[-1][1])
        return []

    @property
    def spawned_kwargs(self) -> Dict[str, Any]:
        """The keyword arguments the service's own spawn was given."""
        call = self.service_call()
        return dict(call[1]) if call is not None else {}

    @property
    def kwargs(self) -> Dict[str, Any]:
        """The keyword arguments, read the flat way.

        The same answer as :attr:`spawned_kwargs`, under the name the
        pins that watch one spawn read.
        """
        return self.spawned_kwargs


def _is_service_spawn(argv: List[str]) -> bool:
    """Is this argv the service's own spawn?

    The service is started through the detached wrapper: the
    interpreter, ``-c``, and the wrapper's own source — which is the
    source that calls ``setsid()``.  Anything else the spawn path asks
    for is a **census**: a helper process the path derives in order to
    learn something about the process it just started.

    The distinction matters because a census is a second process on the
    spawn path, and the spawn path's *shape* is what this file pins.  A
    recorder that took "the last process" for "the service" would read
    the census as the service.
    """
    return (
        len(argv) >= 3
        and argv[1] == "-c"
        and "setsid" in argv[2]
    )


def _is_spawn_by_the_interpreter(argv: Optional[Sequence[str]]) -> bool:
    """True when *argv* is the spawn of the service itself.

    The spawn path probes the machine around the spawn it makes: it
    reads the owner's start time (``ps -o lstart= -p ...``) before the
    spawn, and -- when a spawn registry is named -- it reads the
    started process' start time again after it.  Those probes go
    through ``subprocess`` as well, so a recorder that keeps the last
    call it saw keeps a probe as often as it keeps the service.

    The service is started by the interpreter -- the wrapper runs
    under it -- and a probe is a system tool.  That is what tells the
    two apart, and it is the only thing that does: the probe's argv
    has the same shape as the service's otherwise.

    This is the weaker of the two judgements about "the service's
    spawn": it says which *program* started the process, where
    :func:`_is_the_service_spawn` says which of the spawns carries
    the detached wrapper.  The recorder tells them apart by the
    wrapper's shape; this one says what a probe is.

    The name is not the one base side gave it: this branch already
    had a helper called ``_is_service_spawn``, and it answers the
    stronger question (the wrapper's shape), so the two judgements
    keep their own names rather than shadowing each other.
    """
    if not argv:
        return False
    return argv[0] == (sys.executable or "/usr/bin/python3")


def _install_recorders(monkeypatch, declared: str = "") -> _Spawn:
    """Record what the spawn path asks for, without starting anything."""
    record = _Spawn()
    record.declared = declared
    real_popen = sm.subprocess.Popen

    def _fake_popen(argv, **kwargs):
        # Every call the recorder caught is kept -- the probe included
        # -- because a recorder that threw the others away would be
        # hiding them instead of telling them apart.
        record.calls.append((list(argv), dict(kwargs)))
        if not _is_the_service_spawn(argv):
            # Not the service: a probe of the production's own.  It is
            # handed to the real ``Popen`` so that the production sees
            # what it would see, and so that the capture holds the
            # service's spawn and nothing else.  The probe's own argv
            # is kept as well, so a pin can say that the probe really
            # happened instead of passing by never seeing one.
            record.probe_argvs.append(list(argv))
            return real_popen(argv, **kwargs)
        record.wrapper_spawns.append(list(argv))
        return _FakeProc()

    def _fake_posix_spawn(path, argv, env, **kwargs):
        record.posix_spawn_calls.append(
            (path, list(argv), dict(kwargs))
        )
        # ``os.posix_spawn`` answers with the new process' pid.
        return _FakeProc.pid

    def _watch_the_census_door(module) -> None:
        """Record what the census asks for, through the door it uses.

        The census does not go through the module-level
        ``subprocess.Popen`` this recorder replaces: it goes through the
        ``Popen`` its own module captured at import time (see
        ``process_identity._REAL_POPEN``), because the census is an
        observation point and an observation point a fixture can
        replace is not one -- a census that vanished from the record is
        a census that hid a second process.

        So the recorder watches **that** door as well.  A recorder that
        watched only the replaced one would never see the probe, and the
        pin that says "a probe is not the service" would pass by never
        seeing one.  The census still runs for real: the watcher hands
        the work to the door it found.
        """
        real = module._REAL_POPEN

        def _watcher(argv, *args, **kwargs):
            record.calls.append((list(argv), dict(kwargs)))
            record.probe_argvs.append(list(argv))
            return real(argv, *args, **kwargs)

        monkeypatch.setattr(module, "_REAL_POPEN", _watcher)

    monkeypatch.setattr(sm.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(os, "posix_spawn", _fake_posix_spawn)
    # 2026-11-19（合并 base 之后）：两侧的意图都要留下——普查不得被
    # 模块级 ``subprocess.Popen`` 的替身劫持（本分支的机制：普查走
    # 各模块导入期捕获的 ``_REAL_POPEN``），而普查留下的痕迹又必须
    # 录得到（base 侧「探针不是服务」那条判断的前提：它要真的看见
    # 一次 ``ps``）。这两条并不冲突：录身多看一道门——普查真正走的
    # 那道门——替身仍然只拦模块级那一道。
    _watch_the_census_door(reaper)
    _watch_the_census_door(identity)
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


def _start_recording(
    monkeypatch, tmp_path, **overrides
) -> Tuple[_Spawn, Any]:
    """Start a service with the spawn path recorded rather than run.

    ``wait_until_ready`` is stubbed so the readiness probe does not
    decide the outcome, and ``_await_exit`` is stubbed because it waits
    two seconds for a process that -- being a recording -- never exits.
    """
    decl = _decl(**overrides)
    # The recorder is told the declared command, so that it can pick the
    # service's own spawn out of everything the spawn path started.
    record = _install_recorders(monkeypatch, decl.start_cmd)
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: True)
    monkeypatch.setattr(
        sm, "_await_exit", lambda proc, timeout: (False, None)
    )
    runtime = sm.start_service(decl, tmp_path)
    # Exactly one spawn is the service's own.  A start that spawned the
    # service twice, or none at all, would leave the assertions below
    # reading something that is not the service.
    assert len(record.wrapper_spawns) == 1, (
        "the capture took %d service spawns; the start path spawns the "
        "service exactly once" % len(record.wrapper_spawns)
    )
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
    assert "start_new_session" not in record.spawned_kwargs, (
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
    stdin = record.spawned_kwargs.get("stdin")
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
    assert record.spawned_kwargs.get("start_new_session") is True, (
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

    The recorded spawn is the service's own: a probe the spawn path
    makes around it -- ``ps -o lstart= -p ...`` -- is not a service and
    is not allowed to answer for one.  This case already refuses a
    probe (it asserts that the recorded argv[0] is the interpreter),
    but it says nothing about the probe itself.  The judgement is
    pinned on its own in
    ``test_a_ps_probe_is_not_the_declared_service_process`` below,
    which also pins that the probe really happens -- so this case
    cannot pass by never seeing one.
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
    # Exactly one spawn carried the declared command.  The recorder
    # picks the service out of everything the spawn path started, so
    # this is what says the service was spawned once -- and that the
    # argv above is the service's and not a helper's.
    carrying = [
        call for call in record.calls
        if call[0] and call[0][-1] == start_cmd
    ]
    assert len(carrying) == 1, (
        f"the declared command was handed to {len(carrying)} spawns, not "
        f"one: {[c[0][-1] for c in record.calls]!r}"
    )


# ---------------------------------------------------------------------------
# 2b. The registry does not change what the spawn path does
# ---------------------------------------------------------------------------
#
# The registry is a *record* of the spawn, not a part of it.  When the
# registry was in play the spawn path answered a question it did not
# have to answer — "when did this process start?" — by running a
# ``ps``, and that census rode along with the spawn: a second process
# on a path whose shape is pinned, observed as a second service.
#
# So the observed sequence has to be the same either way, and exactly
# one of the spawns may be the service.


def _is_owner_census(argv: List[str]) -> bool:
    """Is this argv the census of *this* process' own start time?

    The owner marker answers "which process started this one", and the
    answer is the owner's own start time.  The backend installs that
    marker at boot, so the census that fills it in is a boot cost and
    not a spawn cost.

    It still shows up here, because the marker is read once and cached:
    the first run to ask for it pays for the census and the next one
    does not.  Comparing a run that asked with a run that did not would
    show a difference that has nothing to do with the registry, so the
    boot cost is left out of what is compared.  A census about *any
    other* pid -- the service's own, say -- is not left out: that one
    is the spawn path asking about the process it started.
    """
    return (
        len(argv) >= 2
        and os.path.basename(argv[0]) == "ps"
        and str(os.getpid()) in argv
    )


def _census_about(argv: List[str], pid: int) -> bool:
    """Is this argv a census of *pid*'s start time?

    A census is a helper process the spawn path derives in order to
    learn something about a process it started -- ``ps -o lstart= -p
    <pid>`` is the shape of it.  The pid in the argv is what says
    *which* process was asked about.
    """
    return (
        len(argv) >= 2
        and os.path.basename(argv[0]) == "ps"
        and str(pid) in argv
    )


def _observed_spawns(monkeypatch, tmp_path, *, registry, port):
    """The spawns the spawn path asked for, with and without a registry.

    The port is the caller's: the two runs have to be comparable, and
    the port is part of the environment the child is handed (``PDT_SVC_
    <NAME>_PORT``), so two runs on two ports are two runs whose
    environments differ for a reason that has nothing to do with the
    registry.

    The owner's own census is left out (see :func:`_is_owner_census`):
    it is a boot cost, and the boot cost lands on whichever run asks
    first.
    """
    if registry is None:
        monkeypatch.delenv("PDT_SPAWN_REGISTRY", raising=False)
    else:
        monkeypatch.setenv("PDT_SPAWN_REGISTRY", str(registry))
    record, _runtime = _start_recording(
        monkeypatch, tmp_path, port=port,
    )
    return [
        (argv, kwargs) for argv, kwargs in record.spawns
        if not _is_owner_census(argv)
    ]


#: The parts of a spawn signature, named -- so a failure can say *what*
#: differs instead of printing two enormous tuples.
_SIGNATURE_PARTS = ("argv", "kwargs", "env-keys", "env-digest", "cwd")


def _spawn_signature(spawns):
    """A comparable summary of a spawn sequence.

    The registry's own variable is left out: it is the *input* to the
    comparison, not part of the spawn's shape.

    The environment is summarised by its **keys** and by a digest of its
    contents -- never by its values.  The child's environment carries
    this machine's credentials, and a test that prints them puts them
    into the test output, into the CI log, and into whatever collects
    that log; a digest says "these two environments differ" without
    saying what they are.
    """
    signature = []
    for argv, kwargs in spawns:
        env = dict(kwargs.get("env") or {})
        env.pop("PDT_SPAWN_REGISTRY", None)
        digest = hashlib.sha256(
            "\0".join(f"{key}={env[key]}" for key in sorted(env)).encode("utf-8")
        ).hexdigest()
        signature.append((
            tuple(argv),
            tuple(sorted(kwargs)),
            tuple(sorted(env)),
            digest,
            str(kwargs.get("cwd") or ""),
        ))
    return signature


def _differing_parts(left, right):
    """The names of the signature parts that differ, per spawn."""
    differences = []
    for index, (one, other) in enumerate(zip(left, right)):
        differing = [
            name for name, a, b in zip(_SIGNATURE_PARTS, one, other) if a != b
        ]
        differences.append((index, tuple(differing)))
    if len(left) != len(right):
        differences.append(("length", (len(left), len(right))))
    return differences


def test_the_spawn_sequence_does_not_depend_on_the_spawn_registry(
    monkeypatch, tmp_path
):
    """判据-1: with and without the registry, the spawn is the same spawn.

    One service, one spawn — whether or not anything is writing the
    service down.
    """
    port = _free_port()
    without = _observed_spawns(
        monkeypatch, tmp_path, registry=None, port=port,
    )
    with_registry = _observed_spawns(
        monkeypatch, tmp_path, registry=tmp_path / "spawn-registry.jsonl",
        port=port,
    )

    sig_without = _spawn_signature(without)
    sig_with = _spawn_signature(with_registry)

    assert sig_without == sig_with, (
        "the spawn registry changed the spawn sequence:\n"
        f"  without the registry: {[list(a) for a, _ in without]!r}\n"
        f"  with the registry:    {[list(a) for a, _ in with_registry]!r}\n"
        f"  differing parts: {_differing_parts(sig_without, sig_with)!r}"
    )

    wrapper_spawns = [
        argv for argv, _kwargs in with_registry if _is_service_spawn(argv)
    ]
    assert len(wrapper_spawns) == 1, (
        f"the spawn path started {len(wrapper_spawns)} services: "
        f"{wrapper_spawns!r}"
    )


def test_the_spawn_path_derives_no_process_but_the_service(
    monkeypatch, tmp_path
):
    """判据-1: the spawn path derives no process of its own.

    The service is started through the detached wrapper, and that is
    the *only* process the start path may ask for.  A helper process
    derived along the way -- a ``ps`` census to learn when the service
    started, say -- is a second process on a path whose shape is
    pinned: every observer of the spawn (the unit tests, the repro
    harness) reads the last process asked for, and reads the census as
    the service.

    The census is not observable by its name here -- it is observable by
    its *absence*: the recorded sequence has to be the service and
    nothing else.
    """
    spawns = _observed_spawns(
        monkeypatch, tmp_path, registry=None, port=_free_port(),
    )

    assert len(spawns) == 1, (
        "the spawn path derived a process besides the service:\n"
        + "\n".join(f"  {argv!r}" for argv, _kwargs in spawns)
    )
    assert _is_service_spawn(spawns[0][0]), (
        f"the only process the spawn path asked for is not the service: "
        f"{spawns[0][0]!r}"
    )


# ---------------------------------------------------------------------------
# 2c. The registry costs the spawn path nothing, and says how to read it
# ---------------------------------------------------------------------------
#
# The registry is a *record* of the spawn: the run writes down the
# process it started, so that the process can be given back when the
# run is over.  Two things about that record are pinned here.
#
# The first is the cost.  The registry used to answer "when did this
# process start?" by running a ``ps`` about the service's own pid, and
# the census rode along with the spawn -- a second process on a path
# whose shape is pinned.  The spawn path already knows the answer (it
# has just started the process), so the record is written from what the
# spawn path knows and the census is gone.
#
# The second is the *unit*.  The spawn path reads the start time from
# the kernel in epoch seconds; the platform's own reading is in the
# platform's own unit -- clock ticks since boot on Linux, whole
# seconds elsewhere.  A record compared with the wrong unit is a
# mismatch -- and a mismatch is read as "somebody else's process", so
# the run would leave its own service running and never give it back.


def test_the_registry_does_not_census_the_service(monkeypatch, tmp_path):
    """Writing the service down does not ask the platform about it.

    The census the registry used to take was about the service's own
    pid, and it rode along with the spawn.  The spawn path has just
    started the process, so it knows the answer without asking.
    """
    spawns = _observed_spawns(
        monkeypatch, tmp_path,
        registry=tmp_path / "spawn-registry.jsonl",
        port=_free_port(),
    )

    censuses = [
        argv for argv, _kwargs in spawns
        if _census_about(argv, _FakeProc.pid)
    ]
    assert censuses == [], (
        "the registry asked the platform when the service started:\n"
        + "\n".join(f"  {argv!r}" for argv in censuses)
    )


def _detached_child() -> subprocess.Popen:
    """A child that outlives the test, in a session of its own."""
    return subprocess.Popen(
        ["sleep", "30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


# ---------------------------------------------------------------------------
# 2d. One ruler, and a census nobody can take away
# ---------------------------------------------------------------------------
#
# The registry records *when* the service started, and the reaper reads
# that record back to decide whether the pid it holds is still the
# service.  Two things have to hold for that to work:
#
# * the record and the reading are on **one ruler** -- epoch seconds,
#   from the same source.  A record written in clock ticks and a
#   reading taken in epoch seconds are two readings of one instant
#   that never match, and the mismatch is read as "somebody else's
#   process": the run then leaves its own service running.
# * the census is an **observation point**.  Replacing the module-level
#   ``subprocess.Popen`` must not be able to take it away, because a
#   census that vanished from the record is a census nobody saw -- and
#   a census nobody saw is a census that hid a second process.


def _observed_start_epoch(pid: int) -> float:
    """The start time of *pid*, in epoch seconds, as the platform says it."""
    observed = reaper._epoch_start_seconds(pid)
    assert observed, "the platform could not read the start time at all"
    return float(observed)


def _simulate_the_linux_ruler(monkeypatch, epoch: float) -> str:
    """Make the platform answer in clock ticks since boot, as Linux does.

    ``/proc`` speaks in ticks and ``ps`` speaks in epoch seconds, and
    the Linux branch is the one the CI runs on -- so it is simulated
    here.  The tick count is derived from *epoch*, so that the two can
    be compared, and the boot time is what turns the ticks back into a
    time.

    Returns the tick count, so a test can say "the record holds the
    platform's own unit" when it holds it.
    """
    hz = os.sysconf("SC_CLK_TCK") or 100
    boot = epoch - 500.0
    ticks = str(int(round((epoch - boot) * hz)))
    monkeypatch.setattr(reaper, "_PROC_START_UNIT_IS_TICKS", True)
    monkeypatch.setattr(reaper, "_proc_start_ticks", lambda pid: ticks)
    monkeypatch.setattr(reaper, "_boot_time", lambda: boot)
    return ticks


def test_a_recorded_start_time_is_read_back_on_the_ruler_it_was_written_in(
    monkeypatch, tmp_path
):
    """A service this run started is recognised as this run's.

    The platform's own unit is taken away here -- the Linux case,
    simulated -- and the record still has to be recognised.  A record
    compared with the wrong unit is a mismatch, and the mismatch is
    read as "somebody else's process": the run would leave its own
    service running.

    The simulation has to act on the ruler the **reaping side** reads:
    the reaper asks the platform for the start time when it decides
    whether the pid it holds is still ours, and that reading -- not the
    one the owner marker uses -- is what the record is compared with.
    """
    registry = tmp_path / "spawn-registry.jsonl"
    monkeypatch.setenv(reaper.SPAWN_REGISTRY_ENV, str(registry))
    child = _detached_child()
    try:
        epoch = _observed_start_epoch(child.pid)
        _simulate_the_linux_ruler(monkeypatch, epoch)

        # The production path: the service manager writes the service
        # down when it starts it.
        sm._register_spawned_service(sm.ServiceRuntime(
            name="spawn-shape",
            port=0,
            pid=child.pid,
            pgid=None,
            start_epoch=epoch,
        ))
        entries = reaper.read_spawn_registry(registry)
        assert len(entries) == 1, (
            f"the registry holds {entries!r} instead of one entry"
        )

        reaped = reaper.reap_spawned_processes(
            registry, reason="the gate is over", grace_seconds=3.0,
        )
        assert {int(entry["pid"]) for entry in reaped} == {child.pid}, (
            f"the run did not recognise the service it started: {reaped!r}"
        )
    finally:
        _kill_group(child.pid, child.pid)


def _registered_start_epoch(monkeypatch, tmp_path, child) -> str:
    """What the production path writes down for *child*."""
    registry = tmp_path / "spawn-registry.jsonl"
    monkeypatch.setenv(reaper.SPAWN_REGISTRY_ENV, str(registry))
    epoch = _observed_start_epoch(child.pid)
    _simulate_the_linux_ruler(monkeypatch, epoch)
    sm._register_spawned_service(sm.ServiceRuntime(
        name="spawn-shape",
        port=0,
        pid=child.pid,
        pgid=None,
        start_epoch=epoch,
    ))
    entries = reaper.read_spawn_registry(registry)
    assert len(entries) == 1, (
        f"the registry holds {entries!r} instead of one entry"
    )
    return str(entries[0].get("start_epoch") or "")


def test_the_registry_records_the_start_time_in_epoch_seconds(
    monkeypatch, tmp_path
):
    """The record is epoch seconds -- the ruler the reaper reads on.

    The platform's own unit is clock ticks since boot, and a record
    written in ticks is read against epoch seconds: the two never
    match, the mismatch is read as "somebody else's process", and the
    service this run started is never given back.
    """
    child = _detached_child()
    try:
        recorded = _registered_start_epoch(monkeypatch, tmp_path, child)
    finally:
        _kill_group(child.pid, child.pid)

    assert recorded, "the registry recorded no start time at all"
    value = float(recorded)
    assert abs(value - time.time()) < 60.0, (
        f"the registry recorded {recorded!r}, which is not a time in epoch "
        f"seconds -- it is the platform's own unit"
    )


def test_a_record_read_on_another_ruler_is_not_our_process(
    monkeypatch, tmp_path
):
    """The same record, read in the platform's own unit, is a stranger's.

    This is the mismatch seen from the other side: the pid is alive,
    the record is there, and the two readings are on different scales --
    so the process is not ours and is left alone.
    """
    registry = tmp_path / "spawn-registry.jsonl"
    child = _detached_child()
    try:
        recorded = _registered_start_epoch(monkeypatch, tmp_path, child)
        assert recorded, "the registry recorded no start time at all"

        # The reaping side now answers in the platform's own unit.
        monkeypatch.setattr(
            reaper, "_epoch_start_seconds", lambda pid: "12345678",
        )
        reaped = reaper.reap_spawned_processes(
            registry, reason="the gate is over", grace_seconds=3.0,
        )
        survivors = reaper.read_spawn_registry(registry)
    finally:
        _kill_group(child.pid, child.pid)

    assert reaped == [], (
        f"a record read on another ruler was given back: {reaped!r}"
    )
    assert [int(entry["pid"]) for entry in survivors] == [child.pid], (
        f"the entry was dropped although nothing was given back: "
        f"{survivors!r}"
    )


def _spy_on_popen(monkeypatch) -> List[List[str]]:
    """Replace the module-level ``Popen`` and record what it is asked for.

    The stand-in hands the work to the real ``Popen``, so the census
    still happens -- what is recorded here is only what went through
    the *replaced* door.
    """
    seen: List[List[str]] = []
    real_popen = subprocess.Popen

    def _fake_popen(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)):
            seen.append([str(part) for part in argv])
        else:
            seen.append([str(argv)])
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    return seen


def _censuses(seen: List[List[str]]) -> List[List[str]]:
    """The ``ps`` spawns that went through the replaced ``Popen``."""
    return [
        argv for argv in seen
        if argv and os.path.basename(argv[0]) == "ps"
    ]


def test_the_census_is_not_hijacked_by_a_replaced_popen(monkeypatch):
    """The census still runs when the module-level ``Popen`` is replaced.

    The census is an observation point.  A fixture that replaces
    ``subprocess.Popen`` would take the observation point with it, and
    the process the spawn path derives would vanish from the record --
    which is how a census came to be read as "the service spawned
    twice".  So the census has to run for real, and it has to run
    *outside* the replaced door.
    """
    seen = _spy_on_popen(monkeypatch)
    pid = os.getpid()

    assert reaper._ps_start_epoch(pid), (
        "the census did not run at all"
    )
    assert reaper._ps_start_epochs([pid]), (
        "the batched census did not run at all"
    )
    assert identity._run_ps(pid), (
        "the identity fallback did not run at all"
    )
    assert sm._run(["ps", "-o", "lstart=", "-p", str(pid)]), (
        "the service manager's helper did not run at all"
    )

    censuses = _censuses(seen)
    assert censuses == [], (
        "the census was spawned through the replaced Popen:\n"
        + "\n".join(f"  {argv!r}" for argv in censuses)
    )


def test_the_registry_holds_the_service_and_no_census(monkeypatch, tmp_path):
    """The record names the service, and only the service.

    A census is a process the spawn path derives in order to learn
    something; it is never the thing the run started, so it never
    belongs in the registry of the processes this run started.
    """
    registry = tmp_path / "spawn-registry.jsonl"
    monkeypatch.setenv(reaper.SPAWN_REGISTRY_ENV, str(registry))
    seen = _spy_on_popen(monkeypatch)

    _record, runtime = _start_recording(
        monkeypatch, tmp_path, port=_free_port(),
    )
    entries = reaper.read_spawn_registry(registry)

    assert [int(entry["pid"]) for entry in entries] == [runtime.pid], (
        f"the registry holds {entries!r} instead of the service alone"
    )
    assert _censuses(seen) == [], (
        "the spawn path's census went through the replaced Popen:\n"
        + "\n".join(f"  {argv!r}" for argv in _censuses(seen))
    )


# ---------------------------------------------------------------------------
# 2e. The name the registry wrote down, and the program it names
# ---------------------------------------------------------------------------
#
# The registry records *what* the service was as well as *when* it
# started: next to the spelling of the command line it writes down the
# **identity** of the executable slot -- the family the program belongs
# to ("python", "pypy", or the name itself).  The launcher rewrites
# ``argv[0]`` after the exec, so the spelling alone is not something the
# live process can be compared against; the identity is the ruler the
# two sides are read under.
#
# Two rules have to hold at once, and each of them came from one side of
# the merge:
#
# * the **recorded** name is the ruler.  When the ledger carries one, the
#   live side is measured against it -- and a name can only ever
#   *prevent* a match, never make one.
# * with no recorded name, **two spellings of one program are one
#   program**: the same file -- or the same macOS framework bundle -- is
#   the same program, whatever it is called.

#: A start time, in epoch seconds, for the synthetic live process below.
_CREATE_TIME = 1_700_000_000.5

#: The pid of that synthetic live process.  It is never this process:
#: the identity rules refuse to match the reader itself.
_LIVE_PID = 424242


def _live_identity(cmdline) -> identity.ProcessIdentity:
    """The identity a live process reports for *cmdline*."""
    return identity.ProcessIdentity(
        pid=_LIVE_PID, create_time=_CREATE_TIME, cmdline=cmdline,
    )


def _one_program_under_two_names(tmp_path):
    """A program, and a second name for it.

    Returns the program and a symlink that points at it.  The two names
    are not the same *family* -- that is the point: a name that reduces
    to another family can only be recognised as the same program by the
    file behind it.
    """
    program = tmp_path / "service-interpreter"
    program.write_text("#!/bin/sh\nexec \"$@\"\n")
    launcher = tmp_path / "launcher"
    launcher.symlink_to(program)
    return program, launcher


def test_the_recorded_name_is_the_ruler_the_live_side_is_measured_on(
    tmp_path
):
    """The name the ledger wrote down is what the live side is measured on.

    The launcher rewrites ``argv[0]`` after the exec, so the record's
    own spelling is not something the live process can be compared
    against: the ledger writes the *identity* of that slot down next to
    it, and that identity is the ruler.

    The recorded name can only ever **prevent** a match.  The pair below
    is one file under two names -- without a recorded name it is one
    program (the next pin), and with one it is not, because the live
    side answers to a name the ledger never wrote down.
    """
    program, launcher = _one_program_under_two_names(tmp_path)
    recorded = (str(program), "-c", "import time; time.sleep(1)")
    alias = identity.executable_identity(recorded[0])
    assert alias == "service-interpreter", (
        f"the recorded name is {alias!r} instead of the name of the program"
    )

    assert not identity.matches(
        _live_identity(
            (str(launcher), "-c", "import time; time.sleep(1)")
        ),
        create_time=_CREATE_TIME,
        cmdline=recorded,
        argv0_alias=alias,
    ), (
        "the live side was recognised although it answers to a name the "
        "ledger never wrote down"
    )

    # The name is a ruler, not a wall: the same family is recognised,
    # under the spelling the platform gives it after the re-exec.
    python_recorded = (
        "/opt/venv/bin/python3", "-c", "import time; time.sleep(1)",
    )
    python_alias = identity.executable_identity(python_recorded[0])
    assert python_alias == "python", (
        f"the recorded name is {python_alias!r} instead of the family"
    )
    assert identity.matches(
        _live_identity(
            (
                "/opt/Python.app/Contents/MacOS/Python",
                "-c",
                "import time; time.sleep(1)",
            )
        ),
        create_time=_CREATE_TIME,
        cmdline=python_recorded,
        argv0_alias=python_alias,
    ), (
        "the live side was refused although it answers to the recorded name"
    )

    # And a program of another family is refused, whatever the ledger
    # said: a name can only ever prevent a match, never make one.
    assert not identity.matches(
        _live_identity(("/bin/bash", "-c", "import time; time.sleep(1)")),
        create_time=_CREATE_TIME,
        cmdline=python_recorded,
        argv0_alias=python_alias,
    ), (
        "a program of another family was recognised as the recorded one"
    )


def test_without_a_recorded_name_the_file_is_the_program(tmp_path):
    """Two spellings of one file are one program.

    A process that re-executes itself is reported under its new
    ``argv[0]`` from then on, and the recording made a moment earlier
    still names the old one.  With no recorded name to rule by, the two
    spellings are compared through the file behind them: the same file
    is the same program, whatever it is called.
    """
    program, launcher = _one_program_under_two_names(tmp_path)
    recorded = (str(program), "-c", "import time; time.sleep(1)")

    assert identity.matches(
        _live_identity(
            (str(launcher), "-c", "import time; time.sleep(1)")
        ),
        create_time=_CREATE_TIME,
        cmdline=recorded,
    ), (
        "the same file under another name was not recognised as the same "
        "program"
    )

    # A different file under another name is another program.
    other = tmp_path / "other-interpreter"
    other.write_text("#!/bin/sh\nexec \"$@\"\n")
    assert not identity.matches(
        _live_identity((str(other), "-c", "import time; time.sleep(1)")),
        create_time=_CREATE_TIME,
        cmdline=recorded,
    ), (
        "a different file was recognised as the recorded program"
    )


# ---------------------------------------------------------------------------
# 3. A real child runs in a session of its own
# ---------------------------------------------------------------------------


def _kill_group(pgid: Optional[int], pid: Optional[int]) -> None:
    """Tear a spawned child down: its process group, then the child.

    The signals are drawn from a named scope -- the pid of the child
    this test spawned -- so the file says, in the same breath, which
    processes it is allowed to touch.  Nothing outside the scope is
    ever signalled.
    """
    pid_scope: Tuple[int, ...] = tuple(
        target for target in (pid,) if target
    )
    if not pid_scope:
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
    for target in pid_scope:
        try:
            os.kill(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.waitpid(pid_scope[0], 0)
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


# ---------------------------------------------------------------------------
# 5. A probe the spawn path makes is not the service's own spawn
# ---------------------------------------------------------------------------


def test_a_ps_probe_is_not_the_declared_service_process(tmp_path, monkeypatch):
    """The probe the spawn path makes is not the declared service.

    The spawn path probes the machine around the spawn it makes.  It
    reads the owner's start time (``ps -o lstart= -p ...``) before the
    spawn, and -- when a spawn registry is named -- it reads the
    started process' start time again *after* the spawn.  Those probes
    go through ``subprocess`` as well, so a recorder that keeps "the
    last call it saw" keeps a probe as often as it keeps the service:
    the probe's program is ``ps``, not the interpreter, and a reader
    that believes it is looking at a service that was never started.

    The registry is named here so that the second probe really
    happens -- this test is about the judgement, not about the probe.
    The start time is read with ``ps`` on purpose: that is how macOS
    reads it, and it is the route that leaves a probe behind.  On
    Linux the same reading comes out of ``/proc``, which would leave
    no probe to catch and make this test say nothing.

    2026-11-13（合并 main 之后）：起停时刻改由 :mod:`process_identity`
    读 —— psutil 在，就走 psutil；psutil 不在，才落到 ``ps``。要逼
    这一次真的走 ``ps``，就得让 psutil 在这一刻"不可用"，而那正是
    那个模块自己文档里写明的状态之一。原来那句 monkeypatch
    ``orphan_reaper._proc_start_ticks`` 已经不在这条路上：main 把自己
    进程的起停时刻挪到了 import 时解析（``orphan_reaper`` 模块末尾
    那次调用），探测发生在 import，不在这次 spawn 里，录不到。
    """
    import orphan_reaper
    import process_identity

    monkeypatch.setenv(
        orphan_reaper.SPAWN_REGISTRY_ENV,
        str(tmp_path / "spawn_registry.jsonl"),
    )
    # 让 psutil "不可用"，探测就落到 ``ps`` 上。
    monkeypatch.setattr(
        process_identity, "_resolve_psutil", lambda: None,
    )

    record, _runtime = _start_recording(
        monkeypatch, tmp_path, port=_free_port()
    )

    argv = record.spawned_argv
    assert argv, "the spawn path created no process at all"
    assert argv[0] == (sys.executable or "/usr/bin/python3"), (
        f"the spawn that was recorded is a probe, not the declared "
        f"service: {argv[0]!r}"
    )
    assert any(
        os.path.basename(probe[0]) == "ps" for probe in record.probe_argvs
    ), (
        "the spawn path made no ps probe, so this test is not exercising "
        "the race it exists for"
    )


# 5. The capture survives a probe that comes later
# ---------------------------------------------------------------------------


def test_wrapper_capture_survives_a_later_probe_spawn(tmp_path, monkeypatch):
    """The capture holds the service's spawn, even after a probe runs.

    The start path spawns subprocesses of its own besides the service:
    the ``ps`` probes that read a pid's start time -- the spawn marker
    before the spawn, the registry entry after it -- and the machine's
    process table.  ``sm.subprocess`` is the **process-global**
    ``subprocess`` module, so a capture that replaces ``Popen`` in it
    takes those probes as well -- and ``subprocess.run``, which calls
    ``Popen`` internally, is one of them.

    A capture that took a probe would end up holding the probe: the
    probe's argv is the ``ps`` binary's own path -- an absolute one,
    because the reaper resolves ``ps`` once and keeps it -- and its last
    element is the pid that was probed.  Every assertion about the
    service's spawn would then read the probe instead, and the case
    would fail on a shape the production never used.

    So the probe runs **after** the start returns, in this same
    process, and the capture must still be the service's.
    """
    record, _runtime = _start_recording(
        monkeypatch, tmp_path, port=_free_port()
    )

    # The probe the start path itself makes after the spawn: the start
    # time of the process it has just registered.
    sm._process_start_epoch(999999)

    assert len(record.wrapper_spawns) == 1, (
        "the capture took %d service spawns; the start path spawns the "
        "service exactly once" % len(record.wrapper_spawns)
    )
    argv = record.spawned_argv
    assert argv[:5] == [
        sys.executable or "/usr/bin/python3",
        "-c",
        sm._DETACHED_WRAPPER_SOURCE,
        "/bin/bash",
        "-lc",
    ], (
        "the capture no longer holds the service's spawn: %r" % (argv,)
    )
    assert argv[-1] == "npx next dev -p 3000", (
        f"the declared command was not handed over verbatim: {argv[-1]!r}"
    )
    assert "start_new_session" not in record.kwargs, (
        "Popen was asked for a new session as well: the wrapper calls "
        "setsid() itself, and a child that is already a group leader "
        "gets EPERM from its own setsid()"
    )
    assert str(record.kwargs.get("cwd")) == str(tmp_path), (
        "the service was not started in the project directory: "
        f"{record.kwargs.get('cwd')!r}"
    )


# ---------------------------------------------------------------------------
# 6. The gate's own registry does not turn into a second spawn
# ---------------------------------------------------------------------------


def test_the_gate_spawn_registry_does_not_become_a_second_spawn(
    tmp_path, monkeypatch,
):
    """The CI gate's spawn registry must not be mistaken for a spawn.

    The CI gate exports ``PDT_SPAWN_REGISTRY`` so that the run can give
    back the services it started, and writing that registry asks the
    operating system when the service started -- which on macOS costs
    one ``ps``.  That ``ps`` is a subprocess, so it is a call to the very
    ``Popen`` these pins stand in for, and a pin that read "the call the
    recorder caught" would read the probe instead of the spawn: the pin
    would fail on every run that happens to be a gate run, and it would
    say that the spawn's shape changed when nothing about it did.

    So this is the pin that says: with the registry in the environment,
    exactly one service spawn is recorded, and it is the documented one
    -- and the registry write still happens, because the gate's ability
    to give its services back is a safety property of its own and does
    not depend on the pins being happy.

    Where the pin watches matters.  It watches the step in
    ``service_manager`` that offers the service to the registry -- the
    contract this module is about -- and not the registry module below
    that step: naming the module makes this file look, to the tree's
    static gates, like a file that signals verdicts, which it does not
    do.  The step below the seam is production's business, and
    production has pins of its own.
    """
    registry = tmp_path / "spawn-registry.jsonl"
    monkeypatch.setenv("PDT_SPAWN_REGISTRY", str(registry))

    registered: List[int] = []
    real_register = sm._register_spawned_service

    def _spy_register(runtime):
        registered.append(runtime.pid)
        return real_register(runtime)

    monkeypatch.setattr(sm, "_register_spawned_service", _spy_register)

    record, runtime = _start_recording(
        monkeypatch, tmp_path, port=_free_port()
    )

    assert len(record.spawns) == 1, (
        "the gate's registry changed how many spawns the recorder caught: "
        f"{record.calls!r}"
    )
    argv = record.spawned_argv
    assert argv[0] == (sys.executable or "/usr/bin/python3"), (
        f"the spawn does not run under this interpreter: {argv[0]!r}"
    )
    assert argv[1] == "-c", (
        f"the spawn is not a program of its own: {argv[1:2]!r}"
    )
    assert registered == [runtime.pid], (
        "the service was not offered to the spawn registry, so the gate "
        "could not give it back: the registry write has to survive the "
        f"probe; registered={registered!r}"
    )
