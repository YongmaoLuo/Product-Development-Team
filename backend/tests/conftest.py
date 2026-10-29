"""Shared pytest fixtures for the backend suite.

Annotations are stringified (PEP 563) so PEP 604 unions (``str | None``)
in evaluated positions stay importable under the CI gate's Python 3.9 —
the dev venv runs 3.11 where ``dict | None`` works natively, but CI
bootstraps a 3.9 venv and imports this conftest before collecting.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from _pytest_hermetic_env import (
    install_hermetic_env,
    install_provider_chain_stub,
    install_request_guard_header,
    real_provider_chain_from_api,
    write_minimal_cc_switch_db,
    xdist_worker_id,
)

#: The backend package root — ``<repo>/backend``, since this file lives in
#: ``backend/tests/``. Used to tell "a worker this application started"
#: (which the suite must reclaim) from a library's own thread.
_BACKEND_DIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# The process-global environment contract.
# ---------------------------------------------------------------------------
#
# The guard-header patch, ``PDT_ALLOWED_HOSTS`` / ``PDT_DEBUG_ROUTES``, the
# throwaway state database and every other hermetic env var live in
# ``backend/_pytest_hermetic_env.py``.  They used to be inline here, which
# made "this tree is hermetic" an accident of *this file being imported*:
# ``backend/state_machine/tests/`` collected on its own got no guard header,
# no allowed hostname and no throwaway database, and 20 of its route tests
# answered HTTP 403 while 3 more refused to touch the live ``state.db``.
# See that module's docstring for the whole account.
#
# The four aliases below exist so the fixtures further down keep the names
# they have always had.  ``_SUBAGENT_ROOT`` and ``_XDIST_WORKER`` are read by
# ``pytest_sessionfinish`` and by the session fixture, both at call time.
install_request_guard_header()
_HERMETIC_ENV = install_hermetic_env()
install_provider_chain_stub()
_TEST_STATE_DB_DIR = _HERMETIC_ENV.state_db_dir
_LOCK_ROOT = _HERMETIC_ENV.lock_root
_SUBAGENT_ROOT = _HERMETIC_ENV.subagent_root
_HOME_ROOT = _HERMETIC_ENV.home_root
_HOME_ROOT_OWNER_PID = _HERMETIC_ENV.home_root_owner_pid
_LOCK_ROOT_OWNER_PID = _HERMETIC_ENV.lock_root_owner_pid
_STATE_ROOT_OWNER_PID = _HERMETIC_ENV.state_root_owner_pid
_XDIST_WORKER = xdist_worker_id()


# ---------------------------------------------------------------------------
# The verification-mode fixtures, and the rule that hands them back.
# ---------------------------------------------------------------------------
#
# The fixtures an end-to-end verification run needs — a reproducible upstream
# repository, a minimal PRD, a scripted LLM, an isolated workspace and a
# server on an ephemeral port — live in their own module
# (``backend/tests/fixtures/verification_mode.py``) rather than here: they
# are a coherent set with a contract between them, and a contract spread
# over two files is a contract half of which nobody reads.
#
# They are imported here because this file is where the suite's teardown
# lives, and the teardown has to reach the *same* registry the fixtures
# write to.  A worktree or a server that outlives its test is the same
# defect as a monitor thread that outlives its test, and the teardown
# already says so about threads: "a test hands back every resource it
# starts".
#
# The import sits below ``install_hermetic_env()`` on purpose: the backend
# modules it pulls in read the environment, and the environment has to be
# the hermetic one before they are read the first time.
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from tests.fixtures.verification_mode import (  # noqa: E402
    clear_verification_registries,
    detect_leftover_resources,
    minimal_prd,
    reclaim_verification_resources,
    running_server,
    scripted_llm,
    upstream_repo,
    verification_plan,
    workspace_root,
)


# ---------------------------------------------------------------------------
# Tripwire: no test may aim a broadcast at the machine it runs on.
# ---------------------------------------------------------------------------
#
# ``os.killpg(1, sig)`` is ``kill(-1, sig)`` — a signal to every process
# the caller may signal. On glibc that is exactly what happened here: a
# ``MagicMock`` resolved to process group 1, the provider-fallback tests
# in ``test_coding_tool`` broadcast a SIGKILL, and ``Runner.Worker`` died,
# which is why a step timeout living inside that worker never fired and
# the shard hung until GitHub reaped the job. See ``utils/process.py``.
#
# The guards in ``utils.process`` are the fix. This is the cheap fuse for
# the day someone adds a *new* call site without routing it through them.
#
# Why an audit hook rather than a fixture: the call can happen at import
# time or inside a thread the fixture never sees, and a hook catches all
# of them. ``killpg`` is rare enough that the per-call cost is noise.
#
# Scope — read this before trusting a green run. The hook sees real
# system calls only, so:
#
#   * it catches anything that genuinely reaches the kernel;
#   * it is **blind** to ``os.killpg`` invoked through a test double.
#     A suite that patches ``os.killpg`` replaces the very call the hook
#     observes, so unit tests of the guards can never trip it — and that
#     is exactly the traffic where a regression would hide.
#
# So this is a backstop against a *new call site firing for real*, not a
# substitute for the per-guard tests in ``test_process_utils.py``. Both
# are needed: the tests prove the guards work, this proves nothing grew
# a fifth unguarded ``killpg`` between them.
#
# Reports rather than blocks, and does not raise at the call site: the
# failure lands once at the end of the session with the full list and a
# stack, instead of turning an unrelated test into a confusing error.
# The signal only goes anywhere dangerous if a guard already failed to
# prevent it, which is the case worth being loud about.
_KILLPG_REPORTS: list = []


def _install_killpg_tripwire() -> None:
    """Record every real ``os.killpg`` that a guard failed to prevent.

    A sentinel target (``<= 1``) reaching the kernel is a bug wherever it
    appears, so the report is the deliverable and the signal itself is
    left to do whatever it does — the point is to name the call site, and
    on the platform where it matters the process is already in trouble by
    the time this returns.
    """

    def _hook(event: str, args) -> None:
        if event != "os.killpg":
            return
        pgid = args[0] if args else None
        if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid > 1:
            return
        import traceback

        stack = "".join(traceback.format_stack()[-6:-1])
        _KILLPG_REPORTS.append(f"os.killpg(pgid={pgid!r})\n{stack}")

    sys.addaudithook(_hook)


def _killpg_tripwire_report() -> None:
    """Fail the session if any sentinel ``killpg`` was attempted.

    Registered as a ``pytest_sessionfinish`` hook so the failure lands
    once, with the full list, rather than per test.
    """
    if not _KILLPG_REPORTS:
        return
    report = "\n".join(f"  [{i + 1}] {r}" for i, r in enumerate(_KILLPG_REPORTS))
    raise AssertionError(
        f"{len(_KILLPG_REPORTS)} call(s) reached os.killpg with a POSIX "
        f"sentinel target (pgid <= 1). On glibc that is a broadcast, not "
        f"a group. Route them through utils.process.kill_process_group, "
        f"or through _signallable_pgid if you need the raw check:\n{report}"
    )


def pytest_sessionfinish(session, exitstatus) -> None:
    _killpg_tripwire_report()
    # The controller's own scratch roots. A serial run has already had both
    # removed by ``_hermetic_state_db_dir`` by the time this fires, and
    # ``ignore_errors=True`` makes the second attempt a no-op rather than an
    # error. Under xdist it is the only process that mints a root nobody else
    # will close: the controller loads this module but runs no test, so no
    # session fixture is ever instantiated in it, and its directory would
    # otherwise be the one directory per run that survives — which is the
    # leak the idempotency note above exists to prevent, reintroduced by the
    # fix for it.  Each removal is additionally gated on having minted the
    # root: this block already runs only in the controller, but the guard is
    # what makes "the process that owns it" the rule rather than "the process
    # that got here first", which is what a nested session inherits from us.
    if not _XDIST_WORKER:
        if os.getpid() == _STATE_ROOT_OWNER_PID:
            shutil.rmtree(_TEST_STATE_DB_DIR, ignore_errors=True)
        if os.getpid() == _LOCK_ROOT_OWNER_PID:
            shutil.rmtree(_LOCK_ROOT, ignore_errors=True)
        # Only the process that minted this root removes it: a nested pytest
        # session inherits ``PDT_TEST_FAKE_HOME`` and reuses this directory,
        # so an unconditional removal here would delete the CC Switch database
        # out from under the run that created it.
        if os.getpid() == _HOME_ROOT_OWNER_PID:
            shutil.rmtree(_HOME_ROOT, ignore_errors=True)


_install_killpg_tripwire()


# ---------------------------------------------------------------------------
# Hermetic provider capacity
# ---------------------------------------------------------------------------
#
# Per-provider concurrency caps come from ``provider_capacity.yaml``,
# which is per-user config: the developer running the suite may well have
# one, and a test that silently inherited it would pass or fail depending
# on whose machine it ran on. So the autouse fixture below points the
# loader at a path that does not exist, which is the documented
# "no rules configured" state — every provider gets the default cap.
#
# A test that cares about caps calls ``capacity_config`` to declare its
# own rules, which takes precedence because it sets the same env var
# afterwards.
@pytest.fixture(autouse=True)
def _hermetic_provider_capacity(tmp_path, monkeypatch):
    """Keep the developer's own ``provider_capacity.yaml`` out of the suite."""
    import provider_capacity

    monkeypatch.setenv(
        "PDT_PROVIDER_CAPACITY_FILE",
        str(tmp_path / "absent-provider_capacity.yaml"),
    )
    provider_capacity.clear_capacity_cache()
    yield
    provider_capacity.clear_capacity_cache()


