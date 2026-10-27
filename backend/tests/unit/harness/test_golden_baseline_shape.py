"""The claude argv baseline must keep the shape the harness depends on.

What is pinned here
-------------------
``backend/tests/fixtures/harness/claude_argv_golden.json`` is the
*pre-refactor* evidence. It was captured from
``coding_tool.ClaudeCodingTool._run_claude_interactive`` while the argv
was still built inline in that function, and it is the thing the
refactor has to reproduce. Two properties of it are load-bearing:

* it covers eight inputs -- session mode x MCP config x tool whitelist
  -- with the model / settings / system-prompt switches varied across
  them, so the shape of every optional flag is pinned both "on" and
  "off";
* every captured argv starts with ``claude`` -- the binary itself. A
  sandbox (``sandbox-exec -f <profile> --``) or a backend shim in front
  of it would change that first element, and that change is exactly the
  kind of thing a baseline exists to notice.

The fixture is also required to be free of anything that identifies a
machine or an operator. It is committed and published, so a home
directory, a credential or a sandbox profile name in it is a leak, not a
detail.

Where the evidence comes from
-----------------------------
The capture is a generator, and a generator that reads the code under
test is not evidence: it produces whatever the current implementation
produces, and the gate that compares the two is then comparing the
implementation with itself. So the capture is checked here from two
sides:

* **statically** -- the capture module must not import the dispatch it
  is supposed to be capturing, nor the adapter the refactor produced.
  It has no way to reach either; the only code it runs is the driver
  program it writes into a tree materialized from the baseline commit.
* **dynamically** -- the negative control below patches the current
  adapter so that every invocation it builds carries an extra flag, and
  then asks the capture to regenerate the fixture. The fixture has to
  come out unchanged, and the patched adapter must never have been
  consulted: a capture that could be laundered by the current
  implementation would make every other gate in this file vacuous.

Both checks run without a network, without the git history and without
a claude CLI, so they are the part of the watch that never goes dark.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from harness.claude import ClaudeHarness

# ``backend/tests/unit/harness/`` -> the harness fixtures live two
# levels up, next to the other fixture trees.
_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "harness"
_GOLDEN_PATH = _FIXTURE_DIR / "claude_argv_golden.json"
_CAPTURE_PATH = _FIXTURE_DIR / "capture_claude_baseline.py"

#: Shapes that must never appear in a published fixture. The home-path
#: forms are the two this repository's static gate forbids; the rest
#: are the credential and sandbox-profile shapes that would leak an
#: operator's machine into a file everybody fetches.
_FORBIDDEN_IN_FIXTURE = (
    "/Users/",
    "/home/",
    "sk-",
    "sandbox-exec",
    "sandbox_profile",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)

#: The modules the capture must not import.
#:
#: ``coding_tool`` is the dispatch the baseline is supposed to describe
#: -- importing the working tree's copy would make the capture a
#: recording of the code under test. ``harness`` is the adapter the
#: refactor produced: the same objection, one layer up.
_FORBIDDEN_IMPORTS = ("coding_tool", "harness")


def _golden_text() -> str:
    return _GOLDEN_PATH.read_text(encoding="utf-8")


def _golden() -> dict:
    return json.loads(_golden_text())


def _load_capture_module():
    """Import the capture module by path.

    It lives in a fixture directory rather than on ``sys.path``, and it
    is a generator, not a test module -- so it is loaded explicitly
    instead of being imported by name.
    """
    spec = importlib.util.spec_from_file_location(
        "pdt_capture_claude_baseline", _CAPTURE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. The fixture's shape
# ---------------------------------------------------------------------------


def test_golden_has_eight_cases():
    """Eight inputs: session mode x MCP config x tool whitelist."""
    cases = _golden()["cases"]
    assert len(cases) == 8, (
        f"the baseline must cover 8 inputs, found {len(cases)}: "
        f"{[case.get('name') for case in cases]}"
    )


def test_every_argv_starts_with_claude():
    """The first element is the binary, not a wrapper in front of it."""
    for case in _golden()["cases"]:
        argv = case["argv"]
        assert argv, f"case {case.get('name')!r} has an empty argv"
        assert argv[0] == "claude", (
            f"case {case.get('name')!r} does not start with the claude "
            f"binary: {argv[0]!r}"
        )


def test_every_case_is_identified_and_shaped_consistently():
    """A case is a name, a non-empty argv and a cwd (or no cwd)."""
    names = []
    for case in _golden()["cases"]:
        assert isinstance(case.get("name"), str) and case["name"]
        assert isinstance(case.get("argv"), list)
        assert all(isinstance(item, str) for item in case["argv"])
        assert case.get("cwd") is None or isinstance(case["cwd"], str)
        names.append(case["name"])
    assert len(set(names)) == len(names), f"duplicate case names: {names}"


def test_every_case_carries_the_environment_it_was_given():
    """Every case records the environment the command line contributed.

    A case without an ``env_delta`` would make the environment part of
    the comparison in ``test_claude_golden.py`` vacuous -- and a
    comparison with nothing to compare is a gate that passes. So the
    field is required, not optional.

    The ``run_env_delta`` beside it is the environment the *run* owns
    (the per-run uuid and its log file): recorded separately, because
    the values are random on every dispatch and one of them is a path
    under the operator's temporary directory.
    """
    module = _load_capture_module()
    for case in _golden()["cases"]:
        name = case.get("name")
        assert isinstance(case.get("env_delta"), dict), (
            f"the baseline case {name!r} carries no env_delta; the "
            "environment the command line contributes is part of what "
            "the baseline pins"
        )
        assert isinstance(case.get("run_env_delta"), dict), (
            f"the baseline case {name!r} carries no run_env_delta"
        )
        assert set(case["run_env_delta"]) == set(module.RUN_OWNED_ENV_KEYS), (
            f"the baseline case {name!r} records the run's environment as "
            f"{sorted(case['run_env_delta'])}, which is not the run's own "
            f"({sorted(module.RUN_OWNED_ENV_KEYS)})"
        )
        assert not set(case["env_delta"]) & set(module.RUN_OWNED_ENV_KEYS), (
            f"the baseline case {name!r} records {sorted(case['env_delta'])} "
            "as the command line's own environment, but those belong to "
            "the run"
        )


def test_the_fixture_carries_nothing_machine_specific():
    """Published fixture: no home path, no credential, no profile name."""
    text = _golden_text()
    for needle in _FORBIDDEN_IN_FIXTURE:
        assert needle not in text, (
            f"the fixture carries {needle!r}; it is published, and that "
            f"identifies a machine or an operator"
        )


def test_the_fixture_records_where_it_came_from():
    """The header names the commit and how the evidence was taken.

    A baseline without a provenance is a number somebody wrote down. The
    provenance says which commit the evidence describes, which file it
    came out of, and how it was taken -- so a reader can check the
    claim instead of taking it.
    """
    golden = _golden()
    assert golden.get("commit"), "the fixture names no commit"

    provenance = golden.get("provenance")
    assert isinstance(provenance, dict), (
        "the fixture records no provenance; a baseline that does not say "
        "where it came from is a claim nobody can check"
    )
    for field in ("commit_sha", "source", "method", "coding_tool_sha256"):
        assert provenance.get(field), f"the provenance records no {field}"

    assert str(provenance["commit_sha"]).startswith(str(golden["commit"])), (
        f"the provenance names commit {provenance['commit_sha']!r} while "
        f"the fixture names {golden['commit']!r}"
    )
    assert len(str(provenance["commit_sha"])) == 40, (
        "the provenance does not name a full commit SHA"
    )


# ---------------------------------------------------------------------------
# 2. The capture cannot vouch for the current implementation
# ---------------------------------------------------------------------------


def _imported_module_names(tree: ast.AST) -> List[Tuple[int, str]]:
    """``(lineno, module)`` for every module the source imports.

    Both the statement forms count: ``import x``, ``import x.y`` and
    ``from x import y`` all name a module, and a dynamic import
    (``importlib.import_module("x")``) names one just as much -- it only
    spells it as a string instead of as a keyword.
    """
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import cannot name a top-level module.
                continue
            found.append((node.lineno, node.module or ""))
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None
            )
            if name in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.append((node.lineno, first.value))
    return found


def test_the_capture_never_imports_the_dispatch_or_the_adapter():
    """The capture reaches neither the dispatch nor the adapter.

    The capture's whole value is that it says what the code built
    *before* the argv moved. A capture that imports the working tree's
    ``coding_tool`` says what the code says today, which is the code
    under test -- and a baseline that the implementation itself
    produces is not a baseline at all.

    The driver program that does the capturing is written into a tree
    materialized from the baseline commit and run there, so the import
    that matters happens in another process, against another copy of the
    code. It is source *data* here, not an import statement, and this
    gate looks at statements.
    """
    source = _CAPTURE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_CAPTURE_PATH))

    offenders = [
        (lineno, name)
        for lineno, name in _imported_module_names(tree)
        if any(
            name == forbidden or name.startswith(f"{forbidden}.")
            for forbidden in _FORBIDDEN_IMPORTS
        )
    ]
    assert not offenders, (
        "the capture imports the code it is supposed to be capturing: "
        + ", ".join(f"{_CAPTURE_PATH.name}:{lineno}: {name}" for lineno, name in offenders)
        + ". The baseline has to come from the commit it describes."
    )


def test_the_current_adapter_cannot_lay_over_the_committed_baseline():
    """The negative control: the fixture cannot be regenerated from here.

    The adapter is patched so that every invocation it builds carries an
    extra flag, and the capture is then asked to regenerate the
    fixture. Three things have to hold afterwards:

    * the committed fixture is byte for byte what it was;
    * the patched adapter was never consulted -- the capture has no way
      to produce a payload from the working tree's dispatch, because it
      does not import it;
    * whatever was written (into a temporary directory, so the committed
      fixture is never at risk) carries no trace of the patched flag.

    Without this, "the fixture still matches" is a statement about
    nothing: the fixture would match because the same code produced
    both sides.
    """
    module = _load_capture_module()

    before = _GOLDEN_PATH.read_bytes()
    consulted: List[Any] = []

    original = ClaudeHarness.build_invocation

    def _patched(self, req):
        consulted.append(req)
        invocation = original(self, req)
        return dataclasses.replace(
            invocation,
            argv=tuple(invocation.argv) + ("--pdt-launder-probe",),
        )

    scratch = Path(tempfile.mkdtemp(prefix="pdt-launder-probe-"))
    try:
        harness_dir = scratch / "harness"
        harness_dir.mkdir(parents=True, exist_ok=True)
        module._HERE = harness_dir / _CAPTURE_PATH.name

        ClaudeHarness.build_invocation = _patched
        try:
            module.main(["--write-baseline"])
        except SystemExit:
            # Refusing is the expected outcome away from the baseline
            # commit, and it is a refusal, not a write.
            pass
        finally:
            ClaudeHarness.build_invocation = original

        written = harness_dir / "claude_argv_golden.json"
        payload = written.read_bytes() if written.exists() else None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    assert not consulted, (
        "the capture consulted the current adapter; it produced its "
        "payload from the code under test"
    )
    assert _GOLDEN_PATH.read_bytes() == before, (
        "the attempt to regenerate the fixture changed the committed one"
    )
    assert payload is None or b"--pdt-launder-probe" not in payload, (
        "the regenerated fixture carries the patched adapter's flag; the "
        "capture produced it from the current implementation"
    )


def test_the_capture_refuses_to_run_away_from_the_baseline_commit():
    """``main`` refuses unless the checkout is at the baseline commit.

    The fixture may only be written from the commit it describes. A
    capture taken anywhere else would be the current implementation
    vouching for itself, so the entry point refuses and exits non-zero
    rather than producing a payload that could be mistaken for one.
    """
    module = _load_capture_module()
    try:
        head = module.head_commit()
        baseline = module.baseline_sha()
    except module.BaselineUnavailable:
        # The baseline commit is not in this checkout -- a shallow clone
        # does not carry it. The capture has to say so out loud rather
        # than go on without the code it is supposed to describe, and
        # saying so means refusing.
        with pytest.raises(SystemExit):
            module.main([])
        return

    if head == baseline:
        # The checkout *is* the baseline commit, which is the one place
        # the capture is allowed to run. There is nothing to refuse.
        return

    with pytest.raises(SystemExit):
        module.main([])


# ---------------------------------------------------------------------------
# 3. The fixture still describes the baseline commit
# ---------------------------------------------------------------------------


def test_the_committed_baseline_is_what_the_baseline_commit_produces():
    """The committed fixture is the baseline commit's own output.

    The capture is taken again, from the same commit, and compared with
    the committed fixture. A fixture that had been hand-edited, or that
    had been produced by something other than the baseline, would not
    survive the comparison.

    A clone that does not carry the baseline commit -- a shallow clone
    does not -- cannot produce the evidence at all. The capture says so
    out loud, and that refusal is what this asserts there: a baseline
    that cannot be taken is an error, never a shrug.
    """
    module = _load_capture_module()
    try:
        fresh = module.capture_cases()
    except module.BaselineUnavailable as exc:
        assert (
            module.BASELINE_COMMIT in str(exc) or "git" in str(exc)
        ), (
            "the capture failed for a reason that is not about the "
            f"baseline commit being absent: {exc}"
        )
        return

    assert fresh["cases"] == _golden()["cases"], (
        "the captured argv no longer matches the committed baseline"
    )
    assert fresh["provenance"] == _golden()["provenance"], (
        "the committed baseline records a different provenance than the "
        "capture it was taken from"
    )


def test_the_eight_inputs_cover_the_whole_matrix():
    """Session mode x MCP config x tool whitelist, each combination once.

    A baseline that covers, say, every combination with the tool
    whitelist on and off but never a plain dispatch would pin half the
    behaviour and leave the other half free to change.
    """
    module = _load_capture_module()
    combinations = {
        (case["mode"], bool(case["mcp"]), bool(case["tools"]))
        for case in module.CASES
    }
    assert len(module.CASES) == 8
    assert len(combinations) == 8, (
        f"the eight inputs do not cover the matrix: {sorted(combinations)}"
    )


def test_every_orthogonal_switch_is_switched_both_ways():
    """``--model`` / ``--settings`` / ``--append-system-prompt``.

    A switch that is only ever on pins the "on" shape and says nothing
    about the "off" one -- which is the shape the other seven cases
    depend on.
    """
    module = _load_capture_module()
    for key in ("model", "settings", "sys"):
        states = {bool(case[key]) for case in module.CASES}
        assert states == {True, False}, (
            f"the {key!r} switch is never turned both ways: {states}"
        )


def test_the_golden_cases_are_the_captured_inputs_in_order():
    """The fixture and the generator describe the same eight inputs."""
    module = _load_capture_module()
    assert [case["name"] for case in _golden()["cases"]] == [
        case["name"] for case in module.CASES
    ]


# ---------------------------------------------------------------------------
# 4. The capture polices its own output
# ---------------------------------------------------------------------------


def test_the_capture_refuses_to_hand_over_a_machine_specific_fixture():
    """The generator polices its own output, before anything is written.

    ``test_the_fixture_carries_nothing_machine_specific`` checks the
    *committed* file. That check runs after the fact: a leaky capture
    has already been written, and the leak is already in the working
    tree for whatever wrote it to pick up. The generator has to refuse
    the payload itself, so a capture that goes wrong stops at the
    capture instead of at the gate.
    """
    module = _load_capture_module()

    leaky_home = {
        "commit": "8046e97",
        "cases": [
            {
                "name": "leaky",
                "argv": ["claude", "--settings", "/Users/someone/.claude/s.json"],
                "cwd": None,
            }
        ],
    }
    with pytest.raises(ValueError):
        module.assert_publishable(leaky_home)

    leaky_outside = {
        "commit": "8046e97",
        "cases": [
            {
                "name": "leaky",
                "argv": ["claude", "--mcp-config", "/var/folders/zz/mcp.json"],
                "cwd": None,
            }
        ],
    }
    with pytest.raises(ValueError):
        module.assert_publishable(leaky_outside)


def test_the_capture_accepts_its_own_baseline():
    """The check the generator applies does not reject its own output."""
    module = _load_capture_module()
    assert module.assert_publishable(_golden()) == _golden()


def test_the_capture_refuses_a_run_owned_value_it_cannot_describe():
    """A placeholder that does not describe the value is a refusal.

    The run's own environment values are recorded with their random
    values replaced. A value that does not have the shape the
    placeholder claims would make the published fixture a claim nothing
    backs, so the capture stops instead.
    """
    module = _load_capture_module()

    assert module._run_env_placeholder("PDT_SUBAGENT_UUID", "0" * 32) == (
        "<per-run-uuid>"
    )
    with pytest.raises(module.BaselineUnavailable):
        module._run_env_placeholder("PDT_SUBAGENT_UUID", "not-a-uuid")
    with pytest.raises(module.BaselineUnavailable):
        module._run_env_placeholder("PDT_SUBAGENT_LOG_FILE", "/tmp/elsewhere.log")
