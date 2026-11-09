"""executor 被信号带走时：先写下现场，再退出。

背景
----
``cli._on_sigterm`` / ``cli._on_sigint`` 此前直接 ``os._exit``：进程没了，
磁盘上什么也没有，事后只能从「子进程什么时候停的」去猜执行器死在哪一步。
``main()`` 里的 ``CrashGuard`` 已经在「未捕获异常」和「非零 ``sys.exit``」
两条路上写现场，信号这条路是最后一条漏的 —— 而它恰恰是最需要现场的一条：
信号到达时进程正卡在某个栈里，那个栈就是全部证据。

What is under test
------------------
信号处理器在退出之前把现场写下来：

* SIGTERM → 现场里 ``signal=15`` / ``SIGTERM``，退出码仍然是 143；
* SIGINT → 现场里 ``signal=2`` / ``SIGINT``，退出码仍然是 130；
* 第二个信号走「立刻退出」的早退分支 → 不再写第二份现场；
* 没有 guard（测试或工具直接调处理器）→ 什么都不写，退出码不变；
* 写现场自己抛异常 → 吞掉，退出码不变。

现场写在收尾之后、退出之前：停止序列、ledger 清扫、锁释放的顺序不变
（清扫先于释放，释放先于退出），现场不能插到它们前面 —— 那一步是
「让计划自己的进程树先收干净」，把它挤到最后会让现场变成第二件事。

测试不碰真实进程：``stop_all_live_dispatches`` / ``reap_plan`` /
``_exit_now`` 都换成记录器（真的退出就会把 pytest 一起带走）。
"""

from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Callable, List

import pytest

import cli
from crash_forensics import FORENSICS_ENV, CrashGuard


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_plan(tmp_path: Path, name: str = "p1") -> Path:
    """A plan directory -- the directory the crash scene is written under."""
    plan_dir = tmp_path / name
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text("[]\n", encoding="utf-8")
    return plan_dir


def _scene_files(plan_dir: Path) -> List[Path]:
    """The crash scenes written under ``plan_dir``, oldest first."""
    logs = plan_dir / "logs"
    if not logs.is_dir():
        return []
    return sorted(logs.glob("executor_crash_*.json"))


