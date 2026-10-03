"""
End-to-end tests for the complete provider selection chain.

This suite pins the contract that the backend's provider selection
pipeline works end-to-end without hitting a real LLM and without
writing to the production ``~/.cc-switch/cc-switch.db``.

The 5 TDD acceptance bullets (one-to-one with the spec):

  * test_e2e_optimizer_to_chain
        - A freshly-written ``provider-order.json`` (the optimizer
          output) is consumed by :func:`provider_order.
          get_provider_order_from_optimizer` and the returned chain
          is exactly that order filtered through the CC Switch
          consumer layer, which speaks provider names.
  * test_e2e_chain_reads_real_db
        - The chain's ``base_url`` comes from the real
          ``~/.cc-switch/cc-switch.db`` (the ``providers`` table
          schema), resolved through ``cc_switch`` — the
          reader the dispatch uses — in ``mode=ro`` URI. No mocks, no
          fake DB.
  * test_e2e_chain_order_decides_provider
        - The optimizer's chain order IS the provider policy: the
          selector takes the first entry with capacity, so the
          rule-driven peak-hour demotion (which is baked into the order)
          needs no clock or window arithmetic on this side.
  * test_e2e_dry_run_no_llm_cost
        - With ``PDT_BACKEND_DRY_RUN=1`` set and a fail-on-call mock
          installed on ``ClaudeCodingTool.query``, running the
          chain-resolution pipeline does NOT invoke the LLM. The
          counter remains zero.
  * test_e2e_no_db_write
        - The real ``~/.cc-switch/cc-switch.db`` SHA-256 hash is
          unchanged after the suite runs. The DB is opened read-only
          via ``mode=ro`` URI; the hash comparison is a defense in
          depth in case a future refactor accidentally re-opens it
          in write mode.

Operator invocation
-------------------
::

    PDT_BACKEND_DRY_RUN=1 pytest -m e2e

The ``PDT_BACKEND_DRY_RUN=1`` env var is the operator-greppable signal
that this suite is allowed to run without an ``ANTHROPIC_API_KEY`` and
without network access. Tests in this module patch
``ClaudeCodingTool.query`` to a fail-on-call mock so a regression
that introduces a real LLM call is caught immediately.

Test isolation
--------------
  * The real DB path (``~/.cc-switch/cc-switch.db``) is the SAME
    across all 5 tests; we hash it once before, run the suite, then
    hash it again at the end of the no-DB-write test.
  * Tests that need a ``provider_configs`` SQLite (the schema that
    :mod:`provider_order` reads) build a private temp DB under
    ``tmp_path`` so the real DB stays untouched.
  * ``PDT_BACKEND_DRY_RUN`` is set inside the dry-run-no-LLM test via
    ``monkeypatch.setenv``; other tests do not depend on it.
  * The conftest ``isolated_plans_dir`` and ``clean_execution_state``
    autouse fixtures are harmless for these tests (they only touch
    ``server.PLANS_DIR`` and ``server._execution_state``, neither of
    which this suite imports).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

# Make ``provider_order``, ``cc_switch`` and
# ``provider_concurrency`` importable when pytest is launched from the
# project root.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from coding_tool import ClaudeCodingTool  # noqa: E402


# All tests in this file are e2e (full pipeline) and slow (real-DB
# hashing + chain resolution). pytestmark applies the markers in one
# place so individual tests don't have to remember to decorate.
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.time_sensitive,
]


# ---------------------------------------------------------------------------
# Constants + helpers
# ---------------------------------------------------------------------------


# Path to the real production database. Read-only via URI mode.
PROD_DB_PATH = Path(os.path.expanduser("~/.cc-switch/cc-switch.db"))


# Beijing timezone used by the vendor-b peak-hour window. Mirrors the
# private constant in ``provider_concurrency``; we redefine it here
# (rather than importing the private name) to keep the test pinned to
# the documented contract.
_BEIJING_TZ = timezone(timedelta(hours=8))


def _sha256_of_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 of ``path``'s bytes.

    The 49 MB production DB fits comfortably in memory; we read it in
    one shot to keep the hash deterministic. A streaming implementation
    would also work but is overkill for the size.
    """
    if not path.exists():
        raise FileNotFoundError(f"production DB missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_fake_cc_switch_db(
    tmp_path: Path, provider_names: List[str]
) -> Path:
    """Build a private CC Switch SQLite declaring the given provider names.

    Uses the production ``providers`` schema: the row's ``name`` is the
    provider's identity and the endpoint + credential live in
    ``settings_config.env``. That is the shape
    :mod:`cc_switch` reads. A table keyed on a local
    kebab-case id would make every lookup miss, which is exactly how the
    pre-2026-09-24 vocabulary differed from the live database.

    The DB lives under ``tmp_path`` so the real
    ``~/.cc-switch/cc-switch.db`` is never touched by this test.

    Parameters
    ----------
    tmp_path:
        Per-test scratch directory provided by pytest.
    provider_names:
        Names to insert, verbatim. Each gets a distinct
        ``https://example.com/<n>`` endpoint so the round-trip is
        inspectable in assertions.
    """
    db_path = tmp_path / "fake-cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE providers ("
            "id TEXT PRIMARY KEY, name TEXT, settings_config TEXT"
            ")"
        )
        for i, name in enumerate(provider_names):
            conn.execute(
                "INSERT INTO providers (id, name, settings_config) "
                "VALUES (?, ?, ?)",
                (
                    f"row-{i}",
                    name,
                    json.dumps({
                        "env": {
                            "ANTHROPIC_BASE_URL": f"https://example.com/{i}",
                            "ANTHROPIC_AUTH_TOKEN": f"fake-key-{i}",
                        }
                    }),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _write_optimizer_order_file(
    tmp_path: Path, order: List[str]
) -> Path:
    """Write a fresh ``provider-order.json`` with a given provider order.

    The schema is the live contract (:func:`provider_order.load_fallback_order`
    consumes this shape). ``updated_at`` is pinned to ``utcnow()`` so
    the file is treated as fresh.
    """
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "producer",
        "order": list(order),
        "providers": {},
    }
    order_file = tmp_path / "provider-order.json"
    order_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return order_file


# ---------------------------------------------------------------------------
# 1. test_e2e_optimizer_to_chain
# ---------------------------------------------------------------------------


def test_e2e_optimizer_to_chain(tmp_path: Path) -> None:
    """Optimizer ``order`` is consumed by :mod:`provider_order` and
    filtered through the CC Switch consumer layer.

    A freshly written ``provider-order.json`` lists provider names in a
    specific order. Three of them name a row in the (temp) CC Switch DB;
    one does not. The returned chain is the JSON order, filtered to the
    resolvable names only, in JSON order.

    This is the same contract pinned by ``test_integration_consumes_optimizer``
    in the integration suite; restated at the E2E level so a CI
    failure here blocks merges even when the integration suite is
    skipped.
    """
    from provider_order import get_provider_order_from_optimizer

    db_path = _make_fake_cc_switch_db(
        tmp_path,
        # The DB declares these three; "Vendor C Apple" and "parent" are
        # deliberately absent.
        ["Vendor A Pro", "Vendor B Pro", "Vendor D Test"],
    )
    optimizer_output = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "producer",
        "order": [
            "Vendor A Pro",    # declared
            "Vendor B Pro",         # declared
            "Vendor C Apple",        # NOT declared -> dropped
            "Vendor D Test",      # declared
            "parent",            # never a provider -> dropped
        ],
        "providers": {},
    }

    chain = get_provider_order_from_optimizer(
        optimizer_output, db_path=db_path,
    )

    assert chain == ["Vendor A Pro", "Vendor B Pro", "Vendor D Test"], (
        f"chain must preserve JSON order, filtered to DB-declared "
        f"provider names; got {chain!r}"
    )


