"""Unit tests for the plan reaper.

Why these tests exist
---------------------
``_kill_process_tree`` signals the executor's own process group. Every
other layer of PDT's tree deliberately detaches from it — a sub-agent
runs in its own session, a VP service in its own — so that one dead
parent never takes the others with it. That is the right design and it
has a consequence: killing the executor's group reaches *none* of the
detached children, and after a backend restart the in-memory handle is
gone as well, so the only thing left that knows which pids belong to a
plan is the ledger.

The reaper is what turns that ledger back into an action, and the two
things it must never get wrong are the two the ledger was built for:

* **order** — a sub-agent holds the tools it started, so the tools and
  the sub-agent go before the executor. Killing the parent first would
  orphan exactly the processes the sweep is trying to collect;
* **identity** — pids are recycled, so "the ledger says 4242" is not
  "4242 is ours". The kernel's start time and command line are checked
  before a signal is sent, and a mismatch is *reported*, never acted on.

And the third rule is the one that keeps the sweep from becoming a
murder: a process that cannot be identified is **reported, never
guessed at**. An escaped grand-child that called ``setsid`` is named in
the outcome with the methods that were tried; it is not killed on the
strength of a heuristic.

Each test below pins one of those.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import process_identity  # noqa: E402
import process_ledger as ledger  # noqa: E402
import process_reaper as reaper  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _spawn(argv, cwd=None, new_session=True):
    """Start a real child process and return the ``Popen`` handle.

    ``new_session`` gives the child its own process group, which is what
    a detached PDT layer looks like. The reaper refuses to signal a
    group that is the caller's own, so a test that wants the child
    signalled at all has to detach it — exactly like the real tree.
    """
    return subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=new_session,
    )


def _cleanup(*procs):
    """Kill and reap every child the test started."""
    for proc in procs:
        if proc is None:
            continue
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:  # noqa: BLE001 - cleanup must not fail the test
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass


def _wait_for(path, timeout=10.0):
    """Wait until ``path`` exists.

    The fixtures announce themselves through a file rather than through
    a sleep: a sleep is a race with the interpreter's start-up, and a
    race is a flake that shows up on someone else's machine.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def _stable_identity(pid, tries=40, delay=0.05):
    """Probe ``pid`` until two consecutive readings agree.

    On macOS the framework launcher rewrites ``argv[0]`` shortly after
    the exec, so a reading taken the instant a process is spawned can
    spell the command line differently from a reading taken a moment
    later. The identity check compares command lines, so the *recorded*
    one has to be the settled one.
    """
    previous = None
    for _ in range(tries):
        ident = process_identity.probe(pid)
        if ident is not None:
            if previous is not None and ident.cmdline == previous.cmdline:
                return ident
            previous = ident
        time.sleep(delay)
    return previous


def _record(plan_dir, role, proc, *, plan_id="p1", create_time_delta=0.0,
            cwd=None):
    """Write a spawn record for ``proc`` into the ledger at ``plan_dir``."""
    ident = _stable_identity(proc.pid)
    assert ident is not None, "the test child has no readable identity"
    record = ledger.ProcessRecord(
        plan_id=plan_id,
        role=role,
        pid=proc.pid,
        pgid=ident.pgid if ident.pgid else proc.pid,
        create_time=ident.create_time + create_time_delta,
        cmdline=list(ident.cmdline),
        cwd=cwd if cwd is not None else (ident.cwd or ""),
        owner_pid=os.getpid(),
        owner_role="backend",
    )
    ledger.record_spawn(plan_dir, record)
    return record


def _record_own_pid(plan_dir, plan_id="p1"):
    """Write a spawn record for *this* process into the ledger."""
    ident = process_identity.probe(os.getpid())
    assert ident is not None
    record = ledger.ProcessRecord(
        plan_id=plan_id,
        role="tool",
        pid=os.getpid(),
        pgid=os.getpgrp(),
        create_time=ident.create_time,
        cmdline=list(ident.cmdline),
        cwd=ident.cwd or "",
        owner_pid=os.getpid(),
        owner_role="backend",
    )
    ledger.record_spawn(plan_dir, record)
    return record


# ---------------------------------------------------------------------------
# the contracted constants
# ---------------------------------------------------------------------------

