"""HITL decision storage: CAS answers, idempotency and the audit mirror.

Background
----------
A human-in-the-loop answer is written by whoever answers first and
read by everyone afterwards.  The contract that makes that safe is the
compare-and-set on ``status``: an answer may only move a decision that
is *still* ``pending``, so two humans clicking "accept" at the same
second produce exactly one stored answer, and the second click is a
no-op rather than an overwrite.

The store owns three more guarantees this module pins:

  1. **CAS** — ``answer_if_pending`` succeeds at most once per decision
     (``test_answer_cas``), even when several threads with several
     connections race it.
  2. **Idempotency** — a second answer, or an answer to a decision that
     has already been defaulted / paused, returns ``False`` and leaves
     ``answered_by`` / ``answered_at`` / ``answer`` exactly as the
     first writer left them (``test_answer_does_not_overwrite``).
  3. **Audit mirror** — ``hitl_decisions.jsonl`` records exactly one
     line per *successful* state transition and no line at all for a
     rejected one, with a fixed field whitelist (``test_audit_jsonl_lines``).

Why an integration test
-----------------------
The CAS guarantee is not observable from a single connection in a
single thread: it is a property of SQLite's write lock plus a
``WHERE status = 'pending'`` predicate.  Racing it needs real threads
and real connections, so the tests live here.

Test command::

    pytest backend/tests/integration/test_hitl_store.py -v
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List

import pytest

from hitl.models import Decision, DecisionMode, DecisionRequest, DecisionStatus
from hitl.store import AUDIT_FIELDS, HitlStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(
    *,
    plan_id: str = "plan-1",
    question: str = "Retry the failing task?",
    options: List[str] | None = None,
    default_option: str | None = "retry",
    mode: str = DecisionMode.AUTO.value,
    timeout_sec: int | None = 1800,
    source: str = "execution",
) -> DecisionRequest:
    return DecisionRequest(
        plan_id=plan_id,
        source=source,
        question=question,
        options=list(options or ["retry", "skip", "stop"]),
        default_option=default_option,
        mode=mode,
        timeout_sec=timeout_sec,
    )


def _pending(
    decision_id: str = "d1",
    *,
    plan_id: str = "plan-1",
    **kwargs: Any,
) -> Decision:
    return Decision.from_request(
        _request(plan_id=plan_id, **kwargs), decision_id=decision_id
    )


def _read_audit(store: HitlStore, plan_id: str = "plan-1") -> List[Dict[str, Any]]:
    path = store.audit_path(plan_id)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "hitl.db"


@pytest.fixture
def store(db_path: Path, tmp_path: Path) -> HitlStore:
    return HitlStore(db_path, audit_dir=tmp_path / "audit")


# ---------------------------------------------------------------------------
# 1. CAS: exactly one answer wins
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_answer_cas(store: HitlStore, db_path: Path, tmp_path: Path) -> None:
    """Racing answers: exactly one succeeds, and it is the one stored.

    Each racer opens its OWN store (hence its own SQLite connection),
    so the winner is decided by the database's write lock and the
    ``WHERE status='pending'`` predicate — not by anything in this
    process.  That is the shape a real deployment has: the executor
    thread, a route handler and a CLI can all answer at once.
    """
    store.insert(_pending("d1"))

    racers = 8
    barrier = threading.Barrier(racers)
    results: List[bool] = [False] * racers
    errors: List[BaseException] = []

    def race(index: int) -> None:
        try:
            other = HitlStore(db_path, audit_dir=tmp_path / "audit")
            barrier.wait(timeout=10)
            results[index] = other.answer_if_pending(
                "d1", f"option-{index}", f"user-{index}", ""
            )
        except BaseException as exc:  # surfaced by the assert below
            errors.append(exc)

    threads = [
        threading.Thread(target=race, args=(i,), name=f"racer-{i}")
        for i in range(racers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"a racer raised: {errors!r}"
    assert not any(t.is_alive() for t in threads), "a racer thread hung"
    assert sum(results) == 1, (
        f"exactly one racer may win the CAS; got {results!r}"
    )

    winner = results.index(True)
    stored = store.get("d1")
    assert stored is not None
    assert stored.status == DecisionStatus.ANSWERED.value
    assert stored.answered_by == f"user-{winner}"
    assert stored.answer == f"option-{winner}"
    assert stored.answered_at, "the winner must stamp answered_at"

    # The rejected racers must not have left audit lines behind: one
    # transition, one line.
    audit = _read_audit(store)
    assert [line["event"] for line in audit] == ["created", "answered"]


# ---------------------------------------------------------------------------
# 2. Idempotency: a second answer never overwrites the first
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_answer_does_not_overwrite(store: HitlStore) -> None:
    """A second answer returns False and leaves the first answer intact."""
    store.insert(_pending("d1"))

    assert store.answer_if_pending("d1", "accept", "alice", "") is True
    first = store.get("d1")
    assert first is not None

    assert store.answer_if_pending("d1", "revise", "bob", "") is False
    second = store.get("d1")
    assert second is not None

    assert second.answered_by == "alice"
    assert second.answered_at == first.answered_at
    assert second.answer == "accept"
    assert second.audit_seq == first.audit_seq


@pytest.mark.integration
def test_defaulted_and_paused_do_not_overwrite(store: HitlStore) -> None:
    """A defaulted decision is no longer answerable."""
    store.insert(_pending("d1"))

    assert store.mark_defaulted("d1", None, "deadline passed") is True
    defaulted = store.get("d1")
    assert defaulted is not None
    assert defaulted.status == DecisionStatus.DEFAULTED.value
    assert defaulted.answer == "retry"  # the stored default_option

    assert store.answer_if_pending("d1", "accept", "alice", "") is False
    after = store.get("d1")
    assert after is not None
    assert after.answered_by is None
    assert after.answer == "retry"


@pytest.mark.integration
def test_mark_paused_only_from_pending(store: HitlStore) -> None:
    """``paused`` is a pending-only transition, like the others."""
    store.insert(_pending("d1"))
    assert store.mark_paused("d1", "thread exiting") is True
    paused = store.get("d1")
    assert paused is not None
    assert paused.status == DecisionStatus.PAUSED.value
    assert store.mark_paused("d1", "again") is False


# ---------------------------------------------------------------------------
# 3. Audit mirror: one line per transition, fixed fields
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_audit_jsonl_lines(store: HitlStore) -> None:
    """Every successful transition appends exactly one whitelisted line."""
    store.insert(_pending("d1"))

    lines = _read_audit(store)
    assert len(lines) == 1, f"insert is a transition; got {lines!r}"
    assert lines[0]["event"] == "created"
    assert lines[0]["decision_id"] == "d1"
    assert lines[0]["plan_id"] == "plan-1"
    assert lines[0]["status"] == DecisionStatus.PENDING.value
    assert list(lines[0]) == list(AUDIT_FIELDS), (
        "the audit line carries a fixed field whitelist, in order"
    )

    assert store.answer_if_pending("d1", "accept", "alice", "looks good") is True
    lines = _read_audit(store)
    assert len(lines) == 2
    assert lines[1]["event"] == "answered"
    assert lines[1]["status"] == DecisionStatus.ANSWERED.value
    assert lines[1]["answer"] == "accept"
    assert lines[1]["answered_by"] == "alice"
    assert lines[1]["note"] == "looks good"
    assert list(lines[1]) == list(AUDIT_FIELDS)

    # Rejected writes are not transitions and must not append.
    assert store.answer_if_pending("d1", "revise", "bob", "") is False
    assert store.mark_paused("d1", "") is False
    assert store.mark_defaulted("d1", None, "") is False
    assert len(_read_audit(store)) == 2

    # A second decision gets its own lines, under the same plan.
    store.insert(_pending("d2"))
    assert store.mark_defaulted("d2", None, "deadline") is True
    events = [line["event"] for line in _read_audit(store)]
    assert events == ["created", "answered", "created", "defaulted"]


@pytest.mark.integration
def test_append_audit_filters_to_the_whitelist(store: HitlStore) -> None:
    """``append_audit`` writes the whitelist and nothing else."""
    store.append_audit(
        {
            "plan_id": "plan-1",
            "decision_id": "d9",
            "event": "created",
            "status": "pending",
            "answer": None,
            "answered_by": None,
            "note": None,
            "audit_seq": 0,
            "ts": "2026-10-21T00:00:00+00:00",
            "secret_prompt_text": "must never be mirrored",
        }
    )
    lines = _read_audit(store)
    assert len(lines) == 1
    assert list(lines[0]) == list(AUDIT_FIELDS)
    assert "secret_prompt_text" not in lines[0]


# ---------------------------------------------------------------------------
# 4. Unknown ids are reported, not invented
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_unknown_id(store: HitlStore) -> None:
    """An unknown decision id reads as ``None`` and writes as ``False``."""
    assert store.get("no-such-decision") is None
    assert store.answer_if_pending("no-such-decision", "accept", "alice", "") is False
    assert store.mark_defaulted("no-such-decision", None, "") is False
    assert store.mark_paused("no-such-decision", "") is False

    # A known decision proves the store is working, and that the
    # unknown-id writes above appended nothing.
    store.insert(_pending("d1"))
    assert store.get("d1") is not None
    assert len(_read_audit(store)) == 1

    # A read of an unknown plan has nothing to list.
    assert store.list_by_plan("no-such-plan") == []


# ---------------------------------------------------------------------------
# 5. Round-trip reads
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_insert_get_round_trip(store: HitlStore) -> None:
    """What goes in comes back, options and answer included."""
    decision = _pending("d1")
    store.insert(decision)

    stored = store.get("d1")
    assert stored is not None
    assert stored.plan_id == decision.plan_id
    assert stored.source == decision.source
    assert stored.question == decision.question
    assert stored.options == decision.options
    assert stored.default_option == decision.default_option
    assert stored.mode == decision.mode
    assert stored.timeout_sec == decision.timeout_sec
    assert stored.status == DecisionStatus.PENDING.value
    assert stored.deadline_at == decision.deadline_at
    assert stored.created_at == decision.created_at


@pytest.mark.integration
def test_list_by_plan(store: HitlStore) -> None:
    """``list_by_plan`` returns one plan, optionally filtered by status."""
    store.insert(_pending("d1", plan_id="plan-1"))
    store.insert(_pending("d2", plan_id="plan-1"))
    store.insert(_pending("d3", plan_id="plan-2"))

    assert [d.decision_id for d in store.list_by_plan("plan-1")] == ["d1", "d2"]
    assert store.answer_if_pending("d1", "accept", "alice", "") is True
    assert store.mark_paused("d2", "") is True

    assert [d.decision_id for d in store.list_by_plan("plan-1", status="pending")] == []
    assert [
        d.decision_id for d in store.list_by_plan("plan-1", status="answered")
    ] == ["d1"]
    assert [
        d.decision_id for d in store.list_by_plan("plan-1", status="paused")
    ] == ["d2"]
    assert [d.decision_id for d in store.list_by_plan("plan-2")] == ["d3"]