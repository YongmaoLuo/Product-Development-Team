"""The executor's entry point writes down the scene of a bad end.

背景
----
``cli.main()`` had nothing but a ``finally``.  An exception that was not
handled bubbled up to whoever spawned the executor and left no trace on
disk; a ``sys.exit(7)`` ended the process with a code and no
explanation.  Either way the next question -- "which task was running
when it died?" -- had no answer, because the answer had never been
written down.

What is under test
------------------
``main()`` now holds a ``CrashGuard`` for the length of the run and asks
it to write the scene on the three ways a run can end badly:

* an exception that was not handled -- the traceback is the evidence;
* ``sys.exit(n)`` with ``n`` not zero -- the exit code is the evidence;
* ``KeyboardInterrupt`` -- the signal is the evidence.

and three things it must **not** do:

* write anything on a clean run (``SystemExit(0)``, or a plain return);
* change the outcome it is reporting -- the exception is re-raised
  unchanged and the exit code is the one the caller asked for;
* add one byte to the run's own ``execution.log``.

The guard is a witness, not a narrator: it is there to say what
happened, never to say something *instead* of what happened.

测试不碰真实执行器：``autonomous_coding`` 换成抛出各种结局的桩，
计划目录是 tmp 下的临时目录。
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from typing import List

import pytest

import cli


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_plan(tmp_path: Path, name: str = "p1") -> Path:
    """A plan directory with a ``tasks.json`` in it.

    The plan directory is the parent of the tasks file -- that is the
    convention ``cli._executor_scope`` uses, and it is what gives the
    plan its id ("p1" here).
    """
    plan_dir = tmp_path / name
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text("[]\n", encoding="utf-8")
    return plan_dir


def _argv(project: Path, plan_dir: Path) -> List[str]:
    """The command line an executor is spawned with."""
    return [
        "cli.py",
        "--recover",
        "-w",
        str(project),
        "--tasks-file",
        str(plan_dir / "tasks.json"),
    ]


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

    ``main()`` leaves three things behind: the flag that says the stop
    handler is installed, the module's own guard slot, and the plan
    context in the environment (``PDT_PLAN_ID`` and ``PDT_PLANS_DIR``).
    None of them belongs to a test that has finished, and a test that
    leaves the stop handler claimed would silently disarm the next test
    that runs ``main()``.
    """
    monkeypatch.setattr(cli, "_stop_handler_installed", False)
    # ``raising=False``: the slot is part of the contract, but a test
    # that fails because the slot is missing says less than a test that
    # fails because the scene was not written.
    monkeypatch.setattr(cli, "_crash_guard", None, raising=False)
    # The plan context is restored, not merely removed: ``main()`` pins
    # both halves of it, and a test that removed ``PDT_PLANS_DIR`` for
    # its whole duration would send every resolver that reads it back
    # to the repository's live tree.
    for name in ("PDT_PLAN_ID", "PDT_PLANS_DIR"):
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PDT_CRASH_FORENSICS", raising=False)


# ---------------------------------------------------------------------------
# the three bad ends
# ---------------------------------------------------------------------------


def test_uncaught_exception_writes_report_and_reraises(monkeypatch, tmp_path):
    """未捕获异常 → 写下现场（traceback 是证据），异常原样抛出。

    异常必须原样冒泡：入口不是替调用者吞掉错误的边界，它只是在旁边
    多写一份现场。现场里 ``killed_by='exception'``，退出码和信号都是
    没有的 —— 一个自己抛出来的异常不是被信号杀死的，也不是一个退出码。
    """
    plan_dir = _make_plan(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(sys, "argv", _argv(project, plan_dir))

    def boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "autonomous_coding", boom)

    with pytest.raises(RuntimeError) as excinfo:
        cli.main()

    # 原样抛出：类型与消息都没有被改写。
    assert str(excinfo.value) == "boom"

    scene = _read_scene(plan_dir)
    assert scene["killed_by"] == "exception"
    assert scene["exit_code"] is None
    assert scene["signal"] is None
    assert scene["plan_id"] == "p1"
    assert "RuntimeError: boom" in scene["traceback"]


