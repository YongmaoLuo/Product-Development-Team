"""Requirement-clarification phase class (skeleton).

Skeleton only — see :mod:`backend.verification.phases` for the
splitting-plan context. The real behaviour migrates out of
``backend/verification_agent.py`` in a follow-up task; ``run()``
inherited from :class:`backend.verification.base.Phase` raises
:class:`NotImplementedError` so a partially-wired caller fails fast.
"""

from __future__ import annotations

from .base import Phase

__all__ = ["InterviewPhase"]


class InterviewPhase(Phase):
    """Requirement-clarification phase concern (skeleton).

    Placeholder — implementation lands when the phase logic is migrated
    out of ``backend/verification_agent.py``.
    """

    name = "interview"
