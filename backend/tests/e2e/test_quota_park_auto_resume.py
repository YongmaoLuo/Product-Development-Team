"""R5 — a parked plan comes back on its own when the quota window reopens.

What this file is
-----------------
Every other test of the resume sweep replaces one half of it with a
recorder: the sweep is called directly, the start path is a stub, the
quota reading is a literal.  Each of those substitutions is defensible
on its own and all of them together are the reason a defect in the
*chain* survives: a sweep that reads the wrong table, a start path that
refuses the plan it was handed, a heartbeat that never calls the sweep
— every one of those is invisible from a unit test that mocks the seam
the defect is behind.

So this file runs the chain the way the product runs it:

* a **real** temporary ``state.db`` (``isolated_plans_dir`` redirects
  ``server._state_db_path`` at it), holding the rows a parked plan
  really has;
* a **real** ``HeartbeatMonitor._tick()`` — the 30-second heartbeat is
  the only thing on the server that runs often enough to satisfy the
  two-minute requirement, so "the sweep is called" is not enough: it
  has to be called *from the heartbeat*;
* the **real** read / probe / start path — the plan is revived through
  ``routes.execution.start_execution`` and
  ``routes.verification.start_verification``, the same entry points a
  human's ``POST /start`` reaches;
* the **real** quota reading, made controllable: the probe is the one
  seam that is replaced, because a provider quota endpoint cannot be
  asked to run dry and then recover on command;
* the **executor spawn** is the only other thing replaced, by a fake
  process — no model is called, no subprocess is started.

No human ``start`` happens anywhere in this file: the plan is parked,
the window closes, the window reopens, and the heartbeat does the rest.

The five cases
--------------
1. ``test_park_then_recovery_resumes_execution`` — the whole chain:
   parked → quota back → the heartbeat starts the plan again, the row
   says ``running`` and the sweep wrote its line.
2. ``test_no_recovery_no_dispatch`` — the negative control: a plan that
   is parked while every provider is still exhausted is NOT started.
   A sweep that started it anyway would start a plan into a wall.
3. ``test_two_ticks_dispatch_once`` — two ticks, one dispatch (R3).
4. ``test_parked_verification_resumes_same_round`` — a plan whose
   *execution* is finished and whose *verification round* is parked
   resumes the verification, and the round number does not move: the
   round that was interrupted is the round that continues.
5. ``test_stopped_plan_is_not_resumed`` — a plan the operator stopped
   is not selected, even when a parked plan sits next to it in the
   same table.

Two things the seed has to get right
------------------------------------
* **Both rows say "parked".**  The reader decides "parked" from
  ``plan_execution`` / ``plan_verification``, while the panel decides
  it from ``plan_routing.current_phase``.  A seed that sets one and
  not the other describes a plan that two layers of the product
  disagree about — and the sweep would be tested against a state the
  product cannot produce.
* **The working directory is on disk, not in the request.**  The start
  path refuses a start that cannot name a working directory, and the
  sweep names no directory (it has only a plan id).  The place the
  directory comes from is the plan's pause checkpoint — which is
  exactly where it comes from in production, and why a parked plan
  that still carries one can be started again.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import quota_resume  # noqa: E402
import server  # noqa: E402
import verification_loop  # noqa: E402
from routes import verification as routes_verification  # noqa: E402

#: The marker that keeps this file in the e2e lane.  See
#: ``backend/pytest.ini`` for what the lane means; the short version is
#: that this file drives the real start paths, so it does not belong to
#: the fast unit shard.
pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The fake executor
# ---------------------------------------------------------------------------


class _FakeExecutor:
    """A stand-in for the executor subprocess.

    Three properties matter, and all of them are about the *test*, not
    about the product:

    * the process is **alive** — it answers ``os.kill(pid, 0)`` — so
      the heartbeat's liveness sweep does not declare the resumed run
      dead before the assertions run;
    * the process **never ends on its own** — ``wait()`` blocks — so
      the run stays "running" for as long as the case is looking at
      it.  A fake that exited immediately would let the real success
      path write ``completed`` over the row the case is asserting on;
    * the process **can be released** — ``release()``/``terminate()``
      unblock ``wait()``.  This is not optional: the watcher thread
      the start path spawns parks inside ``wait()``, and the suite's
      teardown rule is that no worker this application starts may
      outlive the test that started it.  A fake with no release hook
      makes the worker outlive the case, and the suite fails the file
      on a rule that has nothing to do with what the case is about.

    The pid is the test process's own: it is alive by construction, and
    it is not a pid any executor could collide with.
    """

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.returncode: Optional[int] = None
        self._never_ends = threading.Event()

    def poll(self) -> Optional[int]:
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        # ``timeout`` is ignored on purpose: the run must outlive the
        # case, and a timeout would let the success path write
        # ``completed`` over the row under assertion.
        self._never_ends.wait()
        return self.returncode

    def kill(self) -> None:  # pragma: no cover - the case never stops it
        self.returncode = -9
        self._never_ends.set()

    def terminate(self) -> None:
        self.returncode = 0
        self._never_ends.set()

    def release(self) -> None:
        """Let the watcher thread go on — called at teardown."""
        self.terminate()


@pytest.fixture
def fake_spawn(monkeypatch, tmp_path, reclaim_workers):
    """Replace the executor spawn with a fake process.

    Returns the list of spawn calls, so a case can assert *how many*
    executors were started — the assertion that matters for the
    negative control and for the idempotency case.

    The fixture also hands the case back what it started: the watcher
    thread the start path spawns parks inside the fake's ``wait()``,
    and a worker left running when the next case begins resolves its
    paths against the *next* case's database.  So the fake is released
    and the worker joined here, while this case still owns it.
    """
    calls: List[Dict[str, Any]] = []
    created: List[_FakeExecutor] = []

    def _spawn(**kwargs):
        calls.append(kwargs)
        process = _FakeExecutor()
        created.append(process)
        return process, tmp_path / "executor.log"

    monkeypatch.setattr(server, "_spawn_executor_subprocess", _spawn)
    yield calls

    for process in created:
        process.release()
    stuck = reclaim_workers()
    assert not stuck, (
        "the watcher thread the resume started must not outlive the "
        f"case; {len(stuck)} worker(s) still alive"
    )


@pytest.fixture(autouse=True)
def _idle_server(monkeypatch):
    """Put the server in the state the sweep has to work in.

    The resume sweep must not depend on a plan being in flight: a
    server whose only outstanding work is a *parked* plan has empty
    registries, and that server is exactly the one that needs the
    sweep.  Every case therefore starts from an idle server.

    The HITL expiry sweep is stubbed out — it writes to the state
    database and it has nothing to do with the resume sweep; the case
    that wants to observe it stubs it itself.

    The tail of a *finished* execution is stubbed out too: the
    auto-verification hand-off would call a model, and the usage
    re-aggregation would reach a local ledger.  Neither belongs to
    the resume chain, and both would only run after the case's
    assertions are done.

    The sweep's retry ledger is module state, and it is cleared here so
    one case's attempt cannot put another case's plan inside its retry
    window.
    """
    monkeypatch.setattr(server, "_execution_state", {})
    monkeypatch.setattr(server, "_verification_state", {})
    for plan_id in list(server.sub_agent_registry._by_plan.keys()):
        server.sub_agent_registry.cleanup_for_plan(plan_id)
    monkeypatch.setattr(server, "expire_due", lambda **_kwargs: 0)
    monkeypatch.setattr(server, "_run_auto_verification_loop", lambda *a, **k: None)
    monkeypatch.setattr(server, "_refresh_usage_report", lambda *a, **k: None)
    quota_resume._LAST_ATTEMPT.clear()
    yield
    quota_resume._LAST_ATTEMPT.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connect():
    """Open the test's own state database, with the schema applied."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    migrate(conn)
    return conn


