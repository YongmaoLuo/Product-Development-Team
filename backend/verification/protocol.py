"""Phase protocol — shared contract for the six workflow phase classes.

Per the ``verification_agent.py`` split-out plan, the six workflow-phase
concerns (interview, prd, arch, test, tasks, verification) are extracted
into dedicated phase classes that all implement a common
:class:`Phase` protocol, so the agent module becomes a thin orchestrator
that wires phases instead of inlining them.

The protocol pins the three members every phase must expose:

  * ``name`` — stable label used in logs and ``plan_state``
    transitions.
  * ``can_enter()`` — guard predicate that decides whether the phase is
    reachable from the current plan state. Returns ``True`` to permit
    entry, ``False`` to skip. Default implementation returns ``True``
    (always enterable) so concrete phases can opt in to gating by
    overriding the method.
  * ``run()`` — execute the phase against the current plan state,
    returning a structured result (shape pinned by the follow-up
    migration task, not by this skeleton).

The module also re-exports :class:`backend.verification.base.Phase` as
``Phase`` so callers can ``from backend.verification.protocol import
Phase`` and get the concrete base class (with the ``can_enter`` default
inherited by every concrete phase). A ``typing.Protocol`` definition is
also exported as :class:`PhaseProtocol` for static type-checking
purposes — ``Phase`` is a runtime-compatible implementation of it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .base import Phase

__all__ = ["Phase", "PhaseProtocol"]


@runtime_checkable
class PhaseProtocol(Protocol):
    """Static-typing contract every workflow phase class honours.

    The concrete :class:`Phase` base class (re-exported above as
    ``Phase``) implements this protocol at runtime — every concrete
    phase class subclasses ``Phase`` and so inherits a default
    ``can_enter()`` (returns ``True``) and a skeleton ``run()`` (raises
    :class:`NotImplementedError`).

    Attributes:
        name: Stable label used in logs and ``plan_state`` transitions.
    """

    name: str

    def can_enter(self) -> bool:
        """Decide whether this phase is reachable from current plan state.

        Returns:
            True if the phase may run now, False to skip it.
        """
        ...

    def run(self) -> None:
        """Execute the phase against the current plan state.

        Returns:
            Structured phase result (shape pinned by the follow-up
            migration task; the skeleton raises
            :class:`NotImplementedError`).
        """
        ...
