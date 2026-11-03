"""The group-signal guard: a stop must never take the caller down with it.

What this file pins
-------------------
``service_manager.kill_service`` may signal a **process group** rather
than a single pid — that is how one SIGTERM reaches the whole
``npx next dev`` chain.  A group signal is also the one way a stop can
kill the caller: ``killpg(g, sig)`` signals every member of ``g``,
and if the caller (or one of its ancestors) is a member of ``g``, the
caller dies of a signal it never sent.

The shape that actually happened in CI: the caller lives in a group of
its own (a service started with ``start_new_session=True``), and the
group it is asked to stop holds the shell that is waiting for the test
session.  Signalling that group kills the shell, the shell forwards the
signal to the child it waits for, and the caller dies of a signal it
never sent.

So ``kill_service`` refuses a group that holds the caller or any of
the caller's ancestors:

* the caller's own group — ``os.getpgrp()``;
* the group of any ancestor of the caller, read out of the process
  table (``/proc/<pid>/stat`` on Linux, ``libproc`` on macOS, ``ps``
  as the fallback).

A process tree that cannot be read is not an empty one: it means the
tree is *unknown*, and an unknown tree makes the stop refuse
everything — the group and the individual pids alike.

A refused group degrades to the individual pids when the caller allows
that (``fallback_to_pids``); a caller that does not allow it gets no
signal at all, which is its own choice and not this one's.  The refusal
itself is written down: the kill request carries ``refused_pgid``.

The POSIX sentinels are a separate protection and are pinned here
too: ``pgid == 1`` is ``kill(-1)`` — a broadcast to every process the
caller may signal — and ``pgid == 0`` is the caller's own group.  See
``utils.process._signallable_pgid`` and the ``os.killpg`` tripwire in
``backend/tests/conftest.py``.

Hermeticity
------------
No signal leaves this file.  ``os.killpg`` and ``os.kill`` are replaced
by recorders in every test, so the assertions are about **what would
have been signalled**, not about what was.  The process table is
replaced by a fake one, so the ancestor walk is the shape this file
describes rather than whatever the machine running it happens to look
like.

The ``ps`` call that *builds* the table is replaced too, in the tests
that exercise the parsing itself: no ``ps`` is run, and the output the
parser sees is written out in the test.  The native reader is switched
off in those tests as well, so they exercise the ``ps`` path and
nothing else.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402


#: The round token every kill request of this file carries.
ROUND_TOKEN = "round-token-kill-group-guard"

#: The caller every kill request of this file names.
CALLER = "test.group_guard"

#: A pid that is certainly not this process — the service that is
#: "being stopped" never has to exist for the guard to be exercised.
_FAKE_SERVICE_PID = 424242

#: A pid used as the *parent* in the fake process table.  It is never
#: signalled; it only has to be a number the ancestor walk can step to.
_FAKE_PARENT_PID = 424243

#: The group the fake table puts the fake parent in.  It is not the
#: caller's own group and it is greater than 1, so it is refused by the
#: ancestor rule alone.
_ANCESTOR_GROUP = 777001

#: A group that belongs to nobody in this process's tree: not the
#: caller's own, not an ancestor's.
_UNRELATED_GROUP = 777002


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def round_dirs(tmp_path: Path) -> Tuple[Path, Path]:
    """The plan directory and the project directory of one round.

    The same shape as the fixture of the same name in
    ``test_service_kill_attribution.py``: the kill request is written
    into the plan directory's own event stream.
    """
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir, project_dir


class _SignalRecorder:
    """Every ``os.kill`` / ``os.killpg`` call this test would have made.

    Nothing is signalled: the recorder writes the call down and returns.
    """

    def __init__(self) -> None:
        self.killpg_calls: List[Tuple[int, int]] = []
        self.kill_calls: List[Tuple[int, int]] = []

    def killpg(self, pgid: int, sig: int) -> None:
        self.killpg_calls.append((pgid, sig))

    def kill(self, pid: int, sig: int) -> None:
        self.kill_calls.append((pid, sig))


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> _SignalRecorder:
    """Record the signals instead of sending them."""
    recorder = _SignalRecorder()
    monkeypatch.setattr(os, "killpg", recorder.killpg)
    monkeypatch.setattr(os, "kill", recorder.kill)
    return recorder


@pytest.fixture
def empty_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process tree that could not be read.

    This is what a sandbox that refuses ``ps`` (and a platform with no
    native reader) produces: the table comes back ``None``, the tree
    is unknown, and the stop fails closed.
    """
    monkeypatch.setattr(sm, "_process_groups_table", lambda: None)


