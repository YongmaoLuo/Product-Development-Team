"""TDD spec — 确定性夹具：验证计划 / 效果基线样例 / 旧 round payload。

Background
----------
集成与 E2E 需要**固定、可复跑**的验证计划与复测样例。真实数据不能直接当夹具
用：真实的冻结基线里带着真实 plan id，真实的 round payload 里带着真实路径与
真实时间戳 —— 把它们提交进仓库等于把某一次运行的痕迹公开出去，而且换个
机器、换个部署它们就不再成立。

所以这里只放**合成夹具**：数值分布与真实一致，标识全部替换掉。

三组夹具
--------
``backend/tests/fixtures/verification/vp-fixture-001/verification_plan.json``
    **全确定性**计划：``api_test`` × 2 + ``full_ci`` × 1 + ``e2e`` × 1。四条
    都能被框架自己跑、自己判 —— 判定里没有任何一次模型往返。

``backend/tests/fixtures/verification/vp-fixture-002/verification_plan.json``
    **混合**计划：一条 ``code_review``（不是可确定性执行的方法）、一条主题外
    验证点（``related_prd_criteria`` 引用了不在 PRD 里的 ``AC-NOT-IN-PRD``）、
    一条没有任何验收标准支撑的孤儿验证点。

    这一组顺带钉住一件容易被忘掉的事：**「可确定性执行」不等于「在主题内」**。
    扫描内核只按执行方法分桶，不判主题 —— 主题相关性是另一道判定。所以这里
    的主题外验证点仍然是 deterministic 的，在测试里写死，免得有人以为扫描
    顺手把主题也判了。

``backend/tests/fixtures/effect_report/baseline-sample.json``
    效果基线样例：与真实基线同 schema、同数值分布，``plan_id`` 全部替换成
    ``sample-plan-NNN``。真实基线本身只落 gitignored 的 ``plans/_analysis/``，
    不入库。

``backend/tests/fixtures/failure_history/legacy_payload.json``
    **旧** round payload：形状与今天的 round payload 相同（``round`` +
    ``results`` + ``snapshot_at``），但**没有** ``failure_fingerprints`` ——
    消费方必须容忍这个键不存在，而不是读出一个 KeyError。

TDD contract — 3 cases
-----------------------
1. ``test_each_fixture_loadable`` —— 两个计划都能被 :func:`scan_plans` 扫到，
   也能被既有计划加载器 :func:`plan_defined_vp_ids` 读出它定义的验证点。
2. ``test_fixture_scannable_counts`` —— ``deterministic_total`` 与人工标注
   一致（下面那张表就是人工数一遍的结果）。
3. ``test_no_local_measurements`` —— 夹具文本对家目录路径、日期前缀的真实
   plan id、真实 provider 名零命中。

另有两个补充用例（实现之后补的覆盖，不是上面三条之一）：混合计划的三种情形
（``test_mixed_fixture_carries_the_three_documented_cases``）与真实基线的
位置（``test_real_baseline_location_is_gitignored``），外加基线样例可被
``effect_report`` 算出指标、旧 payload 没有指纹这两个用例。

边界条件
--------
* plan id / 路径不得含家目录、日期前缀真实 plan id、真实 provider 名。
* 基线样例数值分布与真实一致但 id 全替换；真实基线只落 gitignored
  ``plans/_analysis/``。
* 夹具不含时间戳型随机 id（避免 ``-n`` 分片收集不一致）—— 所以夹具里的
  标识全部是固定字面量，没有一个是从时间戳推出来的。

测试命令::

    pytest backend/tests/integration/verification_chain/test_fixture_plans_loadable.py -v
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

# ``backend/`` 进 sys.path，``import verification.*`` 才成立 —— 与仓库里
# 其它测试文件同一套做法，从哪个目录起 pytest 都一样。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from effect_report import compute_metrics, normalize_usage_report  # noqa: E402
from verification.failure_history import (  # noqa: E402
    merge_round,
    plan_defined_vp_ids,
)
from verification.scan import PLAN_FILENAME, scan_plans  # noqa: E402


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 夹具在哪里
# ---------------------------------------------------------------------------

#: 所有夹具的根目录 —— ``backend/tests/fixtures``。
FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "fixtures"

#: 验证计划夹具的根目录（两个计划目录的父目录）。任务书的输入/输出示例
#: 里的 ``FIXTURES`` 就是它：``FIXTURES/"vp-fixture-002"/…``。
FIXTURES = FIXTURES_ROOT / "verification"

#: 全确定性计划夹具：一个计划目录，里面是它的 ``verification_plan.json``。
VP_FIXTURE_001 = FIXTURES / "vp-fixture-001"

#: 混合计划夹具：``code_review`` + 主题外验证点 + 孤儿验证点。
VP_FIXTURE_002 = FIXTURES / "vp-fixture-002"

#: 效果基线样例（真实基线不入库，只落 gitignored 的 ``plans/_analysis/``）。
EFFECT_REPORT_BASELINE = FIXTURES_ROOT / "effect_report" / "baseline-sample.json"

#: 旧 round payload —— 没有 ``failure_fingerprints`` 的那一种。
LEGACY_ROUND_PAYLOAD = FIXTURES_ROOT / "failure_history" / "legacy_payload.json"

#: 本模块涉及的全部夹具文件。隐私检查一次扫完。
FIXTURE_FILES = (
    VP_FIXTURE_001 / PLAN_FILENAME,
    VP_FIXTURE_002 / PLAN_FILENAME,
    EFFECT_REPORT_BASELINE,
    LEGACY_ROUND_PAYLOAD,
)


# ---------------------------------------------------------------------------
# 人工标注的期望值
# ---------------------------------------------------------------------------

#: 人工数一遍的结果 —— 测试拿它跟扫描内核的输出对账。
#:
#: ``vp-fixture-001``：四条全是可确定性执行的方法。
#:
#: ``vp-fixture-002``：三条里两条是 ``api_test``（其中一条主题外 —— 主题
#: 不影响执行方法的分桶），一条是 ``code_review``（框架不执行它，所以不
#: 进 deterministic）。
EXPECTED_PLAN_COUNTS = {
    "vp-fixture-001": {
        "vp_total": 4,
        "by_method": {"api_test": 2, "full_ci": 1, "e2e": 1},
        "deterministic_total": 4,
    },
    "vp-fixture-002": {
        "vp_total": 3,
        "by_method": {"api_test": 2, "code_review": 1},
        "deterministic_total": 2,
    },
}


# ---------------------------------------------------------------------------
# 隐私规则
# ---------------------------------------------------------------------------

#: 家目录路径的形状。与 ``static_gates/test_no_local_home_path_in_first_party``
#: 的规则同源（Unix / macOS / Windows 盘符三种写法），这里自带一份是因为
#: 那个门禁的文件不许在模块作用域 import pytest，而本文件要 import。
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)/")

#: 不指向任何一台真实机器的占位段。跟门禁的豁免表同一份名单。
_PLACEHOLDER_SEGMENTS = frozenset({
    "a", "b", "x", "y", "u", "me", "user", "username", "you", "someone",
    "whoami", "name", "example", "your-name", "...",
})

#: 真实 plan id 的形状：日期前缀 + 名字（``20260926-Example-Plan``）。
#: 与 ``static_gates/test_no_plan_ids_in_non_test_source`` 的
#: :data:`PLAN_ID_SHAPE` 同源。
_PLAN_ID_SHAPE = re.compile(r"20[0-9]{6}-[A-Za-z][A-Za-z0-9._-]*")

#: 真实部署里会出现的 provider 名。夹具里一个都不该有：夹具一旦写死某个
#: provider 名，它就绑死了某台机器的 provider 集合，换个部署就名不副实。
_PROVIDER_NAMES = (
    "claude", "anthropic", "openai", "gemini", "deepseek", "minimax",
    "qwen", "kimi", "moonshot", "glm", "zhipu", "doubao", "ollama",
)

#: 标识里不许出现的形状：十位及以上的连续数字 —— 那是 unix 时间戳。
_TIMESTAMP_LIKE = re.compile(r"\d{10,}")


def _fixture_text(path: Path) -> str:
    """The fixture's text, read as UTF-8."""
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 每个夹具都能被读出来
# ---------------------------------------------------------------------------


