"""复现结果的机械分类 —— 五分类闭集。

这个模块要解决什么
------------------
PRD 要求「在**干净 main** 上复现失败，才允许把失败判成框架问题」。复现协议把
同一条失败命令在临时 detached worktree 上跑两遍，留下两份 attempt；把这两份
attempt 归进哪一个类别，就是「这是不是框架问题」的唯一判定（架构决策点 3）。

分类必须是纯函数
----------------
不读文件、不发请求、不看时钟、不问模型。同一份材料在任何进程、任何机器上给出
同一个分类 —— 否则「这是不是框架缺陷」这个问题本身就不可复算，跨 plan 的全局
去重也无从谈起。

分类表
------

============================================  ======================
两次 attempt 的结果                             classification
============================================  ======================
2/2 以相同归一化首错行失败                      framework_deterministic
2/2 以同一超时签名超时                          framework_timeout
1/2 失败、1/2 通过                             flake
2/2 通过                                       not_reproducible
其它（输出截断 / 命令不可执行 / 证据不全）      unclassified
============================================  ======================

首错行先归一化再比较
--------------------
两次失败的首错行逐字不同，并不代表是两个缺陷：临时路径、node id、VP id、轮次、
计划号、时间戳都是 plan 局部量或运行噪声。归一化口径复用任务 2 的
``framework_fix.fingerprint.normalize_line`` —— 掩码不另造第二套，两处各写一遍
就会漂。

超时签名同样先归一化再比较
--------------------------
「两次都超时」不是「同一个超时」：一次挂在克隆、一次挂在测试，都是超时，但
不是同一个缺陷。所以超时也要比签名 —— 签名取该 attempt 的首错行（runner 从
``TimeoutExpired`` 的残余输出里取到），归一化口径与首错行**同一套**。

只有**两次都超时**且**超时签名全同**才是 ``framework_timeout``。超时与通过 /
失败混合（1/2 超时）走 ``flake`` —— 一次挂死不足以说明框架里挂着东西。

判定顺序
--------
顺序本身是契约的一部分，每一条都对应一个具体的现场：

1. **不足两次观察 -> ``unclassified``。** 协议跑两遍，2/2 的判据需要两次观察。
   一次观察撑不起任何一个 2/2 的结论 —— 一次通过不能叫「不可复现」，一次失败
   也不能叫「确定性失败」。
2. **有没跑起来的 -> ``unclassified``。** 命令不可执行、进程没起来 —— 证据不全，
   不判。**这条排在超时之前**：没跑起来的那一跑连「挂没挂死」都无从说起，拿它
   去参与「两次都超时」的比拼，是拿残缺的证据当证据。
3. **有超时 -> 比超时签名。** 两次都超时且签名全同 -> ``framework_timeout``；
   超时与通过 / 失败混合 -> ``flake``；两次都超时但签名不同 -> ``flake``。
4. **都通过 -> ``not_reproducible``。** 干净 main 上跑不出这个失败，问题在 plan
   分支或已经消失。
5. **一过一败 -> ``flake``。** 不稳定，不能算框架缺陷。
6. **都失败 -> 比归一化后的首错行。** 相同 -> ``framework_deterministic``；不同
   -> ``flake``；有一份失败没有可比的首错行（输出被截断）-> ``unclassified``。

复现协议的执行侧
----------------
分类是纯函数，但「把命令跑两遍」不是：它要建临时 worktree、起子进程、掐
超时、删目录。这些副作用集中在本模块下方的 :class:`ReproCoordinator` 里，
与上面的纯函数分开 —— **判定仍然是纯的，执行才是脏的**。

协调器做三件事：

1. 把请求登记进队列（进程内的字典，或 ``framework_fix_repro`` 表）。
2. 每次执行时在**失败所属的仓库**里用 ``git worktree add --detach`` 现场建
   一棵临时 worktree，在 ``origin/main`` 上把命令跑两遍，留下两份 attempt。
3. 把两次 attempt 交给 :func:`classify`，把结论与证据写回队列。

四条硬性约束：

* **绝不在原工作区跑。** 原工作区带着 plan 分支的改动；在那里跑出来的失败
  不能证明「干净 main 上也失败」。复现树必须由 git 现场建立，**绝不复用
  plan 自己的 worktree**。
* **基准只有一个。** 基准固定为失败所属仓库的 ``origin/main``。读不出来时
  结论是 ``unclassified`` 并留下证据 —— **不换另一个 ref 冒充干净 main**。
* **超时掐掉整棵进程树。** 只杀直接子进程的话，它 spawn 的孙进程会活下来，
  继续占着 CPU、内存和 SQLite 锁 —— 而调用方以为命令已经被杀了。
* **跑完删掉 worktree。** 临时目录不是垃圾桶；异常路径同样清理。

队列的并发契约：按仓串行 + 重启恢复
--------------------------------
复现的前提是「同一时刻这个仓库只有一条复现在跑」：两条复现同时跑在一个仓
上，各自看到的干净 main 可能已被对方搅过，判出来的「框架缺陷」是自己造
出来的。所以名额是**按仓库**分的：

* :meth:`ReproCoordinator.submit` 登记请求的同时就替它**占名额** —— 抢到
  的请求直接进入 ``running``，抢不到的停在 ``queued``（排队，等这个仓库的
  名额）。名额按仓库分，所以不同仓库的两个请求可以同时在飞。
* 名额的最终权威在数据库：``framework_fix_repro`` 上的部分唯一索引
  ``idx_framework_fix_repro_running`` 保证同一个仓库最多一行 ``running``。
  两个进程同时抢同一个仓库时，抢输的那个撞上唯一索引冲突 —— 它**退化成
  排队**，而不是把错误抛给调用方。
* 进程被杀掉时，跑了一半的请求会在库里留下一行 ``running``，那个仓库的名额
  就永远占着。:meth:`ReproCoordinator.recover_interrupted` 在重启后把残留的
  ``running`` 放回 ``queued`` 并写进审计链。

包形态
------
``backend/framework_fix/`` 是 PEP 420 的隐式命名空间包，与
``framework_fix.fingerprint`` 同处一室；本模块同样**没有** ``__init__.py``。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from bounded_subprocess import run_bounded
except ImportError:  # pragma: no cover
    from backend.bounded_subprocess import run_bounded  # type: ignore

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from framework.clock import utcnow_iso
except ImportError:  # pragma: no cover
    from backend.framework.clock import utcnow_iso  # type: ignore

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from framework.ids import validate_plan_id
except ImportError:  # pragma: no cover
    from backend.framework.ids import validate_plan_id  # type: ignore

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from framework_fix.fingerprint import compute_fingerprint, normalize_line
except ImportError:  # pragma: no cover
    from backend.framework_fix.fingerprint import (  # type: ignore
        compute_fingerprint,
        normalize_line,
    )

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from verification.tree_fingerprint import compute_tree_fingerprint
except ImportError:  # pragma: no cover
    from backend.verification.tree_fingerprint import (  # type: ignore
        compute_tree_fingerprint,
    )

__all__ = [
    "CLASSIFICATIONS",
    "DEFAULT_BASE_REF",
    "FLAKE",
    "FRAMEWORK_DETERMINISTIC",
    "FRAMEWORK_TIMEOUT",
    "MAX_ARGUMENT_LENGTH",
    "MAX_COMMAND_PARTS",
    "MAX_ERROR_CLASS_LENGTH",
    "MAX_ERROR_LINE_LENGTH",
    "MAX_FRAMEWORK_FILES",
    "MAX_FRAMEWORK_PATH_LENGTH",
    "MAX_REPO_LENGTH",
    "MAX_REQUEST_ID_LENGTH",
    "NOT_REPRODUCIBLE",
    "REQUIRED_ATTEMPTS",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "UNCLASSIFIED",
    "ReproAttempt",
    "ReproCoordinator",
    "ReproError",
    "ReproRequest",
    "ReproductionResult",
    "Runner",
    "build_git_argv",
    "classify",
    "default_runner",
    "latest_result",
]


# ---------------------------------------------------------------------------
# 五个分类词
# ---------------------------------------------------------------------------

#: 2/2 以相同归一化首错行失败 —— 干净 main 上稳定复现，是最该升级成修复 plan
#: 的一类。
FRAMEWORK_DETERMINISTIC = "framework_deterministic"

#: 超时 —— 两次都超时，且超时签名相同：挂死在框架里的同一个地方。
FRAMEWORK_TIMEOUT = "framework_timeout"

#: 不稳定 —— 一次通过一次失败，两次的首错行不同，或只有一次超时。它不是框架
#: 缺陷的证据。
FLAKE = "flake"

#: 两次都通过 —— 干净 main 上跑不出这个失败。
NOT_REPRODUCIBLE = "not_reproducible"

#: 归不了类 —— 证据不全或组合未知。**不是**「通过」，也不是「框架缺陷」。
UNCLASSIFIED = "unclassified"

#: 闭集：分类的取值只能落在这五个词里。面板要画它们、API 要发布它们，多出
#: 第六个词就是 bug，而不是一种新的处境。
CLASSIFICATIONS: FrozenSet[str] = frozenset(
    {
        FRAMEWORK_DETERMINISTIC,
        FRAMEWORK_TIMEOUT,
        FLAKE,
        NOT_REPRODUCIBLE,
        UNCLASSIFIED,
    }
)

#: 复现协议跑几遍。2/2 的判据需要两次观察。
REQUIRED_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# 一次 attempt
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReproAttempt:
    """一次复现尝试的结果。

    Attributes:
        run: 第几次运行 —— 协议跑两遍，所以是 1 与 2。
        exit_code: 进程退出码。``None`` 表示进程没有给出退出码：命令不可执行，
            或者进程被超时信号带走（后者由 ``timed_out`` 区分）。
        timed_out: 这一跑撞上了超时。
        first_error_line: 首错行原文。比较之前先归一化 —— 逐字不同的两行可能
            是同一个错误。``None`` 当作空行处理。
        duration_s: 耗时。它是证据的一部分，**不参与分类判定**：判定只认
            「通过 / 失败 / 超时」与首错行的身份。
    """

    run: int
    exit_code: Optional[int]
    timed_out: bool
    first_error_line: str
    duration_s: float


class _Outcome(str, Enum):
    """一次 attempt 的观测结果 —— 分类只认这四种。"""

    #: 撞上了超时。
    TIMEOUT = "timeout"
    #: 没有跑起来（命令不可执行 / 进程没起来），没有退出码可比。
    UNRUNNABLE = "unrunnable"
    #: 退出码 0 —— 通过。
    PASSED = "passed"
    #: 退出码非 0 —— 失败。
    FAILED = "failed"


def _outcome(attempt: ReproAttempt) -> _Outcome:
    """一次 attempt 的观测结果。

    超时先看：超时的进程通常没有退出码（被信号带走），若先看退出码，一次超时
    会被读成「没跑起来」，把 :data:`FRAMEWORK_TIMEOUT` 降级成
    :data:`UNCLASSIFIED`。
    """
    if attempt.timed_out:
        return _Outcome.TIMEOUT
    if attempt.exit_code is None:
        return _Outcome.UNRUNNABLE
    if attempt.exit_code == 0:
        return _Outcome.PASSED
    return _Outcome.FAILED


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------
def classify(attempts: Sequence[ReproAttempt]) -> str:
    """把复现尝试归进五分类闭集。

    Args:
        attempts: 复现协议留下的 attempt 序列 —— 协议跑两遍，所以通常是两个。

    Returns:
        五个词之一：``framework_deterministic`` / ``framework_timeout`` /
        ``flake`` / ``not_reproducible`` / ``unclassified``。

    这个函数**永不抛错**：输入不是它期望的形状（``None``、字符串、别的对象）
    也只是得到 ``unclassified``。分类是调度器每 tick 都要走的路，它崩掉会把一个
    判定问题变成一次事故。
    """
    try:
        return _classify(attempts)
    except Exception:
        return UNCLASSIFIED


def _classify(attempts: Sequence[ReproAttempt]) -> str:
    """分类本体。异常由 :func:`classify` 兜住。"""
    items: List[ReproAttempt] = list(attempts)

    # 1. 不足两次观察 -> 证据不足。2/2 的判据需要两次观察。
    if len(items) < REQUIRED_ATTEMPTS:
        return UNCLASSIFIED

    outcomes = [_outcome(attempt) for attempt in items]

    # 2. 有一跑没起来 -> 证据不全，不判。
    #    **排在超时之前**：没跑起来的那一跑连「挂没挂死」都无从说起，拿它去
    #    参与「两次都超时」的比拼，是拿残缺的证据当证据。
    if _Outcome.UNRUNNABLE in outcomes:
        return UNCLASSIFIED

    # 3. 有超时 -> 比超时签名。
    #    两次都超时**且签名全同** -> 框架里挂着东西；只要有一跑不是超时，超时
    #    就只是一半的观察 -> 不稳定。
    if _Outcome.TIMEOUT in outcomes:
        if not all(outcome is _Outcome.TIMEOUT for outcome in outcomes):
            # 1/2 超时（另一跑通过或失败）—— 一次挂死不足以说明框架缺陷。
            return FLAKE
        signatures = {
            normalize_line(attempt.first_error_line) for attempt in items
        }
        if len(signatures) == 1:
            # 同一个超时（签名取自首错行，归一化口径与失败分支同一套）。
            return FRAMEWORK_TIMEOUT
        # 两次都超时，但挂的不是同一个地方 —— 两个不同的挂法。
        return FLAKE

    # 4. 都通过 -> 干净 main 上跑不出这个失败。
    if all(outcome is _Outcome.PASSED for outcome in outcomes):
        return NOT_REPRODUCIBLE

    # 5. 一过一败 -> 不稳定。
    if any(outcome is _Outcome.PASSED for outcome in outcomes):
        return FLAKE

    # 6. 都失败：能不能算「同一个错误」，看归一化后的首错行。
    signatures = [
        normalize_line(attempt.first_error_line) for attempt in items
    ]
    if not all(signatures):
        # 有一份失败没有可比的首错行（输出被截断）—— 比不了就不比。
        return UNCLASSIFIED
    if len(set(signatures)) == 1:
        return FRAMEWORK_DETERMINISTIC
    return FLAKE


# ---------------------------------------------------------------------------
# 复现协议的执行侧
# ---------------------------------------------------------------------------

#: 复现的基准 ref。协议要求「干净 main」，所以基准是 origin/main —— 不是
#: 本地分支，也不是 HEAD（HEAD 会带着 plan 分支的提交）。
DEFAULT_BASE_REF = "origin/main"

#: 复现队列表。请求与结论都落在这里；表由生产迁移建，协调器**不建表**。
_REPRO_TABLE = "framework_fix_repro"

#: 队列状态。``queued`` -> ``running`` -> ``done`` / ``failed``。
#: ``running`` 是数据库唯一索引 ``idx_framework_fix_repro_running`` 认的那个
#: 值：同一个仓库同时只允许一条复现在跑。
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

#: ``queued`` 的别名 —— 同一个状态的另一个名字，不是第二个状态。
#:
#: 上游架构（决策点 3）给复现队列定的状态词是 ``queued | running |
#: finished``；任务简报里把这个「还没拿到名额、还在排队」的状态叫做
#: *pending*。这里给出别名，免得两处代码各写一个词 —— 队列的状态词表是
#: 闭集，多一个同义词就是多一个会漂移的地方。
STATUS_PENDING = STATUS_QUEUED

#: 单条 git 命令的时限。克隆一个大仓库要时间，但挂住的 git 不能挂住整个
#: 复现 —— 超时是「环境不对」的一种，不是「再等等」。
_GIT_TIMEOUT_SECONDS = 120.0

#: 连续多少轮「基准读不出来」之后不再自动重排队，改为升级 HITL。
#:
#: PRD 的口径是「连续 3 轮无法归类 -> 升级 HITL」。前两轮把请求放回排队（下一
#: 轮再试一次：远端可能只是暂时没有那个分支），第三轮还读不出来就不再重排队
#: —— 无限重排队等于没有人管这件事。
UNCLASSIFIED_ESCALATION_ROUNDS = 3

#: 证据里那条「基准读不出来」的标记。结论是 ``unclassified`` 时靠它把「读不
#: 出来」与「跑了两遍但归不了类」分开：前者要重新排队，后者是分类本身的结
#: 果。
EVIDENCE_BASE_REF_UNREADABLE = "base_ref_unreadable"

#: runner 的协议：给它一条命令、一个工作目录、一个时限，它回报一次 attempt。
#:
#: 返回值里的 ``run`` 字段**被协调器改写**（协调器知道这是第几次跑），其余
#: 字段原样采信。让 runner 自己数次数，等于让两份证据的编号由两个地方决定。
Runner = Callable[[Sequence[str], Path, Optional[float]], ReproAttempt]


class ReproError(RuntimeError):
    """复现协议跑不起来。

    继承 :class:`RuntimeError`：环境不对（仓库克隆不了、基准 ref 解析不了、
    状态库没跑迁移）是运行时的问题，不是调用方传错了参数。
    """


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------
#: 复现请求里每一项输入的上界。
#:
#: 上界不是「省点内存」，是**拒绝面**：请求的原料来自 plan，而 plan 不是
#: 长度可信的来源。没有上界的输入校验等于没有校验 —— 一个 10 MB 的仓库
#: 地址会把一次输入错误变成一次磁盘上的事故。
MAX_REPO_LENGTH = 512
MAX_REQUEST_ID_LENGTH = 256
MAX_COMMAND_PARTS = 128
MAX_ARGUMENT_LENGTH = 4096
MAX_FRAMEWORK_FILES = 64
MAX_FRAMEWORK_PATH_LENGTH = 512
MAX_ERROR_CLASS_LENGTH = 256
MAX_ERROR_LINE_LENGTH = 8192

#: 仓库地址允许的字符集。
#:
#: 仓库地址会作为**工作目录**（``-C <repo>``）交给 git。协议全程不经 shell，
#: 所以 ``;`` 在 argv 里不是命令分隔符 —— 但「一个能被安全转述进 shell 行的
#: 地址」是更强的性质，值得直接要求：只允许字母、数字与 ``. _ / @ : + - = %``。
#:
#: ``-`` 与 ``=`` 在里面是**有意的**：仓库名里本来就有 ``-``（``my-repo.git``），
#: 而 ``=`` 出现在 URL 的查询串里。地址**以** ``-`` 开头的那一类由
#: :func:`_validate_repo` 直接拒掉 —— 仓库地址是 ``-C`` 的值，git 在那里读的是
#: 一个工作目录，不是一个路径参数；复现树自己的路径参数（worktree 路径、基准
#: ref）由 ``--`` 分隔符挡在选项之外（见 :func:`build_git_argv`）。
_SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9._/@:+\-=%]+$")

#: 命令的**程序名**里不允许出现的字符。
#:
#: 参数里允许有元字符 —— 那正是 argv 的意义（``;`` 只是一个参数）。但程序名
#: 里出现 ``;`` 只会是拼错了。
_SHELL_METACHARS = frozenset(";|&$`<>(){}[]!*?\"'\\\n\r\t")


def _validate_repo(repo: Any) -> str:
    """仓库地址 —— 一条能被安全转述的字符串。

    Args:
        repo: 裸仓库路径或远端地址。

    Returns:
        校验通过的地址（原样返回）。

    Raises:
        ValueError: 不是非空字符串、超长、或含有 shell 元字符。
    """
    if not isinstance(repo, str):
        raise ValueError(f"repo must be a string; got {type(repo).__name__}")
    if not repo.strip():
        raise ValueError("repo must be a non-empty string")
    if len(repo) > MAX_REPO_LENGTH:
        raise ValueError(
            f"repo is {len(repo)} chars; the maximum allowed length is "
            f"{MAX_REPO_LENGTH}"
        )
    if repo.startswith("-"):
        raise ValueError(
            f"repo {repo!r} must not start with '-': git would read it as an "
            "option (--upload-pack=... is the shape of that attack)"
        )
    if not _SAFE_REPO_RE.fullmatch(repo):
        raise ValueError(
            f"repo {repo!r} contains characters that are not safe to hand to "
            "git: only letters, digits and . _ / @ : + - = % are allowed. A "
            "repo address must never carry shell metacharacters."
        )
    return repo


def _validate_command(command: Any) -> List[str]:
    """命令必须是一条 **argv 列表**。

    字符串不是命令，是**一段 shell 程序**：``"pytest -k x"`` 里的空格分隔
    词、``";"`` 起第二条命令。协议全程不经 shell，所以命令只能是列表。

    参数里**允许**有元字符 —— 那正是 argv 的意义（``"; touch x"`` 只是一个
    参数）。被拒的是「命令本身是字符串」和「程序名里有元字符」。

    Args:
        command: 命令的 argv。

    Returns:
        校验通过的 argv 列表。

    Raises:
        ValueError: 不是列表、是字符串、空、太长，或某个参数不是字符串。
    """
    if isinstance(command, str):
        raise ValueError(
            "command must be an argv list, not a shell string: a shell string "
            "lets ';' and '|' start a second command"
        )
    if command is None:
        raise ValueError("command must be an argv list, not None")
    if not isinstance(command, (list, tuple)):
        raise ValueError(
            f"command must be an argv list; got {type(command).__name__}"
        )
    if not command:
        raise ValueError("command must not be empty")
    if len(command) > MAX_COMMAND_PARTS:
        raise ValueError(
            f"command has {len(command)} parts; the maximum allowed is "
            f"{MAX_COMMAND_PARTS}"
        )
    parts: List[str] = []
    for index, part in enumerate(command):
        if not isinstance(part, str):
            raise ValueError(
                f"command[{index}] must be a string; got {type(part).__name__}"
            )
        if "\x00" in part:
            raise ValueError(f"command[{index}] must not contain a NUL byte")
        if len(part) > MAX_ARGUMENT_LENGTH:
            raise ValueError(
                f"command[{index}] is {len(part)} chars; the maximum allowed "
                f"length is {MAX_ARGUMENT_LENGTH}"
            )
        parts.append(part)
    program = parts[0]
    if not program.strip():
        raise ValueError("command[0] must not be empty")
    if any(char in _SHELL_METACHARS for char in program):
        raise ValueError(
            f"command[0] {program!r} contains shell metacharacters: the "
            "program to run must be a plain path"
        )
    return parts


def _validate_framework_files(files: Any) -> List[str]:
    """框架文件 —— 仓库**相对**路径。

    文件名以 ``-`` 开头是**允许**的：那是一个文件，不是一个选项（真要把它
    交给 git，:func:`build_git_argv` 会先放一个 ``--``）。被拒的是逃出仓库
    的路径（``..``）、绝对路径、NUL 与控制字符。

    Args:
        files: 文件路径列表；给一个字符串时按单个文件处理。

    Returns:
        校验通过的路径列表。

    Raises:
        ValueError: 不是列表、有非字符串项、空路径、超长、绝对路径，
            或者路径里有 ``..``。
    """
    if isinstance(files, str):
        files = [files]
    if files is None:
        files = []
    if not isinstance(files, (list, tuple)):
        raise ValueError(
            f"framework_files must be a list; got {type(files).__name__}"
        )
    if len(files) > MAX_FRAMEWORK_FILES:
        raise ValueError(
            f"framework_files has {len(files)} entries; the maximum allowed "
            f"is {MAX_FRAMEWORK_FILES}"
        )
    paths: List[str] = []
    for index, item in enumerate(files):
        if not isinstance(item, str):
            raise ValueError(
                f"framework_files[{index}] must be a string; got "
                f"{type(item).__name__}"
            )
        if not item.strip():
            raise ValueError(f"framework_files[{index}] must not be empty")
        if len(item) > MAX_FRAMEWORK_PATH_LENGTH:
            raise ValueError(
                f"framework_files[{index}] is {len(item)} chars; the maximum "
                f"allowed length is {MAX_FRAMEWORK_PATH_LENGTH}"
            )
        if "\x00" in item or any(char in item for char in "\n\r"):
            raise ValueError(
                f"framework_files[{index}] must not contain control characters"
            )
        if item.startswith("/") or (
            len(item) > 1 and item[1] == ":" and item[0].isalpha()
        ):
            raise ValueError(
                f"framework_files[{index}] must be a repo-relative path; "
                f"{item!r} is an absolute path"
            )
        segments = item.replace("\\", "/").split("/")
        if any(segment == ".." for segment in segments):
            raise ValueError(
                f"framework_files[{index}] must not escape the repository "
                f"root; {item!r} traverses upwards"
            )
        paths.append(item)
    return paths


def _validate_plan_id(plan_id: Any) -> str:
    """计划号 —— 走全仓统一的 :func:`validate_plan_id`。

    空字符串表示「不属于任何计划」，因此放行；非空时必须是合法的计划号
    （单个路径分量、没有 ``..``、字符集受限）。``InvalidPlanIdError`` 是
    ``ValueError`` 的子类，所以调用方只需要 catch 一种异常。

    Args:
        plan_id: 计划号。

    Returns:
        校验通过的计划号。

    Raises:
        ValueError: 计划号非法（``InvalidPlanIdError``）。
    """
    if plan_id is None:
        return ""
    if not isinstance(plan_id, str):
        raise ValueError(
            f"plan_id must be a string; got {type(plan_id).__name__}"
        )
    if not plan_id:
        return ""
    validate_plan_id(plan_id)
    return plan_id


def _validate_base_ref(base_ref: Any) -> str:
    """基准 ref —— 一个 git ref 名。

    ref 会进 ``git rev-parse`` / ``git checkout``。它不能以 ``-`` 开头
    （那会被读成选项），也不能带 shell 元字符。

    Args:
        base_ref: 基准 ref。

    Returns:
        校验通过的 ref。

    Raises:
        ValueError: 不是非空字符串、超长、以 ``-`` 开头，或含有非法字符。
    """
    if not isinstance(base_ref, str):
        raise ValueError(
            f"base_ref must be a string; got {type(base_ref).__name__}"
        )
    if not base_ref.strip():
        raise ValueError("base_ref must be a non-empty string")
    if len(base_ref) > MAX_REPO_LENGTH:
        raise ValueError(
            f"base_ref is {len(base_ref)} chars; the maximum allowed length "
            f"is {MAX_REPO_LENGTH}"
        )
    if base_ref.startswith("-"):
        raise ValueError(
            f"base_ref {base_ref!r} must not start with '-': git would read "
            "it as an option"
        )
    if not _SAFE_REPO_RE.fullmatch(base_ref):
        raise ValueError(
            f"base_ref {base_ref!r} contains characters that are not safe to "
            "hand to git"
        )
    return base_ref


def _validate_text(value: Any, name: str, limit: int) -> str:
    """一段有上界的自由文本。

    Args:
        value: 文本。
        name: 字段名（诊断里用）。
        limit: 上界。

    Returns:
        原样返回的文本。

    Raises:
        ValueError: 不是字符串，或超长。
    """
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string; got {type(value).__name__}")
    if len(value) > limit:
        raise ValueError(
            f"{name} is {len(value)} chars; the maximum allowed length is "
            f"{limit}"
        )
    return value


# ---------------------------------------------------------------------------
# 请求 / 结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReproRequest:
    """一条待复现的失败。

    Attributes:
        request_id: 请求号。它是队列的主键，也是 :meth:`ReproCoordinator.get`
            的入参。
        repo: 被复现的仓库 —— 失败发生的那棵仓库。协议在它里面建一棵临时的
            detached worktree（``git worktree add --detach``），所以它必须是一棵
            git 仓库，且带着失败发生那一刻的基准（``origin/main`` 在里面解析得
            出来）；它不必是 bare。
        command: 失败命令的 **argv 列表**，不是 shell 字符串。命令里带着
            plan 局部量（路径、参数），交给 shell 就多了一套要转义的语法，
            而且 ``;`` / ``|`` 会被执行 —— 那是命令注入，不是复现。
        framework_files: 失败所在 / 修复将要触碰的框架文件（repo 相对路径）。
        error_class: 异常类型或错误类别。
        first_error_line: 首错行（原文）。它是**期望**值：用来核对复现出来的
            是不是同一个错误。分类本身比较的是两次跑出来的首错行。
        timeout_s: 这一条命令的时限（秒）。``None`` 表示「用协调器的默认
            时限」。
        base_ref: 复现的基准 ref，默认 ``origin/main``。
        plan_id: 计划号。它进数据库的 ``plan_id`` 列；协议本身不需要它。
    """

    request_id: str
    repo: str
    command: List[str]
    framework_files: List[str]
    error_class: str
    first_error_line: str
    timeout_s: Optional[float] = None
    base_ref: str = DEFAULT_BASE_REF
    plan_id: str = ""

    def __post_init__(self) -> None:
        """把可变字段拷成一份自己的，并**校验**。

        请求是 frozen 的，但列表不是 —— 调用方手里那份列表改了，请求就跟着
        改了，而队列里登记的已经是另一条命令了。这里把内容**拷**进来，frozen
        才真的冻得住。

        校验也放在这一步，理由是**时机**：一条带着 shell 元字符的仓库地址、
        一条以字符串形态给出的命令、一个带 ``../`` 的计划号，在它被排进队列
        之前就该被拒掉。等到跑它的时候再拒，队列里已经躺着一个不该存在的
        东西了 —— 而队列是会落库的。

        Raises:
            ValueError: 任何一项输入不合法（见上面的各个 ``_validate_*``）。
        """
        object.__setattr__(self, "command", _validate_command(self.command))
        object.__setattr__(
            self, "framework_files", _validate_framework_files(
                self.framework_files
            )
        )
        object.__setattr__(self, "repo", _validate_repo(self.repo))
        object.__setattr__(
            self,
            "request_id",
            _validate_text(
                self.request_id, "request_id", MAX_REQUEST_ID_LENGTH
            ),
        )
        object.__setattr__(
            self,
            "error_class",
            _validate_text(
                self.error_class, "error_class", MAX_ERROR_CLASS_LENGTH
            ),
        )
        object.__setattr__(
            self,
            "first_error_line",
            _validate_text(
                self.first_error_line, "first_error_line", MAX_ERROR_LINE_LENGTH
            ),
        )
        object.__setattr__(self, "plan_id", _validate_plan_id(self.plan_id))
        object.__setattr__(
            self, "base_ref", _validate_base_ref(self.base_ref)
        )

    def to_dict(self) -> Dict[str, Any]:
        """这个请求的 JSON 形态 —— 进数据库的 ``request_json`` 列。"""
        return {
            "request_id": self.request_id,
            "repo": self.repo,
            "command": list(self.command),
            "framework_files": list(self.framework_files),
            "error_class": self.error_class,
            "first_error_line": self.first_error_line,
            "timeout_s": self.timeout_s,
            "base_ref": self.base_ref,
            "plan_id": self.plan_id,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ReproRequest":
        """从 JSON 形态还原一个请求 —— 队列从库里读回自己时用。"""
        return cls(
            request_id=str(payload.get("request_id", "")),
            repo=str(payload.get("repo", "")),
            command=list(payload.get("command") or []),
            framework_files=list(payload.get("framework_files") or []),
            error_class=str(payload.get("error_class", "")),
            first_error_line=str(payload.get("first_error_line", "")),
            timeout_s=payload.get("timeout_s"),
            base_ref=str(payload.get("base_ref", DEFAULT_BASE_REF)),
            plan_id=str(payload.get("plan_id", "")),
        )


@dataclass(frozen=True)
class ReproductionResult:
    """一次复现的结论与证据。

    Attributes:
        classification: 五分类闭集里的一个词（见 :func:`classify`）。
        attempts: 两次 attempt。**两次都在** —— 一次观察撑不起 2/2 的判据。
        clean_commit: 复现时 HEAD 所在的 commit（``origin/main`` 的 sha）。
        tree_fingerprint: 复现时那棵树的指纹。它回答「这份结论是在哪份代码
            上得出来的」。
        evidence: 结论背后的**原始材料**。基准读不出来时，证据里是请求的
            base_ref、仓库路径与 git 的原始输出 —— 「读不出来」是一个事实，
            它得留下出处。老行没有这个键时读回来是空字典。
    """

    classification: str
    attempts: Tuple[ReproAttempt, ...]
    clean_commit: str
    tree_fingerprint: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """这个结论的 JSON 形态 —— 进数据库的 ``result_json`` 列。"""
        return {
            "classification": self.classification,
            "attempts": [
                {
                    "run": attempt.run,
                    "exit_code": attempt.exit_code,
                    "timed_out": attempt.timed_out,
                    "first_error_line": attempt.first_error_line,
                    "duration_s": attempt.duration_s,
                }
                for attempt in self.attempts
            ],
            "clean_commit": self.clean_commit,
            "tree_fingerprint": self.tree_fingerprint,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ReproductionResult":
        """从 JSON 形态还原一个结论 —— 队列从库里读回自己时用。"""
        attempts: List[ReproAttempt] = []
        for index, item in enumerate(payload.get("attempts") or [], start=1):
            attempts.append(
                ReproAttempt(
                    run=int(item.get("run", index)),
                    exit_code=item.get("exit_code"),
                    timed_out=bool(item.get("timed_out", False)),
                    first_error_line=str(item.get("first_error_line", "")),
                    duration_s=float(item.get("duration_s", 0.0)),
                )
            )
        evidence = payload.get("evidence")
        return cls(
            classification=str(payload.get("classification", UNCLASSIFIED)),
            attempts=tuple(attempts),
            clean_commit=str(payload.get("clean_commit", "")),
            tree_fingerprint=str(payload.get("tree_fingerprint", "")),
            evidence=dict(evidence) if isinstance(evidence, dict) else {},
        )


# ---------------------------------------------------------------------------
# 最近一次结论
# ---------------------------------------------------------------------------
def latest_result(
    fingerprint: str,
    *,
    db_path: Optional[Path] = None,
) -> Optional[ReproductionResult]:
    """这个签名最近一次复现的结论 —— 没有就是 ``None``。

    判定入口靠它回答「这次失败复现出来是什么」。复现协议把结论写进
    ``framework_fix_repro`` 的 ``result_json``，``failure_ref`` 是那次复现
    对应的缺陷指纹。

    Args:
        fingerprint: 缺陷指纹。
        db_path: 状态库文件。``None`` 表示用生产那本库。

    Returns:
        最近一次复现的结论；没跑过复现、结论解不开、或者库读不到，都是
        ``None`` —— 「没有结论」与「结论是某一种」是两件事，判定要分得开：
        前者本轮判不了（``needs_repro``），后者才有得判。
    """
    reference = str(fingerprint or "").strip()
    if not reference:
        return None
    path = Path(db_path) if db_path is not None else _default_state_db_path()
    if path is None or not path.exists():
        return None
    try:
        from state_machine.db.connection import open as open_db

        conn = open_db(path)
    except Exception:  # noqa: BLE001 - 读不到就是「没有结论」
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT result_json FROM framework_fix_repro "
            "WHERE failure_ref = ? AND result_json IS NOT NULL "
            "ORDER BY queued_at DESC, repro_id DESC LIMIT 1",
            (reference,),
        ).fetchone()
    except sqlite3.Error:
        # 库没跑过迁移（表不在）或读不出来 —— 都是「没有结论」。
        return None
    finally:
        conn.close()
    if row is None or not row["result_json"]:
        return None
    try:
        payload = json.loads(row["result_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or "classification" not in payload:
        return None
    return ReproductionResult.from_dict(payload)


def _default_state_db_path() -> Optional[Path]:
    """生产那本状态库 —— 解析不出来就是 ``None``。"""
    try:
        from config_paths import resolve_state_db_path
    except ImportError:  # pragma: no cover
        try:
            from backend.config_paths import resolve_state_db_path  # type: ignore
        except ImportError:
            return None
    try:
        return Path(resolve_state_db_path())
    except Exception:  # noqa: BLE001 - 解析不出来就是「没有结论」
        return None


# ---------------------------------------------------------------------------
# 首错行
# ---------------------------------------------------------------------------
def _first_error_line(stderr: Optional[str], stdout: Optional[str]) -> str:
    """从一次运行的输出里取首错行。

    口径：**stderr 优先；取最后一条不在缩进里的非空行**。

    为什么不是「最后一行」：Python 的 traceback 最后两行是

    ::

        File "/tmp/x.py", line 1, in <module>
            raise AssertionError("boom")
        AssertionError: boom

    真正说明错误的是最后那条 ``AssertionError: boom``；traceback 的中间那些
    帧行是缩进的，路径还带着临时目录 —— 逐字比较两次跑的结果时，那一行既
    不是错误本身，也会把两次跑区分开。

    为什么不是「第一条含 Error 的行」：错误类别的前缀在不同运行时里不统一
    （``AssertionError`` / ``assert 1 == 2`` / ``Error: ...``），按前缀找要
    维护一张前缀表，而那张表一定会漂。

    Args:
        stderr: 进程的标准错误。
        stdout: 进程的标准输出。命令把错误写在 stdout 上时（不少脚本这么
            干），stderr 是空的 —— 所以它排在第二个被看。

    Returns:
        首错行；没有输出时是空字符串。空行是「没有可比的首错行」这个事实，
        不该被编造出来。
    """
    for stream in (stderr, stdout):
        lines = [line for line in (stream or "").splitlines() if line.strip()]
        if not lines:
            continue
        for line in reversed(lines):
            if not line[:1].isspace():
                return line.strip()
        return lines[-1].strip()
    return ""


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
def default_runner() -> Runner:
    """真实的 runner：在给定目录里跑一次命令，回报一次 attempt。

    协议的三条要求落在这一个函数里：

    * **argv 列表，不经 shell。** ``shell=False``：命令里的 ``;`` / ``|`` 是
      参数，不是第二条命令。
    * **超时掐掉整棵进程树。** 交给 :func:`bounded_subprocess.run_bounded` ——
      它用 ``start_new_session=True`` 起子进程，超时时 ``killpg`` 整个进程
      组。只杀直接子进程的话，它 spawn 的孙进程会活下来，而调用方以为命令
      已经被杀了。
    * **跑不起来也是一份证据。** 命令不存在（``OSError``）时回报
      ``exit_code=None``，分类因此是 ``unclassified`` 而不是「通过」。

    Returns:
        一个 runner 可调用对象：``(command, cwd, timeout_s) -> ReproAttempt``。
        返回的 attempt 里 ``run`` 字段是 0 —— 次数由协调器编号。
    """

    def _run(
        command: Sequence[str],
        cwd: Path,
        timeout_s: Optional[float],
    ) -> ReproAttempt:
        argv = [str(part) for part in command]
        started = time.monotonic()
        try:
            completed = run_bounded(
                argv,
                cwd=str(cwd),
                timeout=timeout_s,
                shell=False,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            return ReproAttempt(
                run=0,
                exit_code=None,
                timed_out=True,
                first_error_line=_first_error_line(exc.stderr, exc.stdout),
                duration_s=time.monotonic() - started,
            )
        except OSError as exc:
            return ReproAttempt(
                run=0,
                exit_code=None,
                timed_out=False,
                first_error_line=str(exc),
                duration_s=time.monotonic() - started,
            )
        return ReproAttempt(
            run=0,
            exit_code=completed.returncode,
            timed_out=False,
            first_error_line=_first_error_line(
                completed.stderr, completed.stdout
            ),
            duration_s=time.monotonic() - started,
        )

    return _run


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
def _git_env() -> Dict[str, str]:
    """git 命令跑在什么环境里。

    三条设置，每一条都有理由：

    * ``GIT_TERMINAL_PROMPT=0`` —— 远端要密码时**失败**，而不是挂在那里等
      一个没人在的提示。复现协议是无人值守的。
    * ``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM`` 指向 ``/dev/null`` ——
      全局配置里的 ``init.templateDir`` 会把模板 hooks 复制进新建的仓库，
      ``core.hooksPath`` 会让 ``post-checkout`` hook 在检出时跑起来。一个
      开发者本机上的模板不该改变「干净 main 上会发生什么」这个问题的答案。
    * 其余环境原样继承 —— 远端地址、代理、凭据助手都还在。
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def build_git_argv(
    args: Sequence[str],
    paths: Sequence[str] = (),
    cwd: Optional[Path] = None,
) -> List[str]:
    """一条 git 命令的 argv —— **路径参数前面一定有 ``--``**。

    为什么这一条是硬要求：git 把以 ``-`` 开头的参数读成**选项**。一个叫
    ``-x`` 的文件因此变成 ``git checkout -x``（一个选项，不是一个文件），
    一个叫 ``--upload-pack=...`` 的仓库地址因此变成一条要让远端执行的
    命令。``--`` 是 git 自己给的「选项到此为止」的记号，路径必须待在它
    后面。

    这条分隔符是**唯一**能把路径和选项分开的东西，所以所有带路径的 git
    调用都必须走这个函数 —— 少写一次 ``--`` 就是一次选项注入。

    Args:
        args: git 的参数（不含 ``git`` 本身）。
        paths: 路径参数（仓库地址、目录、文件名）。它们会被放到 ``--`` 之后。
        cwd: 在哪个仓库里跑（``-C``）。

    Returns:
        完整的 argv 列表。
    """
    argv = ["git", "-c", "safe.directory=*"]
    if cwd is not None:
        argv += ["-C", str(cwd)]
    argv += [str(arg) for arg in args]
    if paths:
        argv.append("--")
        argv += [str(path) for path in paths]
    return argv


