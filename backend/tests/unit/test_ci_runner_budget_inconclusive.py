"""A gate that ran out of its budget did not prove the code is broken.

Why this file exists
--------------------
``run_ci_verification`` graded a ``full_ci`` gate on one bit: exit 0 is
PASSED, everything else is FAILED.  But the gate has three exit codes
(``scripts/run_tests.sh``): 0 — the gate passed, 1 — a phase failed,
and 3 — the run could not decide (the budget ran out, or the host could
not be measured).

Exit 3 says nothing about the code.  Reporting it as FAILED put "the
machine was too slow" into the same bucket as "the code is broken", and
the repair loop turns every FAILED verdict into a fix task — so a slow
host generated work for a deliverable nobody had proven defective.

What is pinned here
-------------------
* a gate that ran out of its own budget is INCONCLUSIVE, not FAILED;
* exit 3 with no parsable phase is INCONCLUSIVE too — the gate's own
  verdict line stands on its own;
* exit 1 is still FAILED — a phase that failed is a code failure;
* a watchdog timeout is still FAILED — the gate was killed from the
  outside, which is a different thing from "the gate stopped itself";
* the evidence carries the phase that ran out of budget and how long
  the gate said it had been running.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification_ci_runner import (  # noqa: E402
    STATUS_INCONCLUSIVE,
    run_ci_verification,
)

#: A gate that ran out of its own budget, in the real gate's own words.
#: The three summary lines it prints (``gate budget`` / ``gate elapsed``
#: / ``gate budget exhausted in``) are what the runner reads back.
GATE_OUT_OF_BUDGET = """
echo '=== phase 1/3 (parallel) ==='
echo '=== gate summary ==='
echo 'phase 1/3 (parallel):             INCONCLUSIVE (the gate budget was exhausted)'
echo 'host measurability:               OK (sleep(0.05)=0.051s, loadavg=3.10)'
echo 'gate budget: 7200s'
echo 'gate elapsed: 7200s'
echo 'gate budget exhausted in: 1/3 (parallel)'
echo 'gate: INCONCLUSIVE (exit 3)'
exit 3
"""

#: A gate that could not decide *without* naming a phase — the host was
#: not measurable, so the real gate prints no "budget exhausted in" line.
GATE_INCONCLUSIVE_WITHOUT_PHASE = """
echo '=== gate summary ==='
echo 'gate budget: 7200s'
echo 'gate elapsed: 7200s'
echo 'gate: INCONCLUSIVE (exit 3)'
exit 3
"""


def _gate(tmp_path: Path, body: str) -> Path:
    """A gate script that runs *body*."""
    gate = tmp_path / "gate.sh"
    gate.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    gate.chmod(0o755)
    return gate


def _vp(entry: str, **extra) -> Dict:
    vp = {
        "id": "VP-017",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "ci_entry": entry,
    }
    vp.update(extra)
    return vp


# --------------------------------------------------------------------------
# A gate that stopped itself is not a code failure
# --------------------------------------------------------------------------


def test_a_gate_that_ran_out_of_its_budget_is_inconclusive(tmp_path):
    """The gate stopped at its own budget — that is not a code failure.

    The gate declares its own budget and stops at it; the machine was too
    slow, the code was never given a chance to be judged.  Reporting
    that as FAILED is what sends the repair loop after a deliverable
    nobody has proven defective.
    """
    gate = _gate(tmp_path, GATE_OUT_OF_BUDGET)

    result = run_ci_verification(
        _vp(f"bash {gate}", ci_timeout_seconds=60), project_dir=tmp_path,
    )

    assert result.status == STATUS_INCONCLUSIVE, (
        f"a gate that ran out of its own budget is not a code failure:\n"
        f"{result.reasons}"
    )
    assert result.to_verdict_dict()["status"] == "INCONCLUSIVE"


def test_an_exit_three_without_a_parsable_phase_is_still_inconclusive(tmp_path):
    """The gate's own verdict line stands on its own.

    The real gate prints no "budget exhausted in" line when the host
    could not be measured — but it still says ``gate: INCONCLUSIVE
    (exit 3)``, and that alone is enough to refuse to call the run a
    failure.
    """
    gate = _gate(tmp_path, GATE_INCONCLUSIVE_WITHOUT_PHASE)

    result = run_ci_verification(
        _vp(f"bash {gate}", ci_timeout_seconds=60), project_dir=tmp_path,
    )

    assert result.status == STATUS_INCONCLUSIVE, (
        f"exit 3 with no parsable phase is still inconclusive:\n"
        f"{result.reasons}"
    )
    assert result.evidence["budget_exhausted_in"] == ""


# --------------------------------------------------------------------------
# Everything else keeps its verdict
# --------------------------------------------------------------------------


def test_an_exit_one_is_still_failed(tmp_path):
    """A phase that failed is a code failure, and stays one."""
    gate = _gate(tmp_path, "echo 'a phase failed'\nexit 1\n")

    result = run_ci_verification(
        _vp(f"bash {gate}", ci_timeout_seconds=60), project_dir=tmp_path,
    )

    assert result.status == "FAILED", (
        f"exit 1 is a code failure:\n{result.reasons}"
    )


def test_a_watchdog_timeout_is_still_failed(tmp_path):
    """The gate was killed from the outside — a different thing entirely.

    A watchdog that fires says "something went wrong between the gate
    and the watchdog".  It is not the gate's own "I ran out of budget",
    and it keeps its FAILED verdict.
    """
    result = run_ci_verification(
        _vp("sleep 30", ci_timeout_seconds=2), project_dir=tmp_path,
    )

    assert result.status == "FAILED", (
        f"a watchdog timeout is still a failure:\n{result.reasons}"
    )
    assert result.evidence["timed_out"] is True


def test_an_exit_three_that_said_nothing_is_still_failed(tmp_path):
    """A run with no summary at all declared nothing.

    The gate's own summary is the evidence that a run could not decide
    — it is where the gate says which phase ran out of budget, or that
    the host could not be measured.  An entry that exits 3 without
    printing a summary has said nothing, so there is no declaration to
    read and the historical FAILED verdict stands.
    """
    result = run_ci_verification(
        _vp("exit 3", ci_timeout_seconds=30), project_dir=tmp_path,
    )

    assert result.status == "FAILED", (
        f"an entry that declared nothing is not an inconclusive gate:\n"
        f"{result.reasons}"
    )


# --------------------------------------------------------------------------
# The evidence carries what the gate said about itself
# --------------------------------------------------------------------------


def test_the_evidence_carries_the_exhausted_phase_and_elapsed(tmp_path):
    """Which phase ran out of budget, and how long the gate had been running.

    Both are the gate's own account of itself.  Without them the verdict
    says only "something went wrong", and the reader has to go dig for
    the summary that was there all along.
    """
    gate = _gate(tmp_path, GATE_OUT_OF_BUDGET)

    result = run_ci_verification(
        _vp(f"bash {gate}", ci_timeout_seconds=60), project_dir=tmp_path,
    )

    assert result.evidence["budget_exhausted_in"] == "1/3 (parallel)"
    assert result.evidence["gate_elapsed_seconds"] == 7200

    reason = " ".join(result.reasons)
    assert "1/3 (parallel)" in reason, (
        f"the reason does not name the phase that ran out of budget:\n"
        f"{reason}"
    )
    assert "7200" in reason, (
        f"the reason does not say how long the gate had been running:\n"
        f"{reason}"
    )
