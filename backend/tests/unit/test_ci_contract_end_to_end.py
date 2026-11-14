"""The CI contract, end to end: a real gate script and a real plan directory.

The files that came before this one each pinned one link of the chain:
the parser reads a contract out of a plan directory, the runner honours
the exit codes that contract declares.  Neither of them walked the whole
way from ``VerificationAgent.plan_dir`` — the place the plan actually
lives — to a verdict, so a break anywhere between the two links would
show up as two healthy halves and one broken chain.

This file walks it.  A real ``bash`` script leaves with 5 and prints a
line the framework's summary format cannot read; the same script, the
same plan directory and the same agent give two different verdicts
depending on whether the plan declares what 5 means:

* with a ``ci_contract.json`` saying 5 means "the gate could not
  decide" -> **INCONCLUSIVE**, because a machine that could not decide
  has not proven the delivery defective;
* without one -> **FAILED** with ``gate_failed``, the verdict every
  plan that writes no contract has always got.

The second half is not a formality.  It is the zero-regression promise,
and the only way to prove it is to run the very same script without the
declaration and see the very same FAILED verdict.

Nothing here is mocked.  The gate is a script on disk, it is executed,
and its exit code is the input to the verdict — so what is under test is
the chain, not a stand-in for it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from ci_contract import CONTRACT_FILENAME  # noqa: E402
from verification_agent import VerificationAgent  # noqa: E402
from verification_ci_runner import (  # noqa: E402
    SIGNAL_GATE_FAILED,
    STATUS_FAILED,
    STATUS_INCONCLUSIVE,
)

#: The VP the end-to-end run grades.
VP_ID = "VP-E2E"

#: The gate: a real script that says something the framework's summary
#: format cannot read, and leaves with 5.  5 is not the framework's own
#: "could not decide" code (that is 3), so the verdict depends on the
#: plan's declaration rather than on the framework's own default.
GATE_SCRIPT = "#!/bin/bash\necho 'FOREIGN SUMMARY'\nexit 5\n"

#: The contract the plan writes: 5 means "could not decide", and the
#: output is not in the framework's summary format (so nothing is
#: parsed out of the gate's noise).
CONTRACT_BODY = json.dumps({
    "inconclusive_exit_codes": [5],
    "summary_format": "none",
})


def _gate(tmp_path: Path) -> Path:
    """The gate script: a real file on disk, run by a real shell."""
    gate = tmp_path / "gate.sh"
    gate.write_text(GATE_SCRIPT, encoding="utf-8")
    gate.chmod(0o755)
    return gate


def _plan_dir(tmp_path: Path) -> Path:
    """A plan directory that holds the contract."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / CONTRACT_FILENAME).write_text(CONTRACT_BODY, encoding="utf-8")
    return plan_dir


def _project(tmp_path: Path) -> Path:
    """The project the gate runs in — an empty directory is enough."""
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    return project


def _vp(entry: str) -> dict:
    """A ``full_ci`` verification point pointing at *entry*."""
    return {
        "id": VP_ID,
        "verification_method": "full_ci",
        "verification_phase": 2,
        "ci_entry": entry,
    }


def _verdict_text(verdict: dict) -> str:
    """The verdict, in full.

    Every assertion in this file carries it: a failure that says only
    "expected INCONCLUSIVE" leaves the reader to go and find the run
    that disagreed, and the run is the thing under test.
    """
    try:
        return json.dumps(verdict, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(verdict)


def _run(plan_dir: Path, project_dir: Path, gate: Path) -> dict:
    """Grade one ``full_ci`` VP the way the framework does.

    The agent is handed the plan directory it belongs to — that is the
    only place the contract can be, and the only place the audit
    artifacts can land.
    """
    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )
    return agent._run_full_ci_vp(_vp(f"bash {gate}"))


# --------------------------------------------------------------------------
# With a contract: the plan's own exit code means INCONCLUSIVE
# --------------------------------------------------------------------------


