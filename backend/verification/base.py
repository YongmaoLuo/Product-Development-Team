"""Phase protocol base class (skeleton).

Skeleton only. Per ``/.review-findings-hotpath.md`` line 23, the six
workflow-phase concerns currently inlined in
``backend/verification_agent.py`` (3,618 LOC) are being extracted into
phase classes implementing a common ``Phase`` protocol, so
``verification_agent.py`` can become a thin orchestrator that wires
phases. The concrete phase classes live in
:mod:`backend.verification.phases`; this module pins the shared base.

The ``Phase`` base will eventually own the contract every phase honours:

  * ``name`` — stable label used in logs and ``plan_state`` transitions.
  * ``run()`` — execute the phase against the current plan state,
    returning a structured result (shape pinned by the follow-up
    migration task, not by this skeleton).

The skeleton pins the class name and the ``run()`` entry point — whose
body raises :class:`NotImplementedError` — so a partially-wired caller
fails fast at the callsite instead of silently returning ``None``.
"""

from __future__ import annotations

__all__ = ["Phase"]


class Phase:
    """Common protocol for the six workflow phase classes (skeleton).

    The constructor body is intentionally a no-op. Wiring (plan state
    handle, coding tool, logger) lands in a follow-up task once the
    phase logic is migrated out of ``backend/verification_agent.py``.

    ``run()`` raises :class:`NotImplementedError` — see the module
    docstring for the contract.
    """

    #: Stable label used in logs and plan_state transitions. Concrete
    #: phases override this class attribute.
    name: str = ""

    def __init__(self) -> None:
        """Initialise an empty phase placeholder.

        Accepts no arguments for the skeleton slice so callers can
        begin passing instances around without committing to a final
        constructor signature.
        """

    def can_enter(self) -> bool:
        """Decide whether this phase is reachable from current plan state.

        Default implementation returns ``True`` so every phase is
        considered enterable unless a concrete phase overrides this
        method to add gating logic. The split-out plan pins
        ``can_enter`` as part of the shared :class:`Phase` protocol —
        see :mod:`backend.verification.protocol`.

        Returns:
            True if the phase may run now, False to skip it.
        """
        return True

    def run(self) -> None:
        """Execute the phase against the current plan state.

        Placeholder — implementation lands when the phase logic is
        migrated out of ``backend/verification_agent.py``.

        Raises:
            NotImplementedError: always, in the skeleton slice.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.run is a skeleton — the phase-logic "
            "migration out of backend/verification_agent.py has not "
            "landed yet. See /.review-findings-hotpath.md line 23."
        )
