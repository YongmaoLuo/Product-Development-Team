"""停止理由带上退出码或信号，新一轮开始时旧崩溃清干净。

背景
----
``non_zero_exit`` 只说了「executor 挂了」，没说「怎么挂的」。
退出 1 和退出 7 在那一列里长得一模一样，SIGKILL 和 SIGTERM 也是
—— 而这两种崩溃要的反应正好相反。理由必须把结束它的那一个数字
带上。

另一半是轮次的边界。上一轮崩溃写进 ``plan_execution.flags`` 的
``crash`` 键会一直留在那里，直到有人把它拿走。于是「第一轮崩了、
第二轮又跑了一遍」的 plan，在第二轮里仍然挂着第一轮的崩溃：
读者分不清那个崩溃属于哪一轮。轮次的边界就是启动那一刻 ——
新一轮开始，旧崩溃清掉。

这里钉的是**接线**：走真正的生产入口（``start_execution`` →
watcher → 数据库），断言那一列里最后是带数字的理由；启动之后，
flags 里只剩它本来就有的键。

还有一件事：改了口径，认口径的人也得跟着改。信号死亡以前写的是
``process_died_unexpectedly``，读者拿这个字面量判断「这是不是崩
了」；现在它写 ``signal:9``，于是那些读者必须**换个写法也要认**，
否则死掉的 pid 留在记录上、写丢的判决再也补不回来 —— 一个早就
死了的执行看起来还活着。

TDD 规格 — 7 条契约测试
------------------------
1. ``test_non_zero_exit_reason_carries_the_code`` — rc=7 →
   ``non_zero_exit:7``。
2. ``test_signal_death_reason_names_the_signal`` — rc=-9 →
   ``signal:9``。
3. ``test_clean_crash_reason_unchanged`` — rc=0 → 理由仍然是
   ``process_died_unexpectedly``（没带数字的死亡不改口径）。
4. ``test_start_clears_previous_crash_flags`` — 启动后只剩
   ``{'arch_enabled': True}``，崩溃键没了。
5. ``test_start_without_crash_flags_does_not_write`` — 没有旧崩溃
   可清时，不产生多余的写。
6. ``test_signal_death_clears_the_dead_pid`` — 信号死亡之后，
   死掉的 pid 不留在内存里。
7. ``test_lost_write_is_healed_for_a_signal_death`` — 判决写丢了，
   读接口把它补回去，而且补回去的理由带着信号。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` must be imported first: it seeds ``sys.modules['server']``
# and then wires the ``routes`` package, and ``routes.execution``
# late-binds back into it.
import server  # noqa: E402


_PLAN_ID = "executor-stop-reason-refined-plan"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Minimal stand-in for the executor ``subprocess.Popen`` handle."""

    def __init__(self, returncode: int) -> None:
        self.pid = 424242
        self.returncode = returncode
        self.args = ["/venv/bin/python3", "backend/cli.py"]

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


def _make_state_db(tmp_path: Path) -> Path:
    """Allocate a hermetic ``state.db`` and return its path."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()
    return db_path


def _seed_plan(
    db_path: Path,
    plan_id: str,
    project_dir: Path,
    *,
    flags: Optional[Dict[str, Any]] = None,
) -> None:
    """Insert the ``plan_routing`` / ``plan_execution`` rows a start needs."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, substage, current_phase, version, completed_phases, "
            " review_rounds, flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, None, "queued", 0, "[]",
                "{}", "{}",
                json.dumps(
                    {
                        "status": "pending",
                        "round": 0,
                        "max_rounds": 3,
                        "stop_reason": None,
                    }
                ),
                "2026-11-07T00:00:00", "2026-11-07T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution "
            "(plan_id, attempt_count, project_dir, stop_reason, task_progress, "
            " next_run_at, card_state, flags, exec_pid, exec_status, "
            " started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, 0, str(project_dir), "startup_reconcile",
                json.dumps({"tasks": {}}),
                None, "{}", json.dumps(flags if flags is not None else {}),
                None, "not_started", None,
                "2026-11-07T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _read_row(db_path: Path, plan_id: str) -> Dict[str, Any]:
    """Read the plan's ``plan_execution`` row: status, reason and flags."""
    from state_machine.db.connection import open as open_db

    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT exec_status, stop_reason, flags, exec_pid "
            "FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        )
        row = cur.fetchone()
        assert row is not None, "the plan has no execution row"
        return {
            "exec_status": row[0],
            "stop_reason": row[1],
            "flags": json.loads(row[2]) if row[2] else None,
            "exec_pid": row[3],
        }
    finally:
        conn.close()


