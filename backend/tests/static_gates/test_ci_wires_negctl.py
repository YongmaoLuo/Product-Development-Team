"""The negative controls must be wired into CI, not only run on a laptop.

Why this gate exists
--------------------
``scripts/negctl_p0_1.sh`` and ``scripts/negctl_p0_3.sh`` are the negative
controls for the P0-1 and P0-3 fixes. Each one answers the half of the
claim that a behavioural suite cannot: *"the bug was there"*. It reads the
PRE-FIX source out of git and shows that the shape the fix removed really
existed — so a green behavioural suite measures a difference that existed
to begin with, rather than a difference between two versions of the same
code.

That makes them controls, and a control that only runs on the machine of
the person who wrote it is not a gate:

* it is green on every laptop, so it proves nothing to anybody else;
* the baseline it reads is only reachable with a full clone, and a
  checkout at the default depth does not have it — so the controls
  could not even run there;
* and a control that cannot fail is not a control: if it silently skips,
  it reads exactly like one that passed.

So the controls are wired into ``.github/workflows/ci.yml`` as their own
job, and this file is what stops that wiring from quietly rotting. Each
way it can rot is one test below:

* the job disappears (or is renamed) — the controls stop running at all;
* a script path is dropped, or both scripts collapse into one step — one
  control silently stops running;
* the job stays but leaves the merge gate's ``needs`` — it goes red and
  nothing closes, which is the failure mode every other gate in this
  repository was written to avoid;
* the checkout goes shallow, the job grows no time cap, it reaches for a
  field the workflow does not allow, or it starts pulling in an action
  nobody has vetted — the last of those is the one that turns a routine
  CI change into a supply-chain decision, and the third is the one that
  turns a wedged runner into a 45-minute wait.

The whitelist is not this file's invention: ``ci.yml``'s own header
comment declares it the single source of truth and names the Python
constants that are supposed to mirror it. Those constants are checked
against that comment here, because the file the comment points at
(``tests/integration/test_ci_alignment.py``) does not exist in this
repository — so without this check the comment and the constants would
drift apart silently, and the comment would be the one that still reads
like the truth.

Scope, stated honestly: this reads the workflow file and asserts on its
structure. It does not run the controls, so it cannot prove that they
still pass — it proves that they are still *wired*, which is the part a
refactor of ``ci.yml`` can silently undo.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The job that runs the two negative controls.
NEGCTL_JOB = "negctl"

#: The terminal job that decides whether a pull request may merge.
MERGE_GATE_JOB = "merge-gate"

#: The controls, in the order the job runs them. Paths are relative to
#: the repository root, which is where the job's ``run:`` steps execute.
NEGCTL_SCRIPTS = ("scripts/negctl_p0_1.sh", "scripts/negctl_p0_3.sh")

#: The field whitelist, as ci.yml's own header comment declares it. The
#: comment calls itself the single source of truth and points at Python
#: constants that are supposed to mirror it — a test file that does not
#: exist in this repository — so these are those constants, and
#: ``test_negctl_job_uses_only_whitelisted_fields`` checks them against
#: the comment so the two cannot drift apart.
ALLOWED_TOP_LEVEL_FIELDS = frozenset(
    {"name", "on", "concurrency", "permissions", "jobs"}
)
ALLOWED_JOB_LEVEL_FIELDS = frozenset(
    {
        "name",
        "runs-on",
        "needs",
        "if",
        "steps",
        "permissions",
        "timeout-minutes",
        "concurrency",
        "strategy",
        "env",
        "continue-on-error",
    }
)
ALLOWED_STEP_LEVEL_FIELDS = frozenset(
    {
        "id",
        "name",
        "if",
        "run",
        "uses",
        "with",
        "working-directory",
        "continue-on-error",
        "env",
        "shell",
        "timeout-minutes",
    }
)

#: The most a test lane in this workflow is allowed to run. The unit
#: shards and the nightly get 40 minutes; nothing that gates a pull
#: request gets more, because a job that outlives the merge gate it
#: feeds is a job nobody is waiting for.
MAX_TEST_LANE_TIMEOUT_MINUTES = 40

#: ``#   * Job-level fields:`` and the comma-separated lines under it.
_WHITELIST_LABEL = re.compile(r"^\*\s*([A-Za-z][A-Za-z-]*)\s+fields:\s*$")
_WHITELIST_TOKEN = re.compile(r"^[a-z][a-z0-9-]*$")


def _declared_whitelist(text: str) -> dict:
    """The field whitelist as ci.yml's own header comment declares it.

    The comment is three labelled groups, each followed by comma-separated
    field names that may wrap onto the next comment line::

        #   * Job-level fields:
        #       name, runs-on, needs, if, steps, permissions, timeout-minutes,
        #       concurrency, strategy, env, continue-on-error

    Reading the list out of the file — rather than restating it here —
    is what keeps the comment and the constants from drifting. The
    comment says it is the single source of truth; this is the one place
    that takes it at its word.
    """
    groups: dict = {}
    label = None
    for raw in text.splitlines():
        if not raw.startswith("#"):
            if groups:
                break
            continue
        body = raw[1:].strip()
        match = _WHITELIST_LABEL.match(body)
        if match:
            label = match.group(1).lower().replace("-", " ")
            groups.setdefault(label, [])
            continue
        if label is None:
            continue
        tokens = [tok.strip() for tok in body.split(",")]
        tokens = [tok for tok in tokens if tok]
        if not tokens or not all(_WHITELIST_TOKEN.match(tok) for tok in tokens):
            # Not a field list, so the whitelist block is over.
            if groups:
                break
            continue
        groups[label].extend(tokens)
    return groups


@pytest.fixture(scope="module")
def workflow() -> dict:
    if not WORKFLOW.is_file():
        pytest.fail(
            f"{WORKFLOW} does not exist; there is no CI to wire the "
            f"negative controls into"
        )
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def workflow_text() -> str:
    """ci.yml as text — the whitelist lives in its header comment."""
    if not WORKFLOW.is_file():
        pytest.fail(
            f"{WORKFLOW} does not exist; there is no CI to wire the "
            f"negative controls into"
        )
    return WORKFLOW.read_text(encoding="utf-8")


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


def _run_steps(job: dict) -> list[dict]:
    """The job's steps that carry a ``run:`` body."""
    return [
        step
        for step in (job.get("steps") or [])
        if isinstance(step, dict) and step.get("run")
    ]