def test_each_fixture_loadable():
    """Both plans are scannable and readable by the existing plan loader."""
    for plan_dir in (VP_FIXTURE_001, VP_FIXTURE_002):
        plan_file = plan_dir / PLAN_FILENAME
        assert plan_file.is_file(), f"missing fixture plan: {plan_file}"

        payload = json.loads(plan_file.read_text(encoding="utf-8"))
        assert isinstance(payload, dict), f"{plan_file} is not a JSON object"
        points = payload.get("verification_points")
        assert isinstance(points, list) and points, (
            f"{plan_file} defines no verification point"
        )

        # 既有计划加载器：读得出这个计划定义了哪些验证点。
        defined = plan_defined_vp_ids(plan_dir)
        assert defined is not None, (
            f"the plan loader could not read {plan_file}"
        )
        assert defined == {
            str(vp.get("id")).strip()
            for vp in points
            if isinstance(vp, dict) and str(vp.get("id") or "").strip()
        }

    # 扫描内核：两个计划目录都被扫到。
    scanned = [entry["plan_id"] for entry in scan_plans(FIXTURES)["plans"]]
    assert scanned == [VP_FIXTURE_001.name, VP_FIXTURE_002.name]


# ---------------------------------------------------------------------------
# 2. 计数与人工标注一致
# ---------------------------------------------------------------------------


