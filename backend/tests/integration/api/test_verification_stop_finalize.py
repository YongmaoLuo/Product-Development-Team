"""The phase is flipped by the finalize callback — and only when the plan
is really quiet.

2026-11-20 — the stop closes the gate: the routing row is parked at the
plan-level ``user_stopping`` phase and the answer says ``stopping``.  The
flip back to a parked phase is NOT the request's business — it belongs to
the background layer, to the supervisor's verdict on the plan's silence.

What is pinned here
-------------------
1. **a quiet plan is parked at the phase it came from** — a paused round
   at ``verification``, a stopped repair at ``verification_repairing``;
2. **a plan that still has work running is NOT parked** — it stays at
   ``user_stopping``, because a plan that is still working is not a
   stopped plan, and a phase that says otherwise is the "said it stopped
   and kept running" bug this whole chain exists to kill;
3. **the flip is preceded by a fresh silence check** — the supervisor's
   quiet window closed a moment before the callback ran, and a plan that
   starts new work in that gap is not quiet;
4. **the stop record is cleared once the stop is over** — a stop that has
   been dealt with must not gate the next run;
5. **a plan another path has moved is not overwritten** — the row is no
   longer the stop's to write, but the record is still cleared;
6. **the survivors the supervisor hands over are honoured** — a non-empty
   handover means the plan was not quiet, so the phase stays
   ``user_stopping`` and the verification row stays non-terminal.

The callback is called with the plan id alone, and the supervisor's
survivors ride on it.  A non-empty handover is a verdict of its own — the
plan was not quiet — so it is honoured without a second look; an empty
handover is not a verdict, so the callback finds the survivors for itself
before it parks anything.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
import stop_state
import stop_supervisor
import verification_cancel
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)

PLAN_ID = "20261120-stop-finalize"

VP_IDS = ["VP-001", "VP-002", "VP-003"]

ROUND_VERDICTS = [
    {
        "vp_id": "VP-001",
        "status": "PASSED",
        "worker_id": "executor",
        "seq": 1,
        "reasons": [],
        "evidence": {},
    },
]


@pytest.fixture(autouse=True)
def _clean_process_state():
    """The stop's process-local state is global — do not leak it."""
    verification_cancel._reset_for_tests()
    stop_state._reset_for_tests()
    stop_supervisor._reset_for_tests()
    yield
    stop_supervisor._reset_for_tests()
    stop_state._reset_for_tests()
    verification_cancel._reset_for_tests()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test plans directory + state.db isolation."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()
    server._verification_state.clear()

    yield plans_dir, db_path

    server._execution_state.clear()
    server._verification_state.clear()
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def captured_finalize(monkeypatch):
    """The finalize callback the stop hands to the supervisor.

    The supervisor is replaced by a recorder, so the callback is driven
    by the test instead of by a background thread: the verdict is then
    something the test observes, not something it races.
    """
    captured: dict = {}

    def _fake_start(plan_id, *, finalize, **kwargs):
        captured["plan_id"] = plan_id
        captured["finalize"] = finalize
        return None

    monkeypatch.setattr(stop_supervisor, "start", _fake_start)
    return captured


