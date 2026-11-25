"""收尾阶段的产品代码零改动校验 —— 越界即停。

What this file witnesses
------------------------
架构决策点 4 要求：每个条目前后对产品代码路径做 diff 校验，非空即该条
判 FAIL、停止收尾、通知人工。本文件用**真实 git** 与**临时仓**钉住这条
判据的可观察后果：

1. **越界即抛** —— 条目执行期间写下 ``backend/x.py``，守卫抛
   :class:`ProductCodeModified`，消息指回条目与被碰的文件；
2. **干净条目不留痕** —— 正常条目跑完后 ``git status --porcelain`` 为空；
3. **字节级不变** —— 跑前跑后产品树的文件哈希集合逐项相等；
4. **越界后不再往下跑** —— 第一个条目越界之后，后续条目一次都没被执行。

后面三条钉的是**生产路径**上的同一件事 —— 守卫不是一句 prompt，是编排器
真的把它套在每个条目上：

5. **编排器判该条 FAIL** —— 越界的那一条在报告里是 ``FAIL``，不是
   ``PASS``：判据「退出码为 0」并没有覆盖「产品代码被改了」；
6. **整轮停在 postprocess_failed** —— 越界之后后面的条目一次都没跑，
   失败通知只发一条，带上计划、失败条目与证据路径；
7. **证据里是 diff 原文** —— 失败那条的证据摘录带着 ``git status
   --porcelain`` 与 ``git diff --stat`` 的原文，人不必相信摘要；
8. **判定器能表达「diff 为空」** —— A5 的判据由判定器承载：diff 为空
   才算通过，非空即 FAIL。

另外两条边界也在这里钉住：``plans/{id}/`` 下的收尾产物**允许**写，
而落在允许范围之外的任何改动（含未跟踪文件）都算越界。

工具边界
--------
**真实 git**（临时仓 + 真实 porcelain + 真实 ``ls-files``）负责判定，
只有「条目执行器被调用了几次」这种断言用注入的 ``run`` 桩 —— 桩记录
argv，因此「零调用」是可数的，而不是靠推断。

What this file does NOT test
----------------------------
白名单与待批准分流是 ``backend/tests/unit/postprocess/
test_executor_command_runner.py`` 的事；本文件只关心产品代码零改动。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import pytest

from postprocess import progress as progress_module
from postprocess.executor import (
    CommandRunner,
    ProductCodeModified,
    declared_product_paths,
    product_diff_guard,
)
from postprocess.judge import judge
from postprocess.models import EvidenceRecord
from postprocess.orchestrator import PostProcessOrchestrator
from postprocess.spec import Spec, SpecItem, freeze, load_spec
from tests.fixtures.postprocess.builders import FakeNotifier


#: 本文件整体是集成测试 —— 它跑真实 git 与真实临时仓。
pytestmark = pytest.mark.integration


#: 一个条目携带的命令 —— argv 逐字进冻结要求书，所以放行。
STATUS_COMMAND: List[str] = ["git", "status"]


class RecordingRun:
    """一个记录 argv、并按剧本产生副作用的 ``run`` 桩。

    副作用写在 ``cwd`` 底下 —— 那正是 :class:`CommandRunner` 交给进程的
    工作目录，所以桩写出来的文件落在被校验的仓库里。
    """

    def __init__(
        self,
        effect: Optional[Callable[[Path], None]] = None,
        *,
        stdout: str = "",
    ) -> None:
        self.effect = effect
        self.stdout = stdout
        self.calls: List[List[str]] = []

    def __call__(
        self,
        argv: Any,
        cwd: Any = None,
        shell: bool = False,
        capture_output: bool = False,
        text: bool = False,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess:
        """记录这次调用，跑一遍副作用剧本，返回一个成功的完成结果。"""
        argv = list(argv)
        self.calls.append(argv)
        if self.effect is not None:
            self.effect(Path(cwd) if cwd is not None else Path.cwd())
        return subprocess.CompletedProcess(argv, 0, self.stdout, "")

    @property
    def log(self) -> List[List[str]]:
        """每次调用记下的 argv。"""
        return list(self.calls)


def _item(
    item_id: str,
    command: Optional[List[str]] = None,
    *,
    target: str = "origin",
) -> SpecItem:
    """一条要求书条目 —— 形状与 ``postprocess.spec.SpecItem`` 一致。"""
    return SpecItem(
        id=item_id,
        action=f"run {item_id}",
        command=command,
        acceptance_predicate={"kind": "exit_code", "equals": 0},
        evidence_requirement={"kind": "exit_code"},
        external=True,
        target=target,
        required=True,
        wait={"type": "none"},
    )


def _make_repo(path: Path) -> Path:
    """一个有一次基线提交的临时 git 仓 —— 产品代码树。

    基线提交里除了产品代码（``backend/``、``frontend/``），还有一个**已跟踪
    的非产品文件** ``README.md``。它让「改了已跟踪的、产品代码之外的文件照样
    越界」这一条有个能落脚的场景 —— 守卫对未跟踪的临时产物收口（未跟踪的新
    文件不算越界），所以这条判据只能拿已跟踪的文件来钉。

    基线里**没有** ``plans/`` 下的任何东西 —— 「往别的计划目录里新建文件」
    那个场景要的是一个空白的起点。

    Args:
        path: 仓的目录。

    Returns:
        仓的目录。
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", str(path)], check=True, capture_output=True
    )
    for key, value in (
        ("user.email", "runner@example.com"),
        ("user.name", "runner"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(
            ["git", "-C", str(path), "config", key, value], check=True
        )

    (path / "backend").mkdir()
    (path / "frontend").mkdir()
    (path / "backend" / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
    (path / "frontend" / "app.js").write_text(
        "export default 1;\n", encoding="utf-8"
    )
    (path / "README.md").write_text("baseline\n", encoding="utf-8")

    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--quiet", "-m", "baseline"],
        check=True,
        capture_output=True,
    )
    return path


def _git(repo: Path, *args: str) -> str:
    """在临时仓里跑一条 git 命令，返回它的标准输出。"""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _hashes(repo: Path) -> Dict[str, str]:
    """仓库里每个文件的 sha256 —— 路径 → 摘要。"""
    digests: Dict[str, str] = {}
    for path in sorted(repo.rglob("*")):
        relative = path.relative_to(repo)
        if ".git" in relative.parts or not path.is_file():
            continue
        digests[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _write_product_file(name: str) -> Callable[[Path], None]:
    """一个副作用剧本：在仓库的 ``backend/`` 下写一个文件。"""

    def effect(workspace: Path) -> None:
        (workspace / "backend" / name).write_text("X = 1\n", encoding="utf-8")

    return effect


def _write_plan_artifact(plan_id: str) -> Callable[[Path], None]:
    """一个副作用剧本：在 ``plans/{plan_id}/`` 下写一份收尾产物。"""

    def effect(workspace: Path) -> None:
        target = workspace / "plans" / plan_id / "postprocess_report.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")

    return effect


def _leave_untracked_file(relative: str, content: str = "{}\n") -> Callable[[Path], None]:
    """一个副作用剧本：在仓库里写一个**未跟踪**文件（父目录一并建出来）。"""

    def effect(workspace: Path) -> None:
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    return effect


def _declare_product_paths(repo: Path, content: str) -> Path:
    """在仓里写下项目自己声明的产品代码路径清单。

    位置就是框架已有的、被 gitignore 的项目级定制目录：``.pdt/``。

    Args:
        repo: 仓的目录。
        content: 清单的原文。

    Returns:
        写下的清单文件路径。
    """
    target = repo / ".pdt" / "product_paths"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# ---------------------------------------------------------------------
# 四条判据
# ---------------------------------------------------------------------


def test_modification_outside_scope_fails_item(tmp_path):
    """条目写进 ``backend/x.py`` —— 守卫抛 ProductCodeModified。"""
    repo = _make_repo(tmp_path / "repo")
    item = _item("refactor", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""),
        workspace=repo,
        run=RecordingRun(_write_product_file("x.py")),
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            runner.run_item(item)

    assert excinfo.value.item_id == "refactor"
    assert "backend/x.py" in excinfo.value.paths
    assert str(excinfo.value) == "item 'refactor' touched backend/x.py"


def test_clean_run_leaves_porcelain_empty(tmp_path):
    """正常条目跑完之后，工作区干净 —— porcelain 是空的。"""
    repo = _make_repo(tmp_path / "repo")
    item = _item("report", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""), workspace=repo, run=RecordingRun()
    )

    with product_diff_guard(item, repo=repo):
        runner.run_item(item)

    assert _git(repo, "status", "--porcelain") == ""


def test_file_hashes_unchanged_after_run(tmp_path):
    """跑前跑后，产品树的每一个文件都还是同一个字节。"""
    repo = _make_repo(tmp_path / "repo")
    before = _hashes(repo)
    item = _item("report", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""), workspace=repo, run=RecordingRun()
    )

    with product_diff_guard(item, repo=repo):
        runner.run_item(item)

    assert _hashes(repo) == before


