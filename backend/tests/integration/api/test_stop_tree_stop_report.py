"""The stop response carries the stop report.

背景
----
停止一个执行器会留下两半证据：**服务端**自己那一半（``execution_stop_result``
—— 执行器是自己退的还是被硬杀的）和**执行器**那一半（``dispatch_stop_result``
—— 每个子进程停在哪一阶段）。两条都落在 plan 自己的事件流里，但接口的响应
只说了 ``stopped``：页面按了「停止」，屏幕上只有一个「已停止」，没人答得出
「每个子进程停在哪一步」「有没有漏网的进程还在跑」。

这份报告回答三个问题：

* ``executor`` —— 执行器自己停在哪一步（``SIGTERM`` / ``SIGKILL`` /
  ``already_gone``），以及它是不是被硬杀的；
* ``children`` —— 每个子进程停在哪一阶段（``stdout`` / ``SIGTERM`` /
  ``SIGKILL``），逐个列出；
* ``leftover_pids`` —— 复查之后**还活着**的 pid：停机没停干净的证据。

报告是**新增**的键。响应里原有的四个键（``status`` / ``paused`` /
``paused_at`` / ``interrupted_task_count``）一个都不改——「停止即暂停」的
契约不因为多了一份报告而松动。报告**只在真的存在时出现**：空闲停止
（plan 本来就没在跑）没有执行器可停，也就没有可停的东西可报告，于是那条
200 只有四个键。一份凭空合成的空报告和真报告形状一模一样，「这次停止没
东西可报」会和「这次停止没东西可停」回答得一模一样——所以缺席比空形状
更诚实。

测试不碰真实进程：执行器是一个假进程（停机路径只会问它 ``poll()`` 和
``wait()``），子进程的结果由一个记录器在**停机过程中**写进 plan 的事件
流——这正是真实执行器被 SIGTERM 打断时做的事（它先停子进程、再退出）。
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import server
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.plan_task_repository import PlanTaskRepository
from state_machine.repositories.routing_repository import RoutingRepository

PLAN_ID = "20261027-stop-tree-report"

#: The pid the fake executor answers with.  Nothing signals it: the
#: fixture replaces the signal step, so this number is only ever a label.
FAKE_PID = 4242

#: Pids no process can have — Linux caps ``pid_max`` at 4194304 and macOS
#: at 99999, so a pid this large is guaranteed to be gone.  The leftover
#: check asks the kernel whether a pid is alive, so the pids it is asked
#: about have to be *provably* dead for the expectation to be stable.
DEAD_PID = 2_000_000_000
DEAD_PID_2 = 2_000_000_001


class FakeProc:
    """The executor, as the stop path sees it.

    ``_kill_process_tree`` asks a process three things: ``poll()`` (is it
    still there), ``pid`` (who to signal) and ``wait(timeout=...)`` (did
    it go away).  This answers all three, and reports itself gone when
    the wait returned — which is what a cooperative exit looks like to
    the server.
    """

    def __init__(self, pid: int = FAKE_PID) -> None:
        self.pid = pid
        self.alive = True
        self.wait_timeouts: List[Optional[float]] = []

    def poll(self) -> Optional[int]:
        return None if self.alive else 0

    def wait(self, timeout=None) -> int:
        self.wait_timeouts.append(timeout)
        self.alive = False
        return 0


class StopRecorder:
    """Stands in for ``server._kill_process_tree`` — but only for the writes.

    The **real** kill is still called: the server's own verdict on the
    stop (``execution_stop_result``: cooperative exit or hard kill) is
    one of the two things the report is built from, and a fake that
    skipped it would be testing a fiction.

    What the recorder adds is what the **executor** does while it is
    being terminated: it writes one ``dispatch_stop_result`` per child
    it stopped into the plan's own event stream.  Those writes land
    between the stop's start stamp and the report, so they are this
    stop's children — and a record left over from an earlier stop does
    not.
    """

    def __init__(
        self, plan_id: str, plan_dir: Path, real_kill: Any = None
    ) -> None:
        self.plan_id = plan_id
        self.plan_dir = plan_dir
        self.real_kill = real_kill
        #: The children the executor reports having stopped.
        self.children: List[Dict[str, Any]] = []
        #: The plan's in-memory state at the moment of the kill.
        self.state_at_kill: Dict[str, Any] = {}
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, proc: Any, grace_seconds: Any = None) -> bool:
        self.calls.append({"proc": proc, "grace_seconds": grace_seconds})
        self.state_at_kill = dict(server._execution_state.get(self.plan_id) or {})
        self._write_child_results()
        if self.real_kill is None:
            return False
        return self.real_kill(proc, grace_seconds=grace_seconds)

    def _write_child_results(self) -> None:
        """Write one ``dispatch_stop_result`` per child, as the executor does."""
        if not self.children:
            return
        try:
            with open(self.plan_dir / "execution.log", "a", encoding="utf-8") as handle:
                for child in self.children:
                    handle.write(
                        json.dumps(
                            {
                                "ts": datetime.utcnow().isoformat(),
                                "level": "INFO",
                                "event": "dispatch_stop_result",
                                "message": (
                                    f"child pid={child.get('pid')} stopped at "
                                    f"{child.get('stage')}"
                                ),
                                "data": dict(child),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        except OSError:
            # A log that cannot be written is exactly the case the report
            # has to survive — the stop goes on.
            pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    # The plan's event stream is read back through the resolved plans
    # root, so both resolvers have to point at the same tree.
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    server._execution_state.clear()

    yield plans_dir, db_path

    server._execution_state.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def stop_recorder(monkeypatch):
    """Replace the kill with a recorder that adds the executor's own writes.

    The signal step is replaced too: the fake executor's pid belongs to
    nobody, and the stop path must never reach a real ``killpg`` because
    of a test.
    """
    real_kill = server._kill_process_tree

    def _install(plan_id: str, plan_dir: Path) -> StopRecorder:
        rec = StopRecorder(plan_id, plan_dir, real_kill=real_kill)

        def _kill(proc, grace_seconds=None):
            return rec(proc, grace_seconds=grace_seconds)

        monkeypatch.setattr(server, "_kill_process_tree", _kill)
        monkeypatch.setattr(
            server, "_send_stop_signal", lambda proc: False,
        )
        return rec

    return _install


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    plan_id: str = PLAN_ID,
    task_statuses: Optional[Dict[str, str]] = None,
    running: bool = True,
):
    """A plan mid-execution, with tasks and a live executor.

    Returns ``(plan_dir, project_dir)``.  A SECOND connection is used
    (not the fixture's) so the rows the endpoint reads back are the
    committed ones.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    tasks = [
        {"id": "1", "title": "first task"},
        {"id": "2", "title": "second task"},
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "executing")
        ExecutionRepository(conn).insert(
            plan_id=plan_id,
            project_dir=str(project_dir),
            exec_status="running",
            exec_pid=FAKE_PID,
        )
        repo = PlanTaskRepository(conn)
        for task in tasks:
            repo.add_task(plan_id, dict(task))
        for task_id, status in (task_statuses or {}).items():
            repo.update_task(plan_id, task_id, {"status": status})
    finally:
        conn.close()

    if running:
        server._execution_state[plan_id] = {
            "status": "running",
            "logs": deque(maxlen=100),
            "project_dir": str(project_dir),
            "process": FakeProc(),
            "started_at": "2026-10-27T09:00:00",
            "ended_at": None,
            "pid": FAKE_PID,
            "stop_reason": None,
        }
    return plan_dir, project_dir


def _write_old_stop_record(plan_dir: Path, pid: int, stage: str) -> None:
    """Write a ``dispatch_stop_result`` as an *earlier* run would have."""
    stale = datetime.utcnow() - timedelta(hours=1)
    with open(plan_dir / "execution.log", "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "ts": stale.isoformat(),
                    "level": "INFO",
                    "event": "dispatch_stop_result",
                    "message": f"child pid={pid} stopped at {stage}",
                    "data": {"pid": pid, "stage": stage, "forced": False},
                },
                ensure_ascii=False,
            )
            + "\n"
        )


