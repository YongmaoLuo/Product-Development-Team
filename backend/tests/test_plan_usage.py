"""Tests for the per-plan usage aggregator (``backend/plan_usage.py``).

The CC Switch ledger is faked with a small SQLite fixture carrying the
exact columns the aggregator reads, so these tests never touch the
operator's real ``~/.cc-switch/cc-switch.db``.

The plans tree is already redirected by the autouse
``isolated_plans_dir`` conftest fixture (it sets ``PDT_PLANS_DIR``), and
``usage_registry.repo_plans_dir`` delegates to
``config_paths.resolve_plans_dir`` — so registry writes and the report
output both land in ``tmp_path``.
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
CREATE TABLE model_pricing (
    model_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    input_cost_per_million TEXT NOT NULL,
    output_cost_per_million TEXT NOT NULL,
    cache_read_cost_per_million TEXT NOT NULL DEFAULT '0',
    cache_creation_cost_per_million TEXT NOT NULL DEFAULT '0'
);
"""

PROVIDER_VENDOR_D = "2392fb99-0000-4000-8000-000000000001"
PROVIDER_VENDOR_A = "0a3d1b26-0000-4000-8000-000000000002"


def _make_db(path: Path, rows, providers=(), pricing=()) -> Path:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO proxy_request_logs (session_id, data_source, provider_id,"
            " model, request_model, input_tokens, output_tokens, cache_read_tokens,"
            " cache_creation_tokens, total_cost_usd, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.executemany(
            "INSERT INTO providers (id, app_type, name) VALUES (?,?,?)",
            [(pid, "claude", name) for pid, name in providers],
        )
        conn.executemany(
            "INSERT INTO model_pricing (model_id, display_name,"
            " input_cost_per_million, output_cost_per_million,"
            " cache_read_cost_per_million, cache_creation_cost_per_million)"
            " VALUES (?,?,?,?,?,?)",
            pricing,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _row(
    session_id,
    *,
    data_source="proxy",
    provider_id=PROVIDER_VENDOR_D,
    model="vendor-d-lite",
    input_tokens=100,
    output_tokens=50,
    cache_read_tokens=0,
    cache_creation_tokens=0,
    cost="0.01",
    created_at=1_758_000_000,
):
    return (
        session_id, data_source, provider_id, model, model,
        input_tokens, output_tokens, cache_read_tokens,
        cache_creation_tokens, cost, created_at,
    )


def _record(session_id, *, scene="execution", task_id=None, plan_id="plan-u",
            usage=None, cost="0.004", num_turns=1, ok=True):
    with usage_registry.plan_usage_context(plan_id=plan_id, task_id=task_id):
        usage_registry.record_llm_call(
            session_id=session_id,
            scene=scene,
            ok=ok,
            usage=usage,
            cost_usd=cost,
            num_turns=num_turns,
        )


def test_aggregates_both_data_sources(tmp_path, monkeypatch):
    db = _make_db(
        tmp_path / "cc.db",
        [
            _row("s-proxy", data_source="proxy", input_tokens=1000,
                 output_tokens=200, cache_read_tokens=4000, cost="0.05"),
            _row("s-direct", data_source="session_log",
                 provider_id="_session", model="Vendor A-M3",
                 input_tokens=500, output_tokens=100, cost="0.01"),
        ],
        providers=[(PROVIDER_VENDOR_D, "Vendor D API")],
    )
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record("s-proxy", scene="prd")
    _record("s-direct", scene="execution", task_id="1-1")

    report = plan_usage.build_usage_report("plan-u")

    assert report["sessions"] == {
        "registry_entries": 2, "distinct_sessions": 2,
        "tracked": 2, "untracked": 0,
    }
    assert report["totals"]["requests"] == 2
    assert report["totals"]["new_input_tokens"] == 1500
    assert report["totals"]["output_tokens"] == 300
    assert report["totals"]["cache_read_tokens"] == 4000
    assert report["totals"]["total_tokens"] == 5800
    assert report["totals"]["cost_usd"] == pytest.approx(0.06)
    # cache_read / (input + cache_read) = 4000 / 5500
    assert report["totals"]["cache_hit_rate"] == pytest.approx(0.7273, abs=1e-4)

    sources = {item["key"]: item for item in report["by_data_source"]}
    assert set(sources) == {"proxy", "session_log"}
    assert sources["proxy"]["new_input_tokens"] == 1000
    assert sources["session_log"]["new_input_tokens"] == 500

    providers = {item["key"]: item for item in report["by_provider"]}
    assert providers["Vendor D API"]["requests"] == 1
    # synthetic session-log provider id resolves to a readable name
    assert providers["Claude (Session)"]["requests"] == 1

    assert {item["key"] for item in report["by_scene"]} == {"prd", "execution"}
    assert {item["key"] for item in report["by_task"]} == {
        "1-1", "(unattributed)"
    }


def test_null_data_source_treated_as_proxy(tmp_path, monkeypatch):
    """Pre-v9 rows store NULL; CC Switch reads them as 'proxy'."""
    db = _make_db(tmp_path / "cc.db", [_row("s-null")])
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE proxy_request_logs SET data_source = NULL")
    conn.commit()
    conn.close()
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record("s-null")

    report = plan_usage.build_usage_report("plan-u")
    assert [item["key"] for item in report["by_data_source"]] == ["proxy"]


def test_untracked_sessions_are_reported_not_dropped(tmp_path, monkeypatch):
    db = _make_db(tmp_path / "cc.db", [_row("s-known")])
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record("s-known", scene="prd")
    _record("s-dead", scene="repair_generation")  # no CC Switch row

    report = plan_usage.build_usage_report("plan-u")

    assert report["sessions"]["tracked"] == 1
    assert report["sessions"]["untracked"] == 1
    assert report["cross_check"]["verdict"] == "partial"
    unseen = report["cross_check"]["sessions_unseen_by_cc_switch"]
    assert [item["session_id"] for item in unseen] == ["s-dead"]
    assert unseen[0]["scenes"] == ["repair_generation"]


def test_mismatch_when_cc_switch_has_no_rows(tmp_path, monkeypatch):
    db = _make_db(tmp_path / "cc.db", [])
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record("s-orphan")

    report = plan_usage.build_usage_report("plan-u")
    assert report["cross_check"]["verdict"] == "mismatch"
    assert report["totals"]["requests"] == 0
    assert any("no rows" in note for note in report["cross_check"]["notes"])


def test_no_registry_entries_is_no_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_SWITCH_DB", str(tmp_path / "cc.db"))
    report = plan_usage.build_usage_report("plan-empty")
    assert report["cross_check"]["verdict"] == "no_data"
    assert report["totals"]["requests"] == 0
    assert report["pdt_side_account"]["attempts"] == 0


def test_missing_ledger_degrades_to_ac_side_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_SWITCH_DB", str(tmp_path / "does-not-exist.db"))
    _record("s-x", usage={"input_tokens": 10, "output_tokens": 5})

    report = plan_usage.build_usage_report("plan-u")
    assert report["cross_check"]["verdict"] == "ledger_unavailable"
    assert report["pdt_side_account"]["attempts"] == 1
    assert report["pdt_side_account"]["tokens"]["new_input_tokens"] == 10
    assert report["totals"]["requests"] == 0


def test_pdt_side_account_and_multi_turn_note(tmp_path, monkeypatch):
    db = _make_db(
        tmp_path / "cc.db",
        [
            _row("s-multi", input_tokens=900, output_tokens=100),
            _row("s-bad", input_tokens=50, output_tokens=10, cost="0.05"),
        ],
    )
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record(
        "s-multi",
        usage={
            "input_tokens": 300,
            "output_tokens": 40,
            "cache_read_input_tokens": 700,
            "cache_creation_input_tokens": 20,
        },
        cost="0.02",
        num_turns=3,
    )
    _record("s-bad", ok=False, cost=None)

    report = plan_usage.build_usage_report("plan-u")
    ac = report["pdt_side_account"]
    assert ac["attempts"] == 2
    assert ac["ok"] == 1 and ac["failed"] == 1
    assert ac["multi_turn_attempts"] == 1
    assert ac["tokens"] == {
        "new_input_tokens": 300,
        "output_tokens": 40,
        "cache_read_tokens": 700,
        "cache_creation_tokens": 20,
    }
    assert ac["cost_usd"] == pytest.approx(0.02)
    # CC Switch saw 0.01 + 0.05; the backend recorded 0.02 → delta 0.04
    assert report["cross_check"]["cost_delta_usd"] == pytest.approx(0.04, abs=1e-6)
    assert any("num_turns>1" in note for note in report["cross_check"]["notes"])
    # cost delta is explained rather than left as an unexplained mismatch
    assert any("以 CC Switch 为准" in note for note in report["cross_check"]["notes"])


def test_session_query_is_chunked(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_usage, "_SESSION_CHUNK", 2)
    sessions = [f"s-{i}" for i in range(5)]
    db = _make_db(tmp_path / "cc.db", [_row(sid) for sid in sessions])
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    for sid in sessions:
        _record(sid)

    report = plan_usage.build_usage_report("plan-u")
    assert report["totals"]["requests"] == 5
    assert report["sessions"]["tracked"] == 5


def test_write_usage_report_persists_json(tmp_path, monkeypatch):
    db = _make_db(tmp_path / "cc.db", [_row("s-1")])
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record("s-1")

    path = plan_usage.write_usage_report("plan-u")

    assert path == tmp_path / "plans" / "plan-u" / "usage_report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["plan_id"] == "plan-u"
    assert payload["totals"]["requests"] == 1


def test_by_day_bucket_is_sorted(tmp_path, monkeypatch):
    db = _make_db(
        tmp_path / "cc.db",
        [
            _row("s-a", created_at=1_758_000_000),  # 2025-09-17 UTC
            _row("s-b", created_at=1_758_086_400),  # +1 day
        ],
    )
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record("s-a")
    _record("s-b")

    report = plan_usage.build_usage_report("plan-u")
    days = [item["key"] for item in report["by_day"]]
    assert days == sorted(days)
    assert len(days) == 2


# ---------------------------------------------------------------------------
# Pricing — CC Switch's table is the authority
# ---------------------------------------------------------------------------

_VENDOR_D_PRICING = (
    "vendor-d-lite", "Vendor D Lite", "0.5", "2.0", "0.05", "0",
)


def test_pricing_block_comes_from_cc_switch_table(tmp_path, monkeypatch):
    db = _make_db(
        tmp_path / "cc.db",
        [_row("s-price", input_tokens=1_000_000, output_tokens=1_000_000,
              cache_read_tokens=1_000_000, cost="3.05")],
        providers=[(PROVIDER_VENDOR_D, "Vendor D API")],
        pricing=[_VENDOR_D_PRICING],
    )
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record("s-price")

    report = plan_usage.build_usage_report("plan-u")

    assert report["pricing"]["source"] == "cc_switch.model_pricing"
    assert report["pricing"]["unit"] == "per_million_tokens"
    entry = report["pricing"]["models"][0]
    assert entry["model"] == "vendor-d-lite"
    assert entry["match"] == "exact"
    assert entry["display_name"] == "Vendor D Lite"
    assert entry["input_cost_per_million"] == pytest.approx(0.5)
    assert entry["cache_read_cost_per_million"] == pytest.approx(0.05)
    # headline cost stays the ledger's number — CC Switch is the bill
    assert report["totals"]["cost_usd"] == pytest.approx(3.05)
    assert report["totals"]["cost_source"] == "cc_switch"


def test_repriced_cost_uses_current_cc_switch_rates(tmp_path, monkeypatch):
    db = _make_db(
        tmp_path / "cc.db",
        [_row("s-reprice", input_tokens=1_000_000, output_tokens=1_000_000,
              cache_read_tokens=1_000_000, cost="3.05")],
        pricing=[_VENDOR_D_PRICING],
    )
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record("s-reprice")

    report = plan_usage.build_usage_report("plan-u")

    # 1M @ $0.5 + 1M @ $2.0 + 1M @ $0.05 = $2.55
    assert report["totals"]["repriced_cost_usd"] == pytest.approx(2.55)
    # 3.05 vs 2.55 = 16% off → the stale-pricing note fires
    assert any("重算" in note for note in report["cross_check"]["notes"])


def test_unpriced_model_is_flagged_not_priced_at_zero(tmp_path, monkeypatch):
    db = _make_db(
        tmp_path / "cc.db",
        [_row("s-unknown", model="mystery-model-9", cost="0")],
        pricing=[_VENDOR_D_PRICING],
    )
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record("s-unknown")

    report = plan_usage.build_usage_report("plan-u")

    entry = report["pricing"]["models"][0]
    assert entry["match"] == "none"
    assert entry["input_cost_per_million"] is None
    # "unknown" must not be reported as "free"
    assert report["totals"]["repriced_cost_usd"] is None
    assert any("没有匹配条目" in note for note in report["cross_check"]["notes"])


def test_fuzzy_pricing_match_is_labelled(tmp_path, monkeypatch):
    db = _make_db(
        tmp_path / "cc.db",
        [_row("s-fuzzy", model="Vendor A-M3-2026", input_tokens=1000, cost="0.01")],
        pricing=[("Vendor A-M3", "Vendor A M3", "0.3", "1.2", "0.03", "0")],
    )
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    _record("s-fuzzy")

    report = plan_usage.build_usage_report("plan-u")

    entry = report["pricing"]["models"][0]
    assert entry["match"] == "fuzzy"
    assert entry["model_id"] == "Vendor A-M3"


def test_report_shape_is_stable_without_data(tmp_path, monkeypatch):
    """Consumers (the UI) read cost_source / repriced_cost_usd unconditionally."""
    monkeypatch.setenv("CC_SWITCH_DB", str(tmp_path / "cc.db"))

    report = plan_usage.build_usage_report("plan-shape")

    assert report["cross_check"]["verdict"] == "no_data"
    assert report["totals"]["cost_source"] == "cc_switch"
    assert report["totals"]["repriced_cost_usd"] is None
    assert report["pricing"] == {
        "source": "cc_switch.model_pricing",
        "currency": "USD",
        "unit": "per_million_tokens",
        "models": [],
    }
