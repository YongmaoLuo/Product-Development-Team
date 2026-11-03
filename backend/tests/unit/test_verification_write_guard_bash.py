"""Bash 写目标检测 —— 子 agent 不能用 shell 绕过写护栏。

背景（2026-11-02）
------------------
任务 1 的护栏只拦 Edit/Write：子 agent 拿不到 Edit/Write 工具也照样能改源码，
``sed -i``、``> file``、``git checkout -- file`` 都写得动被验证仓库。

这一份把同一条边界搬到 shell 上：**写目标落在被验证仓库里 → 拒绝**；落在
plan 目录或临时目录 → 放行。

关键取舍
--------
* **只认解析得出来的写目标。** 命令看不懂（变量展开、复杂管道）就放行 ——
  护栏宁可漏一次，也不能把 VP 的正常取证挡下来。
* **只拦"写进被验证仓库"。** 仓库之外的落点（plan 目录、临时目录）一律
  放行；仓库这个边界来自计划书"禁止的写：被验证仓库内的任何受版本控制的
  文件"。
* **相对路径按 cwd 解析**，cwd 依次取 payload 的 ``tool_input.cwd``、
  ``PDT_PROJECT_DIR``、进程自己的 cwd。

这些断言走的是**黑盒**：钩子的契约是"退出码 + stderr + 拒绝证据"，让进程
真的产出它才是诚实的验法（同 ``test_verification_write_guard.py``）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_HOOK_SCRIPT = _BACKEND_DIR / "verification_write_guard.py"

#: 被验证仓库的替身：真实的后端目录。
#:
#: 不能用 pytest 的 ``tmp_path`` 当这个目录 —— macOS 上 ``tmp_path`` 在
#: ``$TMPDIR`` 下面，而 ``$TMPDIR`` 是护栏认的临时目录，于是"写进仓库"会
#: 变成"写进临时目录"而被放行，测出来的是另一回事。
_REPO_DIR = _BACKEND_DIR

#: 被验证仓库里的一个源文件（相对 ``_REPO_DIR``）。
_REPO_FILE = "agent.py"


def _hook_env(**overrides):
    """Build the environment for one hook invocation.

    先把四个 scope 变量清掉 —— 免得外层残留的值替某条断言做决定。
    """
    env = os.environ.copy()
    for name in (
        "PDT_VP_WRITE_SCOPE",
        "PDT_VP_ID",
        "PDT_VP_ROUND",
        "PDT_PROJECT_DIR",
    ):
        env.pop(name, None)
    for key, value in overrides.items():
        env[key] = str(value)
    return env


def _run_hook(command, env, cwd=None):
    """Run the guard as a black box on a Bash payload.

    Returns:
        ``(exit_code, stderr)``.
    """
    assert _HOOK_SCRIPT.is_file(), f"护栏脚本不存在：{_HOOK_SCRIPT}"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    if cwd is not None:
        payload["tool_input"]["cwd"] = str(cwd)
    proc = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return proc.returncode, proc.stderr


def _scoped_env(tmp_path, **extra):
    """The environment of a hooked verification sub-agent."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    env = _hook_env(
        PDT_VP_WRITE_SCOPE=plan_dir,
        PDT_VP_ID="VP-055",
        PDT_VP_ROUND=3,
        PDT_PROJECT_DIR=_REPO_DIR,
    )
    env.update({key: str(value) for key, value in extra.items()})
    return env, plan_dir


def _marker(plan_dir, round_no=3, vp_id="VP-055"):
    """The refusal marker path the guard is contracted to write."""
    return (
        plan_dir
        / "vp_write_scope"
        / "refusals"
        / f"round-{round_no}"
        / f"{vp_id}.jsonl"
    )


# ---------------------------------------------------------------------------
# 写进被验证仓库：拒绝
# ---------------------------------------------------------------------------
def test_redirect_into_project_is_refused(tmp_path):
    """``echo x > a.py`` 写进被验证仓库 → exit 2。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook("echo x > a.py", env, cwd=_REPO_DIR)

    assert code == 2, stderr


def test_git_checkout_is_refused(tmp_path):
    """``git checkout -- a.py`` 会改工作区 → exit 2。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook("git checkout -- a.py", env, cwd=_REPO_DIR)

    assert code == 2, stderr


