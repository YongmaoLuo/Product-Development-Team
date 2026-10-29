"""The stop report is only there when there is a report to show.

背景
----
``POST /api/execution/{plan_id}/stop`` answers 200 with a small body.
Four of its keys are the contract every reader already knows:
``status`` / ``paused`` / ``paused_at`` / ``interrupted_task_count``.

A fifth key, ``stop_report``, answers "what did this stop actually do
to the process tree" — the executor's own stage, the stage of every
child it stopped, and the pids that were still alive afterwards.  It
is only meaningful when the stop **stopped something**.

On the three idle branches — no checkpoint at all, a checkpoint written
before the report existed, and a discard — there was nothing to stop.
Those branches used to answer with a **synthesised** report: the same
three parts, with an executor that never existed (``already_gone``,
no pid), no children and no leftovers.

A synthesised report is shaped exactly like a real one, so a reader
cannot tell the two apart by looking at the body: "the stop found
nothing to report" and "the stop found nothing to stop" answer the
same way.  And the discard branch answered with a report about the
very processes it had just thrown away.

这份测试钉的契约是：**报告只在真的存在时出现**。

* 空闲停止（没有 checkpoint）→ 响应体就是那四个键，没有 ``stop_report``，
  磁盘上也不会凭空多出一个 ``pause_state.json``；
* 丢弃（``?discard=true``）→ 同样没有 ``stop_report``，checkpoint 被删掉；
* 重复停止 → 只回答 checkpoint 里**已经写着**的那份报告，逐字段原样返回；
* 旧 checkpoint（写的时候还没有报告这个键）→ 没有 ``stop_report``，
  但 ``paused`` 仍然是 ``true``——暂停这件事是真的；
* 真的在跑的 plan → 报告照旧，三个部分一个不少。

没有 ``stop_report`` 的分支，答案里也**不出现**一个空的报告：空报告和
没有报告是两句话。前者说"我停了，但没停着什么"，后者说"我没停任何东西"。

这份文件只写契约，不改生产代码——它先把新契约钉成失败的样子。

有四条断言今天**本来就是红的**
------------------------------
七条里有四条断言的是**还没有实现**的契约：空闲停止、丢弃停止、旧
checkpoint 停止这三条分支现在还会凭空塞一份报告进来，``_empty_stop_report()``
这个助手也还在。这正是这份文件要钉住的东西——它必须以失败的样子存在，
否则没人看得见契约和现状之间的差。

但"红的"不能变成"永远红的"，更不能变成"看起来是绿的"。生产代码的改动归
plan task 2（``backend/routes/execution.py``）：它会删掉那个助手和它的三个
调用点，到那时上面那四条断言会**真的**跑起来并且变绿。

这四条失败**不要**用 ``xfail`` / ``skip`` 掩掉。plan 的 ``import.json`` 明写
「不放宽断言、不 skip/xfail 到绿」：把红变成绿的标记，会让这份文件在契约
还没落地时就报绿——"契约没立住"和"契约立住了"看起来一模一样，正是这份
文件要消灭的那种混同。断言一个字都没有放宽，红就是红。

代价：在契约落地之前，这个文件**必然**是红的，plan task 1 的
``test_commands`` 跑它会退出 1。这是 plan 的 task-1 命令与 task 1 的交付物
（「以失败形式钉住」）之间的矛盾，不是这份文件的缺陷——该由 plan 决定
task 1 的门禁怎么表达 RED，而不是由这份文件假装自己绿了。
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

# ``import server`` has to come FIRST.  ``server`` imports ``routes.execution``
# back at the bottom of its own module, and ``routes.execution`` late-binds
# into ``server`` — so importing ``routes.execution`` first walks into a
# half-initialised ``server`` and dies with an ImportError.
import server
from server import app
from routes import execution as ex

import pause_state
from pause_state import checkpoint_path
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.plan_task_repository import PlanTaskRepository
from state_machine.repositories.routing_repository import RoutingRepository

PLAN_ID = "20261028-stop-report-idle"

#: The pid the fake executor answers with.  Nothing signals it: the
#: fixture replaces the signal step, so this number is only ever a label.
FAKE_PID = 4242


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


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    plan_id: str = PLAN_ID,
    task_statuses: Optional[Dict[str, str]] = None,
    running: bool = True,
):
    """A plan, its tasks, and (optionally) a live executor.

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
            "started_at": "2026-10-28T09:00:00",
            "ended_at": None,
            "pid": FAKE_PID,
            "stop_reason": None,
        }
    return plan_dir, project_dir


def _write_checkpoint(plan_dir: Path, project_dir: Path) -> Dict[str, Any]:
    """A checkpoint as a previous stop left it — no report inside."""
    return pause_state.write_pause_checkpoint(
        plan_dir,
        kind="execution",
        project_dir=str(project_dir),
        reason="user_requested",
        interrupted_tasks=[],
    )


def _file_report_into_checkpoint(
    plan_dir: Path, report: Dict[str, Any]
) -> None:
    """Put a report into the checkpoint, the way a first stop would.

    The report is filed **inside** the checkpoint document, so a repeat
    of that stop reads it back from there.  This writes the same shape
    by hand rather than calling the production helper: this file is
    about what the endpoint does with what is on disk, and a test that
    seeds the disk through the code under test cannot tell a broken
    reader from a broken writer.
    """
    path = checkpoint_path(plan_dir)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["stop_report"] = report
    path.write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Nothing was stopped → there is no report
