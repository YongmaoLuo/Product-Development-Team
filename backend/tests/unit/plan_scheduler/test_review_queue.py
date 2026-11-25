"""Unit tests: the 「待你审核」 aggregation of three sources into one read model.

Background
----------
Three different things are waiting on the operator, and they live in
three different places:

1. **A pull request waiting to be merged** — the finishing stage's
   approval wait, which carries the PR's number and link.
2. **A pending HITL decision** — a failure review, a question a plan
   asked a human — which carries the decision's own kind.
3. **A postprocess approval waiting** — a wait that asks "may this be
   merged?" without naming a pull request.

The panel wants one block, not three: *what is waiting for me*.  So the
three sources are folded into one document with three fixed groups.

The one thing that must not happen
----------------------------------
An approval is **both** a HITL decision and a wait.  Taking it from
both sides would show the operator the same question twice — once as a
decision, once as a wait — and the panel would look broken.  So the
decision side gives the approval up: a decision whose kind is
``postprocess_approval`` is **not** reported in the HITL group.  The
wait represents it, because the wait is the side that carries the
evidence an operator can act on — the PR number, the link, the item.

What this file asserts
----------------------
**The grouping.**  Which source lands in which group, and what the
row says.  The row's shape is closed — six keys — and the three groups
are always all three keys, even when they are empty, because the panel
hides an empty group by the key being there and the list being empty.

**The duplication rules.**  The approval decision is dropped on purpose;
a row that names the same item twice is listed once.

The build is a pure function: it opens no database and starts no
process, so the panel can poll it as often as it likes.
"""

from __future__ import annotations

from typing import Any, Dict, List

from plan_scheduler.review_queue import build_review_queue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hitl(
    decision_id: str,
    plan_id: str,
    kind: str = "failure_review",
    question: str = "本轮失败如何处理？",
) -> Dict[str, Any]:
    """A HITL decision row, shaped like the one the HITL service hands out."""
    return {
        "decision_id": decision_id,
        "plan_id": plan_id,
        "kind": kind,
        "question": question,
        "created_at": "2026-11-20T08:00:00",
    }


def _wait(
    plan_id: str,
    item_id: str,
    *,
    question: str = "批准合入 main？",
    pr_number: Any = 42,
    pr_url: Any = "https://github.com/o/r/pull/42",
    source: str = "postprocess_approval",
) -> Dict[str, Any]:
    """A waiting row, shaped like the one the scheduler's read model gives."""
    return {
        "plan_id": plan_id,
        "item_id": item_id,
        "wait_type": "human",
        "question": question,
        "source": source,
        "pr_number": pr_number,
        "pr_url": pr_url,
    }


# ---------------------------------------------------------------------------
# 1. The three sources land in three groups
# ---------------------------------------------------------------------------


def test_groups_by_kind() -> None:
    """Each source lands in its own group, and says what it has to say.

    The PR wait goes to ``pr_merge`` and is titled by its number; the
    failure review goes to ``hitl`` and is titled by the kind of
    decision it is; the approval wait that names no PR goes to
    ``approval``.

    The approval *decision* (``d2``) is not in the HITL group: the
    wait that stands for it (``i8``) already carries it, and showing
    both would show the same question twice.
    """
    result = build_review_queue(
        hitl_rows=[
            _hitl("d1", "p1", kind="failure_review", question="Q"),
            _hitl("d2", "p2", kind="postprocess_approval", question="批准合入 main？"),
        ],
        wait_rows=[
            _wait(
                "p2",
                "i8",
                question="批准合入 main？",
                pr_number=42,
                pr_url="https://github.com/o/r/pull/42",
            ),
            _wait(
                "p3",
                "i9",
                question="批准执行？",
                pr_number=None,
                pr_url=None,
            ),
        ],
    )

    assert result == {
        "pr_merge": [
            {
                "kind": "pr_merge",
                "plan_id": "p2",
                "title": "PR #42",
                "detail": "批准合入 main？",
                "link": "https://github.com/o/r/pull/42",
                "item_id": "i8",
            }
        ],
        "hitl": [
            {
                "kind": "hitl",
                "plan_id": "p1",
                "title": "failure_review",
                "detail": "Q",
                "link": "",
                "item_id": "d1",
            }
        ],
        "approval": [
            {
                "kind": "approval",
                "plan_id": "p3",
                "title": "待批准",
                "detail": "批准执行？",
                "link": "",
                "item_id": "i9",
            }
        ],
    }


# ---------------------------------------------------------------------------
# 2. An empty queue still has all three groups
# ---------------------------------------------------------------------------