def _fake_table(self_pgid: int, parent_pgid: int) -> Dict[int, Tuple[int, int]]:
    """A process table in which the fake parent sits in *parent_pgid*.

    The shape the ancestor walk reads: it starts at this process,
    takes the ``(ppid, pgid)`` of every step, and stops at the first
    parent that is pid 1.
    """
    me = os.getpid()
    return {
        me: (_FAKE_PARENT_PID, self_pgid),
        _FAKE_PARENT_PID: (1, parent_pgid),
    }


def _runtime(pid: int = _FAKE_SERVICE_PID) -> sm.ServiceRuntime:
    """A service runtime of the shape the attribution test builds."""
    port = sm.allocate_free_port()
    assert port is not None, "could not allocate a free port"
    return sm.ServiceRuntime(name="api", port=port, pid=pid)


# ---------------------------------------------------------------------------
# The caller's own group
# ---------------------------------------------------------------------------


def test_the_callers_own_group_is_refused_and_degrades_to_pids(
    round_dirs, signals, monkeypatch,
) -> None:
    """The caller's own group is never signalled — the pids are.

    ``os.getpgrp()`` is the group the caller itself lives in.  A
    ``killpg`` aimed at it is a signal to the caller and to everything
    it owns, sent by the very code that is supposed to be stopping some
    *other* service.

    The refusal is written down (``refused_pgid``), the group is
    dropped (``pgid`` is ``None``), no group signal goes out, and the
    stop degrades to the individual pids — which are the processes this
    call is about.

    The process tree is **available** here: the refusal comes from the
    protected-group rule, not from an unknown tree (an unknown tree
    refuses everything, pids included — see
    ``test_without_a_process_table_everything_is_refused``).
    """
    plan_dir, _ = round_dirs
    own_group = os.getpgrp()
    monkeypatch.setattr(
        sm, "_process_groups_table",
        lambda: _fake_table(os.getpgrp(), _ANCESTOR_GROUP),
    )
    runtime = _runtime()

    outcome = sm.kill_service(
        runtime, CALLER, "the caller's own group",
        ROUND_TOKEN, plan_dir=plan_dir, pgid=own_group,
    )

    assert outcome["refused_pgid"] == own_group, outcome
    assert outcome["pgid"] is None, outcome
    assert outcome["group_signalled"] is False, outcome
    assert signals.killpg_calls == [], (
        f"the caller's own group must never be signalled; got "
        f"{signals.killpg_calls!r}"
    )
    # The degradation actually happened: the pids were signalled one by
    # one instead.
    assert outcome["pids"] == [runtime.pid], outcome
    assert signals.kill_calls == [(runtime.pid, signal.SIGTERM)], (
        f"the refusal must degrade to the individual pids; got "
        f"{signals.kill_calls!r}"
    )

    # And the refusal is on the record.
    events = sm.read_kill_events(plan_dir)
    assert len(events) == 1, events
    assert events[0]["refused_pgid"] == own_group, events[0]
    assert events[0]["pgid"] is None, events[0]


# ---------------------------------------------------------------------------
# An ancestor's group
# ---------------------------------------------------------------------------