def test_end_to_end_a_plan_contract_turns_exit_five_into_inconclusive(tmp_path):
    """A contract that declares 5 -> the run is INCONCLUSIVE.

    The gate left with 5 without saying anything about the code.  The
    plan declares that 5 means "could not decide", so the framework
    files the run as INCONCLUSIVE: a machine that could not decide has
    not proven a delivery defective, and a FAILED verdict here would
    become a repair task against code that never failed.
    """
    plan_dir = _plan_dir(tmp_path)
    project = _project(tmp_path)
    gate = _gate(tmp_path)

    verdict = _run(plan_dir, project, gate)

    assert verdict["status"] == STATUS_INCONCLUSIVE, _verdict_text(verdict)
    # The verdict says which declaration it was based on — a reader can
    # tell that this one was not the framework's own default.
    contract = verdict["evidence"]["ci_contract"]
    assert contract["applied"] is True, _verdict_text(verdict)
    assert contract["inconclusive_exit_codes"] == [5], _verdict_text(verdict)
    # The audit copy of the run lands next to the plan, under the VP.
    artifact = plan_dir / "vp_artifacts" / VP_ID / "ci_output.json"
    assert artifact.is_file(), _verdict_text(verdict)


# --------------------------------------------------------------------------
# Without a contract: the historical verdict, unchanged
# --------------------------------------------------------------------------


def test_end_to_end_without_a_contract_exit_five_is_failed(tmp_path):
    """The same gate, the contract deleted -> FAILED, as it always was.

    This is the zero-regression promise measured end to end: the very
    same script, run from the very same plan directory, produces the
    verdict a plan that writes no contract has always got.  The exit
    code is 5, the signal is ``gate_failed``, and nothing about the
    contract machinery is visible in the verdict.
    """
    plan_dir = _plan_dir(tmp_path)
    # The contract is there and then it is not: the plan directory is
    # the same directory, and the only difference is the file.
    (plan_dir / CONTRACT_FILENAME).unlink()
    project = _project(tmp_path)
    gate = _gate(tmp_path)

    verdict = _run(plan_dir, project, gate)

    assert verdict["status"] == STATUS_FAILED, _verdict_text(verdict)
    assert verdict["evidence"]["exit_code"] == 5, _verdict_text(verdict)
    assert verdict["evidence"]["signal"] == SIGNAL_GATE_FAILED, _verdict_text(verdict)
    # No contract was applied, and the evidence says so rather than
    # pretending a declaration was found.
    contract = verdict["evidence"]["ci_contract"]
    assert contract["applied"] is False, _verdict_text(verdict)
    assert contract["path"] is None, _verdict_text(verdict)


# --------------------------------------------------------------------------
# The audit artifact carries the contract
# --------------------------------------------------------------------------


def test_end_to_end_the_artifact_records_the_contract(tmp_path):
    """The artifact on disk names the contract the verdict was based on.

    ``ci_output.json`` is what a reader opens when the verdict has to
    be re-examined later, so it has to say which declaration produced
    it — and the path has to point into **this** plan directory, not at
    some other plan's contract.
    """
    plan_dir = _plan_dir(tmp_path)
    project = _project(tmp_path)
    gate = _gate(tmp_path)

    verdict = _run(plan_dir, project, gate)

    artifact = plan_dir / "vp_artifacts" / VP_ID / "ci_output.json"
    assert artifact.is_file(), _verdict_text(verdict)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    # The artifact is the audit copy of **this** run — not of some other
    # one: it names this VP and carries the exit code the gate left
    # with, and the verdict that exit code produced is the one the
    # reader is being sent back to.
    assert payload["vp_id"] == VP_ID, _verdict_text(verdict)
    assert payload["exit_code"] == 5, _verdict_text(verdict)
    assert verdict["status"] == STATUS_INCONCLUSIVE, _verdict_text(verdict)
    contract = payload["ci_contract"]
    assert contract["applied"] is True, _verdict_text(verdict)
    assert contract["path"] == str(plan_dir / CONTRACT_FILENAME), _verdict_text(verdict)
    assert contract["inconclusive_exit_codes"] == [5], _verdict_text(verdict)
    assert contract["summary_format"] == "none", _verdict_text(verdict)