def test_fixture_scannable_counts():
    """``deterministic_total`` agrees with the hand-written table above."""
    report = scan_plans(FIXTURES)
    entries = {entry["plan_id"]: entry for entry in report["plans"]}
    assert set(entries) == set(EXPECTED_PLAN_COUNTS)

    for plan_id, expected in EXPECTED_PLAN_COUNTS.items():
        entry = entries[plan_id]
        assert entry["vp_total"] == expected["vp_total"], plan_id
        assert entry["by_method"] == expected["by_method"], plan_id
        assert entry["deterministic_total"] == expected["deterministic_total"], plan_id
        # 分桶自洽：deterministic 是 by_method 的子集。
        for method, count in entry["deterministic"].items():
            assert count <= entry["by_method"].get(method, 0), plan_id

    # 任务书给的输出示例。
    assert report["plans"][0]["vp_total"] == 4

    totals = report["totals"]
    assert totals["vp_total"] == 7
    assert totals["deterministic_total"] == 6
    assert totals["ratio"] == round(6 / 7, 3)


# ---------------------------------------------------------------------------
# 3. 夹具里没有本机的度量
# ---------------------------------------------------------------------------


def test_no_local_measurements():
    """No home path, no real plan id, no provider name in any fixture."""
    for path in FIXTURE_FILES:
        assert path.is_file(), f"missing fixture: {path}"
        for lineno, line in enumerate(_fixture_text(path).splitlines(), start=1):
            for match in _HOME_PATH_RE.finditer(line):
                segment = match.group(1)
                assert segment in _PLACEHOLDER_SEGMENTS, (
                    f"{path}:{lineno}: a local home path names a machine"
                )
            assert not _PLAN_ID_SHAPE.search(line), (
                f"{path}:{lineno}: a date-prefixed plan id leaked into a fixture"
            )
            lowered = line.lower()
            for name in _PROVIDER_NAMES:
                assert not re.search(rf"\b{re.escape(name)}\b", lowered), (
                    f"{path}:{lineno}: a real provider name leaked into a fixture"
                )


# ---------------------------------------------------------------------------
# 4. 混合计划覆盖任务书点名的三种情形
# ---------------------------------------------------------------------------


def test_mixed_fixture_carries_the_three_documented_cases():
    """One code_review VP, one off-topic VP, one orphan VP."""
    payload = json.loads(
        (VP_FIXTURE_002 / PLAN_FILENAME).read_text(encoding="utf-8")
    )
    points = {
        vp["id"]: vp
        for vp in payload["verification_points"]
        if isinstance(vp, dict) and vp.get("id")
    }

    # 主题外：引用的验收标准不在本计划的 PRD 里。任务书给的就是这个例子。
    off_topic = points["VP-900"]
    assert off_topic["related_prd_criteria"] == ["AC-NOT-IN-PRD"]

    # code_review：框架不执行它，所以它不进 deterministic 桶。
    reviews = [
        vp for vp in points.values()
        if vp.get("verification_method") == "code_review"
    ]
    assert len(reviews) == 1, "the mixed plan must hold exactly one code_review VP"

    # 孤儿：没有任何验收标准支撑它 —— 裁决它等于对着一份不存在的标准打分。
    orphans = [vp for vp in points.values() if not vp.get("related_prd_criteria")]
    assert len(orphans) == 1, "the mixed plan must hold exactly one orphan VP"

    # VP-901 是 code_review 那条：它引用的验收标准在本计划的 PRD 里。
    assert points["VP-901"]["related_prd_criteria"] == ["AC-001"]


# ---------------------------------------------------------------------------
# 5. 真实基线的位置：只在 gitignored 的 plans/ 里
# ---------------------------------------------------------------------------


