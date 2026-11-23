"""The changelog gate has to run on the pull request, not merely exist.

Why this file exists
--------------------
``scripts/ci_changelog_gate.py`` knows how to decide that a
judgment-surface change carries its ``## [Unreleased]`` entry. It is
stdlib-only, it takes one argument, and it is a *script* — which means
that until something runs it on a runner it is a file in a repository
and nothing else.

A gate that only runs on the machine of the person who wrote it proves
nothing to anybody else, and the accident this whole line of work comes
from (§1) was exactly that: a rule that was there, in the repository,
and was not applied to the change that needed it. The 137-line judgment
change landed inside an unrelated pull request and the changelog said
nothing about it.

So the gate is wired into ``.github/workflows/ci.yml`` as its own job,
and this file is what stops that wiring from quietly rotting. Each way
it can rot is one case below:

* the job disappears, or is renamed — the gate stops running at all;
* the job's checkout goes shallow — the base ref the gate diffs against
  is not in the clone, so the gate cannot tell what the change touched
  and answers "未触及受保护路径" to every question;
* the job stops naming the base ref — it diffs against nothing, or
  against a moving ref, and the diff it measures is not the pull
  request's diff;
* the job stays but leaves the merge gate's ``needs`` — it goes red and
  nothing closes, which is the failure mode every other gate in this
  repository was written to avoid.

Scope, stated honestly: this reads the workflow file and asserts on its
structure. It does not run the gate, so it cannot prove that a change
*should* have carried an entry — that is the script's job. What it
proves is that the gate is still *wired*, which is the part an edit of
``ci.yml`` can silently undo.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

#: The repository root — three levels up from ``backend/tests/static_gates/``.
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The job that runs the changelog gate.
CHANGELOG_GATE_JOB = "changelog-gate"

#: The terminal job that decides whether a pull request may merge.
MERGE_GATE_JOB = "merge-gate"

#: The script the job runs — the CLI task 22 delivered. The path is
#: repository-relative; the job reaches it as ``../scripts/...`` because
#: it runs from the ``backend/`` working directory.
GATE_SCRIPT = "scripts/ci_changelog_gate.py"

#: The invocation the job has to make. Written out whole rather than
#: assembled from parts: the gate's whole claim is "this change, against
#: this base, carried an entry", and each of the three pieces — the
#: interpreter, the script, the base — is load-bearing. A job that ran
#: the script without ``--base`` would diff against the default
#: ``origin/main``, which is a *moving* ref: it names whatever main
#: happens to be when the job runs, not the branch this pull request
#: targets.
GATE_INVOCATION = "python ../scripts/ci_changelog_gate.py"

#: The base the gate diffs against: the pull request's own base branch,
#: fetched into ``origin/`` by a depth-0 checkout.
GATE_BASE_ARG = "--base origin/${{ github.base_ref }}"


@pytest.fixture(scope="module")
def workflow() -> dict:
    if not WORKFLOW.is_file():
        pytest.fail(
            f"{WORKFLOW} does not exist; there is no CI to wire the "
            f"changelog gate into"
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


def test_job_exists_and_is_pull_request_scoped(jobs: dict) -> None:
    """There is a ``changelog-gate`` job, and it runs on pull requests.

    The job is pull-request-only, and deliberately so: the gate compares
    the pull request's diff against its base, and a dispatch or a
    schedule has no pull request to compare. A job scoped to those
    triggers would either never run (so it gates nothing) or run against
    a diff that is not a pull request's diff (so it judges the wrong
    thing).

    The checkout has to be deep. The gate reads the base ref out of the
    repository and diffs against it, and ``actions/checkout``'s default
    is ``fetch-depth: 1`` — a shallow clone in which that ref does not
    exist. The gate then cannot resolve the diff and exits non-zero
    rather than skipping, so a shallow checkout does not make this job
    vacuously green; it makes it red. Either way it is wrong, and this
    assertion says which knob turns it.
    """
    assert CHANGELOG_GATE_JOB in jobs, (
        f"ci.yml has no `{CHANGELOG_GATE_JOB}` job, so the changelog gate "
        f"never runs in CI. The gate is a file in a repository until "
        f"something runs it on a runner — and a gate that only runs on "
        f"the machine of the person who wrote it proves nothing to "
        f"anybody else."
    )
    job = jobs[CHANGELOG_GATE_JOB]

    condition = str(job.get("if", ""))
    assert "github.event_name == 'pull_request'" in condition, (
        f"the `{CHANGELOG_GATE_JOB}` job's `if:` is {condition!r}, which "
        f"does not admit a pull request. The gate compares the pull "
        f"request's diff against its base, so a dispatch or a schedule "
        f"has nothing for it to compare."
    )
    assert "workflow_dispatch" not in condition and "schedule" not in condition, (
        f"the `{CHANGELOG_GATE_JOB}` job's `if:` is {condition!r}, which "
        f"admits more than a pull request. The gate is a pull-request "
        f"check; a second trigger does not make it cover more, it makes "
        f"it judge a diff that is not the pull request's."
    )

    checkout = _checkout_step(job)
    assert checkout is not None, (
        f"the `{CHANGELOG_GATE_JOB}` job does not check the repository "
        f"out, so the gate has no working tree and no git history to "
        f"read the base ref from"
    )
    depth = (checkout.get("with") or {}).get("fetch-depth")
    assert str(depth) == "0", (
        f"the `{CHANGELOG_GATE_JOB}` job's checkout has fetch-depth="
        f"{depth!r}. The gate diffs the pull request against its base, "
        f"and actions/checkout's default (1) is a shallow clone in which "
        f"that ref does not exist — the gate then exits non-zero rather "
        f"than skipping, so the job goes red on a tree that is fine. "
        f"Use fetch-depth: 0."
    )


def test_job_in_merge_gate_needs(jobs: dict) -> None:
    """The merge gate covers the changelog gate.

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
    assert CHANGELOG_GATE_JOB in needs, (
        f"the merge gate needs {needs!r}, which does not include "
        f"`{CHANGELOG_GATE_JOB}`. The changelog gate would run on every "
        f"pull request and its failure would block nothing — a red gate "
        f"that closes no gate is a gate nobody has to fix."
    )


