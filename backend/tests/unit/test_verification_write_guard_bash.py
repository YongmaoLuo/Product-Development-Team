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
# heredoc：正文是数据，不是命令
#
# 2026-11-03，验证轮 round 1 的 VP-022 用一条**只写 /tmp** 的命令被误拦
# （``refusals/round-1/VP-022.jsonl`` 里躺着一条 ``<repo>/spec:``）：
#
#     mkdir -p /tmp/vp022 && cat > /tmp/vp022/probe.py <<'PYEOF'
#     rows = [1]
#     # -> spec: something
#     # <testsuites><testsuite name="x">
#     PYEOF
#
# 护栏把命令逐行拆开当 shell 解析，于是**正文**也被当成了命令：``->`` 里的
# ``>`` 认成了重定向，``spec:`` 认成了写目标（按 cwd 解析进被验证仓库）；
# ``<testsuites><testsuite`` 同理，``<testsuite`` 成了写目标。
#
# heredoc 的正文是**数据**（子 agent 写的临时脚本），它不碰仓库。
# ---------------------------------------------------------------------------

#: 一条只写 /tmp 的 heredoc 命令 —— 正文里有两种曾被误认成重定向的东西。
_HEREDOC_COMMAND = (
    "mkdir -p /tmp/vp022 && cat > /tmp/vp022/probe.py <<'PYEOF'\n"
    "rows = [1]\n"
    "# -> spec: something\n"
    '# <testsuites><testsuite name="x">\n'
    "PYEOF\n"
)


def test_heredoc_body_is_not_parsed_as_shell():
    """heredoc 的正文不被当成命令 —— 只有命令行上的重定向算数。"""
    import verification_write_guard

    targets = verification_write_guard.bash_write_targets(
        _HEREDOC_COMMAND,
        "/repo",
    )
    assert targets == ["/tmp/vp022/probe.py"], targets


def test_heredoc_command_is_allowed(tmp_path):
    """只写 /tmp 的 heredoc 命令 → 放行，且不留拒绝证据。"""
    env, plan_dir = _scoped_env(tmp_path)
    code, stderr = _run_hook(_HEREDOC_COMMAND, env, cwd=_REPO_DIR)

    assert code == 0, stderr
    assert not (plan_dir / "vp_write_scope").exists(), (
        "放行的命令不该留下拒绝痕迹"
    )


def test_write_before_heredoc_is_still_refused(tmp_path):
    """负控：heredoc **之前**的真实重定向仍然被认出来、仍然被拒。

    正文该跳过，但命令行的落点一个都不能少 —— 否则这条修复就把护栏拆了。
    """
    env, plan_dir = _scoped_env(tmp_path)
    command = f"echo VP022_PROBE > {_REPO_FILE} && " + _HEREDOC_COMMAND
    code, stderr = _run_hook(command, env, cwd=_REPO_DIR)

    assert code == 2, stderr
    assert _refused_paths(plan_dir) == [str(_REPO_DIR / _REPO_FILE)]


def test_write_commands_before_heredoc_are_still_refused(tmp_path):
    """负控：算子**之前**的 tee / sed -i / cp / mv 目标仍然被拒。"""
    env, _plan = _scoped_env(tmp_path)
    commands = [
        f"cat x | tee {_REPO_FILE} && " + _HEREDOC_COMMAND,
        f"sed -i 's/a/b/' {_REPO_FILE} && " + _HEREDOC_COMMAND,
        f"cp /tmp/x {_REPO_FILE} && " + _HEREDOC_COMMAND,
        f"mv {_REPO_FILE} /tmp/x && " + _HEREDOC_COMMAND,
    ]

    for command in commands:
        code, stderr = _run_hook(command, env, cwd=_REPO_DIR)
        assert code == 2, (command, stderr)


def test_write_after_the_heredoc_ends_is_still_refused(tmp_path):
    """负控：heredoc **结束之后**的命令行不是正文 —— 它的真实写仍然被拒。"""
    env, _plan = _scoped_env(tmp_path)
    command = _HEREDOC_COMMAND + f"echo VP022_PROBE > {_REPO_FILE}\n"
    code, stderr = _run_hook(command, env, cwd=_REPO_DIR)

    assert code == 2, stderr