def test_reap_order_and_child_roles_are_the_contracted_constants():
    """The order is part of the contract, not an implementation detail."""
    assert reaper.REAP_ORDER == (
        "subagent",
        "vp_service",
        "tool",
        "repair_executor",
        "executor",
    )
    assert reaper.CHILD_ROLES == ("subagent", "vp_service", "tool")


# ---------------------------------------------------------------------------
# child before parent
# ---------------------------------------------------------------------------

def test_reap_kills_subagent_before_executor(tmp_path):
    """A sub-agent goes before the executor that started it.

    The executor is the parent of the sub-agent in the ledger's tree.
    Killing it first would leave the sub-agent running with nothing left
    to own it — the leak this reaper exists to close.
    """
    subagent = _spawn(["sleep", "30"], cwd=tmp_path)
    executor = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record(tmp_path, "subagent", subagent)
        _record(tmp_path, "executor", executor)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=2.0
        )

        assert outcome.reaped == [subagent.pid, executor.pid]

        killed = [
            e for e in ledger.read_events(tmp_path) if e["event"] == "killed"
        ]
        assert [e["role"] for e in killed] == ["subagent", "executor"]
    finally:
        _cleanup(subagent, executor)


# ---------------------------------------------------------------------------
# SIGTERM, then SIGKILL after the grace window
# ---------------------------------------------------------------------------

def test_reap_sends_sigterm_then_sigkill_after_grace(tmp_path):
    """A process that ignores SIGTERM is killed, not waited for forever.

    The child ignores SIGTERM, so the only thing that can end it is
    SIGKILL — which is exactly what the exit status proves. The grace
    window is waited out first, because escalating immediately would
    deny a well-behaved process its chance to clean up.

    The child announces that it has installed the ignore, so the test
    never races the interpreter's start-up against the first signal.
    """
    ready = tmp_path / "ready"
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "open({ready!r}, 'w').write('ready')\n"
        "time.sleep(30)\n"
    ).format(ready=str(ready))
    child = _spawn([sys.executable, "-c", code], cwd=tmp_path)
    try:
        _wait_for(ready)
        _record(tmp_path, "tool", child)

        started = time.monotonic()
        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=0.5
        )
        elapsed = time.monotonic() - started

        assert outcome.reaped == [child.pid]
        assert outcome.failed == []
        # The grace window was actually spent: SIGTERM was not enough.
        assert elapsed >= 0.5
        # SIGTERM was ignored, so only SIGKILL could have ended it.
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

def test_identity_mismatch_is_reported_and_not_killed(tmp_path):
    """A pid whose identity does not match is skipped, not signalled.

    The recorded start time is forged, which is what a recycled pid
    looks like from the ledger's side. The process keeps running: the
    reaper does not know that it is ours, and "not known" is not a
    licence to kill.
    """
    child = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record(tmp_path, "subagent", child, create_time_delta=3600.0)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=0.5
        )

        assert outcome.skipped == [
            {
                "pid": child.pid,
                "role": "subagent",
                "reason": "identity_mismatch",
            }
        ]
        assert outcome.reaped == []
        assert outcome.failed == []
        # Still running: no signal was sent.
        assert child.poll() is None
        assert any(
            e["event"] == "identity_mismatch"
            for e in ledger.read_events(tmp_path)
        )
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# every signal is preceded by its own identity check
# ---------------------------------------------------------------------------

def _sigterm_ignoring_child(tmp_path):
    """A child that ignores SIGTERM, so the escalation has to happen.

    It announces that it has installed the ignore, so the test never
    races the interpreter's start-up against the first signal.
    """
    ready = tmp_path / "ready"
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "open({ready!r}, 'w').write('ready')\n"
        "time.sleep(30)\n"
    ).format(ready=str(ready))
    child = _spawn([sys.executable, "-c", code], cwd=tmp_path)
    _wait_for(ready)
    return child


def test_the_escalation_re_reads_the_identity_before_it_signals(
    tmp_path, monkeypatch
):
    """The SIGKILL is preceded by a check of its own.

    The identity is read from the kernel again rather than remembered
    from the SIGTERM: the grace window is long enough for the pid to
    be handed to somebody else, and a SIGKILL sent on a remembered
    identity can land on a stranger.
    """
    child = _sigterm_ignoring_child(tmp_path)
    try:
        _record(tmp_path, "tool", child)

        real_probe = process_identity.probe
        probed = []

        def counting_probe(pid):
            probed.append(pid)
            return real_probe(pid)

        monkeypatch.setattr(process_identity, "probe", counting_probe)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=0.5
        )

        # One reading per signal: the SIGTERM's and the SIGKILL's.
        assert probed == [child.pid, child.pid]
        # The escalation still happened, and it still worked.
        assert outcome.reaped == [child.pid]
        assert outcome.failed == []
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        _cleanup(child)