def _quota_reading(recovered: bool):
    """A quota reading the sweep can act on.

    ``recovered=False`` is every provider still exhausted — the window
    is shut and nothing may be started.  ``recovered=True`` is one
    provider with room again, which is what the sweep reads as "the
    quota came back".
    """
    return {"p1": SimpleNamespace(exhausted=not recovered)}


def _parked_execution(
    plan_id: str,
    plans_dir: Path,
    project_dir: Path,
    *,
    exec_status: str = "waiting_for_quota",
) -> Path:
    """Write the files and the rows of a plan parked for quota.

    The rows are the two the reader looks at, and they are written the
    way the product writes them:

    * ``plan_routing.current_phase`` — the workflow phase, moved there
      along the production edge ``executing → waiting_for_quota``;
    * ``plan_execution.exec_status`` / ``stop_reason`` — the run's own
      verdict, written through the repository that owns the row.

    The plan's directory carries the two files the start path reads:
    ``tasks.json`` (the work that is still owed) and the pause
    checkpoint (the working directory the run continues in).
    """
    from pause_state import write_pause_checkpoint
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_dir = Path(plans_dir) / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [{"id": "t1", "status": "pending"}],
                "stop_reason": "waiting_for_quota",
                "reason_detail": json.dumps(
                    {
                        "providers": ["kimi"],
                        "needs_manual_topup": True,
                        "waited_sec": 12.0,
                        "at": "2026-11-15T00:00:00",
                    }
                ),
            }
        ),
        encoding="utf-8",
    )
    # The checkpoint is where the working directory lives: the sweep
    # names a plan, not a directory, and the start path takes the
    # directory from the checkpoint when there is one.
    write_pause_checkpoint(
        plan_dir,
        kind="execution",
        project_dir=str(project_dir),
        reason="waiting_for_quota",
    )

    conn = _connect()
    try:
        routing = RoutingRepository(conn)
        routing.insert(plan_id, "executing")
        routing.try_mark_phase(plan_id, ("executing",), "waiting_for_quota")
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            exec_status=exec_status,
            stop_reason="waiting_for_quota",
            project_dir=str(project_dir),
        )
    finally:
        conn.close()
    return plan_dir


