"""TDD spec — backend/verification/ package skeleton with 6 phase classes.

Background
----------
Per ``/.review-findings-hotpath.md`` line 23, ``backend/verification_agent.py``
(3,618 LOC) mixes the six workflow-phase orchestration concerns plus
``asyncio.sleep`` test fakes in a single module, with phase boundaries not
enforced at the type level. The pinned fix is to extract phase classes —
``InterviewPhase``, ``PrdPhase``, ``ArchPhase``, ``TestPhase``,
``TasksPhase``, ``VerificationPhase`` — implementing a common ``Phase``
protocol, so ``verification_agent.py`` can later become a thin orchestrator
that wires phases.

This task lays down the ``backend/verification/`` package as a **skeleton**:
the phase-class bodies are intentionally absent (each ``run()`` raises
``NotImplementedError``) so the import surface is stable before follow-up
tasks migrate real logic into them. Pattern matches the
``backend/scheduling/`` skeleton (task-scheduling-skeleton, commit
``37db8f04``) and the ``backend/api/`` skeleton (task-40-1-2, commit
``66631ac7``).

Name-collision contract
-----------------------
``backend/verification.py`` already existed and is imported as
``from verification import VerificationOrchestrator`` by
``backend/server.py:60`` and three test files
(``tests/test_verification_orchestrator.py``,
``tests/test_verification_state_machine.py``,
``tests/e2e/test_verification_e2e_dryrun.py``). A ``backend/verification/``
package shadows a same-named module under CPython's FileFinder (packages win
over ``.py`` files), so the old module is moved into the package as
``verification/orchestrator.py`` and re-exported from the package
``__init__.py`` — keeping the historical import surface working unchanged.

TDD contract — 6 test cases
---------------------------
1. ``test_verification_package_importable`` — ``import verification`` works,
   is a package (``__path__`` present), and re-exports the ``base`` /
   ``phases`` / ``orchestrator`` submodules via the package ``__init__.py``.
2. ``test_orchestrator_backcompat_import_surface`` —
   ``from verification import VerificationOrchestrator`` still resolves to a
   class, preserving the server.py / test-suite import contract.
3. ``test_base_module_exposes_phase_protocol`` — ``verification.base.Phase``
   exists, is a class, and is listed in the module's ``__all__``.
4. ``test_phases_module_exposes_six_phase_classes`` — all six phase classes
   exist in ``verification.phases`` and are listed in ``__all__``.
5. ``test_phase_classes_subclass_phase_protocol`` — each of the six classes
   is a subclass of ``verification.base.Phase``.
6. ``test_phase_skeletons_instantiable_and_run_not_implemented`` — each phase
   accepts a no-arg constructor, exposes a distinct ``name`` class attribute,
   and ``run()`` raises ``NotImplementedError`` so a partially-wired caller
   fails fast instead of silently returning ``None``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure backend/ is on sys.path so ``import verification`` works regardless
# of the test runner entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


PHASE_CLASS_NAMES = [
    "InterviewPhase",
    "PrdPhase",
    "ArchPhase",
    "TestPhase",
    "TasksPhase",
    "VerificationPhase",
]


# ---------------------------------------------------------------------------
# Test 1 — package and submodules are importable
# ---------------------------------------------------------------------------


def test_verification_package_importable():
    """``backend/verification/`` is a Python package exposing its submodules.

    The phase-extraction refactor requires a stable import path so that
    ``verification_agent.py`` can later do
    ``from verification.phases import InterviewPhase, ...`` without a
    try/except ImportError shim. The package ``__init__.py`` must re-export
    the ``base``, ``phases`` and ``orchestrator`` submodules so callers can
    reach them as ``verification.base`` / ``verification.phases`` /
    ``verification.orchestrator``.
    """
    import verification  # noqa: F401 — presence is the assertion

    assert hasattr(verification, "__path__"), (
        "verification must be a package (directory), not a module — the "
        "backend/verification.py module should have been moved into the "
        "package as verification/orchestrator.py"
    )
    for submodule in ("base", "phases", "orchestrator"):
        assert hasattr(verification, submodule), (
            f"verification package must re-export the {submodule} submodule"
        )
        assert submodule in verification.__all__, (
            f"verification.__all__ must list {submodule!r}, got "
            f"{verification.__all__!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 — backward-compatible VerificationOrchestrator import surface
# ---------------------------------------------------------------------------


def test_orchestrator_backcompat_import_surface():
    """``from verification import VerificationOrchestrator`` keeps working.

    ``backend/server.py:60`` and three test files use exactly this import
    form. Moving ``backend/verification.py`` into the package must not break
    them — the package ``__init__.py`` re-exports the class from
    ``verification.orchestrator``.
    """
    from verification import VerificationOrchestrator

    assert isinstance(VerificationOrchestrator, type), (
        f"VerificationOrchestrator must be a class (got "
        f"{type(VerificationOrchestrator).__name__})"
    )
    assert VerificationOrchestrator.__module__ == "verification.orchestrator", (
        f"VerificationOrchestrator should now live in "
        f"verification.orchestrator, got {VerificationOrchestrator.__module__!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Phase protocol base class exists with the export contract
# ---------------------------------------------------------------------------


def test_base_module_exposes_phase_protocol():
    """``verification.base.Phase`` is the common phase-protocol placeholder.

    Per ``/.review-findings-hotpath.md`` line 23 the six phase classes must
    implement a shared ``Phase`` protocol. The skeleton pins the base-class
    name and module-level ``__all__`` so the six phase classes have a stable
    target to subclass before real behaviour is migrated in.
    """
    from verification import base as base_mod

    assert hasattr(base_mod, "Phase"), (
        "verification.base must expose a Phase class"
    )
    cls = base_mod.Phase
    assert isinstance(cls, type), (
        f"Phase must be a class (got {type(cls).__name__})"
    )
    assert "Phase" in base_mod.__all__, (
        f"verification.base.__all__ must list 'Phase', got "
        f"{base_mod.__all__!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — all six phase classes exist with the export contract
# ---------------------------------------------------------------------------


def test_phases_module_exposes_six_phase_classes():
    """``verification.phases`` exposes the six phase-class placeholders.

    The six class names are pinned verbatim from the fix recommendation in
    ``/.review-findings-hotpath.md`` line 23: ``InterviewPhase``,
    ``PrdPhase``, ``ArchPhase``, ``TestPhase``, ``TasksPhase`` and
    ``VerificationPhase``. The skeleton pins the class names and
    module-level ``__all__`` so the future thin-orchestrator callsite has a
    stable target.
    """
    from verification import phases as phases_mod

    for name in PHASE_CLASS_NAMES:
        assert hasattr(phases_mod, name), (
            f"verification.phases must expose a {name} class"
        )
        cls = getattr(phases_mod, name)
        assert isinstance(cls, type), (
            f"{name} must be a class (got {type(cls).__name__})"
        )
        assert name in phases_mod.__all__, (
            f"verification.phases.__all__ must list {name!r}, got "
            f"{phases_mod.__all__!r}"
        )


# ---------------------------------------------------------------------------
# Test 5 — phase classes subclass the Phase protocol
# ---------------------------------------------------------------------------


def test_phase_classes_subclass_phase_protocol():
    """Each of the six phase classes subclasses ``verification.base.Phase``.

    The refactor goal is a thin orchestrator that treats phases
    polymorphically; the skeleton enforces the inheritance edge now so a
    later migration cannot accidentally introduce a phase class outside the
    protocol.
    """
    from verification.base import Phase
    from verification import phases as phases_mod

    for name in PHASE_CLASS_NAMES:
        cls = getattr(phases_mod, name)
        assert issubclass(cls, Phase), (
            f"{name} must subclass verification.base.Phase"
        )
        assert cls is not Phase, (
            f"{name} must be a distinct subclass, not Phase itself"
        )


# ---------------------------------------------------------------------------
# Test 6 — skeletons instantiable with no args; run() raises
# ---------------------------------------------------------------------------


def test_phase_skeletons_instantiable_and_run_not_implemented():
    """Each phase class accepts a no-arg constructor and fails fast on run().

    The skeleton slice deliberately keeps the constructor signature empty so
    callers can start passing instances around without committing to the
    final wiring (plan state handle, coding tool, logger). ``run()`` raises
    ``NotImplementedError`` so a partially-wired caller fails fast at the
    callsite instead of silently returning ``None``. Each phase also exposes
    a distinct ``name`` class attribute so logs / state transitions have a
    stable label.
    """
    import pytest

    from verification import phases as phases_mod

    seen_names = set()
    for name in PHASE_CLASS_NAMES:
        cls = getattr(phases_mod, name)
        instance = cls()
        phase_name = getattr(instance, "name", None)
        assert isinstance(phase_name, str) and phase_name, (
            f"{name}.name must be a non-empty string, got {phase_name!r}"
        )
        assert phase_name not in seen_names, (
            f"{name}.name {phase_name!r} collides with another phase's name"
        )
        seen_names.add(phase_name)
        with pytest.raises(NotImplementedError):
            instance.run()