#: The env var each control reads its baseline revision from, derived from
#: the script's file name: ``negctl_p0_1.sh`` reads ``P0_1_PREFIX_REV``.
#: The mapping is derived rather than written out by hand because a
#: hand-written mapping is a second list to keep in sync with the scripts' own
#: defaults.
_BASELINE_ENV = re.compile(r"p0_(?P<slot>\d+)")


def _baseline_env_var(script: str) -> str:
    """The env var the control in *script* reads its baseline revision from."""
    match = _BASELINE_ENV.search(Path(script).stem)
    assert match, (
        f"cannot derive the baseline env var for {script}: its file name "
        f"carries no `p0_<n>` slot"
    )
    return f"{match.group(0).upper()}_PREFIX_REV"


def _step_running(job: dict, script: str) -> dict | None:
    """The step whose ``run:`` body invokes *script*, if there is one."""
    for step in _run_steps(job):
        if script in str(step.get("run", "")):
            return step
    return None


def test_negctl_job_present(jobs: dict) -> None:
    """There is a ``negctl`` job, and it is wired to run after the unit lane.

    The job is chained after ``unit-tests`` on purpose. The controls run
    behavioural suites of their own, and running them beside the shards
    would spend a second matrix on a tree whose unit lane has not been
    shown to be green yet.

    The checkout has to be deep. Both controls read their baseline out of
    git (``git show <rev>:<path>``), and ``actions/checkout``'s default
    is ``fetch-depth: 1`` — a shallow clone in which the baseline commit
    does not exist. A control that cannot read its baseline exits
    non-zero rather than skipping, so a shallow checkout does not make
    this job vacuously green; it makes it red. Either way it is wrong,
    and this assertion is what says which knob turns it.
    """
    assert NEGCTL_JOB in jobs, (
        f"ci.yml has no `{NEGCTL_JOB}` job, so the negative controls for "
        f"P0-1 and P0-3 never run in CI. They are controls, not local "
        f"conveniences: a control that only runs on the machine of the "
        f"person who wrote it proves nothing to anybody else, and the "
        f"baseline it reads is not even reachable from a shallow clone."
    )
    job = jobs[NEGCTL_JOB]

    needs = _needs_of(job)
    assert "unit-tests" in needs, (
        f"the `{NEGCTL_JOB}` job needs {needs!r}, which does not include "
        f"`unit-tests`. The controls run behavioural suites of their own; "
        f"running them beside the shards spends a second matrix on a tree "
        f"whose unit lane has not been shown to be green yet."
    )

    checkout_steps = [
        step
        for step in (job.get("steps") or [])
        if "actions/checkout" in str(step.get("uses", ""))
    ]
    assert checkout_steps, (
        f"the `{NEGCTL_JOB}` job does not check the repository out, so the "
        f"controls have no working tree and no git history to read a "
        f"baseline from"
    )
    depth = (checkout_steps[0].get("with") or {}).get("fetch-depth")
    assert str(depth) == "0", (
        f"the `{NEGCTL_JOB}` job's checkout has fetch-depth={depth!r}. The "
        f"controls read their pre-fix baseline out of git history, and "
        f"actions/checkout's default (1) is a shallow clone in which that "
        f"commit does not exist — the control then exits non-zero rather "
        f"than skipping, so the job goes red on a tree that is fine. Use "
        f"fetch-depth: 0."
    )


