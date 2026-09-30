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


# ---------------------------------------------------------------------------
# A lane that detaches pytest must bound it itself
# ---------------------------------------------------------------------------

#: Lanes that start pytest under ``setsid``. Every one of them needs its own
#: deadline, because the one it declares does not reach this process tree.
_SETSID_LANES = ("unit-tests", "unit-staircase")

_WATCHDOG_RE = re.compile(r"WATCHDOG_SECONDS=(\d+)")


def _setsid_steps(job_name: str) -> list[dict]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"].get(job_name)
    assert job is not None, f"ci.yml has no job named {job_name!r}"
    return [
        step
        for step in job.get("steps", [])
        if "setsid" in (step.get("run") or "")
    ]


@pytest.mark.parametrize("job_name", _SETSID_LANES)
def test_a_lane_that_detaches_pytest_bounds_it_itself(job_name: str) -> None:
    """``timeout-minutes`` is not a ceiling once pytest runs under ``setsid``.

    That is the whole reason the watchdog exists, and it is not a theory:
    on run 36544551232 the unit shard's pytest step sat ``in_progress``
    for 44 minutes with ``timeout-minutes: 20`` declared, the post-steps
    stayed ``pending``, and GitHub reaped the job at exactly 45m00s
    having uploaded nothing. Six consecutive runs share that signature,
    and the missing log is the part that mattered — a shard that dies
    without a log names no culprit.

    ``setsid`` is what breaks the declared ceiling: pytest ends up in a
    process group the runner never created, and the runner's timeout
    machinery does not reach it. A plain ``sleep``+``kill`` does, because
    it is an ordinary child of the step's own shell.

    So the invariant this pins is two-sided:

      * a lane that detaches pytest must declare a watchdog at all, and
      * the watchdog must fire BEFORE the step's ceiling, or it can never
        win the race it exists to win.

    Scope, stated honestly: this reads the workflow. It cannot verify that
    a budget is *long enough* for the shard, only that one exists and is
    the smaller of the two numbers that would otherwise fight.
    """
    steps = _setsid_steps(job_name)
    assert steps, (
        f"{job_name!r} no longer starts pytest under setsid — if that was "
        f"deliberate, drop it from _SETSID_LANES; if not, the lane lost the "
        f"process-group isolation its comments describe."
    )

    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    job_ceiling_min = workflow["jobs"][job_name].get("timeout-minutes")

    for step in steps:
        run = step["run"]
        match = _WATCHDOG_RE.search(run)
        assert match, (
            f"the {job_name!r} step {step.get('name')!r} runs pytest under "
            f"`setsid` with no `WATCHDOG_SECONDS=<n>` self-imposed deadline. "
            f"`timeout-minutes` does not reach a setsid'd child (see the "
            f"docstring), so without this the step can hang until GitHub "
            f"reaps the job — uploading no log, which is the failure mode "
            f"this whole shape exists to end."
        )
        budget_s = int(match.group(1))

        step_ceiling_min = step.get("timeout-minutes")
        assert step_ceiling_min is not None, (
            f"the {job_name!r} step {step.get('name')!r} has a watchdog but "
            f"no `timeout-minutes`; the watchdog's whole job is to beat it"
        )
        assert budget_s < step_ceiling_min * 60, (
            f"the {job_name!r} watchdog fires at {budget_s}s but the step "
            f"ceiling is {step_ceiling_min}m ({step_ceiling_min * 60}s). The "
            f"watchdog must be the smaller of the two, or the step dies "
            f"first and the `if: always()` upload never runs — which is the "
            f"exact defect it was added to fix."
        )
        if isinstance(job_ceiling_min, int):
            assert budget_s < job_ceiling_min * 60, (
                f"the {job_name!r} watchdog fires at {budget_s}s but the "
                f"JOB ceiling is {job_ceiling_min}m "
                f"({job_ceiling_min * 60}s), leaving no room for the upload "
                f"steps that run after it fails."
            )