def test_an_ancestors_group_is_refused(
    round_dirs, signals, monkeypatch,
) -> None:
    """A group that holds one of the caller's ancestors is refused.

    The ancestor is not the caller, but signalling its group still
    takes the caller down: the ancestor forwards the signal to the
    child it waits for, and the caller dies of a signal it never sent.

    The fake process table puts the caller's parent in a group of its
    own, which is not the caller's own group — so the refusal comes
    from the ancestor rule alone.
    """
    plan_dir, _ = round_dirs
    monkeypatch.setattr(
        sm, "_process_groups_table",
        lambda: _fake_table(os.getpgrp(), _ANCESTOR_GROUP),
    )
    runtime = _runtime()

    outcome = sm.kill_service(
        runtime, CALLER, "an ancestor's group",
        ROUND_TOKEN, plan_dir=plan_dir, pgid=_ANCESTOR_GROUP,
    )

    assert outcome["refused_pgid"] == _ANCESTOR_GROUP, outcome
    assert outcome["pgid"] is None, outcome
    assert outcome["group_signalled"] is False, outcome
    assert signals.killpg_calls == [], (
        f"a group holding an ancestor must never be signalled; got "
        f"{signals.killpg_calls!r}"
    )


# ---------------------------------------------------------------------------
# A group nobody in this tree is in
# ---------------------------------------------------------------------------


def test_an_unrelated_group_is_signalled(
    round_dirs, signals, monkeypatch,
) -> None:
    """A group that holds neither the caller nor an ancestor is signalled.

    The guard is not "refuse every group": it refuses the dangerous
    ones.  A group that belongs to nobody in this process's tree is
    exactly what the group signal is for, and it must still go out.
    """
    plan_dir, _ = round_dirs
    monkeypatch.setattr(
        sm, "_process_groups_table",
        lambda: _fake_table(os.getpgrp(), _ANCESTOR_GROUP),
    )
    runtime = _runtime()

    outcome = sm.kill_service(
        runtime, CALLER, "an unrelated group",
        ROUND_TOKEN, plan_dir=plan_dir, pgid=_UNRELATED_GROUP,
    )

    assert outcome["refused_pgid"] is None, outcome
    assert outcome["pgid"] == _UNRELATED_GROUP, outcome
    assert outcome["group_signalled"] is True, outcome
    assert signals.killpg_calls == [(_UNRELATED_GROUP, signal.SIGTERM)], (
        f"the group signal must go out; got {signals.killpg_calls!r}"
    )


# ---------------------------------------------------------------------------
# The POSIX sentinels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sentinel_pgid", [1, 0])
def test_a_sentinel_pgid_is_never_signalled(
    round_dirs, signals, empty_table, sentinel_pgid,
) -> None:
    """``pgid <= 1`` is a POSIX sentinel, and neither one is a group.

    ``1`` is ``kill(-1)`` — every process the caller may signal.
    ``0`` is the caller's own group.  Neither is a group this stop
    could mean, so neither is signalled.

    This protection is **independent** of the ancestor rule: it holds
    even when the process table says nothing at all, and it is the
    reason ``utils.process._signallable_pgid`` refuses them.
    """
    plan_dir, _ = round_dirs
    runtime = _runtime()

    outcome = sm.kill_service(
        runtime, CALLER, "a POSIX sentinel pgid",
        ROUND_TOKEN, plan_dir=plan_dir, pgid=sentinel_pgid,
    )

    assert outcome["pgid"] is None, outcome
    assert outcome["group_signalled"] is False, outcome
    assert signals.killpg_calls == [], (
        f"pgid={sentinel_pgid} is a sentinel, not a group; got "
        f"{signals.killpg_calls!r}"
    )


# ---------------------------------------------------------------------------
# No degradation, no signal
# ---------------------------------------------------------------------------