def test_real_baseline_location_is_gitignored():
    """The real baseline can only live in the ignored ``plans/`` tree."""
    gitignore_path = _REPO_ROOT / ".gitignore"
    assert gitignore_path.is_file(), (
        f"{gitignore_path} is missing — the real baseline's home would not "
        f"be protected by an ignore rule"
    )
    rules = {
        line.strip()
        for line in gitignore_path.read_text(encoding="utf-8").splitlines()
    }
    assert "/plans/" in rules, (
        "the real baseline lives in plans/_analysis/ — that tree must stay "
        "ignored, otherwise a real plan id can be committed by accident"
    )

    # 样例本身不在被忽略的目录里 —— 它是要入库的那一份。
    assert not EFFECT_REPORT_BASELINE.is_relative_to(_REPO_ROOT / "plans")
    assert EFFECT_REPORT_BASELINE.is_relative_to(FIXTURES_ROOT)


# ---------------------------------------------------------------------------
# 6. 效果基线样例
# ---------------------------------------------------------------------------


def test_effect_report_baseline_sample_is_loadable():
    """The baseline sample is a usable baseline: same shape, synthetic ids."""
    data = json.loads(EFFECT_REPORT_BASELINE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)

    # 与真实基线同一套顶层形状。
    for key in (
        "schema_version", "frozen_at", "period", "ledger",
        "metric_names", "escape_signals", "targets", "plans",
    ):
        assert key in data, f"baseline sample is missing {key!r}"

    plans = data["plans"]
    assert isinstance(plans, list) and plans

    # id 全替换：既不是日期前缀的真实 plan id，也不是任何真实标识。
    plan_ids = [str(plan.get("plan_id") or "") for plan in plans]
    for plan_id in plan_ids:
        assert re.fullmatch(r"sample-plan-\d{3}", plan_id), plan_id
        assert not _PLAN_ID_SHAPE.search(plan_id), plan_id
        assert not _TIMESTAMP_LIKE.search(plan_id), plan_id

    # 数值分布与真实一致：每个 plan 都带着完整的度量分块。
    for plan in plans:
        assert plan.get("size") in {"small", "medium", "large", "unknown"}
        assert plan.get("outcome") in {"passed", "failed", "partial", "blocked", "unknown"}
        assert isinstance(plan.get("task_count"), int)
        totals = plan.get("totals") or {}
        for key in ("requests", "total_tokens", "cost_usd"):
            assert key in totals, key
        for partition in ("by_scene", "by_vp", "by_round", "by_day"):
            assert isinstance(plan.get(partition), dict), partition

    # 指标能算出来 —— 走的是真实基线走的那条路。
    records = [
        normalize_usage_report(
            plan,
            plan_id=plan.get("plan_id"),
            completed_at=plan.get("completed_at"),
            size=plan.get("size"),
            outcome=plan.get("outcome"),
            task_count=plan.get("task_count"),
        )
        for plan in plans
    ]
    metrics = compute_metrics(records)
    assert metrics["pdt_plan_tokens"]["value"] > 0
    assert metrics["per_plan_tokens"]["value"] > 0


# ---------------------------------------------------------------------------
# 7. 旧 round payload
# ---------------------------------------------------------------------------


def test_legacy_round_payload_has_no_fingerprints():
    """The legacy payload parses, and carries no fingerprint at all."""
    payload = json.loads(LEGACY_ROUND_PAYLOAD.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)

    assert "failure_fingerprints" not in payload, (
        "a legacy payload must not carry fingerprints — the whole point is "
        "that the key is absent"
    )
    assert isinstance(payload.get("round"), int)
    results = payload.get("results")
    assert isinstance(results, list) and results

    for entry in results:
        assert isinstance(entry, dict)
        assert "failure_fingerprints" not in entry
        assert "failure_fingerprint" not in entry
        assert str(entry.get("id") or "").strip(), "a result must name its VP"
        assert not _TIMESTAMP_LIKE.search(str(entry.get("id")))

    # 旧 payload 仍然读得进历史：这一轮失败的验证点进历史，通过的不进。
    failed = [entry for entry in results if entry.get("status") == "FAILED"]
    assert failed, "the legacy payload must carry at least one failure"
    history = merge_round(
        {}, round_number=payload["round"], failures=failed,
    )
    assert set(history) == {str(entry["id"]) for entry in failed}
    for vp_id, entries in history.items():
        for entry in entries:
            assert entry["failure_class"], vp_id
            assert entry["failure_class_source"], vp_id
            # 没有指纹的记录就是没有指纹 —— 消费方只能退回按分类比较。
            assert "failure_fingerprint" not in entry, vp_id
