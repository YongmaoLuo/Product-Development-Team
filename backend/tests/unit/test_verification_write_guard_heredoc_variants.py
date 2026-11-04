"""heredoc 的四种变体 —— 剥离器必须一条一条钉住。

背景（2026-11-04）
------------------
任务 1 只用最简写法验证过剥离（``<<'PY'`` + 正文 + 定界符单独成行）。四种
变体一旦处理错，护栏两个方向都会出事：

* **误伤**：正文里的 ``>`` 被当成 shell 重定向 → 一条完全合法的命令被算成
  "往被验证仓库里写" → exit 2 → VP 的正常取证被自己的护栏挡下来。
* **漏放**：正文没被跳过 → 该拦的写目标没拦。

这一份钉住四种变体：

* ``<<-EOF``：bash 规矩里 ``<<-`` 会去掉正文和定界符行的**前导 tab** →
  定界符行可以带 tab 缩进。
* 一行两个 heredoc（``cat <<A <<B``）：两段正文按出现顺序依次消费。
* ``<<<``（here-string）：**没有正文**，不是 heredoc，也不改变后续行的识别。
* 未终止的 heredoc：其后所有行都算正文。

外加一条负控（``test_hook_still_refuses_a_real_write_in_the_same_command``）：
同一行里的**真实**重定向（``cat <<'PY' > agent.py``）仍然要被拒（exit 2）——
"跳过正文"不能把护栏整体放宽。

正文里的 ``>>`` / ``2>`` 一律不产出目标：那是字面数据里的比较运算符，不是
shell 的重定向。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

#: 被验证仓库的替身：真实的后端目录。
#:
#: 不能用 pytest 的 ``tmp_path`` 当这个目录 —— macOS 上 ``tmp_path`` 在
#: ``$TMPDIR`` 下面，而 ``$TMPDIR`` 是护栏认的临时目录，于是"写进仓库"会
#: 变成"写进临时目录"而被放行，测出来的是另一回事。
_REPO_DIR = _BACKEND_DIR


# ---------------------------------------------------------------------------
# 变体一：``<<-`` 的定界符行带 tab 缩进
# ---------------------------------------------------------------------------
def test_dash_delimiter_strips_tabs():
    """``<<-EOF`` 的定界符行可以带 tab 缩进，正文仍然要被跳过。

    bash 的 ``<<-`` 会去掉正文和定界符行的前导 tab，所以 ``\\tEOF`` 就是
    结束行。定界符没认出来 → 正文一路吞到命令末尾 → 后面的命令也不再被解析
    （连同真正的写目标一起丢掉）。
    """
    import verification_write_guard

    command = "cat <<-EOF > /tmp/o\n\tbody > x\n\tEOF\necho hi > out.txt\n"
    targets = verification_write_guard.bash_write_targets(command, "/wt")

    # 命令那一行的重定向是真的；正文里的 ``> x`` 不是。
    assert targets == ["/tmp/o", "/wt/out.txt"]
    assert "/wt/x" not in targets


# ---------------------------------------------------------------------------
# 变体二：一行两个 heredoc
# ---------------------------------------------------------------------------
def test_multiple_heredocs_consume_bodies_in_order():
    """``cat <<A <<B`` 的两段正文按出现顺序依次消费。

    bash 的读法是顺序的：先读到 ``A`` 结束第一段，再读到 ``B`` 结束第二段。
    两段都得跳过 —— 任何一段漏掉，正文里的 ``>`` 就会变成写目标。
    """
    import verification_write_guard

    command = (
        "cat <<A <<B\n"
        "body A > x\n"
        "A\n"
        "body B > y\n"
        "B\n"
        "echo hi > out.txt\n"
    )
    targets = verification_write_guard.bash_write_targets(command, "/wt")

    assert targets == ["/wt/out.txt"]
    assert "/wt/x" not in targets
    assert "/wt/y" not in targets


# ---------------------------------------------------------------------------
# 变体三：``<<<`` 是 here-string，不是 heredoc
# ---------------------------------------------------------------------------
def test_herestring_is_not_a_heredoc():
    """``<<<`` 没有正文 —— 它不改变后续行的识别。

    如果把 here-string 当成 heredoc，接下来那行就会被当成"正文"吃掉，
    真正的写目标跟着一起消失。
    """
    import verification_write_guard

    command = "cat <<< 'x > y'\necho hi > out.txt\n"
    targets = verification_write_guard.bash_write_targets(command, "/wt")

    assert targets == ["/wt/out.txt"]


# ---------------------------------------------------------------------------
# 变体四：未终止的 heredoc
# ---------------------------------------------------------------------------
def test_unterminated_heredoc_swallows_the_rest():
    """没有结束行 → 其后所有行都算正文。

    正文里的 ``>`` / ``>>`` / ``2>`` 一律不产出目标。
    """
    import verification_write_guard

    command = "cat <<EOF\nbody > x\nmore >> y\nnope 2> z\n"
    assert verification_write_guard.bash_write_targets(command, "/wt") == []


# ---------------------------------------------------------------------------
# 负控：跳过正文不等于放过整条命令
# ---------------------------------------------------------------------------
def test_hook_still_refuses_a_real_write_in_the_same_command(tmp_path, monkeypatch):
    """``cat <<'PY' > agent.py`` 里的 ``> agent.py`` 是真写入 → 仍然 exit 2。

    这是负控：证明"跳过 heredoc 正文"没有把护栏整体放宽。命令那一行上的
    重定向和正文无关，不管正文有没有被跳过，它都成立。
    """
    import verification_write_guard

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("PDT_VP_WRITE_SCOPE", str(plan_dir))
    monkeypatch.setenv("PDT_VP_ID", "VP-055")
    monkeypatch.setenv("PDT_VP_ROUND", "3")
    monkeypatch.setenv("PDT_PROJECT_DIR", str(_REPO_DIR))

    payload = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "cat <<'PY' > agent.py",
            "cwd": str(_REPO_DIR),
        },
    }

    assert verification_write_guard.check_bash_payload(payload) == 2

    # 拒绝留下了证据：不是"什么都没发生"。
    marker = plan_dir / "vp_write_scope" / "refusals" / "round-3" / "VP-055.jsonl"
    assert marker.is_file(), "拒绝证据没有落盘 —— 这次拒绝不算数"