def test_sed_in_place_is_refused(tmp_path):
    """``sed -i`` 原地改仓库里的源文件 → exit 2。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook(
        f"sed -i 's/a/b/' {_REPO_FILE}",
        env,
        cwd=_REPO_DIR,
    )

    assert code == 2, stderr


def test_tee_into_project_is_refused(tmp_path):
    """``tee`` 写进被验证仓库 → exit 2。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook(f"cat x | tee {_REPO_FILE}", env, cwd=_REPO_DIR)

    assert code == 2, stderr


def test_copy_into_project_is_refused(tmp_path):
    """``cp`` 的**目标**写进被验证仓库 → exit 2。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook(f"cp /tmp/x {_REPO_FILE}", env, cwd=_REPO_DIR)

    assert code == 2, stderr


def test_dd_of_into_project_is_refused(tmp_path):
    """``dd of=`` 写进被验证仓库 → exit 2。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook(
        f"dd if=/dev/zero of={_REPO_FILE}",
        env,
        cwd=_REPO_DIR,
    )

    assert code == 2, stderr


def test_git_add_is_refused(tmp_path):
    """``git add`` 写仓库状态 → exit 2。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook(f"git add {_REPO_FILE}", env, cwd=_REPO_DIR)

    assert code == 2, stderr


# ---------------------------------------------------------------------------
# 合法落点：放行
# ---------------------------------------------------------------------------
def test_redirect_to_tmp_is_allowed(tmp_path):
    """写临时目录 → exit 0。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook("echo x > /tmp/x", env, cwd=_REPO_DIR)

    assert code == 0, stderr


def test_redirect_to_plan_dir_is_allowed(tmp_path):
    """写 plan 目录 → exit 0。"""
    env, plan_dir = _scoped_env(tmp_path)
    code, stderr = _run_hook(
        f"echo x > {plan_dir / 'notes.md'}",
        env,
        cwd=_REPO_DIR,
    )

    assert code == 0, stderr


def test_pytest_run_is_allowed(tmp_path):
    """``pytest -q tests/unit`` 只读 → exit 0。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook("pytest -q tests/unit", env, cwd=_REPO_DIR)

    assert code == 0, stderr


def test_curl_is_allowed(tmp_path):
    """``curl`` 只读 → exit 0。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook(
        "curl -s https://example.com/a?b=c",
        env,
        cwd=_REPO_DIR,
    )

    assert code == 0, stderr


def test_copy_out_of_project_is_allowed(tmp_path):
    """``cp`` 的源在仓库里、**目标**在临时目录 → exit 0。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook(f"cp {_REPO_FILE} /tmp/x", env, cwd=_REPO_DIR)

    assert code == 0, stderr


def test_move_out_of_project_is_refused(tmp_path):
    """``mv`` 把仓库里的文件搬走 → exit 2（源也是写落点：文件从那儿没了）。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook(f"mv {_REPO_FILE} /tmp/x", env, cwd=_REPO_DIR)

    assert code == 2, stderr


def test_scope_off_is_noop(tmp_path):
    """没设 ``PDT_VP_WRITE_SCOPE`` → exit 0（旧调用方不受影响）。"""
    env = _hook_env(PDT_PROJECT_DIR=_REPO_DIR)
    code, stderr = _run_hook("echo x > a.py", env, cwd=_REPO_DIR)

    assert code == 0, stderr