# ---------------------------------------------------------------------------


def test_idle_stop_omits_stop_report(env, client):
    """A stop against a plan that is not running reports nothing.

    There was no executor to stop and no child to have stopped, so the
    answer is the four keys every reader already knows — and not one
    more.  A synthesised report would say "already_gone, no children"
    about a process that never existed.

    Nothing is recorded either: a stop that interrupted nothing leaves
    no ``pause_state.json`` behind for a later reader to mistake for a
    real interruption.
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


def test_idle_stop_files_no_report(env, client, monkeypatch):
    """An idle stop never files a report into a checkpoint.

    Filing is how a report survives to the next reader, so a stop that
    has no report to file must not file one.  The counter is on the
    production helper itself, not on the response body — a stop that
    filed an empty report and then left it out of the answer would
    still be caught here.
    """
    plans_dir, db_path = env
    _seed_plan(plans_dir, db_path, running=False)

    real = ex._file_stop_report
    filed: List[Any] = []

    def _counting(plan_dir, stop_report):
        filed.append((plan_dir, stop_report))
        return real(plan_dir, stop_report)

    monkeypatch.setattr(ex, "_file_stop_report", _counting)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text
    assert filed == [], filed


def test_idle_discard_stop_omits_stop_report(env, client):
    """A discard throws the work away — and reports nothing about it.

    The checkpoint is gone afterwards, so the report that would have
    been filed next to it is gone too: a report about processes the
    operator just discarded is not evidence of anything.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, running=False)
    _write_checkpoint(plan_dir, project_dir)
    assert (plan_dir / "pause_state.json").exists()

    resp = client.post(f"/api/execution/{PLAN_ID}/stop?discard=true")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert "stop_report" not in body, body
    assert not (plan_dir / "pause_state.json").exists(), sorted(
        p.name for p in plan_dir.iterdir()
    )


# ---------------------------------------------------------------------------
# A repeat stop answers with the report that is already on disk
# ---------------------------------------------------------------------------


def test_repeat_stop_returns_the_stored_report(env, client):
    """A repeat stop answers with the FIRST stop's report, verbatim.

    The repeat has no processes left to report on, so the only honest
    answer is the one the first stop collected and filed.  It is
    returned as it was written — field by field — because a report
    that came back reshaped is a report nobody can compare with the
    one on disk.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, running=False)
    _write_checkpoint(plan_dir, project_dir)

    stored = {
        "executor": {"pid": FAKE_PID, "stage": "SIGTERM", "hard_killed": False},
        "children": [{"pid": 5150, "stage": "stdout", "forced": False}],
        "leftover_pids": [5151],
    }
    _file_report_into_checkpoint(plan_dir, stored)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert "stop_report" in body, body
    report = body["stop_report"]
    assert report["executor"] == stored["executor"], report
    assert report["children"] == stored["children"], report
    assert report["leftover_pids"] == stored["leftover_pids"], report
    assert report == stored, report


def test_repeat_stop_with_a_legacy_checkpoint_omits_stop_report(env, client):
    """A checkpoint written before the report existed carries no report.

    The pause is real — the plan IS paused, and the answer says so with
    ``paused: true``.  What it must not do is invent a report to fill
    the shape: an empty one would claim the stop found an executor
    that never existed.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path, running=False)
    _write_checkpoint(plan_dir, project_dir)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert "stop_report" not in body, body
    assert body["paused"] is True, body


# ---------------------------------------------------------------------------
# The real stop keeps its report
# ---------------------------------------------------------------------------


def test_running_stop_still_carries_the_full_report(env, client, monkeypatch):
    """A stop that actually stopped something still answers with a report.

    This is the path the report was built for: an executor was
    signalled, it may have stopped children, and the answer has to say
    so.  The report is filed into the checkpoint, so the contract above
    (a repeat answers with what is on disk) has something real to hand
    back.

    The kill and the signal are replaced: the fake executor's pid
    belongs to nobody, and the stop path must never reach a real
    ``killpg`` because of a test.
    """
    plans_dir, db_path = env
    plan_dir, _ = _seed_plan(
        plans_dir, db_path, task_statuses={"2": "in_progress"}, running=True,
    )

    killed: List[Any] = []

    def _fake_kill(proc, grace_seconds=None):
        killed.append((proc, grace_seconds))
        # A cooperative exit: the executor goes away on its own.
        proc.wait(timeout=0)
        return False

    monkeypatch.setattr(server, "_kill_process_tree", _fake_kill)
    monkeypatch.setattr(server, "_send_stop_signal", lambda proc: False)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert killed, "the stop never reached the executor"
    assert "stop_report" in body, body
    assert set(body["stop_report"]) == {
        "executor", "children", "leftover_pids",
    }, body


# ---------------------------------------------------------------------------
# The helper that made up the reports is gone
# ---------------------------------------------------------------------------


def test_empty_stop_report_helper_is_gone():
    """``_empty_stop_report()`` no longer exists.

    The helper existed to fill the shape on the branches that had no
    report — which is exactly what made a synthesised report
    indistinguishable from a real one.  With the key gone from those
    branches, the helper has no caller left, and keeping it would only
    invite the next reader to wire it back up.
    """
    assert not hasattr(ex, "_empty_stop_report"), (
        "routes.execution still carries the _empty_stop_report() helper"
    )
