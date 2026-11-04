#!/usr/bin/env python3
"""树快照护栏 —— PreToolUse 拍快照、PostToolUse 比对。

背景（缺陷 A 第 2 条）
----------------------
现有钩子**只挂在 PreToolUse** 上（``verification_subagent._HOOK_RULES`` 的 12
条）：它们能挡住一条命令，但不能回答"这条命令**跑完之后**这棵树变成了什么样"。
"验证子 agent 说自己没改被验证仓库"是一句自述，不是一份证据。

这个脚本把两半接起来：

* **PreToolUse**（``PDT_TREE_GUARD_PHASE=pre``）—— 工具调用之前拍一份快照；
* **PostToolUse**（``PDT_TREE_GUARD_PHASE=post``）—— 工具调用之后拍一份快照，
  和之前那份比对，把" created / modified / deleted"落盘。

比对用的是 :mod:`tree_snapshot`（任务 1 的产物）：它只**测量**，不预测。

怎么用
------
::

    before = snapshot(root)     # PreToolUse
    ...                         # 工具跑
    after = snapshot(root)      # PostToolUse
    report = diff(before, after)

两个阶段共用一个脚本，靠环境变量 ``PDT_TREE_GUARD_PHASE`` 区分。payload 从
stdin 读（Claude Code 的钩子协议），JSON 解析不出来就当没有 payload。

三个环境变量
------------
``PDT_TREE_GUARD_PHASE``
    ``pre`` 或 ``post``。没设 → 当 ``pre``（拍快照是这脚本默认做的事）；是
    别的值 → 什么都不做，exit 0。
``PDT_PROJECT_DIR``
    **被观察的树**。快照拍的是它，比对比的也是它。没设 → exit 0。
``PDT_VP_ROUND``
    轮次号，决定证据落在 ``round-<n>/`` 下面。缺省 ``0``。

还有一个开关
------------
``PDT_VP_WRITE_SCOPE``
    没设（或空串）→ **不拍快照、exit 0**。这是旧调用方的兼容路径：执行子
    agent、refiner、各类自己写 settings.json 的地方都不认识这个变量，所以
    不能因为认识它而被拖下水。它在这里的作用和 ``verification_write_guard.py``
    里一样 —— 声明"这是一个声明了范围的验证子 agent"。

证据落在哪
----------
``<root>/.pdt/verify/round-<n>/`` —— ``<root>`` 是被观察的树。``.pdt/`` 是这个
仓库本来就有的运行时状态目录（``.gitignore`` 里已经排除了它），所以证据本身
不会变成"被验证仓库的变化"；报告里仍然把 ``.pdt/`` 下的路径剔掉，因为报告要
说的是**别人**改了什么。

``snapshot-<id>.json``
    一份快照（``root`` + 每个文件的 ``(size, mtime_ns, sha256)``）。
``compare-<id>.json``
    一次比对（``MutationReport`` 的 JSON 序列化：``created`` / ``modified`` /
    ``deleted``）。
``last.json``
    "上一次 pre 拍的是哪一份快照、比过了没有"。**缺 ``tool_use_id`` 时它才是
    配对的唯一线索** —— 那时快照叫 ``snapshot-_pending.json``，post 靠
    ``last.json`` 找回它。

改动被发现之后：存证 → 回滚 → 记账
----------------------------------
测出"这棵树被改了"之后，**先存证、再回滚、最后记账** —— 顺序不能换：patch 是
"改之前"和"改之后"两张快照之差，**回滚之后就重建不出来了**。

存证
    ``<root>/.pdt/evidence/mutations/round-<n>/<n>.patch`` —— tracked 的改动
    （``git diff HEAD --binary``）加上新建文件的内容（``git diff --no-index
    --binary /dev/null <path>``），所以这份 patch 是真的能 ``git apply`` 回去的。
回滚
    tracked 文件 ``git checkout -- <path>`` 还原；新建的 untracked 文件删除 ——
    **仅限树内、且不在 gitignore 里**。回滚失败（``git checkout`` 非 0）不抛
    异常，失败的路径记进账本的 ``restore_failed``。
记账
    ``<plan>/.pdt/vp_write_scope/mutations/round-<n>/<vp>.jsonl`` —— 一行一条
    JSON，追加写。判定层（``verification_agent``）读的就是它。

存证目录与账本目录都在 ``.pdt/`` 下面：证据是**观测**的产物，不是被观测的
内容 —— 它不进被验证树的可提交文件集合。

配对的三条边界
--------------
1. **payload 缺 ``tool_use_id``** → 快照写成 ``snapshot-_pending.json``，
   ``last.json`` 记住它；post 靠 ``last.json`` 配对。
2. **post 一直没来**（工具调用崩了、进程被杀了）→ 下一次 pre 先把上一份快照
   补比对了，再拍自己这份。补比对量的就是"上一条工具调用改了哪些文件"。
3. **stdin 不是合法 JSON、或者是空的** → exit 0。钩子看不懂输入时最该做的是
   别乱拦：它是一个**观测**钩子，不是一个**阻断**钩子。

任何异常都 exit 0 —— 同上，观测钩子坏了不该把正常的工具调用一起弄坏。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ``tree_snapshot`` 与本文件同目录。钩子把本脚本当作独立进程跑，所以同目录
# 得自己挂到 ``sys.path`` 上（``backend/file_lock_cli.py`` 是同一个做法）。
_BACKEND_DIR = str(Path(__file__).resolve().parent)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from tree_snapshot import (  # noqa: E402
    Entry,
    MutationReport,
    Snapshot,
    diff,
    restore,
    snapshot,
)

__all__ = [
    "compare_path",
    "evidence_dir",
    "guard_enabled",
    "last_path",
    "ledger_path",
    "main",
    "project_root",
    "read_mutations",
    "record_mutation",
    "restore_tree",
    "round_no",
    "snapshot_path",
    "verify_dir",
    "write_patch",
]

#: 阶段：工具调用之前。
PHASE_PRE = "pre"

#: 阶段：工具调用之后。
PHASE_POST = "post"

#: 阶段由这个环境变量决定。
PHASE_ENV = "PDT_TREE_GUARD_PHASE"

#: 护栏的开关。没设 → 整个脚本不动。
SCOPE_ENV = "PDT_VP_WRITE_SCOPE"

#: 被观察的树。
PROJECT_DIR_ENV = "PDT_PROJECT_DIR"

#: 轮次号。
ROUND_ENV = "PDT_VP_ROUND"

#: 缺 ``tool_use_id`` 时用的占位 id。
PENDING_ID = "_pending"

#: 证据目录相对被观察的树的位置。``.pdt/`` 是这个仓库本来就有的运行时状态
#: 目录（``.gitignore`` 里排除了它），所以证据不会变成"被验证仓库的变化"。
VERIFY_SUBDIR = (".pdt", "verify")

#: 护栏自己的记账目录。报告里要把这个前缀下的路径剔掉。
GUARD_STATE_DIR = ".pdt"

#: ``tool_use_id`` 会进文件名，所以只留安全的字符。
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]")

#: 文件名里的 id 最长这么长 —— 名字太长在某些文件系统上会写不进去。
_MAX_ID_LENGTH = 120

#: 存证目录（patch）相对被观察的树的位置。
EVIDENCE_SUBDIR = (".pdt", "evidence", "mutations")

#: 账本目录相对 plan 目录的位置。
LEDGER_SUBDIR = (".pdt", "vp_write_scope", "mutations")

#: 证据目录自己的 .gitignore —— 证据不进被验证树的可提交文件集合。
EVIDENCE_GITIGNORE = "*\n"

#: git 调用的硬上限。挂住的 git 不能把护栏一起挂住。
GIT_TIMEOUT_SEC = 30

#: 账本里 ``command`` 字段的长度上限 —— 命令原文，截断着存。
_COMMAND_MAX_LENGTH = 500

#: 归属的 VP。跟写护栏用的是同一个变量名。
VP_ID_ENV = "PDT_VP_ID"


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------


def guard_enabled() -> bool:
    """Whether the guard is switched on.

    没设 ``PDT_VP_WRITE_SCOPE`` → 护栏完全不生效。这是旧调用方的兼容路径：
    它们不认识这个变量，所以不能因为认识它而被拖下水。
    """
    return bool(os.environ.get(SCOPE_ENV, "").strip())


def project_root() -> Optional[Path]:
    """The tree to watch, or ``None`` when there is none.

    ``PDT_PROJECT_DIR`` 没设 → ``None``（没法观察一棵没指定的树）。
    """
    raw = os.environ.get(PROJECT_DIR_ENV, "").strip()
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def round_no() -> int:
    """The round the evidence belongs to.

    ``PDT_VP_ROUND`` 不是数字 → ``0``（证据落在 ``round-0/`` 下面）。
    """
    raw = os.environ.get(ROUND_ENV, "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def verify_dir(root: Path, round_number: int) -> Path:
    """Where this round's evidence lives."""
    return root.joinpath(*VERIFY_SUBDIR) / f"round-{round_number}"


