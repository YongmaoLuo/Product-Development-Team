"""heredoc 正文不是重定向 —— ``bash_write_targets`` 必须跳过正文。

背景（2026-11-04）
------------------
VP 子 agent 常用 ``python3 - <<'PY'`` 跑脚本。写目标解析器逐行 token 化，
把 heredoc 正文里**未加引号的** ``>`` 当成 shell 重定向：

    python3 - <<'PY'
    if len(x) > 0:
        pass
    PY

正文里的 ``> 0:`` 被解析成"重定向到 ``0:``"，于是这条完全合法的命令被算出
一个写目标 —— 落在被验证仓库里 → exit 2 → VP 的正常取证被自己的护栏挡下来，
VP 随后被改判 FAILED。

heredoc 正文是**字面数据**：里面的 ``>`` 是 Python 的比较运算符，不是 shell
的重定向。解析器必须跳过正文。

这一份钉住四件事：

* 单引号 / 双引号 / 不加引号的定界符都要认（``<<'PY'`` / ``<<"PY"`` /
  ``<<PY``）。
* 定界符可以跟 ``<<`` 在同一个 token 里，也可以隔一个空格。
* 只有「定界符单独成行」才结束正文；没终止就一直吞到命令末尾。
* 正文行原样置空（行数不变），**命令那一行**上的重定向照常算。

黑盒那一支（``test_heredoc_command_is_not_refused``）走的是钩子的真实契约
（退出码 + stderr）—— 修复前它会 exit 2。
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


def _hook_env(**overrides):
    """Build the environment for one hook invocation."""
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


# ---------------------------------------------------------------------------
# 正文里的 ``>`` 不是重定向
# ---------------------------------------------------------------------------
def test_heredoc_body_redirect_is_not_a_target():
    """正文里的 ``if len(x) > 0:`` 不产生写目标。

    修复前这里返回 ``['/tmp/wt/0:']`` —— 合法命令被算成往仓库里写。
    """
    import verification_write_guard

    command = "python3 - <<'PY'\nif len(x) > 0:\n    pass\nPY"
    assert verification_write_guard.bash_write_targets(command, "/tmp/wt") == []


def test_double_quoted_delimiter_is_skipped():
    """``<<"PY"`` 的正文同样要跳过。"""
    import verification_write_guard

    command = 'python3 - <<"PY"\nif len(x) > 0:\n    pass\nPY'
    assert verification_write_guard.bash_write_targets(command, "/tmp/wt") == []


def test_unquoted_delimiter_is_skipped():
    """``<<PY``（定界符不加引号）的正文同样要跳过。"""
    import verification_write_guard

    command = "python3 - <<PY\nif len(x) > 0:\n    pass\nPY"
    assert verification_write_guard.bash_write_targets(command, "/tmp/wt") == []


def test_redirect_on_the_command_line_still_counts():
    """命令那一行上的重定向照常算 —— 跳过正文不是放过整个命令。

    ``python3 - > out.log <<'PY'`` 里的 ``> out.log`` 是真写目标。
    """
    import verification_write_guard

    command = "python3 - > out.log <<'PY'\nif len(x) > 0:\n    pass\nPY"
    assert verification_write_guard.bash_write_targets(command, "/tmp/wt") == [
        "/tmp/wt/out.log"
    ]


# ---------------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------------
def test_delimiter_separated_by_space_is_skipped():
    """定界符隔一个空格（``<< 'PY'``）也要认。"""
    import verification_write_guard

    command = "python3 - << 'PY'\nif len(x) > 0:\n    pass\nPY"
    assert verification_write_guard.bash_write_targets(command, "/tmp/wt") == []


def test_body_ends_only_on_a_delimiter_line():
    """只有「定界符单独成行」才结束正文。

    正文里出现 ``PY`` 但不是单独成行 → 正文继续，后面的 ``>`` 也不算数。
    """
    import verification_write_guard

    command = (
        "python3 - <<'PY'\n"
        "print('PY')\n"
        "if len(x) > 0:\n"
        "    pass\n"
        "PY\n"
    )
    assert verification_write_guard.bash_write_targets(command, "/tmp/wt") == []


def test_unterminated_heredoc_swallows_to_the_end():
    """没终止的 heredoc 一路吞到命令末尾。

    正文后面那行 ``> file`` 属于正文，不是命令 —— 算进去就是误判。
    """
    import verification_write_guard

    command = "python3 - <<'PY'\nif len(x) > 0:\n    pass\n"
    assert verification_write_guard.bash_write_targets(command, "/tmp/wt") == []


def test_strip_heredoc_bodies_keeps_the_line_count():
    """正文行原样置空串，行数不变。"""
    import verification_write_guard

    lines = ["python3 - <<'PY'", "if len(x) > 0:", "    pass", "PY", "echo hi"]
    stripped = verification_write_guard._strip_heredoc_bodies(lines)

    assert len(stripped) == len(lines)
    assert stripped[0] == "python3 - <<'PY'"
    assert stripped[1] == ""
    assert stripped[2] == ""
    assert stripped[3] == ""
    # 正文之后的命令照常。
    assert stripped[4] == "echo hi"


def test_redirect_after_a_closed_heredoc_still_counts():
    """heredoc 正常结束后，后面的命令照常解析。"""
    import verification_write_guard

    command = "python3 - <<'PY'\nif len(x) > 0:\n    pass\nPY\necho x > out.log"
    assert verification_write_guard.bash_write_targets(command, "/tmp/wt") == [
        "/tmp/wt/out.log"
    ]


def test_redirect_glued_to_the_heredoc_opener_still_counts():
    """``cat <<EOF>out.log`` 的落点是 ``out.log``。

    bash 的词法把 ``<<EOF`` 和 ``>out.log`` 切成两截：定界符是 ``EOF``，
    重定向的目标是 ``out.log``。定界符不能把 ``>out.log`` 一起吞掉。
    """
    import verification_write_guard

    command = "cat <<EOF>out.log"
    assert verification_write_guard.bash_write_targets(command, "/tmp/wt") == [
        "/tmp/wt/out.log"
    ]


def test_here_string_is_not_a_heredoc():
    """``<<<``（here-string）没有正文，不当 heredoc 处理。

    它后面的内容是**数据**，不是命令 —— 里面不该冒出写目标。
    """
    import verification_write_guard

    command = 'cat <<<"hello > world"'
    assert verification_write_guard.bash_write_targets(command, "/tmp/wt") == []


# ---------------------------------------------------------------------------
# 黑盒：合法命令不再被自己的护栏挡下来
# ---------------------------------------------------------------------------
def test_heredoc_command_is_not_refused(tmp_path):
    """heredoc 正文里的 ``>`` 不该让钩子 exit 2。

    修复前这条命令被算成"写进被验证仓库" → exit 2，VP 的正常取证被挡。
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    env = _hook_env(
        PDT_VP_WRITE_SCOPE=plan_dir,
        PDT_VP_ID="VP-055",
        PDT_VP_ROUND=3,
        PDT_PROJECT_DIR=_REPO_DIR,
    )
    command = "python3 - <<'PY'\nif len(x) > 0:\n    pass\nPY"

    code, stderr = _run_hook(command, env, cwd=_REPO_DIR)

    assert code == 0, stderr
    assert not (plan_dir / "vp_write_scope").exists()
