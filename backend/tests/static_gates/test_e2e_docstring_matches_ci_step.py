"""The E2E module's docstring quotes the CI step that runs it, and this gate
checks the quote against the workflow rather than against itself.

Why this gate exists
--------------------
The whole-delivery E2E's docstring has a section headed "Where this file
runs", and it is the only thing in the repository that tells a reader how to
reproduce the macOS run: the job it is in, the step's name, the step's exact
command line, and the ``env:`` line that puts the file in its strict
position. Everything else about the lane is readable from ``ci.yml`` — the
quote is the part a reader is meant to believe.

Which makes it a claim, and a claim in a docstring is the kind that rots
quietly. The moment somebody edits the step — renames it, adds an argument,
changes the value the switch is set to — the quote stops describing the
workflow and starts describing a workflow that used to exist. Nothing turns
red. The file is green, the job is green, and the docstring is confidently
wrong, which is the one failure shape a reader cannot detect: a step that is
named but no longer wired looks exactly like a step that is named and wired,
right up until somebody tries the command it prints.

So the quote is checked here. The check is not "the docstring contains a
string I typed" — that is a third copy, and a copy inside the check goes
stale exactly the way the copy inside the docstring does, only later and
still green. Both sides are read out of the repository: the workflow is
parsed, the switch's name and its accepted values are read out of the E2E
module's own AST, and the expected quote is assembled from those readings.
The two things being compared are the two files a reader is looking at.

What is compared
----------------
The E2E step is found by the path its run line hands pytest, resolved
against the step's ``working-directory`` and compared against the file this
gate is paired with. A path spelled relative to ``backend/`` and a step run
from the repository root are different commands that happen to contain the
same file name, and only one of them collects the file.

Four lines of the step are then required to appear in the docstring, each
assembled from the parsed step: ``- name:``, ``working-directory:``, the
``run:`` line, and the ``env:`` line carrying the strict switch. The switch
is checked twice, and the two checks are not the same one: the value the
workflow sets has to be one the E2E module actually reads as "on" — read
out of ``_STRICT_SWITCH_ON_VALUES``, the module's own definition of strict —
and the docstring has to quote the same value the workflow sets. The first
is a wiring check; the second is the drift check, and either can go red on
its own.

Why the workflow is parsed rather than scanned
----------------------------------------------
The step carries roughly fifty-five lines of explanatory comment directly
above it, and the comment names the file, the switch, the job and both
gates that already pin this step. A line-oriented scan for "the line that
mentions the E2E" therefore finds the comment first, always, and every
value it reports comes from prose that describes the step rather than from
the step. That is not a hazard the scan has to be careful about; it is what
a scan does here.

``yaml.safe_load`` is the alternative, and it is the right one: a comment is
not data, so the parser hands back the step and the fifty-five lines are
gone. The one place in this file that reads lines is the negative control
below, which rewrites the switch inside the step it has already located —
anchored on the step's parsed name and left alone from there, so it edits
one line of the real file and none of the comment.

The negative control
--------------------
A check that compares two files can be a constant. ``_drift`` returns the
same empty list for a workflow that agrees with the docstring and for one
that does not, and only the second test can tell those apart. So the
control takes the committed workflow, changes the step's switch value, and
requires the result to carry a mismatch *about that switch* — not merely
any mismatch, since a control that fires for the wrong reason passes for
the wrong reason. Its premise is asserted first: the same function has to
report nothing for the file before it is edited, or the red afterwards is
indistinguishable from a red that was already there.

Shared, not copied
------------------
The E2E module's path, its constants and its parse guard are imported from
``test_keychain_macos_job_is_a_merge_gate.py`` rather than restated. The
module is the largest file in the suite and has been found more than once
standing on report prose where its source belongs, so the guard that names
that condition has to be the one every reader of the file goes through —
a second copy of it is a second place to forget to update, and the reader
who hits the uncopied one gets the stdlib's ``SyntaxError`` instead of the
diagnosis.

Scope, stated honestly
----------------------
This compares two files' text. It cannot prove that a macOS runner was
allocated, that a keychain was present on it, or that the six real-tool
cases ran there. A green run here means the docstring's reproduction recipe
is the workflow's actual step — which is what makes it safe to trust, and
is not the same as the recipe having worked.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import NamedTuple, Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: The path token the lane's run line hands pytest, the constants that define
#: the strict switch, and the parse guard that turns an unreadable E2E module
#: into a named failure. Imported rather than restated for the reason the
#: section above gives: the switch is spelled in exactly one place, and a
#: second spelling here is a switch nobody sets, reported as one somebody did.
from test_keychain_macos_job_is_a_merge_gate import (  # noqa: E402
    _MACOS_DELIVERY_E2E,
    _MACOS_DELIVERY_E2E_PATH,
    _REPO_ROOT,
    _e2e_module_constant,
    _parse_e2e_module,
)

#: The drift categories, as codes rather than free text so a caller can ask
#: about one of them specifically. The negative control does exactly that:
#: it requires ``SWITCH`` and would be satisfied by nothing else.
NO_STEP = "no-step"
WORKING_DIRECTORY = "working-directory"
SWITCH = "switch"
QUOTE = "quote"
JOB = "job"


class _Drift(NamedTuple):
    """One way the workflow and the docstring have come apart."""

    code: str
    detail: str


#: The workflow, resolved from the module's own path rather than written out
#: — a checkout sits at a different place on every machine, and this is the
#: only file the check reads. The sibling gate resolves the same file the
#: same way; the path is spelled twice because these are two independent
#: readers of it, not because either copy can go stale without the other
#: noticing — they resolve to the same tree or the gate fails to read.
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _workflow_text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _docstring() -> str:
    """The E2E module's own docstring, cleaned the way Python cleans it.

    Through :func:`_parse_e2e_module` rather than a bare ``ast.parse``, so a
    module that has been clobbered arrives here as the named failure with a
    recovery command attached — which is the one time a reader of this gate
    is going to need it.
    """
    docstring = ast.get_docstring(_parse_e2e_module(_MACOS_DELIVERY_E2E_PATH))
    assert docstring, (
        f"{_MACOS_DELIVERY_E2E_PATH.name} has no module docstring, so there is "
        f"no quoted step for this gate to check. The section that tells a "
        f"reader how to reproduce the macOS run is the one thing this file "
        f"exists to keep true, and a module without it has nothing to check."
    )
    return docstring


def _invoked_path(run: str) -> Optional[str]:
    """The path token in *run* that names the delivery E2E, or ``None``.

    Matched on the file name rather than on a full path, because the two
    spellings in the repository differ: the repository-root path a reader
    would type from a checkout, and the path the lane hands pytest from
    ``working-directory: backend``. Which one a run line carries is a
    property of the step, resolved below rather than assumed here.
    """
    for token in run.split():
        if token.endswith(_MACOS_DELIVERY_E2E):
            return token
    return None


def _quoted_lines(step: dict, switch: str, value) -> list:
    """The docstring lines this gate requires for *step*, as sets of spellings.

    Assembled from the parsed step and from the switch's own name, so the
    expected quote is a reading of the workflow rather than a second copy of
    it. Each entry is a set of *alternative* spellings and one of them has
    to be present: the ``env:`` line may be written with the value quoted
    or bare, because the quoting is a convention of the docstring's prose
    and not a claim it is making, while the value itself is the claim. The
    other three lines have exactly one spelling, and each is a group of one
    — kept in the same shape so "which of these is absent" is one question
    rather than two.
    """
    return [
        frozenset({f"- name: {step.get('name')}"}),
        frozenset({f"working-directory: {step.get('working-directory')}"}),
        frozenset({f"run: {step.get('run')}"}),
        frozenset({f"{switch}: \"{value}\"", f"{switch}: {value}"}),
    ]


def _candidates(workflow: dict) -> list:
    """``(job name, step)`` for every step whose ``run:`` names the E2E."""
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return []
    return [
        (job_name, step)
        for job_name, job in jobs.items()
        for step in (job.get("steps") or [] if isinstance(job, dict) else [])
        if isinstance(step, dict)
        and isinstance(step.get("run"), str)
        and _invoked_path(step["run"]) is not None
    ]


def _drift(workflow_text: str, docstring: str) -> list:
    """Every way *workflow_text* and *docstring* disagree. Empty means they agree.

    Total: a workflow that cannot be parsed, declares no jobs, or names the
    file nowhere produces findings rather than raising. A gate that crashes
    on the input it is judging is a gate that reports the crash instead of
    the defect, and the crash names no file.
    """
    try:
        workflow = yaml.safe_load(workflow_text)
    except yaml.YAMLError as exc:
        return [_Drift(NO_STEP, f"ci.yml does not parse as YAML: {exc}")]
    if not isinstance(workflow, dict):
        return [
            _Drift(NO_STEP, f"ci.yml parsed as {type(workflow).__name__}, not a mapping")
        ]

    candidates = _candidates(workflow)
    if not candidates:
        return [
            _Drift(
                NO_STEP,
                f"no step in ci.yml runs {_MACOS_DELIVERY_E2E!r} by path, so "
                f"there is no step for the docstring to be quoting. The "
                f"reproduction recipe it prints would collect nothing.",
            )
        ]

    switch = _e2e_module_constant("_STRICT_SWITCH_ENV")
    strict_values = _e2e_module_constant("_STRICT_SWITCH_ON_VALUES")
    found = []
    quoted = {line.strip() for line in docstring.splitlines()}

    for job_name, step in candidates:
        where = f"{job_name} :: {step.get('name')!r}"
        if not _job_named(docstring, job_name):
            found.append(
                _Drift(
                    JOB,
                    f"the docstring of {_MACOS_DELIVERY_E2E_PATH.name} does not "
                    f"name {job_name!r}, the job ci.yml runs this file from, so "
                    f"the recipe it prints does not say where to look. It "
                    f"quotes the step's name, its working directory, its run "
                    f"line and its switch — all four of which travel "
                    f"unchanged if the step is moved to another job, which is "
                    f"what makes a renamed job the one edit that leaves the "
                    f"rest of the quote true and the recipe unfindable.",
                )
            )

        token = _invoked_path(step["run"])
        workdir = step.get("working-directory")
        resolved = _REPO_ROOT / workdir / token if isinstance(workdir, str) else None
        if resolved != _MACOS_DELIVERY_E2E_PATH:
            found.append(
                _Drift(
                    WORKING_DIRECTORY,
                    f"the step at {where} collects {_MACOS_DELIVERY_E2E!r} as "
                    f"{token!r} with working-directory {workdir!r}, which "
                    f"resolves to {resolved} rather than "
                    f"{_MACOS_DELIVERY_E2E_PATH}. The same file name under a "
                    f"different working directory is a different command, and "
                    f"pytest would collect something else or nothing.",
                )
            )

        env = step.get("env")
        value = env.get(switch) if isinstance(env, dict) else None
        if value is None:
            found.append(
                _Drift(
                    SWITCH,
                    f"the step at {where} sets no {switch}, so the file runs "
                    f"in its default position there, where a macOS runner "
                    f"that cannot start the keychain tool skips the six real "
                    f"cases and the step still goes green.",
                )
            )
        elif str(value).strip() not in strict_values:
            found.append(
                _Drift(
                    SWITCH,
                    f"the step at {where} sets {switch}={value!r}, which is "
                    f"not one of {sorted(strict_values)} — the spellings "
                    f"{_MACOS_DELIVERY_E2E_PATH.name} itself reads as strict. "
                    f"The lane is in its default position, so a missing "
                    f"capability is a skip rather than a red merge gate.",
                )
            )

        missing = [
            sorted(forms)[0]
            for forms in _quoted_lines(step, switch, value)
            if not (forms & quoted)
        ]
        if missing:
            found.append(
                _Drift(
                    QUOTE,
                    f"the docstring of {_MACOS_DELIVERY_E2E_PATH.name} does not "
                    f"quote the step at {where} as ci.yml spells it. Missing: "
                    + "; ".join(repr(line) for line in missing),
                )
            )
    return found


def _step_block(lines: list, name: str) -> tuple:
    """``(first line index, one past the last line)`` of the step named *name*.

    Line-anchored on the parsed step name rather than on a search for the
    switch, because the step's own name is the one line in the file that
    belongs to the step and to nothing above it. The block runs to the first
    following line indented no further than the ``- name:`` line, which is
    what separates this step's keys from the next step's — the fifty-five
    comment lines above the anchor are never entered, because the walk
    starts below it.
    """
    wanted = f"- name: {name}"
    anchors = [i for i, line in enumerate(lines) if line.strip() == wanted]
    assert len(anchors) == 1, (
        f"expected exactly one {wanted!r} line in ci.yml, found {len(anchors)}. "
        f"The negative control rewrites the switch inside that step and "
        f"cannot pick which one to edit, so the sample it builds would not "
        f"differ from the real workflow in the one edit under test."
    )
    start = anchors[0]
    anchor_indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(lines[index]) - len(lines[index].lstrip()) <= anchor_indent:
            end = index
            break
    return start, end


def _with_switch_value(workflow_text: str, step: dict, switch: str, value: str) -> str:
    """*workflow_text* with that step's *switch* set to *value*, byte for byte elsewhere.

    Rewritten rather than re-serialized. ``yaml.safe_dump`` would hand back a
    different file — no comments, no key order, its own quoting — and a
    sample that differs from the real workflow in more than the edit under
    test cannot attribute a red to that edit.
    """
    lines = workflow_text.splitlines(keepends=True)
    _start, end = _step_block(lines, step["name"])
    prefix = f"{switch}:"
    hits = [
        index
        for index in range(_start, end)
        if lines[index].strip().startswith(prefix)
    ]
    assert len(hits) == 1, (
        f"expected exactly one {prefix} line inside the step at "
        f"{step.get('name')!r}, found {len(hits)}. The negative control below "
        f"edits that line, and a sample whose edit landed somewhere else "
        f"would pass for a reason unrelated to the switch."
    )
    index = hits[0]
    original = lines[index]
    indent = original[: len(original) - len(original.lstrip())]
    # The line terminator is taken from the original line rather than
    # re-added, so a file with no final newline and a file with one both
    # survive the edit as themselves. Dropping it would splice this key onto
    # the next line and hand the parser a workflow that does not parse — a
    # red, and one that says the sample is broken rather than that the
    # switch moved.
    ending = original[len(original.rstrip()) :]
    lines[index] = f'{indent}{switch}: "{value}"{ending}'
    return "".join(lines)


def _real_step() -> dict:
    """The step in the committed workflow that runs the E2E, for the control to edit."""
    candidates = _candidates(yaml.safe_load(_workflow_text()))
    assert len(candidates) == 1, (
        f"expected exactly one step running {_MACOS_DELIVERY_E2E!r} in "
        f"ci.yml, found {len(candidates)}. The negative control below needs "
        f"one unambiguous step to edit; with more than one, a red there "
        f"would not say which step the switch moved off."
    )
    return candidates[0][1]


def _real_job_name(workflow_text: str) -> str:
    """The job in *workflow_text* that runs the E2E, for the control to rename.

    Takes the text rather than reading the committed file, so the control
    can ask the same question of its edited sample and confirm the rename
    landed — the control is only measuring something if the sample it
    built resolves to a different job than the sample it started from.
    """
    candidates = _candidates(yaml.safe_load(workflow_text))
    assert len(candidates) == 1, (
        f"expected exactly one step running {_MACOS_DELIVERY_E2E!r}, found "
        f"{len(candidates)}. The job rename below needs one unambiguous job "
        f"to edit, and a sample that resolves to a different one would not be "
        f"the edit under test."
    )
    return candidates[0][0]


def _switch_name() -> str:
    return _e2e_module_constant("_STRICT_SWITCH_ENV")


# ---------------------------------------------------------------------------
# The two files, and the four lines of the step that have to be in both
# ---------------------------------------------------------------------------


def test_the_workflow_runs_the_e2e_from_the_backend_directory() -> None:
    """A file name without its working directory is not a step that runs it.

    The lane hands pytest a path relative to ``backend/`` and selects the
    file by that path rather than by a marker, so the directory is part of
    the command. A step moved to the repository root without the path
    adjusted collects a different file, or none — and the docstring's
    reproduction recipe, which prints the run line on its own, stops being
    the command the lane runs.
    """
    found = _drift(_workflow_text(), _docstring())
    offending = [d for d in found if d.code in (NO_STEP, WORKING_DIRECTORY)]
    assert not offending, "\n".join(f"[{d.code}] {d.detail}" for d in offending)


def test_the_e2e_step_sets_the_switch_to_a_value_the_module_reads_as_strict() -> None:
    """The switch has to be set, and to a spelling the E2E module accepts.

    Both halves are read out of that module: the variable's name from
    ``_STRICT_SWITCH_ENV`` and the spellings that mean "on" from
    ``_STRICT_SWITCH_ON_VALUES``. So the workflow and the module cannot drift
    apart silently — a rename of the variable, or a job that sets a value the
    file never reads, turns this red rather than leaving a lane that looks
    strict while running in its default position, where a macOS runner that
    cannot start the keychain tool skips the six real cases and the step is
    still green.
    """
    switch = _switch_name()
    assert _MACOS_DELIVERY_E2E_PATH.is_file(), (
        f"{_MACOS_DELIVERY_E2E_PATH} does not exist, so the constants this "
        f"check reads {switch!r} and {sorted(_e2e_module_constant('_STRICT_SWITCH_ON_VALUES'))} "
        f"out of are not the ones the lane's file carries."
    )
    found = _drift(_workflow_text(), _docstring())
    offending = [d for d in found if d.code == SWITCH]
    assert not offending, "\n".join(f"[{d.code}] {d.detail}" for d in offending)


def _job_named(docstring: str, job_name: str) -> bool:
    """Whether *docstring* names *job_name* as a job, in the recipe's spelling.

    The recipe's first line is the job id followed by the runner in
    parentheses — ``e2e-keychain  (macos-latest)`` — and the runner is
    decoration: it is prose about where the job runs, not the claim being
    made, and it is the *job id* that has to survive a rename for a reader to
    find the step. So the line is split at the parenthesis and only the
    leading token is compared, which is what lets the runner wording move
    without this turning red while still catching a job that has been
    renamed.

    One function, called by both the check and its control. A control that
    re-implements the comparison it is meant to test proves nothing: it
    stays green when the comparison it copied is deleted, which is the same
    defect this file exists to catch, one level up.
    """
    for line in docstring.splitlines():
        if line.strip().split("(")[0].strip() == job_name:
            return True
    return False


def test_the_docstring_names_the_job_the_step_actually_lives_in() -> None:
    """The recipe's first line is a job name, so it is a claim that can rot too.

    The quoted block opens with the job the step is in, and a job is the
    outermost thing a reader has to look for: the step name is a label inside
    a job, and a reader who searches ``ci.yml`` for a step name that has
    since moved to a different job finds the same four keys either way. The
    name, the working directory, the run line and the switch are all
    properties of the step, so they survive a move between jobs unchanged —
    which is what makes the job line the one line of the recipe that a rename
    turns false while every other line still agrees with the workflow.

    The job is read out of the parsed workflow rather than written here: the
    check asks which job holds the step that runs this file, and requires the
    docstring to name that one. Renaming the job in ``ci.yml`` is an ordinary
    thing to do and leaves the rest of the recipe quoting a step that is still
    real, still strict and still collected from ``backend/`` — so without this
    the recipe sends a reader to a job id that no longer exists and every
    other line of the check stays green.

    Read through ``_drift`` and the ``JOB`` code rather than against
    :func:`_job_named` directly, for the reason the module docstring gives:
    a check that calls the predicate it was supposed to be built from
    bypasses every guarantee ``_drift`` provides, and the control below
    exercises ``_drift`` rather than the predicate, so a check written
    against the predicate directly would not be covered by it.
    """
    found = _drift(_workflow_text(), _docstring())
    offending = [d for d in found if d.code == JOB]
    assert not offending, "\n".join(f"[{d.code}] {d.detail}" for d in offending)


def test_the_docstring_quotes_the_step_the_workflow_declares() -> None:
    """The whole point: the recipe a reader is told to trust is the real step.

    No findings at all, across every category, because a docstring that
    quotes a name, a directory, a command line and a switch value none of
    which is the workflow's is exactly the state this file was written for.
    The expected lines are assembled from the parsed step, so this goes red
    on the first edit to any of the four — which is the point. A check
    against a string typed into this file would keep reporting that the
    quote matches the string, long after both had stopped matching ci.yml.
    """
    found = _drift(_workflow_text(), _docstring())
    assert not found, "\n".join(f"[{d.code}] {d.detail}" for d in found)


# ---------------------------------------------------------------------------
# ...and a check that cannot detect drift is decoration
# ---------------------------------------------------------------------------


def test_the_gate_goes_red_when_the_switch_value_changes() -> None:
    """One edit, and the same function has to notice it.

    The check above compares two files, and a comparison can be a constant:
    a function that returned an empty list for every input would satisfy all
    three of them. So the committed workflow is taken, the step's switch is
    changed to a value the E2E module does not read as strict, and the
    result is required to carry a finding *about that switch* — which no
    other edit in this file could produce, so a red for a different reason
    does not count as a pass here.

    The premise comes first, because a control with no green to turn red is
    not a control. And the edit is made in place rather than by
    re-serializing, so the sample differs from the real workflow in the one
    line under test and in nothing else — the fifty-five comment lines above
    the step included, which is the text a looser scan would have been
    reading instead of the step.
    """
    workflow_text = _workflow_text()
    docstring = _docstring()
    switch = _switch_name()

    # (a) the premise: the committed pair agrees, so a finding afterwards is
    # attributable to the edit and not to a red that was already there
    assert not _drift(workflow_text, docstring), (
        "the committed workflow and the E2E docstring already disagree, so a "
        "finding from the edited sample below would not be attributable to "
        "the edit. The checks above say which half has moved; fix that "
        "first, since this control has nothing to attribute a red to."
    )

    # (b) and the sample really does change what it claims to change
    step = _real_step()
    loose = _with_switch_value(workflow_text, step, switch, "0")
    assert loose != workflow_text, (
        "rewriting the switch produced text identical to the workflow it was "
        "read from, so the control below would be measuring nothing."
    )

    # (c) the drift is reported, and reported about the switch
    found = _drift(loose, docstring)
    assert found, (
        f"changing the step's {switch} to '0' left the gate with no finding. "
        f"_drift is returning the same answer for a workflow that runs the "
        f"file strictly and one that does not, so it is not comparing the "
        f"two files at all — and the docstring's quoted switch value still "
        f"matches a step this repository no longer has in that position."
    )
    switch_findings = [d for d in found if d.code == SWITCH]
    assert switch_findings, (
        f"the edited sample was rejected, but not about the switch: "
        f"{[d.code for d in found]}. A red for any other reason would leave "
        f"the switch check untested, which is the half this control exists "
        f"to exercise."
    )
    assert switch in switch_findings[0].detail, (
        f"the switch finding does not name the switch it is about: "
        f"{switch_findings[0].detail!r}. A reader handed this has to be told "
        f"which line of the step moved, or the finding is one more thing to "
        f"go and check by hand."
    )


def test_the_job_check_notices_a_job_that_has_been_renamed() -> None:
    """The job is checked, so the check has to be shown to notice a rename.

    The same reasoning as the control above, for the line the recipe opens
    with. The job is renamed in the workflow and nothing else is touched —
    the step keeps its name, its working directory, its run line and its
    strict switch, so every other assertion in this file is still satisfied
    by the edited sample. That is what makes the job the one edit with no
    other check behind it: if this one were a comparison against a name
    typed into the test, a rename in ``ci.yml`` would leave the recipe
    pointing at a job that no longer exists while the file reported the
    quote as true.

    The premise is asserted first for the reason given above: the committed
    pair has to agree, or a finding afterwards would not be attributable to
    the rename. The rename is made on the job's own key line, anchored
    against the parsed job name rather than by searching for the text, so
    the ``needs:`` list that names the same job elsewhere in the workflow is
    left alone and the sample differs from the real file in one line.

    The finding has to carry the ``JOB`` code specifically, for the reason
    the switch control above gives: a red for any other reason would leave
    the job check untested, and a rename in a sample that is also broken
    some other way would pass this control while the check under it did
    nothing.
    """
    workflow_text = _workflow_text()
    docstring = _docstring()
    job_name = _real_job_name(workflow_text)

    # (a) the premise
    assert not _drift(workflow_text, docstring), (
        f"the committed workflow and the E2E docstring already disagree, so a "
        f"finding from the renamed sample below would not be attributable to "
        f"the rename. The checks above say which half has moved; fix that "
        f"first, since this control has nothing to attribute a red to."
    )

    # (b) rename the job on its own key line, and only that line
    lines = workflow_text.splitlines(keepends=True)
    key = f"  {job_name}:"
    hits = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\n") == key
    ]
    assert len(hits) == 1, (
        f"expected exactly one {key!r} line in ci.yml, found {len(hits)}. "
        f"The rename below anchors on it, and a sample that changed the "
        f"wrong occurrence would not be the edit under test."
    )
    ending = lines[hits[0]][len(lines[hits[0]].rstrip()):]
    # The trailing colon stays: the job is a mapping key, and a line that
    # ends in the new name without it hands the parser a plain scalar where
    # a mapping is expected — which fails on the *next* line and reports a
    # scanner error that names neither the job nor the rename.
    lines[hits[0]] = f"  {job_name}-renamed-by-control:{ending}"
    renamed = "".join(lines)
    assert renamed != workflow_text, (
        "renaming the job produced text identical to the workflow it was read "
        "from, so the control below would be measuring nothing."
    )

    # (c) the rename took, and only the job check notices
    renamed_job = _real_job_name(renamed)
    assert renamed_job != job_name, (
        f"the edited sample still resolves its E2E step to the job "
        f"{renamed_job!r}, so the rename did not take and the control below "
        f"cannot be measuring anything."
    )
    found = _drift(renamed, docstring)
    job_findings = [d for d in found if d.code == JOB]
    assert job_findings, (
        f"renaming the job to {renamed_job!r} in ci.yml left the gate with no "
        f"{JOB!r} finding — it reported {sorted({d.code for d in found})}. "
        f"Every other line of the recipe survives a rename unchanged, so "
        f"nothing else in this file would notice: the docstring would point a "
        f"reader at a job that no longer exists while the check reported the "
        f"quote as true."
    )
    assert job_name in job_findings[0].detail, (
        f"the job finding does not name the job it is about: "
        f"{job_findings[0].detail!r}. A reader handed this has to be told "
        f"which job moved, or the finding is one more thing to go and check "
        f"by hand."
    )