def test_a_recycled_pid_is_not_killed_by_the_escalation(
    tmp_path, monkeypatch
):
    """A pid that changed identity during the grace window is left alone.

    The child ignores SIGTERM, so the grace window expires and the
    escalation would fire. But the pid now belongs to somebody else:
    the second reading of the identity says so, so the SIGKILL is
    never sent. The reaper writes an ``identity_mismatch`` event and
    reports the process as skipped — a pid it cannot vouch for is not
    a pid it may kill.
    """
    child = _sigterm_ignoring_child(tmp_path)
    try:
        _record(tmp_path, "tool", child)

        real_probe = process_identity.probe
        calls = {"n": 0}

        def fake_probe(pid):
            calls["n"] += 1
            live = real_probe(pid)
            if live is None:
                return None
            if calls["n"] > 1:
                # The same number, a different life: what a recycled pid
                # looks like from the kernel's side.
                return process_identity.ProcessIdentity(
                    pid=live.pid,
                    create_time=(live.create_time or 0.0) + 3600.0,
                    cmdline=live.cmdline,
                    pgid=live.pgid,
                    cwd=live.cwd,
                )
            return live

        monkeypatch.setattr(process_identity, "probe", fake_probe)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=0.5
        )

        # The identity was read again before the escalation.
        assert calls["n"] >= 2
        # Nothing was killed and nothing was escalated.
        assert outcome.reaped == []
        assert outcome.failed == []
        assert outcome.skipped == [
            {"pid": child.pid, "role": "tool", "reason": "identity_mismatch"}
        ]
        # The stranger is still running: no SIGKILL was sent.
        assert child.poll() is None
        assert any(
            e["event"] == "identity_mismatch" and e["pid"] == child.pid
            for e in ledger.read_events(tmp_path)
        )
    finally:
        _cleanup(child)


def test_a_pid_that_vanished_before_the_escalation_is_not_signalled(
    tmp_path, monkeypatch
):
    """A pid that no longer names a process is closed, not escalated.

    Between the SIGTERM and the SIGKILL the pid may simply stop naming
    a process. There is then nothing to kill and nothing to claim: the
    record is closed with a terminal event and the escalation never
    happens.
    """
    child = _sigterm_ignoring_child(tmp_path)
    try:
        _record(tmp_path, "tool", child)

        real_probe = process_identity.probe
        calls = {"n": 0}

        def fake_probe(pid):
            calls["n"] += 1
            if calls["n"] > 1:
                # The pid is gone — there is no process left to name.
                return None
            return real_probe(pid)

        monkeypatch.setattr(process_identity, "probe", fake_probe)
        # The grace window is pretended to have expired, and the pid is
        # gone as well as unreadable.
        monkeypatch.setattr(reaper, "_wait_gone", lambda pid, timeout: False)
        monkeypatch.setattr(reaper, "_alive", lambda pid: False)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=0.5
        )

        assert calls["n"] >= 2
        # Nothing was killed, skipped or failed: there was nothing to
        # kill.
        assert outcome.reaped == []
        assert outcome.skipped == []
        assert outcome.failed == []
        # Still running: the SIGKILL was never sent.
        assert child.poll() is None
        # The record was closed, so later sweeps do not chase it.
        exits = [
            e for e in ledger.read_events(tmp_path) if e["event"] == "exit"
        ]
        assert [e["pid"] for e in exits] == [child.pid]
        assert ledger.live_records(tmp_path) == {}
    finally:
        _cleanup(child)


