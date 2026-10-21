"""stop 必须先 SIGTERM 给 grace，超时才 SIGKILL（核心契约 2 / 需求拆解 B）。

背景
----
``_kill_process_tree`` 在 stop / shutdown 路径上替操作者终止执行子进程，
但 grace 窗口是硬编码的 ``proc.wait(timeout=10)``：既没有配置入口（想缩短
grace 的部署只能改源码），也不告诉调用方「这次是不是走了 SIGKILL」——
调用方无从区分「子进程配合地退出了」和「我们不得不硬杀」。

这里钉住的是**信号顺序本身**：SIGTERM 先发、只等一个可配置的 grace，
grace 耗尽才升级 SIGKILL。测试不依赖真实进程 —— 假 proc 记录收到的
``wait(timeout=...)`` 入参和 ``kill()`` 次数，因此断言落在实际传给
``subprocess`` 的参数上，而不是函数内部的计数器上。

优先级：显式参数 > ``PDT_STOP_GRACE_SECONDS`` > 默认 10s。
非法值（非数字 / 负数）回退默认 —— 一个坏配置不能把 grace 变成负数
（等于跳过 SIGTERM 直接 SIGKILL），也不该在 shutdown 路径上抛异常。
"""

from __future__ import annotations

import math
import subprocess
from typing import List, Optional

import pytest

import server


class FakeProc:
    """``subprocess.Popen`` 的最小替身。

    只实现 ``_kill_process_tree`` 真正用到的那三个方法，并记录
    ``wait`` 收到的 timeout 和 ``kill`` 的次数。
    """

    def __init__(
        self,
        *,
        poll_results: Optional[List[Optional[int]]] = None,
        timeout_waits: int = 0,
        pid: int = 987654,
    ) -> None:
        self.pid = pid
        self.wait_timeouts: List[Optional[float]] = []
        self.kill_calls = 0
        self._poll_results = list(poll_results) if poll_results is not None else [None]
        self._timeout_waits = timeout_waits

    def poll(self):
        if len(self._poll_results) > 1:
            return self._poll_results.pop(0)
        return self._poll_results[0]

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if len(self.wait_timeouts) <= self._timeout_waits:
            raise subprocess.TimeoutExpired(cmd="fake-proc", timeout=timeout)
        return 0

    def kill(self):
        self.kill_calls += 1
        self._poll_results = [0]
        return 0


