"""回放语料 —— 用真实命令证明「测量式判定」零假阳性、真阳性不漏。

背景（2026-11-04）
------------------
缺陷 A 第 5 条：判定器不许再靠解析命令字符串去猜「这棵树会不会被改」，
改成一件事：**测量**。测量要拿真实语料证明两句话：

* **不写的一条都不许判成写**（零假阳性）；
* **写的一条都不许漏**（真阳性不漏）。

语料在 ``backend/tests/fixtures/write_guard_replay/corpus.jsonl``（任务 5 冻结），
判定器是 ``backend/tree_snapshot.py`` 的 ``snapshot`` / ``diff``（任务 1）。
本文件把两者接起来：每条命令在**自己的**临时 git 仓库里跑一遍，
前后各拍一次快照，差出来的三个列表就是判定结果。

判定看两样东西：工作树与仓库元数据
------------------------------------
**工作树**：快照前后比一下，``created`` / ``modified`` / ``deleted`` 三个
列表都空，就是「工作树没动」。这是可观测事实，不需要从命令文本推断 ——
判定器里没有解析器。

**仓库元数据**：``git add`` / ``git commit`` 改的是索引与引用，工作树一个
字节没动，**可文件系统确实变了**（``.git/index``、``.git/refs/…``）——
语料把它们标成 ``write`` 是对的。``tree_snapshot`` 的快照**不含 .git/**
（它自己的文档里写着这一条），所以元数据这一面另用
``git status --porcelain=v2 --branch`` 的输出做指纹：分支 oid、分支名、
每个文件的索引与工作树状态都在里面。

两样都没动，才叫「没写」。

两种「两样都没动，但命令确实写了」的情况
----------------------------------------
它们**不算漏判**，但必须记下来（否则就是悄悄放过，语料就白攒了）：

``写落在树外``
    命令里出现了绝对路径（``/tmp/x``、``/repo/b.py``）或 ``~`` 开头。
    写护栏放行树外的写入（那是它的设计），快照也看不见树外 ——
    判「没写」是对的，不是漏。

``命令没走到它的写路径``
    命令自己失败了（源文件不存在、引号没闭合、git 版本不认识那个开关）。
    没跑到的写路径说明不了判定器的任何事。

两种情况都进 ``out_of_scope`` / ``unexercised``，**不进** ``missed``。

边界
----
* 每条命令在**独立的**临时 git 仓库里跑；执行的语义等价于
  ``subprocess.run(["bash", "-c", cmd], cwd=tmp, timeout=10)``。
* 单条超时记进 ``timed_out``，然后继续下一条 —— 不抛 ``TimeoutExpired``，
  也不因为一条卡住就把剩下的悄悄跳过。
* ``never_run=true`` 的条目（六条灾难命令）只做字符串匹配断言，不执行。
* 命令含树外路径（``/tmp/x``、``~``）→ 写落在树外的判「没写」是对的，
  不算漏判；只断言树内 diff。
* 语料里的命令会**真的**往 ``/tmp`` 写（上游明确不做文件系统隔离），
  所以回放会在这台机器的 ``/tmp`` 上留痕。
* 不做聚合墙钟断言：CI 以 ``-n 8`` 并行跑，聚合计时会随机红。
"""

from __future__ import annotations

import functools
import json
import os
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

# ``backend/`` on sys.path so ``import tree_snapshot`` works no matter
# which entry point runs the suite.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from tests.unit.test_write_guard_replay_corpus import load_corpus  # noqa: E402
from tree_snapshot import diff, snapshot  # noqa: E402
from verification_subagent import _HOOK_RULES  # noqa: E402

#: 单条命令的时间上限。语料里有 ``sleep 30`` 这种条目 —— 超了要记下来，
#: 不能把整轮回放拖死。
REPLAY_TIMEOUT_SEC = 10

#: 单次 git 调用的时间上限（``tree_snapshot`` 自己也有一个，这里是沙箱搭建用的）。
_GIT_TIMEOUT_SEC = 30

#: 子进程写文件的字节上限。语料里有一条 ``dd if=/dev/zero of=agent.py``
#: —— 没有 ``count=``，不设上限它会把磁盘写满。超限了 dd 收到 SIGXFSZ
#: 停下，文件照样写出来了，判定照常进行。
_MAX_FILE_SIZE_BYTES = 1 << 20

