"""TDD spec — 轮次作用域的记录与读回（head_sha / 变更集）。

背景
----
复用判定要回答"上一轮的结论还算不算数"，前提是能说出"这一轮相对上一轮改
了什么"。这个变更集只有一个来源：**head_sha** —— 上一轮 HEAD 与本轮 HEAD
之间的 diff，再并上工作区里还没提交的脏文件。

本文件钉住记录文件的读写口径：

1. ``test_record_and_load_round_scope`` — 记一轮、读回那一轮，字段逐个对。
2. ``test_missing_scope_returns_none`` — 文件缺失 / 缺 ``rounds`` / 该轮不存在
   → ``None``；**不做跨轮回落**（问第 3 轮不能拿第 2 轮顶替）。
3. ``test_corrupt_file_returns_none`` — JSON 损坏 → ``None``。
4. ``test_dirty_files_merged_into_change_set`` — 变更集 = diff ∪ 脏文件。
5. ``test_recording_round_three_keeps_round_two`` — 多轮合并写入，不覆盖旧轮。

补充两条：``current_head_sha`` 在非仓库目录上返回 ``None``（不抛）；写入是
原子的（临时文件 + replace，目录里不留残骸）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification_round_scope import (  # noqa: E402
    SCOPE_FILE_NAME,
    changed_files,
    current_head_sha,
    load_round_scope,
    record_round_scope,
)


# ---------------------------------------------------------------------------
# 辅助：一个真的 git 仓库
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """在 ``repo`` 里跑一条 git 命令；失败即测试失败（错误原文带出来）。"""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed:\n{proc.stderr}"
    return proc.stdout


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c", "user.email=scope@example.invalid",
        "-c", "user.name=Scope",
        "-c", "commit.gpgsign=false",
        "commit", "-m", message,
    )
    return _git(repo, "rev-parse", "HEAD").strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "scope@example.invalid")
    _git(repo, "config", "user.name", "Scope")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


# ---------------------------------------------------------------------------
# 1. 记一轮、读回那一轮
# ---------------------------------------------------------------------------


def test_record_and_load_round_scope(tmp_path):
    """记录文件是 ``{"version": 1, "rounds": {"<n>": {...}}}``；读回的是那一轮。"""
    plan_dir = tmp_path / "plan"

    record_round_scope(plan_dir, 1, head_sha="bbb")
    record_round_scope(
        plan_dir,
        2,
        head_sha="aaa",
        prev_head_sha="bbb",
        changed=("backend/server.py",),
    )

    scope = load_round_scope(plan_dir, 2)
    assert scope is not None
    assert scope["round"] == 2
    assert scope["head_sha"] == "aaa"
    assert scope["prev_head_sha"] == "bbb"
    assert scope["changed"] == ["backend/server.py"]

    # 第 1 轮没有上一轮：``prev_head_sha`` 与 ``changed`` 都不写进去 ——
    # "没有"和"空"不是一回事，写成空值会让读回的人分不清。
    first = load_round_scope(plan_dir, 1)
    assert first is not None
    assert first["round"] == 1
    assert first["head_sha"] == "bbb"
    assert "prev_head_sha" not in first
    assert "changed" not in first

    # 落盘结构逐字固定。
    document = json.loads((plan_dir / SCOPE_FILE_NAME).read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert set(document["rounds"]) == {"1", "2"}
    assert document["rounds"]["2"] == {
        "round": 2,
        "head_sha": "aaa",
        "prev_head_sha": "bbb",
        "changed": ["backend/server.py"],
    }

    # 原子写：目录里不留临时文件残骸。
    assert sorted(p.name for p in plan_dir.iterdir()) == [SCOPE_FILE_NAME]


# ---------------------------------------------------------------------------
# 2. 读不到就是读不到 —— 不跨轮回落
# ---------------------------------------------------------------------------


def test_missing_scope_returns_none(tmp_path):
    """文件缺失 / 缺 ``rounds`` / 该轮不存在 → ``None``；不拿别的轮顶替。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    # 文件根本不存在。
    assert load_round_scope(plan_dir, 1) is None

    # 文件在，但没有 ``rounds`` 键。
    (plan_dir / SCOPE_FILE_NAME).write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert load_round_scope(plan_dir, 1) is None

    # ``rounds`` 在，但问的那一轮没有。
    record_round_scope(plan_dir, 1, head_sha="bbb")
    record_round_scope(
        plan_dir, 2, head_sha="aaa", prev_head_sha="bbb", changed=("backend/server.py",)
    )
    assert load_round_scope(plan_dir, 3) is None
    # 不跨轮回落：问第 3 轮不能拿第 2 轮顶替。
    assert load_round_scope(plan_dir, 3) != load_round_scope(plan_dir, 2)


# ---------------------------------------------------------------------------
# 3. 损坏的文件读不出来
# ---------------------------------------------------------------------------