def test_a_pid_that_cannot_be_read_any_more_is_reported_not_killed(
    tmp_path, monkeypatch
):
    """A pid that is still there but cannot be identified is reported.

    The identity cannot be read — there is nothing left to vouch for —
    but something is still running under that number. The reaper may
    not signal it (a pid it cannot vouch for is not a pid it may kill)
    and it may not claim the tree is dead either: the survivor is
    reported, which is what makes the sweep unclean.
    """
    child = _sigterm_ignoring_child(tmp_path)
    try:
        _record(tmp_path, "tool", child)

        real_probe = process_identity.probe
        calls = {"n": 0}

        def fake_probe(pid):
            calls["n"] += 1
            if calls["n"] > 1:
                # The pid cannot be read any more.
                return None
            return real_probe(pid)

        monkeypatch.setattr(process_identity, "probe", fake_probe)
        # The grace window is pretended to have expired.
        monkeypatch.setattr(reaper, "_wait_gone", lambda pid, timeout: False)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=0.5
        )

        assert calls["n"] >= 2
        # Nothing was killed: the pid could not be vouched for.
        assert outcome.reaped == []
        assert outcome.skipped == []
        assert outcome.failed
        assert outcome.failed[0]["pid"] == child.pid
        assert outcome.failed[0]["role"] == "tool"
        assert outcome.clean() is False
        # Still running: no SIGKILL was sent.
        assert child.poll() is None
        assert any(
            e["event"] == "failed" and e["pid"] == child.pid
            for e in ledger.read_events(tmp_path)
        )
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# the reaper never signals itself
# ---------------------------------------------------------------------------

def test_reap_skips_own_pid_and_own_process_group(tmp_path):
    """The reaper's own pid and its own process group are off limits.

    Signalling either would take down the caller — the backend, or under
    pytest the whole session. Both are reported as skipped and both are
    left running.
    """
    # No ``new_session``: this child shares the reaper's process group.
    child = _spawn(["sleep", "30"], cwd=tmp_path, new_session=False)
    try:
        _record(tmp_path, "tool", child)
        _record_own_pid(tmp_path)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=0.5
        )

        reasons = {entry["pid"]: entry["reason"] for entry in outcome.skipped}
        assert reasons[os.getpid()] == "own_pid"
        assert reasons[child.pid] == "own_process_group"
        assert outcome.reaped == []
        assert outcome.failed == []
        assert child.poll() is None
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# the named event
# ---------------------------------------------------------------------------

def test_reap_records_named_killed_event(tmp_path):
    """A kill is recorded under its own name, with enough to explain it.

    ``pid`` / ``role`` say what died, ``cmdline`` / ``cwd`` say what it
    was. Without them a ledger entry is a number, and a number cannot
    be argued with after the fact.
    """
    child = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record(tmp_path, "subagent", child)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=2.0
        )
        assert outcome.reaped == [child.pid]

        killed = [
            e for e in ledger.read_events(tmp_path) if e["event"] == "killed"
        ]
        assert len(killed) == 1
        event = killed[0]
        assert event["pid"] == child.pid
        assert event["role"] == "subagent"
        assert event["cmdline"]
        assert event["cwd"] == os.path.realpath(str(tmp_path))
    finally:
        _cleanup(child)


# ---------------------------------------------------------------------------
# the escaped grand-child
# ---------------------------------------------------------------------------

def test_escaped_grandchild_is_reported_not_killed(tmp_path):
    """A grand-child that called ``setsid`` is reported, never guessed at.

    It left the process group the reaper signalled, so the group kill
    cannot reach it. The heuristic (``cwd`` + ``cmdline`` +
    ``create_time``) finds it — and the only thing the reaper does with
    that finding is *report* it, with the list of methods that were
    tried. It does not kill it: a heuristic that guesses wrong kills a
    stranger.
    """
    pidfile = tmp_path / "grandchild.pid"
    ready = tmp_path / "escaped"
    grandchild_code = (
        "import os, time\n"
        "os.setsid()\n"
        "open({ready!r}, 'w').write('escaped')\n"
        "time.sleep(30)\n"
    ).format(ready=str(ready))
    parent_code = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', {code!r}])\n"
        "while not os.path.exists({ready!r}):\n"
        "    time.sleep(0.02)\n"
        "open({pidfile!r}, 'w').write(str(child.pid))\n"
        "time.sleep(30)\n"
    ).format(code=grandchild_code, ready=str(ready), pidfile=str(pidfile))

    parent = _spawn([sys.executable, "-c", parent_code], cwd=tmp_path)
    grandchild_pid = None
    try:
        _wait_for(pidfile)
        assert pidfile.exists(), "the fixture never reported its grandchild"
        grandchild_pid = int(pidfile.read_text().strip())

        _record(tmp_path, "subagent", parent)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=1.0
        )

        assert outcome.reaped == [parent.pid]
        assert [u["pid"] for u in outcome.unknown] == [grandchild_pid]
        entry = outcome.unknown[0]
        assert entry["holder_unknown"] is True
        assert entry["tried"] == ["pgid", "cwd+cmdline+create_time"]

        # It is still alive: it was reported, not killed.
        assert os.kill(grandchild_pid, 0) is None
    finally:
        _cleanup(parent)
        if grandchild_pid is not None:
            try:
                os.kill(grandchild_pid, signal.SIGKILL)
            except OSError:
                pass


