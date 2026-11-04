"""跨行引号 —— ``bash_write_targets`` 的假阳性与配套的假阴性。

背景（2026-11-04）
------------------
计划 ``20261102-verification-write-s`` 的 round 1 留下了五条真实拒绝记录，
被拒的"写目标"全是**解析产物**，不是文件：

===========================  ===========================================
被拒目标                       来源
===========================  ===========================================
``<worktree>/len``             ``python3 -c "…"`` 里多行 Python 的 ``> len(…)``
``<worktree>/not``             同族（``if a > not:``）
``<worktree>/spec:``           heredoc 正文里的 ``-> spec:``
``<worktree>/s/vgw/vwg/g``      ``sed -i '' 's/vgw/vwg/g'`` 的表达式
``<worktree>/2>``              ``cp … 2>&1`` 的 ``2>`` 被当成了 cp 的操作数
===========================  ===========================================

护栏据此拒绝、证据入账，五个本来 PASSED 的 VP 被改判成 ``verifier_mutated_tree``
—— 而被验证的仓库一个字节都没变过。

三类来源，三种修法
------------------
1. **跨行引号**（本文件的主线）：``python3 -c "`` 开引号之后，到闭引号为止的
   每一行都属于**同一个参数**。逐行独立切词会把那些行当命令解析，Python 代码
   里的 ``>`` 被认成重定向。修法是逐行切词时带着"这一行开始时还开着哪个引号"
   的状态走。
2. **heredoc 正文**：正文不是命令 —— 这一条 ``main`` 上**已经**修好了
   （``_heredoc_delimiters``），本文件只钉住它不被引号状态机带坏。
3. **单行解析错**：``sed -i ''`` 的 BSD 备份后缀、``2>&1`` 的 ``2>`` 落进 cp
   的操作数 —— 跟引号无关，各自在自己的地方修。

三种都写着，因为它们**长得一样**（都是"凭空多出一个写目标"），混在一起就会
把"引号状态机"当成万能药 —— 而万能药会把真写一起放过去。

配套的假阴性
------------
同一个毛病还有另一半：``python3 -c '…' > escaped.py`` 里，闭引号**之后**的
``> escaped.py`` 是真的重定向，却因为逐行切词时被当成了引号里的数据而**没被
认出来** —— 真写漏过去了。修完必须认出来。

黑盒与库级
----------
两份都写：库级用例钉住"算出来的写目标是什么"，黑盒用例钉住"钩子真的会
拒绝 / 真的放行"（护栏的契约是退出码 + 拒绝证据，不是内部函数）。
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

import verification_write_guard  # noqa: E402

_HOOK_SCRIPT = _BACKEND_DIR / "verification_write_guard.py"

#: 被验证仓库的替身：真实的后端目录。
#:
#: 不能用 pytest 的 ``tmp_path`` 当仓库 —— macOS 上 ``tmp_path`` 在
#: ``$TMPDIR`` 下面，而临时目录是护栏无条件放行的地方，"写进仓库"会变成
#: "写进临时目录"，测出来的是另一回事。
_REPO_DIR = _BACKEND_DIR

#: 被验证仓库里的一个源文件（相对 ``_REPO_DIR``）。
_REPO_FILE = "agent.py"

#: 相对路径解析用的 cwd（不写任何东西，只用来解析相对路径）。
_CWD = "/tmp/vpw-cwd"


def _targets(command: str, cwd: str = _CWD):
    """The write targets the guard works out for one command."""
    return verification_write_guard.bash_write_targets(command, cwd)


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


def _refusal_paths(plan_dir, round_no=1, vp_id="VP-999"):
    """The paths the guard recorded as refused."""
    marker = (
        plan_dir
        / "vp_write_scope"
        / "refusals"
        / f"round-{round_no}"
        / f"{vp_id}.jsonl"
    )
    if not marker.is_file():
        return []
    paths = []
    for line in marker.read_text(encoding="utf-8").splitlines():
        if line.strip():
            paths.append(json.loads(line)["path"])
    return paths


# ---------------------------------------------------------------------------
# 正控（假阳性）：跨行引号里的内容是数据，不是命令行
# ---------------------------------------------------------------------------
def test_multiline_double_quoted_program_is_data():
    """``python3 -c "…"`` 里的多行 Python 不是命令行。

    ``if len(sys.argv) > 2:`` 里的 ``>`` 是 Python 的比较运算符。修之前这行
    被当成 shell 解析，写目标凭空多出 ``<cwd>/2:``。
    """
    command = (
        "python3 -c \"\n"
        "import sys\n"
        "if len(sys.argv) > 2:\n"
        "    print(sys.argv[0])\n"
        "\""
    )
    assert _targets(command) == []


def test_multiline_single_quoted_program_is_data():
    """单引号版本一样：``python3 -c '…'`` 里的 ``>`` 是数据。"""
    command = (
        "python3 -c '\n"
        "import sys\n"
        "if len(sys.argv) > 2:\n"
        "    print(len(sys.argv))\n"
        "'"
    )
    assert _targets(command) == []


def test_multiline_arrow_in_quoted_program_is_data():
    """``-> spec:`` 这种箭头也是数据 —— 它不是重定向。"""
    command = "python3 -c \"\nx -> spec:\nif a -> b:\n    pass\n\""
    assert _targets(command) == []


def test_multiline_sed_expression_is_data():
    """跨行的 ``sed 's/x/y/g'`` 表达式是数据。"""
    command = "sed 's/vgw\n/vwg/g'"
    assert _targets(command) == []


def test_quoted_program_with_a_real_redirect_keeps_only_the_real_one():
    """引号里的假重定向不产生目标，引号外的真重定向照旧认出来。"""
    command = (
        "python3 -c \"\n"
        "if n > 3:\n"
        "    pass\n"
        "\" > /tmp/vpw-real.txt"
    )
    assert _targets(command) == ["/tmp/vpw-real.txt"]


# ---------------------------------------------------------------------------
# 正控（假阴性）：闭引号之后的重定向必须认出来
# ---------------------------------------------------------------------------
def test_redirect_after_closing_quote_is_found():
    """``python3 -c '…' > escaped.py`` 的落点是真写，不能漏。

    修之前逐行切词，闭引号那一行 ``' > escaped.py`` 的 ``>`` 被算进了引号
    里的数据 —— 真写**没被认出来**，护栏放行。
    """
    command = "python3 -c '\nprint(1)\n' > escaped.py"
    assert _targets(command) == [f"{_CWD}/escaped.py"]


def test_redirect_after_closing_double_quote_is_found():
    """双引号版本同样。"""
    command = 'python3 -c "\nprint(1)\n" > escaped.py'
    assert _targets(command) == [f"{_CWD}/escaped.py"]


def test_redirect_before_opening_quote_is_kept():
    """行内引号没闭合时，引号**之前**的重定向仍然要留住。

    ``echo hi > real.txt "没闭合`` —— ``real.txt`` 是真写。
    """
    command = 'echo hi > real.txt "没闭合'
    assert _targets(command) == [f"{_CWD}/real.txt"]


# ---------------------------------------------------------------------------
# 负控：修前就对的写法，行为逐字不变
# ---------------------------------------------------------------------------
def test_plain_redirect_is_unchanged():
    """``echo hi > real.txt`` —— 修前修后都是 ``<cwd>/real.txt``。"""
    assert _targets("echo hi > real.txt") == [f"{_CWD}/real.txt"]


def test_heredoc_target_is_unchanged():
    """``cat > /tmp/x <<'EOF' … EOF`` 的落点不变，正文仍然不是命令。"""
    command = "cat > /tmp/x <<'EOF'\nif n > 3:\nEOF"
    assert _targets(command) == ["/tmp/x"]


def test_quoted_argument_with_gt_is_still_data():
    """``printf '%s' 'a>b' > out.txt`` —— 引号里的 ``>`` 是数据，落点是 out.txt。"""
    command = "printf '%s' 'a>b' > out.txt"
    assert _targets(command) == [f"{_CWD}/out.txt"]


def test_heredoc_body_quotes_do_not_break_the_outer_state():
    """heredoc 正文里的引号不打断外层的引号状态机。

    正文已经被整段跳过，所以正文里那个没配对的 ``'`` 不会把下一行拖进引号
    里 —— 下一行的 ``> real.txt`` 是真重定向，必须认出来。
    """
    command = "cat > /tmp/x <<'EOF'\nit's data\nEOF\necho hi > real.txt"
    assert _targets(command) == ["/tmp/x", f"{_CWD}/real.txt"]


def test_heredoc_body_with_arrow_is_still_data():
    """heredoc 正文里的 ``-> spec:`` 仍然不是重定向（``main`` 上已有的行为）。"""
    command = "cat > /tmp/ok.txt <<'EOF'\nif n > 3:\n  x -> spec:\nEOF"
    assert _targets(command) == ["/tmp/ok.txt"]


# ---------------------------------------------------------------------------
# 生产拒绝记录：五条被拒目标都不能再出现
# ---------------------------------------------------------------------------
def test_production_refusal_targets_are_gone():
    """round-1 的五条被拒目标一个都不能再产生。

    这五条是**解析产物**：``len`` / ``not`` 来自多行 Python 里的比较表达式，
    ``spec:`` 来自 heredoc 正文，``s/vgw/vwg/g`` 是 ``sed`` 的表达式，
    ``2>`` 是 ``2>&1`` 的文件描述符 —— 都不是文件。
    """
    commands = [
        # <worktree>/len —— 多行双引号里的 Python
        "python3 -c \"\nimport sys\nif len(sys.argv) > len(sys.argv):\n    pass\n\"",
        # <worktree>/not —— 同族
        "python3 -c '\nif x > not:\n    pass\n'",
        # <worktree>/spec: —— heredoc 正文
        "cat > /tmp/vpw-ok.txt <<'EOF'\nif n > 3:\n  x -> spec:\nEOF",
        # <worktree>/s/vgw/vwg/g —— sed 的表达式被当成了操作数
        "sed -i '' 's/vgw/vwg/g' /tmp/vp024_probe7.py",
        # <worktree>/2> —— 2>&1 的 2> 落进了 cp 的操作数
        'cp /tmp/a.log /tmp/b.log "/tmp/vpw-dest"/ 2>&1',
    ]
    forbidden = ("/len", "/not", "/spec:", "/s/vgw/vwg/g", "/2>")
    for command in commands:
        targets = _targets(command, cwd="/tmp/vpw-worktree")
        bogus = [target for target in targets if target.endswith(forbidden)]
        assert not bogus, f"命令仍产生假目标：{command!r} → {targets}"


def test_sed_bsd_backup_suffix_is_not_an_operand():
    """``sed -i '' 's/a/b/' file``：空串是备份后缀，不是脚本。

    不认它，表达式就会被当成被改写的文件 —— 写目标凭空多出
    ``<cwd>/s/vgw/vwg/g``。
    """
    command = "sed -i '' 's/vgw/vwg/g' /tmp/vp024_probe7.py"
    assert _targets(command, cwd="/tmp/vpw-worktree") == ["/tmp/vp024_probe7.py"]


def test_sed_in_place_without_suffix_still_finds_the_file():
    """负控：GNU 写法 ``sed -i 's/a/b/' file`` 的落点不变。"""
    command = "sed -i 's/vgw/vwg/g' /tmp/vp024_probe7.py"
    assert _targets(command, cwd="/tmp/vpw-worktree") == ["/tmp/vp024_probe7.py"]


def test_sed_in_place_with_attached_suffix_still_finds_the_file():
    """负控：``sed -i.bak 's/a/b/' file`` 的落点不变。"""
    command = "sed -i.bak 's/vgw/vwg/g' /tmp/vp024_probe7.py"
    assert _targets(command, cwd="/tmp/vpw-worktree") == ["/tmp/vp024_probe7.py"]


def test_sed_without_in_place_writes_nothing():
    """负控：``sed 's/a/b/' file``（没有 ``-i``）不改文件 → 没有写目标。"""
    command = "sed 's/vgw/vwg/g' /tmp/vp024_probe7.py"
    assert _targets(command, cwd="/tmp/vpw-worktree") == []


def test_descriptor_redirect_is_not_a_copy_operand():
    """``cp a b dest/ 2>&1``：``2>`` 是文件描述符重定向，不是 cp 的落点。

    把它当操作数，cp 的"落点"就成了 ``2>`` —— 写目标凭空多出
    ``<cwd>/2>``。
    """
    command = 'cp /tmp/a.log /tmp/b.log "/tmp/vpw-dest"/ 2>&1'
    assert _targets(command, cwd="/tmp/vpw-worktree") == ["/tmp/vpw-dest"]


def test_redirect_after_copy_operands_is_still_found():
    """负控：``cp a b > out`` 的落点是 ``out``，不是 ``b`` 也不是 ``>out``。"""
    command = "cp /tmp/a.log /tmp/b.log > out.txt"
    assert _targets(command, cwd="/tmp/vpw-worktree") == ["/tmp/vpw-worktree/out.txt"]


# ---------------------------------------------------------------------------
# 黑盒：钩子的契约是退出码 + 拒绝证据
# ---------------------------------------------------------------------------
def test_hook_allows_multiline_quoted_program(tmp_path):
    """多行引号里的 Python 不再让钩子拒绝一条没写仓库的命令。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    env = _hook_env(
        PDT_VP_WRITE_SCOPE=plan_dir,
        PDT_VP_ID="VP-999",
        PDT_VP_ROUND=1,
        PDT_PROJECT_DIR=_REPO_DIR,
    )
    command = (
        "python3 -c \"\n"
        "import sys\n"
        "if len(sys.argv) > 2:\n"
        "    print(sys.argv[0])\n"
        "\""
    )
    code, stderr = _run_hook(command, env, cwd=_CWD)
    assert code == 0, f"不该拒绝：{stderr}"
    assert _refusal_paths(plan_dir) == []
    assert not (plan_dir / "vp_write_scope").exists()


