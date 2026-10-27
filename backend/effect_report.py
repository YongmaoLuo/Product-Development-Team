"""效果报表：把「这次收敛优化到底有没有效果」变成一份可复跑的产物。

这个模块**不是门禁**。效果指标没达标，只在报表里留下一条判定；退出码
恒为 0 —— 绝不让流水线因为「效果不够好」变红。这是本模块最重要的一条
契约，:func:`main` 与 :func:`build_effect_report` 都遵守它。

为什么要有基线
--------------
「有没有效果」是一个比较命题，比较的两端必须是可复跑的东西。所以：

* **基线**是一份冻结产物：``plans/_analysis/`` 下的一个 JSON，里面是
  冻结时刻的 7 天账本，以及每个 plan 归一后的 ``usage_report``。
* **窗口**是从合入时刻起的一段观测期，按一条写死的规则关闭（见下）。
* 报表把基线与窗口的 7 个指标逐项对齐，并把**逃逸信号**并排列出来 ——
  指标好看但缺陷照旧逃逸，是这套改动最容易骗过自己的地方。

窗口规则：先到者为准
----------------------
合入后累计 7 天、或跑满 2 个中型 plan，先到者为准。窗口关闭的时刻**只由
数据决定**（合入时刻 + 观测到的 plan 集合），不读墙上时钟 —— 否则同一份
基线跑两次会得到两个不同的报表，报表就不可复跑了。

* 天数规则：观测集合里存在一个 ``completed_at`` 落在「合入 + 7 天」当天
  或之后的 plan，它就是「7 天已经过去」的证据；窗口在合入 + 7 天关闭。
* plan 规则：跑满 2 个中型 plan，窗口在第 2 个中型 plan 完成时关闭。
* 两个条件都成立时，取时刻更早的那个。

指标集合是常量
--------------
:data:`METRIC_NAMES` 与 :data:`ESCAPE_SIGNALS` 是跨轮次比较的锚点：基线里
有 7 项、这一轮只有 6 项，缺的那一项不是「没测」，而是「没人记得它叫什
么」。增删任何一项都必须显式改静态门禁 ``test_effect_report_metrics_frozen``
—— 改门禁的那一次提交，就是「指标集合确实变了」的记录。

数据来源
--------
每个 plan 的用量取自 ``plan_usage.build_usage_report`` 的产物（任务 6 的
``by_vp`` / ``by_round`` 归因切分）：优先读 plan 目录里已经落盘的
``usage_report.json``，没有就在需要时现算。逃逸信号里的两个 git 计数读本
仓库的提交历史。

用法
----
::

    PYTHONPATH=backend python -m effect_report \\
        --baseline plans/_analysis/baseline-2026-10-26.json \\
        --since 2026-10-27 \\
        --out plans/_analysis/effect-2026-11-03.md
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

#: Repository root — the parent of the backend package. Used to find the
#: git history the escape signals read.
REPO_ROOT: Path = _BACKEND_DIR.parent


# ---------------------------------------------------------------------------
# The frozen contracts
# ---------------------------------------------------------------------------

#: The seven metrics, in report order. Frozen: see
#: ``backend/tests/static_gates/test_effect_report_metrics_frozen.py``.
METRIC_NAMES: tuple[str, ...] = (
    "pdt_plan_tokens",
    "per_plan_tokens",
    "repair_ratio",
    "exec_vs_verify",
    "round_median",
    "round_p90",
    "unattributed_ratio",
)

#: The three escape signals — the counterweight to the metrics above. A
#: report whose numbers look fine while defects keep escaping is exactly
#: the failure this set exists to make visible. Frozen: see the static gate.
ESCAPE_SIGNALS: tuple[str, ...] = (
    "later_plan_failures",
    "post_merge_reverts",
    "targeted_fix_commits",
)

#: How long the window may stay open before the days rule closes it.
WINDOW_DAYS: int = 7

#: How many medium-sized plans close the window under the plans rule.
WINDOW_MEDIUM_PLANS: int = 2

#: A plan with this many tasks or fewer is "small".
MEDIUM_PLAN_MIN_TASKS: int = 4

#: A plan with more tasks than this is "large"; between the two is "medium".
MEDIUM_PLAN_MAX_TASKS: int = 8

#: The bucket that carries spend which could not be attributed to a
#: verification point / round / scene.
UNATTRIBUTED: str = "(unattributed)"

#: The scene names the execution/verification split reads.
EXECUTION_SCENE: str = "execution"
VERIFICATION_SCENE: str = "verification"

#: What the report says when the audit trail's own name cannot be read.
_AUDIT_TRAIL_FALLBACK: str = "the VP removal audit trail"

#: ``verification_report.json``'s ``overall_status`` → the report's outcome.
_OUTCOMES: Dict[str, str] = {
    "PASSED": "passed",
    "FAILED": "failed",
    "PARTIAL": "partial",
    "BLOCKED": "blocked",
}

#: A commit subject that reverts earlier work.
_REVERT_RE = re.compile(r"\brevert(?:s|ed|ing)?\b|回滚|回退", re.IGNORECASE)

#: A commit subject that fixes something on purpose.
_FIX_RE = re.compile(r"\bfix(?:es|ed)?\b|\bfixt\b|hotfix|修复|修正", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_datetime(value: Any) -> Optional[datetime]:
    """Parse a timestamp into an aware UTC datetime (``None`` when unusable).

    Accepts a ``datetime``, an ISO string, or a bare ``YYYY-MM-DD`` date.
    Naive values are read as UTC — the ledger writes UTC everywhere, and a
    naive local reading would silently shift every window boundary.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(
                date.fromisoformat(text), datetime.min.time()
            )
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _median(values: Sequence[float]) -> float:
    """The median of ``values`` (``0.0`` for an empty sequence)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def _percentile(values: Sequence[float], fraction: float) -> float:
    """A linearly interpolated percentile of ``values`` (``0.0`` when empty)."""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _groups_to_map(groups: Any) -> Dict[str, Dict[str, Any]]:
    """Normalise a ``by_*`` section into ``{key: {requests, total_tokens, ...}}``.

    ``plan_usage`` writes the scene split as a list and the vp / round
    splits as a mapping; both shapes have to end up as one comparable form.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(groups, dict):
        items: Iterable[Tuple[Any, Any]] = groups.items()
    elif isinstance(groups, list):
        items = ((item.get("key"), item) for item in groups if isinstance(item, dict))
    else:
        return out
    for key, value in items:
        if not isinstance(value, dict):
            continue
        try:
            tokens = int(value.get("total_tokens") or 0)
        except (TypeError, ValueError):
            tokens = 0
        try:
            requests = int(value.get("requests") or 0)
        except (TypeError, ValueError):
            requests = 0
        try:
            cost = float(value.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        out[str(key)] = {
            "requests": requests,
            "total_tokens": tokens,
            "cost_usd": cost,
        }
    return out


def _removal_audit_filename() -> str:
    """The VP removal audit trail's file name, asked of the module that owns it.

    Only the audit module may name that file — a second production module
    spelling it out is a second place a writer could grow. So the name is
    read from the audit module itself, loaded straight from its file
    rather than through its package: importing the package would drag the
    whole verification stack into a reporting tool.
    """
    module_path = _BACKEND_DIR / "verification" / "vp_removal_audit.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "_effect_report_removal_audit", module_path
        )
        if spec is None or spec.loader is None:
            return _AUDIT_TRAIL_FALLBACK
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        name = getattr(module, "AUDIT_FILENAME", None)
        return str(name) if name else _AUDIT_TRAIL_FALLBACK
    except Exception:
        return _AUDIT_TRAIL_FALLBACK


