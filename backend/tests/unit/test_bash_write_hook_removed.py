"""Bash 写范围钩子已摘除 —— 钩子登记表回到 11 条。

背景
----
任务 7 让 ``check_bash_payload`` 恒返回 0：Bash 分支不再判定，改成「放行 +
事后测量」（被验证树的前后两张快照做差，见 ``verification_tree_guard.py``）。
判定没了，``_HOOK_RULES`` 里那条 ``verifier_bash_write_block`` 就成了死钩子 ——
每条 Bash 命令都要白起一个 Python 进程，却什么都不判。任务 17 把它摘掉。

本文件钉四件事
--------------
  1. ``verifier_bash_write_block`` 不在 ``_HOOK_RULES`` 里；
  2. ``verifier_write_scope_block``（Edit|Write）还在 —— Edit/Write 越界写
     仍然要 exit 2；
  3. 六条灾难命令的拦截规则一条不少，位置也不变；
  4. 钩子总数回到 11，且其余条目的相对顺序与摘除前一致。

顺序说明：9 条既有规则（6 条灾难拦截 + 3 条扩展）在前，写范围规则在后，
树快照钩子收尾 —— 摘掉的是中间那一条，其它条目的位置不动。

断言直接读 ``_HOOK_RULES`` 这张登记表：钩子登记是这件事本身，钩子命令
拼得对不对由别的测试管（``test_verification_write_scope_settings.py``）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_subagent import _HOOK_RULES  # noqa: E402


#: 被摘掉的那条钩子的名字（Bash 写范围）。
BASH_WRITE_GUARD_NAME = "verifier_bash_write_block"

#: 还留着的那条钩子的名字（Edit|Write 写范围）。
EDIT_WRITE_GUARD_NAME = "verifier_write_scope_block"

#: 六条灾难命令的拦截规则，顺序与登记表里一致。
DISASTER_RULE_NAMES = (
    "rm_root_block",
    "fork_bomb_block",
    "mkfs_block",
    "dd_zero_block",
    "force_push_main_block",
    "pkill_killall_block",
)

#: 摘除之后 11 条钩子的完整名字（顺序即登记表顺序）。
EXPECTED_RULE_NAMES = (
    "rm_root_block",
    "fork_bomb_block",
    "mkfs_block",
    "dd_zero_block",
    "force_push_main_block",
    "pkill_killall_block",
    "plans_delete_block",
    "pdt_server_restart_block",
    "exec_path_block",
    "verifier_write_scope_block",
    "tree_snapshot_pre",
)


def _names() -> List[str]:
    """The hook names in registration order."""
    return [rule["name"] for rule in _HOOK_RULES]


def test_bash_write_hook_is_gone() -> None:
    """``verifier_bash_write_block`` 已从登记表里摘掉。"""
    assert BASH_WRITE_GUARD_NAME not in _names(), _names()


def test_edit_write_hook_is_kept() -> None:
    """Edit|Write 的写范围钩子还在 —— 越界写仍然要 exit 2。"""
    names = _names()
    assert EDIT_WRITE_GUARD_NAME in names, names

    rule = next(r for r in _HOOK_RULES if r["name"] == EDIT_WRITE_GUARD_NAME)
    assert rule["matcher"] == "Edit|Write", rule
    # 命令还得指着写护栏脚本本身 —— 摘的是 Bash 那条，不是这个。
    assert "verification_write_guard.py" in rule["command"], rule["command"]


def test_six_disaster_blocks_are_kept() -> None:
    """六条灾难命令的拦截规则一条不少，位置也不变。"""
    names = _names()
    for index, name in enumerate(DISASTER_RULE_NAMES):
        assert names[index] == name, (
            f"第 {index + 1} 条应当是 {name}，实际是 {names[index]}"
        )


def test_hook_count_is_11() -> None:
    """12 → 11：摘掉的只有那一条死钩子。"""
    assert len(_HOOK_RULES) == 11, _names()


def test_rule_order_is_unchanged() -> None:
    """其余 11 条的相对顺序与摘除前一致。"""
    assert tuple(_names()) == EXPECTED_RULE_NAMES, _names()
