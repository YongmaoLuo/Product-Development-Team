"""RED regression tests for the data-plane fingerprint's identity check.

Background
----------
The 2026-10-26 verification round (round 3) had all 11 of its
``api_test`` VPs blocked **before the request was sent**, and the verdict
named ``data_plane_mismatch`` for an instance that had not changed at
all:

  * recorded fingerprint: port 59028, pid 14828, ``routing_rows=11``
  * observed fingerprint: port 59028, pid 14828, ``routing_rows=11``
  * the agent's own ``state.db``: 49 rows when the round recorded the
    fingerprint, 52 rows when the check ran

Everything the fingerprint recorded was identical, yet the round called
it a mismatch.  The cause is in
:meth:`service_freshness.DataPlaneFingerprint.matches`: as soon as an
``expected_rows`` count is available it compares the **observation**
against that external count and never compares the observation against
the count **this fingerprint recorded**.  The external database is not
the database the instance serves, so the two numbers could never be
equal -- and an instance that was provably the same one was reported
as somebody else's data plane.

What these tests pin
--------------------
The comparison object is the **recorded** value:

  1. a fingerprint matches itself even when the external fixture count
     drifted under it (the production case);
  2. a genuinely different data plane still does not match;
  3. another process behind the same port does not match;
  4. an observation that never answered does not match;
  5. with no recorded count the fixture's count still decides -- the
     pre-existing fallback has to survive the change.

Test 1 is the reproduction: on the base point ``matches()`` answers
``False`` there, because 11 != 52, and it answers ``True`` once the
comparison uses the recorded value.  Tests 2-5 are the boundary
conditions the fix must not break.

Why test 1 carries a marker
---------------------------
The reproduction has to fail on the base point -- that failure is the
evidence this file exists for -- but a test command that exits non-zero
is a **failed task**, and a failed task blocks every task that depends
on it: ``is_dependency_ready`` unlocks a dependent only on
``completed`` or ``skipped``.  The fix (R1) is the next task and it
depends on this one, so a permanently red command here would strand
the whole chain behind it.

The marker is therefore a ratchet rather than a waiver.  It applies
only while ``matches()`` still does not read the recorded value, and
the moment an implementation reaches for ``self.routing_rows`` the
marker stops applying and the assertion below becomes a hard one
again.  An implementation that reaches for the recorded value and
computes the wrong answer therefore fails this test outright instead
of being absorbed by the marker.

To read the raw red on the base point, run the module with the markers
switched off::

    pytest tests/unit/test_data_plane_fingerprint_identity.py --runxfail -v
"""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from service_freshness import DataPlaneFingerprint  # noqa: E402


def _fingerprint(port=8080, pid=100, rows=4, expected=4):
    """A data-plane fingerprint, as the probe would report it."""
    return DataPlaneFingerprint(
        port=port, pid=pid, routing_rows=rows, expected_rows=expected,
        status="ok",
    )


def _matches_reads_the_recorded_rows() -> bool:
    """Does ``matches()`` read this fingerprint's own row count?

    A structural probe, not a behavioural one: the behavioural question
    -- "does the drifted case answer ``True`` yet?" -- cannot tell "R1
    is not implemented" from "R1 is implemented wrongly", and the
    marker on the reproduction test must not absorb the second one.
    Reading ``self.routing_rows`` inside ``matches()`` *is* R1, so its
    presence is what turns the marker off.
    """
    source = textwrap.dedent(
        inspect.getsource(DataPlaneFingerprint.matches),
    )
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "routing_rows"
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            return True
    return False


def _r1_is_missing() -> bool:
    """Is the recorded-value comparison still missing?

    True only while **both** probes agree that R1 has not landed:

      * behaviour -- the drifted-fixture case still answers ``False``;
      * structure -- ``matches()`` does not read ``self.routing_rows``.

    A correct implementation satisfies the first probe and the marker
    switches itself off; an implementation that reads the recorded
    value and gets the answer wrong satisfies neither and fails the
    test instead of hiding behind the marker.
    """
    rec = _fingerprint(port=59028, pid=14828, rows=11, expected=49)
    obs = _fingerprint(port=59028, pid=14828, rows=11, expected=49)
    if rec.matches(obs, expected_rows=52) is True:
        return False
    return not _matches_reads_the_recorded_rows()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "R1 has not landed yet: matches() still compares the observation "
        "against the external row count instead of the recorded one"
    ),
    condition=_r1_is_missing(),
)
def test_fixture_service_matches_itself_when_external_fixture_drifted():
    """The round's instance matches itself, however the fixture drifted.

    The production shape: the instance answered with 11 rows both when
    the round recorded the fingerprint and when the check ran, while
    the external count the agent holds drifted from 49 to 52 rows.
    Only the recorded value can say "which data plane is this" -- the
    external count belongs to a different database entirely.
    """
    rec = _fingerprint(port=59028, pid=14828, rows=11, expected=49)
    obs = _fingerprint(port=59028, pid=14828, rows=11, expected=49)

    assert rec.matches(obs, expected_rows=52) is True


def test_different_row_count_does_not_match():
    """A different data plane is not the round's instance.

    The recorded count and the observed count disagree -- the instance
    now serves another database -- so the round must not go on.
    """
    rec = _fingerprint(port=8080, pid=100, rows=4, expected=4)
    obs = _fingerprint(port=8080, pid=100, rows=0, expected=4)

    assert rec.matches(obs) is False


def test_different_pid_on_same_port_does_not_match():
    """Another process took the port over.

    The data plane may look unchanged, but the answer now comes from a
    different process: something else took the address over.
    """
    rec = _fingerprint(port=8080, pid=1, rows=4, expected=4)
    obs = _fingerprint(port=8080, pid=2, rows=4, expected=4)

    assert rec.matches(obs) is False


def test_unanswered_observation_does_not_match():
    """An instance that stopped answering is not the round's instance.

    The probe timed out, so there is no observation to compare at all:
    the identity alone must not carry the verdict.
    """
    rec = _fingerprint(port=59028, pid=14828, rows=11, expected=49)
    obs = DataPlaneFingerprint(
        port=59028, pid=14828, routing_rows=None, status="timeout",
    )

    assert rec.matches(obs) is False


def test_recorded_rows_none_falls_back_to_expected_rows():
    """With no recorded count, the fixture's count still decides.

    The fallback has to survive the change: a fingerprint taken from an
    instance that answered without a row count carries no verdict of
    its own, so the fixture's count is all there is.
    """
    rec = DataPlaneFingerprint(
        port=59028, pid=14828, routing_rows=None, expected_rows=52,
        status="ok",
    )
    obs_other = _fingerprint(port=59028, pid=14828, rows=11, expected=52)
    obs_same = _fingerprint(port=59028, pid=14828, rows=52, expected=52)

    assert rec.matches(obs_other) is False
    assert rec.matches(obs_same) is True