def test_refusal_record_carries_the_command(tmp_path):
    """拒绝证据里带着原始命令行 —— 事后要查"它到底想干什么"。"""
    env, plan_dir = _scoped_env(tmp_path)
    command = f"echo VP022_PROBE > {_REPO_FILE} && " + _HEREDOC_COMMAND
    code, stderr = _run_hook(command, env, cwd=_REPO_DIR)
    assert code == 2, stderr

    records = [
        json.loads(line)
        for line in _marker(plan_dir).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert "VP022_PROBE" in records[0]["command"], records


def test_refusal_record_command_is_truncated(tmp_path):
    """记录里的命令行截断到 500 字符 —— 一条命令不能把证据文件撑爆。"""
    env, plan_dir = _scoped_env(tmp_path)
    command = f"echo {'x' * 600} > {_REPO_FILE}"
    code, stderr = _run_hook(command, env, cwd=_REPO_DIR)
    assert code == 2, stderr

    records = [
        json.loads(line)
        for line in _marker(plan_dir).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert len(records[0]["command"]) == 500, records


def test_record_refusal_without_command_stays_compatible(tmp_path, monkeypatch):
    """旧的三参数调用照样能用 —— 记录里只是没有 ``command`` 这一项。"""
    import verification_write_guard

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    monkeypatch.setenv(verification_write_guard.SCOPE_ENV, str(plan_dir))
    monkeypatch.setenv(verification_write_guard.VP_ID_ENV, "VP-055")
    monkeypatch.setenv(verification_write_guard.ROUND_ENV, "3")

    marker = verification_write_guard.record_refusal(
        "Edit", str(tmp_path / "app.py"), "outside_scope",
    )

    assert marker is not None
    records = [
        json.loads(line)
        for line in marker.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert "command" not in records[0], records
    assert records[0]["tool"] == "Edit"
    assert records[0]["reason"] == "outside_scope"
    assert records[0]["vp_id"] == "VP-055"
    assert records[0]["round"] == 3


def test_here_string_is_not_a_heredoc():
    """``<<<`` 是 here-string：它不吞掉后面的行。"""
    import verification_write_guard

    targets = verification_write_guard.bash_write_targets(
        'cat <<< "hi"\necho x > /repo/backend/server.py\n',
        "/repo",
    )
    assert targets == ["/repo/backend/server.py"], targets


def test_heredoc_delimiter_quoting_variants():
    """``<<EOF`` / ``<<'EOF'`` / ``<<"EOF"`` / ``<<\\EOF`` 都认得出来。"""
    import verification_write_guard

    for operator in ("<<EOF", "<<'EOF'", '<<"EOF"', "<<\\EOF"):
        command = (
            f"cat > /tmp/x.py {operator}\n"
            "# -> spec: done\n"
            "EOF\n"
        )
        targets = verification_write_guard.bash_write_targets(command, "/repo")
        assert targets == ["/tmp/x.py"], (operator, targets)


def test_tab_stripped_heredoc_delimiter():
    """``<<-``：正文与分隔符行都可以带行首 tab。"""
    import verification_write_guard

    command = (
        "cat > /tmp/x.py <<-'PYEOF'\n"
        "\t# -> spec: done\n"
        "\tPYEOF\n"
    )
    targets = verification_write_guard.bash_write_targets(command, "/repo")
    assert targets == ["/tmp/x.py"], targets


def test_several_heredocs_in_one_command():
    """一条命令里多个 heredoc：正文按出现顺序各自跳过。"""
    import verification_write_guard

    command = (
        "cat <<'A' <<'B' > /tmp/x.py\n"
        "a > b\n"
        "A\n"
        "c < d\n"
        "B\n"
    )
    targets = verification_write_guard.bash_write_targets(command, "/repo")
    assert targets == ["/tmp/x.py"], targets


def test_redirect_next_to_the_heredoc_operator_is_still_a_target():
    """运算符**那一行**上的重定向仍然算数。"""
    import verification_write_guard

    targets = verification_write_guard.bash_write_targets(
        "cat <<'EOF' > /repo/backend/server.py\nbody\nEOF\n",
        "/repo",
    )
    assert targets == ["/repo/backend/server.py"], targets


def test_unclosed_heredoc_takes_the_rest_as_body():
    """分隔符没出现 → 剩下的行全是正文（bash 自己也不执行这条命令）。

    bash 遇到没闭合的 heredoc 是语法错误，整条命令都不跑；所以这里**不回退**
    成逐行解析 —— 回退等于把正文重新当命令，正是这次要修的那个错。
    """
    import verification_write_guard

    targets = verification_write_guard.bash_write_targets(
        "cat > /tmp/x.py <<'PYEOF'\necho y > /repo/backend/server.py\n",
        "/repo",
    )
    assert targets == ["/tmp/x.py"], targets


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

# ---------------------------------------------------------------------------
# git 的 dry-run：只读，不产生写目标
#
# 2026-11-04，验证 sub-agent 跑 ``git add -A --dry-run 2>&1 | head -n 40``
# 取证（纯只读），护栏却把 ``--dry-run`` 当普通 flag 跳过、把 ``-A`` 当成
# "写全部路径" → 拒绝并记一条 ``outside_scope`` → 该 VP 随后被框架改判
# ``verifier_mutated_tree``（``failure_class=infra``）。
#
# 修法：同一条 git 命令带 ``--dry-run`` / ``-n`` 时，它**不写**任何东西。
# 下面的负控钉住"不许放宽"：不带 dry-run 的同一条命令必须照旧被认出。
# ---------------------------------------------------------------------------
def test_git_dry_run_add_has_no_write_target():
    """``git add -A --dry-run`` 是只读 → 没有写目标。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "git add -A --dry-run",
        "/repo",
    ) == []


def test_git_dash_n_add_has_no_write_target():
    """``git add -A -n`` 是只读（``-n`` = ``--dry-run``）→ 没有写目标。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "git add -A -n",
        "/repo",
    ) == []


def test_git_dash_c_rm_dry_run_has_no_write_target():
    """``git -C /repo rm --dry-run x.py`` → 没有写目标。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "git -C /repo rm --dry-run x.py",
        "/repo",
    ) == []


def test_git_dash_c_add_dash_n_has_no_write_target():
    """flag 在子命令后面、``-C`` 在前面 → 一样是只读。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "git -C /repo add -n .",
        "/repo",
    ) == []


def test_git_commit_dry_run_has_no_write_target():
    """``git commit --dry-run`` 不写仓库 → 没有写目标。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "git commit --dry-run -m 'x'",
        "/repo",
    ) == []


def test_git_add_without_dry_run_still_has_write_target():
    """负控：不带 dry-run 的 ``git add -A`` 必须仍然被认成写。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "git add -A",
        "/repo",
    ) == ["/repo"]


