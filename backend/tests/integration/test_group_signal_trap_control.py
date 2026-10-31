"""The control that tells "the whole group" from "one pid in it" apart.

2026-10-29 — the third piece of VP-014's evidence
---------------------------------------------------
The CI gate died of a SIGTERM that nobody inside the run sent.  The
control that was supposed to say where the signal came from installed a
TERM trap OUTSIDE ``scripts/run_tests.sh`` and appended the group
census to ``group-trap.txt`` when that trap fired.  The round ended with
exit 1 — pytest's own exit code — and ``group-trap.txt`` was never
created: no signal ever reached the session, so the trap never ran.

The absence of the file was therefore not evidence about the sender.  It
was evidence that the round never reached the point where the question
could be asked.  And the question is the one that decides what the
repair has to look like: **did the whole group get the signal, or did
one pid in it?**  A group signal takes the session down; a single-pid
signal takes one process down.  They need different fixes, so the
evidence has to tell them apart — and evidence that cannot tell them
apart is not evidence.

What this file pins
-------------------
``scripts/negctl_group_signal_trap.sh`` is the control, and this file
is its behavioural suite.  The control runs the signal round TWICE, in
the same shape, and the difference between the two runs is the answer:

  * the **group** round signals the whole group, and a sentinel that
    shares the group and carries NO trap dies with it;
  * the **single-pid** round signals one pid only, and the same
    sentinel — in the same group — is untouched.

The sentinel is the witness.  It has no trap, so it cannot report
anything itself; what it can do is die.  "Alive before the signal, gone
after it" is a fact about the run, and it is the fact that separates
the two situations.  A control that wrote the same verdict in both
rounds would answer nothing, so the two rounds have to DISAGREE — that
disagreement is asserted here, not assumed.

Two things this file refuses to do
----------------------------------
It does not let the control signal the session that runs it.  The
control may only signal a group it created itself; a shell that merely
lives in somebody else's group refuses to run at all.  That refusal is
the same boundary repair-r1-01 drew around the product's own group
signals, and the control would be worthless if it could reproduce the
bug it exists to document.

It does not fake the exit status.  A shell that was really terminated
by SIGTERM leaves 143; a shell that was not, leaves something else.
The control reports 143 only because the signal really arrived, and the
round that says 143 is a round that saw the signal.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "negctl_group_signal_trap.sh"

#: The phrase the trap writes when it fires.  It is the marker the
#: verification gate greps for, so it is a contract, not a caption.
TRAP_MARKER = "the control shell was signalled"


def _run(mode: str, out_dir: Path, *, own_session: bool = True) -> subprocess.CompletedProcess:
    """Run one round of the control.

    ``own_session`` decides whether the shell leads a process group of
    its own — the way the control is meant to be run.  With it off, the
    shell sits in the group of whatever started it, which is the case
    the control has to refuse.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", str(SCRIPT), mode, str(out_dir)],
        capture_output=True,
        text=True,
        timeout=120,
        start_new_session=own_session,
    )


def _evidence(out_dir: Path) -> str:
    """The evidence file of a round, or an empty string when there is none."""
    path = out_dir / "group-trap.txt"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _field(evidence: str, key: str) -> str:
    """The value of a ``key=value`` line in the evidence."""
    prefix = f"{key}="
    for line in evidence.splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip()
    return ""


def _census_rows(evidence: str) -> List[List[str]]:
    """The rows of the group census the trap wrote."""
    rows: List[List[str]] = []
    inside = False
    for line in evidence.splitlines():
        if line.startswith("--- ps "):
            inside = True
            continue
        if inside and line.startswith("--- end of the census"):
            break
        if not inside or not line.strip():
            continue
        parts = line.split(None, 3)
        if parts[0] == "PID" or not parts[0].isdigit():
            continue
        if len(parts) >= 3:
            rows.append(parts)
    return rows


@pytest.fixture(scope="module")
def rounds(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, object]:
    """Both rounds of the control, run once for the whole module."""
    out = tmp_path_factory.mktemp("group-signal-trap")
    group = _run("group", out / "group")
    single = _run("single-pid", out / "single-pid")
    return {
        "group": group,
        "single-pid": single,
        "group_evidence": _evidence(out / "group"),
        "single_evidence": _evidence(out / "single-pid"),
    }


