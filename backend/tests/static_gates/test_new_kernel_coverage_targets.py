"""The new kernel's coverage gate is judged per module path, and its list is pinned.

Why this gate exists
--------------------

The repository's coverage floor is **one number for the whole tree**, and a
whole-tree number is diluted by the directories that were already there.
The seven modules this feature added are about 1400 statements against a
repository of tens of thousands, so one of them can sit far below the
floor while the union still reads above it — and the union says nothing
about *which* module is the weak one. ``root_cause_task.py`` is the
clearest case: it is a brand-new module that the whole-tree number never
mentions.

Test-design decision point 12 says the new kernel is judged **per module
path**: every module carries its own threshold, so a strong module cannot
carry a weak one and a module that loses coverage fails on its own
number. That decision has to become something CI enforces, or it is a
sentence in a document — a gate is only a gate if dropping below the line
makes a build red.

This file is the half of that gate that lives in the repository rather
than on the runner:

1. :func:`test_targets_match_ci` — the modules CI judges per module and
   the list pinned here are the same seven. The list is written down in
   both places on purpose: CI has to *name* the modules, and a list that
   only CI knows about cannot be reviewed. Changing one without the other
   is the drift this test exists to catch.
2. :func:`test_fail_under_is_module_level` — every module carries its own
   threshold, and the module gate adds no repository-wide threshold of
   its own. A threshold that applies to the whole repository is exactly
   the diluted number the decision replaced.
3. :func:`test_no_new_dependency` — the gate runs on the coverage tooling
   that was already declared. A coverage gate that needs a new third-party
   package is a dependency decision disguised as a CI change, and this
   repository's rule is that dependency changes are declared, pinned and
   reviewed on their own.

What the gate does **not** claim
--------------------------------

* It does not run the suite and it does not measure anything. It reads
  the workflow, the manifest and the lockfile. A green run here means
  "the workflow and the repository agree with each other" — not "the
  coverage is high". The measurement happens on the runner, in the
  ``coverage-gate`` job, against the data the shards produced.
* It does not police the *value* of a threshold. What a module's floor
  should be is a judgement about how much of that module is exercised,
  and that judgement belongs to the module's own tests. This gate only
  insists that the floor is attached to a module rather than to the
  repository as a whole — the shape of the decision, not its arithmetic.
* The repository-wide floor that predates this gate (the ``--fail-under``
  on the combined report) is **not** this gate's business. That line is
  older than the module gate and it is not what the module gate
  replaced; what the module gate must not do is *add* a second
  repository-wide threshold, and that is what
  :func:`test_fail_under_is_module_level` checks.

Missing files fail, they never skip
-----------------------------------

A gate whose target disappeared passes vacuously. Every helper below
raises :class:`AssertionError` when the workflow, the manifest or the
lockfile cannot be read or parsed, rather than skipping.

How the per-module floor is expressed
-------------------------------------

The brief for this gate names "the modules' ``--cov`` paths and
``--cov-fail-under``". The two flags cannot say what the gate needs to
say, and the reason is worth writing down rather than leaving as a
surprise:

* pytest-cov's ``--cov`` takes *module names*, not file paths — pass it
  a path and coverage reads it as a module, finds nothing, and reports
  "No data was collected".
* pytest-cov's ``--cov-fail-under`` is a total over everything it was
  handed. Hand it the seven modules at once and the threshold is a
  module-**set** threshold: a strong module carries a weak one, which is
  the dilution this gate removes, moved from the repository level to the
  module-set level. Hand it one module at a time and the suite has to
  be re-run seven times inside the gate job, each run measuring a
  smaller selection than the shards measured.

What the gate uses instead is ``coverage report --include=<module path>
--fail-under=<n>``, run over the shard data the ``coverage-gate`` job
already combines. ``--include`` slices the measured data by path, so
each line is that one module's own number, and the seven lines together
judge the seven modules separately over one measurement.

The reader below accepts both spellings (``--include=`` and
``--cov=``), because what is pinned here is *which modules are judged
per module* — not which flag happens to spell it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Mapping, Set, Tuple

import yaml

#: Repository root. ``backend/tests/static_gates/`` is three levels deep.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

#: The workflow under test.
WORKFLOW: Path = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The dependency manifest, and the lockfile beside it.
MANIFEST: Path = REPO_ROOT / "backend" / "pyproject.toml"
LOCKFILE: Path = REPO_ROOT / "backend" / "uv.lock"

#: The job that owns the coverage floors.
COVERAGE_GATE_JOB: str = "coverage-gate"

#: The step that judges the new kernel one module at a time.
MODULE_GATE_STEP: str = "Enforce the new-kernel module coverage gate"

#: The seven modules the new kernel added, relative to ``backend/`` (the
#: directory the coverage-gate job runs in).
#:
#: Three of them are the verification kernel proper — the pure decision
#: functions (``verification/topic_relevance.py``,
#: ``verification/failure_fingerprint.py``) and the deterministic scan
#: (``verification/scan.py``). The other four are the modules that grew
#: around them: the context pack, the effect report, the root-cause task
#: builder and the archive/removal audit.
#:
#: Changing this list is a deliberate act: it changes what CI judges, so
#: the constant and ``ci.yml`` have to move together. That is what
#: :func:`test_targets_match_ci` is for.
NEW_KERNEL_COVERAGE_TARGETS: Tuple[str, ...] = (
    "context_pack.py",
    "effect_report.py",
    "root_cause_task.py",
    "verification/failure_fingerprint.py",
    "verification/scan.py",
    "verification/topic_relevance.py",
    "verification/vp_removal_audit.py",
)

#: The coverage tooling the dependency manifest already declares.
#:
#: ``pytest-cov`` is what the shards measure with and ``coverage`` is
#: what the gate combines and reports with. Both were declared before
#: this gate existed; a third coverage-related package would mean the
#: gate had quietly become a dependency change.
DECLARED_COVERAGE_PACKAGES: frozenset[str] = frozenset(
    {"coverage", "pytest-cov"}
)

#: The floor each module carries, mirroring the ``--fail-under`` in
#: ``ci.yml``.
#:
#: The list above says WHICH modules are judged per module; this says
#: at what level. Pinning the numbers and not only the list is the same
#: argument twice: CI has to name a number, and a number that only the
#: runner knows about cannot be reviewed. A floor is a ratchet — every
#: step of one is a decision somebody has to look at — so a floor that
#: moves in ``ci.yml`` without moving here fails the static-gates job
#: instead of landing quietly.
#:
#: The levels are the modules' branch-coverage level minus a 5-point
#: margin, and the runner reports LINE coverage (the shards collect no
#: branch data), so these floors sit 6 to 14 points below the numbers
#: the runner will read. The ratchet tightens as the tests catch up.
NEW_KERNEL_COVERAGE_FLOORS: Mapping[str, int] = {
    "context_pack.py": 80,
    "effect_report.py": 72,
    "root_cause_task.py": 55,
    "verification/failure_fingerprint.py": 87,
    "verification/scan.py": 80,
    "verification/topic_relevance.py": 64,
    "verification/vp_removal_audit.py": 69,
}

#: ``--include=<path>`` — the module path a coverage report is sliced
#: down to. The value may be quoted, because a shell line that names a
#: path usually quotes it.
_INCLUDE_RE = re.compile(r"--include=[\"']?([^\"'\s]+)")

#: ``--cov=<path>`` — the same idea in pytest-cov's spelling: the
#: measurement is asked for a named source. Accepted alongside
#: ``--include`` so that the pinned list below is about *which modules
#: are judged per module*, not about which flag spells it. The
#: repository-wide spelling (``--cov=.``) is not a module and is
#: rejected by :func:`_named_module`.
_COV_RE = re.compile(r"--cov=[\"']?([^\"'\s]+)")

#: ``--fail-under=<n>`` — the threshold a report has to reach.
_FAIL_UNDER_RE = re.compile(r"--fail-under=(\d+(?:\.\d+)?)")

#: The source values that mean "the whole repository" rather than one
#: module.
_REPOSITORY_WIDE_SOURCES = frozenset({".", "./", "backend", "backend/"})


def _named_module(line: str) -> str | None:
    """The module path a coverage line names, or ``None``.

    ``None`` means the line does not name a module: either it names
    nothing at all, or it names the repository as a whole. The two are
    the same thing for this gate — a threshold that is not attached to
    a module is the diluted repository-wide number, whether it says so
    with ``--cov=.`` or by saying nothing.
    """
    for pattern in (_INCLUDE_RE, _COV_RE):
        match = pattern.search(line)
        if match is not None:
            value = match.group(1)
            if value in _REPOSITORY_WIDE_SOURCES:
                return None
            return value
    return None


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _as_workflow(workflow: Any) -> Dict[str, Any]:
    """Return the workflow mapping, parsing raw YAML text if given."""
    if isinstance(workflow, str):
        parsed = yaml.safe_load(workflow)
        assert isinstance(parsed, dict), (
            "the workflow text did not parse into a mapping; this gate "
            "reads it as YAML and cannot work with anything else"
        )
        return parsed
    return dict(workflow)


def _workflow() -> Dict[str, Any]:
    """The parsed workflow."""
    return _as_workflow(WORKFLOW.read_text(encoding="utf-8"))


def _coverage_gate_job(workflow: Any) -> Dict[str, Any]:
    """The ``coverage-gate`` job, asserted present.

    A missing job is a named failure rather than a ``None`` that the
    next assertion turns into a confusing ``TypeError``: the reader
    wants to know which lane is missing.
    """
    jobs = _as_workflow(workflow).get("jobs") or {}
    job = jobs.get(COVERAGE_GATE_JOB)
    assert job is not None, (
        f"ci.yml has no job named {COVERAGE_GATE_JOB!r}. The module-level "
        "coverage floors live in that job; without it this gate checks "
        "nothing."
    )
    return job


def _run_blocks(job: Mapping[str, Any]) -> List[str]:
    """The ``run:`` bodies of *job*."""
    return [
        step["run"]
        for step in job.get("steps", []) or []
        if isinstance(step.get("run"), str)
    ]


def _gate_lines(workflow: Any) -> List[str]:
    """Every ``coverage report`` line the coverage-gate job runs."""
    lines: List[str] = []
    for block in _run_blocks(_coverage_gate_job(workflow)):
        for line in block.split("\n"):
            stripped = line.strip()
            if "coverage report" in stripped:
                lines.append(stripped)
    return lines


def _module_gate_lines(workflow: Any) -> List[str]:
    """The ``coverage report`` lines of the module-gate step.

    The step is found by name so that this gate reads the module gate
    and not the whole job: the job also carries the repository-wide
    floor that predates this gate, and mixing the two would make every
    assertion below ambiguous about which threshold it is talking about.
    """
    job = _coverage_gate_job(workflow)
    for step in job.get("steps", []) or []:
        if step.get("name") == MODULE_GATE_STEP:
            run = step.get("run")
            assert isinstance(run, str), (
                f"the {MODULE_GATE_STEP!r} step has no shell body; this "
                "gate reads the thresholds out of it"
            )
            return [
                line.strip()
                for line in run.split("\n")
                if "coverage report" in line
            ]
    raise AssertionError(
        f"the {COVERAGE_GATE_JOB!r} job has no step named "
        f"{MODULE_GATE_STEP!r}. The module-level floors live in that step; "
        "if it was renamed, rename it here too — a gate that looks for a "
        "step that no longer exists checks nothing."
    )


# ---------------------------------------------------------------------------
# The two things CI says about the new kernel
# ---------------------------------------------------------------------------


def ci_cov_targets(workflow: Any) -> Set[str]:
    """The module paths the coverage gate judges on their own.

    A "target" is a module path the gate names when it asks for a
    measurement (``--include=<path>`` or ``--cov=<path>``), i.e. a
    module that carries a threshold of its own instead of being folded
    into the repository-wide number. A line that names the repository
    as a whole names no target.
    """
    targets: Set[str] = set()
    for line in _gate_lines(workflow):
        module = _named_module(line)
        if module is not None:
            targets.add(module)
    return targets


def ci_cov_fail_under(workflow: Any) -> Dict[str, float]:
    """The threshold each judged module carries.

    Only lines that name *both* a module and a threshold contribute: a
    threshold that is not attached to a module is the repository-wide
    number, and this mapping is about the per-module floors.
    """
    thresholds: Dict[str, float] = {}
    for line in _gate_lines(workflow):
        module = _named_module(line)
        fail_under = _FAIL_UNDER_RE.search(line)
        if module is not None and fail_under is not None:
            thresholds[module] = float(fail_under.group(1))
    return thresholds


def floor_drift(thresholds: Mapping[str, float]) -> Dict[str, Tuple[float, int]]:
    """The modules whose CI floor is not the floor pinned here.

    The value is the pair ``(ci floor, pinned floor)``; a module CI
    gives no floor at all reads as ``None`` on the left, which is also
    drift — a module that lost its floor is a module nobody judges.
    """
    drift: Dict[str, Tuple[float, int]] = {}
    for module, pinned in NEW_KERNEL_COVERAGE_FLOORS.items():
        actual = thresholds.get(module)
        if actual is None or actual != float(pinned):
            drift[module] = (actual, pinned)
    return drift


def _declared_names(manifest: Mapping[str, Any]) -> Set[str]:
    """Every distribution name the manifest declares, from both groups."""
    names: Set[str] = set()
    project = manifest.get("project") or {}
    for raw in project.get("dependencies", []) or []:
        names.add(_requirement_name(raw))
    for entries in (manifest.get("dependency-groups") or {}).values():
        for raw in entries or []:
            names.add(_requirement_name(raw))
    return names


def _requirement_name(raw: str) -> str:
    """The distribution name of a PEP 508 requirement string.

    Deliberately not a full parser: the manifest is hand-written and
    carries no URLs or markers, so the only two shapes that occur are
    ``name`` and ``name<specifier>``. Anything else raises, so a
    requirement that grows a marker cannot slip past a comparison that
    silently ignored the part it did not understand.
    """
    match = re.match(
        r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<rest>[<>=!~;\[].*)?$",
        raw.strip(),
    )
    assert match is not None, (
        f"cannot parse requirement {raw!r} in {MANIFEST.name}; this gate "
        "only handles `name` and `name<specifier>` forms"
    )
    return match.group("name")


def _coverage_packages(names: Set[str]) -> Set[str]:
    """The coverage-related distribution names out of *names*.

    "Coverage-related" means the name mentions coverage measurement:
    ``coverage`` itself, ``pytest-cov`` (the plugin the shards measure
    with), and anything else that would claim the same job.
    """
    return {name for name in names if "cov" in name.lower()}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_targets_match_ci() -> None:
    """CI's per-module coverage targets and the pinned list are the same seven.

    The list is written down twice — here and in ``ci.yml`` — because
    both readers need it: CI has to name the modules it judges, and a
    reviewer has to be able to see the list without reading a runner's
    log. The two copies are only useful if they cannot drift, which is
    what this test is for: adding a module to the gate without adding it
    here fails the build, and so does removing it from one of the two.
    """
    targets = ci_cov_targets(_workflow())
    assert set(targets) == set(NEW_KERNEL_COVERAGE_TARGETS), (
        "the modules CI judges per module and the modules this file pins "
        f"are not the same set.\n  CI: {sorted(targets)}\n"
        f"  pinned: {sorted(NEW_KERNEL_COVERAGE_TARGETS)}\n"
        "The new kernel's coverage targets are written down twice on "
        "purpose — once in ci.yml, once here — so that a change to one "
        "has to be a change to both. If the target list really should "
        "change, change it in ci.yml and in "
        "NEW_KERNEL_COVERAGE_TARGETS in this file, in the same commit."
    )


def test_fail_under_is_module_level() -> None:
    """Every judged module carries its own threshold, and none is the repository's.

    Two shapes are wrong here, and they are the two ways this gate can
    quietly become the diluted number it replaced:

    * a module with no threshold of its own — it is judged by the
      repository-wide number, so a module can fall to 40% without
      anything going red;
    * a threshold that applies to the repository as a whole — the same
      dilution, spelled the other way round.
    """
    workflow = _workflow()
    thresholds = ci_cov_fail_under(workflow)

    without_threshold = [
        target
        for target in NEW_KERNEL_COVERAGE_TARGETS
        if target not in thresholds
    ]
    assert not without_threshold, (
        f"these modules are named as coverage targets but carry no "
        f"threshold of their own: {without_threshold}. A module without a "
        "floor is judged by the repository-wide number, which is the "
        "diluted measurement this gate exists to replace."
    )

    lines = _module_gate_lines(workflow)
    assert lines, (
        f"the {MODULE_GATE_STEP!r} step runs no `coverage report` at all. "
        "The per-module floors are the step's whole content; a step that "
        "reports nothing floors nothing."
    )

    repository_wide = [
        line for line in lines if _named_module(line) is None
    ]
    assert not repository_wide, (
        "the module gate applies a threshold that is not attached to any "
        f"module: {repository_wide}. A repository-wide threshold is the "
        "whole-tree number — the big directories that already existed "
        "dilute it, which is why the new kernel is judged per module "
        "path. Keep the repository-wide floor where it already is; do "
        "not add another one here."
    )

    unattached = [
        line for line in lines if _FAIL_UNDER_RE.search(line) is None
    ]
    assert not unattached, (
        f"these module-gate lines carry no threshold: {unattached}. A "
        "module that is named but not floored is a module that is "
        "reported on and not judged."
    )

    # The floors are pinned, not just the list of modules. A floor is a
    # ratchet: every step of one is a decision somebody has to look at,
    # and a floor that could be lowered in ci.yml without anybody
    # noticing would make the ratchet a suggestion.
    drift = floor_drift(thresholds)
    assert not drift, (
        "these modules are judged in CI at a floor that differs from the "
        f"one pinned here: {drift} (CI floor, pinned floor).\n"
        "The per-module floors live in two places on purpose — once in "
        "ci.yml, once in NEW_KERNEL_COVERAGE_FLOORS in this file — so "
        "that moving one has to be a change to both. If a floor really "
        "should change, change it in ci.yml and in "
        "NEW_KERNEL_COVERAGE_FLOORS in this file, in the same commit."
    )

    # A floor of 0 never fires; a floor of 100 can never be met. Both
    # are the gate turned off, and both are worth a named failure.
    vacuous = {
        module: floor
        for module, floor in thresholds.items()
        if not 0 < floor < 100
    }
    assert not vacuous, (
        f"these module floors are vacuous: {vacuous}. A floor of 0 "
        "never fires and a floor of 100 can never be met; both are the "
        "gate turned off rather than tightened."
    )


def test_no_new_dependency() -> None:
    """The coverage gate runs on tooling the manifest already declares.

    The gate needs exactly two things: something to measure with
    (``pytest-cov``, which the shards already use) and something to
    combine and report with (``coverage``, already declared beside it).
    A third coverage-related package would mean the gate had quietly
    become a dependency change — and a dependency change is a review of
    its own, not something that rides along with a CI edit.
    """
    manifest = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    declared = _coverage_packages(_declared_names(manifest))
    assert declared == set(DECLARED_COVERAGE_PACKAGES), (
        "the coverage-related packages in "
        f"{MANIFEST.name} are {sorted(declared)}, and this gate expects "
        f"exactly {sorted(DECLARED_COVERAGE_PACKAGES)}.\n"
        "A coverage gate must run on the tooling the repository already "
        "declares. If a new coverage-related package is genuinely needed, "
        "it is a dependency change: declare it, lock it, and review it as "
        "one — not as a side effect of a CI edit."
    )

    lock = tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))
    locked = _coverage_packages(
        {package.get("name", "") for package in lock.get("package", [])}
    )
    assert locked == set(DECLARED_COVERAGE_PACKAGES), (
        f"the coverage-related packages in {LOCKFILE.name} are "
        f"{sorted(locked)}, and this gate expects exactly "
        f"{sorted(DECLARED_COVERAGE_PACKAGES)}."
    )

    # The gate shells out to `coverage`; if the manifest stopped
    # declaring it, the job would install nothing and the floor would
    # never be checked.
    assert "coverage" in declared, (
        f"{MANIFEST.name} declares no `coverage` package, so the "
        "coverage-gate job has nothing to combine or report with and the "
        "module floors would never be evaluated."
    )


# ---------------------------------------------------------------------------
# Negative controls -- proof that the checks above are not vacuous
# ---------------------------------------------------------------------------


def test_the_target_reader_finds_a_module_path() -> None:
    """A ``--include`` on a coverage report line is read as a target.

    Without this, a reader that returned the empty set for every input
    would make :func:`test_targets_match_ci` fail for the wrong reason
    — or, worse, pass if the pinned list were ever emptied too.
    """
    workflow = {
        "jobs": {
            COVERAGE_GATE_JOB: {
                "steps": [
                    {
                        "name": MODULE_GATE_STEP,
                        "run": (
                            './.venv/bin/python3 -m coverage report '
                            '--include="verification/scan.py" '
                            "--fail-under=80\n"
                        ),
                    }
                ]
            }
        }
    }
    assert ci_cov_targets(workflow) == {"verification/scan.py"}, (
        "a module path named on a coverage report line was not read as a "
        "target — the target reader is not working"
    )


def test_the_threshold_reader_ignores_an_unattached_threshold() -> None:
    """A threshold without a module is not a module's threshold.

    The mapping this gate reads is about per-module floors. If a
    repository-wide ``--fail-under`` counted as one, the mapping would
    report a floor for a module that has none.
    """
    workflow = {
        "jobs": {
            COVERAGE_GATE_JOB: {
                "steps": [
                    {
                        "name": MODULE_GATE_STEP,
                        "run": (
                            "./.venv/bin/python3 -m coverage report "
                            "--fail-under=80\n"
                            './.venv/bin/python3 -m coverage report '
                            '--include="context_pack.py" --fail-under=70\n'
                        ),
                    }
                ]
            }
        }
    }
    thresholds = ci_cov_fail_under(workflow)
    assert thresholds == {"context_pack.py": 70.0}, (
        f"the per-module thresholds read as {thresholds}; a "
        "repository-wide threshold must not be counted as a module's own"
    )


def test_a_repository_wide_source_is_not_a_module() -> None:
    """``--cov=.`` names the repository, not a module.

    The shards measure the whole tree with ``--cov=.``. If the reader
    counted that as a target, the pinned list would have to contain
    ``"."`` and the per-module gate would have swallowed the
    repository-wide measurement it exists to replace.
    """
    workflow = {
        "jobs": {
            COVERAGE_GATE_JOB: {
                "steps": [
                    {
                        "name": MODULE_GATE_STEP,
                        "run": (
                            "./.venv/bin/python3 -m pytest --cov=. "
                            "--cov-report=\n"
                            './.venv/bin/python3 -m coverage report '
                            '--include="verification/scan.py" '
                            "--fail-under=80\n"
                        ),
                    }
                ]
            }
        }
    }
    assert ci_cov_targets(workflow) == {"verification/scan.py"}, (
        "the repository-wide `--cov=.` was read as a module target; the "
        "pinned list is about modules, and `.` is the whole tree"
    )


def test_a_floor_that_moved_is_reported_as_drift() -> None:
    """A floor that differs from the pinned one is drift, not a detail.

    The floors are a ratchet: every step of one is a decision somebody
    has to look at. If a floor could be lowered in ``ci.yml`` without
    anybody noticing, the ratchet would be a suggestion. This control
    proves the comparison reads a difference at all — without it, a
    checker that returned "no drift" for everything would make
    :func:`test_fail_under_is_module_level` pass for the wrong reason.
    """
    workflow = {
        "jobs": {
            COVERAGE_GATE_JOB: {
                "steps": [
                    {
                        "name": MODULE_GATE_STEP,
                        "run": (
                            './.venv/bin/python3 -m coverage report '
                            '--include="root_cause_task.py" --fail-under=1\n'
                        ),
                    }
                ]
            }
        }
    }
    drift = floor_drift(ci_cov_fail_under(workflow))
    assert "root_cause_task.py" in drift, (
        "a floor that differs from the pinned one was not reported as "
        f"drift; the drift check read {drift}. A floor that can be moved "
        "silently is not a ratchet."
    )
