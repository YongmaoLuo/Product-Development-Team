"""A pytest process must not inherit the executor's per-task stamps.

What went wrong
---------------
The executor runs a task in a process whose environment carries that task's
stamps: ``PDT_ALLOWED_WRITE_FILES`` (the task's declaration),
``PDT_UNDECLARED_WRITE_DIR`` (where refusal markers are written),
``PDT_READONLY_TASK``, ``PDT_REPORT_PATH``, ``PDT_LOCK_TASK_ID``,
``PDT_DECLARED_MANIFEST_HASH`` and ``PDT_LOCK_TIMEOUT``.  A pytest run
launched by that executor inherits all of them -- and the suite's own guard
tests then trip over them.

``tests/test_edit_write_containment_guard.py`` starts the guard with
``os.environ.copy()`` and pops only the project/plans variables.  The
inherited declaration does the rest: the guard sees a declaration that does
not list the test's own probe file, refuses it as "this task did not
declare that file", and -- because ``PDT_UNDECLARED_WRITE_DIR`` is also
inherited -- writes the refusal marker into **the executor's** marker
directory.  The executor then reads that marker and classifies the run as
an undeclared write by the agent.

The refusal was the test's, and the marker was aimed at the test's scratch
file, but the executor cannot tell the difference: the marker channel
carries no owner.  The run was reported as an agent violation on the
strength of the suite's own negative control.

The rule this pins: the executor's stamps describe ONE task, and a test
process is not that task.  They are cleared, unconditionally, when the
suite installs its hermetic environment -- the same treatment
``PDT_LOCK_BROKER`` already gets, and for the same reason: a
machine/process-level variable must not decide the suite's verdict.

Why a subprocess
----------------
``install_hermetic_env()`` is idempotent through a module-level
``_INSTALLED_ENV`` short-circuit: the second call in a process is a no-op.
A test that set the variables and called it *in this process* would be
asserting about a call that never ran the installer.  The probe therefore
runs in a child, where the first call is the real one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The stamps the executor writes into the environment of the process it
#: runs a task in.  They describe one task; a test process is not it.
TASK_SCOPE_VARS = (
    "PDT_ALLOWED_WRITE_FILES",
    "PDT_UNDECLARED_WRITE_DIR",
    "PDT_READONLY_TASK",
    "PDT_REPORT_PATH",
    "PDT_LOCK_TASK_ID",
    "PDT_DECLARED_MANIFEST_HASH",
    "PDT_LOCK_TIMEOUT",
)


def _stamps() -> dict[str, str]:
    """A plausible set of per-task stamps, as the executor writes them."""
    return {
        "PDT_ALLOWED_WRITE_FILES": json.dumps(["backend/a.py"]),
        "PDT_UNDECLARED_WRITE_DIR": "/tmp/pdt-task-markers",
        "PDT_READONLY_TASK": "1",
        "PDT_REPORT_PATH": "/tmp/pdt-task-report.md",
        "PDT_LOCK_TASK_ID": "repair-r2-01",
        "PDT_DECLARED_MANIFEST_HASH": "deadbeef",
        "PDT_LOCK_TIMEOUT": "300",
    }


def _probe(env_overrides: dict[str, str], *, install: bool) -> dict[str, str | None]:
    """Run a child that reports the stamps it can still see.

    ``install=True`` makes the child call ``install_hermetic_env()`` first,
    which is the call under test.
    """
    script = (
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(_BACKEND_DIR)!r})\n"
        "from _pytest_hermetic_env import install_hermetic_env\n"
        + ("install_hermetic_env()\n" if install else "")
        + f"print(json.dumps({{v: os.environ.get(v) for v in {list(TASK_SCOPE_VARS)!r}}}))\n"
    )
    env = dict(os.environ)
    env.update(env_overrides)

    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"the probe child failed.\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_stamps_really_do_reach_a_child_process():
    """The control: the setup works.

    Without this, the assertion below could pass because the stamps never
    arrived in the first place -- a misspelled variable name would look
    exactly like a fixed installer.
    """
    seen = _probe(_stamps(), install=False)

    missing = [v for v in TASK_SCOPE_VARS if seen[v] is None]
    assert not missing, (
        f"the probe child did not inherit {missing} -- the setup is broken, "
        f"so the case below would pass vacuously"
    )


def test_installing_the_hermetic_env_drops_the_task_stamps():
    """The rule: the suite does not answer to a task's stamps.

    ``install_hermetic_env()`` is the one installer both pytest roots call
    at import, so this is where the stamps have to go -- and the executor
    launches the suite with them set, so the suite is exactly the process
    that inherits them.
    """
    seen = _probe(_stamps(), install=True)

    survived = {v: seen[v] for v in TASK_SCOPE_VARS if seen[v] is not None}
    assert not survived, (
        f"the hermetic install left the task stamps in place: {survived}.  A "
        f"test process that answers to a task's declaration and marker "
        f"directory turns the suite's own guard tests into undeclared "
        f"writes -- recorded in the executor's channel, and reported as an "
        f"agent violation."
    )
