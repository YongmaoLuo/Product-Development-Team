"""The verification markers are registered, excluded by the fast shards, and
collected by a shard of their own.

Why this gate exists
--------------------

The verification work brought three new markers with it — ``observational``,
``real_model`` and ``needs_real_process`` — and each of them answers a
question the fast shards cannot answer: does this case merely CHARACTERISE a
behaviour, does it really call a model, does it really start a process? A
marker that says any of those things is a request not to be run beside seven
other workers on a shared runner, and the request has to be honoured in the
lane's ``-m`` expression.

Nothing tied the two together. The marker list in ``pytest.ini`` and the
``-m`` expressions in ``ci.yml`` are two files that both claim to say where
a case runs, and this repository has already paid for letting them drift:
``perf`` was documented as excluded from the default lanes while the addopts
never excluded it, and the startup-latency benchmarks were collected by the
unit shard and asserted a millisecond-scale difference on a shared runner.
The gate that caught that one is ``test_ci_lanes_match_marker_contracts.py``;
this file is the same idea for the three markers that arrived afterwards.

What this pins
--------------
1. **The markers are declared.** Under ``--strict-markers`` a marker that
   is not in the configuration is a collection ERROR, not a warning — so a
   case that uses ``observational`` without the declaration does not run at
   all, it fails the run that was supposed to prove something about it.
2. **The default lanes exclude them.** The unit shards are the ones that
   run with eight workers beside them, so a case that only means something
   on a quiet machine has to be kept out of them.
3. **Something still collects them.** Excluding a marker everywhere is the
   other way of making a case never run, and it is invisible: the lane is
   green, the file is unrun, and nothing in the tree says the two are
   connected. So every marker the fast shards drop has to be named by a lane
   that collects it.

Scope, stated honestly
----------------------

This reads the workflow and the marker declarations. It does not execute the
workflow, so a green run here means "the lanes agree with what the
repository says about itself" — not "the lanes passed on a runner". The two
are different claims, and only the first one is a static gate's to make.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

#: Repository root. This file lives at ``backend/tests/static_gates/``, so
#: the root is three parents up.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

#: The workflow under test.
WORKFLOW: Path = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The pytest configuration that declares the markers.
PYTEST_INI: Path = REPO_ROOT / "pytest.ini"

#: The three markers this plan registered. Each one says the case needs
#: something the fast shards cannot give it: a quiet machine, a live
#: provider, or a real process.
NEW_MARKERS = ("observational", "real_model", "needs_real_process")

#: The lanes that run the pull request's fast shards. These are the lanes
#: that must not collect a marker the plan says belongs to a shard of its
#: own.
DEFAULT_LANE_JOBS = ("unit-tests", "unit-staircase")

#: The shard that owns the verification end-to-end cases.
VERIFICATION_JOB = "e2e-verification"

#: The directory that shard owns.
VERIFICATION_LANE_PATH = "tests/e2e/verification"

#: The job whose ``needs`` decide whether a merge may happen.
MERGE_GATE_JOB = "merge-gate"

#: The lanes whose numbers are wall-clock numbers, and which therefore have
#: to keep the machine to themselves.
WALL_CLOCK_JOBS = ("existing-test", "verification-perf")

#: The ``-m "..."`` expression of a pytest step.
_MARKER_EXPRESSION_RE = re.compile(r'-m\s+"([^"]+)"')

#: xdist's worker flag. ``-n 8`` and ``-n=8`` are the same request, and the
#: second spelling is what a careful editor produces.
_XDIST_WORKERS_RE = re.compile(r"(?<!\S)-n[= ]\s*\d+")

#: xdist's other entry points. ``--dist`` selects the scheduling mode and
#: ``-p xdist`` loads the plugin; either one parallelises a run that has to
#: stay serial.
_XDIST_OTHER_RE = re.compile(r"(?<!\S)(--dist[= ]|--dist\b|-p\s+xdist)")


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _workflow() -> Dict[str, Any]:
    """The parsed workflow."""
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> Dict[str, Any]:
    """The workflow's jobs."""
    return _workflow().get("jobs") or {}


def _job(name: str) -> Dict[str, Any]:
    """The job called *name*, asserted present.

    A missing job is a named failure rather than a ``None`` that the next
    assertion turns into a confusing ``TypeError``: the reader wants to know
    which lane is missing, not which line blew up.
    """
    job = _jobs().get(name)
    assert job is not None, (
        f"ci.yml has no job named {name!r}. If it was renamed, rename it "
        f"here too — a gate that looks for a job that no longer exists "
        f"checks nothing."
    )
    return job