def test_a_refused_group_without_fallback_signals_nothing(
    round_dirs, signals, empty_table,
) -> None:
    """A caller that forbids the fallback gets no signal at all.

    ``fallback_to_pids=False`` is a caller that has a group it trusts
    and must not fall back to a pid that might belong to somebody
    else.  For such a caller a refused group means exactly that: no
    group signal and no pid signal.
    """
    plan_dir, _ = round_dirs
    own_group = os.getpgrp()
    runtime = _runtime()

    outcome = sm.kill_service(
        runtime, CALLER, "a refused group, no fallback",
        ROUND_TOKEN, plan_dir=plan_dir, pgid=own_group,
        fallback_to_pids=False,
    )

    assert outcome["refused_pgid"] == own_group, outcome
    assert outcome["pgid"] is None, outcome
    assert outcome["group_signalled"] is False, outcome
    assert outcome["pids"] == [], outcome
    assert signals.killpg_calls == [], (
        f"the group must not be signalled; got {signals.killpg_calls!r}"
    )
    assert signals.kill_calls == [], (
        f"the fallback is forbidden, so no pid may be signalled either; "
        f"got {signals.kill_calls!r}"
    )


# ---------------------------------------------------------------------------
# The degradation is intentional
# ---------------------------------------------------------------------------


def test_without_a_process_table_everything_is_refused(
    round_dirs, signals, monkeypatch,
) -> None:
    """No process tree means **no signal at all**.

    ``_process_groups_table`` returns ``None`` when the tree could not
    be read — no ``ps``, a timeout, a sandbox that refuses the
    subprocess.  The old behaviour was to read that as "there is no
    ancestor to protect" and carry on signalling, which is fail-open:
    the very group the ancestor rule exists to refuse was the group
    that got signalled.

    Now the answer is fail-closed.  The group is refused and named
    (``refused_reason``), and the degradation to the individual pids is
    switched off as well — a pid that cannot be placed in the tree is
    no safer than the group.  Nothing goes out, and the runtime is not
    marked as stopped, because nothing was stopped.
    """
    plan_dir, _ = round_dirs
    monkeypatch.setattr(sm, "_process_groups_table", lambda: None)
    runtime = _runtime()

    outcome = sm.kill_service(
        runtime, CALLER, "the same group, without a process table",
        ROUND_TOKEN, plan_dir=plan_dir, pgid=_ANCESTOR_GROUP,
    )

    assert outcome["refused_pgid"] == _ANCESTOR_GROUP, outcome
    assert outcome["pgid"] is None, outcome
    assert outcome["group_signalled"] is False, outcome
    assert signals.killpg_calls == [], (
        f"an unknown tree must not signal the group; got "
        f"{signals.killpg_calls!r}"
    )
    assert signals.kill_calls == [], (
        f"an unknown tree must not fall back to the pids either; got "
        f"{signals.kill_calls!r}"
    )
    assert outcome["process_tree"] == "unavailable", outcome
    assert outcome["refused_reason"] == "process_tree_unavailable", outcome
    assert runtime.stopped is not True, (
        f"nothing was signalled, so nothing was stopped; got "
        f"{runtime.stopped!r}"
    )

    # And the refusal is on the record, with the reason.
    events = sm.read_kill_events(plan_dir)
    assert len(events) == 1, events
    assert events[0]["process_tree"] == "unavailable", events[0]
    assert events[0]["refused_reason"] == "process_tree_unavailable", events[0]


def test_immediate_parent_group_is_protected_without_table(
    round_dirs, signals, monkeypatch,
) -> None:
    """The caller's own parent is protected even with no process table.

    The kernel knows the parent's group without anybody reading a
    table, so that one protection survives an unreadable tree: the
    parent is the process that most often forwards a signal to the
    caller, and its group must never be signalled.

    The caller's **own** group is moved out of the way first (it is
    faked to a group of its own), so that the assertion below can only
    be explained by the parent fallback and not by the "own group" rule
    — on many machines the parent shares the caller's group.
    """
    plan_dir, _ = round_dirs
    ppid = os.getppid()
    if ppid <= 1:
        pytest.skip("no usable immediate parent on this machine")
    parent_group = os.getpgid(ppid)

    monkeypatch.setattr(sm, "_process_groups_table", lambda: None)
    # The caller's own group is no longer the parent's group.
    monkeypatch.setattr(sm.os, "getpgrp", lambda: parent_group + 1)
    runtime = _runtime()

    # The parent is in the protected groups without any table.
    assert parent_group in sm._protected_groups(table=None)
    assert parent_group + 1 in sm._protected_groups(table=None)

    outcome = sm.kill_service(
        runtime, CALLER, "the parent's group, without a process table",
        ROUND_TOKEN, plan_dir=plan_dir, pgid=parent_group,
    )

    assert outcome["refused_pgid"] == parent_group, outcome
    assert outcome["process_tree"] == "unavailable", outcome
    assert outcome["refused_reason"] == "process_tree_unavailable", outcome
    assert signals.killpg_calls == [], (
        f"the parent's group must never be signalled; got "
        f"{signals.killpg_calls!r}"
    )