def test_both_scripts_invoked(jobs: dict) -> None:
    """Both controls run, and each runs in its own step.

    One step per control, not one step running both: the controls are
    independent claims about independent fixes, and a single step that
    runs the first script and then the second reports only the first
    failure. Separated, a red run names which control broke.

    The scripts are also asserted to exist. A ``run:`` step that invokes
    a path that is not in the repository fails at run time, on the
    runner, where the failure reads as "the control is broken" rather
    than "the control was never wired up".
    """
    job = jobs.get(NEGCTL_JOB)
    assert job is not None, (
        f"ci.yml has no `{NEGCTL_JOB}` job, so neither negative control "
        f"runs in CI"
    )

    steps = _run_steps(job)
    holders = {
        script: [step for step in steps if script in str(step.get("run", ""))]
        for script in NEGCTL_SCRIPTS
    }
    missing = [script for script, hits in holders.items() if not hits]
    assert not missing, (
        f"the `{NEGCTL_JOB}` job does not run {missing}. Every negative "
        f"control that is not wired in is a control that never runs, and "
        f"a control that never runs is indistinguishable from one that "
        f"passed."
    )

    first, second = (holders[script][0] for script in NEGCTL_SCRIPTS)
    assert first is not second, (
        f"both controls run in the same step of the `{NEGCTL_JOB}` job. "
        f"They are independent claims about independent fixes, and one "
        f"step that runs the first and then the second reports only the "
        f"first failure — a red run then names no control at all."
    )

    absent = [
        script for script in NEGCTL_SCRIPTS if not (REPO_ROOT / script).is_file()
    ]
    assert not absent, (
        f"the `{NEGCTL_JOB}` job runs {absent}, which are not in the "
        f"repository. The step would fail on the runner, where it reads "
        f"as a broken control rather than a control that was never wired "
        f"up."
    )


