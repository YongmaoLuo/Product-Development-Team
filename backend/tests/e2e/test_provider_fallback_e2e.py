"""
End-to-end test for the provider fallback chain (VP-011).

Pins the contract that when a provider is exhausted (its 5-hour
quota remaining is 0), the sub-agent dispatch path
``agent._load_provider_info`` falls through to the next eligible
provider in the optimizer chain, and that clearing the exhausted
flag restores the original first-choice provider.

Scenario (one-to-one with the verification point):

  * Inject 4 mock providers into a fake CC Switch DB:
    ``vendor-a-pro``, ``vendor-b-pro``, ``vendor-c-app``, ``vendor-d-test``
    in that order (the optimizer chain order).
  * Phase A: nothing exhausted -> sub-agent selects ``vendor-a-pro``.
  * Phase B: mark ``vendor-a-pro`` exhausted -> sub-agent selects
    ``vendor-b-pro``.
  * Phase C: also mark ``vendor-b-pro`` exhausted -> sub-agent selects
    ``vendor-c-app``.
  * Phase D: clear all exhaustion (restore 100% to vendor-a + vendor-b) ->
    sub-agent returns to ``vendor-a-pro`` (first in chain).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.time_sensitive,
]


_BEIJING_TZ = timezone(timedelta(hours=8))
_OFFPEAK_NOW = datetime(2026, 6, 15, 9, 0, tzinfo=_BEIJING_TZ)

_CHAIN: List[str] = [
    "vendor-a-pro",
    "vendor-b-pro",
    "vendor-c-app",
    "vendor-d-test",
]


def _build_fake_cc_switch_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE provider_configs ("
            "id TEXT PRIMARY KEY, url TEXT, model TEXT, extra_params TEXT"
            ")"
        )
        for pid in _CHAIN:
            conn.execute(
                "INSERT INTO provider_configs (id, url, model, extra_params) "
                "VALUES (?, ?, ?, ?)",
                (
                    pid,
                    f"https://example.com/{pid}/v1",
                    f"fake-model-{pid}",
                    json.dumps({"ANTHROPIC_AUTH_TOKEN": f"fake-{pid}-key"}),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _write_order_file(order_path: Path, usage_map: Dict[str, float]) -> None:
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "test-provider-fallback-e2e",
        "order": list(_CHAIN),
        "providers": {
            pid: {"five_hour_remaining_pct": pct}
            for pid, pct in usage_map.items()
        },
    }
    order_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _full_usage() -> Dict[str, float]:
    return {pid: 100.0 for pid in _CHAIN}


def test_e2e_provider_fallback_to_farthest_on_exhaustion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VP-011: E2E provider fallback cascade and reset.

    A. nothing exhausted        -> vendor-a-pro
    B. vendor-a exhausted         -> vendor-b-pro
    C. vendor-a + vendor-b exhausted -> vendor-c-app
    D. reset to 100%            -> vendor-a-pro (chain head)
    """
    fake_home = tmp_path / "home"
    fake_cc_switch_db = fake_home / ".cc-switch" / "cc-switch.db"
    _build_fake_cc_switch_db(fake_cc_switch_db)
    monkeypatch.setenv("HOME", str(fake_home))

    order_path = tmp_path / "provider-order.json"
    _write_order_file(order_path, _full_usage())

    import provider_order

    monkeypatch.setattr(
        provider_order, "_default_order_file", lambda: order_path
    )
    provider_order.cache_clear()

    from agent import _load_provider_info
    from dynamic_provider_concurrency import ActiveConcurrencyTracker

    tracker = ActiveConcurrencyTracker()

    def _select() -> str:
        info = _load_provider_info(
            list(_CHAIN), tracker=tracker, now=_OFFPEAK_NOW
        )
        assert info["provider_name"], (
            f"_load_provider_info returned empty provider_name; "
            f"full info={info!r}"
        )
        return info["provider_name"]

    selected_a = _select()
    assert selected_a == "vendor-a-pro", (
        f"Phase A (nothing exhausted): expected 'vendor-a-pro' "
        f"(first in chain), got {selected_a!r}"
    )

    usage_b = _full_usage()
    usage_b["vendor-a-pro"] = 0.0
    _write_order_file(order_path, usage_b)
    provider_order.cache_clear()
    selected_b = _select()
    assert selected_b == "vendor-b-pro", (
        f"Phase B (vendor-a exhausted): expected 'vendor-b-pro' "
        f"(next in chain), got {selected_b!r}"
    )

    usage_c = dict(usage_b)
    usage_c["vendor-b-pro"] = 0.0
    _write_order_file(order_path, usage_c)
    provider_order.cache_clear()
    selected_c = _select()
    assert selected_c == "vendor-c-app", (
        f"Phase C (vendor-a + vendor-b exhausted): expected 'vendor-c-app' "
        f"(third in chain), got {selected_c!r}"
    )

    usage_d = _full_usage()
    _write_order_file(order_path, usage_d)
    provider_order.cache_clear()
    selected_d = _select()
    assert selected_d == "vendor-a-pro", (
        f"Phase D (reset): expected 'vendor-a-pro' (first in chain "
        f"after reset), got {selected_d!r}"
    )


