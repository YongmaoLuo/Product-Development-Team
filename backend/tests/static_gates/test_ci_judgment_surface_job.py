"""The judgment-surface consistency checks have to run on the pull request.

Why this file exists
--------------------
The judgment surface is described by three artifacts that are allowed to
drift apart:

* ``docs/judgment_points.json`` — the **manifest**: which functions decide
  PASS/FAIL, and what pins each of them;
* the **decision tables** the manifest's Tier A rows name — a table that
  was renamed or emptied is a claim about a table that is not there;
* the **behavioural goldens** (``backend/tests/fixtures/judgment_snapshots.json``)
  — what those functions actually return, frozen byte for byte.

Each of the three has its own checker in this repository, and each of
them is a *script* or a *test file* — which means that until something
runs them on a runner they are files in a repository and nothing else.
Three separate entry points also means three separate ways for the
chain to rot: the manifest can drift while the tables stay honest, the
goldens can drift while the manifest stays honest, and any of the three
can stop running entirely while the other two keep reporting green.

So this line of work puts the three behind **one** entry point,
``scripts/ci_judgment_surface.sh``, and runs that entry point as its own
pull-request job. This file is what stops the wiring from quietly
rotting. Each way it can rot is one case below:

* the script stops running one of the three checks — the chain is broken
  in the middle and the two ends still report green;
* the job disappears, or is renamed — nothing runs the script at all;
* the job's checkout goes shallow — the job checks a truncated tree, so
  its verdict is about a tree nobody reviewed;
* the job stays but leaves the merge gate's ``needs`` — it goes red and
  nothing closes, which is the failure mode every other gate in this
  repository was written to avoid.

Scope, stated honestly: this reads the script and the workflow file and
asserts on their structure, plus one bounded run of the script against a
root that holds no repository. It does not run the three checks against
*this* tree — that is the script's job, and the end-to-end assertion
that it passes here belongs to the acceptance suite, not to a gate that
would otherwise be red for reasons that have nothing to do with its
wiring.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

#: The repository root — three levels up from ``backend/tests/static_gates/``.
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The script that runs the three checks.
SCRIPT = REPO_ROOT / "scripts" / "ci_judgment_surface.sh"

#: The job that runs the script.
JOB = "judgment-surface-consistency"

#: The terminal job that decides whether a pull request may merge.
MERGE_GATE_JOB = "merge-gate"

#: The census checker — the manifest's own drift check. It lives in the
#: tree under test and is told which tree that is with ``--root``.
CENSUS = Path("scripts") / "census_judgment_points.py"

#: The golden checker — the behavioural snapshots' drift check.
SNAPSHOTS = Path("scripts") / "judgment_snapshots.py"

#: The three static gates, in the order the script runs them: the
#: manifest gate, the tables gate, the coverage gate. These are test
#: *files*, so the script runs them with pytest — and they resolve the
#: tree they inspect from their own location, which is why they have to
#: be the ones in the tree under test.
STATIC_GATES = (
    Path("backend/tests/static_gates/test_judgment_points_manifest.py"),
    Path("backend/tests/static_gates/test_judgment_points_tables_exist.py"),
    Path("backend/tests/static_gates/test_judgment_snapshot_coverage.py"),
)

#: The line the script prints when all three checks passed.
OK_LINE = "JUDGMENT SURFACE: OK (3/3 checks)"


@pytest.fixture(scope="module")
def workflow() -> dict:
    if not WORKFLOW.is_file():
        pytest.fail(
            f"{WORKFLOW} does not exist; there is no CI to wire the "
            f"judgment-surface checks into"
        )
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jobs(workflow: dict) -> dict:
    return workflow.get("jobs") or {}


def _needs_of(job: dict) -> list[str]:
    """The job's ``needs:`` as a list.

    GitHub accepts both a scalar (``needs: unit-tests``) and a sequence,
    so both shapes are normalised here — a gate that only understood one
    of them would pass on the shape it knows and miss the other.
    """
    needs = job.get("needs")
    if needs is None:
        return []
    if isinstance(needs, str):
        return [needs]
    return [str(name) for name in needs]


def _checkout_step(job: dict) -> dict | None:
    """The step that checks the repository out, if there is one."""
    for step in job.get("steps") or []:
        if isinstance(step, dict) and "actions/checkout" in str(step.get("uses", "")):
            return step
    return None


def _run_steps(job: dict) -> list[dict]:
    """The job's steps that carry a ``run:`` body."""
    return [
        step
        for step in (job.get("steps") or [])
        if isinstance(step, dict) and step.get("run")
    ]