#: 沙箱里预先放好的文件。每一份都有理由：**让语料里的某条命令真的走到它的
#: 写路径**，而不是因为「文件不存在」提前失败。
_FIXTURE_FILES: Dict[str, str] = {
    # pytest / 解释器的缓存不进快照（.gitignore），否则每条命令都算成改了树。
    ".gitignore": ".pytest_cache/\n__pycache__/\n*.pyc\n",
    "a.py": "alpha\n",
    "agent.py": "agent\n",
    "out.txt": "out\n",
    "real.txt": "real\n",
    "server.py": "server\n",
    "x.py": "x\n",
    "b.py": "b\n",
    "tasks.json": "{}\n",
    "scheduler_view.png": "png\n",
    "tools/provider-resolver/delegate.sh": "#!/bin/sh\n",
}

#: 提交之后再改脏的文件。命令只有作用在**改脏的文件**上才改得了工作树：
#:   ``a.py``     —— ``git checkout -- a.py`` 只在文件被改过时才还原得出差异
#:   ``agent.py`` —— ``git add agent.py`` 只在文件被改过时才动得了索引
_DIRTY_FILES: Dict[str, str] = {
    "a.py": "alpha\n# dirty\n",
    "agent.py": "agent\n# dirty\n",
}

#: 未跟踪文件：让 ``git add -A`` 有东西可暂存（索引变了，工作树没变）。
_UNTRACKED_FILES: Dict[str, str] = {
    "scratch.txt": "untracked\n",
}

#: 已暂存、还没提交的改动：让 ``git commit`` 真能提交。没有它，git 会
#: 抱怨「nothing to commit」直接退出 —— 命令根本没走到它的写路径，
#: 语料上那条「写」就白攒了。
_STAGED_FILES: Dict[str, str] = {
    "staged.txt": "staged\n# staged\n",
}

#: 命令里出现的「树外路径」。绝对路径（``/tmp/x``、``/repo/b.py``）与
#: ``~`` 开头都算。
#:
#: 往前看的那一眼很重要：``https://example.com/a?b=c`` 里有两个 ``/``，
#: 但它们前面是 ``:`` 和 ``/``，不是路径的开始；``a/b`` 前面是字母，
#: 是相对路径 —— 都在树内。
_OUT_OF_TREE_RE = re.compile(r"(?<![\w:/~.-])(?:/[A-Za-z0-9._~+-]|~(?=[/\\]))")

#: 既有护栏里那六条「事前拦截」规则的名字。灾难命令走的是这一层 ——
#: 它们不含解析，是纯字符串匹配，误报率约等于 0。
_DISASTER_RULES = (
    "rm_root_block",
    "fork_bomb_block",
    "mkfs_block",
    "dd_zero_block",
    "force_push_main_block",
    "pkill_killall_block",
)


# ---------------------------------------------------------------------------
# 回放结果
# ---------------------------------------------------------------------------


@dataclass
class ReplayResult:
    """一次回放的结果。

    ``false_positives`` / ``missed`` 是**要为空**的两个列表；
    其余的列表是「为什么不是漏判」的账本 —— 账本是给人看的，
    判定只看前两个。
    """

    #: 判「不写」却判成了「写」的条目 —— 假阳性，必须为空。
    false_positives: List[Dict[str, Any]] = field(default_factory=list)
    #: 判「写」却判成了「不写」的条目 —— 漏判，必须为空。
    missed: List[Dict[str, Any]] = field(default_factory=list)
    #: 判「写」且判对了的条目。
    detected: List[Dict[str, Any]] = field(default_factory=list)
    #: 超时的条目：没跑完，判不了。
    timed_out: List[Dict[str, Any]] = field(default_factory=list)
    #: ``never_run=true``：按约定不执行。
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    #: 工作树与仓库元数据都没变，但写落在树外。
    out_of_scope: List[Dict[str, Any]] = field(default_factory=list)
    #: 两样都没变，且命令自己失败了 —— 没走到它的写路径。
    unexercised: List[Dict[str, Any]] = field(default_factory=list)
    #: 跑完并判过的条目数（超时与跳过的都不算）。
    checked: int = 0
    #: 判过的「不写」条目数。
    no_write_checked: int = 0
    #: 判过的「写」条目数。
    write_checked: int = 0

    def summary(self) -> Dict[str, Any]:
        """The four-number summary the plan asks for."""
        return {
            "false_positives": [item["command"] for item in self.false_positives],
            "missed": [item["command"] for item in self.missed],
            "timed_out": [item["command"] for item in self.timed_out],
            "checked": self.checked,
        }