# ---------------------------------------------------------------------------
# The executor's own stage
# ---------------------------------------------------------------------------


def test_stop_response_carries_executor_stage(env, client, stop_recorder):
    """The response says which stage the executor's own stop reached.

    The executor went away on its own — it did not have to be SIGKILLed
    — so the report says ``SIGTERM`` and ``hard_killed`` is False.
    """
    plans_dir, db_path = env
    plan_dir, _ = _seed_plan(plans_dir, db_path, task_statuses={"2": "in_progress"})
    stop_recorder(PLAN_ID, plan_dir)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert "stop_report" in body, body
    executor = body["stop_report"]["executor"]
    assert executor["stage"] == "SIGTERM", executor
    assert executor["hard_killed"] is False, executor
    assert executor["pid"] == FAKE_PID, executor


# ---------------------------------------------------------------------------
# One entry per child
# ---------------------------------------------------------------------------


def test_children_listed_per_process(env, client, stop_recorder):
    """Two stopped children → two entries, each with its own stage."""
    plans_dir, db_path = env
    plan_dir, _ = _seed_plan(plans_dir, db_path, task_statuses={"2": "in_progress"})
    recorder = stop_recorder(PLAN_ID, plan_dir)
    recorder.children = [
        {"pid": DEAD_PID, "stage": "stdout", "forced": False},
        {"pid": DEAD_PID_2, "stage": "SIGTERM", "forced": False},
    ]

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    children = resp.json()["stop_report"]["children"]
    assert len(children) == 2, children
    assert [c["pid"] for c in children] == [DEAD_PID, DEAD_PID_2], children
    assert [c["stage"] for c in children] == ["stdout", "SIGTERM"], children
    assert [c["forced"] for c in children] == [False, False], children


