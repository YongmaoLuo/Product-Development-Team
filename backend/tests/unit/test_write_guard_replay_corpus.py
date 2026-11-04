"""回放语料的结构约束 —— 语料是冻结的，判定逻辑不再改。

背景（2026-11-04）
------------------
写护栏的判定逻辑改了六轮，每一轮都是"遇到一种新的争议形状 → 在判定逻辑里
加一条特判"。这样下去，判定逻辑越来越像一张补丁表，而**没人说得清哪一条
特判还在被谁依赖**：改掉一条特判，可能悄悄放过一条真写。

所以把语料从判定逻辑里拆出来：**争议形状进语料，不进判定逻辑**。以后再遇到
一种新形状，往 ``corpus.jsonl`` 里加一条就行 —— 判定逻辑一个字节都不动。

语料必须是**真实命令**
----------------------
新编的"形状单测"（为了覆盖某个形状而凭空捏的命令）证明不了任何事：真实世界
的形状长什么样，只有真实命令知道。所以本语料的每一条都来自两个地方之一：

``verification-log``
    验证轮真实拒绝记录里留下的命令行。那些记录里带 ``command`` 字段的
    （``verification_write_guard.record_refusal`` 记的）就是当初**真的被拒过**
    的那条命令。本机路径与计划号被替换成中性的 ``/tmp`` 落点，命令的形状一字
    未改。

``existing-test``
    既有单元测试里当**真实命令**用的字符串字面量 —— 那些用例是拿真命令去喂
    护栏的，不是凭空捏的形状。

``redirect`` / ``heredoc`` / ``git``
    上面两类里以某种形状为主角的那些（重定向、heredoc、git 子命令）。

``disaster``
    计划书点名的六条灾难命令。它们的 ``expect`` 描述的是命令**本身**的破坏性，
    但 ``never_run`` 为真 —— 回放时**不得执行**。

``expect`` 是什么
-----------------
``expect`` 是这条命令跑起来**会不会改动文件系统**：

* ``write`` —— 会（创建 / 修改 / 删除文件，或改动远端仓库）；
* ``no_write`` —— 不会。

两个刻意的细节：

* **设备与丢弃目标不算写。** ``echo hi 2>/dev/null`` 是 ``no_write`` ——
  ``/dev/null`` 是个设备，丢掉的东西不算写。（护栏的落点清单里会有
  ``/dev/null``，但钩子放行它：它在被验证的树之外。）
* **解析不出来的落点不算写。** 护栏的契约是"看不懂就不猜"：变量展开、
  复杂管道一律放行。语料里没有"落点写不出来"的条目 —— 那种条目对回放没有
  判定价值。

已知的一处**有意的**分歧
------------------------
``echo hi 2>/dev/null`` 的 ``expect`` 是 ``no_write``，而护栏的落点清单会列出
``/dev/null``。这是有意的：语料记的是**命令会不会写文件**，护栏的落点清单记
的是**解析出来的操作数**，两者在设备文件上本来就分岔。钩子的裁决（放行）在这
条上与语料一致。

本文件只钉**结构**
------------------
条目数、``expect`` 的取值词表、灾难命令的 ``never_run``、九类形状的覆盖、
每条都带 ``source`` 与布尔 ``never_run``。语料的内容对不对，是回放那一轮的事。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

#: 语料文件。路径从本文件推出来 —— 写死本机路径的测试换台机器就废了。
_CORPUS_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "write_guard_replay"
    / "corpus.jsonl"
)

#: ``expect`` 的取值词表 —— 冻结了。只有这两个值，没有第三种。
_EXPECT_VOCABULARY = frozenset({"write", "no_write"})

#: 计划书点名的六条灾难命令。它们**只进语料、不执行**：回放遇到
#: ``never_run`` 为真的条目必须跳过。
_DISASTER_COMMANDS = (
    "rm -rf /",
    ":(){ :|:& };:",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/sda",
    "git push --force origin main",
    "pkill -9 -1",
)

#: 九类必须覆盖的争议形状，以及各自的识别方式。
#:
#: 识别方式是**命令文本**上的正则 —— 语料的行格式是冻结的
#: （``{command, expect, source, never_run}``），不另开一个 ``shape`` 字段。
_NAMED_SHAPES = {
    # ``2>`` / ``2>&1``：文件描述符重定向，不是落点。
    "descriptor-redirect": re.compile(r"2>&1|2>/dev/null"),
    # ``> len(...)``：Python 比较表达式，旧解析器把 ``len`` 当成了落点。
    "len": re.compile(r">\s*len\("),
    # ``> 1:``：同族，数字字面量后面跟着冒号。
    "1:": re.compile(r">\s*1:"),
    # ``> index(...)``：同族。
    "index": re.compile(r">\s*index\("),
    # ``> single(...)``：同族。
    "single": re.compile(r">\s*single\("),
    # heredoc 正文：正文是数据，不是命令。
    "heredoc-body": re.compile(r"<<(?!<)"),
    # 跨行引号：``python3 -c "`` 之后到闭引号为止是同一个参数。
    "multiline-quote": re.compile(r"-c\s+[\"']\n"),
    # git 的 dry-run：只读，不产生落点。
    "git-dry-run": re.compile(r"--dry-run"),
    # 树外路径：落在临时目录里的落点（护栏无条件放行的地方）。
    "out-of-tree-path": re.compile(r"/tmp/"),
}


def load_corpus(path: Path = _CORPUS_PATH) -> List[Dict[str, Any]]:
    """Read the replay corpus.

    Args:
        path: The ``corpus.jsonl`` to read.

    Returns:
        One dict per non-empty line, in file order.
    """
    text = path.read_text(encoding="utf-8")
    entries: List[Dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        entry = json.loads(line)
        if not isinstance(entry, dict):
            raise ValueError(f"{path}:{number} 不是一条 JSON 对象")
        entries.append(entry)
    return entries


def test_corpus_has_at_least_30_entries():
    """语料至少 30 条 —— 少于这个数，覆盖不了九类形状。"""
    corpus = load_corpus()

    assert len(corpus) >= 30, f"语料只有 {len(corpus)} 条"


def test_expect_values_are_frozen_vocabulary():
    """``expect`` 只有 ``write`` / ``no_write`` 两个值。"""
    for entry in load_corpus():
        assert entry.get("expect") in _EXPECT_VOCABULARY, entry


def test_disaster_commands_are_never_run():
    """六条灾难命令都在案，且都标了 ``never_run`` —— 回放时不得执行。"""
    by_command = {entry["command"]: entry for entry in load_corpus()}

    for command in _DISASTER_COMMANDS:
        assert command in by_command, f"灾难命令不在案：{command!r}"
        assert by_command[command].get("never_run") is True, (
            f"灾难命令必须标 never_run：{command!r}"
        )


def test_corpus_covers_all_named_shapes():
    """九类争议形状各有至少一条 —— 少一类就说明有一种形状没人盯。"""
    corpus = load_corpus()

    for name, pattern in _NAMED_SHAPES.items():
        matched = [entry for entry in corpus if pattern.search(entry["command"])]
        assert matched, f"形状 {name!r} 一条都没有"


def test_every_entry_has_source_and_never_run():
    """每条都带 ``command`` / ``expect`` / ``source`` / 布尔 ``never_run``。

    ``source`` 只是**类别名**：它不许是路径，更不许是本机路径或计划号 ——
    语料是要给别人用的，泄漏谁在什么计划里跑过什么命令，不应该发生。
    """
    for entry in load_corpus():
        for key in ("command", "expect", "source", "never_run"):
            assert key in entry, entry

        source = entry["source"]
        assert isinstance(source, str) and source.strip(), entry
        assert not source.startswith("/"), f"source 是路径：{source!r}"
        assert "/" not in source, f"source 里带路径分隔符：{source!r}"

        assert isinstance(entry["never_run"], bool), entry