def test_guard_stops_remaining_items(tmp_path):
    """第一个条目越界之后，后续条目一次都没被执行。"""
    repo = _make_repo(tmp_path / "repo")
    first = _item("refactor", STATUS_COMMAND)
    second = _item("publish", ["git", "log"])
    stub = RecordingRun(_write_product_file("x.py"))
    runner = CommandRunner(
        Spec(items=(first, second), markdown=""), workspace=repo, run=stub
    )

    executed: List[str] = []
    for item in (first, second):
        try:
            with product_diff_guard(item, repo=repo):
                executed.append(item.id)
                runner.run_item(item)
        except ProductCodeModified:
            break

    assert executed == ["refactor"]
    assert stub.log == [STATUS_COMMAND]


# ---------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------


def test_finishing_artifacts_under_the_plan_dir_are_allowed(tmp_path):
    """``plans/{id}/`` 下的收尾产物允许写 —— 不算越界。"""
    repo = _make_repo(tmp_path / "repo")
    item = _item("report", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""),
        workspace=repo,
        run=RecordingRun(_write_plan_artifact("2026-01-01")),
    )

    with product_diff_guard(item, repo=repo, plan_id="2026-01-01"):
        runner.run_item(item)

    assert (repo / "plans" / "2026-01-01" / "postprocess_report.json").is_file()


def test_new_file_under_another_plan_dir_is_out_of_scope(tmp_path):
    """条目在**别的**计划目录下**新建**文件 —— 越界，即使那个文件未被跟踪。

    收尾阶段只允许写 ``plans/{本 plan}/``。别的计划目录不许被写 —— 新建
    也不行，否则一个条目就能把另一个计划的收尾产物目录搅乱。
    """
    repo = _make_repo(tmp_path / "repo")
    assert not (repo / "plans" / "2026-02-02").exists(), (
        "基线里不该有这个计划目录 —— 这个用例测的是「新建」"
    )
    item = _item("report", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""),
        workspace=repo,
        run=RecordingRun(_write_plan_artifact("2026-02-02")),
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo, plan_id="2026-01-01"):
            runner.run_item(item)

    assert "plans/2026-02-02/postprocess_report.json" in excinfo.value.paths


def test_artifact_outside_the_named_plan_dir_is_out_of_scope(tmp_path):
    """允许范围只到那一个计划目录 —— 别的计划目录照样越界。"""
    repo = _make_repo(tmp_path / "repo")
    item = _item("report", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""),
        workspace=repo,
        run=RecordingRun(_write_plan_artifact("2026-02-02")),
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo, plan_id="2026-01-01"):
            runner.run_item(item)

    assert "plans/2026-02-02/postprocess_report.json" in excinfo.value.paths