def _parked_verification(
    plan_id: str,
    plans_dir: Path,
    project_dir: Path,
    *,
    round_n: int = 2,
) -> Path:
    """Write the rows of a plan whose *verification round* is parked.

    The execution of this plan is finished — its row says
    ``completed`` and it names the working directory — and the round
    that ran out of quota is the verification one.  The two rows say
    different things on purpose: it is the discrimination the case is
    about.  A plan whose *execution* is parked and a plan whose
    *verification* is parked are two different plans, and the sweep
    has to pick the right one for each.
    """
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    plan_dir = Path(plans_dir) / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "t1", "status": "completed"}]}),
        encoding="utf-8",
    )

    conn = _connect()
    try:
        routing = RoutingRepository(conn)
        routing.insert(plan_id, "executing")
        routing.try_mark_phase(plan_id, ("executing",), "waiting_for_quota")
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            exec_status="completed",
            project_dir=str(project_dir),
        )
        VerificationRepository(conn).insert(
            plan_id,
            verification_status="running",
            round=round_n,
            max_rounds=3,
        )
    finally:
        conn.close()

    # The park itself is written by the production entry point: the
    # row's status and stop reason, and the side-channel snapshot the
    # restart path reads.
    verification_loop._park_verification_round_for_quota(plan_id, round_n)
    return plan_dir


def _stopped_execution(plan_id: str, plans_dir: Path, project_dir: Path) -> Path:
    """Write the rows of a plan the operator stopped.

    A stopped plan is a plan a human ended on purpose.  Its row says
    ``stopped`` and its stop reason says who stopped it — nothing to
    do with a quota window — and its phase is the terminal ``stopped``.

    The rows matter because the resume sweep reads the *same table* the
    parked plans are in: a row that says "parked" and a row that says
    "stopped" sit next to each other, and only the second one is
    allowed to be picked up.
    """
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_dir = Path(plans_dir) / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "t1", "status": "pending"}]}),
        encoding="utf-8",
    )

    conn = _connect()
    try:
        routing = RoutingRepository(conn)
        routing.insert(plan_id, "executing")
        routing.try_mark_phase(plan_id, ("executing",), "stopped")
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            exec_status="stopped",
            stop_reason="user_stopped",
            project_dir=str(project_dir),
        )
    finally:
        conn.close()
    return plan_dir


def _exec_status(plan_id: str) -> Optional[str]:
    """The run's own status, read back from the state database."""
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = _connect()
    try:
        row = ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()
    return None if row is None else row.get("exec_status")


def _verification_round(plan_id: str) -> Optional[int]:
    """The verification round the plan is on, read back from the row."""
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _connect()
    try:
        row = VerificationRepository(conn).current(plan_id)
    finally:
        conn.close()
    return None if row is None else row.get("round")


