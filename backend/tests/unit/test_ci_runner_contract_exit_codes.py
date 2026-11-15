"""The exit codes that mean "the gate could not decide" come from the plan.

``scripts/run_tests.sh`` in this repository uses exit code 3 for "the
machine could not decide", and the framework used to hard-code that
number.  A plan whose gate speaks a different language had no way to say
so, so a run that could not decide was filed as a code failure — and a
code failure becomes a repair task against a delivery that never
failed.  The contract file ``<plan_dir>/ci_contract.json`` gives the
plan a way to declare its own exit codes; this file pins what the
runner does with that declaration.

Covers:
  * a **declared** exit code is INCONCLUSIVE, not FAILED;
  * an exit code the contract does **not** declare is FAILED
    (``gate_failed``);
  * **no contract** keeps the historical verdict exactly — an exit 3 that
    said nothing is still FAILED, and still carries
    ``gate_inconclusive``;
  * a **broken** contract changes nothing: it falls back to the
    framework defaults and says so in the evidence;
  * the agent reads the contract **from its own plan directory** — the
    plan directory is the one the plan lives in;
  * the evidence carries the contract that was applied, so a reader can
    tell which declaration the verdict was based on.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from unittest.mock import Mock


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_ci_runner as ci_runner  # noqa: E402
from ci_contract import CONTRACT_FILENAME  # noqa: E402
from verification_ci_runner import (  # noqa: E402
    STATUS_FAILED,
    STATUS_INCONCLUSIVE,
    run_ci_verification,
)


def _vp(entry: str, **extra) -> dict:
    """A ``full_ci`` verification point pointing at *entry*."""
    vp = {
        "id": "VP-901",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "phase_order": 1,
        "ci_entry": entry,
    }
    vp.update(extra)
    return vp


def _project(tmp_path: Path, exit_code: int) -> Path:
    """A project whose gate says nothing and exits *exit_code*."""
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True, exist_ok=True)
    gate = project / "scripts" / "run_tests.sh"
    gate.write_text(f"#!/bin/bash\nexit {exit_code}\n", encoding="utf-8")
    return project


def _plan_dir(tmp_path: Path, contract: str = "") -> Path:
    """A plan directory, with a contract file when *contract* is given."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    if contract:
        (plan_dir / CONTRACT_FILENAME).write_text(contract, encoding="utf-8")
    return plan_dir


def _contract_with(**fields) -> str:
    """A contract file body declaring *fields*."""
    return json.dumps(fields)


# --------------------------------------------------------------------------
# The declared exit code
# --------------------------------------------------------------------------


def test_a_declared_exit_code_is_inconclusive(tmp_path):
    """Exit 7 with a contract that declares 7 -> INCONCLUSIVE.

    The gate said nothing at all — it just left with 7.  The contract
    says 7 means "could not decide", and that is the whole point of the
    contract: the plan knows its gate's language, the framework does not.
    """
    project = _project(tmp_path, 7)
    plan_dir = _plan_dir(tmp_path, _contract_with(inconclusive_exit_codes=[7]))

    result = run_ci_verification(
        _vp("bash scripts/run_tests.sh"),
        project_dir=project,
        plan_dir=plan_dir,
    )

    assert result.status == STATUS_INCONCLUSIVE
    # Every INCONCLUSIVE answer names itself in the evidence.  The
    # triage reads "no signal" as "no verdict at all" and files the
    # round as a deliverable defect — which is the opposite of what an
    # undecided gate is.  The signal is ``gate_inconclusive``, not a
    # failure signal: the verdict is still not a failure.
    assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_INCONCLUSIVE


def test_a_code_not_in_the_contract_is_failed(tmp_path):
    """Exit 7 with a contract that declares only 3 -> FAILED.

    The contract is the plan's own declaration, so a code it does not
    name is a code failure — exactly like the framework's own default
    treats every code it does not know.
    """
    project = _project(tmp_path, 7)
    plan_dir = _plan_dir(tmp_path, _contract_with(inconclusive_exit_codes=[3]))

    result = run_ci_verification(
        _vp("bash scripts/run_tests.sh"),
        project_dir=project,
        plan_dir=plan_dir,
    )

    assert result.status == STATUS_FAILED
    assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_FAILED


