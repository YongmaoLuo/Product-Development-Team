#!/usr/bin/env python3
"""VP 子 agent 的可写范围护栏 —— 只放行 plan 目录与临时目录。

背景（2026-11-02）
------------------
VP-055 的子 agent 直接改写了**被验证仓库**的 ``backend/verification_agent.py``。

现有 9 条 PreToolUse 钩子（``verification_subagent._HOOK_RULES``）里唯一管
写入路径的是 ``exec_path_block``：它把 ``PDT_PROJECT_DIR`` 整个目录算作合法
写入范围。对**执行**子 agent 来说那是它的家；对**验证**子 agent 来说，被
验证仓库恰恰是它只读的对象。于是"验证"这件事可以被"验证"自己改掉 ——
守卫在、守卫放行、结果不作数。

这条护栏换一个方向：不问"能不能写进 project_dir"，只问"是不是写进了 plan
目录或临时目录"。写别处 —— 包括被验证仓库的源码 —— 一律 exit 2（Edit/Write
这条路上；Bash 这条路见下面"只放行"那一节）。

它是一个**独立**的钩子脚本（不挂在 ``_HOOK_RULES`` 里），因为它的开关是
环境变量而不是规则表：``PDT_VP_WRITE_SCOPE`` 没设就完全不生效。旧调用方
（执行子 agent、refiner、各类自己写 settings.json 的地方）因此一行不改也
不受影响；执行器想要这条约束时，只需要在自己的子进程环境里设上它。

四个环境变量
------------
``PDT_VP_WRITE_SCOPE``
    打开护栏，同时给出**唯一**的可写根（plan 目录）。没设或设成空串 →
    护栏不生效，exit 0。
``PDT_VP_ID``
    拒绝证据的归属（``VP-055``）。缺省 ``unknown``。
``PDT_VP_ROUND``
    轮次号，决定证据落在 ``round-<n>/`` 下面。缺省 ``0``。

四条边界
--------
* plan 目录内 / ``/tmp`` / ``/private/tmp`` / ``tempfile.gettempdir()`` → exit 0。
* payload 里没有 ``file_path`` → exit 0（没有写入目标就谈不上越界）。
* 既不在 plan 也不在 tmp → exit 2，reason ``outside_scope``。
* 拒绝证据写失败 → **仍然** exit 2。写不进去只是"这次没留下痕"，不是
  "这次放行"；原因记到 stderr。

Bash 分支（2026-11-05 起只放行）
-------------------------------
只拦 Edit/Write 是不够的：子 agent 拿不到 Edit/Write 也能改源码 ——
``sed -i``、``> file``、``git checkout -- file`` 都写得动。所以同一个脚本早先
也接了一条 Bash 分支，去**猜**这条命令会把文件写到哪里。

**猜的那条路已经拆掉了。** 判定路径上不再有命令解析器：:func:`check_bash_payload`
恒返回 0。理由是解析器会持续产生假阳性 —— 它把 Python 代码里的 ``>``
（``if a > len(b):``）当成 shell 重定向，把 ``len`` / ``1:`` / ``index`` /
``single`` 当成写目标，于是"被验证仓库一个字节都没改过"的 VP 被拒绝、被改判成
FAILED（``failure_class=infra``）。猜错的代价是误杀，误杀比漏看贵得多。

现在这条命令会怎样？**放行，然后测量。** PreToolUse 先给被验证树拍一张快照，
命令跑完之后 PostToolUse 再拍一张，两张快照做差就是"这次改了哪些文件" ——
那是**量出来的**，不是猜出来的。测出改动就存证、回滚、记账
（``verification_tree_guard.py``）。

解析器本身没有删：:func:`bash_write_targets` 及其同伴仍然是纯函数，其它测试
仍然 import 它们、仍然断言它们的输出（包括 heredoc 正文不算命令这条规矩）。
**它们只是不在判定路径上了。**

Edit/Write 分支不受影响：越界写仍然 exit 2，仍然在 marker 里留下证据。

拒绝证据
--------
``<plan>/vp_write_scope/refusals/round-<n>/<vp_id>.jsonl`` —— 一行一条 JSON，
追加写。放在 plan 目录里而不是临时目录：临时目录会被清掉，而"这个 VP 试图
改被验证仓库"正是事后要查的事。

每条记录里还有 ``command``（原始命令行，截断到 500 字符）：只有落点的话，
事后看不出"是哪条命令想写它"。

路径比较一律先 ``resolve()``：``/tmp`` 在 macOS 上是 ``/private/tmp`` 的
软链，``plan/../..`` 也是逃逸的经典写法 —— 不解析符号链接的比较等于没比。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

__all__ = [
    "Decision",
    "bash_write_targets",
    "check_bash_payload",
    "check_edit_payload",
    "is_inside_tree",
    "main",
    "read_refusals",
    "record_refusal",
    "refusal_dir",
]

#: 打开护栏、同时给出唯一可写根（plan 目录）的环境变量。
SCOPE_ENV = "PDT_VP_WRITE_SCOPE"

#: 拒绝证据的归属：哪个 VP。
VP_ID_ENV = "PDT_VP_ID"

#: 拒绝证据的轮次。
ROUND_ENV = "PDT_VP_ROUND"

#: 越界写入的机器可读原因。
REASON_OUTSIDE_SCOPE = "outside_scope"

#: 拒绝证据相对 plan 目录的位置。
REFUSAL_SUBDIR = ("vp_write_scope", "refusals")

#: 临时目录的根。``/tmp`` 与 ``/private/tmp`` 都列上：macOS 上前者是后者
#: 的软链，Linux 上只有前者；``tempfile.gettempdir()`` 还要算进去，因为
#: ``$TMPDIR`` 可以把临时目录挪到别处（macOS 默认就是 ``/var/folders/...``）。
#: ``nosec B108`` —— 硬写的临时目录**就是**这里要的东西：这是一份**放行**
#: 名单，不是往临时目录里写文件的地方（证据落在 plan 目录，见
#: :func:`refusal_dir`）。
_TMP_ROOTS = ("/tmp", "/private/tmp")  # nosec B108

#: 识别 shell 重定向操作符。``fd`` 是可选的文件描述符（``2>``），``op`` 是
#: 操作符本身（``>`` / ``>>`` / ``&>`` / ``&>>`` / ``>|`` / ``>|>``）。
#: 目标紧跟在操作符后面时要另取（``> file`` 与 ``>file`` 是两种写法）。
#:
#: 顺序要紧：**长先行**。``>[|]>`` 必须排在 ``>[|]`` 前面 —— 否则 ``>|>``
#: 只会被认成 ``>|``，尾巴上多出一个 ``>``；``>>[|]`` 必须排在 ``>>`` 前面，
#: 同理。管道符号写成 ``[|]`` 而不是 ``\|``：``\|`` 在正则里跟"或"长得
#: 一样，交给人读会数错交替项的边界。``>|`` 是 bash 的 clobber 写法
#: （``>| file`` 就是写 ``file``），漏了它就等于给"越界 clobber"留了一条路。
_REDIRECT_RE = re.compile(r"(?P<fd>\d*)?(?P<op>&>>|&>|>>[|]|>[|]>|>[|]|>>|>)")

#: 识别 heredoc 操作符（``<<`` / ``<<-``）。``<<<`` 是 here-string，**不是**
#: heredoc —— 它不吞掉后面的行，所以前后都得排掉 ``<``：前一个字符不能是
#: ``<``（否则 ``<<<`` 的第二对也会被认成操作符），后一个字符不能是 ``<``
#: （否则 ``<<<`` 的第一对会被认成操作符）。
#:
#: ``dash`` 是 ``<<-`` 那个 ``-``：它表示正文行首的 tab 会被去掉，分隔符
#: 行也因此要按同样的规则比较。
_HEREDOC_RE = re.compile(r"(?<![<>])<<(?P<dash>-?)(?![<>])")

#: shell 的命令分隔符。token 化之后它们单独成 token。
_SHELL_SEPARATORS = (";", "&&", "||", "|", "&", "(", ")")

#: 这些命令只是"包一层"，真正的命令在它们后面（``env FOO=1 cmd``）。
_COMMAND_PREFIXES = (
    "env",
    "sudo",
    "command",
    "nohup",
    "time",
    "nice",
    "ionice",
    "stdbuf",
)

#: 会改工作区或仓库状态的 git 子命令，一律按"写仓库"处理。
_GIT_WRITE_SUBCOMMANDS = (
    "checkout",
    "restore",
    "reset",
    "stash",
    "apply",
    "add",
    "commit",
)


class Decision(NamedTuple):
    """一次 Edit/Write 的裁决。

    Attributes:
        allowed: 放行（``True``）还是拒绝（``False``）。
        reason: 拒绝原因；放行时是空串。机器可读，不给人读。
    """

    allowed: bool
    reason: str


def _resolve(path_str: str) -> str:
    """Return the absolute, symlink-resolved form of ``path_str``.

    解析失败（符号链接成环、NUL 字节等）时退回 ``abspath``：宁可拿一个
    没解析的绝对路径去比，也不要在这一步抛异常 —— 抛异常等于护栏失灵。
    """
    expanded = os.path.expanduser(str(path_str))
    try:
        return str(Path(expanded).resolve())
    except (OSError, RuntimeError, ValueError):
        return os.path.abspath(expanded)


def _is_inside(target: str, root: str) -> bool:
    """Whether ``target`` is ``root`` itself or lives under it.

    比的是解析之后的路径，所以 ``plan/../elsewhere`` 逃不出去。按分隔符
    收尾，所以 ``/tmp-evil`` 不会被当成 ``/tmp`` 里的东西。
    """
    if not target or not root:
        return False
    if target == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return target.startswith(prefix)


def is_inside_tree(path: Any, root: Any) -> bool:
    """Whether ``path`` lies inside ``root`` once both are resolved.

    护栏拒绝的是"写出可写范围"的写，而"写出了可写范围"和"改了被验证的树"
    **不是一回事** —— 写 agent 自己的 memory 目录也被拒，但它动不了被验证
    仓库。调用方要分清这两件事，就得能问"这个路径在树内吗"。

    比较前两边都解析：``~`` 展开、``..`` 消解、符号链接解掉。护栏记下的
    Edit / Write 路径是**原样**的（可能带 ``~``、可能是相对路径），Bash
    分支记的是解析过的绝对路径 —— 不先解析就没法把两者放在一起比。

    Args:
        path: The path to test; may be relative or carry a ``~``.
        root: The tree the path is compared against.

    Returns:
        ``True`` when ``path`` is ``root`` itself or lives under it.
    """
    if not path or not root:
        return False
    return _is_inside(_resolve(str(path)), _resolve(str(root)))


def scope_root() -> Optional[Path]:
    """The single writable root, or ``None`` when the guard is off.

    没设 / 设成空串 → ``None``（护栏不生效）。这是旧调用方的兼容路径：
    它们不认识这个变量，所以不能因为认识它而被拦下。
    """
    raw = os.environ.get(SCOPE_ENV, "")
    if not raw.strip():
        return None
    return Path(_resolve(raw))


def tmp_roots() -> List[str]:
    """Every scratch root this platform counts as temporary space."""
    roots: List[str] = []
    for raw in list(_TMP_ROOTS) + [tempfile.gettempdir()]:
        if not raw:
            continue
        resolved = _resolve(raw)
        if resolved and resolved not in roots:
            roots.append(resolved)
    return roots


def _target_path(payload: Any) -> str:
    """The path the payload wants to write, or ``""`` when it names none."""
    if not isinstance(payload, dict):
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    raw = tool_input.get("file_path") or ""
    return str(raw) if raw else ""


def _tool_name(payload: Any) -> str:
    """The tool the payload asks for, for the refusal record."""
    if not isinstance(payload, dict):
        return "unknown"
    return str(payload.get("tool_name") or payload.get("tool") or "unknown")


def check_edit_payload(payload: Any) -> Decision:
    """Decide whether the payload's write is inside the writable scope.

    Args:
        payload: The hook payload the SDK sent on stdin.

    Returns:
        A :class:`Decision`. Allowed writes carry an empty reason; refused
        ones carry ``outside_scope``.
    """
    root = scope_root()
    if root is None:
        # 护栏没开 —— 旧行为，一个字节都不改。
        return Decision(True, "")

    raw = _target_path(payload)
    if not raw:
        # 没有写入目标，就没有越界可言。
        return Decision(True, "")

    target = _resolve(raw)
    if _is_inside(target, str(root)):
        return Decision(True, "")
    for candidate in tmp_roots():
        if _is_inside(target, candidate):
            return Decision(True, "")
    return Decision(False, REASON_OUTSIDE_SCOPE)


# ---------------------------------------------------------------------------
# Bash 分支：算出命令的写目标，看有没有落在被验证仓库里
# ---------------------------------------------------------------------------
def _tokenize_with_state(
    text: str, state: Optional[str] = None
) -> Tuple[List[List[Tuple[str, bool]]], Optional[str]]:
    """Split one shell line into tokens, tracking the quote state across lines.

    每个 token 是 ``[(片段, 是否被引号包住)]`` 的列表。引号这件事必须留着：
    ``echo "a > b"`` 里的 ``>`` 是数据不是重定向，而"整块是不是被引号包住"
    分不开这两种情况 —— 所以按片段记，识别操作符时只看不带引号的片段。

    分隔符（``;`` ``|`` ``&`` ``(`` ``)``）单独成 token；``&>`` 不是分隔符，
    它是重定向操作符的一部分。

    ``state`` 是**这一行开始时**是否还处在一个没闭合的引号里（``None`` /
    ``"'"`` / ``'"'``）。跨行的引号在 bash 里是**一个**字符串：``python3 -c "``
    开了引号，下一行到闭引号为止都是**同一个参数的内容**，不是命令行。
    逐行独立切词会把那些行当成命令解析，于是 ``if a > len(b):`` 里的 ``>``
    被认成重定向，"写目标"凭空多出一个 ``<worktree>/len`` —— 护栏据此拒绝，
    两个没动过被验证仓库的 VP 被改判成 FAILED。

    所以这里返回 ``(tokens, 结束状态)``：结束状态是这一行结束时还开着的引号
    （``None`` 表示没有）。调用方把它带到下一行 —— heredoc 正文不在这个状态
    里（正文已在别处被整段跳过），所以正文里的引号不会打断外层的状态机。

    Args:
        text: 这一行的原文。
        state: 行首的引号状态。

    Returns:
        ``(tokens, 结束状态)``。
    """
    tokens: List[List[Tuple[str, bool]]] = []
    parts: List[Tuple[str, bool]] = []
    pending: List[str] = []

    def flush_pending() -> None:
        nonlocal pending
        if pending:
            parts.append(("".join(pending), False))
            pending = []

    def flush_token() -> None:
        nonlocal parts
        flush_pending()
        if parts:
            tokens.append(parts)
            parts = []

    def push(chunk: str, quoted: bool) -> None:
        flush_pending()
        parts.append((chunk, quoted))

    index = 0
    length = len(text)

    # 行首就处在引号里：先吃掉"到闭引号为止"的那一段 —— 它是数据。
    if state is not None:
        quote = state
        chunk: List[str] = []
        closed = False
        while index < length:
            current = text[index]
            if quote == '"' and current == "\\" and index + 1 < length:
                chunk.append(text[index + 1])
                index += 2
                continue
            if current == quote:
                index += 1
                closed = True
                break
            chunk.append(current)
            index += 1
        push("".join(chunk), True)
        if not closed:
            # 引号到行尾还没闭合：整行都是数据，行末仍处在这个引号里。
            return tokens, state
        state = None

    while index < length:
        char = text[index]
        if char in " \t":
            flush_token()
            index += 1
            continue
        if char == ";":
            flush_token()
            tokens.append([(";", False)])
            index += 1
            continue
        if char == "|":
            if pending and pending[-1] == ">":
                # ``>|`` / ``>>|``：管道符号是**重定向操作符的一部分**
                # （bash 的 clobber 写法），不是命令分隔符。它必须和
                # 前面那个 ``>`` 待在同一个片段里，操作符才认得出来。
                pending.append("|")
                index += 1
                continue
            flush_token()
            if text[index + 1 : index + 2] == "|":
                tokens.append([("||", False)])
                index += 2
            else:
                tokens.append([("|", False)])
                index += 1
            continue
        if char == "&":
            if text[index + 1 : index + 3] == ">>":
                # ``&>>`` 是一个操作符（stdout+stderr 追加），必须整块留下 ——
                # 拆成 ``&>`` 加一个 ``>`` 的话，目标会被认成 ``>`` 自己。
                push("&>>", False)
                index += 3
                continue
            if text[index + 1 : index + 2] == ">":
                push("&>", False)
                index += 2
                continue
            flush_token()
            tokens.append([("&", False)])
            index += 1
            continue
        if char in "()":
            flush_token()
            tokens.append([(char, False)])
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            push(text[index + 1], True)
            index += 2
            continue
        if char in "'\"":
            quote = char
            index += 1
            chunk = []
            closed = False
            while index < length:
                if quote == '"' and text[index] == "\\" and index + 1 < length:
                    chunk.append(text[index + 1])
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    closed = True
                    break
                chunk.append(text[index])
                index += 1
            if not closed:
                # 引号开了没闭：行尾剩下的都是数据，下一行仍在这个引号里。
                state = quote
            push("".join(chunk), True)
            continue
        if char == "`":
            index += 1
            chunk = []
            while index < length and text[index] != "`":
                chunk.append(text[index])
                index += 1
            index += 1
            push("".join(chunk), True)
            continue
        if char == "$" and text[index + 1 : index + 2] == "(":
            index += 2
            chunk = []
            depth = 1
            while index < length:
                current = text[index]
                if current == "(":
                    depth += 1
                elif current == ")":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                chunk.append(current)
                index += 1
            push("".join(chunk), True)
            continue
        pending.append(char)
        index += 1
    flush_token()
    return tokens, state


def _tokenize(text: str) -> List[List[Tuple[str, bool]]]:
    """Split one shell line into tokens (no cross-line quote state).

    单行独立切词 —— 调用方不关心引号状态时用它。跨行引号必须走
    :func:`_tokenize_with_state`。
    """
    tokens, _ = _tokenize_with_state(text)
    return tokens


def _text(token: List[Tuple[str, bool]]) -> str:
    """The literal text of a token."""
    return "".join(chunk for chunk, _ in token)


def _find_redirect(token: List[Tuple[str, bool]]) -> Optional[Tuple[str, str]]:
    """Return ``(前面, 后面)`` for the token's first unquoted redirect operator.

    ``前面`` 是操作符之前的文本（``x>file`` 里是 ``x``），``后面`` 是操作符
    之后紧跟的目标（``>file`` 里是 ``file``；``> file`` 里是空串，目标在
    下一个 token）。没有重定向操作符 → ``None``。
    """
    offset = 0
    for position, (chunk, quoted) in enumerate(token):
        if not quoted:
            match = _REDIRECT_RE.search(chunk)
            if match:
                head = _text(token)[: offset + match.start()]
                tail = chunk[match.end() :] + "".join(
                    part for part, _ in token[position + 1 :]
                )
                return head, tail
        offset += len(chunk)
    return None


def _segments(tokens: List[List[Tuple[str, bool]]]) -> List[List[List[Tuple[str, bool]]]]:
    """Split a token list into command segments at the separators."""
    segments: List[List[List[Tuple[str, bool]]]] = []
    current: List[List[Tuple[str, bool]]] = []
    for token in tokens:
        if len(token) == 1 and not token[0][1] and token[0][0] in _SHELL_SEPARATORS:
            segments.append(current)
            current = []
            continue
        current.append(token)
    segments.append(current)
    return segments


def _command_index(tokens: List[List[Tuple[str, bool]]]) -> Optional[int]:
    """The index of the segment's command word, or ``None`` when unclear."""
    for position, token in enumerate(tokens):
        text = _text(token)
        if not text:
            continue
        if "=" in text and text.split("=", 1)[0].isidentifier():
            continue  # VAR=value 前缀
        if text in _COMMAND_PREFIXES:
            continue
        if text.startswith("-"):
            return None  # 选项先于命令名：认不准就不猜
        return position
    return None