@pytest.fixture
def capacity_config(tmp_path, monkeypatch):
    """Declare per-provider caps for one test.

    Returns a writer so a test names only the rules it needs::

        capacity_config([("^Vendor A", 10)], default=5)

    Rules are ``(regex, cap)`` pairs, tried in order, first match wins —
    the same semantics the real file has.
    """
    import provider_capacity

    path = tmp_path / "provider_capacity.yaml"

    def _write(rules=(), default=None):
        lines = ["version: 1"]
        if default is not None:
            lines.append(f"default_max_concurrency: {default}")
        lines.append("providers:")
        for pattern, cap in rules:
            lines.append(f'  - pattern: "{pattern}"')
            lines.append(f"    max_concurrency: {cap}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setenv("PDT_PROVIDER_CAPACITY_FILE", str(path))
        provider_capacity.clear_capacity_cache()
        return path

    yield _write
    provider_capacity.clear_capacity_cache()


@pytest.fixture
def capacity_from_text(tmp_path, monkeypatch):
    """Point the capacity loader at a file written verbatim.

    For the malformed-input cases ``capacity_config`` cannot express.
    """
    import provider_capacity

    path = tmp_path / "provider_capacity.yaml"

    def _write(text):
        path.write_text(text, encoding="utf-8")
        monkeypatch.setenv("PDT_PROVIDER_CAPACITY_FILE", str(path))
        provider_capacity.clear_capacity_cache()
        return path

    yield _write
    provider_capacity.clear_capacity_cache()

# ---------------------------------------------------------------------------
# Hermetic quota-tier memory
# ---------------------------------------------------------------------------
#
# The quota-tier memory is a file in the configuration directory: it
# records which quota windows a provider has ever reported, so that a
# later reading can tell "this provider has no weekly window" from "this
# provider's weekly window went missing".  The quota reader both reads
# and writes it, so a suite that ran against the real file would plant
# the suite's own history in the operator's ``.config`` directory — and
# the next run on that machine would answer from it.
#
# The autouse fixture below points the memory at a path inside
# ``tmp_path``, which is thrown away with the test.
@pytest.fixture(autouse=True)
def _hermetic_quota_tier_memory(tmp_path, monkeypatch):
    """Keep the developer's own quota-tier memory out of the suite."""
    import quota_tier_memory

    monkeypatch.setenv(
        "PDT_QUOTA_TIER_MEMORY_FILE",
        str(tmp_path / "quota-tier-memory.json"),
    )
    quota_tier_memory.reset_cache()
    yield
    quota_tier_memory.reset_cache()

# ---------------------------------------------------------------------------
# Hermetic state DB — the single most important isolation rule in this suite.
# ---------------------------------------------------------------------------
#
# 2026-09-13 — the suite must never touch the developer's live state.db.
# Each test gets its own scratch database instead.
#
# Every state resolution point in the backend — ``server._state_db_path``,
# ``plan_state._state_db_path``, ``task_manager``, ``task_repository`` and
# ``agent._get_task_progress_repository`` — honours the ``PDT_STATE_DB_PATH``
# env var, and each of them otherwise defaults to ``<backend-parent>/state.db``.
# That default is the *live* database the running backend server reads and writes.
#
# Before this block the suite had no session-wide redirect, and the
# per-test ``isolated_plans_dir`` fixture below only patched ``server``'s
# copies of ``PLANS_DIR`` / ``_state_db_path``. Every other module
# (``plan_state.PlanState`` in particular, which is what most workflow
# tests drive) kept resolving the real ``state.db``. Two consequences,
# both observed in the wild:
#
#   1. **The suite wrote test plans into the live database.** A full run
#      left 243 fixture plan ids (``vp-*``, ``test-*``, ``t-cap``, ...)
#      in the operator's ``state.db``, which the running backend server then
#      listed as real plans and — because ``plan_dir_resolver`` sweeps
#      every ``plan_routing`` row whose ``stage`` is ``executing`` /
#      ``verification_running`` — pushed Feishu/Telegram cards for.
#      That is the "新推了一张卡片, 但那张卡片里面除了状态什么都没有"
#      report: a junk plan id with no tasks renders exactly one
#      "📍 当前状态" line and nothing else.
#   2. **Cross-run residue made tests order-dependent**, because a
#      previous run's rows survived into the next (the same failure the
#      ``_scrub_fixture_plan_ids`` band-aid below was fighting, one
#      plan_id at a time).
#
# Setting the env var at *module import* time (not in a fixture) is
# deliberate: ``tests/conftest.py`` is imported before any test module,
# and a fixture would run too late for anything that resolves the path
# during collection.
#
# Individual tests that need their own fresh DB still call
# ``monkeypatch.setenv("PDT_STATE_DB_PATH", ...)`` — monkeypatch restores
# this session value on teardown, so the two mechanisms compose.
# ---------------------------------------------------------------------------
#
# The env vars themselves -- the session scratch root, ``PDT_STATE_DB_PATH``,
# ``PDT_PLANS_DIR``, ``PDT_LOCK_ROOT`` and the credential scratch root -- are
# installed by ``install_hermetic_env()`` at the top of this file, which is
# where their per-entry reasoning moved with them.  The fixtures below only
# read the four aliases it returned, and ``_hermetic_state_db_dir`` closes
# over the same two roots the session-finish hook removes.



@pytest.fixture(scope="session", autouse=True)
def _hermetic_state_db_dir():
    """Remove the session-scoped test state DB directory at the end.

    The DB itself is created lazily by whichever module first calls
    ``open_db``; only the directory is pre-created here. Kept as a
    fixture (rather than an ``atexit`` hook) so the cleanup is visible
    in the fixture list and reports failures through pytest.
    """
    _TEST_STATE_DB_DIR.mkdir(parents=True, exist_ok=True)
    (_TEST_STATE_DB_DIR / "plans").mkdir(parents=True, exist_ok=True)
    yield _TEST_STATE_DB_DIR
    # Only the process that minted a root removes it.  Under xdist this
    # fixture is instantiated once *per worker*, and every worker inherited
    # one ``PDT_LOCK_ROOT`` from the controller -- so the unconditional
    # removal this replaced deleted the live broker sockets of the workers
    # still running, which surfaces as "[pdt-lock] broker unavailable
    # (exit 4)".  A worker owns the state directory (it minted or scoped it
    # to its own worker id) but not the lock root.
    if os.getpid() == _STATE_ROOT_OWNER_PID:
        shutil.rmtree(_TEST_STATE_DB_DIR, ignore_errors=True)
    if os.getpid() == _LOCK_ROOT_OWNER_PID:
        shutil.rmtree(_LOCK_ROOT, ignore_errors=True)


# ---------------------------------------------------------------------------
# The provider chain CC Switch reports
# ---------------------------------------------------------------------------
#
# ``provider_order.load_fallback_order()`` asks CC Switch for the chain
# and RAISES ``ProviderOrderError`` when CC Switch does not answer. That
# answer is runtime state of another application — an HTTP server this
# repository does not start — and it does not exist on a clean CI
# checkout. A test that boots the app would then die inside the
# lifespan — ``server._lifespan`` -> ``load_providers()`` ->
# ``load_fallback_order()`` — for an environment reason rather than a
# code one.
#
# The stub that answers it lives in ``_pytest_hermetic_env`` and is
# installed by ``install_provider_chain_stub()`` at the top of this file.
# It used to live here as the session fixture
# ``_provider_chain_from_cc_switch``, which was the reason the two trees
# disagreed: a fixture belongs to the conftest that declares it, so
# ``state_machine/tests`` — which never collected this file — got no
# chain at all and its route tests really dialled
# ``http://127.0.0.1:4000``.  The installer belongs to no tree, so both
# get it.  See that module's docstring for why one installer, not two.
#
# It stubs the reader the chain resolves through — ``_chain_from_api`` —
# and leaves ``cc_switch`` untouched. That matters: ``cc_switch`` is
# still the reader of record for endpoint and credentials
# (``server.load_providers`` resolves every name through
# ``cc_switch.get_provider``), so stubbing it would stop these fixtures
# exercising the real lookup, and the tests that *are* about the
# resolver can put the genuine reader back with ``real_cc_switch``
# below.
#
# ``fake_cc_switch_home`` below is the other half — it points
# ``cc_switch``'s own database reads at a throwaway store, for the
# tests that exercise those directly.


@pytest.fixture
def real_cc_switch(monkeypatch):
    """Put the real chain resolver back for one test.

    The shared stub answers the resolver so that booting the app works
    without a CC Switch on the machine. A test about the resolver itself
    needs the opposite, and this is how it says so — ``monkeypatch``
    restores the stub when the test ends. Such a test then has to give
    the transport an answer of its own; there is no second source for
    the real resolver to fall back on.
    """
    import provider_order

    monkeypatch.setattr(
        provider_order, "_chain_from_api", real_provider_chain_from_api()
    )
    provider_order.cache_clear()
    yield provider_order
    provider_order.cache_clear()


@pytest.fixture
def provider_chain(monkeypatch):
    """Install a specific chain for one test, then restore the session stub.

    Returns a callable so a test can name the chain it is exercising
    against. Use this in tests about the **consumers** of a chain; the
    tests about where a chain comes from stub the legs directly, so a
    change to the transport shows up there and not here.
    """
    import provider_order

    def _install(chain):
        monkeypatch.setattr(
            provider_order.urllib.request,
            "urlopen",
            lambda *a, **k: _payload_response(list(chain)),
        )
        provider_order.cache_clear()
        return list(chain)

    return _install


def _payload_response(names):
    """Build an HTTP response object carrying a CC Switch provider list."""
    import json as _json

    class _Response:
        status = 200

        def read(self):
            return _json.dumps(
                {"providers": [{"name": n} for n in names]}
            ).encode()

        def getcode(self):
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _Response()


@pytest.fixture
def fake_cc_switch_home(monkeypatch, tmp_path):
    """Redirect ``$HOME`` at a minimal CC Switch database.

    For the tests that exercise ``cc_switch``'s own reads: it resolves
    ``~/.cc-switch/cc-switch.db``, and that file is the desktop app's
    store — present on a developer machine, absent from any clean
    checkout. A test pointing a read at a throwaway store needs the
    store to exist first.

    The provider **chain** is not among them. The chain is asked of
    CC Switch's HTTP provider list and this fixture has no say in it;
    what a test needs for the chain is a transport stub. What lives
    here is the roster ``cc_switch.get_provider`` /
    ``list_provider_names`` resolve against while ``server
    .load_providers`` turns a chain's names into configs.

    Opt-in rather than autouse: the session already runs against a throwaway
    ``$HOME`` that carries its own CC Switch database
    (``install_hermetic_env`` installs both).  What this fixture adds is that
    the database *moves* -- into this test's own ``tmp_path``, redirected by
    this test's ``monkeypatch`` and restored with it.  A test that needs a
    database it can point at something other than the session home asks for
    this; a test that merely needs *a* database already has one.
    """
    fake_home = tmp_path / "home"
    cc_dir = fake_home / ".cc-switch"
    # The schema is defined once, in ``_pytest_hermetic_env``, and shared with
    # the session-wide install there.  Two copies of this DDL would be free to
    # drift and nothing in either tree would say so.
    write_minimal_cc_switch_db(cc_dir)

    # ``resolve_db_path`` calls ``Path.home()`` on every call, so
    # redirecting the env var is enough — no module constant to patch.
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


# ---------------------------------------------------------------------------
# The process environment file
# ---------------------------------------------------------------------------
#
# 2026-10-08: this section used to materialise ``backend/.env.ci`` as
# ``backend/.env`` for the whole session, so the suite ran with the three
# placeholder variables ``env_config.REQUIRED_VARS`` demanded. That
# demand is gone, and so is the fixture.
#
# The history is worth one line because the failure it caused was invisible
# for a long time. The fixture was itself the third instance of the same
# cause as the two above it: something the code requires is **runtime
# state that a git checkout does not carry**. ``load_env()`` raised
# ``RuntimeError`` when a variable was absent, the variables lived in a
# gitignored file, and so
# ``tests/unit/test_agent_persist.py::test_cli_passes_tasks_file_through``
# failed on every CI run while passing locally. Nobody saw it because the
# full ``unit`` shard wedges before reaching that test; it took staircase
# shard u-55 (run 35852698516) — the first shard small enough to finish —
# to surface it. The fix at the time was to feed the suite placeholders.
#
# The fix now is that there is nothing to feed it. No API key is read from
# a dotenv by this repository — the key is supplied by the provider layer
# — the other two required names never had a reader at all, and
# ``env_config.REQUIRED_VARS`` is empty. A fresh checkout runs the suite
# with no environment file, which is the point.
#
# Nothing below replaces it: the loader has no required variables, so
# there is no configuration a missing file can withhold. If a
# deployment-specific requirement is ever added to ``REQUIRED_VARS``,
# the session fixture that satisfies it comes back with it.


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Print TEST_RESULT marker at end of test session.

    The backend's execution agent looks for TEST_RESULT: PASSED/FAILED
    in the test output to determine task success.
    """
    terminalreporter.write_sep("=", "TEST RESULT")
    if exitstatus == 0:
        total = terminalreporter._session.testscollected
        terminalreporter.write_line(f"TEST_RESULT: PASSED")
        terminalreporter.write_line(f"REASON: {total} tests collected, all passed. Exit code: 0")
    else:
        terminalreporter.write_line(f"TEST_RESULT: FAILED")
        terminalreporter.write_line(f"REASON: pytest exited with code {exitstatus}")


#: Whether the scrub DB has had its schema migrated in this session.
#: ``_scrub_fixture_plan_ids`` runs per test; re-running the DDL every
#: time was a per-test disk cost that dominated the suite on CI.
_SCRUB_SCHEMA_MIGRATED = False


def _scrub_fixture_plan_ids() -> None:
    """Delete the four plan_* rows for known test fixture plan_ids.

    Several e2e tests bootstrap a plan via :func:`PlanState` with a
    hard-coded ``plan_id`` (``"e2e"``) for fixture convenience. The
    state-machine refactor introduced persistent SQLite rows for
    every plan, so a second test run would inherit the previous
    run's persisted row (e.g. ``verification_round=1``) instead of
    the freshly-written ``plan_state.json`` — surfacing as a
    ``round == 0`` assertion failure or a stale "current_phase"
    mismatch. The failure is real (the test reads wrong state) but
    the cause is environmental (residue from a prior run), not a
    regression in the code under test.

    Fix: scrub SQLite rows for any ``plan_id`` that matches a small
    set of known fixture ids. The set is explicit (not regex) so an
    accidental name collision in production data cannot be wiped by a
    test run.

    Called from the ``_scrub_fixture_plan_ids_per_test`` autouse
    fixture (per test) rather than a collection-time hook, because
    earlier tests in the same session re-create the row — scrubbing
    only once at collection is too early to keep the row clean.
    """
    fixture_plan_ids = {"e2e", "state-machine", "test-plan", "test-plan-rp-disk"}
    try:
        # Resolve the db path the same way ``PlanState`` does so the
        # scrub targets the same file the test's ``PlanState`` reads.
        # ``_open_db()`` requires an explicit path; calling it with no
        # args raises ``TypeError`` and the scrub would silently no-op.
        from plan_state import _state_db_path
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
    except ImportError:
        return
    try:
        conn = _open_db(_state_db_path())
    except Exception:  # noqa: BLE001 — SQLite unavailable in some envs
        return
    try:
        # 2026-09-23 — cost control. This function runs before EVERY
        # test, so anything unconditional here is multiplied by the
        # whole suite. It used to run ``_migrate()`` (the full schema
        # DDL) and then 16 unconditional DELETEs plus a ``commit()`` —
        # a real write transaction with an fsync, per test. On a
        # developer SSD that is sub-millisecond and invisible; on a
        # GitHub runner's disk it is ~0.3-0.5s.
        #
        # That per-test constant is what made the sharded unit lane take
        # >45 minutes on CI while running in 80s locally, and it is why
        # the smaller ``misc`` shard inflated by exactly 194 x ~0.36s
        # (a constant per test is invisible in a small shard and fatal
        # in a large one — a hang would have tripped pytest-timeout at
        # 60s, so nothing here was hanging; every test was just paying
        # the same tax).
        #
        # So: migrate once per session, then probe with one read-only
        # query and do NOTHING when no fixture row is present — which is
        # the overwhelmingly common case, since only a handful of tests
        # ever create these ids.
        global _SCRUB_SCHEMA_MIGRATED
        if not _SCRUB_SCHEMA_MIGRATED:
            _migrate(conn)
            _SCRUB_SCHEMA_MIGRATED = True

        tables = (
            "plan_artifacts",
            "plan_verification",
            "plan_execution",
            "plan_routing",
        )
        placeholders = ", ".join("?" for _ in fixture_plan_ids)
        probe = " UNION ALL ".join(
            f"SELECT plan_id FROM {tbl} WHERE plan_id IN ({placeholders})"
            for tbl in tables
        )
        params = tuple(fixture_plan_ids) * len(tables)
        if not conn.execute(
            f"SELECT 1 FROM ({probe}) LIMIT 1", params
        ).fetchone():
            return  # nothing to scrub: skip the writes AND the fsync

        for pid in fixture_plan_ids:
            for tbl in tables:
                conn.execute(
                    f"DELETE FROM {tbl} WHERE plan_id = ?", (pid,)
                )
        conn.commit()
    except Exception:  # noqa: BLE001
        # The scrub is best-effort: if the schema is in a state
        # that doesn't accept the DELETE, the test will still run
        # and surface a meaningful error rather than a silent
        # isolation failure.
        _SCRUB_SCHEMA_MIGRATED = False  # re-migrate on the next call
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _scrub_fixture_plan_ids_per_test():
    """Scrub fixture plan_ids from the dev ``state.db`` before every test.

    Multiple e2e tests in ``tests/e2e/test_verification_e2e_dryrun.py``
    share the hard-coded ``plan_id="e2e"`` and call
    ``agent.start_verification_round(1)``, which persists a
    ``plan_routing`` row with ``verification.round=1`` into the shared
    dev ``state.db``. Because ``PlanState._load_state`` prefers SQLite
    over the freshly-written ``plan_state.json``, a later test in the
    same session (e.g. ``test_e2e_dryrun_state_machine_repair_loop``)
    would read the stale ``round=1`` and fail its ``round == 0``
    assertion.

    A per-test autouse fixture (rather than a once-per-session
    collection hook) is required precisely because earlier tests in the
    session re-create the row — scrubbing only at collection time is
    too early. This runs before each test so the row is always clean at
    the moment the test bootstraps its own state.
    """
    _scrub_fixture_plan_ids()
    yield


def pytest_collection_modifyitems(config, items):
    """Skip the real-model tests in CI; run them in local development.

    Marker split (2026-09-25)
    -------------------------
    ``integration`` — slow, spawns local subprocesses, drives the real
    server — but needs **no model**. Runs in CI.

    ``real_model`` — actually calls a model (``claude`` via
    ``ClaudeCodingTool``, see
    ``coding_tool.ClaudeCodingTool._run_claude_interactive``). Needs
    ``ANTHROPIC_*`` plus a live provider, which a hosted runner has
    neither of: such a run would either fail at runtime or, worse,
    silently burn real tokens against the runner's account. So these are
    the tests CI must skip.

    Before 2026-09-25 the boundary was the ``integration`` marker, which
    was too wide. It skipped the 15 file-lock / concurrency / hotpath
    tests in ``tests/integration/`` that need no model at all, which made
    the ``integration-tests`` CI job collect them and skip every one —
    a gate that could never fail (measured: ``15 skipped, 0 passed``,
    exit 0). It also left the real-model tests skipped in *every* lane,
    including the nightly, which is why the local nightly exists.

    Contract:

      * **Local, no ``CI``** → everything runs (``real_model``
        included). The local nightly runs ``-m real_model``; a bare
        ``pytest tests/`` runs all of it.
      * **CI (``CI=true``)** → ``real_model`` tests are skipped;
        everything else, ``integration`` included, runs. The cloud
        nightly covers the cloud-runnable set; the local nightly covers
        ``real_model``. The two sets do not overlap.
    """
    if os.environ.get("CI") == "true":
        skip_real_model = pytest.mark.skip(
            reason=(
                "real-model test: needs the claude CLI plus a live "
                "provider (ANTHROPIC_*), which a hosted CI runner does not "
                "have. Run it locally without CI=true — the local nightly "
                "runs the 'real_model' marker for exactly this reason."
            )
        )
        for item in items:
            # Marker-only lookup, not ``"real_model" in item.keywords``:
            # pytest's keyword set also holds name-derived tokens, so a
            # membership test would silently skip any test whose name (or
            # class name) happens to mention the marker token.
            if item.get_closest_marker("real_model") is not None:
                item.add_marker(skip_real_model)


@pytest.fixture(autouse=True)
def isolated_provider_state():
    """Give every test its own provider concurrency + cooldown state.

    ``dynamic_provider_concurrency`` keeps two process-global handles:

      * ``_shared_tracker`` — the ``ActiveConcurrencyTracker`` the
        FastAPI lifespan publishes as ``RuntimeState.dynamic_tracker``,
        and the default for every ``ClaudeCodingTool`` built without an
        explicit ``concurrency_tracker=``;
      * ``_shared_cooldown`` — the parked-provider set written when a
        provider answers "quota exhausted".

    Both outlive the test that touched them. The failure that made this
    fixture necessary (2026-09-24) was purely cross-file: an earlier
    test held slots or parked a provider, and a later
    ``test_coding_tool_scene_capacity`` dispatch then found every pool
    full and polled in ``acquire_provider_with_dynamic_capacity`` until
    pytest's per-test timeout killed it. The file passes in 6 s on its
    own and only hangs inside a full-suite run — a length of the prefix,
    not any single test, decided the outcome.

    ``set_shared_tracker`` / ``set_shared_cooldown`` exist for exactly
    this (their docstrings say "Passing None resets the handle
    (tests)"); nothing was calling them per test. Installing a fresh
    instance — rather than None — keeps ``get_shared_tracker()``
    non-None for tests that call it directly, while guaranteeing no
    slot or park survives into the next test.
    """
    from dynamic_provider_concurrency import (
        ActiveConcurrencyTracker,
        ProviderCooldown,
        set_shared_cooldown,
        set_shared_tracker,
    )

    set_shared_tracker(ActiveConcurrencyTracker())
    set_shared_cooldown(ProviderCooldown())
    try:
        yield
    finally:
        set_shared_tracker(None)
        set_shared_cooldown(None)


# ---------------------------------------------------------------------------
# HITL persistence isolation
# ---------------------------------------------------------------------------
#
# The HITL feature (human-in-the-loop decisions) gives the backend a
# persistence surface of its own: the ``plan_decisions`` table in the
# state database, plus the per-plan artifacts a decision asks about.  A
# decision row outlives the call that wrote it — it is asked by one
# thread and answered by another, minutes later — so a row that
# survives the test that wrote it is read by the *next* test as if it
# were a live question.  That failure is order-dependent: it reproduces
# only on the runs where the two tests happen to be adjacent, and the
# evidence points at the wrong code.
#
# Two things are pinned below, and they are the difference between a
# named failure and a flake:
#
#   1. **The redirect is proven, not assumed.**  ``isolated_plans_dir``
#      sets the environment variables and then asks the *production*
#      resolvers where they would write.  A resolver that stops
#      honouring its variable — a new persistence path that resolves
#      its root some other way — fails the test loudly, at the point
#      where the redirect broke, instead of writing into the operator's
#      tree and surfacing three tests later as an unexplained row.
#
#   2. **A knob the fixture cannot redirect is refused.**  The HITL
#      package will grow its own persistence (a decision configuration
#      file, for one).  The day it reads a ``PDT_*`` variable, that
#      variable has to be redirectable; the session-scoped guard below
#      refuses to start a run in which the package reads one the
#      fixture does not know about, and names it.
#
# Nothing in this section creates a file.  The fixture only *points*
# paths at the per-test tmp root, because a test that asserts on the
# contents of ``tmp_path`` (there is one that requires nothing but
# ``tasks.json`` to be in there) would otherwise see the fixture's own
# artefacts.


def _resolve_plans_dir() -> Path:
    """The path the application writes its per-plan artifacts to.

    Imported lazily: ``config_paths`` is a leaf module, but the conftest
    is imported by every test tree, and a slimmed session may not ship
    the backend package at all.
    """
    import config_paths

    return config_paths.resolve_plans_dir()


def _resolve_state_db_path() -> Path:
    """The path the application writes its state database to."""
    import config_paths

    return config_paths.resolve_state_db_path()


#: The persistence roots the suite redirects, and the environment
#: variable that relocates each one.  The third element is the
#: *production* resolver — the function the application itself calls to
#: obtain the path — so the guard asks the application where it would
#: write rather than assuming the variable was honoured.
_HITL_PERSISTENCE_ROOTS = (
    ("plans root", "PDT_PLANS_DIR", _resolve_plans_dir),
    ("state db", "PDT_STATE_DB_PATH", _resolve_state_db_path),
)

#: The knobs the fixture is able to redirect, by name.  A ``PDT_*``
#: variable outside this set cannot be pointed at the per-test tmp
#: root, which is why the session guard below treats one as an error
#: rather than as a detail.
_HITL_REGISTERED_KNOBS = frozenset(
    env_var for _name, env_var, _resolve in _HITL_PERSISTENCE_ROOTS
)

#: The shapes a ``PDT_*`` variable is read in.  Three spellings, because
#: three spellings are in use across the backend
#: (``os.getenv("X")``, ``os.environ.get("X")``, ``os.environ["X"]``).
_PDT_KNOB_PATTERNS = (
    re.compile(r"""os\.getenv\(\s*["'](PDT_[A-Z0-9_]+)["']"""),
    re.compile(r"""os\.environ\.get\(\s*["'](PDT_[A-Z0-9_]+)["']"""),
    re.compile(r"""os\.environ\[\s*["'](PDT_[A-Z0-9_]+)["']"""),
)


def _is_within(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or lives under it.

    Both sides are resolved first: on macOS ``/tmp`` is a symlink to
    ``/private/tmp``, so a string comparison would call a path inside
    the per-test root "outside" it.
    """
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False
    return True


def _assert_hitl_persistence_isolated(tmp_root: Path) -> None:
    """Fail loudly when a persistence root escaped the per-test redirect.

    Called by ``isolated_plans_dir`` for every test, so the failure it
    reports is the *first* thing that went wrong rather than a stray
    row three tests later.  Each root is checked twice: the variable
    has to point inside the per-test root, and the resolver the
    application calls has to agree with it.
    """
    for name, env_var, resolve in _HITL_PERSISTENCE_ROOTS:
        raw = os.environ.get(env_var)
        assert raw, (
            f"the {name} was not redirected: {env_var} is unset, so the "
            f"suite would write into the operator's tree"
        )
        assert _is_within(Path(raw), tmp_root), (
            f"the {name} points outside the per-test root: {env_var}={raw!r} "
            f"is not under {tmp_root!s}"
        )
        resolved = Path(resolve())
        assert _is_within(resolved, tmp_root), (
            f"the {name} resolver ignored {env_var}: it resolves to "
            f"{resolved!s}, which is not under the per-test root "
            f"{tmp_root!s}. A persistence path that no longer honours the "
            f"override has to be fixed before the suite can be trusted."
        )


def _assert_decision_table_starts_empty(db_path: Path) -> None:
    """The ``plan_decisions`` table of the per-test database must be empty.

    The database is a per-test file, so in the normal case it does not
    exist yet when a test starts and the table cannot hold a row.  This
    check is the backstop for the case where it does: a decision row
    left over from another test means the database is shared after all,
    and the next test would read a stranger's decision as if it were
    live.  Failing here — with the row count named — is the difference
    between a diagnosis and a flake.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='plan_decisions'"
        ).fetchone()
        if table is None:
            return
        count = conn.execute("SELECT COUNT(*) FROM plan_decisions").fetchone()[0]
    except sqlite3.OperationalError:
        # A database that cannot be read is not this check's business;
        # the test that needs it reports the real error.
        return
    finally:
        conn.close()
    assert count == 0, (
        f"the per-test state database {db_path} already holds {count} "
        f"plan_decisions row(s) before the test started: the database is "
        f"shared, so a decision written by one test would be read by the "
        f"next"
    )


def _hitl_sources() -> list[Path]:
    """The HITL sources whose ``PDT_*`` knobs the fixture must redirect.

    ``backend/hitl`` (the decision model and store), ``backend/hooks``
    (the one reader and writer of ``<plan_dir>/hitl_config.json``) and
    the module paths a HITL route would plausibly take.  Absent paths
    are skipped: the point of the scan is what the HITL code *reads*,
    not that the HITL code exists yet.
    """
    candidates = [
        _BACKEND_DIR / "hitl",
        _BACKEND_DIR / "hooks",
        _BACKEND_DIR / "hitl.py",
        _BACKEND_DIR / "routes" / "hitl.py",
    ]
    return [candidate for candidate in candidates if candidate.exists()]


def _scan_pdt_knobs(source: Path) -> set[str]:
    """Return the ``PDT_*`` variables the module(s) at ``source`` read.

    ``source`` may be a single module or a package directory (scanned
    recursively).  Reading the source rather than importing it is
    deliberate: the scan must work for a module that cannot be
    imported in this process, and it must not have side effects.
    """
    source = Path(source)
    if source.is_dir():
        files = sorted(source.rglob("*.py"))
    elif source.is_file():
        files = [source]
    else:
        return set()
    found: set[str] = set()
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover
            continue
        for pattern in _PDT_KNOB_PATTERNS:
            found.update(pattern.findall(text))
    return found


def _unregistered_hitl_knobs(knobs) -> list[str]:
    """Return the knobs the fixture cannot redirect, by name."""
    return sorted(set(knobs) - _HITL_REGISTERED_KNOBS)


@pytest.fixture(scope="session", autouse=True)
def _hitl_persistence_knobs_are_registered():
    """Refuse to run when the HITL package reads an unregistered knob.

    A ``PDT_*`` variable the fixture does not know about cannot be
    redirected, so the suite would read and write the operator's tree
    through it.  Catching that here — at session start, with the knob
    named — is what keeps a new HITL persistence path from becoming the
    kind of failure that only shows up on the runs where two tests
    happen to be adjacent.
    """
    for source in _hitl_sources():
        unregistered = _unregistered_hitl_knobs(_scan_pdt_knobs(source))
        assert not unregistered, (
            f"the HITL source {source} reads {unregistered!r}, which the "
            f"test fixture does not redirect: add the knob to "
            f"_HITL_PERSISTENCE_ROOTS and redirect it in "
            f"isolated_plans_dir, or the suite will write into the "
            f"operator's tree"
        )


@pytest.fixture(autouse=True)
def isolated_plans_dir(monkeypatch, tmp_path):
    """Redirect PLANS_DIR to tmp_path for isolation.

    Defensive: some test sessions import a slimmed ``server`` module
    that does not expose ``PLANS_DIR``.  In that case the fixture is a
    no-op rather than failing collection.

    2026-10-21 (HITL): the fixture now also *proves* the redirect took
    effect, and proves that the HITL persistence surface starts empty.
    The two checks are the reason a new persistence path fails loudly
    instead of turning into a flake three tests later:

      * every registered persistence root must resolve inside this
        test's ``tmp_path`` — checked through the production
        resolvers, not through the variables this fixture sets;
      * the ``plan_decisions`` table of the per-test database must hold
        no rows when the test starts, so a decision asked by one test
        cannot be answered by another.

    Neither check creates a file: the fixture only points paths at the
    per-test root.
    """
    import server
    if hasattr(server, "PLANS_DIR"):
        monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    # The /progress endpoint and the executor's recovery path read
    # from the state-machine SQLite row, not a disk file. The
    # framework resolves the db path via ``_state_db_path`` which
    # defaults to ``<PLANS_DIR.parent> / state.db`` — for an isolated
    # test that would point outside ``tmp_path``. We redirect the db
    # to a per-test ``tmp_path / state.db`` so the test does not leak
    # into the developer's real state.db.
    if hasattr(server, "_state_db_path"):
        monkeypatch.setattr(
            server, "_state_db_path",
            lambda request=None: tmp_path / "state.db",
        )
    # 2026-09-13: also redirect the *env* knob to the same per-test file.
    # Patching ``server._state_db_path`` alone only covers the routes that
    # call ``server``'s copy; ``plan_state.PlanState``, ``task_manager``,
    # ``task_repository`` and ``agent._get_task_progress_repository`` each
    # have their own resolver and honour only ``PDT_STATE_DB_PATH``. Without
    # this line a test could drive ``PlanState`` into the session-scoped
    # temp DB while ``server``'s routes read ``tmp_path`` — two different
    # databases in one test, which is exactly the kind of split-state that
    # produces "the endpoint says X but the state machine says Y" flakes.
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    # 2026-09-14: same reasoning for the plans TREE. ``server.PLANS_DIR``
    # is patched above, but ``ExecutionLogger`` / ``plan_state.plans_dir``
    # / ``repair_generator`` / ``verification_agent`` resolve through
    # ``config_paths.resolve_plans_dir`` and honour only ``PDT_PLANS_DIR``.
    # Without this line a test that constructs one of those helpers
    # without an explicit directory still writes into the repo's live
    # ``plans/`` tree.
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))
    # 2026-10-21 (HITL): prove the redirect above took effect, and that
    # the decision table of the per-test database starts empty.  See the
    # section comment above for why the proof is worth the handful of
    # path resolutions it costs.
    _assert_hitl_persistence_isolated(tmp_path)
    _assert_decision_table_starts_empty(_resolve_state_db_path())
    yield tmp_path / "plans"


@pytest.fixture(autouse=True)
def hermetic_cc_switch_current_provider(monkeypatch):
    """Keep provider-selection tests off the operator's live CC Switch.

    The legacy dispatch walk asks CC Switch which provider is currently
    selected (``ClaudeCodingTool._load_cc_switch_current_provider``,
    2026-09-21) so a scene-less call stops inventing a hybrid
    name/endpoint. That lookup reads ``~/.cc-switch`` — the developer's
    live operator state — which would make every provider-selection test
    depend on whichever provider happens to be selected on the machine.

    Neutralise it by default (returns ``None`` → walk behaves as before).
    Tests that cover the shortcut patch the method with their own
    ``ProviderConfig``; the resolver itself is covered hermetically via a
    temp ``HOME``.
    """
    try:
        import coding_tool
    except Exception:  # slimmed test sessions without the module
        yield
        return
    monkeypatch.setattr(
        coding_tool.ClaudeCodingTool,
        "_load_cc_switch_current_provider",
        staticmethod(lambda: None),
        raising=False,
    )
    yield


#: ``(thread, process)`` for every application worker started during the
#: current test.  ``process`` is the object the worker waits on, when the
#: thread is an execution monitor; it stays ``None`` for the rest.
#:
#: It lives at module scope on purpose: the autouse fixture below owns the
#: *reclaim* at teardown, but a test that starts a worker needs to reclaim
#: it **before it ends**, not after — a worker that is still running when
#: the next test starts resolves its runtime paths (the state.db path is
#: resolved on every call) against the next test and writes there.  That
#: is the 2026-09-14 ``database disk image is malformed`` flake.
_TRACKED_WORKERS: "list[tuple[threading.Thread, object]]" = []


def _reclaim_workers(timeout: float = 10.0) -> "list[threading.Thread]":
    """Release every fake process a parked worker waits on, then join it.

    This is the "release, then join" half of the containment rule the
    autouse ``clean_execution_state`` fixture enforces at teardown, exposed
    so a test can apply it **while it still owns the worker**.  The order
    matters: releasing only wakes the worker, and the worker still has to
    run its tail — and that tail is where the state.db is opened and
    written.  Joining turns "usually finishes in time" into an ordering
    guarantee.

    Returns the workers that were still alive when the join gave up.
    """
    for _thread, process in list(_TRACKED_WORKERS):
        hook = getattr(process, "release", None) or getattr(
            process, "terminate", None
        )
        if callable(hook):
            try:
                hook()
            except Exception:  # noqa: BLE001 - best-effort unblock only
                pass
    # The join has to see the workers a released worker *spawns while it
    # runs*: the execution monitor spawns the usage-report thread from
    # its own tail, so a snapshot taken before the first join would leave
    # that thread running past teardown.  Iterate until no live worker is
    # left, so a worker that spawns a worker is reclaimed too.
    joined: set[int] = set()
    while True:
        pending = [
            thread
            for thread, _process in _TRACKED_WORKERS
            if thread.ident is not None
            and thread.is_alive()
            and id(thread) not in joined
        ]
        if not pending:
            break
        for thread in pending:
            joined.add(id(thread))
            thread.join(timeout=timeout)
    return [thread for thread, _process in _TRACKED_WORKERS if thread.is_alive()]


@pytest.fixture
def reclaim_workers():
    """Release, then join, the application workers this test started.

    The autouse ``clean_execution_state`` fixture applies the same rule at
    teardown — but teardown is too late for a worker whose tail writes to
    the state database, because that path is resolved **on every call**:
    a worker still running when the next test starts writes into the NEXT
    test's database (2026-09-14: ``database disk image is malformed``).
    A test that starts such a worker therefore has to reclaim it while it
    still owns it.

    The fake process a worker waits on must expose ``release()`` (or
    ``terminate()``) for the release half to work — see
    ``tests/api/test_execution_state_persistence.py`` for a fake that parks
    its worker on purpose and is still reclaimable.

    Returns the workers that were still alive when the join gave up.
    """
    return _reclaim_workers


@pytest.fixture(autouse=True)
def clean_execution_state(monkeypatch):
    """Clear global execution state after each test, and reclaim its workers.

    Defensive: only touches state attributes when they exist on the
    imported ``server`` module.

    Monitor containment (2026-09-25)
    --------------------------------
    The rule this enforces: **a test hands back every resource it starts.**
    Application threads are no exception — an execution monitor, a usage
    report refresh, a repair waiter. Only library threads (``anyio``,
    ``httpx``, pytest plugins) and the standard library's own are out of
    scope; they are not ours to reclaim and several live for the session.

    Why a leaked application thread is worse than a stray object:
    ``POST /api/execution/{id}/start`` spawns a daemon *monitor* thread
    that waits on the subprocess and then runs the completion path — the
    auto-verification loop included. That thread resolves its SQLite path
    through ``_state_db_path()`` / ``PDT_STATE_DB_PATH`` **on every call**,
    and the autouse ``isolated_plans_dir`` fixture re-points those at
    whichever test is running at that moment.

    So a monitor that outlives its own test does not finish quietly in its
    own sandbox — it moves on to the next test and writes *there*. On the
    way it loads ``PlanState``, which migrates the finished test's
    ``plan_state.json`` into a stranger's database as a fresh
    ``plan_routing`` row with no ``plan_execution`` sibling, and its
    remaining writes land in the same place. Two observed shapes:

      * ``crash_recovery``: ``assert_db_consistent`` raised
        ``[I2] plan_id='vp-003-explicit' ... (orphan CAS)`` — a different
        victim in every full-suite run, always green in isolation.
      * ``test_state_from_db.py``: its hand-built schema collided with the
        tables a stray monitor had already created —
        ``sqlite3.OperationalError: table plan_routing already exists``.

    Both are the same defect: a leaked monitor. Containment has two parts,
    and both are needed:

      * **Release.** ``FakeProcess``-style stubs often cap ``wait()`` at
        30 s and the monitor calls ``process.wait()`` with no argument, so
        an unreleased fake looks like "exited 0" half a minute later —
        long after the owning test's stubs were unwound. The process is
        reachable from ``_execution_state[plan_id]["process"]``, which the
        start endpoint stores several lines before building the thread;
        reading it *at thread-creation time* means no test-local fixture
        can clear it out from under us.
      * **Join.** Releasing only wakes the monitor; it still has to run
        its tail. Joining here — while the test's stubs and DB redirect
        are still in force (``monkeypatch`` is set up before this fixture,
        so it unwinds after it) — turns "usually finishes in time" into an
        ordering guarantee.

    A test must hand back everything it starts. A fake that parks its
    monitor on purpose (a test asserting the "running" state needs it
    parked) is therefore expected to expose a ``release`` / ``terminate``
    hook anyway — the assertion below treats a worker that survives its
    own test as a defect in whichever fake could not be reclaimed.
    """
    import server

    # Descriptors and child processes are reclaimed on every path out of
    # this fixture, including the slimmed-module path below, so the
    # registries are emptied before the test rather than after it: a
    # session interrupted mid-teardown would otherwise carry the last
    # test's registrations into the next one.
    _OPEN_FDS.clear()
    _CHILD_PROCESSES.clear()
    # The same rule for the verification-mode resources: the worktrees and
    # the servers a test registered belong to that test, and a session
    # interrupted mid-teardown must not carry them into the next one.
    clear_verification_registries()

    if not hasattr(server, "_execution_state"):
        try:
            yield
        finally:
            _reclaim_registered_resources()
            verification_problems = _reclaim_verification_resources()
            assert not verification_problems, (
                f"{len(verification_problems)} verification resource(s) outlived "
                f"their test:\n  "
                + "\n  ".join(verification_problems)
            )
        return

    #: The application workers started during this test.  Module scope, so
    #: a test can reclaim its own worker before it ends — see
    #: :func:`reclaim_workers`.
    workers = _TRACKED_WORKERS
    workers.clear()
    real_thread = threading.Thread

    def _is_app_worker(target) -> bool:
        """True when ``target`` is a function defined in this application.

        The rule is "no thread this application starts may outlive the
        test that started it" — not just the execution monitors. Threads
        belonging to libraries (``anyio``, ``httpx``, pytest plugins) and
        the standard library are left alone: they are not ours to reclaim
        and several are long-lived by design.
        """
        module_name = getattr(target, "__module__", None)
        module = sys.modules.get(module_name) if module_name else None
        module_file = getattr(module, "__file__", None)
        if not module_file:
            return False
        try:
            return Path(module_file).resolve().is_relative_to(_BACKEND_DIR)
        except (OSError, ValueError):  # pragma: no cover - defensive
            return False

    class _RecordingThread(real_thread):
        """A ``Thread`` that remembers the application workers it builds."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if not _is_app_worker(self._target):
                return
            process = None
            if self._target is getattr(server, "_run_in_plan_ctx", None):
                plan_id = self._args[0] if self._args else None
                state = server._execution_state.get(plan_id) or {}
                process = state.get("process")
            workers.append((self, process))

    monkeypatch.setattr(threading, "Thread", _RecordingThread)
    yield

    stuck = _reclaim_workers()

    # Before the assertion below, which ends this fixture when it fires
    # and would leave every registered descriptor open. A descriptor a
    # test was handed and never spent is the same defect as a worker that
    # outlived its test: it belongs to the process, and the next test is
    # the one that inherits it.
    _reclaim_registered_resources()

    # The same rule for the verification-mode resources: a worktree that
    # outlives its test keeps a checkout registered in the upstream
    # repository, and a server that outlives its test answers the next
    # run's health probe. Both are reclaimed here, before any assertion
    # below can end this fixture.
    verification_problems = _reclaim_verification_resources()

    if hasattr(server, "_execution_state"):
        server._execution_state.clear()
    if hasattr(server, "_execution_locks"):
        server._execution_locks.clear()
    assert not stuck, (
        f"{len(stuck)} application worker thread(s) outlived their test. "
        f"Every test hands back what it starts: a worker still running past "
        f"teardown resolves the runtime paths against a later test and "
        f"writes there, which is how unrelated tests fail at random. If "
        f"this fires, either the test spawns something it does not stop, or "
        f"the fake process the worker waits on needs a ``release`` (or "
        f"``terminate``) hook so this fixture can unblock it — see "
        f"tests/api/test_execution_state_persistence.py for a fake that "
        f"parks its worker on purpose and is still reclaimable."
    )
    assert not verification_problems, (
        f"{len(verification_problems)} verification resource(s) outlived their "
        f"test:\n  "
        + "\n  ".join(verification_problems)
        + "\nEvery test hands back what it starts: a verification worktree or "
        "a server that outlives its test keeps writing into a later run's "
        "tree, and a leftover directory is litter nobody remembers to clean."
    )


# ---------------------------------------------------------------------------
# Provider secrets: isolating the memo, minting canaries, and reclaiming
# the descriptors and children a credential test starts.
# ---------------------------------------------------------------------------
#
# ``backend/credentials.py`` memoises a resolved secret for the life of
# the *process*, and pytest runs a whole shard in one interpreter. One
# test that turns the keychain path on and reads a value successfully
# therefore leaves that answer behind for every test that runs after it,
# in a direction nobody can predict from the file: the next test either
# inherits a secret it never configured, or — the other half of the same
# coin — a stale "missing" hides the value its own environment provides.
# Both are the "green alone, red in the suite" failure, and both move
# whenever a file is added.
#
# Three pieces, each usable on its own by the tests that follow this
# change:
#
#   * ``reset_credentials_cache_per_test`` (autouse) empties the memo
#     around every test, through the module's own public entry point —
#     ``credentials.reset_cache()``, which exists precisely so a caller
#     can end a memo's lifetime without a restart.
#   * ``canary`` mints a value that is unmistakably synthetic.
#   * ``register_open_fd`` / ``register_child_process`` hand a descriptor
#     or a child to the teardown that already reclaims a test's workers,
#     so the provider's own resources ride the existing rule rather than
#     a second one somebody has to remember.

#: Descriptors and child processes a test registered, and what the most
#: recent teardown reclaimed. Module-level because what they describe
#: belongs to the process, not to a test.
_OPEN_FDS: list = []
_CHILD_PROCESSES: list = []
_RECLAIMED_FDS: list = []
_RECLAIMED_CHILDREN: list = []

#: The marker every canary carries. It is what makes a value legible in
#: the places a fixture value actually turns up — a captured environment,
#: a log line, a screenshot of a notification — and it is also what
#: keeps the value away from the shape a real credential has. A secret
#: scanner is looking for provider-shaped strings, and a canary that
#: matched one would be a canary somebody eventually has to add to an
#: allowlist, which is an allowlist that will also miss the real thing.
_CANARY_MARKER = "pdt-test"

#: The prefix template per kind. Only the prefix is written down; the
#: unique suffix is derived at call time (below), so no complete canary
#: value is a literal anywhere in this repository.
#:
#: The two index entries keep the *shape* their real counterparts have —
#: a Feishu app id reads as ``cli_…``, a Telegram chat id as ``-100…`` —
#: so a test handed one walks the path a deployment would walk. An index
#: is not a secret, but a lookup under a key no deployment could produce
#: is a lookup that fails before the behaviour under test is reached.
_CANARY_KINDS = {
    "feishu_secret": _CANARY_MARKER + "-feishu-secret",
    "telegram_token": _CANARY_MARKER + "-telegram-token",
    "feishu_index": "cli_" + _CANARY_MARKER + "-feishu-index",
    "telegram_index": "-100" + _CANARY_MARKER + "-telegram-index",
}

#: How many hex characters the derived suffix carries. Long enough that
#: two values in one test have not shared one across the suite's history;
#: short enough that a reader scanning a log still reads the whole thing.
_CANARY_SUFFIX_LENGTH = 8

_CANARY_LOCK = threading.Lock()
_CANARY_ISSUED = 0


def _next_canary_suffix(scope) -> str:
    """Return a short digest that no earlier call in this process returned.

    Derived from the calling test's directory and a per-process counter,
    and that pairing is what makes the two guarantees hold at once: the
    counter separates two calls made by the *same* test (which are given
    the same directory), and the directory separates two tests (whose
    counters are consecutive but whose digests are of different inputs).
    A digest rather than a random value, so a failure that prints a canary
    prints the same string on the next run.
    """
    global _CANARY_ISSUED
    with _CANARY_LOCK:
        _CANARY_ISSUED += 1
        serial = _CANARY_ISSUED
    seed = "{}:{}".format(scope, serial)
    return hashlib.sha256(seed.encode("utf-8", "surrogateescape")).hexdigest()[
        :_CANARY_SUFFIX_LENGTH
    ]


def _register_open_fd(fd: int) -> int:
    """Hand ``fd`` to the teardown; return it, so a call can be inlined.

    Registration rather than a blind sweep: a descriptor the suite never
    handed out is not the suite's to close, and a test that mints one by
    hand has its own lifetime to manage.
    """
    _OPEN_FDS.append(fd)
    return fd


def _register_child_process(proc):
    """Hand ``proc`` to the teardown; return it, so a call can be inlined.

    Same rule as the descriptor: only a process the test declares is
    reaped, because the suite's other processes — a server started by a
    fixture, a helper a library owns — have owners of their own.
    """
    _CHILD_PROCESSES.append(proc)
    return proc


def _join_child(proc) -> None:
    """Stop ``proc`` if it is still running, then wait for it.

    Both halves, in this order, for the reason ``clean_execution_state``
    gives for its workers: terminating only *asks* a child to stop, and a
    child that has been asked and not waited on is a zombie — one that
    still holds its table entry and is still this test's responsibility
    until something calls ``wait`` on it. The cap is generous for the
    same reason the thread join's is: it covers a loaded machine, not a
    child that refuses to die.
    """
    if not callable(getattr(proc, "poll", None)):
        # Not a process — a stand-in without a wait, which there is
        # nothing to do about and no reason to fail the test over.
        return
    try:
        if proc.poll() is None:
            hook = getattr(proc, "terminate", None) or getattr(proc, "kill", None)
            if callable(hook):
                hook()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001 - best-effort teardown, never the verdict
        pass


def _reclaim_registered_resources() -> None:
    """Close every registered descriptor and join every registered child.

    Called from ``clean_execution_state`` on every path out of that
    fixture, because it is the one place the suite already states the
    rule this enforces: a test hands back every resource it starts.
    Registration makes the two symmetric — a worker thread is reclaimed
    whether or not the test announced it, and a descriptor or a child
    only when it did, so a test that starts one has to say so. That
    asymmetry is the price of not closing descriptors the suite did not
    open, and it is the cheaper of the two mistakes.
    """
    fds, children = list(_OPEN_FDS), list(_CHILD_PROCESSES)
    _OPEN_FDS.clear()
    _CHILD_PROCESSES.clear()
    _RECLAIMED_FDS.clear()
    _RECLAIMED_CHILDREN.clear()

    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            # Already closed — by the code under test, or by a test that
            # read the wire form directly. Both are correct endings, and
            # both still count as reclaimed: the descriptor is not the
            # suite's any more.
            pass
        _RECLAIMED_FDS.append(fd)

    for proc in children:
        _join_child(proc)
        _RECLAIMED_CHILDREN.append(proc)


def _reclaim_verification_resources() -> list:
    """Reclaim the worktrees and the servers a verification-mode test started.

    Returns the problems rather than raising: the reclamation has to run
    to the end before any verdict is delivered, and a raise from the middle
    of it would leave the rest of the resources behind.  A leftover is a
    leftover whether or not the removal failed — the point of the second
    list is the directory that is still there after the removal.
    """
    problems = reclaim_verification_resources()
    problems.extend(detect_leftover_resources())
    return problems


@pytest.fixture(autouse=True)
def reset_credentials_cache_per_test():
    """Empty the provider credentials memo around every test.

    Autouse, and run on both sides of the test, because the pollution it
    prevents arrives from two directions. Resetting before means a test
    never starts from an answer another test resolved; resetting after
    means it never leaves one behind for the next. Either half alone
    leaves a hole, and the hole is exactly the order-dependent failure
    described above.

    Through ``credentials.reset_cache()`` rather than by clearing the
    dict: the module exposes that entry point for callers who cannot
    restart the process, and a conftest that reached past it would be a
    second, private way of doing the same thing — the kind that rots the
    moment the memo grows a second store behind one name.

    The import is local, like the other fixtures in this file that need a
    backend module: a slimmed test session that does not ship
    ``credentials`` gets a no-op instead of a collection error.
    """
    try:
        import credentials
    except ImportError:  # pragma: no cover - slimmed session
        yield
        return

    credentials.reset_cache()
    try:
        yield
    finally:
        credentials.reset_cache()


@pytest.fixture
def canary():
    """Return a factory that mints a synthetic provider credential.

    Called as ``canary(kind, tmp_path)``: the kind selects the prefix
    template, the path identifies the test whose value this is. The
    returned string is ``<prefix>-<8 derived hex characters>``.

    Four rules, each of which is a way a test fixture can go wrong:

    * **The value is obviously synthetic.** It is not a plausible
      credential that happens to be wrong; it says, in the first eight
      characters, that it is test data. A placeholder is most often read
      in a place nobody will go back to the test for — a captured
      environment, an assertion message, a log the CI job kept.
    * **The value is not the shape of a real one.** A canary that
      matched a bot token or a bare app secret would be a string a
      scanner has to be told to skip, and a scanner told to skip one
      shape is a scanner that will miss the next one.
    * **Every call is a different string.** Two tests minting the same
      value cannot be told apart in a failure, and a value reused within
      one test cannot prove that a per-test reset happened.
    * **Nothing is written to disk.** The value is assembled here and
      handed over; a stand-in keychain or a forged ``.env`` belongs in
      the test's own ``tmp_path``, which is the only directory a test may
      write without deciding whose machine it is.
    """
    def _make(kind: str, path) -> str:
        try:
            prefix = _CANARY_KINDS[kind]
        except KeyError:
            # Failed here rather than answered with an invented value: a
            # fixture that guesses leaves the test running to completion
            # against something it made up, and a later failure points at
            # the provider instead of at the typo.
            pytest.fail(
                "unknown canary kind {!r}; the four are: {}".format(
                    kind, ", ".join(sorted(_CANARY_KINDS))
                )
            )
        return "{}-{}".format(prefix, _next_canary_suffix(path))

    return _make


@pytest.fixture
def register_open_fd():
    """Hand a descriptor to the teardown that reclaims a test's resources.

    Returns the registration function rather than being one, so a caller
    that already has a descriptor can write ``fd = register_open_fd(fd)``
    and keep the value it was going to use anyway.
    """
    return _register_open_fd


@pytest.fixture
def register_child_process():
    """Hand a child process to the teardown that reclaims a test's resources.

    Same shape as ``register_open_fd``, and for the same reason: a test
    that starts a process and never waits on it has left a zombie, and
    this is the one place in the suite that will notice.
    """
    return _register_child_process


@pytest.fixture
def reclaimed():
    """Return a reader for what the most recent teardown reclaimed.

    Reads the *last* teardown's record, so a test can assert on what the
    fixture did to a resource a previous test left — which is the only
    way to observe a teardown from inside pytest, since the test that
    started the resource is over by the time the fixture has closed it.
    """
    def _read() -> dict:
        return {
            "fds": tuple(_RECLAIMED_FDS),
            "children": tuple(_RECLAIMED_CHILDREN),
        }

    return _read


def residual_secret_env_report(names) -> dict:
    """Report, per requested name, whether it is present in ``os.environ``.

    A **reporter, not an executor.** The invariant behind it — a provider
    secret reaches a child process through the keychain or a file
    descriptor, never through the environment — cannot be established by
    anything inside this repository: a value exported by the parent shell
    is inherited by every child the operator starts, and no code change
    here can reach back into that shell. So acceptance is two-staged
    (test design decision point 9): the code-side assertions come first,
    and this is what verifies the operational migration afterwards — a
    residue the code cannot remove is diagnosed, not hidden.

    Three properties, each of which is a way a diagnostic can make things
    worse:

    * **Read-only.** It reads ``os.environ`` and writes nothing. A probe
      that popped the key it found would delete the evidence and leave
      the machine exactly as dirty: the next test would read a clean
      environment, the security assertion downstream would pass, and
      every child the operator starts would still inherit the secret.
    * **Per key, never a single answer.** The finding is acted on by
      editing a shell profile or a job definition, and which file that is
      depends on *which* variable is still exported. One boolean over the
      whole set cannot name it.
    * **Existence only, never a value.** The report is destined for a
      failure message and a CI log; a probe that echoed what it found
      would publish the secret at the moment somebody is already reading
      about it. A key that is present and a key that is empty are the
      same finding — both mean the environment will hand something to a
      child — so presence is the whole of what there is to report.

    Returns a plain ``dict`` in the order ``names`` was given, so a
    caller can map a line of the report back to the variable it names.
    """
    return {name: name in os.environ for name in names}


@pytest.fixture
def residual_secret_env():
    """Return :func:`residual_secret_env_report`.

    A fixture wrapper rather than a module-level import, for the reason
    ``test_bugfix_fixtures.py`` gives for the plan factories: pytest
    resolves ``conftest.py`` by directory, so this works whether pytest
    was invoked from the project root (``backend/tests/...``) or from
    ``backend/`` (``tests/...``), and a ``from conftest import ...`` in a
    test would break in the second case.
    """
    return residual_secret_env_report


class StateDbReader:
    """Read-only view over the state-machine SQLite tables for one test.

    Why this exists (2026-09-13): the workflow used to persist its state
    as on-disk JSON next to each plan (``plans/{id}/plan_state.json``,
    ``plans/{id}/execution.json``). Both files were retired — the
    ``plan_routing`` / ``plan_execution`` / ``plan_verification`` rows in
    ``state.db`` are now the single source of truth (see
    ``plan_state.py``'s module docstring and the three "legacy
    ``execution.json`` read was removed" notes in ``server.py``).

    A large number of tests still asserted the removed files, so they
    failed with "file not found" instead of "state is wrong". This reader
    gives them a one-line way to assert the *same contract* against the
    table that actually holds the data, so the test side reads SQLite
    exactly the way production does.

    Bound to the per-test ``tmp_path`` DB that the autouse
    ``isolated_plans_dir`` fixture installs, so a read can never touch
    the live ``state.db``.
    """

    _TABLES = ("plan_routing", "plan_execution", "plan_verification", "plan_artifacts")

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    # -- internals ---------------------------------------------------------

    def _row(self, table: str, plan_id: str) -> dict | None:
        if not self.db_path.exists():
            return None
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate

        conn = _open_db(self.db_path)
        try:
            _migrate(conn)
            cur = conn.execute(
                f"SELECT * FROM {table} WHERE plan_id = ?", (plan_id,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        finally:
            conn.close()

    # -- public API --------------------------------------------------------

    def routing(self, plan_id: str) -> dict | None:
        """``plan_routing`` row — the ``plan_state.json`` successor."""
        return self._row("plan_routing", plan_id)

    def execution(self, plan_id: str) -> dict | None:
        """``plan_execution`` row — the ``execution.json`` successor."""
        return self._row("plan_execution", plan_id)

    def verification(self, plan_id: str) -> dict | None:
        """``plan_verification`` row — the verification loop state."""
        return self._row("plan_verification", plan_id)

    def current_phase(self, plan_id: str) -> str | None:
        row = self.routing(plan_id)
        return row.get("current_phase") if row else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StateDbReader({self.db_path})"


@pytest.fixture
def state_db_reader(tmp_path) -> StateDbReader:
    """Reader bound to this test's ``tmp_path/state.db``.

    The autouse ``isolated_plans_dir`` fixture points both
    ``PDT_STATE_DB_PATH`` and ``server._state_db_path`` at this same
    file, so a reader built here sees exactly what the code under test
    wrote.
    """
    return StateDbReader(tmp_path / "state.db")



@pytest.fixture
def sample_plan_factory(tmp_path):
    """Factory that creates a minimal plan directory with all required files."""
    def _make(plan_id="test-plan", phase="ready", extra_files=None):
        plan_dir = tmp_path / "plans" / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)

        # plan_state.json
        (plan_dir / "plan_state.json").write_text(
            json.dumps({
                "plan_id": plan_id,
                "current_phase": phase,
                "completed_phases": [],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {},
            }),
            encoding="utf-8",
        )

        # interview.json
        (plan_dir / "interview.json").write_text(
            json.dumps({"requirement": "test requirement"}),
            encoding="utf-8",
        )

        # prd.json
        (plan_dir / "prd.json").write_text(
            json.dumps({"prd": "test prd"}),
            encoding="utf-8",
        )

        # review.json
        (plan_dir / "review.json").write_text(
            json.dumps({"reviewed": True}),
            encoding="utf-8",
        )

        # tasks.json
        (plan_dir / "tasks.json").write_text(
            json.dumps({"tasks": []}),
            encoding="utf-8",
        )

        if extra_files:
            for name, content in extra_files.items():
                (plan_dir / name).write_text(
                    json.dumps(content) if isinstance(content, (dict, list)) else content,
                    encoding="utf-8",
                )

        return plan_dir

    return _make


@pytest.fixture
def mock_subprocess(monkeypatch):
    """Parameterized Popen mock for lifecycle tests."""
    class FakeProcess:
        def __init__(self, returncode=0, block_on_wait=None):
            self.pid = 12345
            self._returncode = returncode
            self._block = block_on_wait
            self._terminated = False

        @property
        def returncode(self):
            return self._returncode

        @property
        def stdout(self):
            return iter([])

        def wait(self, timeout=None):
            if self._block is not None:
                self._block.wait(timeout=10)
            return self._returncode

        def poll(self):
            if self._terminated or (self._block is not None and self._block.is_set()):
                return self._returncode
            return None

        def terminate(self):
            self._terminated = True
            if self._block is not None:
                self._block.set()

    def _patch(returncode=0, block_on_wait=None):
        monkeypatch.setattr(
            "server.subprocess.Popen",
            lambda *args, **kwargs: FakeProcess(returncode=returncode, block_on_wait=block_on_wait),
        )

    return _patch


# ---------------------------------------------------------------------------
# Subagent isolation fixtures (decision point 5)
#
# These back the three orthogonal assertions in
# backend/tests/unit/test_subagent_isolation.py:
#   1) URL blackbox: execution.log [SUBAGENT_PROVIDER] base_url != parent-proxy.invalid
#   2) SQLite self-consistency: ~/.pdt/subagent_metrics.db total_tokens > 0
#   3) cc-switch quota reconciliation: API usage within 5% of (2)
# ---------------------------------------------------------------------------


@pytest.fixture
def time_window():
    """Return a (start, end) ISO-8601 UTC tuple spanning a 1-hour window.

    Both timestamps are derived from ``datetime.utcnow()`` so the window
    always contains "now", and the end is start + 1h. Tests can use the
    tuple to filter rows from ``~/.pdt/subagent_metrics.db`` (started_at
    range) and to scope a cc-switch usage query.

    Returns
    -------
    tuple[str, str]
        ``(start_iso, end_iso)`` — both ISO 8601 UTC, ``%Y-%m-%dT%H:%M:%SZ``.
    """
    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(hours=1)
    return (
        start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


@pytest.fixture
def mock_execution_log(tmp_path):
    """Write a fake ``execution.log`` JSON-lines file with a few
    ``[SUBAGENT_PROVIDER]`` entries plus surrounding noise.

    The fixture returns the path to the log file. Default content
    contains:
      * 1 ``task_started`` JSON-line (warm-up entry)
      * 1 ``[SUBAGENT_PROVIDER]`` line with ``base_url`` set to
        ``https://api.vendor-a.example/anthropic`` (the vendor-a-pro
        endpoint, NOT parent-proxy.invalid)
      * 1 ``[SUBAGENT_PROVIDER]`` line with a different
        vendor-a-pro base URL (multi-call evidence)
      * 1 ``task_completed`` JSON-line (closing entry)

    Tests that need a different shape (no log file, log file with
    proxy URL only, log file with zero events) should *not* use this
    fixture and should write their own fixture/contents.
    """
    log_file = tmp_path / "execution.log"
    base_urls = [
        "https://api.vendor-a.example/anthropic",
        "https://api.vendor-a-pro.example/v1",
    ]
    # Anchor timestamps to ``utcnow()`` so the [SUBAGENT_PROVIDER] lines
    # always land inside the dynamic ``time_window`` fixture (which is
    # also derived from utcnow()). Using a hard-coded 2026-06-01
    # timestamp would silently fail the self-consistency check on any
    # test run whose window is "now".
    now = datetime.utcnow()
    ts_started = (now - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%S")
    ts_provider = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_completed = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S")
    lines = []
    lines.append(json.dumps({
        "ts": ts_started,
        "level": "INFO",
        "event": "task_started",
        "message": "warm-up entry",
        "task_id": "1-1",
        "phase": "task_execution",
    }))
    for url in base_urls:
        lines.append(
            f"[SUBAGENT_PROVIDER] base_url={url} "
            f"timestamp={ts_provider}"
        )
    lines.append(json.dumps({
        "ts": ts_completed,
        "level": "INFO",
        "event": "task_completed",
        "message": "closing entry",
        "task_id": "1-1",
        "phase": "task_execution",
    }))
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_file


@pytest.fixture
def mock_subagent_metrics_db(tmp_path, monkeypatch):
    """Create a fake ``~/.pdt/subagent_metrics.db`` SQLite database and
    redirect ``$HOME`` so the production code path resolves to the
    fake file.

    The schema is the exact one from
    ``backend/coding_tool_hooks/init_metrics_db.sh``:

        CREATE TABLE subagent_token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,
            task_summary TEXT,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            started_at TIMESTAMP NOT NULL,
            ended_at TIMESTAMP NOT NULL
        );

    Two rows are inserted by default (one inside the test ``time_window``,
    one outside it) so the SQLite self-consistency assertion can verify
    window filtering and total_tokens > 0 simultaneously.

    Returns
    -------
    Path
        The path to the fake database file.
    """
    # Isolate from the real ~/.pdt: redirect $HOME so any code path that
    # expands ``~/`` or reads $HOME lands on tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_home = tmp_path / ".pdt"
    fake_home.mkdir(parents=True, exist_ok=True)
    db_path = fake_home / "subagent_metrics.db"

    # Pin row timestamps to ``utcnow()`` so the in-window rows always
    # fall inside the dynamic ``time_window`` fixture (which is also
    # derived from utcnow()). Using a hard-coded 2026-06-01 timestamp
    # would silently fail on any test run whose window is "now".
    in_window_now = datetime.utcnow()
    out_of_window_now = in_window_now - timedelta(hours=24)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subagent_token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                task_summary TEXT,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                started_at TIMESTAMP NOT NULL,
                ended_at TIMESTAMP NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_task_type
                ON subagent_token_usage(task_type);
            CREATE INDEX IF NOT EXISTS idx_started_at
                ON subagent_token_usage(started_at);
            """
        )
        # Row 1: in-window, counts toward total_tokens
        started_1 = in_window_now - timedelta(minutes=30)
        ended_1 = started_1 + timedelta(seconds=1)
        conn.execute(
            "INSERT INTO subagent_token_usage "
            "(task_type, task_summary, input_tokens, output_tokens, "
            "started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "general",
                "fake subagent run #1",
                1000,
                2000,
                started_1.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ended_1.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
        # Row 2: in-window, additional tokens
        started_2 = in_window_now - timedelta(minutes=20)
        ended_2 = started_2 + timedelta(seconds=1)
        conn.execute(
            "INSERT INTO subagent_token_usage "
            "(task_type, task_summary, input_tokens, output_tokens, "
            "started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "general",
                "fake subagent run #2",
                500,
                1500,
                started_2.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ended_2.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
        # Row 3: out-of-window (well before the 1h time_window fixture)
        conn.execute(
            "INSERT INTO subagent_token_usage "
            "(task_type, task_summary, input_tokens, output_tokens, "
            "started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "general",
                "old subagent run, outside window",
                9999,
                9999,
                out_of_window_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                (out_of_window_now + timedelta(seconds=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture
def cc_switch_api_mock(monkeypatch):
    """Return a factory that patches the cc-switch usage query path
    for the vendor-a-pro provider, plus a probe helper that returns
    whether the patch is *active* (i.e. a real local cc-switch API
    would also be reachable, used to drive ``skipif``).

    The factory signature is::

        cc_switch_api_mock(provider_id="vendor-a-pro",
                           tokens_used=3000,   # total in [start, end]
                           reachable=True,      # does the local API
                                              # endpoint respond?
                           http_status=200)

    When ``reachable`` is True, calls to the cc-switch
    ``/api/usage`` endpoint (or any helper that fetches
    vendor-a-pro usage in a window) return a fake
    ``ProviderUsageDto``-shaped dict with ``tiers[0].remaining`` and
    ``tiers[0].used`` aggregated to ``tokens_used`` (consumed tokens).

    When ``reachable`` is False, the mock raises ``ConnectionError``
    to simulate an unreachable cc-switch REST API.

    Returns
    -------
    Callable
        ``_patch(provider_id, tokens_used, reachable, http_status)``
        that registers the mock and returns the mock object for
        assertions. Also exposes ``.reachable`` so the
        ``test_cc_switch_quota_reconcile`` function can
        ``@pytest.mark.skipif`` itself based on the runtime probe.
    """
    import urllib.error

    state = {"reachable": True, "calls": 0}

    def _patch(
        provider_id: str = "vendor-a-pro",
        tokens_used: int = 3000,
        reachable: bool = True,
        http_status: int = 200,
    ) -> MagicMock:
        state["reachable"] = reachable
        state["calls"] = 0
        state["provider_id"] = provider_id
        state["tokens_used"] = tokens_used

        mock = MagicMock()

        def _fake_urlopen(request, *args, **kwargs):
            state["calls"] += 1
            if not reachable:
                raise urllib.error.URLError("cc-switch API unreachable")
            if http_status != 200:
                raise urllib.error.HTTPError(
                    request.full_url, http_status, "HTTP Error",
                    {}, None,
                )
            body = {
                "providers": [
                    {
                        "provider": provider_id,
                        "app_type": "claude",
                        "tiers": [
                            {
                                "name": "5h",
                                "used_percent": 0.0,
                                "remaining": 100000 - tokens_used,
                                "total": 100000,
                                "unit": "tokens",
                                "resets_at": None,
                            },
                        ],
                        "updated_at": "2026-06-01T00:00:00Z",
                    },
                ],
            }
            return _FakeResponse(body)

        monkeypatch.setattr(
            "urllib.request.urlopen", _fake_urlopen, raising=False,
        )
        return mock

    def _is_reachable() -> bool:
        """Runtime probe — does the local cc-switch API answer?

        Used by ``test_cc_switch_quota_reconcile`` via
        ``@pytest.mark.skipif(not is_cc_switch_api_available(), ...)``
        so the test self-skips in environments where cc-switch is not
        running locally.

        The endpoint comes from ``PDT_TEST_CC_SWITCH_API_URL`` and has
        **no default**: the address is a property of one operator's
        machine, not of this project, and the production code never
        calls this API at all (it reads the cc-switch SQLite store —
        see ``backend/cc_switch.py``). Unset means "not configured",
        which is the same skip as "not reachable".
        """
        base = os.environ.get("PDT_TEST_CC_SWITCH_API_URL", "").strip()
        if not base:
            return False
        try:
            import urllib.request
            with urllib.request.urlopen(
                f"{base.rstrip('/')}/api/usage", timeout=1,
            ) as r:
                return r.status == 200
        except Exception:
            return False

    _patch.reachable = state
    _patch.is_reachable = _is_reachable
    return _patch


class _FakeResponse:
    """Minimal ``http.client.HTTPResponse`` stand-in used by
    ``cc_switch_api_mock``. Only ``read()`` and ``status`` are needed
    by the production code path that the test patches.
    """
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

# ---------------------------------------------------------------------------
# CLI option registration: ``--e2e`` enables the e2e lifecycle gate.
# ---------------------------------------------------------------------------
#
# The e2e three-defense test (``test_e2e_three_layer_defense`` in
# ``tests/test_server_json_cleanup.py``) spawns a real server.py on
# ``EXEC_PORT=8001`` and exercises the full /api/execution lifecycle.
# It is opt-in via ``--e2e`` so the standard unit-test pass stays
# fast; without this flag, pytest would reject ``--e2e`` with
# "unrecognized arguments: --e2e".


def pytest_addoption(parser):
    """Register the ``--e2e`` CLI flag."""
    parser.addoption(
        "--e2e",
        action="store_true",
        default=False,
        help="Run the e2e three-defense lifecycle gate (default: skip).",
    )


# ---------------------------------------------------------------------------
# Bug-fix mock plan factories (state machine / migration / API / E2E tiers)
# ---------------------------------------------------------------------------
#
# These three helpers give the state-machine, migration, API and E2E tests a
# single source of truth for "what a plan on disk looks like".  Before they
# existed each test hand-rolled its own dict literal, so a schema change in
# ``plan_state.json`` had to be chased through a dozen files and the tiers
# silently drifted apart.
#
# Isolation contract
# ------------------
# Every call returns a FRESH dict with FRESH nested containers.  No mutable
# default arguments, no module-level shared literals: mutating the result of
# one call can never be observed by another.  This is what
# ``test_mock_plan_factories_are_isolated`` pins.
#
# Schema contract
# ---------------
# ``assert_plan_state_schema`` / ``assert_verification_report_schema`` raise
# ``AssertionError`` when a required key is missing, so a test that builds a
# state by hand (rather than via the factory) still fails loudly instead of
# silently exercising a half-populated fixture.

#: Keys every ``plan_state.json`` payload must carry.
PLAN_STATE_REQUIRED_KEYS = (
    "plan_id",
    "current_phase",
    "completed_phases",
    "review_rounds",
    "flags",
    "verification",
)

#: Keys every ``verification`` sub-object must carry.
VERIFICATION_REQUIRED_KEYS = ("status", "round", "max_rounds", "stop_reason")

#: Keys every ``verification_report.json`` payload must carry.
VERIFICATION_REPORT_REQUIRED_KEYS = (
    "plan_id",
    "round",
    "overall_status",
    "verification_points",
    "requirement_deviations",
    "summary",
)


def make_mock_plan_state(
    verification_status="pending",
    current_phase="completed",
    *,
    plan_id="test-plan",
    flags=None,
    completed_phases=None,
    review_rounds=None,
    verification_round=0,
    max_rounds=3,
    stop_reason=None,
):
    """Build a ``plan_state.json``-shaped dict.

    ``verification_status`` and ``current_phase`` are positional so the
    common call reads like the task spec's example::

        state = make_mock_plan_state(
            "pending", "completed",
            flags={"arch_enabled": False, "test_enabled": False},
        )

    Every mutable container in the returned dict is freshly constructed on
    each call (including when the caller passes ``flags`` /
    ``completed_phases`` — those are *copied*, never aliased), so two calls
    can never share state.
    """
    return {
        "plan_id": plan_id,
        "current_phase": current_phase,
        # ``list(...)`` / ``dict(...)`` both copy AND normalise, so passing a
        # tuple or a generator works and the caller's object is never aliased.
        "completed_phases": list(completed_phases) if completed_phases else [],
        "review_rounds": (
            dict(review_rounds)
            if review_rounds
            else {"prd": 0, "arch": 0, "test": 0}
        ),
        "flags": dict(flags) if flags else {},
        "verification": {
            "status": verification_status,
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": stop_reason,
        },
    }


def make_mock_verification_report(
    overall_status="PASSED",
    *,
    plan_id="test-plan",
    round=0,
    verification_points=None,
    requirement_deviations=None,
    summary=None,
):
    """Build a ``verification_report.json``-shaped dict.

    Same isolation contract as :func:`make_mock_plan_state`: all nested
    containers are freshly built (or copied) per call.
    """
    points = [dict(p) for p in verification_points] if verification_points else []
    deviations = (
        [dict(d) for d in requirement_deviations] if requirement_deviations else []
    )
    if summary is not None:
        computed_summary = dict(summary)
    else:
        computed_summary = {
            "passed": sum(1 for p in points if p.get("status") == "PASSED"),
            "failed": sum(1 for p in points if p.get("status") == "FAILED"),
            "skipped": sum(1 for p in points if p.get("status") == "SKIPPED"),
            "total": len(points),
        }
    return {
        "plan_id": plan_id,
        "round": round,
        "overall_status": overall_status,
        "verification_points": points,
        "requirement_deviations": deviations,
        "summary": computed_summary,
    }


def assert_plan_state_schema(state):
    """Raise ``AssertionError`` when ``state`` is missing a required key."""
    assert isinstance(state, dict), (
        f"plan_state must be a dict, got {type(state).__name__}"
    )
    missing = [k for k in PLAN_STATE_REQUIRED_KEYS if k not in state]
    assert not missing, f"plan_state missing required key(s): {missing}"
    verification = state["verification"]
    assert isinstance(verification, dict), (
        f"plan_state['verification'] must be a dict, "
        f"got {type(verification).__name__}"
    )
    missing_v = [k for k in VERIFICATION_REQUIRED_KEYS if k not in verification]
    assert not missing_v, (
        f"plan_state['verification'] missing required key(s): {missing_v}"
    )




#: Alias for :func:`assert_plan_state_schema` -- spec-named
#: ``validate_plan_state_schema`` (decision point 7). Both names
#: accept the same dict and raise ``AssertionError`` on missing keys.
validate_plan_state_schema = assert_plan_state_schema


def assert_verification_report_schema(report):
    """Raise ``AssertionError`` when ``report`` is missing a required key."""
    assert isinstance(report, dict), (
        f"verification_report must be a dict, got {type(report).__name__}"
    )
    missing = [k for k in VERIFICATION_REPORT_REQUIRED_KEYS if k not in report]
    assert not missing, f"verification_report missing required key(s): {missing}"




def make_mock_sidecar_state(
    sidecar_type="verification_executor",
    *,
    plan_id="test-plan",
    round=0,
    events=None,
    captured_at=None,
):
    """Build a sidecar-state dict (verification-executor / plan-state sidecar).

    Sidecars are auxiliary JSON files written alongside plan artifacts to
    capture in-flight VP events or plan-state transitions without round-
    tripping through the primary artifact file. This factory provides a
    single source of truth for the sidecar schema used by unit, integration,
    API and E2E test modules.

    Isolation contract: every call returns a FRESH dict with FRESH nested
    containers (events list is copied, not aliased).
    """
    return {
        "sidecar_type": sidecar_type,
        "plan_id": plan_id,
        "round": round,
        "events": list(events) if events else [],
        "captured_at": captured_at,
    }


def seed_plan_sqlite(
    plan_dir: Path,
    plan_id: str | None = None,
    *,
    phase: str | None = None,
    project_dir: str | Path | None = None,
    verification: dict | None = None,
    verification_round: int | None = None,
    verification_status: str | None = None,
    verification_results: dict | None = None,
    max_rounds: int | None = None,
    stop_reason: str | None = None,
) -> Path:
    """Seed the state-machine SQLite rows a plan fixture implies.

    Why this exists
    ---------------
    Production is SQLite-first: ``PlanState`` reads ``plan_routing``,
    ``plan_execution`` holds ``project_dir``, and
    ``plan_verification`` holds the round/verdict columns. Plan
    fixtures used to materialise only the *legacy on-disk* artifacts
    (``plans/<id>/plan_state.json``, ``execution.json``), which
    production stopped reading — so the fixture looked complete on
    disk while every endpoint answered 404 / 400 "plan not found" /
    "missing project directory". That mismatch accounted for ~40
    failures in the 2026-09-14 full-suite run (``test_verification_api``
    alone had 26).

    What it does
    ------------
    1. Constructs ``PlanState(plan_dir)`` once — the documented one-shot
       migration path — so the plan's ``plan_state.json`` contents
       (``current_phase`` / ``completed_phases`` / ``review_rounds`` /
       ``flags`` / ``verification``) land in a ``plan_routing`` row.
    2. Re-asserts ``current_phase`` from ``phase`` if given (see the
       1b note in the body for why a session-scoped DB needs it).
    3. Optionally upserts a ``plan_execution`` row carrying
       ``project_dir`` — what ``/api/verification/{id}/start`` and
       ``/api/execution/{id}/start`` read to find the target repo.
    4. Optionally upserts the ``plan_routing.verification`` JSON blob
       (status / round / max_rounds / stop_reason).
    5. Clears the ``plan_verification`` runtime row (round / verdicts)
       so the fixture reads as a plan on which no verification round
       has run. Pass ``verification_round`` / ``verification_status`` /
       ``verification_results`` / ``stop_reason`` to seed a concrete
       row instead.

    Idempotent: safe to call repeatedly for the same ``plan_id`` in one
    test — each step upserts rather than blindly inserting. Returns
    ``plan_dir`` so it composes into a fixture return value.
    """
    plan_id = plan_id or plan_dir.name

    from config_paths import resolve_plans_dir  # noqa: F401  (documents the anchor)
    from plan_state import PlanState

    # 1. legacy plan_state.json -> plan_routing row (one-shot migration).
    if (plan_dir / "plan_state.json").exists():
        PlanState(plan_dir)

    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate
    from plan_state import _state_db_path

    db_path = _state_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open_db(db_path)
    try:
        _migrate(conn)

        # 1b. Re-assert ``plan_routing.current_phase`` from ``phase``.
        # ``PlanState``'s migration is one-shot — it only writes the
        # ``plan_routing`` row when none exists — and the hermetic state
        # DB is session-scoped, so a fixture reusing a plan_id inherited
        # whatever the *previous* test left behind. After a ``/start``
        # test the phase is ``verification_running``, so the next test's
        # ``phase="prd_review"`` fixture kept it and ``POST /start``
        # answered 409 instead of the expected 400.
        #
        # 2026-09-17 (schema v5): this used to write TWO columns — a
        # routing ``stage`` (derived from ``phase`` through production's
        # projection map, or overridden explicitly by a caller that
        # wanted the two to disagree). One column now, so there is
        # nothing to derive and nothing to make disagree.
        if phase is not None:
            conn.execute(
                "UPDATE plan_routing SET current_phase = ? "
                "WHERE plan_id = ?",
                (phase, plan_id),
            )

        if verification is not None:
            payload = json.dumps(verification, ensure_ascii=False)
            cur = conn.execute(
                "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
                (payload, plan_id),
            )
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO plan_routing "
                    "(plan_id, current_phase, version, verification, "
                    "updated_at) "
                    "VALUES (?, ?, 0, ?, datetime('now'))",
                    (plan_id, phase or "ready", payload),
                )

        # 5. ``plan_verification`` — the *runtime* round/verdict store.
        # This is a different table from ``plan_routing.verification``
        # above (that one is the routing-layer snapshot). A freshly
        # materialised plan fixture means "no round has ever run", so
        # any pre-existing row is dropped unless the caller explicitly
        # seeds one.
        #
        # Why the drop matters: the hermetic state DB is scoped to the
        # *session*, not the test, so a fixture reusing a plan_id
        # (``test_verification_api`` uses ``test-plan`` throughout)
        # inherits the previous test's round. That leaked as
        # ``POST /api/verification/{id}/start`` answering 409
        # ``max_rounds_exceeded`` for a plan that had never run.
        conn.execute(
            "DELETE FROM plan_verification WHERE plan_id = ?", (plan_id,)
        )
        if (
            verification_round is not None
            or verification_status is not None
            or verification_results is not None
            or stop_reason is not None
        ):
            conn.execute(
                "INSERT INTO plan_verification "
                "(plan_id, verification_status, round, max_rounds, "
                " verification_stop_reason, results, "
                " started_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                (
                    plan_id,
                    verification_status or "not_started",
                    verification_round or 0,
                    max_rounds if max_rounds is not None else 3,
                    stop_reason,
                    None
                    if verification_results is None
                    else json.dumps(verification_results, ensure_ascii=False),
                ),
            )

        if project_dir is not None or phase is not None:
            existing = conn.execute(
                "SELECT COUNT(*) FROM plan_execution WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()[0]
            if existing:
                if project_dir is not None:
                    conn.execute(
                        "UPDATE plan_execution SET project_dir = ? "
                        "WHERE plan_id = ?",
                        (str(project_dir), plan_id),
                    )
                # 2026-10-16 (schema v6): the mirror ``UPDATE ... SET
                # current_phase`` that used to sit here is deleted.  It
                # wrote a second copy of the phase from this helper,
                # which is the same drift the migration removed; the
                # authoritative write is the ``plan_routing`` UPDATE a
                # few lines above.
            else:
                conn.execute(
                    "INSERT INTO plan_execution (plan_id, attempt_count, project_dir, updated_at) VALUES (?, 0, ?, datetime('now'))",
                    (plan_id, str(project_dir) if project_dir is not None else None),
                )
        conn.commit()
    finally:
        conn.close()

    return plan_dir


@pytest.fixture
def plan_sqlite_seeder():
    """Fixture wrapper around :func:`seed_plan_sqlite`."""
    return seed_plan_sqlite


def write_plan_dir(tmp_path, plan_id, state, report=None):
    """Materialise a plan directory under ``tmp_path`` and return its ``Path``.

    Always writes ``plan_state.json``.  Writes ``verification_report.json``
    only when ``report`` is not ``None`` — the "no report" boundary case in
    the task spec.

    Both payloads are schema-checked before they hit disk, so a malformed
    fixture fails at the point of construction (with a message naming the
    missing key) rather than deep inside the code under test.
    """
    assert_plan_state_schema(state)
    if report is not None:
        assert_verification_report_schema(report)

    plan_dir = Path(tmp_path) / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    (plan_dir / "plan_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if report is not None:
        (plan_dir / "verification_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return plan_dir


@pytest.fixture
def mock_plan_state_factory():
    """Fixture wrapper around :func:`make_mock_plan_state`."""
    return make_mock_plan_state


@pytest.fixture
def mock_verification_report_factory():
    """Fixture wrapper around :func:`make_mock_verification_report`."""
    return make_mock_verification_report


@pytest.fixture
def plan_dir_writer():
    """Fixture wrapper around :func:`write_plan_dir`."""
    return write_plan_dir


@pytest.fixture(autouse=True)
def isolated_runtime_plan_state():
    """Every test starts with empty runtime plan state, and leaves it empty.

    ``server`` keeps two process-global dictionaries that map a plan id to
    whatever the server currently believes about that plan's run:

        * ``_verification_state`` — round, status, stop reason
        * ``_execution_state``     — the same shape for execution

    Fifteen test files write into the first and thirteen into the second,
    and the writes are bare ``dict[key] = ...`` — a ``monkeypatch`` in the
    same function is usually patching something else, so it does not undo
    these. Every one of them therefore leaks its entries into every later
    test in the same process, which makes "what ran before me" part of the
    input to any assertion that counts across the dictionary.

    That is not hypothetical here, and it is not subtle once you see it.
    ``test_round_start_liveness_state.py`` seeds one plan as ``loop_stopped``
    — a terminal status — and then asserts ``get_active_tasks()`` reports
    zero active verifications. Its own entry does not count. The count
    spans the whole dictionary, so a single non-terminal entry left behind
    by any earlier test flips that assertion from 0 to 1. The test passes
    in its natural position and fails in a shuffled one; the suite has
    been green for that reason rather than because the property holds.

    Two fixtures in this file already isolate process globals the same way
    — ``isolated_provider_state`` for the shared tracker and cooldown, and
    ``_scrub_fixture_plan_ids_per_test`` for rows the e2e lane leaves in
    the state database. This is the third instance of the same shape, and
    it is the last one that can be added reactively: the remaining
    exposure is whatever nobody has written a test for yet, which is what
    running the suite in a shuffled order is for.

    The snapshot is shallow. Keys are restored, values are shared — so a
    test that mutates a nested dict *in place* still leaks that mutation.
    Deep-copying was rejected on purpose: the values hold thread handles
    and cancellation tokens, and copying those is a worse failure than the
    one it would prevent. Restoring rather than clearing outright is also
    deliberate, so a session-scoped fixture that seeds state on purpose
    still has it after the first test runs.
    """
    import server

    saved: dict[str, dict] = {}
    for attr in ("_verification_state", "_execution_state"):
        store = getattr(server, attr, None)
        if isinstance(store, dict):
            saved[attr] = dict(store)
            store.clear()
    try:
        yield
    finally:
        for attr, snapshot in saved.items():
            store = getattr(server, attr, None)
            if isinstance(store, dict):
                store.clear()
                store.update(snapshot)
