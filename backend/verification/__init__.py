"""backend/verification — verification orchestration + phase classes.

Splitting plan: per ``/.review-findings-hotpath.md`` line 23, the six
workflow-phase concerns inlined in ``backend/verification_agent.py``
(3,618 LOC) are being extracted into phase classes implementing a
common :class:`~backend.verification.base.Phase` protocol, so
``verification_agent.py`` becomes a thin orchestrator that wires
phases:

    * :mod:`backend.verification.base`         — the shared ``Phase``
      protocol base class.
    * :mod:`backend.verification.interview`    — ``InterviewPhase``
      (requirement-clarification concern).
    * :mod:`backend.verification.prd`          — ``PrdPhase`` (PRD
      generation / review-loop concern).
    * :mod:`backend.verification.arch`         — ``ArchPhase``
      (architecture design / review-loop concern).
    * :mod:`backend.verification.test`         — ``TestPhase`` (test
      design / review-loop concern).
    * :mod:`backend.verification.tasks`        — ``TasksPhase`` (task
      generation concern).
    * :mod:`backend.verification.verification` — ``VerificationPhase``
      (verification loop concern).
    * :mod:`backend.verification.phases`       — flat re-export facade
      for the six phase classes.
    * :mod:`backend.verification.orchestrator` — the pre-existing
      ``VerificationOrchestrator`` (moved verbatim from the historical
      ``backend/verification.py`` module so this package could take the
      ``verification`` import name).

Backward-compatibility contract
-------------------------------
``backend/server.py:60`` and three test files import the orchestrator
as ``from verification import VerificationOrchestrator``. Because a
package shadows a same-named module under CPython's FileFinder, the old
``backend/verification.py`` was moved into this package as
``orchestrator.py`` and its public class is re-exported here — the
historical import surface keeps working unchanged.

The phase classes currently expose only skeleton bodies (``run()``
raises :class:`NotImplementedError`) so downstream callers and unit
tests can import and wire them without waiting for the full
implementation. The phase behaviour will be migrated from
``backend/verification_agent.py`` into these classes in follow-up
tasks.
"""

from __future__ import annotations

from . import (
    arch,
    base,
    interview,
    orchestrator,
    phases,
    prd,
    tasks,
    test,
    verification,
)
from .orchestrator import VerificationOrchestrator

__all__ = [
    "base",
    "phases",
    "orchestrator",
    "interview",
    "prd",
    "arch",
    "test",
    "tasks",
    "verification",
    "VerificationOrchestrator",
]
