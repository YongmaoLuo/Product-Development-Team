"""两个离线 CLI 的端到端入口契约：``verification.scan`` 与 ``effect_report``。

这两个 CLI 是本轮唯一能**完全离线**驱动的入口，所以「它们能跑」这件事必须
由真的子进程证明：读源码能证明代码写成了这样，不能证明
``python -m verification.scan`` 起得来、落盘的文件 schema 合法、跑两遍得到
同一份东西。

真实的东西
----------
* **真的子进程** —— 两条 CLI 真的被起一次，退出码真的被读回来。测试读的是
  子进程**落盘的那份文件**，不是内存里的对象。
* **真的文件系统** —— 计划目录、plan 目录、夹具 git 仓库都在临时目录里真的
  建出来，产物也真的写在临时目录里。
* **真的 git 历史** —— 逃逸信号读的是夹具仓库的提交历史（真的提交、真的提交
  时刻），不是这台机器碰巧的提交历史。

替身化的部分
------------
只有三处，且每一处都有理由：

* ``PYTHONPATH`` —— 让子进程 import 得到 ``backend/`` 下的模块。
* ``PDT_PLANS_DIR`` —— 把被观察的 plan 树指到夹具目录，不碰操作员的
  ``plans/``。
* ``GIT_DIR`` —— 把逃逸信号读的提交历史指到夹具仓库。

TDD contract — 4 cases
-----------------------
1. ``test_scan_cli_exit_and_schema`` —— 扫描 CLI 退 0，产物是合法 JSON，
   ``schema_version`` 是 1，计数与夹具逐项对得上。
2. ``test_effect_report_cli_exit_and_schema`` —— 复测 CLI 退 0，报表里七项
   指标齐全（一个不多一个不少），窗口装的是夹具里的 plan。
3. ``test_rerun_identical_except_meta`` —— 两个 CLI 各跑两遍，去掉元数据后
   逐字相同。
4. ``test_empty_plans_dir_zero_report`` —— 空计划目录 → 零值报告，且退 0。

边界条件
--------
* 产物写进临时目录，``finally`` 里清理：一次测试不在磁盘上留下东西。
* 子进程在 ``finally`` 里被回收：一次测试不在进程表里留下东西。
* ``--since`` 与夹具里的时刻是写死的常量 —— 跑批时刻一变，窗口的成员就变了。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Union

import pytest

# ``backend/tests/e2e/verification/`` → ``backend/``。
BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import effect_report  # noqa: E402


#: 整个文件带 ``e2e`` 标记：它起真的子进程、真的 plan 目录、真的 git 仓库。
pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 跑批用的解释器。优先用仓库自己的 venv（CI 的每一条 lane 都用它），没有
#: 就退回当前解释器。
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: 子进程的 ``PYTHONPATH``。``backend/`` 让 ``verification.scan`` 与
#: ``effect_report`` 可 import；仓库根让 ``backend.framework.prompts`` 可
#: import —— ``backend/verification/__init__.py`` 会走那条链，少了它
#: ``python -m verification.scan`` 起不来。
SUBPROCESS_PYTHONPATH = os.pathsep.join([str(BACKEND_DIR), str(REPO_ROOT)])

#: 冻结的基线（任务 7 的产物）。这份测试用的就是它，不另造一份 —— 基线换了，
#: 报表就换了。
BASELINE_FIXTURE = (
    BACKEND_DIR / "tests" / "fixtures" / "effect_report" / "baseline-sample.json"
)

#: 合入时刻。写死，不取「今天」—— 跑批时刻一变，窗口的成员就变了。
SINCE = "2026-10-27"

#: 单次跑批的预算（秒）。超时即失败：这份测试的价值在于它当场知道链路断了，
#: 而不是十分钟后有人想起来看一眼。
RUN_BUDGET_SECONDS = 120

#: 七项指标，逐字写死在这里。这份测试要钉的是「**报表里出现的是这七个名
#: 字**」，不是「模块里定义了七个名字」。
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

#: 报告里天生每次都不同的键。JSON 产物的时间戳就落在这里。
VOLATILE_KEYS = frozenset({"generated_at", "created_at", "timestamp"})

#: 夹具 plan 的用量：执行侧的 token 远多于验证侧，于是 ``exec_vs_verify``
#: 的比值落在 5 上下。
_EXECUTION_TOKENS = 100_000
_VERIFICATION_TOKENS = 20_000
_TOTAL_TOKENS = _EXECUTION_TOKENS + _VERIFICATION_TOKENS


# ---------------------------------------------------------------------------
# 夹具：扫描用的 VP
# ---------------------------------------------------------------------------

#: 一条合法的 ``api_test`` VP：request 齐全 + assertions 齐全。
API_VP = {
    "id": "VP-001",
    "verification_method": "api_test",
    "request": {"method": "GET", "url": "http://127.0.0.1:8000/api/items"},
    "assertions": [{"name": "状态码", "status": 200}],
}

#: 一条合法的 ``full_ci`` VP：整仓入口存在，且没有被收窄成「只跑一个用例」。
CI_VP = {
    "id": "VP-002",
    "verification_method": "full_ci",
    "ci_entry": "python scripts/ci_local.py",
}

#: 一条 ``e2e`` VP：它的执行入口是 ``target_url``。
E2E_VP = {
    "id": "VP-003",
    "verification_method": "e2e",
    "target_url": "http://127.0.0.1:5173/checkout",
}

#: 一条 ``code_review`` VP：它是合法 VP，但不是可确定性执行的方法。
REVIEW_VP = {"id": "VP-004", "verification_method": "code_review"}

#: 一条不带 ``verification_method`` 的 VP：计入总数，不计入确定性。
UNSPECIFIED_VP = {"id": "VP-005"}


# ---------------------------------------------------------------------------
# 夹具：真的文件系统
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace() -> Iterator[Path]:
    """一次测试的工作区：夹具仓库、plan 目录、产物都在里面。

    ``finally`` 里删掉它 —— 一次测试不该在磁盘上留下东西。
    """
    root = Path(tempfile.mkdtemp(prefix="cli-e2e-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _write_scan_plan(
    plans_dir: Path, plan_id: str, vps: Sequence[dict]
) -> Path:
    """写一份真的机器计划：``<plans_dir>/<plan_id>/verification_plan.json``。"""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(
        json.dumps({"verification_points": list(vps)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return plan_dir


def _usage_report() -> dict:
    """夹具 plan 的用量报告：token 与归因切分都在里面。"""
    return {
        "totals": {
            "requests": 12,
            "total_tokens": _TOTAL_TOKENS,
            "cost_usd": 1.5,
        },
        "by_scene": {
            "execution": {
                "requests": 9,
                "total_tokens": _EXECUTION_TOKENS,
                "cost_usd": 1.2,
            },
            "verification": {
                "requests": 3,
                "total_tokens": _VERIFICATION_TOKENS,
                "cost_usd": 0.3,
            },
        },
        "by_vp": {
            "VP-001": {
                "requests": 9,
                "total_tokens": _EXECUTION_TOKENS,
                "cost_usd": 1.2,
            },
            "VP-002": {
                "requests": 3,
                "total_tokens": _VERIFICATION_TOKENS,
                "cost_usd": 0.3,
            },
        },
        "by_round": {
            "1": {
                "requests": 9,
                "total_tokens": _EXECUTION_TOKENS,
                "cost_usd": 1.2,
            },
            "2": {
                "requests": 3,
                "total_tokens": _VERIFICATION_TOKENS,
                "cost_usd": 0.3,
            },
        },
        "by_day": {
            "2026-10-28": {
                "requests": 12,
                "total_tokens": _TOTAL_TOKENS,
                "cost_usd": 1.5,
            }
        },
    }


def _write_plan(
    plans_dir: Path,
    plan_id: str,
    *,
    task_count: int,
    completed_at: str,
    outcome: str,
) -> Path:
    """在 ``plans_dir`` 下写一个真的 plan 目录。

    四个文件各回答一个问题：``tasks.json`` 决定规模，``llm_sessions.jsonl``
    决定完成时刻，``verification_report.json`` 决定结果，``usage_report.json``
    决定 token 与归因切分。
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {"tasks": [{"id": f"T-{index + 1}"} for index in range(task_count)]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plan_dir / "llm_sessions.jsonl").write_text(
        json.dumps({"ts": completed_at, "session_id": "s-1"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (plan_dir / "verification_report.json").write_text(
        json.dumps(
            {"overall_status": outcome, "generated_at": completed_at},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plan_dir / "usage_report.json").write_text(
        json.dumps(_usage_report(), ensure_ascii=False),
        encoding="utf-8",
    )
    return plan_dir


# ---------------------------------------------------------------------------
# 夹具：一个真的 git 仓库
# ---------------------------------------------------------------------------

#: 夹具仓库的四个提交。主题里的 ``Revert`` 与 ``fix`` 是报表数逃逸信号用的
#: 两种形状；时刻分两个时间带 —— 基线期（10-20 ~ 10-26）与窗口期
#: （10-27 ~ 11-03）。
_FIXTURE_COMMITS = (
    ("2026-10-21T10:00:00+00:00", "feat: baseline work"),
    ("2026-10-23T10:00:00+00:00", 'Revert "feat: baseline work"'),
    ("2026-10-28T10:00:00+00:00", "fix: repair the checkout flow"),
    ("2026-10-30T10:00:00+00:00", 'Revert "fix: repair the checkout flow"'),
)


def _git(repo: Path, *args: str, when: Optional[str] = None) -> str:
    """跑一条 git 命令，返回它的标准输出。

    ``when`` 钉住提交时刻（作者与提交者都用它）：逃逸信号是按时间窗口过滤的，
    提交时刻不钉死，「窗口里有没有那个提交」就会随跑批时刻变。

    git 的配置也一并隔离（系统配置与全局配置都不读）：夹具仓库的提交时刻、
    身份、行为不能受这台机器的全局 git 配置影响。
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "CLI Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "CLI Fixture",
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
def fixture_repo(workspace: Path) -> Path:
    """一个真的 git 仓库：四个提交，两个时间带。"""
    repo = workspace / "fixture-repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--quiet")
    for index, (when, subject) in enumerate(_FIXTURE_COMMITS):
        (repo / f"file-{index}.txt").write_text(f"{subject}\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "--quiet", "-m", subject, when=when)
    return repo


# ---------------------------------------------------------------------------
# 跑一次真的 CLI
# ---------------------------------------------------------------------------


def _run_cli(
    args: Sequence[str],
    *,
    cwd: Path,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """起一次真的 ``python -m <args>`` 子进程。

    子进程在 ``finally`` 里被回收：一次测试不该在进程表里留下东西。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = SUBPROCESS_PYTHONPATH
    env.update(extra_env or {})
    command = [str(VENV_PYTHON), "-m", *args]
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
            f"the CLI did not finish in {RUN_BUDGET_SECONDS}s: {' '.join(command)}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def _run_scan(
    plans_dir: Path, out: Path, *, cwd: Path
) -> subprocess.CompletedProcess:
    """跑一次真的 ``python -m verification.scan``。"""
    return _run_cli(
        ["verification.scan", "--plans-dir", str(plans_dir), "--out", str(out)],
        cwd=cwd,
    )


def _run_effect(
    *,
    baseline: Path,
    since: str,
    out: Path,
    cwd: Path,
    plans_dir: Path,
    repo: Path,
) -> subprocess.CompletedProcess:
    """跑一次真的 ``python -m effect_report``。

    ``PDT_PLANS_DIR`` 把被观察的 plan 树指到夹具目录，``GIT_DIR`` 把逃逸信号
    读的提交历史指到夹具仓库 —— 后者不指的话，它读到的是这台机器上碰巧的提交
    历史，跑批时刻不同，数字就不同。
    """
    return _run_cli(
        [
            "effect_report",
            "--baseline",
            str(baseline),
            "--since",
            since,
            "--out",
            str(out),
        ],
        cwd=cwd,
        extra_env={
            "PDT_PLANS_DIR": str(plans_dir),
            "GIT_DIR": str(repo / ".git"),
        },
    )


# ---------------------------------------------------------------------------
# 读产物
# ---------------------------------------------------------------------------


def _drop_volatile(value):
    """递归去掉天生每次都不同的键。"""
    if isinstance(value, dict):
        return {
            key: _drop_volatile(item)
            for key, item in value.items()
            if key not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_drop_volatile(item) for item in value]
    return value


def strip_meta(payload: Union[str, bytes]) -> str:
    """去掉天生每次都不同的元数据，剩下的部分必须逐字相同。

    两种产物各有一种元数据：

    * JSON 报告里的时间戳键；
    * markdown 报表末尾那段「复跑」命令 —— 它把自己写的路径印了进去，两次
      跑批写到不同的文件，路径就不同。
    """
    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return json.dumps(
                _drop_volatile(data), ensure_ascii=False, sort_keys=True, indent=2
            )
    body = text.split("## 复跑", 1)[0]
    return "\n".join(
        line for line in body.splitlines() if "generated_at" not in line
    )


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


def metric_names(out: Path) -> set:
    """报表点名的指标 —— 从**落盘的那份文件**读回来。

    刻意不读内存里的对象：这份测试要证明的是「跑批的产物里有这些名字」。
    """
    return set(_table_rows(out.read_text(encoding="utf-8"), "## 效果指标"))


def escape_signal_names(out: Path) -> set:
    """报表点名的三类逃逸信号。"""
    return set(_table_rows(out.read_text(encoding="utf-8"), "## 逃逸信号"))


def window_plan_ids(out: Path) -> set:
    """窗口里被计入的 plan id。

    ``rounds_and_outcome`` 那张表的第一列就是窗口里每一个 plan 的 id ——
    「窗口里装的是谁」是报表的内容，不是报表的旁白。
    """
    return set(_table_rows(out.read_text(encoding="utf-8"), "## rounds_and_outcome"))


# ---------------------------------------------------------------------------
# 夹具：一次跑批需要的 plan 目录
# ---------------------------------------------------------------------------


def _seed_plans(plans_dir: Path) -> None:
    """两个 plan：一个落在窗口里，一个落在窗口之外。

    ``plan-a`` 在合入后一天结束（中型），它证明窗口装的是夹具里的 plan；
    ``plan-b`` 在合入之后很久才结束，它是「天数规则」到点的证据 —— 它本身
    不在窗口里（窗口在合入 + 7 天就关了）。
    """
    _write_plan(
        plans_dir,
        "plan-a",
        task_count=6,
        completed_at="2026-10-28T00:00:00+00:00",
        outcome="PASSED",
    )
    _write_plan(
        plans_dir,
        "plan-b",
        task_count=2,
        completed_at="2026-11-05T00:00:00+00:00",
        outcome="FAILED",
    )


# ---------------------------------------------------------------------------
# 1. 扫描 CLI：退 0，schema 合法
# ---------------------------------------------------------------------------


def test_scan_cli_exit_and_schema(workspace):
    """扫描 CLI 退 0，产物是合法 JSON，schema_version 是 1，计数对得上。"""
    plans_dir = workspace / "plans"
    _write_scan_plan(
        plans_dir,
        "plan-a",
        [API_VP, CI_VP, E2E_VP, REVIEW_VP, UNSPECIFIED_VP],
    )
    out = workspace / "scan.json"

    proc = _run_scan(plans_dir, out, cwd=workspace)

    assert proc.returncode == 0, (
        f"the scan CLI failed (rc={proc.returncode}): {proc.stderr}"
    )
    assert out.exists(), f"the CLI did not write the report: {out}"

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1, (
        f"the scan report must carry schema_version 1: {report!r}"
    )
    assert report["totals"] == {
        "plans": 1,
        "vp_total": 5,
        "deterministic_total": 3,
        "ratio": 0.6,
    }, f"the totals disagree with the fixture: {report['totals']!r}"
    assert len(report["plans"]) == 1
    plan = report["plans"][0]
    assert plan["plan_id"] == "plan-a"
    assert plan["deterministic"] == {"api_test": 1, "full_ci": 1, "e2e": 1}
    assert plan["by_method"] == {
        "api_test": 1,
        "full_ci": 1,
        "e2e": 1,
        "code_review": 1,
        "(unspecified)": 1,
    }


# ---------------------------------------------------------------------------
# 2. 复测 CLI：退 0，指标齐全
# ---------------------------------------------------------------------------


def test_effect_report_cli_exit_and_schema(workspace, fixture_repo):
    """复测 CLI 退 0，报表里七项指标齐全，窗口装的是夹具里的 plan。"""
    plans_dir = workspace / "plans"
    _seed_plans(plans_dir)
    out = workspace / "effect.md"

    proc = _run_effect(
        baseline=BASELINE_FIXTURE,
        since=SINCE,
        out=out,
        cwd=workspace,
        plans_dir=plans_dir,
        repo=fixture_repo,
    )

    assert proc.returncode == 0, (
        f"the effect report CLI failed (rc={proc.returncode}): {proc.stderr}"
    )
    assert out.exists(), f"the CLI did not write the report: {out}"

    named = metric_names(out)
    assert named == set(EXPECTED_METRIC_NAMES), (
        "the report must name exactly the seven metrics — a missing one is a "
        f"metric nobody can compare against the baseline: {sorted(named)}"
    )
    assert named == set(effect_report.METRIC_NAMES), (
        "the report and the module's frozen metric set disagree: "
        f"{sorted(named)} vs {sorted(effect_report.METRIC_NAMES)}"
    )
    assert escape_signal_names(out) == set(EXPECTED_ESCAPE_SIGNALS), (
        "the report must name the three escape signals: "
        f"{sorted(escape_signal_names(out))}"
    )

    # 窗口里装的是夹具里的 plan，不是这台机器上碰巧的 plan。
    assert window_plan_ids(out) == {"plan-a"}, (
        "the report must count the fixture plan in the window: "
        f"{sorted(window_plan_ids(out))}"
    )

    # 指标的值来自夹具的用量报告 —— 不是零值，也不是别的目录的数字。
    rows = _table_rows(out.read_text(encoding="utf-8"), "## 效果指标")
    assert rows["pdt_plan_tokens"][2] == str(_TOTAL_TOKENS)
    assert rows["round_median"][2] == "2"
    assert rows["exec_vs_verify"][2] == (
        f"{_EXECUTION_TOKENS} : {_VERIFICATION_TOKENS} (×5.00)"
    )

    # 逃逸信号的数字来自夹具仓库的提交历史：窗口里一个 revert、一个修复。
    signals = _table_rows(out.read_text(encoding="utf-8"), "## 逃逸信号")
    assert signals["post_merge_reverts"][2] == "1"
    assert signals["targeted_fix_commits"][2] == "1"


# ---------------------------------------------------------------------------
# 3. 连跑两次：除元数据外逐字一致
# ---------------------------------------------------------------------------


def test_rerun_identical_except_meta(workspace, fixture_repo):
    """两个 CLI 各跑两遍，去掉元数据后逐字相同。"""
    plans_dir = workspace / "plans"
    _write_scan_plan(
        plans_dir,
        "plan-a",
        [API_VP, CI_VP, E2E_VP, REVIEW_VP, UNSPECIFIED_VP],
    )
    _seed_plans(plans_dir)

    scan_outs = [workspace / "scan-1.json", workspace / "scan-2.json"]
    for target in scan_outs:
        proc = _run_scan(plans_dir, target, cwd=workspace)
        assert proc.returncode == 0, (
            f"the scan CLI failed (rc={proc.returncode}): {proc.stderr}"
        )
    assert strip_meta(scan_outs[0].read_bytes()) == strip_meta(
        scan_outs[1].read_bytes()
    ), "two scan runs must produce the same report once the metadata is gone"

    effect_outs = [workspace / "effect-1.md", workspace / "effect-2.md"]
    for target in effect_outs:
        proc = _run_effect(
            baseline=BASELINE_FIXTURE,
            since=SINCE,
            out=target,
            cwd=workspace,
            plans_dir=plans_dir,
            repo=fixture_repo,
        )
        assert proc.returncode == 0, (
            f"the effect report CLI failed (rc={proc.returncode}): {proc.stderr}"
        )
    assert strip_meta(effect_outs[0].read_bytes()) == strip_meta(
        effect_outs[1].read_bytes()
    ), "two effect report runs must produce the same report once the metadata is gone"


# ---------------------------------------------------------------------------
# 4. 空计划目录：零值报告，退 0
# ---------------------------------------------------------------------------


def test_empty_plans_dir_zero_report(workspace):
    """空计划目录 → 零值报告，且退 0。

    「库里没有计划」是一个合法的答案，不是错误 —— 所以这里钉的是「退 0」，
    而不是「不抛错就算过」。
    """
    plans_dir = workspace / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    out = workspace / "scan.json"

    proc = _run_scan(plans_dir, out, cwd=workspace)

    assert proc.returncode == 0, (
        f"the scan CLI failed on an empty plans dir (rc={proc.returncode}): "
        f"{proc.stderr}"
    )
    assert out.exists(), f"the CLI did not write the report: {out}"

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["plans"] == []
    assert report["totals"] == {
        "plans": 0,
        "vp_total": 0,
        "deterministic_total": 0,
        "ratio": 0.0,
    }
