"""负控：护栏不许把合法的取证写入挡下来。

背景
----
写护栏（``backend/verification_write_guard.py``）的判据是"写到哪里"，不是
"写了没有"：plan 目录与临时目录放行，别处（尤其是被验证仓库）拒绝。任务 7
据此把越权过的 VP 改判成 FAILED（``verifier_mutated_tree``）。

但这条护栏一旦**误伤**，VP 就没法把证据落到 plan 目录里 —— 取证做不成，整轮
失真。所以本文件钉的是**负控**：护栏该放行的地方，它必须放行，而且结论不许
被改判。

本文件钉五件事
--------------
  1. ``test_edit_into_plan_dir_is_allowed`` —— 写 plan 目录的任意子目录
     （``vp_artifacts`` / ``logs`` / 顶层文件）→ exit 0；
  2. ``test_edit_into_tmp_is_allowed`` —— 写 ``/tmp`` 与子进程自己的
     ``tempfile.gettempdir()`` → exit 0；
  3. ``test_bash_write_to_tmp_is_allowed`` —— Bash 往临时目录写
     （``> /tmp/x.log`` / ``tee /tmp/x``）→ exit 0；
  4. ``test_heredoc_write_to_tmp_is_allowed`` —— heredoc 写临时脚本 →
     exit 0（正文是数据，不是命令）；
  5. ``test_passed_verdict_survives_without_refusals`` —— 上面这些合法写入
     之后没有拒绝记录 → 子 agent 的 PASSED 原样保留，不写 marker，被验证
     仓库一个字节都没变（``git status --porcelain`` 为空）。

断言走的是**生产入口**
---------------------
* 钩子命令来自任务 3 的 ``SettingsJsonBuilder.build()`` —— 子 agent 真正读的
  就是那份 settings.json，钩子只有在真的被写进去时才谈得上"接上了活路径"。
  钩子以子进程方式跑一遍，看退出码（0 放行 / 2 拒绝）。
* 结论那一段走任务 7 的最小 Orchestrator：``Orchestrator._delegate_to_sub_agent``。

别让"放行"这条断言变成空断言
----------------------------
plan 目录放在 pytest 的 ``tmp_path`` 下，而 ``tmp_path`` 本身通常就落在临时
目录里 —— 于是"写 plan 目录被放行"可能只是被**临时目录白名单**放行的，跟
plan 白名单无关。所以第一个用例额外钉一条：把临时目录白名单摘掉（把
``tmp_roots()`` 换成一个别的目录）之后再问一次 —— 仍然放行，才说明放行来自
plan 白名单本身。

变异对照
--------
把护栏在**副本**上改坏（仓库里的文件一个字节都没动），这四个用例分别被
逮住 —— 也就是说它们各自盯着自己那条边界，不是凑数的：

=====================================  ==========================================
变异                                    被逮住的用例
=====================================  ==========================================
plan 目录不再豁免                      ``test_edit_into_plan_dir_is_allowed``、
                                       ``test_passed_verdict_survives_without_refusals``
临时目录白名单消失                    ``test_edit_into_tmp_is_allowed``、
                                       ``test_passed_verdict_survives_without_refusals``
Bash 分支的两个出口（临时目录白名单、   ``test_bash_write_to_tmp_is_allowed``、
"不在被验证仓库里"）都没了              ``test_passed_verdict_survives_without_refusals``
heredoc 的正文又被当成命令解析         ``test_heredoc_write_to_tmp_is_allowed``
没有拒绝证据也改判                    ``test_passed_verdict_survives_without_refusals``
=====================================  ==========================================
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_write_guard  # noqa: E402
from verification_subagent import SettingsJsonBuilder  # noqa: E402

# 任务 7 的最小 Orchestrator —— 结论那一段走的是同一条生产路径，
# 不另起一份，免得两处替身漂移。
from tests.unit.test_verifier_mutated_tree_verdict import (  # noqa: E402
    MUTATION_EVENT,
    _delegate,
    _orchestrator,
)

#: 这个 VP 的编号 —— 拒绝证据按 VP 分文件。
VP_ID = "VP-001"

#: 这一轮的轮次号 —— 拒绝证据按轮次分目录。
ROUND_NO = 2


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path) -> Path:
    """The plan directory — the guard's one writable root."""
    plan = tmp_path / "plan"
    plan.mkdir(parents=True, exist_ok=True)
    return plan