def test_an_unavailable_tree_is_read_once(
    round_dirs, signals, monkeypatch,
) -> None:
    """An unreadable tree is read **once**, not once per question.

    ``None`` means "already read, and unavailable" — not "no table, go
    and read one".  A stop asks for the protected groups *and* the
    protected pids, and each of those walks the ancestors; if every
    one of them read the table again, an unavailable tree would cost
    several reads, and each read that falls back to ``ps`` can sit in
    its timeout.
    """
    plan_dir, _ = round_dirs
    reads: List[int] = []

    def counting_table():
        reads.append(1)
        return None

    monkeypatch.setattr(sm, "_process_groups_table", counting_table)
    runtime = _runtime()

    outcome = sm.kill_service(
        runtime, CALLER, "the ancestor's group, with an unreadable tree",
        ROUND_TOKEN, plan_dir=plan_dir, pgid=_ANCESTOR_GROUP,
    )

    assert len(reads) == 1, (
        f"the process table must be read exactly once per stop; got "
        f"{len(reads)} reads"
    )
    assert outcome["process_tree"] == "unavailable", outcome
    assert outcome["refused_reason"] == "process_tree_unavailable", outcome


# ---------------------------------------------------------------------------
# The ``ps`` output that builds the table
# ---------------------------------------------------------------------------
#
# Everything above replaced the process table wholesale, so it only ever
# tested what the table *means*.  What the table is *made of* was untested:
# the ``ps`` call itself, and the parsing of what it prints.  A parsing
# bug there does not fail loudly — it returns a table that is short one
# row, and the ancestor walk then walks one step less and protects one
# group less than it should.

#: The argv ``_process_groups_table`` runs.  Three integer columns and
#: nothing else: the command column is not printed here, so it is not
#: asked for.
_FAKE_PS_ARGV = ["ps", "-axo", "pid=,ppid=,pgid="]


def _fake_ps(
    stdout: str, returncode: int = 0, calls: List[Tuple[List[str], dict]] = None,
) -> object:
    """A stand-in for ``subprocess.run`` that answers with *stdout*.

    The stand-in records the argv and the keywords it was called with,
    so a test can pin what the function asked for.  It never starts a
    process.
    """

    def run(argv, **kwargs):
        if calls is not None:
            calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    return run


def test_ps_output_is_parsed_into_pid_ppid_pgid(monkeypatch) -> None:
    """A normal ``ps`` output becomes ``{pid: (ppid, pgid)}``.

    The columns are right-aligned and separated by whitespace, which is
    what ``ps -axo pid=,ppid=,pgid=`` prints; the parser splits on
    whitespace and reads three integers.
    """
    monkeypatch.setattr(sm, "_process_groups_table_native", lambda: None)
    calls: List[Tuple[List[str], dict]] = []
    monkeypatch.setattr(
        sm.subprocess, "run",
        _fake_ps("  100   200   300\n  101   200   301\n", calls=calls),
    )

    table = sm._process_groups_table()

    assert table == {100: (200, 300), 101: (200, 301)}, table
    # And the argv is the one the table is built from — three integer
    # columns, no command column.
    assert len(calls) == 1, calls
    assert calls[0][0] == _FAKE_PS_ARGV, calls[0][0]