def _drive_start(
    tmp_path,
    monkeypatch,
    *,
    returncode: int = 1,
    flags: Optional[Dict[str, Any]] = None,
    keep_state: bool = False,
) -> Tuple[Path, Optional[Any]]:
    """Run the real ``start_execution``; return the db path and the watcher.

    The watcher is a daemon thread in production; the test hands the body
    over instead (``_run_in_plan_ctx``), so the assertions cannot race
    the thread.  The caller decides whether to run it at all: the flags
    tests want the *start* only, the reason tests want the watcher's
    verdict as well.

    ``keep_state`` leaves the in-memory execution record in the
    registry.  The verdict readers (``_adopt_row_verdict``, the status
    endpoint) read *that* record, so a test about them needs it to be
    there; the other tests do not care and get a clean registry.
    """
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / _PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "1", "title": "t", "depends_on": []}]}),
        encoding="utf-8",
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db_path = _make_state_db(tmp_path)
    _seed_plan(db_path, _PLAN_ID, project_dir, flags=flags)

    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    monkeypatch.setattr(server, "_reject_if_placeholder_prd", lambda *a, **k: None)
    monkeypatch.setattr(
        "state_machine.db.archive_scan.classify_plan", lambda *a, **k: "active",
    )
    monkeypatch.setattr(
        server,
        "_spawn_executor_subprocess",
        lambda **kwargs: (_FakeProcess(returncode), plan_dir / "executor.log"),
    )
    monkeypatch.setattr(server, "_refresh_usage_report", lambda *a, **k: None)
    monkeypatch.setattr(
        server,
        "_count_unfinished_tasks",
        lambda *a, **k: {
            "total": 0,
            "pending_or_unstarted": 0,
            "failed": 0,
            "unfinished_ids": [],
        },
    )
    # The verification round is not what this test is about, and a real
    # one would call out to a provider.
    monkeypatch.setattr(
        server, "_run_auto_verification_loop", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        server, "_settle_phase_after_verification_loop", lambda *a, **k: None,
    )

    class _FakePlanState:
        """Stands in for ``server.PlanState`` — the name the route uses."""

        def __init__(self, plan_dir_arg):
            self.plan_dir = plan_dir_arg

        def transition_to(self, phase):
            pass

        def force_set_phase(self, phase):
            pass

    monkeypatch.setattr(server, "PlanState", _FakePlanState)

    captured: List[Any] = []
    monkeypatch.setattr(
        server, "_run_in_plan_ctx", lambda plan_id_arg, fn: captured.append(fn),
    )

    server._execution_state.pop(_PLAN_ID, None)
    try:
        server.start_execution(
            _PLAN_ID,
            server.StartExecutionRequest(project_dir=str(project_dir)),
        )
        deadline = time.monotonic() + 10.0
        while not captured and time.monotonic() < deadline:
            time.sleep(0.01)
        assert captured, "the execution watcher never handed over"
        return db_path, captured[0]
    finally:
        if not keep_state:
            server._execution_state.pop(_PLAN_ID, None)


# ---------------------------------------------------------------------------
# the reason carries the number
# ---------------------------------------------------------------------------


def test_non_zero_exit_reason_carries_the_code(tmp_path, monkeypatch):
    """executor 退出码 7 → 理由是 ``non_zero_exit:7``。

    ``non_zero_exit`` 只说「非零退出」——退出 1 和退出 7 在那一列里
    长得一模一样，而它们要的反应未必一样。
    """
    db_path, watcher = _drive_start(tmp_path, monkeypatch, returncode=7)

    watcher()

    row = _read_row(db_path, _PLAN_ID)
    assert row["exec_status"] == "failed"
    assert row["stop_reason"] == "non_zero_exit:7", (
        "the reason must name the exit code that ended the run"
    )


def test_signal_death_reason_names_the_signal(tmp_path, monkeypatch):
    """executor 被信号打死（rc=-9）→ 理由是 ``signal:9``。

    信号死亡和退出码死亡是两种事故：一个要查是谁发的信号，一个要查
    程序自己干了什么。``process_died_unexpectedly`` 把两者抹成同一句
    话，读者只能回头去翻日志。
    """
    db_path, watcher = _drive_start(tmp_path, monkeypatch, returncode=-9)

    watcher()

    row = _read_row(db_path, _PLAN_ID)
    assert row["exec_status"] == "failed"
    assert row["stop_reason"] == "signal:9", (
        "a death by signal must name the signal"
    )


def test_clean_crash_reason_unchanged():
    """没有数字可带的死亡 → 理由一个字都不改。

    ``process_died_unexpectedly`` 说的不是「退出码是 0」，而是「死了，
    但说不出是怎么死的」。那种死亡不该被伪装成一次有编号的退出。
    """
    from routes.execution import _crash_stop_reason

    assert _crash_stop_reason(0) == "process_died_unexpectedly", (
        "a death that carries no number keeps the reason it always had"
    )


# ---------------------------------------------------------------------------
# a new round does not inherit the last round's crash
# ---------------------------------------------------------------------------