def _run_blocks(job: Dict[str, Any]) -> List[str]:
    """The ``run:`` bodies of *job*."""
    return [
        step["run"]
        for step in job.get("steps", []) or []
        if isinstance(step.get("run"), str)
    ]


def _shell_code(job: Dict[str, Any]) -> str:
    """The job's shell code with its comment lines dropped.

    A ``run:`` block is mostly prose, and prose mentions ``-n 8`` and the
    markers by name while explaining itself. Only the commands are the job.
    """
    lines: List[str] = []
    for block in _run_blocks(job):
        for line in block.split("\n"):
            if line.lstrip().startswith("#"):
                continue
            lines.append(line)
    return "\n".join(lines)


def _marker_expressions(job: Dict[str, Any]) -> List[str]:
    """Every ``-m "..."`` expression in *job*."""
    return [
        match.group(1)
        for block in _run_blocks(job)
        for match in _MARKER_EXPRESSION_RE.finditer(block)
    ]


def _the_marker_expression(job_name: str) -> str:
    """The one ``-m "..."`` expression of the lane called *job_name*.

    Raises rather than returning ``""``: a lane that stopped passing ``-m``
    is exactly the regression this gate exists to catch, and an empty string
    would satisfy every "must contain X" assertion below.
    """
    expressions = _marker_expressions(_job(job_name))
    assert expressions, (
        f"job {job_name!r} has no `-m \"...\"` marker expression in any of "
        f"its run steps. This gate owns that expression; losing it would "
        f"make every assertion here vacuous."
    )
    assert len(expressions) == 1, (
        f"job {job_name!r} has {len(expressions)} marker expressions "
        f"({expressions}); this gate reads exactly one, and silently picking "
        f"the first would check the wrong lane."
    )
    return expressions[0]


def _declared_markers(path: Path) -> List[str]:
    """The marker names *path* registers.

    ``configparser`` rather than a regex: the block is an indented
    multi-line value whose entries carry long descriptions containing
    colons, backticks and quotes, and a hand-rolled line parser is how this
    repository's other gates end up matching their own prose.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    raw = parser["pytest"]["markers"]
    return [
        line.split(":", 1)[0].strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _collects(job: Dict[str, Any], marker: str) -> bool:
    """True when one of the job's ``-m`` expressions collects *marker*.

    "Collects" means the marker appears in a *selecting* position: the
    expression names it and does not name it as an exclusion. The negative
    form is ``not <marker>``, so the two are told apart by what precedes the
    name rather than by whether the name is there at all — an expression
    that says ``not observational`` mentions ``observational`` in every way
    that matters, and reading that as a collection would make this gate pass
    on the exact shape it exists to catch.
    """
    pattern = re.compile(rf"(?<!not )\b{re.escape(marker)}\b")
    for expression in _marker_expressions(job):
        normalised = " ".join(expression.split())
        if pattern.search(normalised):
            return True
    return False


def _needs_of(job: Dict[str, Any]) -> List[str]:
    """The job's ``needs``, as a list."""
    needs = job.get("needs")
    if needs is None:
        return []
    return [needs] if isinstance(needs, str) else list(needs)


# ---------------------------------------------------------------------------
# The markers are declared
# ---------------------------------------------------------------------------


def test_markers_declared() -> None:
    """All three new markers are registered in ``pytest.ini``.

    Under ``--strict-markers`` an undeclared marker is a collection ERROR,
    so this is not a tidiness rule: a case that carries ``observational``
    without the declaration fails the run that was supposed to prove
    something about it, and the failure names the marker rather than the
    case.
    """
    declared = _declared_markers(PYTEST_INI)
    missing = [marker for marker in NEW_MARKERS if marker not in declared]
    assert not missing, (
        f"pytest.ini declares no {missing}. A marker that is not declared is "
        f"a collection error under --strict-markers, so every case that "
        f"carries one of them fails to run at all. Declared markers: "
        f"{declared}."
    )


# ---------------------------------------------------------------------------
# The default lanes do not collect them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job_name", DEFAULT_LANE_JOBS)
@pytest.mark.parametrize("marker", NEW_MARKERS)
def test_default_lanes_exclude_new_markers(job_name: str, marker: str) -> None:
    """A fast shard must carry ``not <marker>`` for each new marker.

    These shards run eight workers beside each other. A case that measures
    something, calls a model, or starts a real process does not mean the
    same thing there — and the unit shard's per-test ceiling is 60
    seconds, which a cold interpreter plus a live provider cannot meet.
    """
    expression = _the_marker_expression(job_name)
    assert f"not {marker}" in expression, (
        f"the {job_name} lane runs with `-m \"{expression}\"`, which does not "
        f"exclude `{marker}`. The markers this plan registered say the case "
        f"needs a quiet machine, a live provider or a real process, and the "
        f"fast shards give it none of those."
    )


