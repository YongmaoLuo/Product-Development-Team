"""协作式停止的真进程验证：忽略 SIGTERM 的 stub CLI 也必须被杀掉。

背景
----
``backend/tests/unit/test_cooperative_stop_sequence.py`` 与
``backend/tests/unit/test_cli_sigterm_stop_handler.py`` 都只用替身：假 process
记录 ``wait(timeout=...)``，假 ``kill_process_group`` 记录信号。它们钉住了
**顺序与预算**，却没有钉住**进程**这一层 —— 真信号有没有真的送到、真子进程
有没有真的死、真 pid 有没有残留。

这里补的就是那一层：真的起一个 executor 子进程（真 ``Popen``、真 SIGTERM、
真 SIGKILL 升级），让它持有一个**忽略 SIGTERM** 的 stub CLI，然后断言

* executor 以 143（128 + SIGTERM）退出；
* stub 的 pid 不再存在 —— 停止序列把它收掉了，没有留下孤儿。

为什么 stub 必须忽略 SIGTERM
----------------------------
``claude`` 子进程以 ``start_new_session=True`` 启动，自成进程组，**executor
收到的 SIGTERM 打不到它**；而且一个正在写 stdout 的 CLI 在管道断开之后往往
自己就走了 —— 那条路走通，不代表协作式停止真的能收掉一个不肯走的子进程。

所以 stub 用 ``sh -c 'trap "" TERM; exec sleep 300'``：SIGTERM 被明确忽略
（``trap ""`` 设成 ``SIG_IGN``，这个处置跨 ``exec`` 继承，所以被 ``exec`` 换
成的 ``sleep`` 同样无视 SIGTERM），只有 SIGKILL 能结束它。这正是停止序列
第三段存在的理由。

为什么子脚本写到 tmp_path 而不是仓库里
--------------------------------------
被驱动的那个进程需要一个**真 CLI**（真 pid、真进程组、真信号响应），但它不是
产品代码，不该进仓库：它是一次性的驱动脚本，写进 ``tmp_path``，跑完即弃。
仓库里留下的只有断言。

边界
----
* 预算是 ``PDT_STOP_GRACE_SECONDS=4``（server 的 grace 窗口），子进程通过
  环境变量继承它 —— 与生产路径同一条通道。
* 停止序列本身要 0.7×预算 ≈ 2.8s，handler 的硬上限是 0.9×预算 = 3.6s，
  所以整条链必须在那之前结束；两个用例加起来 < 30s。
* **前提先被验掉**：stub 必须真的活着，而且真的无视 SIGTERM（直接给它发一个
  SIGTERM，它还得站着）。这两条不是装饰 —— 一个从没起来过的 stub，或者一个
  会乖乖听 SIGTERM 的 stub，都会让「没有孤儿」这条断言变成空转：前者从一开始
  就没有 pid，后者会让停止序列停在第二段、永远走不到 SIGKILL 升级。
* 子进程用 ``register_child_process`` 登记，由 autouse 的
  ``clean_execution_state`` 在 teardown 统一回收；用例自己的 ``finally``
  另外杀掉残留 pid —— 登记是兜底，finally 是当场。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

#: ``backend/``，从本文件算出来而不是写死：仓库在每台机器上的路径不同，
#: 而子进程是以被测模块在 import path 上的方式起来的，这个值必须处处正确。
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: server 的 grace 窗口，通过环境变量交给子进程 —— 与生产路径同一条通道。
_BUDGET_SECONDS = 4

#: 父进程等子进程就绪 / 等它退出的上限。
_READY_TIMEOUT_SECONDS = 30.0
_EXIT_TIMEOUT_SECONDS = 20.0

#: stub CLI 的命令行：SIGTERM 被明确忽略，只有 SIGKILL 能结束它。
_STUB_COMMAND = 'trap "" TERM; exec sleep 300'

#: 子进程的源码。它是一次性的驱动脚本，写进 ``tmp_path``，不进仓库。
#:
#: 它做的是生产路径上真正发生的三件事：起一个自己进程组的 stub CLI、把
#: 拥有它的 tool 登记进活跃 dispatch 登记表、装上 executor 的 SIGTERM
#: handler；然后把 stub 的 pid 写给父进程 —— 那是父进程要看着消失的东西。
_CHILD_SOURCE = """\
# The executor child: a real process that owns a SIGTERM-ignoring stub.
#
# Run as: python executor_child.py <backend_dir> <ready_json> <stub_command>

