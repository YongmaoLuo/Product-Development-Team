"""Shared pytest fixtures for the backend suite.

Annotations are stringified (PEP 563) so PEP 604 unions (``str | None``)
in evaluated positions stay importable under the CI gate's Python 3.9 —
the dev venv runs 3.11 where ``dict | None`` works natively, but CI
bootstraps a 3.9 venv and imports this conftest before collecting.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

#: The backend package root — ``<repo>/backend``, since this file lives in
#: ``backend/tests/``. Used to tell "a worker this application started"
#: (which the suite must reclaim) from a library's own thread.
_BACKEND_DIR = Path(__file__).resolve().parents[1]

# Prevent a shell-provided CC Switch proxy URL from leaking into backend tests
# that exercise provider-specific routing.
os.environ.pop("ANTHROPIC_BASE_URL", None)

# Provider mode is a property of the *deployment*, not of one test process.
#
# ``ClaudeCodingTool._detect_inherit_mode()`` probes for a CC Switch database
# under ``$HOME``.  This suite redirects ``$HOME`` per-test (the isolation
# fixtures below), so an unpinned mode is re-derived from ambient state and
# can flip *mid-run*: a test that points HOME at a scratch dir would silently
# switch the whole pipeline into inherit mode and then fail its own
# provider-contract assertions.
#
# It is worse on a machine without CC Switch — which is exactly CI, and
# exactly the audience this repo is for.  There the suite would run *entirely*
# in inherit mode and ~16 provider-contract tests would fail, while the same
# commit passed on the maintainer's machine.  A suite whose result depends on
# the runner's installed apps is not a suite.
#
# ``setdefault`` so an explicit outer override still wins; inherit-mode tests
# set ``PDT_PROVIDER_MODE=inherit`` themselves (``test_dispatch_inherit_mode``,
# ``test_subagent_inherit_env``) and are unaffected.
os.environ.setdefault("PDT_PROVIDER_MODE", "cc-switch")

# ---------------------------------------------------------------------------
# Talking to the guarded API like a real client would.
# ---------------------------------------------------------------------------
#
# ``server.request_guard`` refuses any ``/api/*`` request that arrives
# without the guard header, and serves only loopback hostnames. Both are
# the intended production behaviour, so the suite adapts to the guard
# rather than the other way round — a bypass here would leave the guard
# untested by the very requests that exercise every route.
#
#   * ``testserver`` joins the allowed hostnames: that is the ``Host``
#     Starlette's TestClient sends by default.
#   * every ``TestClient`` has the guard header injected below, which
#     keeps 62 construction sites across 48 files unchanged.
#   * the debug routes are opt-in production; this suite is what
#     exercises them, so it opts in.
#
# All three are set at import, before any test module imports ``server``.
os.environ.setdefault("PDT_ALLOWED_HOSTS", "testserver")
os.environ.setdefault("PDT_DEBUG_ROUTES", "1")


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


_install_killpg_tripwire()


def _install_request_guard_header() -> None:
    """Make every ``TestClient`` request carry the guard header.

    Wrapping ``TestClient.__init__`` rather than editing call sites: a
    guard that only some of the 62 clients satisfy would fail in the
    least informative way available, and the call sites would drift back
    out of sync the first time someone adds one.
    """
    from starlette.testclient import TestClient as _TestClient

    import request_guard

    if getattr(_TestClient, "_pdt_guard_installed", False):
        return

    _original_init = _TestClient.__init__

    def _init_with_guard(self, *args, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault(request_guard.REQUEST_HEADER, request_guard.REQUEST_HEADER_VALUE)
        kwargs["headers"] = headers
        _original_init(self, *args, **kwargs)

    _TestClient.__init__ = _init_with_guard
    _TestClient._pdt_guard_installed = True


_install_request_guard_header()


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
# The session scratch root — must be idempotent across re-imports
# ---------------------------------------------------------------------------
#
# pytest loads this file as ``conftest``. A test that does ``from
# backend.tests.conftest import ...`` loads it a **second** time under the
# dotted name, which runs this module body again — so an unconditional
# ``mkdtemp`` here mints one directory per execution while only the one
# the fixture closes over is ever removed. The other one is never
# collected by anything, which is how a suite run leaks a directory,
# every run, forever.
#
# Keying the root on the environment makes the second execution reuse the
# first one's directory, so there is exactly one per process and the one
# teardown removes is the only one that exists. An externally-set value
# wins, which is also what lets a caller point the suite somewhere it
# owns.
if not os.environ.get("PDT_TEST_SESSION_ROOT"):
    os.environ["PDT_TEST_SESSION_ROOT"] = tempfile.mkdtemp(prefix="ac-test-state-")
_TEST_STATE_DB_DIR = Path(os.environ["PDT_TEST_SESSION_ROOT"])
os.environ["PDT_STATE_DB_PATH"] = str(_TEST_STATE_DB_DIR / "state.db")

# ---------------------------------------------------------------------------
# Hermetic plans root — 2026-09-14, the same decision that moved the
# suite off the live database: tests must not touch state.db, they
# create their own.
#
# The SQLite redirect above stops the suite writing the live DATABASE;
# without this second redirect it still wrote the live TREE. Leaf helpers
# that take no explicit directory — ``ExecutionLogger(plan_id)``,
# ``plan_state.plans_dir``, ``repair_generator.generate_repair_tasks``,
# ``verification_agent.run_verification`` — each defaulted to
# ``<repo>/plans``, so a fixture plan_id materialised a real
# ``plans/<fixture-id>/`` directory (``ac-skip-test-arch-*``,
# ``resume-test-*``, ``vp-sync-targets-*``, ...). The running server's
# notifier startup sweep treats every ``plans/<id>/`` with an
# executing/verification_running routing row as an in-flight plan and
# pushes a card for it — which is how the junk got to Feishu even after
# the DB was clean.
#
# ``PDT_PLANS_DIR`` mirrors ``server.PLANS_DIR``'s pre-existing override;
# every leaf resolver now routes through ``config_paths.resolve_plans_dir``.
# ---------------------------------------------------------------------------

os.environ["PDT_PLANS_DIR"] = str(_TEST_STATE_DB_DIR / "plans")

# The startup credential sweep is a machine-wide rewrite — it walks the
# real temp roots and overwrites credentials in the settings payloads it
# recognises. In production that is the point (booting is the one moment
# nothing of ours can legitimately be running). Under test it would mean
# the suite silently rewrites the developer's own residue: the lifespan
# tests here enter ``server._lifespan`` directly, and
# ``test_state_from_db.py`` enters it through ``TestClient``'s context
# manager.
#
# Set for the whole session rather than stubbed per test, because the
# hazard is attached to *booting the app*, not to any one test — a new
# test that boots the app would otherwise reintroduce it silently.
# ``utils.secret_sweep.default_roots`` and ``sweep`` are untouched, so the
# pipeline is still exercisable against a ``tmp_path``.
os.environ.setdefault("PDT_DISABLE_TEMP_SWEEP", "1")

# Workspace-derived lock state — the fallback lock directory and the
# broker socket are keyed by workspace digest, so a suite that gives every
# case its own ``tmp_path`` mints one lock directory per case. Left at the
# default root, that lands in the machine's temp root, for workspaces that
# were deleted seconds later, and nothing ever collects it.
#
# The redirect root gets its **own** short ``mkdtemp`` rather than a
# subdirectory of ``_TEST_STATE_DB_DIR`` on purpose: ``socket_path`` puts a
# 32-character socket name under it, and ``AF_UNIX`` paths are capped at
# 104 bytes on macOS. Nested under the state dir the full path measures
# 108 characters and ``bind()`` fails with "AF_UNIX path too long" — the
# lock layer silently gone, which is the exact failure ``socket_path``'s
# docstring exists to warn about.
#
# Idempotent for the same re-import reason as the session root above, and
# keyed on the same variable the production code reads so there is one
# source of truth for "where the lock state goes".
if not os.environ.get("PDT_LOCK_ROOT"):
    os.environ["PDT_LOCK_ROOT"] = tempfile.mkdtemp(prefix="lk-")
_LOCK_ROOT = Path(os.environ["PDT_LOCK_ROOT"])

# Credential-payload scratch directories. Every dispatch mints one
# ``pdt-subagent-*`` directory through ``secret_files.private_dir``, and
# the suite dispatches constantly, so left at the default root the suite
# mints them in the machine's temp root and **nothing there collects
# them**: the boot-time sweep is switched off above (deliberately — it
# would rewrite the developer's own residue), and the directories that do
# hold a settings payload are non-empty, so the empty-directory prune
# cannot reach them either.
#
# Redirected rather than swept so the suite's scratch never leaves the
# suite in the first place — the same shape as the lock root above, and
# keyed on the variable production reads so there is one source of truth
# for "where the private directories go". Nesting it under the session
# root is safe here, unlike the socket: these are regular paths, with no
# ``sun_path`` cap to clear.
_SUBAGENT_ROOT = _TEST_STATE_DB_DIR / "private"
os.environ.setdefault("PDT_SECRET_TEMP_ROOT", str(_SUBAGENT_ROOT))


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
    shutil.rmtree(_TEST_STATE_DB_DIR, ignore_errors=True)
    shutil.rmtree(_LOCK_ROOT, ignore_errors=True)


# ---------------------------------------------------------------------------
# The provider-order contract file
# ---------------------------------------------------------------------------
#
# 2026-09-23. ``provider_order.load_fallback_order()`` has no hard-coded
# chain and no YAML fallback: it reads the contract file and RAISES
# ``ProviderOrderError`` when that file is absent.
#
# That file is runtime state. ``.gitignore`` excludes the whole
# ``.config/`` directory, so it exists on a machine where a producer is
# running and does NOT exist on a fresh CI checkout. Every test that
# boots the app then fails inside the lifespan — ``server._lifespan`` ->
# ``load_providers()`` -> ``load_fallback_order()`` — for an environment
# reason rather than a code one.
#
# Measured on a clean clone, with nothing else different: 1 failed + 3
# errors in ``tests/test_state_from_db.py``, both VP-008 contract tests
# in ``tests/integration/test_provider_order_integration.py``, and
# ``tests/test_base_yaml_no_order.py``. All of them pass on a machine
# that has a producer running, which is precisely why the coupling went
# unnoticed.
#
# Why the file rather than the ``PROVIDER_ORDER_FILE`` env var, which
# ``load_fallback_order`` documents as an operator escape hatch:
# ``_default_order_file()`` prefers an already-imported ``server``
# module's ``PROVIDER_ORDER_FILE`` — an **import-time snapshot** taken by
# ``config_paths`` — over the live env var. The suite imports ``server``
# early, so an env var set by a fixture is ignored.
#
# Create-if-absent, never overwrite. A developer running against a live
# optimiser keeps exercising the real artifact; this only fills the gap
# a clean checkout has. The path is gitignored, so nothing here can be
# committed.

#: Minimal payload ``provider_order._validate_schema`` accepts:
#: ``version`` must match, ``order`` must be a NON-empty list of
#: strings, and ``updated_at`` must be a parseable ISO 8601 stamp.
#: ``["parent"]`` is the caller's own fallback sentinel — it is never a
#: pool member (``acquire_provider_with_dynamic_capacity`` skips it), so
#: a fixture chain built from it cannot accidentally hand a test a
#: provider slot.
_PROVIDER_ORDER_FIXTURE = {
    "version": 1,
    "order": ["parent"],
    "providers": {},
}


@pytest.fixture(scope="session", autouse=True)
def _provider_order_contract_file():
    """Make a clean checkout look like a machine with an optimiser.

    Yields the path that was in force (the pre-existing real file, the
    one created here, or ``None`` when the location could not be
    resolved). Nothing asserts on the value; it exists so the fixture is
    visible in ``--fixtures`` and debuggable.
    """
    try:
        from config_paths import DEFAULT_PROVIDER_ORDER_FILE
    except Exception:  # pragma: no cover - config_paths is a leaf utility
        yield None
        return

    target = Path(DEFAULT_PROVIDER_ORDER_FILE)
    if target.exists():
        # The real artifact is present — leave it alone.
        yield target
        return

    payload = dict(_PROVIDER_ORDER_FIXTURE)
    # Aware, fresh, and timezone-explicit: ``_staleness_minutes``
    # subtracts this from an aware "now", so a naive stamp would raise
    # where a stale one would merely warn.
    payload["updated_at"] = datetime.now().astimezone().isoformat()

    created_dir = not target.parent.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:  # pragma: no cover - read-only checkout
        yield None
        return

    try:
        yield target
    finally:
        # Restore the checkout. The file is gitignored, but leaving it
        # behind would let a *later* manual ``python server.py`` in the
        # same clone pick up a one-provider fixture chain, which is a
        # confusing thing to debug. Best-effort only — a failure here
        # must not fail the session.
        try:
            target.unlink()
            if created_dir:
                target.parent.rmdir()
        except OSError:  # pragma: no cover - defensive
            pass


@pytest.fixture
def fake_cc_switch_home(monkeypatch, tmp_path):
    """Redirect ``$HOME`` at a minimal CC Switch database.

    The second half of the same coupling. Once ``provider-order.json``
    exists, ``load_fallback_order`` filters its chain through
    ``cc_switch``, which reads
    ``~/.cc-switch/cc-switch.db`` and treats a missing file as a **hard**
    error (``ProviderOrderError: CC Switch database unavailable`` — see
    ``provider_order._load_fallback_order_uncached``). So a test that
    boots the app needs that DB to exist, and it is the CC Switch
    desktop app's store: present on a developer machine, absent from any
    clean checkout.

    An empty but valid database is enough: ``list_provider_ids`` returns
    ``[]`` when neither table is present, and ``server.load_providers``
    treats an empty chain as "no providers configured" and starts
    normally rather than raising.

    Opt-in rather than autouse: ``Path.home()`` is also how
    ``tests/test_edit_write_containment_guard.py`` resolves its "foreign
    checkout" fixtures, and redirecting it for the whole session would
    quietly change what those tests exercise.
    """
    import sqlite3

    fake_home = tmp_path / "home"
    cc_dir = fake_home / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path = cc_dir / "cc-switch.db"

    conn = sqlite3.connect(str(db_path))
    try:
        # A table, so the file is non-zero-length: ``_connect_db``
        # rejects a zero-byte file as corrupt.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS providers ("
            "id TEXT PRIMARY KEY, name TEXT, settings_config TEXT)"
        )
        conn.commit()
    finally:
        conn.close()

    # ``_resolve_db_path`` calls ``Path.home()`` on every call, so
    # redirecting the env var is enough — no module constant to patch.
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


# ---------------------------------------------------------------------------
# The process environment file
# ---------------------------------------------------------------------------
#
# 2026-09-23, third instance of the same cause as the two fixtures above:
# something the code requires is **runtime state that a git checkout does
# not carry**. `env_config.load_env()` requires ANTHROPIC_API_KEY,
# NOTION_TOKEN and NOTION_PARENT_PAGE_ID and raises RuntimeError when any
# is absent; they normally live in `backend/.env`, which `.gitignore`
# excludes. Present on a developer machine, absent on a runner.
#
# It is worth noting *why* this one survived so long: the failure it causes
# is hidden behind the shard wedge. `tests/unit/test_agent_persist.py::test_cli_passes_tasks_file_through`
# has been failing on every CI run, and nobody has seen it, because the
# full `unit` shard stops before reaching it. It took staircase shard u-55
# (run 35852698516) — the first shard small enough to finish — to surface it.
#
# `backend/.env.ci` is the committed, non-secret template (see its header).
# This materialises it as `backend/.env` when that file is absent, and
# removes it again afterwards, so the checkout is left as found.
#
# Failure stays loud: this fixture only supplies the three placeholders the
# loader demands. A missing *other* variable still raises from
# `env_config.load_env()` at the point of use — the loader does not retry,
# sleep or degrade, which is the behaviour a missing configuration should
# have. `tests/unit/test_env_config_fails_fast.py` pins that.

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_ENV_TEMPLATE = _BACKEND_ROOT / ".env.ci"
_ENV_TARGET = _BACKEND_ROOT / ".env"


@pytest.fixture(scope="session", autouse=True)
def _env_file_for_tests():
    """Give the suite the same environment a developer machine has.

    Yields the path in force (the real ``.env``, the materialised template,
    or ``None`` when neither could be used). Nothing asserts on the value;
    it exists so the fixture is visible in ``--fixtures`` and debuggable.
    """
    if _ENV_TARGET.exists():
        # A developer's own credentials — never touch them.
        yield _ENV_TARGET
        return

    if not _ENV_TEMPLATE.exists():  # pragma: no cover - template is committed
        yield None
        return

    try:
        _ENV_TARGET.write_text(
            _ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8",
        )
    except OSError:  # pragma: no cover - read-only checkout
        yield None
        return

    try:
        yield _ENV_TARGET
    finally:
        try:
            _ENV_TARGET.unlink()
        except OSError:  # pragma: no cover - defensive
            pass


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


@pytest.fixture(autouse=True)
def isolated_plans_dir(monkeypatch, tmp_path):
    """Redirect PLANS_DIR to tmp_path for isolation.

    Defensive: some test sessions import a slimmed ``server`` module
    that does not expose ``PLANS_DIR``.  In that case the fixture is a
    no-op rather than failing collection.
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

    if not hasattr(server, "_execution_state"):
        yield
        return

    #: ``(thread, process)`` for every application worker started during
    #: this test. ``process`` is the object the worker waits on, when the
    #: thread is an execution monitor; it stays ``None`` for the rest.
    workers: "list[tuple[threading.Thread, object]]" = []
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

    for thread, process in workers:
        hook = getattr(process, "release", None) or getattr(
            process, "terminate", None
        )
        if callable(hook):
            try:
                hook()
            except Exception:  # noqa: BLE001 - best-effort unblock only
                pass
    for thread, _ in workers:
        if thread.ident is None:
            # Built but never started — several tests construct a Thread as
            # a "dead worker" stand-in for the watchdog paths. Nothing is
            # running, so there is nothing to reclaim (and joining an
            # unstarted thread raises).
            continue
        # A released worker has nothing slow left to do; the generous cap
        # only covers scheduler starvation on a loaded machine.
        thread.join(timeout=10)

    stuck = [thread for thread, _ in workers if thread.is_alive()]

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
                if phase is not None:
                    conn.execute(
                        "UPDATE plan_execution SET current_phase = ? "
                        "WHERE plan_id = ?",
                        (phase, plan_id),
                    )
            else:
                conn.execute(
                    "INSERT INTO plan_execution "
                    "(plan_id, current_phase, attempt_count, project_dir,"
                    " updated_at) VALUES (?, ?, 0, ?, datetime('now'))",
                    (
                        plan_id,
                        phase or "executing",
                        str(project_dir) if project_dir is not None else None,
                    ),
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
