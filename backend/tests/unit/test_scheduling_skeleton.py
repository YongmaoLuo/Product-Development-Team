"""TDD spec — backend/scheduling/{dispatcher,guard}.py skeleton modules.

Background
----------
The dispatcher refactor splits ``AutonomousAgent._run_async`` (a ~380 LOC
inline ``while True`` loop in ``backend/agent.py``) into three orthogonal
collaborators, per ``/.review-findings-hotpath.md`` line 21 and the
``AutonomousAgent.__init__`` docstring at ``backend/agent.py:1481-1499``:

  * ``guard`` — coarse-grained validator (cycle, same-id loop,
    file-modify contract) called once per tick before the dispatcher
    picks the next task.
  * ``dispatcher`` — per-tick loop controller: picks the next eligible
    task, invokes the executor, persists the result via the
    runtime_state handle.
  * ``runtime_state`` — typed wrapper around the
    ``plan_verification.runtime_state`` SQLite JSON column (already
    landed as ``backend/runtime_state.py``).

``AutonomousAgent.__init__`` was extended in task-36 (commit ``5adf23e1``)
to accept all three as optional kwargs. This task (the current one) lays
down the two new ``backend/scheduling/`` submodules as **skeletons** —
the bodies are intentionally absent so the import surface is stable
before downstream tasks wire real logic into them. Pattern matches
task-40-1-2 (commit ``66631ac7``) which laid down the ``backend/api/``
skeleton the same way.

TDD contract — 4 test cases
---------------------------
The skeleton contract is deliberately minimal so the import surface
stays stable across follow-up tasks that may add/reshape method bodies:

1. ``test_scheduling_package_importable`` — ``import scheduling`` works
   and exposes ``dispatcher`` / ``guard`` submodules via the package
   ``__init__.py`` re-export.
2. ``test_dispatcher_module_exposes_dispatcher_class`` —
   ``scheduling.dispatcher.Dispatcher`` exists, is a class, and is
   listed in the module's ``__all__``.
3. ``test_guard_module_exposes_guard_class`` —
   ``scheduling.guard.Guard`` exists, is a class, and is listed in the
   module's ``__all__``.
4. ``test_skeleton_classes_instantiable_with_no_args`` — both classes
   accept a no-arg constructor so callers (``AutonomousAgent.__init__``
   via the ``guard=``/``dispatcher=`` kwargs, plus unit tests) can
   start passing instances around without committing to a final
   constructor signature.

These tests deliberately avoid asserting specific method names
(``start_layer``/``next_layer``/``mark_completed``/``validate``) —
those are downstream tasks' scope. The skeleton slice pins only the
package + class import surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure backend/ is on sys.path so ``import scheduling`` works regardless
# of the test runner entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Test 1 — package and submodules are importable
# ---------------------------------------------------------------------------


def test_scheduling_package_importable():
    """``backend/scheduling/`` is a Python package exposing both submodules.

    The dispatcher/guard refactor requires a stable import path so that
    ``agent.py`` can later do ``from scheduling.dispatcher import
    Dispatcher`` without a try/except ImportError shim. The package
    ``__init__.py`` must re-export the two submodules so callers can
    reach them as ``scheduling.dispatcher`` / ``scheduling.guard``.
    """
    import scheduling  # noqa: F401 — presence is the assertion

    # Both submodules must be reachable through the package namespace
    # (the __init__.py re-exports them).
    assert hasattr(scheduling, "dispatcher"), (
        "scheduling package must re-export the dispatcher submodule"
    )
    assert hasattr(scheduling, "guard"), (
        "scheduling package must re-export the guard submodule"
    )
    # ``__all__`` should also advertise the two names so
    # ``from scheduling import *`` keeps working.
    assert "dispatcher" in scheduling.__all__, (
        f"scheduling.__all__ must list 'dispatcher', got "
        f"{scheduling.__all__!r}"
    )
    assert "guard" in scheduling.__all__, (
        f"scheduling.__all__ must list 'guard', got "
        f"{scheduling.__all__!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Dispatcher class exists with the documented export contract
# ---------------------------------------------------------------------------


def test_dispatcher_module_exposes_dispatcher_class():
    """``scheduling.dispatcher.Dispatcher`` is the per-tick loop
    controller placeholder.

    The class is the future owner of the ``_run_async`` per-tick loop
    (next-task selection, executor invocation, runtime_state
    persistence). The skeleton pins the class name and module-level
    ``__all__`` so downstream callers have a stable target before the
    real implementation lands.
    """
    from scheduling import dispatcher as dispatcher_mod

    assert hasattr(dispatcher_mod, "Dispatcher"), (
        "scheduling.dispatcher must expose a Dispatcher class"
    )
    cls = dispatcher_mod.Dispatcher
    assert isinstance(cls, type), (
        f"Dispatcher must be a class (got {type(cls).__name__})"
    )
    assert "Dispatcher" in dispatcher_mod.__all__, (
        f"scheduling.dispatcher.__all__ must list 'Dispatcher', got "
        f"{dispatcher_mod.__all__!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Guard class exists with the documented export contract
# ---------------------------------------------------------------------------


def test_guard_module_exposes_guard_class():
    """``scheduling.guard.Guard`` is the coarse-grained per-tick validator
    placeholder.

    Per ``backend/agent.py:1489`` the guard covers three responsibilities:
    cycle detection, same-id loop detection, and the file-modify contract.
    The skeleton pins the class name and module-level ``__all__`` so the
    future ``AutonomousAgent`` callsite has a stable target.
    """
    from scheduling import guard as guard_mod

    assert hasattr(guard_mod, "Guard"), (
        "scheduling.guard must expose a Guard class"
    )
    cls = guard_mod.Guard
    assert isinstance(cls, type), (
        f"Guard must be a class (got {type(cls).__name__})"
    )
    assert "Guard" in guard_mod.__all__, (
        f"scheduling.guard.__all__ must list 'Guard', got "
        f"{guard_mod.__all__!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — skeleton classes are instantiable with no args
# ---------------------------------------------------------------------------


def test_skeleton_classes_instantiable_with_no_args():
    """Both skeleton classes accept a no-arg constructor.

    The skeleton slice deliberately keeps the constructor signature
    empty so callers (``AutonomousAgent.__init__`` via the
    ``guard=``/``dispatcher=`` kwargs, plus unit tests) can start
    passing instances around without committing to the final wiring.
    Real wiring (runtime_state handle, task repository, executor,
    logger) lands in a follow-up task once the per-tick logic is
    migrated out of ``AutonomousAgent._run_async``.
    """
    from scheduling.dispatcher import Dispatcher
    from scheduling.guard import Guard

    dispatcher = Dispatcher()
    assert isinstance(dispatcher, Dispatcher)

    guard = Guard()
    assert isinstance(guard, Guard)
