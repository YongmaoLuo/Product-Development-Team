"""The coverage floors are stated once, and the CI enforces them.

Why this gate exists
--------------------
The harness refactor put a second backend behind one registry, and the
contract drawn for it has three layers: the registry that chooses a
backend, the adapters behind it, and the event and output contracts
they share. A contract that nobody measures is a contract that decays
quietly, so the plan fixed the measurement: two floors over the
harness surface (``backend/harness/`` and ``backend/mcode_profiles.py``)
and one floor over the change under review (the lines this branch
changed in ``backend/coding_tool.py``).

A floor that lives in a discussion is not a floor. It has to be a
number in the CI file, and the number has to be the number somebody
agreed to — otherwise the floor is whatever the last person to edit
the workflow typed, and nobody finds out until the day it is lowered
without anybody noticing. This file is the second half of that: the
three constants live here, and the assertions below hold the CI file
to them.

The three floors, and what each one measures
--------------------------------------------
``HARNESS_LINE_FLOOR`` (90)
    The coverage floor of the harness unit lane, applied to the total
    ``coverage.py`` reports for ``--cov=harness --cov=mcode_profiles``.
    With ``--cov-branch`` on, that total counts statements and
    branches together.

``HARNESS_BRANCH_FLOOR`` (85)
    The branch floor, measured on the branch percentage **alone** and
    compared on its own. This is not a second copy of the line floor
    and it is not folded into it: one mixed number cannot tell a
    well-covered branch set from a well-covered statement set, and a
    codebase can have one without the other. The CI therefore reads
    the branch total out of the coverage report and compares it with
    this floor by itself.

``DIFF_COVER_FLOOR`` (100)
    The floor on the lines a pull request *changed* in
    ``backend/coding_tool.py``. The other two floors measure a tree;
    this one measures a diff, and it is the only one of the three
    that says anything about the change being proposed. It is
    enforced by ``diff-cover``, which CI installs — pinned, in a tool
    environment of its own — and this gate never imports.

What this gate can and cannot see
----------------------------------
It reads the workflow file. It cannot prove that a runner was
allocated, that a coverage report was produced, or that a floor was
met — a green run here means the floors are *declared* as agreed, not
that the tree meets them. The floors themselves are enforced by the
jobs, and the numbers they produce are in those jobs' logs.

It also does not import ``diff-cover``. That package is a CI-only
dependency: the gate checks that CI installs and runs it, and a
machine that has never heard of the package still runs the gate.

Where the numbers live
----------------------
The three constants above are the definition; ``.github/workflows/ci.yml``
carries the rendering of them, because a workflow is a YAML file and
cannot import a Python module. The assertions below are what make
that arrangement safe: they fail the moment the rendering stops
matching the definition, so the number in the workflow is not a second
number that happens to agree today but a number this gate holds to the
one that was agreed. Lowering a floor is therefore an edit to this
file — and, per the plan's boundary, a pull request note saying why
and which files are excluded from it.
"""

from __future__ import annotations

import glob
import re
import shlex
import subprocess
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_BACKEND_DIR = _REPO_ROOT / "backend"
_VENV_PYTHON = _BACKEND_DIR / ".venv" / "bin" / "python3"

# ---------------------------------------------------------------------------
# The floors
# ---------------------------------------------------------------------------

#: The coverage floor of the harness unit lane, as a percentage of the
#: total ``coverage.py`` reports for the harness packages.
HARNESS_LINE_FLOOR = 90

#: The branch floor, as a percentage of the branches *alone*. Measured
#: separately from the line floor on purpose — see the module docstring.
HARNESS_BRANCH_FLOOR = 85

#: The floor on the changed lines of ``backend/coding_tool.py``, as a
#: percentage. A changed line is either covered or it is not, so this
#: one has no tolerance in it at all.
DIFF_COVER_FLOOR = 100

# ---------------------------------------------------------------------------
# Where the floors are applied
# ---------------------------------------------------------------------------

#: The job that runs the harness unit suite, and with it the two
#: harness floors.
HARNESS_UNIT_JOB = "harness-unit"

#: The pull request's coverage job: the one that already combines the
#: unit lane's coverage data and reports on it, and therefore the one
#: that holds the coverage report ``diff-cover`` measures against.
PR_COVERAGE_JOB = "coverage-gate"

