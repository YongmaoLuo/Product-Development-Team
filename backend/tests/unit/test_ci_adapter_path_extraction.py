"""路径提取：token 首尾的括号不是路径的一部分。

为什么要有这条规则（2026-11-05，arch 决策点 2 —— 路径提取）
----------------------------------------------------------
``ci_entry`` 是**一条 shell 命令行**，不是一个参数列表。读它的人如果
不懂 shell 的分组语法，就会把分组符号当成路径的一部分。

``(test -e backend/.venv) && bash scripts/run_tests.sh`` 这条入口被
``shlex`` 切成 ``(test`` / ``-e`` / ``backend/.venv)`` / ``&&`` / ``bash``
/ ``scripts/run_tests.sh`` —— 右括号紧贴在 ``backend/.venv`` 后面，它是
shell 的分组符号，**不是路径的一部分**。旧读法只 rstrip 掉 ``;&|``，
括号留在 token 上，于是路径被读成 ``backend/.venv)``；执行期的
「引用路径存在性检查」拿着这个路径去 ``project_dir`` 下找，找不到，
于是报一条**假的**「路径不存在」—— 门禁明明跑得起来，却被自己的
读法判死。

剥掉 token 首尾的 ``(`` 与 ``)`` 之后，这条入口读出的路径就是
``backend/.venv`` 和 ``scripts/run_tests.sh`` —— 与命令真正引用的
东西一致。

本模块钉住四件事：

1. 括号紧贴路径 → 路径里没有多余的右括号（核心用例）；
2. 单独一个 ``)`` → 分隔符，**不产生路径**，且它后面的 token 是新命令
   的开头；
3. 边界：``$(dirname "$0")/ci_gha.sh`` 的读法**不变**（那里的 ``)`` 在
   token 中间，收的是命令替换，不是分组）；
4. 边界：``cd`` 之后的相对路径语义**不变**。
"""

from __future__ import annotations

import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_ci_runner import _referenced_paths  # noqa: E402


#: 一条内联复合命令 —— 右括号紧贴在路径后面。
GROUPED_ENTRY = "(test -e backend/.venv) && bash scripts/run_tests.sh"


def test_grouped_command_path_has_no_stray_parenthesis():
    """括号紧贴路径时，路径里不能留下右括号。"""
    paths = _referenced_paths(GROUPED_ENTRY)

    assert paths == ["backend/.venv", "scripts/run_tests.sh"], paths
    assert not any(")" in path for path in paths), paths


def test_parenthesis_only_token_is_skipped():
    """单独一个 ``)`` 是分隔符 —— 不产生路径，后面是新命令的开头。"""
    assert _referenced_paths(")") == []

    # 分隔符语义：``)`` 之后的 token 重新从命令开头读起，所以 ``cd``
    # 仍然生效 —— 读出来的路径相对于 ``cd`` 之后的目录。
    assert _referenced_paths(") cd backend && pytest state_machine/tests") == [
        "backend/state_machine/tests",
    ]


def test_command_substitution_reading_is_unchanged():
    """``$(dirname "$0")/ci_gha.sh`` 的读法不变。

    这里的 ``)`` 收的是**命令替换**，落在 token 中间而不是首尾 —— 只
    剥首尾的括号碰不到它。
    """
    assert _referenced_paths('$(dirname "$0")/ci_gha.sh') == ["$0)/ci_gha.sh"]


def test_relative_path_after_cd_is_unchanged():
    """``cd`` 之后的相对路径语义不变。"""
    assert _referenced_paths("cd backend && pytest state_machine/tests") == [
        "backend/state_machine/tests",
    ]