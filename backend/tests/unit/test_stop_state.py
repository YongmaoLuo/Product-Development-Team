"""A stop needs one plan-level record of what it is stopping.

2026-11-20 — the stop path used to answer "what is this plan doing right
now, and what should be shut down?" from whatever the caller happened to
know: the HTTP handler remembered the phase it read, the spawn gate
remembered only a boolean, and the quiet-window judgement remembered a
dispatch count it had taken on its own.  None of those three facts were
recorded anywhere, so nothing could answer *"which plan is stopping, from
which phase did it come in, and what was the dispatch baseline when the
stop began?"* — the three questions the cooperative stop needs in order
to be idempotent, to dispatch the right scope, and to tell "nothing new
was dispatched" from "something was dispatched and we did not notice".

``stop_state`` is that record, and it is deliberately the *first* one:
the baseline is taken when the stop begins, so a dispatch that happens
afterwards is visible as a difference.  A baseline captured later — or
re-captured on a second stop attempt — would silently erase the very
evidence the quiet window exists to produce.

The properties this file pins:

1. **The round trip** — ``begin()`` then ``get()`` returns the record
   that was begun.
2. **The first record wins** — a second ``begin()`` on the same plan
   keeps the original record and its original baseline.  A stop that is
   requested twice is one stop, not two.
3. **``clear()`` forgets** — after it, ``get()`` returns ``None``.
4. **The lock holds** — concurrent ``begin()`` calls on one plan leave
   exactly one record behind.
5. **An empty plan id is a no-op** — it never registers anywhere, so a
   non-plan caller cannot be gated by accident.
"""

from __future__ import annotations

import threading

import pytest

import stop_state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_stop_state():
    """The registry is process-global; every test starts empty."""
    stop_state._reset_for_tests()
    yield
    stop_state._reset_for_tests()


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_begin_get_roundtrip():
    stop_state.begin(
        "p1",
        scope=stop_state.SCOPE_VERIFICATION,
        previous_phase="verification_running",
        dispatch_baseline=7,
    )

    rec = stop_state.get("p1")

    assert rec is not None, "begin() must make the record readable"
    assert rec.plan_id == "p1"
    assert rec.scope == "verification"
    assert rec.previous_phase == "verification_running"
    assert rec.dispatch_baseline == 7
    assert rec.started_at > 0, "the record must say when the stop began"
    assert stop_state.is_stopping("p1") is True


# ---------------------------------------------------------------------------
# The first record wins
# ---------------------------------------------------------------------------


def test_second_begin_keeps_baseline():
    stop_state.begin(
        "p1",
        scope=stop_state.SCOPE_VERIFICATION,
        previous_phase="verification_running",
        dispatch_baseline=7,
    )
    stop_state.begin(
        "p1",
        scope=stop_state.SCOPE_REPAIR,
        previous_phase="verification_repairing",
        dispatch_baseline=99,
    )

    rec = stop_state.get("p1")

    assert rec is not None
    assert rec.dispatch_baseline == 7, (
        "a second begin() must not overwrite the baseline — the quiet "
        "window compares against the count taken when the stop began"
    )
    assert rec.scope == "verification"
    assert rec.previous_phase == "verification_running"


# ---------------------------------------------------------------------------
# Clearing
# ---------------------------------------------------------------------------


def test_clear_removes_record():
    stop_state.begin(
        "p1",
        scope=stop_state.SCOPE_VERIFICATION,
        previous_phase="verification_running",
        dispatch_baseline=7,
    )
    assert stop_state.is_stopping("p1") is True

    stop_state.clear("p1")

    assert stop_state.get("p1") is None
    assert stop_state.is_stopping("p1") is False


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_begin_single_record():
    """Racing begins on one plan leave exactly one record behind."""
    threads = 16
    barrier = threading.Barrier(threads)
    seen = []
    errors = []

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            seen.append(stop_state.begin(
                "p1",
                scope=stop_state.SCOPE_VERIFICATION,
                previous_phase="verification_running",
                dispatch_baseline=index,
            ))
        except Exception as exc:  # noqa: BLE001 — reported below, not hidden
            errors.append(exc)

    workers = [threading.Thread(target=_worker, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)

    assert not errors, f"concurrent begin() raised: {errors!r}"
    assert not any(worker.is_alive() for worker in workers), "a worker hung"
    assert len(seen) == threads

    records = [record for record in seen if record is not None]
    assert len(records) == threads, "every begin() must return the record"
    assert all(record is records[0] for record in records), (
        "concurrent begin() calls must all observe the same record"
    )

    stored = stop_state.get("p1")
    assert stored is records[0]
    assert len(stop_state._records) == 1, (
        "a racing begin() must not leave more than one record behind"
    )


# ---------------------------------------------------------------------------
# The empty plan id
# ---------------------------------------------------------------------------


def test_empty_plan_id_is_noop():
    assert stop_state.begin("") is None
    assert stop_state.get("") is None
    assert stop_state.is_stopping("") is False

    stop_state.clear("")

    assert stop_state.get("") is None
    assert len(stop_state._records) == 0, (
        "an empty plan id must not register anywhere"
    )