def _sleeper(cwd):
    """A process that does nothing for five minutes, in ``cwd``."""
    return _spawn(
        [sys.executable, "-c", "import time\ntime.sleep(300)\n"], cwd=cwd
    )


def _reap_in_another_process(plan_dir):
    """Reap ``plan_dir`` from a process that is not this one.

    The sweep runs in the backend — the process that owns the plan. A
    test that wants to know what the sweep says about a process of
    *another* tree has to run the sweep there as well: run it here and
    every process this test started is a descendant of the sweep, and
    the question cannot be asked at all.
    """
    script = (
        "import json, sys\n"
        "sys.path.insert(0, %r)\n"
        "import process_reaper as reaper\n"
        "outcome = reaper.reap_plan(%r, 'p1', reason='stop',\n"
        "                        grace_seconds=0.5)\n"
        "print(json.dumps({'unknown': outcome.unknown,\n"
        "                  'reaped': outcome.reaped,\n"
        "                  'clean': outcome.clean()}))\n"
    ) % (str(_BACKEND_DIR), str(plan_dir))
    finished = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120,
    )
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout.strip().splitlines()[-1])


def test_an_escape_is_reported_even_when_the_sweep_killed_nothing(tmp_path):
    """A survivor is reported even when the sweep had nothing to kill.

    The executor on the ledger ended on its own before the sweep ran,
    so the sweep killed nothing — and a descendant of it had escaped the
    group and was still going. The sweep has to *say* so: an escape
    that is not reported is an escape nobody will ever look for, and the
    tree looks dead when it is not.

    It is reported and not killed. The reaper may not guess, and a
    guess that lands on a stranger is worse than an orphan.
    """
    executor = _sleeper(tmp_path)
    escapee = None
    try:
        _record(tmp_path, "executor", executor)

        # The executor ends on its own: there is nothing left to kill.
        executor.kill()
        executor.wait(timeout=10)

        # A descendant that escaped the group and is still running.
        escapee = _sleeper(tmp_path)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="start_force", grace_seconds=0.5
        )

        # The sweep really did kill nothing.
        assert outcome.reaped == []

        # And it still named the process that got away.
        assert [u["pid"] for u in outcome.unknown] == [escapee.pid]
        entry = outcome.unknown[0]
        assert entry["holder_unknown"] is True
        assert entry["tried"] == ["pgid", "cwd+cmdline+create_time"]

        # It is on the record under its own name, with the means that
        # were tried — a report nobody can read is not a report.
        unknown_events = [
            e for e in ledger.read_events(tmp_path)
            if e["event"] == "unknown" and e.get("pid") == escapee.pid
        ]
        assert unknown_events, "the sweep let the escape go without a word"
        assert unknown_events[0]["holder_unknown"] is True
        assert unknown_events[0]["tried"]

        # The sweep is not clean: something of ours is still running.
        assert outcome.clean() is False

        # Reported, not killed: a heuristic may not guess.
        assert escapee.poll() is None
    finally:
        _cleanup(executor, escapee)


def test_a_process_the_ledger_knows_is_not_reported_as_unknown(tmp_path):
    """A process the ledger mentions is known, not unknown.

    The escape scan looks for processes the ledger does *not* mention.
    One it does mention is a process of this plan — the sweep dealt with
    it, or deliberately left it alone — and reporting it as an unknown
    escapee would bury the real escapes under the plan's own processes.
    """
    first = _spawn(["sleep", "30"], cwd=tmp_path)
    second = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record(tmp_path, "subagent", first)
        _record(tmp_path, "executor", second)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=2.0
        )

        assert outcome.reaped == [first.pid, second.pid]
        # Both are on the ledger: neither is an unknown escapee.
        assert outcome.unknown == []
        assert outcome.clean() is True
    finally:
        _cleanup(first, second)


