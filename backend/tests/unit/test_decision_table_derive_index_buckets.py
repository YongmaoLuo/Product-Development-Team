"""Table 9 — which of the four buckets does a verdict land in?

Why this table exists
---------------------
``verification_buckets.derive_index_buckets`` is the one place that
answers "is this verification point done, failed, skipped, or still
waiting?".  It is a Tier A point: the answer decides what the report
says about the round **and** which verdicts become the input of the
next round's repair tasks — a verdict that lands in the failed bucket
turns into "go and fix this", and a verdict that lands nowhere is
forgotten.

The four buckets mean four different things:

* ``completed_vps`` — the verdict passed;
* ``failed_vps``    — the verdict failed, or could not be reached at
  all (``BLOCKED``); **this bucket is the input of the repair tasks**;
* ``skipped_vps``   — the verdict was skipped or split; running it
  again would only skip it again;
* ``deferred_vps``  — the verdict was deferred or inconclusive:
  neither a success nor a failure, so the next round asks again.

What the table pins
-------------------
Every row states a concrete value, and every id says why the row is
there:

* ``status-maps-to-four-buckets`` — the whole status → bucket map in
  one row: each of the four buckets holds exactly the statuses that
  belong to it;
* ``blocked-is-failure`` — ``BLOCKED`` means "could not be reached",
  and it belongs to the failed bucket (this row is also the task
  brief's example, row for row);
* ``deferred-and-inconclusive-are-deferred`` — "the machine could not
  answer" is neither a success nor a failure: it waits in the
  deferred bucket and **never** becomes a repair task;
* ``retired-superseded-is-skipped`` — a verdict the plan replaced by
  another one lands in the skipped bucket, so the next round does not
  run the big parent again;
* ``retired-obsolete-is-dropped`` — a verdict the plan retired lands
  in **no** bucket at all: a failure for a criterion nobody asks for
  would invent work;
* ``insertion-order-preserved`` — the order inside a bucket is the
  order of the verdict table, so the dashboard's counts line up with
  the verdict table line by line;
* ``unknown-status-enters-no-bucket`` — a verdict nobody can read is
  not guessed at: it lands nowhere, and the next round asks again;
* ``empty-verdict-table-gives-four-empty-buckets`` — an empty table
  still answers with the four keys, so the caller never has to ask
  whether the buckets are there.

Scope, stated honestly: this file **reads** the function it pins.  It
never writes to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

#: The backend package root — this file lives at ``backend/tests/unit/``.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_buckets import derive_index_buckets  # noqa: E402
from tests.judgment_tables import (  # noqa: E402
    Case,
    assert_decision_table,
    assert_full_branch_coverage,
)

#: The repository root — three parents up from ``backend/tests/unit/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The manifest that records which table pins which judgment point.
_MANIFEST = _REPO_ROOT / "docs" / "judgment_points.json"
#: The manifest's row for the point this table pins.
_MANIFEST_ROW = ("verification_buckets.py", "derive_index_buckets")
#: The value that row's ``table`` field has to carry.
_MANIFEST_TABLE = (
    "backend/tests/unit/test_decision_table_derive_index_buckets.py::CASES"
)

#: The four buckets, in the order they are reported in.  A verdict table
#: of any size answers with exactly these four keys.
_EMPTY_BUCKETS: Dict[str, List[str]] = {
    "completed_vps": [],
    "failed_vps": [],
    "skipped_vps": [],
    "deferred_vps": [],
}

#: One verdict per status the buckets know about: passed, failed,
#: skipped, split, deferred, inconclusive.
_VERDICTS_ALL_STATUSES = {
    "a": {"status": "PASSED"},
    "b": {"status": "FAILED"},
    "c": {"status": "SKIPPED"},
    "d": {"status": "SPLIT"},
    "e": {"status": "DEFERRED"},
    "f": {"status": "INCONCLUSIVE"},
}

#: A passed verdict next to a blocked one — the task brief's example.
_VERDICTS_PASSED_AND_BLOCKED = {
    "a": {"status": "PASSED"},
    "b": {"status": "BLOCKED"},
}

#: A deferred verdict next to an inconclusive one: "the machine could
#: not answer" twice, in two different words.
_VERDICTS_DEFERRED_AND_INCONCLUSIVE = {
    "d": {"status": "DEFERRED"},
    "i": {"status": "INCONCLUSIVE"},
}

#: A failed verdict whose verification point the plan retired.
_VERDICTS_RETIRED = {"old": {"status": "FAILED"}}
#: The plan's two ways of retiring a verification point: the parent was
#: replaced by a child, or the criterion is not wanted any more.
_RETIRED_SUPERSEDED = {"old": "superseded_by"}
_RETIRED_OBSOLETE = {"old": "obsolete"}

#: Verdicts that land in no bucket at all: a status nobody knows, a
#: verdict without a status, and a verdict that is not a verdict.
_VERDICTS_UNREADABLE = {
    "u": {"status": "WAT"},
    "m": {},
    "n": "not-a-dict",
}

#: Two verdicts per bucket, written in an order that is not the
#: alphabetical one — the buckets have to keep the order of the table.
_VERDICTS_OUT_OF_ORDER = {
    "p2": {"status": "PASSED"},
    "f1": {"status": "FAILED"},
    "p1": {"status": "PASSED"},
    "f2": {"status": "FAILED"},
}


def _derive(
    verdicts: Any,
    retired: Optional[Dict[str, str]] = None,
) -> Dict[str, List[str]]:
    """The pinned function, with ``retired`` reachable positionally.

    ``derive_index_buckets`` takes ``retired`` as a **keyword-only**
    argument, and the table machinery calls the function under test with
    positional arguments only.  This adapter is that one difference and
    nothing else: it hands the retired mapping to the pinned function
    and gives back the pinned function's buckets.
    """
    return derive_index_buckets(verdicts, retired=retired)


#: The table itself (§4.2).  Every row names a judgment value, and the
#: rows between them reach every branch of the function.
CASES = [
    Case(
        id="status-maps-to-four-buckets",
        inputs=(_VERDICTS_ALL_STATUSES,),
        expected={
            "completed_vps": ["a"],
            "failed_vps": ["b"],
            "skipped_vps": ["c", "d"],
            "deferred_vps": ["e", "f"],
        },
    ),
    Case(
        id="blocked-is-failure",
        inputs=(_VERDICTS_PASSED_AND_BLOCKED,),
        expected={
            "completed_vps": ["a"],
            "failed_vps": ["b"],
            "skipped_vps": [],
            "deferred_vps": [],
        },
    ),
    Case(
        id="deferred-and-inconclusive-are-deferred",
        inputs=(_VERDICTS_DEFERRED_AND_INCONCLUSIVE,),
        expected={
            "completed_vps": [],
            "failed_vps": [],
            "skipped_vps": [],
            "deferred_vps": ["d", "i"],
        },
    ),
    Case(
        id="retired-superseded-is-skipped",
        inputs=(_VERDICTS_RETIRED, _RETIRED_SUPERSEDED),
        expected={
            "completed_vps": [],
            "failed_vps": [],
            "skipped_vps": ["old"],
            "deferred_vps": [],
        },
    ),
    Case(
        id="retired-obsolete-is-dropped",
        inputs=(_VERDICTS_RETIRED, _RETIRED_OBSOLETE),
        expected={
            "completed_vps": [],
            "failed_vps": [],
            "skipped_vps": [],
            "deferred_vps": [],
        },
    ),
    Case(
        id="insertion-order-preserved",
        inputs=(_VERDICTS_OUT_OF_ORDER,),
        expected={
            "completed_vps": ["p2", "p1"],
            "failed_vps": ["f1", "f2"],
            "skipped_vps": [],
            "deferred_vps": [],
        },
    ),
    Case(
        id="unknown-status-enters-no-bucket",
        inputs=(_VERDICTS_UNREADABLE,),
        expected=dict(_EMPTY_BUCKETS),
    ),
    Case(
        id="empty-verdict-table-gives-four-empty-buckets",
        inputs=({},),
        expected=dict(_EMPTY_BUCKETS),
    ),
]

#: The rows of the table that the pinned function can be called with
#: directly: the ones that carry no retired mapping, because
#: ``retired`` is keyword-only there and the coverage machinery calls
#: the function under test positionally.  They are what says that every
#: branch of ``derive_index_buckets`` itself is reached by a row of
#: this table.
_PINNED_CASES = [case for case in CASES if len(case.inputs) == 1]


def test_decision_table() -> None:
    """The table is the test: every row states a value and every branch is reached.

    The helper checks three things at once — the rows are usable (ids
    are unique and say something), every row's value is the value the
    row expects (compared by type as well as by equality), and the rows
    reach every branch of the function under test.

    The function under test here is :func:`_derive`, the one-line
    adapter that lets the retired mapping travel positionally; the
    adapter has no branches of its own, so the coverage half is asked of
    the pinned function itself, with the table's own rows.
    """
    assert_decision_table(_derive, CASES)
    assert_full_branch_coverage(derive_index_buckets, _PINNED_CASES)


def test_manifest_points_at_this_table() -> None:
    """The manifest names this table as the one that pins the point.

    A manifest row whose ``table`` field points at a file that does not
    exist — or at a table that does not carry the case list — reads as
    "this point is pinned" while nothing pins it.  The link is checked
    here, where the table it names lives, so a rename of either side
    shows up as a red test instead of as a manifest nobody reads.
    """
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    rows = [
        point
        for point in manifest["points"]
        if (point["module"], point["function"]) == _MANIFEST_ROW
    ]

    assert len(rows) == 1, (
        f"the manifest holds {len(rows)} rows for "
        f"{_MANIFEST_ROW[0]}::{_MANIFEST_ROW[1]}; it has to hold exactly one"
    )
    assert rows[0]["table"] == _MANIFEST_TABLE, (
        f"the manifest's row for {_MANIFEST_ROW[0]}::{_MANIFEST_ROW[1]} "
        f"points at {rows[0]['table']!r}; it has to point at "
        f"{_MANIFEST_TABLE!r}"
    )
