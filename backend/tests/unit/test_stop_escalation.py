"""The bounded escalation that ends what the cooperation window could not.

2026-11-20 — the stop of a plan's verification work has two halves.
The cooperative half asks the work to stop on its own; the watchdog
then waits for the plan to go quiet.  What is left over is the
uncooperative half: a child that ignores the request, a process no
layer ever claimed, one that was started after the last look.  Left
alone those keep the plan "busy" for ever, and a watchdog that waits
for ever is a watchdog that never ends.

``stop_escalation`` is the bounded end of that wait.  This file pins
the four properties the escalation is allowed to have:

1. **One SIGKILL per survivor** — the group first, the process itself
   when the group cannot be signalled, and never twice for the same
   survivor.
2. **The orphan sweep runs once, after the kills** — the sweep is the
   last look over what no layer claimed, so it has to see the world
   the kills left behind.
3. **What is still alive is reported** — the escalation hands back the
   survivors a fresh scan finds, not an assumption that the kills
   worked.
4. **Nothing raises** — a kill that fails is a kill that failed, not a
   crash in the middle of a stop.

The signals and the liveness probe are faked so the tests say what
they mean: "this pid is alive" / "this pid is gone", and "this is the
signal that was delivered".
"""

from __future__ import annotations

import os
import signal
from typing import List, Tuple

import pytest

import process_reaper
import stop_escalation
from verification_quiesce import Survivor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kills(monkeypatch) -> List[Tuple[str, int, int]]:
    """Record every signal the escalation delivers.

    The liveness probe is faked to "alive" so the escalation reaches
    the signal at all: the probe is the thing that decides whether a
    pid is worth signalling, and a test that wants to see the signal
    has to say that the pid is there.
    """
    recorded: List[Tuple[str, int, int]] = []

    monkeypatch.setattr(process_reaper, "_alive", lambda pid: True)
    monkeypatch.setattr(os, "getpgid", lambda pid: 9000 + pid)
    monkeypatch.setattr(
        os, "killpg",
        lambda pgid, sig: recorded.append(("group", pgid, sig)),
    )
    monkeypatch.setattr(
        os, "kill",
        lambda pid, sig: recorded.append(("pid", pid, sig)),
    )
    return recorded


@pytest.fixture
def no_sweep():
    """A sweep that does nothing — the escalation's own bookkeeping."""
    return lambda plan_id: None


# ---------------------------------------------------------------------------
# 1. One SIGKILL per survivor
# ---------------------------------------------------------------------------


def test_sigkill_each_survivor(kills, no_sweep):
    """Every survivor is escalated exactly once — the group, SIGKILLed."""
    survivors = [
        Survivor(pid=4242, kind="registry", detail="VP-001"),
        Survivor(pid=4243, kind="repair_executor", detail=""),
    ]

    stop_escalation.escalate(
        "p1", survivors, scan=lambda plan_id: [], orphan_sweep=no_sweep,
    )

    assert kills == [
        ("group", 13242, signal.SIGKILL),
        ("group", 13243, signal.SIGKILL),
    ]


def test_sigkill_falls_back_to_the_process_itself(kills, no_sweep, monkeypatch):
    """A group that cannot be signalled costs the group, not the process.

    The process is still this plan's work, so the fallback signals it
    directly — the one SIGKILL is still one SIGKILL.
    """

    def _no_group(pgid, sig):
        raise OSError("no such process group")

    monkeypatch.setattr(os, "killpg", _no_group)

    stop_escalation.escalate(
        "p1",
        [Survivor(pid=4242, kind="registry", detail="VP-001")],
        scan=lambda plan_id: [],
        orphan_sweep=no_sweep,
    )

    assert kills == [("pid", 4242, signal.SIGKILL)]


def test_a_pid_is_signalled_once_even_when_it_is_listed_twice(kills, no_sweep):
    """The same pid twice in the list is still one process to end."""
    survivors = [
        Survivor(pid=4242, kind="registry", detail="VP-001"),
        Survivor(pid=4242, kind="registry", detail="VP-002"),
    ]

    stop_escalation.escalate(
        "p1", survivors, scan=lambda plan_id: [], orphan_sweep=no_sweep,
    )

    assert kills == [("group", 13242, signal.SIGKILL)]


