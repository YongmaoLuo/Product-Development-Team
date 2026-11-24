"""The open HITL decisions, across every plan.

What this pins
--------------
R5 aggregates the human-in-the-loop decisions that are still waiting
for an answer, and it aggregates them **across plans**: a question
asked by a plan that is not the one on screen is still a question a
human is being waited on.  ``hitl.service.list_decisions`` and
``hitl.store.list_by_plan`` are both plan-scoped, so this file pins the
cross-plan query that sits on top of them:

  * **both plans are listed** -- two plans each with one pending
    decision produce two rows (``test_lists_pending_across_plans``);
  * **a settled decision is not listed** -- ``answered`` (likewise
    ``defaulted`` / ``unroutable``) is not a question anybody is
    waiting for any more (``test_excludes_answered``);
  * **the row shape is fixed** -- exactly five keys, so a consumer
    that renders the list does not have to guard every field
    (``test_shape_is_fixed_keys``);
  * **the kind is the stored source** -- ``kind`` *is* the decision's
    ``source`` column, which is the name the event payload already
    gives it (``test_kind_comes_from_source``).

Two boundaries are pinned alongside them: a ``paused`` decision is
still open (its asker may still come back), and a database that does
not exist yet is not an error -- it is an empty list.

Test command::

    pytest backend/tests/unit/test_hitl_open_decisions.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hitl import service
from hitl.models import Decision
from hitl.store import HitlStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decision(
    decision_id: str = "d1",
    *,
    plan_id: str = "p1",
    source: str = "execution",
    question: str = "本轮失败如何处理？",
    status: str = "pending",
    created_at: str = "2026-11-20T08:00:00",
) -> Decision:
    """A stored decision, as the service would have built it."""
    return Decision(
        decision_id=decision_id,
        plan_id=plan_id,
        source=source,
        question=question,
        options=["retry", "skip"],
        default_option="retry",
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.fixture
def store(tmp_path: Path) -> HitlStore:
    """A store on a throwaway database, away from the operator's own."""
    return HitlStore(tmp_path / "hitl.db", audit_dir=tmp_path / "audit")


# ---------------------------------------------------------------------------
# 1. The query is cross-plan
# ---------------------------------------------------------------------------


def test_lists_pending_across_plans(store: HitlStore) -> None:
    """Two plans, one pending decision each: both are listed."""
    store.insert(_decision("d1", plan_id="p1"))
    store.insert(_decision("d2", plan_id="p2"))

    rows = service.list_open_decisions(store=store)

    assert [row["decision_id"] for row in rows] == ["d1", "d2"]
    assert {row["plan_id"] for row in rows} == {"p1", "p2"}


# ---------------------------------------------------------------------------
# 2. A settled decision is not open
# ---------------------------------------------------------------------------


def test_excludes_answered(store: HitlStore) -> None:
    """An answered decision is not waiting for anybody."""
    store.insert(_decision("d1", plan_id="p1", status="answered"))
    store.insert(_decision("d2", plan_id="p1"))
    store.insert(_decision("d3", plan_id="p2", status="defaulted"))
    store.insert(_decision("d4", plan_id="p2", status="unroutable"))

    rows = service.list_open_decisions(store=store)

    assert [row["decision_id"] for row in rows] == ["d2"]


# ---------------------------------------------------------------------------
# 3. The row shape is fixed
# ---------------------------------------------------------------------------


def test_shape_is_fixed_keys(store: HitlStore) -> None:
    """A row carries exactly the five keys a consumer may rely on."""
    store.insert(_decision("d1", plan_id="p1"))

    rows = service.list_open_decisions(store=store)

    assert len(rows) == 1
    assert set(rows[0]) == {
        "decision_id",
        "plan_id",
        "kind",
        "question",
        "created_at",
    }
    assert rows[0]["decision_id"] == "d1"
    assert rows[0]["plan_id"] == "p1"
    assert rows[0]["question"] == "本轮失败如何处理？"
    assert rows[0]["created_at"] == "2026-11-20T08:00:00"


# ---------------------------------------------------------------------------
# 4. The kind is the stored source
# ---------------------------------------------------------------------------


def test_kind_comes_from_source(store: HitlStore) -> None:
    """``kind`` is the decision's ``source``, under the name it is known by."""
    store.insert(
        _decision("d1", plan_id="p1", source="postprocess_approval")
    )

    rows = service.list_open_decisions(store=store)

    assert [row["kind"] for row in rows] == ["postprocess_approval"]


# ---------------------------------------------------------------------------
# 5. The boundaries around "open"
# ---------------------------------------------------------------------------


def test_paused_is_still_open(store: HitlStore) -> None:
    """A paused question may still be answered, so it is still listed."""
    store.insert(_decision("d1", plan_id="p1", status="paused"))

    rows = service.list_open_decisions(store=store)

    assert [row["decision_id"] for row in rows] == ["d1"]


def test_no_open_decisions_is_an_empty_list(store: HitlStore) -> None:
    """Nothing outstanding is an empty list, not an error."""
    store.insert(_decision("d1", plan_id="p1", status="answered"))

    assert service.list_open_decisions(store=store) == []


def test_missing_database_is_an_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database that does not exist yet is created and reads empty."""
    monkeypatch.setenv(
        "PDT_STATE_DB_PATH", str(tmp_path / "not-created-yet" / "state.db")
    )

    assert service.list_open_decisions() == []


def test_rows_are_plain_dicts(store: HitlStore) -> None:
    """A row is a plain dict, so it can be serialised as it is."""
    store.insert(_decision("d1", plan_id="p1"))

    row = service.list_open_decisions(store=store)[0]

    assert isinstance(row, dict)
    assert type(row) is dict
    assert isinstance(row["kind"], str)
    assert isinstance(row["created_at"], str)
