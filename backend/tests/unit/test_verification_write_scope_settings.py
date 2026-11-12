"""把 VP 写入护栏接进 SettingsJsonBuilder —— 生产入口真的带上可写范围。

背景
----
任务 1/2 写好了护栏脚本（``backend/verification_write_guard.py``），但它是
**独立**的：``PDT_VP_WRITE_SCOPE`` 不设就完全不生效（exit 0）。而子 agent
真正读的是 ``SettingsJsonBuilder.build()`` 写出来的那份 ``settings.json`` ——
护栏没进这份文件，就等于没接上活路径。

本文件钉四件事
--------------
  1. builder 带 ``plan_dir`` 时，写出来的 settings.json 的 ``env`` 里声明了
     可写范围（plan 目录 + 轮次 + VP 号）；
  2. ``_HOOK_RULES`` 里多出第 10 条：``verifier_write_scope_block``
     （Edit|Write）——第 11 条 ``verifier_bash_write_block``（Bash）已于
     2026-11-05 摘除：Bash 分支不再判定（恒放行），改动改由树快照事后测量；
  3. 从这份 settings.json 里读出来的钩子命令，跑起来真的能挡住"写被验证
     仓库"（exit 2）；
  4. 不带 ``plan_dir`` 时不写 scope env —— 旧调用方（执行子 agent、refiner、
     自己写 settings.json 的地方）一行不改也完全不受影响。

环境变量名
----------
护栏脚本自己定的三个变量（见 ``backend/verification_write_guard.py`` 的模块
docstring）：``PDT_VP_WRITE_SCOPE``（**值就是 plan 目录** —— 护栏把它的值
当成唯一可写根）、``PDT_VP_ID``、``PDT_VP_ROUND``。这里照抄那三个名字，另加
一个纯说明性的：``PDT_VP_REFUSAL_DIR`` —— 把拒绝证据的落点直接写出来，省得
读的人自己拼一遍路径。plan 目录本身只写一份（``PDT_VP_WRITE_SCOPE``）：同一
件事写两个名字，就是两份会漂的副本。

断言走的是**生产入口**：``SettingsJsonBuilder.build()`` 写出来的文件，而不是
直接摸 ``_HOOK_RULES`` 里的常量 —— 钩子只有在真的被写进 settings.json 时，
才谈得上"接上了活路径"。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_subagent import (  # noqa: E402
    _HOOK_RULES,
    SettingsJsonBuilder,
)

#: 护栏脚本自己的路径。钩子命令必须指向它 —— 命令里写死的是脚本的绝对路径。
_GUARD_SCRIPT = _BACKEND_DIR / "verification_write_guard.py"

#: 被验证仓库自己的一个源文件。写它正是这条护栏必须拒绝的事：验证子 agent
#: 在验证这棵树，不是在这棵树里开发。
_REPO_FILE = _BACKEND_DIR / "agent.py"

#: 第 10 条钩子的名字（Edit|Write）。
EDIT_GUARD_NAME = "verifier_write_scope_block"

#: 已摘除的第 11 条钩子的名字（Bash 写范围）。2026-11-05 起它不再登记在册 ——
#: Bash 分支恒放行（判定改成事后测量），留着只会白起一个 Python 进程。
BASH_GUARD_NAME = "verifier_bash_write_block"

#: scope env 的三个键。前两个由护栏脚本消费，最后一个是给人看的落点说明。
SCOPE_ENV_KEYS = (
    "PDT_VP_WRITE_SCOPE",
    "PDT_VP_ID",
    "PDT_VP_REFUSAL_DIR",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(vp_id: str = "VP-055", plan_id: str = "p", **kwargs) -> Dict:
    """Build the settings.json through the production entry point.

    Returns the parsed document. The temp file is removed before returning —
    测试不留下垃圾，也不依赖 cleanup() 的时序。
    """
    builder = SettingsJsonBuilder(vp_id=vp_id, plan_id=plan_id, **kwargs)
    path = builder.build()
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    finally:
        builder.cleanup()


def _hook_commands(data: Dict, matcher: str) -> List[str]:
    """The hook commands the built settings.json carries for ``matcher``."""
    return [
        entry["hooks"][0]["command"]
        for entry in data.get("hooks", {}).get("PreToolUse", [])
        if entry.get("matcher") == matcher
    ]


def _child_env(settings_env: Dict) -> Dict[str, str]:
    """The environment a hook subprocess would run with.

    Claude Code 把 settings.json 的 ``env`` 折进进程环境，钩子子进程继承
    它 —— 所以这里照抄那份合并的结果。先把外层残留的 scope 变量清掉：
    免得宿主机上恰好设过 ``PDT_VP_WRITE_SCOPE`` 时，替某条断言做决定。
    """
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("PDT_VP_"):
            env.pop(name, None)
    for key, value in (settings_env or {}).items():
        env[str(key)] = str(value)
    return env


def _run_hook(command: str, payload: Dict, env: Dict[str, str]) -> int:
    """Run a hook command the way Claude Code runs it.

    The hook reads the tool payload from stdin as JSON and exits 2 to block
    or 0 to allow. Returns the process exit code.
    """
    completed = subprocess.run(
        command,
        shell=True,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return completed.returncode


# ---------------------------------------------------------------------------
# env —— 可写范围由 env 声明
# ---------------------------------------------------------------------------


def test_build_writes_scope_env(tmp_path):
    """带 plan_dir → settings.json 的 env 里三个 scope 键齐全。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    data = _build(plan_dir=plan_dir, round_no=3)
    env = data.get("env", {})

    for key in SCOPE_ENV_KEYS:
        assert key in env, f"env 少了 {key}：{sorted(env)}"

    assert env["PDT_VP_ID"] == "VP-055"
    assert env["PDT_VP_REFUSAL_DIR"] == str(
        plan_dir / "vp_write_scope" / "refusals" / "round-3"
    )
    # plan 目录只写一份：``PDT_VP_WRITE_SCOPE`` 就是它。同一件事再写一个
    # 名字（``PDT_VP_PLAN_DIR``）就是一份会漂的副本 —— 护栏读的那份和
    # 人读的那份可以指向不同的目录。
    assert "PDT_VP_PLAN_DIR" not in env, (
        f"plan 目录被写了两份：{sorted(env)}"
    )
    # 护栏脚本把 PDT_VP_WRITE_SCOPE 的**值**当成唯一可写根，所以这里必须是
    # plan 目录本身（一个 flag 式的 "1" 会让护栏拿 cwd/1 当可写根）。
    assert env["PDT_VP_WRITE_SCOPE"] == str(plan_dir)
    # 轮次决定拒绝证据落在 round-<n>/ 下面 —— 护栏按它拼路径。
    assert env["PDT_VP_ROUND"] == "3"