def test_a_process_outside_the_plan_tree_is_not_reported(tmp_path):
    """A process that only looks like an escapee belongs to somebody else.

    Same directory, same program, born after the plan's process — the
    shape of an escapee, and the shape is all a heuristic has. The kernel
    still knows who started what, and that is what settles the question:
    the plan's processes hang off the process that owns the plan, and
    this one hangs off a tree of its own.

    Reporting it would be crying wolf. A sweep that names a stranger
    makes the plan look like it has a survivor it does not have, and a
    plan that looks like that is not stopped.
    """
    anchor = _sleeper(tmp_path)
    stranger = None
    try:
        _record(tmp_path, "executor", anchor)

        # Born after the anchor, same directory, same program, own
        # session: the shape of an escapee, and nothing but the shape.
        stranger = _sleeper(tmp_path)

        # The sweep runs in the process that owns the plan — the
        # backend. The stranger hangs off this test instead, so the
        # kernel's answer is that it is not ours.
        outcome = _reap_in_another_process(tmp_path)

        assert outcome["unknown"] == []
        assert outcome["clean"] is True
        # And nobody touched it: it was never ours to touch.
        assert stranger.poll() is None
    finally:
        _cleanup(anchor, stranger)


class _FakeProcess:
    """One process of a tree that answers the way the kernel does."""

    def __init__(self, pid, parents):
        self._pid = pid
        self._parents = parents

    def ppid(self):
        if self._pid not in self._parents:
            raise KeyError(self._pid)
        return self._parents[self._pid]


class _FakeProcessTree:
    """A process tree, read the way psutil reads one.

    ``parents`` maps a pid to its parent. A pid that is not in it has
    gone — and a process that cannot be read is a process that has
    gone.
    """

    def __init__(self, parents):
        self._parents = parents

    def Process(self, pid):  # noqa: N802 - the psutil spelling
        return _FakeProcess(pid, self._parents)


def test_an_ancestry_that_ends_at_the_top_of_the_tree_is_not_ours():
    """The top of the tree is the top of the tree, however it is spelled.

    On Linux a process whose parent lives outside the pid namespace
    reads ``ppid == 0`` — a container's session leader, for one — and
    the chain ends there instead of at init. The answer is the same
    either way: a chain that reaches the top without passing through
    one of ours belongs to somebody else, and naming it would be crying
    wolf.
    """
    tree = _FakeProcessTree({100: 200, 200: 0})
    assert reaper._attributable(tree, 100, known={999}) is False


def test_an_ancestry_that_ends_at_init_is_not_ours():
    """``ppid == 1`` and ``ppid == 0`` are the same place to stop."""
    tree = _FakeProcessTree({100: 200, 200: 1})
    assert reaper._attributable(tree, 100, known={999}) is False


def test_a_candidate_whose_own_parent_is_out_of_sight_is_kept():
    """The opposite case: the candidate may be an orphan of ours.

    Its own parent is the one that cannot be seen, so the chain is
    broken at the first link, and the parent it lost may well have
    been one of the plan's. A missed escapee is a process nobody will
    ever look for, so the answer stays one-sided.
    """
    tree = _FakeProcessTree({100: 0})
    assert reaper._attributable(tree, 100, known={999}) is True


def test_an_ancestry_that_passes_through_one_of_ours_is_ours():
    """A chain that reaches the top is a stranger; a chain that reaches
    *us* first is ours, and the top never gets a say."""
    tree = _FakeProcessTree({100: 200, 200: 999})
    assert reaper._attributable(tree, 100, known={999}) is True


# ---------------------------------------------------------------------------
# the boundaries around the sweep
# ---------------------------------------------------------------------------

def test_roles_limits_the_sweep_to_the_named_roles(tmp_path):
    """``roles`` narrows the sweep, so a caller can reap one layer.

    The VP services are reaped through this entry point; a caller that
    wants only the services must not take the executor down with them.
    """
    subagent = _spawn(["sleep", "30"], cwd=tmp_path)
    executor = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record(tmp_path, "subagent", subagent)
        _record(tmp_path, "executor", executor)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="vp_reap", grace_seconds=2.0,
            roles=("vp_service", "tool"),
        )
        assert outcome.reaped == []
        assert subagent.poll() is None
        assert executor.poll() is None

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="vp_reap", grace_seconds=2.0,
            roles=("subagent",),
        )
        assert outcome.reaped == [subagent.pid]
        assert executor.poll() is None
    finally:
        _cleanup(subagent, executor)


