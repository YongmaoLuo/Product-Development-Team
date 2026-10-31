"""The gate has to be able to say "I ran out of time".

Why this file exists
--------------------
The gate used to have no wall-clock budget of its own.  It had per-case
budgets (``--timeout=600``) and nothing else, so the *total* was
whatever the machine happened to need.  The CI watchdog, on the other
hand, had a budget of its own — 3600 s — and the two had never heard
of each other.

The consequence was the failure the round-4 report names: on a loaded
host the gate needed more than 3600 s, so the watchdog killed it from
the outside.  The gate never got to print a verdict.  The round-2
artifact says it all: ``exit_code=null``, ``timed_out=true``, and a
failure fingerprint that had degenerated into "the last line of stdout".

A gate that is killed from the outside has no verdict at all.  A gate
that knows its own budget can stop *itself*, print what it knows, and
say "inconclusive" — which is a verdict, and an honest one: the host
was too slow, the code was not proven broken.

What is pinned here
-------------------
* the gate *declares* a budget, per lane, in a form a machine can read;
* the declaration is visible in the summary (``gate budget:`` /
  ``gate elapsed:``);
* when the budget is gone the gate stops the phase, prints its verdict
  and exits 3 — INCONCLUSIVE — instead of waiting to be killed;
* a phase that was stopped or never ran is not reported as "passed".
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: The gate script under contract.
GATE_SCRIPT = REPO_ROOT / "scripts" / "run_tests.sh"

#: The lanes the gate knows, and the name each one declares its budget
#: under.
LANES = ("all", "unit", "e2e", "wallclock")

#: The declaration the gate publishes.  The CI watchdog reads the same
#: numbers out of the same file (``backend/verification_ci_runner.py``),
#: which is what makes the watchdog's budget and the gate's budget one
#: number instead of two that happen to live next to each other.
_BUDGET_DECLARATION_RE = re.compile(
    r"^GATE_BUDGET_SECONDS_([A-Z0-9]+)=([0-9]+)\s*$", re.MULTILINE
)


# --------------------------------------------------------------------------
# The declaration
# --------------------------------------------------------------------------


def _declared_budgets() -> Dict[str, int]:
    """The budgets the gate script declares, keyed by lane."""
    text = GATE_SCRIPT.read_text(encoding="utf-8")
    return {
        lane.lower(): int(value)
        for lane, value in _BUDGET_DECLARATION_RE.findall(text)
    }


def test_the_gate_declares_a_budget_for_every_lane() -> None:
    """Every lane has a budget, and the numbers are usable.

    A budget of zero would mean "no budget" and a budget of zero would
    also mean "the gate stops before it starts"; either way the
    declaration would be a decoration rather than a contract.
    """
    budgets = _declared_budgets()

    for lane in LANES:
        assert lane in budgets, (
            f"the gate declares no budget for the '{lane}' lane; it "
            f"declares {sorted(budgets)}"
        )
        assert budgets[lane] > 0, (
            f"the '{lane}' lane declares a budget of {budgets[lane]}s — a "
            f"budget has to be a number of seconds the gate may spend"
        )


def test_the_all_lane_budget_is_the_sum_of_the_lanes() -> None:
    """The whole-gate budget is the three lanes added up.

    This is what makes the four numbers one number rather than four:
    a run of ``--lane all`` may spend what the three lanes would spend
    separately, and not a second more.
    """
    budgets = _declared_budgets()

    assert budgets["all"] == (
        budgets["unit"] + budgets["e2e"] + budgets["wallclock"]
    ), (
        f"the whole-gate budget ({budgets['all']}s) is not the sum of the "
        f"three lanes ({budgets['unit']} + {budgets['e2e']} + "
        f"{budgets['wallclock']})"
    )


# --------------------------------------------------------------------------
# The gate, run against a stub pytest
# --------------------------------------------------------------------------


#: A stand-in for pytest: it records its argv, optionally takes its
#: time, and exits with the code the test asked for.
_STUB_SOURCE = """#!/bin/sh
printf '%s\\n' "$*" >> "$PDT_STUB_LOG"
if [ -n "$PDT_STUB_SLEEP" ]; then
    sleep "$PDT_STUB_SLEEP"