@pytest.fixture
def project_dir(tmp_path):
    """A real working directory for the executor to run in."""
    target = tmp_path / "project"
    target.mkdir()
    return target


# ---------------------------------------------------------------------------
# 1 — park → recovery → the plan runs again
# ---------------------------------------------------------------------------


def test_park_then_recovery_resumes_execution(
    isolated_plans_dir,
    project_dir,
    monkeypatch,
    caplog,
    fake_spawn,
):
    """The whole chain, with no human start anywhere in it.

    The plan is parked, the quota comes back, and the heartbeat — the
    only thing that runs often enough — starts the plan again.  Three
    things have to be true afterwards, and each of them is a different
    layer:

    * the executor was spawned (the work went out);
    * the row says ``running`` (the durable state agrees);
    * the sweep wrote its line (an operator can see what happened).
    """
    plan_id = "p9"
    _parked_execution(plan_id, isolated_plans_dir, project_dir)

    monkeypatch.setattr(
        quota_resume, "_default_probe", lambda: _quota_reading(recovered=True)
    )

    with caplog.at_level(logging.INFO, logger="quota_resume"):
        server.heartbeat_monitor._tick()

    assert len(fake_spawn) == 1, (
        "the recovered plan must be started exactly once — a heartbeat "
        f"tick that starts it twice is two executors on one plan; got "
        f"{len(fake_spawn)} spawn(s)"
    )
    assert fake_spawn[0].get("plan_id") == plan_id

    assert _exec_status(plan_id) == "running", (
        "the resumed plan must read as running in the state database — "
        f"got {_exec_status(plan_id)!r}"
    )

    assert "quota_recovered_resume plan=p9 round=0 reason=quota_recovered" in (
        caplog.text
    ), (
        "the sweep must write the line that says a revival happened; got "
        f"{caplog.text!r}"
    )


# ---------------------------------------------------------------------------
# 2 — the negative control: no recovery, no dispatch
# ---------------------------------------------------------------------------


def test_no_recovery_no_dispatch(
    isolated_plans_dir,
    project_dir,
    monkeypatch,
    fake_spawn,
):
    """A parked plan is NOT started while every provider is still dry.

    This is the control for the case above.  A sweep that started a
    plan on a shut window would put a second executor into a provider
    that has no room — the very thing the park was protecting against.
    """
    plan_id = "p-dry"
    _parked_execution(plan_id, isolated_plans_dir, project_dir)

    monkeypatch.setattr(
        quota_resume, "_default_probe", lambda: _quota_reading(recovered=False)
    )

    server.heartbeat_monitor._tick()

    assert fake_spawn == [], (
        "nothing may be started while every provider is exhausted; got "
        f"{len(fake_spawn)} spawn(s)"
    )
    assert _exec_status(plan_id) == "waiting_for_quota", (
        "the parked plan must stay parked while the window is shut; got "
        f"{_exec_status(plan_id)!r}"
    )


# ---------------------------------------------------------------------------
# 3 — two ticks, one dispatch
# ---------------------------------------------------------------------------


def test_two_ticks_dispatch_once(
    isolated_plans_dir,
    project_dir,
    monkeypatch,
    fake_spawn,
):
    """A second heartbeat tick must not start the plan a second time.

    The heartbeat runs every thirty seconds and the plan stays in the
    table for as long as it is parked, so the sweep will see it again
    and again.  One plan, one executor: the second tick has to be a
    no-op.
    """
    plan_id = "p-twice"
    _parked_execution(plan_id, isolated_plans_dir, project_dir)

    monkeypatch.setattr(
        quota_resume, "_default_probe", lambda: _quota_reading(recovered=True)
    )

    server.heartbeat_monitor._tick()
    server.heartbeat_monitor._tick()

    assert len(fake_spawn) == 1, (
        "two ticks must produce one executor — a plan with two "
        f"executors has two runs writing the same task rows; got "
        f"{len(fake_spawn)} spawn(s)"
    )


# ---------------------------------------------------------------------------
# 4 — a parked verification round resumes the SAME round
# ---------------------------------------------------------------------------


