"""No test may mock ``subprocess.Popen``.

Why this gate exists
--------------------
``subprocess.Popen`` is where a harness process actually starts.  A
test that replaces it does not test the harness: it tests the test's
own stand-in, and it does so *at the one seam where the harness's
behaviour is decided* — the order of the sandbox decision, the
environment merge, the spawn and the reap.

The failure this prevents is quiet.  A harness test that mocks
``Popen`` passes whether the runner spawns the command line the
adapter built, spawns the wrong one, or spawns nothing at all: the
mock swallows the argv, the assertion looks at the mock's
return value, and the run is green.  The mock is also how a *regression*
hides: a change that moved the spawn out of the runner and back into
the dispatch module would still pass every test that mocks
``Popen``, because the mock is installed on the module the test
imported.

That is why the harness test suite says, in its own docstrings, that
no ``Popen`` is mocked.  A promise in a docstring is a promise that
the next commit can break by accident; this gate is the promise made
executable.

What counts as mocking
-----------------------
The forbidden shapes are the ones that *replace* the spawn point:

* ``patch("subprocess.Popen")`` — the string target form;
* ``patch.object(subprocess, "Popen")`` — the attribute form;
* ``setattr(subprocess, "Popen", ...)`` — and ``monkeypatch.setattr``,
  which is the same thing with a fixture that restores it afterwards.

A test that *mentions* ``"subprocess.Popen"`` is not mocking it.  The
assertion ``assert "subprocess.Popen" not in source`` is the harness
suite checking that the dispatch module does not spawn any more, and
flagging that assertion would flag the very check this gate exists to
make possible.  So the gate reads the AST: it looks at calls, and at
what those calls are called on.

Why the legacy sites are named rather than allowed by default
-------------------------------------------------------------
Seven test modules and one capture script mock ``Popen`` today, all of
them predating the harness layer: a tripwire that fails a test if
anything spawns, recorders that capture the argv and the child's
environment, the service manager's own child being faked, and the
server's own child being stubbed.  They are named in
:data:`ALLOWED_MOCK_SITES`, each with the reason it exists, rather
than being excluded by a pattern — an exclusion list that nobody
reads is how a gate stops catching anything.

The harness test suite is **not** allowlistable.  There is no entry
for it and there may not be one: the whole point of the suite is that
it runs against the real spawn point.

Why a *gate* and not just the rule
----------------------------------
Because the rule is one line long and the failure is invisible.  A
mocked ``Popen`` in one test file does not fail that file; it removes
the harness's spawn from that file's coverage, and the suite stays
green while the guarantee it was written to provide evaporates.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: The backend directory, derived from this file's location rather than
#: from the process working directory (CI runs the suite with
#: ``working-directory: backend``).
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The test tree this gate reads.
TESTS_DIR = BACKEND_DIR / "tests"

#: The attribute that is never mocked.
MOCKED_ATTRIBUTE = "Popen"

#: The string target that is never patched.
MOCKED_TARGET = "subprocess.Popen"

#: The harness test suites, which may never be allowlisted: they run
#: against the real spawn point.
HARNESS_TEST_DIRS: tuple[str, ...] = (
    "tests/unit/harness",
    "tests/integration/harness",
)

#: The mock sites that predate the harness layer, each named with the
#: reason it exists.  A new one is a red gate until it is written down
#: here.
#:
#: Keys are paths relative to :data:`BACKEND_DIR`.
ALLOWED_MOCK_SITES: dict[str, str] = {
    "tests/e2e/test_capacity_wait_plan_state_end_to_end.py": (
        "the fixture wraps the real Popen in a counter so the end-to-end "
        "run can assert that a quota park left no child behind; the "
        "wrapper still calls the real spawn, and the spawn it counts is "
        "the executor's own"
    ),
    "tests/fixtures/harness/capture_claude_baseline.py": (
        "the capture script's whole purpose is to record the argv the "
        "dispatch builds without running the CLI; it fakes the child "
        "process to do that"
    ),
    "tests/integration/api/test_api_error_matrix.py": (
        "the API error matrix stubs the server's own child so the "
        "matrix can walk every error path"
    ),
    "tests/integration/api/test_archived_plan_write_returns_410.py": (
        "the archived-plan test stubs the server's own child for the "
        "same reason as the error matrix"
    ),
    "tests/integration/test_capacity_wait_negative_control.py": (
        "the fixture wraps the real Popen in a counter so the parked "
        "run can assert that a quota park left no child behind; the "
        "wrapper still calls the real spawn, and the spawn it counts is "
        "the executor's own"
    ),
    "tests/integration/test_capacity_wait_positive_control.py": (
        "the fixture wraps the real Popen in a counter so the recovery "
        "run can assert that a quota park left no child behind; the "
        "wrapper still calls the real spawn, and the spawn it counts is "
        "the executor's own"
    ),
    "tests/unit/test_coding_tool_cc_switch_current_provider.py": (
        "the recorder captures the argv and the environment the child was "
        "started with, which is what the cc-switch current-provider "
        "assertion is about"
    ),
    "tests/unit/test_coding_tool_scene_routing.py": (
        "the recorder captures the environment the child was started with, "
        "which is what the scene-routing assertion is about"
    ),
    "tests/unit/test_cooperative_stop_sequence.py": (
        "mocks Popen so the dispatch has an execution child that never "
        "answers, which is what the live-dispatch registry bookkeeping "
        "is asserted against; this predates the harness layer and is not "
        "part of the harness suite"
    ),
    "tests/unit/test_credentials_switch.py": (
        "a tripwire: the test fails if anything spawns at all, which is "
        "the assertion the credential switch is about"
    ),
    "tests/unit/test_service_failure_diagnostics.py": (
        "the service manager's own child is faked so the not-ready "
        "diagnostic can be assembled without a real service; the lsof "
        "probe is let through to the real Popen on purpose"
    ),
    "tests/unit/test_service_manager.py": (
        "the service manager's own child is faked so the manager's "
        "start/stop bookkeeping can be tested without a real service"
    ),
    "tests/unit/test_service_manager_log_excerpt.py": (
        "the service manager's own child is faked so the log excerpt is "
        "read out of a fixture log instead of a running service"
    ),
    "tests/unit/test_service_spawn_shape.py": (
        "a recorder: the test asserts what the spawn path asks for — the "
        "argv and the keywords — without starting anything"
    ),
    "tests/unit/test_session_reuse_planning_phases.py": (
        "the recorder captures the argv the dispatch spawns, which is "
        "what the session-reuse assertion is about"
    ),
    "tests/unit/test_spawn_repro_harness.py": (
        "the service manager's own child is faked so the spawn repro "
        "harness can assert on the argv and the keywords it was given"
    ),
    "tests/unit/test_test_command_rules.py": (
        "a tripwire: the rule must not start a process itself, so any "
        "spawn at all fails the test"
    ),
    "tests/unit/test_verification_plan_doc_structure.py": (
        "a tripwire: the structure check must not shell out, so any "
        "spawn at all fails the test"
    ),
}


def _is_subprocess_module(node: ast.AST) -> bool:
    """True when *node* refers to the ``subprocess`` module.

    Both ``subprocess`` and ``module.subprocess`` (the module imported
    into another module's namespace) count: the second is how a test
    reaches the very same object.
    """
    if isinstance(node, ast.Name):
        return node.id == "subprocess"
    if isinstance(node, ast.Attribute):
        return node.attr == "subprocess"
    return False


def _is_subprocess_target(node: ast.AST) -> bool:
    """True when *node* is the string ``"subprocess.Popen"``."""
    if isinstance(node, ast.Constant):
        return node.value == MOCKED_TARGET
    if isinstance(node, ast.JoinedStr):
        # An f-string target is built at runtime; a literal that spells
        # the whole name is still the same target.
        return any(
            isinstance(part, ast.Constant) and part.value == MOCKED_TARGET
            for part in node.values
        )
    return False


def _call_kind(node: ast.Call) -> str | None:
    """The mocking shape of *node*, or ``None`` when it is not a mock.

    One of ``"patch"``, ``"patch.object"`` or ``"setattr"``.
    """
    func = node.func
    if isinstance(func, ast.Name):
        if func.id == "patch":
            return "patch"
        if func.id == "setattr":
            return "setattr"
        return None
    if isinstance(func, ast.Attribute):
        if func.attr == "patch":
            return "patch"
        if func.attr == "object" and isinstance(func.value, (ast.Name, ast.Attribute)):
            inner = func.value
            if isinstance(inner, ast.Name) and inner.id == "patch":
                return "patch.object"
            if isinstance(inner, ast.Attribute) and inner.attr == "patch":
                return "patch.object"
        if func.attr == "setattr":
            return "setattr"
    return None


def find_popen_mocks(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, description)`` for every mock of ``Popen``.

    Args:
        source: The text of the test module to scan.

    Returns:
        One ``(lineno, description)`` pair per mock, in source order.
        The description names the shape — ``patch("subprocess.Popen")``
        and friends — and never quotes a line of the file, so a failure
        message cannot carry the file's own text.
    """
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _call_kind(node)
        if kind == "patch":
            if node.args and _is_subprocess_target(node.args[0]):
                found.append((node.lineno, f'patch("{MOCKED_TARGET}")'))
        elif kind == "patch.object":
            if (
                len(node.args) >= 2
                and _is_subprocess_module(node.args[0])
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == MOCKED_ATTRIBUTE
            ):
                found.append((node.lineno, f'patch.object(subprocess, "{MOCKED_ATTRIBUTE}")'))
        elif kind == "setattr":
            if not node.args:
                continue
            # ``monkeypatch.setattr("subprocess.Popen", fake)`` — the
            # string form of the same mock.
            if _is_subprocess_target(node.args[0]):
                found.append((node.lineno, f'setattr("{MOCKED_TARGET}", ...)'))
                continue
            if (
                len(node.args) >= 2
                and _is_subprocess_module(node.args[0])
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == MOCKED_ATTRIBUTE
            ):
                found.append((node.lineno, f'setattr(subprocess, "{MOCKED_ATTRIBUTE}", ...)'))
    found.sort(key=lambda hit: hit[0])
    return found


def _test_modules():
    """Yield ``(relative_path, absolute_path)`` for every test module."""
    for path in sorted(TESTS_DIR.rglob("*.py")):
        rel = path.relative_to(BACKEND_DIR)
        if "__pycache__" in rel.parts:
            continue
        yield rel, path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_popen_mocking() -> None:
    """No test module mocks the spawn point — and the gate can still fail.

    Two halves of one guarantee: the tree is clean, and the gate is
    not a rubber stamp.  A gate that cannot fail is indistinguishable
    from a passing one, so a violating sample is injected and has to
    be reported.
    """
    offenders: list[str] = []
    for rel, path in _test_modules():
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        try:
            mocks = find_popen_mocks(source)
        except SyntaxError:  # pragma: no cover - a broken file is another test's problem
            continue
        if not mocks:
            continue
        allowed = ALLOWED_MOCK_SITES.get(str(rel))
        if allowed is not None:
            continue
        for lineno, description in mocks:
            offenders.append(f"{rel}:{lineno}: {description}")

    assert not offenders, (
        "a test mocks subprocess.Popen. The harness suite runs against "
        "the real spawn point: a mocked Popen passes whether the runner "
        "spawns the command line the adapter built or the wrong one, and "
        "it hides a regression that moves the spawn back out of the "
        "runner.\n  " + "\n  ".join(offenders)
    )

    # The gate has to be able to fail.
    injected = find_popen_mocks(
        "from unittest.mock import patch\n"
        "\n"
        "def test_something():\n"
        '    with patch("subprocess.Popen", fake):\n'
        "        run_the_thing()\n"
    )
    assert injected, (
        "the gate did not report an injected Popen mock, so it cannot "
        "fail and the assertion above proves nothing"
    )


def test_the_gate_catches_every_mocking_shape() -> None:
    """Each forbidden shape is reported, and each allowed one is not.

    The allowed shapes matter as much as the forbidden ones: a gate
    that also flagged a test which merely *mentions*
    ``"subprocess.Popen"`` would flag the harness suite's own check
    that the dispatch module does not spawn, and would be allowlisted
    within a week.
    """
    shapes = {
        "patch": 'with patch("subprocess.Popen", fake):\n    pass\n',
        "patch.object": "with patch.object(subprocess, \"Popen\"):\n    pass\n",
        "setattr": "monkeypatch.setattr(subprocess, \"Popen\", fake)\n",
        "module.setattr": "monkeypatch.setattr(ct.subprocess, \"Popen\", fake)\n",
        "setattr string": 'monkeypatch.setattr("subprocess.Popen", fake)\n',
    }
    for label, source in shapes.items():
        hits = find_popen_mocks(source)
        assert hits, f"the gate did not report the {label} shape"

    allowed = {
        "mention": 'assert "subprocess.Popen" not in source\n',
        "other attribute": 'monkeypatch.setattr(subprocess, "DEVNULL", fake)\n',
        "other module": 'monkeypatch.setattr(other, "Popen", fake)\n',
        "no mock": "process = subprocess.Popen(argv)\n",
    }
    for label, source in allowed.items():
        assert not find_popen_mocks(source), f"the gate reported the {label} shape"


def test_the_allowlist_names_no_harness_test() -> None:
    """The harness test suite may never be allowlisted.

    The harness suite is the one that runs against the real spawn
    point.  An entry for it would turn "no harness test mocks Popen"
    into "no harness test mocks Popen except the ones somebody listed",
    which is the rule this gate exists to remove.
    """
    offenders = [
        name
        for name in ALLOWED_MOCK_SITES
        if any(name.startswith(prefix) for prefix in HARNESS_TEST_DIRS)
    ]
    assert not offenders, (
        "ALLOWED_MOCK_SITES names a harness test module. The harness "
        "suite runs against the real spawn point and may not mock "
        "it.\n  " + "\n  ".join(offenders)
    )


def test_every_allowlist_entry_is_used_and_explained() -> None:
    """No stale entries, and no entry without a reason.

    A stale entry is a hole: the mock is deleted, the entry stays, and
    the next person to add the mock finds it already allowed.  An
    entry without a reason is a hole with a worse shape, because
    nobody can tell whether it is still true.
    """
    used: set[str] = set()
    for rel, path in _test_modules():
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        try:
            mocks = find_popen_mocks(source)
        except SyntaxError:  # pragma: no cover - a broken file is another test's problem
            continue
        if mocks:
            used.add(str(rel))

    stale = [name for name in ALLOWED_MOCK_SITES if name not in used]
    assert not stale, (
        "ALLOWED_MOCK_SITES names a file that no longer mocks Popen. A "
        "stale entry is a hole: the mock comes back and finds itself "
        "already allowed.\n  " + "\n  ".join(stale)
    )

    unexplained = [
        name for name, reason in ALLOWED_MOCK_SITES.items() if not reason.strip()
    ]
    assert not unexplained, (
        "ALLOWED_MOCK_SITES has an entry without a reason. The reason is "
        "the part a reviewer needs; an entry without one cannot be "
        "checked.\n  " + "\n  ".join(unexplained)
    )


def test_the_gate_reads_the_real_tree() -> None:
    """The scan target must be the test tree, and it must not be empty.

    A gate whose scan root has moved reads nothing and passes
    vacuously — which is how the original defect shipped: the paths
    were there and the check was not running.
    """
    modules = list(_test_modules())
    assert modules, (
        "the test tree came back empty, so the gate would scan nothing "
        "and pass vacuously"
    )
    assert any(
        str(rel).startswith(HARNESS_TEST_DIRS[0]) for rel, _ in modules
    ), (
        "the harness unit tests are not in the scanned tree, so the "
        "suite this gate is about would not be read"
    )