def test_job_runs_changelog_gate_script(jobs: dict) -> None:
    """The job runs the changelog gate, against the pull request's base.

    Three things have to be true at once, and each of them is a way the
    job can be present but useless:

    * the script the job runs is the one in the repository — a step that
      names a path that is not there fails on the runner, where the
      failure reads as "the gate is broken" rather than "the gate was
      never wired up";
    * the script is invoked *with* ``--base``, naming the pull
      request's own base branch. Without it the gate falls back to
      ``origin/main`` — a moving ref, which is not this pull request's
      base, and which does not exist in a clone that fetched one branch;
    * the invocation is made from the ``backend/`` directory, which is
      where ``../scripts/ci_changelog_gate.py`` resolves to the
      repository's script and where the gate's default ``--root``
      resolves back to the repository root.
    """
    job = jobs.get(CHANGELOG_GATE_JOB)
    assert job is not None, (
        f"ci.yml has no `{CHANGELOG_GATE_JOB}` job, so the changelog gate "
        f"never runs in CI"
    )

    steps = _run_steps(job)
    holders = [step for step in steps if GATE_SCRIPT in str(step.get("run", ""))]
    assert holders, (
        f"the `{CHANGELOG_GATE_JOB}` job does not run {GATE_SCRIPT}. The "
        f"gate is a script until something runs it, and a script nobody "
        f"runs is indistinguishable from one that passed."
    )
    step = holders[0]

    run = str(step.get("run", ""))
    assert GATE_INVOCATION in run, (
        f"the `{CHANGELOG_GATE_JOB}` job runs {GATE_SCRIPT}, but not as "
        f"`{GATE_INVOCATION}` — the `run:` body is:\n{run}\n"
        f"The gate is stdlib-only and is invoked the way the repository's "
        f"other stdlib-only scripts are: from the `backend/` working "
        f"directory, by the interpreter setup-python installed."
    )
    assert GATE_BASE_ARG in run, (
        f"the `{CHANGELOG_GATE_JOB}` job does not pass `{GATE_BASE_ARG}`. "
        f"The `run:` body is:\n{run}\n"
        f"Without the base ref the gate falls back to `origin/main` — a "
        f"moving ref that names whatever main happens to be when the job "
        f"runs, not the branch this pull request targets. The diff it "
        f"measures would then not be the pull request's diff."
    )
    assert str(step.get("working-directory", "")) == "backend", (
        f"the step that runs the changelog gate has working-directory="
        f"{step.get('working-directory')!r}. The invocation is "
        f"`{GATE_INVOCATION}`, which resolves to the repository's script "
        f"only from the `backend/` directory — and from there the gate's "
        f"default `--root` resolves back to the repository root."
    )

    assert (REPO_ROOT / GATE_SCRIPT).is_file(), (
        f"the `{CHANGELOG_GATE_JOB}` job runs {GATE_SCRIPT}, which is not "
        f"in the repository. The step would fail on the runner, where it "
        f"reads as a broken gate rather than a gate that was never wired "
        f"up."
    )
