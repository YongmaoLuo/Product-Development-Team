"""The creation case delivers through the command hook, and this file is
the gate that keeps it there.

Why this file exists
--------------------
A plan's hook used to be stored as a *description* of a transport::

    {"kind": "cc_connect", "destination": "grp-1", "session_id": "s-1"}

The dispatcher does not interpret that description — its transports are
``http`` and ``cmd`` — so the stored shape was rewritten.  What a plan
stores now is the delivery command itself::

    {"kind": "cmd",
     "command": "<python> -m backend.hooks.session_send --project grp-1 --session s-1"}

``tests/e2e/test_hitl_hooks_e2e.py::test_creation_binds_destination``
pinned the old shape: it asserted that the plan's configuration held a
``cc_connect`` element, that the dispatcher skipped it as a transport it
does not implement, and that the question was therefore settled as
``unroutable``.  After the rewrite that case no longer described what
the product does — it described a transport the product stopped
writing.

A case that used to be green cannot be the evidence that it was
aligned, though.  It was green *before* the rewrite and red *after* it,
which makes it a witness to the change and not a gate on it: nothing
about the case itself says the shape it asserts is the shape the
product writes.  So the alignment is pinned here, in a file whose only
subject is that alignment.

What this gate pins
-------------------
1. **The case asks for the command hook.**  The body of
   ``test_creation_binds_destination`` names the ``cmd`` kind, and it
   does not mention the transport the rewrite replaced.  A case that
   asserted the old element again would be asserting a contract the
   writer no longer implements.
2. **The case's file names the transport it runs, and keeps it in its
   own work directory.**  The delivery is made by a stub transport the
   product is pointed at through the environment, so the file has to
   name the variables that say which transport and where it records
   what it was handed — and it has to build the stub's path and the
   capture's path from the work directory, never from the repository.
3. **The aligned case passes.**  The case is run as a subprocess, so
   this gate reports what the case *does* and not only what its text
   says — a case that was edited into a shape nobody can run would
   pass the two checks above and fail here.

Scope, stated honestly
----------------------
This gate says nothing about the other two cases in the file, and
nothing about the dispatcher's transports.  It is about one function:
the one that binds a creation request's destination to the plan's own
delivery command.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# The e2e layer, and only the e2e layer, for the same reason the
# sibling file says it: this file drives pytest as a subprocess, so it
# is not a unit test, and the default lanes exclude the e2e marker.
pytestmark = [pytest.mark.e2e]


#: ``backend/`` — this file lives in ``backend/tests/e2e/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The suite this gate governs.
TARGET_FILE = BACKEND_DIR / "tests" / "e2e" / "test_hitl_hooks_e2e.py"

#: The case in that suite the rewrite left behind.
TARGET_FUNCTION = "test_creation_binds_destination"

#: The transport the product is pointed at, and the variable that says
#: where the transport records what it was handed.  The file that owns
#: the case has to name both: the delivery is made by running a
#: command, and that command is pointed at a transport.
TRANSPORT_BIN_ENV = "PDT_CC_CONNECT_BIN"
TRANSPORT_CAPTURE_ENV = "PDT_CC_CONNECT_CAPTURE"

#: The transport the rewrite replaced.  Its name is what the case must
#: not expect any more.
OLD_TRANSPORT = "cc_connect"

#: The names the gated file gives the stub transport and the file it
#: records what it was handed into.  Both are paths the gated file
#: builds from its work directory, never from the repository.
STUB_CONSTANT = "STUB_FILENAME"
CAPTURE_CONSTANT = "CAPTURE_FILENAME"

# Prefer the checkout's own virtualenv; fall back to the interpreter
# already running pytest when there is no ``backend/.venv``.  The same
# choice the gated file makes, and for the same reason: the child
# session has to be able to import the backend it exercises.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)


def _target_source() -> str:
    """The text of the gated file, read from disk."""
    assert TARGET_FILE.exists(), (
        f"{TARGET_FILE} is missing: the gate is pointing at a tree that moved."
    )
    source = TARGET_FILE.read_text(encoding="utf-8")
    assert source.strip(), f"{TARGET_FILE} is empty: a gate that reads nothing proves nothing."
    return source


def _target_body(source: str) -> str:
    """The source of the gated case, read out of the file's own AST.

    The function is located by name and its text is taken from the
    parsed module rather than from a regex: a regex over the file
    cannot tell the case's body from the prose around it, and the
    prose is where a stale expectation is most likely to survive.
    """
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == TARGET_FUNCTION:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, (
                f"{TARGET_FUNCTION} is defined in {TARGET_FILE} but its "
                "source could not be read back; the file's lines do not "
                "match its parse."
            )
            return segment
    raise AssertionError(
        f"{TARGET_FUNCTION} is not defined in {TARGET_FILE}: the gate is "
        "pointing at a case that has been renamed or removed."
    )


def test_creation_case_source_expects_cmd_delivery() -> None:
    """The creation case asks for the command hook, and names the
    transport it runs.

    The kind is asserted by name, because the kind is the contract: a
    plan's hook is the delivery command itself, and a case that expects
    a description of a transport is expecting a contract the writer no
    longer implements.

    The transport variables are required in the file rather than
    assumed: the delivery is made by running a command, and that
    command is pointed at a transport through the environment.  A file
    that delivered without naming the transport would be delivering to
    whatever happened to be on ``PATH``.
    """
    source = _target_source()
    body = _target_body(source)

    assert '"cmd"' in body, (
        f"{TARGET_FUNCTION} does not expect the 'cmd' hook kind.\n"
        "A plan's hook is now the delivery command itself, so the case "
        "has to name the kind it expects: the hook is run, not "
        "interpreted."
    )
    assert OLD_TRANSPORT not in body, (
        f"{TARGET_FUNCTION} still expects the {OLD_TRANSPORT!r} hook.\n"
        "That transport is not what a plan is written with any more; the "
        "dispatcher's transports are 'http' and 'cmd'."
    )

    for variable in (TRANSPORT_BIN_ENV, TRANSPORT_CAPTURE_ENV):
        # The name has to appear as a whole token, not as the prefix of a
        # longer one: a file that renamed the variable to
        # ``PDT_CC_CONNECT_BIN2`` still *contains* the name as a
        # substring, and the delivery it made would be pointed at
        # nothing.  A boundary is what tells the two apart.
        assert re.search(rf"\b{re.escape(variable)}\b", source), (
            f"{TARGET_FILE.name} does not name {variable}.\n"
            "The delivery is made by running the plan's command, and that "
            "command has to be pointed at a transport: one variable says "
            "which transport, the other says where the transport records "
            "what it was handed."
        )

    for name in (STUB_CONSTANT, CAPTURE_CONSTANT):
        assert f"workdir / {name}" in source, (
            f"{TARGET_FILE.name} does not build the path of {name} from the "
            "work directory.\n"
            "The stub transport and the file it records into belong to the "
            "test's own temporary directory: a test that writes them into "
            "the repository leaves its own residue behind."
        )

    assert OLD_TRANSPORT not in source, (
        f"{TARGET_FILE.name} still mentions {OLD_TRANSPORT!r}.\n"
        "The transport the rewrite replaced is not a thing this file "
        "delivers through any more, and a file that still names it is "
        "describing a contract the product does not implement."
    )


def test_creation_binding_case_passes() -> None:
    """The aligned case passes when it is run on its own.

    The two checks above read text; this one runs the case, so the gate
    fails on an assertion that does not hold and not only on a spelling
    that drifted.

    The pass count is asserted as well as the exit status: pytest exits
    5 for "no tests were collected", which is a different failure from a
    case that broke, and a driver that read only the exit status would
    report an emptied file as health.
    """
    env = dict(os.environ)
    # ``PYTEST_ADDOPTS`` comes from the operator's shell and would be
    # inherited by the child session; anything that turned that session
    # parallel (or filtered the case away) would defeat the run.  The
    # xdist worker variables are dropped for the same reason: a child
    # session that believes it is a worker tries to talk to a
    # controller that is not there.
    for name in (
        "PYTEST_ADDOPTS",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
    ):
        env.pop(name, None)

    result = subprocess.run(
        [
            str(VENV_PYTHON),
            "-m",
            "pytest",
            f"tests/e2e/{TARGET_FILE.name}::{TARGET_FUNCTION}",
            "-q",
            "--no-header",
            "-p",
            "no:xdist",
        ],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )

    assert result.returncode == 0, (
        f"{TARGET_FUNCTION} does not pass on its own "
        f"(exit {result.returncode}).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )

    reported = re.search(r"(\d+) passed", result.stdout)
    assert reported is not None, (
        "the child session reported no pass count.\n"
        f"--- stdout ---\n{result.stdout}"
    )
    assert int(reported.group(1)) == 1, (
        f"the child session passed {reported.group(1)} cases; this gate "
        f"runs exactly one ({TARGET_FUNCTION}).\n"
        f"--- stdout ---\n{result.stdout}"
    )