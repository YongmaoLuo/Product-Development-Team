"""Append-only audit trail for off-topic VP removals.

Why this module exists
----------------------
主题外 VP（与本次主题无关的验证点）的删除必须「先归档、后移除」。
审计文件是这次删除**唯一**的持久化真相：

  * 移除一个 VP 等于抹掉一段「为什么认为它主题外」的证据；
  * 因此顺序被钉死 —— 先把决策追加进审计文件，**写成功之后**才允许移除
    那个 VP；
  * 写不进去（磁盘满、只读、路径不可写）就必须**中止删除** ——
    :func:`archive_removal` 因此让异常一路冒上去，不吞、不降级成一个
    「大概写进去了」。

中止必须是**干净**的
--------------------
抛了异常就意味着这次归档没有兑现，于是审计文件里**不能**多出一行
「已归档」的声明 —— 那一行读出来就是一次移除，而移除并没有发生。追加写
的文件没法把多出去的那一行拿掉（能拿掉就不叫审计了），所以唯一的办法是
**在写之前**就知道写不进去：:func:`archive_removal` 先 sync 一次，
fsync 失败的文件系统根本不该收到这次写入。

写到一半才发现的失败（磁盘正好在这时满了）仍然会留下一段半行。读路径
跳过它 —— 半行不是缺失，缺失是「没有这个文件」。于是那条验证点下一轮
还会被验：**宁可重验，不可漏验**。

只增不改
--------
审计文件**只能**以 ``"a"`` 模式打开：:func:`archive_removal` 是本模块
唯一的 ``open`` 调用点，读路径走 ``Path.read_text``，不再开第二次文件。
本模块**不导出**任何覆写 / 删除入口 —— 审计文件一旦能被改写，「已归档」
就从一个事实退化成一句可以被抹掉的声明，而删除正是拿这句话当依据的。

读路径的容错
------------
审计文件是追加写的，所以它的尾部可能因为写入进程被杀掉而留下半行。
读的时候逐行解析，**坏行跳过、不抛异常**，其余完整行照常返回。半行不是
缺失；缺失是「没有这个文件」，那要读出空集合 —— 两者必须能分开。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, Optional, Set, Union

#: The audit file's name inside the plan directory.  It is part of the
#: contract: anything that reads the audit outside this module has to
#: agree on this name.
AUDIT_FILENAME = "vp_removal_audit.jsonl"

#: Anything that can name a directory.
_PathLike = Union[str, os.PathLike]

__all__ = ["AUDIT_FILENAME", "archive_removal", "load_removed_vp_ids"]


def _audit_path(plan_dir: _PathLike) -> Path:
    """The audit file's path inside ``plan_dir``."""
    return Path(plan_dir) / AUDIT_FILENAME


def archive_removal(record: Mapping[str, Any], plan_dir: _PathLike) -> None:
    """Append one removal record to the plan's audit file.

    The record is written as a single JSON Lines entry and fsync'd before
    this function returns, so a caller that removes the VP right after
    the call knows the record is on disk — not merely in the page cache.

    Nothing is ever rewritten here: the file is opened in append mode, so
    an earlier record stays exactly where it was.

    Args:
        record: The removal decision being archived.  Any JSON-serialisable
            mapping; the reader picks the VP id out of ``vp_id`` (falling
            back to ``vp.id``).
        plan_dir: The plan's directory — the audit file lives directly
            inside it.

    Raises:
        TypeError: ``record`` is not a mapping, or holds a value that
            cannot be serialised to JSON.  Nothing is written in that
            case, and the caller must abort the removal.
        OSError: The record could not be written (read-only plan
            directory, full disk, unwritable path).  The exception is
            deliberately not swallowed: **写不进去就不得移除**.

    Note:
        失败必须是**干净**的：这次调用抛了异常，审计文件里就不能多出
        一行「已归档」的声明 —— 那一行读出来就是一次移除，而移除并没有
        发生。追加写的文件没法把多出去的那一行拿掉（能拿掉就不叫审计
        了），所以唯一的办法是**在写之前**就知道写不进去：先 sync 一次，
        fsync 失败的文件系统根本不该收到这次写入。
    """
    if not isinstance(record, Mapping):
        raise TypeError(
            f"removal record must be a mapping, got {type(record).__name__}"
        )

    path = _audit_path(plan_dir)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        # 写之前先问一次「这个文件系统还能不能落盘」。
        #
        # 一个连 sync 都不肯做的文件系统，收下这一行也只是让它躺在页缓存
        # 里 —— 崩溃就没了。这种情况下我们宁可**什么都不写**：审计文件里
        # 没有这一行，移除就没有发生，这正是「归档写不进去 ⇒ 移除不发生」
        # 要的那一边（多验一条只是多花钱，少验一条是丢了结论）。
        #
        # 诚实的边界：只有**写之前**才发现的失败才能保证文件原样。写到
        # 一半才发现的失败（磁盘正好在这时满了）会留下一段半行 —— 读路径
        # 跳过它，于是那条验证点下一轮还会被验（宁可重验不可漏验）。
        os.fsync(handle.fileno())
        handle.write(line)
        handle.flush()
        # 记录真的落盘了，「已归档」才从一句声明变成一个事实。
        os.fsync(handle.fileno())


def load_removed_vp_ids(plan_dir: _PathLike) -> Set[str]:
    """Rebuild the set of removed VP ids from the plan's audit file.

    The audit file is the persistent truth about what was removed, so this
    is how a reader answers "which VPs are gone?" after a restart.

    A line that does not parse — a half-written tail left by a killed
    writer, or any other malformed entry — is skipped, and the remaining
    lines are returned as usual.  A missing audit file means no removal
    ever happened, and yields an empty set.

    Args:
        plan_dir: The plan's directory.

    Returns:
        The set of removed VP ids.  Empty when nothing was removed.

    Raises:
        OSError: The audit file exists but could not be read.  That is
            deliberately **not** reported as "nothing was removed" — an
            unreadable audit is not an empty audit.
    """
    path = _audit_path(plan_dir)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # 没有审计文件 == 没有发生过移除
        return set()

    removed: Set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except ValueError:
            # 半行 / 坏行：跳过这一行，其余行照常读出
            continue
        if not isinstance(payload, dict):
            continue
        vp_id = _vp_id_of(payload)
        if vp_id:
            removed.add(vp_id)
    return removed


def _vp_id_of(payload: Dict[str, Any]) -> Optional[str]:
    """The removed VP's id out of one archived record.

    ``vp_id`` is the field the archive contract names; ``vp.id`` is the
    same id as it appears inside the archived VP itself, and is accepted
    as a fallback so a record written by a slightly different producer is
    still readable.
    """
    vp_id = payload.get("vp_id")
    if isinstance(vp_id, str) and vp_id:
        return vp_id
    vp = payload.get("vp")
    if isinstance(vp, dict):
        nested = vp.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None