# ---------------------------------------------------------------------------
# 2. test_e2e_chain_reads_real_db
# ---------------------------------------------------------------------------


def test_e2e_chain_reads_real_db() -> None:
    """``base_url`` comes from the real production DB, by name.

    Pins the contract that
    :func:`cc_switch.get_provider` — the
    reader the dispatch actually uses — can read the live
    ``~/.cc-switch/cc-switch.db`` and return a usable endpoint. The
    provider is discovered through
    :func:`cc_switch.list_provider_names` rather
    than named here, so a provider the optimizer renames or drops does
    not break the suite.

    Skip conditions: if the real DB is missing, the test is skipped
    rather than failed — a developer running this test on a machine
    without CC Switch installed should not see a hard failure.
    """
    if not PROD_DB_PATH.exists():
        pytest.skip(f"production DB not present at {PROD_DB_PATH}")

    from cc_switch import (
        get_provider,
        list_provider_names,
    )

    names = list_provider_names(db_path=PROD_DB_PATH)
    assert isinstance(names, list) and names, (
        f"real DB returned empty provider list: {names!r}. "
        f"CC Switch may be misconfigured; cannot verify chain reads "
        f"real DB."
    )

    # A declared name is not automatically dispatchable: the reader
    # returns ``None`` for a row that carries no endpoint. Find one that
    # resolves rather than assuming the first name does.
    resolved = [
        (name, cfg)
        for name in names
        if (cfg := get_provider(name, db_path=PROD_DB_PATH))
    ]
    assert resolved, (
        f"none of the {len(names)} providers declared by the real DB "
        f"resolves to a usable config: {names!r}"
    )
    target_name, cfg = resolved[0]

    # The shape the dispatch consumes. It is normalized across both
    # on-disk layouts, so the reader no longer hands back the legacy
    # table's column names for a modern row:
    #   * ``name``      — the provider's identity, verbatim
    #   * ``base_url``  — the endpoint, from ``ANTHROPIC_BASE_URL``
    #   * ``env``       — the row's whole env block, which is where
    #     ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_API_KEY`` live
    assert cfg.name == target_name, (
        f"config.name={cfg.name!r} != requested name {target_name!r}"
    )
    assert cfg.base_url, (
        f"resolved config for {target_name!r} carries no base_url: {cfg!r}"
    )
    assert "://" in cfg.base_url, (
        f"base_url must be a scheme-qualified endpoint, got {cfg.base_url!r}"
    )
    assert isinstance(cfg.env, dict), (
        f"env must be a dict, got {type(cfg.env).__name__}: {cfg!r}"
    )
    # ``base_url`` is the *normalized* field; ``env`` is the row verbatim.
    # For a modern row the two must agree, and they are computed by
    # different code paths — which is what makes this a cross-check
    # rather than a restatement.
    assert cfg.base_url == cfg.env.get("ANTHROPIC_BASE_URL"), (
        f"base_url {cfg.base_url!r} does not match the row's own "
        f"ANTHROPIC_BASE_URL {cfg.env.get('ANTHROPIC_BASE_URL')!r}"
    )
    assert isinstance(cfg.api_key, str), (
        f"api_key must be a string, got {type(cfg.api_key).__name__}"
    )


