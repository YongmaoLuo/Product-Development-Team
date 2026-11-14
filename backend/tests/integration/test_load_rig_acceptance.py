"""Acceptance: the CPU load rig really loads the host, and it really gives it back.

Why this file exists
--------------------
Tasks 1 and 2 built the rig (``backend/tests/_load_rig.py``) and the one
gate that guards it (``backend/tests/_host_load.py``).  Task 3 forbade the
hand-rolled busy loop, and task 4 wrote down which lane owns a load case.
All four are unit-level statements about the parts.

None of them is the statement a reader actually wants answered: *does the
thing work?*  A rig that starts its workers and then idles satisfies
every assertion about "the requested number of workers was started"; a
reclamation path that only runs when nothing goes wrong satisfies every
assertion about "the block ended cleanly".  So this file exercises the
rig the way a case would use it — start it, let it burn CPU, check that
the host really got busy, break the block, and check that the host got
its cores back.

And one more thing, which is the reason this file exists rather than
another unit test next to the rig: **where the file lives is part of the
contract**.  A load case that the fast lane collects runs beside the
parallel unit lane, and a wall-clock measurement taken there measures the
machine rather than the code under test.  So the lane partition is
asserted here, from the inside, rather than assumed:

* the fast lane's selection (``-m "not time_sensitive"``) must collect
  nothing from this module — not one case;
* the wall-clock lane's selection (``-m time_sensitive``) must collect
  all of them.

Both are checked by asking a *second* pytest what it would collect.  The
question "would the fast lane pick this up?" has exactly one authoritative
answer — the answer the lane's own marker expression gives — so it is
asked of pytest itself rather than of a re-implementation of the
partition.

Why the nested runs only ever collect
-------------------------------------
Both nested runs pass ``--collect-only``.  Without it they would *run*
the module, and one of the cases in it is the one that starts the nested
run: the wall-clock case would collect-and-then-execute this module,
which would collect-and-then-execute this module, without end.  So
``--collect-only`` is not an optimisation here, it is what keeps the
recursion bounded.  The selection is a question about *collection*, and
collection is exactly what ``--collect-only`` reports.

Why the child gets a clean environment
--------------------------------------
The child inherits the environment of the pytest run that started it, and
that environment carries the *parent session's* scratch roots and pytest's
own bookkeeping.  A child that adopted the parent's session root would
delete it on exit — the parent's state database with it — which shows up
much later as an unrelated failure in a case that has nothing to do with
this one.  So the child drops those variables and mints its own, which is
the arrangement the suite already uses for its other nested runs.

Why the markers are what they are
---------------------------------
``integration`` — the cases start real subprocesses.

``time_sensitive`` — the rig makes the host busy on purpose, so this
module belongs to the lane that runs with the machine to itself; and the
lane membership is itself part of what is under test here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests._load_rig import cpu_load


pytestmark = [pytest.mark.integration, pytest.mark.time_sensitive]


#: This file, by absolute path.  The nested pytest is pointed at it by
#: path rather than by name, so it collects this module and nothing else.
_THIS_FILE = Path(__file__).resolve()

#: The backend directory — the nested pytest runs with this as its working
#: directory, so it resolves ``backend/pytest.ini`` (and with it the
#: suite's ``pythonpath``) the same way a lane run does.
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: Every case in this module, by name.  The two lane assertions read this
#: tuple: the fast lane must name none of them, the wall-clock lane must
#: name all of them.  Keeping the list here means a case that is added to
#: this module without being added to the tuple is caught by the lane
#: assertions rather than quietly slipping into one lane only.
_CASES = (
    "test_the_rig_burns_cpu_end_to_end",
    "test_an_exception_in_the_block_still_reclaims_every_worker",
    "test_the_fast_lane_selection_excludes_this_module",
    "test_the_wall_clock_lane_selects_this_module",
)

#: The variables that key the suite's scratch roots and the pytest
#: session's own bookkeeping.  They are dropped from the child's
#: environment: the child must mint its own scratch roots and must not
#: take the parent's ones away with it when it exits.
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


def _child_env() -> dict:
    """The environment the nested pytest run gets.

    The parent's copy of the session's own bookkeeping is dropped, so the
    child answers for itself and leaves the parent's scratch alone.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _CHILD_ENV_DROP
    }