@pytest.fixture
def signals(monkeypatch):
    """把 killpg 换成记录器，返回收到的 ``(pgid, sig)`` 列表。"""
    sent = []
    monkeypatch.setattr(server.os, "getpgid", lambda pid: 4242)
    monkeypatch.setattr(server, "_signallable_pgid", lambda pgid: pgid)
    monkeypatch.setattr(server.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    return sent


@pytest.fixture(autouse=True)
def _no_ambient_grace(monkeypatch):
    """环境变量必须是测试自己设的，不能被跑测试的 shell 污染。"""
    monkeypatch.delenv("PDT_STOP_GRACE_SECONDS", raising=False)


def test_sigterm_then_wait_with_grace(signals):
    """SIGTERM 先发给进程组，``wait`` 收到的 grace 就是显式传入的值。"""
    proc = FakeProc()

    hard = server._kill_process_tree(proc, grace_seconds=2.0)

    assert hard is False
    assert proc.wait_timeouts == [2.0]
    assert proc.kill_calls == 0
    assert signals == [(4242, server.signal.SIGTERM)]


def test_kill_after_grace_timeout(signals):
    """grace 耗尽 → 升级 SIGKILL，并把「硬杀过」这件事报告给调用方。"""
    proc = FakeProc(timeout_waits=1)

    hard = server._kill_process_tree(proc, grace_seconds=2.0)

    assert hard is True
    assert proc.kill_calls == 1
    # 先等 grace，再在 SIGKILL 之后用固定的 5s 收尸。
    assert proc.wait_timeouts == [2.0, 5]
    # SIGTERM 仍然先发过 —— 契约是「先礼后兵」，不是「直接杀」。
    assert signals == [(4242, server.signal.SIGTERM)]


def test_clean_exit_returns_false(signals):
    """子进程在 grace 内退出 → 没有硬杀，返回 False。"""
    proc = FakeProc()

    hard = server._kill_process_tree(proc, grace_seconds=3.0)

    assert hard is False
    assert proc.kill_calls == 0
    assert proc.wait_timeouts == [3.0]


def test_env_override_grace(signals, monkeypatch):
    """不传参时读 ``PDT_STOP_GRACE_SECONDS``（部署侧的配置入口）。"""
    monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", "2")
    proc = FakeProc()

    hard = server._kill_process_tree(proc)

    assert hard is False
    assert proc.wait_timeouts == [2]


def test_already_exited_is_noop(signals):
    """进程早已退出 → 一个信号都不发，也不 wait。"""
    proc = FakeProc(poll_results=[0])

    hard = server._kill_process_tree(proc)

    assert hard is False
    assert proc.wait_timeouts == []
    assert proc.kill_calls == 0
    assert signals == []


def test_default_grace_when_nothing_configured(signals):
    """既不传参也没有环境变量 → 默认 10s（保持既有行为）。"""
    proc = FakeProc()

    server._kill_process_tree(proc)

    assert proc.wait_timeouts == [10.0]


def test_explicit_arg_beats_env(signals, monkeypatch):
    """优先级：显式参数 > 环境变量。"""
    monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", "30")
    proc = FakeProc()

    server._kill_process_tree(proc, grace_seconds=1.5)

    assert proc.wait_timeouts == [1.5]


@pytest.mark.parametrize(
    "bad",
    [-1, -0.5, "abc", "", math.nan],
    ids=["negative-int", "negative-float", "not-a-number", "empty", "nan"],
)
def test_invalid_grace_falls_back_to_default(signals, bad):
    """非数字 / 负数的 grace 回退默认 10 —— 坏配置不能变成「跳过 SIGTERM」。"""
    proc = FakeProc()

    server._kill_process_tree(proc, grace_seconds=bad)

    assert proc.wait_timeouts == [10.0]
    assert proc.kill_calls == 0


@pytest.mark.parametrize("bad", ["abc", "-4"], ids=["not-a-number", "negative"])
def test_invalid_env_grace_falls_back_to_default(signals, monkeypatch, bad):
    """环境变量里的坏值同样回退默认，而不是让 shutdown 崩掉。"""
    monkeypatch.setenv("PDT_STOP_GRACE_SECONDS", bad)
    proc = FakeProc()

    server._kill_process_tree(proc)

    assert proc.wait_timeouts == [10.0]


def test_zero_grace_is_honoured(signals):
    """0 不是负数：显式要求 0 就给 0（SIGTERM 后不再等）。"""
    proc = FakeProc()

    server._kill_process_tree(proc, grace_seconds=0)

    assert proc.wait_timeouts == [0.0]


@pytest.mark.parametrize(
    "exc", [ProcessLookupError, PermissionError, OSError], ids=["lookup", "perm", "oserror"]
)
def test_killpg_error_still_waits(signals, monkeypatch, exc):
    """killpg 抛错（进程刚死 / 权限不足）→ 吞掉并继续走 wait 流程。"""

    def _boom(pgid, sig):
        raise exc("gone")

    monkeypatch.setattr(server.os, "killpg", _boom)
    proc = FakeProc()

    hard = server._kill_process_tree(proc, grace_seconds=2.0)

    assert hard is False
    assert proc.wait_timeouts == [2.0]
    assert proc.kill_calls == 0


def test_hard_kill_reported_even_if_reap_fails(signals):
    """SIGKILL 后收尸再超时 → 仍然报告 True：调用方需要知道「被硬杀过」。"""
    proc = FakeProc(timeout_waits=2)

    hard = server._kill_process_tree(proc, grace_seconds=1.0)

    assert hard is True
    assert proc.kill_calls == 1
    assert proc.wait_timeouts == [1.0, 5]