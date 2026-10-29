"""The xfail ratchet of the instance-identity baseline is **closed**.

Background
----------
``tests/unit/test_service_instance_identity_baseline.py`` was written as
a **RED reproduction**: on the base point its assertions failed, and a
strict ``xfail`` marker absorbed them, so that the task chain was not
stranded behind a permanently red command.  The marker is a **ratchet**
rather than a waiver: it applies only while the comparison still uses
the round's own tree and this process's own database, and it switches
itself off the moment the recorded baseline is in.

Since 2026-10-28 the recorded **workspace** is the baseline, so the
ratchet is closed: the assertions of that module are hard ones again.
This file is what keeps it that way.

What this pins
--------------
The baseline module is run in a **subprocess** -- a real pytest run,
because the marker is applied by pytest at **collection** time, so
only pytest can answer what the marker does.  Two runs, two questions:

  * **the ratchet ignored** (``--runxfail``): the module has to pass on
    its own merits -- the exit code.  A module that only passes
    because its failures are absorbed is not a passing module;
  * **the ratchet applied** (the ordinary run): not one ``xfailed``
    and not one ``XPASS``.  This is where an *open* ratchet would
    show up, and it is the question the ``--runxfail`` run cannot
    answer: ``--runxfail`` switches the marker off entirely, so a run
    with it can never report an ``xfailed`` in the first place.

The child gets a throwaway environment of its own: the variables that
key the suite's scratch roots are dropped from it, so the child never
adopts -- and, at teardown, never removes -- the roots of the session
that started it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent

#: The module whose ratchet this file pins.
BASELINE_MODULE = "tests/unit/test_service_instance_identity_baseline.py"

#: The variables that key the suite's scratch roots and the pytest
#: session's own bookkeeping.  They are dropped from the child's
#: environment: the child must mint its own scratch roots and must not
#: remove the ones the parent owns.
_CHILD_ENV_DROP = (
    "PYTEST_ADDOPTS",
    "PYTEST_CURRENT_TEST",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PDT_PLANS_DIR",
    "PDT_STATE_DB_PATH",
    "PDT_TEST_SESSION_ROOT",
    "_PDT_TEST_SESSION_ROOT_OWNER",
)


def _child_env() -> Dict[str, str]:
    """The environment the child pytest run gets.

    A copy of the current environment without the variables that key
    the scratch roots of the session that is running this file.
    """
    return {
        key: value for key, value in os.environ.items()
        if key not in _CHILD_ENV_DROP
    }


def _run_baseline_module(extra_args: List[str]) -> subprocess.CompletedProcess:
    """Run the baseline module in a child pytest, with the backend root as cwd.

    The child is a real pytest run: it collects the module the way the
    suite collects it, so the marker is applied -- or not -- exactly as
    it is in the suite.
    """
    return subprocess.run(
        [
            sys.executable, "-m", "pytest",
            BASELINE_MODULE,
            *extra_args,
            "-q",
        ],
        cwd=str(_BACKEND_DIR),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_the_identity_baseline_module_passes_with_the_ratchet_ignored():
    """The baseline module stands on its own -- and nothing in it xfails.

    The first run switches the marker off: the module has to pass on
    its own merits, with no marker to absorb a failure.

    The second run leaves the marker in place: a **closed** ratchet is
    one that applies to nothing, so the run must report no ``xfailed``
    (an open ratchet absorbing a failure) and no ``XPASS`` (a marker
    that no longer has anything to absorb).
    """
    ignored = _run_baseline_module(["--runxfail"])
    ignored_output = ignored.stdout + ignored.stderr

    assert ignored.returncode == 0, (
        f"关掉标记之后基线模块必须自己站得住，实际退出码是 "
        f"{ignored.returncode}:\n{ignored_output}"
    )
    assert "xfailed" not in ignored_output, (
        f"棘轮必须已经关闭，模块里不该还有 xfail：\n{ignored_output}"
    )
    assert "skipped" not in ignored_output, (
        f"基线模块的断言不许被跳过：\n{ignored_output}"
    )
    passed = re.search(r"(\d+) passed", ignored_output)
    assert passed is not None, (
        f"输出里没有通过数，这一跑没有测到东西：\n{ignored_output}"
    )
    assert int(passed.group(1)) >= 7, (
        f"基线模块的覆盖面被削掉了：只跑了 {passed.group(1)} 条，"
        f"至少要有 7 条：\n{ignored_output}"
    )

    applied = _run_baseline_module([])
    applied_output = applied.stdout + applied.stderr

    assert applied.returncode == 0, (
        f"棘轮关闭之后基线模块必须直接通过，实际退出码是 "
        f"{applied.returncode}:\n{applied_output}"
    )
    assert "xfailed" not in applied_output, (
        f"棘轮必须已经关闭，模块里不该还有 xfail：\n{applied_output}"
    )
    assert "XPASS" not in applied_output, (
        f"棘轮必须已经关闭，模块里不该还有被吸收的断言：\n{applied_output}"
    )
