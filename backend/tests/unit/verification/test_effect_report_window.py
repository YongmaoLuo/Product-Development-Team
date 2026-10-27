"""TDD spec — backend/effect_report.py 效果报表：窗口规则与退出码语义。

Background
----------
收敛优化合入之后，效果指标必须被测量，但**不能**当合入门禁：指标没达标
只在报表里留下判定，绝不让流水线变红。所以本文件钉住两件事：

1. **窗口规则** —— 合入后累计 7 天、或跑满 2 个中型 plan，先到者为准。
   两组 fixture 各命中一次（``days`` 一组、``plans`` 一组）。
2. **退出码语义** —— 指标未达标 → 报表给出判定，退出码仍然是 0。

窗口为什么由数据决定、而不是由墙上时钟决定
--------------------------------------------
窗口关闭的时刻必须能从**冻结产物**里重算出来，否则同一份基线跑两次会
得到两个不同的报表，报表就不可复跑了。所以本模块的窗口规则只读两样东西：
合入时刻（``since``）和窗口内观测到的 plan 集合。

* 天数规则：窗口内存在一个 completed_at 落在「合入 + 7 天」当天或之后的
  plan —— 这��� plan 就是「7 天已经过去」的证据；窗口在合入 + 7 天那一刻
  关闭。
* plan 规则：跑满 2 个中型 plan，窗口在第 2 个中型 plan 完成的那一刻关闭。
* 两个条件都成立时，取**先到者**的那个时刻。

TDD contract — 4 cases
-----------------------
1. ``test_window_seven_days`` — 时间窗命中 → 窗口类型 ``days``。
2. ``test_window_two_medium_plans`` — 2 个中型 plan 命中 → 窗口类型 ``plans``。
3. ``test_window_takes_whichever_comes_first`` — 两个条件都成立时取先到的。
4. ``test_not_blocking_exit_code_zero`` — 指标未达标仍退 0。

边界条件（同一文件覆盖）：

* 两个条件都不成立 → 窗口类型 ``open``（还没到复测点，不是错误）。
* 窗口内的 plan 以 completed_at 落在 ``[合入时刻, 关闭时刻]`` 为准；
  关闭时刻之后的 plan 不进窗口（它只是「7 天到了」的证据）。
* 没有 plan 时报表照常生成：所有指标判定为 ``no_data``，退出码仍是 0。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import effect_report  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

#: 合入时刻。窗口从这一刻开始算。
SINCE = "2026-10-01"


def _plan(plan_id: str, size: str, completed_at: str) -> dict:
    """One observed plan, as the window resolver sees it."""
    return {"plan_id": plan_id, "size": size, "completed_at": completed_at}


def _usage_report(
    plan_id: str,
    *,
    total_tokens: int,
    scene: str = "execution",
    rounds: dict | None = None,
) -> dict:
    """A saved per-plan usage report, in the shape ``plan_usage`` writes."""
    groups = rounds or {"1": {"requests": 2, "total_tokens": total_tokens, "cost_usd": 1.0}}
    return {
        "plan_id": plan_id,
        "generated_at": "2026-10-05T00:00:00+00:00",
        "totals": {
            "requests": 2,
            "total_tokens": total_tokens,
            "cost_usd": 1.0,
        },
        "by_scene": [
            {
                "key": scene,
                "requests": 2,
                "total_tokens": total_tokens,
                "cost_usd": 1.0,
            }
        ],
        "by_vp": {
            "(unattributed)": {
                "key": "(unattributed)",
                "requests": 2,
                "total_tokens": total_tokens,
                "cost_usd": 1.0,
            }
        },
        "by_round": {
            key: dict(value, key=key) for key, value in groups.items()
        },
    }


def _write_plan(plans_dir: Path, plan_id: str, *, tokens: int, tasks: int = 5) -> None:
    """Materialise one observed plan directory under ``plans_dir``."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "usage_report.json").write_text(
        json.dumps(_usage_report(plan_id, total_tokens=tokens), ensure_ascii=False),
        encoding="utf-8",
    )
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": str(i)} for i in range(tasks)]}),
        encoding="utf-8",
    )
    (plan_dir / "verification_report.json").write_text(
        json.dumps({"plan_id": plan_id, "overall_status": "PASSED"}),
        encoding="utf-8",
    )


