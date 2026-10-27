"""复测报表（``effect_report`` CLI）的端到端契约。

这份文件问的是复测报表的**可复跑性**：报表必须是「拿同一份基线再跑一次，
得到同一份东西」的产物，否则两张报表之间的比较（这次比基线好还是差）根本
不成立。

替身化的部分
------------

只有**墙上的钟**。报表里的每个数字都来自磁盘上的东西：冻结的基线、plan
目录里的用量报告、git 的提交历史。所以这里没有 mock 掉的读取，也没有
「假装 git 返回了这样的历史」—— 一条命令都不读源码，全部走真的子进程。

真实的东西
----------

* **真的子进程** —— ``python -m effect_report`` 真的被起一次，退出码真的
  被读回来。报表是子进程写出来的文件，测试读的是**那份文件**。
* **真的 git 仓库** —— 逃逸信号读的是提交历史，所以「它能不能认出一个
  revert」只能在真的仓库上问。夹具仓库里真的有一个 ``git revert`` 产生的
  提交（主题是 git 自己写的那句 ``Revert "..."``），也真的有一个修复提交。
* **真的 plan 目录** —— 每个 plan 目录里四个文件各回答一个问题：
  ``tasks.json`` 决定规模（4~8 个任务是中型），``llm_sessions.jsonl`` 决定
  完成时刻，``verification_report.json`` 决定结果，``usage_report.json``
  决定 token 与归因切分。

窗口规则的两个分支
------------------

窗口按「先到者为准」关闭（合入 + 7 天，或者跑满 2 个中型 plan），所以这份
测试准备两组 plan，各命中一个分支：

* **天数分支** —— 一个中型 plan（合入 + 2 天）、一个小 plan（合入 + 4 天），
  再加一个合入 + 9 天才结束的 plan。那个 plan 是「7 天已经过去」的**证据**
  （它本身不在窗口里：窗口在合入 + 7 天就关了）。
* **plan 分支** —— 两个中型 plan（合入 + 1 天、合入 + 3 天）加一个小 plan
  （合入 + 5 天）。没有 plan 越过 7 天那条线，所以只有 plan 规则成立。

两个分支各自**装的是谁**也被钉住：只说「类型是 days」不够，窗口里那份
名单也得对 —— 它同时证明报表读的是夹具目录（不是这台机器上碰巧的目录），
以及窗口切得准。

为什么逃逸信号要指到夹具仓库
----------------------------

报表读的是**它自己那个仓**的提交历史（``effect_report.REPO_ROOT``）。不指
的话，它读到的是这台机器上碰巧的提交历史 —— 跑批时刻不同，数字就不同，
「两次跑批逐字相同」这条契约也就无从谈起。所以子进程的 ``GIT_DIR`` 指到
夹具仓库：读到的仍然是**真的 git 历史**（真的提交、真的提交时刻），只是
换了一个不会随跑批时刻变化的仓库。

TDD contract — 5 cases + 1 guard
--------------------------------
1. ``test_metrics_complete`` —— 七项指标齐全，一个不多一个不少。
2. ``test_window_branches_covered`` —— 两个窗口分支各命中一次。
3. ``test_escape_signal_from_real_repo`` —— revert 与修复提交被识别。
4. ``test_rerun_byte_identical`` —— 去掉元数据后两次跑批逐字相同。
5. ``test_exit_zero_when_targets_missed`` —— 指标未达标仍退 0。

外加一条护栏：``test_production_verification_package_is_not_shadowed`` ——
本目录叫 ``verification``，生产包也叫 ``verification``。这条护栏盯住「谁
把 ``backend/tests/e2e/__init__.py`` 删了」：删了它，pytest 就会把本目录
当成顶层 ``verification``，生产包被顶掉，整套测试的收集与导入一起坏掉。

边界条件
--------

* 两次跑批除元数据（末尾那段印着自己路径的「复跑」命令）外逐字相同。
* 逃逸信号从真的夹具 git 仓库采集（真的 revert 提交 + 真的修复提交）。
* 指标未达标只是报表里的一条判定，退出码仍然是 0。
* 子进程与临时目录都在 ``finally`` 里回收 —— 一次测试不在磁盘上、也不在
  进程表里留下东西。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import pytest

# ``backend/tests/e2e/verification/`` → ``backend/``。
BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import effect_report  # noqa: E402


#: 整个文件带 ``e2e`` 标记：它起真的子进程、真的 git 仓库、真的 plan 目录。
pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 跑批用的解释器。优先用仓库自己的 venv（CI 的每一条 lane 都用它），
#: 没有就退回当前解释器。
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: 冻结的基线（任务 7 的产物）。这份测试用的就是它，不另造一份 ——
#: 基线换了，报表就换了。
BASELINE_FIXTURE = (
    BACKEND_DIR / "tests" / "fixtures" / "effect_report" / "baseline-sample.json"
)

#: 合入时刻：基线冻结的第二天。
SINCE = "2026-10-27"

#: 单次跑批的预算（秒）。超时即失败：这份测试的价值在于它当场知道链路
#: 断了，而不是十分钟后有人想起来看一眼。
RUN_BUDGET_SECONDS = 120

#: 指标名，逐字写死在这里。这份测试要钉的是「**报表里出现的是这七个
#: 名字**」，不是「模块里定义了七个名字」—— 后者由静态门禁
#: ``test_effect_report_metrics_frozen`` 管。
EXPECTED_METRIC_NAMES = (
    "pdt_plan_tokens",
    "per_plan_tokens",
    "repair_ratio",
    "exec_vs_verify",
    "round_median",
    "round_p90",
    "unattributed_ratio",
)

#: 三类逃逸信号，同样逐字写死。
EXPECTED_ESCAPE_SIGNALS = (
    "later_plan_failures",
    "post_merge_reverts",
    "targeted_fix_commits",
)

#: 夹具 plan 的用量。执行侧的 token 远多于验证侧 —— 比例落在基线声明的
#: 区间之外，于是 ``exec_vs_verify`` 必然判 ``not_met``，而报表**仍然**
#: 退 0（这正是「未达标仍退 0」那条用例要的现场）。
EXECUTION_TOKENS = 100_000
VERIFICATION_TOKENS = 1_000


# ---------------------------------------------------------------------------
# 读报表
# ---------------------------------------------------------------------------


def _table_rows(report: str, heading: str) -> Dict[str, List[str]]:
    """一张 markdown 表格：``{行首的名字: 这一行的格子}``。

    表头与分隔行不带反引号，被这一行挡掉；剩下的每一行第一格是指标或信号
    的名字（带反引号）。
    """
    section = report.split(heading, 1)[1].split("\n## ", 1)[0]
    rows: Dict[str, List[str]] = {}
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or "`" not in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows[cells[0].strip("`")] = cells
    return rows


def json_metrics(out: Path) -> set:
    """报表点名的指标 —— 从**落盘的那份文件**读回来。

    刻意不读内存里的对象：这份测试要证明的是「跑批的产物里有这些名字」。
    """
    return set(_table_rows(out.read_text(encoding="utf-8"), "## 效果指标"))


def json_escape_signals(out: Path) -> set:
    """报表点名的三类逃逸信号。"""
    return set(_table_rows(out.read_text(encoding="utf-8"), "## 逃逸信号"))


def escape_window_values(out: Path) -> Dict[str, str]:
    """逃逸信号表里「窗口」那一列的值。"""
    rows = _table_rows(out.read_text(encoding="utf-8"), "## 逃逸信号")
    return {name: cells[2] for name, cells in rows.items()}


def window_plan_ids(report: str) -> set:
    """窗口里被计入的 plan id。

    ``rounds_and_outcome`` 那张表的第一列就是窗口里每一个 plan 的 id ——
    「窗口里装的是谁」是报表的内容，不是报表的旁白。
    """
    return set(_table_rows(report, "## rounds_and_outcome"))


def metric_verdicts(out: Path) -> Dict[str, str]:
    """指标表里「判定」那一列的值。"""
    rows = _table_rows(out.read_text(encoding="utf-8"), "## 效果指标")
    return {name: cells[5] for name, cells in rows.items()}


def strip_meta(report: str) -> str:
    """去掉天生每次都不同的元数据。

    报表末尾那段「复跑」命令把它自己写的路径印了进去（基线路径、输出路
    径）—— 两次跑批写到不同的文件，路径就不同。除此之外报表应当逐字相
    同，所以这一段连同任何提到 ``generated_at`` 的行一起去掉，剩下的才
    是可以逐字比较的部分。
    """
    body = report.split("## 复跑", 1)[0]
    return "\n".join(
        line for line in body.splitlines() if "generated_at" not in line
    )


# ---------------------------------------------------------------------------
# 夹具：一个真的 git 仓库
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, when: Optional[str] = None) -> str:
    """跑一条 git 命令，返回它的标准输出。

    ``when`` 钉住提交时刻（作者与提交者都用它）：逃逸信号是按时间窗口过
    滤的，提交时刻不钉死，「窗口里有没有那个提交」就会随跑批时刻变。

    git 的配置也一并隔离（系统配置与全局配置都不读）：夹具仓库的提交
    时刻、身份、行为不能受这台机器的 ``~/.gitconfig`` 影响。
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Effect Report Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Effect Report Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {repo} (rc={proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


@pytest.fixture
def workspace() -> Iterator[Path]:
    """一次测试的工作区：夹具仓库、plan 目录、报表产物都在里面。

    ``finally`` 里删掉它 —— 一次测试不该在磁盘上留下东西。
    """
    root = Path(tempfile.mkdtemp(prefix="effect-report-e2e-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def fixture_repo(workspace: Path) -> Path:
    """一个真的 git 仓库，里面有真的 revert 提交与真的修复提交。

    六个提交，三个时间带：

    * ``2026-10-19`` —— 基线期之前，不属于任何一个窗口；
    * ``2026-10-21`` / ``2026-10-23`` —— 基线期里：一个修复提交，加上一个
      真的 ``git revert``（主题是 git 自己写的那句 ``Revert "..."``）；
    * ``2026-10-27`` / ``2026-10-28`` / ``2026-10-29`` —— 窗口里：一个
      被真的 revert 掉的提交、一个真的 ``git revert``、一个修复提交。
    """
    repo = workspace / "fixture-repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "commit.gpgsign", "false")

    (repo / "ledger.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "feat: add the ledger reader",
        when="2026-10-19T09:00:00+00:00",
    )

    (repo / "ledger.py").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "fix: correct the ledger rounding",
        when="2026-10-21T09:00:00+00:00",
    )
    _git(repo, "revert", "--no-edit", "HEAD", when="2026-10-23T09:00:00+00:00")

    (repo / "window.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "feat: add the plan window",
        when="2026-10-27T09:00:00+00:00",
    )
    _git(repo, "revert", "--no-edit", "HEAD", when="2026-10-28T09:00:00+00:00")

    (repo / "window.py").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "fix: repair the plan window boundary",
        when="2026-10-29T09:00:00+00:00",
    )
    return repo


# ---------------------------------------------------------------------------
# 夹具：真的 plan 目录
# ---------------------------------------------------------------------------


def _write_plan(
    plans_dir: Path,
    plan_id: str,
    *,
    tasks: int,
    completed_at: str,
    outcome: str,
) -> None:
    """写一个可被观察到的 plan 目录。

    ``usage_report.json`` 里**必须**带 ``by_vp``：报表优先读已经落盘的用量
    报告，只有它缺了归因切分时才会现算 —— 现算出来的那份带着「算它的这一
    刻」的时间戳，报表就不再可复跑了。
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    total = EXECUTION_TOKENS + VERIFICATION_TOKENS

    (plan_dir / "usage_report.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "generated_at": completed_at,
                "totals": {
                    "requests": 4,
                    "total_tokens": total,
                    "cost_usd": 1.0,
                },
                "by_scene": [
                    {
                        "key": "execution",
                        "requests": 2,
                        "total_tokens": EXECUTION_TOKENS,
                        "cost_usd": 1.0,
                    },
                    {
                        "key": "verification",
                        "requests": 2,
                        "total_tokens": VERIFICATION_TOKENS,
                        "cost_usd": 1.0,
                    },
                ],
                "by_vp": {
                    "(unattributed)": {
                        "requests": 4,
                        "total_tokens": total,
                        "cost_usd": 1.0,
                    }
                },
                "by_round": {
                    "1": {
                        "requests": 2,
                        "total_tokens": EXECUTION_TOKENS,
                        "cost_usd": 1.0,
                    },
                    "2": {
                        "requests": 2,
                        "total_tokens": VERIFICATION_TOKENS,
                        "cost_usd": 1.0,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": f"t{index}"} for index in range(tasks)]}),
        encoding="utf-8",
    )
    (plan_dir / "verification_report.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "overall_status": outcome,
                "generated_at": completed_at,
            }
        ),
        encoding="utf-8",
    )
    (plan_dir / "llm_sessions.jsonl").write_text(
        json.dumps(
            {
                "ts": completed_at,
                "session_id": f"session-{plan_id}",
                "scene": "execution",
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def days_window_plans(workspace: Path) -> Path:
    """天数规则关闭窗口的那一组 plan。

    合入 + 2 天一个中型 plan、合入 + 4 天一个小 plan、合入 + 9 天一个见证
    plan。那个见证 plan 是「7 天已经过去」的证据，它本身不在窗口里。
    """
    plans_dir = workspace / "plans-days"
    plans_dir.mkdir()
    _write_plan(
        plans_dir,
        "win-medium",
        tasks=5,
        completed_at="2026-10-29T00:00:00+00:00",
        outcome="FAILED",
    )
    _write_plan(
        plans_dir,
        "win-small",
        tasks=2,
        completed_at="2026-10-31T00:00:00+00:00",
        outcome="PASSED",
    )
    _write_plan(
        plans_dir,
        "win-witness",
        tasks=3,
        completed_at="2026-11-05T00:00:00+00:00",
        outcome="PASSED",
    )
    return plans_dir


@pytest.fixture
def plans_window_plans(workspace: Path) -> Path:
    """plan 规则关闭窗口的那一组 plan。

    合入 + 1 天与合入 + 3 天各一个中型 plan，合入 + 5 天一个小 plan。没有
    plan 越过 7 天那条线，所以只有 plan 规则成立。
    """
    plans_dir = workspace / "plans-plans"
    plans_dir.mkdir()
    _write_plan(
        plans_dir,
        "win-medium-a",
        tasks=5,
        completed_at="2026-10-28T00:00:00+00:00",
        outcome="PASSED",
    )
    _write_plan(
        plans_dir,
        "win-medium-b",
        tasks=5,
        completed_at="2026-10-30T00:00:00+00:00",
        outcome="FAILED",
    )
    _write_plan(
        plans_dir,
        "win-small",
        tasks=2,
        completed_at="2026-11-01T00:00:00+00:00",
        outcome="PASSED",
    )
    return plans_dir


# ---------------------------------------------------------------------------
# 跑一次真的 CLI
# ---------------------------------------------------------------------------


def _run_cli(
    *,
    out: Path,
    plans_dir: Path,
    repo: Path,
    cwd: Path,
    baseline: Path = BASELINE_FIXTURE,
) -> subprocess.CompletedProcess:
    """起一次真的 ``python -m effect_report`` 子进程。

    三个环境变量是这份测试的全部「替身」，每一个都有理由：

    * ``PYTHONPATH`` —— 让子进程 import 得到 ``backend/`` 下的模块。
    * ``PDT_PLANS_DIR`` —— 把被观察的 plan 树指到夹具目录，不碰操作员的
      ``plans/``。
    * ``GIT_DIR`` —— 把逃逸信号读的提交历史指到夹具仓库（理由见模块文档）。

    子进程在 ``finally`` 里被回收：一次测试不该在进程表里留下东西。
    """
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(BACKEND_DIR),
            "PDT_PLANS_DIR": str(plans_dir),
            "GIT_DIR": str(repo / ".git"),
        }
    )
    command = [
        str(VENV_PYTHON),
        "-m",
        "effect_report",
        "--baseline",
        str(baseline),
        "--since",
        SINCE,
        "--out",
        str(out),
    ]
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=RUN_BUDGET_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AssertionError(
            f"the effect report CLI did not finish in {RUN_BUDGET_SECONDS}s"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


# ---------------------------------------------------------------------------
# 1. 七项指标齐全
# ---------------------------------------------------------------------------


def test_metrics_complete(workspace, fixture_repo, days_window_plans):
    """七项指标齐全 —— 一个不多，一个不少。

    报表要能跟基线逐项对齐，缺的那一项不是「没测」，而是「没人记得它叫什
    么」。所以这里比的是**报表里出现的那七个名字**，不是模块里的常量。
    """
    out = workspace / "effect-metrics.md"
    proc = _run_cli(
        out=out,
        plans_dir=days_window_plans,
        repo=fixture_repo,
        cwd=workspace,
    )

    assert proc.returncode == 0, (
        f"the effect report CLI failed (rc={proc.returncode}): {proc.stderr}"
    )
    assert out.exists(), f"the CLI did not write the report: {out}"

    named = json_metrics(out)
    assert named == set(EXPECTED_METRIC_NAMES), (
        "the report must name exactly the seven metrics — a missing one is a "
        f"metric nobody can compare against the baseline: {sorted(named)}"
    )
    assert len(EXPECTED_METRIC_NAMES) == 7, (
        f"the frozen metric set has seven entries: {EXPECTED_METRIC_NAMES!r}"
    )
    assert named == set(effect_report.METRIC_NAMES), (
        "the report and the module's frozen metric set disagree: "
        f"{sorted(named)} vs {sorted(effect_report.METRIC_NAMES)}"
    )
    assert json_escape_signals(out) == set(EXPECTED_ESCAPE_SIGNALS), (
        "the report must name the three escape signals: "
        f"{sorted(json_escape_signals(out))}"
    )


# ---------------------------------------------------------------------------
# 2. 两个窗口分支各命中一次
# ---------------------------------------------------------------------------


def test_window_branches_covered(workspace, fixture_repo, days_window_plans, plans_window_plans):
    """窗口规则的两个分支各命中一次。

    窗口按「先到者为准」关闭，所以这一条要跑两次：一次让 7 天那条线先到，
    一次让第 2 个中型 plan 先到。两个分支都命中，窗口规则才真的被测到了。
    """
    days_out = workspace / "effect-days.md"
    days_proc = _run_cli(
        out=days_out,
        plans_dir=days_window_plans,
        repo=fixture_repo,
        cwd=workspace,
    )
    assert days_proc.returncode == 0, (
        f"the effect report CLI failed (rc={days_proc.returncode}): "
        f"{days_proc.stderr}"
    )
    days_report = days_out.read_text(encoding="utf-8")

    plans_out = workspace / "effect-plans.md"
    plans_proc = _run_cli(
        out=plans_out,
        plans_dir=plans_window_plans,
        repo=fixture_repo,
        cwd=workspace,
    )
    assert plans_proc.returncode == 0, (
        f"the effect report CLI failed (rc={plans_proc.returncode}): "
        f"{plans_proc.stderr}"
    )
    plans_report = plans_out.read_text(encoding="utf-8")

    assert "类型 `days`" in days_report, (
        "the days rule must close the window when no plan crosses the 2-medium "
        f"line: {days_report[:600]!r}"
    )
    assert "类型 `plans`" in plans_report, (
        "the plans rule must close the window when the second medium plan "
        f"finishes first: {plans_report[:600]!r}"
    )
    assert "类型 `days`" not in plans_report, (
        "the plans fixture must not also satisfy the days rule, or the second "
        "branch would never be exercised on its own"
    )
    assert "类型 `plans`" not in days_report, (
        "the days fixture must not also satisfy the plans rule, or the first "
        "branch would never be exercised on its own"
    )

    # 窗口的类型对了还不够，窗口里**装的是谁**也得对：天数分支下那个合入
    # +9 天才结束的见证 plan 不在窗口里（窗口在合入 + 7 天就关了），plan
    # 分支下那个合入 + 5 天的小 plan 也不在窗口里（窗口在第 2 个中型 plan
    # 就关了）。这两条同时钉住「读的是夹具目录」与「窗口切得准」。
    assert window_plan_ids(days_report) == {"win-medium", "win-small"}, (
        "the days window must hold exactly the two plans that finished before "
        "merge + 7 days — the witness plan that finished later is outside it: "
        f"{sorted(window_plan_ids(days_report))}"
    )
    assert window_plan_ids(plans_report) == {"win-medium-a", "win-medium-b"}, (
        "the plans window must close on the second medium plan, so the small "
        "plan that finished later is outside it: "
        f"{sorted(window_plan_ids(plans_report))}"
    )


# ---------------------------------------------------------------------------
# 3. 逃逸信号从真的夹具仓库采集
# ---------------------------------------------------------------------------


def test_escape_signal_from_real_repo(workspace, fixture_repo, days_window_plans):
    """revert 与修复提交被识别 —— 数字来自真的 git 历史。

    「指标好看但缺陷照旧逃逸」是这套改动最容易骗过自己的地方，所以逃逸信
    号必须是**数出来的**，不是猜出来的。这里数的是夹具仓库里真的那一个
    ``git revert`` 与真的那一个修复提交。
    """
    out = workspace / "effect-escape.md"
    proc = _run_cli(
        out=out,
        plans_dir=days_window_plans,
        repo=fixture_repo,
        cwd=workspace,
    )

    assert proc.returncode == 0, (
        f"the effect report CLI failed (rc={proc.returncode}): {proc.stderr}"
    )

    subjects = _git(fixture_repo, "log", "--format=%s")
    assert 'Revert "feat: add the plan window"' in subjects, (
        "the fixture repository must carry a real revert commit: "
        f"{subjects.splitlines()!r}"
    )
    assert "fix: repair the plan window boundary" in subjects, (
        "the fixture repository must carry a real fix commit: "
        f"{subjects.splitlines()!r}"
    )

    values = escape_window_values(out)
    assert values["post_merge_reverts"] == "1", (
        "the report must count the one revert commit inside the window: "
        f"{values!r}"
    )
    assert values["targeted_fix_commits"] == "1", (
        "the report must count the one fix commit inside the window "
        "(a revert is not also counted as a fix): " f"{values!r}"
    )


# ---------------------------------------------------------------------------
# 4. 两次跑批逐字相同
# ---------------------------------------------------------------------------


def test_rerun_byte_identical(workspace, fixture_repo, days_window_plans):
    """同一份基线跑两次，去掉元数据后逐字相同。

    报表要能跟下一轮比较，它自己就必须先稳定：两份报表如果连字节都不一样，
    「这一轮比基线好还是差」就无从谈起。两次跑批写到**不同的**输出文件，
    所以差异只允许出现在印着自己路径的那一段。
    """
    first_out = workspace / "effect-run-1.md"
    second_out = workspace / "effect-run-2.md"

    first_proc = _run_cli(
        out=first_out,
        plans_dir=days_window_plans,
        repo=fixture_repo,
        cwd=workspace,
    )
    assert first_proc.returncode == 0, (
        f"the effect report CLI failed (rc={first_proc.returncode}): "
        f"{first_proc.stderr}"
    )
    second_proc = _run_cli(
        out=second_out,
        plans_dir=days_window_plans,
        repo=fixture_repo,
        cwd=workspace,
    )
    assert second_proc.returncode == 0, (
        f"the effect report CLI failed (rc={second_proc.returncode}): "
        f"{second_proc.stderr}"
    )

    first = first_out.read_text(encoding="utf-8")
    second = second_out.read_text(encoding="utf-8")

    assert strip_meta(first) == strip_meta(second), (
        "the same baseline must produce the same report on a second run — a "
        "report that changes between runs cannot be compared with anything"
    )
    assert first.replace(str(first_out), "<OUT>") == second.replace(
        str(second_out), "<OUT>"
    ), (
        "the only difference between two runs may be the output path the "
        "report prints in its own rerun instructions"
    )


# ---------------------------------------------------------------------------
# 5. 未达标仍退 0
# ---------------------------------------------------------------------------


def test_exit_zero_when_targets_missed(workspace, fixture_repo, days_window_plans):
    """指标未达标 → 报表记一条判定，退出码仍然是 0。

    效果报表是**测量**，不是门禁：它没达标就不达标，绝不能让流水线因为
    「效果不够好」变红。所以判定必须写在报表里，而退出码必须是 0。
    """
    out = workspace / "effect-missed.md"
    proc = _run_cli(
        out=out,
        plans_dir=days_window_plans,
        repo=fixture_repo,
        cwd=workspace,
    )

    assert proc.returncode == 0, (
        "the effect report is a measurement, not a gate: a missed target must "
        f"not change the exit code (got {proc.returncode}): {proc.stderr}"
    )

    verdicts = metric_verdicts(out)
    assert verdicts["exec_vs_verify"] == "not_met", (
        "the fixture window's execution/verification ratio sits outside the "
        "baseline's band, so this metric must be judged a miss: "
        f"{verdicts!r}"
    )
    assert "not_met" in out.read_text(encoding="utf-8"), (
        "the report must record the missed target instead of hiding it"
    )


# ---------------------------------------------------------------------------
# 6. 护栏：这个包不遮蔽生产包
# ---------------------------------------------------------------------------


def test_production_verification_package_is_not_shadowed():
    """顶层 ``verification`` 必须仍然是生产包 ``backend/verification``。

    这个目录叫 ``verification``，生产包也叫 ``verification`` —— 同一个名字。
    pytest 从测试文件往上找 ``__init__.py`` 来决定包根：只要
    ``backend/tests/e2e/__init__.py`` 在，本目录的模块名就是
    ``tests.e2e.verification.test_xxx``，顶层 ``verification`` 归生产包；
    少了它，本目录就成了顶层 ``verification``，生产包被顶掉 —— 症状是别的
    测试突然 ``cannot import name 'VerificationOrchestrator' from
    'verification'``，或者本文件干脆收集不到。

    这条断言把那条线钉死：谁动了 ``backend/tests/e2e/__init__.py``，这里
    立刻红。
    """
    import verification

    package_dir = Path(verification.__file__).resolve().parent
    assert package_dir == (BACKEND_DIR / "verification").resolve(), (
        "the top-level `verification` name must still belong to the "
        "production package — if it points at the test package, pytest is "
        f"importing this directory as the top-level `verification`: {package_dir}"
    )