"""The watchdog has to be longer than the gate, and it has to say why.

Why this file exists
--------------------
Two budgets lived in two files and had never heard of each other.  The
gate (``scripts/run_tests.sh``) had per-case budgets and no total; the
watchdog (``backend/verification_ci_runner.py``) had a total of 3600 s.
The gate did not know it had a watchdog, and the watchdog did not know
how long the gate needed.

On a loaded host the gate needed more than the watchdog allowed, so the
watchdog killed it from the outside.  The gate never printed a verdict.
What the round-2 artifact recorded was ``exit_code=null``,
``timed_out=true`` and a failure fingerprint that had degenerated into
"the last line of stdout" — no phase, no elapsed time, no budget, and
above all no distinction between *the code is broken* and *the machine
ran out of time*.

What is pinned here
-------------------
* the watchdog's budget is never shorter than the budget the gate
  declares — the gate's declaration is the single source of truth, and
  the watchdog reads it out of the gate's own script;
* a VP that declares a *shorter* timeout does not get one (a watchdog
  shorter than the gate is the bug, whoever asked for it);
* a VP that declares a *longer* timeout keeps it;
* a gate that declares nothing falls back to the historical default;
* when the watchdog fires, the failure says whether the gate exhausted
  its own budget or was killed from the outside, and it names the phase
  that was running, the elapsed time and the declared budget.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification_ci_runner import (  # noqa: E402
    DEFAULT_CI_TIMEOUT_SECONDS,
    GATE_BUDGET_MARGIN_SECONDS,
    ci_timeout,
    gate_budget_seconds,
    parse_gate_summary,
    run_ci_verification,
)

#: A gate script that declares its budget the way the real one does.
GATE_WITH_BUDGET = """#!/bin/bash
GATE_BUDGET_SECONDS_ALL=7200
GATE_BUDGET_SECONDS_UNIT=1800
GATE_BUDGET_SECONDS_E2E=3600
GATE_BUDGET_SECONDS_WALLCLOCK=1800
echo "gate budget: 7200s"
"""

#: A gate script that declares nothing.
GATE_WITHOUT_BUDGET = """#!/bin/bash
echo "no budget here"
"""


def _project(tmp_path: Path, script: str) -> Path:
    """A project directory whose gate script is *script*."""
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True, exist_ok=True)
    (project / "scripts" / "run_tests.sh").write_text(script, encoding="utf-8")
    return project


def _vp(entry: str, **extra) -> Dict:
    vp = {
        "id": "VP-016",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "ci_entry": entry,
    }
    vp.update(extra)
    return vp


# --------------------------------------------------------------------------
# The watchdog is never shorter than the gate
# --------------------------------------------------------------------------


def test_the_watchdog_is_longer_than_the_budget_the_gate_declares(tmp_path):
    """The watchdog's budget is the gate's budget plus a margin.

    This is the invariant the round-2 failure needed: the watchdog can
    no longer fire while the gate still had time left to give a verdict.
    """
    project = _project(tmp_path, GATE_WITH_BUDGET)
    entry = "bash scripts/run_tests.sh"

    timeout = ci_timeout(_vp(entry), project_dir=project, entry=entry)

    assert timeout >= 7200 + GATE_BUDGET_MARGIN_SECONDS, (
        f"the watchdog gives the gate {timeout}s while the gate itself "
        f"declares 7200s — the watchdog is shorter than the gate"
    )


def test_a_vp_that_declares_a_shorter_timeout_does_not_get_one(tmp_path):
    """A VP cannot buy a watchdog shorter than the gate it runs.

    ``VP-016`` declares ``ci_timeout_seconds: 3600`` while the gate
    declares 7200 s.  Honouring the VP would put the watchdog back
    exactly where the round-2 failure found it.
    """
    project = _project(tmp_path, GATE_WITH_BUDGET)
    entry = "bash scripts/run_tests.sh"

    timeout = ci_timeout(
        _vp(entry, ci_timeout_seconds=3600), project_dir=project, entry=entry,
    )

    assert timeout >= 7200 + GATE_BUDGET_MARGIN_SECONDS, (
        f"the watchdog gives the gate {timeout}s — shorter than the budget "
        f"the gate declares"
    )


def test_a_vp_that_declares_a_longer_timeout_keeps_it(tmp_path):
    """A VP that waits longer than the gate needs still waits longer."""
    project = _project(tmp_path, GATE_WITH_BUDGET)
    entry = "bash scripts/run_tests.sh"

    timeout = ci_timeout(
        _vp(entry, ci_timeout_seconds=100000), project_dir=project, entry=entry,
    )

    assert timeout == 100000


def test_a_gate_that_declares_nothing_falls_back_to_the_default(tmp_path):
    """No declaration, no invention: the historical default stands."""
    project = _project(tmp_path, GATE_WITHOUT_BUDGET)
    entry = "bash scripts/run_tests.sh"

    assert ci_timeout(
        _vp(entry), project_dir=project, entry=entry,
    ) == DEFAULT_CI_TIMEOUT_SECONDS
    assert ci_timeout(
        _vp(entry, ci_timeout_seconds=120), project_dir=project, entry=entry,
    ) == 120


def test_the_budget_the_gate_declares_is_read_out_of_the_gate(tmp_path):
    """The declaration lives in the gate, and the runner reads it there.

    The watchdog and the gate cannot drift apart as long as there is one
    number and both of them read it.
    """
    project = _project(tmp_path, GATE_WITH_BUDGET)

    assert gate_budget_seconds(project, "bash scripts/run_tests.sh") == 7200
    assert gate_budget_seconds(
        project, "bash scripts/run_tests.sh --lane unit",
    ) == 1800
    assert gate_budget_seconds(
        project, "bash scripts/run_tests.sh --lane=wallclock",
    ) == 1800
    assert gate_budget_seconds(
        project, "bash scripts/run_tests.sh --lane e2e",
    ) == 3600


def test_the_real_gate_and_the_runner_agree_on_the_budget():
    """The repository's own gate and the runner read the same number.

    A parser that drifted away from the script would make the watchdog
    quietly wrong again — so this compares the two halves directly.
    """
    declared = gate_budget_seconds(REPO_ROOT, "bash scripts/run_tests.sh")

    assert declared == 7200, (
        f"the repository's gate declares a budget of {declared}s; the "
        f"runner does not read the number the gate publishes"
    )


# --------------------------------------------------------------------------
# The summary the gate prints is read back
# --------------------------------------------------------------------------


GATE_OUTPUT = """=== phase 1/3 (parallel) ===
...
=== phase 2/3 (e2e + integration) ===
...
=== gate summary ===
phase 1/3 (parallel):             passed
phase 2/3 (e2e + integration):    INCONCLUSIVE (the gate budget was exhausted)
phase 3/3 (time_sensitive + perf): not run (the gate budget was exhausted)
host measurability:               OK (sleep(0.05)=0.051s, loadavg=3.10)
gate budget: 7200s
gate elapsed: 7200s
gate budget exhausted in: 2/3 (e2e + integration)
gate: INCONCLUSIVE (exit 3) — the gate budget of 7200s was exhausted in 2/3 (e2e + integration); the machine did not finish the run, which is not a code failure
"""


def test_the_gate_summary_is_read_back():
    """What the gate said about itself is recoverable from its output.

    The summary is the gate's own account of the run; a reader that
    cannot parse it is a reader that only sees the last line of stdout.
    """
    summary = parse_gate_summary(GATE_OUTPUT)

    assert summary["budget_seconds"] == 7200
    assert summary["elapsed_seconds"] == 7200
    assert summary["verdict"] == "INCONCLUSIVE"
    assert summary["exit_code"] == 3
    assert summary["budget_exhausted_in"] == "2/3 (e2e + integration)"
    assert summary["running_phase"] == "2/3 (e2e + integration)"


def test_a_summary_that_is_not_there_reads_as_nothing_known():
    """Output that is not a gate summary does not invent one."""
    summary = parse_gate_summary("BOOM: 2 failed")

    assert summary["budget_seconds"] is None
    assert summary["elapsed_seconds"] is None
    assert summary["verdict"] == ""
    assert summary["running_phase"] == ""


# --------------------------------------------------------------------------
# A timeout produces a structured verdict
# --------------------------------------------------------------------------


def test_a_timeout_says_the_gate_was_killed_from_outside(tmp_path):
    """A watchdog that fires says so, and says what it interrupted.

    "超时：3600s 内没有跑完" was a true sentence that answered no
    question: not which phase, not how long the gate had been running,
    not what the gate had budgeted for itself.  The reason has to carry
    those three.
    """
    result = run_ci_verification(
        _vp("sleep 30", ci_timeout_seconds=2), project_dir=tmp_path,
    )

    assert result.status == "FAILED"
    assert result.evidence["timed_out"] is True
    reason = " ".join(result.reasons)
    assert "被外部杀掉" in reason, (
        f"the reason does not say the gate was killed from the outside:\n"
        f"{reason}"
    )
    assert "超时" in reason, (
        f"the reason does not say the run timed out:\n{reason}"
    )
    assert "正在跑" in reason, (
        f"the reason does not name the phase that was running:\n{reason}"
    )
    assert "已用" in reason, (
        f"the reason does not say how long the gate had been running:\n{reason}"
    )
    assert "预算" in reason, (
        f"the reason does not say what the gate had budgeted for itself:\n"
        f"{reason}"
    )


def test_a_timeout_carries_the_gate_summary_in_the_evidence(tmp_path):
    """The parsed summary is kept, so the verdict can be read later."""
    result = run_ci_verification(
        _vp("sleep 30", ci_timeout_seconds=2), project_dir=tmp_path,
    )

    assert "gate_summary" in result.evidence
    assert isinstance(result.evidence["gate_summary"], dict)


def test_the_verdict_of_a_gate_that_finished_is_unchanged(tmp_path):
    """A gate that answers for itself is not disturbed by any of this."""
    result = run_ci_verification(
        _vp("exit 0"), project_dir=tmp_path,
    )

    assert result.status == "PASSED"
    assert result.evidence["exit_code"] == 0
    assert result.evidence["timed_out"] is False


def test_a_gate_that_ran_out_of_its_own_budget_is_reported_as_such(tmp_path):
    """A gate that stopped itself is not "killed from outside".

    The two are different verdicts about different things: one says the
    machine was too slow, the other says something went wrong between
    the gate and the watchdog.  The gate's own summary says which one
    it was, and the reason follows the gate.

    It is also not a code failure: a gate that stopped at its own
    budget never judged the code, so the verdict is INCONCLUSIVE
    (``test_ci_runner_budget_inconclusive.py`` carries the details).
    """
    gate = tmp_path / "gate.sh"
    gate.write_text(
        "#!/bin/bash\n"
        "echo '=== phase 1/3 (parallel) ==='\n"
        "echo '=== gate summary ==='\n"
        "echo 'phase 1/3 (parallel):             INCONCLUSIVE (the gate budget was exhausted)'\n"
        "echo 'gate budget: 7200s'\n"
        "echo 'gate elapsed: 7200s'\n"
        "echo 'gate budget exhausted in: 1/3 (parallel)'\n"
        "echo 'gate: INCONCLUSIVE (exit 3)'\n"
        "exit 3\n",
        encoding="utf-8",
    )
    gate.chmod(0o755)

    result = run_ci_verification(
        _vp(f"bash {gate}", ci_timeout_seconds=30), project_dir=tmp_path,
    )

    assert result.status == "INCONCLUSIVE"
    reason = " ".join(result.reasons)
    assert "预算用尽" in reason, (
        f"the reason does not report the budget exhaustion the gate "
        f"declared:\n{reason}"
    )
    assert "1/3 (parallel)" in reason, (
        f"the reason does not name the phase that ran out of budget:\n{reason}"
    )
