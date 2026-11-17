"""CommandRunner 的白名单、逐字审计与待批准分流。

What this file witnesses
------------------------
架构决策点 4 要求：所有 ``git`` / ``gh`` 调用经唯一入口执行；只放行冻结
要求书中逐字声明过的 argv；合入 main、破坏性操作、花钱三类只能转成
「请求批准」的待办，不由执行器执行。本文件钉住这三条的可观察后果：

1. **放行的那条真的跑了** —— 真实 ``git push`` 把分支推进裸仓，审计三元组
   完整（动作、argv 原文、目标）；
2. **没放行的一条一次都没跑** —— 未声明的 argv、非 ``git``/``gh`` 的二进制、
   字符串形态的 command，全部在 spawn 之前被拒，桩的调用日志为空；
3. **argv 逐字进审计** —— 审计里的 argv 与桩记录的 argv 逐字相等，且是
   argv 数组而不是拼起来的字符串；
4. **三类动作不执行** —— ``gh pr merge`` 之类返回「请求批准」，桩零调用；
5. **shell 元字符不展开** —— ``;``、``$(...)``、反引号只是 argv 片段。

工具边界用**真实子进程**证明（临时 git 仓 + 本地裸仓），只有「桩零调用」
这类断言用注入的 ``run`` 桩 —— 桩记录 argv，因此「零调用」是可数的，
而不是靠推断。

What this file does NOT test
----------------------------
产品代码零改动校验（``product_diff_guard``）是另一条任务的事；本文件只
关心命令入口本身。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from postprocess.executor import (
    CommandRejected,
    CommandRunner,
    NeedsApproval,
    execute,
)
from postprocess.models import AuditRecord, EvidenceRecord
from postprocess.spec import Spec, SpecItem


#: 放行的那条命令：把当前 HEAD 推进裸仓的 ``x`` 分支。
PUSH_COMMAND: List[str] = ["git", "push", "origin", "HEAD:refs/heads/x"]

#: 合入 main —— 三类必须转待批准的动作之一。
MERGE_COMMAND: List[str] = ["gh", "pr", "merge", "7", "--merge"]


@dataclass(frozen=True)
class FakeFrozenSpec:
    """任务 4 的 ``FrozenSpec`` 的替身。

    真身是 ``FrozenSpec(spec, spec_sha256, markdown_sha256, frozen_at)``，
    条目挂在 ``spec.items`` 上；替身保持同一个形状，好让本文件测的就是
    「条目藏在 ``.spec.items`` 里」这条读取路径，而不是一个更方便的形状。
    """

    spec: Spec
    spec_sha256: str = "a" * 64
    markdown_sha256: str = "b" * 64
    frozen_at: str = "2026-01-01T00:00:00+00:00"


class RecordingRun:
    """一个记录 argv 的 ``run`` 桩 —— ``subprocess.run`` 的替身。

    它记下每一次调用的 argv、cwd 与 ``shell``，因此「零调用」是可数的
    断言，而不是「没看见副作用」这种间接推断。
    """

    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: List[Dict[str, Any]] = []

    def __call__(
        self,
        argv: Any,
        cwd: Any = None,
        shell: bool = False,
        capture_output: bool = False,
        text: bool = False,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess:
        """记录这次调用，返回一个固定的完成结果。"""
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "shell": shell,
                "capture_output": capture_output,
                "text": text,
            }
        )
        return subprocess.CompletedProcess(
            list(argv), self.returncode, self.stdout, self.stderr
        )

    @property
    def log(self) -> List[List[str]]:
        """每次调用记下的 argv。"""
        return [call["argv"] for call in self.calls]


def _item(
    item_id: str = "push",
    command: Any = None,
    *,
    external: bool = True,
    target: Optional[str] = "origin",
    action: str = "push the branch to origin",
) -> SpecItem:
    """一条要求书条目 —— 形状与 ``postprocess.spec.SpecItem`` 一致。"""
    return SpecItem(
        id=item_id,
        action=action,
        command=command,
        acceptance_predicate={"kind": "exit_code", "equals": 0},
        evidence_requirement={"kind": "exit_code"},
        external=external,
        target=target,
        required=True,
        wait={"type": "none"},
    )


def _frozen(items: List[SpecItem]) -> FakeFrozenSpec:
    """把条目包成一份冻结要求书。"""
    return FakeFrozenSpec(spec=Spec(items=tuple(items), markdown=""))


def _make_repo(path: Path, remote: Optional[Path] = None) -> Path:
    """一个有一次基线提交的临时 git 仓 —— 产品代码树。

    Args:
        path: 仓的目录。
        remote: 扮演 origin 的裸仓；给了就登记成 ``origin`` 这个 remote。

    Returns:
        仓的目录。
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", str(path)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "runner@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "runner"],
        check=True,
    )
    if remote is not None:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", str(remote)],
            check=True,
        )
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"], check=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "--quiet", "-m", "base"],
        check=True,
    )
    return path


