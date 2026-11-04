"""树快照钩子 —— PreToolUse 拍快照、PostToolUse 比对。

背景
----
缺陷 A 第 2 条：现有钩子只挂在 PreToolUse 上（``verification_subagent._HOOK_RULES``
的 11 条），**命令跑完之后**这棵树变成了什么样，没有机器可读的记录。"子 agent 说
自己没有改仓库"是一句自述，不是一份证据。

这份测试钉五件事
----------------
  1. ``pre`` 阶段按 ``tool_use_id`` 拍下快照（``snapshot-<id>.json``）；
  2. ``post`` 阶段读回配对的快照，比对出 ``compare-<id>.json``；
  3. payload 缺 ``tool_use_id`` 时落到 ``snapshot-_pending.json``，并由
     ``last.json`` 记住"还有一份没比对的快照"，post 靠它配对；
  4. ``SettingsJsonBuilder.build()`` 写出来的那份 settings.json 里真的有
     ``PostToolUse`` 条目，命令前缀是 ``PDT_TREE_GUARD_PHASE=post``；
  5. ``_HOOK_RULES`` 正好 11 条（2026-11-05 摘除了已无判定的 Bash 写范围
     钩子），里面那条叫 ``tree_snapshot_pre``。

钩子不得阻断正常调用
--------------------
stdin 不是合法 JSON、或者是空的、或者没有声明可写范围（``PDT_VP_WRITE_SCOPE``
没设）时，一律 exit 0 —— 旧调用方一行不改也完全不受影响。

测的是**生产入口**：``SettingsJsonBuilder.build()`` 写出来的那份 settings.json
（Claude Code CLI 真正读的那份），而不是直接摸 ``_HOOK_RULES`` 里的常量。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_tree_guard  # noqa: E402
from verification_subagent import (  # noqa: E402
    _HOOK_RULES,
    SettingsJsonBuilder,
)

#: 第 12 条钩子的名字（PreToolUse 侧）。
TREE_GUARD_NAME = "tree_snapshot_pre"

#: 已摘除的 Bash 写范围钩子（2026-11-05，任务 17）：Bash 分支恒放行，
#: 判定改成事后测量，留着只会白起一个 Python 进程。
BASH_GUARD_NAME = "verifier_bash_write_block"

#: PostToolUse 侧的命令前缀。
POST_PHASE_PREFIX = "PDT_TREE_GUARD_PHASE=post"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    """Run ``git`` inside ``repo``."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(repo: Path) -> Path:
    """Create a throw-away git repository with one tracked file."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--initial-branch", "main")
    if not (repo / ".git").is_dir():
        # 老版本 git 不认 ``--initial-branch`` —— 退回默认分支名。
        _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


def _env(monkeypatch, repo: Path, round_number: int = 1) -> None:
    """Declare the environment the hook runs in."""
    # 可写范围 = 护栏的开关。没设它，护栏完全不生效。
    monkeypatch.setenv("PDT_VP_WRITE_SCOPE", str(repo.parent / "plan"))
    # 被观察的树。
    monkeypatch.setenv("PDT_PROJECT_DIR", str(repo))
    monkeypatch.setenv("PDT_VP_ROUND", str(round_number))


def _run(monkeypatch, payload, phase: str) -> int:
    """Run the guard the way the hook runs it.

    The payload arrives on stdin as JSON; the phase comes from the
    environment. Returns the process exit code.
    """
    monkeypatch.setenv("PDT_TREE_GUARD_PHASE", phase)
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    return verification_tree_guard.main([])


def _verify_dir(repo: Path, round_number: int = 1) -> Path:
    """Where the guard keeps its evidence for this round."""
    return repo / ".pdt" / "verify" / f"round-{round_number}"


# ---------------------------------------------------------------------------
# pre —— 拍快照
# ---------------------------------------------------------------------------


def test_pre_writes_snapshot_by_tool_use_id(tmp_path, monkeypatch):
    """pre + 正常的 tool_use_id → snapshot-<id>.json 落盘，条目非空。"""
    repo = _init_repo(tmp_path / "repo")
    _env(monkeypatch, repo)

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo x"},
        "tool_use_id": "tu_1",
    }
    assert _run(monkeypatch, payload, "pre") == 0

    snap = _verify_dir(repo) / "snapshot-tu_1.json"
    assert snap.exists(), f"快照没落盘：{snap}"
    data = json.loads(snap.read_text(encoding="utf-8"))
    assert data["entries"], "快照里一个条目都没有"
    assert "tracked.txt" in data["entries"], sorted(data["entries"])


def test_pre_without_tool_use_id_writes_pending_snapshot(tmp_path, monkeypatch):
    """payload 缺 tool_use_id → snapshot-_pending.json + last.json。"""
    repo = _init_repo(tmp_path / "repo")
    _env(monkeypatch, repo)

    payload = {"tool_name": "Bash", "tool_input": {"command": "echo x"}}
    assert _run(monkeypatch, payload, "pre") == 0

    evidence = _verify_dir(repo)
    assert (evidence / "snapshot-_pending.json").exists(), sorted(
        p.name for p in evidence.iterdir()
    )
    assert (evidence / "last.json").exists()


# ---------------------------------------------------------------------------
# post —— 比对
# ---------------------------------------------------------------------------


def test_post_reads_paired_snapshot(tmp_path, monkeypatch):
    """pre + post 同一个 id → compare-<id>.json 落盘，说清改了哪些文件。"""
    repo = _init_repo(tmp_path / "repo")
    _env(monkeypatch, repo)

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo x"},
        "tool_use_id": "tu_1",
    }
    assert _run(monkeypatch, payload, "pre") == 0

    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    assert _run(monkeypatch, payload, "post") == 0

    compare = _verify_dir(repo) / "compare-tu_1.json"
    assert compare.exists(), f"比对结果没落盘：{compare}"
    report = json.loads(compare.read_text(encoding="utf-8"))
    assert "new.txt" in report["created"], report
    # 护栏自己的记账文件不是"被验证仓库的变化"。
    assert not [p for p in report["created"] if p.startswith(".pdt/")], report


def test_missing_tool_use_id_falls_back_to_last(tmp_path, monkeypatch):
    """缺 id → last.json 记着待比对的快照，post 靠它配对。"""
    repo = _init_repo(tmp_path / "repo")
    _env(monkeypatch, repo)

    payload = {"tool_name": "Bash", "tool_input": {"command": "echo x"}}
    assert _run(monkeypatch, payload, "pre") == 0

    evidence = _verify_dir(repo)
    assert (evidence / "last.json").exists()

    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    assert _run(monkeypatch, payload, "post") == 0

    compare = evidence / "compare-_pending.json"
    assert compare.exists(), f"没配对出比对结果：{sorted(p.name for p in evidence.iterdir())}"
    report = json.loads(compare.read_text(encoding="utf-8"))
    assert "new.txt" in report["created"], report


def test_next_pre_catches_up_on_an_uncompared_snapshot(tmp_path, monkeypatch):
    """post 没来过 → 下一次 pre 先把上一份快照补比对了。"""
    repo = _init_repo(tmp_path / "repo")
    _env(monkeypatch, repo)

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo x"},
        "tool_use_id": "tu_1",
    }
    assert _run(monkeypatch, payload, "pre") == 0

    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    # 第二次 pre：先补比对上一份，再拍自己这份。
    assert _run(monkeypatch, payload, "pre") == 0

    compare = _verify_dir(repo) / "compare-tu_1.json"
    assert compare.exists(), "下一次 pre 没有补比对"
    report = json.loads(compare.read_text(encoding="utf-8"))
    assert "new.txt" in report["created"], report


# ---------------------------------------------------------------------------
# 钩子不得阻断正常调用
# ---------------------------------------------------------------------------


def test_invalid_stdin_exits_zero(tmp_path, monkeypatch):
    """stdin 不是合法 JSON → exit 0，且什么也不拍。"""
    repo = _init_repo(tmp_path / "repo")
    _env(monkeypatch, repo)

    assert _run(monkeypatch, "not json at all", "pre") == 0
    assert not (repo / ".pdt").exists(), "看不懂的 payload 不该留下任何记账"


def test_empty_stdin_exits_zero(tmp_path, monkeypatch):
    """stdin 是空的 → exit 0，且什么也不拍。"""
    repo = _init_repo(tmp_path / "repo")
    _env(monkeypatch, repo)

    assert _run(monkeypatch, "", "pre") == 0
    assert not (repo / ".pdt").exists()


def test_without_write_scope_no_snapshot_is_taken(tmp_path, monkeypatch):
    """没声明可写范围 → 不拍快照、exit 0（旧调用方零影响）。"""
    repo = _init_repo(tmp_path / "repo")
    _env(monkeypatch, repo)
    monkeypatch.delenv("PDT_VP_WRITE_SCOPE", raising=False)

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo x"},
        "tool_use_id": "tu_1",
    }
    assert _run(monkeypatch, payload, "pre") == 0
    assert not (repo / ".pdt").exists(), "没声明范围就不该拍快照"


# ---------------------------------------------------------------------------
# 接进 settings.json
# ---------------------------------------------------------------------------


def test_settings_json_lists_post_tool_use_hook():
    """build() 写出来的那份 settings.json 里有 PostToolUse 条目。"""
    builder = SettingsJsonBuilder(vp_id="VP-055", plan_id="p")
    path = builder.build()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    finally:
        builder.cleanup()

    entries = data.get("hooks", {}).get("PostToolUse")
    assert entries, "settings.json 里没有 PostToolUse 条目"

    commands = [
        hook["command"] for entry in entries for hook in entry.get("hooks", [])
    ]
    assert commands, "PostToolUse 条目里没有命令"
    assert any(POST_PHASE_PREFIX in command for command in commands), commands
    assert any(
        "verification_tree_guard.py" in command for command in commands
    ), commands
    # 一个阶段赋值就够 —— 两个的话，后一个会盖掉前一个，post 就会跑成 pre。
    for command in commands:
        assert command.count("PDT_TREE_GUARD_PHASE=") == 1, command


def test_hook_registry_grows_by_exactly_one():
    """12 → 11：摘掉的是那条已无判定的 Bash 写范围钩子，树快照钩子还在。

    2026-11-05（任务 17）：``verifier_bash_write_block`` 已摘除 —— Bash 分支
    恒放行（判定改成事后测量，任务 7），留着它只会白起一个 Python 进程。
    其余一条不少：树快照钩子（``tree_snapshot_pre``）在册，写范围钩子
    （``verifier_write_scope_block``）只挂在 Edit|Write 上。
    """
    names = [rule["name"] for rule in _HOOK_RULES]

    assert len(_HOOK_RULES) == 11, names
    assert TREE_GUARD_NAME in names, names
    assert BASH_GUARD_NAME not in names, names

    rule = next(r for r in _HOOK_RULES if r["name"] == TREE_GUARD_NAME)
    assert rule["matcher"] == "Bash|Edit|Write"
    assert "verification_tree_guard.py" in rule["command"], rule["command"]
    # pre 这一侧也要把阶段点明 —— 否则脚本不知道自己是哪一半。
    assert "PDT_TREE_GUARD_PHASE=pre" in rule["command"], rule["command"]
    assert rule["command"].count("PDT_TREE_GUARD_PHASE=") == 1, rule["command"]


def test_the_built_hooks_run_end_to_end(tmp_path, monkeypatch):
    """真正跑一次：build() 出来的两条命令，拍快照、比对，都落盘。

    这一条是黑盒 —— 命令从 settings.json 里读出来，用 shell 跑，payload 走
    stdin，和 Claude Code 跑钩子的方式一样。少了它，"命令拼错了但测试照样
    绿"这种事就没人拦得住。
    """
    repo = _init_repo(tmp_path / "repo")
    plan = tmp_path / "plan"
    plan.mkdir()

    builder = SettingsJsonBuilder(
        vp_id="VP-055", plan_id="p", plan_dir=plan, round_no=1
    )
    path = builder.build()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    finally:
        builder.cleanup()

    pre_command = next(
        hook["command"]
        for entry in data["hooks"]["PreToolUse"]
        for hook in entry["hooks"]
        if "verification_tree_guard.py" in hook["command"]
    )
    post_command = next(
        hook["command"]
        for entry in data["hooks"]["PostToolUse"]
        for hook in entry["hooks"]
    )

    env = os.environ.copy()
    for name in list(env):
        if name.startswith("PDT_VP_") or name == "PDT_PROJECT_DIR":
            env.pop(name, None)
    env.update({key: str(value) for key, value in data.get("env", {}).items()})
    env["PDT_PROJECT_DIR"] = str(repo)

    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo x"},
            "tool_use_id": "tu_42",
        }
    )

    before = subprocess.run(
        pre_command,
        shell=True,
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert before.returncode == 0, before.stderr
    assert json.loads(before.stdout)["phase"] == "pre", before.stdout

    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    after = subprocess.run(
        post_command,
        shell=True,
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert after.returncode == 0, after.stderr
    assert json.loads(after.stdout)["phase"] == "post", after.stdout

    compare = _verify_dir(repo) / "compare-tu_42.json"
    assert compare.exists(), sorted(p.name for p in _verify_dir(repo).iterdir())
    report = json.loads(compare.read_text(encoding="utf-8"))
    assert "new.txt" in report["created"], report