def snapshot_path(root: Path, round_number: int, tool_use_id: str) -> Path:
    """The file one pre-phase snapshot is written to."""
    return verify_dir(root, round_number) / f"snapshot-{tool_use_id}.json"


def compare_path(root: Path, round_number: int, tool_use_id: str) -> Path:
    """The file one comparison is written to."""
    return verify_dir(root, round_number) / f"compare-{tool_use_id}.json"


def last_path(root: Path, round_number: int) -> Path:
    """The file that remembers the last pre-phase snapshot."""
    return verify_dir(root, round_number) / "last.json"


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def _safe_id(raw: str) -> str:
    """Turn a tool-use id into something safe to put in a filename.

    ``tool_use_id`` 是外部输入，而它会进文件名 —— 所以 ``/``、``..`` 这类
    字符必须先清掉，否则一份 payload 就能把证据写到目录外面去。
    """
    cleaned = _UNSAFE_ID_CHARS.sub("_", str(raw)).lstrip(".")
    if not cleaned:
        return PENDING_ID
    return cleaned[:_MAX_ID_LENGTH]


def snapshot_to_dict(snap: Snapshot) -> Dict[str, Any]:
    """Serialise a snapshot so it can be read back by the post phase."""
    return {
        "root": snap.root,
        "entries": {
            path: {
                "size": entry.size,
                "mtime_ns": entry.mtime_ns,
                "sha256": entry.sha256,
            }
            for path, entry in snap.entries.items()
        },
    }