import json
import os
import subprocess
import sys
import threading
import time

backend_dir = sys.argv[1]
ready_path = sys.argv[2]
stub_command = sys.argv[3]

sys.path.insert(0, backend_dir)

import cli
import coding_tool


class StubTool:
    # The two fields the stop path reaches for: the process and its lock.
    def __init__(self, process):
        self._current_process = process
        self._process_lock = threading.Lock()


def main():
    # The stub CLI. Its own session, so it leads its own process group --
    # the SIGTERM the executor receives does not reach it.
    stub = subprocess.Popen(
        ["/bin/sh", "-c", stub_command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Registered the way a live dispatch registers itself.
    coding_tool.register_live_dispatch(StubTool(stub))

    # The handler the executor installs before it starts working.
    cli.install_stop_handler()

    # Tell the parent which pid it has to find gone. Written through a
    # temporary name and renamed, so the parent never reads a half-written
    # file.
    tmp_path = ready_path + ".tmp"
    with open(tmp_path, "w") as handle:
        json.dump({"stub_pid": stub.pid, "child_pid": os.getpid()}, handle)
    os.replace(tmp_path, ready_path)

    # Wait for the stop. The handler runs on this thread.
    while True:
        time.sleep(3600)


main()
"""


def _tail(path, limit=4000) -> str:
    """The last ``limit`` characters of the child's log; "" when unreadable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def _kill_pid(pid) -> None:
    """SIGKILL one pid; a pid that is already gone is the wanted outcome."""
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_group(pid) -> None:
    """SIGKILL the process group led by ``pid`` (the stub leads its own).

    The leadership is checked first: a pid that was recycled into some
    other process is not ours to signal, and ``killpg`` on a group we do
    not own would take down a stranger.
    """
    try:
        if os.getpgid(pid) != pid:
            return
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _wait_until_gone(pid, timeout: float = 10.0) -> bool:
    """True once ``os.kill(pid, 0)`` says the pid names no process.

    Polled rather than checked once: a corpse the executor has not reaped
    yet still holds its table entry, so a single check could report an
    orphan that is really a zombie about to be collected.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # It exists and is not ours to signal -- not gone.
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _await_ready(ready_path: Path, log_path: Path, proc) -> dict:
    """Wait for the child to report its stub's pid; fail with its log."""
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(
                "the executor child exited (code {}) before it registered "
                "its stub:\n{}".format(proc.returncode, _tail(log_path))
            )
        if ready_path.exists():
            try:
                return json.loads(ready_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        time.sleep(0.05)
    pytest.fail(
        "the executor child never registered its stub within {}s:\n{}".format(
            _READY_TIMEOUT_SECONDS, _tail(log_path)
        )
    )


def _wait_for_exit(proc, log_path: Path) -> int:
    """Wait for the child to leave; fail with its log if it hangs."""
    try:
        return proc.wait(timeout=_EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        pytest.fail(
            "the executor child was still running {}s after SIGTERM:\n{}".format(
                _EXIT_TIMEOUT_SECONDS, _tail(log_path)
            )
        )


def _assert_stub_is_alive(stub_pid) -> None:
    """Fail unless the stub is running.

    A stub that was never there would make everything after it vacuous: the
    pid it reported would name no process from the start, and the "no
    orphan" assertion would pass without the stop sequence having done
    anything at all.
    """
    try:
        os.kill(stub_pid, 0)
    except ProcessLookupError:
        pytest.fail("the stub CLI is not running -- this test would prove nothing")
    except PermissionError:
        pytest.fail("the stub CLI is not ours to signal -- this test would prove nothing")


def _assert_stub_ignores_sigterm(stub_pid) -> None:
    """Fail unless the stub survives a direct SIGTERM.

    That is the premise of the whole suite: the stub has to be the
    stubborn kind. A stub that died on SIGTERM would let the stop sequence
    finish at stage 2, and this test would pass without ever reaching the
    SIGKILL escalation -- the very thing it exists to pin.
    """
    os.kill(stub_pid, signal.SIGTERM)
    time.sleep(0.2)
    try:
        os.kill(stub_pid, 0)
    except ProcessLookupError:
        pytest.fail(
            "the stub CLI did not ignore SIGTERM -- it is not the stubborn "
            "stub this suite needs"
        )


def _start_executor(tmp_path: Path, register_child_process):
    """Start the executor child; return ``(proc, info, log_path)``."""
    script = tmp_path / "executor_child.py"
    script.write_text(_CHILD_SOURCE, encoding="utf-8")
    ready_path = tmp_path / "ready.json"
    log_path = tmp_path / "executor.log"

    env = dict(os.environ)
    env["PDT_STOP_GRACE_SECONDS"] = str(_BUDGET_SECONDS)

    # The child's output goes to a file, not a pipe: a pipe nobody reads
    # fills up and blocks the child, and this child's output is evidence.
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(_BACKEND_DIR),
                str(ready_path),
                _STUB_COMMAND,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(_BACKEND_DIR),
        )

    # Registered for the teardown that reclaims a test's children -- the
    # backstop for a test that fails before it reaches its own cleanup.
    register_child_process(proc)

    info = _await_ready(ready_path, log_path, proc)
    _assert_stub_is_alive(info["stub_pid"])
    _assert_stub_ignores_sigterm(info["stub_pid"])
    return proc, info, log_path


def test_sigterm_terminates_stub_and_leaves_no_orphan(tmp_path, register_child_process):
    """SIGTERM → executor 以 143 退出，忽略 SIGTERM 的 stub 也被杀掉。

    这是「真进程 + 真信号 + 真 SIGKILL 升级」这一条链：stub 明确无视
    SIGTERM，所以只有停止序列的第三段（SIGKILL 整个进程组）能收掉它。
    收不掉就是留下一个还在烧 token 的孤儿 —— 那正是这套机制要防的事。
    """
    proc, info, log_path = _start_executor(tmp_path, register_child_process)
    stub_pid = info["stub_pid"]

    try:
        os.kill(proc.pid, signal.SIGTERM)

        exit_code = _wait_for_exit(proc, log_path)
        assert exit_code == 143, (
            "the executor must leave with 128 + SIGTERM (143); got {}\n{}".format(
                exit_code, _tail(log_path)
            )
        )

        assert _wait_until_gone(stub_pid), (
            "the stub CLI is still alive after the stop sequence -- it was "
            "left behind as an orphan (pid {})\n{}".format(
                stub_pid, _tail(log_path)
            )
        )

        # The check the acceptance note names: the pid no longer names a
        # process at all.
        with pytest.raises(ProcessLookupError):
            os.kill(stub_pid, 0)
    finally:
        # Whatever happened, nothing of this test's may stay behind.
        _kill_pid(proc.pid)
        _kill_group(stub_pid)


def test_stop_finishes_inside_the_budget(tmp_path, register_child_process):
    """整条停止链必须在预算（grace 窗口）之内结束。

    慢了的后果不是「慢一点」，而是 server 先动的手：grace 窗口走完就把
    executor 的进程组 SIGKILL 掉，``hard_killed`` 翻 True，一次可续跑的
    停机降级成整轮重跑。所以这条断言钉的是「预算之内」而不是「最终会
    退出」。
    """
    proc, info, log_path = _start_executor(tmp_path, register_child_process)
    stub_pid = info["stub_pid"]

    try:
        started = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        exit_code = _wait_for_exit(proc, log_path)
        elapsed = time.monotonic() - started

        assert exit_code == 143, (
            "the executor must leave with 128 + SIGTERM (143); got {}\n{}".format(
                exit_code, _tail(log_path)
            )
        )
        assert elapsed < _BUDGET_SECONDS + 5, (
            "the stop took {:.1f}s, past the {}s budget (+5s tolerance) -- the "
            "server would have SIGKILLed the group first\n{}".format(
                elapsed, _BUDGET_SECONDS, _tail(log_path)
            )
        )
    finally:
        _kill_pid(proc.pid)
        _kill_group(stub_pid)
