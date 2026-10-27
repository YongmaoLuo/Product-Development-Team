"""扫描报告落盘产物与聚合自洽性回归。

Background
----------
A4 的验收基准是**一份落盘的扫描报告**：数字同口径、可复跑、事后可核对。
没有落盘产物，验收基准就只存在于某个人的记忆里 —— 下一个人重新数一遍，
数出来的数不一样，却没人说得清是口径变了还是库变了。

这份产物有三个性质，缺一个它就当不了基准：

1. **同口径**：报告的每一个数字都来自
   :func:`verification.scan.scan_plans` —— 也就是真正跑验证时用的那套
   判定（``verification_api_runner`` / ``verification_ci_runner`` 的
   ``validate_vp``）。报告自己另写一套判定，两份数字会在同一个仓库上打架。
2. **可复跑**：同一份输入连跑两次，产物除**生成时间戳**外逐字节相同。
   差一个字节就说明报告里混进了随机量 —— 那份数字没法当基准。
3. **自洽**：``totals`` 里的每一个数都等于逐 plan 累加的结果。这条不是
   洁癖：一份自己跟自己都对不上的报告，没人会拿它去对账。

为什么产物落在 gitignored 的 ``plans/_analysis/`` 下
----------------------------------------------------
报告里带着**真实的 plan id**。真实 plan id 进了被跟踪的文件，就等于把某
一次运行的痕迹公开出去，而且换个机器、换个部署它就不再成立。所以产物只
作**本地分析产物**：它落在 ``plans/_analysis/``（被 ``/plans/`` 忽略），
不入库。入库的那一份是合成夹具
（``backend/tests/fixtures/verification/``），见任务 7。

报告装配器为什么住在这个文件里
------------------------------
任务 18 声明的落笔范围只有三份文件：本测试文件，加上那两份产物。扫描
内核（``backend/verification/scan.py``）是任务 4 的产物，本任务**只消费**
它、不改它 —— 所以「补上生成时间戳、补上三个方法的分桶数、把同一份报告
渲染成 Markdown」这三件事由本模块承担。

这不是把生产逻辑塞进测试的权宜之计，而是这份产物的性质决定的：它是
**一次分析**的产物，不是运行时状态。架构设计里写得很清楚 ——「扫描报告
与复测基线属于分析产物，不是运行时状态」。分析产物的装配器与它的自洽性
回归放在同一处，改一处就必然改另一处：报告多一个字段，对账的断言就得
跟着多一条。

用法
----
::

    # 落盘（计划目录给真实语料，产物落 gitignored 的 plans/_analysis/）
    python -c "import sys; sys.path[:0] = ['backend', 'backend/tests/integration/verification']; \\
                import test_scan_report_artifact as m; \\
                m.write_artifacts('plans', 'plans/_analysis/vp-scannable-2026-10-26.json')"

TDD contract — 4 cases
-----------------------
1. ``test_report_schema_and_totals`` —— 字段齐全，且
   ``deterministic_total == api_test + full_ci + e2e``。
2. ``test_report_matches_plan_sum`` —— ``totals`` 等于逐 plan 累加。
3. ``test_rerun_identical`` —— 两次扫描除生成时间戳外完全一致。
4. ``test_artifact_path_under_plans_analysis`` —— 产物路径正确。

另有两个补充用例，覆盖任务书点名的两条边界条件：扫描**不修改任何计划
文件**（``test_scan_does_not_touch_plan_files``），以及报告**只作本地
分析产物**（``test_report_artifact_is_not_tracked``）。第三个补充用例把
文档里那条扫描命令真的跑一遍（``test_cli_scan_agrees_with_the_report``）
——「可复跑」说的是命令行，不只是函数。

边界条件
--------
* 连跑两次除生成时间戳外结果完全一致。
* ``plans/`` 为 gitignored，报告只作本地分析产物，不得进入已跟踪文件。
* 报告可读性：``totals`` 与逐 plan 累加必须自洽。
* 扫描不修改任何计划文件。

测试命令::

    cd backend && .venv/bin/python3 -m pytest \
        tests/integration/verification_chain/test_scan_report_artifact.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

# ``backend/`` 进 sys.path，``import verification.*`` 才成立 —— 与仓库里
# 其它测试文件同一套做法，从哪个目录起 pytest 都一样。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification.scan import (  # noqa: E402
    DETERMINISTIC_METHODS,
    SCHEMA_VERSION,
    scan_plans,
)


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 夹具与产物路径
# ---------------------------------------------------------------------------

#: 验证计划夹具的根目录 —— 任务 7 建的合成夹具。报告拿它当输入：不带真实
#: plan id，任何机器上跑出来的数字都一样。
FIXTURES = _BACKEND_DIR / "tests" / "fixtures" / "verification"

#: 产物落盘目录的形状：``<仓库根>/plans/_analysis/``。
REPORT_SUBDIR = ("plans", "_analysis")

#: 产物的文件名（不带扩展名）。与架构设计里写的
#: ``plans/_analysis/vp-scannable-<date>.json`` 同名规则。
REPORT_STEM = "vp-scannable-2026-10-26"

#: 产物文件名。``.json`` 与同名 ``.md`` 一对。
REPORT_JSON_NAME = f"{REPORT_STEM}.json"
REPORT_MD_NAME = f"{REPORT_STEM}.md"

#: 报告里放生成时间戳的字段名。
GENERATED_AT_FIELD = "generated_at"

#: 人工数一遍夹具的结果 —— 测试拿它跟报告对账。
#:
#: ``vp-fixture-001``：api_test × 2 + full_ci × 1 + e2e × 1 = 4 条全可确定性执行。
#: ``vp-fixture-002``：api_test × 2（其中一条主题外，主题不影响执行方法分桶）
#: + code_review × 1（框架不执行）= 3 条，其中 2 条可确定性执行。
EXPECTED_TOTALS = {
    "plans": 2,
    "vp_total": 7,
    "api_test": 4,
    "full_ci": 1,
    "e2e": 1,
    "deterministic_total": 6,
    "ratio": round(6 / 7, 3),
}

#: 固定的时间戳。测试里凡是跟时间有关的，都用这个字面量，不读时钟 ——
#: 一个从墙上时钟推出来的参数，会让 ``-n`` 分片的用例收集结果不稳定。
FIXED_GENERATED_AT = "2026-10-26T00:00:00+00:00"


# ---------------------------------------------------------------------------
# 报告装配器
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    """现在这一刻（UTC，ISO-8601）。"""
    return datetime.now(timezone.utc).isoformat()


def build_report(plans_dir: Any, generated_at: Optional[str] = None) -> Dict[str, Any]:
    """把扫描内核的输出变成落盘报告。

    :func:`verification.scan.scan_plans` 是纯聚合内核：同一份输入，两次
    运行逐字节相同，也不读时钟。落盘的那一份报告还需要一样东西 ——
    **它是哪一刻生成的**。所以这里只加这一个字段，别的什么都不动。

    ``totals`` 里补上三个确定性方法的分桶数（``api_test`` / ``full_ci`` /
    ``e2e``），这样「确定性执行的总数」可以被当场验算：三个方法之和必须
    等于 ``deterministic_total``。没有这三个数，报告里的总数是一个没人
    能当场对账的孤零零的数字。

    三个分桶数由逐 plan 的 ``deterministic`` 累加而来 —— 与 ``totals``
    的其余数字同源，所以「``totals`` 等于逐 plan 累加」这条自洽性是
    构造出来的，不是事后补的。
    """
    scan = scan_plans(plans_dir)
    totals = scan["totals"]

    per_method = {method: 0 for method in DETERMINISTIC_METHODS}
    for entry in scan["plans"]:
        for method in DETERMINISTIC_METHODS:
            per_method[method] += int(entry["deterministic"].get(method, 0))

    return {
        "schema_version": scan["schema_version"],
        GENERATED_AT_FIELD: generated_at or _utc_now(),
        "plans": scan["plans"],
        "totals": {
            "plans": totals["plans"],
            "vp_total": totals["vp_total"],
            "api_test": per_method["api_test"],
            "full_ci": per_method["full_ci"],
            "e2e": per_method["e2e"],
            "deterministic_total": totals["deterministic_total"],
            "ratio": totals["ratio"],
        },
    }


def render_markdown(report: Dict[str, Any], source_dir: Optional[str] = None) -> str:
    """报告的 Markdown 版 —— 给人看的那一份。"""
    totals = report["totals"]
    lines = [
        "# 全库验证点可确定性执行扫描报告",
        "",
        f"- 生成时间：{report[GENERATED_AT_FIELD]}",
        f"- 报告 schema 版本：{report['schema_version']}",
    ]
    if source_dir:
        lines.append(f"- 计划目录：`{source_dir}`")
    lines += [
        "",
        "## 汇总",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 计划数 | {totals['plans']} |",
        f"| 验证点总数 | {totals['vp_total']} |",
        f"| 可确定性执行 | {totals['deterministic_total']} |",
        f"| 占比 | {totals['ratio'] * 100:.1f}% |",
        f"| api_test | {totals['api_test']} |",
        f"| full_ci | {totals['full_ci']} |",
        f"| e2e | {totals['e2e']} |",
        "",
        "## 逐计划",
        "",
        "| 计划 | 验证点 | 可确定性执行 | api_test | full_ci | e2e |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in report["plans"]:
        deterministic = entry["deterministic"]
        lines.append(
            f"| {entry['plan_id']} | {entry['vp_total']} | "
            f"{entry['deterministic_total']} | {deterministic['api_test']} | "
            f"{deterministic['full_ci']} | {deterministic['e2e']} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_artifacts(
    plans_dir: Any,
    out_json: Any,
    generated_at: Optional[str] = None,
) -> Tuple[Path, Path]:
    """把 JSON 报告与同名 Markdown 一起落盘，返回两个路径。

    ``out_json`` 是 JSON 那一份的路径；Markdown 落在**同一个目录、同一个
    文件名主干**下，只是扩展名不同 —— 架构设计要求的「同名 ``.md``」。
    """
    report = build_report(plans_dir, generated_at=generated_at)
    json_path = Path(out_json)
    md_path = json_path.with_suffix(".md")
    for path in (json_path, md_path):
        parent = path.parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(
        render_markdown(report, source_dir=str(plans_dir)), encoding="utf-8"
    )
    return json_path, md_path


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _without_timestamp(text: str, generated_at: str) -> str:
    """Replace the generation timestamp with a placeholder."""
    return text.replace(generated_at, "<GENERATED_AT>")


def _tree_digest(root: Path) -> dict:
    """Map every file under ``root`` to its mtime, size and content digest."""
    digest = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        digest[str(path.relative_to(root))] = (
            stat.st_mtime_ns,
            stat.st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return digest


def _assert_self_consistent(report: Dict[str, Any]) -> None:
    """The invariants every report must hold, whatever corpus it describes."""
    totals = report["totals"]
    plans = report["plans"]

    assert set(totals) == {
        "plans",
        "vp_total",
        "api_test",
        "full_ci",
        "e2e",
        "deterministic_total",
        "ratio",
    }, f"unexpected totals fields: {sorted(totals)}"

    assert totals["deterministic_total"] == (
        totals["api_test"] + totals["full_ci"] + totals["e2e"]
    )

    assert totals["plans"] == len(plans)
    assert totals["vp_total"] == sum(entry["vp_total"] for entry in plans)
    assert totals["deterministic_total"] == sum(
        entry["deterministic_total"] for entry in plans
    )
    for method in DETERMINISTIC_METHODS:
        assert totals[method] == sum(
            entry["deterministic"][method] for entry in plans
        ), method

    expected_ratio = (
        round(totals["deterministic_total"] / totals["vp_total"], 3)
        if totals["vp_total"]
        else 0.0
    )
    assert totals["ratio"] == expected_ratio

    for entry in plans:
        assert set(entry["deterministic"]) == set(DETERMINISTIC_METHODS)
        assert entry["deterministic_total"] == sum(entry["deterministic"].values())
        assert sum(entry["by_method"].values()) == entry["vp_total"]
        for method, count in entry["deterministic"].items():
            assert count <= entry["by_method"].get(method, 0), entry["plan_id"]


# ---------------------------------------------------------------------------
# 1. 字段齐全，且 deterministic_total 与三个方法分桶对得上
# ---------------------------------------------------------------------------


def test_report_schema_and_totals(tmp_path):
    """Every field is present, and the deterministic total adds up."""
    report = build_report(FIXTURES, generated_at=FIXED_GENERATED_AT)

    # 顶层字段：schema 版本、生成时间戳、逐 plan 聚合、totals。
    assert report["schema_version"] == SCHEMA_VERSION
    assert report[GENERATED_AT_FIELD] == FIXED_GENERATED_AT
    assert isinstance(report["plans"], list) and report["plans"]
    assert isinstance(report["totals"], dict)

    # 字段齐全，且三个方法之和就是确定性执行的总数。
    _assert_self_consistent(report)

    # 与人工数一遍的夹具结果一致。
    assert report["totals"] == EXPECTED_TOTALS


# ---------------------------------------------------------------------------
# 2. totals 等于逐 plan 累加
# ---------------------------------------------------------------------------


def test_report_matches_plan_sum(tmp_path):
    """The global totals are the sum of the per-plan numbers."""
    report = build_report(FIXTURES, generated_at=FIXED_GENERATED_AT)
    plans = report["plans"]
    totals = report["totals"]

    assert totals["plans"] == len(plans)
    assert totals["vp_total"] == sum(entry["vp_total"] for entry in plans)
    assert totals["deterministic_total"] == sum(
        entry["deterministic_total"] for entry in plans
    )
    for method in DETERMINISTIC_METHODS:
        assert totals[method] == sum(
            entry["deterministic"][method] for entry in plans
        ), method

    # 占比与它自己的分母是同一对数。
    assert totals["ratio"] == round(
        totals["deterministic_total"] / totals["vp_total"], 3
    )


# ---------------------------------------------------------------------------
# 3. 两次扫描除生成时间戳外完全一致
# ---------------------------------------------------------------------------


def test_rerun_identical(tmp_path):
    """Two runs differ in the timestamp and in nothing else."""
    first_json, first_md = write_artifacts(
        FIXTURES,
        tmp_path / "run-a" / REPORT_SUBDIR[0] / REPORT_SUBDIR[1] / REPORT_JSON_NAME,
    )
    second_json, second_md = write_artifacts(
        FIXTURES,
        tmp_path / "run-b" / REPORT_SUBDIR[0] / REPORT_SUBDIR[1] / REPORT_JSON_NAME,
    )

    payload_a = json.loads(first_json.read_text(encoding="utf-8"))
    payload_b = json.loads(second_json.read_text(encoding="utf-8"))

    # 时间戳是两次运行之间**唯一**允许不同的东西。
    assert payload_a[GENERATED_AT_FIELD]
    assert payload_b[GENERATED_AT_FIELD]
    without_a = {k: v for k, v in payload_a.items() if k != GENERATED_AT_FIELD}
    without_b = {k: v for k, v in payload_b.items() if k != GENERATED_AT_FIELD}
    assert without_a == without_b

    # 逐字节：把时间戳换成占位符之后，两份产物完全一样。
    assert _without_timestamp(
        first_json.read_text(encoding="utf-8"), payload_a[GENERATED_AT_FIELD]
    ) == _without_timestamp(
        second_json.read_text(encoding="utf-8"), payload_b[GENERATED_AT_FIELD]
    )

    # 同一份输入、两个**不同**的时间戳：报告只差这一个字段。这条把
    # 「时间戳是唯一变量」钉死，不依赖两次运行落在哪一微秒。
    explicit_a = build_report(FIXTURES, generated_at="2026-10-26T00:00:00+00:00")
    explicit_b = build_report(FIXTURES, generated_at="2026-10-27T00:00:00+00:00")
    assert explicit_a[GENERATED_AT_FIELD] != explicit_b[GENERATED_AT_FIELD]
    assert {
        k: v for k, v in explicit_a.items() if k != GENERATED_AT_FIELD
    } == {k: v for k, v in explicit_b.items() if k != GENERATED_AT_FIELD}

    # Markdown 产物同样：只有时间戳会变。
    assert _without_timestamp(
        first_md.read_text(encoding="utf-8"), payload_a[GENERATED_AT_FIELD]
    ) == _without_timestamp(
        second_md.read_text(encoding="utf-8"), payload_b[GENERATED_AT_FIELD]
    )


# ---------------------------------------------------------------------------
# 4. 产物路径正确
# ---------------------------------------------------------------------------


def test_artifact_path_under_plans_analysis(tmp_path):
    """The artefact lands in ``plans/_analysis/`` — JSON and Markdown alike."""
    analysis = tmp_path.joinpath(*REPORT_SUBDIR)
    out_path = analysis / REPORT_JSON_NAME

    json_path, md_path = write_artifacts(FIXTURES, out_path)

    assert json_path == out_path
    assert json_path.is_file()
    assert md_path.is_file(), f"the Markdown twin {md_path} was not written"

    # 路径形状：``plans/_analysis/<stem>.json`` 与 ``plans/_analysis/<stem>.md``。
    assert out_path.relative_to(tmp_path).parts == (
        *REPORT_SUBDIR,
        REPORT_JSON_NAME,
    )
    assert md_path.relative_to(tmp_path).parts == (
        *REPORT_SUBDIR,
        REPORT_MD_NAME,
    )
    # 同名：同一个主干，两个扩展名。
    assert out_path.stem == md_path.stem == REPORT_STEM

    # Markdown 产物说的就是这份 JSON 里的数。
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert render_markdown(payload, source_dir=str(FIXTURES)) == md_path.read_text(
        encoding="utf-8"
    )
    assert str(payload["totals"]["vp_total"]) in md_path.read_text(encoding="utf-8")
    assert payload[GENERATED_AT_FIELD] in md_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 5. 扫描不修改任何计划文件
# ---------------------------------------------------------------------------


def test_scan_does_not_touch_plan_files(tmp_path):
    """Scanning is read-only: the plan files come out byte-identical."""
    before = _tree_digest(FIXTURES)

    build_report(FIXTURES, generated_at=FIXED_GENERATED_AT)
    scan_plans(FIXTURES)
    write_artifacts(FIXTURES, tmp_path.joinpath(*REPORT_SUBDIR) / REPORT_JSON_NAME)

    assert _tree_digest(FIXTURES) == before, (
        "the scan must not touch the plan files it reads"
    )


# ---------------------------------------------------------------------------
# 6. 报告只作本地分析产物，不进被跟踪的文件
# ---------------------------------------------------------------------------


def test_report_artifact_is_not_tracked():
    """``plans/`` is ignored, so a real plan id cannot be committed by accident."""
    gitignore = _REPO_ROOT / ".gitignore"
    assert gitignore.is_file(), f"{gitignore} is missing"

    rules = {
        line.strip()
        for line in gitignore.read_text(encoding="utf-8").splitlines()
    }
    assert "/plans/" in rules, (
        "the scan report lives in plans/_analysis/ — that tree must stay "
        "ignored, otherwise a real plan id can be committed by accident"
    )

    if shutil.which("git") is None:  # pragma: no cover - git is always there
        pytest.skip("git is not available")

    # git 自己也得认为产物路径是被忽略的。
    proc = subprocess.run(
        [
            "git",
            "check-ignore",
            "-q",
            "--no-index",
            "/".join((*REPORT_SUBDIR, REPORT_JSON_NAME)),
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
    )
    assert proc.returncode == 0, (
        "git does not ignore the scan report path — the report would be "
        "picked up by `git add .`"
    )

    # 而且此刻它确实没有被跟踪。
    tracked = subprocess.run(
        ["git", "ls-files", "--", "plans/"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert tracked.stdout.strip() == "", (
        f"files under plans/ are tracked: {tracked.stdout!r}"
    )


# ---------------------------------------------------------------------------
# 7. 文档里那条扫描命令与报告说的是同一批数
# ---------------------------------------------------------------------------


def test_cli_scan_agrees_with_the_report(tmp_path):
    """``python -m verification.scan`` and the report count the same VPs."""
    out_path = tmp_path / "scan.json"

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_BACKEND_DIR), str(_REPO_ROOT), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "verification.scan",
            "--plans-dir",
            str(FIXTURES),
            "--out",
            str(out_path),
        ],
        cwd=str(_BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert out_path.is_file()

    cli_scan = json.loads(out_path.read_text(encoding="utf-8"))
    report = build_report(FIXTURES, generated_at=FIXED_GENERATED_AT)

    # 同一套口径：逐 plan 的聚合与总数必须一致。差别只有报告多出来的那
    # 三个方法分桶数与生成时间戳 —— 它们是从同一批 per-plan 数据里
    # 累加出来的，不是另算的。
    assert cli_scan["plans"] == report["plans"]
    assert cli_scan["totals"]["plans"] == report["totals"]["plans"]
    assert cli_scan["totals"]["vp_total"] == report["totals"]["vp_total"]
    assert (
        cli_scan["totals"]["deterministic_total"]
        == report["totals"]["deterministic_total"]
    )
    assert report["totals"] == EXPECTED_TOTALS


# ---------------------------------------------------------------------------
# 8. 已经落盘的那一份产物
# ---------------------------------------------------------------------------


def test_landed_artifact_is_self_consistent():
    """The artefact on disk is self-consistent, whatever corpus it describes."""
    path = _REPO_ROOT.joinpath(*REPORT_SUBDIR) / REPORT_JSON_NAME
    if not path.is_file():
        pytest.skip("the scan report artefact has not been produced in this checkout")

    landed = json.loads(path.read_text(encoding="utf-8"))
    _assert_self_consistent(landed)

    # Markdown 那一份也在，且说的就是这份 JSON 里的数。
    md_path = path.with_suffix(".md")
    assert md_path.is_file(), f"the Markdown twin {md_path} is missing"
    markdown = md_path.read_text(encoding="utf-8")
    assert str(landed["totals"]["vp_total"]) in markdown
    assert landed[GENERATED_AT_FIELD] in markdown


def test_landed_artifact_matches_a_fresh_scan():
    """The landed artefact is what the same corpus produces right now."""
    path = _REPO_ROOT.joinpath(*REPORT_SUBDIR) / REPORT_JSON_NAME
    if not path.is_file():
        pytest.skip("the scan report artefact has not been produced in this checkout")

    corpus = _REPO_ROOT / "plans"
    if not any(corpus.glob("*/verification_plan.json")):
        pytest.skip("the corpus this checkout would scan is not present here")

    landed = json.loads(path.read_text(encoding="utf-8"))
    fresh = build_report(corpus)
    assert {k: v for k, v in landed.items() if k != GENERATED_AT_FIELD} == {
        k: v for k, v in fresh.items() if k != GENERATED_AT_FIELD
    }, (
        "the landed report no longer matches the corpus on disk — the corpus "
        "moved, so the report has to be regenerated"
    )