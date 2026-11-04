"""存证 → 回滚 → 记账 —— 测量到树被改之后的补救动作。

背景
----
缺陷 A 第 3 条：测量到树被改之后，光"记下改了哪些文件"不够 ——
**回滚之后就重建不出 patch 了**（diff 是"改之前"和"改之后"两张快照之差，
树一复原，差就没了）。所以顺序必须是：

  1. **存证** —— 把 diff 写下来（``<root>/.pdt/evidence/mutations/round-<n>/<n>.patch``）；
  2. **回滚** —— 把树复原（tracked 还原、新建文件删除）；
  3. **记账** —— 把这一轮发生过什么写进判定层能读的账本
     （``<plan>/.pdt/vp_write_scope/mutations/round-<n>/<vp>.jsonl``）。

这份测试钉四件事
----------------
  1. ``test_patch_written_before_restore`` —— patch 在回滚之前落盘，里面还有 diff；
  2. ``test_restore_reverts_and_deletes`` —— 回滚之后 tracked 文件复原、新文件消失；
  3. ``test_ledger_record_has_all_fields`` —— 账本一条记录含全部字段；
  4. ``test_restore_failure_is_recorded_not_raised`` —— git checkout 失败 → 记
     ``restore_failed``，不抛异常。

外加几条边界条件：patch 写失败仍然回滚（``patch=null`` + ``patch_error``）；
gitignore 内 / 树外的 created 路径不删；账本里的坏行跳过；证据不进被验证树的
可提交文件集合。

全部跑在 ``tmp_path`` 下的临时 git 仓库里 —— 被测的仓库一个字节都不碰。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

# ``backend/`` on sys.path so ``import verification_tree_guard`` works no
# matter which entry point runs the suite.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_tree_guard as guard  # noqa: E402
from tree_snapshot import snapshot  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _mutate(repo: Path) -> dict:
    """Do what a verifier's command did: rewrite a tracked file, add a new one.

    Returns the payload the post phase hands to :func:`record_mutation`.
    """
    before = snapshot(repo)
    (repo / "tracked.txt").write_text("tracked\nx\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    return {
        "root": str(repo),
        "command": "echo x >> tracked.txt",
        "tool_use_id": "tu_1",
        "created": ["new.txt"],
        "modified": ["tracked.txt"],
        "deleted": [],
        "before": guard.snapshot_to_dict(before),
    }


def _ledger_file(plan_dir: Path, round_number: int = 1, vp_id: str = "VP-001") -> Path:
    """Where the mutation ledger of one round and one VP lives."""
    return (
        plan_dir
        / ".pdt"
        / "vp_write_scope"
        / "mutations"
        / f"round-{round_number}"
        / f"{vp_id}.jsonl"
    )


def _records(plan_dir: Path, round_number: int = 1, vp_id: str = "VP-001"):
    """The ledger records, read back through the public reader."""
    return guard.read_mutations(plan_dir, round_number, vp_id)


# ---------------------------------------------------------------------------
# 1. 存证在回滚之前
# ---------------------------------------------------------------------------


def test_patch_written_before_restore(tmp_path):
    """patch 在回滚之前落盘 —— 里面还有 diff（回滚之后就重建不出来了）。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    payload = _mutate(repo)

    guard.record_mutation(plan_dir, 1, "VP-001", payload)

    records = _records(plan_dir)
    assert len(records) == 1, records
    patch = Path(records[0]["patch"])
    assert patch.exists(), f"patch 没落盘：{patch}"
    assert patch == repo / ".pdt" / "evidence" / "mutations" / "round-1" / "1.patch"

    text = patch.read_text(encoding="utf-8")
    # tracked 文件的改动在里面。
    assert "diff --git" in text, text
    assert "+x" in text, text
    # 新建的文件也在里面（不然"新建了什么"只剩一句自述）。
    assert "new.txt" in text, text
    assert "+new" in text, text

    # 树已经被复原了 —— patch 里的 diff 只可能来自回滚**之前**。
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "tracked\n"


