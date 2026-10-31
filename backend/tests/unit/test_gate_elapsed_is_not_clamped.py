"""The gate has to report the time it really spent.

Why this file exists
--------------------
The gate's summary reported its elapsed time *clipped to its own
budget*::

    gate_now=$(date +%s)
    gate_elapsed=$((gate_now - gate_started_at))
    if [ "$gate_elapsed" -gt "$gate_budget" ]; then
        gate_elapsed=$gate_budget
    fi

So a run that went over its deadline reported exactly the budget, every
time.  The overrun was invisible: the summary could not say how far
over the deadline the run had gone, which is the one number a reader
of a slow run needs.  "The gate took 1800 s of its 1800 s budget" says
nothing about whether it finished in 1800 s or in 3600 s.

The clip was never part of the budget decision.  The gate decides "the
budget is gone" from its own bookkeeping (``budget_exhausted``), and
that decision never looks at the reported elapsed time -- so removing
the clip does not touch the exit codes (0 / 1 / 3).

The number the gate reports is whole seconds, and it is rounded *up*.
That is what keeps the overrun visible: a whole-second report that
rounded down would swallow an overrun of less than a second and report
the budget again.  For the same reason the gate's budget is a deadline
on the same millisecond clock the summary reports, so "the budget was
exhausted" and "the elapsed time is larger than the budget" are two
readings of one clock and cannot contradict each other.

What is pinned here
-------------------
* an over-budget run reports an elapsed time *larger* than its budget:
  the real number, not the budget it was clipped to;
* the three machine-readable lines of the summary stay parseable --
  they are what the CI runner reads the run's account out of;
* a run that stayed inside its budget reports what it always did;
* the CI runner's parser reads an elapsed time larger than the budget
  as a number, not as a verdict.
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

from verification_ci_runner import parse_gate_summary  # noqa: E402

#: The gate script under contract.
GATE_SCRIPT = REPO_ROOT / "scripts" / "run_tests.sh"


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
    raise AssertionError(
        f"the summary has no '{key}:' line:\n" + "\n".join(summary)
    )


def _seconds(value: str) -> int:
    """``"12s"`` -> ``12``."""
    assert value.endswith("s"), f"not a number of seconds: {value!r}"
    return int(value.rstrip("s"))


# --------------------------------------------------------------------------
# The elapsed time is the real one
# --------------------------------------------------------------------------


def test_an_over_budget_run_reports_its_real_elapsed(tmp_path: Path) -> None:
    """A run that went over its budget says how far over it went.

    The budget is one second and the stub sleeps far longer than that,
    so the gate runs out of its budget and stops itself.  The elapsed
    time it reports then has to be *larger* than the budget: a report
    of exactly the budget is the clipped number, and it is the number
    this file exists to forbid.

    The verdict is unchanged by this -- the gate still decides "the
    budget is gone" from its own bookkeeping and still exits 3.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"

    completed = _run_gate(
        ["--lane", "unit"],
        env=dict(
            _stub_env(stub, log, sleep="30"),
            GATE_BUDGET_SECONDS="1",
        ),
    )

    assert completed.returncode == 3, (
        f"a gate that ran out of its budget exited {completed.returncode}, "
        f"not 3:\n" + completed.stdout + completed.stderr
    )
    summary = _summary(completed.stdout)
    budget = _seconds(_summary_value(summary, "gate budget"))
    elapsed = _seconds(_summary_value(summary, "gate elapsed"))

    assert elapsed > budget, (
        f"the gate reports {elapsed}s spent of a {budget}s budget -- the "
        f"elapsed time is still clipped to the budget, so the overrun is "
        f"invisible:\n" + "\n".join(summary)
    )


def test_the_three_summary_lines_stay_parseable(tmp_path: Path) -> None:
    """The three lines a reader needs are still three lines.

    ``gate budget:``, ``gate elapsed:`` and ``gate budget exhausted
    in:`` are the machine-readable account of the run; the CI runner
    reads all three out of the gate's stdout.  Changing what the
    elapsed time says must not change the shape of the summary.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"

    completed = _run_gate(
        ["--lane", "unit"],
        env=dict(
            _stub_env(stub, log, sleep="30"),
            GATE_BUDGET_SECONDS="1",
        ),
    )

    summary = _summary(completed.stdout)
    joined = "\n".join(summary)
    assert re.search(r"^gate budget: \d+s$", joined, re.MULTILINE), (
        "the summary has no 'gate budget:' line:\n" + joined
    )
    assert re.search(r"^gate elapsed: \d+s$", joined, re.MULTILINE), (
        "the summary has no 'gate elapsed:' line:\n" + joined
    )
    assert re.search(
        r"^gate budget exhausted in: .+$", joined, re.MULTILINE
    ), ("the summary has no 'gate budget exhausted in:' line:\n" + joined)

    parsed = parse_gate_summary(completed.stdout)
    assert parsed["budget_seconds"] == 1, (
        f"the parser did not read the budget: {parsed}"
    )
    assert parsed["elapsed_seconds"] is not None, (
        f"the parser did not read the elapsed time: {parsed}"
    )
    assert parsed["budget_exhausted_in"], (
        f"the parser did not read the phase that ran out of budget: {parsed}"
    )


def test_an_under_budget_run_is_unchanged(tmp_path: Path) -> None:
    """A run that stayed inside its budget still says so.

    The clip is gone; the report of a run that did *not* go over its
    budget is what it always was: a number of seconds, at most the
    budget, and a green gate.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"

    completed = _run_gate(
        ["--lane", "unit"],
        env=dict(_stub_env(stub, log), GATE_BUDGET_SECONDS="60"),
    )

    assert completed.returncode == 0, (
        f"the gate exited {completed.returncode} on a stub that passes:\n"
        + completed.stdout
        + completed.stderr
    )
    summary = _summary(completed.stdout)
    budget = _seconds(_summary_value(summary, "gate budget"))
    elapsed = _seconds(_summary_value(summary, "gate elapsed"))

    assert elapsed <= budget, (
        f"the gate reports {elapsed}s spent of a {budget}s budget -- it "
        f"spent more than it declared:\n" + "\n".join(summary)
    )


def test_parse_gate_summary_tolerates_an_over_budget_elapsed() -> None:
    """The parser reads an elapsed time larger than the budget.

    The parser only extracts numbers; it does not read the elapsed
    time as a verdict.  A gate that says it went over its budget must
    not turn into a summary nobody can read.
    """
    summary = parse_gate_summary("gate budget: 1s\ngate elapsed: 3s\n")

    assert summary["budget_seconds"] == 1
    assert summary["elapsed_seconds"] == 3