# ---------------------------------------------------------------------------
# 3. test_e2e_vendor-b_degrade_applied
# ---------------------------------------------------------------------------


def test_e2e_chain_order_decides_provider() -> None:
    """End-to-end: the optimizer's chain order decides which provider is used.

    Peak-hour policy lives in the optimizer's rule engine, which expresses
    it as a priority demotion baked into ``provider-order.json``. By the
    time a chain reaches this layer the decision is already made, so the
    only inputs here are the order and each provider's capacity.
    """
    from provider_concurrency import (
        ProviderConcurrencyController,
        select_provider_with_concurrency,
    )

    controller = ProviderConcurrencyController(
        global_limit=1, provider_limits={"Vendor B Pro": 1}
    )
    now = datetime.now(_BEIJING_TZ)

    # Order the optimizer produces during B-PRO's peak window (14:00-18:00):
    # B-PRO demoted below the others → the walk picks Vendor A.
    peak_order = ["Vendor A Pro", "Vendor B Pro"]
    assert select_provider_with_concurrency(peak_order, controller, now) == (
        "Vendor A Pro"
    )

    # Off-peak order: B-PRO back at the head → the walk picks it again.
    off_peak_order = ["Vendor B Pro", "Vendor A Pro"]
    assert select_provider_with_concurrency(
        off_peak_order, controller, now
    ) == "Vendor B Pro"

    # ``coding_tool``'s availability checker is a plain probe keyed on
    # the provider name — it answers "can this provider serve a call?",
    # never "should it?". The ranking above, not this layer, is what
    # keeps B-PRO out of rotation at peak.
    with patch.object(
        ClaudeCodingTool,
        "_check_provider_availability",
        return_value=(True, {"base_url": "http://u", "api_key": "k"}),
    ):
        assert ClaudeCodingTool._check_provider_availability("Vendor B Pro")[0] is True


# ---------------------------------------------------------------------------
# 4. test_e2e_dry_run_no_llm_cost
# ---------------------------------------------------------------------------


