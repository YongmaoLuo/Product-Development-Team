"""判定只测量，不预测 —— Bash 分支不再解析命令。

背景（2026-11-05）
------------------
缺陷 A 第 4 条：``bash_write_targets`` / ``_git_targets`` / ``_redirect_targets`` /
``_find_redirect`` 参与判定，就会持续产生解析类假阳性。解析器把 Python 代码里的
``>``（``if a > len(b):``）当成 shell 重定向，把 ``len`` / ``1:`` / ``index`` /
``single`` 当成写目标；护栏据此拒绝，一个字节都没改过被验证仓库的 VP 被改判成
FAILED（``failure_class=infra``）。

测量链路（任务 2 的树快照钩子 + 任务 3 的存证/回滚/记账）上线之后，判定不必再
猜：命令先放行，跑完再拿树的前后两张快照做差 —— "改了哪些文件"是**量出来的**，
不是从命令里**猜出来的**。猜错的代价是误杀（把 PASSED 改判成 FAILED），量错的
代价是漏看，而漏看还有回滚与账本兜着。

这份测试钉四件事
----------------
1. ``test_bash_write_is_allowed_and_measured`` —— 树内写命令 → 放行（0），而
   改动由 PostToolUse 的测量链路兜住：改过的文件被回滚，账本上留下一条记录；
2. ``test_edit_write_still_refused`` —— 树内 Edit 路径 → 仍然 2（判定没有被整体
   放宽：Edit/Write 分支原样）；
3. ``test_no_parser_call_in_decision_path`` —— 把四个解析函数换成"一调用就抛
   异常"，``check_bash_payload`` 仍然返回 0：判定路径上根本没有解析器；
4. ``test_five_known_shapes_do_not_produce_refusals`` —— round-1 真实出现过的
   五种形状（``2>`` / ``len`` / ``1:`` / ``index`` / ``single``）全部返回 0，
   且一条拒绝记录都不写。

夹具建在哪里
------------
被验证树的替身建在**启动时的家目录**下（``PDT_TEST_REAL_HOME``），不在 pytest 的
``tmp_path`` 里：``tmp_path`` 落在 ``$TMPDIR``，而临时目录是护栏无条件放行的
地方 —— 写进去会被当成"写临时目录"而放行，测出来的是另一回事（同
``test_verifier_write_scope_live_refusals.py`` 的理由）。``finally`` 里收掉它。

那个替身同时是一个临时 git 仓库：测量链路量的就是"命令改了哪些文件"，量的是
真树才作数。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_tree_guard as tree_guard  # noqa: E402
import verification_write_guard  # noqa: E402

#: The VP these samples belong to.
VP_ID = "VP-100"

#: The round the evidence belongs to.
ROUND_NO = 1

#: The name of the throw-away tree under the launch home.
TREE_NAME = "pdt-write-guard"

#: The five shapes round-1 really produced refusals for.
#:
#: ``2>`` 是 fd 重定向；``len`` / ``1:`` / ``index`` / ``single`` 是 Python
#: 代码片段 —— 旧解析器把它们认成重定向的落点，于是算出 ``<tree>/len`` 这种
#: 根本不存在的写目标。
KNOWN_SHAPES = (
    ("2>", "echo hi 2>/dev/null"),
    ("len", "python3 - <<'PY'\nif a > len(b):\n    pass\nPY\n"),
    ("1:", "python3 - <<'PY'\nif a > 1:\n    pass\nPY\n"),
    ("index", "python3 - <<'PY'\nif a > index(b):\n    pass\nPY\n"),
    ("single", "python3 - <<'PY'\nif a > single(b):\n    pass\nPY\n"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _real_home() -> Path:
    """The home this run was launched with — not the redirected one.

    ``install_hermetic_env()`` points ``$HOME`` at a ``mkdtemp`` directory
    inside the zone the write guard allows unconditionally, so a stand-in
    tree built there would be *allowed* to be written to and the case would
    assert nothing at all.
    """
    value = os.environ.get("PDT_TEST_REAL_HOME")
    if not value:
        raise RuntimeError(
            "PDT_TEST_REAL_HOME is unset; install_hermetic_env() must run "
            "before this module derives a path."
        )
    return Path(value)


def _bash_payload(command: str, cwd: Any) -> Dict[str, Any]:
    """A Bash payload running ``command`` in ``cwd``."""
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(cwd)},
    }


def _edit_payload(path: Any) -> Dict[str, Any]:
    """An Edit payload naming ``path``."""
    return {"tool_name": "Edit", "tool_input": {"file_path": str(path)}}


def _arm(monkeypatch, plan_dir: Path, project_dir: Path) -> None:
    """Set up the environment the guard and the ledger read.

    ``PDT_VP_WRITE_SCOPE`` 打开护栏并给出唯一可写根；``PDT_PROJECT_DIR`` 是被
    验证的树；``PDT_VP_ID`` / ``PDT_VP_ROUND`` 决定证据归谁、哪一轮。
    """
    monkeypatch.setenv(verification_write_guard.SCOPE_ENV, str(plan_dir))
    monkeypatch.setenv(verification_write_guard.VP_ID_ENV, VP_ID)
    monkeypatch.setenv(verification_write_guard.ROUND_ENV, str(ROUND_NO))
    monkeypatch.setenv("PDT_PROJECT_DIR", str(project_dir))


def _marker(plan_dir: Path) -> Path:
    """Where the guard records this VP's refusals for this round."""
    return (
        verification_write_guard.refusal_dir(plan_dir, ROUND_NO)
        / f"{VP_ID}.jsonl"
    )


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` inside ``repo`` and return its stdout."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


def _init_repo(repo: Path) -> Path:
    """Create a throw-away git repository with one tracked file."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_under_verification() -> Path:
    """The tree under verification — a git checkout outside every
    temporary root.

    ``tracked.txt`` 是被跟踪的文件（改它 → 测量链路要能复原它）；
    ``backend/server.py`` 是树内的一个源文件（Edit 用例拿它当越界落点）。
    """
    root = _real_home() / f"{TREE_NAME}-{uuid.uuid4().hex}"
    _init_repo(root)
    (root / "backend").mkdir()
    (root / "backend" / "server.py").write_text("VALUE = 1\n", encoding="utf-8")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """The plan directory — the guard's one writable root."""
    path = tmp_path / "plan"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# 1. 树内写命令 → 放行；改动由测量链路兜住
# ---------------------------------------------------------------------------
def test_bash_write_is_allowed_and_measured(
    monkeypatch, repo_under_verification, plan_dir,
):
    """树内写命令放行（0），而改动被测量、回滚、记账。

    判定的部分：``check_bash_payload`` 恒放行 —— 它不再问"这条命令会写哪儿"。

    兜住的部分：PostToolUse 的测量链路（任务 2 的快照 + 任务 3 的存证/回滚/
    记账）在命令跑完之后拿两张快照做差，把树复原，并把"改过什么"记进账本。
    放行因此不是"不管"，而是"换个时机管"。
    """
    repo = repo_under_verification
    _arm(monkeypatch, plan_dir, repo)

    command = "echo x >> tracked.txt"

    # 1. 判定：树内写命令放行
    assert verification_write_guard.check_bash_payload(
        _bash_payload(command, repo)
    ) == 0, "Bash 分支不再判定 —— 树内写命令必须放行"

    # 2. 测量：pre 拍快照 → 命令改了树 → post 比对
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": "tu_1",
    }
    tree_guard.run_pre(repo, ROUND_NO, "tu_1", payload)
    (repo / "tracked.txt").write_text("tracked\nx\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    tree_guard.run_post(repo, ROUND_NO, "tu_1", True, payload)

    # 3. 回滚：树复原了
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "tracked\n"
    assert not (repo / "new.txt").exists(), "新建的越权文件必须被删掉"

    # 4. 记账：判定层能读到"这一轮改过什么"
    records = tree_guard.read_mutations(plan_dir, ROUND_NO, VP_ID)
    assert len(records) == 1, records
    record = records[0]
    assert record["modified"] == ["tracked.txt"], record
    assert record["created"] == ["new.txt"], record
    assert record["vp_id"] == VP_ID, record


# ---------------------------------------------------------------------------
# 2. Edit/Write 分支原样：树内 Edit → 仍然 2
# ---------------------------------------------------------------------------
def test_edit_write_still_refused(
    monkeypatch, repo_under_verification, plan_dir,
):
    """树内 Edit 路径 → 仍然 exit 2。

    负控：证明 Bash 分支的放行不是把护栏整体放宽了。Edit/Write 越界分支
    原样 —— 树内 Edit 仍然被拒，仍然留下拒绝证据。
    """
    _arm(monkeypatch, plan_dir, repo_under_verification)
    target = repo_under_verification / "backend" / "server.py"

    decision = verification_write_guard.check_edit_payload(
        _edit_payload(target),
    )
    assert decision.allowed is False, "树内 Edit 必须被拒"
    assert decision.reason == verification_write_guard.REASON_OUTSIDE_SCOPE

    # and the guard's own exit code stays 2
    code = verification_write_guard.main(
        ["--payload", json.dumps(_edit_payload(target))]
    )
    assert code == 2, f"树内 Edit 必须 exit 2，却拿到了 {code}"

    # the refusal is on record
    records = verification_write_guard.read_refusals(plan_dir, ROUND_NO, VP_ID)
    assert len(records) == 1, records
    assert records[0]["tool"] == "Edit", records
    assert records[0]["path"] == str(target), records


# ---------------------------------------------------------------------------
# 3. 判定路径上不再有解析器
# ---------------------------------------------------------------------------
def test_no_parser_call_in_decision_path(
    monkeypatch, repo_under_verification, plan_dir,
):
    """把解析器换成"一调用就抛异常"，判定仍然放行。

    这是"解析器退出判定路径"的硬证据：不看源码，只看行为 —— 判定路径上
    任何一个解析函数被调用到，这个用例就会炸。
    """
    _arm(monkeypatch, plan_dir, repo_under_verification)

    def _boom(*args: Any, **kwargs: Any):
        raise AssertionError("判定路径上不该再调用命令解析器")

    monkeypatch.setattr(
        verification_write_guard, "bash_write_targets", _boom
    )
    monkeypatch.setattr(
        verification_write_guard, "_redirect_targets", _boom
    )
    monkeypatch.setattr(
        verification_write_guard, "_find_redirect", _boom
    )
    monkeypatch.setattr(
        verification_write_guard, "_git_targets", _boom
    )

    command = "echo x > backend/server.py"
    assert verification_write_guard.check_bash_payload(
        _bash_payload(command, repo_under_verification)
    ) == 0, "判定路径上不该再有解析器"


# ---------------------------------------------------------------------------
# 4. 五种历史形状：全部放行，且不写拒绝记录
# ---------------------------------------------------------------------------
def test_five_known_shapes_do_not_produce_refusals(
    monkeypatch, repo_under_verification, plan_dir,
):
    """``2>`` / ``len`` / ``1:`` / ``index`` / ``single`` → 全部 0，零拒绝记录。

    这五种形状是 round-1 真实产生过拒绝记录的那五种（见
    ``test_verifier_write_scope_live_refusals.py`` 的模块 docstring）。它们
    判定的落点都在被验证树里 —— 放行它们，Bash 分支就再也不会凭猜测拒绝。
    """
    _arm(monkeypatch, plan_dir, repo_under_verification)

    for name, command in KNOWN_SHAPES:
        assert verification_write_guard.check_bash_payload(
            _bash_payload(command, repo_under_verification)
        ) == 0, f"{name} 形状不该触发拒绝"

    # 没有拒绝记录 —— marker 文件连出现都没有
    assert not _marker(plan_dir).exists(), "不该留下拒绝证据"
    assert verification_write_guard.read_refusals(
        plan_dir, ROUND_NO, VP_ID
    ) == [], "不该留下拒绝记录"