@pytest.fixture
def repo(tmp_path) -> Path:
    """The repository under verification — a small, clean git repository."""
    path = tmp_path / "repo"
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(path, "add", "module.py")
    _git(
        path,
        "-c", "user.email=probe@example.com",
        "-c", "user.name=probe",
        "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "baseline",
    )
    return path


@pytest.fixture
def scratch(tmp_path) -> Path:
    """The temporary directory the hook subprocess sees as its own."""
    path = tmp_path / "scratch"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def settings(plan_dir):
    """任务 3 的 build() 产物 —— 子 agent 真正读的那份 settings.json."""
    builder = SettingsJsonBuilder(
        vp_id=VP_ID, plan_id="p1", plan_dir=plan_dir, round_no=ROUND_NO,
    )
    path = builder.build()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        yield data
    finally:
        builder.cleanup()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Run one git command in ``repo`` and return its stdout."""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"git {' '.join(args)} 失败：{completed.stderr}"
    )
    return completed.stdout


def _git_status(repo: Path) -> str:
    """``git status --porcelain`` —— an empty string means nothing changed."""
    return _git(repo, "status", "--porcelain")


def _guard_command(settings: Dict[str, Any], matcher: str) -> str:
    """The write-guard hook command the built settings.json carries."""
    commands = [
        entry["hooks"][0]["command"]
        for entry in settings.get("hooks", {}).get("PreToolUse", [])
        if entry.get("matcher") == matcher
    ]
    command = next(
        (c for c in commands if "verification_write_guard.py" in c), None,
    )
    assert command, f"{matcher} 钩子里没有接上写入护栏脚本：{commands}"
    return command


def _child_env(
    settings_env: Dict[str, Any], repo: Path, scratch: Path,
) -> Dict[str, str]:
    """The environment a hook subprocess runs with.

    Claude Code folds a settings file's ``env`` into the process environment
    and the hook subprocesses inherit it — so the merge result is copied
    here.  Any ``PDT_VP_*`` left over on the host is cleared first: it
    would otherwise decide an assertion on our behalf.

    ``TMPDIR`` points at the directory this test prepared for the child:
    the child's own ``tempfile.gettempdir()`` is then that directory (not
    the host's), which is what makes "the temporary directory" a precisely
    nameable boundary condition.  ``PDT_PROJECT_DIR`` points at the
    repository under verification — the Bash branch uses it to tell "does
    this command touch the tree under verification".
    """
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("PDT_VP_"):
            env.pop(name, None)
    env["TMPDIR"] = str(scratch)
    env["PDT_PROJECT_DIR"] = str(repo)
    for key, value in (settings_env or {}).items():
        env[str(key)] = str(value)
    return env


def _run_hook(command: str, payload: Dict[str, Any], env: Dict[str, str]) -> int:
    """Run a hook command the way Claude Code runs it.

    The hook reads the tool payload from stdin as JSON and exits 2 to block
    or 0 to allow.  Returns the process exit code.
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


def _child_tempdir(env: Dict[str, str]) -> Path:
    """The ``tempfile.gettempdir()`` the hook subprocess sees.

    The subprocess is asked, not the test process: both have the same
    ``TMPDIR``, so the answer is the one the subprocess really acts on.
    """
    completed = subprocess.run(
        [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return Path(completed.stdout.strip())


def _refusal_marker(plan_dir: Path, round_no: int = ROUND_NO) -> Path:
    """Where a refusal for this VP would be recorded."""
    return (
        verification_write_guard.refusal_dir(plan_dir, round_no)
        / f"{VP_ID}.jsonl"
    )


def _edit_payload(path: Path) -> Dict[str, Any]:
    """An Edit/Write payload naming ``path``."""
    return {"tool_name": "Edit", "tool_input": {"file_path": str(path)}}


def _bash_payload(command: str, cwd: Path) -> Dict[str, Any]:
    """A Bash payload running ``command`` in ``cwd``."""
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(cwd)},
    }