def snapshot_from_dict(data: Any) -> Optional[Snapshot]:
    """Read a snapshot back, or ``None`` when the file is not one."""
    if not isinstance(data, dict):
        return None
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, dict):
        return None
    entries: Dict[str, Entry] = {}
    for path, value in raw_entries.items():
        if not isinstance(value, dict):
            continue
        try:
            size = int(value.get("size", 0))
            mtime_ns = int(value.get("mtime_ns", 0))
        except (TypeError, ValueError):
            continue
        sha256 = value.get("sha256")
        entries[str(path)] = Entry(
            size=size,
            mtime_ns=mtime_ns,
            sha256=str(sha256) if sha256 is not None else None,
        )
    return Snapshot(root=str(data.get("root", "")), entries=entries)


def report_to_dict(report: MutationReport) -> Dict[str, Any]:
    """Serialise a mutation report."""
    return {
        "created": list(report.created),
        "modified": list(report.modified),
        "deleted": list(report.deleted),
    }


def _without_guard_paths(report: MutationReport) -> MutationReport:
    """Drop the guard's own bookkeeping from a report.

    护栏把证据写在被观察的树里（``.pdt/`` 下面）。那些文件是**观测**的产物，
    不是被观测的变化 —— 报告里留着它们，读报告的人会以为子 agent 动过
    ``.pdt/``。
    """
    prefix = GUARD_STATE_DIR + "/"

    def keep(path: str) -> bool:
        return not path.startswith(prefix)

    return MutationReport(
        created=[path for path in report.created if keep(path)],
        modified=[path for path in report.modified if keep(path)],
        deleted=[path for path in report.deleted if keep(path)],
    )


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write one JSON document, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """Read one JSON document, or ``None`` when it is missing or broken."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def _load_payload(argv: Sequence[str]) -> Dict[str, Any]:
    """Read the hook payload from ``--payload`` or from stdin.

    解析不出来就当没有 payload —— 钩子拿到一份看不懂的输入时最该做的是别乱
    拦，但也别装作拦住了。
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
            "[pdt] could not parse the tree-guard payload; skipping the snapshot",
            file=sys.stderr,
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _payload_tool_use_id(payload: Any) -> Tuple[str, bool]:
    """The tool-use id of a payload, and whether it carried one.

    缺 id 时用 ``_pending`` 占位：快照仍然要拍（不然这一次工具调用就白跑了），
    只是没人能把它和某一次调用对上 —— ``last.json`` 记着它，post 靠它配对。
    """
    raw = ""
    if isinstance(payload, dict):
        raw = payload.get("tool_use_id") or ""
    text = str(raw).strip()
    if not text:
        return PENDING_ID, False
    return _safe_id(text), True


