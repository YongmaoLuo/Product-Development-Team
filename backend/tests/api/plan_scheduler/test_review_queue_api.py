"""``GET /api/scheduler/state`` — the 「待你审核」 queue in the read model.

What is pinned here
-------------------
The operator has one question to answer — *what is waiting for me* — and
the answer comes from two different places: the decisions a plan asked a
human (``hitl.service.list_open_decisions``) and the approval waits the
finishing stage is holding.  Architecture decision R5 asks for one
block, so the two are folded into one document by
:func:`plan_scheduler.review_queue.build_review_queue` and handed to the
panel under the ``review_queue`` key.

Four things are pinned here, and each of them is a way the block could
lie to the operator:

1. **The queue is in the read model** (``test_state_includes_review_queue``).
   A pending decision that nobody can see is a question nobody is
   waiting on, which is the one thing this block exists to prevent.
2. **A source that cannot be read does not take the endpoint with it**
   (``test_source_failure_keeps_state_200``).  The panel polls this
   endpoint every five seconds; a decision store that is momentarily
   unreadable must not leave the operator without a panel at all.  The
   failed source answers empty, the other one carries on.
3. **An approval is not counted twice**
   (``test_approval_not_duplicated_in_hitl``).  An approval is *both* a
   decision and a wait; reading it from both sides would show the
   operator the same question twice, once as a decision and once as a
   wait — and the panel would look broken.
4. **An ordinary wait is not an approval**
   (``test_non_approval_wait_excluded``).  Only the waits the finishing
   stage asked under ``postprocess_approval`` are approvals; a plain
   human wait is not one, and counting it here would put a question in
   the review block that the review block cannot answer.

Why this directory has no ``__init__.py``
-----------------------------------------
The production package is called ``plan_scheduler``, and so is this
directory.  A package marker here would make pytest import this
directory as the **top-level** package ``plan_scheduler`` — and the name
would then mean two things at once, with the winner decided by the order
the files happen to be imported in.  See
``tests/api/plan_scheduler/test_scheduler_state_api.py`` for the long
version of that argument; the short version is that the marker belongs
one level up, in ``backend/tests/api/``.

Test command::

    pytest backend/tests/api/plan_scheduler/test_review_queue_api.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from hitl import service as hitl_service  # noqa: E402
from hitl.models import Decision  # noqa: E402
from hitl.store import HitlStore  # noqa: E402
from postprocess.progress import PROGRESS_FILENAME  # noqa: E402
from routes import scheduler as routes_scheduler  # noqa: E402


client = TestClient(server.app)

#: The service's own reader, captured before any test replaces it — so a
#: test that undoes its own replacement can put the real one back.
_REAL_LIST_OPEN_DECISIONS = hitl_service.list_open_decisions

#: The endpoint under test.
STATE_PATH = "/api/scheduler/state"

#: The three groups the queue is made of.  All three are always there —
#: an empty group is a group, not a missing key, so the panel has one
#: way of saying "nothing here" and not two.
GROUPS = ("pr_merge", "hitl", "approval")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def plans_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway plans root — the tree the waiting rows are read from."""
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))
    return root


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway decision database — the store the open decisions live in."""
    path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(path))
    return path


def _seed_decision(
    db_path: Path,
    decision_id: str = "d1",
    *,
    plan_id: str = "p1",
    source: str = "failure_review",
    question: str = "本轮失败如何处理？",
    status: str = "pending",
) -> None:
    """Store one decision, as the service would have stored it."""
    store = HitlStore(db_path, audit_dir=db_path.parent / "audit")
    try:
        store.insert(
            Decision(
                decision_id=decision_id,
                plan_id=plan_id,
                source=source,
                question=question,
                options=["retry", "skip"],
                default_option="retry",
                status=status,
                created_at="2026-11-20T08:00:00",
                updated_at="2026-11-20T08:00:00",
            )
        )
    finally:
        store.close()


def _seed_wait(
    plans_root: Path,
    plan_id: str,
    item_id: str,
    *,
    source: str = "postprocess_approval",
    question: str = "批准合入 main？",
    pr_number: Optional[int] = None,
    pr_url: Optional[str] = None,
) -> None:
    """Write one pending wait into a plan's durable progress file.

    The wait record is the one the finishing stage writes: the item, the
    kind, the question, the deadline, the extension audit, the status —
    and the three review fields (the source it was asked under and the
    pull request it is about).
    """
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    record: Dict[str, Any] = {
        "item_id": item_id,
        "wait_type": "human",
        "question": question,
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "extensions": [],
        "status": "pending",
        "source": source,
        "pr_number": pr_number,
        "pr_url": pr_url,
    }
    progress = {
        "spec_sha256": "",
        "started_at": "",
        "items": {item_id: {"wait": record}},
    }
    (plan_dir / PROGRESS_FILENAME).write_text(
        json.dumps(progress, ensure_ascii=False), encoding="utf-8"
    )


def _queue(response: Any) -> Dict[str, Any]:
    """The ``review_queue`` of a response, as a document."""
    body = response.json()
    assert "review_queue" in body, sorted(body)
    return body["review_queue"]


# ---------------------------------------------------------------------------
# 1 — the queue is in the read model
# ---------------------------------------------------------------------------


def test_state_includes_review_queue(state_db: Path, plans_root: Path) -> None:
    """A pending decision is in the queue the panel reads.

    The decision is asked under ``failure_review`` — the ordinary kind
    of question a plan asks a human.  It lands in the ``hitl`` group,
    and it lands there **once**: the group is the operator's list of
    questions to answer, and a question listed twice is a question the
    operator answers twice.
    """
    _seed_decision(state_db, "d1", plan_id="p1", source="failure_review")

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    queue = _queue(response)
    assert set(queue) == set(GROUPS), sorted(queue)
    assert len(queue["hitl"]) == 1, queue
    row = queue["hitl"][0]
    assert row["plan_id"] == "p1", row
    assert row["kind"] == "hitl", row
    assert row["item_id"] == "d1", row


# ---------------------------------------------------------------------------
# 2 — a source that cannot be read does not take the panel with it
# ---------------------------------------------------------------------------


def test_source_failure_keeps_state_200(
    state_db: Path, plans_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that cannot be read leaves the endpoint answering.

    The panel polls this endpoint every five seconds.  A store that is
    momentarily unreadable must cost the operator the *decisions*, not
    the panel: the failed source answers empty, the other source keeps
    its rows, and the endpoint still answers 200.

    Both directions are checked, because both are ways of losing one
    half of the block: the decision store that cannot be read, and the
    plans tree that cannot be walked.
    """

    def _unreadable() -> Any:
        raise RuntimeError("the source is unreadable")

    # The decision store cannot be read: the decisions answer empty and
    # the approval waits still answer.
    monkeypatch.setattr(hitl_service, "list_open_decisions", _unreadable)
    _seed_wait(
        plans_root,
        "p2",
        "i8",
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
    )

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    queue = _queue(response)
    assert set(queue) == set(GROUPS), sorted(queue)
    # The source that failed answers empty ...
    assert queue["hitl"] == [], queue
    # ... and the source that did not fail still answers.
    assert [row["title"] for row in queue["pr_merge"]] == ["PR #42"], queue

    # And the other way round: the plans tree cannot be walked, so the
    # approvals answer empty and the decisions still answer.
    monkeypatch.setattr(
        hitl_service, "list_open_decisions", _REAL_LIST_OPEN_DECISIONS
    )
    monkeypatch.setattr(routes_scheduler, "_pending_waiting_rows", _unreadable)
    _seed_decision(state_db, "d1", plan_id="p1", source="failure_review")

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    queue = _queue(response)
    assert set(queue) == set(GROUPS), sorted(queue)
    assert queue["approval"] == [], queue
    assert queue["pr_merge"] == [], queue
    assert [row["item_id"] for row in queue["hitl"]] == ["d1"], queue


