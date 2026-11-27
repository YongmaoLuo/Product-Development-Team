"""Table 8 — does a deferred gate layer get its turn this round?

Why this table exists
---------------------
``DEFERRED`` is not a verdict: it says "this layer has not run *this
round*".  So the moment the thing that held it back goes away, the
layer has to run — otherwise "the full gate really ran and it was
green" can never be delivered, and the round ends with a gate that was
never asked.

:meth:`verification_executor.VerificationExecutor.rerun_deferred_gate_layers`
is the method that does the re-asking.  It is a Tier A point: what it
dispatches (and what it leaves deferred) is what the round's verdicts
are built from, so a layer that is dropped without being run reads as
"green" when nothing ever ran.

What the table pins
-------------------
Every row states a concrete value, and every id says why the row is
there:

* ``unblocked-layer-dispatched`` — nothing blocks the layer any more,
  so it is dispatched and the id of its first item is reported back:
  this is the whole point of the method;
* ``blocked-layer-kept`` — something still blocks it, so nothing is
  dispatched and the layer stays on the deferred list: a layer that is
  still waiting must not be quietly dropped;
* ``unknown-blocker-marks-the-layer-unavailable`` — the upstream
  reached no verdict at all (``UNKNOWN``), so the layer is *unavailable*
  rather than *deferred*: it still is not dispatched, but it must not
  be recorded as "waiting for a failure that is known";
* ``empty-layer-skipped`` — a layer with no items in it has nothing to
  run, so it is skipped rather than dispatched.

The rows below say what the method *returns*.  The side effects — which
items ran, which layer was deferred and with which reason, which layer
left the deferred list — are asserted by the tests that follow the
table, because a returned list cannot say them.

Scope, stated honestly: this file **reads** the method it pins.  It
never writes to it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

#: The backend package root — this file lives at ``backend/tests/unit/``.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_buckets import STATUS_UNKNOWN  # noqa: E402
from verification_executor import VerificationExecutor  # noqa: E402
from tests.judgment_tables import Case, assert_decision_table  # noqa: E402

#: The repository root — three parents up from ``backend/tests/unit/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The manifest that records which table pins which judgment point.
_MANIFEST = _REPO_ROOT / "docs" / "judgment_points.json"
#: The manifest's row for the point this table pins.
_MANIFEST_ROW = (
    "verification_executor.py",
    "VerificationExecutor.rerun_deferred_gate_layers",
)
#: The value that row's ``table`` field has to carry.
_MANIFEST_TABLE = (
    "backend/tests/unit/test_decision_table_rerun_deferred_gate_layers.py"
    "::CASES"
)

#: The reason a still-blocked layer is deferred with.
_BLOCK_REASON = "budget exhausted"
#: The reason the method writes into the deferral record.
_DEFERRED_REASON = f"Phase 2 关卡延后：{_BLOCK_REASON}"
#: The reason the method writes into the "unavailable" record.
_UNAVAILABLE_REASON = f"Phase 2 关卡不可用：{_BLOCK_REASON}"
#: The logger the executor writes its dispatch failures to.
_EXECUTOR_LOGGER = "verification_executor"


def _dispatch_failures(caplog) -> List[logging.LogRecord]:
    """The records the executor logged as a failure of the dispatch.

    Only records that carry an exception count: a dispatch failure is
    logged with the traceback, and a plain error message is something
    else.
    """
    return [
        record
        for record in caplog.records
        if record.name == _EXECUTOR_LOGGER and record.exc_info
    ]


class _ExecutorStub:
    """The stand-in for the executor.

    The method under test is called unbound, with this object as
    ``self``.  It holds the deferred layers, answers "is this layer
    still blocked?", and records what the method did with each layer.
    """

    def __init__(
        self,
        block_reason: str,
        layers: List[List[Dict[str, Any]]],
    ) -> None:
        self.block_reason = block_reason
        #: The layers the stub starts from; :meth:`reset` copies them.
        self.template_layers = layers
        self.reset()

    def reset(self) -> None:
        """Put the stub back into the state the table starts from.

        The method under test **removes** a layer it dispatched, so a
        stub that kept its state would answer differently the second
        time the table ran.  A table has to give the same answer every
        time it runs.
        """
        self._deferred_gate_layers: List[List[Dict[str, Any]]] = [
            [dict(item) for item in layer] for layer in self.template_layers
        ]
        #: The ids of the items that were actually dispatched.
        self.executed: List[str] = []
        #: The deferral records the method wrote.
        self.marked: List[Dict[str, Any]] = []
        #: The "unavailable" records the method wrote.
        self.unknown_marked: List[Dict[str, Any]] = []

    # -- what the method under test calls ---------------------------------

    def _discover_deferred_gate_layers(self) -> None:
        """Bring the deferred layers back from the ledger.

        The stub's deferred layers *are* its ledger (it has no store of
        its own), so there is nothing to bring back — the method under
        test may call this, and the stub answers honestly: the layers it
        starts from are the ones the ledger knows about.
        """
        return None

    def _gate_block_reason_for(self, gate: Any) -> str:
        """The reason this layer is still blocked (empty = it may run)."""
        return self.block_reason

    def _gate_block_decision_for(self, gate: Any) -> Any:
        """The blocking decision of this layer: ``(reason, status)``.

        2026-11-27: the executor answers "may this layer run?" with a
        **pair** — the reason *and* the upstream status — because "the
        upstream never reached a verdict" (``UNKNOWN``) and "the
        upstream is known to have failed" (``FAILED``) mean different
        things downstream.  The stub carries no upstream status, so it
        hands back the reason with no status, exactly as the executor
        does when nothing upstream is known to have failed.
        """
        return (self._gate_block_reason_for(gate), None)

    @staticmethod
    def _reason_marks_unknown(detail: str) -> bool:
        """Whether the wording of a reason says "the upstream is UNKNOWN".

        The same wording check the executor does: it is a fallback for
        the case where the reason was handed in from the outside, so
        there is no status to read.
        """
        return "结论为 UNKNOWN" in detail

    def _reason_names_a_verdict(self, reason: str) -> bool:
        """Whether the reason names a verdict that really exists.

        Mirrors :meth:`VerificationExecutor._reason_names_a_verdict`: a
        reason that says "尚未" says the executor cannot *see* the
        blocking verdict.
        """
        return "尚未" not in reason

    def _mark_layer_deferred(
        self,
        layer: List[Dict[str, Any]],
        reason: str,
        refresh: bool = False,
    ) -> None:
        """Record that the layer is deferred again, and why."""
        self.marked.append(
            {
                "layer": [dict(item) for item in layer],
                "reason": reason,
                "refresh": refresh,
            }
        )

    def _mark_layer_unknown(
        self,
        layer: List[Dict[str, Any]],
        reason: str,
    ) -> None:
        """Record that the layer is unavailable ("no verdict upstream")."""
        self.unknown_marked.append(
            {
                "layer": [dict(item) for item in layer],
                "reason": reason,
            }
        )

    def _derive_item_id(self, item: Dict[str, Any]) -> str:
        """The id of a verification item."""
        return str(item["id"])

    async def _execute_one_item(self, item: Dict[str, Any], gate: Any) -> None:
        """Run one verification item."""
        self.executed.append(str(item["id"]))


class _FailingExecutorStub(_ExecutorStub):
    """A stand-in whose items blow up when they are dispatched."""

    async def _execute_one_item(self, item: Dict[str, Any], gate: Any) -> None:
        self.executed.append(str(item["id"]))
        raise RuntimeError("this item blew up")


class _LedgerOnlyBlockedExecutorStub(_ExecutorStub):
    """A stand-in whose layer **only the ledger** knows about.

    The memory table is empty when the round close starts; the ledger still
    holds the deferral, so the discovery brings the layer back.  The
    executor cannot see the blocking verdict ("尚未产生结论"), so the reason
    already in the ledger ("结论为 BLOCKED") is more accurate than anything
    the executor could recompute — it must be left alone.
    """

    #: The reason a rebuilt executor computes when it cannot see the
    #: blocking verdict.
    _BLIND_REASON = "Phase 1 的 VP-001 本轮尚未产生结论"

    def __init__(self) -> None:
        super().__init__(self._BLIND_REASON, [])

    def _discover_deferred_gate_layers(self) -> None:
        """Bring the deferred layer back from the ledger."""
        self._deferred_gate_layers.append([{"id": "L1"}])


class _UnstartableExecutorStub(_ExecutorStub):
    """A stand-in whose dispatch cannot even be started."""

    def _execute_one_item(self, item: Dict[str, Any], gate: Any) -> Any:
        # Not a coroutine: ``asyncio.gather`` refuses it outright, and
        # that refusal is what the pinned method has to survive.
        self.executed.append(str(item["id"]))
        return None


class _UnknownBlockingExecutorStub(_ExecutorStub):
    """A stand-in whose upstream reached no verdict at all.

    2026-11-27: "the upstream never reached a conclusion" (``UNKNOWN``)
    is not the same thing as "the upstream is known to have failed" — the
    layer is *unavailable*, not *deferred*.  The executor tells the two
    apart by the **status** it hands back with the reason, so the stub
    has to be able to hand a status back too.
    """

    def _gate_block_decision_for(self, gate: Any) -> Any:
        """The blocking decision of this layer: ``(reason, UNKNOWN)``."""
        return (self._gate_block_reason_for(gate), STATUS_UNKNOWN)


#: A layer with one verification item in it.
_LAYER_ONE = [{"id": "L1"}]
#: A layer with nothing in it.
_LAYER_EMPTY: List[Dict[str, Any]] = []

#: Nothing blocks the layer, so it may run.
_STUB_DISPATCHABLE = _ExecutorStub("", [_LAYER_ONE])
#: Something still blocks the layer.
_STUB_BLOCKED = _ExecutorStub(_BLOCK_REASON, [_LAYER_ONE])
#: The upstream reached no verdict: the layer is unavailable.
_STUB_UNKNOWN_BLOCKED = _UnknownBlockingExecutorStub(
    _BLOCK_REASON, [_LAYER_ONE]
)
#: The layer has no items to run.
_STUB_EMPTY_LAYER = _ExecutorStub("", [_LAYER_EMPTY])
#: The layer is known to the ledger only, and is still blocked by a verdict
#: this executor cannot see.
_STUB_LEDGER_ONLY_BLOCKED = _LedgerOnlyBlockedExecutorStub()


#: The table itself (§4.2).  Every row names a judgment value, and the
#: rows between them reach every branch of the method.
CASES = [
    Case(
        id="unblocked-layer-dispatched",
        inputs=(_STUB_DISPATCHABLE, None),
        expected=["L1"],
    ),
    Case(
        id="blocked-layer-kept",
        inputs=(_STUB_BLOCKED, None),
        expected=[],
    ),
    Case(
        id="unknown-blocker-marks-the-layer-unavailable",
        inputs=(_STUB_UNKNOWN_BLOCKED, None),
        expected=[],
    ),
    Case(
        id="empty-layer-skipped",
        inputs=(_STUB_EMPTY_LAYER, None),
        expected=[],
    ),
    Case(
        id="ledger-only-layer-kept-with-its-ledger-reason",
        inputs=(_STUB_LEDGER_ONLY_BLOCKED, None),
        expected=[],
    ),
]


def _reset_table_stubs() -> None:
    """Put every row's stand-in back into its starting state.

    The method under test removes the layers it dispatches, so the
    stubs have to be rewound before the rows run — otherwise the second
    run of the table would start from an empty deferred list and answer
    differently.
    """
    for case in CASES:
        stub = case.inputs[0]
        assert isinstance(stub, _ExecutorStub)
        stub.reset()


def test_decision_table() -> None:
    """The table is the test: every row states a value and every branch is reached.

    The helper checks three things at once — the rows are usable (ids
    are unique and say something), every row's value is the value the
    row expects (compared by type as well as by equality), and the rows
    reach every branch of
    :meth:`VerificationExecutor.rerun_deferred_gate_layers`.  A table
    that left a branch unreached would be a table that let a new branch
    in unnoticed.
    """
    _reset_table_stubs()

    assert_decision_table(
        VerificationExecutor.rerun_deferred_gate_layers, CASES
    )


def test_an_unblocked_layer_runs_its_items_and_leaves_the_list() -> None:
    """Nothing blocks the layer → every item in it runs, and it is forgotten.

    The layer leaving the deferred list is the point of the method: a
    layer that ran must not be deferred again next time the method is
    called, or it would run twice.
    """
    stub = _ExecutorStub("", [[{"id": "L1"}, {"id": "L2"}]])

    dispatched = asyncio.run(
        VerificationExecutor.rerun_deferred_gate_layers(stub)
    )

    assert dispatched == ["L1"], (
        "the dispatched layer is reported by the id of its first item"
    )
    assert stub.executed == ["L1", "L2"], (
        "every item of the layer has to run, not just the first one"
    )
    assert stub._deferred_gate_layers == [], (
        "a layer that ran must not stay on the deferred list"
    )


def test_a_blocked_layer_stays_deferred_with_the_current_reason() -> None:
    """Something still blocks the layer → it stays deferred, and the reason is refreshed.

    The reason has to name the blocker that is current *now*: a reason
    that names a blocker which has long since turned green is a lie
    that ends up in the report.
    """
    stub = _ExecutorStub(_BLOCK_REASON, [[{"id": "L1"}]])

    dispatched = asyncio.run(
        VerificationExecutor.rerun_deferred_gate_layers(stub)
    )

    assert dispatched == [], (
        "a layer that is still blocked must not be dispatched"
    )
    assert stub.executed == [], (
        "a layer that is still blocked must not run any of its items"
    )
    assert stub._deferred_gate_layers == [[{"id": "L1"}]], (
        "a layer that is still blocked has to stay on the deferred list"
    )
    assert stub.marked == [
        {
            "layer": [{"id": "L1"}],
            "reason": _DEFERRED_REASON,
            "refresh": True,
        }
    ], (
        "the deferral record has to name the current blocker and has to "
        "be refreshed, not left at the reason of an earlier round"
    )


def test_a_layer_blocked_by_an_unknown_upstream_is_marked_unavailable() -> None:
    """The upstream reached no verdict → the layer is unavailable, not deferred.

    "The upstream never reached a conclusion" and "the upstream is known
    to have failed" are different facts, and they lead to different
    records: a layer waiting for a *known* failure is deferred, a layer
    waiting for a conclusion that never came is unavailable.  Recording
    the second as the first tells the reader that a failure is known
    when it is not.
    """
    stub = _UnknownBlockingExecutorStub(_BLOCK_REASON, [[{"id": "L1"}]])

    dispatched = asyncio.run(
        VerificationExecutor.rerun_deferred_gate_layers(stub)
    )

    assert dispatched == [], (
        "a layer whose upstream has no verdict must not be dispatched"
    )
    assert stub.executed == [], (
        "a layer whose upstream has no verdict must not run any of its items"
    )
    assert stub._deferred_gate_layers == [[{"id": "L1"}]], (
        "an unavailable layer stays on the deferred list: UNKNOWN is not "
        "terminal, so the layer still has to be asked later"
    )
    assert stub.unknown_marked == [
        {
            "layer": [{"id": "L1"}],
            "reason": _UNAVAILABLE_REASON,
        }
    ], (
        "the layer has to be recorded as unavailable, and the record has "
        "to name the reason the upstream has no verdict"
    )
    assert stub.marked == [], (
        "an unavailable layer must not also be recorded as deferred: that "
        "would claim a failure is known when it is not"
    )


def test_an_empty_layer_is_left_alone() -> None:
    """A layer with no items has nothing to run → it is skipped, not dispatched."""
    stub = _ExecutorStub("", [[]])

    dispatched = asyncio.run(
        VerificationExecutor.rerun_deferred_gate_layers(stub)
    )

    assert dispatched == [], (
        "a layer with no items has nothing to dispatch"
    )
    assert stub.executed == [], (
        "a layer with no items must not run anything"
    )
    assert stub.marked == [], (
        "a layer with no items must not be written a deferral record"
    )
    assert stub._deferred_gate_layers == [[]], (
        "an empty layer is skipped, not removed: it is not the method's "
        "business to decide that it is meaningless"
    )


def test_an_item_that_fails_does_not_break_the_round(caplog) -> None:
    """One item blows up → the round survives it, and the layer still ran.

    A single failing verification item must not take the whole re-run
    down with it: the **other** items of the layer still have to be
    asked, and the round has to reach its end.

    The failing item is *that item's* failure, not a failure of the
    dispatch: the dispatch did its job, so the executor's dispatch-error
    handler must not run.  The two assertions here and in the test below
    check each other — a log assertion that could never fire would
    prove nothing, and one that always fires would prove nothing
    either.
    """
    stub = _FailingExecutorStub("", [[{"id": "L1"}, {"id": "L2"}]])

    with caplog.at_level(logging.ERROR, logger=_EXECUTOR_LOGGER):
        dispatched = asyncio.run(
            VerificationExecutor.rerun_deferred_gate_layers(stub)
        )

    assert stub.executed == ["L1", "L2"], (
        "the failing item was dispatched, and the item behind it was "
        "asked too: one failure must not take the rest of the layer "
        "down with it"
    )
    assert dispatched == ["L1"], (
        "a layer whose item failed has still run: the round must not be "
        "broken by one failing item"
    )
    assert _dispatch_failures(caplog) == [], (
        "a failing item is that item's own failure, not a failure of the "
        "dispatch: the executor's dispatch-error handler must not run"
    )


def test_a_dispatch_that_cannot_start_does_not_break_the_round(caplog) -> None:
    """The dispatch itself blows up → the round survives it.

    This is the other way the dispatch can go wrong: the gather never
    gets to run at all.  The round still has to reach its end, and the
    layer still counts as dispatched — it was taken off the deferred
    list, so pretending otherwise would hide the gap.  And the failure
    *is* a failure of the dispatch, so this time the executor's
    dispatch-error handler has to run and say so.
    """
    stub = _UnstartableExecutorStub("", [[{"id": "L1"}]])

    with caplog.at_level(logging.ERROR, logger=_EXECUTOR_LOGGER):
        dispatched = asyncio.run(
            VerificationExecutor.rerun_deferred_gate_layers(stub)
        )

    assert stub.executed == ["L1"], (
        "the dispatch was attempted — it is the failure that has to be "
        "survived"
    )
    assert dispatched == ["L1"], (
        "a layer whose dispatch could not start has still been taken "
        "off the deferred list, so it has to be reported as dispatched"
    )
    assert len(_dispatch_failures(caplog)) == 1, (
        "a dispatch that could not start is a failure of the dispatch "
        "itself, and it has to be recorded as one"
    )


# ---------------------------------------------------------------------------
# The ledger is the truth: a gate layer the memory table forgot still runs
# ---------------------------------------------------------------------------
#
# 2026-11-27 (``20261116-K-Magic-DNS``, round 1): the two Phase 2 full gates
# (VP-017 / VP-018) were deferred by the gate at ~11:13 with the reason
# "Phase 2 关卡延后：Phase 1 的 VP-001 结论为 BLOCKED".  VP-001 turned green
# later that round (PASSED at 12:04) — and the two gates were **never
# dispatched at all** (no ``vp_start`` event for them in the round's log),
# while the report still carried the DEFERRED entry with the *stale* BLOCKED
# reason.
#
# The break is not in the judgment (the gate is only released by Phase 1's
# final conclusion) but in **where the deferred layer is remembered**:
# ``_deferred_gate_layers`` lives in the process, the ledger
# (``_verdicts_by_round``) is the round's truth.  The round close runs on a
# **rebuilt** executor (resume / new process / restart), so its in-memory
# table is empty — and ``rerun_deferred_gate_layers`` used to consult *only*
# that table.  "This layer has not run yet" was therefore forgotten at the
# one moment that could still act on it.


def _gate_plan() -> Dict[str, Any]:
    """A minimal plan: one Phase 1 VP and one Phase 2 full gate."""
    return {
        "vps": [
            {
                "id": "VP-001",
                "method": "automated_test",
                "title": "vp VP-001",
                "expected_result": "通过",
                "test_command": "pytest tests/VP-001.py",
            },
            {
                "id": "VP-GATE",
                "method": "automated_test",
                "title": "全量 Nightly CI",
                "expected_result": "通过",
                "test_command": "pytest tests/VP-GATE.py",
                "verification_phase": 2,
                "phase_order": 1,
            },
        ]
    }


def _round_executor(
    plan: Dict[str, Any],
    tmp_path: Path,
    verdicts: Dict[str, str],
    *,
    round_no: Optional[int] = 1,
):
    """An executor whose runner answers ``verdicts[vp_id]``.

    Two executors built on the same ``tmp_path`` share one ``plan_dir``,
    so the second one reads back what the first one wrote — that is how a
    round continuation sees the earlier part of the same round (and how
    the process-local deferred-layer table is lost while the ledger
    survives).
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    calls: List[str] = []

    async def runner(vp: Dict[str, Any]) -> Dict[str, Any]:
        vp_id = vp["id"]
        calls.append(vp_id)
        return {
            "status": verdicts.get(vp_id, "PASSED"),
            "reasons": [f"{vp_id} stub"],
            "evidence": {},
        }

    executor = VerificationExecutor(
        verification_plan=plan,
        plan_id="plan-ledger-gate",
        plan_dir=plan_dir,
        sub_agent_runner=runner,
        verif_repo=None,
        round_no=round_no,
    )
    return executor, calls


