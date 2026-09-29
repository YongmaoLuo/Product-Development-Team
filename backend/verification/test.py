"""Test-design phase class (skeleton).

Skeleton only — see :mod:`backend.verification.phases` for the
splitting-plan context. The real behaviour migrates out of
``backend/verification_agent.py`` in a follow-up task; ``run()``
inherited from :class:`backend.verification.base.Phase` raises
:class:`NotImplementedError` so a partially-wired caller fails fast.

Note: the module name ``test.py`` does not collide with pytest
collection — the default ``python_files`` glob is ``test_*.py``, which
``test.py`` does not match, and absolute-import rules keep the stdlib
``test`` module unaffected.
"""

from __future__ import annotations

from .base import Phase

__all__ = ["TestPhase"]


class TestPhase(Phase):
    """Test design / review-loop phase concern (skeleton).

    Placeholder — implementation lands when the phase logic is migrated
    out of ``backend/verification_agent.py``.
    """

    name = "test"