def test_merge_gate_depends(jobs: dict) -> None:
    """The merge gate covers the controls.

    A job outside the gate's ``needs`` is unmonitored: if it goes red,
    nothing closes, and if it is skipped, nothing complains. That is the
    whole reason this repository has a terminal merge gate — see
    ``test_merge_gate_contract.py`` — and a new PR-gating job that is not
    in it is exactly the unmonitored job that gate exists to catch.

    The controls are the case where this matters most: they read their
    baseline out of git, so they are the jobs most likely to be red for a
    reason nobody is looking at.
    """
    gate = jobs.get(MERGE_GATE_JOB)
    assert gate is not None, (
        f"ci.yml has no `{MERGE_GATE_JOB}` job; without it nothing "
        f"aggregates the pull request's jobs and no failure blocks a "
        f"merge"
    )
    needs = _needs_of(gate)
    assert NEGCTL_JOB in needs, (
        f"the merge gate needs {needs!r}, which does not include "
        f"`{NEGCTL_JOB}`. The controls would run on every pull request and "
        f"their failure would block nothing — a red control that closes "
        f"no gate is a control nobody has to fix."
    )


def test_negctl_job_is_bounded_by_a_timeout(jobs: dict) -> None:
    """The job carries a time cap.

    The controls run two pytest suites of their own, so this job is a
    test lane, and every test lane in this workflow is capped. Without a
    cap the job runs until GitHub cancels it at ~45 minutes — the "lost
    communication with the server" shape that took the unit lane down
    three runs in a row — and the merge gate waits out the whole of it
    for a job that never reports.

    The cap turns a wedge into a red job that names itself, which is
    the difference between a runner problem and a mystery.
    """
    job = jobs.get(NEGCTL_JOB)
    assert job is not None, (
        f"ci.yml has no `{NEGCTL_JOB}` job, so the negative controls for "
        f"P0-1 and P0-3 never run in CI"
    )
    cap = job.get("timeout-minutes")
    assert isinstance(cap, int) and cap > 0, (
        f"the `{NEGCTL_JOB}` job has timeout-minutes={cap!r}. The controls "
        f"run two pytest suites of their own, so this is a test lane, and "
        f"every test lane in this workflow is capped: an uncapped job runs "
        f"until GitHub cancels it at ~45 minutes with no log uploaded and "
        f"no job that names itself."
    )
    assert cap <= MAX_TEST_LANE_TIMEOUT_MINUTES, (
        f"the `{NEGCTL_JOB}` job is allowed {cap} minutes, more than the "
        f"{MAX_TEST_LANE_TIMEOUT_MINUTES} minutes any test lane in this "
        f"workflow gets. A job that outlives the merge gate it feeds is a "
        f"job nobody is waiting for."
    )


def test_negctl_job_uses_only_whitelisted_fields(
    jobs: dict, workflow_text: str
) -> None:
    """The job uses only fields ci.yml's whitelist allows.

    The whitelist is the workflow's own statement of the surface it is
    allowed to have. A field outside it is a field nobody checked
    against GitHub Actions' schema, and the reason the list is short is
    so that the set of things this workflow can do stays reviewable.

    The whitelist is read out of ci.yml's header comment rather than
    restated, because the comment declares itself the single source of
    truth. Reading it is also what catches the other failure: the
    comment naming a test file that does not exist means the constants
    and the comment could drift apart with nothing to notice.
    """
    declared = _declared_whitelist(workflow_text)
    assert declared, (
        "ci.yml's header comment no longer declares a field whitelist, so "
        "this gate has nothing to check the job against"
    )
    for label, allowed in (
        ("top level", ALLOWED_TOP_LEVEL_FIELDS),
        ("job level", ALLOWED_JOB_LEVEL_FIELDS),
        ("step level", ALLOWED_STEP_LEVEL_FIELDS),
    ):
        assert label in declared, (
            f"ci.yml's header comment does not declare a {label!r} field "
            f"list, so this test cannot check the `{NEGCTL_JOB}` job "
            f"against it"
        )
        assert set(declared[label]) == set(allowed), (
            f"the {label} fields this test allows ({sorted(allowed)}) are "
            f"not the ones ci.yml's header comment declares "
            f"({sorted(declared[label])}). The comment is the single source "
            f"of truth, so the two have to agree: change the comment and "
            f"the constant together."
        )

    job = jobs.get(NEGCTL_JOB)
    assert job is not None, (
        f"ci.yml has no `{NEGCTL_JOB}` job, so the negative controls for "
        f"P0-1 and P0-3 never run in CI"
    )
    extra = set(job) - ALLOWED_JOB_LEVEL_FIELDS
    assert not extra, (
        f"the `{NEGCTL_JOB}` job uses {sorted(extra)}, which ci.yml's field "
        f"whitelist does not allow. A field outside the whitelist is a "
        f"field nobody has checked against GitHub Actions' schema."
    )
    step_extra = {
        key
        for step in (job.get("steps") or [])
        if isinstance(step, dict)
        for key in step
    } - ALLOWED_STEP_LEVEL_FIELDS
    assert not step_extra, (
        f"a step of the `{NEGCTL_JOB}` job uses {sorted(step_extra)}, which "
        f"ci.yml's field whitelist does not allow."
    )


