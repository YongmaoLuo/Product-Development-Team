"""API contract for per-plan usage stats (2026-09-21).

Covers ``GET /api/plan/{plan_id}/usage`` and the compact ``usage`` block
that ``GET /api/plan/{plan_id}/summary`` carries.

The CC Switch ledger is faked with a small SQLite fixture (same columns
the aggregator reads), so these tests never touch the operator's real
``~/.cc-switch/cc-switch.db``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import plan_usage
import usage_registry
from server import app

client = TestClient(app)

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


@pytest.fixture
def plans_root(tmp_path, monkeypatch):
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("server.PLANS_DIR", root)
    return root


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Fake CC Switch DB with one tracked session, wired via CC_SWITCH_DB."""
    db = tmp_path / "cc.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO proxy_request_logs (session_id, data_source, provider_id,"
            " model, request_model, input_tokens, output_tokens, cache_read_tokens,"
            " cache_creation_tokens, total_cost_usd, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("s-live", "proxy", "prov-1", "vendor-d-lite", "vendor-d-lite",
             1000, 250, 3000, 0, "0.07", 1_758_000_000),
        )
        conn.execute(
            "INSERT INTO providers (id, app_type, name) VALUES ('prov-1','claude','Vendor D API')"
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("CC_SWITCH_DB", str(db))
    return db


def _seed_plan(root: Path, plan_id: str) -> Path:
    plan_dir = root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text("{}", encoding="utf-8")
    return plan_dir


def _record(session_id: str, plan_id: str, *, scene: str = "execution") -> None:
    with usage_registry.plan_usage_context(plan_id=plan_id, task_id="1-1"):
        usage_registry.record_llm_call(
            session_id=session_id,
            scene=scene,
            ok=True,
            usage={"input_tokens": 900, "output_tokens": 240},
            cost_usd="0.06",
            num_turns=2,
        )


def test_usage_endpoint_computes_on_demand(plans_root, ledger):
    _seed_plan(plans_root, "plan-api")
    _record("s-live", "plan-api")

    response = client.get("/api/plan/plan-api/usage")

    assert response.status_code == 200
    payload = response.json()
    assert payload["plan_id"] == "plan-api"
    assert payload["totals"]["requests"] == 1
    assert payload["totals"]["new_input_tokens"] == 1000
    assert payload["totals"]["output_tokens"] == 250
    assert payload["totals"]["cache_read_tokens"] == 3000
    assert payload["totals"]["cost_usd"] == pytest.approx(0.07)
    assert payload["by_provider"][0]["key"] == "Vendor D API"
    assert payload["sessions"] == {
        "registry_entries": 1, "distinct_sessions": 1,
        "tracked": 1, "untracked": 0,
    }
    assert payload["cross_check"]["verdict"] == "consistent"


def test_usage_endpoint_prefers_cached_report(plans_root, ledger):
    plan_dir = _seed_plan(plans_root, "plan-cached")
    cached = {"plan_id": "plan-cached", "sentinel": True, "totals": {"requests": 0}}
    (plan_dir / "usage_report.json").write_text(
        json.dumps(cached), encoding="utf-8"
    )

    response = client.get("/api/plan/plan-cached/usage")

    assert response.status_code == 200
    assert response.json()["sentinel"] is True


def test_usage_endpoint_refresh_recomputes(plans_root, ledger):
    plan_dir = _seed_plan(plans_root, "plan-refresh")
    _record("s-live", "plan-refresh")
    (plan_dir / "usage_report.json").write_text(
        json.dumps({"plan_id": "plan-refresh", "sentinel": True}), encoding="utf-8"
    )

    response = client.get("/api/plan/plan-refresh/usage?refresh=true")

    assert response.status_code == 200
    payload = response.json()
    assert "sentinel" not in payload
    assert payload["totals"]["requests"] == 1
    # the refresh persisted the fresh report for later cached reads
    persisted = json.loads((plan_dir / "usage_report.json").read_text(encoding="utf-8"))
    assert persisted["totals"]["requests"] == 1


def test_usage_endpoint_404_for_unknown_plan(plans_root, ledger):
    response = client.get("/api/plan/does-not-exist/usage")
    assert response.status_code == 404


def test_usage_endpoint_degrades_when_ledger_missing(plans_root, tmp_path, monkeypatch):
    _seed_plan(plans_root, "plan-nodb")
    _record("s-live", "plan-nodb")
    monkeypatch.setenv("CC_SWITCH_DB", str(tmp_path / "missing.db"))

    response = client.get("/api/plan/plan-nodb/usage")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cross_check"]["verdict"] == "ledger_unavailable"
    assert payload["pdt_side_account"]["attempts"] == 1


def test_summary_carries_compact_usage_block(plans_root, ledger):
    plan_dir = _seed_plan(plans_root, "plan-sum")
    _record("s-live", "plan-sum")
    report = plan_usage.build_usage_report("plan-sum")
    (plan_dir / "usage_report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )

    response = client.get("/api/plan/plan-sum/summary")

    assert response.status_code == 200
    usage = response.json()["usage"]
    assert usage is not None
    assert usage["totals"]["requests"] == 1
    assert usage["sessions"]["tracked"] == 1
    assert usage["cross_check"]["verdict"] == "consistent"


def test_summary_usage_block_is_none_without_report(plans_root, ledger):
    _seed_plan(plans_root, "plan-norep")

    response = client.get("/api/plan/plan-norep/summary")

    assert response.status_code == 200
    assert response.json()["usage"] is None


def test_refresh_hook_writes_report(plans_root, ledger):
    """The execution/verification hook path: refresh persists the report."""
    import server

    _seed_plan(plans_root, "plan-hook")
    _record("s-live", "plan-hook")

    server._refresh_usage_report("plan-hook", block=True)

    path = plans_root / "plan-hook" / "usage_report.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["totals"]["requests"] == 1


def test_refresh_hook_swallows_aggregation_failure(plans_root, monkeypatch):
    """Accounting must never raise into the executor/verification path."""
    import server

    _seed_plan(plans_root, "plan-boom")

    def _boom(_plan_id, **_kwargs):
        raise RuntimeError("CC Switch DB exploded")

    monkeypatch.setattr(plan_usage, "write_usage_report", _boom)
    server._refresh_usage_report("plan-boom", block=True)  # must not raise

    assert not (plans_root / "plan-boom" / "usage_report.json").exists()