def test_unparseable_ps_lines_are_skipped(monkeypatch) -> None:
    """A line the parser cannot read is skipped, not fatal.

    Blank lines, lines with fewer than three fields, and lines whose
    fields are not integers are all skipped; every other line is parsed
    as usual.  ``ps`` on a busy machine prints a header it was not asked
    for, and a process that died between the two halves of the call can
    leave a truncated line behind: one unreadable line must not cost
    the whole table.
    """
    stdout = (
        "\n"
        "  100   200   300\n"
        "123 456\n"
        "abc def ghi\n"
        "12 x 34\n"
        "\n"
        "  101     1   101\n"
    )
    monkeypatch.setattr(sm, "_process_groups_table_native", lambda: None)
    monkeypatch.setattr(sm.subprocess, "run", _fake_ps(stdout))

    table = sm._process_groups_table()

    assert table == {100: (200, 300), 101: (1, 101)}, table


def test_a_failing_ps_still_has_its_output_parsed(monkeypatch) -> None:
    """A non-zero ``ps`` does not stop the output from being parsed.

    The implementation does not look at ``returncode``: whatever the
    command printed is parsed, and only a command that could not be run
    at all (``OSError``) or that failed as a subprocess contributes
    nothing.  This is pinned as it is implemented — see the note in the
    report: the module's own prose elsewhere reads a non-zero exit as a
    table that says nothing, which is not what the code does.
    """
    monkeypatch.setattr(sm, "_process_groups_table_native", lambda: None)
    monkeypatch.setattr(
        sm.subprocess, "run",
        _fake_ps("  100   200   300\n", returncode=1),
    )

    table = sm._process_groups_table()

    assert table == {100: (200, 300)}, table


def test_a_missing_ps_contributes_nothing(monkeypatch) -> None:
    """No ``ps`` on the platform: the table is unknown, not empty.

    ``OSError`` is the "there is no such program" answer.  The table
    comes back ``None`` — *not* ``{}`` — because an empty table would
    say "this machine has no other processes", and the caller would
    then believe that no group holds the caller's own shell.
    """
    monkeypatch.setattr(sm, "_process_groups_table_native", lambda: None)

    def run(argv, **kwargs):
        raise OSError(2, "No such file or directory: 'ps'")

    monkeypatch.setattr(sm.subprocess, "run", run)

    assert sm._process_groups_table() is None


def test_a_ps_that_times_out_contributes_nothing(monkeypatch) -> None:
    """A ``ps`` that times out contributes nothing, and raises nothing.

    A timeout is a ``SubprocessError``, and it is caught by the same
    branch that catches the missing program: the table comes back
    ``None``.
    """
    monkeypatch.setattr(sm, "_process_groups_table_native", lambda: None)
    calls: List[Tuple[List[str], dict]] = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(sm.subprocess, "run", run)

    assert sm._process_groups_table() is None
    assert len(calls) == 1, calls


# ---------------------------------------------------------------------------
# The native reader
# ---------------------------------------------------------------------------
#
# The ``ps`` reader is a fallback: on Linux the table comes from
# ``/proc/<pid>/stat`` and on macOS from ``libproc``.  Both answer the
# same question without starting a process, which is what makes the
# table readable in a sandbox where ``ps`` is refused.


def test_native_reader_wins_over_ps(monkeypatch) -> None:
    """The native reader answers first; ``ps`` is never run.

    The native reader needs no subprocess, so it is both cheaper and
    the one that works where ``ps`` is not allowed to run at all.
    """
    native = {1: (0, 1)}
    monkeypatch.setattr(sm, "_process_groups_table_native", lambda: native)

    def run(argv, **kwargs):
        raise AssertionError(
            f"ps must not be run when the native reader answered: {argv!r}"
        )

    monkeypatch.setattr(sm.subprocess, "run", run)

    assert sm._process_groups_table() == native


