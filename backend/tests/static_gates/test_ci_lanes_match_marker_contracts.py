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
# A lane that runs pytest must bound it itself, and the bound must leave
# evidence behind
# ---------------------------------------------------------------------------

#: Lanes that start pytest. Every one of them needs its own deadline, and
#: every one of them must be able to end its own step — see
#: ``test_the_watchdog_does_not_hold_the_step_open`` for what "end its own
#: step" has to mean.
_BOUNDED_LANES = ("unit-tests", "unit-staircase")

_WATCHDOG_RE = re.compile(r"WATCHDOG_SECONDS=(\d+)")


def _shell_code(run: str) -> str:
    """``run`` with its comment lines dropped.

    Every check below is about what the step *does*, and a ``run`` block is
    mostly prose: this step's own comment explains the ``tail -f`` it no
    longer runs. Grepping the raw text therefore matches the explanation,
    not the code — which is how the previous version of this file came to
    believe a lane still ran pytest under ``setsid`` on the strength of a
    sentence mentioning ``setsid``.

    A ``#`` at the start of a shell line starts a comment, and none of the
    commands here quote one, so this is a safe approximation rather than a
    shell parser.
    """
    return "\n".join(
        line for line in run.split("\n") if not line.lstrip().startswith("#")
    )


def _pytest_steps(job_name: str) -> list[dict]:
    """The steps in ``job_name`` that actually launch pytest.

    Matched on the budget, not on ``setsid``. The old helper grepped for
    ``setsid``, which is a mechanism rather than an invariant, and it went
    on matching after the mechanism was gone — a comment mentioning
    ``setsid`` was enough to keep a lane in this list, so the gates below
    spent a run reading a step that no longer ran pytest.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"].get(job_name)
    assert job is not None, f"ci.yml has no job named {job_name!r}"
    return [
        step
        for step in job.get("steps", [])
        if _WATCHDOG_RE.search(step.get("run") or "")
    ]


@pytest.mark.parametrize("job_name", _BOUNDED_LANES)
def test_a_lane_that_runs_pytest_bounds_it_itself(job_name: str) -> None:
    """A declared ``timeout-minutes`` is not a ceiling you can rely on.

    That is the whole reason the budget exists, and it is not a theory: on
    run 36544551232 the unit shard's pytest step sat ``in_progress`` for
    44 minutes with ``timeout-minutes: 20`` declared, the post-steps
    stayed ``pending``, and GitHub reaped the job at exactly 45m00s having
    uploaded nothing. Nine ``unit-00``/``root-4`` runs since share that
    signature, and the missing log is the part that mattered — a shard
    that dies without a log names no culprit, which is why locating this
    cost ten hours instead of one run.

    So the invariant is two-sided, and both halves are load-bearing:

      * a lane that runs pytest must declare a budget at all, and
      * the budget must fire BEFORE the step's and the job's ceilings, or
        it can never win the race it exists to win — and a step that dies
        on the runner's own ceiling never reaches its ``if: always()``
        upload.

    Scope, stated honestly: this reads the workflow. It cannot verify that
    a budget is *long enough* for the shard, only that one exists and is
    the smallest of the three numbers that would otherwise fight.
    """
    steps = _pytest_steps(job_name)
    assert steps, (
        f"{job_name!r} has no step that runs pytest under a "
        f"`WATCHDOG_SECONDS=` budget. `timeout-minutes` is enforced by the "
        f"runner, and a runner that has stopped reporting can neither "
        f"enforce one nor upload the log — so without this the step hangs "
        f"until GitHub reaps the job, taking the evidence with it."
    )

    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    job_ceiling_min = workflow["jobs"][job_name].get("timeout-minutes")

    for step in steps:
        run = step["run"]
        match = _WATCHDOG_RE.search(run)
        assert match, (
            f"the {job_name!r} step {step.get('name')!r} declares "
            f"WATCHDOG_SECONDS but never reads it into the `timeout` "
            f"command, so the budget is a comment."
        )
        budget_s = int(match.group(1))

        step_ceiling_min = step.get("timeout-minutes")
        assert step_ceiling_min is not None, (
            f"the {job_name!r} step {step.get('name')!r} has a budget but "
            f"no `timeout-minutes`; the budget's whole job is to beat it"
        )
        assert budget_s < step_ceiling_min * 60, (
            f"the {job_name!r} budget fires at {budget_s}s but the step "
            f"ceiling is {step_ceiling_min}m ({step_ceiling_min * 60}s). The "
            f"budget must be the smaller of the two, or the step dies "
            f"first and the `if: always()` upload never runs — which is the "
            f"exact defect it was added to fix."
        )
        if isinstance(job_ceiling_min, int):
            assert budget_s < job_ceiling_min * 60, (
                f"the {job_name!r} budget fires at {budget_s}s but the "
                f"JOB ceiling is {job_ceiling_min}m "
                f"({job_ceiling_min * 60}s). Same reason, one level up: a "
                f"job cancelled at its ceiling uploads nothing at all."
            )


@pytest.mark.parametrize("job_name", _BOUNDED_LANES)
def test_the_watchdog_photographs_the_shard_before_it_kills(job_name: str) -> None:
    """The deadline is only half the instrument; the dump is the other half.

    Killing a wedged shard makes it *finish*, which makes its log upload —
    but a log ends at the last test that STARTED, and the interesting
    question is always one level deeper. ``pytest-timeout``'s thread
    method cannot answer it: it raises into the main thread through
    ``PyThreadState_SetAsyncExc``, delivered only at a bytecode boundary,
    so a test parked in ``waitpid``/``select`` never sees it and produces
    no traceback at all. That is the failure this gate exists for: the
    shards that wedged for ten hours had no ``--timeout`` traceback to
    read and no dump either, so nothing in the repo said where they were.

    ``ci_process_guard`` arms ``faulthandler`` on SIGUSR1 for exactly
    that, so the budget must ask before it kills. Three details are
    load-bearing and all three were settled by running the thing:

    * **The sink is a file, not stderr.** pytest's default ``fd``-level
      capture replaces fd 2 while a test runs, so a dump written to
      stderr is discarded at the moment the shard is killed.
    * **SIGUSR1 reaches pytest ALONE.** ``timeout --foreground`` signals
      the direct child; without it the signal goes to the process GROUP,
      a leaked descendant has no SIGUSR1 handler, so it dies on the spot
      and takes the EOF pytest is blocked on with it — the shard finishes
      and the dump is never written. The thing being photographed has to
      outlive the photograph.
    * **The kill is delayed, not immediate.** ``--kill-after`` is what
      gives the handler time to walk the C stacks; a ``SIGKILL`` at the
      same instant as the request races it and usually wins.

    Scope, stated honestly: this reads the workflow and checks the
    conventions agree with the plugin. It cannot prove a dump will be
    produced on a given hang, only that every part is wired to the same
    channel.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"][job_name]

    for step in _pytest_steps(job_name):
        run = step["run"]
        assert "--signal=USR1" in run, (
            f"the {job_name!r} budget kills the shard without first asking "
            f"for a stack dump. Use `timeout --signal=USR1`, or the next "
            f"wedge buys another 45 minutes and another log that says only "
            f"which test started."
        )
        assert "--foreground" in run, (
            f"the {job_name!r} budget signals the whole process GROUP. An "
            f"unhandled SIGUSR1 terminates its target, so this kills the "
            f"child the shard is blocked on, unblocks the test being "
            f"photographed, and loses the dump. Add `--foreground`."
        )
        assert re.search(r"--kill-after=\d+", run), (
            f"the {job_name!r} budget asks for the dump and kills in the "
            f"same instant, so SIGKILL races the handler and usually wins. "
            f"Add `--kill-after=<n>s`."
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


@pytest.mark.parametrize("job_name", _BOUNDED_LANES)
def test_the_watchdog_does_not_hold_the_step_open(job_name: str) -> None:
    """Nothing this step starts may outlive it holding the runner's pipe.

    The wedge is a pipe that never reaches EOF. ``pytest ... | tee log``
    is the textbook version: ``tee`` returns on EOF, so a descendant that
    inherited the write end and outlived pytest keeps the step open, the
    post-steps stay ``pending``, and GitHub reaps the job having recorded
    nothing. The step that caused it here was one layer worse —
    ``setsid pytest &`` plus ``tail -f --pid`` plus a backgrounded watchdog
    plus ``wait``, four independent ways for the *step's own shell* to fail
    to return, which is what actually happened nine times.

    So the shape is pinned rather than the wording: pytest in the
    FOREGROUND under ``timeout`` (an ordinary child of the step's shell,
    so the shell always returns), its output to a FILE (a descendant
    inherits a file, not the runner's pipe, so there is nothing for it to
    hold), and no helper left in the background.

    Pinned rather than commented because the failure is invisible in every
    run where the budget does *not* fire: the stray process only matters on
    the path that already went wrong, which is exactly the path nobody
    exercises before shipping.
    """
    for step in _pytest_steps(job_name):
        run = _shell_code(step["run"])
        for forbidden, why in (
            (r"\|\s*tee\b", "pytest's output is piped into `tee`"),
            # `tail -f`, `tail -F`, and the `tail -n +1 -f` spelling the
            # old step used all follow a file forever; a bare `tail -n 40`
            # (which this step also does, to show the log) does not.
            (r"\btail\b[^\n]*\s-[fF]\b",
             "a `tail -f` outlives the file it is following and holds the "
             "step open"),
            (r"^\s*wait\s+\$",
             "a bare `wait` blocks the step's shell on a process the runner "
             "may not reap"),
            (r"\bsetsid\b[^\n]*-m\s+pytest",
             "pytest is detached into its own session, out of the tree the "
             "runner knows how to time out"),
        ):
            assert not re.search(forbidden, run, re.MULTILINE), (
                f"the {job_name!r} pytest step {why}. That is the shape that "
                f"made the shard unreapable and its log unrecoverable — a "
                f"step that does not end never reaches its `if: always()` "
                f"upload, so the one artifact that could name the culprit is "
                f"the first thing lost."
            )
        assert re.search(
            r">\s*pytest-\$\{\{\s*matrix\.shard\s*\}\}\.log", run
        ), (
            f"the {job_name!r} pytest step does not redirect pytest to a "
            f"file, so its output goes to the runner's pipe and any "
            f"surviving descendant can hold that pipe open."
        )
        assert re.search(r"<\s*/dev/null", run), (
            f"the {job_name!r} pytest step leaves stdin attached to the "
            f"runner's pipe. A child that reads stdin then blocks the step "
            f"for as long as the runner is willing to wait."
        )


def test_an_upload_of_a_hidden_file_opts_in_to_hidden_files() -> None:
    """A dot-prefixed artifact path is silently skipped by upload-artifact.

    ``actions/upload-artifact`` v4.4 added ``include-hidden-files`` and
    defaulted it to **false**: a path segment beginning with ``.`` never
    matches, and the step reports ``No files were found`` — which reads as
    "the run produced nothing" rather than "the glob excluded it".

    That is not hypothetical here. ``COVERAGE_FILE`` is conventionally
    dot-prefixed, so ``backend/.coverage.<shard>`` was excluded from every
    shard's coverage upload while the file sat on disk at 237 KB. No shard
    ever produced a ``coverage-data-*`` artifact, and the coverage gate —
    which had never once run to completion, because two shards always
    wedged the lane first — could only report ``No data to combine``,
    naming neither a shard nor a cause. The sibling ``pytest-*.log`` upload
    one step earlier is not hidden, which is precisely why it always
    worked and made this look like coverage itself was broken.

    Pinned over every upload step rather than the one that was broken: the
    trap is a property of the file name, so the next step that uploads a
    dotfile hits it too.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))

    offenders: list[str] = []
    checked = 0
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            if "upload-artifact" not in str(step.get("uses", "")):
                continue
            with_ = step.get("with") or {}
            path = str(with_.get("path", ""))
            # Split on whitespace (several paths may be listed) and then
            # on `/`, because the hidden segment is a *component*:
            # `backend/.coverage.unit-00` hides behind a directory. Only
            # literal components count — a `*` or `**` component matches
            # hidden entries only if the pattern says so.
            segments = [
                seg
                for token in path.replace("\n", " ").split()
                for seg in token.split("/")
                if seg.startswith(".") and seg not in (".", "..")
            ]
            if not segments:
                continue
            checked += 1
            if with_.get("include-hidden-files") is not True:
                offenders.append(
                    f"{job_name} / {step.get('name')!r}: path {path!r} names "
                    f"hidden segment(s) {segments} but does not set "
                    f"`include-hidden-files: true`"
                )

    assert checked, (
        "no upload step in ci.yml names a hidden file, so this gate would "
        "pass vacuously — the coverage upload it was written for is the "
        "one that has to keep existing"
    )
    assert not offenders, (
        "these uploads name a dot-prefixed path without opting into hidden "
        "files, so upload-artifact will skip them and report 'No files were "
        "found':\n  " + "\n  ".join(offenders)
    )



# ---------------------------------------------------------------------------
# The I/O-heavy lanes must work around the known runner bug
# ---------------------------------------------------------------------------

#: Mitigation for actions/runner-images#13770: the Ubuntu 24.04 image ships
#: a ``read_ahead_kb`` that thrashes the page cache on I/O-heavy jobs.
_RUNNER_BUG_MITIGATION = "read_ahead_kb"


@pytest.mark.parametrize("job_name", _BOUNDED_LANES)
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
        for job in _BOUNDED_LANES
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