def test_a_dead_pid_is_skipped(monkeypatch, no_sweep):
    """A pid that is already gone is not signalled at all."""
    recorded: List[Tuple[str, int, int]] = []
    monkeypatch.setattr(process_reaper, "_alive", lambda pid: False)
    monkeypatch.setattr(os, "getpgid", lambda pid: 9000 + pid)
    monkeypatch.setattr(
        os, "killpg",
        lambda pgid, sig: recorded.append(("group", pgid, sig)),
    )
    monkeypatch.setattr(
        os, "kill",
        lambda pid, sig: recorded.append(("pid", pid, sig)),
    )

    stop_escalation.escalate(
        "p1",
        [Survivor(pid=4242, kind="registry", detail="VP-001")],
        scan=lambda plan_id: [],
        orphan_sweep=no_sweep,
    )

    assert recorded == []


def test_an_unknown_pid_is_skipped(monkeypatch, no_sweep):
    """A pid nobody can name (0, 1, a negative one) is never signalled."""
    recorded: List[Tuple[str, int, int]] = []
    monkeypatch.setattr(process_reaper, "_alive", lambda pid: True)
    monkeypatch.setattr(os, "getpgid", lambda pid: 9000 + pid)
    monkeypatch.setattr(
        os, "killpg",
        lambda pgid, sig: recorded.append(("group", pgid, sig)),
    )
    monkeypatch.setattr(
        os, "kill",
        lambda pid, sig: recorded.append(("pid", pid, sig)),
    )

    stop_escalation.escalate(
        "p1",
        [
            Survivor(pid=0, kind="registry", detail="VP-001"),
            Survivor(pid=1, kind="registry", detail="VP-002"),
            Survivor(pid=-5, kind="registry", detail="VP-003"),
        ],
        scan=lambda plan_id: [],
        orphan_sweep=no_sweep,
    )

    assert recorded == []


# ---------------------------------------------------------------------------
# 2. The orphan sweep runs once, after the kills
# ---------------------------------------------------------------------------


def test_orphan_sweep_after_kill(monkeypatch, no_sweep):
    """The sweep runs once, and it runs after the kills.

    The sweep is the last look over what no layer claimed, so it has to
    see the world the kills left behind — a sweep that ran first would
    report processes the kills were about to end.
    """
    order: List[str] = []

    monkeypatch.setattr(process_reaper, "_alive", lambda pid: True)
    monkeypatch.setattr(os, "getpgid", lambda pid: 9000 + pid)
    monkeypatch.setattr(
        os, "killpg",
        lambda pgid, sig: order.append("kill"),
    )
    monkeypatch.setattr(os, "kill", lambda pid, sig: order.append("kill"))

    def _sweep(plan_id):
        order.append("sweep")

    stop_escalation.escalate(
        "p1",
        [Survivor(pid=4242, kind="registry", detail="VP-001")],
        scan=lambda plan_id: [],
        orphan_sweep=_sweep,
    )

    assert order == ["kill", "sweep"]


def test_orphan_sweep_runs_once_even_with_several_survivors(
    monkeypatch, no_sweep,
):
    """One sweep per escalation, not one per survivor."""
    sweeps: List[str] = []

    monkeypatch.setattr(process_reaper, "_alive", lambda pid: True)
    monkeypatch.setattr(os, "getpgid", lambda pid: 9000 + pid)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    stop_escalation.escalate(
        "p1",
        [
            Survivor(pid=4242, kind="registry", detail="VP-001"),
            Survivor(pid=4243, kind="registry", detail="VP-002"),
        ],
        scan=lambda plan_id: [],
        orphan_sweep=lambda plan_id: sweeps.append(plan_id),
    )

    assert sweeps == ["p1"]


def test_orphan_sweep_failure_does_not_raise(monkeypatch, no_sweep):
    """A sweep that fails is logged, not raised.

    The escalation is the last stage of a stop; a stop that crashes
    half way leaves more behind than it found.
    """
    monkeypatch.setattr(process_reaper, "_alive", lambda pid: True)
    monkeypatch.setattr(os, "getpgid", lambda pid: 9000 + pid)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    def _broken(plan_id):
        raise RuntimeError("sweep exploded")

    result = stop_escalation.escalate(
        "p1",
        [Survivor(pid=4242, kind="registry", detail="VP-001")],
        scan=lambda plan_id: [],
        orphan_sweep=_broken,
    )

    assert result == []