@dataclass
class _Outcome:
    """一条命令跑完之后的三个事实：退出码、有没有超时。"""

    returncode: Optional[int]
    timed_out: bool = False


# ---------------------------------------------------------------------------
# 沙箱
# ---------------------------------------------------------------------------


def _git(repo: Path, args: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run ``git`` inside ``repo``."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SEC,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc


def _write_files(root: Path, files: Dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _build_template(root: Path) -> Path:
    """Build the repository template every sandbox is copied from.

    每条命令都要一个**自己的** git 仓库；但 ``git init`` + ``git add`` +
    ``git commit`` 跑 77 遍是纯开销 —— 模板造一次，复制 77 次。
    """
    repo = root / "template"
    repo.mkdir(parents=True)
    _git(repo, ["init", "-q"])
    _git(repo, ["config", "user.email", "replay@example.invalid"])
    _git(repo, ["config", "user.name", "replay"])
    _write_files(repo, _FIXTURE_FILES)
    _git(repo, ["add", "-A"])
    _git(repo, ["commit", "-qm", "replay fixture"])
    # 提交之后再改脏：见 _DIRTY_FILES 的注释。
    _write_files(repo, _DIRTY_FILES)
    # 未跟踪文件：见 _UNTRACKED_FILES 的注释。
    _write_files(repo, _UNTRACKED_FILES)
    # 已暂存待提交：见 _STAGED_FILES 的注释。
    _write_files(repo, _STAGED_FILES)
    _git(repo, ["add", "staged.txt"])
    return repo


@functools.lru_cache(maxsize=1)
def _real_git() -> Optional[Path]:
    """The real ``git`` binary, if it can be found.

    macOS 上的 ``/usr/bin/git`` 是 **xcrun 工具壳**（它链接的是
    ``libxcselect``）：壳要解析当前的开发者目录，顺手写一份自己的缓存 ——
    那份缓存比沙箱的文件大小上限还大，于是沙箱里每一条 git 命令都会在
    真正跑起来之前被 ``SIGXFSZ`` 打死（退出码 -25），一条都测不到。
    ``xcrun --find git`` 指向壳后面那个真的二进制。

    Linux 上没有这一层，``xcrun`` 不存在 —— 退回 PATH 上的 ``git``。
    """
    xcrun = shutil.which("xcrun")
    if xcrun:
        try:
            proc = subprocess.run(
                [xcrun, "--find", "git"],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SEC,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0:
            candidate = Path(proc.stdout.strip())
            if candidate.is_file():
                return candidate
    found = shutil.which("git")
    return Path(found) if found else None


def _child_env(sandbox_root: Path) -> Dict[str, str]:
    """The environment the replayed command runs in.

    四处刻意与「跑测试的那台机器」不同：

    * **git 的全局 / 系统配置被摘掉** —— 否则这台机器的 ``core.excludesFile``
      要是盖住了沙箱里的某个文件，快照就看不见它，判定跟着失真。
    * **``$OUT`` 被摘掉** —— 语料里有一条命令读它；不摘的话，``$OUT``
      指到哪儿全看环境，命令就会往树里写东西。
    * **代理指向一个没人听的端口** —— 回放不需要网络，``curl`` 立刻失败
      比挂满 10 秒好。
    * **PATH 最前面放的是真的 ``git``** —— 见 :func:`_real_git`：
      macOS 上 PATH 上的 ``git`` 是个会把自己写死的 xcrun 壳。
    """
    home = sandbox_root / "home"
    home.mkdir(exist_ok=True)
    xdg = sandbox_root / "xdg"
    xdg.mkdir(exist_ok=True)
    gitconfig = sandbox_root / "gitconfig"
    gitconfig.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(xdg)
    env["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_AUTHOR_NAME"] = "replay"
    env["GIT_AUTHOR_EMAIL"] = "replay@example.invalid"
    env["GIT_COMMITTER_NAME"] = "replay"
    env["GIT_COMMITTER_EMAIL"] = "replay@example.invalid"
    env.pop("OUT", None)
    for key in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
    ):
        env[key] = "http://127.0.0.1:9"

    # PATH 最前面放一份指向真二进制的 ``git``（见 _real_git）。
    real_git = _real_git()
    if real_git is not None:
        bindir = sandbox_root / "bin"
        bindir.mkdir(exist_ok=True)
        link = bindir / "git"
        try:
            if not link.exists():
                link.symlink_to(real_git)
            env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
        except OSError:
            pass
    return env


def _child_setup(pid_file: Path):
    """The child's own setup: a file-size ceiling and its pid.

    pid 是为了超时之后能把**整个进程组**收掉 —— ``subprocess.run`` 超时只
    杀直接子进程（bash），孙进程（``sleep`` / ``dd``）会被遗留下来。
    """

    def _setup() -> None:
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (_MAX_FILE_SIZE_BYTES, _MAX_FILE_SIZE_BYTES),
        )
        with open(pid_file, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))

    return _setup


