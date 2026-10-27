"""TDD spec — backend/verification/scan.py 全库扫描聚合内核。

Background
----------
A4 需要一份**同口径、可复跑**的全库扫描：给出「可确定性执行的 VP 数」与
它在全部 VP 里的占比，作为收益上界与验收基准。手工数做不到「同口径」——
每次数一遍都会换一个口径，所以判定必须写成一个纯聚合内核。

扫描内核的三条硬约束（写进实现，不是写进注释）：

1. **只读入参** ``plans_dir``。不读环境变量、不发网络请求、不调 LLM ——
   扫描是「统计」，不是「判定」，任何一次执行都会引入不确定性的动作都不许有。
2. **判定口径复用现有内核**：``api_test`` 用
   :func:`verification_api_runner.validate_vp`（request + assertions 齐全），
   ``full_ci`` 用 :func:`verification_ci_runner.validate_vp`（入口存在且没收窄）。
   不重新发明一套「看起来对」的判定 —— 扫描数出来的口径必须与真正跑起来
   的口径是同一套，否则这份报告没有验收价值。
3. **可复跑**：同一个 ``plans_dir`` 跑两次必须给出同一份报告。

TDD contract — 4 cases
-----------------------
1. ``test_totals_self_consistent`` — 每个 plan 与全局 totals 的计数自洽：
   ``deterministic_total == api_test + full_ci + e2e``，``by_method`` 之和
   等于 ``vp_total``，百分比与分母用的是同一个 ``vp_total``。
2. ``test_empty_dir_zero_report`` — 空目录 → 零值报告，**不抛错**。
3. ``test_unknown_method_counted_but_not_deterministic`` — 未知方法计入
   ``by_method``，**不**计入 ``deterministic``（它不是可确定性执行的方法）。
4. ``test_malformed_plan_skipped`` — 缺 ``id`` 的 VP 跳过，其余计数仍然自洽。

补充用例（同一份文件）：

* ``test_scan_matches_spec_example`` — 输出结构与任务书给的示例逐字段一致。
* ``test_unreadable_plan_file_skipped`` — 机器计划读不出来的计划目录被跳过
  （不谎报「这个计划有 0 条 VP」）。
* ``test_cli_writes_report_to_out_file`` — CLI 落到磁盘的报告与内核返回值
  完全一致（CLI 只是把内核的输出换个地方放）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification.scan import (  # noqa: E402
    DETERMINISTIC_METHODS,
    SCHEMA_VERSION,
    main,
    scan_plans,
)


# ---------------------------------------------------------------------------
# helpers
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

#: 一条 ``code_review`` VP：它是合法 VP，但不是可确定性执行的方法。
REVIEW_VP = {
    "id": "VP-003",
    "verification_method": "code_review",
}


def _write_plan(plans_dir: Path, plan_id: str, vps) -> Path:
    """Write a synthetic ``<plans_dir>/<plan_id>/verification_plan.json``."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    payload = {"verification_points": list(vps)}
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return plan_dir


def _plan(report: dict, plan_id: str) -> dict:
    """Return the per-plan block of ``report`` (fails loudly when absent)."""
    for entry in report["plans"]:
        if entry["plan_id"] == plan_id:
            return entry
    raise AssertionError(f"plan {plan_id!r} not in report: {report!r}")


# ---------------------------------------------------------------------------
# 1. totals self-consistent
# ---------------------------------------------------------------------------


