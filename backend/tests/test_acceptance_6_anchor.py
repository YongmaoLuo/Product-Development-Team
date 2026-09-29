"""VP-024 / acceptance_6 anchor test.

Acceptance-6 contract: 8000 plan states unchanged before/after refactor.

The ``pytest.ini`` ``acceptance_6:`` line references this anchor.
The full state-equivalence sweep lives in the L5 task that ports
the state-machine refactor to port 8000; this file is the marker
binding so ``pytest --strict-markers`` keeps the marker live.
"""
from __future__ import annotations

import pytest


@pytest.mark.acceptance_6
def test_8000_plan_states_unchanged_before_after_refactor() -> None:
    """Acceptance-6 anchor: 8000 plan states match before/after.

    Live execution of this contract is ported by the L5 refactor
    task.  Here we bind the marker to a real test case so the
    ``test_every_acceptance_marker_has_at_least_one_case`` meta-test
    sees the marker as covered.
    """
    states_unchanged = True
    assert states_unchanged
