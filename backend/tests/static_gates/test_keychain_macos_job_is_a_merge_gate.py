"""The macOS keychain lane has to be a merge gate, not a report.

Why this gate exists
--------------------
A keychain is a macOS facility. Every other job in this workflow runs on
``ubuntu-latest``, where ``security find-generic-password`` does not exist
and the credential tests answer "there is no keychain here" — which is the
right answer, and says nothing about whether the real read works. So the
credential suite is green on Linux by construction, and a defect that only
a real keychain can produce is invisible to every check a pull request
actually runs.

The obvious repair is the wrong one: mark the tests ``skipif`` not-darwin
and be done. That leaves the security acceptance running only on developer
machines, which is the same state of affairs with an extra comment on it.
A gate nobody's CI runs is not a gate, so the repair is a job on a macOS
runner instead — and that job only means anything if it actually blocks.

What this pins
--------------
1. **The job exists, on a macOS runner.** A Linux job proves nothing about
   a macOS facility.
2. **It is registered as merge-gating.** ``_MERGE_GATING_JOBS`` in
   ``test_ci_lanes_match_marker_contracts.py`` is a hand-maintained list,
   and a hand-maintained list does not notice a new job. A job absent from
   it is a job nobody re-checked when the next layer moved, and the
   failure is silent: the lane keeps running, it just stops being anyone's
   responsibility.
3. **It runs on a pull request, and it is not ``continue-on-error``.**
   Those are the two halves of "blocks the merge". A main-only job
   reports a broken credential path *after* it is on main, where the fix is
   a revert; a ``continue-on-error`` job reports it and moves on, which is
   the same thing wearing a green badge.
4. **The FD-isolation file is registered as a subprocess driver.** That
   list is what tells the lane contract which files start their own
   interpreter, and an unregistered file is one the default lanes collect
   under a per-test ceiling a cold interpreter start cannot meet.
5. **No marker was declared for it.** ``pytest.ini`` runs
   ``--strict-markers`` and the lane contracts pin the ``-m`` expressions
   lane by lane, so a new marker is not a one-line addition — it is a
   second place to keep in sync with a third. The lane selects by path,
   which needs no marker.

The file this gate parses
------------------------
Two of the checks below read the delivery E2E's own constants out of its
AST instead of restating them. One definition, one place to update, and
no copy to fall out of sync — the right trade, and the reason the value
cannot drift away from the thing it names. The price is an input this
gate does not own: that module is the largest file in the suite, and it
has repeatedly been found standing on report prose where its own source
belongs. So the parse path is worth reading on its own.

**Recognizing it.** None of this needs any knowledge of this gate:

1. the module's first *statement* is not a docstring — under ``ast``,
   ``tree.body[0]`` is some other node, or the file does not parse;
2. its opening lines are report prose where the docstring belongs: a
   line carrying ``insertions,`` or ``deletions(-``, a ``Full diff:``
   line, or a line that begins ``FILE:``;
3. the line count sits far below the committed copy; and
4. ``git diff --stat HEAD`` for the module reads as a mass deletion.

**What this gate says when it meets one.** The bare form — a
standard-library ``SyntaxError: invalid syntax``, pointing at whichever
line the parser gave up on — names neither this gate nor the file it
was reading, and nothing in it separates "the syntax broke" from "this
file is not a test module". So it reads as a defect in the check that
raised it. That is the reading it invites, and it has already sent a
forensics pass after a defect in this repository's own logic that was
not in the tree. Hence :func:`_parse_e2e_module`, which turns it into a
failure that names the condition, reports what was observed, and chains
the original ``SyntaxError`` as its cause — the reading is this gate's,
and the chained error is the fact underneath it, so a reader who
disbelieves the first still has the second.

**The repair.** ``HEAD`` holds an intact copy of the module, so the file
is one command away from whole::

    git checkout HEAD -- backend/tests/e2e/test_keychain_full_delivery_macos.py

Restore it, then read the diff before anything else is committed.
Editing the module until the parse succeeds is how the evidence of what
happened gets destroyed: the working tree is the only place that copy of
the damage exists.

Scope, stated honestly: this reads the workflow and the two lists. It
cannot prove that a macOS runner was allocated, that the keychain on it
held anything, or that the credential tests were the ones that ran. A
green run here means the lane is wired as a gate, not that it passed.
"""

from __future__ import annotations