def test_parked_verification_resumes_same_round(
    isolated_plans_dir,
    project_dir,
    monkeypatch,
    caplog,
):
    """A parked verification round continues as itself.

    The plan's execution is finished; it is the *verification round*
    that ran out of quota.  The round number is the contract the
    plan's verdicts and its report are written against, so the resume
    has to pick that round back up — starting "round 3" for work that
    was already half-done in round 2 would relabel it under a round
    that never ran.

    The verification loop is the one thing replaced here: it is the
    part that would call a model.  Everything around it — the read, the
    park, the start path, the round resolution, the CAS — is the real
    thing.
    """
    plan_id = "pv1"
    round_n = 2
    _parked_verification(
        plan_id, isolated_plans_dir, project_dir, round_n=round_n
    )

    started: List[Dict[str, Any]] = []

    def _fake_auto_loop(plan_id_arg, plan_dir, project_dir_arg, **kwargs):
        started.append(
            {
                "plan_id": plan_id_arg,
                "start_round": kwargs.get("start_round"),
            }
        )

    monkeypatch.setattr(
        routes_verification,
        "_run_auto_loop_with_crash_persistence",
        _fake_auto_loop,
    )
    monkeypatch.setattr(
        quota_resume, "_default_probe", lambda: _quota_reading(recovered=True)
    )

    with caplog.at_level(logging.INFO, logger="quota_resume"):
        server.heartbeat_monitor._tick()

    assert _verification_round(plan_id) == round_n, (
        "the parked round must be resumed as itself — the round number "
        f"is the contract the verdicts are written against; got "
        f"{_verification_round(plan_id)!r}, expected {round_n!r}"
    )

    assert "quota_recovered_resume plan=pv1 round=2 reason=quota_recovered" in (
        caplog.text
    ), (
        "the sweep must write the line that says a revival happened, and "
        "it must name the round it revived; got " + repr(caplog.text)
    )

    # The loop itself is the only faked part; give it a moment to be
    # entered so the assertion is about a round that really started.
    deadline = threading.Event()
    for _ in range(100):
        if started:
            break
        deadline.wait(0.01)
    assert started, (
        "the parked round must actually be started again — the sweep "
        "reported a revival but no round was running"
    )
    assert started[0]["start_round"] == round_n, (
        "the round that runs must be the parked one; got "
        f"{started[0]['start_round']!r}, expected {round_n!r}"
    )


# ---------------------------------------------------------------------------
# 5 — a plan the operator stopped is never selected
# ---------------------------------------------------------------------------


def test_stopped_plan_is_not_resumed(
    isolated_plans_dir,
    project_dir,
    monkeypatch,
    caplog,
    fake_spawn,
):
    """A stopped plan stays stopped, even next to a parked one.

    Two plans sit in the same table: one parked for the quota, one the
    operator stopped on purpose.  The quota coming back says nothing
    about the second one — a human ended it, and the whole point of
    stopping a plan is that it does not come back by itself.

    The two plans are seeded together on purpose.  A sweep that found
    "nothing to do" would also pass a case with the stopped plan
    alone; putting a parked plan next to it makes the case say what it
    means: the sweep saw both rows and picked one.
    """
    parked_id = "p-parked"
    stopped_id = "p-stopped"
    _parked_execution(parked_id, isolated_plans_dir, project_dir)
    _stopped_execution(stopped_id, isolated_plans_dir, project_dir)

    monkeypatch.setattr(
        quota_resume, "_default_probe", lambda: _quota_reading(recovered=True)
    )

    with caplog.at_level(logging.INFO, logger="quota_resume"):
        server.heartbeat_monitor._tick()

    assert len(fake_spawn) == 1, (
        "exactly one plan may be started — the parked one; got "
        f"{len(fake_spawn)} spawn(s)"
    )
    assert fake_spawn[0].get("plan_id") == parked_id, (
        "the started plan must be the parked one; got "
        f"{fake_spawn[0].get('plan_id')!r}"
    )

    # "Not selected" is the contract, and it is stronger than "did not
    # start": a plan the sweep picked up and then failed to start would
    # still have been picked up.  So the sweep must not *mention* the
    # stopped plan at all — neither revived nor refused.
    sweep_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "quota_resume"
    ]
    assert not any(stopped_id in line for line in sweep_lines), (
        "the sweep must not have selected the stopped plan; got "
        f"{sweep_lines!r}"
    )
    assert _exec_status(stopped_id) == "stopped", (
        "the stopped plan must still read as stopped; got "
        f"{_exec_status(stopped_id)!r}"
    )