def test_hook_allows_multiline_quoted_program_in_repo_cwd(tmp_path):
    """同一条命令，但 cwd 在被验证仓库里 —— 修之前会被拒。

    修之前那行 Python 里的 ``>`` 被当成重定向，落点 ``<repo>/2:`` 落在被验证
    仓库里 → 钩子拒绝；修之后它是数据 → 放行。这条是"假阳性"的**生产入口**
    证据：走的是钩子脚本（退出码 + 拒绝证据），不是内部函数。
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    env = _hook_env(
        PDT_VP_WRITE_SCOPE=plan_dir,
        PDT_VP_ID="VP-999",
        PDT_VP_ROUND=1,
        PDT_PROJECT_DIR=_REPO_DIR,
    )
    command = (
        "python3 -c \"\n"
        "import sys\n"
        "if len(sys.argv) > 2:\n"
        "    print(sys.argv[0])\n"
        "\""
    )
    code, stderr = _run_hook(command, env, cwd=_REPO_DIR)
    assert code == 0, f"不该拒绝：{stderr}"
    assert _refusal_paths(plan_dir) == []


def test_hook_refuses_real_write_after_closing_quote(tmp_path):
    """闭引号之后的真写必须被拒 —— 修之前它是被放过去的（假阴性）。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    env = _hook_env(
        PDT_VP_WRITE_SCOPE=plan_dir,
        PDT_VP_ID="VP-999",
        PDT_VP_ROUND=1,
        PDT_PROJECT_DIR=_REPO_DIR,
    )
    target = _REPO_DIR / _REPO_FILE
    command = f"python3 -c '\nprint(1)\n' > {target}"
    code, stderr = _run_hook(command, env, cwd=_CWD)
    assert code == 2, f"真写必须被拒：{stderr}"
    assert _refusal_paths(plan_dir) == [str(target)]


def test_hook_still_allows_heredoc_to_tmp(tmp_path):
    """负控：只写 /tmp 的 heredoc 命令仍然放行、不留证据。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    env = _hook_env(
        PDT_VP_WRITE_SCOPE=plan_dir,
        PDT_VP_ID="VP-999",
        PDT_VP_ROUND=1,
        PDT_PROJECT_DIR=_REPO_DIR,
    )
    command = "cat > /tmp/vpw-hook.txt <<'EOF'\nif n > 3:\nEOF"
    code, stderr = _run_hook(command, env, cwd=_CWD)
    assert code == 0, f"不该拒绝：{stderr}"
    assert _refusal_paths(plan_dir) == []
