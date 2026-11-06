"""轮次作用域：本轮能不能复用上一轮 verdict 的纯函数判定。

为什么要有这个模块
------------------
验证点（VP）的 verdict 是一次复核的**结论**：它对"被复核的那份代码"负责，
不对"某一次运行"负责。历史实现把 verdict 存成一个不带轮次的列表，执行器按
``vp_id`` 去查 —— 于是上一轮 PASSED 的验证点在这一轮被当成"已经跑过"而直接
跳过：实测 27 个 VP 只跑了 7 个，却报出 27 条结果。

复用不是不能做，但**必须说得出凭什么**。本模块把"凭什么"写成四条可判定的
条件（:func:`may_reuse`），让执行器与编排器引用同一份口径：

1. 上一轮该 VP 的状态是 ``PASSED``；
2. 该 VP 声明了非空的 ``covers_paths``（它负责的范围）；
3. 本轮变更集与该范围**不相交**；
4. 本轮变更集不碰任何**共享面**。

四条全真才允许复用；任何一条为假 → 本轮真跑。默认是"真跑" —— 复用是例外，
例外必须能被这四条复核出来。

共享面
------
有些文件一改，几乎所有验证点的结论都可能失效（状态机、提示词、跑测试的脚本、
CI 工作流）。碰了它们就全量重跑，不再逐个判断范围。清单见
:data:`SHARED_SURFACE_PREFIXES`，逐字固定：目录项带尾斜杠（用 ``startswith``
匹配），文件项不带（用相等匹配）。

范围从哪来
----------
``covers_paths`` 是 VP 自己的属性，由它对应任务的 ``files_to_modify`` 派生
（见 :func:`covers_paths_for_files`），不允许模型自由发明路径 —— 否则"范围"
就成了模型说了算的事，复用判据随之失效。没有范围的 VP 每轮重跑。

本仓库没有独立的 migrations 目录：DDL 在 ``backend/state_machine/db/``，
已经被 ``backend/state_machine/`` 这条前缀覆盖。不要凭想象往清单里加路径。

这个模块有两半。

**判定内核**（:func:`may_reuse` 等）是纯函数：不读环境变量、不发网络请求、
不调模型、不读文件、不看时钟。它被执行器（编排层）与编排器同时引用，所以
两侧的口径必然一致 —— 口径分叉正是这个缺陷的成因。

**轮次记录**（:func:`record_round_scope` / :func:`load_round_scope` /
:func:`current_head_sha` / :func:`changed_files`）要落盘、要问 git：它把
"这一轮相对上一轮改了什么"记下来。判定内核吃的就是它算出来的 ``changed``，
所以变更集怎么算只有这一处说了算 —— 复用判据与变更集口径不会分叉。

变更集的两种"空"
-----------------
``changed`` 是空元组时，含义是**"不知道变了什么"**，不是"什么都没变"：
两个 sha 有一个读不出来（不是仓库、没装 git、git 太久）时，变更集就是未知
的。读记录的人必须按"未知"处理 —— 未知的一律全量重跑。同理，一轮的
``head_sha`` 或 ``prev_head_sha`` 为 ``None`` 时，那一轮的变更集不可信。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from utils.atomic_io import atomic_write_json

__all__ = [
    "SCOPE_FILE_NAME",
    "SCOPE_VERSION",
    "SHARED_SURFACE_PREFIXES",
    "changed_files",
    "covers_paths_for_files",
    "current_head_sha",
    "is_shared_surface",
    "load_round_scope",
    "may_reuse",
    "merge_change_sets",
    "normalise_prefix",
    "parse_porcelain",
    "record_round_scope",
    "scope_file_path",
]


#: 共享面清单（改动即全量重跑）。
#:
#: 目录项带尾斜杠、文件项不带 —— 匹配规则由这个约定决定：目录项用
#: ``startswith``，文件项用相等。清单逐字固定，改它需要一次显式的决定，
#: 不是"顺手补一条"。
SHARED_SURFACE_PREFIXES: Tuple[str, ...] = (
    "backend/state_machine/",
    "backend/phase_vocabulary.py",
    "backend/prompts.py",
    "scripts/run_tests.sh",
    "scripts/ci_gha.sh",
    ".github/workflows/",
)

#: "这不是文件路径"的哨兵值（与 ``backend/change_surface.py`` 的同名常量
#: 同值）。这里不 import 那个模块：本模块是纯判定内核，不该为一个常量拖进
#: 一个会读盘的模块。
_SENTINELS: Tuple[str, ...] = (
    "__UNKNOWN_MODIFICATIONS__",
    "__NO_FILE_CHANGES__",
)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _normalise_one(value: str) -> str:
    """归一化单个路径 / 前缀。

    做三件事：反斜杠转正斜杠、去掉尾部的 ``/``、去掉开头的 ``./``。
    """
    text = str(value).strip().replace("\\", "/")
    while text.endswith("/"):
        text = text[:-1]
    if text.startswith("./"):
        text = text[2:]
    return text


def _dedupe(values: Iterable[str]) -> Tuple[str, ...]:
    """去重且保序（按首次出现的顺序）。"""
    seen = set()
    kept: List[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        kept.append(value)
    return tuple(kept)


def _as_tuple(value: Union[str, Iterable[str], None]) -> Tuple[str, ...]:
    """把入参收成元组：``None`` → 空元组，裸字符串 → 单元素元组。"""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _unquote(value: str) -> str:
    """去掉 git 给"含特殊字符的路径"加的引号。"""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        inner = value[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return value


def _prefix_covers(prefix: str, path: str) -> bool:
    """``path`` 是否落在 ``prefix`` 覆盖的范围内。

    ``prefix`` 既可以是目录（``backend/verification``）也可以是文件
    （``backend/prompts.py``）。目录只匹配它自己加上它下面的东西 ——
    ``backend/verification_extra/x.py`` 不在 ``backend/verification`` 里。
    """
    if not prefix or not path:
        return False
    if path == prefix:
        return True
    return path.startswith(prefix + "/")


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def normalise_prefix(prefix: Union[str, Iterable[str]]) -> Union[str, Tuple[str, ...]]:
    """归一化前缀。

    单个前缀 → 归一化后的字符串；一组前缀 → 归一化后**去重且保序**的元组。

    归一化做三件事：反斜杠转正斜杠、去掉尾部的 ``/``、去掉开头的 ``./``。
    去掉尾斜杠是为了让同一个范围只有一种写法 —— 否则
    ``backend/verification`` 与 ``backend/verification/`` 会被当成两个范围，
    去重也就无从谈起。
    """
    if isinstance(prefix, str):
        return _normalise_one(prefix)
    return _dedupe(_normalise_one(item) for item in prefix if isinstance(item, str))


def covers_paths_for_files(files: Iterable[str]) -> Tuple[str, ...]:
    """从文件清单派生 ``covers_paths``：文件所在的目录，去重且保序。

    哨兵值（"这个任务不改文件" / "作者没能列出要改哪些文件"）不是路径，跳过。
    仓库根目录下的文件没有目录前缀 —— 它不属于任何子目录范围，因此不产生
    范围；没有范围的验证点每轮重跑，这是安全的一侧。
    """
    prefixes: List[str] = []
    for raw in files or ():
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text or text in _SENTINELS:
            continue
        path = _normalise_one(text)
        if not path:
            continue
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if not parent:
            continue
        prefixes.append(parent)
    return _dedupe(prefixes)


def parse_porcelain(text: str) -> Tuple[str, ...]:
    """解析 ``git status --porcelain`` 的输出，取出其中的文件路径。

    porcelain 每行的格式是 ``XY <path>``（``XY`` 是两位状态码）。重命名 / 复制
    写成 ``XY <old> -> <new>`` —— 两边都算改动：旧路径消失本身就是一次改动，
    只认新路径会漏掉"某个范围内的文件被搬走了"。
    """
    paths: List[str] = []
    for line in (text or "").splitlines():
        if len(line) < 4:
            continue
        rest = line[3:].strip()
        if not rest:
            continue
        if " -> " in rest:
            old, _, new = rest.partition(" -> ")
            paths.append(_unquote(old.strip()))
            paths.append(_unquote(new.strip()))
        else:
            paths.append(_unquote(rest))
    return _dedupe(_normalise_one(path) for path in paths)


def merge_change_sets(*groups: Iterable[str]) -> Tuple[str, ...]:
    """把多组变更集（diff 结果 ∪ 脏文件）合并成一个去重且保序的元组。"""
    merged: List[str] = []
    for group in groups:
        for raw in group or ():
            if not isinstance(raw, str):
                continue
            path = _normalise_one(raw)
            if path:
                merged.append(path)
    return _dedupe(merged)


def is_shared_surface(path: str) -> bool:
    """路径是否落在共享面上（改动即全量重跑）。

    清单里的目录项用 ``startswith`` 匹配、文件项用相等匹配。
    """
    text = _normalise_one(path) if isinstance(path, str) else ""
    if not text:
        return False
    for entry in SHARED_SURFACE_PREFIXES:
        if entry.endswith("/"):
            if text.startswith(entry):
                return True
        elif text == entry:
            return True
    return False


def may_reuse(
    prev_status: str,
    covers_paths: Sequence[str],
    changed: Sequence[str],
    shared_hit: bool,
) -> bool:
    """本轮能否复用上一轮该 VP 的 verdict：四条判据全真才复用。

    :param prev_status: 上一轮该 VP 的状态。必须是 ``PASSED`` —— ``SKIPPED``
        只在本轮内有效，不参与跨轮复用。
    :param covers_paths: 该 VP 声明的范围（前缀）。为空 → 一律重跑。
    :param changed: 本轮变更集（相对上一轮）。
    :param shared_hit: 变更集是否命中共享面。调用方按
        ``any(is_shared_surface(p) for p in changed)`` 预算好传进来；本函数
        还会自己复核一遍 —— 参数传错时不会静默放行。
    :return: True 表示本轮可以复用上一轮的结论。
    """
    if shared_hit:
        return False
    if str(prev_status or "").strip() != "PASSED":
        return False

    covers = _dedupe(
        _normalise_one(prefix) for prefix in _as_tuple(covers_paths) if isinstance(prefix, str)
    )
    if not covers:
        return False

    changes = merge_change_sets(_as_tuple(changed))
    if any(is_shared_surface(path) for path in changes):
        return False
    for prefix in covers:
        for path in changes:
            if _prefix_covers(prefix, path):
                return False
    return True


# ---------------------------------------------------------------------------
# 轮次记录：本轮 HEAD、上一轮 HEAD、本轮变更集
# ---------------------------------------------------------------------------

#: 记录文件落在 plan 目录下，名字逐字固定。
#:
#: 它是**派生数据**，不是状态机的真相来源：真相在 SQLite 里，这里只是把
#: "这一轮相对上一轮改了什么"留在盘上，供复用判定读。丢了可以从 git 重算，
#: 所以它不需要进状态机。
SCOPE_FILE_NAME = "verification_round_scope.json"

#: 记录文件的结构版本。改结构就改这里 —— 读的时候按版本认。
SCOPE_VERSION = 1

#: git 探测的超时（秒）。问 git 的代价必须封顶：问不出来就当"不知道"，
#: 不能让一轮验证卡在一个不响的 git 上。
_GIT_TIMEOUT_SECONDS = 10


def scope_file_path(plan_dir) -> Path:
    """记录文件的路径：``<plan_dir>/verification_round_scope.json``。"""
    return Path(plan_dir) / SCOPE_FILE_NAME


def _git_output(args: Sequence[str], cwd) -> Optional[str]:
    """跑一条 git 命令并返回 stdout；问不出来返回 ``None``。从不抛。

    问不出来有四种：git 没装、目录不是仓库、命令超时、命令非零退出。
    对调用方来说这四种是同一件事 —— "不知道"，而不是"轮次起不来"。
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _paths_from_lines(output: Optional[str]) -> Tuple[str, ...]:
    """把"一行一个路径"的 git 输出拆成路径元组。

    ``git diff --name-only`` 输出的是裸路径（不是 porcelain 的 ``XY 路径``），
    所以不能拿 :func:`parse_porcelain` 解析 —— 那个函数会砍掉路径的前三个
    字符。
    """
    paths: List[str] = []
    for line in (output or "").splitlines():
        text = line.strip()
        if not text:
            continue
        paths.append(_unquote(text))
    return _dedupe(_normalise_one(path) for path in paths)


