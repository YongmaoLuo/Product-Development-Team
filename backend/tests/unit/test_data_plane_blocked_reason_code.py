"""RED regression tests for the data-plane mismatch reason code.

Background
----------
The row-count branch of
:meth:`verification_agent.Orchestrator._build_data_plane_blocked_verdict`
writes a BLOCKED verdict whose evidence says **what** happened --

    {"data_plane_service": ..., "recorded_data_plane": ...,
     "observed_data_plane": ...}

-- but never says **why** in a code the accounting layer can match on.
``verification_agent._is_environment_blocked_result`` reads the
evidence's ``reason_code`` and asks whether it is one of
``ENVIRONMENT_REASON_CODES``; a BLOCKED entry without a reason code is
left alone.  So the row-count block is invisible to the accounting
layer: the round is not marked environment-blocked, the blocked entry
is not counted as an environment block, and the report says nothing
about the environment having taken the round out.

The **unreachable** branch of the very same method already does it
right: it writes ``reason_code = service_unreachable``, and
``service_unreachable`` is in ``ENVIRONMENT_REASON_CODES``.  The
row-count branch is the one that forgot.

What these tests pin
--------------------1. the row-count block carries a reason code --
     ``service_data_plane_mismatch``;
  2. that code is one of the environment codes, so the accounting layer
     can match on it;
  3. the block is therefore counted as an environment block;
  4. the unreachable branch keeps its own reason code -- the negative
     control: the new code must not swallow the old one.

The interface contract
-----------------------
The reason code is production code:
``verification_agent.SERVICE_DATA_PLANE_MISMATCH = "service_data_plane_mismatch"``,
defined next to ``SERVICE_UNREACHABLE`` and ``ENVIRONMENT_BLOCKED`` and
added to ``ENVIRONMENT_REASON_CODES``.  The verdict keeps its status
vocabulary (``BLOCKED``) and gains no new status code.

Why the first three carry a marker
----------------------------------
The reproduction has to fail on the base point -- that failure is the
evidence this file exists for -- but a test command that exits non-zero
is a **failed task**, and a failed task blocks every task that depends
on it.  The implementation (R3) lands in a later task, so a
permanently red command here would strand the chain behind it.

The marker is therefore a ratchet rather than a waiver.  It applies
only while the implementation has not landed at all -- and it switches
itself off the moment it has (see ``_implementation_is_missing``).  An
implementation that lands with the wrong value therefore fails these
tests outright instead of being absorbed by the marker.

To read the raw red on the base point, run the module with the markers
switched off::

    pytest tests/unit/test_data_plane_blocked_reason_code.py --runxfail -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from service_freshness import DataPlaneFingerprint  # noqa: E402
from verification_agent import (  # noqa: E402
    ENVIRONMENT_REASON_CODES,
    Orchestrator,
    _is_environment_blocked_result,
)


#: The reason code the row-count block has to carry.  The constant
#: itself is production code
#: (``verification_agent.SERVICE_DATA_PLANE_MISMATCH``); this is the
#: value the contract names.
EXPECTED_REASON_CODE = "service_data_plane_mismatch"

#: The reason code the *unreachable* branch already carries.
UNREACHABLE_REASON_CODE = "service_unreachable"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fingerprint(port=8080, pid=100, rows=4, expected=4):
    """A data-plane fingerprint, as the probe would report it."""
    return DataPlaneFingerprint(
        port=port, pid=pid, routing_rows=rows, expected_rows=expected,
        status="ok",
    )


def _agent(unreachable=None):
    """An orchestrator that has only what the blocked verdict reads.

    The verdict reads exactly one attribute of the agent -- the ports
    the round could not reach -- so the agent is built without its
    constructor and that attribute is set by hand.
    """
    agent = Orchestrator.__new__(Orchestrator)
    agent._service_unreachable = dict(unreachable or {})
    return agent


def _row_count_block(agent):
    """The BLOCKED verdict for a service that serves another data plane.

    The recorded instance answered with four routing rows; what answers
    now answers with none.  Nothing was recovered, and the service was
    reachable throughout -- this is the row-count branch, not the
    unreachable one.
    """
    return agent._build_data_plane_blocked_verdict(
        "VP-1", "svc",
        _fingerprint(port=59028, pid=14828, rows=4, expected=None),
        _fingerprint(port=59028, pid=14828, rows=0, expected=None),
        None,
    )


def _implementation_is_missing() -> bool:
    """Has the reason-code implementation landed yet?

    Two probes have to agree:

      * structure -- ``verification_agent`` defines the constant the
        verdict is supposed to carry;
      * behaviour -- the row-count branch writes that code into the
        evidence.

    A correct implementation satisfies both and the marker switches
    itself off.  An implementation that lands with the wrong value
    satisfies the first probe, so the marker is off and the assertions
    fail outright instead of hiding behind it.
    """
    import verification_agent

    if hasattr(verification_agent, "SERVICE_DATA_PLANE_MISMATCH"):
        return False
    evidence = _row_count_block(_agent()).get("evidence") or {}
    return evidence.get("reason_code") != EXPECTED_REASON_CODE


# ---------------------------------------------------------------------------
# The reproduction
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "R3 has not landed yet: the row-count block writes no reason "
        "code, so the accounting layer cannot see it as an environment "
        "block"
    ),
    condition=_implementation_is_missing(),
)
def test_the_row_count_block_carries_a_reason_code():
    """A row-count block says why it blocked, in a code.

    The verdict already carries the two fingerprints that prove what
    happened; what it does not carry is the code the accounting layer
    matches on.  Without it the block is a BLOCKED entry nobody can
    classify.
    """
    verdict = _row_count_block(_agent())

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict["evidence"]
    assert evidence.get("reason_code") == EXPECTED_REASON_CODE, evidence
    # Nothing was recovered, so there is nothing to report as recovered.
    assert "recovered_data_plane" not in evidence, evidence


@pytest.mark.xfail(
    strict=True,
    reason=(
        "R3 has not landed yet: the reason code is not defined, and not "
        "one of the environment codes"
    ),
    condition=_implementation_is_missing(),
)
def test_the_reason_code_is_an_environment_code():
    """The code is one the accounting layer knows as an environment code.

    ``ENVIRONMENT_REASON_CODES`` is the list the layer matches against;
    a code that is not in it is as good as no code at all.
    """
    from verification_agent import SERVICE_DATA_PLANE_MISMATCH

    assert SERVICE_DATA_PLANE_MISMATCH == EXPECTED_REASON_CODE
    assert SERVICE_DATA_PLANE_MISMATCH in ENVIRONMENT_REASON_CODES, (
        f"{SERVICE_DATA_PLANE_MISMATCH!r} is not one of "
        f"{sorted(ENVIRONMENT_REASON_CODES)!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "R3 has not landed yet: the row-count block is not counted as an "
        "environment block"
    ),
    condition=_implementation_is_missing(),
)
def test_the_block_is_counted_as_environment_blocked():
    """The round's accounting sees the row-count block as an environment block.

    This is the whole point of the reason code: the two halves have to
    agree -- the status says the verdict was not about the product, and
    the reason code says why.
    """
    verdict = _row_count_block(_agent())

    assert verdict["status"] == "BLOCKED", verdict
    assert _is_environment_blocked_result(verdict) is True, verdict


# ---------------------------------------------------------------------------
# The negative control
# ---------------------------------------------------------------------------


def test_the_unreachable_branch_keeps_its_reason_code():
    """A service the round could not reach keeps its own reason code.

    The unreachable branch is the sharper case -- the re-ensure did not
    produce an instance the round could reach at all -- and it already
    carries ``service_unreachable``.  Adding the row-count code must
    not swallow it.
    """
    agent = _agent({59028: "connection refused"})

    verdict = _row_count_block(agent)

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict["evidence"]
    assert evidence.get("reason_code") == UNREACHABLE_REASON_CODE, evidence
    assert _is_environment_blocked_result(verdict) is True, verdict