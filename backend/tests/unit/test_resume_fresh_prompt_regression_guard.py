"""Regression guard: the fresh_prompt plumbing must not disturb the session suites.

What this pins
--------------
The ``fresh_prompt`` work changed the retry layer of ``coding_tool.py``
(empty response -> retry in a fresh session) and the resume entry point
that hands the fresh session its prompt.  The four suites below are the
ones that already exercise that neighbourhood:

* ``tests/unit/test_coding_tool.py``
* ``tests/unit/test_coding_tool_reusable_json_session.py``
* ``tests/unit/test_task_session_resume.py``
* ``tests/unit/test_fastfail_deterministic_local_error.py``

All four were green BEFORE the change, so running them as a
"test_command" proves nothing on their own — they cannot distinguish
"the change was inert" from "the change was never applied".  What they
CAN do is guard the change: a regression guard that runs them as a
subprocess and fails loudly the moment one of them goes red.

Why a subprocess
----------------
The guard has to observe the suites the way the gate runs them, from the
backend directory, under the backend's own ``pytest.ini`` — not as an
in-process import of the same modules.  Importing them would run them
under whatever rootdir/marker expression the outer invocation happened
to resolve, which is exactly the drift the guard is supposed to catch.
The subprocess pins ``cwd`` to the backend directory, so the inner run
resolves ``backend/pytest.ini`` and its ``addopts`` every time.

The four paths are named one by one.  Recursive collection would drag
this file into its own run: the guard would spawn a subprocess that
collects the guard, which spawns another one, and so on.

The subprocess uses ``sys.executable`` (the interpreter running this
test), never a hard-coded path, so the guard works under whatever
virtualenv invoked it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_SIBLING_FILES = (
    "tests/unit/test_coding_tool.py",
    "tests/unit/test_coding_tool_reusable_json_session.py",
    "tests/unit/test_task_session_resume.py",
    "tests/unit/test_fastfail_deterministic_local_error.py",
)


def test_the_named_session_suites_still_pass():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *_SIBLING_FILES, "-q"],
        cwd=str(_BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        "the fresh_prompt plumbing must not disturb the existing "
        "session suites:\n"
        + proc.stdout[-4000:]
        + proc.stderr[-2000:]
    )