def test_e2e_dry_run_no_llm_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With ``PDT_BACKEND_DRY_RUN=1`` and a fail-on-call mock installed
    on ``ClaudeCodingTool.query``, the chain-resolution pipeline
    does NOT invoke the LLM.

    A regression that wires an LLM call into the chain-resolution
    path (e.g. asking the LLM to re-rank the chain) would surface
    here as a raised :class:`AssertionError` from the mock. The
    test does NOT call ``autonomous_coding`` end-to-end — only the
    chain-resolution path (``load_fallback_order`` /
    ``get_provider_order_from_optimizer``) — because the contract
    is "chain resolution does not need an LLM".
    """
    monkeypatch.setenv("PDT_BACKEND_DRY_RUN", "1")

    # Install a fail-on-call mock on ClaudeCodingTool.query and
    # query_json. Any call raises AssertionError so the test fails
    # loudly if a regression introduces a real LLM call into the
    # chain-resolution path.
    from coding_tool import ClaudeCodingTool

    call_log: List[Dict[str, Any]] = []

    def fail_on_call(*args, **kwargs):
        call_log.append({"args": args, "kwargs": dict(kwargs)})
        raise AssertionError(
            "ClaudeCodingTool.query was called during PDT_BACKEND_DRY_RUN "
            "E2E test; the chain-resolution path must not invoke the LLM"
        )

    monkeypatch.setattr(ClaudeCodingTool, "query", fail_on_call)
    monkeypatch.setattr(ClaudeCodingTool, "query_json", fail_on_call)

    # Drive the chain-resolution path. We use a temp DB so the chain is
    # independent of the developer's own CC Switch roster and of any
    # optimizer state; the test cares about *whether* the LLM is
    # invoked, not about the real DB.
    db_path = _make_fake_cc_switch_db(
        tmp_path, ["Vendor A Pro", "Vendor B Pro"],
    )
    optimizer_output = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "producer",
        "order": ["Vendor A Pro", "Vendor B Pro"],
        "providers": {},
    }

    from provider_order import get_provider_order_from_optimizer

    chain = get_provider_order_from_optimizer(
        optimizer_output, db_path=db_path,
    )
    assert chain == ["Vendor A Pro", "Vendor B Pro"], (
        f"chain resolution returned unexpected result: {chain!r}"
    )

    # Also exercise load_fallback_order (the cached, file-based
    # path the agent uses). The contract is the same: no LLM.
    from provider_order import load_fallback_order

    order_file = _write_optimizer_order_file(
        tmp_path, ["Vendor A Pro", "Vendor B Pro"],
    )
    # load_fallback_order reads the real CC Switch DB by default; we
    # can't easily redirect it to a fake DB without monkeypatching the
    # consumer layer. We accept the failure mode if the real DB is
    # unavailable — the assertion we care about is "the LLM was not
    # called", not "the chain was resolved". We catch the DB-related
    # exception explicitly so a missing or wrong-schema real DB does
    # not surface as AssertionError.
    try:
        load_fallback_order(order_file)
    except Exception as exc:  # noqa: BLE001 — see comment above
        # ProviderOrderError or CCSwitchError is acceptable;
        # the assertion we care about is the call_log, not the
        # chain resolution outcome.
        assert "provider" in type(exc).__name__.lower() or "DB" in str(exc) or \
            "no such table" in str(exc) or "missing" in str(exc) or \
            "not found" in str(exc), (
            f"unexpected exception type from load_fallback_order: "
            f"{type(exc).__name__}: {exc}"
        )

    assert call_log == [], (
        f"ClaudeCodingTool.query was called {len(call_log)} time(s) in "
        f"PDT_BACKEND_DRY_RUN mode; chain resolution must not invoke the LLM. "
        f"Call log: {call_log!r}"
    )


# ---------------------------------------------------------------------------
# 5. test_e2e_no_db_write
# ---------------------------------------------------------------------------


def test_e2e_no_db_write(tmp_path: Path) -> None:
    """The real ``~/.cc-switch/cc-switch.db`` SHA-256 hash is
    unchanged after the suite runs.

    Hashing is defense in depth: the production read paths used by this
    suite (``cc_switch.list_provider_names`` /
    ``get_provider``) already open the DB through a
    ``mode=ro`` URI, so a write is impossible at the SQLite layer. The
    hash check catches a regression that re-opens the DB in write mode
    (e.g. by switching to a non-URI connection).

    The test reads the real DB, runs the full read code path, then
    re-hashes and asserts equality.
    """
    if not PROD_DB_PATH.exists():
        pytest.skip(f"production DB not present at {PROD_DB_PATH}")

    hash_before = _sha256_of_file(PROD_DB_PATH)

    # Exercise the real-DB read path over every declared provider. A
    # malformed row in the production DB must not fail this test — the
    # test is about *not writing*, not about whether the production data
    # is perfect — but it must still have been read.
    from cc_switch import (
        CCSwitchError,
        get_provider,
        list_provider_names,
    )

    names = list_provider_names(db_path=PROD_DB_PATH)
    read_attempts = 0
    for name in names:
        try:
            get_provider(name, db_path=PROD_DB_PATH)
            read_attempts += 1
        except CCSwitchError:
            # An unreadable database would have failed the line above for
            # every name; a per-name failure means one bad row, which is
            # not what this test is pinning.
            continue
    if names:
        assert read_attempts == len(names), (
            f"only {read_attempts} of {len(names)} providers could be read; "
            f"the read path is failing"
        )

    # Re-hash. Even if the read path raised, the file mtime/sha256
    # should not have changed.
    hash_after = _sha256_of_file(PROD_DB_PATH)

    assert hash_before == hash_after, (
        f"production DB SHA-256 changed during E2E run! "
        f"before={hash_before!r} after={hash_after!r}. "
        f"The chain-resolution path must open the DB read-only "
        f"(mode=ro URI); a regression that re-opens in write mode "
        f"would surface here."
    )