def test_totals_self_consistent(tmp_path):
    """Every count in the report agrees with every other count."""
    plans_dir = tmp_path / "plans"
    _write_plan(plans_dir, "plan-a", [API_VP, CI_VP, REVIEW_VP])
    _write_plan(
        plans_dir,
        "plan-b",
        [
            dict(REVIEW_VP, id="VP-101"),
            {
                "id": "VP-102",
                "verification_method": "e2e",
                "target_url": "http://127.0.0.1:5173/checkout",
            },
        ],
    )

    report = scan_plans(plans_dir)

    for entry in report["plans"]:
        deterministic = entry["deterministic"]
        # The three deterministic methods are the whole denominator of the
        # "deterministic_total" claim — the sum must not drift from them.
        assert entry["deterministic_total"] == sum(deterministic.values())
        assert entry["deterministic_total"] == (
            deterministic["api_test"] + deterministic["full_ci"] + deterministic["e2e"]
        )
        # Every counted VP lands in exactly one by_method bucket.
        assert sum(entry["by_method"].values()) == entry["vp_total"]
        # The deterministic buckets are a subset of by_method.
        for method, count in deterministic.items():
            assert count <= entry["by_method"].get(method, 0)

    totals = report["totals"]
    assert totals["plans"] == len(report["plans"])
    assert totals["vp_total"] == sum(e["vp_total"] for e in report["plans"])
    assert totals["deterministic_total"] == sum(
        e["deterministic_total"] for e in report["plans"]
    )
    # The percentage uses the SAME denominator as the count it reports.
    expected_ratio = (
        round(totals["deterministic_total"] / totals["vp_total"], 3)
        if totals["vp_total"]
        else 0.0
    )
    assert totals["ratio"] == expected_ratio
    assert report["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 2. empty dir → zero report
# ---------------------------------------------------------------------------


def test_empty_dir_zero_report(tmp_path):
    """An empty plans dir yields a zero-valued report — it never raises."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()

    report = scan_plans(plans_dir)

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["plans"] == []
    assert report["totals"] == {
        "plans": 0,
        "vp_total": 0,
        "deterministic_total": 0,
        "ratio": 0.0,
    }


# ---------------------------------------------------------------------------
# 3. unknown method → counted, but not deterministic
# ---------------------------------------------------------------------------


def test_unknown_method_counted_but_not_deterministic(tmp_path):
    """A method outside the deterministic set shows up in by_method only."""
    plans_dir = tmp_path / "plans"
    _write_plan(
        plans_dir,
        "plan-a",
        [API_VP, dict(REVIEW_VP, id="VP-900", verification_method="quantum_flux")],
    )

    report = scan_plans(plans_dir)
    entry = _plan(report, "plan-a")

    assert entry["vp_total"] == 2
    assert entry["by_method"]["quantum_flux"] == 1
    assert entry["by_method"]["api_test"] == 1
    # The unknown method is NOT one of the deterministic buckets...
    assert "quantum_flux" not in entry["deterministic"]
    # ...and it does not inflate deterministic_total.
    assert entry["deterministic_total"] == 1
    assert entry["deterministic"]["api_test"] == 1
    assert entry["deterministic"]["full_ci"] == 0
    assert entry["deterministic"]["e2e"] == 0
    assert report["totals"]["deterministic_total"] == 1
    assert report["totals"]["ratio"] == 0.5


# ---------------------------------------------------------------------------
# 4. malformed plan → skipped, counts stay consistent
# ---------------------------------------------------------------------------


def test_malformed_plan_skipped(tmp_path):
    """A VP without an ``id`` is skipped; the rest still adds up."""
    plans_dir = tmp_path / "plans"
    _write_plan(
        plans_dir,
        "plan-a",
        [
            API_VP,
            {"verification_method": "api_test"},  # no ``id`` → skipped
            REVIEW_VP,
        ],
    )

    report = scan_plans(plans_dir)
    entry = _plan(report, "plan-a")

    assert entry["vp_total"] == 2
    assert entry["by_method"] == {"api_test": 1, "code_review": 1}
    assert entry["deterministic_total"] == 1
    assert entry["deterministic"] == {"api_test": 1, "full_ci": 0, "e2e": 0}
    assert report["totals"]["vp_total"] == 2
    assert report["totals"]["deterministic_total"] == 1
    assert report["totals"]["ratio"] == 0.5


# ---------------------------------------------------------------------------
# shape / spec-example
# ---------------------------------------------------------------------------


def test_scan_matches_spec_example(tmp_path):
    """The report shape is exactly the one the task brief spells out."""
    plans_dir = tmp_path / "plans"
    _write_plan(plans_dir, "plan-a", [API_VP, CI_VP, REVIEW_VP])

    report = scan_plans(plans_dir)

    assert report == {
        "schema_version": 1,
        "plans": [
            {
                "plan_id": "plan-a",
                "vp_total": 3,
                "by_method": {"api_test": 1, "full_ci": 1, "code_review": 1},
                "deterministic": {"api_test": 1, "full_ci": 1, "e2e": 0},
                "deterministic_total": 2,
            }
        ],
        "totals": {
            "plans": 1,
            "vp_total": 3,
            "deterministic_total": 2,
            "ratio": 0.667,
        },
    }


def test_unreadable_plan_file_skipped(tmp_path):
    """A plan whose machine plan cannot be parsed is skipped, not faked."""
    plans_dir = tmp_path / "plans"
    broken = plans_dir / "plan-broken"
    broken.mkdir(parents=True)
    (broken / "verification_plan.json").write_text("{not json", encoding="utf-8")
    _write_plan(plans_dir, "plan-ok", [API_VP])

    report = scan_plans(plans_dir)

    assert [e["plan_id"] for e in report["plans"]] == ["plan-ok"]
    assert report["totals"]["plans"] == 1
    assert report["totals"]["vp_total"] == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_writes_report_to_out_file(tmp_path):
    """The CLI writes exactly what the kernel returns."""
    plans_dir = tmp_path / "plans"
    _write_plan(plans_dir, "plan-a", [API_VP, CI_VP, REVIEW_VP])
    out_path = tmp_path / "reports" / "scan.json"

    rc = main(["--plans-dir", str(plans_dir), "--out", str(out_path)])

    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload == scan_plans(plans_dir)
    assert payload["totals"]["deterministic_total"] == 2


def test_deterministic_methods_are_the_documented_three():
    """The deterministic set is exactly the three methods the brief names."""
    assert tuple(DETERMINISTIC_METHODS) == ("api_test", "full_ci", "e2e")