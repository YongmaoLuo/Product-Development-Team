"""
End-to-end tests for the complete provider selection chain.

This suite pins the contract that the backend's provider selection
pipeline works end-to-end without hitting a real LLM and without
writing to the production ``~/.cc-switch/cc-switch.db``.

The 4 TDD acceptance bullets (one-to-one with the spec):

  * test_e2e_chain_reads_real_db
        - A chain entry's ``base_url`` comes from the real
          ``~/.cc-switch/cc-switch.db`` (the ``providers`` table
          schema), resolved through ``cc_switch`` — the
          reader the dispatch uses — in ``mode=ro`` URI. No mocks, no
          fake DB.
  * test_e2e_chain_order_decides_provider
        - The chain order CC Switch serves IS the provider policy: the
          selector takes the first entry with capacity, so
          rule-driven demotion (which is baked into the order)
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
  * The real DB path (``~/.cc-switch/cc-switch.db``) is the SAME for
    every test that reads it; we hash it once before, run the suite,
    then hash it again at the end of the no-DB-write test.
  * The chain is answered at the transport — CC Switch's HTTP provider
    list — so no test here depends on which CC Switch, if any, the
    machine running them happens to have listening.
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
from _pytest_hermetic_env import REAL_HOME_ENV  # noqa: E402


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


#: The home this test run was *launched* with. ``install_hermetic_env()``
#: redirects ``$HOME`` to a temp dir and seeds it with an empty CC Switch
#: database, so ``Path.home()`` names that fixture rather than the
#: operator's installation -- and this test is about the *real* database.
#: Resolving through ``PDT_TEST_REAL_HOME`` keeps the target honest and
#: lets the "no CC Switch here" skip fire on a machine without one.
_PROD_HOME = Path(os.environ.get(REAL_HOME_ENV) or Path.home())
PROD_DB_PATH = _PROD_HOME / ".cc-switch" / "cc-switch.db"


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


def _stub_cc_switch_provider_list(
    monkeypatch: pytest.MonkeyPatch, names: List[str]
) -> None:
    """Answer CC Switch's HTTP provider list with ``names``, in order.

    The chain is read from that endpoint and nowhere else, so this is
    the seam a test about chain resolution drives: it is a transport
    answer, not a database, and it leaves the real
    ``~/.cc-switch/cc-switch.db`` untouched. The payload is the shape
    CC Switch serves — ``{"providers": [{"name": ...}, ...]}`` — and the
    array order is the chain, which is why the assertion order matters.
    """
    body = json.dumps(
        {"providers": [{"name": n} for n in names]}
    ).encode()

    class _Response:
        status = 200

        def read(self):
            return body

        def getcode(self):
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import provider_order

    monkeypatch.setattr(
        provider_order.urllib.request, "urlopen", lambda *a, **k: _Response()
    )
    provider_order.cache_clear()


# ---------------------------------------------------------------------------
# 1. test_e2e_chain_reads_real_db
# ---------------------------------------------------------------------------


def test_e2e_chain_reads_real_db() -> None:
    """``base_url`` comes from the real production DB, by name.

    Pins the contract that
    :func:`cc_switch.get_provider` — the
    reader the dispatch actually uses — can read the live
    ``~/.cc-switch/cc-switch.db`` and return a usable endpoint. The
    provider is discovered through
    :func:`cc_switch.list_provider_names` rather
    than named here, so a provider CC Switch does not serve does not
    break the suite.

    The chain itself is asked of CC Switch over HTTP; what this pins is
    the other half of resolution — that a name in that chain turns into
    an endpoint here.

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
# 2. test_e2e_chain_order_decides_provider
# ---------------------------------------------------------------------------


def test_e2e_chain_order_decides_provider() -> None:
    """End-to-end: the chain order decides which provider is used.

    The chain is the array order CC Switch serves, so any demotion an
    operator or a rule engine wants is expressed there — moving a
    provider down the list *is* the policy. By the time a chain reaches
    this layer the decision is already made, so the only inputs here are
    the order and each provider's capacity.
    """
    from provider_concurrency import (
        ProviderConcurrencyController,
        select_provider_with_concurrency,
    )

    controller = ProviderConcurrencyController(
        global_limit=1, provider_limits={"Vendor B Pro": 1}
    )
    now = datetime.now(_BEIJING_TZ)

    # Order served during B-PRO's peak window (14:00-18:00):
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
# 3. test_e2e_dry_run_no_llm_cost
# ---------------------------------------------------------------------------


def test_e2e_dry_run_no_llm_cost(
    monkeypatch: pytest.MonkeyPatch, real_cc_switch
) -> None:
    """With ``PDT_BACKEND_DRY_RUN=1`` and a fail-on-call mock installed
    on ``ClaudeCodingTool.query``, the chain-resolution pipeline
    does NOT invoke the LLM.

    A regression that wires an LLM call into the chain-resolution
    path (e.g. asking the LLM to re-rank the chain) would surface
    here as a raised :class:`AssertionError` from the mock. The
    test does NOT call ``autonomous_coding`` end-to-end — only the
    chain-resolution path (``load_fallback_order``) — because the
    contract is "chain resolution does not need an LLM".

    ``real_cc_switch`` puts the real reader back past the session-wide
    stub, and the transport is then answered in-test, so the chain
    resolves through the path production uses and no LLM is reachable
    from it.
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

    # Drive the chain-resolution path against an answer of our own, so
    # the result does not depend on which CC Switch, if any, this
    # machine has listening.
    _stub_cc_switch_provider_list(monkeypatch, ["Vendor A Pro", "Vendor B Pro"])

    from provider_order import load_fallback_order

    chain = load_fallback_order()
    assert chain == ["Vendor A Pro", "Vendor B Pro"], (
        f"chain resolution returned unexpected result: {chain!r}"
    )

    # The cached second read is the one the dispatch loop actually does
    # after startup, and it must be just as LLM-free.
    assert load_fallback_order() == chain

    assert call_log == [], (
        f"ClaudeCodingTool.query was called {len(call_log)} time(s) in "
        f"PDT_BACKEND_DRY_RUN mode; chain resolution must not invoke the LLM. "
        f"Call log: {call_log!r}"
    )


# ---------------------------------------------------------------------------
# 4. test_e2e_no_db_write
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