def test_proc_stat_parser_handles_comm_with_spaces_and_parens(
    tmp_path: Path,
) -> None:
    """``/proc/<pid>/stat`` is parsed from the **last** ``)``.

    The executable name is the text in parentheses and it may contain
    spaces *and* parentheses, so the fields after the state character
    are found after the last ``)`` — not by splitting the whole line.
    """
    root = tmp_path / "proc"
    (root / "123").mkdir(parents=True)
    (root / "123" / "stat").write_text("123 (a b) (c) S 45 67 89 0 0\n")
    # A line without a closing parenthesis is not a stat line at all.
    (root / "456").mkdir()
    (root / "456" / "stat").write_text("456 (broken S 45 67\n")
    # A line whose fields are not integers is skipped too.
    (root / "789").mkdir()
    (root / "789" / "stat").write_text("789 (ok) S pp 89 0 0\n")

    table = sm._read_proc_table(root)

    assert table == {123: (45, 67)}, table


def test_native_reader_matches_kernel_for_own_pid() -> None:
    """The native reader agrees with the kernel about this process.

    ``os.getppid()`` and ``os.getpgrp()`` are the kernel's own
    answers; the table is what the ancestor walk reads.  If the two
    disagreed, every group the guard refuses would be the wrong one.
    """
    if sys.platform != "darwin":
        pytest.skip("the native reader checked here is macOS's libproc")

    table = sm._process_groups_table_native()

    assert table is not None, "the macOS native reader must answer"
    me = os.getpid()
    assert table.get(me) == (os.getppid(), os.getpgrp()), (
        f"the native table must agree with the kernel about pid {me}; "
        f"got {table.get(me)!r}"
    )


# ---------------------------------------------------------------------------
# The ancestor walk is bounded
# ---------------------------------------------------------------------------

#: The pids of the fake deep process tree.  They are far above anything
#: the machine running the test can have.
_FAKE_DEEP_BASE_PID = 510000

#: The process groups of the fake deep process tree — one per level, all
#: different from each other and all greater than 1, so every one of them
#: is a group the ancestor walk would collect.
_FAKE_DEEP_BASE_PGID = 520000

#: How many levels the fake deep tree has.  It is deeper than the walk
#: is allowed to go.
_FAKE_DEEP_LEVELS = 12


def _deep_table(levels: int = _FAKE_DEEP_LEVELS) -> Dict[int, Tuple[int, int]]:
    """A process table with *levels* ancestors above this process.

    Every level has a process group of its own, all different, all
    greater than 1.  The deepest level's parent is pid 1, so the tree
    really does end — the walk stops there, and not before, because the
    walk's own limit is reached first.
    """
    chain = [os.getpid()] + [
        _FAKE_DEEP_BASE_PID + i for i in range(1, levels)
    ]
    table: Dict[int, Tuple[int, int]] = {}
    for index in range(levels - 1):
        table[chain[index]] = (
            chain[index + 1], _FAKE_DEEP_BASE_PGID + index,
        )
    table[chain[-1]] = (1, _FAKE_DEEP_BASE_PGID + levels - 1)
    return table


def test_the_ancestor_walk_stops_after_eight_levels(monkeypatch) -> None:
    """The walk collects the first eight levels and stops there.

    The walk is bounded: it looks at most ``max_depth`` (eight) levels
    up.  A tree deeper than that is walked only as far as the bound
    goes — the ninth level's group and the ones above it are not
    collected.

    The bound is the point of the walk being bounded: an unbounded walk
    over a table that never reaches pid 1 would go on for as long as the
    table is long, and the table is the whole machine.
    """
    table = _deep_table()
    monkeypatch.setattr(sm, "_process_groups_table", lambda: table)

    groups = sm._ancestor_process_groups()

    expected = {_FAKE_DEEP_BASE_PGID + i for i in range(8)}
    assert groups == expected, groups
    # The ninth level and the ones above it are not in the set.
    for index in range(8, _FAKE_DEEP_LEVELS):
        assert _FAKE_DEEP_BASE_PGID + index not in groups, (
            f"level {index} is past the walk's bound and must not be "
            f"collected; got {groups!r}"
        )