def test_children_ignore_previous_runs(env, client, stop_recorder):
    """A record left by an earlier stop is not this stop's child.

    The report is about ONE stop.  The plan's event stream is append-only
    and outlives the stop that wrote it, so without the ``since`` stamp
    every stop would answer with the union of every stop that ever ran.
    """
    plans_dir, db_path = env
    plan_dir, _ = _seed_plan(plans_dir, db_path, task_statuses={"2": "in_progress"})
    _write_old_stop_record(plan_dir, DEAD_PID_2, "stdout")
    recorder = stop_recorder(PLAN_ID, plan_dir)
    recorder.children = [
        {"pid": DEAD_PID, "stage": "SIGTERM", "forced": False},
    ]

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    children = resp.json()["stop_report"]["children"]
    assert [c["pid"] for c in children] == [DEAD_PID], children


# ---------------------------------------------------------------------------
# The leftovers
# ---------------------------------------------------------------------------


def test_leftover_pids_empty_after_stop(env, client, stop_recorder):
    """A child that is gone is not a leftover."""
    plans_dir, db_path = env
    plan_dir, _ = _seed_plan(plans_dir, db_path, task_statuses={"2": "in_progress"})
    recorder = stop_recorder(PLAN_ID, plan_dir)
    recorder.children = [
        {"pid": DEAD_PID, "stage": "SIGTERM", "forced": False},
    ]

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    report = resp.json()["stop_report"]
    assert report["leftover_pids"] == [], report


# ---------------------------------------------------------------------------
# The paths that have nothing to report
# ---------------------------------------------------------------------------