# ---------------------------------------------------------------------------
# ...and a lane collects them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", NEW_MARKERS)
def test_lane_collects_new_markers(marker: str) -> None:
    """Some lane collects each marker the fast shards drop.

    The complement of the assertion above, and the reason it is safe to add
    the exclusions: excluding a marker everywhere is the other way of
    making a case never run, and it is invisible from the tree — the lane
    is green and the file is unrun.
    """
    collectors = sorted(
        name for name, job in _jobs().items() if _collects(job, marker)
    )
    assert collectors, (
        f"no lane in ci.yml collects `{marker}`. The default lanes exclude "
        f"it, so a marker that no lane collects is a case that runs "
        f"nowhere — and the lane that excludes it still reports green."
    )


def test_the_verification_lane_owns_the_verification_directory() -> None:
    """The verification shard runs ``tests/e2e/verification``.

    The cases that need a shard of their own live in one directory. A lane
    that stopped naming it would collect nothing, and the marker expression
    alone would not notice: the expression says what to select, the path
    says what there is to select.
    """
    code = _shell_code(_job(VERIFICATION_JOB))
    assert VERIFICATION_LANE_PATH in code, (
        f"the {VERIFICATION_JOB!r} job does not run {VERIFICATION_LANE_PATH!r}. "
        f"That directory holds the cases this shard exists for; a lane that "
        f"stopped naming it collects nothing and reports green."
    )


def test_the_verification_lane_is_serial() -> None:
    """The verification shard runs one pytest process, not eight.

    The cases in that directory start real servers, real git worktrees and
    real subprocesses. Two of them at once contend for the same ports and
    the same repository, so a parallelised run is not faster — it is two
    halves of the same contract arguing with each other.
    """
    code = _shell_code(_job(VERIFICATION_JOB))
    workers = _XDIST_WORKERS_RE.search(code)
    assert not workers, (
        f"the {VERIFICATION_JOB!r} job runs pytest with xdist workers "
        f"({workers.group(0) if workers else ''!r}). These cases start real "
        f"servers and real subprocesses; running them beside each other is "
        f"how a port collision looks like a contract failure."
    )
    other = _XDIST_OTHER_RE.search(code)
    assert not other, (
        f"the {VERIFICATION_JOB!r} job parallelises its pytest run "
        f"({other.group(0) if other else ''!r}). Serial means serial: one "
        f"process, no scheduler, no other worker's noise in the result."
    )


def test_the_verification_lane_is_chained_after_the_shards() -> None:
    """The verification shard starts on a quiet machine.

    The unit shards run in parallel with anything that is not chained after
    them, so an unchained lane starts on a machine that is still busy. The
    case that reproduces a failure then reproduces it under a load it was
    never written for.
    """
    needs = _needs_of(_job(VERIFICATION_JOB))
    assert "unit-tests" in needs, (
        f"the {VERIFICATION_JOB!r} job needs {needs!r} and not 'unit-tests'. "
        f"The shards run in parallel with anything that is not chained after "
        f"them, so an unchained lane starts on a machine that is still busy."
    )


def test_the_verification_lane_is_inside_the_merge_gate() -> None:
    """A pull-request lane that nothing watches is a lane that fails quietly.

    The merge gate aggregates the pull request's jobs, and a job outside it
    is unmonitored: if it goes red nothing closes, and if it is skipped
    nothing complains.
    """
    needs = _needs_of(_job(MERGE_GATE_JOB))
    assert VERIFICATION_JOB in needs, (
        f"the merge gate needs {needs!r} and not {VERIFICATION_JOB!r}. A "
        f"pull-request job outside the gate is unmonitored — if it goes red "
        f"nothing closes, and if it is skipped nothing complains."
    )


# ---------------------------------------------------------------------------
# The wall-clock lanes stay serial
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job_name", WALL_CLOCK_JOBS)
def test_the_wall_clock_lanes_stay_serial(job_name: str) -> None:
    """``perf`` and ``time_sensitive`` still have the machine to themselves.

    Both lanes assert on wall-clock time, and a wall-clock bound measured
    beside other workers is a measurement of the workers. Registering new
    markers next to them is exactly the kind of edit that gives one of
    them a ``-n 8`` while nobody is looking.
    """
    code = _shell_code(_job(job_name))
    workers = _XDIST_WORKERS_RE.search(code)
    assert not workers, (
        f"the {job_name!r} job runs pytest with xdist workers "
        f"({workers.group(0) if workers else ''!r}). Its budgets are "
        f"wall-clock numbers, and a wall-clock number measured beside "
        f"other workers is a measurement of the workers."
    )