def test_start_clears_previous_crash_flags(tmp_path, monkeypatch):
    """新一轮开始 → 上一轮的崩溃键没了，其余键原样留着。

    ``crash`` 是上一轮写进去的。它留在那里，下一轮的读者就会把上一轮
    的事故当成这一轮的事故。轮次的边界是启动那一刻，所以清理也发生
    在启动那一刻。
    """
    db_path, _watcher = _drive_start(
        tmp_path,
        monkeypatch,
        flags={
            "arch_enabled": True,
            "crash": {
                "exit_code": 1,
                "signal": None,
                "report": "executor_crash_20261107-424242.json",
                "crashed_at": "2026-11-07T00:00:00+00:00",
            },
        },
    )

    row = _read_row(db_path, _PLAN_ID)
    assert row["flags"] == {"arch_enabled": True}, (
        "the previous round's crash must not survive into this round, and "
        "the flags the row already had must be kept"
    )


def test_start_without_crash_flags_does_not_write(tmp_path, monkeypatch):
    """没有旧崩溃可清 → 一次多余的写都没有。

    清理是「有才清」：flags 里没有 ``crash`` 键的时候，写回去的
    还是同一份内容，那次写只是白白改了一次 ``updated_at``。
    """
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    calls: List[Tuple[str, Dict[str, Any]]] = []
    original = ExecutionRepository.update_flags

    def _counting(self, plan_id, flags):
        calls.append((plan_id, dict(flags)))
        return original(self, plan_id, flags)

    monkeypatch.setattr(
        "state_machine.repositories.execution_repository."
        "ExecutionRepository.update_flags",
        _counting,
    )

    db_path, _watcher = _drive_start(
        tmp_path, monkeypatch, flags={"arch_enabled": True},
    )

    assert calls == [], (
        "a start with nothing to clear must not write the flags column"
    )
    row = _read_row(db_path, _PLAN_ID)
    assert row["flags"] == {"arch_enabled": True}


# ---------------------------------------------------------------------------
# the crash verdict is still a crash, whatever wording it wears
# ---------------------------------------------------------------------------


def test_signal_death_clears_the_dead_pid(tmp_path, monkeypatch):
    """信号死亡 → 死掉的 pid 不再留在内存里。

    崩溃是关于**进程**的判决，不是关于这一轮的判决：进程没了，它留下
    的 pid 就是一张过期的名片 —— 而每一个「这个执行还活着吗」的读者
    都把 pid 当成活着的证据。

    理由换了写法（``signal:9`` 不再等于旧的 ``process_died_unexpectedly``），
    这条判决就得**换个写法也要认**：认不出来，死 pid 就漏给了读者。
    """
    db_path, watcher = _drive_start(
        tmp_path, monkeypatch, returncode=-9, keep_state=True,
    )
    try:
        watcher()

        state = server._execution_state[_PLAN_ID]
        assert state["stop_reason"] == "signal:9"
        assert state["pid"] == 424242, (
            "the pid is still on the record before the verdict is read"
        )

        from routes.execution import get_execution_status

        get_execution_status(_PLAN_ID)

        assert server._execution_state[_PLAN_ID]["pid"] is None, (
            "a dead executor's pid must not survive its crash verdict — "
            "every 'is this execution alive?' reader believes a pid"
        )
    finally:
        server._execution_state.pop(_PLAN_ID, None)


def test_lost_write_is_healed_for_a_signal_death(tmp_path, monkeypatch):
    """判决写丢了 → 状态读接口把它补回去（信号死亡也是崩溃）。

    读接口有一条自愈：内存里已经有了判决、而那一行还写着 ``running``
    时，说明判决写丢了，于是它把判决补写回去。改口径以后这条自愈必须
    还认得 ``signal:9`` —— 认不得，那一行就永远停在 ``running``：一个
    早就死了的执行，看起来还活着，而且带着一个死 pid。
    """
    db_path, watcher = _drive_start(
        tmp_path, monkeypatch, returncode=-9, keep_state=True,
    )
    try:
        # The watcher reaches its verdict, but the write is lost: the row
        # stays ``running`` while the memory already says ``failed``.
        monkeypatch.setattr(
            "state_machine.repositories.execution_repository."
            "ExecutionRepository.update_status",
            lambda self, plan_id, status, **kwargs: None,
        )
        watcher()

        row = _read_row(db_path, _PLAN_ID)
        assert row["exec_status"] == "running", (
            "the write must be lost for this test to mean anything"
        )
        assert server._execution_state[_PLAN_ID]["stop_reason"] == "signal:9"

        from routes.execution import get_execution_status

        resp = get_execution_status(_PLAN_ID)

        row = _read_row(db_path, _PLAN_ID)
        assert row["exec_status"] == "failed", (
            "the lost verdict must be healed: a row that says 'running' "
            "forever is a dead executor that looks alive"
        )
        assert row["stop_reason"] == "signal:9", (
            "the healed verdict must keep the signal that named the death"
        )
        assert row["exec_pid"] is None, (
            "a judged row must not keep the pid of the process that died"
        )
        assert resp["status"] == "failed"
        assert resp["pid"] is None, (
            "the dead pid must not leak into the status answer"
        )
        assert server._execution_state[_PLAN_ID]["pid"] is None, (
            "the dead pid must not stay in the memory either"
        )
    finally:
        server._execution_state.pop(_PLAN_ID, None)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