def test_git_add_dash_n_without_dry_run_flag_still_has_write_target():
    """负控：``-n`` 只在 git 的语境里当 dry-run，其它命令不变。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "git add -A",
        "/repo",
    ) == ["/repo"]
    # ``cp -n`` 是 no-clobber（不覆盖），不是 dry-run → 落点仍是写目标。
    assert verification_write_guard.bash_write_targets(
        "cp -n /tmp/a /repo/b.py",
        "/repo",
    ) == ["/repo/b.py"]
    # ``sed -n`` 是"不打印"，跟写目标无关 → sed 没被认成写命令。
    assert verification_write_guard.bash_write_targets(
        "sed -n 1p /repo/b.py",
        "/repo",
    ) == []


def test_git_branch_creation_is_not_a_dry_run():
    """负控：``git checkout -n -b feature`` 建分支 → 真的是写。"""
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "git checkout -n -b feature",
        "/repo",
    ) == ["/repo"]


def test_heredoc_body_git_dry_run_has_no_write_target():
    """heredoc 正文里的 ``git add --dry-run`` 是数据，不是命令。"""
    import verification_write_guard

    command = (
        "mkdir -p /tmp/vp055 && cat > /tmp/vp055/probe.py <<'PYEOF'\n"
        "# git add -A --dry-run\n"
        "git add --dry-run\n"
        "PYEOF\n"
    )
    targets = verification_write_guard.bash_write_targets(command, "/repo")
    assert targets == ["/tmp/vp055/probe.py"], targets


def test_git_dry_run_is_allowed_by_the_hook(tmp_path):
    """生产入口：只读的 ``git add -A --dry-run`` → exit 0，不留拒绝证据。"""
    env, plan_dir = _scoped_env(tmp_path)
    code, stderr = _run_hook(
        "git add -A --dry-run 2>&1 | head -n 40",
        env,
        cwd=_REPO_DIR,
    )

    assert code == 0, stderr
    assert not _marker(plan_dir).exists(), "放行的命令不该留下拒绝痕迹"


def test_git_add_without_dry_run_is_still_refused_by_the_hook(tmp_path):
    """负控（生产入口）：真的 ``git add -A`` → exit 2，仍然被拒。"""
    env, _plan = _scoped_env(tmp_path)
    code, stderr = _run_hook("git add -A", env, cwd=_REPO_DIR)

    assert code == 2, stderr


def test_git_commit_dash_n_is_no_verify_and_still_writes():
    """负控：``git commit -n`` 是 ``--no-verify`` —— 它照样创建提交，仍算写。

    git 里 ``-n`` 确实是 dry-run 的只有 ``add`` / ``rm`` / ``mv`` / ``clean``；
    ``commit`` 的 ``-n`` 是 ``--no-verify``。当成 dry-run 就等于把一条真写
    命令放过 —— 护栏被削弱。
    """
    import verification_write_guard

    assert verification_write_guard.bash_write_targets(
        "git commit -n -m 'x'",
        "/repo",
    ) == ["/repo"]