def test_script_runs_three_checks(tmp_path: Path) -> None:
    """The script runs the census, the three static gates and the goldens.

    Three things have to be true at once, and each of them is a way the
    script can be present but useless:

    * it names **all three** checkers — the manifest's census, the three
      static gates, and the behavioural goldens. A script that runs two
      of the three leaves a hole exactly where the chain is weakest;
    * it runs them **in order** — census, gates, goldens — because each
      one describes the tree the next one reads;
    * it fails. The run below points the script at a root that holds no
      repository, and requires a non-zero exit and no success line: a
      script that cannot go red is not a check, and one that prints the
      success line no matter what it found is worse than not running at
      all.

    The failure is also required to be **exit 1** rather than the
    failing check's own status. The status belongs to the check; the
    script's contract is "this tree is not consistent", and every
    consumer of it — the CI job, a developer at a terminal — reads one
    number.
    """
    assert SCRIPT.is_file(), (
        f"{SCRIPT} does not exist; the three checks have no entry point, "
        f"so the job that is supposed to run them runs nothing"
    )
    text = SCRIPT.read_text(encoding="utf-8")

    assert "set -euo pipefail" in text, (
        "the script does not carry `set -euo pipefail`. Without `-u` an "
        "undefined variable expands to nothing and the check it was "
        "supposed to run silently does not run; without `pipefail` a "
        "pipeline reports the exit code of its last stage."
    )

    for checker in (CENSUS, SNAPSHOTS, *STATIC_GATES):
        assert checker.name in text, (
            f"the script does not mention {checker}. The three checks are "
            f"the manifest's census, the three static gates (manifest, "
            f"tables, coverage) and the behavioural goldens; a script that "
            f"leaves one of them out reports the chain as consistent while "
            f"one link of it is not checked at all."
        )

    assert text.count("--check") >= 2, (
        "the script does not run both drift checkers in `--check` mode. "
        "Without `--check` the census and the goldens *print* their "
        "contents instead of comparing them, which turns two of the three "
        "checks into a way of reading a file."
    )

    order = [
        text.index(CENSUS.name),
        text.index(STATIC_GATES[0].name),
        text.index(SNAPSHOTS.name),
    ]
    assert order == sorted(order), (
        "the script does not run the three checks in order — census, "
        "static gates, goldens. Each one describes the tree the next one "
        "reads, so the order is part of what the script means."
    )

    assert OK_LINE in text, (
        f"the script never prints `{OK_LINE}`. The success line is what a "
        f"reader — the CI log, a developer at a terminal — sees, and it is "
        f"what says how many of the three checks passed."
    )

    # The behavioural half: a gate that cannot fail is not a gate.
    empty_root = tmp_path / "not-a-repository"
    empty_root.mkdir()
    completed = subprocess.run(
        ["bash", str(SCRIPT), "--root", str(empty_root)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHON": sys.executable},
    )
    assert completed.returncode == 1, (
        f"the script exited {completed.returncode} against a root that "
        f"holds no repository. It has to exit 1: the status of the check "
        f"that failed is the check's business, and the script's contract "
        f"is one number. stdout was:\n{completed.stdout}\nstderr was:\n"
        f"{completed.stderr}"
    )
    assert OK_LINE not in completed.stdout, (
        f"the script printed `{OK_LINE}` against a root that holds no "
        f"repository. It cannot tell whether the three checks passed, so "
        f"the success line is not a fact about this tree.\n"
        f"{completed.stdout}"
    )
    assert re.search(
        r"^JUDGMENT SURFACE: (OK|WARN|FAILED) \(\d+/\d+ checks",
        completed.stdout + completed.stderr,
        re.MULTILINE,
    ), (
        f"the script's summary line is missing or malformed. It is the "
        f"line a reader — the CI log, a developer at a terminal — takes "
        f"the verdict from, and it has to say how many of the three "
        f"checks passed. stdout was:\n{completed.stdout}\nstderr was:\n"
        f"{completed.stderr}"
    )


def test_job_calls_script_and_is_pr_scoped(jobs: dict) -> None:
    """There is a job that runs the script, and it runs on pull requests.

    The job is pull-request-only because that is the event the merge
    gate aggregates: a job that also answered to a dispatch or a
    schedule would report a result the merge gate never reads, and the
    pull request would be gated by a check whose verdict came from
    somewhere else.

    The checkout has to be deep. The three checks read the tree — the
    manifest, the tables it names, the goldens — and a depth-1 clone is
    a truncated view of that tree: the job would then be answering a
    question about a tree nobody reviewed.
    """
    assert JOB in jobs, (
        f"ci.yml has no `{JOB}` job, so the three judgment-surface checks "
        f"never run in CI. A script nobody runs is a file in a repository "
        f"and nothing else."
    )
    job = jobs[JOB]

    condition = str(job.get("if", ""))
    assert "github.event_name == 'pull_request'" in condition, (
        f"the `{JOB}` job's `if:` is {condition!r}, which does not admit a "
        f"pull request. The merge gate aggregates the pull request's jobs, "
        f"so a check that answers to another event is a check whose "
        f"verdict the merge gate never reads."
    )

    checkout = _checkout_step(job)
    assert checkout is not None, (
        f"the `{JOB}` job does not check the repository out, so the script "
        f"has no tree to check"
    )
    depth = (checkout.get("with") or {}).get("fetch-depth")
    assert str(depth) == "0", (
        f"the `{JOB}` job's checkout has fetch-depth={depth!r}. The three "
        f"checks read the tree itself, and actions/checkout's default (1) "
        f"is a shallow clone — a truncated view of it. Use fetch-depth: 0."
    )

    steps = _run_steps(job)
    holders = [
        step
        for step in steps
        if "scripts/ci_judgment_surface.sh" in str(step.get("run", ""))
    ]
    assert holders, (
        f"the `{JOB}` job does not run scripts/ci_judgment_surface.sh. The "
        f"script is the thing that runs the three checks; a job that does "
        f"not call it gates nothing, however green it is."
    )


def test_job_in_merge_gate_needs(jobs: dict) -> None:
    """The merge gate covers the judgment-surface job.

    A job outside the gate's ``needs`` is unmonitored: if it goes red,
    nothing closes, and if it is skipped, nothing complains. That is the
    whole reason this repository has a terminal merge gate — see
    ``test_merge_gate_contract.py`` — and a new pull-request job that is
    not in it is exactly the unmonitored job that gate exists to catch.

    This is the same contract from the other side: that file derives the
    set of pull-request jobs from the workflow and requires each of them
    to be in the gate's ``needs``, and this one asks the question by
    name. Both have to hold — the derived check catches a job added
    later, the named check catches a job whose key was renamed.
    """
    gate = jobs.get(MERGE_GATE_JOB)
    assert gate is not None, (
        f"ci.yml has no `{MERGE_GATE_JOB}` job; without it nothing "
        f"aggregates the pull request's jobs and no failure blocks a "
        f"merge"
    )
    needs = _needs_of(gate)
    assert JOB in needs, (
        f"the merge gate needs {needs!r}, which does not include `{JOB}`. "
        f"The job would run on every pull request and its failure would "
        f"block nothing — a red gate that closes no gate is a gate nobody "
        f"has to fix."
    )