# ---------------------------------------------------------------------------
# 3 — an approval is not counted twice
# ---------------------------------------------------------------------------


def test_approval_not_duplicated_in_hitl(state_db: Path, plans_root: Path) -> None:
    """The approval decision stands down; the wait speaks for it.

    An approval is both a decision and a wait.  The decision side gives
    it up: the wait carries the pull request, the link and the item —
    the evidence the operator can act on — so the decision is not
    reported as well.
    """
    _seed_decision(
        state_db,
        "d2",
        plan_id="p2",
        source="postprocess_approval",
        question="批准合入 main？",
    )
    _seed_wait(
        plans_root,
        "p2",
        "i8",
        question="批准合入 main？",
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
    )

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    queue = _queue(response)
    assert queue["hitl"] == [], queue
    assert len(queue["pr_merge"]) == 1, queue
    row = queue["pr_merge"][0]
    assert row["item_id"] == "i8", row
    assert row["plan_id"] == "p2", row
    assert row["title"] == "PR #42", row
    assert row["link"] == "https://github.com/o/r/pull/42", row


# ---------------------------------------------------------------------------
# 4 — an ordinary wait is not an approval
# ---------------------------------------------------------------------------


def test_non_approval_wait_excluded(state_db: Path, plans_root: Path) -> None:
    """A wait that was not asked under the approval source is not listed.

    The review block answers "what is waiting for **you** to decide".
    A wait the finishing stage holds for another reason is not that,
    and putting it here would put a row in the block that the block
    cannot answer.
    """
    _seed_wait(plans_root, "p3", "i9", source="", question="人工等待")

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    queue = _queue(response)
    assert queue["approval"] == [], queue
    assert queue["pr_merge"] == [], queue
    assert queue["hitl"] == [], queue