def test_build_without_plan_dir_writes_no_scope():
    """不带 plan_dir → 不写 scope env（旧行为，钩子放行）。"""
    data = _build()
    env = data.get("env", {})

    leaked = [key for key in env if key.startswith("PDT_VP_")]
    assert not leaked, f"没有 plan_dir 就不该有 scope env，却有 {leaked}"


def test_build_without_plan_dir_hook_allows_project_write():
    """没有 scope env → 钩子放行写被验证仓库（旧调用方不受影响）。"""
    data = _build()
    commands = _hook_commands(data, "Edit|Write")
    assert commands, "settings.json 里没有 Edit|Write 钩子"

    command = next(
        (c for c in commands if "verification_write_guard.py" in c), None
    )
    assert command, "Edit|Write 钩子里没有接上写入护栏脚本"

    code = _run_hook(
        command,
        {"tool_name": "Edit", "tool_input": {"file_path": str(_REPO_FILE)}},
        _child_env(data.get("env", {})),
    )
    assert code == 0, "没有 plan_dir 时护栏必须放行（否则旧调用方全被拦）"


# ---------------------------------------------------------------------------
# 钩子登记 —— 第 10 条（Edit|Write 写范围）
# ---------------------------------------------------------------------------


def test_build_registers_the_edit_guard_rule_only():
    """写范围钩子只剩 Edit|Write 一条；Bash 那条已摘除（任务 17）。"""
    names = [rule["name"] for rule in _HOOK_RULES]

    assert EDIT_GUARD_NAME in names, names
    assert BASH_GUARD_NAME not in names, names

    edit_rule = next(r for r in _HOOK_RULES if r["name"] == EDIT_GUARD_NAME)
    assert edit_rule["matcher"] == "Edit|Write"


def test_hook_rules_count_is_11():
    """12 → 11：摘掉的是那条已无判定的 Bash 写范围钩子，其余一条不少。"""
    assert len(_HOOK_RULES) == 11, [r["name"] for r in _HOOK_RULES]


def test_guard_command_is_the_quoted_interpreter_and_script():
    """命令 = sys.executable + 脚本绝对路径，两段都过了 shlex.quote。"""
    command = next(
        r["command"] for r in _HOOK_RULES if r["name"] == EDIT_GUARD_NAME
    )
    # 按 shell 的切法切回来还是两段 —— 说明引号没把路径拆开（路径里有空格
    # 也不会切成两段），而且没多带参数。
    parts = shlex.split(command)
    assert len(parts) == 2, parts
    assert parts[0] == sys.executable
    assert parts[1] == str(_GUARD_SCRIPT)
    assert Path(parts[1]).is_absolute()


def test_built_settings_carry_the_edit_guard_rule():
    """写范围护栏进了生产写出来的那份 settings.json（Edit|Write 一侧）。"""
    data = _build()

    edit_commands = _hook_commands(data, "Edit|Write")

    assert any("verification_write_guard.py" in c for c in edit_commands), (
        "Edit|Write 钩子里没有接上写入护栏脚本"
    )


# ---------------------------------------------------------------------------
# 钩子真的能挡 —— 黑盒跑一次
# ---------------------------------------------------------------------------


def test_built_edit_hook_blocks_project_path(tmp_path):
    """跑 build() 出来的命令 → 写被验证仓库的源码被挡（exit 2）。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    data = _build(plan_dir=plan_dir, round_no=3)
    commands = _hook_commands(data, "Edit|Write")
    command = next(
        (c for c in commands if "verification_write_guard.py" in c), None
    )
    assert command, "Edit|Write 钩子里没有接上写入护栏脚本"

    code = _run_hook(
        command,
        {"tool_name": "Edit", "tool_input": {"file_path": str(_REPO_FILE)}},
        _child_env(data.get("env", {})),
    )
    assert code == 2, "写被验证仓库的源码必须被挡下"


def test_built_settings_carry_no_bash_write_guard(tmp_path):
    """Bash 侧不再挂写范围护栏 —— 判定改成事后测量（任务 7/17）。

    Bash 分支恒放行（``check_bash_payload`` 恒返回 0），留着这条钩子只是白起
    一个 Python 进程。漏过去的改动由树快照的前后两张图做差来抓（任务 2/3）。
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    data = _build(plan_dir=plan_dir, round_no=3)
    commands = _hook_commands(data, "Bash")

    assert not any(
        "verification_write_guard.py" in c for c in commands
    ), "Bash 侧不该再挂写范围护栏（判定已改成事后测量）"
