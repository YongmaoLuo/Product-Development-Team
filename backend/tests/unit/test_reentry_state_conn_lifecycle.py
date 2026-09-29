"""2026-09-15 regression — state.db connection lifetime across the
post-repair re-entry (``_on_repair_complete``).

Live bug being pinned: the re-entry block did

    _vc, _, repo = _open_verification_state()
    _vc.close()                     # ← repo is BOUND to this conn
    VerificationOrchestrator(..., verif_repo=repo)

so the whole next round wrote through a dead handle. Symptom (a production plan,
every round that followed a repair): ``update_progress_state failed …
ProgrammingError('Cannot operate on a closed database.')`` on a ~30 s
cadence for the entire round — the per-VP progress the card reads froze,
and the plan dropped out of ``/api/system/active`` while it was running.

The fix routes the binding through ``_bind_verification_state_conn``
(keep the handle, close the PREVIOUS one) and releases it after the
callback returns via ``_release_verification_state_conn``. Both helpers
are module-level and therefore directly testable — the callback itself
is an unreachable closure, and the existing "chain" tests exercise a
re-implementation shim rather than the real body.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import server


@pytest.fixture(autouse=True)
def _clean_state():
    """Never leak the plan slot into other tests."""
    yield
    for pid in ("p-reentry", "p-reentry-2"):
        server._verification_state.pop(pid, None)


def test_bind_keeps_the_new_connection_open():
    """The connection handed to the new round must stay open — that is
    the whole point of the fix (closing it killed the round's writes)."""
    conn = MagicMock(name="reentry-conn")

    server._bind_verification_state_conn("p-reentry", conn)

    conn.close.assert_not_called()
    assert (
        server._verification_state["p-reentry"]["_state_db_conn"] is conn
    ), "the fresh connection must be the plan's bound handle"


def test_bind_closes_the_previous_connection():
    """At most one live handle per plan: rebinding retires the old one."""
    first, second = MagicMock(name="first"), MagicMock(name="second")

    server._bind_verification_state_conn("p-reentry", first)
    server._bind_verification_state_conn("p-reentry", second)

    first.close.assert_called_once()
    second.close.assert_not_called()
    assert server._verification_state["p-reentry"]["_state_db_conn"] is second


def test_bind_is_idempotent_for_the_same_connection():
    """Re-binding the SAME handle must not close it out from under the
    caller (the pure-split path can re-enter through the same slot)."""
    conn = MagicMock(name="same-conn")

    server._bind_verification_state_conn("p-reentry", conn)
    server._bind_verification_state_conn("p-reentry", conn)

    conn.close.assert_not_called()
    assert server._verification_state["p-reentry"]["_state_db_conn"] is conn


def test_release_pops_and_closes():
    conn = MagicMock(name="released-conn")
    server._bind_verification_state_conn("p-reentry", conn)

    server._release_verification_state_conn("p-reentry")

    conn.close.assert_called_once()
    assert "_state_db_conn" not in server._verification_state["p-reentry"]


def test_release_is_idempotent_and_tolerates_unknown_plans():
    """The watcher thread calls this unconditionally after the callback —
    a plan with no slot, or one already released, must be a no-op."""
    server._release_verification_state_conn("p-reentry")       # no slot
    server._release_verification_state_conn("p-reentry-2")     # no slot

    conn = MagicMock(name="twice-released")
    server._bind_verification_state_conn("p-reentry-2", conn)
    server._release_verification_state_conn("p-reentry-2")
    server._release_verification_state_conn("p-reentry-2")

    assert conn.close.call_count == 1


def test_bind_survives_a_close_that_raises():
    """A previous handle whose close() throws (already-closed sqlite
    handle, driver error) must not abort the re-entry."""
    broken, fresh = MagicMock(name="broken"), MagicMock(name="fresh")
    broken.close.side_effect = RuntimeError("already closed")

    server._bind_verification_state_conn("p-reentry", broken)
    server._bind_verification_state_conn("p-reentry", fresh)

    assert server._verification_state["p-reentry"]["_state_db_conn"] is fresh
    fresh.close.assert_not_called()