#: The packages the harness floors apply to.
HARNESS_SOURCES = ("harness", "mcode_profiles")

#: The file the diff-cover floor applies to, as a path relative to the
#: repository root — the form ``git diff`` reports it in.
DIFF_COVER_TARGET = "backend/coding_tool.py"

#: The name of the environment variable the branch floor is passed to
#: the CI step that measures it. The floor is compared against the
#: branch percentage by that step, and the value it reads is the one
#: pinned here.
BRANCH_FLOOR_ENV = "HARNESS_BRANCH_FLOOR"

#: The name of the environment variable the *line* floor is passed to
#: the same step. The line floor needs its own variable for the same
#: reason the branch floor does: the two are compared against two
#: different percentages, and a step that read one number for both
#: would be comparing both floors against the same thing.
LINE_FLOOR_ENV = "HARNESS_LINE_FLOOR"


# ---------------------------------------------------------------------------
# Reading the workflow
# ---------------------------------------------------------------------------


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _job(name: str) -> dict:
    jobs = _workflow().get("jobs", {})
    assert name in jobs, (
        f"ci.yml has no job named {name!r}. The coverage floors are "
        f"enforced by jobs, and a job that is renamed without being "
        f"renamed here is a floor that is no longer checked."
    )
    return jobs[name]


def _run_blocks(job: dict) -> list[str]:
    return [
        step["run"]
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str)
    ]


def _run_text(job: dict) -> str:
    """Every shell command of *job*, as one string."""
    return "\n".join(_run_blocks(job))


def _step_running(job: dict, needle: str) -> dict:
    """The step of *job* whose ``run`` block mentions *needle*.

    The step, not just its text: where a step runs is part of what it
    does, and for diff-cover it is the difference between measuring
    the change and measuring nothing.
    """
    for step in job.get("steps", []):
        run = step.get("run")
        if isinstance(run, str) and needle in run:
            return step
    raise AssertionError(
        f"no step of the {job.get('name', 'job')!r} job runs {needle!r}"
    )


def _diff_cover_argv(job: dict) -> list[str]:
    """The argv of the ``diff-cover`` command in *job*.

    The command is read out of the workflow's own text rather than
    guessed, so the assertion below is about the command CI will
    actually run. The argv starts at the ``diff-cover`` token rather
    than at the beginning of the line, because the command may be
    reached through a runner — uv's tool mechanism, say — and the
    options that matter are the ones after it.
    """
    for block in _run_blocks(job):
        if "diff-cover" not in block:
            continue
        # A command spread over several lines with a trailing
        # backslash is one command.
        joined = block.replace("\\\n", " ")
        for line in joined.splitlines():
            tokens = shlex.split(line.strip())
            if "diff-cover" not in tokens:
                continue
            return tokens[tokens.index("diff-cover") :]
    return []


def _include_patterns(argv: list[str]) -> list[str]:
    """The ``--include`` patterns diff-cover would actually use.

    ``--include`` takes a list of patterns, but argparse's store
    action *replaces* that list when the option is given a second
    time, so the last ``--include`` on the command line is the one in
    force. This models that, because the difference between the two
    forms is the difference between a gate that measures something
    and a gate that measures nothing and reports 100%.
    """
    patterns: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--include":
            index += 1
            collected: list[str] = []
            while index < len(argv) and not argv[index].startswith("-"):
                collected.append(argv[index])
                index += 1
            patterns = collected
            continue
        if argument.startswith("--include="):
            patterns = [argument.split("=", 1)[1]]
        index += 1
    return patterns


def _matched_by(patterns: list[str]) -> set[str]:
    """The repository-relative paths the *patterns* select.

    ``diff-cover`` matches its include patterns with ``glob.glob``
    against the path ``git diff`` reports, so the patterns are
    expanded from the repository root here the same way.
    """
    matched: set[str] = set()
    for pattern in patterns:
        for path in glob.glob(pattern, root_dir=_REPO_ROOT, recursive=True):
            # ``root_dir`` makes glob return paths relative to it, which
            # is exactly the form ``git diff`` reports them in.
            matched.add(Path(path).as_posix())
    return matched


