"""进程安全护栏 —— 验证子 agent 不得按模式杀进程。

背景
----
2026-10-27 02:48:17，一个 VP 子 agent 执行 ``pkill -f "backend/cli.py"``。
``pkill -f`` 按 **命令行子串** 选进程，而宿主上每个 PDT 执行器的 argv
都含 ``.../product-development-team/backend/cli.py`` —— 于是另外三个 plan
的执行器被一起 SIGTERM 掉。``killall`` 同理：它按进程名选，不问是谁起的。

两道防线
--------
  1. **提示词硬规则** —— ``_build_prompt`` 里的 ``PROCESS SAFETY`` 段
     （断言在 ``test_verification_prompt_adversarial.py``）。
  2. **机器护栏** —— 本文件测的 PreToolUse 钩子 ``pkill_killall_block``：
     命中 ``pkill`` / ``killall`` 就 exit 2（阻断），否则 exit 0（放行）。

这些断言走的是**生产入口** —— ``SettingsJsonBuilder.build()`` 写出来的那份
``settings.json``（Claude Code CLI 真正读的那份），而不是直接摸
``_HOOK_RULES`` 里的常量：钩子命令只有在真的被写进 settings.json 时，
才谈得上"接上了活路径"。

四个必须钉死的 exit code
------------------------
  * ``pkill -f "backend/cli.py"``            → 2（阻断；2026-10-27 的那一条）
  * ``killall python``                       → 2（阻断）
  * ``kill 1234``                            → 0（放行；精确 PID 是允许的）
  * ``lsof -ti tcp:18093 | xargs -r kill``    → 0（放行；按端口发现 PID 再逐个杀）
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_subagent import (  # noqa: E402
    _HOOK_RULES,
    SettingsJsonBuilder,
)

#: 钩子的名字（``_HOOK_RULES`` 里登记的短名）。
GUARD_NAME = "pkill_killall_block"

#: 用来识别"这条护栏"的样例命令：只有它能拦住这一条。
BLOCKING_SAMPLE = 'pkill -f "backend/cli.py"'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_hook(command: str, sample: str) -> int:
    """Run a hook command the way Claude Code runs it.

    The hook reads the tool payload from stdin as JSON and exits 2 to
    block or 0 to allow. Returns the process exit code.
    """
    payload = json.dumps({"tool_input": {"command": sample}})
    completed = subprocess.run(
        command,
        shell=True,
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.returncode


def _built_bash_hook_commands() -> List[str]:
    """The Bash hook commands from the settings.json the sub-agent runs with.

    Goes through ``SettingsJsonBuilder.build()`` — the production entry
    point — and reads back the file that was written, so a rule that was
    never wired into the settings cannot pass a test here.
    """
    builder = SettingsJsonBuilder(vp_id="VP-001", plan_id="pkill-guard")
    path = builder.build()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    finally:
        builder.cleanup()
    return [
        entry["hooks"][0]["command"]
        for entry in data["hooks"]["PreToolUse"]
        if entry.get("matcher") == "Bash"
    ]


@pytest.fixture(scope="module")
def guard_command() -> str:
    """The one Bash hook in the built settings.json that blocks ``pkill``.

    Identified by behaviour, not by position: whichever hook refuses
    ``pkill -f "backend/cli.py"`` is the guard under test.
    """
    for command in _built_bash_hook_commands():
        if _run_hook(command, BLOCKING_SAMPLE) == 2:
            return command
    pytest.fail(
        "no Bash hook in the built settings.json blocks "
        f"{BLOCKING_SAMPLE!r} — the process-safety guard is not wired in"
    )


# ---------------------------------------------------------------------------
# The registry shape — the rule is registered where the PRD says it is
# ---------------------------------------------------------------------------


def test_guard_rule_is_registered() -> None:
    """``pkill_killall_block`` must exist in the rule registry."""
    names = [rule["name"] for rule in _HOOK_RULES]
    assert GUARD_NAME in names, names


def test_guard_rule_sits_after_basic_and_before_extended() -> None:
    """Basic 6 in front, Extended 3 last — the order is pinned."""
    names = [rule["name"] for rule in _HOOK_RULES]
    assert names.index(GUARD_NAME) == 5, names
    assert names.index("plans_delete_block") > names.index(GUARD_NAME), names
    assert len(_HOOK_RULES) == 9, names


def test_guard_rule_matches_bash() -> None:
    """Only Bash is guarded — Edit/Write must not be intercepted."""
    rule = next(r for r in _HOOK_RULES if r["name"] == GUARD_NAME)
    assert rule["matcher"] == "Bash"


# ---------------------------------------------------------------------------
# The four exit codes — run through the built settings.json
# ---------------------------------------------------------------------------


def test_pkill_by_pattern_is_blocked(guard_command: str) -> None:
    """``pkill -f`` killed three unrelated plans on 2026-10-27 → exit 2."""
    assert _run_hook(guard_command, 'pkill -f "backend/cli.py"') == 2


def test_killall_is_blocked(guard_command: str) -> None:
    """``killall`` selects by process name → exit 2."""
    assert _run_hook(guard_command, "killall python") == 2


def test_exact_pid_kill_is_allowed(guard_command: str) -> None:
    """Killing the PID you started is the sanctioned way → exit 0."""
    assert _run_hook(guard_command, "kill 1234") == 0


def test_port_discovered_pid_kill_is_allowed(guard_command: str) -> None:
    """``lsof -ti tcp:<port>`` then kill each PID → exit 0."""
    assert _run_hook(guard_command, "lsof -ti tcp:18093 | xargs -r kill") == 0