def test_git_dash_c_paths_resolve_in_that_repo(tmp_path):
    """``git -C <repo>`` 的路径按**那个仓库**解析，不是按 shell 的 cwd。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook(
        f"git -C {_REPO_DIR} checkout -- a.py",
        env,
        # nosec B108 — 硬写的 /tmp 就是这里要的东西：cwd 必须在仓库之外，
        # 才能证明路径是按 git 自己的仓库目录解析的
        cwd="/tmp",  # nosec B108
    )

    assert code == 2, stderr


def test_git_dash_c_other_repo_is_allowed(tmp_path):
    """``git -C`` 指向仓库之外 → exit 0（动的是别人的仓库）。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook(
        "git -C /tmp/other-repo checkout -- a.py",
        env,
        cwd=_REPO_DIR,
    )

    assert code == 0, stderr


def test_unreadable_command_is_allowed(tmp_path):
    """解析不出来的命令 → exit 0（不误杀）。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook(
        "python -c \"import os; open(os.environ['OUT'],'w')\"",
        env,
        cwd=_REPO_DIR,
    )

    assert code == 0, stderr


# ---------------------------------------------------------------------------
# 拒绝证据
# ---------------------------------------------------------------------------
def test_refusal_is_recorded(tmp_path):
    """被拒的 Bash 写入留下证据：tool=Bash，path 指向被验证仓库。"""
    env, plan_dir = _scoped_env(tmp_path)
    code, stderr = _run_hook("echo x > a.py", env, cwd=_REPO_DIR)
    assert code == 2, stderr

    assert _marker(plan_dir).is_file(), (
        "拒绝必须留下证据：没有 marker 的话，这次越权写就事后无人查证。"
    )
    records = [
        json.loads(line)
        for line in _marker(plan_dir).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["tool"] == "Bash"
    assert records[0]["reason"] == "outside_scope"
    assert records[0]["path"].endswith("a.py")


def test_allowed_command_records_nothing(tmp_path):
    """放行的命令不留拒绝痕迹。"""
    env, plan_dir = _scoped_env(tmp_path)
    code, stderr = _run_hook("pytest -q tests/unit", env, cwd=_REPO_DIR)
    assert code == 0, stderr

    assert not (plan_dir / "vp_write_scope").exists()


# ---------------------------------------------------------------------------
# bash_write_targets：写目标清单本身
# ---------------------------------------------------------------------------
def test_bash_write_targets_lists_resolved_paths():
    """相对路径按 cwd 解析成绝对路径。"""
    import verification_write_guard

    targets = verification_write_guard.bash_write_targets(
        "echo x > a.py",
        "/repo",
    )
    assert "/repo/a.py" in targets


def test_bash_write_targets_ignores_read_only_commands():
    """只读命令没有写目标。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "pytest -q tests/unit",
        "/repo",
    ) == []