# ---------------------------------------------------------------------------
# The floors in the CI file agree with the floors here
# ---------------------------------------------------------------------------


def test_thresholds_match_constants() -> None:
    """Every threshold CI enforces is the threshold agreed here.

    A floor is a number somebody agreed to. The number in the workflow
    and the number in this file have to be the same number, and the
    only way to keep them the same number is to check — otherwise the
    floor in the CI file is whatever the last editor typed, and the
    two copies drift apart silently, with the copy nobody reads being
    the one that governs.
    """
    unit_job = _job(HARNESS_UNIT_JOB)
    pr_job = _job(PR_COVERAGE_JOB)

    line_floor = re.search(r"--cov-fail-under=(\d+)", _run_text(unit_job))
    assert line_floor, (
        f"the {HARNESS_UNIT_JOB!r} job does not set --cov-fail-under. The "
        f"harness floor is {HARNESS_LINE_FLOOR}%, and without the flag "
        f"the lane reports its coverage and enforces nothing."
    )
    assert int(line_floor.group(1)) == HARNESS_LINE_FLOOR, (
        f"the {HARNESS_UNIT_JOB!r} job enforces "
        f"--cov-fail-under={line_floor.group(1)}, and the agreed floor is "
        f"{HARNESS_LINE_FLOOR}%. A floor that is lower than the agreed "
        f"one is a decision that was made by editing a file rather than "
        f"by making a decision: lower it here, with the reason, or "
        f"raise it in the workflow."
    )

    branch_floor = unit_job.get("env", {}).get(BRANCH_FLOOR_ENV)
    assert branch_floor is not None, (
        f"the {HARNESS_UNIT_JOB!r} job does not pass {BRANCH_FLOOR_ENV}. "
        f"The branch floor is {HARNESS_BRANCH_FLOOR}% and it is measured "
        f"against the branch percentage on its own, so the step that "
        f"measures it has to be told what the floor is."
    )
    assert int(branch_floor) == HARNESS_BRANCH_FLOOR, (
        f"the {HARNESS_UNIT_JOB!r} job enforces a branch floor of "
        f"{branch_floor}%, and the agreed floor is {HARNESS_BRANCH_FLOOR}%."
    )

    # The floor is read off the diff-cover command itself rather than
    # off the job's text: the job runs more than one gate, and the
    # aggregate coverage floor that sits beside it is a different
    # number about a different thing.
    argv = _diff_cover_argv(pr_job)
    assert argv, (
        f"the {PR_COVERAGE_JOB!r} job does not run diff-cover. The "
        f"changed-line floor for {DIFF_COVER_TARGET} is "
        f"{DIFF_COVER_FLOOR}%, and diff-cover is what enforces it."
    )
    diff_floor = None
    for index, argument in enumerate(argv):
        if argument.startswith("--fail-under="):
            diff_floor = argument.split("=", 1)[1]
        elif argument == "--fail-under" and index + 1 < len(argv):
            diff_floor = argv[index + 1]
    assert diff_floor is not None, (
        f"the diff-cover command in the {PR_COVERAGE_JOB!r} job does not "
        f"set --fail-under. The changed-line floor for "
        f"{DIFF_COVER_TARGET} is {DIFF_COVER_FLOOR}%, and without the flag "
        f"diff-cover reports the number and enforces nothing."
    )
    assert float(diff_floor) == DIFF_COVER_FLOOR, (
        f"the {PR_COVERAGE_JOB!r} job enforces --fail-under={diff_floor}, "
        f"and the agreed floor is {DIFF_COVER_FLOOR}%. A changed line is "
        f"covered or it is not, so this floor has no tolerance in it."
    )


def test_branch_coverage_enabled() -> None:
    """The harness lane measures branches, and measures the right files.

    ``--cov-branch`` is what makes the branch floor measurable at all:
    without it the report has no branch totals, and the step that
    compares the branch percentage against its floor would be
    comparing a number that was never measured. The two ``--cov``
    sources are the two halves of the harness surface — the package
    itself and the profile table it resolves against.
    """
    unit_text = _run_text(_job(HARNESS_UNIT_JOB))
    assert "--cov-branch" in unit_text, (
        f"the {HARNESS_UNIT_JOB!r} job does not pass --cov-branch. The "
        f"branch floor is {HARNESS_BRANCH_FLOOR}% and branch coverage is "
        f"only measured when the branch flag is on; without it the "
        f"floor is a number in a file and nothing else."
    )
    for source in HARNESS_SOURCES:
        assert f"--cov={source}" in unit_text, (
            f"the {HARNESS_UNIT_JOB!r} job does not measure {source!r}. "
            f"The floors apply to {', '.join(HARNESS_SOURCES)} — a floor "
            f"over a surface that is not measured is a floor over "
            f"nothing."
        )


