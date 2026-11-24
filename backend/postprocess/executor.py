"""收尾阶段的统一命令入口：白名单、逐字审计与待批准分流。

What this module owns
---------------------
架构决策点 4 把「可执行什么、执行了什么、对谁执行」从一句 prompt 变成
结构性约束，本模块就是那个约束的代码形态：

* :class:`CommandRunner` —— **唯一入口**。所有 ``git`` / ``gh`` 调用都从
  这里出去，且只放行冻结要求书里**逐字**声明过的那一条 argv；
* :class:`CommandRejected` —— 拒绝。没被放行的命令在 **spawn 之前** 就被
  拒，所以「拒绝」不留进程、不碰网络、不留半截副作用；
* :class:`NeedsApproval` —— 转待批准。合入 main、破坏性操作、花钱三类
  不由执行器执行，只转成一条「请求批准」的待办，等人当场拍板。

执行器只产出**证据**与**审计**，不产出结论（架构决策点 2）：它交出
:class:`~postprocess.models.EvidenceRecord`（命令的退出码与两路输出）与
:class:`~postprocess.models.AuditRecord`（动作、argv 原文、目标），PASS
与 FAIL 是判定器的事。

Why the whitelist is the frozen book itself
-------------------------------------------
白名单不是一份写死的命令表，而是**冻结要求书本身**：条目 id → 声明的
argv。放行一条命令要同时过三道闸：

1. **二进制**在 ``{git, gh}`` 内 —— ``curl``、``rm``、``python`` 一律不
   放行，哪怕它已经写进了要求书；
2. **条目**是 ``external=true`` —— 不对外的条目不携带命令；
3. **argv 与冻结要求书逐字相等** —— 顺序、参数、一个字符的差别都算
   不相等。这三道闸都过、且 argv 本身属于三类「必须由人批准」的动作
   时，才轮到真正 spawn。

第三道闸是关键：它把「执行方临时拼一条相近的命令」变成结构性不可能。
执行方改一个参数、换一个顺序、或者干脆换一个条目 id，都不再是被放行
的命令，而是被拒的命令 —— 而且拒绝发生在 spawn 之前。

Why ``shell=False`` is not negotiable
-------------------------------------
命令以 argv 数组的形式传给进程，**永远不经 shell**。所以 ``;``、
``$(...)``、反引号只是 argv 里的几个字符，不会被展开成第二条命令 ——
这一点由 ``test_shell_metachars_not_expanded`` 用真实子进程钉住（哨兵
文件必须不存在）。

Why the three gated categories become a question, not a refusal
---------------------------------------------------------------
合入 main、破坏性操作、花钱三类**不是**「非法命令」，而是「合法的命令
需要一个人当场点头」。所以它们返回 :class:`NeedsApproval`（带着要问的
问题和 argv 原文），而不是抛 :class:`CommandRejected`：调用方据此把该
条目转成一条等待批准的待办，而不是把整轮收尾判成失败。

三类动作的识别是**保守**的：宁可多问一次，也不放过一次。识别不出来
的（比如「花钱」只能从 argv 里的字样去猜）走 :data:`MONEY_MARKERS` 这
张短表 —— 猜错的方向是「多问一次」，不是「少问一次」。

Why the zero-diff check is a guard, not a prompt
------------------------------------------------
A5 要求「写入必须改代码才能满足的判据」时收尾停下、该条 FAIL、
``git diff`` 为空。一句 prompt 说「不许改产品代码」不可判定 —— 所以
这条判据落在代码里：:func:`product_diff_guard` 是一个上下文管理器，
每个条目套着它跑；条目跑完之后它读一次工作区（``git status
--porcelain`` 加产品代码文件的哈希），发现越界改动就抛
:class:`ProductCodeModified`。抛异常的时机在条目**跑完之后**，所以
「是哪个条目越界」是确定的事实，不是推断。

守卫只**抛**不**判**：PASS/FAIL 是 :mod:`postprocess.judge` 的事。
守卫抛出来的那一条由编排器判 FAIL、停止收尾、通知人工 —— 它自己不
修复、不临时放宽判据。

守卫读工作区用的 ``git status`` / ``git ls-files`` 是**只读勘查**，
不是对外动作：它们不改任何东西、不碰网络、不留下副作用。白名单管的是
「对外做了什么」，只读勘查不在其列 —— 但它同样不经 shell、同样是 argv
数组。

What this module does NOT do
----------------------------
它不判定 PASS/FAIL（那是 :mod:`postprocess.judge` 的事）；不自己回答
待批准的问题（那是等待生命周期的事）。它也不吞掉 spawn 的失败：进程起
不来时那个 OSError 会照常往上抛，因为那是环境坏了，不是命令没被放行。
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from postprocess.models import AuditRecord, EvidenceRecord


#: 本模块的 logger —— 缺省产品代码路径的 warning、声明文件里写坏的行，
#: 都记在这里。
logger = logging.getLogger(__name__)


__all__ = [
    "ALLOWED_BINARIES",
    "FINISHING_ARTIFACT_ROOT",
    "MONEY_MARKERS",
    "PRODUCT_CODE_PATHS",
    "PROTECTED_BRANCHES",
    "UPSTREAM_MERGE_COMMAND",
    "CommandRejected",
    "CommandRunner",
    "NeedsApproval",
    "ProductCodeModified",
    "execute",
    "product_diff_guard",
    "product_guard_root",
]


#: 唯一允许被执行的二进制。写死的名字，不是路径 —— ``/usr/bin/git`` 也
#: 不放行，因为「这个路径上是什么」不归这条白名单管。
ALLOWED_BINARIES: Tuple[str, ...] = ("git", "gh")

#: 受保护分支：推送到它们等同于合入 main，必须由人当场批准。
PROTECTED_BRANCHES: Tuple[str, ...] = ("main", "master")

#: 「花钱」的字样标记。命中即转待批准 —— 猜错的方向是多问一次。
MONEY_MARKERS: Tuple[str, ...] = (
    "billing",
    "payment",
    "purchase",
    "checkout",
    "invoice",
    "subscription",
    "spend",
    "charge",
)

#: 强制类 git 选项。
_FORCE_FLAGS: Tuple[str, ...] = ("-f", "--force", "--force-with-lease")

#: 产品代码路径的**缺省值** —— 仓库根下的相对路径。
#:
#: 守卫对产品代码路径下的每一个文件做 sha256 比对：新增、改动、删除都算
#: 越界。这条判据同时覆盖**已跟踪**与**未跟踪**的文件（``ls-files -co
#: --exclude-standard`` 把未跟踪的也列出来），所以条目新写一个源码文件也
#: 抓得到 —— 不依赖 porcelain 报不报得出它。
#:
#: 这是**缺省值**，不是真相：每个项目的源码目录叫什么名字不一样，框架不
#: 可能全知道，也不该猜。真正生效的清单由项目自己声明 ——
#: ``<repo>/.pdt/product_paths``，见 :func:`declared_product_paths`。
#: 项目声明了就用项目的，没声明才退回这张表（并打一条 warning）。
#:
#: 刻意**不含** ``tests``：e2e 截图之类的产物落在 ``tests/screenshots/``
#: 下，含进去会把产物误判成产品代码。
#:
#: 刻意**不含** ``reports`` / ``docs`` / ``data`` / ``coverage`` / ``projects``：
#: 那些是产物与数据，不是产品代码。
PRODUCT_CODE_PATHS: Tuple[str, ...] = (
    "api",
    "app",
    "backend",
    "components",
    "domain",
    "frontend",
    "infrastructure",
    "models",
    "scripts",
    "services",
    "src",
)

#: 项目声明产品代码路径的**文件**（仓库相对路径）—— 放在项目已有的、被
#: gitignore 的 ``.pdt/`` 目录下面（``.pdt/evidence``、``.pdt/service-locks``
#: 就在那儿）。
PRODUCT_PATHS_FILE: str = ".pdt/product_paths"

#: 收尾产物的根目录 —— 计划目录 ``plans/{id}/`` 下的产物允许写。
FINISHING_ARTIFACT_ROOT: str = "plans"

#: 收尾要求书里声明的**上游合并**命令 —— 逐字匹配，不做宽判据。
#:
#: 这一条命令（``git merge origin/main``）按定义就会把上游的改动带进
#: 工作区，而上游的改动里有产品代码 —— 所以「产品代码零改动」对它没有
#: 意义：这不是条目改了产品代码，是条目把上游合进来了。
#:
#: 刻意**逐字匹配**：``git merge feature/x``、``git merge origin/main
#: --no-ff``、``git -c x=y merge origin/main`` 都不算 —— 那些不是
#: 「合上游主干的这一条」。
UPSTREAM_MERGE_COMMAND: Tuple[str, ...] = ("git", "merge", "origin/main")


class CommandRejected(Exception):
    """一条命令没有被放行。

    抛出它的地方都在 spawn **之前**，所以命令一次都没有跑过。调用方
    （编排器）据此停下这一轮，后续条目不再执行。

    Args:
        reason: 为什么被拒 —— 一句能指回条目与命令的话。
        item_id: 被拒的条目 id，知道了才知道该改哪一条。
        command: 被拒的 argv 原文（拿不到时是 ``None``）。
    """

    def __init__(
        self,
        reason: str,
        *,
        item_id: Optional[str] = None,
        command: Any = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.item_id = item_id
        self.command = list(command) if isinstance(command, (list, tuple)) else command


@dataclass(frozen=True)
class NeedsApproval:
    """一条需要人当场批准的动作。

    执行器**不执行**它，只把它转成一条待办：问句是要问人的那句话，
    argv 是人批准之后真正要跑的那条命令（逐字，不拼字符串）。

    Args:
        question: 问人的那句话。
        command: 待批准的 argv 原文。
    """

    question: str
    command: List[str] = field(default_factory=list)


class CommandRunner:
    """收尾阶段执行命令的唯一入口。

    Args:
        frozen_spec: 冻结的要求书 —— 白名单就从它的条目里长出来。可以是
            ``FrozenSpec``（条目在 ``.spec.items`` 上）、``Spec``（条目在
            ``.items`` 上）、一个 ``{"items": [...]}`` 映射，或条目序列。
        workspace: 命令的工作目录 —— 被执行的进程在哪个仓里跑。
        run: 实际跑进程的可调用对象，默认 :func:`subprocess.run`。注入它
            是为了让「命令一次都没跑过」这种断言可以被数出来。
    """

    def __init__(
        self,
        frozen_spec: Any,
        *,
        workspace: Any,
        run: Optional[Callable[..., Any]] = subprocess.run,
    ) -> None:
        self.frozen_spec = frozen_spec
        self.workspace = Path(workspace)
        self._run = run if run is not None else subprocess.run
        self._declared: Dict[str, Any] = _index_declared(frozen_spec)

    # -- 入口 ---------------------------------------------------------

    def run_item(
        self,
        item: Any,
    ) -> Union[Tuple[EvidenceRecord, AuditRecord], NeedsApproval]:
        """跑一个要求书条目，产出证据与审计。

        Args:
            item: 要求书条目（``SpecItem`` 或同形状的映射）。

        Returns:
            ``(EvidenceRecord, AuditRecord)`` —— 命令跑了，产出证据与审计
            三元组；或者一个 :class:`NeedsApproval` —— 命令合法但需要人
            当场批准，执行器没有 spawn。

        Raises:
            CommandRejected: 命令没被放行（形状不对、二进制不在白名单、
                条目不对外、argv 与冻结要求书不逐字相等、条目根本没被
                声明）。拒绝发生在 spawn 之前。
        """
        item_id = _text_field(item, "id")
        command = _field(item, "command")
        external = bool(_field(item, "external", False))
        declared_target = _text_field(item, "target")

        argv = self._checked_argv(item_id, command, external)

        if argv is None:
            # 没有声明命令的条目：什么都不跑，证据里也没有退出码。
            evidence = EvidenceRecord(item_id=item_id, command=[])
            audit = AuditRecord(
                action=_text_field(item, "action"),
                command=[],
                target=declared_target or item_id,
            )
            return evidence, audit

        self._check_declared(item_id, argv)

        approval = _approval_needed(argv)
        if approval is not None:
            return approval

        completed = self._run(
            argv,
            cwd=str(self.workspace),
            shell=False,
            capture_output=True,
            text=True,
        )

        evidence = EvidenceRecord(
            item_id=item_id,
            command=list(argv),
            exit_code=_returncode(completed),
            stdout=_stream_text(completed, "stdout"),
            stderr=_stream_text(completed, "stderr"),
        )
        audit = AuditRecord(
            action=_action_of(argv),
            command=list(argv),
            target=_target_of(declared_target, argv, item_id),
        )
        return evidence, audit

    def execute(
        self,
        item: Any,
        context: Any = None,
    ) -> Union[Tuple[EvidenceRecord, AuditRecord], NeedsApproval]:
        """跑一个条目 —— 委托 :meth:`run_item`。

        Args:
            item: 要求书条目。
            context: 调用上下文。本方法用不上它（工作目录与冻结要求书都
                已经在 ``self`` 上了），但保留这个形参是为了让
                ``execute(item, context)`` 这个调用形状成立。

        Returns:
            与 :meth:`run_item` 相同。
        """
        return self.run_item(item)

    # -- 三道闸 -------------------------------------------------------

    def _checked_argv(
        self,
        item_id: str,
        command: Any,
        external: bool,
    ) -> Optional[List[str]]:
        """过形状闸与二进制闸、外部闸。

        Args:
            item_id: 条目 id，用于拒绝消息。
            command: 条目声明的 command，形态未经检查。
            external: 条目是否对外。

        Returns:
            校验过的 argv；条目没有声明命令时是 ``None``。

        Raises:
            CommandRejected: 形态不对、二进制不在白名单，或不对外的条目
                携带了命令。
        """
        if isinstance(command, str):
            raise CommandRejected(
                "command 必须是 argv 列表（list[str]），不能是字符串",
                item_id=item_id,
                command=command,
            )
        if command is None:
            return None
        if not isinstance(command, (list, tuple)):
            raise CommandRejected(
                f"command 必须是 argv 列表（list[str]），得到 "
                f"{type(command).__name__}",
                item_id=item_id,
            )

        argv: List[str] = []
        for argument in command:
            if not isinstance(argument, str) or not argument:
                raise CommandRejected(
                    "command 的每一项必须是非空字符串",
                    item_id=item_id,
                    command=list(command),
                )
            argv.append(argument)

        if not argv:
            raise CommandRejected("command 不能为空", item_id=item_id)

        if argv[0] not in ALLOWED_BINARIES:
            raise CommandRejected(
                f"命令 {argv[0]!r} 不在白名单 {ALLOWED_BINARIES} 内",
                item_id=item_id,
                command=argv,
            )

        if not external:
            raise CommandRejected(
                "external=false 的条目不得携带 command",
                item_id=item_id,
                command=argv,
            )

        return argv

    def _check_declared(self, item_id: str, argv: List[str]) -> None:
        """过冻结要求书这一道闸：条目被声明过，且 argv 逐字相等。

        Args:
            item_id: 条目 id。
            argv: 待放行的 argv。

        Raises:
            CommandRejected: 条目没被声明、没声明命令，或 argv 与声明的
                那条不逐字相等（顺序或参数有差别都算）。
        """
        declared = self._declared.get(item_id)
        if declared is None:
            raise CommandRejected(
                f"条目 {item_id!r} 未在冻结要求书中声明",
                item_id=item_id,
                command=argv,
            )

        declared_command = _field(declared, "command")
        if not isinstance(declared_command, (list, tuple)):
            raise CommandRejected(
                f"冻结要求书条目 {item_id!r} 未声明 command",
                item_id=item_id,
                command=argv,
            )

        if list(declared_command) != argv:
            raise CommandRejected(
                f"条目 {item_id!r} 的 argv 与冻结要求书声明的命令不逐字相等",
                item_id=item_id,
                command=argv,
            )


def execute(
    item: Any,
    context: Any = None,
) -> Union[Tuple[EvidenceRecord, AuditRecord], NeedsApproval]:
    """跑一个要求书条目 —— 委托给 ``context`` 里的 :class:`CommandRunner`。

    Args:
        item: 要求书条目。
        context: 装着 runner 的上下文 —— 可以是 runner 本身、``{"runner":
            runner}`` 这样的映射，或带 ``runner`` 属性的对象。

    Returns:
        与 :meth:`CommandRunner.run_item` 相同：``(EvidenceRecord,
        AuditRecord)`` 或一个 :class:`NeedsApproval`。

    Raises:
        TypeError: context 里没有 runner。
        CommandRejected: 命令没被放行。
    """
    return _runner_from_context(context).run_item(item)


def _runner_from_context(context: Any) -> "CommandRunner":
    """从上下文里找出 runner。

    Args:
        context: runner 本身、``{"runner": runner}`` 这样的映射，或带
            ``runner`` 属性的对象。

    Returns:
        上下文里的 :class:`CommandRunner`。

    Raises:
        TypeError: 上下文里没有 runner。
    """
    if isinstance(context, CommandRunner):
        return context

    if isinstance(context, Mapping):
        for key in ("runner", "command_runner"):
            candidate = context.get(key)
            if isinstance(candidate, CommandRunner):
                return candidate

    for attribute in ("runner", "command_runner"):
        candidate = getattr(context, attribute, None)
        if isinstance(candidate, CommandRunner):
            return candidate

    raise TypeError("execute 需要 context 里的 CommandRunner")


# ---------------------------------------------------------------------
# 三类必须由人批准的动作
# ---------------------------------------------------------------------


def _approval_needed(argv: Sequence[str]) -> Optional[NeedsApproval]:
    """这条命令是不是「必须由人当场批准」的那三类之一。

    Args:
        argv: 待放行的 argv。

    Returns:
        一个 :class:`NeedsApproval`，或者 ``None``（这条命令可以直接跑）。
    """
    if _merges_into_main(argv):
        return NeedsApproval(question="批准合入 main？", command=list(argv))
    if _is_destructive(argv):
        return NeedsApproval(question="批准执行破坏性操作？", command=list(argv))
    if _spends_money(argv):
        return NeedsApproval(question="批准执行付费操作？", command=list(argv))
    return None


def _merges_into_main(argv: Sequence[str]) -> bool:
    """这条命令是不是把改动合进 main。

    Args:
        argv: 待放行的 argv。

    Returns:
        ``True`` 表示它合入 main —— ``gh pr merge``，或把内容推进受保护
        分支的 ``git push``。
    """
    binary = argv[0]
    rest = list(argv[1:])

    if binary == "gh":
        return len(rest) >= 2 and rest[0] == "pr" and rest[1] == "merge"

    if binary == "git" and rest[:1] == ["push"]:
        return _pushes_into_protected(rest[1:])

    return False


def _pushes_into_protected(args: Sequence[str]) -> bool:
    """``git push`` 的目标是不是受保护分支。

    Args:
        args: ``git push`` 之后的参数。

    Returns:
        ``True`` 表示这条 push 的目标是 ``main`` / ``master``。
    """
    positional = [token for token in args if not token.startswith("-")]
    if len(positional) < 2:
        # 只有 remote，没有 refspec —— 推的是当前分支，推不到 main。
        return False

    for token in positional[1:]:
        destination = token.split(":", 1)[1] if ":" in token else token
        destination = destination.lstrip("+")
        if destination.startswith("refs/heads/"):
            destination = destination[len("refs/heads/") :]
        if destination in PROTECTED_BRANCHES:
            return True
    return False


def _is_destructive(argv: Sequence[str]) -> bool:
    """这条命令是不是破坏性操作。

    Args:
        argv: 待放行的 argv。

    Returns:
        ``True`` 表示它会丢东西 —— 强制推送、硬重置、清理工作区、删分支
        或删远端对象。
    """
    binary = argv[0]
    rest = list(argv[1:])
    subcommand = rest[0] if rest else ""

    if binary == "git":
        if subcommand == "push":
            return any(flag in rest for flag in _FORCE_FLAGS)
        if subcommand == "reset":
            return "--hard" in rest
        if subcommand == "clean":
            return any(flag in rest for flag in ("-f", "--force"))
        if subcommand == "branch":
            return "-D" in rest or (
                "--delete" in rest and any(flag in rest for flag in _FORCE_FLAGS)
            )
        if subcommand in ("checkout", "switch"):
            return any(flag in rest for flag in _FORCE_FLAGS)
        if subcommand == "update-ref":
            return "-d" in rest
        return False

    if binary == "gh":
        if subcommand in (
            "repo",
            "release",
            "secret",
            "gist",
            "label",
            "cache",
            "ruleset",
            "variable",
        ):
            return "delete" in rest
        if subcommand == "api":
            return _gh_api_deletes(rest)
        return False

    return False


def _gh_api_deletes(args: Sequence[str]) -> bool:
    """``gh api`` 这次调用是不是个删除。

    Args:
        args: ``gh api`` 之后的参数。

    Returns:
        ``True`` 表示请求方法是 DELETE。
    """
    for index, token in enumerate(args):
        if token in ("-X", "--method") and index + 1 < len(args):
            if args[index + 1].upper() == "DELETE":
                return True
    return False


def _spends_money(argv: Sequence[str]) -> bool:
    """这条命令是不是在花钱。

    Args:
        argv: 待放行的 argv。

    Returns:
        ``True`` 表示 argv 里出现了花钱的字样。只有 ``gh`` 会花钱，所以
        只在 ``gh`` 上看 —— git 命令不会。
    """
    if argv[0] != "gh":
        return False
    return any(
        marker in token.lower()
        for token in argv[1:]
        for marker in MONEY_MARKERS
    )


# ---------------------------------------------------------------------
# 读取冻结要求书
# ---------------------------------------------------------------------


def _index_declared(frozen_spec: Any) -> Dict[str, Any]:
    """把冻结要求书的条目按 id 索引起来。

    Args:
        frozen_spec: 冻结的要求书。

    Returns:
        ``{条目 id: 条目}``。

    Raises:
        TypeError: 读不出条目。
    """
    index: Dict[str, Any] = {}
    for declared in _declared_items(frozen_spec):
        item_id = _text_field(declared, "id")
        if item_id and item_id not in index:
            index[item_id] = declared
    return index


def _declared_items(frozen_spec: Any) -> List[Any]:
    """从冻结要求书里读出条目列表。

    Args:
        frozen_spec: 冻结的要求书 —— ``FrozenSpec``（条目在 ``.spec.items``
            上）、``Spec``（条目在 ``.items`` 上）、``{"items": [...]}``
            映射，或条目序列本身。

    Returns:
        条目列表。

    Raises:
        TypeError: 这个形状里读不出条目。
    """
    if isinstance(frozen_spec, Mapping):
        raw = frozen_spec.get("items")
        if raw is None:
            raise TypeError("冻结要求书缺少 items")
        return list(raw)

    if isinstance(frozen_spec, (list, tuple)):
        return list(frozen_spec)

    items = getattr(frozen_spec, "items", None)
    if items is not None and not callable(items):
        return list(items)

    inner = getattr(frozen_spec, "spec", None)
    if inner is not None:
        return _declared_items(inner)

    raise TypeError(
        f"无法从 {type(frozen_spec).__name__} 读出冻结要求书的条目"
    )


# ---------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------


def _field(item: Any, name: str, default: Any = None) -> Any:
    """读条目的一个字段 —— 映射与对象都认。"""
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _text_field(item: Any, name: str) -> str:
    """读条目的一个文本字段，缺失时是空串。"""
    value = _field(item, name)
    if value is None:
        return ""
    return str(value)


def _action_of(argv: Sequence[str]) -> str:
    """这条命令的动作 —— 审计三元组的第一项。

    Args:
        argv: 已放行的 argv。

    Returns:
        动作名：``git push`` 是 ``"push"``，``gh pr create`` 是
        ``"pr create"``。
    """
    binary = argv[0]
    rest = [token for token in argv[1:] if not token.startswith("-")]
    if binary == "git":
        return rest[0] if rest else "git"
    if binary == "gh":
        return " ".join(rest[:2]) if rest else "gh"
    return binary


def _target_of(declared_target: str, argv: Sequence[str], item_id: str) -> str:
    """这条命令的目标 —— 审计三元组的第三项。

    Args:
        declared_target: 条目声明的 target（没有时是空串）。
        argv: 已放行的 argv。
        item_id: 条目 id —— 最后的兜底，让目标总能指回条目。

    Returns:
        目标：条目声明的 target 优先，其次是 ``git push`` 的 remote，
        最后是条目 id。
    """
    if declared_target:
        return declared_target
    if len(argv) > 2 and argv[0] == "git" and argv[1] == "push":
        positional = [token for token in argv[2:] if not token.startswith("-")]
        if positional:
            return positional[0]
    return item_id


def _returncode(completed: Any) -> Optional[int]:
    """完成结果的退出码 —— 拿不到时是 ``None``（这本身也是事实）。"""
    return getattr(completed, "returncode", None)


def _stream_text(completed: Any, name: str) -> str:
    """完成结果的一路输出 —— 拿不到时是空串。"""
    value = getattr(completed, name, None)
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# ---------------------------------------------------------------------
# 产品代码零改动校验
# ---------------------------------------------------------------------


class ProductCodeModified(Exception):
    """收尾阶段的产品代码被改了。

    抛出它的地方在条目**执行完毕之后**：条目跑了，守卫比对了它跑前跑后
    的工作区，发现产品代码被动过 —— 于是这一条判 FAIL、收尾停下、通知
    人工。守卫自己不修复、不临时放宽判据。

    Args:
        message: 一句话，指回条目与被碰到的文件。
        item_id: 越界的条目 id —— 知道了才知道该看哪一条。
        paths: 被碰到的路径（仓库相对路径，已排序去重）。
        diff: 越界的**原文证据** —— ``git status --porcelain`` 与
            ``git diff --stat`` 两条只读命令的输出，以及被碰到的路径。
            判据（``models.EvidenceRecord.git_diff``）拿到的就是它，
            所以报告里那一段是人可以自己复核的原文，不是摘要。
    """

    def __init__(
        self,
        message: str,
        *,
        item_id: Optional[str] = None,
        paths: Sequence[str] = (),
        diff: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.item_id = item_id
        self.paths: Tuple[str, ...] = tuple(paths)
        self.diff: str = diff


@dataclass(frozen=True)
class _WorkingTree:
    """一个时刻的工作区状态。

    Attributes:
        changed: **跑完那一刻**的 ``git status --porcelain`` 全量路径
            （含未跟踪文件）—— 它是当时工作区的整个快照，不区分哪一条
            是这次跑出来的、哪一条是跑之前就有的。判「本次越界」时要用
            跑之前的那一份作差集。
        hashes: 产品代码路径下每个文件的 sha256 —— 路径 → 摘要。
        tracked: ``git ls-files`` 报出的**已跟踪**路径 —— 未跟踪的产物不在
            里面，所以守卫不会把「跑的时候新出现的临时文件」算成越界
            （``plans/`` 下别的计划目录另有判据，见 :meth:`touched`）。
    """

    changed: Tuple[str, ...] = ()
    hashes: Mapping[str, str] = field(default_factory=dict)
    tracked: Tuple[str, ...] = ()

    @classmethod
    def capture(cls, root: Path) -> "_WorkingTree":
        """读一次工作区状态。

        Args:
            root: 仓库根目录。

        Returns:
            工作区状态：porcelain 报出的路径，加上产品代码文件的哈希，
            加上已跟踪路径的全量清单。

        Raises:
            RuntimeError: git 读不出来（不是仓库、或 git 本身坏了）。
        """
        changed = _porcelain_paths(
            _git_text(
                root,
                "status",
                "--porcelain",
                "-z",
                # 不加这个，git 把未跟踪的**目录**折成一行（``?? backend/``），
                # 报出来的就不是被碰的那个文件，而是一个笼统的目录名。
                "--untracked-files=all",
            )
        )
        hashes: Dict[str, str] = {}
        tracked = tuple(
            p for p in _git_text(root, "ls-files", "-z").split("\0") if p
        )
        listing = _git_text(
            root,
            "--literal-pathspecs",
            "ls-files",
            "-co",
            "--exclude-standard",
            "-z",
            "--",
            # 前缀列表不会为空（项目没声明就用缺省值），所以这里传出去的
            # pathspec 一定是非空的 —— 空 pathspec 会列出全仓。
            *product_paths(root),
        )
        for relative in listing.split("\0"):
            if not relative:
                continue
            path = root / relative
            if path.is_file():
                hashes[relative] = _sha256(path)
        return cls(changed=tuple(changed), hashes=hashes, tracked=tracked)

    def touched(
        self,
        before: "_WorkingTree",
        allowed: Sequence[str],
        *,
        skip_product_hashes: bool = False,
    ) -> List[str]:
        """这段时间里被碰到的越界路径。

        两条判据合起来看：

        1. porcelain 里**本次跑出来的、落在允许范围之外的**路径 —— 越界。
           其中两条入选条件满足其一即可：

           a. 该路径**是已跟踪的** —— 已跟踪的文件不许被条目改动，不管它在
              产品代码路径之内还是之外；
           b. 该路径**在收尾产物根目录（``plans/``）之下、但不在本计划的
              目录之下** —— **别的计划的收尾产物目录不许被写，新建也不行**。

           跑之前就已经存在的改动不算本次越界，那是进入收尾阶段之前的状态，
           不是条目干的。其余的未跟踪新文件也不算越界（那是产物，不是产品
           代码）；产品代码路径下的新文件由下面的哈希判据抓。理由：守卫对
           「仓库被改了什么」负责，不对「仓库里多了个临时产物」负责 ——
           条目只对**自己跑的那段时间**负责。
        2. 产品代码文件的哈希变了、消失了或新出现了 —— 越界。「产品代码
           文件」= 落在**项目声明的**产品代码路径下的文件（项目在
           ``.pdt/product_paths`` 里声明；没声明就用缺省值，见
           :func:`product_paths`）。这一条同时覆盖 git 自己不报的情形：被
           ``assume-unchanged`` / ``skip-worktree`` 藏起来的文件，改了它
           ``git status`` 仍然是空的；以及条目**新写**的源码文件（未跟踪，
           但落在产品代码路径下）。

        Args:
            before: 条目跑之前的状态。
            allowed: 允许改动的路径前缀（收尾产物目录）。
            skip_product_hashes: 跳过第 2 条判据（产品代码路径下文件的
                sha256 比对）。只给「声明的上游合并」用：干净完成的
                ``git merge origin/main`` 会把上游的内容带进产品代码路径
                下的文件 —— 那是上游的改动，不是条目自己改的。
                **第 1 条判据（porcelain 越界路径）不受影响**：一次干净
                完成的合并不留 porcelain 改动；合并冲突未完成、或合并
                之后又手工改了文件，都会留下 porcelain 改动，照样判红。

        Returns:
            越界路径，已排序去重；没有越界时是空列表。
        """
        before_changed = set(before.changed)
        tracked_any = set(before.tracked) | set(self.tracked)
        out_of_scope = [
            path
            for path in self.changed
            if path not in before_changed
            and not _within(path, allowed)
            and (path in tracked_any or _protected_new_path(path, allowed))
        ]
        if skip_product_hashes:
            return sorted(set(out_of_scope))
        rewritten = [
            path
            for path, digest in self.hashes.items()
            if before.hashes.get(path) != digest
        ]
        vanished = [path for path in before.hashes if path not in self.hashes]
        return sorted(set(out_of_scope) | set(rewritten) | set(vanished))


@contextmanager
def product_diff_guard(
    item: Any,
    *,
    repo: Any = None,
    plan_id: Optional[str] = None,
) -> Iterator[None]:
    """守住一个条目：产品代码被动过，这一抛。

    用法::

        with product_diff_guard(item, repo=workspace):
            runner.run_item(item)

    守卫在条目跑完之后读一次工作区：``git status --porcelain`` 报出来的
    路径、以及产品代码路径下每个文件的 sha256。落在允许范围之外的改动
    —— 包括未跟踪文件 —— 就是越界，于是抛 :class:`ProductCodeModified`，
    编排器据此判该条 FAIL、停止收尾、通知人工。剩下的条目不再执行。

    **窄豁免**：条目若正是 ``git merge origin/main``（逐字匹配，见
    :data:`UPSTREAM_MERGE_COMMAND`），则跳过产品代码 sha256 那一判据 ——
    合入上游必然会改动产品代码路径下的文件，那是上游的改动。porcelain
    那一判据原样保留：冲突未完成的合并、合并之后的手工改动照样判红。

    Args:
        item: 要求书条目 —— 它的 id 进异常消息，指得回是哪一条越界。
        repo: 被校验的仓库；不给就是当前工作目录。
        plan_id: 收尾产物所在的计划 id —— ``plans/{plan_id}/`` 下的产物
            （进度、报告、要求书引用）允许写，其余改动一律越界。不给就
            什么都不允许写。

    Yields:
        ``None`` —— 守卫自己不产出东西，它只负责在条目跑完之后比对。

    Raises:
        ProductCodeModified: 条目跑完之后工作区里出现了越界改动。
        RuntimeError: 给的不是一个 git 仓库 —— 校验不了就不当没看见。
    """
    item_id = _text_field(item, "id")
    root = _repository_root(repo)
    allowed = _allowed_prefixes(plan_id)
    before = _WorkingTree.capture(root)
    # 「声明的上游合并」豁免第 2 条判据（产品代码哈希）。豁免**只**针对
    # 一次干净完成的 ``git merge origin/main``：它把上游的内容带进工作区，
    # 那是上游的改动，不是条目改的。第 1 条判据（porcelain 越界路径）
    # 原样保留 —— 合并冲突未完成、或者合并之后又手工改了文件，都会留下
    # porcelain 改动，照样判红。
    skip_product_hashes = _is_upstream_merge(item)

    try:
        yield
    except BaseException as failure:
        # 条目自己失败时，它自己的异常更有信息量 —— 但产品代码被改过这
        # 一事实不能丢，所以两条一起报：守卫的异常从条目那个异常派生。
        _raise_if_modified(
            root,
            before,
            allowed,
            item_id,
            cause=failure,
            skip_product_hashes=skip_product_hashes,
        )
        raise

    _raise_if_modified(
        root, before, allowed, item_id, skip_product_hashes=skip_product_hashes
    )


def _is_upstream_merge(item: Any) -> bool:
    """这一条是不是「声明的上游合并」。

    判据是**精确 argv 匹配** :data:`UPSTREAM_MERGE_COMMAND`
    （``["git", "merge", "origin/main"]``）—— 不是「命令里含 merge 字样」。
    含 merge 字样的命令多得很（``git merge feature/x``、
    ``git merge origin/main --no-ff``），那些不是「合上游主干的这一条」。

    条目的形状可以是 dataclass（``SpecItem``）也可以是映射 —— 两种都认。

    Args:
        item: 要求书条目。

    Returns:
        ``True`` 表示这一条就是那一条上游合并命令。
    """
    command = _field(item, "command")
    if command is None or isinstance(command, (str, bytes)):
        return False
    try:
        argv = tuple(str(token) for token in command)
    except TypeError:
        return False
    return argv == UPSTREAM_MERGE_COMMAND


def _raise_if_modified(
    root: Path,
    before: _WorkingTree,
    allowed: Sequence[str],
    item_id: str,
    cause: Optional[BaseException] = None,
    *,
    skip_product_hashes: bool = False,
) -> None:
    """条目跑完之后，工作区被改了就抛。

    Args:
        root: 仓库根目录。
        before: 条目跑之前的工作区状态。
        allowed: 允许改动的路径前缀。
        item_id: 条目 id —— 进异常消息。
        cause: 条目自己的异常（有的话，守卫的异常从它派生）。
        skip_product_hashes: 跳过产品代码哈希判据 —— 只给声明的上游合并
            用，语义见 :meth:`_WorkingTree.touched`。

    Raises:
        ProductCodeModified: 出现了越界改动。
    """
    after = _WorkingTree.capture(root)
    touched = after.touched(
        before, allowed, skip_product_hashes=skip_product_hashes
    )
    if not touched:
        return
    message = f"item {item_id!r} touched {', '.join(touched)}"
    diff = _diff_evidence(root, touched)
    if cause is None:
        raise ProductCodeModified(
            message, item_id=item_id, paths=tuple(touched), diff=diff
        )
    raise ProductCodeModified(
        message, item_id=item_id, paths=tuple(touched), diff=diff
    ) from cause


def _diff_evidence(root: Path, touched: Sequence[str]) -> str:
    """越界的**原文证据** —— 人可以自己复核的那一段。

    两条只读命令（``git status --porcelain`` 与 ``git diff --stat``）
    的输出**逐字**写进来，再加上被碰到的路径。写摘要而不是原文，
    等于让读者只能相信这个模块对越界的描述 —— 而 A5 要的是「git diff
    为空」这句话本身可核对。

    Args:
        root: 仓库根目录。
        touched: 被碰到的越界路径。

    Returns:
        证据原文；没有越界时是空串 —— 没被碰过就没有 diff 可展示。
    """
    if not touched:
        return ""
    status = _git_text_quiet(root, "status", "--porcelain")
    stat = _git_text_quiet(root, "diff", "--stat")
    return (
        "git status --porcelain:\n"
        f"{status.rstrip()}\n"
        "git diff --stat:\n"
        f"{stat.rstrip()}\n"
        f"touched: {', '.join(touched)}\n"
    )


def _git_text_quiet(root: Path, *args: str) -> str:
    """一条只读 git 命令的输出 —— 命令失败时是空串，不是异常。

    Args:
        root: 仓库根目录。
        args: git 的参数（不含 ``git`` 本身）。

    Returns:
        命令的标准输出；命令失败时是空串。
    """
    try:
        return _git_text(root, *args)
    except RuntimeError:
        return ""


def product_guard_root(repo: Any) -> Optional[Path]:
    """守卫要守的那个仓库根目录 —— 不在仓库里就是 ``None``。

    调用方（编排器）用它先问一句「这里有没有产品代码要守」：没有
    仓库就没有产品代码，守卫无从谈起，所以这里返回 ``None`` 而不是
    抛异常。守卫本身仍然只对**真的**仓库负责。

    Args:
        repo: 仓库里的任意目录；``None`` 表示当前工作目录。

    Returns:
        仓库根目录；``repo`` 不在 git 仓库里时是 ``None``。
    """
    try:
        return _repository_root(repo)
    except RuntimeError:
        return None


def _repository_root(repo: Any) -> Path:
    """被校验仓库的根目录。

    Args:
        repo: 仓库里的任意目录；``None`` 表示当前工作目录。

    Returns:
        仓库根目录。

    Raises:
        RuntimeError: 这里不是一个 git 仓库 —— 校验不了就不当没看见。
    """
    start = Path(repo) if repo is not None else Path.cwd()
    completed = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{start} 不在 git 仓库里 —— 无法校验产品代码零改动"
        )
    return Path(completed.stdout.strip())


def _allowed_prefixes(plan_id: Optional[str]) -> Tuple[str, ...]:
    """允许改动的路径前缀 —— 收尾产物目录。

    Args:
        plan_id: 计划 id；``None`` 表示没有允许范围。

    Returns:
        路径前缀元组；没有允许范围时是空元组。
    """
    if not plan_id:
        return ()
    return (f"{FINISHING_ARTIFACT_ROOT}/{plan_id}",)


def _within(path: str, allowed: Sequence[str]) -> bool:
    """这个路径落在允许范围之内吗。

    Args:
        path: 仓库相对路径。
        allowed: 允许的路径前缀。

    Returns:
        ``True`` 表示落在允许范围之内。
    """
    for prefix in allowed:
        normalized = prefix.rstrip("/")
        if path == normalized or path.startswith(normalized + "/"):
            return True
    return False


def declared_product_paths(repo: Path) -> Optional[Tuple[str, ...]]:
    """读**项目自己声明**的产品代码路径清单。

    位置：``<repo>/.pdt/product_paths`` —— ``.pdt/`` 是框架已有的、被
    gitignore 的项目级定制目录（``.pdt/evidence``、``.pdt/service-locks``
    就在那儿），清单放那儿最自然。

    为什么由项目声明：每个项目的源码目录名不一样，框架不可能全知道，也不
    该猜 —— 猜出来的清单要么漏掉真正的产品代码，要么把产物误判成产品代码。

    格式：纯文本，一行一个仓库相对路径前缀（``src`` 或 ``frontend/components``
    都行）；``#`` 开头是注释；空行忽略；行首行尾空白去掉。绝对路径、``..``、
    ``~`` 一律**跳过该行**并记一条 warning —— 一份写坏的文件不该让收尾阶段
    整个瘫掉。

    Args:
        repo: 仓库根目录。

    Returns:
        声明的路径前缀（元组，保持文件里的顺序）；文件不存在或解析后为空时
        返回 ``None`` —— 表示「没声明」。
    """
    declaration = Path(repo) / PRODUCT_PATHS_FILE
    try:
        text = declaration.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        logger.warning(
            "读不到 %s（%s），按缺省产品代码路径判定", declaration, error
        )
        return None

    paths: List[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.rstrip("/")
        if not line:
            continue
        if not _is_repo_relative_prefix(line):
            logger.warning(
                "%s 第 %d 行不是仓库相对路径前缀（%r），跳过",
                declaration,
                number,
                line,
            )
            continue
        if line not in paths:
            paths.append(line)
    if not paths:
        return None
    return tuple(paths)


def _is_repo_relative_prefix(line: str) -> bool:
    """这一行是不是一个仓库相对路径前缀。

    Args:
        line: 已经去掉空白的一行。

    Returns:
        ``True`` 表示它是仓库相对路径前缀；绝对路径、``..``、``~`` 一律
        ``False``。
    """
    if line.startswith("/") or line.startswith("~"):
        return False
    parts = line.split("/")
    return ".." not in parts and "~" not in parts


def product_paths(repo: Path) -> Tuple[str, ...]:
    """这个仓的产品代码路径 —— 项目声明的优先，否则用缺省值。

    项目在 ``<repo>/.pdt/product_paths`` 里声明了就用项目的（**覆盖**缺省
    值，不是并集 —— 项目最清楚自己的源码在哪）；没声明才退回
    :data:`PRODUCT_CODE_PATHS`，并打一条 warning，让运维知道该去建那个文件。

    Args:
        repo: 仓库根目录。

    Returns:
        仓库相对路径前缀元组；不会为空。
    """
    declared = declared_product_paths(repo)
    if declared:
        return declared
    logger.warning(
        "项目没有声明 %s（%s），按缺省产品代码路径 %s 判定；"
        "建议在该项目里创建这个文件，一行一个产品代码目录。",
        PRODUCT_PATHS_FILE,
        Path(repo) / PRODUCT_PATHS_FILE,
        list(PRODUCT_CODE_PATHS),
    )
    return PRODUCT_CODE_PATHS


def _protected_new_path(path: str, allowed: Sequence[str]) -> bool:
    """未跟踪也要拦的路径：收尾产物根目录下、但不在本计划的目录里。

    **别的计划的收尾产物目录不许被写** —— 新建也不行。收尾阶段只允许写
    ``plans/{本 plan}/``；写到别的计划目录里，不管那个文件是新建的还是
    改动已跟踪的，都是越界。

    Args:
        path: 仓库相对路径。
        allowed: 允许改动的路径前缀（本计划的收尾产物目录）。

    Returns:
        ``True`` 表示这个路径落在收尾产物根目录下、但不在允许范围之内。
    """
    root = FINISHING_ARTIFACT_ROOT.rstrip("/")
    return _within(path, (root,)) and not _within(path, allowed)


def _porcelain_paths(text: str) -> List[str]:
    """``git status --porcelain -z`` 的输出 → 路径清单。

    ``-z`` 让 git 不做路径引用，所以这里切出来的就是仓库相对路径。
    重命名与复制会占两个字段：先是新路径，紧跟着原路径 —— 两个都算被
    碰过。

    Args:
        text: porcelain 的原始输出。

    Returns:
        路径清单。
    """
    fields = [field for field in text.split("\0") if field]
    paths: List[str] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4:
            continue
        status = field[:2]
        paths.append(field[3:])
        if "R" in status or "C" in status:
            if index < len(fields):
                paths.append(fields[index])
                index += 1
    return paths


def _git_text(root: Path, *args: str) -> str:
    """在仓库里跑一条只读的 git 命令，返回它的标准输出。

    Args:
        root: 仓库根目录。
        args: git 的参数（不含 ``git`` 本身）。

    Returns:
        命令的标准输出。

    Raises:
        RuntimeError: 命令失败了。
    """
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败：{completed.stderr.strip()}"
        )
    return completed.stdout


def _sha256(path: Path) -> str:
    """一个文件的 sha256。

    Args:
        path: 文件路径。

    Returns:
        十六进制摘要。
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
