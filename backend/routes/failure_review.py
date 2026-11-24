"""The failure-review read-only endpoint.

``GET /api/plan/{plan_id}/failure-review`` answers with what was
announced about a failed plan: the decision it was announced as, what
went wrong, what should happen about it, and when it was announced.

What it reads
-------------
The announcement leaves a **sidecar** behind —
``<plan_dir>/failure_review.json`` — and this endpoint reads that file
(:func:`failure_review.read_review`).  It does not query the decision
store: the store is the decision's own, writable home, and a read-only
consumer that went there would be a second reader of a table that the
decision service also writes.  The sidecar is a snapshot of what was
announced, which is exactly what a reader wants to show.

What it writes
--------------
Nothing.  The endpoint resolves a path and reads one file; it creates no
file, stamps no phase, and settles no decision.  A read that also wrote
would be a second writer of the plan's state.

The two modules of the same name
--------------------------------
This module is ``backend/routes/failure_review.py``; the core module it
reads through is ``backend/failure_review.py``.  The import below is
therefore **absolute** on purpose: a relative import would resolve back
to this module, and the endpoint would read itself.

Late binding into ``server``
----------------------------
``server`` owns the shared helpers — notably ``_plan_dir``, the one place
a request-supplied plan id is allowed to become a path.  The module is
imported and the attribute is looked up when the request is served, so
this module can be imported while ``server`` is still loading.  See
``routes/scheduler.py`` for the same rule.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

# Late binding into the application module: ``server`` owns the shared
# helpers (notably ``_plan_dir``, the one place a request-supplied plan
# id is allowed to become a path).  The attribute is read when the
# request is served, not when this module is imported, so the import
# does not have to wait for ``server`` to finish loading.
import server as _server

# The core module — ``backend/failure_review.py`` — under a name that
# says which one this is.  The import is **absolute**: this module and
# the core module share a name, and a relative import would resolve to
# this module instead of the core one.
import failure_review as _core

router = APIRouter()

#: What a plan without a failure review is answered with.  It says what
#: is missing rather than leaving the caller to guess between "no such
#: plan" and "no review for this plan".
REVIEW_NOT_FOUND_DETAIL = "failure review not found"

#: The four fields the endpoint hands on.  The sidecar carries more —
#: the plan id, the round, the question — and those are the
#: announcement's own bookkeeping: they are not part of the answer.
REVIEW_FIELDS = ("decision_id", "problem", "suggestion", "created_at")


@router.get("/api/plan/{plan_id}/failure-review")
def get_failure_review(plan_id: str) -> Dict[str, Any]:
    """Answer with the plan's failure review, or say there is none.

    Parameters
    ----------
    plan_id:
        The plan the review belongs to.  It is admitted through the
        shared guard (``server._plan_dir``), so a malformed id — a
        traversal, a separator — is refused with the guard's own 400.
        That answer is left alone: a malformed id is a bad request, not
        a missing plan.

    Returns
    -------
    The four recorded fields: ``decision_id``, ``problem``,
    ``suggestion`` and ``created_at``.

    Raises
    ------
    HTTPException: 404 when the plan has no review to show — no
        sidecar, a sidecar that does not parse, or one missing a field.
    """
    plan_dir = _server._plan_dir(plan_id)

    review = _core.read_review(plan_dir)

    if review is None:
        raise HTTPException(404, REVIEW_NOT_FOUND_DETAIL)

    # ``read_review`` only answers with a record that carries all four
    # fields, so the reads below cannot fail on a missing key.
    return {field: review[field] for field in REVIEW_FIELDS}