def test_patch_is_a_real_git_patch(tmp_path):
    """patch 是一份真的 git patch：干净树上能 apply 回去。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    payload = _mutate(repo)

    guard.record_mutation(plan_dir, 1, "VP-001", payload)

    patch = Path(_records(plan_dir)[0]["patch"])
    # 回滚之后树已经回到改动之前的样子 —— patch 能把它打回改动之后。
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "tracked\n"
    assert not (repo / "new.txt").exists()

    applied = subprocess.run(
        ["git", "-C", str(repo), "apply", "--check", str(patch)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr


# ---------------------------------------------------------------------------
# 2. 回滚
# ---------------------------------------------------------------------------


def test_restore_reverts_and_deletes(tmp_path):
    """回滚之后：tracked 文件复原、新建文件消失。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    payload = _mutate(repo)

    guard.record_mutation(plan_dir, 1, "VP-001", payload)

    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "tracked\n"
    assert not (repo / "new.txt").exists(), "新建的文件没有被删掉"
    # 被验证树回到干净状态。
    assert _git(repo, "status", "--porcelain").strip() == "", "树没回到干净状态"


def test_restore_failure_is_recorded_not_raised(tmp_path):
    """git checkout 失败 → 记 ``restore_failed``，不抛异常。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"

    # 把 tracked.txt 从索引和 HEAD 里摘掉 —— checkout 它必然失败。
    _git(repo, "rm", "--cached", "tracked.txt")
    _git(repo, "commit", "-m", "untrack")
    before = snapshot(repo)
    (repo / "tracked.txt").write_text("tracked\nx\n", encoding="utf-8")

    payload = {
        "root": str(repo),
        "command": "echo x >> tracked.txt",
        "tool_use_id": "tu_1",
        "created": [],
        "modified": ["tracked.txt"],
        "deleted": [],
        "before": guard.snapshot_to_dict(before),
    }

    # 不抛异常 —— 抛出来就等于"因为回滚失败所以树留着"。
    guard.record_mutation(plan_dir, 1, "VP-001", payload)

    records = _records(plan_dir)
    assert len(records) == 1, records
    assert records[0]["restore_failed"] == ["tracked.txt"], records[0]


# ---------------------------------------------------------------------------
# 3. 记账
# ---------------------------------------------------------------------------


def test_ledger_record_has_all_fields(tmp_path):
    """账本一条记录含 created/modified/deleted/patch/command/tool_use_id。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    payload = _mutate(repo)

    ledger = guard.record_mutation(plan_dir, 1, "VP-001", payload)

    assert ledger == _ledger_file(plan_dir)
    assert ledger.exists(), f"账本没落盘：{ledger}"
    records = _records(plan_dir)
    assert len(records) == 1, records
    record = records[0]
    assert record["vp_id"] == "VP-001"
    assert record["round"] == 1
    assert record["created"] == ["new.txt"]
    assert record["modified"] == ["tracked.txt"]
    assert record["deleted"] == []
    assert record["command"] == "echo x >> tracked.txt"
    assert record["tool_use_id"] == "tu_1"
    assert record["patch"], record
    assert Path(record["patch"]).exists(), record


def test_ledger_appends_one_line_per_mutation(tmp_path):
    """同一个 VP 的第二条改动追加成第二行，不覆盖第一条。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"

    guard.record_mutation(plan_dir, 1, "VP-001", _mutate(repo))
    payload = _mutate(repo)
    payload["tool_use_id"] = "tu_2"
    guard.record_mutation(plan_dir, 1, "VP-001", payload)

    records = _records(plan_dir)
    assert len(records) == 2, records
    assert [r["tool_use_id"] for r in records] == ["tu_1", "tu_2"]
    # 两条记录各有各的 patch。
    assert records[0]["patch"] != records[1]["patch"], records


def test_ledger_skips_broken_lines(tmp_path):
    """账本里的坏行跳过 —— 崩掉的一轮不该赔上整轮证据。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    guard.record_mutation(plan_dir, 1, "VP-001", _mutate(repo))

    ledger = _ledger_file(plan_dir)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write("{this is not json\n")
        handle.write("\n")

    records = _records(plan_dir)
    assert len(records) == 1, records
    assert records[0]["vp_id"] == "VP-001"


def test_records_of_other_vps_do_not_mix(tmp_path):
    """别的 VP 的记录不串到这个 VP。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"

    guard.record_mutation(plan_dir, 1, "VP-001", _mutate(repo))
    guard.record_mutation(plan_dir, 1, "VP-002", _mutate(repo))

    assert len(_records(plan_dir, 1, "VP-001")) == 1
    assert len(_records(plan_dir, 1, "VP-002")) == 1
    assert _records(plan_dir, 1, "VP-003") == []


# ---------------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------------


def test_patch_write_failure_still_restores(tmp_path, monkeypatch):
    """patch 写失败 → 仍然回滚，记录里 ``patch=null`` 且带 ``patch_error``。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    payload = _mutate(repo)

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(guard, "write_patch", boom)

    guard.record_mutation(plan_dir, 1, "VP-001", payload)

    records = _records(plan_dir)
    assert len(records) == 1, records
    assert records[0]["patch"] is None, records[0]
    assert "disk on fire" in records[0]["patch_error"], records[0]
    # 存证失败没有把回滚一起取消掉。
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "tracked\n"
    assert not (repo / "new.txt").exists()