def test_diff_cover_scoped_to_coding_tool() -> None:
    """The diff-cover floor covers ``coding_tool.py`` and nothing else.

    Two things have to hold at once. The scope has to be *exactly* the
    one file: the floor is a statement about the code this change
    touches, and a scope that reaches further says something about
    code this change does not touch. And the scope has to *match*:
    diff-cover matches its include patterns with ``glob.glob``
    against the path ``git diff`` reports, and a pattern that matches
    nothing makes diff-cover report 100% and exit 0 — a gate that
    passes because it measured nothing, which is the failure mode a
    coverage gate cannot be allowed to have.
    """
    pr_job = _job(PR_COVERAGE_JOB)
    argv = _diff_cover_argv(pr_job)
    assert argv, (
        f"the {PR_COVERAGE_JOB!r} job does not run diff-cover. The "
        f"changed-line floor for {DIFF_COVER_TARGET} is "
        f"{DIFF_COVER_FLOOR}%, and diff-cover is the only thing that "
        f"measures a diff."
    )
    assert "--include=coding_tool.py" in _run_text(pr_job), (
        f"the diff-cover command in the {PR_COVERAGE_JOB!r} job does not "
        f"scope itself to coding_tool.py. The floor is a statement about "
        f"the file this change touches."
    )

    patterns = _include_patterns(argv)
    assert patterns, (
        f"the diff-cover command in the {PR_COVERAGE_JOB!r} job has no "
        f"--include, so it would measure every file in the diff rather "
        f"than the one the floor is about."
    )
    for pattern in patterns:
        assert pattern.split("/")[-1] == "coding_tool.py", (
            f"diff-cover is scoped to {patterns!r}. Only "
            f"{DIFF_COVER_TARGET} may be in scope: the floor is a "
            f"statement about the file this change touches."
        )

    assert DIFF_COVER_TARGET in _matched_by(patterns), (
        f"the diff-cover include pattern(s) {patterns!r} select "
        f"{sorted(_matched_by(patterns))!r}, and not {DIFF_COVER_TARGET!r}. "
        f"diff-cover matches its include patterns against the path "
        f"`git diff` reports — repository-root relative, so "
        f"{DIFF_COVER_TARGET!r} — and a pattern that matches nothing "
        f"makes diff-cover report 100% and exit 0. A gate that passes "
        f"because it measured nothing is not a gate."
    )


def test_coverage_surface_is_exactly_the_harness_surface() -> None:
    """The floor is measured over the harness surface and nothing else.

    The floors are stated over ``backend/harness/`` and
    ``backend/mcode_profiles.py``. A stray ``--cov=.`` in the same
    command would measure the whole backend instead, and the number
    the lane reports would then be a number about code this floor was
    never agreed over — a floor that has quietly become a different
    floor, which is the same failure as lowering it: nobody decided
    to, and nobody finds out.

    So the sources are checked to be exactly the two named ones. Both
    the extra-source case and the missing-source case fail here.
    """
    unit_text = _run_text(_job(HARNESS_UNIT_JOB))
    measured = re.findall(r"--cov=([^\s\\]+)", unit_text)
    assert sorted(measured) == sorted(HARNESS_SOURCES), (
        f"the {HARNESS_UNIT_JOB!r} job measures {sorted(measured)!r} and "
        f"the floors apply to {sorted(HARNESS_SOURCES)!r}. A source that "
        f"is not one of the two widens the floor over code nobody "
        f"agreed it should cover; a source that is missing narrows it "
        f"below what was agreed."
    )