def current_head_sha(repo_root) -> Optional[str]:
    """``repo_root`` 的 HEAD 指向的 commit。

    ``None`` = 问不出来：不是仓库、还没有 commit、git 没装、git 太慢。
    不抛 —— "不知道"是一个合法答案，抛异常不是。
    """
    output = _git_output(["rev-parse", "HEAD"], repo_root)
    if output is None:
        return None
    sha = output.strip()
    return sha or None


def changed_files(repo_root, prev_head_sha, head_sha) -> Tuple[str, ...]:
    """本轮变更集：两个 commit 之间的 diff ∪ 工作区的脏文件。

    diff 回答"这两轮之间提交了什么"，脏文件回答"还有哪些改动没提交"。
    两者都要：只看 diff 会漏掉没提交的工作区改动，只看脏文件会漏掉上一轮
    里已经提交过的改动。

    重命名按"删除 + 新增"记（``--no-renames``）：文件从 X 搬到 Y，X 的内容
    变了（少了）、Y 的内容变了（多了），两个路径都要进变更集 —— 只记新路径
    会让覆盖 X 的验证点以为"我没被碰过"。

    任一 sha 未知（``None`` 或空串）→ 变更集**未知**，返回空元组。空变更集
    在这里的含义是"不知道变了什么"，不是"什么都没变"。
    """
    if not prev_head_sha or not head_sha:
        return ()

    diff_output = _git_output(
        ["diff", "--no-renames", "--name-only", prev_head_sha, head_sha],
        repo_root,
    )
    diff_paths = _paths_from_lines(diff_output)

    # ``--no-optional-locks``:``git status`` 会去拿 index 的可选锁来刷新
    # stat 缓存。这棵树可能有别的进程在写，替它拿锁就是干扰 —— 而这个探测
    # 要回答的恰恰是"有没有人在改这棵树"。
    status_output = _git_output(
        ["--no-optional-locks", "status", "--porcelain"], repo_root
    )
    dirty_paths = parse_porcelain(status_output) if status_output is not None else ()

    return merge_change_sets(diff_paths, dirty_paths)