def test_created_path_in_gitignore_is_not_deleted(tmp_path):
    """created 的路径在 gitignore 内 → 不删（那不是我们的东西）。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "ignore")
    (repo / "ignored.txt").write_text("keep\n", encoding="utf-8")

    payload = {
        "root": str(repo),
        "command": "echo keep > ignored.txt",
        "tool_use_id": "tu_1",
        "created": ["ignored.txt"],
        "modified": [],
        "deleted": [],
    }
    guard.record_mutation(plan_dir, 1, "VP-001", payload)

    assert (repo / "ignored.txt").exists(), "gitignore 里的文件被删了"


def test_created_path_outside_tree_is_not_deleted(tmp_path):
    """created 的路径在树外 → 不删（树外的路径不是这棵树的事）。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    outside = tmp_path / "escape.txt"
    outside.write_text("keep\n", encoding="utf-8")

    payload = {
        "root": str(repo),
        "command": "echo keep > ../escape.txt",
        "tool_use_id": "tu_1",
        "created": ["../escape.txt"],
        "modified": [],
        "deleted": [],
    }
    guard.record_mutation(plan_dir, 1, "VP-001", payload)

    assert outside.exists(), "树外的文件被删了"


def test_evidence_is_not_in_the_committable_file_set(tmp_path):
    """证据与账本都不进可提交文件集合。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    payload = _mutate(repo)

    guard.record_mutation(plan_dir, 1, "VP-001", payload)

    # 存证目录在被验证树里 —— 它必须是 git 看不见的。
    assert (repo / ".pdt" / "evidence").exists()
    assert _git(repo, "status", "--porcelain").strip() == "", (
        "证据进了被验证树的可提交文件集合"
    )
    # 账本目录在 plan 目录里 —— 它同样不能是可提交文件。
    assert _ledger_file(plan_dir).exists()
    assert _git(plan_dir, "status", "--porcelain").strip() == "", (
        "账本进了 plan 目录的可提交文件集合"
    )


def test_no_mutation_means_no_ledger(tmp_path):
    """没有改动 → 不写账本（账本记的是"发生过什么"，不是"每次调用"）。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    before = snapshot(repo)

    payload = {
        "root": str(repo),
        "command": "echo x",
        "tool_use_id": "tu_1",
        "created": [],
        "modified": [],
        "deleted": [],
        "before": guard.snapshot_to_dict(before),
    }
    guard.record_mutation(plan_dir, 1, "VP-001", payload)

    assert _records(plan_dir) == []


# ---------------------------------------------------------------------------
# post 阶段：存证 → 回滚 → 记账
# ---------------------------------------------------------------------------


def test_post_phase_stores_rolls_back_and_records(tmp_path, monkeypatch):
    """post 阶段跑完：树复原了、patch 落盘了、账本记上了。"""
    repo = _init_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo x >> tracked.txt"},
        "tool_use_id": "tu_1",
    }

    monkeypatch.setenv("PDT_VP_WRITE_SCOPE", str(plan_dir))
    monkeypatch.setenv("PDT_PROJECT_DIR", str(repo))
    monkeypatch.setenv("PDT_VP_ROUND", "1")
    monkeypatch.setenv("PDT_VP_ID", "VP-001")

    def _run(phase: str) -> int:
        monkeypatch.setenv("PDT_TREE_GUARD_PHASE", phase)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        return guard.main([])

    assert _run("pre") == 0

    (repo / "tracked.txt").write_text("tracked\nx\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    assert _run("post") == 0

    # 回滚。
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "tracked\n"
    assert not (repo / "new.txt").exists()
    # 存证。
    patch = repo / ".pdt" / "evidence" / "mutations" / "round-1" / "1.patch"
    assert patch.exists(), f"patch 没落盘：{patch}"
    # 记账。
    records = guard.read_mutations(plan_dir, 1, "VP-001")
    assert len(records) == 1, records
    assert records[0]["created"] == ["new.txt"]
    assert records[0]["modified"] == ["tracked.txt"]
    assert records[0]["command"] == "echo x >> tracked.txt"
    assert records[0]["tool_use_id"] == "tu_1"