def test_pytest_cov_available() -> None:
    """``pytest-cov`` is importable from ``backend/.venv``.

    The harness floors are measured by ``pytest --cov``, so a venv
    without the plugin cannot measure them, and the job that is
    supposed to enforce them would fail with "unrecognized arguments:
    --cov" — a message that names the plugin nobody installed. This
    fails here instead, with the command that fixes it, because a
    missing plugin is a missing plugin and not a reason to skip a
    gate: a skipped floor is a floor nobody is holding.
    """
    assert _VENV_PYTHON.exists(), (
        f"backend/.venv is missing ({_VENV_PYTHON}). The harness floors "
        f"are measured through that interpreter, so the gate cannot run "
        f"without it. Create it with: cd backend && uv sync --locked"
    )
    probe = subprocess.run(
        [str(_VENV_PYTHON), "-c", "import pytest_cov, pytest_cov.plugin"],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, (
        f"pytest-cov is not importable from backend/.venv:\n"
        f"{probe.stderr.strip()}\n"
        f"Install it with: cd backend && .venv/bin/python3 -m pip install "
        f"'pytest-cov>=5.0'"
    )

    options = subprocess.run(
        [str(_VENV_PYTHON), "-m", "pytest", "--help"],
        capture_output=True,
        text=True,
    )
    assert "--cov=" in options.stdout, (
        "the pytest in backend/.venv does not offer the --cov option, so "
        "pytest-cov is installed but not registered as a plugin. "
        "Reinstall it with: cd backend && .venv/bin/python3 -m pip install "
        "--force-reinstall 'pytest-cov>=5.0'"
    )


# ---------------------------------------------------------------------------
# The boundaries the floors are read under
# ---------------------------------------------------------------------------


def test_diff_cover_is_a_ci_only_dependency() -> None:
    """``diff-cover`` is installed by CI, and never imported here.

    The local gate checks that CI installs and runs diff-cover; it
    does not run it. A gate that needs a package the developer may not
    have installed is a gate that gets skipped on the machines where
    it would have been useful, and the boundary says so: the local
    gate validates the CI definition and the thresholds, and the
    package exists only where the measurement happens.

    The install is pinned. The version that judges a pull request is
    the version the workflow names, not the one the network happens to
    serve that afternoon — the drift this repository's dependency
    manifest exists to end, and the reason a named install in a
    workflow is not allowed to float.
    """
    pr_job = _job(PR_COVERAGE_JOB)
    pr_text = _run_text(pr_job)
    assert "diff-cover" in pr_text, (
        f"the {PR_COVERAGE_JOB!r} job does not mention diff-cover. The "
        f"changed-line floor is enforced by diff-cover, and a job that "
        f"runs a tool it never installed fails on the tool, not on the "
        f"code."
    )

    installs = [
        line.strip()
        for block in _run_blocks(pr_job)
        for line in block.splitlines()
        if line.strip()
        and not line.strip().startswith("#")
        and "diff-cover" in line
        and "install" in line
    ]
    for line in installs:
        assert re.search(r"diff-cover==[0-9][0-9.]*", line), (
            f"the diff-cover install {line!r} is not pinned to a version. "
            f"An unpinned install is a package whose version changes "
            f"without a commit, and the floor is then enforced by "
            f"something nobody reviewed."
        )

    source = Path(__file__).read_text(encoding="utf-8")
    imported = re.search(r"^\s*(?:import|from)\s+diff_cover", source, re.M)
    assert imported is None, (
        "this gate imports diff_cover. It must not: the package is a "
        "CI-only dependency, and a gate that cannot run without it is a "
        "gate that stops being run on the machines that lack it."
    )


def test_diff_cover_runs_from_the_repository_root() -> None:
    """The diff-cover step runs in the directory where its paths line up.

    diff-cover matches its include patterns with ``glob.glob`` against
    the path ``git diff`` reports, and it looks the coverage up under
    those same paths. The two only line up in one directory: the
    repository root, where ``git diff`` reports ``backend/coding_tool.py``
    and the glob for that pattern finds the file.

    Run from ``backend/`` the same command reports "No lines with
    coverage information in this diff" and exits 0 — a gate that
    passes because it measured nothing. This was measured rather than
    assumed: the identical command from the two directories, against
    the same coverage report and the same diff.
    """
    step = _step_running(_job(PR_COVERAGE_JOB), "diff-cover")
    working_directory = step.get("working-directory")
    assert working_directory is None, (
        f"the diff-cover step runs in {working_directory!r}. It has to "
        f"run in the repository root: diff-cover matches its include "
        f"patterns against the path `git diff` reports, and `git diff` "
        f"reports repository-root-relative paths whatever the "
        f"directory it runs in. From {working_directory!r} the pattern "
        f"matches nothing, diff-cover reports no coverage information "
        f"and exits 0, and the changed-line floor passes without "
        f"measuring a single line."
    )


def test_branch_floor_is_measured_on_its_own() -> None:
    """The branch floor is compared against the branch percentage.

    A single mixed number cannot tell a well-covered branch set from a
    well-covered statement set, and folding the two together is how a
    branch floor quietly stops being a branch floor. The step that
    enforces it therefore reads the branch total out of the coverage
    report — ``percent_branches_covered``, which coverage.py computes
    over the branches alone — and compares it with the branch floor.
    """
    unit_text = _run_text(_job(HARNESS_UNIT_JOB))
    assert "percent_branches_covered" in unit_text, (
        f"the {HARNESS_UNIT_JOB!r} job does not read the branch total "
        f"out of the coverage report. The branch floor is measured "
        f"against the branch percentage on its own, so the number it "
        f"compares has to be that number and not the mixed one."
    )
    assert "percent_covered" not in unit_text, (
        f"the {HARNESS_UNIT_JOB!r} job compares the branch floor against "
        f"the mixed total. The mixed total counts statements and "
        f"branches together, which is exactly the folding the branch "
        f"floor exists to avoid."
    )
    assert BRANCH_FLOOR_ENV in unit_text, (
        f"the {HARNESS_UNIT_JOB!r} job does not read {BRANCH_FLOOR_ENV} "
        f"where it measures the branch percentage. The floor is a "
        f"number somebody agreed to; a step that hard-codes its own "
        f"copy of it is a step whose copy nobody checks."
    )


def test_line_coverage_floor_is_measured_on_its_own() -> None:
    """The line floor is compared against the line percentage.

    ``--cov-fail-under`` compares against the number coverage.py calls
    the *total*, and with ``--cov-branch`` on that total is the
    statements and the branches counted together
    (``n_executed + n_executed_branches`` over ``n_statements +
    n_branches``). A branch set that is better covered than the
    statement set can therefore carry that mixed total over the floor
    while the statements sit below it — with a branch floor of 85 and
    a mixed floor of 90, statements at 85% and branches at 95% pass.

    So the flag alone does not enforce the agreed "lines ≥ 90%". The
    lane therefore reads the statement total out of the report as well
    — ``percent_statements_covered``, which coverage.py computes over
    the statements alone — and compares it with the line floor by
    itself, the way the branch floor is compared with the branch
    percentage by itself.
    """
    unit_job = _job(HARNESS_UNIT_JOB)
    unit_text = _run_text(unit_job)

    assert "percent_statements_covered" in unit_text, (
        f"the {HARNESS_UNIT_JOB!r} job does not read the statement total "
        f"out of the coverage report. --cov-fail-under compares against "
        f"the mixed total, so the line floor of {HARNESS_LINE_FLOOR}% is "
        f"only enforced on its own if something reads the statement "
        f"percentage and compares it with that floor."
    )

    assert LINE_FLOOR_ENV in unit_text, (
        f"the {HARNESS_UNIT_JOB!r} job does not read {LINE_FLOOR_ENV} "
        f"where it measures the statement percentage. The floor is a "
        f"number somebody agreed to; a step that hard-codes its own "
        f"copy of it is a step whose copy nobody checks."
    )

    line_floor = unit_job.get("env", {}).get(LINE_FLOOR_ENV)
    assert line_floor is not None, (
        f"the {HARNESS_UNIT_JOB!r} job does not pass {LINE_FLOOR_ENV}. "
        f"The line floor is {HARNESS_LINE_FLOOR}% and it is measured "
        f"against the statement percentage on its own, so the step that "
        f"measures it has to be told what the floor is."
    )
    assert int(line_floor) == HARNESS_LINE_FLOOR, (
        f"the {HARNESS_UNIT_JOB!r} job enforces a line floor of "
        f"{line_floor}%, and the agreed floor is {HARNESS_LINE_FLOOR}%."
    )
