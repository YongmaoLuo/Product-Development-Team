"""The suite must never materialise a plan directory in the operator's tree.

Why this gate exists
--------------------
``<repo>/plans/`` is *live operator state*: it is where the running
server keeps every plan's artifacts, and the notifier's startup sweep
reads it to decide which plans are in flight.  A directory that appears
there is therefore not litter — it is a claim, made to every reader of
the tree, that a plan exists and may be running.

On 2026-09-24 eight such claims appeared in one operator's tree:
``resume-test-1790215922380`` and seven siblings, each holding a
``plan_state.json`` with ``current_phase: "executing"``.  They were
written by a test helper that derived its plans root from its own
``__file__`` — ``<repo>/plans/<fixture-id>/`` — instead of resolving it
through ``config_paths.resolve_plans_dir()``, and it never cleaned up.
Two pytest runs produced eight directories (four test functions each).
The listing then answered ``status=ready`` with ``current_phase=
executing`` for all eight, and every count of "how many plans are
running?" that keyed on the phase was eight too high.

The repository already owns the defence: ``PDT_PLANS_DIR`` redirects the
whole tree for a test session, and ``config_paths.resolve_plans_dir()``
is the one resolver every leaf goes through.  What was missing is a
check that the defence still holds — nothing was watching the operator's
tree, so a helper that resolved its root any other way wrote into it
without a single test turning red.

What this pins
--------------
1. **The root in force under the suite is not the operator's tree.**
   ``resolve_plans_dir()`` must land outside ``<repo>``; otherwise every
   fixture the suite materialises is a claim about a real plan.
2. **Materialising a plan fixture leaves the operator's tree alone.**
   The fixture is written through the production resolver, in the exact
   shape the 2026-09-24 leftovers had (three JSON files, a
   ``resume-test-`` id), and the operator's tree is compared before and
   after.
3. **Nothing added to the operator's tree during the whole session.**
   The session-scoped fixture snapshots the tree when the session
   starts and compares it again when the session ends, so a helper that
   leaks in a test this file does not know about is still caught.

Scope, stated honestly
----------------------
This compares directory *names*.  It cannot see a write that lands
inside a directory that already existed, and it cannot tell a leaked
directory from one the operator created while the suite was running —
the operator's own tree is compared, not a copy of it.  Both limits are
stated rather than papered over: the second one is why the assertion
names what it saw instead of claiming the tree was untouched.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

import config_paths

pytestmark = pytest.mark.unit


#: The operator's own plans tree — the directory a run of the server (not
#: of the suite) writes plan artifacts into.  ``config_paths.PLANS_DIR`` is
#: the *default* root, deliberately not ``resolve_plans_dir()``: this file
#: is about the tree a production run would use.
OPERATOR_PLANS_DIR: Path = config_paths.PLANS_DIR


def _plan_dir_names() -> set[str]:
    """The names of the plan directories in the operator's tree."""
    if not OPERATOR_PLANS_DIR.is_dir():
        return set()
    return {p.name for p in OPERATOR_PLANS_DIR.iterdir() if p.is_dir()}


@pytest.fixture(scope="session")
def operator_plans_tree_at_session_start():
    """Snapshot the operator's tree, and hold it still for the session.

    The teardown half is the part that matters: a helper that leaks
    while a LATER test runs is caught here, which is the only way to see
    a leak this file does not itself perform.
    """
    before = _plan_dir_names()
    yield before
    after = _plan_dir_names()
    added = sorted(after - before)
    assert not added, (
        f"the test suite added {added} to the operator's plans tree "
        f"({OPERATOR_PLANS_DIR}). Every directory there is read by the "
        f"server's startup sweep as a plan that may be in flight, so a "
        f"fixture written here is a claim about a real plan. Resolve the "
        f"plans root through ``config_paths.resolve_plans_dir()`` instead "
        f"of deriving it from ``__file__``, and clean up what you create."
    )


def test_a_plan_fixture_never_lands_in_the_operator_plans_tree(
    operator_plans_tree_at_session_start,
):
    """Materialise a plan fixture; prove the operator's tree never sees it."""
    # 1. Materialise a fixture in the shape the 2026-09-24 leftovers had:
    #    a ``resume-test-`` id and the three JSON files they carried.  The
    #    root is the one the production resolver hands out — the same root
    #    every leaf helper uses.
    plan_id = f"resume-test-{int(time.time() * 1000)}"
    effective_root = config_paths.resolve_plans_dir()
    plan_dir = effective_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    try:
        (plan_dir / "plan_state.json").write_text(
            json.dumps({
                "plan_id": plan_id,
                "current_phase": "executing",
                "completed_phases": ["interview", "prd_generation", "task_generation"],
                "review_rounds": {"prd": 1, "arch": 0, "test": 0},
                "flags": {"arch_enabled": False, "test_enabled": False},
            }),
            encoding="utf-8",
        )
        (plan_dir / "tasks.json").write_text(
            json.dumps({"tasks": []}), encoding="utf-8",
        )
        (plan_dir / "execution.json").write_text(
            json.dumps({
                "status": "running", "started_at": "2026-09-24T02:12:02Z",
                "ended_at": None, "pid": None, "stop_reason": None,
                "project_dir": "/tmp/nonexistent",
            }),
            encoding="utf-8",
        )

        # 2. The fixture exists — where it was asked to exist.
        assert plan_dir.is_dir()

        # 3. And the operator's tree never saw it.  This is the assertion
        #    that matters: it is what a helper that resolved its root any
        #    other way than through the resolver would break.
        assert plan_id not in _plan_dir_names(), (
            f"{plan_id} appeared in the operator's plans tree "
            f"({OPERATOR_PLANS_DIR}) — the fixture was written where the "
            f"server reads plans from."
        )
        assert not any(
            name.startswith("resume-test-") for name in _plan_dir_names()
        ), (
            f"a ``resume-test-*`` directory is in the operator's plans tree "
            f"({OPERATOR_PLANS_DIR}): {sorted(_plan_dir_names())}"
        )

        # 4. The root the suite resolved is not the operator's tree at
        #    all — stated separately because it is the cause of the two
        #    assertions above, and because it is the thing a reader of
        #    this file needs to know to fix a leak.
        assert effective_root != OPERATOR_PLANS_DIR, (
            f"the suite resolves the plans root to the operator's own tree "
            f"({OPERATOR_PLANS_DIR}). Every fixture it materialises would be "
            f"a claim about a real plan."
        )
        assert not effective_root.is_relative_to(config_paths.PROJECT_ROOT), (
            f"the suite resolves the plans root to {effective_root}, which is "
            f"inside the repository ({config_paths.PROJECT_ROOT}). The "
            f"``PDT_PLANS_DIR`` redirect is what keeps plan fixtures out of "
            f"the operator's tree."
        )
    finally:
        shutil.rmtree(plan_dir, ignore_errors=True)
