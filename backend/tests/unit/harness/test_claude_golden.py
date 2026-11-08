"""The claude adapter must reproduce the pre-refactor argv byte for byte.

What is pinned here
-------------------
The claude command line used to be assembled inline in
``coding_tool.ClaudeCodingTool._run_claude_interactive``. It now lives in
:class:`harness.claude.ClaudeHarness`, and this file is the gate that says
the move changed nothing.

The gate is a *comparison against the baseline*, not a restatement of the
adapter. The baseline
(``backend/tests/fixtures/harness/claude_argv_golden.json``) was captured
from the old inline construction before the argv moved, and it is the only
statement about "what the command line used to be" that does not come from
the code under suspicion. The expectation in every assertion below is read
out of that file.

The two sides of the comparison
-------------------------------
The *input* side is written down here: :data:`CAPTURED_INPUTS` says which
eight requests the capture was made with, and the values it used. The
*expectation* side is the baseline: the captured argv, the captured
environment and the captured working directory, byte for byte.

The two are kept apart on purpose. A request reconstructed from the
baseline's own argv would follow the baseline everywhere -- a baseline
that had quietly lost a flag would then be *reproduced* rather than
reported, and the gate would pass on a weakened baseline. Written down
here, the baseline is what the adapter's answer has to match, and the two
disagreeing is the signal.

The input table is a transcription of the capture generator's matrix
(``backend/tests/fixtures/harness/capture_claude_baseline.py``) rather than
an import of it: the generator drives the pre-refactor dispatch, and that
dispatch is what the refactor is removing -- a gate that imported it would
break exactly when the refactor completes, and would then be "fixed" by
pointing the generator at the new code, which is the one thing a baseline
must never be.

Why byte for byte
------------------
The flags are order-sensitive in a way the CLI is not. ``--model`` and
``--append-system-prompt`` in either order parse the same and run the same
model, so a comparison written as "the same set of flags", or as "the same
flags, sorted", would pass on a command line that had quietly changed
shape. The comparison here is a byte comparison: both sides are serialised
with ``json.dumps(..., ensure_ascii=False, separators=(",", ":"),
sort_keys=False)`` and the two strings have to be equal. Nothing is
sorted, nothing is normalised, and ``cwd`` travels in the comparison as
well -- a command line that is right but runs in the wrong directory is
still a change.

The environment travels with it, for the same reason. The baseline records
the environment the *command line* contributes -- the variables it adds to
the inherited environment -- and the adapter's ``env_delta`` is the
counterpart of exactly that. The comparison is byte for byte and
``sort_keys=False``, so the order the variables are named in is part of
what is pinned: an environment that is the same set of variables in a
different order is a different environment.

What is not compared
--------------------
``--settings``. The baseline was captured before the settings file moved
out of the inline construction, so two of its eight cases still carry the
``--settings`` flag and the path that goes with it. That flag belongs to the
wiring that writes the file -- ``coding_tool.py`` appends it once the
environment has been finalised -- and not to the adapter, which is why the
adapter's argv does not have it (``test_claude_harness_unit.py`` pins that
too). The pair is therefore dropped from the expectation, and everything
else is compared byte for byte and in order.

A missing fixture fails
-----------------------
If the baseline is not there, this file fails. It does not skip: a skipped
gate is indistinguishable from a passing one, and the whole point of a
baseline is that its absence is an error rather than a shrug. A baseline
*case* without an ``env_delta`` fails the same way: the environment the
command line contributes is part of what the baseline pins, and a case
that had quietly stopped recording it would make the comparison below
compare nothing.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from harness.claude import ClaudeHarness
from harness.contract import SessionMode, SessionRef, TaskInvocation

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------

# ``backend/tests/unit/harness/`` -> the harness fixtures live two
# levels up, next to the other fixture trees.
_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "harness"
_GOLDEN_PATH = _FIXTURE_DIR / "claude_argv_golden.json"


def _load_golden() -> Dict[str, Any]:
    """Read the baseline.

    A missing baseline is a failure, not a skip. Skipping would make a
    gate that checks nothing indistinguishable from a gate that passes.

    Returns:
        The parsed baseline.
    """
    if not _GOLDEN_PATH.is_file():
        pytest.fail(
            f"the claude argv baseline is missing: {_GOLDEN_PATH}. A "
            "baseline that is not there cannot prove that moving the argv "
            "changed nothing."
        )
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


def _without_settings(argv: List[str]) -> List[str]:
    """Drop the ``--settings`` flag and the value that goes with it.

    The settings file is written by the wiring that owns it, and the flag
    is appended there rather than by the adapter -- which is why the
    baseline still carries it (it was captured before that split) and why
    the adapter's argv does not.

    Args:
        argv: A captured command line.

    Returns:
        The command line without the settings pair.
    """
    trimmed: List[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item == "--settings":
            skip_next = True
            continue
        trimmed.append(item)
    return trimmed


def _baseline_cases() -> List[Dict[str, Any]]:
    """The baseline cases, in the shape this file replays them.

    Every case is a name, the command line the adapter owes, the
    working directory it owes it in, the environment the command line
    itself contributes and the environment the *run* owns -- all of them
    read out of the baseline.

    A case without an ``env_delta`` is a failure, not a shrug. The
    environment is part of the command line the baseline pins; a case
    that had stopped recording it would leave the comparison below with
    nothing to compare, and a comparison with nothing to compare is a
    gate that passes.

    Returns:
        The eight cases.
    """
    cases: List[Dict[str, Any]] = []
    for golden_case in _load_golden()["cases"]:
        if "env_delta" not in golden_case:
            pytest.fail(
                f"the baseline case {golden_case.get('name')!r} carries no "
                "env_delta. The environment the command line contributes "
                "is part of what the baseline pins, and a case without it "
                "cannot be compared against the adapter's."
            )
        if not isinstance(golden_case["env_delta"], dict):
            pytest.fail(
                f"the baseline case {golden_case.get('name')!r} carries an "
                f"env_delta that is not a mapping: {golden_case['env_delta']!r}"
            )
        cases.append(
            {
                "name": golden_case.get("name"),
                "argv": _without_settings(list(golden_case["argv"])),
                "cwd": golden_case.get("cwd"),
                "env_delta": golden_case["env_delta"],
                "run_env_delta": golden_case.get("run_env_delta") or {},
            }
        )
    return cases


def _case(name: str) -> Dict[str, Any]:
    """The baseline case with this name.

    Args:
        name: The name of the case.

    Returns:
        The case.
    """
    for case in _baseline_cases():
        if case["name"] == name:
            return case
    pytest.fail(
        f"the baseline has no case named {name!r}; it holds "
        f"{[case['name'] for case in _baseline_cases()]}"
    )


# ---------------------------------------------------------------------------
# The captured inputs
# ---------------------------------------------------------------------------

#: The session id the capture handed to the dispatch: a fixed, obviously
#: synthetic UUID, forwarded verbatim.
CAPTURED_SESSION_ID = "11111111-1111-1111-1111-111111111111"

#: The working directory the capture ran in. A scratch directory under
#: the system temporary directory: the same string on every machine, and
#: a directory that is not anybody's home.
CAPTURED_CWD = "/tmp/pdt-claude-baseline/workdir"

#: The MCP configuration file the capture pointed at.
CAPTURED_MCP_CONFIG = "/tmp/pdt-claude-baseline/workdir/mcp.json"

#: The settings template the capture pointed at. The dispatch wrote its
#: own private copy and pointed ``--settings`` at that; the template is
#: what the request named, and the adapter has no use for it.
CAPTURED_SETTINGS = "/tmp/pdt-claude-baseline/settings.json"

#: The eight requests the capture was made with.
#:
#: The matrix is the session mode (a new conversation or an existing one)
#: x the MCP configuration (present or not) x the tool whitelist (present
#: or not), with ``--model``, ``--settings`` and ``--append-system-prompt``
#: turned both ways across it.
CAPTURED_INPUTS: Tuple[Dict[str, Any], ...] = (
    {
        "name": "create_mcp_tools_model",
        "mode": "create",
        "mcp": True,
        "tools": ("Bash",),
        "model": "m1",
        "settings": False,
        "sys": "S",
    },
    {
        "name": "create_mcp_settings",
        "mode": "create",
        "mcp": True,
        "tools": (),
        "model": None,
        "settings": True,
        "sys": None,
    },
    {
        "name": "create_tools_sys",
        "mode": "create",
        "mcp": False,
        "tools": ("Bash", "Read"),
        "model": None,
        "settings": False,
        "sys": "S",
    },
    {
        "name": "create_plain",
        "mode": "create",
        "mcp": False,
        "tools": (),
        "model": None,
        "settings": False,
        "sys": None,
    },
    {
        "name": "resume_mcp_tools_sys",
        "mode": "resume",
        "mcp": True,
        "tools": ("Bash",),
        "model": None,
        "settings": False,
        "sys": "S",
    },
    {
        "name": "resume_mcp_sys",
        "mode": "resume",
        "mcp": True,
        "tools": (),
        "model": None,
        "settings": False,
        "sys": "S",
    },
    {
        "name": "resume_tools_settings",
        "mode": "resume",
        "mcp": False,
        "tools": ("Bash", "Read"),
        "model": None,
        "settings": True,
        "sys": None,
    },
    {
        "name": "resume_model",
        "mode": "resume",
        "mcp": False,
        "tools": (),
        "model": "m1",
        "settings": False,
        "sys": None,
    },
)

#: The names of the eight cases, in the order they are replayed.
CAPTURED_CASE_NAMES: Tuple[str, ...] = tuple(
    spec["name"] for spec in CAPTURED_INPUTS
)


def _request_from_spec(spec: Dict[str, Any]) -> TaskInvocation:
    """The request one captured input stands for.

    Args:
        spec: One entry of :data:`CAPTURED_INPUTS`.

    Returns:
        The request the adapter is asked about.
    """
    return TaskInvocation(
        prompt="",
        system_instruction=spec["sys"],
        model=spec["model"],
        mcp_config_path=CAPTURED_MCP_CONFIG if spec["mcp"] else None,
        allowed_tools=tuple(spec["tools"]),
        settings_path=CAPTURED_SETTINGS if spec["settings"] else None,
        session=SessionRef(pinned_session_id=CAPTURED_SESSION_ID),
        mode=(
            SessionMode.RESUME
            if spec["mode"] == "resume"
            else SessionMode.CREATE
        ),
        cwd=CAPTURED_CWD,
    )


def _spec(name: str) -> Dict[str, Any]:
    """The captured input with this name.

    Args:
        name: The name of the case.

    Returns:
        The captured input.
    """
    for spec in CAPTURED_INPUTS:
        if spec["name"] == name:
            return spec
    pytest.fail(f"no captured input is named {name!r}")


def _bytes_of(value: Any) -> str:
    """Serialise a value for comparison, without normalising anything.

    ``ensure_ascii=False`` keeps a non-ASCII element as itself rather than
    as an escape sequence, and the tight separators make the serialisation
    a function of the value alone: two values that serialise to the same
    string are the same bytes.

    ``sort_keys=False`` is the load-bearing part: the order the keys are
    named in is part of the value being pinned, so a mapping that is the
    same set of variables in a different order is a *different* value and
    has to compare unequal.

    Args:
        value: The value to serialise.

    Returns:
        Its serialised form.
    """
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=False
    )


# ---------------------------------------------------------------------------
# 1. Every baseline case, replayed through the adapter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_name", CAPTURED_CASE_NAMES)
def test_argv_matches_golden_bytes(case_name):
    """One baseline case, replayed through the adapter, byte for byte.

    The command line is compared as bytes, in order, the environment the
    command line contributes is compared as bytes, and the working
    directory travels with it: a command line that is right but runs in the
    wrong directory is still a change.
    """
    case = _case(case_name)

    invocation = ClaudeHarness().build_invocation(
        _request_from_spec(_spec(case_name))
    )

    _assert_invocation_matches(case, invocation)


def _assert_invocation_matches(case: Dict[str, Any], invocation: Any) -> None:
    """Assert that an invocation is the baseline case, byte for byte.

    Args:
        case: One baseline case, as :func:`_baseline_cases` returns it.
        invocation: The invocation the adapter built.

    Raises:
        AssertionError: The invocation is not the baseline case.
    """
    name = case["name"]

    assert _bytes_of(list(invocation.argv)) == _bytes_of(
        case["argv"]
    ), f"the argv for {name!r} no longer matches the baseline"

    assert _bytes_of(dict(invocation.env_delta)) == _bytes_of(
        case["env_delta"]
    ), (
        f"the environment the command line contributes for {name!r} "
        "no longer matches the baseline"
    )

    assert case["cwd"] is not None, (
        f"the baseline case {name!r} names no working directory, so "
        "there is nothing to compare the invocation's directory against"
    )
    assert _bytes_of(invocation.cwd) == _bytes_of(
        case["cwd"]
    ), f"the working directory for {name!r} no longer matches the baseline"


def test_the_gate_goes_red_when_the_adapter_changes():
    """The comparison can tell a changed adapter from the baseline.

    A gate that cannot fail is not a gate. This is the negative control
    for the whole file: the adapter is patched so that every invocation
    it builds carries an extra flag, and the comparison has to notice.

    It is the other half of the laundering check. The capture refuses
    to rewrite the fixture from the current implementation, and this
    says why that refusal is worth anything -- because when the
    implementation *does* change, the gate says so.
    """
    case_name = CAPTURED_CASE_NAMES[0]
    case = _case(case_name)

    original = ClaudeHarness.build_invocation

    def _patched(self, req):
        invocation = original(self, req)
        return dataclasses.replace(
            invocation,
            argv=tuple(invocation.argv) + ("--pdt-launder-probe",),
        )

    ClaudeHarness.build_invocation = _patched
    try:
        invocation = ClaudeHarness().build_invocation(
            _request_from_spec(_spec(case_name))
        )
    finally:
        ClaudeHarness.build_invocation = original

    with pytest.raises(AssertionError):
        _assert_invocation_matches(case, invocation)


def test_the_adapter_does_not_own_the_runs_environment():
    """The run's own identifiers stay the run's.

    The baseline records the two variables the pre-refactor dispatch
    stamped into the environment it handed the child -- the per-run uuid
    and the log file that goes with it -- separately from the command
    line's own environment delta, because their values are random on
    every dispatch and one of them is a path under the operator's
    temporary directory.

    The adapter's ``env_delta`` is the command line's, so it must not
    carry them: a value that is random per run does not belong in a
    command line, and one that did would be a different value on every
    spawn.
    """
    run_owned: set = set()
    for case in _baseline_cases():
        assert case["run_env_delta"], (
            f"the baseline case {case['name']!r} records no run-owned "
            "environment, so there is nothing to keep the adapter away from"
        )
        run_owned |= set(case["run_env_delta"])

    for case_name in CAPTURED_CASE_NAMES:
        invocation = ClaudeHarness().build_invocation(
            _request_from_spec(_spec(case_name))
        )
        overlap = set(invocation.env_delta) & run_owned
        assert not overlap, (
            f"the adapter's environment carries {sorted(overlap)}, which "
            "the baseline records as the run's own"
        )


# ---------------------------------------------------------------------------
# 2. The baseline covers the eight cases, and only those
# ---------------------------------------------------------------------------


def test_golden_covers_eight_cases():
    """The baseline covers exactly the eight cases this file replays.

    A baseline that lost a case would still let the replay pass -- the
    replay would simply have one case fewer. This test is what makes the
    shortfall loud.
    """
    names = [case["name"] for case in _baseline_cases()]

    assert len(names) == len(set(names)), f"duplicate case names: {names}"
    assert set(names) == set(CAPTURED_CASE_NAMES), (
        "the baseline's cases and the cases replayed here have drifted "
        f"apart: baseline={sorted(set(names))} "
        f"replayed={sorted(set(CAPTURED_CASE_NAMES))}"
    )
    assert len(CAPTURED_CASE_NAMES) == 8, (
        f"the baseline must be replayed over 8 cases, "
        f"found {len(CAPTURED_CASE_NAMES)}"
    )