def _write_baseline(path: Path, *, tokens: int, plans: int = 1) -> None:
    """A frozen baseline: a ledger slice plus one normalised plan."""
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "frozen_at": "2026-10-05T00:00:00+00:00",
                "period": {"from": "2026-09-25", "to": "2026-10-01"},
                "ledger": {
                    "2026-09-25": {"requests": 1, "total_tokens": tokens, "cost_usd": 1.0}
                },
                "metric_names": list(effect_report.METRIC_NAMES),
                "escape_signals": list(effect_report.ESCAPE_SIGNALS),
                "targets": {
                    "pdt_plan_tokens": {"direction": "down", "baseline": tokens},
                    "per_plan_tokens": {"direction": "down", "baseline": tokens},
                    "repair_ratio": {"direction": "down", "baseline": 0.0},
                    "exec_vs_verify": {"direction": "band", "low": 0.2, "high": 2.0},
                    "round_median": {"direction": "down", "baseline": 1.0},
                    "round_p90": {"direction": "down", "baseline": 1.0},
                    "unattributed_ratio": {"direction": "down", "baseline": 0.0},
                },
                "plans": [
                    {
                        "plan_id": f"baseline-plan-{i}",
                        "size": "medium",
                        "task_count": 5,
                        "completed_at": "2026-09-30T00:00:00+00:00",
                        "outcome": "passed",
                        "totals": {
                            "requests": 2,
                            "total_tokens": tokens,
                            "cost_usd": 1.0,
                        },
                        "by_scene": {"execution": {"total_tokens": tokens}},
                        "by_vp": {"(unattributed)": {"total_tokens": tokens}},
                        "by_round": {"1": {"total_tokens": tokens}},
                    }
                    for i in range(plans)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. seven days — the days rule closes the window
# ---------------------------------------------------------------------------


def test_window_seven_days():
    """时间窗命中 → 窗口类型 days。

    Fixture：合入后第 2 天跑完一个中型 plan，第 5 天跑完一个小 plan，
    第 9 天有一个 plan 活动 —— 越过「合入 + 7 天」这条线，所以天数规则
    成立。窗口在合入 + 7 天那一刻关闭，只有落在窗口里的两个 plan 算数。
    """
    plans = [
        _plan("p-medium", "medium", "2026-10-02T00:00:00+00:00"),
        _plan("p-small", "small", "2026-10-05T00:00:00+00:00"),
        _plan("p-witness", "small", "2026-10-09T00:00:00+00:00"),
    ]

    window = effect_report.resolve_window(SINCE, plans)

    assert window["type"] == "days", (
        f"expected the days rule to close the window, got {window['type']!r}"
    )
    assert window["closed_at"].startswith("2026-10-08"), (
        f"the days rule closes at merge + 7 days, got {window['closed_at']!r}"
    )
    assert [p["plan_id"] for p in window["plans"]] == ["p-medium", "p-small"], (
        "only the plans that completed inside the window belong to it; the "
        f"witness plan is outside: {[p['plan_id'] for p in window['plans']]}"
    )


# ---------------------------------------------------------------------------
# 2. two medium plans — the plans rule closes the window
# ---------------------------------------------------------------------------


def test_window_two_medium_plans():
    """2 个中型 plan 命中 → 窗口类型 plans。

    Fixture：合入后第 2 天、第 5 天各跑完一个中型 plan，第 7 天还有一个
    小 plan。没有 plan 越过「合入 + 7 天」，所以只有 plan 规则成立，窗口
    在第 2 个中型 plan 完成的那一刻关闭。
    """
    plans = [
        _plan("p-medium-a", "medium", "2026-10-02T00:00:00+00:00"),
        _plan("p-medium-b", "medium", "2026-10-05T00:00:00+00:00"),
        _plan("p-small", "small", "2026-10-07T00:00:00+00:00"),
    ]

    window = effect_report.resolve_window(SINCE, plans)

    assert window["type"] == "plans", (
        f"expected the plans rule to close the window, got {window['type']!r}"
    )
    assert window["closed_at"].startswith("2026-10-05"), (
        "the plans rule closes when the second medium plan completes, got "
        f"{window['closed_at']!r}"
    )
    assert [p["plan_id"] for p in window["plans"]] == ["p-medium-a", "p-medium-b"], (
        "the window ends at the closing plan, so the later small plan is out: "
        f"{[p['plan_id'] for p in window['plans']]}"
    )


# ---------------------------------------------------------------------------
# 3. whichever comes first
# ---------------------------------------------------------------------------


def test_window_takes_whichever_comes_first():
    """两个条件都成立时取先到的那个 —— 先到者为准。

    同一组 plan 换一下时间：把第 2 个中型 plan 拖到第 9 天，天数规则就
    先落地，窗口类型从 ``plans`` 变成 ``days``。
    """
    late = [
        _plan("p-medium-a", "medium", "2026-10-02T00:00:00+00:00"),
        _plan("p-medium-b", "medium", "2026-10-09T00:00:00+00:00"),
        _plan("p-witness", "small", "2026-10-09T00:00:00+00:00"),
    ]

    window = effect_report.resolve_window(SINCE, late)

    assert window["type"] == "days", (
        "the 7-day mark is reached before the second medium plan finishes, "
        f"so the days rule wins; got {window['type']!r}"
    )
    assert window["closed_at"].startswith("2026-10-08"), (
        f"expected the days mark, got {window['closed_at']!r}"
    )


def test_window_ignores_plans_that_finished_before_the_merge():
    """合入之前跑完的 plan 不算「跑满 2 个中型 plan」。

    中型 plan 的计数只看窗口**内**完成的：合入之前结束的 plan 对「这次
    改动表现得怎么样」没有任何说明力，拿它凑数会让窗口在合入之前就
    关闭 —— 关闭时刻甚至会早于窗口开始。
    """
    plans = [
        _plan("p-before-a", "medium", "2026-09-20T00:00:00+00:00"),
        _plan("p-before-b", "medium", "2026-09-25T00:00:00+00:00"),
        _plan("p-after", "small", "2026-10-03T00:00:00+00:00"),
    ]

    window = effect_report.resolve_window(SINCE, plans)

    assert window["type"] == "open", (
        "two medium plans finished before the merge do not close the "
        f"window; got {window['type']!r} closed at {window['closed_at']!r}"
    )
    assert window["closed_at"] is None


def test_window_open_when_no_rule_is_met():
    """两个条件都不成立 → 窗口类型 open（还没到复测点，不是错误）。"""
    plans = [_plan("p-small", "small", "2026-10-03T00:00:00+00:00")]

    window = effect_report.resolve_window(SINCE, plans)

    assert window["type"] == "open", (
        f"expected an open window, got {window['type']!r}"
    )
    assert window["closed_at"] is None


# ---------------------------------------------------------------------------
# 4. metrics never gate
# ---------------------------------------------------------------------------


def test_not_blocking_exit_code_zero(tmp_path, monkeypatch):
    """指标未达标 → 报表给出判定，退出码仍然是 0。

    Fixture：基线 1 个 plan、100 tokens；窗口里 2 个中型 plan、各 500
    tokens —— ``pdt_plan_tokens`` 明显比基线差，目标判定必须是
    ``not_met``。但本模块不做门禁，所以退出码必须是 0，报表必须把判定
    写出来。
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    _write_plan(plans_dir, "win-medium-a", tokens=500)
    _write_plan(plans_dir, "win-medium-b", tokens=500)

    baseline_path = tmp_path / "baseline.json"
    _write_baseline(baseline_path, tokens=100)

    out_path = tmp_path / "effect.md"
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))

    code = effect_report.main(
        [
            "--baseline",
            str(baseline_path),
            "--since",
            SINCE,
            "--out",
            str(out_path),
        ]
    )

    assert code == 0, (
        "the effect report is a measurement, not a gate: a missed target "
        f"must not change the exit code (got {code})"
    )
    report = out_path.read_text(encoding="utf-8")
    assert "not_met" in report, (
        "the report must record the missed target instead of hiding it"
    )
    assert "pdt_plan_tokens" in report, (
        "the report must name the metric it judged"
    )


def test_completed_at_comes_from_the_plan_itself(tmp_path):
    """plan 的完成时刻取自它自己留下的记录，不取「算报告的这一刻」。

    现算出来的 usage_report 带着「算它的这一刻」的时间戳。拿它当完成
    时刻，等于把每个 plan 的完成时刻都推到当下，于是每个 plan 都落进每
    个窗口 —— 窗口规则直接失效。这里钉住：注册表里的最后一条记录说了算，
    比落盘的 usage_report 更晚也算它更早。
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    (plan_dir / "llm_sessions.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-10-03T00:00:00+00:00",
                "session_id": "s1",
                "scene": "execution",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (plan_dir / "usage_report.json").write_text(
        json.dumps(_usage_report("plan-x", total_tokens=10)),
        encoding="utf-8",
    )

    record = effect_report.observe_plan(plan_dir)

    assert record["completed_at"].startswith("2026-10-03"), (
        "the plan's completion is the moment it last did work, not the "
        f"moment its usage report was written: {record['completed_at']!r}"
    )


def test_zero_baseline_leaves_the_metric_unjudged(tmp_path, monkeypatch):
    """基线是 0 的指标不能被判「未达标」。

    「不比 0 差」不是一个能达成的目标，是地板而不是上界。历史语料里没有
    轮次归因，基线的 ``round_median`` 就是 0；拿它当上界，窗口里只要有
    轮次就必然 ``not_met`` —— 那是口径变了，不是效果变差了。这种指标必须
    留着不判，而不是假装它不合格。
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    for name in ("win-medium-a", "win-medium-b"):
        plan_dir = plans_dir / name
        plan_dir.mkdir()
        (plan_dir / "usage_report.json").write_text(
            json.dumps(
                _usage_report(
                    name,
                    total_tokens=500,
                    rounds={
                        "1": {"requests": 2, "total_tokens": 500, "cost_usd": 1.0}
                    },
                )
            ),
            encoding="utf-8",
        )
        (plan_dir / "tasks.json").write_text(
            json.dumps({"tasks": [{"id": str(i)} for i in range(5)]}),
            encoding="utf-8",
        )

    baseline_path = tmp_path / "baseline.json"
    _write_baseline(baseline_path, tokens=100)

    out_path = tmp_path / "effect.md"
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))

    code = effect_report.main(
        [
            "--baseline",
            str(baseline_path),
            "--since",
            SINCE,
            "--out",
            str(out_path),
        ]
    )

    assert code == 0
    report = out_path.read_text(encoding="utf-8")
    assert "no_baseline" in report, (
        "a metric whose baseline carries no comparable value must stay "
        "unjudged instead of being called a miss"
    )


def test_rising_escape_signal_marks_strictness_damaged(tmp_path, monkeypatch):
    """逃逸信号上升 → 结论标记「严格度受损」并回溯 VP 移除审计。

    指标再好也不作数：缺陷照旧逃逸，说明严格度被削弱了，而削弱严格度的
    典型手法就是摘掉一个 VP —— 所以结论必须把读者送回那份追加写的审计。
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    for name, outcome in (("win-medium-a", "FAILED"), ("win-medium-b", "PASSED")):
        plan_dir = plans_dir / name
        plan_dir.mkdir()
        (plan_dir / "usage_report.json").write_text(
            json.dumps(_usage_report(name, total_tokens=500)),
            encoding="utf-8",
        )
        (plan_dir / "tasks.json").write_text(
            json.dumps({"tasks": [{"id": str(i)} for i in range(5)]}),
            encoding="utf-8",
        )
        (plan_dir / "verification_report.json").write_text(
            json.dumps({"plan_id": name, "overall_status": outcome}),
            encoding="utf-8",
        )

    baseline_path = tmp_path / "baseline.json"
    _write_baseline(baseline_path, tokens=100)

    out_path = tmp_path / "effect.md"
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))

    code = effect_report.main(
        [
            "--baseline",
            str(baseline_path),
            "--since",
            SINCE,
            "--out",
            str(out_path),
        ]
    )

    assert code == 0, "even a rising escape signal must not fail the command"
    report = out_path.read_text(encoding="utf-8")
    assert "严格度受损" in report, (
        "a rising escape signal must mark the conclusion as strictness "
        f"damage: {report[-400:]!r}"
    )
    assert "later_plan_failures" in report
    assert "vp_removal_audit.jsonl" in report, (
        "the conclusion must send the reader back to the VP removal audit"
    )


def test_report_names_every_metric_and_escape_signal(tmp_path, monkeypatch):
    """报表必须把 7 个指标与 3 类逃逸信号都点名 —— 少一个就没人能核对。"""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    _write_plan(plans_dir, "win-medium-a", tokens=500)
    _write_plan(plans_dir, "win-medium-b", tokens=500)

    baseline_path = tmp_path / "baseline.json"
    _write_baseline(baseline_path, tokens=100)

    out_path = tmp_path / "effect.md"
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))

    code = effect_report.main(
        [
            "--baseline",
            str(baseline_path),
            "--since",
            SINCE,
            "--out",
            str(out_path),
        ]
    )

    assert code == 0
    report = out_path.read_text(encoding="utf-8")
    for name in effect_report.METRIC_NAMES:
        assert name in report, f"the report never names the metric {name!r}"
    for name in effect_report.ESCAPE_SIGNALS:
        assert name in report, f"the report never names the escape signal {name!r}"