# ---------------------------------------------------------------------------
# 1. 写 plan 目录 → 放行
# ---------------------------------------------------------------------------
def test_edit_into_plan_dir_is_allowed(
    settings, plan_dir, repo, scratch, monkeypatch,
):
    """写 plan 目录的任意子目录 → 放行（exit 0）。"""
    command = _guard_command(settings, "Edit|Write")
    env = _child_env(settings.get("env", {}), repo, scratch)

    paths = [
        plan_dir / "vp_artifacts" / VP_ID / "citations.json",
        plan_dir / "logs" / "vp-001.log",
        plan_dir / "notes.md",
    ]
    exit_codes: List[int] = [
        _run_hook(command, _edit_payload(path), env) for path in paths
    ]

    assert all(rc == 0 for rc in exit_codes), (
        f"写 plan 目录必须放行，却拿到了 {[rc for rc in exit_codes if rc != 0]}"
    )
    assert not _refusal_marker(plan_dir).exists(), "放行就不该留下拒绝证据"

    # 别让上面那条变成空断言：把临时目录白名单摘掉再问一次 —— 仍然放行，
    # 才说明放行来自 plan 白名单本身（而不是 plan 目录恰好落在临时目录里）。
    monkeypatch.setenv(verification_write_guard.SCOPE_ENV, str(plan_dir))
    monkeypatch.setattr(
        verification_write_guard, "tmp_roots", lambda: [str(scratch)],
    )
    decision = verification_write_guard.check_edit_payload(
        _edit_payload(paths[0]),
    )
    assert decision.allowed, (
        "摘掉临时目录白名单之后 plan 目录必须仍然放行 —— 否则上面那条"
        "断言测的是临时目录白名单，不是 plan 白名单"
    )


# ---------------------------------------------------------------------------
# 2. 写临时目录 → 放行
# ---------------------------------------------------------------------------
def test_edit_into_tmp_is_allowed(settings, repo, scratch):
    """写临时目录（``/tmp`` 与子进程的 ``tempfile.gettempdir()``）→ 放行。"""
    command = _guard_command(settings, "Edit|Write")
    env = _child_env(settings.get("env", {}), repo, scratch)

    paths = [
        Path("/tmp") / f"vp_probe_{uuid.uuid4().hex[:12]}.py",
        _child_tempdir(env) / "vp_probe.py",
    ]
    exit_codes: List[int] = [
        _run_hook(command, _edit_payload(path), env) for path in paths
    ]

    assert all(rc == 0 for rc in exit_codes), (
        f"写临时目录必须放行，却拿到了 {[rc for rc in exit_codes if rc != 0]}"
    )


# ---------------------------------------------------------------------------
# 3. Bash 往临时目录写 → 放行
# ---------------------------------------------------------------------------
def test_bash_write_to_tmp_is_allowed(settings, repo, scratch):
    """Bash 往临时目录写（``> /tmp/x.log`` / ``tee /tmp/x``）→ 放行。"""
    command = _guard_command(settings, "Bash")
    env = _child_env(settings.get("env", {}), repo, scratch)

    unique = uuid.uuid4().hex[:12]
    targets = [
        Path("/tmp") / f"vp_probe_{unique}.log",
        Path("/tmp") / f"vp_probe_{unique}_tee.txt",
    ]
    shell_commands = [
        f"echo probe > {targets[0]}",
        f"echo probe | tee {targets[1]}",
    ]
    exit_codes: List[int] = [
        _run_hook(command, _bash_payload(shell, repo), env)
        for shell in shell_commands
    ]

    assert all(rc == 0 for rc in exit_codes), (
        f"Bash 写临时目录必须放行，却拿到了 "
        f"{[rc for rc in exit_codes if rc != 0]}"
    )
    for target in targets:
        assert not target.exists(), "命令不该真的被执行"