def test_sys_exit_seven_writes_exit_code_report(monkeypatch, tmp_path):
    """``sys.exit(7)`` → 写下现场（退出码是证据），退出码仍然是 7。

    入口改不了进程怎么退出：它只是把「为什么是 7」写下来。现场的
    ``killed_by='exit_code'``、``exit_code=7``，信号为空 —— 退出码不是
    信号。
    """
    plan_dir = _make_plan(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(sys, "argv", _argv(project, plan_dir))

    def exit_seven(**kwargs):
        sys.exit(7)

    monkeypatch.setattr(cli, "autonomous_coding", exit_seven)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    # 退出码没有被我改写。
    assert excinfo.value.code == 7

    scene = _read_scene(plan_dir)
    assert scene["killed_by"] == "exit_code"
    assert scene["exit_code"] == 7
    assert scene["signal"] is None
    assert scene["plan_id"] == "p1"


def test_keyboard_interrupt_writes_signal_report(monkeypatch, tmp_path):
    """``KeyboardInterrupt`` → 写下 signal=2 的现场，异常原样抛出。

    正常情况下 SIGINT 由停止处理器接管，走的是信号那条路；但处理器没
    装上（或者被别人换掉）的时候，进程会在这里死掉 —— 那时磁盘上什么
    都没有。这条路径必须自己写下现场。
    """
    plan_dir = _make_plan(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(sys, "argv", _argv(project, plan_dir))

    def interrupt(**kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "autonomous_coding", interrupt)

    with pytest.raises(KeyboardInterrupt):
        cli.main()

    scene = _read_scene(plan_dir)
    assert scene["killed_by"] == "signal"
    assert scene["signal"] == int(signal.SIGINT)
    assert scene["signal_name"] == "SIGINT"
    assert scene["exit_code"] is None
    assert scene["plan_id"] == "p1"


# ---------------------------------------------------------------------------
# the good ends
# ---------------------------------------------------------------------------


def test_clean_run_writes_nothing(monkeypatch, tmp_path):
    """正常跑完 → 磁盘上一个现场文件都没有。

    没有坏事发生的时候不该留下「好像出了事」的痕迹：日志目录里一个
    ``executor_crash_*`` 都不该有。
    """
    plan_dir = _make_plan(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(sys, "argv", _argv(project, plan_dir))

    seen = {}

    def stub(**kwargs):
        # 运行期间，槽位里放着的就是这一趟的 guard —— 信号处理器要靠
        # 它写现场（它没有 ``main()`` 的局部变量可用）。
        seen["guard"] = cli._crash_guard

    monkeypatch.setattr(cli, "autonomous_coding", stub)

    cli.main()

    assert _scene_files(plan_dir) == []
    # 运行期间槽位是满的，跑完了就空了：它属于这一趟运行，不属于下一趟。
    assert seen["guard"] is not None
    assert cli._crash_guard is None


def test_exit_zero_writes_nothing(monkeypatch, tmp_path):
    """``sys.exit(0)`` → 不写现场。

    退出码 0 是干净地走掉，不是出事。把它记成一次崩溃，会让「崩溃过
    几次」这个问题从此答错。
    """
    plan_dir = _make_plan(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(sys, "argv", _argv(project, plan_dir))

    def exit_zero(**kwargs):
        sys.exit(0)

    monkeypatch.setattr(cli, "autonomous_coding", exit_zero)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 0
    assert _scene_files(plan_dir) == []


def test_clean_run_log_bytes_unchanged(monkeypatch, tmp_path):
    """成功路径上 ``execution.log`` 的字节不因 forensics 而变。

    guard 是个见证，不是个旁白：它可以写现场，但不能往这次运行自己的
    日志里添一行。下面是同一趟运行跑两遍 —— 一遍把 forensics 关掉，
    一遍开着 —— 两次留下的 ``execution.log`` 必须是同一个文件。
    """
    lines = [
        '{"event": "task_started", "task_id": "1-1"}',
        '{"event": "task_finished", "task_id": "1-1"}',
    ]

    def run_once(plan_name: str, forensics: str) -> bytes:
        plan_dir = _make_plan(tmp_path, plan_name)
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)

        monkeypatch.setenv("PDT_CRASH_FORENSICS", forensics)
        monkeypatch.setattr(sys, "argv", _argv(project, plan_dir))

        def stub(**kwargs):
            # 这次运行自己的日志：固定内容，两遍才能逐字节比较。
            (plan_dir / "execution.log").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )

        monkeypatch.setattr(cli, "autonomous_coding", stub)
        cli.main()
        return (plan_dir / "execution.log").read_bytes()

    with_forensics_off = run_once("plan-off", "0")
    with_forensics_on = run_once("plan-on", "1")

    assert with_forensics_off == with_forensics_on
    assert with_forensics_on != b""


# ---------------------------------------------------------------------------
# the edges
# ---------------------------------------------------------------------------


def test_sys_exit_with_a_message_writes_exit_code_one(monkeypatch, tmp_path):
    """``sys.exit("boom")`` → 按 1 写现场。

    Python 自己也是这么解释这句话的：把消息打印出去，退出码 1。现场
    记的必须是同一个 1，而不是把那句话塞进 ``exit_code``。
    """
    plan_dir = _make_plan(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(sys, "argv", _argv(project, plan_dir))

    def exit_with_message(**kwargs):
        sys.exit("boom")

    monkeypatch.setattr(cli, "autonomous_coding", exit_with_message)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    # 进程怎么退出没有被我改写。
    assert excinfo.value.code == "boom"

    scene = _read_scene(plan_dir)
    assert scene["killed_by"] == "exit_code"
    assert scene["exit_code"] == 1


def test_a_failed_scene_does_not_change_the_outcome(monkeypatch, tmp_path):
    """现场写不出来 → 退出码不变，异常照旧。

    写现场是为了解释结局，不是为了改写结局。写现场这一路自己出了
    问题，也不能把进程本来要带的退出码换掉。
    """
    plan_dir = _make_plan(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(sys, "argv", _argv(project, plan_dir))

    def explode(**kwargs):
        sys.exit(7)

    def broken_scene(self, code):
        raise OSError("the disk is full")

    monkeypatch.setattr(cli, "autonomous_coding", explode)
    monkeypatch.setattr(cli.CrashGuard, "on_exit_code", broken_scene)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 7
    assert _scene_files(plan_dir) == []


def test_a_failed_close_still_releases_the_lock(monkeypatch, tmp_path):
    """``close()`` 抛异常 → 锁照样释放。

    锁是「这个计划正在被做」的唯一凭据。一个关不掉的 guard 不该把锁
    一起锁死 —— 下一趟执行器会以为自己撞上了别人，白等一场。
    """
    from executor_lock import acquire

    plan_dir = _make_plan(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(sys, "argv", _argv(project, plan_dir))
    monkeypatch.setattr(cli, "autonomous_coding", lambda **kwargs: None)

    def broken_close(self):
        raise OSError("the temp file is stuck")

    monkeypatch.setattr(cli.CrashGuard, "close", broken_close)

    cli.main()

    # 锁已经不在本进程手里了：再拿一次拿得到。
    again = acquire(plan_dir)
    assert again is not None
    again.release()