def _read_scene(plan_dir: Path) -> dict:
    """The one crash scene written under ``plan_dir``."""
    files = _scene_files(plan_dir)
    assert len(files) == 1, "expected exactly one crash scene, got {}".format(files)
    return json.loads(files[0].read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _isolated_module_state(monkeypatch):
    """Give the module globals and the environment back the way they were.

    处理器读的是模块级的槽位：``_crash_guard``（现场由谁写）、``_stop_in_progress``
    （是不是第二个信号）、``_executor_plan_dir`` / ``_executor_plan_id`` /
    ``_executor_lock``（要清扫谁）。这些槽位是上一趟运行留下的，不还原的话
    后面那个测试会拿到别人的状态 —— 而失败的信号处理器是会真的退出的。
    """
    monkeypatch.setattr(cli, "_crash_guard", None, raising=False)
    monkeypatch.setattr(cli, "_stop_in_progress", False)
    monkeypatch.setattr(cli, "_executor_plan_dir", None)
    monkeypatch.setattr(cli, "_executor_plan_id", "")
    monkeypatch.setattr(cli, "_executor_lock", None)
    monkeypatch.delenv(FORENSICS_ENV, raising=False)


@pytest.fixture
def guard_factory(monkeypatch) -> Callable[[Path], CrashGuard]:
    """Put a guard in the module slot, the way a running executor has it."""
    made: List[CrashGuard] = []

    def make(plan_dir: Path) -> CrashGuard:
        guard = CrashGuard(plan_dir, "p1")
        guard.install()
        made.append(guard)
        monkeypatch.setattr(cli, "_crash_guard", guard)
        return guard

    yield make

    for guard in made:
        try:
            guard.close()
        except Exception:  # noqa: BLE001 — a test must not fail on cleanup
            pass


@pytest.fixture
def exits(monkeypatch) -> List[int]:
    """The exit codes the handler asked for, instead of the exit itself."""
    codes: List[int] = []
    monkeypatch.setattr(cli, "_exit_now", codes.append)
    return codes


@pytest.fixture
def quiet_stop(monkeypatch) -> None:
    """The stop sequence and the ledger sweep, as recorders.

    真的去停 dispatch、真的去清扫进程树，会把 pytest 自己的进程也卷进来。
    """
    monkeypatch.setattr(cli, "stop_all_live_dispatches", lambda budget: 0)
    monkeypatch.setattr(cli, "reap_plan", lambda *args, **kwargs: None)


# ---------------------------------------------------------------------------
# the scene is written before the process leaves
# ---------------------------------------------------------------------------


def test_sigterm_writes_signal_report_before_exit(
    monkeypatch, tmp_path, guard_factory, exits, quiet_stop
):
    """SIGTERM → 写下 signal=15 的现场，然后退出码 143。

    现场是信号这条路上唯一的证据：进程被 ``os._exit`` 带走，什么都不剩。
    现场里的 ``killed_by='signal'``、``signal=15``、``signal_name='SIGTERM'``，
    退出码仍然是没有的 —— 退出码是 143，不是信号。
    """
    plan_dir = _make_plan(tmp_path)
    guard_factory(plan_dir)

    cli._on_sigterm(signal.SIGTERM, None)

    # 退出码没有被我改写。
    assert exits == [143]

    scene = _read_scene(plan_dir)
    assert scene["killed_by"] == "signal"
    assert scene["signal"] == int(signal.SIGTERM)
    assert scene["signal_name"] == "SIGTERM"
    assert scene["exit_code"] is None
    assert scene["plan_id"] == "p1"


def test_sigint_writes_signal_report(
    monkeypatch, tmp_path, guard_factory, exits, quiet_stop
):
    """SIGINT → 写下 signal=2 的现场，然后退出码 130。

    SIGINT 和 SIGTERM 是两条路：现场里记的是 2，退出码是 130（``128 + 2``）。
    两处都不能写成对方的 —— 那会让事后的人以为死因记错了。
    """
    plan_dir = _make_plan(tmp_path)
    guard_factory(plan_dir)

    cli._on_sigint(signal.SIGINT, None)

    assert exits == [130]

    scene = _read_scene(plan_dir)
    assert scene["killed_by"] == "signal"
    assert scene["signal"] == int(signal.SIGINT)
    assert scene["signal_name"] == "SIGINT"
    assert scene["exit_code"] is None


def test_second_signal_keeps_one_report(
    monkeypatch, tmp_path, guard_factory, exits, quiet_stop
):
    """连发两个 SIGTERM → 只有一份现场。

    第二个信号走的是「立刻退出」的早退分支：停止序列已经跑过了，再写一份
    现场不是第二场事故，是同一场事故的副本。guard 自己也是一次性的。
    """
    plan_dir = _make_plan(tmp_path)
    guard_factory(plan_dir)

    cli._on_sigterm(signal.SIGTERM, None)
    cli._on_sigterm(signal.SIGTERM, None)

    assert len(_scene_files(plan_dir)) == 1
    assert exits == [143, 143]


def test_no_guard_is_a_noop(monkeypatch, tmp_path, exits, quiet_stop):
    """槽位是空的 → 什么都不写，退出码不变。

    测试和工具会直接调处理器，那时没有一趟运行，也就没人可写现场。
    这条路必须照旧退出，不能因为「没东西可写」就不退。
    """
    plan_dir = _make_plan(tmp_path)
    assert cli._crash_guard is None

    cli._on_sigterm(signal.SIGTERM, None)

    assert exits == [143]
    assert _scene_files(plan_dir) == []


# ---------------------------------------------------------------------------
# the edges
# ---------------------------------------------------------------------------


def test_a_broken_scene_does_not_stop_the_exit(
    monkeypatch, tmp_path, guard_factory, exits, quiet_stop
):
    """写现场自己抛异常 → 吞掉，退出码不变。

    现场是为了解释结局，不是为了改写结局。写现场这一路自己出了问题，
    也不能把进程本来要带的退出码换掉，更不能把 executor 挂在原地。
    """
    plan_dir = _make_plan(tmp_path)
    guard = guard_factory(plan_dir)

    def broken(self, signum):
        raise OSError("the disk is full")

    monkeypatch.setattr(cli.CrashGuard, "on_signal", broken)

    cli._on_sigterm(signal.SIGTERM, None)

    assert exits == [143]
    assert _scene_files(plan_dir) == []


def test_the_scene_is_written_after_the_cleanup(
    monkeypatch, tmp_path, guard_factory, exits
):
    """现场写在收尾之后、退出之前。

    收尾的顺序是那条命的：先停 dispatch，再清扫 ledger，最后释放锁 ——
    清扫要在释放之前，因为锁就是「这个计划正在被做」的唯一凭据。现场是
    旁证，不能插到这条链前面去，把「先收干净」挤成第二件事。
    """
    plan_dir = _make_plan(tmp_path)
    guard = guard_factory(plan_dir)

    order: List[str] = []

    monkeypatch.setattr(
        cli, "stop_all_live_dispatches", lambda budget: order.append("stop")
    )
    monkeypatch.setattr(cli, "reap_plan", lambda *args, **kwargs: order.append("reap"))
    monkeypatch.setattr(cli, "_executor_plan_dir", plan_dir)
    monkeypatch.setattr(cli, "_executor_plan_id", "p1")

    original = guard.on_signal

    def spy(signum):
        order.append("scene")
        return original(signum)

    monkeypatch.setattr(guard, "on_signal", spy)
    monkeypatch.setattr(cli, "_exit_now", lambda code: order.append("exit"))

    cli._on_sigterm(signal.SIGTERM, None)

    assert order == ["stop", "reap", "scene", "exit"]
