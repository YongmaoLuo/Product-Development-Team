"""Flat re-export facade for the six workflow phase classes (skeleton).

Skeleton only. Per ``/.review-findings-hotpath.md`` line 23, the six
phase concerns currently inlined in ``backend/verification_agent.py``
(3,618 LOC) are being extracted into dedicated phase classes so the
agent module becomes a thin orchestrator that wires phases. The six
class names are pinned verbatim from the review finding:

    * :class:`InterviewPhase`    — requirement-clarification concern
      (:mod:`backend.verification.interview`)
    * :class:`PrdPhase`          — PRD generation / review-loop concern
      (:mod:`backend.verification.prd`)
    * :class:`ArchPhase`         — architecture design / review-loop
      concern (:mod:`backend.verification.arch`)
    * :class:`TestPhase`         — test design / review-loop concern
      (:mod:`backend.verification.test`)
    * :class:`TasksPhase`        — task generation concern
      (:mod:`backend.verification.tasks`)
    * :class:`VerificationPhase` — verification loop concern
      (:mod:`backend.verification.verification`)

Each class subclasses :class:`backend.verification.base.Phase`, is
defined in its own per-phase submodule (mirroring the
``backend/scheduling/{dispatcher,guard}.py`` one-class-per-module
skeleton pattern), and is re-exported here so callers can use either
the flat ``from verification.phases import PrdPhase`` form or the
per-module ``from verification.prd import PrdPhase`` form.

All ``run()`` bodies (inherited from the ``Phase`` base) raise
:class:`NotImplementedError` so a partially-wired caller fails fast at
the callsite instead of silently returning ``None`` — the real
behaviour migrates out of ``backend/verification_agent.py`` in
follow-up tasks.
"""

from __future__ import annotations

from .arch import ArchPhase
from .interview import InterviewPhase
from .prd import PrdPhase
from .tasks import TasksPhase
from .test import TestPhase
from .verification import VerificationPhase

__all__ = [
    "InterviewPhase",
    "PrdPhase",
    "ArchPhase",
    "TestPhase",
    "TasksPhase",
    "VerificationPhase",
]