def _redirect_targets(tokens: List[List[Tuple[str, bool]]]) -> List[str]:
    """Every path the redirections in this segment write to."""
    targets: List[str] = []
    index = 0
    while index < len(tokens):
        found = _find_redirect(tokens[index])
        if found is None:
            index += 1
            continue
        head, tail = found
        if head and ("/" in head or ":" in head):
            # 前缀像路径或 URL —— 这是数据，不是粘在一起的重定向。
            index += 1
            continue
        if tail.startswith("&"):
            # ``2>&1`` 这样的描述符复制，不是文件。
            index += 1
            continue
        if tail:
            targets.append(tail)
            index += 1
            continue
        if index + 1 < len(tokens):
            following = _text(tokens[index + 1])
            if (
                following
                and following not in _SHELL_SEPARATORS
                and not _REDIRECT_RE.match(following)
            ):
                targets.append(following)
                index += 2
                continue
        index += 1
    return targets


def _sed_targets(args: List[str]) -> List[str]:
    """The files ``sed -i`` rewrites in place."""
    in_place = False
    has_expression = False
    operands: List[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-e", "--expression"):
            has_expression = True
            index += 2
            continue
        if arg.startswith("--expression="):
            has_expression = True
            index += 1
            continue
        if arg in ("-i", "--in-place") or arg.startswith("--in-place="):
            in_place = True
            index += 1
            # BSD 的写法：``sed -i '' 's/a/b/' file`` —— 紧跟在 ``-i`` 后面的
            # 空串是**备份文件后缀**，不是脚本。不认它，脚本就会被当成文件，
            # 凭空多出一个写目标（``<worktree>/s/a/b/``）。
            if arg == "-i" and index < len(args) and args[index] == "":
                index += 1
            continue
        if arg.startswith("-i") and len(arg) > 2:
            in_place = True  # -i.bak
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        operands.append(arg)
        index += 1
    if not in_place:
        return []
    if has_expression:
        return operands
    return operands[1:]  # 第一个操作数是脚本，不是文件


def _copy_targets(args: List[str], include_sources: bool = False) -> List[str]:
    """The paths ``cp`` / ``mv`` writes to.

    ``cp`` 只写目标；``mv`` 还写源 —— 文件从源那儿**没了**，对只读的仓库
    来说这也是一次写入（``mv agent.py /tmp/x`` 把仓库里的文件搬走了）。
    """
    operands: List[str] = []
    destination: Optional[str] = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-t":
            if index + 1 < len(args):
                destination = args[index + 1]
                index += 2
                continue
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        operands.append(arg)
        index += 1
    targets: List[str] = []
    if destination:
        targets.append(destination)
    elif len(operands) >= 2:
        targets.append(operands[-1])
    else:
        return []
    if include_sources:
        sources = operands if destination else operands[:-1]
        targets = sources + targets
    return targets


#: git 里取值要吃掉下一个词的选项 —— 它们的**值**不能当成 flag 读。
#:
#: ``git commit -m "--dry-run"`` 里的 ``--dry-run`` 是提交信息，不是 dry-run。
_GIT_VALUE_OPTIONS = (
    "-m",
    "-F",
    "--message",
    "--file",
    "--author",
    "--date",
    "--cleanup",
    "--trailer",
    "-t",
    "--template",
    "--squash",
    "--fixup",
    "-C",
    "--reuse-message",
    "--pathspec-from-file",
    "--git-path",
    "-S",
    "--gpg-sign",
)


#: git 里 ``-n`` **确实是** ``--dry-run`` 的子命令。
#:
#: ``git commit -n`` 是 ``--no-verify`` —— 它照样创建提交，不是 dry-run。
#: 护栏只认得 git 自己的语义：短选项 ``-n`` 只在这几个子命令里当 dry-run。
_GIT_DRY_RUN_SHORT_SUBCOMMANDS = ("add", "rm", "mv", "clean")


def _git_args_have_dry_run(args: List[str], subcommand: str) -> bool:
    """Whether a git subcommand's own arguments ask for a dry run.

    ``--dry-run`` 是 git 自己的 dry-run（``git commit --dry-run`` 也是
    dry-run）；短选项 ``-n`` **只在那几个子命令里**是 dry-run ——
    ``git add -n``、``git rm -n``、``git mv -n``、``git clean -n``。
    ``git commit -n`` 是 ``--no-verify``：**照样创建提交**，不能当 dry-run
    （当成 dry-run 就等于把一条真写命令放过）。``--`` 之后的词是 pathspec，
    不再是 flag。

    **建分支不是 dry-run。** ``git checkout -n -b feature`` 里的 ``-n`` 是
    ``--no-track``（只影响跟踪设置），而 ``-b`` 真的会写 ``.git`` —— 这种
    命令要照常当写处理。
    """
    index = 0
    creates_branch = False
    dry_run = False
    short_dry_run = False
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        if arg in _GIT_VALUE_OPTIONS:
            index += 2
            continue
        if arg == "--dry-run":
            dry_run = True
            index += 1
            continue
        if arg.startswith("--dry-run="):
            # ``--dry-run=false`` 把布尔量关掉 —— 那是**真写**。
            dry_run = arg.split("=", 1)[1].strip().lower() in ("1", "true", "yes", "on")
            index += 1
            continue
        if arg == "--orphan" or (
            arg.startswith("-")
            and not arg.startswith("--")
            and len(arg) > 1
            and ("b" in arg[1:] or "B" in arg[1:])
        ):
            creates_branch = True
            index += 1
            continue
        if arg.startswith("--"):
            index += 1
            continue
        if arg.startswith("-") and len(arg) > 1:
            # 短选项可以打包（``-An``），里面有 ``n`` 就**可能**是 dry-run ——
            # 是不是，看子命令（``commit`` 的 ``-n`` 不是 dry-run）。
            if "n" in arg[1:] and not arg.startswith("-c"):
                short_dry_run = True
            index += 1
            continue
        index += 1
    if creates_branch:
        return False
    if dry_run:
        return True
    return short_dry_run and subcommand in _GIT_DRY_RUN_SHORT_SUBCOMMANDS


def _git_targets(args: List[str], base: str) -> List[str]:
    """The paths a state-changing git subcommand writes to.

    **dry-run 不写。** ``git add -A --dry-run``（``-n``）只报告将要发生什么，
    工作区和仓库状态一个字都没动 —— 把它算成写目标，护栏就会拒掉一条纯只读
    的取证命令（``git add -A --dry-run 2>&1 | head -n 40``），VP 随后被框架
    改判成 ``verifier_mutated_tree``。所以同一条 git 命令带 ``--dry-run`` /
    ``-n`` 时，这里返回 ``[]``。

    ``-n`` 只在 git 里 **``-n`` 确实是 dry-run 的子命令**里当 dry-run
    （``add`` / ``rm`` / ``mv`` / ``clean``）；``git commit -n`` 是
    ``--no-verify``，**照样创建提交** —— 照常算写。其它命令（``cp -n``
    = no-clobber、``sed -n`` = 不打印）在各自的解析器里，行为不变。
    """
    index = 0
    repo = base
    dry_run = False
    short_dry_run = False
    while index < len(args):
        arg = args[index]
        if arg == "-C":
            if index + 1 < len(args):
                repo = args[index + 1]
                index += 2
                continue
            return []
        if arg.startswith("-C") and len(arg) > 2:
            repo = arg[2:]
            index += 1
            continue
        if arg in ("-c", "--exec-path", "--namespace"):
            index += 2
            continue
        if arg == "--dry-run":
            dry_run = True
            index += 1
            continue
        if arg.startswith("--dry-run="):
            # ``--dry-run=false`` 把布尔量关掉 —— 那是**真写**。
            dry_run = arg.split("=", 1)[1].strip().lower() in ("1", "true", "yes", "on")
            index += 1
            continue
        if arg.startswith("--"):
            index += 1
            continue
        if arg.startswith("-") and len(arg) > 1:
            # 短选项可以打包（``-An``），里面有 ``n`` 就**可能**是 dry-run ——
            # 是不是，看子命令（``commit`` 的 ``-n`` 不是 dry-run）。
            if "n" in arg[1:] and not arg.startswith("-c"):
                short_dry_run = True
            index += 1
            continue
        break
    if index >= len(args):
        return []
    subcommand = args[index]
    if subcommand not in _GIT_WRITE_SUBCOMMANDS:
        return []
    rest = args[index + 1 :]
    if dry_run:
        return []
    if short_dry_run and subcommand in _GIT_DRY_RUN_SHORT_SUBCOMMANDS:
        return []
    if _git_args_have_dry_run(rest, subcommand):
        return []
    # git 的相对路径是相对**仓库目录**的，不是相对 shell 的 cwd ——
    # ``git -C other/repo checkout -- a.py`` 动的是 other/repo/a.py。
    repo_root = repo if os.path.isabs(repo) else os.path.join(base or "", repo)
    if subcommand in ("checkout", "restore"):
        paths: List[str] = []
        if "--" in rest:
            paths = rest[rest.index("--") + 1 :]
        elif subcommand == "restore":
            paths = [arg for arg in rest if not arg.startswith("-")]
        if paths:
            return [
                path if os.path.isabs(path) else os.path.join(repo_root, path)
                for path in paths
            ]
    # 没有具体路径 —— 整棵工作区（以及仓库状态）都在写。
    return [repo_root]


def _command_targets(name: str, args: List[str], base: str) -> List[str]:
    """The paths a known write command writes to."""
    if name == "tee":
        return [arg for arg in args if not arg.startswith("-")]
    if name == "sed":
        return _sed_targets(args)
    if name in ("cp", "mv"):
        return _copy_targets(args, include_sources=(name == "mv"))
    if name == "dd":
        return [arg[3:] for arg in args if arg.startswith("of=") and len(arg) > 3]
    if name == "git":
        return _git_targets(args, base)
    return []


def _absolute(raw: str, base: str) -> Optional[str]:
    """Resolve a write target against ``base``; ``None`` when unresolvable."""
    if not raw or raw == "-" or raw.startswith("&"):
        return None
    if "$" in raw or "`" in raw:
        return None  # 变量展开：解析不出来就不猜
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        if not base:
            return None
        expanded = os.path.join(base, expanded)
    return os.path.normpath(expanded)


def _word_text(parts: Sequence[Tuple[str, bool]]) -> str:
    """The text of one shell word, cut at its first unquoted ``<`` / ``>``.

    一个 token 里的片段属于同一个词（token 是按空白切的），所以片段要接
    起来看；但引号外的 ``<`` / ``>`` 是重定向操作符，**不属于**这个词 ——
    ``<<EOF>x`` 的分隔符是 ``EOF``，``>x`` 是重定向。引号里的 ``<`` / ``>``
    是数据（``<<'a>b'`` 的分隔符就是 ``a>b``）。
    """
    pieces: List[str] = []
    for chunk, quoted in parts:
        if quoted:
            pieces.append(chunk)
            continue
        stop = len(chunk)
        for char in ("<", ">"):
            found = chunk.find(char)
            if found != -1:
                stop = min(stop, found)
        pieces.append(chunk[:stop])
        if stop < len(chunk):
            break  # 词到这儿就断了，后面的片段是另一个词
    return "".join(pieces)


def _heredoc_delimiters(
    tokens: List[List[Tuple[str, bool]]],
) -> List[Tuple[str, bool]]:
    """The heredoc delimiters in one tokenised line, in order of appearance.

    每个元素是 ``(分隔符, 是否去掉行首 tab)``。

    认得出来的写法：``<<EOF``、``<<'EOF'``、``<<"EOF"``、``<<\\EOF``、
    ``<<-EOF``（去 tab），操作符与分隔符之间允许有空白。``<<<`` 是
    here-string，不在这里面。

    分隔符是紧跟操作符的那个**词**：引号里的内容原样算进去（引号被去掉），
    引号外的重定向字符把词截断。
    """
    delimiters: List[Tuple[str, bool]] = []
    for position, token in enumerate(tokens):
        for part_index, (text, quoted) in enumerate(token):
            if quoted:
                continue
            cursor = 0
            while True:
                match = _HEREDOC_RE.search(text, cursor)
                if match is None:
                    break
                strip_tabs = bool(match.group("dash"))
                after = match.end()
                # 分隔符词里不能再有引号外的重定向字符。
                stop = len(text)
                for char in ("<", ">"):
                    found = text.find(char, after)
                    if found != -1:
                        stop = min(stop, found)
                remainder: List[Tuple[str, bool]] = [(text[after:stop], False)]
                # 同一个 token 里后面的片段属于同一个词（``<<'EOF'``）。
                remainder.extend(token[part_index + 1 :])
                word = _word_text(remainder)
                if not word and position + 1 < len(tokens):
                    # ``<< EOF``：分隔符是下一个 token。
                    word = _word_text(tokens[position + 1])
                delimiters.append((word, strip_tabs))
                cursor = stop
                if cursor >= len(text):
                    break
    return delimiters


def bash_write_targets(command: str, cwd: str) -> List[str]:
    """Every path the shell command writes to, resolved against ``cwd``.

    识别的写法：``>`` / ``>>`` / ``&>`` 重定向、``tee``、``sed -i``、
    ``cp`` / ``mv`` 的落点（``mv`` 的源也算：文件从源那儿没了）、``dd of=``、
    以及改了工作区或仓库状态的 git 子命令（``checkout`` / ``restore`` /
    ``reset`` / ``stash`` / ``apply`` / ``add`` / ``commit``）。

    **heredoc 的正文不是命令。** ``cat > /tmp/x <<'EOF' … EOF`` 里，从分隔符
    行到 ``EOF`` 之前都是**数据**（子 agent 写的脚本正文），不是 shell。
    逐行把正文当命令解析，正文里的 ``>``、``->``、``<tag>`` 就会被认成重定向，
    把一行注释认成一次写入 —— 于是"只写 /tmp"的命令被算成"写进了被验证
    仓库"。所以命令先按"命令行 / 正文"切开，只有命令行参与解析。

    **跨行的引号也是一个整体。** ``python3 -c "`` 开引号之后，到闭引号为止的
    每一行都属于**同一个参数**，不是命令行。逐行独立切词会把那些行当命令
    解析，里面的 ``if a > len(b):`` 里的 ``>`` 被认成重定向 —— 写目标凭空
    多出 ``<worktree>/len``，护栏据此拒绝，两个没动过被验证仓库的 VP 被改判
    成 FAILED。所以逐行切词时带着"这一行开始时还开着哪个引号"的状态走：
    闭引号之前的内容当数据，闭引号之后的部分才当命令行。

    Args:
        command: The shell command as the agent wrote it.
        cwd: The directory relative paths resolve against.

    Returns:
        Absolute, normalised paths. A command whose write targets cannot be
        worked out yields an empty list — the caller treats that as "nothing
        to refuse", because a guard that guesses is worse than one that
        stays quiet.
    """
    if not isinstance(command, str) or not command.strip():
        return []
    base = cwd or ""
    current = base
    targets: List[str] = []
    try:
        lines = command.splitlines() or [command]
        index = 0
        # 跨行引号状态：这一行开始时还开着哪个引号（``None`` 表示没有）。
        quote_state: Optional[str] = None
        while index < len(lines):
            line = lines[index]
            index += 1
            # 引号跨行时，闭引号之前的内容是**数据**，不是命令行 ——
            # ``python3 -c "`` 开引号之后到闭引号为止的每一行都属于同一个
            # 参数，里面的 ``>`` / ``->`` 是数据，不是重定向。
            tokens, quote_state = _tokenize_with_state(line, quote_state)
            # 运算符**那一行**照常解析：``cat > /tmp/x <<'EOF'`` 的
            # ``> /tmp/x`` 必须留住。
            for segment in _segments(tokens):
                position = _command_index(segment)
                if position is not None and _text(segment[position]) == "cd":
                    current = _cd_target(segment, position + 1, current)
                    continue
                raw_targets = _redirect_targets(segment)
                if position is not None:
                    name = os.path.basename(_text(segment[position]))
                    args = [
                        _text(token)
                        for token in segment[position + 1 :]
                        # 带重定向操作符的 token 不是操作数：``cp a b >out``
                        # 的落点是 ``b``，``2>&1`` 的 ``2>`` 也不是 cp 的
                        # 操作数 —— 把它们当操作数会凭空造出一个写目标。
                        if _find_redirect(token) is None
                    ]
                    raw_targets.extend(_command_targets(name, args, current))
                for raw in raw_targets:
                    resolved = _absolute(raw, current)
                    if resolved and resolved not in targets:
                        targets.append(resolved)
            # heredoc 的正文跟在命令行后面 —— 正文不是命令，逐行跳过。
            # 分隔符没出现（命令没闭合）时，剩下的行全是正文：bash 自己遇到
            # 没闭合的 heredoc 是语法错误、整条命令都不跑，所以这里**不**
            # 回退成逐行解析（回退等于把正文重新当成命令）。
            for delimiter, strip_tabs in _heredoc_delimiters(tokens):
                while index < len(lines):
                    candidate = lines[index]
                    index += 1
                    if strip_tabs:
                        candidate = candidate.lstrip("\t")
                    if candidate == delimiter:
                        break
    except Exception:  # noqa: BLE001 —— 解析不了就当没有写目标
        return []
    return targets


def _cd_target(segment: List[List[Tuple[str, bool]]], start: int, base: str) -> str:
    """The directory a ``cd`` in this segment moves to."""
    operands = [
        _text(token)
        for token in segment[start:]
        if not _text(token).startswith("-")
    ]
    if not operands:
        return os.path.expanduser("~")
    return os.path.normpath(
        operands[0]
        if os.path.isabs(operands[0])
        else os.path.join(base or os.path.expanduser("~"), operands[0])
    )


def _bash_command(payload: Any) -> str:
    """The shell command the payload wants to run, or ``""``."""
    if not isinstance(payload, dict):
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    raw = tool_input.get("command") or ""
    return raw if isinstance(raw, str) else str(raw)


def check_bash_payload(payload: Any) -> int:
    """Allow every Bash call — this branch no longer judges.

    Bash 的判定交给**测量**：命令先放行，跑完再拿被验证树的前后两张快照做差
    （``verification_tree_guard.py`` 的 pre/post 钩子）。测出改动就存证、回滚、
    记账 —— "改了哪些文件"是量出来的，不是从命令里猜出来的。

    所以这里恒返回 0。判定路径上不再有命令解析器：:func:`bash_write_targets`
    及其同伴（:func:`_redirect_targets` / :func:`_find_redirect` /
    :func:`_git_targets`）仍在 —— 它们是纯函数，别的测试还 import 它们、还
    断言它们的输出 —— 但不再被这里调用。解析器猜写目标时会把 Python 代码里的
    ``>`` 当成重定向，那种假阳性会把没动过被验证仓库的 VP 改判成 FAILED。

    Args:
        payload: The hook payload the SDK sent on stdin. 留着是为了让调用方
            （钩子、测试）不必改签名。

    Returns:
        ``0`` —— 永远放行。
    """
    return 0


def _round_number(value: Any) -> int:
    """Coerce a round number to an int; anything unusable becomes 0."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _safe_name(name: str) -> str:
    """A filesystem-safe stem for a marker file."""
    safe = "".join(
        ch if ch.isalnum() or ch in "-_" else "_" for ch in str(name)
    )
    return safe or "unknown"


def _vp_id() -> str:
    """The VP the refusals belong to."""
    return os.environ.get(VP_ID_ENV, "").strip() or "unknown"


def refusal_dir(plan_dir: Any, round_no: Any) -> Path:
    """The directory holding one round's refusal evidence.

    ``<plan_dir>/vp_write_scope/refusals/round-<n>/``
    """
    return (
        Path(str(plan_dir)).expanduser().joinpath(*REFUSAL_SUBDIR)
        / f"round-{_round_number(round_no)}"
    )


def record_refusal(
    tool: Any,
    path: Any,
    reason: Any,
    command: Optional[str] = None,
) -> Optional[Path]:
    """Append one refusal record; return the marker path, or ``None``.

    证据写失败**不抛异常**：调用方要的是"这次拒绝仍然 exit 2"，而写不进去
    只是一条 stderr。抛出去会让钩子变成"因为证据没写成所以放行"，那正好是
    这条护栏要防的反面。

    Args:
        tool: The tool that was refused.
        path: The path it wanted to write.
        reason: The machine-readable reason.
        command: The command line the agent ran, when there is one. It goes
            into the record (truncated) so that "what was it trying to do"
            can be answered afterwards from the evidence alone — a refusal
            that only names the path does not say which command caused it.

    Returns:
        The marker file that was written, or ``None`` when the evidence
        could not be recorded.
    """
    plan_dir = os.environ.get(SCOPE_ENV, "").strip()
    if not plan_dir:
        return None

    round_no = _round_number(os.environ.get(ROUND_ENV, ""))
    vp_id = _vp_id()
    directory = refusal_dir(plan_dir, round_no)
    marker = directory / f"{_safe_name(vp_id)}.jsonl"
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": str(tool or "unknown"),
        "path": str(path or ""),
        "reason": str(reason or REASON_OUTSIDE_SCOPE),
        "vp_id": vp_id,
        "round": round_no,
    }
    if command is not None:
        record["command"] = str(command)[:500]
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(marker, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(
            f"[pdt] could not record the write refusal marker: {exc}",
            file=sys.stderr,
        )
        return None
    return marker


def read_refusals(
    plan_dir: Any,
    round_no: Any,
    vp_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read one round's refusal records back.

    Args:
        plan_dir: The plan directory the refusals were recorded under.
        round_no: The round to read.
        vp_id: When given, only that VP's records; otherwise every VP's.

    Returns:
        The records, in the order they were appended. A marker line that is
        not valid JSON is skipped rather than fatal — a truncated last line
        from a crashed run must not cost the whole round's evidence.
    """
    directory = refusal_dir(plan_dir, round_no)
    if not directory.is_dir():
        return []

    if vp_id:
        candidates = [directory / f"{_safe_name(vp_id)}.jsonl"]
    else:
        candidates = sorted(directory.glob("*.jsonl"))

    records: List[Dict[str, Any]] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
    return records


def _load_payload(argv: Sequence[str]) -> Dict[str, Any]:
    """Read the hook payload from ``--payload`` or from stdin.

    解析不出来就当没有 payload（于是没有 ``file_path``，于是放行）：钩子
    拿到一份看不懂的输入时最该做的是别乱拦，但也别装作拦住了 —— 所以这里
    在 stderr 上留一句。
    """
    args = list(argv)
    raw = ""
    if "--payload" in args:
        index = args.index("--payload")
        raw = args[index + 1] if index + 1 < len(args) else ""
    else:
        try:
            raw = sys.stdin.read()
        except (OSError, ValueError):
            raw = ""

    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        print(
            "[pdt] could not parse the hook payload; allowing the call",
            file=sys.stderr,
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _wants_bash(args: Sequence[str], payload: Any) -> bool:
    """Whether this invocation belongs to the Bash branch of the guard.

    判据有两条，缺一不可地各管一半：

    * ``tool_name`` 是 ``Bash`` —— 钩子正常传进来的样子。
    * ``tool_input`` 里有 ``command`` —— 有的调用方只给
      ``{"tool_input": {"command": "..."}}``，没有 ``tool_name``。按这条判，
      这种 payload 才不会被当成 Edit/Write 放过去。

    ``--mode bash`` 可以显式点名，方便直接调用这条分支。
    """
    argv = list(args)
    if "--mode" in argv:
        index = argv.index("--mode")
        mode = argv[index + 1].strip().lower() if index + 1 < len(argv) else ""
        if mode:
            return mode == "bash"
    if _tool_name(payload).strip().lower() == "bash":
        return True
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    return isinstance(tool_input, dict) and bool(tool_input.get("command"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the guard once.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` to allow the call, ``2`` to block it.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    payload = _load_payload(args)
    if _wants_bash(args, payload):
        return check_bash_payload(payload)
    decision = check_edit_payload(payload)
    if decision.allowed:
        return 0

    tool = _tool_name(payload)
    path = _target_path(payload)
    marker = record_refusal(
        tool, path, decision.reason, command=_bash_command(payload),
    )
    scope = os.environ.get(SCOPE_ENV, "")
    print(
        "[pdt] refusing to write outside the VP write scope: "
        + path
        + ". The verification sub-agent may only write inside the plan "
        "directory ("
        + scope
        + ") or a temporary directory; the repository under verification is "
        "read-only for it."
        + (
            " Refusal recorded at " + str(marker) + "."
            if marker
            else " The refusal could NOT be recorded (see the line above)."
        ),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))