def _make_bare(path: Path) -> Path:
    """一个本地裸仓 —— 扮演 origin。"""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", "--bare", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def _bare_refs(origin: Path) -> List[str]:
    """裸仓里的全部 ref 名。"""
    completed = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)"],
        cwd=str(origin),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.split()


# ---------------------------------------------------------------------
# 放行
# ---------------------------------------------------------------------


def test_declared_external_command_runs_and_is_audited(tmp_path):
    """声明过的对外命令真的执行了，裸仓里出现新分支，三元组完整。"""
    origin = _make_bare(tmp_path / "origin.git")
    repo = _make_repo(tmp_path / "repo", remote=origin)
    frozen = _frozen([_item("push", PUSH_COMMAND)])
    runner = CommandRunner(frozen, workspace=repo)

    evidence, audit = runner.run_item(_item("push", PUSH_COMMAND))

    assert "refs/heads/x" in _bare_refs(origin), (
        "声明过的 push 必须真的把分支推进裸仓"
    )
    assert audit.action == "push"
    assert audit.command == PUSH_COMMAND
    assert audit.target == "origin"
    assert isinstance(audit, AuditRecord)
    assert isinstance(evidence, EvidenceRecord)
    assert evidence.item_id == "push"
    assert evidence.exit_code == 0


