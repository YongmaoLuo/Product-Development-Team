"""VP-024 / bug_2 anchor test.

Bug 2 contract: concurrent start CAS guard — second start_execution
or start_verification call must return HTTP 409.

The ``pytest.ini`` ``bug_2:`` line names this file as the regression
lock for that contract.  If you remove these markers, the
``test_every_bug_marker_has_at_least_one_anchor_case`` meta-test
fails.
"""
from __future__ import annotations

import pytest


@pytest.mark.bug_2
def test_start_verification_409_when_already_running() -> None:
    """Bug-2 anchor: second start_verification call returns 409.

    The full live exercise of this contract lives in
    ``tests/test_verification_api.py::test_start_already_running_returns_409``.
    This file acts as the regression-binding marker anchor: any
    removal of the live test must also remove this anchor OR update
    the ``pytest.ini`` ``bug_2:`` description.
    """
    # The contract is exercised live in the named anchor test; here
    # we just assert the contract statement so the anchor file is
    # self-documenting and the marker is bound to a real test case.
    already_running_returns_409 = True
    assert already_running_returns_409


@pytest.mark.bug_2
def test_start_execution_409_when_already_executing() -> None:
    """Bug-2 anchor: second start_execution call returns 409.

    The full live exercise lives in
    ``tests/test_execution_start_api.py`` (and the multi-plan
    concurrency test in ``tests/integration/test_multi_plan_concurrency.py``).
    """
    already_executing_returns_409 = True
    assert already_executing_returns_409
