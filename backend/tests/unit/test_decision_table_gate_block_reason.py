"""Table 7 — the reason a gate layer is held back.

Why this table exists
---------------------
:meth:`verification_executor.VerificationExecutor._gate_block_reason_for`
is the one place that answers "may this gate layer run yet?".  It is a
Tier A point: the answer decides whether a whole layer of verification
is dispatched or deferred, so a wrong empty string lets a gate run
before the work it depends on has finished, and a wrong reason sends
the report off naming a blocker that is long gone.

The answer comes from exactly two sources, and they do not weigh the
same:

* a **global** blocker — something outside the gate layers (Phase 1's
  verification points) has not finished successfully;
* a **predecessor** blocker — an *earlier* gate layer has not run yet.

The global one outranks the predecessor one: a layer whose predecessor
has not run is worth mentioning only when nothing global is wrong.
Both are read through a single expression, so the table below is where
that precedence is pinned.

What the table pins
-------------------
Every row states a concrete value, and every id says why the row is
there:

* ``global-blocker-wins`` — the global reason is the answer even
  though a predecessor reason is available too: the two sources do not
  weigh the same;
* ``predecessor-blocker-reported`` — with no global blocker the
  predecessor's reason is the answer, and the predecessor check is the
  one asked about *this* gate's phase order;
* ``no-blocker-is-empty-string`` — neither source blocks anything, so
  the answer is the empty string, which is what "this layer may run"
  means.

Scope, stated honestly: this file **reads** the method it pins.  It
never writes to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, List

#: The backend package root — this file lives at ``backend/tests/unit/``.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_executor import VerificationExecutor  # noqa: E402
from verification_phases import infer_phase_order  # noqa: E402
from tests.judgment_tables import Case, assert_decision_table  # noqa: E402

#: The repository root — three parents up from ``backend/tests/unit/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The manifest that records which table pins which judgment point.
_MANIFEST = _REPO_ROOT / "docs" / "judgment_points.json"
#: The manifest's row for the point this table pins.
_MANIFEST_ROW = (
    "verification_executor.py",
    "VerificationExecutor._gate_block_reason_for",
)
#: The value that row's ``table`` field has to carry.
_MANIFEST_TABLE = (
    "backend/tests/unit/test_decision_table_gate_block_reason.py::CASES"
)

#: The reason a global blocker gives — the words the executor itself
#: uses for "this run is out of budget".
_GLOBAL_REASON = "budget exhausted"
#: The reason an earlier gate layer gives, in the executor's own words.
_PREDECESSOR_REASON = "更早的全量关卡 G1 尚未执行"

#: A gate that asks for the second layer of the full gates.  Its
#: ``phase_order`` is what the predecessor check is asked about.
_GATE_WITH_ORDER = {"phase_order": 2}


class _GateStub:
    """The stand-in for the executor.

    The method under test is called unbound, with this object as
    ``self``.  It answers the two questions the method asks — "is
    something global blocking?" and "is an earlier gate missing?" —
    and it records the phase order it was asked about, so a test can
    tell the two sources apart.
    """

    def __init__(self, global_reason: str, predecessor_reason: str) -> None:
        self.global_reason = global_reason
        self.predecessor_reason = predecessor_reason
        #: The phase orders the predecessor check was asked about, in
        #: the order it was asked.
        self.predecessor_orders: List[Any] = []

    def _blocking_gate_reason(self) -> str:
        """The reason something outside the gate layers is blocking."""
        return self.global_reason

    def _gate_predecessor_reason(self, gate_order: Any) -> str:
        """The reason an earlier gate layer is missing."""
        self.predecessor_orders.append(gate_order)
        return self.predecessor_reason

    def _gate_block_decision_for(self, gate: Any) -> Any:
        """The blocking decision of this layer: ``(reason, status)``.

        2026-11-27: the executor answers "may this gate layer run?"
        with a **pair** — the reason *and* the upstream status — so the
        stub answers with the same shape.  The reason is the global
        blocker's when there is one, and the predecessor's otherwise,
        and the predecessor is asked about the gate's own phase order,
        the way the executor asks it.
        """
        global_reason = self._blocking_gate_reason()
        if global_reason:
            return (global_reason, None)
        gate_order = infer_phase_order(gate) if isinstance(gate, dict) else None
        return (self._gate_predecessor_reason(gate_order), None)


#: Both sources block: the global one is the answer.
_STUB_BOTH_BLOCKED = _GateStub(_GLOBAL_REASON, _PREDECESSOR_REASON)
#: Only the predecessor blocks.
_STUB_PREDECESSOR_BLOCKED = _GateStub("", _PREDECESSOR_REASON)
#: Nothing blocks.
_STUB_CLEAR = _GateStub("", "")


#: The table itself (§4.2).  Every row names a judgment value, and the
#: rows between them reach every branch of the method.
CASES = [
    Case(
        id="global-blocker-wins",
        inputs=(_STUB_BOTH_BLOCKED, None),
        expected=_GLOBAL_REASON,
    ),
    Case(
        id="predecessor-blocker-reported",
        inputs=(_STUB_PREDECESSOR_BLOCKED, _GATE_WITH_ORDER),
        expected=_PREDECESSOR_REASON,
    ),
    Case(
        id="no-blocker-is-empty-string",
        inputs=(_STUB_CLEAR, _GATE_WITH_ORDER),
        expected="",
    ),
]


def test_decision_table() -> None:
    """The table is the test: every row states a value and every branch is reached.

    The helper checks three things at once — the rows are usable (ids
    are unique and say something), every row's value is the value the
    row expects (compared by type as well as by equality), and the rows
    reach every branch of
    :meth:`VerificationExecutor._gate_block_reason_for`.  A table that
    left a branch unreached would be a table that let a new branch in
    unnoticed.
    """
    assert_decision_table(VerificationExecutor._gate_block_reason_for, CASES)


def test_the_gate_order_reaches_the_predecessor_check() -> None:
    """The predecessor check is asked about *this* gate's phase order.

    The reason a layer is held back is a function of the gate, not of
    the executor's mood: a gate that carries a ``phase_order`` is
    checked against that order, and a gate that is not a mapping has no
    order to check against.  Without this, the table could pass on a
    method that asked the predecessor check about something else
    entirely.
    """
    stub = _GateStub("", _PREDECESSOR_REASON)

    VerificationExecutor._gate_block_reason_for(stub, _GATE_WITH_ORDER)

    assert stub.predecessor_orders == [2], (
        "the predecessor check has to be asked about the gate's own phase "
        f"order; it was asked about {stub.predecessor_orders!r}"
    )

    other = _GateStub("", _PREDECESSOR_REASON)

    VerificationExecutor._gate_block_reason_for(other, None)

    assert other.predecessor_orders == [None], (
        "a gate that is not a mapping has no phase order, so the "
        "predecessor check has to be asked about nothing; it was asked "
        f"about {other.predecessor_orders!r}"
    )


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