def test_empty_keeps_all_groups() -> None:
    """Nothing waiting → three empty groups, never fewer.

    The panel hides a group by looking for it and finding it empty.  A
    missing key would be a second way of saying "nothing here", and the
    panel would have to decide which of the two answers to believe.
    """
    result = build_review_queue(hitl_rows=[], wait_rows=[])

    assert result == {"pr_merge": [], "hitl": [], "approval": []}
    assert set(result) == {"pr_merge", "hitl", "approval"}


# ---------------------------------------------------------------------------
# 3. A wait that names no pull request is an approval
# ---------------------------------------------------------------------------


def test_wait_without_pr_number_is_approval() -> None:
    """A wait with no PR number goes to ``approval``, not to ``pr_merge``.

    There is nothing to merge and nothing to link to, so the row is
    the plain 「待批准」 — and it is not in the PR group either, because
    a row with no number in it cannot be a pull request.
    """
    result = build_review_queue(
        hitl_rows=[],
        wait_rows=[
            _wait("p3", "i9", question="批准执行？", pr_number=None, pr_url=None),
        ],
    )

    assert result["pr_merge"] == []
    assert len(result["approval"]) == 1
    assert result["approval"][0] == {
        "kind": "approval",
        "plan_id": "p3",
        "title": "待批准",
        "detail": "批准执行？",
        "link": "",
        "item_id": "i9",
    }


def test_wait_with_blank_pr_number_is_approval() -> None:
    """An empty string is no number either.

    A row written by hand, or by a version of the wait registry that
    had no PR fields yet, carries ``""`` where the number belongs.
    """
    result = build_review_queue(
        hitl_rows=[],
        wait_rows=[_wait("p3", "i9", pr_number="", pr_url="")],
    )

    assert result["pr_merge"] == []
    assert len(result["approval"]) == 1


# ---------------------------------------------------------------------------
# 4. The approval decision gives way to the approval wait
# ---------------------------------------------------------------------------


def test_approval_kind_not_in_hitl() -> None:
    """A decision whose kind is ``postprocess_approval`` is not reported.

    The approval is a HITL decision *and* a wait.  Reporting both would
    show the operator one question twice, so the decision side stands
    down and the wait speaks for it.
    """
    result = build_review_queue(
        hitl_rows=[
            _hitl("d1", "p1", kind="failure_review"),
            _hitl("d2", "p2", kind="postprocess_approval"),
        ],
        wait_rows=[],
    )

    assert [row["item_id"] for row in result["hitl"]] == ["d1"]
    assert all(
        row["kind"] != "postprocess_approval" for row in result["hitl"]
    )


# ---------------------------------------------------------------------------
# 5. The same item is listed once
# ---------------------------------------------------------------------------


def test_duplicate_item_listed_once() -> None:
    """Two rows naming the same plan and item are listed once.

    The first one wins — the rows arrive in the order the sources gave
    them, and the first arrival is the one that is kept.
    """
    result = build_review_queue(
        hitl_rows=[],
        wait_rows=[
            _wait("p2", "i8", question="第一行", pr_number=42),
            _wait("p2", "i8", question="第二行", pr_number=43),
        ],
    )

    assert len(result["pr_merge"]) == 1
    assert result["pr_merge"][0]["detail"] == "第一行"
    assert result["pr_merge"][0]["title"] == "PR #42"


def test_different_items_of_one_plan_are_both_listed() -> None:
    """Two items of one plan are two rows.

    The plan is not the unit — the *item* is.  One plan can be waiting
    on two different things, and the operator has to see both.
    """
    result = build_review_queue(
        hitl_rows=[],
        wait_rows=[
            _wait("p2", "i8", question="合入？", pr_number=42),
            _wait("p2", "i9", question="发布？", pr_number=None, pr_url=None),
        ],
    )

    assert len(result["pr_merge"]) == 1
    assert len(result["approval"]) == 1
    assert result["pr_merge"][0]["item_id"] == "i8"
    assert result["approval"][0]["item_id"] == "i9"


# ---------------------------------------------------------------------------
# 6. The row's shape is closed
# ---------------------------------------------------------------------------


def test_every_row_carries_the_same_six_keys() -> None:
    """A row carries exactly six keys, whatever group it is in.

    The panel draws the three groups the same way, so a row that
    carried a different set of keys per group would be a shape the
    panel has to know three times over.
    """
    result = build_review_queue(
        hitl_rows=[_hitl("d1", "p1")],
        wait_rows=[
            _wait("p2", "i8", pr_number=42),
            _wait("p3", "i9", pr_number=None, pr_url=None),
        ],
    )

    expected_keys = {"kind", "plan_id", "title", "detail", "link", "item_id"}
    rows: List[Dict[str, Any]] = []
    for group in ("pr_merge", "hitl", "approval"):
        rows.extend(result[group])

    assert len(rows) == 3
    for row in rows:
        assert set(row) == expected_keys