def test_idle_stop_has_an_empty_stop_report(env, client):
    """A stop against a plan that is not running carries NO report.

    There was no executor to stop and no child to have stopped, so
    there is nothing to report.  A synthesised empty report would be
    shaped exactly like a real one — and "the stop found nothing to
    report" would answer the same way as "the stop found nothing to
    stop".  So the key is ABSENT on this path, not empty.

    The name is historical: it used to pin the empty-report contract,
    which was overturned on 2026-10-28.

    2026-10-28: this used to assert the opposite ("every 200 answer
    carries the key, empty on an idle stop").  The contract is now that
    the report shows up ONLY when there really is one, so the
    assertions below pin the idle answer exactly: the four original
    keys, all of them, and nothing else — and no checkpoint, because a
    stop that interrupted nothing has nothing to resume from.
    """
    plans_dir, db_path = env
    plan_dir, _ = _seed_plan(
        plans_dir, db_path, task_statuses={"2": "in_progress"}, running=False,
    )

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert set(body) == {
        "status", "paused", "paused_at", "interrupted_task_count",
    }, body
    assert body == {
        "status": "stopped",
        "paused": False,
        "paused_at": None,
        "interrupted_task_count": 0,
    }, body
    assert not (plan_dir / "pause_state.json").exists(), sorted(
        p.name for p in plan_dir.iterdir()
    )


def test_unreadable_log_yields_empty_children(env, client, stop_recorder):
    """A log that cannot be read → no children, and no 500.

    The stop already worked — the work is killed and the checkpoint is
    written.  Losing the report must not undo that, and it must not
    turn the stop into an error either: "the log could not be read" is
    an answer, and the answer here is "no children were reported".
    """
    plans_dir, db_path = env
    plan_dir, _ = _seed_plan(plans_dir, db_path, task_statuses={"2": "in_progress"})
    # A directory where the log file belongs: it cannot be read as one.
    (plan_dir / "execution.log").mkdir()
    stop_recorder(PLAN_ID, plan_dir)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["stop_report"]["children"] == [], body


# ---------------------------------------------------------------------------
# The report is an addition, not a replacement
# ---------------------------------------------------------------------------


def test_stop_still_pauses_the_plan(env, client, stop_recorder):
    """The report does not loosen the stop-is-a-pause contract.

    The plan is marked ``pausing`` while the kill is in flight (the
    executor's watcher and the heartbeat watchdog both act on
    ``running``, so the mark has to be in place first), and the
    checkpoint is written once the interruption is known.
    """
    plans_dir, db_path = env
    plan_dir, _ = _seed_plan(plans_dir, db_path, task_statuses={"2": "in_progress"})
    recorder = stop_recorder(PLAN_ID, plan_dir)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["paused"] is True, body
    assert body["paused_at"], body
    assert "stop_report" in body, body
    assert recorder.state_at_kill.get("stop_reason") == "pausing", (
        recorder.state_at_kill
    )
    assert (plan_dir / "pause_state.json").exists(), sorted(
        p.name for p in plan_dir.iterdir()
    )


def test_existing_fields_keep_their_names_and_types(env, client, stop_recorder):
    """The four original keys are untouched; ``stop_report`` is a fifth.

    The response is a contract other code already reads.  Adding the
    report is allowed; renaming, retyping or dropping one of these is
    not.
    """
    plans_dir, db_path = env
    plan_dir, _ = _seed_plan(plans_dir, db_path, task_statuses={"2": "in_progress"})
    stop_recorder(PLAN_ID, plan_dir)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["status"] == "stopped", body
    assert body["paused"] is True, body
    assert body["paused_at"] is None or isinstance(body["paused_at"], str), body
    assert isinstance(body["interrupted_task_count"], int), body
    assert not isinstance(body["interrupted_task_count"], bool), body

    # The report is an addition: every original key is still there.
    for key in ("status", "paused", "paused_at", "interrupted_task_count"):
        assert key in body, body
    assert set(body["stop_report"]) == {"executor", "children", "leftover_pids"}, body
