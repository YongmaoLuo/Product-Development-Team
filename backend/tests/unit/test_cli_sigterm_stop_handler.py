"""executor 收到 SIGTERM 时：先把 claude 子进程收掉，再自己退出。

背景
----
``claude`` 子进程以 ``start_new_session=True`` 启动（``coding_tool.py`` 的
``_run_claude_interactive``），自成进程组：server 对 executor 进程组发的
SIGTERM **打不到它**。而 executor（``backend/cli.py``）此前没有任何信号
处理 —— 收到 SIGTERM 立刻死掉，``finally`` / ``cleanup()`` 都不跑，子进程
留成孤儿继续烧 token。

这里钉住的是 executor 自己的收尾顺序：

    收到 SIGTERM → 停掉所有活跃 dispatch（协作式，序列自身 ≤0.7×预算）
                → 等它结束（最多 0.9×预算）→ 退出码 143

以及四条不能破的边界：

* **幂等** —— ``install_stop_handler()`` 装两次不能叠成两层 handler；
* **第二次信号立刻退出** —— 停止过程中再来一个 SIGTERM 不重入序列，
  直接退：序列已经跑过一遍，再跑一遍只会把 executor 挂在原地；
* **预算有界** —— 环境变量是服务端给的那个 **grace 窗口**，执行器的
  预算是它的**一半**（``coding_tool.executor_stop_budget``）：缺失 /
  非法 / 非正 → grace 10s → 预算 5s；grace 低于 4s 先抬到 4s；预算
  夹在 [2, 60]；
* **序列抛异常也必须退出** —— 收尾路径崩了不能变成 executor 挂死。

测试不碰真实进程：``stop_all_live_dispatches`` 换成记录器，``_exit_now``
也换成记录器（真的退出就会把 pytest 一起带走）。
"""

from __future__ import annotations

import signal
import sys
import threading

import pytest

import cli


#: 环境变量里配置的**服务端 grace 窗口**（秒）。执行器的停止预算由它
#: 推导 —— 预算是 grace 的一半（``coding_tool.executor_stop_budget``），
#: 所以 6s 的 grace 给序列留 3s。
STOP_GRACE = "6"

#: 停止序列拿到的是「总预算」，由它自己按 0.35 / 0.25 / 0.10 切成三段。
#: 测试里出现的这个数字是**总预算**，不是某一段的等待时间。
BUDGET = 3.0


@pytest.fixture
def sigterm_slot():
    """把 SIGTERM 的槽位借给测试：装之前记下原来的，测完还原。

    ``install_stop_handler()`` 换掉的是进程级的槽位，不还原的话后面
    整个 pytest 进程都带着这个 handler 跑。
    """
    original = signal.getsignal(signal.SIGTERM)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, original)


def test_install_is_idempotent(monkeypatch, sigterm_slot):
    """装两次只装一次：槽位里还是那个函数本身，不是套了一层。"""
    monkeypatch.setattr(cli, "_stop_handler_installed", False)

    cli.install_stop_handler()
    after_first = signal.getsignal(signal.SIGTERM)
    cli.install_stop_handler()
    after_second = signal.getsignal(signal.SIGTERM)

    assert after_first is cli._on_sigterm
    assert after_second is cli._on_sigterm
    assert after_second is after_first


def test_handler_stops_dispatches_then_exits(monkeypatch):
    """SIGTERM → 停 dispatch（拿到 env 预算）→ 退出码 143。"""
    monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", STOP_GRACE)
    monkeypatch.setattr(cli, "_stop_in_progress", False)

    events = []

    def fake_stop(seconds):
        events.append(("stop", seconds))
        return 0

    monkeypatch.setattr(cli, "stop_all_live_dispatches", fake_stop)
    monkeypatch.setattr(cli, "_exit_now", lambda code: events.append(("exit", code)))

    cli._on_sigterm(signal.SIGTERM, None)

    assert events == [("stop", BUDGET), ("exit", 143)]


def test_budget_defaults_and_clamps(monkeypatch):
    """缺失 / 非法 / 非正 → grace 10.0 → 预算 5.0；预算夹在 [2, 60]。"""
    monkeypatch.delenv("PDT_STOP_GRACE_SECONDS", raising=False)
    assert cli._stop_grace_seconds() == 5.0

    for bad in ("abc", "0", "-5", "nan", "inf"):
        monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", bad)
        assert cli._stop_grace_seconds() == 5.0, bad

    # grace 3s 低于下限 4s → 先抬到 4s → 预算 2s。
    monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", "3")
    assert cli._stop_grace_seconds() == 2.0

    monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", "1")
    assert cli._stop_grace_seconds() == 2.0

    # grace 600s → 预算 300s → 被预算上限夹回 60s。
    monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", "600")
    assert cli._stop_grace_seconds() == 60.0


def test_second_signal_exits_immediately(monkeypatch):
    """停止过程中再来一个 SIGTERM：不重入序列，直接退出。"""
    monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", STOP_GRACE)
    monkeypatch.setattr(cli, "_stop_in_progress", False)

    stop_calls = []
    exit_calls = []
    sequence_started = threading.Event()
    release_sequence = threading.Event()

    def fake_stop(seconds):
        stop_calls.append(seconds)
        sequence_started.set()
        release_sequence.wait(timeout=5)
        return 0

    monkeypatch.setattr(cli, "stop_all_live_dispatches", fake_stop)
    monkeypatch.setattr(cli, "_exit_now", exit_calls.append)

    def send_second_signal():
        sequence_started.wait(timeout=5)
        cli._on_sigterm(signal.SIGTERM, None)
        release_sequence.set()

    sender = threading.Thread(target=send_second_signal, daemon=True)
    sender.start()

    cli._on_sigterm(signal.SIGTERM, None)
    sender.join(timeout=5)

    # 序列只跑了一遍 —— 第二个信号没有把它再叫起来。
    assert stop_calls == [BUDGET]
    # 两个信号各退出一次，退出码都是 143。
    assert exit_calls == [143, 143]


def test_stop_sequence_failure_still_exits(monkeypatch):
    """停止序列抛异常：记下来，然后照样退出 —— 绝不把 executor 挂住。"""
    monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", STOP_GRACE)
    monkeypatch.setattr(cli, "_stop_in_progress", False)

    exit_calls = []

    def boom(seconds):
        raise RuntimeError("stop sequence exploded")

    monkeypatch.setattr(cli, "stop_all_live_dispatches", boom)
    monkeypatch.setattr(cli, "_exit_now", exit_calls.append)

    cli._on_sigterm(signal.SIGTERM, None)

    assert exit_calls == [143]


def test_main_installs_handler_before_running(monkeypatch, sigterm_slot, tmp_path):
    """``main()`` 在跑 ``autonomous_coding`` 之前就把 handler 装上。

    装得太晚没有意义：executor 一开跑就会收到第一个 SIGTERM，而那时
    handler 还没装上的话，第一个信号就是裸的默认行为 —— 立刻死掉。
    """
    monkeypatch.setattr(cli, "_stop_handler_installed", False)
    monkeypatch.setattr(sys, "argv", ["autonomous-coding", "--recover", "-w", str(tmp_path)])

    observed = {}

    def fake_autonomous_coding(**kwargs):
        observed["handler"] = signal.getsignal(signal.SIGTERM)
        observed["called"] = True

    monkeypatch.setattr(cli, "autonomous_coding", fake_autonomous_coding)

    cli.main()

    assert observed["called"] is True
    assert observed["handler"] is cli._on_sigterm