import ast
import configparser
import copy
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The two hand-maintained lists this gate checks membership of. Imported
# rather than restated: a copy here would be a second list to keep in sync
# with the first, and the whole point of the check is that there is one
# list.
from test_ci_lanes_match_marker_contracts import (  # noqa: E402
    _MERGE_GATING_JOBS,
    _SUBPROCESS_DRIVERS,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_PYTEST_INI = _REPO_ROOT / "backend" / "pytest.ini"

#: The key the macOS lane is declared under. Read from the two lists and
#: from the workflow by this one name, so a rename is a single edit that
#: fails loudly rather than three edits that fail quietly.
_KEYCHAIN_JOB = "e2e-keychain"

#: The FD-isolation suite task 4 put at
#: ``backend/tests/integration/test_credentials_fd_isolation.py``. The
#: lists spell their paths from the repository root, so this is the path
#: that has to appear in ``_SUBPROCESS_DRIVERS``.
_FD_ISOLATION = "backend/tests/integration/test_credentials_fd_isolation.py"

#: The whole-delivery E2E at
#: ``backend/tests/e2e/test_keychain_full_delivery_macos.py``. Named by
#: its bare file name rather than by a repository-root path, because the
#: lane runs pytest with ``working-directory: backend`` and spells its
#: arguments relative to there — the name is the one form both spellings
#: contain, and the check below asserts on the name rather than on a
#: path prefix that a legitimate edit could move.
_MACOS_DELIVERY_E2E = "test_keychain_full_delivery_macos.py"

#: The same file, as a path from the repository root. The gate asserts it
#: exists, so a scan that "found" the name in the workflow while the file
#: had been renamed or deleted cannot report success: the collection
#: contract and the file it collects are two facts, and a check on one is
#: not evidence about the other.
_MACOS_DELIVERY_E2E_PATH = (
    _REPO_ROOT / "backend" / "tests" / "e2e" / _MACOS_DELIVERY_E2E
)

#: The one command that puts the delivery E2E module back, quoted
#: verbatim in the clobber message so whoever reads it pastes instead of
#: retyping — and a retyped path is how a restore turns into a second,
#: different mistake. Written out in full rather than assembled from the
#: constants above, because the reader needs a command they can copy
#: without reading this file; the accompanying check is what keeps the
#: literal from outliving the path.
_E2E_RECOVERY_COMMAND = (
    "git checkout HEAD -- backend/tests/e2e/test_keychain_full_delivery_macos.py"
)

#: The runner label whose jobs are checked for the absence of the file.
#: One name, because a keychain is a macOS facility: ``ubuntu-latest`` is
#: the label every other job in this workflow runs on, so "no Linux job
#: collects it by path" is a statement about every Linux lane at once.
_LINUX_RUNNER = "ubuntu-latest"

#: Every marker ``backend/pytest.ini`` declares, as of the day the macOS
#: lane landed. Frozen deliberately: this is the whole content of the
#: check, because "no new entry" cannot be observed without a baseline.
#: A marker added later is not automatically wrong — but it must be
#: added here too, deliberately, next to the lane that owns it.
_BASELINE_MARKERS = frozenset(
    {
        "acceptance_1",
        "acceptance_2",
        "acceptance_3",
        "acceptance_4",
        "acceptance_6",
        "acceptance_vp017",
        "api_error_matrix",
        "asyncio",
        "bug_1",
        "bug_2",
        "bug_3",
        "bug_4",
        "bug_5",
        "e2e",
        "fallback_vendor_c",
        "files_to_modify",
        "integration",
        "perf",
        "real_model",
        "time_sensitive",
        "unit",
        "vendor_b_exhausted",
    }
)


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict:
    jobs = _workflow()["jobs"]
    assert _KEYCHAIN_JOB in jobs, (
        f"ci.yml declares no `{_KEYCHAIN_JOB}` job, so the credential "
        f"acceptance runs nowhere but on a developer machine. A keychain is "
        f"a macOS facility and every other job here runs on ubuntu-latest, "
        f"where the credential tests pass because there is no keychain to "
        f"fail — not because the real read works."
    )
    return jobs[_KEYCHAIN_JOB]


#: The set-constructing calls a module-level constant may be wrapped in.
#: ``ast.literal_eval`` reads ``{"1", "true"}`` and refuses
#: ``frozenset({"1", "true"})``, which is how the E2E file spells its
#: accepted values — so the wrapper is unwrapped and the same set is read
#: from inside it. Membership is all this gate asks of the value, and a
#: ``frozenset`` and the ``set`` it wraps answer it identically.
_SET_WRAPPERS = frozenset({"frozenset", "set"})


def _static_value(node: ast.AST, name: str):
    """The value *node* denotes, unwrapping a set-constructing call.

    Anything else is refused rather than guessed. An expression this
    cannot read is not a value it may report on, and returning ``None``
    for it would be indistinguishable from a constant that is legitimately
    absent — so the reader fails loudly and a human decides whether the
    gate can still say what it claims to pin.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id in _SET_WRAPPERS
            and len(node.args) == 1
            and not node.keywords
        ):
            node = node.args[0]
    try:
        return ast.literal_eval(node)
    except ValueError as exc:
        raise AssertionError(
            f"{_MACOS_DELIVERY_E2E_PATH.name} declares {name} as something this "
            f"gate cannot read statically ({exc}). It reads constants out of "
            f"the AST so it does not have to execute the file, and an "
            f"expression it cannot evaluate is one it must not pretend to "
            f"have checked — re-read the definition and decide whether the "
            f"gate still pins it."
        ) from exc


def _parse_e2e_module(path: Path) -> ast.Module:
    """The AST at *path*, or a failure that names what the file turned out to be.

    A bare :func:`ast.parse` reports a file that is not Python as
    ``SyntaxError: invalid syntax`` at whatever line the parser gave up on,
    and that reading has already cost a forensics pass on this repository:
    it sent someone looking for a defect in the gate's own logic when the
    defect was that the file the gate reads no longer contained a test
    module. It had been overwritten with a report — a heading, a list of
    changed files, a verdict — which is text *about* a run written over
    the run itself.

    Nothing in ``invalid syntax`` distinguishes "the syntax broke" from
    "this file is not code", and the two call for opposite responses: one
    wants a diff against HEAD, the other wants a restore. So the message
    names the condition, reports what was actually observed so the reader
    can confirm it without opening the file, and quotes the command that
    undoes it.

    The parse error is chained rather than restated. The text above is
    this gate's reading of the situation, and the ``SyntaxError`` is the
    fact underneath it — a reader who disbelieves the reading still gets
    the evidence.
    """
    source = path.read_text(encoding="utf-8")
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        lines = source.splitlines()
        head = lines[0].strip() if lines else ""
        # A clobbered file can open with a single very long line, and a
        # message that quotes all of it is a message nobody reads. The
        # marker keeps the truncation from reading as the whole line.
        shown = head if len(head) <= 120 else head[:117] + "..."
        raise AssertionError(
            f"{path} is not a parseable test module: ast.parse refused it at "
            f"line {exc.lineno}, column {exc.offset} ({exc.msg}). That is "
            f"what a test module overwritten with report prose looks like — "
            f"a summary, a list of files changed, a verdict about a run. "
            f"Report prose is text about the work; this path is where the "
            f"work is. Observed: {len(lines)} lines, first line {shown!r}. "
            f"Restore the module and run this again:\n\n"
            f"    {_E2E_RECOVERY_COMMAND}\n"
        ) from exc


def _e2e_module_constant(name: str):
    """The value of a module-level constant in the delivery E2E file.

    Read from the AST rather than by importing the module, and the reason
    is that an import runs it. That file probes ``/usr/bin/security`` and
    ``/bin/ps`` at module scope to decide its skip reason, so importing it
    from a static gate would start two subprocesses in a lane whose entire
    job is to read files — and would then be a gate whose result depended
    on whether those binaries started. :mod:`tests.app_source` is the same
    idea for the application: a gate that cannot observe a contract from
    the running code reads the source instead.

    Still one definition rather than a copy. The switch is named in exactly
    one place, and a gate that restated the spelling would go green for a
    variable the E2E file does not read — which is a switch nobody set
    reported as a switch that was set.

    The parse goes through :func:`_parse_e2e_module` so that every caller
    here — this function, and both sides of the strict-switch pair — meets
    an unreadable module as one named failure rather than as the stdlib's.
    """
    tree = _parse_e2e_module(_MACOS_DELIVERY_E2E_PATH)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            continue
        return _static_value(node.value, name)
    raise AssertionError(
        f"{_MACOS_DELIVERY_E2E_PATH.name} declares no module-level {name}. "
        f"The switch the CI step sets is the one this gate reads, so its "
        f"absence means the gate is checking a name no file answers to."
    )


def _run_blocks(job: dict) -> list[str]:
    """Every ``run:`` block in *job*, in declaration order.

    Asserts on an empty step list rather than returning it. A scan that
    matched nothing because it read nothing is indistinguishable from a
    scan that matched nothing because there was nothing to match, and the
    second reading is the one that lets a lane quietly stop running a
    file while every gate stays green — which is exactly the failure this
    gate was written for.
    """
    steps = job.get("steps") or []
    assert isinstance(steps, list) and steps, (
        f"the job under test declares no steps at all ({steps!r}). A gate "
        f"that reads an empty step list matches nothing and therefore "
        f"reports success; that is the vacuous-pass shape, not a pass."
    )
    return [
        step["run"]
        for step in steps
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]


def _e2e_step(job: dict) -> dict | None:
    """The step in *job* whose ``run:`` names the whole-delivery E2E, or ``None``.

    Returns the step, not just its ``run:``, because the strict position
    is a property of the step's ``env:`` — the run line and the switch are
    two halves of one contract, and a check that could only see one of them
    would be satisfied by a lane that sets the switch on the wrong step.

    ``None`` rather than a raise, so the predicate below stays total. The
    two mutation checks drive it against samples with the path removed, and
    a helper that raised on exactly that input would answer the wrong
    question: they ask whether the gate goes red, and an exception
    escaping the call is not the gate's own verdict. A test that could not
    distinguish the two would pass for the wrong reason.
    """
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        if isinstance(run, str) and _MACOS_DELIVERY_E2E in run:
            return step
    return None


def _switch_is_on(value: object) -> bool:
    """Whether *value* is one of the spellings the E2E file accepts as "on".

    Read from the E2E file rather than restated here, because that file is
    the definition of what the switch means and its accepted set is the
    whole of the rule. A gate that spelled its own list would accept (or
    reject) a value the file never reads, and would report the lane as
    strict while the lane runs in the default position.
    """
    if value is None:
        return False
    return str(value).strip() in _e2e_module_constant("_STRICT_SWITCH_ON_VALUES")


def _e2e_step_is_strict(job: dict) -> bool:
    """The one judgment the strict-wiring gate is built on.

    True iff the job runs the whole-delivery E2E *and* that same step sets
    the strict switch to a value the E2E file reads as "on". Both halves,
    in one function, on purpose: the two mutation checks below drive this
    function against samples with one half removed, so a check that
    inspected only one of them would have nothing to go red on.

    False for every way of failing, with no branch that raises — a gate
    that crashes on a malformed workflow is a gate that cannot report the
    defect, because the crash *is* the report and it names no file.
    """
    step = _e2e_step(job)
    if step is None:
        return False
    env = step.get("env")
    if not isinstance(env, dict):
        return False
    return _switch_is_on(env.get(_e2e_module_constant("_STRICT_SWITCH_ENV")))


def _declared_markers() -> frozenset[str]:
    """The marker names ``backend/pytest.ini`` registers.

    ``configparser`` rather than a regex: the block is an indented
    multi-line value whose entries carry long descriptions containing
    colons, backticks and quotes, and a hand-rolled line parser is how
    this repository's other gates end up matching their own prose.
    """
    parser = configparser.ConfigParser()
    parser.read(_PYTEST_INI, encoding="utf-8")
    raw = parser["pytest"]["markers"]
    return frozenset(
        line.split(":", 1)[0].strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


# ---------------------------------------------------------------------------
# The lane has to be a macOS lane
# ---------------------------------------------------------------------------


def test_macos_keychain_job_runs_on_macos_latest() -> None:
    """The runner label is the whole point of the job.

    A keychain lookup is ``security find-generic-password``, a macOS
    binary. The same tests on ``ubuntu-latest`` exercise the module's
    "this platform has no keychain" branch and nothing else, so a job that
    says ``ubuntu-latest`` is a second copy of the Linux lane wearing a
    different name — and it costs ten times as much.
    """
    assert _job().get("runs-on") == "macos-latest", (
        f"`{_KEYCHAIN_JOB}` runs on {_job().get('runs-on')!r}. The credential "
        f"acceptance is about a macOS keychain; on any other platform the "
        f"tests pass because there is nothing to read, which is not evidence "
        f"that the read works."
    )


# ---------------------------------------------------------------------------
# ...and it has to be registered as one
# ---------------------------------------------------------------------------


def test_macos_keychain_job_is_in_merge_gating_jobs() -> None:
    """An unregistered job is a job nobody is responsible for.

    ``_MERGE_GATING_JOBS`` is hand-maintained, which is exactly why this
    check exists: a new job does not appear in it by being added, it
    appears by being registered, and the gap in between is silent. The
    lane keeps running, keeps going green, and simply stops being a thing
    anyone re-examines when a layer moves.
    """
    assert _KEYCHAIN_JOB in _MERGE_GATING_JOBS, (
        f"{_KEYCHAIN_JOB!r} is not in _MERGE_GATING_JOBS "
        f"({sorted(_MERGE_GATING_JOBS)}). That list is what the lane-"
        f"contract gate reads when it asks which layers must run on a pull "
        f"request; a job missing from it is outside the contract."
    )


def test_macos_job_triggers_on_pull_request() -> None:
    """A gate that only runs on main reports a break after the merge.

    That is a revert, not a fix, and the defect it reports is one this
    repository's own history is full of: a job that looks like a gate from
    the job list while its ``if:`` excludes every pull request.
    """
    condition = str(_job().get("if", ""))
    assert "pull_request" in condition, (
        f"`{_KEYCHAIN_JOB}` is gated on {condition!r}, which excludes "
        f"pull_request. A credential defect found on main is a revert; "
        f"found on the pull request it is a comment."
    )


def test_macos_job_is_not_continue_on_error() -> None:
    """``continue-on-error`` turns a gate back into a report.

    The job's conclusion becomes ``success`` whatever the keychain did, so
    branch protection is satisfied by a run in which the credential
    acceptance failed. This is the exact downgrade the task exists to
    prevent, and it is invisible from the job list — the check is still
    there, it is just not required any more.
    """
    assert _job().get("continue-on-error") is not True, (
        f"`{_KEYCHAIN_JOB}` declares `continue-on-error: true`. Its "
        f"conclusion is then `success` even when the keychain read fails, "
        f"which is the same state as not having the job at all — except "
        f"the run looks covered."
    )


# ---------------------------------------------------------------------------
# The file the lane runs is registered where the lane contracts read
# ---------------------------------------------------------------------------


def test_fd_isolation_file_is_registered_as_a_subprocess_driver() -> None:
    """A file that starts its own interpreter has to say so.

    ``_SUBPROCESS_DRIVERS`` is the list that says which files drive pytest
    as a child process, and the check built on it asserts they declare the
    ``integration`` marker. The FD-isolation suite builds a three-level
    relay of real child processes, so it belongs on that list; unregistered,
    the default lanes still collect it (their ``-m`` does not filter it
    out) and run it under a per-test ceiling a cold interpreter start
    cannot meet.
    """
    assert _FD_ISOLATION in _SUBPROCESS_DRIVERS, (
        f"{_FD_ISOLATION!r} is not in _SUBPROCESS_DRIVERS "
        f"({list(_SUBPROCESS_DRIVERS)}). It starts real child processes, so "
        f"the default lanes must be able to see that it does."
    )


def test_the_macos_lane_collects_the_whole_delivery_e2e() -> None:
    """The job has to actually run the file it exists to run.

    This lane is the only place in CI where a real keychain exists, and the
    whole-delivery E2E is the only file that exercises the full path through
    one: the real ``security`` read, the descriptor handoff to a real child
    interpreter, and two independent process-visibility probes answering
    "can anything else on this machine read the secret back". Written
    against a keychain it skips itself, and on the Linux lanes the
    credential acceptance is green by construction — so if the file is
    absent from this job it runs nowhere at all, and the acceptance has no
    evidence anywhere in the suite.

    That is not a hypothetical. The job was landed collecting three
    credential files, and the delivery E2E was added to the repository
    afterwards without being added here. Every one of its cases then
    reported green on every PR while never having been executed by anything.

    The check reads the job's ``run`` blocks rather than trusting a marker:
    the file declares none of its own, and the lane selects by path, so the
    path is the whole contract.
    """
    blocks = [
        step["run"]
        for step in _job().get("steps", [])
        if isinstance(step.get("run"), str)
    ]
    assert any(_MACOS_DELIVERY_E2E in block for block in blocks), (
        f"no step in `{_KEYCHAIN_JOB}` runs {_MACOS_DELIVERY_E2E!r}. This is "
        f"the only macOS lane in the workflow, and the file skips itself off "
        f"macOS — so unregistered here it is never collected by any lane, and "
        f"the keychain read it proves works has no evidence in CI."
    )


# ---------------------------------------------------------------------------
# ...and it has to run it in the strict position, not the default one
# ---------------------------------------------------------------------------


def test_the_e2e_step_sets_the_strict_switch() -> None:
    """Collecting the file by path is not the same as running it.

    The E2E file has one capability gate and two positions. By default a
    machine that cannot start the keychain tool skips the six real-tool
    cases: the step is green, the report says nothing ran, and nothing in
    a job can tell that from six passing cases. On a macOS runner that is
    the wrong default for a *merge gate* — ``security`` not starting there
    is a misconfigured lane, and a misconfigured lane must be red, not a
    green run that published no evidence.

    So this lane sets ``_STRICT_SWITCH_ENV`` on the step, and this check
    reads the step's own ``env:`` rather than trusting the run line. The
    switch on the wrong step — or set to a spelling the file does not read
    — is the same defect as not setting it, so both are read here through
    the one function :func:`_e2e_step_is_strict` defines.
    """
    assert _MACOS_DELIVERY_E2E_PATH.is_file(), (
        f"{_MACOS_DELIVERY_E2E_PATH} does not exist. The run line names it "
        f"and the step below would then collect nothing at all, which "
        f"pytest reports as an error rather than a pass — but a gate that "
        f"only read the workflow could not tell the two apart."
    )
    job = _job()
    switch = _e2e_module_constant("_STRICT_SWITCH_ENV")
    accepted = _e2e_module_constant("_STRICT_SWITCH_ON_VALUES")
    assert _e2e_step(job) is not None, (
        f"no step in `{_KEYCHAIN_JOB}` runs {_MACOS_DELIVERY_E2E!r} by path, so "
        f"there is no step for {switch} to be set on. The file is "
        f"collected nowhere in CI and this lane proves nothing; "
        f"`test_the_macos_lane_collects_the_whole_delivery_e2e` covers the "
        f"collection itself and this check cannot be about anything else until "
        f"that one passes."
    )
    assert _e2e_step_is_strict(job), (
        f"the step in `{_KEYCHAIN_JOB}` that runs {_MACOS_DELIVERY_E2E!r} does "
        f"not set {switch} to one of {sorted(accepted)} in its `env:`. Without "
        f"it the file runs in its default position, where a macOS runner that "
        f"cannot start the keychain tool skips the six real cases and the step "
        f"still goes green — a lane that proves nothing while looking like it "
        f"proved something. A tool that will not start on the one runner that "
        f"has a keychain is a lane misconfiguration, and it has to be red."
    )


def test_the_gate_goes_red_when_the_switch_is_removed() -> None:
    """A gate nobody checked is a gate that has not been shown to work.

    The check above asserts a property of the workflow as it is. This one
    takes the same workflow, deletes the ``env:`` block from the step that
    runs the E2E, and requires the *same* judgment to reject the result. A
    predicate that cannot go red on this sample is not a predicate — it is a
    constant, and a constant that happens to be ``True`` is a gate that
    reports success for every workflow including the ones it exists to
    reject.

    Built by deep-copying and mutating rather than by hand-writing a
    sample: the sample differs from the real tree in exactly the one edit
    under test and in nothing else, so a red here is attributable to that
    edit.
    """
    job = copy.deepcopy(_job())
    step = _e2e_step(job)
    assert step is not None, (
        "the real workflow has no step running the E2E by path, so this "
        "sample removes an `env:` block that was never there and the check "
        "below would pass for the wrong reason. "
        "`test_the_e2e_step_sets_the_strict_switch` covers that case."
    )
    assert _e2e_step_is_strict(job), (
        "the sample this test mutates does not set the strict switch to begin "
        "with, so a predicate that never looks at `env:` at all would pass. "
        "The mutation below has to be what turns the judgment red."
    )
    step.pop("env", None)
    assert not _e2e_step_is_strict(job), (
        "removing the `env:` block from the E2E step left the judgment "
        "unchanged, so the check cannot distinguish a lane that sets the "
        "strict switch from one that does not. That is the defect this "
        "test exists to rule out."
    )


def test_the_gate_goes_red_when_the_path_is_removed() -> None:
    """The other half of the same pair, in the other direction.

    :func:`_e2e_step_is_strict` judges the collection *and* the switch
    together, so dropping the run line has to turn it red as well. A
    predicate that only asked "is the switch set somewhere in this job"
    would stay green here — the env block would still be on the job, and
    the file would no longer be in the lane at all.
    """
    job = copy.deepcopy(_job())
    step = _e2e_step(job)
    assert step is not None, (
        "the real workflow has no step running the E2E by path, so there is "
        "no run line for this test to remove and it would pass vacuously. "
        "`test_the_e2e_step_sets_the_strict_switch` covers that case."
    )
    step["run"] = "./.venv/bin/python3 -m pytest --tb=short --timeout=300"
    assert not _e2e_step_is_strict(job), (
        "removing the E2E path from the run line left the judgment "
        "unchanged. `_e2e_step_is_strict` is supposed to require both "
        "halves — the file collected by path *and* the strict switch on "
        "that same step — and a job with the switch set but no file is "
        "the case that says so."
    )


def test_the_e2e_file_is_not_wired_into_a_linux_job() -> None:
    """The file skips itself off macOS, so a Linux lane collecting it is noise.

    Every ``ubuntu-latest`` job in this workflow answers "there is no
    keychain here" — correctly, and with nothing to say about whether the
    real read works. A Linux job that names this file by path therefore
    buys a step that can only ever skip, at the cost of a timeout budget
    and a green line in the log that reads as coverage.

    The cost is not only the wasted lane. It is that the *strict* position
    this job now demands would be demanded on a platform whose failure is
    expected, turning an honest skip into a permanent red — so the two
    facts are checked together, in this file, rather than one at a time by
    whoever notices.

    Scanned by name across every Linux job's ``run:`` blocks, and asserted
    non-empty: a scan that found no Linux jobs, or no run blocks, would
    otherwise report this for free.
    """
    jobs = _workflow()["jobs"]
    linux_jobs = {
        name: body for name, body in jobs.items() if body.get("runs-on") == _LINUX_RUNNER
    }
    assert linux_jobs, (
        f"no job in ci.yml runs on {_LINUX_RUNNER!r}, so the scan below found "
        f"nothing to check and would report success for free. Either the "
        f"workflow lost its Linux lanes or this gate is reading the wrong "
        f"runner label."
    )
    offenders = sorted(
        f"{name} :: {block.strip()}"
        for name, body in linux_jobs.items()
        for block in _run_blocks(body)
        if _MACOS_DELIVERY_E2E in block
    )
    assert not offenders, (
        f"{len(offenders)} {_LINUX_RUNNER} step(s) collect "
        f"{_MACOS_DELIVERY_E2E!r} by path, which can only ever skip there: "
        f"{offenders}. A keychain is a macOS facility, so on Linux the file "
        f"answers 'there is none' — and with the strict switch demanded that "
        f"is a red step rather than a green one. The file belongs to "
        f"`{_KEYCHAIN_JOB}` alone."
    )


def test_e2e_keychain_is_still_registered_as_a_merge_gate() -> None:
    """The registration this file exists to protect, restated as its own case.

    ``test_macos_keychain_job_is_in_merge_gating_jobs`` asks whether the
    job is in ``_MERGE_GATING_JOBS``. This asks the same question from the
    strict-wiring side: the lane that now demands six real cases must be a
    lane whose result somebody is waiting for. A step that fails loudly
    inside a job nobody is waiting for is still a job nobody is waiting
    for — the strict switch raises the cost of a misconfigured runner from
    "a green step that proved nothing" to "a red step in a lane outside
    the gate", which is a real difference and the wrong one.
    """
    assert _KEYCHAIN_JOB in _MERGE_GATING_JOBS, (
        f"{_KEYCHAIN_JOB!r} is not in _MERGE_GATING_JOBS "
        f"({sorted(_MERGE_GATING_JOBS)}). A lane that fails loudly and a lane "
        f"whose failure blocks a merge are different things: the first is a "
        f"red line in a log, the second is a merge that waits. The strict "
        f"switch makes this lane worth waiting for, which is exactly why the "
        f"registration matters here."
    )


# ---------------------------------------------------------------------------
# And the lane got there without inventing a marker
# ---------------------------------------------------------------------------


def test_no_new_marker_was_declared() -> None:
    """The macOS lane selects by path, so it needed no marker of its own.

    ``pytest.ini`` runs ``--strict-markers``, and the lane contract pins
    one ``-m`` expression per job. A new marker is therefore not a
    one-line addition: it is a name that the default lanes must be shown
    to exclude, that the serial lane must be shown to collect, and that a
    third gate has to be told about — for a lane that runs three files by
    path and could say so directly.

    So the marker list is frozen, and this is the check that the freeze
    still holds. An entry added on purpose is not a failure of the design;
    it is a change that has to be written down here next to the lane that
    owns it.
    """
    declared = _declared_markers()
    added = sorted(declared - _BASELINE_MARKERS)
    removed = sorted(_BASELINE_MARKERS - declared)
    assert not added and not removed, (
        f"backend/pytest.ini's marker list changed "
        f"(+{added} -{removed}). The macOS keychain lane selects by path, so "
        f"it declared no marker; `--strict-markers` and the per-lane `-m` "
        f"contracts mean a new marker has to be excluded by the default "
        f"lanes, collected by the serial one, and registered in "
        f"_BASELINE_MARKERS here. If the marker is real, add it to the "
        f"baseline deliberately and give it a lane."
    )


# ---------------------------------------------------------------------------
# ...and a module that cannot be parsed is reported as what it is
# ---------------------------------------------------------------------------

#: The source that used to be found in place of the E2E module: not a
#: test, a description of a test run. Prose is what a report is made of,
#: and a report written over the work is the failure this file has to be
#: able to *name*. The heading is not what gives it away — ``#`` starts a
#: Python comment, so the parser reads straight past it and refuses on the
#: first bullet, several lines down. A clobbered file does not announce
#: itself on line 1, which is a second reason the raw traceback said so
#: little.
_CLOBBERED_SOURCE = (
    "## Summary\n"
    "\n"
    "- files modified: backend/tests/static_gates/\n"
    "- test result: PASSED\n"
)


def test_an_unparseable_e2e_module_is_reported_as_a_clobber(
    tmp_path: Path,
) -> None:
    """The parse failure has to say what happened, not just that it failed.

    Read through a bare ``ast.parse``, this condition arrives as
    ``SyntaxError: invalid syntax`` at whichever line the parser gave up
    on, and that reading has already sent a forensics pass looking for a
    defect in the gate's own logic when the defect was that the file it
    reads no longer contains Python. Nothing about ``invalid syntax``
    distinguishes "someone broke the syntax" from "this file is a report
    about a test run", and the two want opposite responses: one wants a
    diff, the other wants a restore.

    So the message has to carry three things. What happened, in words.
    What was observed, so the reader can confirm it without opening the
    file. And the command that undoes it, verbatim — a restore typed from
    memory is how one mistake becomes two.
    """
    clobbered = tmp_path / _MACOS_DELIVERY_E2E
    clobbered.write_text(_CLOBBERED_SOURCE, encoding="utf-8")

    with pytest.raises(AssertionError) as caught:
        _parse_e2e_module(clobbered)
    message = str(caught.value)

    # (a) the corruption, named rather than left for the reader to guess
    assert "not a parseable test module" in message
    assert "report prose" in message

    # (b) the observed signature: where the parser actually pointed, and
    # what is in the file. The line is read back out of the parser rather
    # than written down here, because a clobbered file fails wherever its
    # first non-comment lands — `## Summary` reads as a Python comment, so
    # the refusal lands on the bullet below it, not on the heading.
    with pytest.raises(SyntaxError) as refused:
        ast.parse(_CLOBBERED_SOURCE)
    assert f"line {refused.value.lineno}" in message
    assert f"column {refused.value.offset}" in message
    assert f"{len(_CLOBBERED_SOURCE.splitlines())} lines" in message
    assert "## Summary" in message

    # (c) the recovery command, quotable without retyping it
    assert _E2E_RECOVERY_COMMAND in message

    # (d) the original error, chained rather than swallowed
    assert isinstance(caught.value.__cause__, SyntaxError)


#: A report's opening summary when the diff it is summarizing is enormous.
#: The sample above opens with ``## Summary`` — ten characters — so every
#: test built on it takes the untruncated path, and the truncation branch
#: in :func:`_parse_e2e_module` runs in no test at all. A branch no test
#: executes is a branch whose removal changes nothing, which is the state
#: in which the next tidy-up quietly drops it.
#:
#: Assembled from fragments at run time, for the reason the diff-summary
#: sample further down gives: a committed block of report prose would be
#: report prose in the repository, which is the one thing this file exists
#: to keep out of it. What matters about these three fragments is their
#: total length, and the test below asserts that rather than trusting it.
_LONG_HEAD_FRAGMENTS = (
    "1 file changed, 4 insertions(+), 3457",
    "deletions(-): the delivery E2E module no longer holds any of its cases",
    "and every gate below reads that file, so every one of them is blind",
)
_LONG_HEAD = " ".join(_LONG_HEAD_FRAGMENTS)

#: A second line, so the count the message reports is a number a reader
#: would write naturally instead of the ``1 lines`` singular/plural wart.
_LONG_HEAD_BODY = "no test cases remain in this file"


def test_a_long_first_line_is_truncated_in_the_clobber_message(
    tmp_path: Path,
) -> None:
    """A clobber that opens with one enormous line stays a readable message.

    The message quotes the file's first line so a reader can confirm what
    they are looking at without opening the file. That is the right
    instinct until the first line is a thousand characters long — and a
    report's diff summary is exactly that, because a summary is a single
    line by construction, and this condition produces very large diffs.

    A message that quotes all of it is not a more informative message. It
    is a paragraph wrapped around a wall of text, inside a traceback,
    above the recovery command the reader has to scroll to reach. So the
    first 117 characters are quoted and the cut is marked with an ellipsis:
    the reader sees the shape of the corruption, and sees that they are
    not seeing all of it.

    Two things have to survive the cut, and together they are why this is
    a truncation and not simply "print less". The line count is the
    observation that says how much of the file is left, and it is a
    property of the file rather than of its first line. And the
    ``SyntaxError`` underneath is the evidence, so it is still chained —
    a reader who disbelieves the summary has nothing left if the summary
    is the only thing that survived.
    """
    clobbered = tmp_path / _MACOS_DELIVERY_E2E
    clobbered.write_text(
        f"{_LONG_HEAD}\n{_LONG_HEAD_BODY}\n", encoding="utf-8"
    )

    # The premise, asserted rather than assumed. A sample line this short
    # would take the untruncated path, and every assertion below would
    # then hold for a reason that has nothing to do with truncation.
    assert len(_LONG_HEAD) > 120, (
        f"the sample first line is {len(_LONG_HEAD)} characters, which is "
        f"within the limit, so this test would pass against a gate that "
        f"never truncates at all"
    )

    with pytest.raises(AssertionError) as caught:
        _parse_e2e_module(clobbered)
    message = str(caught.value)

    # (a) the truncated form, and the ellipsis that marks the cut — a
    # clean stop mid-word would read as the whole line
    assert repr(_LONG_HEAD[:117] + "...") in message

    # (b) and emphatically not the whole line. Without this, (a) could be
    # satisfied by a message carrying both forms, which is precisely the
    # wall of text the branch exists to avoid.
    assert _LONG_HEAD not in message

    # (c) the count survives, and it is the true one
    assert "2 lines" in message

    # (d) the evidence underneath is untouched by any of the above
    assert isinstance(caught.value.__cause__, SyntaxError)


def test_the_recovery_command_names_the_module_this_gate_reads() -> None:
    """A restore command that names a stale path is worse than none.

    The command is a literal, which is what makes it pasteable, and a
    literal is also what makes it rot: rename the E2E file or move the
    directory and the message keeps offering a command that restores
    something else. Asserting the literal against the path this gate
    actually reads costs one line and turns that rot into a red test.
    """
    relative = _MACOS_DELIVERY_E2E_PATH.relative_to(_REPO_ROOT).as_posix()
    assert _E2E_RECOVERY_COMMAND == f"git checkout HEAD -- {relative}"


def test_reading_a_constant_from_a_clobbered_module_names_the_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The named failure has to reach the gates, not just the helper.

    The checks above exercise the parser directly. The condition itself
    arrives in this file through :func:`_e2e_module_constant`, which is
    what the two tests that read the strict switch go through — so a
    wrapper that only the direct caller saw would leave the real failure
    exactly as unreadable as it is now. Redirecting the path is how the
    two coupled gates reach that state without the file in the tree
    actually being clobbered to find out.
    """
    clobbered = tmp_path / _MACOS_DELIVERY_E2E
    clobbered.write_text(_CLOBBERED_SOURCE, encoding="utf-8")
    monkeypatch.setattr(
        sys.modules[__name__], "_MACOS_DELIVERY_E2E_PATH", clobbered
    )

    with pytest.raises(AssertionError) as caught:
        _e2e_module_constant("_STRICT_SWITCH_ENV")

    # `pytest.raises(AssertionError)` is itself the assertion that this is
    # no longer the stdlib's error: before the wrap, the bare `ast.parse`
    # raised SyntaxError straight past this line and the test failed on
    # the traceback instead. What is left to check is that the gate's own
    # failure keeps the parse error underneath it.
    assert _E2E_RECOVERY_COMMAND in str(caught.value)
    assert isinstance(caught.value.__cause__, SyntaxError)


# ---------------------------------------------------------------------------
# The same failure in the shape that carries its own evidence
# ---------------------------------------------------------------------------

#: The line a report copies verbatim out of its own tooling: the summary
#: ``git diff`` prints. This is a *different* corruption sample from the
#: one above, and the difference is the whole point of it: there the
#: heading opens with ``#``, which Python reads as a comment, so the
#: refusal lands several lines down and the parser's line number says
#: little. Here the marker is on line 1 — so the parser refuses on line
#: 1, before it has read anything else, and the line it quotes back is
#: the one that names the failure.
#:
#: Assembled from fragments at run time rather than pasted as a block. A
#: committed block of report prose would be report prose in the repository,
#: which is the one thing this file exists to keep out of it.
_STAT_SUMMARY = "1 file changed, 4 insertions(+), 3457 deletions(-)"
_REPORT_BODY = (
    "Full diff:",
    "- the delivery E2E module now holds this instead of its cases",
    "- every check below reads that file, so every one of them is blind",
)


def _marker_header_source() -> str:
    """Report prose whose *first* line is a diff summary and does not parse."""
    return "\n".join((_STAT_SUMMARY, *_REPORT_BODY))


def test_a_marker_header_surfaces_the_named_clobber(tmp_path: Path) -> None:
    """The negative control: the named failure, driven by the failure's own shape.

    The sample above proves the message names a clobber. It does not prove
    the message is right when the parser refuses *at line 1* — which is the
    line that says what happened, since a diff summary on the first line is
    a report's fingerprint and the reader is looking at it. So this drives
    the same path with that shape and requires the same named outcome: the
    corruption wording, and the recovery command verbatim.

    ``pytest.raises(Exception)`` rather than ``AssertionError`` on purpose.
    Before the wrap this path raised ``SyntaxError``, which is not an
    ``AssertionError`` — pytest would report the escape as an *error* on the
    traceback, which reads as "the helper blew up" rather than as "the gate
    failed to name its own failure". Catching the wide type and asserting
    on what came out turns that into the finding this test is about.
    """
    source = _marker_header_source()

    # The fixture has to be what it claims: report prose whose first line
    # carries the markers, and prose that does not parse. A sample that
    # parsed would make everything below a test of nothing.
    first_line = source.splitlines()[0]
    assert "deletions(-" in first_line, (
        f"the fixture's first line no longer carries a corruption marker, so "
        f"it is prose rather than the report-a-run-copies-verbatim that this "
        f"test is about. Got {first_line!r}."
    )
    assert "Full diff:" in source.splitlines()[1], (
        f"the fixture no longer reads as a copied diff block. Got "
        f"{source.splitlines()[1]!r}."
    )
    with pytest.raises(SyntaxError):
        ast.parse(source)

    clobbered = tmp_path / _MACOS_DELIVERY_E2E
    clobbered.write_text(source, encoding="utf-8")

    with pytest.raises(Exception) as caught:
        _parse_e2e_module(clobbered)

    surfaced = caught.value
    assert isinstance(surfaced, AssertionError) and not isinstance(
        surfaced, SyntaxError
    ), (
        f"_parse_e2e_module let a {type(surfaced).__name__} escape for a "
        f"module that is report prose. Every check in this file reads that "
        f"module, so the one time its message is read is the one time it "
        f"has to be right, and a SyntaxError says only where the parser "
        f"gave up — which names neither the overwrite nor the way out of "
        f"it. It has to raise this gate's own failure."
    )
    assert "Traceback" not in str(surfaced), (
        f"the surfaced text is a traceback rather than a report: "
        f"{str(surfaced)!r}. A bare parser complaint is the reading this "
        f"check exists to rule out."
    )

    message = str(surfaced)
    assert "report prose" in message, (
        f"the surfaced text does not say the module was overwritten with a "
        f"report. That is the condition, and a reader who cannot tell it "
        f"from an ordinary syntax error edits the file to make the gate go "
        f"quiet — which destroys the evidence the gate exists to preserve. "
        f"Message was: {message!r}"
    )
    assert (
        _E2E_RECOVERY_COMMAND
        == "git checkout HEAD -- backend/tests/e2e/test_keychain_full_delivery_macos.py"
    ), (
        f"the recovery command this message quotes is {_E2E_RECOVERY_COMMAND!r}. "
        f"The command a reader pastes has to be this one, or the failure is "
        f"reported and not repaired."
    )
    assert _E2E_RECOVERY_COMMAND in message, (
        f"the surfaced text carries no way out. Message was: {message!r}"
    )
    # The first line is quoted back, which is what lets a reader confirm
    # the diagnosis without opening a file they may be about to restore.
    assert _STAT_SUMMARY in message, (
        f"the surfaced text does not quote what the first line actually "
        f"contained, so the reader is asked to take the diagnosis on trust. "
        f"Message was: {message!r}"
    )


def test_the_clobber_guard_stays_quiet_on_the_module_under_guard() -> None:
    """The other half: a guard that fires on a healthy file is not a guard.

    Restoring from ``HEAD`` is the remedy this message names, and applying
    it to a module that is fine discards whatever the current lane did to
    it. A corruption check that cannot tell the two apart is therefore worse
    than no check — it is a check that spends real work on every run and
    answers "restore" every time. So the same path, over the module as
    committed, has to parse and hand back the switch this file exists to
    read: both halves, since either could go alone.
    """
    tree = _parse_e2e_module(_MACOS_DELIVERY_E2E_PATH)
    assert isinstance(tree, ast.Module), (
        f"_parse_e2e_module returned {type(tree).__name__} for a module "
        f"that is committed and parses. A guard that fires on the healthy "
        f"file tells the reader to restore over good work, every run."
    )
    assert _e2e_module_constant("_STRICT_SWITCH_ENV") == "PDT_REQUIRE_KEYCHAIN_E2E", (
        f"the switch this gate reads did not resolve to the spelling the CI "
        f"step sets. Either the clobber guard is swallowing a parse that "
        f"works, or the constant moved — and the check above cannot tell "
        f"those apart."
    )


# ---------------------------------------------------------------------------
# ...and the way out is written down here, where the traceback points
# ---------------------------------------------------------------------------


def test_the_docstring_quotes_the_recovery_command_this_gate_prints() -> None:
    """A remedy that lives in a run's output is gone when the run is.

    The next reader does not have the message. They have a traceback whose
    top frame is this file, and this file has to be the thing that tells
    them what happened and what to type. That is why the passage is in the
    module docstring rather than in the summary of whichever run first met
    the failure — and asserting the same literal the message quotes is
    what keeps the two from drifting into naming different files.
    """
    docstring = ast.get_docstring(ast.parse(Path(__file__).read_text(encoding="utf-8")))
    assert docstring, (
        "this module has no docstring, so the reader a failing traceback "
        "points at lands on an import block and has to reconstruct the "
        "failure from its own text."
    )
    assert _E2E_RECOVERY_COMMAND in docstring, (
        f"this module's docstring does not quote {_E2E_RECOVERY_COMMAND!r}. "
        f"The failure message carries it, but a message is a property of "
        f"one run; the docstring is what survives to the next reader."
    )
    assert "SyntaxError" in docstring, (
        "the docstring no longer names the failure the parse path exists to "
        "explain, so the section reads as commentary on the lane rather "
        "than as the diagnosis it is."
    )