def test_the_round_close_reruns_a_gate_layer_only_the_ledger_knows(
    tmp_path: Path,
) -> None:
    """A deferred gate layer the memory table forgot is still re-run at the close.

    The production sequence (2026-11-27, ``20261116-K-Magic-DNS`` round 1):

    1. Phase 1's VP-001 is BLOCKED → the Phase 2 gate layer is deferred:
       the ledger gets the ``DEFERRED`` entry with
       ``evidence.gate == "verification_phase_2"``;
    2. VP-001 turns green (its verdict is rewritten to PASSED);
    3. the **round close** runs — on a rebuilt executor, whose process-local
       deferred-layer table is empty while the ledger still holds the
       deferral;
    4. the gate layer must really run in this round, and its DEFERRED entry
       must be superseded by a real conclusion.

    Without step 3 consulting the ledger, the layer is silently dropped:
    the round ends with a gate that was never asked, and the report keeps
    the DEFERRED entry — with a reason that names a blocker which is
    already green.
    """
    plan = _gate_plan()

    # 1. The blocker is BLOCKED → the gate layer is deferred.
    first, first_calls = _round_executor(
        plan, tmp_path, {"VP-001": "BLOCKED"}, round_no=1,
    )
    asyncio.run(first.run(max_parallel=1))
    assert "VP-GATE" not in first_calls, (
        "the gate must not run while the blocker is still blocked"
    )
    assert first._verdicts["VP-GATE"]["status"] == "DEFERRED", (
        "the deferred gate layer must leave a DEFERRED entry in the ledger"
    )
    assert first._verdicts["VP-GATE"]["evidence"] == {
        "gate": "verification_phase_2",
    }, (
        "the DEFERRED entry has to be the one the gate itself wrote — it is "
        "what tells the round close that this layer has not run yet"
    )

    # 2. The blocker turns green.
    first.record_verdict(
        "VP-001",
        {"status": "PASSED", "reasons": ["重试通过"], "evidence": {}},
    )

    # 3. The round close — on a rebuilt executor: the ledger is still
    #    there, the process-local table is not.
    second, second_calls = _round_executor(plan, tmp_path, {}, round_no=1)
    assert second._deferred_gate_layers == [], (
        "the process-local deferred-layer table is empty after the rebuild: "
        "nobody in this process remembers that the gate layer has not run"
    )
    assert second._current_round_bucket()["VP-GATE"]["status"] == "DEFERRED", (
        "the ledger still holds the deferral — it is the round's truth"
    )
    assert second._verdicts["VP-001"]["status"] == "PASSED", (
        "the blocker is green in this executor's view"
    )

    dispatched = asyncio.run(second.rerun_deferred_gate_layers())

    # 4. The layer really ran, in this round.
    assert dispatched == ["VP-GATE"], (
        "the round close has to dispatch the gate layer the ledger says has "
        f"not run yet; it dispatched {dispatched!r}"
    )
    assert "VP-GATE" in second_calls, (
        "the gate layer's items have to actually run, not just be reported"
    )
    assert second._verdicts["VP-GATE"]["status"] == "PASSED", (
        "the re-run has to produce a real conclusion — a DEFERRED left "
        "behind reads as 'this round never asked'"
    )
    assert not [
        entry
        for entry in second._current_round_bucket().values()
        if isinstance(entry, dict)
        and str(entry.get("status") or "") == "DEFERRED"
    ], (
        "the DEFERRED entry has to be superseded by the real conclusion"
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