def _read_document(path: Path) -> Dict[str, Any]:
    """读回记录文件；读不出来或坏了就当空文档。

    写的时候会把整份文档重写一遍，所以"当空文档"意味着从这一轮重新记 ——
    旧的历史就此丢失，但那份历史本来就是坏文件，git 里的事实还在。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"version": SCOPE_VERSION, "rounds": {}}
    try:
        document = json.loads(raw)
    except (ValueError, TypeError):
        return {"version": SCOPE_VERSION, "rounds": {}}
    if not isinstance(document, dict):
        return {"version": SCOPE_VERSION, "rounds": {}}
    return document


def record_round_scope(
    plan_dir,
    round_number: int,
    *,
    head_sha: Optional[str],
    prev_head_sha: Optional[str] = None,
    changed: Iterable[str] = (),
) -> None:
    """记下某一轮的轮次作用域。

    :param plan_dir: plan 目录；记录文件写在它下面。
    :param round_number: 轮次号（1 起）。
    :param head_sha: 本轮的 commit。``None`` = 读不出来（见
        :func:`current_head_sha`）—— 记下来是"不知道"，不是"没有改动"。
    :param prev_head_sha: 上一轮的 commit。不给就从 ``rounds`` 里上一轮的
        记录取；那一轮不在记录里 → ``None``。
    :param changed: 本轮变更集（diff ∪ 脏文件，见 :func:`changed_files`）。

    **合并写入**：同一 plan 目录多次调用只往 ``rounds`` 里加/改那一轮，
    其它轮原样保留。

    **原子写**：整份文档写到临时文件再 ``os.replace`` 换过去（走
    :func:`utils.atomic_io.atomic_write_json`），所以读者要么看到旧文档，
    要么看到新文档，不会看到半截 JSON。
    """
    number = int(round_number)
    path = scope_file_path(plan_dir)

    document = _read_document(path)
    rounds = document.get("rounds")
    if not isinstance(rounds, dict):
        rounds = {}
        document["rounds"] = rounds

    if prev_head_sha is None:
        # 上一轮的 head_sha 是这一轮 prev_head_sha 的唯一来源。
        previous = rounds.get(str(number - 1))
        if isinstance(previous, dict):
            candidate = previous.get("head_sha")
            if isinstance(candidate, str) and candidate.strip():
                prev_head_sha = candidate.strip()

    entry: Dict[str, Any] = {"round": number, "head_sha": head_sha}
    if prev_head_sha is not None:
        entry["prev_head_sha"] = prev_head_sha
    change_list = list(merge_change_sets(changed))
    if change_list:
        entry["changed"] = change_list

    rounds[str(number)] = entry
    document["version"] = SCOPE_VERSION
    atomic_write_json(path, document, indent=2, ensure_ascii=False)


def load_round_scope(plan_dir, round_number: int) -> Optional[Dict[str, Any]]:
    """读回某一轮的轮次作用域；读不出来返回 ``None``。

    ``None`` 的四种情形：文件不存在、JSON 坏了、文件里没有 ``rounds`` 键、
    那一轮不在 ``rounds`` 里。

    **不做跨轮回落**：问第 3 轮却拿第 2 轮顶替，等于凭空造出一个变更集 ——
    那比"没有"危险得多（"没有"会被读成"这轮全量跑"，凭空造的会被读成
    "只改了这些"）。
    """
    number = int(round_number)
    path = scope_file_path(plan_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        document = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(document, dict):
        return None
    rounds = document.get("rounds")
    if not isinstance(rounds, dict):
        return None
    entry = rounds.get(str(number))
    if not isinstance(entry, dict):
        return None
    return entry