@pytest.fixture
def live_sub_agent():
    """A registered sub-agent whose process is still running.

    The handle carries a live process and no ``scoped_tool`` — the
    production shape of a verification point that is still working.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    handle = server.sub_agent_registry.register_sub_agent(
        PLAN_ID, "VP-003", 1, scoped_tool=None, pid=proc.pid,
    )
    try:
        yield handle
    finally:
        server.sub_agent_registry.unregister(PLAN_ID, handle)
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    phase: str = "verification_running",
    round_n: int = 2,
    verdicts=None,
    max_rounds: int = 3,
):
    """A plan mid-verification, parked at ``phase``, with a round's work."""
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, phase)
        ExecutionRepository(conn).insert(
            plan_id=PLAN_ID, project_dir=str(project_dir),
        )
        verif = VerificationRepository(conn)
        verif.insert(PLAN_ID, verification_status="running",
                     max_rounds=max_rounds)
        verif.init_round(PLAN_ID, round_n=round_n, max_rounds=max_rounds)
        for verdict in (verdicts if verdicts is not None else ROUND_VERDICTS):
            verif.append_verdict(PLAN_ID, dict(verdict))
    finally:
        conn.close()

    (plan_dir / "verification_plan.json").write_text(
        json.dumps(
            {
                "verification_points": [
                    {"id": vp_id, "title": f"check {vp_id}"} for vp_id in VP_IDS
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_dir, project_dir


def _read_phase(db_path: Path, plan_id: str) -> str:
    """The routing row's phase, read back on its own connection."""
    conn = open_db(db_path)
    try:
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


def _read_verification_row(db_path: Path, plan_id: str) -> dict:
    """The verification row, read back on its own connection."""
    conn = open_db(db_path)
    try:
        row = VerificationRepository(conn).current(plan_id)
    finally:
        conn.close()
    return dict(row or {})


def _stop(client) -> dict:
    """Ask for the stop and return the answer."""
    stop = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert stop.status_code == 200, stop.text
    return stop.json()


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------


def test_finalize_parks_verification_phase(
    env, client, captured_finalize,
):
    """A quiet round is parked at ``verification`` — where it can resume.

    The finalize callback is what decides that the round is over.  It
    parks the plan at ``verification``, the phase the next ``/start``
    reads as a resumable source: a stop that never parks the plan
    leaves it stuck at the stopping phase forever.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="verification_running")

    body = _stop(client)
    assert body["status"] == "stopping", body
    assert _read_phase(db_path, PLAN_ID) == "user_stopping", (
        "the stop did not park the plan at the plan-level stopping phase"
    )

    captured_finalize["finalize"](PLAN_ID)

    assert _read_phase(db_path, PLAN_ID) == "verification", (
        "the quiet plan was not parked at 'verification' — the next "
        "/start has no resumable source to read"
    )


def test_finalize_parks_repair_phase(env, client, captured_finalize):
    """A quiet repair is parked at ``verification_repairing``.

    The repair half of the verification chain is not a paused round: the
    round already finished and the repair tasks are what is waiting.  So
    the stop parks the plan back where the repair waits for it — not at
    ``verification``, which would claim a round was interrupted when
    none was.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="verification_repairing")

    _stop(client)
    assert _read_phase(db_path, PLAN_ID) == "user_stopping"

    captured_finalize["finalize"](PLAN_ID)

    assert _read_phase(db_path, PLAN_ID) == "verification_repairing", (
        "the stopped repair was not parked at 'verification_repairing' — "
        "the plan is left in a phase that says nothing about the repair "
        "that is still waiting"
    )


def test_no_flip_while_survivor_alive(
    env, client, captured_finalize, live_sub_agent,
):
    """A plan with work still running is NOT parked.

    This is the defect the whole chain exists to kill: the phase says
    the work stopped while the work is still running.  The callback is
    handed the plan id alone, so it has to find the survivor itself —
    and while it is alive, the phase stays ``user_stopping``.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="verification_running")

    _stop(client)
    assert _read_phase(db_path, PLAN_ID) == "user_stopping"

    captured_finalize["finalize"](PLAN_ID)

    assert _read_phase(db_path, PLAN_ID) == "user_stopping", (
        "the plan was parked while its round was still running — it "
        "drops out of /api/system/active and the scheduler thinks the "
        "slot is free"
    )
    assert stop_state.get(PLAN_ID) is not None, (
        "the stop record was cleared while the work is still running — "
        "the stop is still in progress, and the record is its evidence"
    )


def test_handover_survivors_keep_the_phase_and_the_row(
    env, client, captured_finalize,
):
    """The survivors the supervisor hands over are a verdict of their own.

    The supervisor hands the finalize callback the survivors it could not
    stop — at the hard limit that is exactly what it has.  A non-empty
    handover means the plan was NOT quiet, so the phase stays
    ``user_stopping`` and the verification row stays non-terminal: a plan
    that is still working is not a stopped plan.

    The handover rides on the plan id, because the callback takes the
    plan id alone (see ``stop_supervisor._bind_finalize``).  The other
    tests in this file call the callback with a plain string, so they
    never reach this shape — and a guard written the wrong way round
    passes every one of them.
    """
    from verification_quiesce import Survivor

    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="verification_running")

    _stop(client)
    assert _read_phase(db_path, PLAN_ID) == "user_stopping"

    # The production binding, and the production handover: the callback
    # takes the plan id alone, so the survivors ride on it.
    bound = stop_supervisor._bind_finalize(
        captured_finalize["finalize"], PLAN_ID,
    )
    bound([Survivor(pid=os.getpid(), kind="registry", detail="VP-003")])

    assert _read_phase(db_path, PLAN_ID) == "user_stopping", (
        "the plan was parked while the supervisor was still reporting live "
        "survivors — the phase falsely claims a stop that never completed"
    )
    row = _read_verification_row(db_path, PLAN_ID)
    assert row.get("verification_status") != "loop_stopped", (
        f"the verification row was written terminal while the supervisor "
        f"reported live survivors: {row}"
    )
    assert not row.get("verification_stop_reason"), (
        f"the verification row claims a stop that did not happen: {row}"
    )
    assert stop_state.get(PLAN_ID) is not None, (
        "the stop record was cleared while the work is still running — "
        "the stop is still in progress, and the record is its evidence"
    )


def test_stop_record_cleared(env, client, captured_finalize):
    """The stop record does not outlive the stop.

    The record is the spawn gate's input: while it is there, no new
    work starts for the plan.  A stop that has been dealt with — the
    plan is parked, the work is gone — must not keep the next run out.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="verification_running")

    _stop(client)
    assert stop_state.get(PLAN_ID) is not None, "the stop left no record"

    captured_finalize["finalize"](PLAN_ID)

    assert stop_state.get(PLAN_ID) is None, (
        "the stop record survived the stop — the next run is gated by a "
        "stop that is already over"
    )


def test_finalize_rechecks_silence_before_flipping(
    env, client, captured_finalize, monkeypatch,
):
    """The flip is preceded by a fresh silence check.

    The supervisor's quiet window closed a moment before this callback
    ran, and a plan that starts new work in that gap is not quiet.  The
    callback re-checks for itself rather than trusting the verdict it
    was handed.
    """
    import verification_quiesce

    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="verification_running")

    calls: list = []
    real_wait = verification_quiesce.wait_for_quiescence

    def _recording(plan_id, **kwargs):
        calls.append(plan_id)
        return real_wait(plan_id, **kwargs)

    monkeypatch.setattr(
        verification_quiesce, "wait_for_quiescence", _recording,
    )

    _stop(client)
    captured_finalize["finalize"](PLAN_ID)

    assert calls, (
        "the callback flipped the phase without re-checking for silence — "
        "work that starts after the supervisor's window is missed"
    )
    assert calls[0] == PLAN_ID, calls


def test_finalize_does_not_overwrite_a_plan_moved_by_another_path(
    env, client, captured_finalize,
):
    """A row another path owns is not overwritten — but the record is cleared.

    The phase write is a CAS on ``user_stopping``.  When the row has
    already been moved, the stop has nothing to write — and the plan
    that is there now is not the stop's to touch.  The stop record is
    still cleared: the stop is over, whatever the plan did afterwards.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="verification_running")

    _stop(client)
    assert _read_phase(db_path, PLAN_ID) == "user_stopping"

    # Another path moves the plan while the stop is still in progress.
    conn = open_db(db_path)
    try:
        RoutingRepository(conn).try_mark_phase(
            PLAN_ID, ("user_stopping",), "verification_failed",
        )
    finally:
        conn.close()

    captured_finalize["finalize"](PLAN_ID)

    assert _read_phase(db_path, PLAN_ID) == "verification_failed", (
        "the finalize overwrote a phase another path had already written"
    )
    assert stop_state.get(PLAN_ID) is None, (
        "the stop record survived a plan that was moved by another path — "
        "the next run is gated by a stop that is already over"
    )


# ---------------------------------------------------------------------------
# 2026-11-22: 停止的痕迹不能整条作废
# ---------------------------------------------------------------------------
#
# 事故：``try_mark_phase`` 的 CAS 失配（另一条路径 —— 被打断那一轮的 verdict
# 写入 —— 在停止窗口里把 routing 行从 ``user_stopping`` 挪回了
# ``verification_repairing``），于是 stop 整条提前 return，**验证行一次都没
# 被写成 stopped**。计划没有任何「被停过」的痕迹，列表于是把它显示成
# 「排队中」。
#
# 下面两条钉住：相位仍然不覆盖别人的写，但**验证行必须记下「操作者停过」**。


def test_stop_lost_the_phase_race_still_marks_the_verification_row_stopped(
    env, client, captured_finalize, monkeypatch,
):
    """相位让给了另一条路径 —— 验证行仍然标 stopped。

    CAS 失配时相位**不写**（那条路径对自己的相位负责），但验证行要写：它是
    「操作者停过这条计划」的唯一持久证据。没有它，「停过」这个事实凭空
    消失，计划就退化成一条「失败 / 等待的计划」。

    断言四件事：``mark_stopped`` 被调用了一次、参数是 ``"user_stopped"``；
    相位没有被 stop 覆盖（仍然是另一条路径写的那一个）；停止记录被清空；
    整条回调没有抛异常。
    """
    from state_machine.repositories.routing_repository import (
        ConflictError,
        RoutingRepository,
    )
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="verification_running")

    _stop(client)
    assert _read_phase(db_path, PLAN_ID) == "user_stopping"

    calls: list = []
    real_try_mark_phase = RoutingRepository.try_mark_phase
    real_mark_stopped = VerificationRepository.mark_stopped

    def _moved_by_another_path(self, plan_id, expected_phases, new_phase):
        # 另一条路径（被打断那一轮的 verdict 写入）把行挪走了。
        real_try_mark_phase(
            self, plan_id, ("user_stopping",), "verification_repairing",
        )
        raise ConflictError("the row was moved by another path")

    def _recording_mark_stopped(self, plan_id, reason, *args, **kwargs):
        calls.append((plan_id, reason))
        return real_mark_stopped(self, plan_id, reason, *args, **kwargs)

    monkeypatch.setattr(
        RoutingRepository, "try_mark_phase", _moved_by_another_path,
    )
    monkeypatch.setattr(
        VerificationRepository, "mark_stopped", _recording_mark_stopped,
    )

    # 不许抛异常：一次停止不该把后台层搞挂。
    captured_finalize["finalize"](PLAN_ID)

    assert calls == [(PLAN_ID, "user_stopped")], (
        "the verification row was not marked stopped after the stop lost "
        f"the race for the phase: {calls!r}"
    )
    row = _read_verification_row(db_path, PLAN_ID)
    assert row.get("verification_status") == "loop_stopped", row
    assert row.get("verification_stop_reason") == "user_stopped", row
    assert _read_phase(db_path, PLAN_ID) == "verification_repairing", (
        "the stop overwrote the phase another path had already written — "
        "the CAS must stay a CAS"
    )
    assert stop_state.get(PLAN_ID) is None, (
        "the stop record survived a stop that is already over — the next "
        "run is gated by a stop that never happened"
    )


def test_stop_that_owns_the_phase_still_marks_the_row_stopped(
    env, client, captured_finalize,
):
    """安静路径（stop 赢得 CAS）同样把验证行标成 stopped。

    这是上一条的对照：两条路径都必须留下「被停过」的痕迹，只是相位归谁
    写不同。
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, phase="verification_running")

    _stop(client)
    captured_finalize["finalize"](PLAN_ID)

    row = _read_verification_row(db_path, PLAN_ID)
    assert row.get("verification_status") == "loop_stopped", (
        f"the quiet stop did not mark the verification row stopped: {row}"
    )
    assert row.get("verification_stop_reason") == "user_stopped", row
    assert _read_phase(db_path, PLAN_ID) == "verification"