# --------------------------------------------------------------------------
# No contract -> the historical verdict, word for word
# --------------------------------------------------------------------------


def test_without_a_contract_exit_three_said_nothing_is_still_failed(tmp_path):
    """Exit 3 with no plan directory -> FAILED, as it has always been.

    The zero-regression promise: a plan that writes no contract keeps
    the verdict it has always got.  An exit 3 that printed no summary is
    not a gate that spoke, so it stays FAILED — but it keeps the
    ``gate_inconclusive`` signal, so the triage still files it as an
    infrastructure problem rather than a broken deliverable.
    """
    project = _project(tmp_path, 3)

    result = run_ci_verification(
        _vp("bash scripts/run_tests.sh"), project_dir=project,
    )

    assert result.status == STATUS_FAILED
    assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_INCONCLUSIVE


# --------------------------------------------------------------------------
# The agent reads the contract of the plan it belongs to
# --------------------------------------------------------------------------


def test_the_agent_reads_the_contract_from_its_plan_dir(tmp_path):
    """The agent hands its own plan directory to the runner.

    The contract lives next to the plan, so the plan directory is the
    only place the runner has to look — and the agent is the one that
    knows which plan it is running.
    """
    from verification_agent import VerificationAgent

    project = _project(tmp_path, 7)
    plan_dir = _plan_dir(tmp_path, _contract_with(inconclusive_exit_codes=[7]))

    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project, coding_tool=Mock(),
    )
    verdict = agent._run_full_ci_vp(_vp("bash scripts/run_tests.sh"))

    assert verdict["status"] == STATUS_INCONCLUSIVE


# --------------------------------------------------------------------------
# The evidence carries the contract that was applied
# --------------------------------------------------------------------------


def test_the_evidence_carries_the_contract(tmp_path):
    """The verdict says which declaration it was based on."""
    project = _project(tmp_path, 1)
    plan_dir = _plan_dir(
        tmp_path,
        _contract_with(
            inconclusive_exit_codes=[7], summary_format="gate_v1",
        ),
    )

    result = run_ci_verification(
        _vp("bash scripts/run_tests.sh"),
        project_dir=project,
        plan_dir=plan_dir,
    )

    contract = result.evidence["ci_contract"]
    assert contract["path"] == str(plan_dir / CONTRACT_FILENAME)
    assert contract["applied"] is True
    assert contract["inconclusive_exit_codes"] == [7]
    assert contract["summary_format"] == "gate_v1"
    assert contract["error"] is None


# --------------------------------------------------------------------------
# A broken contract changes nothing
# --------------------------------------------------------------------------


def test_a_broken_contract_does_not_change_the_verdict(tmp_path):
    """A contract that is not JSON -> the framework defaults, unchanged.

    The contract is an **input**, not a criterion: a broken one may not
    turn a red gate green or a green gate red.  It falls back to the
    framework defaults and records why in the evidence.
    """
    project = _project(tmp_path, 1)
    plan_dir = _plan_dir(tmp_path, "{oops")

    result = run_ci_verification(
        _vp("bash scripts/run_tests.sh"),
        project_dir=project,
        plan_dir=plan_dir,
    )

    assert result.status == STATUS_FAILED
    assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_FAILED
    assert result.evidence["ci_contract"]["applied"] is False