@pytest.mark.parametrize("job_name", _SETSID_LANES)
def test_the_watchdog_photographs_the_shard_before_it_kills(job_name: str) -> None:
    """The deadline is only half the instrument; the dump is the other half.

    Killing a wedged shard makes it *finish*, which makes its log upload —
    but a log ends at the last test that STARTED, and the interesting
    question is always one level deeper. ``pytest-timeout``'s thread
    method cannot answer it: it raises into the main thread through
    ``PyThreadState_SetAsyncExc``, delivered only at a bytecode boundary,
    so a test parked in ``waitpid``/``select`` never sees it and produces
    no traceback at all.

    ``ci_process_guard`` arms ``faulthandler`` on SIGUSR1 for exactly
    that, so the watchdog must ask before it kills. Two details are
    load-bearing and both were found by running the thing:

    * **The sink is a file, not stderr.** pytest's default ``fd``-level
      capture replaces fd 2 while a test runs, so a dump written to
      stderr is discarded at the moment the shard is killed.
    * **SIGUSR1 goes to the leader, not the group.** An unhandled SIGUSR1
      terminates its target, so a group-wide one would kill whatever the
      shard is blocked *on* — unblocking the very test being photographed.

    Scope, stated honestly: this reads the workflow and checks the
    conventions agree with the plugin. It cannot prove a dump will be
    produced on a given hang, only that every part is wired to the same
    channel.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"][job_name]

    for step in _setsid_steps(job_name):
        run = step["run"]
        assert 'kill -USR1 "$PYTEST_PID"' in run, (
            f"the {job_name!r} watchdog kills the shard without first "
            f"asking for a stack dump. Add the SIGUSR1 before the TERM, or "
            f"the next wedge buys another 45 minutes and another log that "
            f"says only which test started."
        )
        assert 'kill -USR1 -"$PYTEST_PID"' not in run, (
            f"the {job_name!r} watchdog sends SIGUSR1 to the whole process "
            f"GROUP. An unhandled SIGUSR1 terminates its target, so this "
            f"kills the child the shard is blocked on and unblocks the test "
            f"being photographed. Send it to $PYTEST_PID alone."
        )
        env = step.get("env") or {}
        assert "CI_STACK_DUMP_PATH" in env, (
            f"the {job_name!r} step does not tell the plugin where to write "
            f"the dump, so the artifact below cannot name it."
        )

    # ...and the file the plugin writes is the file the upload collects.
    uploads = [
        step
        for step in job.get("steps", [])
        if "pytest-stack-dump" in str(step.get("with", {}).get("path", ""))
    ]
    assert uploads, (
        f"no upload step in {job_name!r} collects the stack dump, so a dump "
        f"would be written into the workspace and thrown away with the "
        f"runner — the same 'no evidence' outcome the watchdog exists to end."
    )


@pytest.mark.parametrize("job_name", _SETSID_LANES)
def test_the_watchdog_does_not_hold_the_step_open(job_name: str) -> None:
    """A background helper may not keep the runner's output pipe open.

    ``kill $WATCHDOG_PID`` kills the subshell, not its ``sleep``: the child
    is reparented and keeps running — for up to the whole budget — after the
    step's shell has exited. Left holding the write end of the step's
    stdout, it makes the runner wait for an EOF that arrives fifteen minutes
    after the step ended. That is the same mechanism as the ``tee`` wedge
    this job's own comments describe, and the redirect is what stops the
    watchdog from re-creating it.

    Pinned rather than commented because the failure is invisible in every
    run where the budget does *not* fire: the stray process only matters on
    the path that already went wrong, which is exactly the path nobody
    exercises before shipping.
    """
    for step in _setsid_steps(job_name):
        run = step["run"]
        assert re.search(
            r"\)\s*>\s*/dev/null\s+2>&1\s*&\s*\n\s*WATCHDOG_PID=", run
        ), (
            f"the {job_name!r} watchdog is started without redirecting its "
            f"own stdout/stderr. Its `sleep` outlives the kill, so it would "
            f"go on holding the step's output pipe and delay the runner by "
            f"the length of the budget. Redirect the subshell before "
            f"backgrounding it."
        )
        assert "DEADLINE_FLAG=" in run, (
            f"the {job_name!r} watchdog sets no deadline marker, so the "
            f"failure message cannot tell 'the budget fired' from 'something "
            f"else killed pytest' — an OOM kill is also exit 137, and 143 "
            f"alone is ambiguous."
        )


# ---------------------------------------------------------------------------
# The I/O-heavy lanes must work around the known runner bug
# ---------------------------------------------------------------------------

#: Mitigation for actions/runner-images#13770: the Ubuntu 24.04 image ships
#: a ``read_ahead_kb`` that thrashes the page cache on I/O-heavy jobs.
_RUNNER_BUG_MITIGATION = "read_ahead_kb"


@pytest.mark.parametrize("job_name", _SETSID_LANES)
def test_a_heavy_lane_mitigates_the_known_runner_bug(job_name: str) -> None:
    """The lanes that die must carry the workaround the nightly already has.

    Diagnosed from run 36549676307. This shard's pytest step sat
    ``in_progress`` past **both** its own ``timeout-minutes: 20`` and the
    watchdog's 15-minute budget, while ``misc`` — a different, smaller VM on
    the same run — passed in 99s. Two independent ceilings failing together
    is not a slow test; it is a runner that has stopped reporting.

    That also means no in-workflow budget can be the remedy: every ceiling in
    the file is enforced by a process *on the runner*, so a runner that can
    no longer report can neither enforce one nor upload the log. Not
    triggering the bug is the only defence, and the mitigation for exactly
    this bug has been sitting on the nightly job since it was first seen —
    just not on the shards that hit it.

    Two properties, both of which the nightly's copy lacks:

    * it runs **before** the pytest step, or it mitigates nothing, and
    * it reports what it touched and warns when the globs match no device. A
      mitigation that silently no-ops is indistinguishable from one that was
      deleted, and the device names on the runner image have changed before.

    Scope, stated honestly: this reads the workflow. It can confirm the
    mitigation is present, correctly ordered and self-reporting; it cannot
    confirm the kernel accepted the write.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"][job_name]["steps"]

    mitigating = [
        i
        for i, step in enumerate(steps)
        if _RUNNER_BUG_MITIGATION in (step.get("run") or "")
    ]
    assert mitigating, (
        f"{job_name!r} has no `{_RUNNER_BUG_MITIGATION}` step. This lane runs "
        f"pytest with --cov over thousands of test modules; without the "
        f"runner-images#13770 workaround its VM can stop reporting, and then "
        f"no timeout, watchdog or upload in this file can fire — which is "
        f"exactly the 45-minute reap with no evidence."
    )

    detaching = [
        i
        for i, step in enumerate(steps)
        if "setsid" in (step.get("run") or "")
    ]
    assert detaching, f"{job_name!r} no longer starts pytest under setsid"
    assert min(mitigating) < min(detaching), (
        f"in {job_name!r} the `{_RUNNER_BUG_MITIGATION}` step comes after the "
        f"pytest step, so the shard has already done its I/O by the time the "
        f"mitigation applies"
    )

    body = steps[min(mitigating)]["run"]
    assert "::warning::" in body, (
        f"the {job_name!r} mitigation cannot say that it did nothing. The "
        f"globs are matched against runner-image device names, so a stale "
        f"glob list turns the whole step into a silent no-op — and silence "
        f"reads as 'mitigated'."
    )