def _collect_only(marker_expression: str) -> subprocess.CompletedProcess:
    """Ask a second pytest what ``-m <marker_expression>`` would collect.

    Only ever *collects*: the run is pointed at this module, and this
    module contains the case that starts the run, so executing it would
    recurse without end.  ``--collect-only`` is what keeps the recursion
    bounded, and it answers the question the lane partition actually
    asks — which cases the marker expression selects.
    """
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(_THIS_FILE),
            "-m",
            marker_expression,
            "--collect-only",
            "-q",
        ],
        cwd=str(_BACKEND_DIR),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_rig_burns_cpu_end_to_end() -> None:
    """The rig makes the host busy — a rig that started and then idled would pass the rest.

    The rig's own counter file is the evidence: the workers increment a
    slot each and the parent reads the slots, so "the host was busy" is
    a measurement rather than a claim about how long the block slept.

    A host the gate refuses is *skipped*, not failed: the rig asks
    ``tests._host_load`` before it starts anything, and a busy machine
    means the case did not run rather than that the rig misbehaved.
    """
    with cpu_load(workers=2) as rig:
        time.sleep(0.3)
        assert rig.iterations() > 0


def test_an_exception_in_the_block_still_reclaims_every_worker() -> None:
    """A block that ends by raising leaves no worker behind.

    The reclamation path that runs on the way out of a clean block is not
    the one that runs on the way out of a failing block, and the failing
    one is the one nobody exercises until the day it matters.  So the
    block is broken deliberately here, and the workers are then asked for
    by pid: ``os.kill(pid, 0)`` delivers no signal but the kernel still
    looks the pid up, so a pid that no longer exists raises
    ``ProcessLookupError``.  A worker that was stopped but not reaped
    would still answer.
    """
    pids: list[int] = []
    with pytest.raises(RuntimeError):
        with cpu_load(workers=2) as rig:
            pids = list(rig.pids)
            # The workers are alive right now, and that is what makes the
            # check below mean something: "the pid is gone afterwards" is
            # only news if the pid was there before.  Without this the
            # assertion could pass on a rig that never started a worker.
            for pid in pids:
                os.kill(pid, 0)
            raise RuntimeError("the block failed on purpose")

    assert len(pids) == 2
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_the_fast_lane_selection_excludes_this_module() -> None:
    """The fast lane must collect nothing from this module.

    pytest exits 5 for "no tests were collected", which is a different
    answer from "the lane ran and this module was not in it" — so the
    exit status is asserted, not just the output.

    The output is checked for the absence of this module's case names as
    well, and not only for the absence of ``::test_``: the shape of
    pytest's collection report is a detail, and a check that only looks
    for one spelling of a node id would pass on a report that simply
    spells it differently.
    """
    proc = _collect_only("not time_sensitive")

    assert proc.returncode == 5, (
        "the fast lane's selection collected something from this module "
        f"(exit {proc.returncode}, expected 5 = nothing collected).\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "::test_" not in proc.stdout, (
        "the fast lane's selection reported a test from this module.\n"
        f"--- stdout ---\n{proc.stdout}"
    )
    for name in _CASES:
        assert name not in proc.stdout, (
            f"the fast lane's selection reported '{name}' from this module. "
            "A load case in the fast lane measures the machine, not the "
            "code under test.\n"
            f"--- stdout ---\n{proc.stdout}"
        )


def test_the_wall_clock_lane_selects_this_module() -> None:
    """The wall-clock lane must collect every case in this module.

    This is the other half of the partition: the load belongs to the
    lane that runs with the machine to itself, and a case that no lane
    collects is a case that never runs at all.

    The nested run collects and does not execute — see
    ``_collect_only`` — so this case cannot recurse into itself.
    """
    proc = _collect_only("time_sensitive")

    assert proc.returncode == 0, (
        "the wall-clock lane's selection failed to collect this module "
        f"(exit {proc.returncode}).\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    for name in _CASES:
        assert name in proc.stdout, (
            f"the wall-clock lane's selection did not report '{name}'. A "
            "case no lane collects is a case that never runs.\n"
            f"--- stdout ---\n{proc.stdout}"
        )