def test_a_process_that_already_exited_is_not_counted_as_reaped(tmp_path):
    """A process that is already gone needs no reaping, but it is closed.

    It was not signalled, so it does not belong in ``reaped``; and it
    must leave the live set, or every later sweep would keep trying to
    collect it.
    """
    child = _spawn(["sleep", "30"], cwd=tmp_path)
    _record(tmp_path, "subagent", child)
    child.kill()
    child.wait(timeout=10)

    outcome = reaper.reap_plan(
        tmp_path, "p1", reason="stop", grace_seconds=0.5
    )

    assert outcome.reaped == []
    assert outcome.failed == []
    assert outcome.unknown == []
    assert outcome.clean() is True
    assert ledger.live_records(tmp_path) == {}


def test_a_process_that_survives_sigkill_is_counted_as_failed(
    tmp_path, monkeypatch
):
    """A process that will not die is a failure, and it is reported.

    ``_alive`` is stubbed to say "still there" so the escalation runs
    its course without the test having to fight the kernel over it.
    The identity is stubbed to match the record for the same reason: a
    process that really had survived SIGKILL would still be there to
    be read, and a pid that no longer names anything has nothing left
    to be killed — that is the *vanished* case, not this one.
    """
    child = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        record = _record(tmp_path, "tool", child)

        identity = process_identity.ProcessIdentity(
            pid=child.pid,
            create_time=record.create_time,
            cmdline=tuple(record.cmdline),
            pgid=record.pgid,
            cwd=record.cwd,
        )
        monkeypatch.setattr(process_identity, "probe", lambda pid: identity)
        monkeypatch.setattr(reaper, "_alive", lambda pid: True)
        monkeypatch.setattr(reaper, "_KILL_GRACE_SECONDS", 0.1)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=0.1
        )

        assert outcome.reaped == []
        assert outcome.failed
        assert outcome.failed[0]["pid"] == child.pid
        assert outcome.failed[0]["role"] == "tool"
        assert outcome.clean() is False
    finally:
        _cleanup(child)


def test_an_empty_plan_reaps_cleanly(tmp_path):
    """No ledger, no processes, no complaints."""
    outcome = reaper.reap_plan(tmp_path, "p1", reason="stop")
    assert outcome.reaped == []
    assert outcome.skipped == []
    assert outcome.failed == []
    assert outcome.unknown == []
    assert outcome.clean() is True


def test_include_self_lets_the_caller_vouch_for_its_own_pid(tmp_path):
    """``include_self`` lifts the guard on the caller's own pid.

    The reaper signals *itself*, so this runs in a child process: the
    test process has to survive to read the exit status. The child's
    own session is its own process group, so the group the reaper would
    otherwise refuse is exactly the one it has to be allowed into.
    """
    script = textwrap.dedent(
        """
        import os, sys, time
        sys.path.insert(0, {backend!r})
        import process_identity, process_ledger as ledger
        import process_reaper as reaper
        time.sleep(0.3)
        ident = process_identity.probe(os.getpid())
        ledger.record_spawn({plan_dir!r}, ledger.ProcessRecord(
            plan_id="p1", role="executor", pid=os.getpid(),
            pgid=os.getpgrp(), create_time=ident.create_time,
            cmdline=list(ident.cmdline), cwd=ident.cwd or "",
            owner_pid=os.getpid(), owner_role="backend"))
        outcome = reaper.reap_plan(
            {plan_dir!r}, "p1", reason="stop", grace_seconds=0.2,
            include_self=True,
        )
        print("SURVIVED", outcome.reaped)
        """
    ).format(backend=str(_BACKEND_DIR), plan_dir=str(tmp_path))

    child = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        start_new_session=True,
    )

    # The child signalled itself and died of it.
    assert "SURVIVED" not in child.stdout
    assert child.returncode == -signal.SIGTERM, child.stderr


def test_include_self_still_refuses_a_mismatched_identity(tmp_path):
    """A vouched-for pid is still a pid that gets recycled.

    The identity rules are not relaxed by ``include_self`` — only the
    "not this process" refusal is. A record whose start time does not
    match is skipped, exactly as before.
    """
    child = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record(tmp_path, "tool", child, create_time_delta=3600.0)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="stop", grace_seconds=0.5,
            include_self=True,
        )

        assert outcome.skipped == [
            {
                "pid": child.pid,
                "role": "tool",
                "reason": "identity_mismatch",
            }
        ]
        assert child.poll() is None
    finally:
        _cleanup(child)