def _git(
    args: Sequence[str],
    cwd: Optional[Path] = None,
    timeout: float = _GIT_TIMEOUT_SECONDS,
    check: bool = True,
    paths: Sequence[str] = (),
) -> subprocess.CompletedProcess:
    """跑一条 git 命令。

    ``shell=False`` 配 argv 列表：参数里有 ``HEAD^{tree}`` 这种带花括号的
    记号，交给 shell 就多了一套要转义的语法。

    Args:
        args: git 的参数（不含 ``git`` 本身）。
        cwd: 在哪个仓库里跑。
        timeout: 时限。
        check: 非零退出码时抛错吗。
        paths: 路径参数。它们会被放到 ``--`` 之后（见
            :func:`build_git_argv`）。

    Returns:
        跑完的 :class:`subprocess.CompletedProcess`。

    Raises:
        ReproError: ``check`` 为真且 git 非零退出。stderr 一起带上 ——
            「git 失败了」这句话本身什么也没说。
    """
    command = build_git_argv(args, paths, cwd)
    completed = run_bounded(
        command,
        shell=False,
        text=True,
        timeout=timeout,
        env=_git_env(),
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ReproError(
            f"git {' '.join(str(arg) for arg in args)} failed"
            + (f" in {cwd}" if cwd is not None else "")
            + f" (exit {completed.returncode})"
            + (f": {detail}" if detail else "")
        )
    return completed


def _git_paths(
    args: Sequence[str],
    paths: Sequence[str],
    cwd: Optional[Path] = None,
    timeout: float = _GIT_TIMEOUT_SECONDS,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """跑一条**带路径参数**的 git 命令。

    路径参数在 ``--`` 之后 —— 见 :func:`build_git_argv` 里那条「文件名以
    ``-`` 开头就不该变成选项」的道理。
    """
    return _git(
        args, cwd=cwd, timeout=timeout, check=check, paths=paths
    )


def _git_output(cwd: Path, *args: str) -> str:
    """跑一条 git 命令并返回它的 stdout（去掉了尾部换行）。"""
    return (_git(args, cwd=cwd).stdout or "").strip()


def _tree_fingerprint(worktree: Path) -> str:
    """复现时那棵树的指纹。

    算不出来时给空字符串而不是抛错：指纹回答的是「这份结论是在哪份代码上
    得出来的」，而**分类**才是结论本身。一棵树算不出指纹（git 坏了、目录不
    是仓库）不该把已经拿到的分类一起带走。
    """
    try:
        return compute_tree_fingerprint(worktree)
    except (ValueError, RuntimeError):
        return ""


def _json_dumps(payload: Any) -> str:
    """存进数据库的 JSON。键排序 —— 同一份材料存两次要长得一样。"""
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _failure_ref(request: ReproRequest) -> Optional[str]:
    """这条请求复现的是哪个缺陷。

    指纹算不出来（材料不全）时给 ``None`` 而不是抛错：指纹是**登记用的
    索引**，不是复现的前提 —— 复现要的是那条命令，不是那个键。
    """
    try:
        return compute_fingerprint(
            framework_files=request.framework_files,
            error_class=request.error_class,
            first_error_line=request.first_error_line,
        )
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 协调器
# ---------------------------------------------------------------------------
class ReproCoordinator:
    """复现协调器 —— 把一条失败请求变成一个分类结论。

    Examples:
        >>> coordinator = ReproCoordinator(worktree_root=tmp)  # doctest: +SKIP
        >>> coordinator.submit(request)                         # doctest: +SKIP
        'r1'
        >>> coordinator.run_pending()                            # doctest: +SKIP
        1
        >>> coordinator.get('r1')['classification']              # doctest: +SKIP
        'framework_deterministic'
    """

    def __init__(
        self,
        db_path: Any = None,
        worktree_root: Any = None,
        runner: Optional[Runner] = None,
        *,
        timeout_s: Optional[float] = None,
    ) -> None:
        """建一个协调器。

        Args:
            db_path: 状态库文件。``None`` 表示**不落库**：队列只活在进程内存
                里，协调器一个字节都不会写进数据库。给了路径，队列就落进
                ``framework_fix_repro`` 表，别的进程（下一个 tick、另一台
                机器）能接着跑。
            worktree_root: 临时 worktree 建在哪。``None`` 表示用系统临时目录。
                给一个目录是为了让「worktree 建在哪儿、跑完有没有删掉」这件
                事可被断言。
            runner: 跑命令的 callable。``None`` 表示用 :func:`default_runner`。
            timeout_s: 命令的默认时限（秒）。请求自己的 ``timeout_s`` 优先。
                两者都是 ``None`` 时**不限时** —— 调用方要清楚自己在放什么
                进来。

        Raises:
            ReproError: 给了 ``db_path``，但那个库没跑过框架修复迁移。
        """
        self._db_path = Path(db_path) if db_path is not None else None
        self._worktree_root = (
            Path(worktree_root) if worktree_root is not None else None
        )
        self._runner: Runner = runner if runner is not None else default_runner()
        self._timeout_s = timeout_s
        self._requests: Dict[str, ReproRequest] = {}
        self._results: Dict[str, ReproductionResult] = {}
        self._pending: List[str] = []
        #: 连续读不出基准的轮数。**只在不落库时用**（落库时轮数住在
        #: ``result_json`` 的证据里，跟着请求走）。
        self._unclassified_rounds: Dict[str, int] = {}
        # 仓库 -> 占住这个仓库名额的请求号。名额是**按仓库**分的：不同仓库
        # 各占各的，同一个仓库同时只能有一个持有者。
        self._claimed: Dict[str, str] = {}
        self._lock = threading.RLock()
        if self._db_path is not None:
            self._assert_migrated()

    # -- 队列 --------------------------------------------------------------
    def submit(self, request: ReproRequest) -> str:
        """登记一条复现请求。

        登记的同时**替它占住这个仓库的名额**：抢到名额的请求直接进入
        ``running``，抢不到的停在 ``queued``（排队，等这个仓库的名额空出来）。
        名额按仓库分，所以不同仓库的请求互不排队 —— 它们可以同时在飞。

        Args:
            request: 待复现的失败。

        Returns:
            请求号（``request.request_id``）。

        Raises:
            ReproError: 请求不合法（没有 request_id、没有 repo、没有命令），
                或者同一个 request_id 已经登记过了。
        """
        self._validate(request)
        with self._lock:
            if request.request_id in self._requests:
                raise ReproError(
                    f"reproduction request {request.request_id!r} is already queued"
                )
            self._requests[request.request_id] = request
            self._pending.append(request.request_id)
            if self._db_path is not None:
                self._db_insert(request)
            # 占名额。抢不到不是错误 —— 排队就是排队。
            self._claim(request.request_id, request.repo)
        return request.request_id

    def run_pending(self) -> int:
        """把队列里排着的请求逐个跑掉。

        每个请求：在失败所属的仓库里建一棵临时 detached worktree（``git
        worktree add --detach``）-> 在 ``origin/main`` 上把命令跑两遍 ->
        分类 -> 删掉 worktree。

        同一个仓库同一时刻只跑一条：跑完一条，名额空出来，同仓的下一条才排
        得上。不同仓库的请求各自有名额，互不阻塞。

        「基准读不出来」的请求不会被丢掉：它的结论是 ``unclassified``（证据在
        结论里），这一行被放回排队等下一轮再试；连续几轮都读不出来就不再重
        排队，改为往审计链追加一条升级事件。

        Returns:
            跑完的请求数。

        Raises:
            任何异常都**原样往上抛**：跑挂了就是跑挂了，吞掉它只会让队列
            看起来是成功的。抛之前该请求在库里被标成 ``failed``，worktree 也
            已经删掉 —— 队列不会卡在一个永远 ``running`` 的行上。
        """
        processed = 0
        # 这一轮已经处理过的请求。放回排队的那一条下一轮还要再跑，但**这一轮**
        # 不该再跑它 —— 否则一个读不出基准的请求会把这一轮变成死循环。
        handled: Set[str] = set()
        while True:
            request_id = self._next_runnable(skip=handled)
            if request_id is None:
                break
            request = self._requests.get(request_id)
            if request is None:
                request = self._db_load_request(request_id)
            if request is None:
                # 库里有一行，但请求本身读不回来 —— 丢掉它，别让它把队列卡住。
                self._forget(request_id)
                handled.add(request_id)
                continue
            self._requests[request_id] = request
            try:
                result = self._execute(request)
            except BaseException as exc:
                if self._db_path is not None:
                    self._db_mark_failed(request_id, exc)
                self._release(request.repo, request_id)
                handled.add(request_id)
                raise
            self._results[request_id] = result
            retry = False
            if self._is_base_ref_unreadable(result):
                result, round_no = self._stamp_unclassified_round(request_id, result)
                self._results[request_id] = result
                retry = round_no < UNCLASSIFIED_ESCALATION_ROUNDS
                if self._db_path is not None:
                    self._db_store_unclassified(
                        request_id, request, result, round_no, retry
                    )
                else:
                    self._unclassified_rounds[request_id] = round_no
            elif self._db_path is not None:
                self._db_mark_done(request_id, result)
            self._release(request.repo, request_id)
            handled.add(request_id)
            if retry and self._db_path is None:
                # 没有库就没有「排队」这个状态：留在内存队列里，下一轮再试。
                pass
            else:
                self._forget(request_id)
            processed += 1
        return processed

    def recover_interrupted(self) -> int:
        """把残留的 ``running`` 请求放回排队（重启恢复）。

        进程被杀掉时，跑了一半的请求会在库里留下一行 ``running``。那一行占着
        这个仓库的名额 —— 不放回去，这个仓库后面的复现永远排不上队。

        恢复要留痕：每放回一条，就往审计链（``framework_fix_event``）写一条
        事件。「这条请求为什么又跑了一遍」的答案不能只在日志里。

        这一步假定协调器**独占**这条队列：它会把库里**所有** ``running`` 的
        行放回排队。后端是单进程的（架构决策点 3），所以这个假定成立。

        Returns:
            放回排队的请求数。
        """
        recovered: List[str] = []
        if self._db_path is not None:
            for row in self._db_running_rows():
                self._db_recover(row)
                recovered.append(row["repro_id"])
        with self._lock:
            for repo, request_id in list(self._claimed.items()):
                if request_id in recovered:
                    continue
                self._claimed.pop(repo, None)
                recovered.append(request_id)
        for request_id in recovered:
            if request_id not in self._pending:
                self._pending.append(request_id)
        return len(recovered)

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        """一条请求的结论。

        Args:
            request_id: 请求号。

        Returns:
            一个字典，三个键：``request_id``、``classification``（五分类闭集
            里的一个词）、``attempts``（跑了几次）。没有结论时是 ``None`` ——
            没提交过的 request_id 与跑挂了的 request_id 都是「没有结论」。
        """
        result = self._results.get(request_id)
        if result is None and self._db_path is not None:
            result = self._db_load_result(request_id)
        if result is None:
            return None
        return {
            "request_id": request_id,
            "classification": result.classification,
            "attempts": len(result.attempts),
        }

    # -- 执行 --------------------------------------------------------------
    def _claim(self, request_id: str, repo: str) -> bool:
        """占住这个仓库的名额。

        名额是按仓库分的：同一个仓库同时只允许一条复现在跑。抢不到不是错误
        —— 请求继续排队，等这个仓库的名额空出来。

        三道防线，从便宜到权威：

        1. **进程内的名额表** —— 本进程已经占着这个仓库的名额，就不抢。
        2. **库里的 running 行** —— 别的进程占着这个仓库的名额，就不抢。
        3. **部分唯一索引** —— 两个进程同时抢，数据库让只有一个赢；输的那个
           撞上唯一索引冲突，**退化成排队**，不抛错。

        Args:
            request_id: 要占名额的请求号。
            repo: 这个请求属于哪个仓库。

        Returns:
            抢到了名额就是 ``True``；抢不到（要排队）是 ``False``。
        """
        with self._lock:
            if repo in self._claimed:
                return False
            if self._db_path is not None:
                if self._db_repo_is_running(repo):
                    return False
                try:
                    self._db_mark_running(request_id)
                except sqlite3.IntegrityError:
                    # 部分唯一索引兜底：别的进程刚抢到同一个仓库的名额。
                    # 抢输不是事故 —— 排队就行。
                    return False
            self._claimed[repo] = request_id
            return True

    def _release(self, repo: str, request_id: str) -> None:
        """释放这个仓库的名额 —— 请求跑完（或者跑挂了）之后。

        只释放**自己**占的那个：别人的请求占着这个仓库时，释放别人的名额就
        等于让两个复现同时跑在一个仓上。
        """
        with self._lock:
            if self._claimed.get(repo) == request_id:
                del self._claimed[repo]

    def _next_runnable(self, skip: Optional[Set[str]] = None) -> Optional[str]:
        """下一条现在就该跑的请求。

        队列里的请求按登记顺序排队，但**名额**是按仓库分的：同一个仓库只有
        一条请求能同时拿到名额，所以同仓的请求在这里自然排成一条一条；不同
        仓库的请求各有各的名额，互不阻塞。

        Args:
            skip: 这一轮已经处理过的请求号。它们还在队列里（放回排队的那一条
                下一轮还要再跑），但这一轮不再碰。

        Returns:
            请求号；没有请求现在能跑就是 ``None``。
        """
        for request_id in self._pending_ids():
            if skip is not None and request_id in skip:
                continue
            request = self._requests.get(request_id)
            if request is None and self._db_path is not None:
                request = self._db_load_request(request_id)
            if request is None:
                continue
            with self._lock:
                holder = self._claimed.get(request.repo)
            if holder is not None:
                if holder == request_id:
                    # 名额已经是它的了 —— 现在就跑它。
                    return request_id
                # 名额被同仓的另一条占着 —— 排队。
                continue
            if self._claim(request_id, request.repo):
                return request_id
        return None

    def _forget(self, request_id: str) -> None:
        """把请求号从内存队列里去掉。"""
        if request_id in self._pending:
            self._pending.remove(request_id)

    def _pending_ids(self) -> List[str]:
        """队列里排着的请求号。

        内存里的队列与库里的队列**合并**：库里的那一批是别人（上一轮 tick、
        另一台机器）排的，内存里的这一批是本进程排的。两批都该跑。
        """
        ids = list(self._pending)
        if self._db_path is not None:
            for request_id in self._db_pending_ids():
                if request_id not in ids:
                    ids.append(request_id)
        return ids

    @staticmethod
    def _is_base_ref_unreadable(result: ReproductionResult) -> bool:
        """这个结论是「基准读不出来」吗？

        靠证据里的标记认，不靠分类：``unclassified`` 有两种来路 —— 基准读不
        出来（要重新排队），或者跑了两遍但归不了类（分类本身的结论）。只有
        前者要重排队。
        """
        return result.evidence.get("reason") == EVIDENCE_BASE_REF_UNREADABLE

    def _unclassified_round(self, request_id: str) -> int:
        """这一行已经连续几轮读不出基准了。

        轮数住在**上一轮写下的证据**里 —— 它得跟着请求走（进程重启、换一台机器
        都得算得出来），所以它落在 ``result_json`` 里，而不是只活在内存里。
        """
        if self._db_path is None:
            return self._unclassified_rounds.get(request_id, 0)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT result_json FROM framework_fix_repro WHERE repro_id = ?",
                (request_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row["result_json"]:
            return 0
        try:
            payload = json.loads(row["result_json"])
        except (TypeError, ValueError):
            return 0
        if not isinstance(payload, dict):
            return 0
        evidence = payload.get("evidence")
        if not isinstance(evidence, dict):
            return 0
        try:
            return int(evidence.get("unclassified_round", 0))
        except (TypeError, ValueError):
            return 0

    def _stamp_unclassified_round(
        self, request_id: str, result: ReproductionResult
    ) -> Tuple[ReproductionResult, int]:
        """给结论盖上「第几轮未归类」的章。

        轮数写进**证据**里：它是「这条失败已经被看过几次」这个事实的出处，
        下一轮（或者下一个进程）得从它接着数。
        """
        round_no = self._unclassified_round(request_id) + 1
        stamped = ReproductionResult(
            classification=result.classification,
            attempts=result.attempts,
            clean_commit=result.clean_commit,
            tree_fingerprint=result.tree_fingerprint,
            evidence={**result.evidence, "unclassified_round": round_no},
        )
        return stamped, round_no

    def _execute(self, request: ReproRequest) -> ReproductionResult:
        """跑一条请求，得出结论。

        两步：

        1. **解析基准。** 基准固定为失败所属仓库的 ``origin/main``。读不出来
           不是一个异常，而是一个**事实**：结论是 ``unclassified``，证据里留着
           请求的 base_ref、仓库路径与 git 的原始输出。悄悄换个 ref 跑，等于
           在别的地方复现。
        2. **跑两遍。** worktree 的生命周期包在 ``try`` / ``finally`` 里：
           **异常路径同样删目录**。一个装着失败现场的临时目录留在磁盘上，下
           一轮就会有人把它当成证据。
        """
        repo = Path(request.repo)
        base_ref, base_evidence = self._resolve_base_ref(repo, request.base_ref)
        if base_ref is None:
            # 基准读不出来 —— 没有树可跑，结论是 unclassified，证据留住。
            return ReproductionResult(
                classification=UNCLASSIFIED,
                attempts=(),
                clean_commit="",
                tree_fingerprint="",
                evidence={
                    **base_evidence,
                    "reason": EVIDENCE_BASE_REF_UNREADABLE,
                },
            )
        worktree = self._create_worktree(request, base_ref)
        try:
            clean_commit = _git_output(worktree, "rev-parse", "HEAD")
            tree_fingerprint = _tree_fingerprint(worktree)
            timeout_s = (
                request.timeout_s
                if request.timeout_s is not None
                else self._timeout_s
            )
            attempts: List[ReproAttempt] = []
            for index in range(1, REQUIRED_ATTEMPTS + 1):
                attempt = self._runner(request.command, worktree, timeout_s)
                attempts.append(
                    ReproAttempt(
                        run=index,
                        exit_code=attempt.exit_code,
                        timed_out=attempt.timed_out,
                        first_error_line=attempt.first_error_line,
                        duration_s=attempt.duration_s,
                    )
                )
            return ReproductionResult(
                classification=classify(attempts),
                attempts=tuple(attempts),
                clean_commit=clean_commit,
                tree_fingerprint=tree_fingerprint,
            )
        finally:
            self._remove_worktree(repo, worktree)

    def _create_worktree(
        self, request: ReproRequest, base_ref: Optional[str] = None
    ) -> Path:
        """在失败所属的仓库里建一个临时的 detached worktree。

        协议原话：**复现树用 ``git worktree add --detach <tmp> origin/main``
        现场建立、结束即清理，绝不复用 plan 自己的 worktree。** 命令在目标
        仓库里跑（``cwd=request.repo``），建出来的是一棵挂在该仓库上的**链接
        worktree**（它的 ``.git`` 是文件，指向仓库的 ``.git/worktrees/``）。

        为什么是 ``git worktree add`` 而不是克隆：克隆会把远端**再取一遍**，
        取到的是「现在的远端」，而不是失败发生的那一刻的远端；而且克隆出来
        的树与目标仓库无关，基准解析也就落到了另一棵树上。链接 worktree 用
        的就是目标仓库自己的对象库 —— 基准是同一个仓库的同一个 ref。

        路径交给 git 自己创建：``mkdtemp`` 只给出父目录，worktree 目录本身
        还不存在（``git worktree add`` 要求目标路径不存在或为空）。

        Args:
            request: 待复现的失败。
            base_ref: 已经解析出来的基准 ref。``None`` 表示自己解析 —— 解析不
                出来就抛错（:meth:`_execute` 走的是「读不出来就是 unclassified」
                那条路，不会走到这里）。

        Returns:
            worktree 的目录。

        Raises:
            ReproError: 基准解析不出来，或建不出来（git 失败）。
        """
        if base_ref is None:
            resolved, _evidence = self._resolve_base_ref(
                Path(request.repo), request.base_ref
            )
            if resolved is None:
                raise ReproError(
                    f"cannot resolve the base ref {request.base_ref!r} "
                    f"in {request.repo}"
                )
            base_ref = resolved
        root = (
            self._worktree_root
            if self._worktree_root is not None
            else Path(tempfile.gettempdir())
        )
        root.mkdir(parents=True, exist_ok=True)
        parent = Path(tempfile.mkdtemp(prefix="repro-", dir=str(root)))
        worktree = parent / "tree"
        try:
            # 路径与基准 ref 走 ``--`` 分隔符：它们都以路径的身份出现，一个
            # ``-`` 开头的片段不该被 git 读成选项。
            _git_paths(
                ["worktree", "add", "--quiet", "--detach"],
                [str(worktree), base_ref],
                cwd=Path(request.repo),
            )
        except BaseException:
            shutil.rmtree(parent, ignore_errors=True)
            raise
        return worktree

    def _remove_worktree(self, repo: Path, worktree: Path) -> None:
        """把临时 worktree 从目标仓库上摘掉，并删掉它。

        顺序：``git worktree remove --force`` -> ``git worktree prune`` ->
        删目录。三步各管一头：

        * ``remove`` 把这棵树从仓库的 worktree 名单里摘掉；
        * ``prune`` 收拾那些已经不在磁盘上、却还挂在名单里的残骸（进程被杀
          过一次的那种）；
        * 删目录是兜底 —— 目录没了，名单上的残骸由 prune 清掉。

        目录删了但名单没清，等于给下一轮留了一颗地雷：``git worktree add``
        会因为路径被占而失败。
        """
        try:
            _git_paths(
                ["worktree", "remove", "--force"],
                [str(worktree)],
                cwd=repo,
                check=False,
            )
            _git(["worktree", "prune"], cwd=repo, check=False)
        finally:
            shutil.rmtree(worktree.parent, ignore_errors=True)
            shutil.rmtree(worktree, ignore_errors=True)

    @staticmethod
    def _resolve_base_ref(
        repo: Path, base_ref: str
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """基准 ref 在**失败所属的仓库**里解析得出来吗？

        只解析请求声明的那**一个** ref。协议把基准钉死在 ``origin/main``：
        远端默认分支不叫 main 时（``master``、``trunk``），``origin/HEAD`` 是
        同一棵树的另一个名字 —— 但那是**另一处**代码，不是「干净 main」。悄悄
        换个基准跑，同一份失败在别的树上就被判成了框架缺陷。

        解析不出来不是一个异常，是一个**事实**：返回 ``(None, 证据)``，由调用
        方落成 ``unclassified`` 的结论。

        Args:
            repo: 失败所属的仓库（基准在它里面解析）。
            base_ref: 请求声明的基准 ref。

        Returns:
            ``(解析出来的 ref, 证据)``；解析不出来时 ref 是 ``None``，证据里
            是 git 的原始输出或报错。
        """
        evidence: Dict[str, Any] = {
            "base_ref": base_ref,
            "repo": str(repo),
            "git_command": [
                "git",
                "rev-parse",
                "--verify",
                f"{base_ref}^{{commit}}",
            ],
        }
        try:
            completed = _git(
                ["rev-parse", "--verify", f"{base_ref}^{{commit}}"],
                cwd=repo,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ReproError) as exc:
            # 仓库路径不存在、git 跑不起来 —— 同样是「基准读不出来」。
            evidence["git_error"] = f"{type(exc).__name__}: {exc}"
            return None, evidence
        evidence["git_exit_code"] = completed.returncode
        evidence["git_stdout"] = (completed.stdout or "").strip()
        evidence["git_stderr"] = (completed.stderr or "").strip()
        if completed.returncode == 0 and (completed.stdout or "").strip():
            return base_ref, evidence
        return None, evidence

    @staticmethod
    def _validate(request: ReproRequest) -> None:
        """请求够不够跑？

        Raises:
            ReproError: 请求不合法。
        """
        if not isinstance(request, ReproRequest):
            raise ReproError(
                f"expected a ReproRequest, got {type(request).__name__}"
            )
        if not str(request.request_id).strip():
            raise ReproError("request_id must not be empty")
        if not str(request.repo).strip():
            raise ReproError(
                f"reproduction request {request.request_id!r} has no repo"
            )
        if not request.command:
            raise ReproError(
                f"reproduction request {request.request_id!r} has no command"
            )

    # -- 数据库 ------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """一条新连接。

        每次操作开一条新连接，而不是把一条连接存成实例属性：SQLite 的连接
        不是并发安全的，而复现队列会被调度 tick 与验证子进程同时写。
        """
        from state_machine.db.connection import open as open_db

        conn = open_db(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _assert_migrated(self) -> None:
        """库跑过框架修复迁移吗？没跑过就明确报错。

        **不建表。** 建表看起来更「方便」，但它让「这个库跑过迁移没有」这个
        问题永远没有答案：一个从没迁移过的库会在第一次写入时悄悄长出这张
        表，而那张表未必是生产那张 —— 于是队列里有了数据，生产却读不到。
        """
        conn = self._connect()
        try:
            present = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()
        if _REPRO_TABLE not in present:
            raise ReproError(
                f"the state database at {self._db_path} has not run the "
                f"framework-fix migration; the table {_REPRO_TABLE} is "
                "missing. Run state_machine.db.schema.migrate() first — the "
                "coordinator refuses to create its own schema, because a "
                "table it invented is not the table production reads."
            )

    def _db_insert(self, request: ReproRequest) -> None:
        """把请求写进队列。"""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO framework_fix_repro ("
                "  repro_id, repo, plan_id, failure_ref, base_ref, status,"
                "  request_json, result_json, queued_at, started_at, finished_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL)",
                (
                    request.request_id,
                    request.repo,
                    request.plan_id,
                    _failure_ref(request),
                    request.base_ref,
                    STATUS_QUEUED,
                    _json_dumps(request.to_dict()),
                    utcnow_iso(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ReproError(
                f"reproduction request {request.request_id!r} is already queued"
            ) from exc
        finally:
            conn.close()

    def _db_pending_ids(self) -> List[str]:
        """库里排着的请求号。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT repro_id FROM framework_fix_repro "
                "WHERE status = ? ORDER BY queued_at, repro_id",
                (STATUS_QUEUED,),
            ).fetchall()
        finally:
            conn.close()
        return [row["repro_id"] for row in rows]

    def _db_repo_is_running(self, repo: str) -> bool:
        """这个仓库现在有复现在跑吗。

        别的进程占着这个仓库的名额也算 —— 名额是跨进程共享的。
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM framework_fix_repro "
                "WHERE repo = ? AND status = ? LIMIT 1",
                (repo, STATUS_RUNNING),
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def _db_running_rows(self) -> List[sqlite3.Row]:
        """库里所有 ``running`` 的行 —— 重启之后残留的那些。"""
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT repro_id, repo, plan_id, failure_ref "
                "FROM framework_fix_repro WHERE status = ? "
                "ORDER BY queued_at, repro_id",
                (STATUS_RUNNING,),
            ).fetchall()
        finally:
            conn.close()

    def _db_recover(self, row: sqlite3.Row) -> None:
        """把一行 ``running`` 放回排队，并**在同一个事务里**留下事件。

        放回队列与写审计链必须一起成功：分成两个事务就等于给了「请求放回
        队列了、事件没写进去」的机会 —— 那条事件正是「这条请求为什么又跑
        了一遍」的答案。

        ``started_at`` 一起清掉：这一行没跑完过，留着它会让「什么时候开始
        跑的」看起来像一个事实。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE framework_fix_repro "
                    "SET status = ?, started_at = NULL "
                    "WHERE repro_id = ? AND status = ?",
                    (STATUS_QUEUED, row["repro_id"], STATUS_RUNNING),
                )
                conn.execute(
                    "INSERT INTO framework_fix_event ("
                    "  event_id, fingerprint, fix_plan_id, event_type,"
                    "  payload_json, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        row["failure_ref"],
                        row["plan_id"],
                        "repro_recovered",
                        _json_dumps(
                            {
                                "repro_id": row["repro_id"],
                                "repo": row["repo"],
                                "plan_id": row["plan_id"],
                                "previous_status": STATUS_RUNNING,
                                "status": STATUS_QUEUED,
                            }
                        ),
                        utcnow_iso(),
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    # 没有活动事务（COMMIT 之后才失败的）：没什么可回滚的。
                    pass
                raise
        finally:
            conn.close()

    def _db_load_request(self, request_id: str) -> Optional[ReproRequest]:
        """从库里读回一个请求。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT request_json FROM framework_fix_repro "
                "WHERE repro_id = ?",
                (request_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row["request_json"]:
            return None
        try:
            return ReproRequest.from_dict(json.loads(row["request_json"]))
        except (TypeError, ValueError):
            return None

    def _db_load_result(self, request_id: str) -> Optional[ReproductionResult]:
        """从库里读回一个结论。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT result_json FROM framework_fix_repro "
                "WHERE repro_id = ?",
                (request_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row["result_json"]:
            return None
        try:
            payload = json.loads(row["result_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or "classification" not in payload:
            return None
        return ReproductionResult.from_dict(payload)

    def _db_mark_running(self, request_id: str) -> None:
        """把请求标成「在跑」。"""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE framework_fix_repro SET status = ?, started_at = ? "
                "WHERE repro_id = ?",
                (STATUS_RUNNING, utcnow_iso(), request_id),
            )
        finally:
            conn.close()

    def _db_mark_done(self, request_id: str, result: ReproductionResult) -> None:
        """把结论写回去。"""
        self._db_store_result(request_id, result, status=STATUS_DONE)

    def _db_store_result(
        self,
        request_id: str,
        result: ReproductionResult,
        *,
        status: str,
        event: Optional[Dict[str, Any]] = None,
        event_type: str = "",
    ) -> None:
        """把结论写回队列 —— 状态与结论在**同一个事务**里落库。

        分成两个事务就等于给了「结论写进去了、状态没写进去」的机会：那一行会
        永远停在 ``running``，占住这个仓库的名额。

        ``status`` 是 ``queued`` 时这一行还要再跑：``started_at`` 与
        ``finished_at`` 一起清掉 —— 它没跑完（这一轮的结果只是「暂时归不了
        类」），留着时间戳会让「什么时候开始跑的」看起来像一个事实。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if status == STATUS_QUEUED:
                    conn.execute(
                        "UPDATE framework_fix_repro "
                        "SET status = ?, result_json = ?, "
                        "    started_at = NULL, finished_at = NULL "
                        "WHERE repro_id = ?",
                        (status, _json_dumps(result.to_dict()), request_id),
                    )
                else:
                    conn.execute(
                        "UPDATE framework_fix_repro "
                        "SET status = ?, result_json = ?, finished_at = ? "
                        "WHERE repro_id = ?",
                        (
                            status,
                            _json_dumps(result.to_dict()),
                            utcnow_iso(),
                            request_id,
                        ),
                    )
                if event is not None:
                    conn.execute(
                        "INSERT INTO framework_fix_event ("
                        "  event_id, fingerprint, fix_plan_id, event_type,"
                        "  payload_json, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            uuid.uuid4().hex,
                            event.get("fingerprint"),
                            event.get("fix_plan_id"),
                            event_type,
                            _json_dumps(event),
                            utcnow_iso(),
                        ),
                    )
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    # 没有活动事务（COMMIT 之后才失败的）：没什么可回滚的。
                    pass
                raise
        finally:
            conn.close()

    def _db_store_unclassified(
        self,
        request_id: str,
        request: ReproRequest,
        result: ReproductionResult,
        round_no: int,
        retry: bool,
    ) -> None:
        """把「基准读不出来」的结论写回队列。

        这一行**不是** ``failed``：``failed`` 只带一个 ``error`` 键，谁都读不
        出结论，而这条失败还要再看。它被放回 ``queued``（下一轮再试一次：远端
        可能只是暂时没有那个分支）；连续 :data:`UNCLASSIFIED_ESCALATION_ROUNDS`
        轮仍读不出来就不再重排队（``done``），并在**同一个事务**里往审计链追
        加一条升级事件 —— 「为什么这条请求不再被重排队」的答案不能只在日志里。
        """
        event: Optional[Dict[str, Any]] = None
        if not retry:
            event = {
                "repro_id": request_id,
                "repo": request.repo,
                "plan_id": request.plan_id,
                "base_ref": request.base_ref,
                "rounds": round_no,
                "reason": EVIDENCE_BASE_REF_UNREADABLE,
                "fingerprint": _failure_ref(request),
                "fix_plan_id": request.plan_id,
                "previous_status": STATUS_RUNNING,
                "status": STATUS_DONE,
            }
        self._db_store_result(
            request_id,
            result,
            status=STATUS_QUEUED if retry else STATUS_DONE,
            event=event,
            event_type="repro_unclassified_escalated",
        )

    def _db_mark_failed(self, request_id: str, exc: BaseException) -> None:
        """把跑挂了的请求标成 ``failed``。

        这一步不能省：留在 ``running`` 上的行会占住那个仓库的唯一索引名额
        （一个仓库同时只允许一条复现在跑），下一次复现就再也排不上队了。
        """
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE framework_fix_repro "
                "SET status = ?, result_json = ?, finished_at = ? "
                "WHERE repro_id = ?",
                (
                    STATUS_FAILED,
                    _json_dumps({"error": f"{type(exc).__name__}: {exc}"}),
                    utcnow_iso(),
                    request_id,
                ),
            )
        finally:
            conn.close()