# ---------------------------------------------------------------------------
# 4. heredoc 写临时脚本 → 放行
# ---------------------------------------------------------------------------
def test_heredoc_write_to_tmp_is_allowed(settings, plan_dir, repo, scratch):
    """heredoc 写临时脚本 → 放行，不留拒绝证据。

    负控（2026-11-03，round 1 的 VP-022）：heredoc 的**正文**曾被逐行当成
    shell 命令 —— 正文里的 ``# -> spec: something`` 让 ``>`` 认成了重定向，
    ``spec:`` 认成了写目标（按 cwd 解析进被验证仓库），于是这条只写 /tmp
    的命令被拒（``refusals/round-1/VP-022.jsonl`` 里躺着一条
    ``<repo>/spec:``）。子 agent 连临时脚本都写不成，取证做不成。

    正文里必须同时放两种曾被误认的东西：``-> spec:``（``>`` 后面跟着一个
    不含 ``/`` 的词）与 ``<testsuites><testsuite``（``>`` 前面粘着标签）。
    """
    command = _guard_command(settings, "Bash")
    env = _child_env(settings.get("env", {}), repo, scratch)

    unique = uuid.uuid4().hex[:12]
    script = Path("/tmp") / f"vp_probe_{unique}.py"
    heredoc = (
        f"cat > {script} <<'PYEOF'\n"
        "rows = [1]\n"
        "# -> spec: something\n"
        '# <testsuites><testsuite name="x">\n'
        "PYEOF\n"
    )
    rc = _run_hook(command, _bash_payload(heredoc, repo), env)

    assert rc == 0, (
        "只写 /tmp 的 heredoc 必须放行，却拿到了 "
        f"{rc} —— 正文不是命令"
    )
    assert not _refusal_marker(plan_dir).exists(), "放行就不该留下拒绝证据"
    assert verification_write_guard.read_refusals(
        plan_dir, ROUND_NO, VP_ID,
    ) == [], "放行就不该留下拒绝记录"


# ---------------------------------------------------------------------------
# 5. 没有拒绝记录 → 原 PASSED 保持不变
# ---------------------------------------------------------------------------
def test_passed_verdict_survives_without_refusals(
    monkeypatch, tmp_path, settings, plan_dir, repo, scratch,
):
    """合法写入之后没有拒绝记录 → PASSED 原样保留，仓库也没被动过。"""
    edit_command = _guard_command(settings, "Edit|Write")
    bash_command = _guard_command(settings, "Bash")
    env = _child_env(settings.get("env", {}), repo, scratch)

    # 先做一遍那些**合法**的写入：plan 目录、临时目录、往临时目录写的 shell。
    unique = uuid.uuid4().hex[:12]
    legal_paths = [
        plan_dir / "vp_artifacts" / VP_ID / "citations.json",
        _child_tempdir(env) / "vp_probe.py",
        Path("/tmp") / f"vp_probe_{unique}.log",
    ]
    for path in legal_paths[:2]:
        rc = _run_hook(edit_command, _edit_payload(path), env)
        assert rc == 0, f"写 {path} 必须放行，却拿到了 {rc}"
    rc = _run_hook(
        bash_command,
        _bash_payload(f"echo probe > {legal_paths[2]}", repo),
        env,
    )
    assert rc == 0, f"往临时目录写的 shell 必须放行，却拿到了 {rc}"

    # 合法写入不留拒绝证据 —— 这正是"结论不该被改判"的前提。
    assert not _refusal_marker(plan_dir).exists(), "合法写入不该留下拒绝证据"
    assert verification_write_guard.read_refusals(
        plan_dir, ROUND_NO, VP_ID,
    ) == [], "合法写入不该留下拒绝记录"

    # 然后走生产入口：子 agent 交回 PASSED → 结论原样保留。
    orchestrator, original, events = _orchestrator(
        monkeypatch, tmp_path, round_no=ROUND_NO,
    )
    verdict = _delegate(orchestrator, VP_ID)

    assert verdict.verdict == "PASSED", verdict
    assert verdict is original, "没有拒绝证据时 verdict 原样返回，不重建"
    assert [event for event in events if event[1] == MUTATION_EVENT] == [], events

    # 被验证仓库一个字节都没变。
    assert _git_status(repo) == "", "合法写入不该动到被验证仓库"
