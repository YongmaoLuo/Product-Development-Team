"""The judgment-point drift check must be wired into CI as a warning.

Why this gate exists
--------------------
``scripts/census_judgment_points.py --check`` compares the committed
judgment-point manifest with a fresh census of the tree and exits
non-zero when the two disagree.  That exit code is the whole point of
the script, so the wiring has to keep it: if the check is never run,
a judgment point can be added, changed or deleted without the manifest
noticing, and the manifest — which the static gates read as their
source of truth — quietly stops describing the code.

The first version of the check is a warning, not a gate.  A drifting
manifest is a real defect, but turning it red on the first day would
block every pull request that touches a judgment module, and
regenerating the manifest is not yet part of the reviewer's normal
loop.  So the step runs, prints its verdict, and is allowed to fail:
``continue-on-error: true`` keeps the job green while the drift stays
visible in the log.

Two ways that wiring can rot are pinned below:

* the step disappears, or is renamed — the drift stops being checked at
  all, and nothing in the tree says so;
* the step stops being non-blocking — a drift turns the ``static-gates``
  job red, which is the failure mode the warning-only rollout exists to
  avoid.

And one way it can be hollowed out: the step may report success
without ever running the census (``|| true`` at the end of the
command).  A check that cannot fail is not a check — the drift would
be reported as clean, which is worse than not reporting it at all.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Dict, List, Optional

import yaml

#: The repository root — this file lives at ``backend/tests/static_gates/``.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The workflow that holds every job.
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The job the drift check belongs to: the static contracts run there,
#: and it is the job every other lane depends on.
GATE_JOB = "static-gates"

#: The step's name, as it appears in ``ci.yml``.
DRIFT_STEP = "judgment-points-drift"

#: The census script the step must call, relative to the repository root.
CENSUS_SCRIPT = Path("scripts") / "census_judgment_points.py"

#: The manifest the census compares against, relative to the repository root.
MANIFEST = Path("docs") / "judgment_points.json"


def _workflow() -> Dict:
    """The parsed workflow file."""
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _gate_steps() -> List[Dict]:
    """The steps of the ``static-gates`` job."""
    jobs = _workflow().get("jobs") or {}
    assert GATE_JOB in jobs, (
        f"ci.yml has no {GATE_JOB!r} job; the drift check has nowhere to run"
    )
    return jobs[GATE_JOB].get("steps") or []


def _drift_step() -> Dict:
    """The drift step, or a failure that says where it is missing."""
    steps = _gate_steps()
    for step in steps:
        if step.get("name") == DRIFT_STEP:
            return step
    names = [step.get("name") for step in steps]
    raise AssertionError(
        f"the {GATE_JOB!r} job has no step named {DRIFT_STEP!r}; "
        f"its steps are: {names}"
    )


def _resolve(step: Dict, path_like: str) -> Path:
    """Resolve a path from the step's command against the working tree.

    A step's ``run`` executes in the job's checkout, so a relative path
    is relative to ``working-directory`` — the checkout root when the
    step does not set one.
    """
    working_directory = step.get("working-directory") or "."
    return (REPO_ROOT / working_directory / path_like).resolve()


def _option(tokens: List[str], name: str) -> Optional[str]:
    """The value of ``--name value`` or ``--name=value`` in ``tokens``."""
    for index, token in enumerate(tokens):
        if token == name:
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if token.startswith(f"{name}="):
            return token.split("=", 1)[1]
    return None


def test_drift_step_exists_in_static_gates() -> None:
    """The static-gates job runs the drift check under a known name.

    The name is the handle every other reference in this repository
    uses, so it is pinned here rather than left to a name the job
    happens to use today.
    """
    names = [step.get("name") for step in _gate_steps()]
    assert DRIFT_STEP in names, (
        f"the {GATE_JOB!r} job has no step named {DRIFT_STEP!r}; "
        f"its steps are: {names}"
    )


def test_step_runs_census_check() -> None:
    """The step calls the census script in check mode, directly.

    "Directly" is the load-bearing word.  A trailing ``|| true`` would
    leave the step green whatever the census said, and the drift would
    then be reported as clean — a worse outcome than not checking at
    all, because the log would say so.
    """
    step = _drift_step()
    run = step.get("run") or ""
    tokens = shlex.split(run)

    assert "--check" in tokens, (
        f"the {DRIFT_STEP!r} step must ask the census to compare the "
        f"manifest rather than write it; its command is: {run!r}"
    )

    scripts = [token for token in tokens if token.endswith(CENSUS_SCRIPT.name)]
    assert len(scripts) == 1, (
        f"the {DRIFT_STEP!r} step must call {CENSUS_SCRIPT.name} exactly "
        f"once; its command is: {run!r}"
    )
    resolved = _resolve(step, scripts[0])
    assert resolved == (REPO_ROOT / CENSUS_SCRIPT).resolve(), (
        f"the census script the step calls resolves to {resolved}, "
        f"which is not this repository's {CENSUS_SCRIPT}"
    )
    assert resolved.is_file(), f"the census script {resolved} does not exist"

    root = _option(tokens, "--root")
    assert root is not None, (
        f"the {DRIFT_STEP!r} step must pass --root so the census reads "
        f"the checkout it runs in; its command is: {run!r}"
    )
    census_root = _resolve(step, root)
    assert (census_root / MANIFEST).is_file(), (
        f"the census root {census_root} holds no {MANIFEST}, so the check "
        f"would compare the census against nothing"
    )

    assert "||" not in run, (
        f"the {DRIFT_STEP!r} step must call the census directly; the '||' "
        f"in {run!r} would hide the census's own failures"
    )


def test_step_cannot_fail_the_job() -> None:
    """The step is allowed to fail, so drift stays a warning.

    The rollout decision this rollout records is that a drifting
    manifest warns in the first version.  Without ``continue-on-error:
    true`` the step's non-zero exit turns the ``static-gates`` job red,
    and with it every lane that depends on that job.
    """
    step = _drift_step()
    assert step.get("continue-on-error") is True, (
        f"the {DRIFT_STEP!r} step must carry 'continue-on-error: true': a "
        f"drifting manifest is a warning in this rollout, and a red step "
        f"would turn the {GATE_JOB!r} job red with it. The step currently "
        f"reads: {step}"
    )