def test_the_default_sweep_ends_what_the_first_pass_missed(kills, monkeypatch):
    """The default sweep ends the processes the survivor list missed.

    The processes no layer claimed are exactly the ones the survivor
    list did not mention, so the default sweep looks at the plan once
    more and ends what is still there.
    """
    monkeypatch.setattr(
        stop_escalation.verification_quiesce,
        "scan_plan_processes",
        lambda plan_id: [Survivor(pid=7777, kind="registry", detail="VP-777")],
    )

    stop_escalation.escalate("p1", [], scan=lambda plan_id: [])

    assert kills == [("group", 16777, signal.SIGKILL)]


def test_the_default_sweep_does_not_signal_a_pid_twice(kills, monkeypatch):
    """One escalation signal per survivor — the sweep included.

    The sweep sees the plan's own scan, and that scan still names the
    survivor the first pass just ended.  Signalling it again would be a
    second SIGKILL for one process.
    """
    monkeypatch.setattr(
        stop_escalation.verification_quiesce,
        "scan_plan_processes",
        lambda plan_id: [Survivor(pid=4242, kind="registry", detail="VP-001")],
    )

    stop_escalation.escalate(
        "p1",
        [Survivor(pid=4242, kind="registry", detail="VP-001")],
        scan=lambda plan_id: [],
    )

    assert kills == [("group", 13242, signal.SIGKILL)]


# ---------------------------------------------------------------------------
# 3. What is still alive is reported
# ---------------------------------------------------------------------------


def test_remaining_reported(no_sweep):
    """The escalation hands back what the scan still finds.

    The kills are not a verdict — a process can survive a SIGKILL long
    enough to be reported, and a caller that wants to know what is left
    needs the scan's answer rather than an assumption.
    """
    remaining = Survivor(pid=999, kind="registry", detail="VP-009")

    assert stop_escalation.escalate(
        "p1", [], scan=lambda plan_id: [remaining], orphan_sweep=no_sweep,
    ) == [remaining]


def test_nothing_remaining_is_reported_as_an_empty_list(no_sweep):
    """Everything cleared means an empty report, not ``None``."""
    assert stop_escalation.escalate(
        "p1", [], scan=lambda plan_id: [], orphan_sweep=no_sweep,
    ) == []


def test_the_report_is_the_scan_of_the_plan(kills, no_sweep):
    """The report is the plan's own scan — the caller decides what that is."""
    seen: List[str] = []

    def _scan(plan_id):
        seen.append(plan_id)
        return []

    stop_escalation.escalate(
        "p1",
        [Survivor(pid=4242, kind="registry", detail="VP-001")],
        scan=_scan,
        orphan_sweep=no_sweep,
    )

    assert seen == ["p1"]


def test_a_scan_that_fails_is_reported_as_empty(kills, no_sweep):
    """A scan that cannot be answered is an empty report, not a crash."""

    def _broken(plan_id):
        raise RuntimeError("scan exploded")

    assert stop_escalation.escalate(
        "p1", [], scan=_broken, orphan_sweep=no_sweep,
    ) == []


# ---------------------------------------------------------------------------
# 4. Nothing raises
# ---------------------------------------------------------------------------


def test_kill_failure_does_not_raise(monkeypatch, no_sweep):
    """A signal that cannot be delivered is logged, not raised.

    Both halves of the escalation refuse — the group and the process —
    and the escalation still reports what the scan finds.
    """
    monkeypatch.setattr(process_reaper, "_alive", lambda pid: True)
    monkeypatch.setattr(os, "getpgid", lambda pid: 9000 + pid)

    def _no_group(pgid, sig):
        raise OSError("no such process group")

    def _no_process(pid, sig):
        raise OSError("no such process")

    monkeypatch.setattr(os, "killpg", _no_group)
    monkeypatch.setattr(os, "kill", _no_process)

    result = stop_escalation.escalate(
        "p1",
        [Survivor(pid=4242, kind="registry", detail="VP-001")],
        scan=lambda plan_id: [
            Survivor(pid=4242, kind="registry", detail="VP-001"),
        ],
        orphan_sweep=no_sweep,
    )

    assert result == [Survivor(pid=4242, kind="registry", detail="VP-001")]


def test_a_broken_survivor_list_does_not_raise(no_sweep):
    """A survivor that carries no usable pid is skipped, not fatal."""
    result = stop_escalation.escalate(
        "p1",
        [Survivor(pid=4242, kind="registry", detail="VP-001")],
        scan=lambda plan_id: [],
        orphan_sweep=no_sweep,
    )
    assert result == []