def test_modified_tracked_product_file_is_out_of_scope(tmp_path):
    """改了已跟踪的产品文件（``backend/agent.py``）同样越界。"""
    repo = _make_repo(tmp_path / "repo")
    item = _item("refactor", STATUS_COMMAND)

    def rewrite_agent(workspace: Path) -> None:
        (workspace / "backend" / "agent.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )

    runner = CommandRunner(
        Spec(items=(item,), markdown=""), workspace=repo, run=RecordingRun(rewrite_agent)
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            runner.run_item(item)

    assert str(excinfo.value) == "item 'refactor' touched backend/agent.py"


def test_change_outside_product_code_is_still_out_of_scope(tmp_path):
    """**已跟踪的**、产品代码之外的文件被改也算越界。

    守卫对「仓库被改了什么」负责：已跟踪的文件不许被条目改动，不管它在
    产品代码路径之内还是之外。（未跟踪的新文件另说 —— 那是产物。）
    """
    repo = _make_repo(tmp_path / "repo")
    item = _item("refactor", STATUS_COMMAND)

    def write_docs(workspace: Path) -> None:
        (workspace / "README.md").write_text("changed\n", encoding="utf-8")

    runner = CommandRunner(
        Spec(items=(item,), markdown=""), workspace=repo, run=RecordingRun(write_docs)
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            runner.run_item(item)

    assert "README.md" in excinfo.value.paths


def test_assume_unchanged_product_file_is_still_out_of_scope(tmp_path):
    """被 ``assume-unchanged`` 藏起来的改动，porcelain 不报 —— 哈希比对抓得到。

    ``git update-index --assume-unchanged`` 让 git 对某个文件闭嘴：改了它，
    ``git status --porcelain`` 仍然是空的。所以光靠 porcelain 是不够的 ——
    产品代码文件的哈希比对补上这一条。
    """
    repo = _make_repo(tmp_path / "repo")
    _git(repo, "update-index", "--assume-unchanged", "backend/agent.py")
    item = _item("refactor", STATUS_COMMAND)

    def rewrite_agent(workspace: Path) -> None:
        (workspace / "backend" / "agent.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )

    runner = CommandRunner(
        Spec(items=(item,), markdown=""), workspace=repo, run=RecordingRun(rewrite_agent)
    )

    assert _git(repo, "status", "--porcelain") == ""

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            runner.run_item(item)

    assert "backend/agent.py" in excinfo.value.paths


# ---------------------------------------------------------------------
# 差分语义：跑之前就存在的改动，不算本次条目干的
# ---------------------------------------------------------------------


def test_pre_existing_untracked_file_is_not_a_violation(tmp_path):
    """跑之前就存在的未跟踪文件 —— 不是这次条目干的，不判越界。

    这正是实测踩到的坑：收尾阶段的计划自己在工作区里留了一份证据文件
    （``reports/refactor/perf_p99.json``），那是**进收尾阶段之前**就在
    的状态。条目对它记账，每一条都会 FAIL，重跑多少次都一样。
    """
    repo = _make_repo(tmp_path / "repo")
    pre_existing = repo / "reports" / "refactor" / "perf_p99.json"
    pre_existing.parent.mkdir(parents=True, exist_ok=True)
    pre_existing.write_text("{}\n", encoding="utf-8")

    item = _item("report", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""), workspace=repo, run=RecordingRun()
    )

    with product_diff_guard(item, repo=repo):
        runner.run_item(item)

    assert pre_existing.is_file(), "跑之前就存在的未跟踪文件被守卫弄没了"


def test_new_untracked_file_is_still_a_violation(tmp_path):
    """条目在**产品代码路径下**新写的未跟踪文件照样越界 —— 且只报它一个。

    它是未跟踪的，所以 porcelain 那条判据（只看已跟踪路径）不抓它 —— 但它
    在产品代码路径下，哈希判据抓得到（``ls-files -co`` 把未跟踪的也列出来）。
    跑之前已有的那个未跟踪文件不该出现在 ``paths`` 里。
    """
    repo = _make_repo(tmp_path / "repo")
    pre_existing = repo / "reports" / "refactor" / "perf_p99.json"
    pre_existing.parent.mkdir(parents=True, exist_ok=True)
    pre_existing.write_text("{}\n", encoding="utf-8")

    item = _item("refactor", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""),
        workspace=repo,
        run=RecordingRun(_leave_untracked_file("backend/latency.py")),
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            runner.run_item(item)

    assert excinfo.value.paths == ("backend/latency.py",), (
        "越界路径不止新写的那个：%r" % (excinfo.value.paths,)
    )


def test_new_untracked_file_outside_product_paths_is_not_a_violation(tmp_path):
    """条目新写的**非产品路径**未跟踪文件不算越界 —— 那是产物，不是产品代码。

    守卫的职责是「产品代码零改动」，不是「仓库里不许出现任何新文件」。
    """
    repo = _make_repo(tmp_path / "repo")
    item = _item("report", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""),
        workspace=repo,
        run=RecordingRun(_leave_untracked_file("reports/refactor/latency.json")),
    )

    with product_diff_guard(item, repo=repo):
        runner.run_item(item)

    assert (repo / "reports" / "refactor" / "latency.json").is_file(), (
        "条目写出来的产物不见了"
    )


def test_modified_tracked_file_outside_product_paths_is_a_violation(tmp_path):
    """改了**已跟踪的**、产品代码之外的文件 —— 仍然越界。

    守卫对「仓库被改了什么」负责：已跟踪的文件不许被条目改动，不管它在
    产品代码路径之内还是之外。
    """
    repo = _make_repo(tmp_path / "repo")
    assert "README.md" in _git(repo, "ls-files").split(), (
        "夹具里得有一个已跟踪的非产品文件"
    )
    item = _item("refactor", STATUS_COMMAND)

    def rewrite_readme(workspace: Path) -> None:
        (workspace / "README.md").write_text("changed\n", encoding="utf-8")

    runner = CommandRunner(
        Spec(items=(item,), markdown=""), workspace=repo, run=RecordingRun(rewrite_readme)
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            runner.run_item(item)

    assert "README.md" in excinfo.value.paths


def test_new_untracked_file_under_src_is_a_violation(tmp_path):
    """条目在 ``src/`` 下**新写**一个未跟踪的源码文件 —— 越界。

    收尾阶段的 workspace 常常不是框架仓：产品代码可能长在 ``src/``、
    ``api/`` 之类的目录下。哈希那条判据按产品代码路径逐文件比对，所以这些
    目录下的新源码文件也抓得到。
    """
    repo = _make_repo(tmp_path / "repo")
    item = _item("refactor", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""),
        workspace=repo,
        run=RecordingRun(_leave_untracked_file("src/new_module.py")),
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            runner.run_item(item)

    assert excinfo.value.paths == ("src/new_module.py",), (
        "src/ 下的新源码文件没被抓到：%r" % (excinfo.value.paths,)
    )


def test_new_untracked_file_under_a_declared_source_dir_is_a_violation(tmp_path):
    """条目在**项目声明的**源码目录下新写文件 —— 越界。

    守卫不能靠一张写死的目录名表来认产品代码：被测仓的源码目录叫什么名字
    本仓不知道（也不该知道）。清单由项目自己在 ``.pdt/product_paths`` 里
    声明 —— 这条用一个不在缺省名单里的目录名来钉。
    """
    repo = _make_repo(tmp_path / "repo")
    _declare_product_paths(repo, "core_logic\n")
    source = repo / "core_logic"
    source.mkdir()
    (source / "engine.py").write_text("VALUE = 1\n", encoding="utf-8")

    item = _item("refactor", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""),
        workspace=repo,
        run=RecordingRun(_leave_untracked_file("core_logic/extra.py")),
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            runner.run_item(item)

    assert excinfo.value.paths == ("core_logic/extra.py",), (
        "已跟踪的源码目录下的新文件没被抓到：%r" % (excinfo.value.paths,)
    )


def test_declared_product_paths_file_wins(tmp_path):
    """项目声明了 ``.pdt/product_paths`` —— 声明**覆盖**缺省值，不是并集。

    声明里有的 ``src``：条目在 ``src/`` 下新写文件 → 越界。声明里没有的
    ``backend``（它在缺省名单里）：条目在 ``backend/`` 下新写文件 → 不越界。
    """
    repo = _make_repo(tmp_path / "repo")
    _declare_product_paths(repo, "# 注释\nsrc\napi\n")
    item = _item("refactor", STATUS_COMMAND)

    def write_both(workspace: Path) -> None:
        for relative in ("src/x.py", "backend/extra.py"):
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("VALUE = 1\n", encoding="utf-8")

    runner = CommandRunner(
        Spec(items=(item,), markdown=""), workspace=repo, run=RecordingRun(write_both)
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            runner.run_item(item)

    assert excinfo.value.paths == ("src/x.py",), (
        "声明覆盖缺省值这件事没成立：%r" % (excinfo.value.paths,)
    )


def test_declared_product_paths_ignores_blank_and_comment_lines(tmp_path):
    """清单里的注释、空行、行尾空白都被正确跳过。"""
    repo = _make_repo(tmp_path / "repo")
    _declare_product_paths(
        repo, "# 这是一行注释\n\n   src   \n\t\napi\n\n# 又一行注释\n"
    )
    assert declared_product_paths(repo) == ("src", "api")

    item = _item("refactor", STATUS_COMMAND)

    def write_both(workspace: Path) -> None:
        for relative in ("src/y.py", "backend/extra.py"):
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("VALUE = 1\n", encoding="utf-8")

    runner = CommandRunner(
        Spec(items=(item,), markdown=""), workspace=repo, run=RecordingRun(write_both)
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            runner.run_item(item)

    assert excinfo.value.paths == ("src/y.py",), (
        "带注释和空行的清单没解析对：%r" % (excinfo.value.paths,)
    )


def test_declared_product_paths_rejects_absolute_and_parent_paths(tmp_path):
    """清单里的绝对路径与 ``..`` 被跳过 —— 剩下的行照常生效，且不抛异常。"""
    repo = _make_repo(tmp_path / "repo")
    _declare_product_paths(repo, "/etc/passwd\n../outside\nsrc\n")
    assert declared_product_paths(repo) == ("src",)

    item = _item("refactor", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""),
        workspace=repo,
        run=RecordingRun(_leave_untracked_file("src/z.py")),
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            runner.run_item(item)

    assert excinfo.value.paths == ("src/z.py",), (
        "写坏的行没有让整个清单失效：%r" % (excinfo.value.paths,)
    )


def test_missing_declaration_falls_back_to_defaults_and_warns(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """项目没有声明清单 → 按缺省值判定，并打一条 warning。"""
    repo = _make_repo(tmp_path / "repo")
    assert not (repo / ".pdt" / "product_paths").exists()
    item = _item("refactor", STATUS_COMMAND)
    runner = CommandRunner(
        Spec(items=(item,), markdown=""),
        workspace=repo,
        run=RecordingRun(_leave_untracked_file("backend/x.py")),
    )

    with caplog.at_level(logging.WARNING, logger="postprocess.executor"):
        with pytest.raises(ProductCodeModified) as excinfo:
            with product_diff_guard(item, repo=repo):
                runner.run_item(item)

    assert excinfo.value.paths == ("backend/x.py",), (
        "缺省产品代码路径没生效：%r" % (excinfo.value.paths,)
    )
    assert any("pdt/product_paths" in record.getMessage() for record in caplog.records), (
        "没有为「项目没声明清单」打 warning：%r" % ([r.getMessage() for r in caplog.records],)
    )


def test_pre_existing_untracked_file_does_not_hide_a_rewrite(tmp_path):
    """跑之前就存在的未跟踪文件，不得把哈希那条判据一起放掉。

    改了已跟踪的产品文件（``backend/agent.py``）仍然必须抛 —— 差分只
    作用于 porcelain 那一条判据，哈希比对仍然是跑前跑后的差分。
    """
    repo = _make_repo(tmp_path / "repo")
    pre_existing = repo / "reports" / "refactor" / "perf_p99.json"
    pre_existing.parent.mkdir(parents=True, exist_ok=True)
    pre_existing.write_text("{}\n", encoding="utf-8")

    item = _item("refactor", STATUS_COMMAND)

    def rewrite_agent(workspace: Path) -> None:
        (workspace / "backend" / "agent.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )

    runner = CommandRunner(
        Spec(items=(item,), markdown=""), workspace=repo, run=RecordingRun(rewrite_agent)
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            runner.run_item(item)

    assert "backend/agent.py" in excinfo.value.paths
    assert "reports/refactor/perf_p99.json" not in excinfo.value.paths


# ---------------------------------------------------------------------
# 生产路径：编排器把守卫套在每一个条目上
# ---------------------------------------------------------------------


#: 本节每个用例跑的计划 id。
PLAN_ID = "p1"


@pytest.fixture
def plans_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个隔离的 plans 根 —— 产物落在这里，不落进仓库。

    两个逃逸口都要设：``postprocess.progress`` 认模块属性，
    ``postprocess.notify`` 读环境变量。
    """
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))
    monkeypatch.setattr(progress_module, "PLANS_DIR", root)
    return root


@pytest.fixture
def plan_dir(plans_root: Path) -> Path:
    """计划自己的目录 —— 要求书住在那里。"""
    directory = plans_root / PLAN_ID
    directory.mkdir()
    return directory


def book_item(
    item_id: str,
    *,
    command: Optional[List[str]] = None,
    predicate: Optional[Mapping[str, Any]] = None,
    target: str = "origin",
) -> Dict[str, Any]:
    """要求书里的一条 —— 架构声明的七个键，外加对外条目的 argv。

    Args:
        item_id: 条目的 id。
        command: 条目声明的 argv。
        predicate: 条目的判据；默认是「退出码为 0」。
        target: 条目对外作用的标的。

    Returns:
        那一条，作为要求书写下的形状。
    """
    return {
        "id": item_id,
        "action": f"finish {item_id}",
        "command": list(command or STATUS_COMMAND),
        "acceptance_predicate": dict(
            predicate or {"kind": "exit_code", "equals": 0}
        ),
        "evidence_requirement": {"kind": "exit_code"},
        "external": True,
        "target": target,
        "required": True,
        "wait": {"type": "none"},
    }


def write_book(plan_dir: Path, items: Sequence[Mapping[str, Any]]) -> Path:
    """写一个计划的要求书。

    Args:
        plan_dir: 计划自己的目录。
        items: 条目，按要求书声明的顺序。

    Returns:
        写下的要求书的路径。
    """
    path = plan_dir / "postprocess.json"
    path.write_text(
        json.dumps({"items": [dict(item) for item in items]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _orchestrator(
    plan_dir: Path,
    repo: Path,
    stub: RecordingRun,
) -> PostProcessOrchestrator:
    """一个走生产装配的编排器：真的执行器、真的守卫、真的通知。

    Args:
        plan_dir: 计划自己的目录。
        repo: 条目跑进去的仓库。
        stub: 执行器底下那个记录 argv 的 ``run`` 桩。

    Returns:
        装配好的编排器。
    """
    executor = CommandRunner(
        freeze(load_spec(plan_dir)), workspace=repo, run=stub
    )
    return PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=executor,
        sink=FakeNotifier(),
    )


def test_the_orchestrator_fails_the_item_that_modified_the_product_code(
    tmp_path: Path,
    plan_dir: Path,
) -> None:
    """越界的那一条判 FAIL，后面的条目一次都没跑，整轮 postprocess_failed。"""
    repo = _make_repo(tmp_path / "repo")
    write_book(plan_dir, [book_item("mutate"), book_item("after")])
    stub = RecordingRun(_write_product_file("x.py"), stdout="done\n")
    sink = FakeNotifier()
    executor = CommandRunner(
        freeze(load_spec(plan_dir)), workspace=repo, run=stub
    )

    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=executor,
        sink=sink,
    )
    report = orchestrator.start()

    verdicts = {entry["id"]: entry["verdict"] for entry in report["items"]}
    assert report["terminal_status"] == "postprocess_failed", (
        "产品代码被改了，整轮却没有判失败：%r" % (report["terminal_status"],)
    )
    assert verdicts["mutate"] == "FAIL", (
        "越界的那一条被判成了 %r" % (verdicts["mutate"],)
    )
    assert verdicts["after"] != "PASS", "越界之后的条目仍然判了 PASS"
    assert stub.log == [STATUS_COMMAND], "越界之后的条目仍然跑了"
    assert sink.count == 1, "失败通知不是一条，是 %d 条" % (sink.count,)
    payload = sink.calls[0]["payload"]
    assert payload["plan_id"] == PLAN_ID
    assert payload["failed_items"] == ["mutate"]


def test_the_failing_item_carries_the_raw_diff_as_its_evidence(
    tmp_path: Path,
    plan_dir: Path,
) -> None:
    """失败那条的证据摘录里是 ``git status --porcelain`` 与 ``git diff --stat`` 的原文。"""
    repo = _make_repo(tmp_path / "repo")
    write_book(plan_dir, [book_item("mutate")])
    stub = RecordingRun(_write_product_file("x.py"), stdout="done\n")
    orchestrator = _orchestrator(plan_dir, repo, stub)

    report = orchestrator.start()

    entry = next(e for e in report["items"] if e["id"] == "mutate")
    excerpt = entry["evidence_excerpt"]
    assert "git status --porcelain" in excerpt, excerpt
    assert "git diff --stat" in excerpt, excerpt
    assert "backend/x.py" in excerpt, excerpt


def test_the_judge_can_say_the_product_code_diff_is_empty() -> None:
    """判定器能表达「diff 为空」—— A5 的判据由它承载。"""
    item = book_item("untouched", predicate={"kind": "git_diff_empty"})
    untouched = EvidenceRecord(
        item_id="untouched", exit_code=0, stdout="ok", git_diff=""
    )
    touched = EvidenceRecord(
        item_id="untouched",
        exit_code=0,
        stdout="ok",
        git_diff="git status --porcelain:\n?? backend/x.py\n",
    )

    assert judge(item, [untouched]).passed is True
    assert judge(item, [touched]).passed is False


def test_the_orchestrator_passes_with_a_pre_existing_untracked_file(
    tmp_path: Path,
    plan_dir: Path,
) -> None:
    """生产入口上：跑之前就存在的未跟踪文件，不该让整轮判失败。

    这是编排器真的把守卫套在每个条目上的那条路径 —— 差分语义修好之后，
    一份进收尾阶段之前就在工作区里的证据文件，不再让每一条都 FAIL。
    """
    repo = _make_repo(tmp_path / "repo")
    pre_existing = repo / "reports" / "refactor" / "perf_p99.json"
    pre_existing.parent.mkdir(parents=True, exist_ok=True)
    pre_existing.write_text("{}\n", encoding="utf-8")
    write_book(plan_dir, [book_item("report")])
    stub = RecordingRun(stdout="done\n")
    orchestrator = _orchestrator(plan_dir, repo, stub)

    report = orchestrator.start()

    entry = next(e for e in report["items"] if e["id"] == "report")
    assert report["terminal_status"] == "completed", report["terminal_status"]
    assert entry["verdict"] == "PASS", entry
    assert pre_existing.is_file(), "跑之前就存在的未跟踪文件被弄没了"


# ---------------------------------------------------------------------
# 窄豁免：声明的上游合并（``git merge origin/main``）可以改产品代码
# ---------------------------------------------------------------------


#: 收尾要求书里声明上游合并的那一条命令 —— 逐字匹配。
UPSTREAM_MERGE: List[str] = ["git", "merge", "origin/main"]


def _current_branch(repo: Path) -> str:
    """当前分支名。"""
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def _commit_file(repo: Path, relative: str, content: str, message: str) -> str:
    """在当前分支上提交一个文件，返回这次提交的 sha。

    Args:
        repo: 仓的目录。
        relative: 仓库相对路径。
        content: 文件内容。
        message: 提交消息。

    Returns:
        这次提交的 sha。
    """
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", message],
        check=True,
        capture_output=True,
    )
    return _git(repo, "rev-parse", "HEAD").strip()


def _make_upstream_ref(repo: Path) -> None:
    """在临时仓里造一个 ``origin/main`` —— 含一条改产品代码的提交。

    用远程跟踪 ref（``refs/remotes/origin/main``）而不是本地分支：``git
    merge origin/main`` 走的就是远程跟踪分支的解析路径。

    上游这一条只**新增**一个产品文件 —— 与基线没有冲突，合并是干净的。

    Args:
        repo: 仓的目录。
    """
    base = _current_branch(repo)
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--quiet", "-b", "upstream-fixture"],
        check=True,
        capture_output=True,
    )
    sha = _commit_file(repo, "backend/upstream.py", "UPSTREAM = 1\n", "upstream")
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", sha],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--quiet", base],
        check=True,
        capture_output=True,
    )


def _make_conflicting_upstream_ref(repo: Path) -> None:
    """在临时仓里造一个会**冲突**的 ``origin/main``。

    上游与本地各改一次 ``backend/agent.py`` —— 合起来必然冲突，工作区
    会留下 ``UU backend/agent.py``。

    Args:
        repo: 仓的目录。
    """
    base = _current_branch(repo)
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--quiet", "-b", "upstream-fixture"],
        check=True,
        capture_output=True,
    )
    _commit_file(repo, "backend/agent.py", "VALUE = 2\n", "upstream edits agent")
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--quiet", base],
        check=True,
        capture_output=True,
    )
    _commit_file(repo, "backend/agent.py", "VALUE = 3\n", "local edits agent")


def _make_feature_branch(repo: Path) -> None:
    """在临时仓里造一个别的分支 ``feature/x`` —— 它也改产品代码。

    Args:
        repo: 仓的目录。
    """
    base = _current_branch(repo)
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--quiet", "-b", "feature/x"],
        check=True,
        capture_output=True,
    )
    _commit_file(repo, "backend/agent.py", "VALUE = 9\n", "feature edits agent")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--quiet", base],
        check=True,
        capture_output=True,
    )


def _merge(repo: Path, ref: str) -> subprocess.CompletedProcess:
    """在仓里真的跑一次 ``git merge``。

    合并不带 ``--no-edit`` —— 命令逐字就是 ``git merge {ref}``；编辑器
    用环境变量按掉，免得在无终端的环境里卡住。

    Args:
        repo: 仓的目录。
        ref: 要合的 ref。

    Returns:
        完成的进程结果（合并冲突时返回码非零）。
    """
    environment = dict(os.environ)
    environment["GIT_MERGE_AUTOEDIT"] = "no"
    environment["GIT_EDITOR"] = "true"
    return subprocess.run(
        ["git", "-C", str(repo), "merge", ref],
        capture_output=True,
        text=True,
        env=environment,
    )


def test_an_upstream_merge_may_change_product_code(tmp_path):
    """声明的上游合并（``git merge origin/main``）改了产品代码 —— 不抛。

    收尾要求书的第一条就是合并上游；上游的改动必然落在产品代码路径下，
    所以这一条必须豁免，否则每次收尾都判越界、整轮停 ``postprocess_failed``。
    """
    repo = _make_repo(tmp_path / "repo")
    _make_upstream_ref(repo)
    item = _item("merge-upstream", UPSTREAM_MERGE)

    with product_diff_guard(item, repo=repo):
        merged = _merge(repo, "origin/main")

    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert (repo / "backend" / "upstream.py").is_file(), "合并没把上游内容带进来"


def test_a_conflicted_upstream_merge_is_still_a_violation(tmp_path):
    """冲突未完成的上游合并 —— 仍然判红。

    豁免只针对「一次干净完成的合并」：冲突留下的 ``UU`` 状态是 porcelain
    改动，第 1 条判据照样抓住它。
    """
    repo = _make_repo(tmp_path / "repo")
    _make_conflicting_upstream_ref(repo)
    item = _item("merge-upstream", UPSTREAM_MERGE)

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            merged = _merge(repo, "origin/main")

    assert merged.returncode != 0, "这一条的前提是合并真的失败了"
    assert "backend/agent.py" in excinfo.value.paths, excinfo.value.paths


def test_a_merge_of_another_ref_is_still_a_violation(tmp_path):
    """合并别的 ref（``git merge feature/x``）改了产品代码 —— 仍然判红。

    豁免是逐字匹配 ``git merge origin/main`` 的窄豁免，不是「凡是 merge
    都放行」。
    """
    repo = _make_repo(tmp_path / "repo")
    _make_feature_branch(repo)
    item = _item("merge-feature", ["git", "merge", "feature/x"])

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            merged = _merge(repo, "feature/x")

    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert "backend/agent.py" in excinfo.value.paths, excinfo.value.paths


def test_a_non_merge_item_writing_product_code_is_still_a_violation(tmp_path):
    """不是合并的条目写下产品代码 —— 仍然判红。

    与已有的 ``test_modification_outside_scope_fails_item`` 同一件事，
    区别只在这里的命令是 ``python -c ...``：豁免的判据是「是不是那一条
    上游合并」，别的命令一律不豁免。
    """
    repo = _make_repo(tmp_path / "repo")
    script = "open('backend/x.py', 'w').write('X = 1\\n')"
    item = _item("rewrite", ["python", "-c", script])

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(repo),
                check=True,
                capture_output=True,
            )

    assert "backend/x.py" in excinfo.value.paths, excinfo.value.paths


def test_an_upstream_merge_followed_by_a_hand_edit_is_still_a_violation(tmp_path):
    """上游合并之后又手工改了产品文件 —— 仍然判红。

    合并本身干净完成（porcelain 为空），但合并之后的手工改动留下了
    porcelain 改动 —— 第 1 条判据抓住它，豁免放不住它。
    """
    repo = _make_repo(tmp_path / "repo")
    _make_upstream_ref(repo)
    item = _item("merge-upstream", UPSTREAM_MERGE)

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            merged = _merge(repo, "origin/main")
            assert merged.returncode == 0, merged.stdout + merged.stderr
            (repo / "backend" / "upstream.py").write_text(
                "HAND EDIT = 1\n", encoding="utf-8"
            )

    assert "backend/upstream.py" in excinfo.value.paths, excinfo.value.paths


def test_the_orchestrator_passes_an_upstream_merge_item(
    tmp_path: Path,
    plan_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产入口上：收尾要求书里那一条上游合并判 PASS。

    真的 git、真的临时仓、真的编排器、真的执行器（不注入桩）—— 豁免
    是在生产路径上生效的，不只是守卫单测里成立。
    """
    repo = _make_repo(tmp_path / "repo")
    _make_upstream_ref(repo)
    monkeypatch.setenv("GIT_MERGE_AUTOEDIT", "no")
    monkeypatch.setenv("GIT_EDITOR", "true")
    write_book(plan_dir, [book_item("merge-upstream", command=UPSTREAM_MERGE)])
    executor = CommandRunner(freeze(load_spec(plan_dir)), workspace=repo)
    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=executor,
        sink=FakeNotifier(),
    )

    report = orchestrator.start()

    entry = next(e for e in report["items"] if e["id"] == "merge-upstream")
    assert report["terminal_status"] == "completed", report["terminal_status"]
    assert entry["verdict"] == "PASS", entry
    assert (repo / "backend" / "upstream.py").is_file(), "合并没把上游内容带进来"


# ---------------------------------------------------------------------
# 声明的 squash 条目
# ---------------------------------------------------------------------

#: 收尾要求书里声明 squash 的那一条命令 —— 逐字匹配。
DECLARED_SQUASH: List[str] = ["git", "reset", "--soft", "origin/main"]


def _make_squash_scenario(repo: Path) -> str:
    """把仓摆成「分支上有一条改产品代码的提交」的样子。

    收尾阶段压扁（squash）的前提就是这个形状：分支上有提交，于是
    ``git reset --soft origin/main`` 会把那条提交变成**暂存区里的改动** ——
    porcelain 报出来的那批条目，正是这条压扁条目要做的事。

    ``origin/main`` 指向基线提交（产品代码还是基线那一份），所以压扁之后
    分支上的产品代码改动全部落在暂存区里。

    Args:
        repo: 仓的目录。

    Returns:
        基线提交的 sha。
    """
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit_file(repo, "backend/agent.py", "VALUE = 2\n", "feature change")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "update-ref",
            "refs/remotes/origin/main",
            base,
        ],
        check=True,
        capture_output=True,
    )
    return base


def _reset_soft(repo: Path, ref: str) -> subprocess.CompletedProcess:
    """在仓里真的跑一次 ``git reset --soft {ref}``。

    Args:
        repo: 仓的目录。
        ref: 要退回去的 ref。

    Returns:
        完成的进程结果。
    """
    return subprocess.run(
        ["git", "reset", "--soft", ref],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _porcelain(repo: Path) -> str:
    """仓里当前的 ``git status --porcelain`` 原文。"""
    return _git(repo, "status", "--porcelain", "--untracked-files=all")


def test_a_declared_squash_item_may_stage_the_product_diff(tmp_path):
    """声明的 squash 条目（``git reset --soft origin/main``）—— 不抛。

    这一条按定义就是把分支上的 diff 放进暂存区：porcelain 报出那批
    ``M `` 条目正是它要做的事，产物由紧随其后的 commit 条目提交。守卫
    若在这里判红，收尾阶段就永远走不到提交那一步。

    用例里断言 porcelain **确实**变了 —— 否则这条用例可能是空的（没造出
    暂存状态，守卫本来就不会抛）。
    """
    repo = _make_repo(tmp_path / "repo")
    _make_squash_scenario(repo)
    item = _item("squash-to-main-base", DECLARED_SQUASH, target="origin/main")

    with product_diff_guard(item, repo=repo):
        _reset_soft(repo, "origin/main")

    porcelain = _porcelain(repo)
    assert "backend/agent.py" in porcelain, porcelain
    assert porcelain.startswith("M "), porcelain


def test_a_squash_item_with_another_target_is_still_a_violation(tmp_path):
    """argv 与 target 都不是 ``origin/main`` 的压扁条目 —— 仍然判红。

    豁免是逐字匹配那一条声明的 squash 命令的窄豁免，不是「凡是
    ``git reset --soft`` 都放行」。
    """
    repo = _make_repo(tmp_path / "repo")
    _make_squash_scenario(repo)
    item = _item(
        "squash-to-other",
        ["git", "reset", "--soft", "otherbranch"],
        target="otherbranch",
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            _reset_soft(repo, "origin/main")

    assert "backend/agent.py" in excinfo.value.paths, excinfo.value.paths


@pytest.mark.parametrize("base", ["otherbranch", "origin/release"])
def test_a_squash_item_to_another_base_is_still_a_violation(tmp_path, base):
    """压扁到别的基线（argv 与 target 一致，但不是 ``origin/main``）—— 仍然判红。

    认的基线只有 ``origin/main`` 一个 —— 与「声明的上游合并」同形：那条
    也只认 ``origin/main``，合到别处的 merge 照样判红。
    """
    repo = _make_repo(tmp_path / "repo")
    _make_squash_scenario(repo)
    item = _item(
        "squash-to-other-base",
        ["git", "reset", "--soft", base],
        target=base,
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            _reset_soft(repo, "origin/main")

    assert "backend/agent.py" in excinfo.value.paths, excinfo.value.paths


def test_a_squash_item_whose_target_disagrees_with_its_argv_is_still_a_violation(
    tmp_path,
):
    """argv 的第四段与条目自己的 ``target`` 不一致 —— 仍然判红。

    识别是「argv 逐字等于 ``git reset --soft {target}``」：只有 argv 与
    target 互相对得上才算那一条声明的 squash。
    """
    repo = _make_repo(tmp_path / "repo")
    _make_squash_scenario(repo)
    item = _item(
        "squash-mismatched-target",
        DECLARED_SQUASH,
        target="otherbranch",
    )

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            _reset_soft(repo, "origin/main")

    assert "backend/agent.py" in excinfo.value.paths, excinfo.value.paths


def test_a_declared_squash_item_that_rewrites_product_code_is_still_a_violation(
    tmp_path,
):
    """声明的 squash 条目**真的改写了产品文件内容** —— 仍然判红。

    ``git reset --soft`` 不碰文件内容，所以内容判据（哈希比对）照旧有效：
    豁免只放得住 porcelain 那一判据，放不住内容改动。
    """
    repo = _make_repo(tmp_path / "repo")
    _make_squash_scenario(repo)
    item = _item("squash-to-main-base", DECLARED_SQUASH, target="origin/main")

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            _reset_soft(repo, "origin/main")
            (repo / "backend" / "agent.py").write_text(
                "VALUE = 99\n", encoding="utf-8"
            )

    assert "backend/agent.py" in excinfo.value.paths, excinfo.value.paths


def test_a_declared_squash_item_hiding_a_rewrite_is_still_a_violation(tmp_path):
    """porcelain 报不出来的那次改写（``assume-unchanged``）—— 仍然判红。

    这一条把「豁免只放得住 porcelain 判据」钉死：这里 porcelain 是空的，
    唯一能抓住改动的就是产品代码哈希判据 —— 它必须还在。
    """
    repo = _make_repo(tmp_path / "repo")
    _make_squash_scenario(repo)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "update-index",
            "--assume-unchanged",
            "backend/agent.py",
        ],
        check=True,
        capture_output=True,
    )
    item = _item("squash-to-main-base", DECLARED_SQUASH, target="origin/main")

    with pytest.raises(ProductCodeModified) as excinfo:
        with product_diff_guard(item, repo=repo):
            (repo / "backend" / "agent.py").write_text(
                "VALUE = 99\n", encoding="utf-8"
            )

    assert _porcelain(repo) == "", _porcelain(repo)
    assert "backend/agent.py" in excinfo.value.paths, excinfo.value.paths


def test_the_orchestrator_does_not_halt_on_a_declared_squash_item(
    tmp_path: Path,
    plan_dir: Path,
) -> None:
    """生产入口上：收尾阶段不再停在压扁那一条上，后面的条目照跑。

    真的 git、真的临时仓、真的编排器、真的执行器（不注入桩）—— 豁免是在
    生产路径上生效的，不只是守卫单测里成立。

    编排器在**产品代码被改**的那一刻停下整轮；这一条压扁之后 porcelain
    报出的那批条目正是它要做的事，所以整轮不该停在这里 —— 后面那条
    (``git status``) 必须真的跑过。

    压扁那一条自己的裁决仍是 FAIL：``git reset --soft`` 不打印任何东西，
    判定器要「退出码 + 输出」才算证据充分（那是
    ``backend/postprocess/judge.py`` 的另一条判据，不在这次改动里）。
    本用例要钉的是**守卫没有开火**：证据摘录里没有 porcelain 原文。
    """
    repo = _make_repo(tmp_path / "repo")
    _make_squash_scenario(repo)
    write_book(
        plan_dir,
        [
            book_item(
                "squash-to-main-base",
                command=DECLARED_SQUASH,
                target="origin/main",
            ),
            book_item("inspect-staged", command=STATUS_COMMAND),
        ],
    )
    executor = CommandRunner(freeze(load_spec(plan_dir)), workspace=repo)
    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=executor,
        sink=FakeNotifier(),
    )

    report = orchestrator.start()

    squash = next(
        e for e in report["items"] if e["id"] == "squash-to-main-base"
    )
    after = next(e for e in report["items"] if e["id"] == "inspect-staged")
    assert "git status --porcelain" not in squash["evidence_excerpt"], (
        "守卫把声明的压扁条目判成了产品代码越界：%r" % (squash,)
    )
    assert after["verdict"] == "PASS", after
    assert "backend/agent.py" in _porcelain(repo), _porcelain(repo)