def test_negctl_job_adds_no_third_party_action(jobs: dict) -> None:
    """The job reaches for no action the workflow does not already use.

    An action is code that runs inside this repository's CI with this
    repository's credentials; adding one is a supply-chain decision, not
    a formatting choice, and ci.yml goes out of its way to say so —
    "this repository's gates are written against exactly one third-party
    action today".

    The controls need none: they read a file out of git, run pytest, and
    print. The job's only actions are the checkout it needs, the Python
    it runs on, and the cache that keeps the install cheap — all three
    already in the workflow.
    """
    job = jobs.get(NEGCTL_JOB)
    assert job is not None, (
        f"ci.yml has no `{NEGCTL_JOB}` job, so the negative controls for "
        f"P0-1 and P0-3 never run in CI"
    )
    used_elsewhere = {
        step.get("uses")
        for name, other in jobs.items()
        if name != NEGCTL_JOB
        for step in (other.get("steps") or [])
        if isinstance(step, dict) and step.get("uses")
    }
    new = {
        step["uses"]
        for step in (job.get("steps") or [])
        if isinstance(step, dict) and step.get("uses")
    } - used_elsewhere
    assert not new, (
        f"the `{NEGCTL_JOB}` job uses {sorted(new)}, which no other job in "
        f"ci.yml uses. An action runs inside this repository's CI with this "
        f"repository's credentials, so introducing one is a supply-chain "
        f"decision — and this job does not need one."
    )


def test_negctl_steps_pin_their_baseline(jobs: dict) -> None:
    """Each control runs against a pinned baseline, not a moving one.

    Both scripts find a pre-fix baseline for themselves when their env var is
    unset, and CI pins one for them explicitly. Either way the baseline has to
    be a commit that holds the pre-fix source, not a *moving* ref: ``origin/main``
    names whatever main happens to be when the job runs, and a clone that
    fetched one branch does not have it at all. A control whose whole claim is
    "the bug was there" would then be measuring a baseline that moves under it
    — and the day the fix is merged, the control would read the *fixed* source
    and fail for a reason that has nothing to do with the claim it is making.

    So the job pins each control's baseline to one commit, and this test
    says so. A ref name in that env var is the rot it catches: the pin has
    to name one commit, forever.
    """
    job = jobs.get(NEGCTL_JOB)
    assert job is not None, (
        f"ci.yml has no `{NEGCTL_JOB}` job, so the negative controls for "
        f"P0-1 and P0-3 never run in CI"
    )

    for script in NEGCTL_SCRIPTS:
        step = _step_running(job, script)
        assert step is not None, (
            f"the `{NEGCTL_JOB}` job does not run {script}, so its baseline "
            f"cannot be pinned either"
        )
        var = _baseline_env_var(script)
        env = step.get("env") or {}
        assert var in env, (
            f"the step that runs {script} sets no {var}. The script then "
            f"falls back to `origin/main`, a ref that moves: the control "
            f"would read whatever main happens to be when the job runs, "
            f"instead of the one commit that holds the pre-fix source."
        )
        rev = str(env[var])
        assert re.fullmatch(r"[0-9a-fA-F]{40}", rev), (
            f"the step that runs {script} pins {var}={rev!r}, which is not a "
            f"full commit SHA. A ref name here is a moving target: the "
            f"control has to name the one commit that holds the pre-fix "
            f"source, or the claim it makes is not the claim it checks."
        )