def test_the_runner_survives_a_contract_reader_that_raises(
    tmp_path, monkeypatch, caplog,
):
    """A contract loader that raises must not take the gate down.

    The loader promises never to raise, but the runner must not lean on
    that promise: the contract is an **input**, and an input that
    explodes may not decide the verdict.  The gate still runs, on the
    framework's defaults, and the reason is said out loud.
    """
    project = _project(tmp_path, 7)
    plan_dir = _plan_dir(tmp_path)

    def _explode(_plan_dir):
        raise RuntimeError("the contract reader blew up")

    monkeypatch.setattr(ci_runner, "load_ci_contract", _explode)

    with caplog.at_level(logging.WARNING):
        result = run_ci_verification(
            _vp("bash scripts/run_tests.sh"),
            project_dir=project,
            plan_dir=plan_dir,
        )

    # The gate ran and the verdict is the framework's own: exit 7 is not
    # a code any contract declares, so it is a failure.
    assert result.status == STATUS_FAILED
    assert result.evidence["ci_contract"]["applied"] is False
    assert result.evidence["ci_contract"]["error"]
    assert [r.getMessage() for r in caplog.records], (
        "a contract that could not be loaded must be said out loud"
    )


# --------------------------------------------------------------------------
# The runner that judges is the tree's own copy
# --------------------------------------------------------------------------


def test_the_tree_under_tests_own_runner_is_the_one_that_judges(tmp_path):
    """The tree's runner is loaded — and it is the tree's runner.

    2026-11-06: the tree under test's ``verification_ci_runner`` could
    not be **loaded at all**.  It imports ``ci_contract``, and the
    loader only puts the *harness'* ``backend/`` on ``sys.path``, so the
    import raised ``ModuleNotFoundError`` and the loader fell back to
    the harness' own copy.  The plan's contract was therefore never
    read by anybody: the whole feature was dark, and the verdict said
    so only in a field nobody was looking at.

    A tree that is a checkout of this framework is judged by its own
    runner — that is the whole point of the loader.
    """
    import verification_ci_runner_loader as loader

    checkout = tmp_path / "checkout"
    backend = checkout / "backend"
    backend.mkdir(parents=True)
    shutil.copy(
        _BACKEND_DIR / "verification_ci_runner.py",
        backend / "verification_ci_runner.py",
    )
    shutil.copy(
        _BACKEND_DIR / "ci_contract.py", backend / "ci_contract.py",
    )
    (backend / "pyproject.toml").write_text(
        "[project]\nname = 'framework'\n", encoding="utf-8",
    )

    loaded = loader.load_ci_runner(checkout)

    assert loaded.provenance["resolution"] == loader.RESOLUTION_PROJECT, (
        "the tree under test's own runner was not the one that judged: "
        "%r" % (loaded.provenance,)
    )
    assert loaded.provenance["fallback_reason"] == ""
    assert Path(loaded.provenance["module_path"]) == (
        backend / "verification_ci_runner.py"
    ).resolve()


def test_the_runner_finds_its_own_contract_module(tmp_path):
    """The runner reads the contract module that belongs to it.

    The runner is loaded **by path**, so a flat ``from ci_contract
    import ...`` resolves through ``sys.path`` — where the harness'
    copy sits, and where the tree's copy usually is not.  The contract
    code that belongs to a runner is the one next to it.
    """
    checkout = tmp_path / "checkout"
    backend = checkout / "backend"
    backend.mkdir(parents=True)
    shutil.copy(
        _BACKEND_DIR / "verification_ci_runner.py",
        backend / "verification_ci_runner.py",
    )
    shutil.copy(
        _BACKEND_DIR / "ci_contract.py", backend / "ci_contract.py",
    )
    (backend / "pyproject.toml").write_text(
        "[project]\nname = 'framework'\n", encoding="utf-8",
    )

    import verification_ci_runner_loader as loader

    loaded = loader.load_ci_runner(checkout)

    module = loaded.module
    assert module.ci_contract_module_path == str(
        (backend / "ci_contract.py").resolve()
    ), (
        "the runner read its contract module from %r; the module next "
        "to the runner is the one that belongs to it"
        % (module.ci_contract_module_path,)
    )