def test_corrupt_file_returns_none(tmp_path):
    """JSON 损坏 → ``None``（不是异常，也不是半截数据）。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / SCOPE_FILE_NAME).write_text("{not json at all", encoding="utf-8")

    assert load_round_scope(plan_dir, 1) is None
    assert load_round_scope(plan_dir, 2) is None


# ---------------------------------------------------------------------------
# 4. 变更集 = diff ∪ 脏文件
# ---------------------------------------------------------------------------


def test_dirty_files_merged_into_change_set(tmp_path):
    """两个 commit 之间的 diff，加上工作区里没提交的改动（含未跟踪文件）。"""
    repo = _make_repo(tmp_path)
    (repo / "a.py").write_text("print(1)\n", encoding="utf-8")
    (repo / "b.py").write_text("print(2)\n", encoding="utf-8")
    first = _commit_all(repo, "first")

    (repo / "a.py").write_text("print(11)\n", encoding="utf-8")
    second = _commit_all(repo, "second")

    # 脏文件：b.py 改了没提交，c.py 是新文件（未跟踪）。
    (repo / "b.py").write_text("print(22)\n", encoding="utf-8")
    (repo / "c.py").write_text("print(3)\n", encoding="utf-8")

    changed = changed_files(repo, first, second)

    assert "a.py" in changed          # 来自 diff
    assert "b.py" in changed          # 来自脏文件（已跟踪、未提交）
    assert "c.py" in changed          # 来自脏文件（未跟踪）
    assert "print(3)" not in changed  # 认的是路径，不是内容


# ---------------------------------------------------------------------------
# 5. 合并写入，不覆盖旧轮
# ---------------------------------------------------------------------------


def test_recording_round_three_keeps_round_two(tmp_path):
    """记第 3 轮不动第 2 轮；记第 3 轮时上一轮的 head_sha 仍在。"""
    plan_dir = tmp_path / "plan"

    record_round_scope(plan_dir, 1, head_sha="ccc")
    record_round_scope(
        plan_dir, 2, head_sha="bbb", prev_head_sha="ccc", changed=("backend/server.py",)
    )
    record_round_scope(
        plan_dir, 3, head_sha="aaa", prev_head_sha="bbb", changed=("backend/agent.py",)
    )

    second = load_round_scope(plan_dir, 2)
    assert second is not None
    assert second["head_sha"] == "bbb"
    assert second["prev_head_sha"] == "ccc"
    assert second["changed"] == ["backend/server.py"]

    third = load_round_scope(plan_dir, 3)
    assert third is not None
    assert third["head_sha"] == "aaa"
    assert third["prev_head_sha"] == "bbb"
    assert third["changed"] == ["backend/agent.py"]

    # 三轮都在。
    document = json.loads((plan_dir / SCOPE_FILE_NAME).read_text(encoding="utf-8"))
    assert set(document["rounds"]) == {"1", "2", "3"}


# ---------------------------------------------------------------------------
# 补充：上一轮的 head_sha 是这一轮 prev_head_sha 的唯一来源
# ---------------------------------------------------------------------------


def test_prev_head_sha_taken_from_previous_round_record(tmp_path):
    """不给 ``prev_head_sha`` 时，它取自上一轮记录里的 ``head_sha``。"""
    plan_dir = tmp_path / "plan"

    record_round_scope(plan_dir, 1, head_sha="bbb")
    record_round_scope(plan_dir, 2, head_sha="aaa")

    scope = load_round_scope(plan_dir, 2)
    assert scope is not None
    assert scope["prev_head_sha"] == "bbb"


def test_missing_previous_round_leaves_prev_and_change_set_empty(tmp_path):
    """上一轮没有记录 → ``prev_head_sha`` 是 ``None``、变更集是空的。

    空的含义是"不知道变了什么"，不是"什么都没变" —— 读的人必须按前者处理。
    """
    plan_dir = tmp_path / "plan"

    record_round_scope(plan_dir, 2, head_sha="aaa")

    scope = load_round_scope(plan_dir, 2)
    assert scope is not None
    assert scope["head_sha"] == "aaa"
    assert "prev_head_sha" not in scope
    assert "changed" not in scope


# ---------------------------------------------------------------------------
# 补充：head_sha 读不出来时不抛
# ---------------------------------------------------------------------------


def test_current_head_sha_none_when_git_cannot_answer(tmp_path):
    """不是仓库 / 没有 git → ``None``，不抛异常。"""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert current_head_sha(plain) is None


def test_current_head_sha_reads_head_of_a_repo(tmp_path):
    """仓库里读得出 HEAD。"""
    repo = _make_repo(tmp_path)
    (repo / "a.py").write_text("print(1)\n", encoding="utf-8")
    sha = _commit_all(repo, "first")
    assert current_head_sha(repo) == sha


# ---------------------------------------------------------------------------
# 补充：轮次开始时确实记了
# ---------------------------------------------------------------------------


def test_start_verification_round_records_scope(tmp_path):
    """``start_verification_round`` 末尾把本轮作用域记下来。

    第 1 轮没有上一轮 → 没有 ``prev_head_sha``；第 2 轮的上一轮 HEAD 与
    变更集都从第 1 轮的记录接上。
    """
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = _make_repo(tmp_path)
    (project_dir / "a.py").write_text("print(1)\n", encoding="utf-8")
    first = _commit_all(project_dir, "first")

    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )
    agent.start_verification_round(1)

    scope = load_round_scope(plan_dir, 1)
    assert scope is not None
    assert scope["round"] == 1
    assert scope["head_sha"] == first
    assert "prev_head_sha" not in scope

    # 下一轮：多提交一个文件，变更集应当包含它。
    (project_dir / "b.py").write_text("print(2)\n", encoding="utf-8")
    second = _commit_all(project_dir, "second")
    agent.start_verification_round(2)

    scope = load_round_scope(plan_dir, 2)
    assert scope is not None
    assert scope["head_sha"] == second
    assert scope["prev_head_sha"] == first
    assert "b.py" in scope["changed"]
