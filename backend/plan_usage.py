"""Per-plan LLM usage aggregation against CC Switch's own ledger.

Step 2 of the per-plan usage feature (see ``usage_registry`` for step 1,
which records *which* Claude sessions a plan used).

Data model
----------
CC Switch is the single source of truth — every LLM call the backend
makes is either

* observed at CC Switch's local proxy (``data_source='proxy'``, booked
  under the provider that actually served the request), or
* parsed out of the Claude Code session JSONL transcripts
  (``data_source='session_log'``) for calls that went *directly* to a
  provider and therefore never touched the proxy. Session-routed direct
  dispatch is deliberate (see ``provider_routing``) so this second
  source is not an edge case — it is how the high/medium/low scene
  tiers stay honoured.

Both sources land in ``proxy_request_logs`` in ``~/.cc-switch/cc-switch.db``
and are therefore both queried here. A session that appears in one is
*not* expected in the other: CC Switch's cross-source dedup suppresses
the session-log copy whenever the proxy already logged the same request
(``should_skip_session_insert``), so the two sources partition the
traffic rather than overlap it.

Cross-check
-----------
The registry also carries the backend's *own* first-hand account (the
``usage`` / ``total_cost_usd`` fields of the Claude Code stream-json
``result`` event). Comparing the two is the point of this module:
a disagreement means our accounting code is wrong somewhere.

**Cost is CC Switch's, always.** The report's ``totals.cost_usd`` is the
sum of the ledger's own cost column,
computed by CC Switch from ``model_pricing`` — i.e. the price of the
model that actually served the request. The backend's figure prices the model
Claude Code *asked for*, so the two disagree whenever routing remaps a
request; that number is kept as ``pdt_side_account.cost_usd`` for
reference only and is never the headline. The report also re-prices the
observed tokens at CC Switch's *current* rates (``repriced_cost_usd``)
to surface ledger rows written while a price was missing or stale, and
carries the per-model rate table it used under ``pricing``.

One accuracy caveat, encoded in the report rather than papered over:
``result.usage`` describes the **last API call of the turn**, while
``result.total_cost_usd`` is cumulative for the session. So for
multi-turn sessions (``num_turns > 1``) the backend's token totals are expected
to sit *below* CC Switch's, while cost stays comparable. The report
surfaces ``multi_turn_attempts`` so that gap is explainable instead of
looking like a bug.

Reading the DB
--------------
CC Switch opens its database in ``journal_mode=delete`` (not WAL), so
querying the live file can hit "database is locked" while it writes. We
therefore take an online snapshot with ``sqlite3.Connection.backup``
into a temp file and query that — safe under a concurrent writer and
immune to tearing.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

import cc_switch
from config_paths import resolve_plans_dir
from usage_registry import read_llm_calls
from utils.atomic_io import atomic_write_json

#: Rows fetched per ``IN (...)`` chunk. SQLite's default limit on bound
#: variables is 999; 400 leaves headroom and keeps each statement small.
_SESSION_CHUNK = 400

#: Columns we care about, in query order (kept explicit so a schema
#: addition upstream cannot silently shift positional indices).
_USAGE_COLUMNS = (
    "session_id",
    "data_source",
    "provider_id",
    "model",
    "request_model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "total_cost_usd",
    "created_at",
)


def _load_model_pricing(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """CC Switch's own per-model price list (USD per million tokens).

    This is the pricing authority for the whole report: the operator's
    standing instruction is "以 CC Switch 的口径为准", and CC Switch is
    also what actually bills the requests.
    """
    try:
        return conn.execute(
            "SELECT model_id, display_name, input_cost_per_million,"
            " output_cost_per_million, cache_read_cost_per_million,"
            " cache_creation_cost_per_million"
            " FROM model_pricing"
        ).fetchall()
    except sqlite3.Error:
        return []  # older schema without the pricing table


def _match_pricing(
    model: str, pricing_rows: Sequence[sqlite3.Row]
) -> Optional[Dict[str, Any]]:
    """Resolve one ledger model id against CC Switch's price list.

    Exact (case-insensitive) match first, then a containment match in
    either direction — mirroring CC Switch's own lenient lookup. The
    chosen row is reported with its ``match`` kind so a fuzzy hit is
    never silently presented as an exact price.
    """
    if not model:
        return None
    needle = model.strip().lower()
    for row in pricing_rows:
        if (row["model_id"] or "").strip().lower() == needle:
            return _pricing_payload(row, "exact")
    for row in pricing_rows:
        candidate = (row["model_id"] or "").strip().lower()
        if candidate and (candidate in needle or needle in candidate):
            return _pricing_payload(row, "fuzzy")
    return None


def _pricing_payload(row: sqlite3.Row, match: str) -> Dict[str, Any]:
    return {
        "model_id": row["model_id"],
        "display_name": row["display_name"],
        "match": match,
        "input_cost_per_million": _as_float(row["input_cost_per_million"]),
        "output_cost_per_million": _as_float(row["output_cost_per_million"]),
        "cache_read_cost_per_million": _as_float(row["cache_read_cost_per_million"]),
        "cache_creation_cost_per_million": _as_float(
            row["cache_creation_cost_per_million"]
        ),
    }


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _repriced_cost(
    model_groups: Sequence[Dict[str, Any]],
    pricing_by_model: Dict[str, Optional[Dict[str, Any]]],
) -> Optional[float]:
    """Re-price observed tokens at CC Switch's *current* rates.

    The ledger stores the cost computed when the request was written;
    re-pricing with today's table surfaces the case where that stored
    number used stale or missing pricing (a model added to the DB after
    the request, or a row written before its price existed).

    Returns ``None`` when no observed model could be priced at all —
    "unknown" must not be reported as "free".
    """
    total = 0.0
    priced_any = False
    for group in model_groups:
        rates = pricing_by_model.get(group["key"])
        if not rates:
            continue
        priced_any = True
        per_million = 1_000_000.0
        total += group["new_input_tokens"] * rates["input_cost_per_million"] / per_million
        total += group["output_tokens"] * rates["output_cost_per_million"] / per_million
        total += (
            group["cache_read_tokens"]
            * rates["cache_read_cost_per_million"]
            / per_million
        )
        total += (
            group["cache_creation_tokens"]
            * rates["cache_creation_cost_per_million"]
            / per_million
        )
    return round(total, 6) if priced_any else None


def _provider_names(conn: sqlite3.Connection) -> Dict[str, str]:
    names: Dict[str, str] = {}
    try:
        for pid, name in conn.execute(
            "SELECT id, name FROM providers WHERE app_type = 'claude'"
        ):
            names[pid] = name
    except sqlite3.Error:
        pass  # providers table absent/renamed — ids stay raw
    # The registry's session-log rows carry a synthetic provider id.
    names.setdefault("_session", "Claude (Session)")
    return names


def _fetch_rows(
    conn: sqlite3.Connection, session_ids: Sequence[str]
) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows: List[sqlite3.Row] = []
    for start in range(0, len(session_ids), _SESSION_CHUNK):
        chunk = session_ids[start : start + _SESSION_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        sql = (
            f"SELECT {', '.join(_USAGE_COLUMNS)} FROM proxy_request_logs "
            f"WHERE session_id IN ({placeholders})"
        )
        rows.extend(conn.execute(sql, chunk).fetchall())
    return rows


def _empty_tokens() -> Dict[str, int]:
    return {
        "new_input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


def _add_tokens(bucket: Dict[str, int], row: sqlite3.Row) -> None:
    bucket["new_input_tokens"] += int(row["input_tokens"] or 0)
    bucket["output_tokens"] += int(row["output_tokens"] or 0)
    bucket["cache_read_tokens"] += int(row["cache_read_tokens"] or 0)
    bucket["cache_creation_tokens"] += int(row["cache_creation_tokens"] or 0)


def _total_tokens(bucket: Dict[str, int]) -> int:
    return sum(bucket.values())


def _cache_hit_rate(bucket: Dict[str, int]) -> float:
    """Share of prompt tokens served from cache.

    ``input_tokens`` in the Anthropic ledger excludes cache reads, so
    the prompt is the two together.
    """
    prompt = bucket["new_input_tokens"] + bucket["cache_read_tokens"]
    if prompt <= 0:
        return 0.0
    return round(bucket["cache_read_tokens"] / prompt, 4)


def _finalize_groups(groups: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for key, agg in groups.items():
        out.append(
            {
                "key": key,
                "requests": agg["requests"],
                **agg["tokens"],
                "total_tokens": _total_tokens(agg["tokens"]),
                "cost_usd": round(agg["cost_usd"], 6),
            }
        )
    out.sort(key=lambda item: item["total_tokens"], reverse=True)
    return out


def _bump(
    groups: Dict[str, Dict[str, Any]],
    key: Optional[str],
    row: sqlite3.Row,
) -> None:
    key = key or "(unknown)"
    agg = groups.setdefault(
        key, {"requests": 0, "cost_usd": 0.0, "tokens": _empty_tokens()}
    )
    agg["requests"] += 1
    _add_tokens(agg["tokens"], row)
    try:
        agg["cost_usd"] += float(row["total_cost_usd"] or 0)
    except (TypeError, ValueError):
        pass


def _day_of(created_at: Optional[int]) -> str:
    if not created_at:
        return "unknown"
    try:
        return datetime.fromtimestamp(int(created_at), tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
    except (OSError, OverflowError, ValueError):
        return "unknown"


def _pdt_side_account(entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """the backend's own first-hand numbers, from the stream-json result events."""
    account = {
        "attempts": 0,
        "ok": 0,
        "failed": 0,
        "multi_turn_attempts": 0,
        "tokens": _empty_tokens(),
        "cost_usd": 0.0,
        "usage_reported": 0,
    }
    for entry in entries:
        account["attempts"] += 1
        if entry.get("ok"):
            account["ok"] += 1
        else:
            account["failed"] += 1
        turns = entry.get("num_turns")
        if isinstance(turns, int) and turns > 1:
            account["multi_turn_attempts"] += 1
        usage = entry.get("usage") or {}
        if isinstance(usage, dict) and usage:
            account["usage_reported"] += 1
            account["tokens"]["new_input_tokens"] += int(
                usage.get("input_tokens") or 0
            )
            account["tokens"]["output_tokens"] += int(
                usage.get("output_tokens") or 0
            )
            account["tokens"]["cache_read_tokens"] += int(
                usage.get("cache_read_input_tokens") or 0
            )
            account["tokens"]["cache_creation_tokens"] += int(
                usage.get("cache_creation_input_tokens") or 0
            )
        try:
            account["cost_usd"] += float(entry.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
    account["cost_usd"] = round(account["cost_usd"], 6)
    account["total_tokens"] = _total_tokens(account["tokens"])
    return account


def build_usage_report(
    plan_id: str, *, db_path: Optional[Path] = None
) -> Dict[str, Any]:
    """Aggregate one plan's LLM usage. Never raises on missing data."""
    entries = read_llm_calls(plan_id)
    sessions: List[str] = []
    session_meta: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        sid = entry.get("session_id")
        if not sid:
            continue
        if sid not in session_meta:
            sessions.append(sid)
            session_meta[sid] = {"scenes": set(), "tasks": set()}
        meta = session_meta[sid]
        if entry.get("scene"):
            meta["scenes"].add(entry["scene"])
        if entry.get("task_id"):
            meta["tasks"].add(entry["task_id"])

    report: Dict[str, Any] = {
        "plan_id": plan_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cc_switch_db": str(db_path or cc_switch.resolve_db_path()),
        "sessions": {
            "registry_entries": len(entries),
            "distinct_sessions": len(sessions),
            "tracked": 0,
            "untracked": 0,
        },
        "totals": {
            "requests": 0,
            **_empty_tokens(),
            "total_tokens": 0,
            "cost_usd": 0.0,
            # Stable shape across every exit path (no-data / ledger
            # unavailable / normal): consumers must not have to
            # special-case a missing key.
            "cost_source": "cc_switch",
            "repriced_cost_usd": None,
            "cache_hit_rate": 0.0,
        },
        "by_data_source": [],
        "by_provider": [],
        "by_model": [],
        "by_scene": [],
        "by_task": [],
        "by_day": [],
        "pricing": {
            "source": "cc_switch.model_pricing",
            "currency": "USD",
            "unit": "per_million_tokens",
            "models": [],
        },
        "pdt_side_account": _pdt_side_account(entries),
        "cross_check": {
            "verdict": "no_data",
            "notes": [],
            "token_delta": {},
            "cost_delta_usd": None,
            "sessions_unseen_by_cc_switch": [],
        },
    }

    if not entries:
        report["cross_check"]["notes"].append(
            "no registry entries for this plan — either it never made an "
            "LLM call, or it ran before the usage registry existed "
            "(2026-09-21)"
        )
        return report

    rows: List[sqlite3.Row] = []
    names: Dict[str, str] = {}
    pricing_rows: List[sqlite3.Row] = []
    error: Optional[str] = None
    try:
        with cc_switch.snapshot(db_path) as snapshot:
            conn = sqlite3.connect(str(snapshot))
            # Row access by column name for every reader below — set
            # here rather than per-query, so a new query cannot get a
            # plain tuple back just because it runs first.
            conn.row_factory = sqlite3.Row
            try:
                names = _provider_names(conn)
                pricing_rows = _load_model_pricing(conn)
                rows = _fetch_rows(conn, sessions)
            finally:
                conn.close()
    except Exception as exc:  # missing DB / locked / schema drift
        error = f"{type(exc).__name__}: {exc}"
        report["cross_check"]["notes"].append(
            f"CC Switch ledger unavailable ({error}); "
            "report contains backend-side numbers only"
        )
        report["cross_check"]["verdict"] = "ledger_unavailable"
        return report

    seen_sessions = set()
    totals = _empty_tokens()
    by_source: Dict[str, Dict[str, Any]] = {}
    by_provider: Dict[str, Dict[str, Any]] = {}
    by_model: Dict[str, Dict[str, Any]] = {}
    by_scene: Dict[str, Dict[str, Any]] = {}
    by_task: Dict[str, Dict[str, Any]] = {}
    by_day: Dict[str, Dict[str, Any]] = {}
    total_cost = 0.0

    for row in rows:
        seen_sessions.add(row["session_id"])
        _add_tokens(totals, row)
        try:
            total_cost += float(row["total_cost_usd"] or 0)
        except (TypeError, ValueError):
            pass
        source = row["data_source"] or "proxy"
        _bump(by_source, source, row)
        _bump(by_provider, names.get(row["provider_id"], row["provider_id"]), row)
        _bump(by_model, row["model"], row)
        _bump(by_day, _day_of(row["created_at"]), row)
        meta = session_meta.get(row["session_id"], {})
        for scene in meta.get("scenes") or {"(unattributed)"}:
            _bump(by_scene, scene, row)
        for task in meta.get("tasks") or {"(unattributed)"}:
            _bump(by_task, task, row)

    untracked = []
    for sid in sessions:
        if sid in seen_sessions:
            continue
        meta = session_meta.get(sid, {})
        untracked.append(
            {
                "session_id": sid,
                "scenes": sorted(meta.get("scenes") or []),
                "tasks": sorted(meta.get("tasks") or []),
            }
        )

    report["totals"] = {
        "requests": len(rows),
        **totals,
        "total_tokens": _total_tokens(totals),
        "cost_usd": round(total_cost, 6),
        "cache_hit_rate": _cache_hit_rate(totals),
    }
    report["by_data_source"] = _finalize_groups(by_source)
    report["by_provider"] = _finalize_groups(by_provider)
    report["by_model"] = _finalize_groups(by_model)
    report["by_scene"] = _finalize_groups(by_scene)
    report["by_task"] = _finalize_groups(by_task)
    report["by_day"] = sorted(
        _finalize_groups(by_day), key=lambda item: item["key"]
    )
    report["sessions"]["tracked"] = len(seen_sessions)
    report["sessions"]["untracked"] = len(untracked)

    # Pricing, straight from CC Switch's own table. The ledger's cost is
    # the bill; the re-priced figure is the same tokens valued at today's
    # rates, which surfaces rows written while a price was missing/stale.
    pricing_by_model: Dict[str, Optional[Dict[str, Any]]] = {
        group["key"]: _match_pricing(group["key"], pricing_rows)
        for group in report["by_model"]
    }
    report["pricing"] = {
        "source": "cc_switch.model_pricing",
        "currency": "USD",
        "unit": "per_million_tokens",
        "models": [
            {"model": group["key"], **(pricing_by_model[group["key"]] or {
                "model_id": None, "display_name": None, "match": "none",
                "input_cost_per_million": None,
                "output_cost_per_million": None,
                "cache_read_cost_per_million": None,
                "cache_creation_cost_per_million": None,
            })}
            for group in report["by_model"]
        ],
    }
    repriced = _repriced_cost(report["by_model"], pricing_by_model)
    report["totals"]["cost_source"] = "cc_switch"
    report["totals"]["repriced_cost_usd"] = repriced
    unpriced = [
        group["key"]
        for group in report["by_model"]
        if not pricing_by_model.get(group["key"])
    ]

    ac = report["pdt_side_account"]
    notes = report["cross_check"]["notes"]
    report["cross_check"]["sessions_unseen_by_cc_switch"] = untracked
    token_delta = {
        key: totals[key] - ac["tokens"][key] for key in totals
    }
    report["cross_check"]["token_delta"] = token_delta
    report["cross_check"]["cost_delta_usd"] = round(
        report["totals"]["cost_usd"] - ac["cost_usd"], 6
    )

    if not rows:
        report["cross_check"]["verdict"] = "mismatch"
        notes.append(
            "the backend recorded LLM calls for this plan but CC Switch has no rows "
            "for any of those sessions — check the scanner cursor / DB path"
        )
    elif untracked:
        report["cross_check"]["verdict"] = "partial"
        notes.append(
            f"{len(untracked)} of {len(sessions)} sessions have no CC Switch "
            "row (dead session, or the session-log scan has not caught up)"
        )
    else:
        report["cross_check"]["verdict"] = "consistent"
    if ac["multi_turn_attempts"]:
        notes.append(
            f"{ac['multi_turn_attempts']} attempt(s) had num_turns>1 — "
            "result.usage covers only the last API call of the turn, so "
            "backend-side token totals are expected to undercount those sessions "
            "while cost_usd stays cumulative/comparable"
        )
    # Cost authority: CC Switch. Its ledger figure is the bill; the backend's own
    # number is Claude Code pricing the model it *asked for*, kept only
    # as a reference. Never let the two look like equals.
    delta = report["cross_check"]["cost_delta_usd"]
    pdt_reported_cost = (ac["cost_usd"] or 0) > 0 or (ac["usage_reported"] or 0) > 0
    if pdt_reported_cost and delta and abs(delta) > 1e-6:
        notes.append(
            f"参考值对照：backend 侧自报成本 ${ac['cost_usd']:.4f}（Claude Code 按"
            "『请求的模型』计价），CC Switch 计费 "
            f"${report['totals']['cost_usd']:.4f}（按『实际服务的模型』计价）"
            " —— 以 CC Switch 为准，差额是路由改派导致的计价口径不同，不是 bug"
        )
    if unpriced:
        notes.append(
            f"以下模型在 CC Switch 定价表中没有匹配条目，其成本只能沿用账本原值："
            f"{', '.join(unpriced)}"
        )
    if (
        repriced is not None
        and report["totals"]["cost_usd"] > 0
        and abs(repriced - report["totals"]["cost_usd"])
        > 0.01 * report["totals"]["cost_usd"]
    ):
        notes.append(
            f"按 CC Switch 现行单价重算为 ${repriced:.4f}，与账本记账 "
            f"${report['totals']['cost_usd']:.4f} 不一致 —— 说明部分请求写入时"
            "用的定价已过期或缺失（账本保留写入时的价格）"
        )
    return report


def usage_report_path(plan_id: str) -> Path:
    """Where this plan's report lives (single source of truth for readers)."""
    return resolve_plans_dir() / plan_id / "usage_report.json"


def write_usage_report(
    plan_id: str, *, db_path: Optional[Path] = None
) -> Path:
    """Build the report and persist it to ``plans/<plan_id>/usage_report.json``."""
    report = build_usage_report(plan_id, db_path=db_path)
    path = usage_report_path(plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, report)
    return path


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m plan_usage <plan_id>`` — operator/verification entry."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m plan_usage <plan_id>", file=sys.stderr)
        return 2
    path = write_usage_report(args[0])
    print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI shim
    raise SystemExit(main())