# ---------------------------------------------------------------------------
# 回归：``>|``（clobber）与 ``&>>`` 曾被解析错
#
# 2026-11-02，验证轮 round 1 的 VP-002 用负控挖出两个反例：
#
# * ``echo x >| server.py`` —— ``>|`` 是 bash 的 clobber 写法（照样写那个
#   文件），可解析器只认得 ``>``，``|`` 被当成管道分隔符，**目标整个丢掉**
#   → 越界写就这么漏了过去（``bash_write_targets`` 返回空列表）。
# * ``echo x &>> /tmp/x.log`` —— ``&>>`` 被拆成 ``&>`` 加一个 ``>``，目标
#   认成了那个 ``>`` 自己 → 落点算成仓库根下的 ``/>``，**合法的临时写被
#   当成越界写**。
#
# 下面每条正控在修复回退后都会转红（见本文件末尾的负控注释）。
# ---------------------------------------------------------------------------
def _refused_paths(plan_dir):
    """The paths the guard recorded as refused."""
    marker = _marker(plan_dir)
    records = [
        json.loads(line)
        for line in marker.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [record["path"] for record in records]


def test_clobber_redirect_into_project_is_refused(tmp_path):
    """``echo x >| server.py`` 写进被验证仓库 → exit 2。

    ``>|`` 与 ``>`` 写的是同一个文件；只认 ``>`` 等于给越界写留了一条路。
    """
    env, plan_dir = _scoped_env(tmp_path)
    code, stderr = _run_hook("echo x >| server.py", env, cwd=_REPO_DIR)

    assert code == 2, stderr
    # 拒绝的落点必须就是那个文件本身 —— 修复前目标整个丢了（压根没拒）。
    assert _refused_paths(plan_dir) == [str(_REPO_DIR / "server.py")]


def test_append_both_redirect_into_project_is_refused(tmp_path):
    """``echo x &>> server.py`` 写进被验证仓库 → exit 2。

    而且拒绝的落点必须是 ``server.py`` 本身：修复前落点被认成了
    ``<repo>/>server.py``（操作符的尾巴当成了文件名），拒是拒了，写的却是
    另一个地方 —— 证据是错的。
    """
    env, plan_dir = _scoped_env(tmp_path)
    code, stderr = _run_hook("echo x &>> server.py", env, cwd=_REPO_DIR)

    assert code == 2, stderr
    assert _refused_paths(plan_dir) == [str(_REPO_DIR / "server.py")]


def test_append_both_redirect_to_tmp_is_allowed(tmp_path):
    """``echo x &>> /tmp/x.log`` 写临时目录 → exit 0。

    负控：``&>>`` 的目标必须原样落在 ``/tmp`` 上，不能被认成仓库里的 ``>``。
    """
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook("echo x &>> /tmp/x.log", env, cwd=_REPO_DIR)

    assert code == 0, stderr


def test_clobber_redirect_to_tmp_is_allowed(tmp_path):
    """``echo x >| /tmp/x.log`` 写临时目录 → exit 0。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook("echo x >| /tmp/x.log", env, cwd=_REPO_DIR)

    assert code == 0, stderr


def test_bash_write_targets_recognises_clobber_operator():
    """``>|`` 的写目标必须和 ``>`` 一样被认出来。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "echo x >| /repo/backend/server.py",
        "/repo",
    ) == ["/repo/backend/server.py"]


def test_bash_write_targets_recognises_append_both_operator():
    """``&>>`` 的写目标必须是那个文件，不是操作符自己。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "echo x &>> /repo/backend/server.py",
        "/repo",
    ) == ["/repo/backend/server.py"]


def test_bash_write_targets_keeps_append_both_target_outside_repo():
    """负控：``&>> /tmp/x.log`` 的落点是 ``/tmp/x.log``，不是仓库里的 ``>``。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "echo x &>> /tmp/x.log",
        "/repo",
    ) == ["/tmp/x.log"]


def test_bash_write_targets_still_resolves_plain_redirect():
    """对照：``>`` 的老行为没被改坏。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "echo x > /repo/backend/server.py",
        "/repo",
    ) == ["/repo/backend/server.py"]
    assert verification_write_guard.bash_write_targets(
        "echo x >> /tmp/x.log",
        "/repo",
    ) == ["/tmp/x.log"]


# ---------------------------------------------------------------------------
# 分发：这条分支怎么被选中
# ---------------------------------------------------------------------------
def test_payload_without_tool_name_is_still_a_bash_call(tmp_path):
    """只有 ``tool_input.command``、没有 ``tool_name`` 的 payload → 仍然 exit 2。

    有的调用方只给 ``{"tool_input": {"command": "..."}}``。按 ``tool_name``
    判的话这种 payload 会被当成 Edit/Write 放过去 —— 那正是要堵的洞。
    """
    env, _plan = _scoped_env(tmp_path)
    payload = {
        "tool_input": {
            "command": "sed -i 's/a/b/' backend/app.py",
            "cwd": str(_REPO_DIR),
        }
    }
    proc = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert proc.returncode == 2, proc.stderr


def test_mode_bash_selects_the_branch(tmp_path):
    """``--mode bash`` 显式点名这条分支 → exit 2。"""
    env, _plan = _scoped_env(tmp_path)
    payload = {"tool_input": {"command": "echo x > a.py", "cwd": str(_REPO_DIR)}}
    proc = subprocess.run(
        [
            sys.executable,
            str(_HOOK_SCRIPT),
            "--mode",
            "bash",
            "--payload",
            json.dumps(payload),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert proc.returncode == 2, proc.stderr