# ---------------------------------------------------------------------------
# The bisect harness must be the same shape as what it bisects
# ---------------------------------------------------------------------------

#: pytest flags that change *how* a shard runs rather than what it selects.
#: ``--cov`` is the one that matters: it instruments every module under
#: ``backend/`` at collection time, which is the I/O-heavy step.
_SHAPE_FLAGS = ("--cov=.", "-p ci_process_guard", "--tb=short")


@pytest.mark.parametrize("flag", _SHAPE_FLAGS)
def test_the_bisect_lane_runs_the_shape_it_bisects(flag: str) -> None:
    """``unit-staircase`` must invoke pytest like ``unit-tests`` does.

    The staircase exists to bisect a shard that wedges. It ran **without**
    ``--cov=.`` — on the stated grounds that ``coverage-gate`` does not run
    on a dispatch — and that made it a reproduction of something other than
    the thing under investigation. Four bisect rounds came back entirely
    green, controls included, on a wedge that had failed five consecutive
    push runs:

    ==========================  ===============  ===================
    lane                        shape             result
    ==========================  ===============  ===================
    ``Staircase (u00-full)``    no ``--cov=.``    20s, 273 passed
    ``Unit tests (unit-00)``    ``--cov=.``       reaped at 45m00s
    ==========================  ===============  ===================

    Same 24 files, same 273 tests. The harness cannot find a bug that only
    the heavier shape has, and it reports "all green" — the single most
    expensive answer a bisect tool can give.

    The coverage *gate* is genuinely irrelevant here and stays off; only
    the flag that shapes collection is pinned. The two lanes differ in
    their watchdog budget and step ceiling on purpose (the staircase's is
    longer, because a bisect round is expected to be the thing that
    wedges), and those are covered by the test above.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    missing = [
        job
        for job in _SETSID_LANES
        if not any(
            flag in (step.get("run") or "")
            for step in workflow["jobs"][job].get("steps", [])
            if "setsid" in (step.get("run") or "")
        )
    ]
    assert not missing, (
        f"{', '.join(missing)} no longer passes {flag!r} to pytest, so the "
        f"bisect harness runs a different shape than the shard it bisects"
    )
