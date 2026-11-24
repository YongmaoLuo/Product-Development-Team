"""backend.plan_scheduler.review_queue — the 「待你审核」 read model.

One queue, three sources
-------------------------
The operator has to answer one question — *what is waiting for me* — and
the things that make them wait come from three different places:

1. **A pull request waiting to be merged.**  The finishing stage asks
   for an approval, the approval names a pull request, and the wait
   carries the number and the link.
2. **A pending HITL decision.**  A failure review, a question a plan
   asked a human and has not been answered.  Its kind is the source it
   was asked under.
3. **A postprocess approval waiting.**  The same approval as (1), but
   for a command that named no pull request.

The panel wants one block, not three, so the three sources are folded
into one document with three fixed groups: ``pr_merge``, ``hitl`` and
``approval``.

The approval is not counted twice
---------------------------------
An approval is **both** a HITL decision and a wait.  Reading it from
both sides would show the operator the same question twice — once as a
decision, once as a wait — and the panel would look broken.

So the decision side gives the approval up: a decision whose kind is
``postprocess_approval`` (the source the finishing stage asks
approvals under — :data:`postprocess.waits.POSTPROCESS_APPROVAL_SOURCE`)
is **not** reported in the ``hitl`` group.  The wait speaks for it,
because the wait is the side that carries the evidence an operator can
act on: the pull request, the link, the item.

A decision with no wait behind it is not reported either.  The wait is
the operable evidence; a decision nobody is waiting on cannot be
acted on, and inventing a row for it would put something in the queue
that cannot be answered.

The groups are fixed, and always all three
------------------------------------------
The document has exactly three keys — ``pr_merge``, ``hitl`` and
``approval`` — and it has them even when they are empty.  The panel
hides a group by finding it empty, and a missing key would be a second
way of saying "nothing here", which the panel would then have to decide
between.

The row is a closed shape too
-----------------------------
Every row carries exactly six keys: ``kind``, ``plan_id``, ``title``,
``detail``, ``link`` and ``item_id``.  The three groups are drawn the
same way, so a row that carried a different set of keys per group would
be a shape the panel has to know three times over.

The row's ``kind`` is the **group** it is in, and its ``title`` is what
that group shows: a PR wait is titled by the number of the pull request
it is about, a decision is titled by the kind of decision it is
(``failure_review``), and an approval that names no pull request is
titled 「待批准」.

One item is listed once
-----------------------
All three groups share one set of keys, and the key is
``(plan_id, item_id)``.  A row whose key has already been seen is
dropped — the first one wins, because the rows arrive in the order the
sources gave them.  The *item* is the unit, not the plan: one plan can
be waiting on two different things, and the operator has to see both.

The build is a pure function
----------------------------
It opens no database and starts no process: the rows are handed to it,
and it answers with the document the panel renders.  A read that
shelled out would be a read that could block the tick it describes.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

from postprocess.waits import POSTPROCESS_APPROVAL_SOURCE

#: The three groups the queue is always made of, in the order they are
#: built.  A group that is empty is still a key in the document — the
#: panel hides an empty group by finding it empty.
REVIEW_GROUPS = ("pr_merge", "hitl", "approval")

#: The title a wait that names no pull request is given.
APPROVAL_TITLE = "待批准"


def _text(value: Any) -> str:
    """A value as text, with a missing value read as an empty string.

    A row that names nothing is not an error: the read model reads what
    is there and does not fail over a field that is allowed to be
    absent.
    """
    if value is None:
        return ""
    return str(value)


def _has_pr_number(value: Any) -> bool:
    """Whether a waiting row names a pull request.

    The number is absent when it is missing (``None``) or blank
    (``""``) — the two shapes a wait record carries when the command it
    came from named no pull request.  Anything else is a number, and a
    wait that names one is a pull request waiting to be merged.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def build_review_queue(
    *,
    hitl_rows: Optional[Iterable[Mapping[str, Any]]],
    wait_rows: Optional[Iterable[Mapping[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Fold the three sources into one 「待你审核」 document.

    Args:
        hitl_rows: The open HITL decisions, as
            :func:`hitl.service.list_open_decisions` hands them out —
            one dict per decision with the keys ``decision_id``,
            ``plan_id``, ``kind`` and ``question``.  A decision whose
            kind is ``postprocess_approval`` is not reported: the wait
            that stands for it speaks for it.
        wait_rows: The waiting rows, as the scheduler's read model
            hands them out — one dict per wait with the keys
            ``plan_id``, ``item_id``, ``question`` and, for an approval
            wait, ``source``, ``pr_number`` and ``pr_url``.

    Returns:
        A dict with exactly the three keys ``pr_merge``, ``hitl`` and
        ``approval``, each a list of rows.  Every row carries exactly
        the six keys ``kind``, ``plan_id``, ``title``, ``detail``,
        ``link`` and ``item_id``.

        A wait that names a pull request goes to ``pr_merge``, titled
        ``PR #<number>`` and linked to the pull request; a wait that
        names none goes to ``approval``, titled 「待批准」.  A decision
        that is not an approval goes to ``hitl``, titled by the kind of
        decision it is.

        All three groups share one set of keys, ``(plan_id, item_id)``:
        a row whose key has already been seen is dropped, and the
        first row to claim a key is the one that is kept.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {
        "pr_merge": [],
        "hitl": [],
        "approval": [],
    }
    # The keys already claimed, across all three groups.  One set for
    # all of them: an item is an item whichever group it lands in.
    claimed: set = set()

    for row in hitl_rows or []:
        kind = _text(row.get("kind"))
        if kind == POSTPROCESS_APPROVAL_SOURCE:
            # The approval decision stands down — the wait that stands
            # for it carries the pull request, the link and the item.
            # Its key is deliberately *not* claimed: dropping the
            # decision must not drop the wait that replaces it.
            continue
        plan_id = _text(row.get("plan_id"))
        item_id = _text(row.get("decision_id"))
        if (plan_id, item_id) in claimed:
            continue
        claimed.add((plan_id, item_id))
        groups["hitl"].append(
            {
                "kind": "hitl",
                "plan_id": plan_id,
                # The title is the kind of decision this is — the word
                # the operator is asked about.
                "title": kind,
                "detail": _text(row.get("question")),
                "link": "",
                "item_id": item_id,
            }
        )

    for row in wait_rows or []:
        plan_id = _text(row.get("plan_id"))
        item_id = _text(row.get("item_id"))
        if (plan_id, item_id) in claimed:
            continue
        claimed.add((plan_id, item_id))
        pr_number = row.get("pr_number")
        if _has_pr_number(pr_number):
            group = "pr_merge"
            title = "PR #{}".format(_text(pr_number))
        else:
            group = "approval"
            title = APPROVAL_TITLE
        groups[group].append(
            {
                "kind": group,
                "plan_id": plan_id,
                "title": title,
                "detail": _text(row.get("question")),
                "link": _text(row.get("pr_url")),
                "item_id": item_id,
            }
        )

    return groups


__all__ = [
    "APPROVAL_TITLE",
    "POSTPROCESS_APPROVAL_SOURCE",
    "REVIEW_GROUPS",
    "build_review_queue",
]