def test_the_group_round_really_was_terminated_by_a_signal(
    rounds: Dict[str, object],
) -> None:
    """The round leaves the status a real SIGTERM leaves — 143, not 1."""
    for mode in ("group", "single-pid"):
        result = rounds[mode]
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.returncode == 143, (
            f"the {mode} round exited {result.returncode}, not 143; "
            f"143 is what a shell that was really terminated by SIGTERM leaves, "
            f"and an exit 1 would mean no signal ever arrived\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def test_a_group_signal_takes_the_sentinel_with_it(rounds: Dict[str, object]) -> None:
    """The whole group got the signal, and the witness died with it."""
    evidence = rounds["group_evidence"]
    assert isinstance(evidence, str)
    assert TRAP_MARKER in evidence, evidence
    assert _field(evidence, "mode") == "group"
    assert _field(evidence, "sentinel_alive_before_the_signal") == "yes", evidence
    assert _field(evidence, "sentinel_alive_after_the_signal") == "no", evidence
    assert _field(evidence, "received_by") == "group", evidence


def test_a_single_pid_signal_leaves_the_sentinel_alone(rounds: Dict[str, object]) -> None:
    """One pid got the signal, and the witness in the same group lived."""
    evidence = rounds["single_evidence"]
    assert isinstance(evidence, str)
    assert TRAP_MARKER in evidence, evidence
    assert _field(evidence, "mode") == "single-pid"
    assert _field(evidence, "sentinel_alive_before_the_signal") == "yes", evidence
    assert _field(evidence, "sentinel_alive_after_the_signal") == "yes", evidence
    assert _field(evidence, "received_by") == "single-pid", evidence


def test_the_two_rounds_disagree(rounds: Dict[str, object]) -> None:
    """The control can tell the two situations apart.

    A control that reported the same verdict in both rounds would be
    reporting a constant, and a constant answers nothing about where a
    signal went.
    """
    group = _field(str(rounds["group_evidence"]), "received_by")
    single = _field(str(rounds["single_evidence"]), "received_by")
    assert group == "group", group
    assert single == "single-pid", single
    assert group != single, (
        "both rounds reported the same verdict; the control cannot tell "
        "the group from the pid"
    )


def test_the_evidence_records_the_whole_group(rounds: Dict[str, object]) -> None:
    """The census names the group, its leader, and every member of it."""
    evidence = str(rounds["group_evidence"])
    shell_pid = _field(evidence, "shell_pid")
    shell_pgid = _field(evidence, "shell_pgid")
    assert shell_pid.isdigit(), evidence
    assert shell_pgid.isdigit(), evidence
    assert shell_pgid == shell_pid, (
        "the control's shell must lead the group it signals, otherwise "
        "the group belongs to somebody else"
    )
    rows = _census_rows(evidence)
    assert rows, evidence
    for row in rows:
        assert row[2] == shell_pgid, (
            f"the census holds a process outside the control's own group: {row}"
        )
    assert any(row[0] == shell_pid for row in rows), evidence


def test_the_control_never_signals_the_session_that_runs_it(
    rounds: Dict[str, object],
) -> None:
    """The group the control signals is a group it created.

    The test session's own group is the one a stray signal would take
    down — the shape repair-r1-01 closed in the product's own stop
    paths.  The control must not be able to reproduce it.
    """
    shell_pgid = int(_field(str(rounds["group_evidence"]), "shell_pgid"))
    assert shell_pgid != os.getpgrp()


def test_a_shell_that_does_not_lead_its_own_group_refuses_to_signal(
    tmp_path: Path,
) -> None:
    """The same script, in somebody else's group, signals nothing.

    Without a session of its own the script sits in the group of
    whatever started it.  Signalling that group would take down the
    test session, so the script refuses instead — and says which gate
    stopped it, so the operator is told to give the control a session
    of its own rather than to go looking for strangers in the group.
    """
    out_dir = tmp_path / "refused"
    result = _run("group", out_dir, own_session=False)
    assert result.returncode == 2, (
        f"the control exited {result.returncode} instead of refusing\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "refused" in result.stderr, result.stderr
    assert "does not lead its own process group" in result.stderr, (
        "the control must refuse at the leader gate — the gate that says "
        "this shell does not own the group it would signal\n"
        f"stderr:\n{result.stderr}"
    )
    assert not (out_dir / "group-trap.txt").exists(), (
        "a refused round must not leave an evidence file behind: it would "
        "look like a round that saw a signal"
    )


def test_the_script_signals_no_pid_it_did_not_create() -> None:
    """No kill in the script aims at a pid written down as a number.

    The only pids the control may signal are its own, the group it
    created, and the sentinel it started.  A literal pid anywhere in a
    kill command would be a path to a process this control knows
    nothing about — the failure this whole exercise is about.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    offenders: List[Tuple[int, str]] = []
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not re.search(r"\bkill\b", stripped):
            continue
        without_redirects = re.sub(r"\d*\s*>>?\s*\S+", " ", stripped)
        if re.search(r"\d", without_redirects):
            offenders.append((number, stripped))
    assert not offenders, offenders

    for sweeper in ("pkill", "killall", "killpg"):
        assert sweeper not in source, (
            f"the control must not sweep processes by name ({sweeper}); "
            f"it may only signal what it created itself"
        )