"""The budget and the summary format come from the plan's contract.

Why this file exists
--------------------
The runner read two things about a gate in the shape of **this**
repository's gate, and both readings are wrong for a project whose gate
speaks another language:

* the **budget** was read out of ``scripts/run_tests.sh`` and nothing
  else — a gate that declares its budget elsewhere left the watchdog
  with no number at all;
* the **summary** was parsed in this repository's format, always — a
  foreign gate's output was read as a gate summary that said nothing,
  and the lines that carry a lane's conclusion were picked with
  patterns written for this repository's gate.

``<plan_dir>/ci_contract.json`` is the plan's own declaration about
both (see :mod:`ci_contract`).  This file pins what the runner does
with that declaration:

* a contract that declares ``budget_source: "contract"`` has its
  ``budget_seconds`` used as the budget — the gate script's own
  declaration does not override it;
* a contract that declares no budget changes **nothing**: the budget
  still comes from the gate script, and the watchdog is still derived
  from it (the zero-regression promise);
* a gate that cannot be read at all falls back to the VP's own
  ``ci_timeout_seconds`` — the framework's floor for a whole-repo gate
  applies only when there is **no project** to ask;
* a broken contract changes nothing either: the framework defaults
  stand, and the budget comes from the gate script;
* a contract that says nothing about the summary format does not get
  one guessed for it: the gate's output is not parsed at all, so the
  evidence carries no invented summary;
* a contract that names the format (``"gate_v1"``) is parsed exactly
  as it has always been parsed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Optional

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from ci_contract import CONTRACT_FILENAME, load_ci_contract  # noqa: E402
from verification_ci_runner import (  # noqa: E402
    GATE_BUDGET_MARGIN_SECONDS,
    ci_timeout,
    gate_budget_seconds,
    run_ci_verification,
)

#: The entry the plan's VP runs.
GATE_ENTRY = "bash scripts/run_tests.sh"

#: A gate script in this repository's shape: it declares its own budget
#: and prints a verdict line in the framework's summary format.
GATE_WITH_BUDGET = """#!/bin/bash
GATE_BUDGET_SECONDS_ALL=999
echo '=== gate summary ==='
echo 'gate: FAIL (exit 1)'
exit 1
"""


def _project(tmp_path: Path, script: Optional[str] = GATE_WITH_BUDGET) -> Path:
    """A project directory whose gate script is *script*.

    ``script=None`` builds a project **without** a gate script — the
    case where there is nothing to ask about the gate's budget.
    """
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True, exist_ok=True)
    if script is not None:
        (project / "scripts" / "run_tests.sh").write_text(script, encoding="utf-8")
    return project


def _plan_dir(tmp_path: Path, contract: str) -> Path:
    """A plan directory holding a contract file with *contract* in it."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / CONTRACT_FILENAME).write_text(contract, encoding="utf-8")
    return plan_dir


def _vp(entry: str, **extra) -> Dict:
    """A ``full_ci`` verification point pointing at *entry*."""
    vp = {
        "id": "VP-902",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "phase_order": 1,
        "ci_entry": entry,
    }
    vp.update(extra)
    return vp


# --------------------------------------------------------------------------
# The budget: the contract declares it
# --------------------------------------------------------------------------


def test_a_contract_budget_overrides_the_gate_script(tmp_path, monkeypatch):
    """A contract that declares a budget owns that budget.

    The gate script declares 999 s and the contract declares 1234 s.
    The contract is the plan's own declaration about **its** gate, so
    its number is the one the watchdog is derived from.

    ``GATE_BUDGET_SECONDS`` is removed first on purpose: the operator
    override is read from the environment on both sides of the fence
    (the gate reads the same variable), so leaving it in place would
    test the operator's override instead of the contract.
    """
    monkeypatch.delenv("GATE_BUDGET_SECONDS", raising=False)
    project = _project(tmp_path)
    contract = load_ci_contract(_plan_dir(tmp_path, json.dumps({
        "budget_source": "contract",
        "budget_seconds": 1234,
    })))

    assert gate_budget_seconds(project, GATE_ENTRY, contract=contract) == 1234


def test_without_a_contract_budget_the_gate_script_still_wins(tmp_path, monkeypatch):
    """No contract budget -> the gate's own declaration stands.

    This is the zero-regression promise, in the file that can prove
    it: a plan that writes no budget declaration keeps the budget it
    has always had — the one the gate script publishes.
    """
    monkeypatch.delenv("GATE_BUDGET_SECONDS", raising=False)
    project = _project(tmp_path)

    assert gate_budget_seconds(project, GATE_ENTRY) == 999