fi
exit "${PDT_STUB_EXIT:-0}"
"""


def _write_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "fake_pytest"
    stub.write_text(_STUB_SOURCE, encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _run_gate(
    args: List[str],
    env: Optional[Dict[str, str]] = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run the gate script with the given environment."""
    merged = dict(os.environ)
    merged.pop("PDT_RUN_TESTS_DRY_RUN", None)
    merged.update(env or {})
    return subprocess.run(
        ["bash", str(GATE_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=merged,
        timeout=timeout,
    )


def _stub_env(
    tmp_path: Path,
    stub: Path,
    log: Path,
    sleep: str = "",
    exit_code: str = "0",
) -> Dict[str, str]:
    env = {
        "PYTEST_BIN": str(stub),
        "PDT_STUB_LOG": str(log),
        "PDT_STUB_EXIT": exit_code,
    }
    if sleep:
        env["PDT_STUB_SLEEP"] = sleep
    return env


def _summary(stdout: str) -> List[str]:
    """The lines of the gate's ``=== gate summary ===`` block."""
    lines = stdout.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "=== gate summary ===":
            return lines[index + 1 :]
    raise AssertionError(f"the gate printed no summary block:\n{stdout}")


def _summary_value(summary: List[str], key: str) -> str:
    """The value of a ``key: value`` line of the summary."""
    for line in summary:
        if line.strip().startswith(key + ":"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"the summary has no '{key}:' line:\n" + "\n".join(summary))


# --------------------------------------------------------------------------
# The summary says what the gate spent
# --------------------------------------------------------------------------


def test_the_summary_reports_the_budget_and_the_elapsed_time(tmp_path: Path) -> None:
    """The gate says what it was allowed to spend and what it spent.

    These two lines are what a reader — or the CI runner — needs to tell
    "the code failed" from "the machine ran out of time".  Without them
    the only evidence of a run that ended is its exit code.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"

    completed = _run_gate(
        ["--lane", "unit"],
        env=_stub_env(tmp_path, stub, log),
    )

    assert completed.returncode == 0, (
        f"the gate exited {completed.returncode} on a stub that passes:\n"
        + completed.stdout
        + completed.stderr
    )
    summary = _summary(completed.stdout)

    budget = _summary_value(summary, "gate budget")
    elapsed = _summary_value(summary, "gate elapsed")
    assert budget.endswith("s"), f"the budget is not a number of seconds: {budget!r}"
    assert elapsed.endswith("s"), f"the elapsed time is not seconds: {elapsed!r}"
    assert int(elapsed.rstrip("s")) <= int(budget.rstrip("s")), (
        f"the gate reports {elapsed} spent of a {budget} budget — it spent "
        f"more than it declared"
    )


def test_an_over_budget_run_keeps_its_own_elapsed(tmp_path: Path) -> None:
    """A run that went over its budget reports the time it really spent.

    The summary used to clip the elapsed time to the budget, so a run
    that overran always reported exactly the budget -- and the overrun
    was invisible: "1800 s of an 1800 s budget" does not say whether
    the run finished in 1800 s or in 3600 s.

    The clip was never part of the budget decision.  The gate decides
    "the budget is gone" from its own bookkeeping (``budget_exhausted``),
    and that decision never looks at the reported elapsed time, so the
    elapsed time can be the real number without touching the verdict.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"

    env = dict(os.environ)
    env.pop("PDT_RUN_TESTS_DRY_RUN", None)
    env.update(_stub_env(tmp_path, stub, log, sleep="30"))
    env["GATE_BUDGET_SECONDS"] = "3"
    completed = subprocess.run(
        ["bash", str(GATE_SCRIPT), "--lane", "unit"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )

    assert completed.returncode == 3, (
        f"a gate that ran out of its budget exited {completed.returncode}, "
        f"not 3:\n" + completed.stdout + completed.stderr
    )
    summary = _summary(completed.stdout)
    budget = int(_summary_value(summary, "gate budget").rstrip("s"))
    elapsed = int(_summary_value(summary, "gate elapsed").rstrip("s"))

    assert elapsed > budget, (
        f"the gate reports {elapsed}s spent of a {budget}s budget -- the "
        f"elapsed time is still clipped to the budget, so the overrun is "
        f"invisible:\n" + "\n".join(summary)
    )


def test_the_budget_can_be_overridden_by_the_environment(tmp_path: Path) -> None:
    """An operator can raise the budget, and the summary says so.

    The same variable is read by the CI watchdog, so raising it raises
    it for both — which is the point of declaring it in one place.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"

    completed = _run_gate(
        ["--lane", "unit"],
        env=dict(
            _stub_env(tmp_path, stub, log),
            GATE_BUDGET_SECONDS="12345",
        ),
    )

    assert completed.returncode == 0, (
        f"the gate exited {completed.returncode}:\n"
        + completed.stdout
        + completed.stderr
    )
    summary = _summary(completed.stdout)
    assert _summary_value(summary, "gate budget") == "12345s", (
        "the summary does not report the budget the run was given:\n"
        + "\n".join(summary)
    )


# --------------------------------------------------------------------------
# The gate stops itself
# --------------------------------------------------------------------------


def test_the_gate_stops_itself_when_its_budget_is_gone(tmp_path: Path) -> None:
    """The gate ends the run itself; it does not wait to be killed.

    A budget that only exists on paper is not a budget.  This is the
    difference between the round-1 failure (the gate finished in 2858 s
    and said what was wrong) and the round-2 failure (the gate needed
    more than the watchdog allowed, and said nothing at all).
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"

    env = dict(os.environ)
    env.pop("PDT_RUN_TESTS_DRY_RUN", None)
    env.update(_stub_env(tmp_path, stub, log, sleep="120"))
    env["GATE_BUDGET_SECONDS"] = "3"
    completed = subprocess.run(
        ["bash", str(GATE_SCRIPT), "--lane", "unit"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=90,
    )

    assert completed.returncode == 3, (
        f"a gate that ran out of its budget exited {completed.returncode}, "
        f"not 3:\n" + completed.stdout + completed.stderr
    )
    assert completed.returncode >= 0, (
        "the gate was killed by a signal instead of deciding by itself"
    )
    assert "INCONCLUSIVE" in completed.stdout, (
        "the gate exited 3 without saying why:\n" + completed.stdout
    )


def test_a_phase_that_ran_out_of_budget_is_not_reported_as_passed(
    tmp_path: Path,
) -> None:
    """A stopped phase has no verdict — least of all a green one.

    "not selected" and "stopped at the budget" are different things,
    and neither of them is "passed".  A summary that says "passed" for
    a phase that was killed is a claim about work that never finished.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"

    env = dict(os.environ)
    env.pop("PDT_RUN_TESTS_DRY_RUN", None)
    env.update(_stub_env(tmp_path, stub, log, sleep="120"))
    env["GATE_BUDGET_SECONDS"] = "3"
    completed = subprocess.run(
        ["bash", str(GATE_SCRIPT), "--lane", "all"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=90,
    )

    summary = _summary(completed.stdout)
    verdicts = [
        line for line in summary if line.strip().startswith("phase ")
    ]
    assert verdicts, (
        "the summary reports no phase at all:\n" + "\n".join(summary)
    )
    for line in verdicts:
        assert "passed" not in line, (
            f"the summary reports a phase as passed although the budget ran "
            f"out during the run: {line!r}\n" + "\n".join(summary)
        )


def test_the_gate_says_which_phase_ran_out_of_budget(tmp_path: Path) -> None:
    """"Inconclusive" is a verdict only if it says *why* and *where*.

    The round-2 artifact could not answer "which phase was running?"
    — its fingerprint was the last line of stdout.  The gate has to
    answer that from the inside.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"

    env = dict(os.environ)
    env.pop("PDT_RUN_TESTS_DRY_RUN", None)
    env.update(_stub_env(tmp_path, stub, log, sleep="120"))
    env["GATE_BUDGET_SECONDS"] = "3"
    completed = subprocess.run(
        ["bash", str(GATE_SCRIPT), "--lane", "unit"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=90,
    )

    summary = _summary(completed.stdout)
    joined = "\n".join(summary)
    assert "phase 1/3" in joined, (
        "the summary does not say which phase ran out of budget:\n" + joined
    )
