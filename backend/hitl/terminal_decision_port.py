"""The default implementation of the terminal-decision port.

Why this module exists
======================
:mod:`hitl.terminal_execution` names what a human's answer asks for —
*reset the round and start it again*, *close the plan as failed* — and
does so through a port it does not implement.  A port that has no
implementation behind it is a contract nobody honours: the executor
calls ``port.reset(...)`` and ``port.start(...)``, and nothing happens.

This module is the other half of that contract: a thin, mechanical
adapter that carries each of those calls to the thing that already
knows how to do it.

What the actions call
---------------------

==================  ==========================================================
``reset``           :func:`routes.verification.reset_verification`, with a
                     ``clear_artifacts`` request and
                     ``clear_root_cause_chain=True`` — a round that
                     restarts with the chain still hanging on it walks
                     straight back into the terminal branch
``start``           :func:`routes.verification.start_verification`
``close_as_failed`` :meth:`plan_state.PlanState.transition_to` — the
                     ``failed`` phase (retired option; still resolved)
==================  ==========================================================

The door: revise the criterion
------------------------------
*Revise the criterion* is the answer that says "the criterion is
wrong, not the deliverable" — the round's failed points are marked
**obsolete** and the round carries on without them.  The new criterion
is written by the operator **outside** the framework (a new plan, or a
delta of their own); this door does not invent verification points.

It closes in the shape the rest of the chain already speaks:

* the sidecar record's ``failed_points`` become the ids to obsolete
  (:func:`failed_vps_from_record`);
* the ids go through the **existing delta machinery**
  (:func:`verification_plan_delta.apply_delta_persisted`) — the plan
  file is rewritten by the same code that rewrites it for a
  criterion replacement, so the obsoleting keeps the same audit trail
  and the same atomic write.  Nothing here hand-writes the plan file;
* the obsoleted points are then removed from the **root-cause chain**
  (:func:`verification.orchestrator.load_root_cause_chain` /
  :func:`verification.orchestrator.save_root_cause_chain`) — the same
  ledger the orchestrator reads, written by the same functions, so the
  two cannot disagree about its shape.

**Nothing obsoleted means nothing happened.**  If the delta refuses —
the point is not in the plan, or the write fails — this method raises
with the reason, and
:func:`hitl.terminal_execution.execute_terminal_decision` is what
records it in the audit (arch decision point 5).  A silent success
would leave the human believing the criterion was revised when the
plan still carries it.

The retired door: generate repair tasks
---------------------------------------
*Generate repair tasks* used to be the answer that says "the
deliverable is wrong, go and fix it".  It is retired from the menu
(2026-11-06), but a decision recorded before that day still names it,
so the door stays open:

* the sidecar record's ``failed_points`` become **failed-VP records**
  (:func:`failed_vps_from_record`) — the shape
  :class:`repair_generator.RepairTaskGenerator` reads;
* the generator turns them into **repair content**, and
  :class:`repair_generator.RepairTaskAssembler` stamps the local fields
  and hands back **task rows**;
* the rows go into ``state.db``'s ``plan_tasks`` table, where the
  executor's ``_get_pending_repair_tasks`` finds them.

The two ends of that chain are injected (``repair_generator_factory``
and ``task_writer``), so the mechanics can be exercised without a model
and without the operator's database.  A failure is **not** swallowed
here: the call is allowed to raise, and
:func:`hitl.terminal_execution.execute_terminal_decision` is what
records it in the audit (arch decision point 5).

Nothing here is scheduler-dependent, so nothing here takes an engine.
That is not an accident of the signature: a port that accepted an
engine would let the scheduler dependency back in through the front
door, and the mechanical actions would stop being mechanical.

The imports are function-level, and that is deliberate
-----------------------------------------------------
``routes.verification`` imports ``server`` at module level, and
``server`` imports the route modules at the bottom of its own file.  A
module-level ``from routes.verification import ...`` inside this
package therefore closes a cycle at *import* time — ``hitl`` would be
half-initialised while ``server`` is still being loaded, and whoever
imported ``hitl`` first would get a broken module.

So every call into the route layer is resolved **when the call is
made**, not when this module is imported.  By then ``server`` is
loaded and the cycle has already been closed by the normal import
order.  It also means a test can replace ``reset_verification`` on the
route module and see the replacement — which is how the mechanics of
this port are pinned in
``backend/tests/unit/test_terminal_decision_port_mechanics.py``.

The plan directory
------------------
``close_as_failed`` needs to know where the plan lives.  The port
takes a ``plan_dir_of`` resolver for that, and its default asks
``server._plan_dir`` — the one funnel every ``plans/{plan_id}`` access
goes through.  A test (or a caller that keeps its plans elsewhere)
supplies its own resolver instead.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "DefaultTerminalDecisionPort",
    "default_port",
    "failed_vps_from_record",
]


# ---------------------------------------------------------------------------
# The default plan-directory resolver
# ---------------------------------------------------------------------------


def _default_plan_dir_of(plan_id: str) -> Any:
    """Resolve a plan id to its directory, the way the routes do.

    ``server._plan_dir`` is the funnel every ``plans/{plan_id}`` access
    goes through, so using it here means this port cannot disagree with
    the routes about where a plan lives.  The import is function-level
    for the same reason the route imports are (see the module
    docstring).
    """
    import server as _server

    return _server._plan_dir(plan_id)



# ---------------------------------------------------------------------------
# The repair path
# ---------------------------------------------------------------------------

#: The priority a failed point carries when the record names none.
#:
#: A repair derived from a terminal failure is a first-priority task by
#: construction: the round stopped *because* of this failure, and
#: nothing else the plan does can move until it is answered.
_DEFAULT_REPAIR_PRIORITY = "P1"

#: The failure class a failed point carries when the record names none.
#:
#: The same default the repair chain itself uses
#: (:func:`verification.failure_history.failure_class_of`): a record
#: that predates the classification reads as "nobody said otherwise",
#: and "nobody said otherwise" has always meant "repair it".
_DEFAULT_FAILURE_CLASS = "deliverable_defect"


def _record_round_number(record: Any) -> int:
    """The round the record says stopped; ``0`` when it says nothing.

    Only a real number counts.  A round that is missing — or that
    carries something that is not a number at all — reads as round 0,
    so the repair that follows it is round 1.  It is not
    ``None + 1``: the assembler refuses a round below 1, and a repair
    task with no round cannot be grouped into a batch.
    """
    if not isinstance(record, Mapping):
        return 0
    value = record.get("round_number")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def failed_vps_from_record(record: Any) -> List[Dict[str, Any]]:
    """The record's failed points, in the shape the repair chain reads.

    The sidecar keeps the failure scene the analysis was read against
    (:func:`verification.terminal_decision_session.build_record`), and
    that scene is not in the shape
    :class:`repair_generator.RepairTaskGenerator` wants.  This is the
    translation, and it is deliberately small — every field it invents
    says something the record does not:

    ``id``
        the verification point's id, verbatim.
    ``title``
        the point's title; a point that names none falls back to its
        id, because a task whose title is empty reads as a task nobody
        wrote.
    ``priority``
        the point's priority, or ``"P1"`` — see
        :data:`_DEFAULT_REPAIR_PRIORITY`.
    ``actual_result``
        the point's **verdict** — the failure text.  A point that
        carries no verdict has no text to report, and the empty string
        says exactly that.
    ``evidence``
        always empty.  The record carries no evidence array of its own:
        what it has is the verdict, and the verdict is already in
        ``actual_result``.  Inventing one would be a second copy of the
        same fact.
    ``expected_result``
        the point's expected result, verbatim.
    ``failure_class``
        the point's failure class, or ``"deliverable_defect"`` — see
        :data:`_DEFAULT_FAILURE_CLASS`.

    A record that is not there, or that names no failed points, maps to
    an empty list.  Nothing here raises: a record that cannot be read
    is a round whose failures were never recorded, and that is a normal
    state to be in (arch decision point 5: *read it or degrade, never
    raise*).
    """
    if not isinstance(record, Mapping):
        return []
    points = record.get("failed_points")
    if not isinstance(points, (list, tuple)):
        return []

    failed_vps: List[Dict[str, Any]] = []
    for point in points:
        if not isinstance(point, Mapping):
            continue
        vp_id = str(point.get("id", "") or "")
        title = str(point.get("title", "") or "").strip() or vp_id
        priority = (
            str(point.get("priority", "") or "").strip()
            or _DEFAULT_REPAIR_PRIORITY
        )
        failure_class = (
            str(point.get("failure_class", "") or "").strip()
            or _DEFAULT_FAILURE_CLASS
        )
        failed_vps.append(
            {
                "id": vp_id,
                "title": title,
                "priority": priority,
                "actual_result": str(point.get("verdict", "") or ""),
                "evidence": [],
                "expected_result": str(point.get("expected_result", "") or ""),
                "failure_class": failure_class,
            }
        )
    return failed_vps


def _default_write_tasks(plan_id: str, rows: List[Dict[str, Any]]) -> None:
    """Write the repair rows into ``state.db``'s ``plan_tasks`` table.

    This is where the repair becomes work: the executor's
    ``_get_pending_repair_tasks`` reads ``plan_tasks``, so a row that
    never reached the table is a repair nobody runs.

    The database is :func:`config_paths.resolve_state_db_path`'s — the
    one resolver every writer in the backend uses — and it is opened
    once, migrated, written row by row, and closed.

    **Failures are not swallowed.**  A repair that could not be queued
    is a fact the caller has to hear about, and the caller that knows
    how to record it is
    :func:`hitl.terminal_execution.execute_terminal_decision` — it
    writes the failure to the audit (arch decision point 5).
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate as migrate_db
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate_db(conn)
        repo = PlanTaskRepository(conn)
        for row in rows:
            repo.add_task(plan_id, row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The port
# ---------------------------------------------------------------------------


class DefaultTerminalDecisionPort:
    """The mechanical port: a few actions, a few calls, no engine.

    Every method takes the plan id and nothing else, so the port can be
    handed to :func:`hitl.terminal_execution.execute_terminal_decision`
    as-is.

    The doors are the ones the terminal menu names: reset the round and
    start it again, obsolete the round's failed points and start it
    again, and — for a decision recorded before the menu changed —
    close the plan as failed or queue repair tasks.  The free-text door
    is gone: a human who writes something the menu does not offer does
    it outside the framework.
    """

    def __init__(
        self,
        *,
        plan_dir_of: Optional[Callable[[str], Any]] = None,
        coding_tool: Any = None,
        repair_generator_factory: Optional[Callable[[str], Any]] = None,
        task_writer: Optional[Callable[[str, List[Dict[str, Any]]], Any]] = None,
    ) -> None:
        """Build the port.

        ``plan_dir_of`` maps a plan id to the plan's directory; the
        default asks ``server._plan_dir``.  ``coding_tool`` is the tool
        the repair path calls; without one the port builds a
        :class:`coding_tool.ClaudeCodingTool` on the ``verification``
        scene when it is first needed.

        ``repair_generator_factory`` builds the repair generator the
        *generate repair tasks* path calls; it takes a plan id and
        returns something with
        ``generate_repair_contents(failed_vps, round_number)``.  The
        default builds a real
        :class:`repair_generator.RepairTaskGenerator` for that plan.

        ``task_writer`` writes the assembled rows; it takes a plan id
        and the rows.  The default writes them into ``state.db``'s
        ``plan_tasks`` table.

        There is deliberately no ``engine`` parameter: none of these
        actions needs the scheduler.
        """
        self._plan_dir_of = (
            plan_dir_of if plan_dir_of is not None else _default_plan_dir_of
        )
        self._coding_tool = coding_tool
        self._repair_generator_factory = (
            repair_generator_factory
            if repair_generator_factory is not None
            else self._default_repair_generator_factory
        )
        self._task_writer = (
            task_writer if task_writer is not None else _default_write_tasks
        )

    # -- the tool --------------------------------------------------------

    @property
    def coding_tool(self) -> Any:
        """The tool the repair path calls — built on first use when absent.

        Built on first use, not in the constructor: a port that only
        ever resets and starts a plan must not build a provider nobody
        asked for.
        """
        if self._coding_tool is None:
            from coding_tool import ClaudeCodingTool

            self._coding_tool = ClaudeCodingTool(scene="verification")
        return self._coding_tool

    # -- the actions -----------------------------------------------------

    def reset(
        self,
        plan_id: str,
        *,
        clear_artifacts: bool = True,
        clear_root_cause_chain: bool = True,
    ) -> Any:
        """Reset the round's state so it can run again.

        The round is cleared by the route that owns the reset procedure,
        with ``clear_artifacts`` saying whether the round's disk outputs
        go with it.  The flag is handed over unchanged — whether a stale
        artefact survives is not this port's decision to make.

        ``clear_root_cause_chain`` is **on** here, and that is the whole
        point of the *rerun this round* answer: the chain is the ledger
        that says which points are still standing between the plan and
        the human, and a round that restarts with the chain still
        hanging on it walks straight back into the terminal branch —
        the very state the human asked to get out of.  The chain's
        clearing is a decision, so the route's own default (off) is not
        what this path uses.
        """
        import server as _server

        from routes.verification import reset_verification

        request = _server.ResetVerificationRequest(
            clear_artifacts=clear_artifacts,
            clear_root_cause_chain=clear_root_cause_chain,
        )
        return reset_verification(plan_id, request)

    def start(self, plan_id: str) -> Any:
        """Start the plan (or the round) running.

        No request body: the round budget stays whatever the plan was
        set up with.
        """
        from routes.verification import start_verification

        return start_verification(plan_id, None)

    def close_as_failed(self, plan_id: str) -> Any:
        """Close the plan as failed — the failure is the accepted outcome.

        The plan moves to the ``failed`` phase through the state machine,
        which is what makes the outcome visible to everything that reads
        the plan's phase afterwards.
        """
        from plan_state import PlanState

        state = PlanState(self._plan_dir_of(plan_id))
        return state.transition_to("failed")

    # -- the repair path -----------------------------------------------

    def _default_repair_generator_factory(self, plan_id: str) -> Any:
        """The repair generator the port builds when nobody supplied one.

        Built on the plan's own directory and the plan's registered
        workspace, so the repair lands in the tree the verification
        branch owns.  The import is function-level for the same reason
        the route imports are (see the module docstring).
        """
        from repair_generator import RepairTaskGenerator

        plan_dir = Path(self._plan_dir_of(plan_id))
        return RepairTaskGenerator(
            self.coding_tool,
            plan_dir,
            self._repair_project_dir(plan_id, plan_dir),
        )

    def _repair_project_dir(self, plan_id: str, plan_dir: Path) -> Path:
        """Where the repair runs: the plan's registered workspace.

        The plan's execution row is the authority — it is the one field
        the whole execution chain reads — and the plan directory is the
        last resort.  A plan whose row cannot be read still gets a
        repair; it just gets one without a workspace to run in.
        """
        try:
            import server as _server

            registered = _server._get_project_dir(plan_id)
            if registered:
                return Path(registered)
        except Exception:  # noqa: BLE001 - the fallbacks cover this
            pass
        try:
            from repair_generator import resolve_repair_project_dir

            resolved = resolve_repair_project_dir(plan_dir)
            if resolved:
                return Path(resolved)
        except Exception:  # noqa: BLE001 - the fallbacks cover this
            pass
        return plan_dir

    def _assemble_repair_tasks(
        self,
        plan_id: str,
        contents: Any,
        failed_vps: List[Dict[str, Any]],
        round_number: int,
    ) -> List[Dict[str, Any]]:
        """Turn the generator's content into task rows.

        The assembler is local code by construction: it stamps ``id``,
        ``priority``, ``depends_on`` and ``execution_group`` without
        asking a model anything, so a model that misbehaves still
        yields rows the state machine can recognise.

        The plan's verification plan is handed over when it can be
        found, because the assembler reads the plan-side fields (the
        point's own command, its verification method) from there.  A
        plan whose directory cannot be resolved still gets its repair
        tasks — with less in them.
        """
        from repair_generator import RepairTaskAssembler

        plan_path = ""
        try:
            plan_dir = Path(self._plan_dir_of(plan_id))
            plan_path = str(plan_dir / "verification_plan.json")
        except Exception:  # noqa: BLE001 - the plan is optional here
            plan_path = ""

        return RepairTaskAssembler(
            round_number=round_number,
            failed_vps=failed_vps,
            plan_path=plan_path,
        ).assemble(contents)

    def generate_repair_tasks(self, plan_id: str, record: Any = None) -> int:
        """Turn the round's failures into repair tasks; return how many.

        This is the *generate repair tasks* answer, carried out:

        1. the record's failed points become failed-VP records
           (:func:`failed_vps_from_record`);
        2. the generator turns them into repair content, for the round
           **after** the one the record says stopped;
        3. the assembler stamps the local fields and hands back rows;
        4. the rows go into ``state.db``'s ``plan_tasks`` table, where
           the executor's ``_get_pending_repair_tasks`` finds them.

        **Nothing to repair means nothing is generated.**  A record with
        no failed points returns ``0`` without calling the generator: a
        call that has nothing to repair can only produce an empty
        repair, and an empty repair task in the queue is work the
        executor has to pick up and throw away.

        The rows that reach the database carry **no ``status``**:
        :meth:`PlanTaskRepository.add_task` rejects a payload with a
        field it does not know, and ``status`` is the runtime mirror's
        field rather than a static one — the table applies its own
        ``pending`` default.

        Failures are **not** swallowed here.  The call is allowed to
        raise, and :func:`hitl.terminal_execution.execute_terminal_decision`
        is what records it in the audit (arch decision point 5).
        """
        failed_vps = failed_vps_from_record(record)
        if not failed_vps:
            return 0

        round_number = _record_round_number(record) + 1

        generator = self._repair_generator_factory(plan_id)
        contents = generator.generate_repair_contents(failed_vps, round_number)

        tasks = self._assemble_repair_tasks(
            plan_id, contents, failed_vps, round_number,
        )

        rows = [
            {k: v for k, v in task.items() if k != "status"}
            for task in tasks
            if isinstance(task, dict)
        ]
        self._task_writer(plan_id, rows)
        return len(rows)

    # -- the criterion door ----------------------------------------------

    def revise_criterion(self, plan_id: str, record: Any = None) -> int:
        """Obsolete the round's failed points; return how many.

        This is the *revise the criterion* answer, carried out:

        1. the record's failed points become the ids to obsolete
           (:func:`failed_vps_from_record`);
        2. the ids go through the **existing delta machinery**
           (:func:`verification_plan_delta.apply_delta_persisted`) — the
           plan file is rewritten by the same code that rewrites it for
           a criterion replacement, so the obsoleting keeps the same
           audit trail (``obsolete`` / ``obsolete_reason`` /
           ``obsoleted_round``) and the same atomic write.  The plan
           file is **not** hand-written here;
        3. the obsoleted points are removed from the **root-cause
           chain** (:func:`_clear_chain_entries`) — the same ledger the
           orchestrator reads, written by the same functions.

        The new criterion itself is **not** written here: it is the
        operator's work, outside the framework (a new plan, or a delta
        of their own).  What this door does is take the broken
        criterion out of the round's way and leave a record of why.

        **Nothing obsoleted means nothing happened.**  When the delta
        refuses — the point is not in the plan, or the write fails —
        this method raises with the reason instead of returning zero:
        a silent zero would read as "the criterion was revised" while
        the plan still carries it.  The failure is recorded by
        :func:`hitl.terminal_execution.execute_terminal_decision` (arch
        decision point 5).
        """
        from verification_plan_delta import PlanDelta, apply_delta_persisted

        failed_vps = failed_vps_from_record(record)
        vp_ids: List[str] = []
        for failed_vp in failed_vps:
            vp_id = str(failed_vp.get("id") or "").strip()
            if vp_id and vp_id not in vp_ids:
                vp_ids.append(vp_id)
        if not vp_ids:
            raise ValueError(
                "the record names no failed verification point to revise"
            )

        plan_dir = Path(self._plan_dir_of(plan_id))
        plan_file = plan_dir / "verification_plan.json"
        try:
            raw = plan_file.read_bytes()
        except OSError as exc:
            raise RuntimeError(
                f"the verification plan could not be read: {exc}"
            ) from exc
        digest = hashlib.sha256(raw).hexdigest()
        try:
            plan_data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"the verification plan is not readable JSON: {exc}"
            ) from exc
        if not isinstance(plan_data, dict):
            raise RuntimeError(
                "the verification plan does not hold a plan object"
            )

        round_number = _record_round_number(record) or 1
        delta = PlanDelta(
            added=[],
            obsoleted=[
                {
                    "id": vp_id,
                    "reason": (
                        "the operator revised the criterion — the round's "
                        "failure was accepted and the criterion is written "
                        "outside the framework"
                    ),
                }
                for vp_id in vp_ids
            ],
        )
        summary = apply_delta_persisted(
            plan_file, plan_data, delta, round_number,
            expected_digest=digest,
        )
        obsoleted = [
            str(entry.get("id"))
            for entry in (summary.get("obsoleted") or [])
            if isinstance(entry, Mapping) and entry.get("id")
        ]
        if not obsoleted:
            # The delta refused: the plan file is untouched, and
            # nothing may be claimed about it.
            raise RuntimeError(
                "the plan delta obsoleted nothing — the failed "
                f"verification point(s) {', '.join(vp_ids)} are not in "
                "the plan, or the plan refused the change"
            )

        self._clear_chain_entries(plan_dir, obsoleted)
        return len(obsoleted)

    def _clear_chain_entries(self, plan_dir: Path, vp_ids: List[str]) -> None:
        """Take the given points off the root-cause chain.

        The chain is the ledger that says which points are still
        standing between the plan and the human.  A point that has been
        obsoleted is not standing in anybody's way any more, so its
        entry goes — otherwise the next round walks into the terminal
        branch for a point that is no longer in the plan.

        The chain is read and written by the **orchestrator's own**
        functions (:func:`verification.orchestrator.load_root_cause_chain`
        and :func:`verification.orchestrator.save_root_cause_chain`), so
        the two cannot disagree about its shape.  The import is
        function-level for the same reason the route imports are (see
        the module docstring).
        """
        from verification.orchestrator import (
            load_root_cause_chain,
            save_root_cause_chain,
        )

        chain = load_root_cause_chain(plan_dir)
        entries = chain.get("vps")
        if not isinstance(entries, dict) or not entries:
            return
        remaining = {
            vp_id: entry
            for vp_id, entry in entries.items()
            if vp_id not in set(vp_ids)
        }
        if len(remaining) == len(entries):
            return
        chain["vps"] = remaining
        save_root_cause_chain(plan_dir, chain)


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------


def default_port() -> DefaultTerminalDecisionPort:
    """The port to use when nobody supplied one.

    A factory rather than a module-level instance so the port is built
    where it is used, and so a caller that wants a different plan
    directory resolver can still build its own.
    """
    return DefaultTerminalDecisionPort()