def test_the_tree_runner_loads_when_nothing_else_provides_the_contract(
    tmp_path, monkeypatch,
):
    """The harness has no ``ci_contract`` at all — the runner still loads.

    This is the 2026-11-06 failure, reproduced exactly: the harness'
    ``backend/`` sat on ``sys.path`` and carried **no** ``ci_contract``,
    so the tree's runner could not import one and the loader fell back
    to the harness' copy.  The plan's contract was read by nobody.

    ``sys.modules["ci_contract"] = None`` is what the import system sees
    when a name cannot be found: ``import ci_contract`` refuses instead
    of reaching for a file somewhere else.
    """
    checkout = tmp_path / "checkout"
    backend = checkout / "backend"
    backend.mkdir(parents=True)
    shutil.copy(
        _BACKEND_DIR / "verification_ci_runner.py",
        backend / "verification_ci_runner.py",
    )
    shutil.copy(
        _BACKEND_DIR / "ci_contract.py", backend / "ci_contract.py",
    )
    (backend / "pyproject.toml").write_text(
        "[project]\nname = 'framework'\n", encoding="utf-8",
    )

    monkeypatch.setitem(sys.modules, "ci_contract", None)

    import verification_ci_runner_loader as loader

    loaded = loader.load_ci_runner(checkout)

    assert loaded.provenance["resolution"] == loader.RESOLUTION_PROJECT, (
        "the tree under test's own runner was not the one that judged: "
        "%r" % (loaded.provenance,)
    )
    assert loaded.provenance["fallback_reason"] == ""


def test_the_agent_survives_a_harness_without_a_contract_module(
    tmp_path, monkeypatch,
):
    """The agent still finds the contract when it cannot import one.

    The agent's import of ``ci_contract`` sat on the way into
    ``_run_full_ci_vp`` — one flat import away from taking the whole
    full-CI verification point down with it, in a harness that carries
    no ``ci_contract`` at all.  The contract is optional; the agent is
    not allowed to die over it.
    """
    from verification_agent import VerificationAgent

    project = _project(tmp_path, 7)
    plan_dir = _plan_dir(tmp_path, _contract_with(inconclusive_exit_codes=[7]))

    monkeypatch.setitem(sys.modules, "ci_contract", None)

    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project, coding_tool=Mock(),
    )
    verdict = agent._run_full_ci_vp(_vp("bash scripts/run_tests.sh"))

    assert verdict["status"] == STATUS_INCONCLUSIVE, (
        "the agent did not hand the plan's contract to the runner: %r"
        % (verdict,)
    )


def test_the_agent_names_the_contract_after_the_tree_under_test(tmp_path):
    """The tree's own runner names the contract file — and applies it.

    The runner that judges is the tree's own copy, and the contract
    module it reads is the one **next to it**.  So a tree whose contract
    file has another name is read under that name, and the agent hands
    the plan directory over because it found the file under that name.

    This is the whole fix in one test: the tree's runner loaded, the
    tree's contract module read, the plan's contract found, the verdict
    INCONCLUSIVE.
    """
    from verification_agent import VerificationAgent

    project = tmp_path / "checkout"
    backend = project / "backend"
    backend.mkdir(parents=True)
    scripts = project / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "run_tests.sh").write_text(
        "#!/bin/bash\nexit 7\n", encoding="utf-8",
    )
    shutil.copy(
        _BACKEND_DIR / "verification_ci_runner.py",
        backend / "verification_ci_runner.py",
    )
    shutil.copy(
        _BACKEND_DIR / "ci_contract.py", backend / "ci_contract.py",
    )
    (backend / "pyproject.toml").write_text(
        "[project]\nname = 'framework'\n", encoding="utf-8",
    )
    # The tree's contract file has a name of its own.
    (backend / "ci_contract.py").write_text(
        (backend / "ci_contract.py").read_text(encoding="utf-8").replace(
            'CONTRACT_FILENAME = "ci_contract.json"',
            'CONTRACT_FILENAME = "gate_contract.json"',
        ),
        encoding="utf-8",
    )

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "gate_contract.json").write_text(
        _contract_with(inconclusive_exit_codes=[7]), encoding="utf-8",
    )

    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project, coding_tool=Mock(),
    )
    verdict = agent._run_full_ci_vp(_vp("bash scripts/run_tests.sh"))

    assert verdict["status"] == STATUS_INCONCLUSIVE, (
        "the tree's own contract was not applied: %r" % (verdict,)
    )