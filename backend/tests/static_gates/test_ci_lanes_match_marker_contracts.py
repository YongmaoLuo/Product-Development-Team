"""CI lane selection must match the marker contracts the repository declares.

Why this gate exists
--------------------
The markers in ``backend/pytest.ini`` are the repo's statement about *where*
each class of test runs, and the workflow's ``-m`` expressions are what
actually decides it. Nothing tied the two together, and they had drifted in
both directions at once:

* ``perf`` was documented as slow-lane-only — the marker text claimed the
  addopts' ``-m "not slow"`` excluded it, which they never did — so the
  startup-latency benchmarks were collected by the default unit shard and
  asserted a millisecond-scale difference on a shared runner.
* ``test_agent_execute_task_first5.py`` spawns ``pytest`` as a subprocess
  for every test it contains, which is the ``integration`` marker's
  definition, but carried no marker — so the default shard collected it
  too, under a 60-second per-test ceiling it cannot meet.

Neither mistake was visible from any single file. Both made the workflow's
non-lint jobs unreliable, and the second one killed the shard at ~9% before
it ever reached ``static_gates`` — which is how a much larger defect (the
tree gates scanning nothing under CI's working directory; see ENTRY-030)
stayed unnamed.

What this pins
--------------
1. **The default lanes exclude the markers they are documented not to
   run.** ``unit-tests`` and ``unit-staircase`` must exclude ``perf``
   alongside ``e2e`` and ``integration``.
2. **The exclusions have a home.** The slow lane must collect ``perf``;
   excluding a marker from every lane would be a different way of making
   the same tests never run.
3. **A test that spawns subprocesses says so.** The nested-pytest file
   must declare the ``integration`` marker, so its timeout comes from the
   integration lane's budget rather than the unit shard's.

Scope, stated honestly: this reads the workflow's ``-m`` strings and the
marker declarations in source. It does not execute the workflow, and it
cannot tell whether a *value* of a timeout is generous enough — only that
the selection matches the contract. A green run means "the lanes agree with
what the repo says about itself", not "CI passes".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Jobs whose selection this gate owns. ``None`` means "no contract
#: declared here" and is asserted against explicitly, so a job that quietly
#: loses its ``-m`` expression fails rather than passing vacuously.
_MARKER_RE = re.compile(r'-m\s+"([^"]+)"')

#: The set of markers the default lanes must not collect, per the marker
#: documentation in ``backend/pytest.ini``.
_DEFAULT_LANE_EXCLUSIONS = ("e2e", "integration", "perf")


def _run_blocks(job: dict) -> list[str]:
    return [
        step["run"]
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str)
    ]


def _marker_expression(job_name: str) -> str:
    """The ``-m "..."`` expression of the pytest step in *job_name*.

    Raises rather than returning ``""``: a job that stopped passing ``-m``
    is exactly the regression this gate exists to catch, and an empty
    string would satisfy every "must not contain X" assertion below.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"].get(job_name)
    assert job is not None, (
        f"ci.yml has no job named {job_name!r}. If it was renamed, rename it "
        f"here too — a gate that looks for a job that no longer exists "
        f"checks nothing."
    )

    expressions = [
        match.group(1)
        for block in _run_blocks(job)
        for match in _MARKER_RE.finditer(block)
    ]
    assert expressions, (
        f"job {job_name!r} has no `-m \"...\"` marker expression in any of "
        f"its run steps. This gate owns that expression; losing it would "
        f"make every assertion here vacuous."
    )
    assert len(expressions) == 1, (
        f"job {job_name!r} has {len(expressions)} marker expressions "
        f"({expressions}); this gate reads exactly one, and silently "
        f"picking the first would check the wrong lane."
    )
    return expressions[0]


# ---------------------------------------------------------------------------
# The default lanes must not collect what they are documented not to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job_name", ["unit-tests", "unit-staircase"])
@pytest.mark.parametrize("marker", _DEFAULT_LANE_EXCLUSIONS)
def test_default_lane_excludes_the_marker(job_name: str, marker: str) -> None:
    """A default lane must carry ``not <marker>`` for each documented marker."""
    expression = _marker_expression(job_name)
    assert f"not {marker}" in expression, (
        f"the {job_name} lane runs with `-m \"{expression}\"`, which does not "
        f"exclude `{marker}`. Markers the repo documents as belonging to "
        f"another lane must be excluded here, or those tests run in a lane "
        f"whose timeout budget does not fit them."
    )


def test_the_slow_lane_collects_perf() -> None:
    """Excluding ``perf`` everywhere would mean it never runs at all.

    The complement of the assertion above, and the reason it is safe to add
    the exclusion: ``perf`` has a lane. Its budget (``--timeout=1800``) is
    the only one in this workflow that fits a benchmark asserting on
    millisecond-scale timing.

    The job is keyed ``existing-test`` while displaying as "Slow tests
    (main release)" — the key names neither the lane nor the marker, which
    is why this reads the key deliberately rather than guessing from the
    display name. ``_marker_expression`` fails loudly if the key disappears.
    """
    expression = _marker_expression("existing-test")
    assert "perf" in expression, (
        f"the slow lane runs with `-m \"{expression}\"`, which does not "
        f"collect `perf`. The default lanes exclude it (see the test above), "
        f"so with neither collecting it the benchmarks would run nowhere."
    )


# ---------------------------------------------------------------------------
# A test that drives subprocesses must declare that it does
# ---------------------------------------------------------------------------


#: Files that spawn ``pytest`` (or another interpreter) themselves, and the
#: budget their behaviour needs. Each must carry the ``integration`` marker,
#: because the unit lanes' expression excludes it and their per-test ceiling
#: does not fit a cold interpreter start plus a backend import.
_SUBPROCESS_DRIVERS = (
    "backend/tests/integration/test_agent_execute_task_first5.py",
)


@pytest.mark.parametrize("rel_path", _SUBPROCESS_DRIVERS)
def test_a_nested_pytest_driver_declares_integration(rel_path: str) -> None:
    text = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    assert "pytest.mark.integration" in text, (
        f"{rel_path} drives pytest as a subprocess but does not declare the "
        f"`integration` marker. Unmarked, the unit lanes collect it "
        f"(`-m \"not e2e and not integration and not perf\"` does not filter "
        f"it out) and run it under a ceiling a nested interpreter start "
        f"cannot meet on a hosted runner — which is how it was killed "
        f"mid-`subprocess.run` on the first complete CI run."
    )


def test_the_driver_list_is_not_empty() -> None:
    """The parametrised test above must have cases to check."""
    assert _SUBPROCESS_DRIVERS, (
        "no nested-pytest drivers declared — the check above would pass "
        "vacuously"
    )