# ---------------------------------------------------------------------------
# 配对
# ---------------------------------------------------------------------------


def _last_record(root: Path, round_number: int) -> Optional[Dict[str, Any]]:
    """The ``last.json`` record, when there is one."""
    return _read_json(last_path(root, round_number))


def _write_last_record(
    root: Path,
    round_number: int,
    tool_use_id: str,
    snap_path: Path,
    compared: bool,
) -> None:
    """Record which snapshot the last pre phase took."""
    _write_json(
        last_path(root, round_number),
        {
            "tool_use_id": tool_use_id,
            "snapshot": str(snap_path),
            "phase": PHASE_PRE,
            "compared": compared,
        },
    )


def _find_snapshot(
    root: Path,
    round_number: int,
    tool_use_id: str,
    has_id: bool,
) -> Tuple[Optional[str], Optional[Path]]:
    """Find the snapshot the post phase has to compare against.

    Returns ``(id, path)`` — the id is the one the snapshot was taken under,
    so the comparison lands in ``compare-<id>.json``.

    找法有两条：

    * payload 带着 id → 直接找 ``snapshot-<id>.json``；
    * 找不到（或 payload 压根没带 id）→ 问 ``last.json``：它记着上一份 pre
      拍的是哪一份、还没比过没有。缺 id 时这是唯一的线索。
    """
    evidence = verify_dir(root, round_number)

    if has_id:
        candidate = evidence / f"snapshot-{tool_use_id}.json"
        if candidate.exists():
            return tool_use_id, candidate

    record = _last_record(root, round_number)
    if record:
        raw = str(record.get("snapshot") or "")
        candidate = Path(raw) if raw else None
        if candidate is not None and candidate.exists():
            # last.json 是自己写的，但它是磁盘上的文件 —— 别人改了它就不能
            # 跟着去读一个不在证据目录里的路径。
            if candidate.parent == evidence:
                return (
                    str(record.get("tool_use_id") or PENDING_ID),
                    candidate,
                )

    if not has_id:
        candidate = evidence / f"snapshot-{PENDING_ID}.json"
        if candidate.exists():
            return PENDING_ID, candidate

    return None, None


# ---------------------------------------------------------------------------
# 两个阶段
# ---------------------------------------------------------------------------


def _compare(
    root: Path,
    round_number: int,
    tool_use_id: str,
    before: Snapshot,
) -> Tuple[Path, MutationReport]:
    """Diff the tree against ``before`` and write the report out."""
    after = snapshot(root)
    report = _without_guard_paths(diff(before, after))
    out = compare_path(root, round_number, tool_use_id)
    _write_json(out, report_to_dict(report))
    return out, report


def _catch_up(
    root: Path,
    round_number: int,
    payload: Any = None,
) -> None:
    """Compare the previous snapshot, when nobody compared it yet.

    post 没来过（工具调用崩了、进程被杀了）时，"上一条工具调用改了哪些
    文件"就没人回答了。下一次 pre 是最后一个还能测到这件事的时刻 —— 这次
    工具调用还没跑，树还是上一条命令跑完时的样子。
    """
    record = _last_record(root, round_number)
    if not record or record.get("compared"):
        return

    raw = str(record.get("snapshot") or "")
    snap_file = Path(raw) if raw else None
    if snap_file is None or not snap_file.exists():
        return

    before = snapshot_from_dict(_read_json(snap_file))
    if before is None:
        return

    tool_use_id = str(record.get("tool_use_id") or PENDING_ID)
    _, report = _compare(root, round_number, tool_use_id, before)
    record["compared"] = True
    _write_json(last_path(root, round_number), record)
    # 补比对出来的东西同样要存证、回滚、记账 —— 命令崩了不等于树可以留着。
    _record_mutation(root, round_number, tool_use_id, before, report, payload)