def test_argv_recorded_verbatim(tmp_path):
    """审计里的 argv 与桩记录的 argv 逐字相等，且是数组不是字符串。"""
    frozen = _frozen([_item("push", PUSH_COMMAND)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    _, audit = runner.run_item(_item("push", PUSH_COMMAND))

    assert stub.log == [PUSH_COMMAND], "桩必须只被调用一次，且 argv 逐字"
    assert audit.command == stub.log[0], "审计的 argv 必须与桩记录的逐字相等"
    assert audit.command == PUSH_COMMAND
    assert isinstance(audit.command, list)
    assert all(isinstance(part, str) for part in audit.command)


# ---------------------------------------------------------------------
# 拒绝
# ---------------------------------------------------------------------


def test_undeclared_command_rejected_before_spawn(tmp_path):
    """与冻结要求书声明不一致的 argv 在 spawn 之前被拒，桩零调用。"""
    frozen = _frozen([_item("push", PUSH_COMMAND)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    undeclared = _item("push", ["gh", "pr", "create", "--title", "x"])
    with pytest.raises(CommandRejected):
        runner.run_item(undeclared)

    assert stub.log == [], "被拒的命令一次都不能 spawn"


def test_argv_argument_difference_rejected(tmp_path):
    """参数差异（哪怕只差一个值）也算不逐字相等。"""
    frozen = _frozen([_item("push", PUSH_COMMAND)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    different = _item("push", ["git", "push", "origin", "HEAD:refs/heads/y"])
    with pytest.raises(CommandRejected):
        runner.run_item(different)

    assert stub.log == []


def test_argv_order_difference_rejected(tmp_path):
    """顺序差异同样不算逐字相等。"""
    frozen = _frozen([_item("push", PUSH_COMMAND)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    reordered = _item("push", ["git", "push", "HEAD:refs/heads/x", "origin"])
    with pytest.raises(CommandRejected):
        runner.run_item(reordered)

    assert stub.log == []


def test_undeclared_item_rejected_before_spawn(tmp_path):
    """冻结要求书里根本没有的条目 id 同样被拒。"""
    frozen = _frozen([_item("push", PUSH_COMMAND)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    with pytest.raises(CommandRejected):
        runner.run_item(_item("other", PUSH_COMMAND))

    assert stub.log == []


def test_non_git_gh_binary_rejected(tmp_path):
    """argv[0] 不在 {git, gh} 内的命令被拒 —— 即使它已写进要求书。"""
    command = ["curl", "https://example.com/hook"]
    frozen = _frozen([_item("curl", command)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    with pytest.raises(CommandRejected):
        runner.run_item(_item("curl", command))

    assert stub.log == []


def test_command_must_be_list_not_string(tmp_path):
    """command 写成字符串必须被拒 —— 拆分它的人只能靠猜。"""
    frozen = _frozen([_item("push", "git push origin main")])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    with pytest.raises(CommandRejected):
        runner.run_item(_item("push", "git push origin main"))

    assert stub.log == []


def test_external_false_with_command_rejected(tmp_path):
    """external=false 的条目不得携带 command。"""
    frozen = _frozen([_item("push", PUSH_COMMAND, external=False)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    with pytest.raises(CommandRejected):
        runner.run_item(_item("push", PUSH_COMMAND, external=False))

    assert stub.log == []


# ---------------------------------------------------------------------
# 待批准
# ---------------------------------------------------------------------


def test_merge_to_main_requires_approval(tmp_path):
    """合入 main 转成「请求批准」，执行器一次都不 spawn。"""
    frozen = _frozen([_item("merge", MERGE_COMMAND)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    outcome = runner.run_item(_item("merge", MERGE_COMMAND))

    assert isinstance(outcome, NeedsApproval)
    assert outcome.command == MERGE_COMMAND
    assert outcome.question, "待批准必须带着一个问句"
    assert stub.log == [], "待批准的动作一次都不能 spawn"


def test_destructive_command_requires_approval(tmp_path):
    """破坏性操作（强制推送）转成「请求批准」。"""
    command = ["git", "push", "--force", "origin", "HEAD:refs/heads/x"]
    frozen = _frozen([_item("force", command)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    outcome = runner.run_item(_item("force", command))

    assert isinstance(outcome, NeedsApproval)
    assert outcome.command == command
    assert stub.log == []


def test_money_command_requires_approval(tmp_path):
    """花钱动作转成「请求批准」。"""
    command = ["gh", "api", "-X", "POST", "/marketplace/purchases"]
    frozen = _frozen([_item("buy", command)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    outcome = runner.run_item(_item("buy", command))

    assert isinstance(outcome, NeedsApproval)
    assert outcome.command == command
    assert stub.log == []


# ---------------------------------------------------------------------
# shell 边界
# ---------------------------------------------------------------------


def test_shell_metachars_not_expanded(tmp_path):
    """``;``、``$(...)``、反引号只是 argv 片段，不会被 shell 展开。"""
    repo = _make_repo(tmp_path / "repo")
    sentinel = tmp_path / "sentinel"
    payload = f"x; touch '{sentinel}'; $(echo pwned) `echo tick`"
    command = ["git", "config", "--local", "user.metachar", payload]
    frozen = _frozen([_item("meta", command)])
    runner = CommandRunner(frozen, workspace=repo)

    evidence, _ = runner.run_item(_item("meta", command))

    assert evidence.exit_code == 0
    assert not sentinel.exists(), "shell 元字符被展开了：哨兵文件被创建"
    stored = subprocess.run(
        ["git", "config", "--local", "user.metachar"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert stored == payload, "argv 必须原样传给进程"


# ---------------------------------------------------------------------
# 其余契约
# ---------------------------------------------------------------------


def test_item_without_command_runs_nothing(tmp_path):
    """没有声明 command 的条目不执行任何东西。"""
    frozen = _frozen([_item("local", None, external=False, target=None)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    evidence, audit = runner.run_item(
        _item("local", None, external=False, target=None)
    )

    assert stub.log == []
    assert evidence.exit_code is None
    assert evidence.command == []
    assert audit.command == []


def test_runner_reads_items_from_a_plain_spec(tmp_path):
    """白名单从冻结要求书读出来 —— 直接给 Spec 也一样。"""
    frozen = Spec(items=(_item("push", PUSH_COMMAND),), markdown="")
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    _, audit = runner.run_item(_item("push", PUSH_COMMAND))

    assert audit.command == PUSH_COMMAND


def test_execute_delegates_to_run_item(tmp_path):
    """``execute(item, context)`` 委托给 ``run_item``。"""
    frozen = _frozen([_item("push", PUSH_COMMAND)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    outcome = execute(_item("push", PUSH_COMMAND), {"runner": runner})

    assert isinstance(outcome, tuple)
    evidence, audit = outcome
    assert audit.command == PUSH_COMMAND
    assert evidence.item_id == "push"


def test_execute_without_a_runner_is_a_type_error(tmp_path):
    """context 里没有 runner 时是调用方的错误，不是静默的空跑。"""
    with pytest.raises(TypeError):
        execute(_item("push", PUSH_COMMAND), {"nothing": "here"})


def test_audit_target_falls_back_to_the_item_id(tmp_path):
    """条目没有声明 target 时，审计的目标指回条目 id。"""
    command = ["git", "status"]
    frozen = _frozen([_item("status", command, target=None)])
    stub = RecordingRun()
    runner = CommandRunner(frozen, workspace=tmp_path, run=stub)

    _, audit = runner.run_item(_item("status", command, target=None))

    assert audit.target == "status"
