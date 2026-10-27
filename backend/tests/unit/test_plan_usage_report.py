"""The usage report's per-VP / per-round split (2026-10-26).

``build_usage_report`` already splits a plan's spend by scene, by
task, by provider, by model and by day. It could not split it by the
two coordinates that decide *what to optimise*: which verification
point (``vp_id``) and which round. Without them every token the
registry could not name landed in one ``(unattributed)`` bucket, and
the question the report exists to answer — "which VP is burning the
budget, and is it the second round that burns it?" — had no answer.

The rules pinned here:

* an entry that names a VP and a round is split by them;
* an entry from BEFORE those fields existed (a legacy JSONL line with
  no ``vp_id`` / ``round``) is not an error — it lands in
  ``(unattributed)``, and the report still builds;
* the report's shape is stable: ``by_vp`` and ``by_round`` exist even
  when there is nothing to report.

The CC Switch ledger is faked with a small SQLite fixture carrying the
exact columns the aggregator reads, so these tests never touch the
operator's real ``~/.cc-switch/cc-switch.db``. The plans tree is
redirected by the autouse ``isolated_plans_dir`` conftest fixture (it
sets ``PDT_PLANS_DIR``), and ``usage_registry.repo_plans_dir``
delegates to ``config_paths.resolve_plans_dir`` — so the registry
writes and the report output both land in ``tmp_path``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import plan_usage
import usage_registry

_SCHEMA = """
CREATE TABLE proxy_request_logs (
    session_id TEXT,
    data_source TEXT DEFAULT 'proxy',
    provider_id TEXT,
    model TEXT,
    request_model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd TEXT NOT NULL DEFAULT '0',
    created_at INTEGER NOT NULL
);
CREATE TABLE providers (
    id TEXT NOT NULL,
    app_type TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (id, app_type)
);
"""


def _make_db(path: Path, rows) -> Path:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO proxy_request_logs (session_id, data_source,"
            " provider_id, model, request_model, input_tokens, output_tokens,"
            " cache_read_tokens, cache_creation_tokens, total_cost_usd,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _row(session_id, *, input_tokens=100, output_tokens=50, cost="0.01"):
    return (
        session_id, "proxy", "vendor-d", "vendor-d-lite", "vendor-d-lite",
        input_tokens, output_tokens, 0, 0, cost, 1_758_000_000,
    )


def _record(session_id, *, vp_id=None, round=None, plan_id="plan-a",
            scene="verification"):
    """One registry entry, with the attribution coordinates bound."""
    with usage_registry.plan_usage_context(
        plan_id=plan_id, vp_id=vp_id, round=round
    ):
        usage_registry.record_llm_call(
            session_id=session_id,
            scene=scene,
            ok=True,
            usage={"input_tokens": 10, "output_tokens": 5},
            cost_usd="0.004",
            num_turns=1,
        )


def _write_legacy_entries(plan_id, entries) -> None:
    """Write JSONL lines in the PRE-attribution shape (no vp_id / round).

    These are the lines a plan that ran before 2026-10-26 already has
    on disk — the report has to read them without blowing up. Appended,
    so a plan that also has attributed entries keeps both.
    """
    path = usage_registry.repo_plans_dir() / plan_id / "llm_sessions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _legacy_entry(session_id, *, scene="verification"):
    return {
        "ts": "2026-09-21T00:00:00+00:00",
        "plan_id": "plan-legacy",
        "task_id": "1-1",
        "session_id": session_id,
        "scene": scene,
        "provider": "vendor-d",
        "model": "vendor-d-lite",
        "ok": True,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "cost_usd": "0.004",
        "num_turns": 1,
        "duration_ms": 100,
        "error": None,
    }


def test_report_splits_by_vp_and_round(tmp_path, monkeypatch):
    """Attributed entries land in their own VP / round buckets."""
    db = _make_db(
        tmp_path / "cc.db",
        [
            _row("s-vp7-r1", input_tokens=1000, output_tokens=200),
            _row("s-vp9-r2", input_tokens=300, output_tokens=50),
        ],
    )
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record("s-vp7-r1", vp_id="VP-7", round=1)
    _record("s-vp9-r2", vp_id="VP-9", round=2)

    report = plan_usage.build_usage_report("plan-a")

    assert set(report["by_vp"]) == {"VP-7", "VP-9"}
    assert report["by_vp"]["VP-7"]["total_tokens"] == 1200
    assert report["by_vp"]["VP-7"]["requests"] == 1
    assert report["by_vp"]["VP-9"]["total_tokens"] == 350

    # the round key is addressable as the number and as its string form
    assert set(report["by_round"]) == {"1", "2"}
    assert report["by_round"]["1"]["total_tokens"] == 1200
    assert report["by_round"][2]["total_tokens"] == 350

    # the totals are untouched by the split
    assert report["totals"]["total_tokens"] == 1550


def test_legacy_rows_unattributed(tmp_path, monkeypatch):
    """Pre-attribution entries all land in (unattributed) — no crash."""
    db = _make_db(
        tmp_path / "cc.db",
        [
            _row("s-old-1", input_tokens=100, output_tokens=50),
            _row("s-old-2", input_tokens=10, output_tokens=5),
        ],
    )
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _write_legacy_entries(
        "plan-legacy",
        [_legacy_entry("s-old-1"), _legacy_entry("s-old-2", scene="execution")],
    )

    report = plan_usage.build_usage_report("plan-legacy")

    assert set(report["by_vp"]) == {"(unattributed)"}
    assert set(report["by_round"]) == {"(unattributed)"}
    # 150 tokens (100 in + 50 out) + 15 tokens (10 in + 5 out)
    assert report["by_vp"]["(unattributed)"]["total_tokens"] == 165
    assert report["by_vp"]["(unattributed)"]["requests"] == 2
    assert report["by_round"]["(unattributed)"]["total_tokens"] == 165
    # the report is otherwise intact
    assert report["totals"]["requests"] == 2
    assert report["cross_check"]["verdict"] == "consistent"


def test_mixed_attributed_and_legacy_rows(tmp_path, monkeypatch):
    """An attributed entry and a legacy one land in different buckets."""
    db = _make_db(
        tmp_path / "cc.db",
        [_row("s-new", input_tokens=100), _row("s-old", input_tokens=50)],
    )
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record("s-new", plan_id="plan-mixed", vp_id="VP-7", round=1)
    _write_legacy_entries(
        "plan-mixed", [_legacy_entry("s-old")]
    )

    report = plan_usage.build_usage_report("plan-mixed")

    assert set(report["by_vp"]) == {"VP-7", "(unattributed)"}
    assert report["by_vp"]["VP-7"]["total_tokens"] == 150
    assert report["by_vp"]["(unattributed)"]["total_tokens"] == 100
    assert set(report["by_round"]) == {"1", "(unattributed)"}


def test_by_vp_and_by_round_exist_without_data(tmp_path, monkeypatch):
    """The shape is stable: the keys exist even with nothing to report."""
    monkeypatch.setenv("CC_SWITCH_DB", str(tmp_path / "cc.db"))

    report = plan_usage.build_usage_report("plan-empty")

    assert report["by_vp"] == {}
    assert report["by_round"] == {}
    assert report["cross_check"]["verdict"] == "no_data"


def test_by_vp_and_by_round_exist_when_the_ledger_is_missing(
    tmp_path, monkeypatch
):
    """A missing ledger degrades the split to empty, not to an error."""
    monkeypatch.setenv("CC_SWITCH_DB", str(tmp_path / "does-not-exist.db"))
    _record("s-x", plan_id="plan-ledger-gone", vp_id="VP-7", round=1)

    report = plan_usage.build_usage_report("plan-ledger-gone")

    assert report["cross_check"]["verdict"] == "ledger_unavailable"
    assert report["by_vp"] == {}
    assert report["by_round"] == {}