def run_pre(
    root: Path,
    round_number: int,
    tool_use_id: str,
    payload: Any = None,
) -> int:
    """Take a snapshot of the tree before the tool call."""
    # 先补比对上一份 —— 顺序要紧：这一次的工具调用还没跑，树还是上一条命令
    # 跑完时的样子，那正是补比对要量的东西。
    _catch_up(root, round_number, payload)

    snap = snapshot(root)
    target = snapshot_path(root, round_number, tool_use_id)
    _write_json(target, snapshot_to_dict(snap))
    _write_last_record(root, round_number, tool_use_id, target, compared=False)

    print(
        json.dumps(
            {
                "phase": PHASE_PRE,
                "snapshot": str(target),
                "tool_use_id": tool_use_id,
            },
            ensure_ascii=False,
        )
    )
    return 0


def run_post(
    root: Path,
    round_number: int,
    tool_use_id: str,
    has_id: bool,
    payload: Any = None,
) -> int:
    """Compare the tree against the snapshot the pre phase took.

    测出改动之后是 **存证 → 回滚 → 记账** —— 见 :func:`record_mutation`。
    """
    paired_id, snap_file = _find_snapshot(root, round_number, tool_use_id, has_id)
    if snap_file is None:
        return 0

    before = snapshot_from_dict(_read_json(snap_file))
    if before is None:
        return 0

    out, report = _compare(root, round_number, paired_id or PENDING_ID, before)

    record = _last_record(root, round_number)
    if record and str(record.get("tool_use_id") or "") == str(paired_id or ""):
        record["compared"] = True
        _write_json(last_path(root, round_number), record)

    _record_mutation(
        root, round_number, paired_id or PENDING_ID, before, report, payload
    )

    print(
        json.dumps(
            {
                "phase": PHASE_POST,
                "compare": str(out),
                "tool_use_id": paired_id or PENDING_ID,
            },
            ensure_ascii=False,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# 存证 → 回滚 → 记账
# ---------------------------------------------------------------------------


def _git(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess:
    """Run ``git`` inside ``root``.

    退出码不在这儿判断 —— 每个调用方自己决定非 0 意味着什么。
    """
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
        timeout=GIT_TIMEOUT_SEC,
    )


def _git_text(proc: subprocess.CompletedProcess) -> str:
    """The stdout of a git call, decoded."""
    return (proc.stdout or b"").decode("utf-8", errors="replace")


def _git_error(proc: subprocess.CompletedProcess) -> str:
    """The stderr of a git call, decoded."""
    return (proc.stderr or b"").decode("utf-8", errors="replace").strip()


def _round_number(value: Any) -> int:
    """Coerce a round number to an int; anything unusable becomes 0."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _safe_vp_name(name: Any) -> str:
    """A filesystem-safe stem for a VP id."""
    safe = "".join(
        ch if ch.isalnum() or ch in "-_" else "_" for ch in str(name)
    )
    return safe or "unknown"


def evidence_dir(root: Path, round_number: int) -> Path:
    """Where the patch evidence of one round lives."""
    return Path(root).joinpath(*EVIDENCE_SUBDIR) / f"round-{round_number}"


def ledger_path(plan_dir: Any, round_number: Any, vp_id: Any) -> Path:
    """Where the mutation ledger of one round and one VP lives.

    ``<plan_dir>/.pdt/vp_write_scope/mutations/round-<n>/<vp>.jsonl``
    """
    return (
        Path(str(plan_dir)).expanduser().joinpath(*LEDGER_SUBDIR)
        / f"round-{_round_number(round_number)}"
        / f"{_safe_vp_name(vp_id)}.jsonl"
    )


def _ensure_ignored(directory: Path) -> None:
    """Make a directory invisible to git.

    证据写在被观察的树里 —— 它必须**不**进被验证树的可提交文件集合。目录里
    放一个 ``*`` 的 .gitignore，整棵目录树就被 git 忽略了，不管被验证仓库
    自己的 .gitignore 写没写 ``.pdt/``。
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / ".gitignore"
        if not marker.exists():
            marker.write_text(EVIDENCE_GITIGNORE, encoding="utf-8")
    except OSError as exc:
        print(
            f"[pdt] could not hide {directory} from git: {exc}",
            file=sys.stderr,
        )


def _untracked_paths(root: Path) -> List[str]:
    """The untracked, non-ignored files of the tree."""
    proc = _git(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    if proc.returncode != 0:
        return []
    paths: List[str] = []
    for chunk in (proc.stdout or b"").split(b"\0"):
        if not chunk:
            continue
        paths.append(chunk.decode("utf-8", errors="surrogateescape"))
    return paths


def _tracked_diff(root: Path) -> str:
    """The diff of everything git already knows about."""
    head = _git(root, ["rev-parse", "--verify", "HEAD"])
    if head.returncode == 0:
        proc = _git(root, ["diff", "HEAD", "--binary"])
        if proc.returncode == 0:
            return _git_text(proc)
    # 没有 HEAD（仓库还没有提交）：只能比工作区与索引。
    proc = _git(root, ["diff", "--binary"])
    if proc.returncode == 0:
        return _git_text(proc)
    raise RuntimeError(f"git diff failed in {root}: {_git_error(proc)}")


def _new_file_diff(root: Path, rel: str) -> str:
    """The diff that turns nothing into ``rel``."""
    proc = _git(root, ["diff", "--no-index", "--binary", "--", "/dev/null", rel])
    # ``--no-index`` 有差异时退出码是 1 —— 那不是错误，是"确实有差异"。
    return _git_text(proc)


def _next_patch_index(root: Path, round_number: int) -> int:
    """The next free patch number of one round."""
    numbers: List[int] = []
    try:
        entries = list(evidence_dir(root, round_number).glob("*.patch"))
    except OSError:
        entries = []
    for entry in entries:
        try:
            numbers.append(int(entry.stem))
        except ValueError:
            continue
    return max(numbers) + 1 if numbers else 1


def write_patch(root: Path, round_number: int, n: int) -> Path:
    """Write the tree's diff out as a patch file; return its path.

    存证必须在回滚**之前**：patch 是"改之前"和"改之后"两张快照之差，回滚
    之后就重建不出来了。

    Args:
        root: The tree that was changed.
        round_number: The round the evidence belongs to.
        n: The patch number inside that round.

    Returns:
        The patch file that was written. It holds the tracked diff, the
        contents of the files that were created, and the list of those
        files — so it can be applied back with ``git apply``.

    Raises:
        RuntimeError: When the diff could not be produced.
    """
    root = Path(root)
    out = evidence_dir(root, round_number) / f"{int(n)}.patch"
    out.parent.mkdir(parents=True, exist_ok=True)
    # 先把证据目录藏起来 —— 否则它自己会出现在"新建文件"清单里。
    _ensure_ignored(root / ".pdt")

    untracked = _untracked_paths(root)
    chunks = [
        "# verifier mutation evidence\n",
        f"# root: {root}\n",
        f"# round: {round_number}\n",
        "# created files: " + (", ".join(untracked) or "(none)") + "\n",
        _tracked_diff(root),
    ]
    for rel in untracked:
        chunk = _new_file_diff(root, rel)
        if chunk:
            chunks.append(chunk)

    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text("".join(chunks), encoding="utf-8")
    os.replace(tmp, out)
    return out


def _is_inside_tree(root: Path, rel: str) -> bool:
    """True when ``rel`` names a path inside ``root``.

    树外的路径不是这棵树的事 —— 删它就是删别人的东西。
    """
    if not rel or os.path.isabs(rel):
        return False
    base = Path(os.path.normpath(str(root)))
    target = Path(os.path.normpath(str(root / rel)))
    if target == base:
        return False
    try:
        target.relative_to(base)
    except ValueError:
        return False
    return True


def _is_ignored(root: Path, rel: str) -> bool:
    """True when git ignores ``rel``."""
    return _git(root, ["check-ignore", "-q", "--", rel]).returncode == 0


def restore_tree(
    root: Path,
    before: Optional[Snapshot],
    report: MutationReport,
) -> List[str]:
    """Put the tree back the way the snapshot found it.

    Args:
        root: The tree to restore.
        before: The snapshot the tree should be brought back to.
        report: The mutations to undo, as produced by :func:`diff`.

    Returns:
        The relative POSIX paths that could **not** be put back, sorted.
        A path git refused to check out is one of them —— 回滚失败要记下来，
        不是当作没发生过。
    """
    root = Path(root)
    before = before if before is not None else Snapshot(root=str(root), entries={})
    failed: List[str] = []

    tracked = sorted(set(report.modified) | set(report.deleted))
    if tracked:
        restored = set(
            restore(
                root,
                before,
                MutationReport(
                    modified=list(report.modified),
                    deleted=list(report.deleted),
                ),
            )
        )
        failed.extend(rel for rel in tracked if rel not in restored)

    for rel in report.created:
        # 护栏自己的记账目录、树外的路径、gitignore 里的路径 —— 都不是
        # "别人改了这棵树"，不许删。
        if rel.startswith(GUARD_STATE_DIR + "/"):
            continue
        if not _is_inside_tree(root, rel):
            continue
        if _is_ignored(root, rel):
            continue
        if rel not in restore(root, before, MutationReport(created=[rel])):
            failed.append(rel)

    return sorted(set(failed))


def _payload_root(data: Dict[str, Any]) -> Optional[Path]:
    """The tree the payload is about."""
    for key in ("root", "project_dir", "project_root"):
        raw = data.get(key)
        if not raw:
            continue
        try:
            return Path(str(raw)).resolve()
        except (OSError, RuntimeError, ValueError):
            return None
    return None


def _payload_paths(value: Any) -> List[str]:
    """The relative paths of one bucket of a report."""
    if not isinstance(value, (list, tuple)):
        return []
    return [
        str(item) for item in value if isinstance(item, (str, os.PathLike))
    ]


def _payload_report(data: Dict[str, Any]) -> MutationReport:
    """The mutation report the payload carries."""
    raw = data.get("report")
    if isinstance(raw, MutationReport):
        return raw
    source = raw if isinstance(raw, dict) else data
    return MutationReport(
        created=_payload_paths(source.get("created")),
        modified=_payload_paths(source.get("modified")),
        deleted=_payload_paths(source.get("deleted")),
    )


def _payload_before(data: Dict[str, Any], root: Optional[Path]) -> Snapshot:
    """The snapshot the tree should be brought back to."""
    raw = data.get("before")
    if isinstance(raw, Snapshot):
        return raw
    if isinstance(raw, dict):
        parsed = snapshot_from_dict(raw)
        if parsed is not None:
            return parsed
    return Snapshot(root=str(root or ""), entries={})


def record_mutation(
    plan_dir: Any,
    round_no: Any,
    vp_id: Any,
    payload: Any,
) -> Path:
    """存证 → 回滚 → 记账。

    顺序是这整个模块的重点：**先存证**（回滚之后 diff 就没了）、**再回滚**
    （把树复原）、**最后记账**（把这一轮发生过什么写进判定层能读的账本）。

    Args:
        plan_dir: The plan directory the ledger lives in.
        round_no: The round the mutation belongs to.
        vp_id: The verification point the mutation belongs to.
        payload: What was measured. Keys: ``root`` (the tree), ``created`` /
            ``modified`` / ``deleted`` (relative paths), ``command``,
            ``tool_use_id``, and optionally ``before`` (the snapshot the tree
            should be brought back to).

    Returns:
        The ledger file the record was appended to. A payload with no
        mutations records nothing —— 账本只装真的越权。
    """
    plan = Path(str(plan_dir)).expanduser()
    round_number = _round_number(round_no)
    data = payload if isinstance(payload, dict) else {}
    ledger = ledger_path(plan, round_number, vp_id)

    report = _payload_report(data)
    if not (report.created or report.modified or report.deleted):
        return ledger

    root = _payload_root(data)
    before = _payload_before(data, root)

    patch: Optional[Path] = None
    patch_error: Optional[str] = None
    if root is None:
        patch_error = "no root in the payload"
    else:
        try:
            patch = write_patch(
                root, round_number, _next_patch_index(root, round_number)
            )
        except Exception as exc:  # noqa: BLE001 — 存证失败不许取消回滚
            patch_error = f"{type(exc).__name__}: {exc}"
            patch = None

    restore_failed: List[str] = []
    if root is not None:
        try:
            restore_failed = restore_tree(root, before, report)
        except Exception:  # noqa: BLE001 — 回滚失败要记账，不是要崩
            restore_failed = sorted(
                set(report.created) | set(report.modified) | set(report.deleted)
            )

    record = {
        "vp_id": str(vp_id),
        "round": round_number,
        "created": list(report.created),
        "modified": list(report.modified),
        "deleted": list(report.deleted),
        "patch": str(patch) if patch is not None else None,
        "command": str(data.get("command") or "")[:_COMMAND_MAX_LENGTH],
        "tool_use_id": str(data.get("tool_use_id") or ""),
    }
    if patch_error:
        record["patch_error"] = patch_error
    if restore_failed:
        record["restore_failed"] = sorted(set(restore_failed))

    try:
        _ensure_ignored(plan / ".pdt")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(
            f"[pdt] could not record the mutation ledger: {exc}",
            file=sys.stderr,
        )
    return ledger


def read_mutations(
    plan_dir: Any,
    round_no: Any,
    vp_id: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Read one round's mutation records back.

    Args:
        plan_dir: The plan directory the records were written under.
        round_no: The round to read.
        vp_id: When given, only that VP's records; otherwise every VP's.

    Returns:
        The records, in the order they were appended. A line that is not
        valid JSON is skipped rather than fatal —— 崩掉的一轮不该赔上整轮
        证据。
    """
    directory = ledger_path(plan_dir, round_no, vp_id).parent
    if not directory.is_dir():
        return []

    if vp_id:
        candidates = [directory / f"{_safe_vp_name(vp_id)}.jsonl"]
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


def _payload_command(payload: Any) -> str:
    """The command the tool call ran, when the payload carries one."""
    if not isinstance(payload, dict):
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        return command[:_COMMAND_MAX_LENGTH]
    path = tool_input.get("file_path")
    if isinstance(path, str) and path.strip():
        return path[:_COMMAND_MAX_LENGTH]
    return ""


def _record_mutation(
    root: Path,
    round_number: int,
    tool_use_id: str,
    before: Snapshot,
    report: MutationReport,
    payload: Any,
) -> Optional[Path]:
    """存证 → 回滚 → 记账（post 阶段）。

    没有改动就什么都不做 —— 账本只装真的越权。
    """
    if not (report.created or report.modified or report.deleted):
        return None
    plan_dir = os.environ.get(SCOPE_ENV, "").strip()
    if not plan_dir:
        return None
    vp_id = os.environ.get(VP_ID_ENV, "").strip() or "unknown"
    return record_mutation(
        plan_dir,
        round_number,
        vp_id,
        {
            "root": str(root),
            "command": _payload_command(payload),
            "tool_use_id": tool_use_id,
            "created": list(report.created),
            "modified": list(report.modified),
            "deleted": list(report.deleted),
            "before": snapshot_to_dict(before),
        },
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _run(argv: Sequence[str]) -> int:
    """The body of :func:`main`, without the safety net."""
    if not guard_enabled():
        return 0

    phase = os.environ.get(PHASE_ENV, "").strip().lower()
    if phase == PHASE_POST:
        post = True
    elif phase in ("", PHASE_PRE):
        # 没设就当 pre —— 拍快照是这脚本默认要做的事，比对要显式点。
        post = False
    else:
        # 不认识的阶段：不动。
        return 0

    root = project_root()
    if root is None:
        return 0

    payload = _load_payload(argv)
    if not payload:
        return 0

    tool_use_id, has_id = _payload_tool_use_id(payload)
    round_number = round_no()

    if post:
        return run_post(root, round_number, tool_use_id, has_id, payload)
    return run_pre(root, round_number, tool_use_id, payload)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the guard once.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` —— 永远。这个脚本是**观测**钩子，不是阻断钩子：它坏了不该把
        正常的工具调用一起弄坏，所以任何异常都走同一条路（stderr 留一句，
        exit 0）。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 — 见上面那句：观测钩子不许阻断
        print(f"[pdt] tree guard failed: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