def classify_plan_size(task_count: int) -> str:
    """Classify a plan by its task count.

    The thresholds are this module's own convention — nothing in the
    workflow states a plan's size — so they are stated once, here, and
    used everywhere: a plan with fewer than :data:`MEDIUM_PLAN_MIN_TASKS`
    tasks is *small*, up to :data:`MEDIUM_PLAN_MAX_TASKS` is *medium*,
    beyond that *large*. A plan with no readable task list is *unknown*
    and never counts towards the plans rule.
    """
    try:
        count = int(task_count)
    except (TypeError, ValueError):
        return "unknown"
    if count <= 0:
        return "unknown"
    if count < MEDIUM_PLAN_MIN_TASKS:
        return "small"
    if count <= MEDIUM_PLAN_MAX_TASKS:
        return "medium"
    return "large"


# ---------------------------------------------------------------------------
# The window rule
# ---------------------------------------------------------------------------


def resolve_window(
    started_at: Any, plans: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """Close the measurement window by the frozen rule: whichever comes first.

    *Started at* is the moment the change was merged; *plans* is every
    observed plan with a ``plan_id``, a ``size`` and a ``completed_at``.
    The result says which rule closed the window, when it closed, and
    which plans belong to it.

    The window is ``[started_at, closed_at]`` — a plan that finished after
    the window closed is out of it, even when it is the plan that proved
    the days rule had come around.
    """
    start = _as_datetime(started_at)
    if start is None:
        raise ValueError(f"unusable window start: {started_at!r}")

    observed: List[Dict[str, Any]] = []
    for raw in plans or []:
        if not isinstance(raw, dict):
            continue
        completed = _as_datetime(raw.get("completed_at"))
        if completed is None:
            continue
        observed.append(
            {
                "plan_id": raw.get("plan_id"),
                "size": raw.get("size") or "unknown",
                "completed_at": completed,
            }
        )
    observed.sort(key=lambda item: item["completed_at"])

    days_end = start + timedelta(days=WINDOW_DAYS)
    # Only plans that finished *inside* the window count towards the
    # plans rule. A plan that ended before the change was merged says
    # nothing about how the change behaved.
    medium = [
        item
        for item in observed
        if item["size"] == "medium" and item["completed_at"] >= start
    ]

    candidates: List[Tuple[str, datetime]] = []
    if any(item["completed_at"] >= days_end for item in observed):
        candidates.append(("days", days_end))
    if len(medium) >= WINDOW_MEDIUM_PLANS:
        candidates.append(
            ("plans", medium[WINDOW_MEDIUM_PLANS - 1]["completed_at"])
        )

    if not candidates:
        return {
            "type": "open",
            "started_at": start.isoformat(),
            "closed_at": None,
            "days": None,
            "medium_plans": 0,
            "plans": [],
        }

    kind, closed_at = min(candidates, key=lambda item: item[1])
    inside = [
        item
        for item in observed
        if start <= item["completed_at"] <= closed_at
    ]
    return {
        "type": kind,
        "started_at": start.isoformat(),
        "closed_at": closed_at.isoformat(),
        "days": (closed_at - start).days,
        "medium_plans": len(
            [item for item in medium if start <= item["completed_at"] <= closed_at]
        ),
        "plans": inside,
    }


# ---------------------------------------------------------------------------
# Plan records
# ---------------------------------------------------------------------------


def normalize_usage_report(
    report: Dict[str, Any],
    *,
    plan_id: Optional[str] = None,
    completed_at: Any = None,
    size: Optional[str] = None,
    outcome: Optional[str] = None,
    task_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Turn one raw usage report into the record every metric reads.

    The raw shape comes from ``plan_usage.build_usage_report``: totals in
    ``totals``, the scene split as a list under ``by_scene``, and the vp /
    round splits as mappings. Here they all become one comparable form.
    """
    report = report if isinstance(report, dict) else {}
    totals = report.get("totals") if isinstance(report.get("totals"), dict) else {}
    try:
        total_tokens = int(totals.get("total_tokens") or 0)
    except (TypeError, ValueError):
        total_tokens = 0
    try:
        requests = int(totals.get("requests") or 0)
    except (TypeError, ValueError):
        requests = 0
    try:
        cost = float(totals.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0

    completed = _as_datetime(completed_at)
    return {
        "plan_id": plan_id or report.get("plan_id"),
        "size": size or "unknown",
        "task_count": int(task_count or 0),
        "completed_at": completed.isoformat() if completed else None,
        "outcome": outcome or "unknown",
        "totals": {
            "requests": requests,
            "total_tokens": total_tokens,
            "cost_usd": cost,
        },
        "by_scene": _groups_to_map(report.get("by_scene")),
        "by_vp": _groups_to_map(report.get("by_vp")),
        "by_round": _groups_to_map(report.get("by_round")),
        "by_day": _groups_to_map(report.get("by_day")),
    }


def _read_json(path: Path) -> Any:
    """Read a JSON file, or ``None`` when it is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _plan_task_count(plan_dir: Path) -> int:
    """How many tasks the plan's task list declares (``0`` when unknown)."""
    data = _read_json(plan_dir / "tasks.json")
    if isinstance(data, dict):
        tasks = data.get("tasks")
        if isinstance(tasks, list):
            return len(tasks)
    if isinstance(data, list):
        return len(data)
    return 0


def _plan_outcome(plan_dir: Path) -> str:
    """The plan's verification outcome (``passed`` / ``failed`` / ...)."""
    data = _read_json(plan_dir / "verification_report.json")
    if isinstance(data, dict):
        status = data.get("overall_status") or data.get("final_verdict")
        if isinstance(status, str):
            return _OUTCOMES.get(status.strip().upper(), "unknown")
    return "unknown"


def _plan_completed_at(plan_dir: Path) -> Optional[datetime]:
    """When the plan was last at work.

    The candidates are the timestamps the plan's own artefacts carry, and
    they are read in order of how well they answer the question:

    1. the last recorded LLM call — the moment the plan last did work;
    2. the verification report's generation time — when it last judged;
    3. the saved usage report's generation time — when someone last
       asked for its numbers;
    4. the plan directory's own mtime.

    The first one that exists wins. They are *not* maximised over: a
    freshly computed usage report carries the moment it was computed, and
    maximising would push every plan's completion to "now" and put every
    plan in every window.
    """
    registry = plan_dir / "llm_sessions.jsonl"
    try:
        if registry.exists():
            last = ""
            with registry.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        last = line
            if last:
                try:
                    entry = json.loads(last)
                except ValueError:
                    entry = None
                if isinstance(entry, dict):
                    stamp = _as_datetime(entry.get("ts"))
                    if stamp:
                        return stamp
    except OSError:
        pass

    verification = _read_json(plan_dir / "verification_report.json")
    if isinstance(verification, dict):
        stamp = _as_datetime(verification.get("generated_at"))
        if stamp:
            return stamp

    saved = _read_json(plan_dir / "usage_report.json")
    if isinstance(saved, dict):
        stamp = _as_datetime(saved.get("generated_at"))
        if stamp:
            return stamp

    try:
        return datetime.fromtimestamp(plan_dir.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def load_usage_report(plan_id: str, plan_dir: Path) -> Dict[str, Any]:
    """One plan's usage report: the saved one when it is usable, else a fresh one.

    A saved ``usage_report.json`` is used only when it carries the
    attribution splits this module reads; an older report without them
    would silently report every token as unattributed. When there is no
    usable saved report, the report is computed on the spot.
    """
    saved = _read_json(plan_dir / "usage_report.json")
    if isinstance(saved, dict) and "by_vp" in saved:
        return saved
    try:
        import plan_usage  # imported late: it drags in the whole ledger stack
    except Exception:
        return saved if isinstance(saved, dict) else {}
    try:
        return plan_usage.build_usage_report(plan_id)
    except Exception:
        return saved if isinstance(saved, dict) else {}


def observe_plan(plan_dir: Path) -> Dict[str, Any]:
    """Read one plan directory into a normalised record."""
    plan_id = plan_dir.name
    usage = load_usage_report(plan_id, plan_dir)
    task_count = _plan_task_count(plan_dir)
    return normalize_usage_report(
        usage,
        plan_id=plan_id,
        completed_at=_plan_completed_at(plan_dir),
        size=classify_plan_size(task_count),
        outcome=_plan_outcome(plan_dir),
        task_count=task_count,
    )


def collect_observed_plans(plans_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every plan under the plans root, as normalised records."""
    root = Path(plans_dir) if plans_dir is not None else _plans_dir()
    if not root.is_dir():
        return []
    records: List[Dict[str, Any]] = []
    for plan_dir in sorted(root.iterdir()):
        if not plan_dir.is_dir():
            continue
        # ``_analysis`` and friends hold the analysis artefacts, not plans;
        # a leading underscore or dot marks "not a plan".
        if plan_dir.name.startswith((".", "_")):
            continue
        records.append(observe_plan(plan_dir))
    return records


def _plans_dir() -> Path:
    """Where the plans live, honouring the operator's redirect."""
    try:
        from config_paths import resolve_plans_dir

        return resolve_plans_dir()
    except Exception:
        return REPO_ROOT / "plans"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _numeric_rounds(by_round: Dict[str, Dict[str, Any]]) -> Dict[int, int]:
    """The round buckets that name an actual round number."""
    out: Dict[int, int] = {}
    for key, group in by_round.items():
        try:
            number = int(str(key))
        except (TypeError, ValueError):
            continue
        out[number] = int(group.get("total_tokens") or 0)
    return out


def compute_metrics(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The seven metrics, computed over a set of plan records.

    Every metric is returned as ``{"value": ..., "unit": ...}`` so the
    report can render one table without special-casing each row. The
    returned mapping's keys are exactly :data:`METRIC_NAMES`.
    """
    per_plan_tokens = [
        int((record.get("totals") or {}).get("total_tokens") or 0)
        for record in records
    ]
    pdt_plan_tokens = sum(per_plan_tokens)

    repair_tokens = 0
    first_round_tokens = 0
    execution_tokens = 0
    verification_tokens = 0
    round_counts: List[int] = []
    unattributed_shares: List[float] = []

    for record in records:
        by_scene = record.get("by_scene") or {}
        by_vp = record.get("by_vp") or {}
        by_round = record.get("by_round") or {}

        execution_tokens += int(
            (by_scene.get(EXECUTION_SCENE) or {}).get("total_tokens") or 0
        )
        verification_tokens += int(
            (by_scene.get(VERIFICATION_SCENE) or {}).get("total_tokens") or 0
        )

        rounds = _numeric_rounds(by_round)
        round_counts.append(len(rounds))
        for number, tokens in rounds.items():
            if number >= 2:
                repair_tokens += tokens
            if number >= 1:
                first_round_tokens += tokens

        for partition in (by_scene, by_vp, by_round):
            if not partition:
                continue
            total = sum(
                int((group or {}).get("total_tokens") or 0)
                for group in partition.values()
            )
            if total <= 0:
                continue
            unattributed = int(
                (partition.get(UNATTRIBUTED) or {}).get("total_tokens") or 0
            )
            unattributed_shares.append(unattributed / total)

    repair_ratio = (repair_tokens / first_round_tokens) if first_round_tokens else 0.0
    ratio = (
        execution_tokens / verification_tokens if verification_tokens else None
    )

    return {
        "pdt_plan_tokens": {
            "value": pdt_plan_tokens,
            "unit": "tokens",
        },
        "per_plan_tokens": {
            "value": _median(per_plan_tokens),
            "unit": "tokens/plan",
        },
        "repair_ratio": {
            "value": repair_ratio,
            "unit": "ratio",
            "note": (
                "share of round-attributed tokens spent in round 2 or later, "
                "i.e. re-running a verification point after a repair"
            ),
        },
        "exec_vs_verify": {
            "value": {
                "execution_tokens": execution_tokens,
                "verification_tokens": verification_tokens,
                "ratio": ratio,
            },
            "unit": "tokens",
            "note": "execution tokens against verification tokens",
        },
        "round_median": {
            "value": _median(round_counts),
            "unit": "rounds/plan",
        },
        "round_p90": {
            "value": _percentile(round_counts, 0.9),
            "unit": "rounds/plan",
        },
        "unattributed_ratio": {
            "value": max(unattributed_shares) if unattributed_shares else 0.0,
            "unit": "ratio",
            "note": (
                "the worst attribution gap across the scene / vp / round "
                "splits: the share of spend that could not be placed"
            ),
        },
    }


def _metric_scalar(name: str, metrics: Dict[str, Dict[str, Any]]) -> Optional[float]:
    """The comparable number of one metric (``exec_vs_verify`` → its ratio)."""
    metric = metrics.get(name) or {}
    value = metric.get("value")
    if isinstance(value, dict):
        ratio = value.get("ratio")
        return float(ratio) if ratio is not None else None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_targets(
    targets: Dict[str, Any],
    metrics: Dict[str, Dict[str, Any]],
    *,
    plan_count: int,
) -> Dict[str, str]:
    """Judge every metric against the baseline's target.

    ``down`` means "not worse than the baseline value"; ``band`` means
    "inside the band the baseline declares". A window with no plans at all
    judges every metric ``no_data`` — nothing was measured, so nothing can
    be said to have improved or worsened.
    """
    verdicts: Dict[str, str] = {}
    for name in METRIC_NAMES:
        target = targets.get(name) if isinstance(targets, dict) else None
        if not isinstance(target, dict):
            verdicts[name] = "no_target"
            continue
        if plan_count <= 0:
            verdicts[name] = "no_data"
            continue
        value = _metric_scalar(name, metrics)
        if value is None:
            verdicts[name] = "no_data"
            continue
        direction = target.get("direction")
        if direction == "down":
            reference = target.get("baseline")
            try:
                reference = float(reference)
            except (TypeError, ValueError):
                verdicts[name] = "no_target"
                continue
            if reference == 0.0:
                # "Not worse than zero" is not a target — it is a floor the
                # measurement can never be below. A baseline of zero means
                # the baseline carries nothing to compare against, so the
                # metric is left unjudged rather than called a miss.
                verdicts[name] = "no_baseline"
                continue
            verdicts[name] = "met" if value <= reference else "not_met"
        elif direction == "band":
            low = target.get("low")
            high = target.get("high")
            inside = True
            if low is not None:
                inside = inside and value >= float(low)
            if high is not None:
                inside = inside and value <= float(high)
            verdicts[name] = "met" if inside else "not_met"
        else:
            verdicts[name] = "no_target"
    return verdicts


# ---------------------------------------------------------------------------
# Escape signals
# ---------------------------------------------------------------------------


def _git_subjects(
    repo_dir: Path, since: Any = None, until: Any = None
) -> Optional[List[str]]:
    """The commit subjects in a time range (``None`` when git cannot say)."""
    command = ["git", "-C", str(repo_dir), "log", "--format=%s"]
    start = _as_datetime(since)
    end = _as_datetime(until)
    if start is not None:
        command.append(f"--since={start.isoformat()}")
    if end is not None:
        command.append(f"--until={end.isoformat()}")
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def compute_escape_signals(
    records: Sequence[Dict[str, Any]],
    *,
    since: Any = None,
    until: Any = None,
    repo_dir: Optional[Path] = None,
) -> Dict[str, Dict[str, Any]]:
    """The three escape signals over one period.

    * ``later_plan_failures`` — plans in the period whose verification
      outcome was a failure.
    * ``post_merge_reverts`` — commits that reverted earlier work.
    * ``targeted_fix_commits`` — commits that fixed something on purpose;
      a revert is counted under its own signal, not twice.

    A git history that cannot be read yields ``None`` rather than ``0``:
    "not known" and "known to be none" are different answers, and only the
    second one may be compared against the baseline.
    """
    failures = sum(1 for record in records if record.get("outcome") == "failed")
    subjects = _git_subjects(repo_dir or REPO_ROOT, since=since, until=until)

    if subjects is None:
        reverts: Optional[int] = None
        fixes: Optional[int] = None
    else:
        reverts = sum(1 for subject in subjects if _REVERT_RE.search(subject))
        fixes = sum(
            1
            for subject in subjects
            if _FIX_RE.search(subject) and not _REVERT_RE.search(subject)
        )

    return {
        "later_plan_failures": {"value": failures, "unit": "plans"},
        "post_merge_reverts": {"value": reverts, "unit": "commits"},
        "targeted_fix_commits": {"value": fixes, "unit": "commits"},
    }


def rising_escape_signals(
    baseline: Dict[str, Dict[str, Any]], window: Dict[str, Dict[str, Any]]
) -> List[str]:
    """Which escape signals went up between the baseline and the window."""
    risen: List[str] = []
    for name in ESCAPE_SIGNALS:
        before = (baseline.get(name) or {}).get("value")
        after = (window.get(name) or {}).get("value")
        if before is None or after is None:
            continue
        if after > before:
            risen.append(name)
    return risen


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_number(value: Any) -> str:
    """Render one metric value for the table."""
    if value is None:
        return "unknown"
    if isinstance(value, dict):
        execution = value.get("execution_tokens", 0)
        verification = value.get("verification_tokens", 0)
        ratio = value.get("ratio")
        suffix = "" if ratio is None else f" (×{ratio:.2f})"
        return f"{execution} : {verification}{suffix}"
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _format_delta(before: Any, after: Any) -> str:
    """The change between two metric values."""
    if before is None or after is None:
        return "unknown"
    if isinstance(before, dict) or isinstance(after, dict):
        before_ratio = (before or {}).get("ratio") if isinstance(before, dict) else before
        after_ratio = (after or {}).get("ratio") if isinstance(after, dict) else after
        if before_ratio is None or after_ratio is None:
            return "unknown"
        change = float(after_ratio) - float(before_ratio)
        return f"{change:+.4f}"
    try:
        change = float(after) - float(before)
    except (TypeError, ValueError):
        return "unknown"
    return f"{change:+.4f}".rstrip("0").rstrip(".") if change % 1 else f"{int(change):+d}"


def _target_text(target: Any) -> str:
    """The target column: how the metric was supposed to move."""
    if not isinstance(target, dict):
        return "—"
    direction = target.get("direction")
    if direction == "down":
        try:
            return f"≤ {_format_number(float(target.get('baseline')))}"
        except (TypeError, ValueError):
            return "—"
    if direction == "band":
        low = target.get("low")
        high = target.get("high")
        parts = []
        if low is not None:
            parts.append(f"≥ {_format_number(float(low))}")
        if high is not None:
            parts.append(f"≤ {_format_number(float(high))}")
        return " ".join(parts) if parts else "—"
    return "—"


def render_report(
    *,
    baseline_path: Path,
    baseline: Dict[str, Any],
    since: datetime,
    window: Dict[str, Any],
    baseline_metrics: Dict[str, Dict[str, Any]],
    window_metrics: Dict[str, Dict[str, Any]],
    verdicts: Dict[str, str],
    baseline_escape: Dict[str, Dict[str, Any]],
    window_escape: Dict[str, Dict[str, Any]],
    risen: Sequence[str],
    records: Sequence[Dict[str, Any]],
    out_path: Path,
) -> str:
    """Render the report as markdown.

    Every metric name and every escape-signal name appears verbatim: a
    reader comparing two reports has to be able to line the two up by
    name, and a renamed metric is exactly what the frozen contract
    exists to prevent.
    """
    targets = baseline.get("targets") if isinstance(baseline, dict) else {}
    targets = targets if isinstance(targets, dict) else {}
    baseline_plans = baseline.get("plans") if isinstance(baseline, dict) else []
    baseline_plan_count = len(baseline_plans) if isinstance(baseline_plans, list) else 0
    frozen_at = (baseline or {}).get("frozen_at") or "unknown"

    lines: List[str] = []
    lines.append("# 效果报表 — 收敛优化")
    lines.append("")
    lines.append(
        "> **本报表是测量，不是门禁。** 任何指标未达标都只在这里留下一条"
        "判定，退出码恒为 0，流水线不会因为「效果不够好」变红。"
    )
    lines.append("")
    lines.append(f"- 基线：`{baseline_path}`（冻结于 {frozen_at}，{baseline_plan_count} 个 plan）")
    window_type = window.get("type")
    closed_at = window.get("closed_at")
    if window_type == "open":
        lines.append(
            f"- 窗口：合入 {since.date()} → **未关闭**（还没到 7 天 / 2 个中型 plan 的复测点）"
        )
    else:
        lines.append(
            f"- 窗口：合入 {since.date()} → {closed_at}（类型 `{window_type}`，"
            f"7 天 / 2 个中型 plan 先到者为准；窗口内 {len(records)} 个 plan）"
        )
    lines.append("")

    lines.append("## 效果指标")
    lines.append("")
    lines.append("| 指标 | 基线 | 窗口 | 变化 | 目标 | 判定 |")
    lines.append("|---|---|---|---|---|---|")
    for name in METRIC_NAMES:
        before = (baseline_metrics.get(name) or {}).get("value")
        after = (window_metrics.get(name) or {}).get("value")
        lines.append(
            "| `{name}` | {before} | {after} | {delta} | {target} | {verdict} |".format(
                name=name,
                before=_format_number(before),
                after=_format_number(after),
                delta=_format_delta(before, after),
                target=_target_text(targets.get(name)),
                verdict=verdicts.get(name, "no_target"),
            )
        )
    lines.append("")

    lines.append("## rounds_and_outcome（轮次与结果）")
    lines.append("")
    if records:
        lines.append("| plan | 规模 | 任务数 | 轮次 | 结果 | tokens |")
        lines.append("|---|---|---|---|---|---|")
        for record in records:
            rounds = _numeric_rounds(record.get("by_round") or {})
            totals = record.get("totals") or {}
            lines.append(
                "| `{plan_id}` | {size} | {tasks} | {rounds} | {outcome} | {tokens} |".format(
                    plan_id=record.get("plan_id"),
                    size=record.get("size"),
                    tasks=record.get("task_count", 0),
                    rounds=len(rounds),
                    outcome=record.get("outcome"),
                    tokens=totals.get("total_tokens", 0),
                )
            )
    else:
        lines.append("（窗口内没有可测量的 plan。）")
    lines.append("")

    lines.append("## 逃逸信号")
    lines.append("")
    lines.append("| 信号 | 基线 | 窗口 | 变化 | 上升 |")
    lines.append("|---|---|---|---|---|")
    for name in ESCAPE_SIGNALS:
        before = (baseline_escape.get(name) or {}).get("value")
        after = (window_escape.get(name) or {}).get("value")
        lines.append(
            "| `{name}` | {before} | {after} | {delta} | {rose} |".format(
                name=name,
                before=_format_number(before),
                after=_format_number(after),
                delta=_format_delta(before, after),
                rose="是" if name in risen else "否",
            )
        )
    lines.append("")

    lines.append("## 结论")
    lines.append("")
    lines.append(f"- {_conclusion(verdicts, risen, records)}")
    if risen:
        lines.append(
            f"- 逃逸信号上升（{', '.join('`' + name + '`' for name in risen)}）→ "
            f"**严格度受损**：必须回溯 `{_removal_audit_filename()}`，"
            "确认是不是有 VP 在严格度下降的情况下被摘掉了。"
        )
    lines.append("")

    lines.append("## 复跑")
    lines.append("")
    lines.append("```")
    lines.append(
        "PYTHONPATH=backend python -m effect_report "
        f"--baseline {baseline_path} --since {since.date()} --out {out_path}"
    )
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _conclusion(
    verdicts: Dict[str, str], risen: Sequence[str], records: Sequence[Dict[str, Any]]
) -> str:
    """The one-line conclusion the report leads with."""
    if not records:
        return "结论：**数据不足** —— 窗口内没有可测量的 plan，什么都判不了。"
    if risen:
        return (
            "结论：**严格度受损** —— 逃逸信号比基线高，"
            "指标再好也不作数，先查逃逸。"
        )
    if any(verdict == "not_met" for verdict in verdicts.values()):
        return "结论：**效果未达标** —— 有指标比基线差（报表判定，不作门禁）。"
    return "结论：**效果达标** —— 全部指标未劣于基线，逃逸信号未上升。"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _period_bounds(period: Dict[str, Any]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """The inclusive bounds of a day-granular period.

    The end of the period covers the whole day it names: a plan that
    finished at 14:40 on the last day of the baseline period is *in* it.
    """
    start = _as_datetime(period.get("from"))
    end = _as_datetime(period.get("to"))
    if end is not None:
        end = end + timedelta(days=1) - timedelta(microseconds=1)
    return start, end


def _in_period(stamp: Any, period: Dict[str, Any]) -> bool:
    """Whether a completion time falls inside the period."""
    moment = _as_datetime(stamp)
    if moment is None:
        return False
    start, end = _period_bounds(period)
    if start is not None and moment < start:
        return False
    if end is not None and moment > end:
        return False
    return True


def build_effect_report(baseline: Any, since: Any, out: Any) -> int:
    """Build the effect report and write it to ``out``.

    ``baseline`` is the path to the frozen baseline, ``since`` the moment
    the change was merged, ``out`` where the markdown report goes. The
    return value is the process exit code, and it is **0 whenever the
    report could be built** — a missed target is a finding, not a failure.
    """
    baseline_path = Path(baseline)
    data = _read_json(baseline_path)
    if not isinstance(data, dict):
        raise ValueError(f"unreadable baseline: {baseline_path}")

    started = _as_datetime(since)
    if started is None:
        raise ValueError(f"unusable --since value: {since!r}")

    observed = collect_observed_plans()
    window = resolve_window(started, observed)
    window_ids = {record.get("plan_id") for record in window["plans"]}
    records = [
        record for record in observed if record.get("plan_id") in window_ids
    ]

    baseline_plans = data.get("plans")
    baseline_records: List[Dict[str, Any]] = []
    if isinstance(baseline_plans, list):
        for raw in baseline_plans:
            if isinstance(raw, dict):
                baseline_records.append(
                    normalize_usage_report(
                        raw,
                        plan_id=raw.get("plan_id"),
                        completed_at=raw.get("completed_at"),
                        size=raw.get("size"),
                        outcome=raw.get("outcome"),
                        task_count=raw.get("task_count"),
                    )
                )

    # The baseline corpus is the whole frozen set; the baseline *period*
    # is the slice of it that was active before the merge. Comparing the
    # corpus with the window would compare a month against a week.
    period = data.get("period") if isinstance(data.get("period"), dict) else {}
    if period:
        baseline_records = [
            record
            for record in baseline_records
            if _in_period(record.get("completed_at"), period)
        ]

    baseline_metrics = compute_metrics(baseline_records)
    window_metrics = compute_metrics(records)
    verdicts = evaluate_targets(
        data.get("targets") if isinstance(data.get("targets"), dict) else {},
        window_metrics,
        plan_count=len(records),
    )

    baseline_escape = compute_escape_signals(
        baseline_records,
        since=period.get("from"),
        until=data.get("frozen_at"),
    )
    window_escape = compute_escape_signals(
        records,
        since=started,
        until=window.get("closed_at"),
    )
    risen = rising_escape_signals(baseline_escape, window_escape)

    out_path = Path(out)
    report = render_report(
        baseline_path=baseline_path,
        baseline=data,
        since=started,
        window=window,
        baseline_metrics=baseline_metrics,
        window_metrics=window_metrics,
        verdicts=verdicts,
        baseline_escape=baseline_escape,
        window_escape=window_escape,
        risen=risen,
        records=records,
        out_path=out_path,
    )
    if out_path.parent and str(out_path.parent):
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m effect_report --baseline --since --out``.

    The exit code is 0 whenever the report could be built — including
    when the metrics missed their targets, because this is a
    measurement, not a gate. Only a usage error (2) is different.
    """
    parser = argparse.ArgumentParser(
        prog="effect_report",
        description=(
            "Measure the convergence optimisation's effect against a frozen "
            "baseline. Never gates: the exit code stays 0 even when the "
            "metrics miss their targets."
        ),
    )
    parser.add_argument(
        "--baseline",
        required=True,
        help="path to the frozen baseline JSON",
    )
    parser.add_argument(
        "--since",
        required=True,
        help="the moment the change was merged (YYYY-MM-DD or an ISO timestamp)",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="where the markdown report is written",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        code = build_effect_report(args.baseline, args.since, args.out)
    except (OSError, ValueError) as exc:
        print(f"effect_report: {exc}", file=sys.stderr)
        return 2
    print(args.out)
    return code


if __name__ == "__main__":  # pragma: no cover - CLI shim
    raise SystemExit(main())
