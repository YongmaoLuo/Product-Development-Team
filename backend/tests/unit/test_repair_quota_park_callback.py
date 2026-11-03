"""2026-11-02: the post-repair callback must PARK a quota-parked round.

Background
----------
When every provider runs out of capacity the executor parks the plan
instead of failing it: it writes ``stop_reason="waiting_for_quota"`` into
the top level of ``tasks.json`` and exits with rc=0.  The repair path
reads that marker and parks the round under the same word the main
execution path uses (``plan_status.QUOTA_WAIT_PHASE``).

The post-repair callback (``_make_repair_on_complete``'s
``_on_repair_complete``) did not know about that word.  It asked one
question — "did the repair path record a stop reason?" — and ANY
non-empty answer sent the plan down the undrained-repair guard: bump the
guard counter, write ``loop_stopped`` to ``plan_verification`` and stop
the chain.  So a repair round that was parked for capacity was killed on
its FIRST callback, and the plan was reported as broken
(``loop_stopped``) when the only thing that had happened was that the
providers ran dry.

Contract pinned here
--------------------
* stop reason == ``waiting_for_quota`` + a quota marker on disk → the
  plan is PARKED: no terminal verdict is written, the workflow phase
  reads ``waiting_for_quota``, the execution row's ``exec_status`` reads
  ``waiting_for_quota``, and the undrained-repair guard is NOT bumped.
* The park is idempotent, and the idempotence is judged on the PHASE
  ON DISK: a plan whose persisted phase is already
  ``waiting_for_quota`` is not parked twice (task 5's "the phase is
  written once" depends on this).  The in-memory
  ``_execution_state[plan_id]["status"]`` field CANNOT be that
  evidence — the runner writes it before it fires this callback, so
  trusting it would skip the park entirely.
* If the marker is gone (or the park itself fails), the callback falls
  back to the EXISTING undrained-repair guard, verbatim:
  ``loop_stopped`` + ``repair_batch_not_drained:waiting_for_quota:N``.
  A park that could not happen must not look like a park that did.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import server
import verification_loop
from plan_status import QUOTA_WAIT_PHASE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_plan(
    tmp_path: Path,
    plan_id: str,
    *,
    quota_marker: bool,
    pending: int = 2,
) -> Path:
    """Write ``tasks.json`` plus the state.db rows the park needs.

    The seed needs BOTH ``plan_routing`` and ``plan_execution``:

    * ``plan_routing`` is the plan's phase row — the park moves it into
      ``waiting_for_quota``.
    * ``plan_execution`` is the row the park writes ``exec_status`` to.
      Without it ``ExecutionRepository.update_status`` raises
      ``PlanNotFoundError``, which the park swallows — the park would
      then live only in memory.
    """
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    payload: dict = {
        "tasks": [
            {"id": f"repair-r1-{i:02d}"} for i in range(1, pending + 1)
        ],
    }
    if quota_marker:
        payload["stop_reason"] = "waiting_for_quota"
        payload["reason_detail"] = json.dumps(
            {
                "providers": ["kimi"],
                "needs_manual_topup": True,
                "waited_sec": 12.0,
                "at": "2026-11-02T00:00:00",
            }
        )
    (plan_dir / "tasks.json").write_text(json.dumps(payload), encoding="utf-8")

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            (plan_id, "executing", "2026-11-02T00:00:00"),
        )
        conn.execute(
            "INSERT INTO plan_execution (plan_id, attempt_count, project_dir, "
            "exec_status, updated_at) VALUES (?, ?, ?, ?, ?)",
            (plan_id, 1, str(tmp_path), "running", "2026-11-02T00:00:00"),
        )
        for i in range(1, pending + 1):
            conn.execute(
                "INSERT INTO plan_tasks (plan_id, task_id, status, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    plan_id,
                    f"repair-r1-{i:02d}",
                    "pending",
                    "2026-11-02T00:00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return plan_dir


def _parked_state(plan_id: str) -> dict:
    """The in-memory execution record the repair path leaves behind."""
    with server._execution_state_lock:
        server._execution_state.clear()
        server._execution_state[plan_id] = {
            "_source": "repair_execution",
            "stop_reason": "waiting_for_quota",
        }


def _exec_summary(plan_id: str) -> dict:
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(server._state_db_path())
    try:
        return ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()


def _plan_verification_rows(plan_id: str) -> int:
    from state_machine.db.connection import open as open_db

    conn = open_db(server._state_db_path())
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM plan_verification WHERE plan_id = ?",
            (plan_id,),
        )
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def _disk_phase(plan_id: str) -> str:
    """The phase this plan carries ON DISK (``plan_routing``)."""
    from state_machine.db.connection import open as open_db

    conn = open_db(server._state_db_path())
    try:
        cur = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        )
        row = cur.fetchone()
        return "" if row is None else str(row[0])
    finally:
        conn.close()


def _set_disk_phase(plan_id: str, phase: str) -> None:
    """Move this plan's phase row to ``phase`` — the persisted truth."""
    from state_machine.db.connection import open as open_db

    conn = open_db(server._state_db_path())
    try:
        conn.execute(
            "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
            (phase, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_quota_stop_reason_parks_without_terminal(monkeypatch, tmp_path):
    """A quota-parked repair round is parked, not killed.

    The repair executor exited 0 because every provider ran out of
    capacity.  The callback must NOT write a terminal verdict: a
    ``loop_stopped`` here tells the operator their plan is broken when
    the only thing that happened was that the providers ran dry.
    """
    plan_id = "p-repair-quota-callback"
    plan_dir = _seed_plan(tmp_path, plan_id, quota_marker=True)
    _parked_state(plan_id)

    # The guard is seeded NON-ZERO on purpose: a park must CLEAR the
    # counter, not merely decline to bump it.  A bump would take it to
    # the ``repair_never_drained`` threshold and end the chain.
    verification_loop._repair_not_drained_guard[plan_id] = 2

    record_terminal = MagicMock()
    monkeypatch.setattr(
        server, "_record_verification_terminal", record_terminal,
    )

    try:
        callback = verification_loop._make_repair_on_complete(
            plan_id, tmp_path,
        )
        callback(0)
    finally:
        verification_loop._repair_not_drained_guard.pop(plan_id, None)

    assert record_terminal.call_count == 0, (
        "a quota-parked repair round was killed with a terminal verdict "
        f"({record_terminal.call_args_list!r}) — the plan must be parked, "
        "not reported as broken"
    )
    assert _plan_verification_rows(plan_id) == 0, (
        "a parked round must not write a plan_verification row"
    )

    # The park reaches the surfaces the operator reads.
    phase = server.PlanState(plan_dir).get_current_phase()
    assert phase == QUOTA_WAIT_PHASE, (
        f"the plan's phase is {phase!r} — a quota-parked round must park "
        f"the plan under {QUOTA_WAIT_PHASE!r}"
    )
    summary = _exec_summary(plan_id)
    assert summary is not None, "no plan_execution row was written"
    assert summary["exec_status"] == "waiting_for_quota"

    # The guard was cleared, not bumped.
    assert verification_loop._repair_not_drained_guard.get(plan_id, 0) == 0


def test_quota_reason_without_marker_falls_back_to_guard(
    monkeypatch, tmp_path,
):
    """A park that could not happen falls back to the existing guard.

    The marker is gone from ``tasks.json``, so there is nothing to park
    on.  The callback must behave EXACTLY as it did before: the
    undrained-repair guard decides, and the chain stops with
    ``loop_stopped`` + the undrained reason.
    """
    plan_id = "p-repair-quota-no-marker"
    _seed_plan(tmp_path, plan_id, quota_marker=False)
    _parked_state(plan_id)

    record_terminal = MagicMock()
    monkeypatch.setattr(
        server, "_record_verification_terminal", record_terminal,
    )

    try:
        callback = verification_loop._make_repair_on_complete(
            plan_id, tmp_path,
        )
        callback(0)
    finally:
        verification_loop._repair_not_drained_guard.pop(plan_id, None)

    # ``round_token`` names the verification round the verdict belongs
    # to.  It is forwarded verbatim by ``_record_terminal`` (2026-11-01);
    # this callback was built without one, so it is the empty round.
    record_terminal.assert_called_once_with(
        plan_id,
        "loop_stopped",
        "repair_batch_not_drained:waiting_for_quota:2_pending",
        round_token="",
    )


def test_park_happens_when_only_memory_says_parked(monkeypatch, tmp_path):
    """An in-memory "already parked" is NOT evidence that the park happened.

    The repair runner writes ``state["status"] = QUOTA_WAIT_PHASE`` into
    the in-memory execution state BEFORE it fires the callback.  If the
    idempotence guard trusted that field, it would return True on the
    very first park and ``_enter_quota_wait`` would never run: the park
    would live only in memory, the phase on disk would stay
    ``executing``, ``stop_reason`` would stay NULL, no
    ``execution_waiting_for_quota`` event would be written, and the
    plan could never be started again.

    So: memory says parked, disk says not — the park MUST still happen.
    """
    plan_id = "p-repair-quota-memory-not-disk"
    _seed_plan(tmp_path, plan_id, quota_marker=True)

    # The runner's in-memory decision, written before the callback fires.
    with server._execution_state_lock:
        server._execution_state.clear()
        server._execution_state[plan_id] = {
            "_source": "repair_execution",
            "stop_reason": "waiting_for_quota",
            "status": "waiting_for_quota",
        }

    # The disk does NOT know about the park yet — that is the whole point.
    assert _disk_phase(plan_id) == "executing", (
        "the disk phase must still be 'executing' for this case to mean "
        "anything"
    )

    import routes.execution as execution_routes

    entered = []
    monkeypatch.setattr(
        execution_routes,
        "_enter_quota_wait",
        lambda *args, **kwargs: entered.append(args) or True,
    )

    record_terminal = MagicMock()
    monkeypatch.setattr(
        server, "_record_verification_terminal", record_terminal,
    )

    verification_loop._repair_not_drained_guard.pop(plan_id, None)
    callback = verification_loop._make_repair_on_complete(plan_id, tmp_path)
    callback(0)

    assert len(entered) == 1, (
        "the park was skipped because the in-memory state said "
        "'waiting_for_quota' — but that field is the runner's own "
        "pre-callback decision and was never persisted; the park only "
        f"counts when it reaches disk (entered={entered!r})"
    )
    assert record_terminal.call_count == 0


def test_park_is_idempotent_when_disk_phase_already_parked(
    monkeypatch, tmp_path,
):
    """A plan whose PHASE ON DISK is already parked is not parked again.

    This is the real idempotence: the phase is written once per park,
    and whoever owns that write must be the only one to do it.  The
    evidence is the persisted phase — not the in-memory field.
    """
    plan_id = "p-repair-quota-disk-parked"
    _seed_plan(tmp_path, plan_id, quota_marker=True)

    with server._execution_state_lock:
        server._execution_state.clear()
        server._execution_state[plan_id] = {
            "_source": "repair_execution",
            "stop_reason": "waiting_for_quota",
            "status": "waiting_for_quota",
        }

    # The park already happened: the phase on disk says so.
    _set_disk_phase(plan_id, "waiting_for_quota")
    assert _disk_phase(plan_id) == "waiting_for_quota"

    import routes.execution as execution_routes

    entered = []
    monkeypatch.setattr(
        execution_routes,
        "_enter_quota_wait",
        lambda *args, **kwargs: entered.append(args) or True,
    )

    record_terminal = MagicMock()
    monkeypatch.setattr(
        server, "_record_verification_terminal", record_terminal,
    )

    verification_loop._repair_not_drained_guard.pop(plan_id, None)
    callback = verification_loop._make_repair_on_complete(plan_id, tmp_path)
    callback(0)

    assert entered == [], (
        "an already-parked plan (phase on disk) was parked a second time "
        "— the phase is written once"
    )
    assert record_terminal.call_count == 0


def test_quota_reason_when_park_raises_falls_back_to_guard(
    monkeypatch, tmp_path,
):
    """A park that raises falls back to the existing guard.

    The park is best-effort.  When it blows up, the callback must not
    swallow the failure and call the round parked — it falls through to
    the undrained-repair guard, exactly as it did before.
    """
    plan_id = "p-repair-quota-park-raises"
    _seed_plan(tmp_path, plan_id, quota_marker=True)
    _parked_state(plan_id)

    import routes.execution as execution_routes

    def _boom(*args, **kwargs):
        raise RuntimeError("the park blew up")

    monkeypatch.setattr(execution_routes, "_enter_quota_wait", _boom)

    record_terminal = MagicMock()
    monkeypatch.setattr(
        server, "_record_verification_terminal", record_terminal,
    )

    try:
        callback = verification_loop._make_repair_on_complete(
            plan_id, tmp_path,
        )
        callback(0)
    finally:
        verification_loop._repair_not_drained_guard.pop(plan_id, None)

    # ``round_token`` names the verification round the verdict belongs
    # to.  It is forwarded verbatim by ``_record_terminal`` (2026-11-01);
    # this callback was built without one, so it is the empty round.
    record_terminal.assert_called_once_with(
        plan_id,
        "loop_stopped",
        "repair_batch_not_drained:waiting_for_quota:2_pending",
        round_token="",
    )