def test_a_missing_gate_script_falls_back_to_the_vp_timeout(tmp_path, monkeypatch):
    """Nothing to ask -> the VP's own timeout stands.

    There is no gate script to read and the contract declares no
    budget, so the only number left is the one the VP declares.  The
    framework's floor for a whole-repo gate applies only when there is
    **no project** at all — here there is a project, it simply does not
    say anything about its gate.
    """
    monkeypatch.delenv("GATE_BUDGET_SECONDS", raising=False)
    project = _project(tmp_path, script=None)
    contract = load_ci_contract(_plan_dir(tmp_path, json.dumps({
        "inconclusive_exit_codes": [5],
    })))
    vp = _vp(GATE_ENTRY, ci_timeout_seconds=900)

    timeout = ci_timeout(
        vp, project_dir=project, entry=GATE_ENTRY, contract=contract,
    )

    assert timeout == 900


def test_the_watchdog_is_the_contract_budget_plus_a_margin(tmp_path, monkeypatch):
    """The watchdog is never shorter than the budget it watches.

    The contract declares 1234 s; the watchdog gets those seconds plus
    the margin the gate needs to finish its verdict — and it ignores a
    VP that asks for less, because a watchdog shorter than the gate is
    the failure this mechanism exists to prevent.
    """
    monkeypatch.delenv("GATE_BUDGET_SECONDS", raising=False)
    project = _project(tmp_path)
    contract = load_ci_contract(_plan_dir(tmp_path, json.dumps({
        "budget_source": "contract",
        "budget_seconds": 1234,
    })))
    vp = _vp(GATE_ENTRY, ci_timeout_seconds=900)

    timeout = ci_timeout(
        vp, project_dir=project, entry=GATE_ENTRY, contract=contract,
    )

    assert timeout == 1234 + GATE_BUDGET_MARGIN_SECONDS


def test_a_broken_contract_falls_back_to_the_gate_script_budget(tmp_path, monkeypatch):
    """A contract that is not JSON decides nothing.

    The contract is an **input**, not a criterion: a broken one may
    not change which budget the gate runs under.  It falls back to the
    framework defaults — the gate script's own declaration — and says
    in the evidence that it was not applied.
    """
    monkeypatch.delenv("GATE_BUDGET_SECONDS", raising=False)
    project = _project(tmp_path)
    contract = load_ci_contract(_plan_dir(tmp_path, "{oops"))

    assert contract.applied is False
    assert gate_budget_seconds(project, GATE_ENTRY, contract=contract) == 999


# --------------------------------------------------------------------------
# The summary format: the contract says whether there is one
# --------------------------------------------------------------------------


def test_a_contract_without_summary_format_does_not_parse_lane_summaries(tmp_path):
    """A contract that does not name a format gets no format guessed.

    The gate printed ``gate: FAIL (exit 1)`` — a line this
    repository's format would read as a verdict.  A contract that
    declares only its exit codes has not promised that its gate's
    output looks like this one, so nothing is parsed: the evidence
    carries no summary and no lane lines rather than a wrong reading
    of a foreign format.
    """
    project = _project(tmp_path)
    plan_dir = _plan_dir(tmp_path, json.dumps({
        "inconclusive_exit_codes": [5],
    }))

    result = run_ci_verification(
        _vp(GATE_ENTRY), project_dir=project, plan_dir=plan_dir,
    )

    assert result.evidence["lane_summaries"] == []
    assert result.evidence["gate_summary"] == {}


def test_summary_format_gate_v1_parses_lane_summaries(tmp_path):
    """A contract that names the format is parsed exactly as before.

    ``gate_v1`` is this repository's format, and the verdict reads it
    the way it always has.
    """
    project = _project(tmp_path)
    plan_dir = _plan_dir(tmp_path, json.dumps({
        "inconclusive_exit_codes": [5],
        "summary_format": "gate_v1",
    }))

    result = run_ci_verification(
        _vp(GATE_ENTRY), project_dir=project, plan_dir=plan_dir,
    )

    assert result.evidence["lane_summaries"], (
        "the gate's verdict line is not in the evidence:\n"
        f"{result.evidence}"
    )
    assert result.evidence["gate_summary"]["verdict"] == "FAIL"
    assert result.evidence["gate_summary"]["exit_code"] == 1
