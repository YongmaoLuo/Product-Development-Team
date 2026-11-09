"""The undeclared-write gate must not fire on a task that did nothing wrong.

Material R1, round 1 — the "existing behaviour must not regress" clause
-----------------------------------------------------------------------

``test_undeclared_write_gate_enforced.py`` pins the gate's MIGHT: a
write outside the declaration fails the task. This file pins the other
half — the gate's MUST NOT: a task whose writes were all legitimate
still completes.

A gate that fires on legitimate work is worse than no gate at all. It
turns every ordinary task into a failure, the operator sees it fire on
work that was fine, and the gate gets switched off — taking the
accident it was built to prevent back with it. So every boundary the
gate could over-reach on gets its own case here, and each case names
exactly ONE boundary:

* ``test_only_declared_changes_still_complete``
  — a change the task DID declare is not an undeclared write;
* ``test_an_empty_declaration_disables_the_gate``
  — "the task declared nothing" is not "the task declared the empty
  set", so an unusable declaration turns the check OFF instead of
  making every changed path an undeclared write;
* ``test_plan_and_tmp_writes_are_not_undeclared``
  — the runtime's own bookkeeping (plan state, scratch space) is not a
  deliverable and is not the sub-agent's fault.

The file is deliberately SEPARATE from the "might" file. A red test
here says "the gate is too wide"; a red test there says "the gate does
not work". Mixing the two in one file makes a red run ambiguous, and
an ambiguous red run is a run nobody can act on.

The harness is NOT copied. ``_execute_task_with_retry`` is driven
through the same construction task 2 built — ``AutonomousAgent.__new__``
plus MagicMock collaborators over a REAL (tmp) git repository — so a
change to the harness cannot leave these cases testing a stale copy of
the executor. The harness lives in the test tree; none of it is copied
into production code.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The harness, reused rather than duplicated. ``repo`` is a pytest
# fixture: importing it by name is what registers it in this module.
from tests.unit.test_undeclared_write_gate_enforced import (  # noqa: E402
    DECLARED_PATH,
    _completed,
    _make_agent,
    _make_task,
    repo,  # noqa: F401  -- imported for the fixture registration
)

#: A plan bookkeeping file the runtime writes on the way past.
PLAN_ARTEFACT = "plans/P-1/state.json"

#: A scratch file the sub-agent (or the runtime) leaves behind.
TMP_ARTEFACT = "tmp/out.log"


def _untracked_paths(repo: Path) -> list[str]:
    """The paths git reports as untracked in ``repo``."""
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def test_only_declared_changes_still_complete(repo: Path):
    """A change the task DID declare must still complete the task.

    The boundary: the comparison against ``files_to_modify`` must let
    the declared path through. A gate that reported it anyway would
    fail every task that did exactly what it was told.
    """
    agent = _make_agent(repo, writes={DECLARED_PATH: "updated\n"})
    task = _make_task([DECLARED_PATH])

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is True, (
        "a task whose only change was declared was failed — the "
        "undeclared-write gate is firing on the declaration itself"
    )
    assert _completed(agent, task), (
        "a task whose only change was declared must be marked completed"
    )
    # The change has to be real, or the case above passed vacuously:
    # a gate with nothing to look at cannot misfire.
    assert (repo / DECLARED_PATH).read_text(encoding="utf-8") == "updated\n", (
        "the declared file was not actually written — this case would "
        "pass without ever exercising the comparison"
    )


def test_an_empty_declaration_disables_the_gate(repo: Path):
    """An empty declaration turns the gate OFF — it does not fail everything.

    The boundary: "the task declared nothing" and "the task declared
    the empty set" are DIFFERENT statements. Only the second one would
    make every changed path an undeclared write. Treating them alike
    would make the gate fire on every task whose ``files_to_modify``
    was never filled in — exactly the task shape the runtime already
    treats as read-only.
    """
    agent = _make_agent(repo, writes={"backend/x.py": "x = 1\n"})
    task = _make_task([])

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is True, (
        "a task with an empty declaration was failed even though it "
        "declared nothing to fail against — the empty declaration must "
        "disable the gate, not arm it"
    )
    assert _completed(agent, task), (
        "a task with an empty declaration must be marked completed"
    )
    assert (repo / "backend" / "x.py").exists(), (
        "the file was never written — this case would pass without ever "
        "exercising the declaration handling"
    )


def test_plan_and_tmp_writes_are_not_undeclared(repo: Path):
    """Plan bookkeeping and scratch space are runtime artefacts, not writes.

    The boundary: the runtime's own bookkeeping (``plans/``) and the
    scratch space (``tmp/``) are not task deliverables. Flagging them
    would make the gate fire on the runtime's own bookkeeping — and a
    gate that always fires is a gate that gets switched off.
    """
    agent = _make_agent(
        repo,
        writes={
            PLAN_ARTEFACT: "{}\n",
            TMP_ARTEFACT: "sub-agent scratch output\n",
        },
    )
    task = _make_task([DECLARED_PATH])

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is True, (
        "a task whose only writes were plan bookkeeping and scratch "
        "space was failed — the gate is judging the runtime's own "
        "artefacts as undeclared writes"
    )
    assert _completed(agent, task), (
        "a task whose only writes were runtime artefacts must be marked "
        "completed"
    )
    # Anti-vacuity: the case only means something if git actually
    # reports the two artefacts as changed. If a future ignore rule
    # hid them, the gate would have had nothing to exclude and this
    # case would have passed for the wrong reason.
    visible = _untracked_paths(repo)
    assert PLAN_ARTEFACT in visible, (
        f"git does not report {PLAN_ARTEFACT} as changed, so the case "
        f"never exercises the exclusion; git reports: {visible!r}"
    )
    assert TMP_ARTEFACT in visible, (
        f"git does not report {TMP_ARTEFACT} as changed, so the case "
        f"never exercises the exclusion; git reports: {visible!r}"
    )