def _kill_process_group(pid_file: Path) -> None:
    """Kill whatever is left of the timed-out command's process group."""
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _run_command(command: str, sandbox_root: Path, repo: Path, timeout: int) -> _Outcome:
    """Run one command in its own sandbox.

    The semantics are ``subprocess.run(["bash", "-c", cmd], cwd=tmp, timeout=10)``:
    a timeout is **recorded**, not raised, and the next entry still runs.
    """
    pid_file = sandbox_root / "child.pid"
    try:
        completed = subprocess.run(
            ["bash", "-c", command],
            cwd=str(repo),
            timeout=timeout,
            env=_child_env(sandbox_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            start_new_session=True,
            preexec_fn=_child_setup(pid_file),
        )
    except subprocess.TimeoutExpired:
        _kill_process_group(pid_file)
        return _Outcome(returncode=None, timed_out=True)
    return _Outcome(returncode=completed.returncode)


def _repo_state(repo: Path) -> str:
    """A fingerprint of the repository *metadata* (index + HEAD + branch).

    工作树快照**不含 .git/**（``tree_snapshot`` 的文档里写着这一条），
    所以「命令改了索引但没改工作树」这件事，只有在这里才看得见。

    一次 ``git status --porcelain=v2 --branch`` 就够：分支 oid、分支名、
    以及每个文件的索引状态与工作树状态都在里面。
    """
    return _git(repo, ["status", "--porcelain=v2", "--branch"], check=False).stdout


def _mentions_out_of_tree_path(command: str) -> bool:
    """True when the command names a path outside the measured tree."""
    return _OUT_OF_TREE_RE.search(command) is not None


def _run_rule(rule: str, payload: str) -> subprocess.CompletedProcess:
    """Run one interception rule the way the SDK would.

    规则是一条 ``python3 -c "…"`` 的单行命令，从 stdin 读 JSON payload，
    命中就 ``exit 2``。这里把 ``python3`` 换成**跑这个测试的解释器** ——
    规则要验的是它的字符串匹配，不是某个特定版本的 python。
    """
    argv = shlex.split(rule)
    if argv and argv[0] == "python3":
        argv[0] = sys.executable
    return subprocess.run(
        argv,
        input=payload,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SEC,
        check=False,
    )


# ---------------------------------------------------------------------------
# 回放
# ---------------------------------------------------------------------------


def replay(
    entries: Sequence[Dict[str, Any]],
    timeout: int = REPLAY_TIMEOUT_SEC,
) -> ReplayResult:
    """Replay ``entries`` and judge each one by measuring the tree.

    Args:
        entries: Corpus entries (``command`` / ``expect`` / ``never_run``).
        timeout: Per-command wall-clock ceiling, in seconds.

    Returns:
        A :class:`ReplayResult`.
    """
    result = ReplayResult()
    with tempfile.TemporaryDirectory(prefix="pdt-replay-") as tmp:
        template = _build_template(Path(tmp))
        for entry in entries:
            command = entry.get("command", "")
            expect = entry.get("expect")
            if entry.get("never_run"):
                result.skipped.append({"command": command, "reason": "never_run"})
                continue

            with tempfile.TemporaryDirectory(prefix="pdt-replay-case-") as case_tmp:
                case_root = Path(case_tmp)
                repo = case_root / "repo"
                shutil.copytree(template, repo)

                before = snapshot(repo)
                state_before = _repo_state(repo)
                outcome = _run_command(command, case_root, repo, timeout)
                if outcome.timed_out:
                    result.timed_out.append(
                        {"command": command, "expect": expect, "timeout": timeout}
                    )
                    continue
                after = snapshot(repo)
                report = diff(before, after)
                state_after = _repo_state(repo)

                tree_changed = bool(
                    report.created or report.modified or report.deleted
                )
                metadata_changed = state_after != state_before
                changed = tree_changed or metadata_changed
                result.checked += 1
                if expect == "no_write":
                    result.no_write_checked += 1
                    if changed:
                        result.false_positives.append(
                            {
                                "command": command,
                                "created": report.created,
                                "modified": report.modified,
                                "deleted": report.deleted,
                                "metadata_changed": metadata_changed,
                            }
                        )
                    continue

                result.write_checked += 1
                if changed:
                    result.detected.append(
                        {
                            "command": command,
                            "detected_via": "工作树" if tree_changed else "仓库元数据",
                            "created": report.created,
                            "modified": report.modified,
                            "deleted": report.deleted,
                        }
                    )
                    continue

                # 两样都没动 —— 找出「为什么没动」，好把账记清楚。
                if outcome.returncode != 0:
                    result.unexercised.append(
                        {"command": command, "returncode": outcome.returncode}
                    )
                elif _mentions_out_of_tree_path(command):
                    result.out_of_scope.append(
                        {"command": command, "reason": "写落在树外，快照看不见"}
                    )
                else:
                    result.missed.append({"command": command})
    return result


def _replay_by_expect(expect: str, timeout: int = REPLAY_TIMEOUT_SEC) -> ReplayResult:
    """Replay the corpus entries labelled ``expect``."""
    entries = [
        entry
        for entry in load_corpus()
        if entry.get("expect") == expect and not entry.get("never_run")
    ]
    return replay(entries, timeout=timeout)


# ---------------------------------------------------------------------------
# 验收
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def no_write_replay() -> ReplayResult:
    """Replay the corpus entries labelled ``no_write``."""
    return _replay_by_expect("no_write")


@pytest.fixture(scope="module")
def write_replay() -> ReplayResult:
    """Replay the corpus entries labelled ``write``."""
    return _replay_by_expect("write")


def test_no_false_positives_on_no_write_corpus(no_write_replay: ReplayResult) -> None:
    """不写的命令，一条都不许判成写。

    判据只有一条：**工作树与仓库元数据都没动**。语料里的「不写」命令是
    真实跑过的命令 —— 有的读文件、有的只做 dry-run、有的把输出丢进
    ``/dev/null``。
    """
    result = no_write_replay

    assert result.false_positives == [], (
        "这些命令不该改树，却改了树：\n"
        + json.dumps(result.false_positives, ensure_ascii=False, indent=2)
    )

    expected = [
        entry
        for entry in load_corpus()
        if entry.get("expect") == "no_write" and not entry.get("never_run")
    ]
    timed_out = [item for item in result.timed_out if item.get("expect") == "no_write"]
    assert result.no_write_checked + len(timed_out) == len(expected), (
        "有条「不写」的命令没被判到 —— 悄悄跳过等于没验："
        f"判了 {result.no_write_checked} 条，超时 {len(timed_out)} 条，"
        f"语料里 {len(expected)} 条"
    )


def test_every_write_command_is_detected(write_replay: ReplayResult) -> None:
    """写的命令，一条都不许漏。

    「漏」的定义：命令跑完了、工作树与仓库元数据都没动、而且没有任何别的
    解释（写落在树外 / 命令自己没走到写路径）。
    """
    result = write_replay

    assert result.missed == [], (
        "这些命令改了东西，却没被判出来：\n"
        + json.dumps(result.missed, ensure_ascii=False, indent=2)
    )

    expected = [
        entry
        for entry in load_corpus()
        if entry.get("expect") == "write" and not entry.get("never_run")
    ]
    timed_out = [item for item in result.timed_out if item.get("expect") == "write"]
    assert result.write_checked + len(timed_out) == len(expected), (
        "有条「写」的命令没被判到 —— 悄悄跳过等于没验："
        f"判了 {result.write_checked} 条，超时 {len(timed_out)} 条，"
        f"语料里 {len(expected)} 条"
    )
    assert (
        len(result.detected)
        + len(result.out_of_scope)
        + len(result.unexercised)
        + len(result.missed)
        == result.write_checked
    ), "每条「写」的命令都要有账可查：判成了写 / 落在树外 / 没走到写路径"

    # 仓库元数据这一面也要真的被走到 —— ``git add`` 改的是索引，工作树
    # 一个字节没动，这一面要是没走到，它就落进 missed 里了。
    assert any(item.get("detected_via") == "仓库元数据" for item in result.detected), (
        "没有一条命令是靠仓库元数据判出来的 —— 元数据这一面没被走到"
    )

    # 这三条 git 命令在任何机器上都会真的写：一条改工作树，两条改索引与引用。
    # 沙箱要是没真跑起来（比如 git 是个会把自己写死的壳），它们会齐刷刷
    # 变成 unexercised —— 那时候语料等于没验，所以这里钉死。
    detected_commands = {item["command"] for item in result.detected}
    for command in (
        "git checkout -- a.py",
        "git add agent.py",
        "git commit -n -m 'x'",
    ):
        assert command in detected_commands, f"这条命令没被判成「写」：{command!r}"


def test_disaster_commands_match_blocklist() -> None:
    """六条灾难命令要命中**既有**的拦截字符串。

    这六条走的是「事前拦截」那一层：纯字符串匹配，不含解析，误报率约等于 0。
    回放时它们一条都不许执行（``never_run=true``），所以这里只能拿规则本身
    去撞 —— 撞不上的话，这条防线就是空的。
    """
    rules = {rule["name"]: rule["command"] for rule in _HOOK_RULES}
    corpus = {entry["command"]: entry for entry in load_corpus()}

    disaster_commands = [
        entry["command"]
        for entry in load_corpus()
        if entry.get("never_run") is True
    ]
    assert len(disaster_commands) == 6, (
        f"灾难命令应当是六条，实际 {len(disaster_commands)} 条"
    )

    for command in disaster_commands:
        assert corpus[command]["never_run"] is True
        payload = json.dumps({"tool_input": {"command": command}})
        blocked_by = [
            name
            for name in _DISASTER_RULES
            if _run_rule(rules[name], payload).returncode == 2
        ]
        assert blocked_by, f"灾难命令没有被既有拦截规则命中：{command!r}"


def test_timeout_is_recorded_not_raised() -> None:
    """一条命令超时 → 记进 ``timed_out``，剩下的照跑。

    不抛 ``TimeoutExpired``，也不因为一条卡住就把整轮回放悄悄跳过。
    """
    entries = [
        {
            "command": "sleep 30",
            "expect": "no_write",
            "never_run": False,
            "source": "test",
        },
        {
            "command": "echo x > out.txt",
            "expect": "write",
            "never_run": False,
            "source": "test",
        },
        {
            "command": "echo hi",
            "expect": "no_write",
            "never_run": False,
            "source": "test",
        },
    ]

    result = replay(entries, timeout=REPLAY_TIMEOUT_SEC)

    assert [item["command"] for item in result.timed_out] == ["sleep 30"], (
        f"超时的条目没被记下来：{result.timed_out}"
    )
    # 超时之后，剩下的条目照跑照判。
    assert result.checked == 2, f"超时之后剩下的条目没跑完：{result.summary()}"
    assert result.false_positives == [], result.summary()
    assert result.missed == [], result.summary()
    assert result.detected, "超时之后的条目一条都没判成「写」"