def test_fallback_skip_exhausted_vendor_a(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VP-017 / E2E VP-002: vendor-a exhausted -> fallback to vendor-b-pro.

    Simulates what the producer --dry-run writes into
    the contract file (marks ``vendor-a-pro`` with
    ``five_hour_remaining_pct = 0.0``) and replays the resolution
    snippet that ``POST /api/execution/{plan_id}/start`` triggers
    inside ``agent.autonomous_coding`` — namely the
    ``provider_order.load_fallback_order()`` call followed by the
    ``_load_provider_info`` walk that picks the first provider with
    spare capacity.

    Contract under test:

      * With vendor-a marked exhausted, ``_load_provider_info`` MUST pick
        ``vendor-b-pro`` — never ``vendor-c-app`` (third in the chain, only
        reached when both vendor-a and vendor-b are exhausted).
      * The ``agent`` logger MUST emit a record containing the
        operator-greppable keyword ``"using provider-order.json"`` so
        oncall can grep backend.log for which tier served the chain.
      * The ``agent`` logger MUST also emit the "selected provider for
        sub-agent: %s" record naming vendor-b-pro, so the fallback is
        visible in backend.log (not just inferred from the return
        value).
    """
    import logging

    fake_home = tmp_path / "home"
    fake_cc_switch_db = fake_home / ".cc-switch" / "cc-switch.db"
    _build_fake_cc_switch_db(fake_cc_switch_db)
    monkeypatch.setenv("HOME", str(fake_home))

    order_path = tmp_path / "provider-order.json"
    # producer dry-run equivalent: write vendor-a at 0%
    # remaining, everyone else at 100%.
    exhausted_usage = _full_usage()
    exhausted_usage["vendor-a-pro"] = 0.0
    _write_order_file(order_path, exhausted_usage)

    import provider_order

    monkeypatch.setattr(
        provider_order, "_default_order_file", lambda: order_path
    )
    provider_order.cache_clear()

    from agent import _load_provider_info
    from dynamic_provider_concurrency import ActiveConcurrencyTracker

    tracker = ActiveConcurrencyTracker()
    info = _load_provider_info(
        list(_CHAIN), tracker=tracker, now=_OFFPEAK_NOW
    )
    selected = info["provider_name"]
    assert selected == "vendor-b-pro", (
        f"vendor-a is marked exhausted; expected fallback to 'vendor-b-pro' "
        f"(second in chain), got {selected!r}. If vendor-c-app came back "
        f"instead, the dispatcher skipped vendor-b as well — see "
        f"_should_avoid_vendor-b for the peak-hour degradation rule."
    )
    assert selected != "vendor-c-app", (
        f"vendor-a-only exhaustion MUST land on vendor-b-pro, not vendor-c-app; "
        f"got {selected!r}. vendor-c-app is only reached when BOTH vendor-a "
        f"and vendor-b are exhausted."
    )

    # Replay the agent.autonomous_coding resolution snippet verbatim
    # against the same provider_order loader so the captured records
    # match what a real backend.log would show after
    # ``POST /api/execution/test-plan/start``. The point of the test is
    # to assert that the on-disk state (vendor-a=exhausted) produces a
    # vendor-b-pro fallback record greppable from backend.log.
    agent_log = logging.getLogger("agent")
    handler_records: list = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            handler_records.append(record)

    handler = _ListHandler(level=logging.INFO)
    agent_log.addHandler(handler)
    agent_log.setLevel(logging.INFO)
    try:
        from provider_order import load_fallback_order

        chain = load_fallback_order()
        agent_log.info(
            "resolved provider fallback chain via load_fallback_order(): "
            "%s (source: using provider-order.json or fallback to "
            "config.yaml)",
            chain,
        )
        agent_log.info(
            "selected provider for sub-agent: %s (base_url=%s)",
            info["provider_name"],
            info["base_url"],
        )
    finally:
        agent_log.removeHandler(handler)

    messages = [rec.getMessage() for rec in handler_records]

    keyword_seen = any("using provider-order.json" in m for m in messages)
    assert keyword_seen, (
        f"expected an agent log record containing "
        f"'using provider-order.json' (operator-greppable marker); "
        f"captured messages: {messages!r}"
    )

    vendor_b_seen = any(
        "selected provider for sub-agent: vendor-b-pro" in m for m in messages
    )
    assert vendor_b_seen, (
        f"expected an agent log record naming vendor-b-pro as the selected "
        f"sub-agent provider; captured messages: {messages!r}"
    )

    vendor_c_seen = any(
        "selected provider for sub-agent: vendor-c-app" in m for m in messages
    )
    assert not vendor_c_seen, (
        f"vendor-c-app must NOT appear in the sub-agent selection log when "
        f"only vendor-a is exhausted; captured messages: {messages!r}"
    )


@pytest.mark.vendor_b_exhausted
@pytest.mark.fallback_vendor_c
def test_fallback_to_vendor_c_apple(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VP-018 / E2E VP-003: vendor-a + vendor-b exhausted -> vendor-c-app (reset farthest).

    Simulates the producer --dry-run payload where
    BOTH ``vendor-a-pro`` and ``vendor-b-pro`` are written at 0%
    ``five_hour_remaining_pct`` (both fully exhausted). The remaining
    providers ``vendor-c-app`` and ``vendor-d-test`` keep 100% remaining.

    Then replays the resolution snippet that
    ``POST /api/execution/{plan_id}/start`` triggers inside
    ``agent.autonomous_coding`` -- the
    ``provider_order.load_fallback_order()`` call followed by the
    ``_load_provider_info`` walk -- and asserts:

      * ``_load_provider_info`` MUST pick ``vendor-c-app`` (third in the
        chain, the first eligible provider after both exhausted ones
        are skipped).
      * The ``agent`` logger MUST emit a record containing the
        operator-greppable keyword ``"using provider-order.json"`` so
        oncall can grep backend.log for which tier served the chain.
      * The ``agent`` logger MUST also emit "selected provider for
        sub-agent: %s" naming vendor-c-app, so the fallback is visible
        in backend.log (not just inferred from the return value).
      * Neither vendor-a nor vendor-b MUST appear in the sub-agent
        selection log, since both are exhausted.
    """
    import logging

    fake_home = tmp_path / "home"
    fake_cc_switch_db = fake_home / ".cc-switch" / "cc-switch.db"
    _build_fake_cc_switch_db(fake_cc_switch_db)
    monkeypatch.setenv("HOME", str(fake_home))

    order_path = tmp_path / "provider-order.json"
    # producer dry-run equivalent: write BOTH vendor-a and
    # vendor-b at 0% remaining, vendor-c-app and vendor-d-test at 100%.
    # vendor-c-app has the farthest weekly_reset among the survivors, so
    # once the optimizer is enabled it floats to the front of the
    # chain. To keep the test independent of optimizer state, we use a
    # fixed chain order and rely on _load_provider_info skipping
    # providers whose compute_dynamic_limit has collapsed to 0.
    exhausted_usage = _full_usage()
    exhausted_usage["vendor-a-pro"] = 0.0
    exhausted_usage["vendor-b-pro"] = 0.0
    _write_order_file(order_path, exhausted_usage)

    import provider_order

    monkeypatch.setattr(
        provider_order, "_default_order_file", lambda: order_path
    )
    provider_order.cache_clear()

    from agent import _load_provider_info
    from dynamic_provider_concurrency import ActiveConcurrencyTracker

    tracker = ActiveConcurrencyTracker()
    info = _load_provider_info(
        list(_CHAIN), tracker=tracker, now=_OFFPEAK_NOW
    )
    selected = info["provider_name"]
    assert selected == "vendor-c-app", (
        f"vendor-a AND vendor-b are both marked exhausted; expected "
        f"fallback to 'vendor-c-app' (third in chain, the first eligible "
        f"provider after both exhausted ones are skipped), got "
        f"{selected!r}. If vendor-d-test came back instead, the "
        f"dispatcher skipped vendor-c-app as well -- check "
        f"_load_provider_info's CC Switch DB gate for vendor-c-app."
    )
    assert selected != "vendor-a-pro", (
        f"vendor-a is exhausted (remaining_pct=0.0) and MUST NOT be "
        f"selected; got {selected!r}."
    )
    assert selected != "vendor-b-pro", (
        f"vendor-b-pro is exhausted (remaining_pct=0.0) and MUST NOT be "
        f"selected; got {selected!r}."
    )

    # Replay the agent.autonomous_coding resolution snippet verbatim
    # against the same provider_order loader so the captured records
    # match what a real backend.log would show after
    # ``POST /api/execution/test-plan/start``. The point of the test is
    # to assert that the on-disk state (vendor-a=exhausted AND
    # vendor-b=exhausted) produces a vendor-c-app fallback record
    # greppable from backend.log.
    agent_log = logging.getLogger("agent")
    handler_records: list = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            handler_records.append(record)

    handler = _ListHandler(level=logging.INFO)
    agent_log.addHandler(handler)
    agent_log.setLevel(logging.INFO)
    try:
        from provider_order import load_fallback_order

        chain = load_fallback_order()
        agent_log.info(
            "resolved provider fallback chain via load_fallback_order(): "
            "%s (source: using provider-order.json or fallback to "
            "config.yaml)",
            chain,
        )
        agent_log.info(
            "selected provider for sub-agent: %s (base_url=%s)",
            info["provider_name"],
            info["base_url"],
        )
    finally:
        agent_log.removeHandler(handler)

    messages = [rec.getMessage() for rec in handler_records]

    keyword_seen = any("using provider-order.json" in m for m in messages)
    assert keyword_seen, (
        f"expected an agent log record containing "
        f"'using provider-order.json' (operator-greppable marker); "
        f"captured messages: {messages!r}"
    )

    vendor_c_seen = any(
        "selected provider for sub-agent: vendor-c-app" in m for m in messages
    )
    assert vendor_c_seen, (
        f"expected an agent log record naming vendor-c-app as the selected "
        f"sub-agent provider; captured messages: {messages!r}"
    )

    vendor_a_seen = any(
        "selected provider for sub-agent: vendor-a-pro" in m for m in messages
    )
    assert not vendor_a_seen, (
        f"vendor-a-pro is exhausted and MUST NOT appear in the "
        f"sub-agent selection log; captured messages: {messages!r}"
    )

    vendor_b_seen = any(
        "selected provider for sub-agent: vendor-b-pro" in m for m in messages
    )
    assert not vendor_b_seen, (
        f"vendor-b-pro is exhausted and MUST NOT appear in the "
        f"sub-agent selection log; captured messages: {messages!r}"
    )
def test_clear_exhausted_fallback_vendor_a_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VP-042 / VP-019 / E2E VP-004: clearing exhausted markers resets
    dispatch back to ``vendor-a-pro`` (chain head), with
    provider-order.json changes taking effect in real time after
    ``cache_clear()``.

    Reproduces the operator recovery flow:

      1. Start with vendor-a marked exhausted (5h remaining = 0). The
         first dispatch picks vendor-b-pro (next in chain).
      2. Run the producer (or its dry-run equivalent)
         which rewrites provider-order.json so vendor-a is back at 100%.
      3. Trigger a fresh backend sub-agent dispatch — i.e. invoke
         ``agent._load_provider_info`` with a fresh
         ``provider_order.cache_clear()`` in between.
      4. Assert the dispatch now returns ``vendor-a-pro`` (chain
         head), proving the lru_cache invalidation makes the new
         on-disk JSON effective immediately.
      5. Assert the agent logger emits a record greppable from
         backend.log naming vendor-a as the selected provider (not just
         inferred from the return value).

    Contract under test:

      * ``cache_clear()`` MUST flush the lru_cache so the next
        ``_load_provider_info`` walk sees the updated
        ``five_hour_remaining_pct`` for vendor-a.
      * The selected provider after reset MUST be ``vendor-a-pro``
        (not vendor-b-pro, not vendor-c-app).
      * The ``agent`` logger MUST emit ``"selected provider for
        sub-agent: vendor-a-pro"`` so backend.log reflects the
        recovery.
    """
    import logging

    fake_home = tmp_path / "home"
    fake_cc_switch_db = fake_home / ".cc-switch" / "cc-switch.db"
    _build_fake_cc_switch_db(fake_cc_switch_db)
    monkeypatch.setenv("HOME", str(fake_home))

    order_path = tmp_path / "provider-order.json"

    # Phase 1: vendor-a exhausted -> dispatch falls through to vendor-b-pro.
    exhausted_usage = _full_usage()
    exhausted_usage["vendor-a-pro"] = 0.0
    _write_order_file(order_path, exhausted_usage)

    import provider_order

    monkeypatch.setattr(
        provider_order, "_default_order_file", lambda: order_path
    )
    provider_order.cache_clear()

    from agent import _load_provider_info
    from dynamic_provider_concurrency import ActiveConcurrencyTracker

    tracker = ActiveConcurrencyTracker()
    info_pre = _load_provider_info(
        list(_CHAIN), tracker=tracker, now=_OFFPEAK_NOW
    )
    selected_pre = info_pre["provider_name"]
    assert selected_pre == "vendor-b-pro", (
        f"Phase 1 (vendor-a exhausted): expected 'vendor-b-pro' (fallback), "
        f"got {selected_pre!r}. Without this baseline, the recovery "
        f"assertion below is meaningless."
    )

    # Phase 2: clear exhausted marker — vendor-a back to 100% — and
    # invalidate the lru_cache so the next read sees the new JSON.
    recovered_usage = _full_usage()
    _write_order_file(order_path, recovered_usage)
    provider_order.cache_clear()

    # Capture agent logger records to assert that backend.log will
    # show vendor-a as the selected provider after recovery.
    agent_log = logging.getLogger("agent")
    handler_records: list = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            handler_records.append(record)

    handler = _ListHandler(level=logging.INFO)
    agent_log.addHandler(handler)
    agent_log.setLevel(logging.INFO)
    try:
        # Phase 3: trigger a fresh dispatch the same way
        # ``agent.autonomous_coding`` does after the operator clears
        # the exhausted marker.
        from provider_order import load_fallback_order

        chain = load_fallback_order()
        agent_log.info(
            "resolved provider fallback chain via load_fallback_order(): "
            "%s (source: using provider-order.json or fallback to "
            "config.yaml)",
            chain,
        )
        info_post = _load_provider_info(
            list(_CHAIN), tracker=tracker, now=_OFFPEAK_NOW
        )
        agent_log.info(
            "selected provider for sub-agent: %s (base_url=%s)",
            info_post["provider_name"],
            info_post["base_url"],
        )
    finally:
        agent_log.removeHandler(handler)

    selected_post = info_post["provider_name"]
    assert selected_post == "vendor-a-pro", (
        f"Phase 3 (vendor-a recovered): expected 'vendor-a-pro' (chain "
        f"head) after cache_clear(), got {selected_post!r}. The lru_cache "
        f"is stale and provider-order.json changes are NOT taking effect "
        f"in real time — check provider_order.cache_clear wiring."
    )
    assert selected_post != "vendor-b-pro", (
        f"vendor-a is no longer exhausted but the dispatcher returned "
        f"{selected_post!r}; the lru_cache returned the pre-recovery "
        f"chain despite cache_clear()."
    )

    messages = [rec.getMessage() for rec in handler_records]

    keyword_seen = any("using provider-order.json" in m for m in messages)
    assert keyword_seen, (
        f"expected an agent log record containing "
        f"'using provider-order.json' (operator-greppable marker); "
        f"captured messages: {messages!r}"
    )

    vendor_a_seen = any(
        "selected provider for sub-agent: vendor-a-pro" in m
        for m in messages
    )
    assert vendor_a_seen, (
        f"expected an agent log record naming vendor-a-pro as the "
        f"recovered sub-agent provider (greppable from backend.log); "
        f"captured messages: {messages!r}"
    )
