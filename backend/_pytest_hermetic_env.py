"""The process-global environment every pytest tree in this repo gets.

Why this module exists
----------------------

There are two pytest roots under ``backend/`` -- ``backend/tests/`` and
``backend/state_machine/tests/`` -- and until 2026-10-16 each carried its
own ``conftest.py``.  Only the first one installed a hermetic process
environment, and it installed it as a **side effect of being imported**:
module-level ``os.environ[...] = ...`` plus a monkey-patch of
``starlette.testclient.TestClient.__init__``.  The second tree therefore had
no environment of its own and ran green *only* when a single pytest
invocation happened to collect ``backend/tests/`` first.

That is a fake green, and it is invisible from either file alone.  Measured
on ``c4ffdb4``::

    $ pytest state_machine/tests -q           ->  37 failed, 207 passed
    $ pytest tests/unit state_machine/tests -q ->  green

Same 244 tests, same tree, same commit -- 37 tests whose result was decided
by which *other* directory the command line mentioned.  Those 37 were not
one bug but four distinct mechanisms, and this module owns three of them:

============================  =========================================
mechanism                     what it made the second tree do
============================  =========================================
``TestClient`` monkey-patch   20 route tests answered HTTP 403
``PDT_ALLOWED_HOSTS``         (a second 403, this one from Host validation)
``PDT_STATE_DB_PATH``          3 ``test_storage_e2e`` tests raised
                               "refusing to open the live state.db"
============================  =========================================

The fourth (17 tests raising ``FileNotFoundError`` on a hard-derived
``.venv`` path) is **not** fixed here.  It is a different disease with a
different cure, and it is named so the two are not conflated -- see
``state_machine/tests/unit/test_marker_completeness.py``.  This module is
about the shared conftest-level environment contract; that one is about a
subprocess interpreter path.

Why the module lives at ``backend/`` top level and not inside one tree
------------------------------------------------------------------------

It has to be importable by *both* roots before either root's ``conftest``
runs, and neither root may be able to move or delete it.
``backend/_pytest_slash_cov_fix/`` is the existing precedent for a
test-only support package at this level; the leading underscore is this
repo's existing "not imported by production" marker.  Putting it under
``backend/tests/`` would work too, and would be worse:
``state_machine/tests/conftest.py`` would then import from ``tests/``,
recreating the tree-to-tree coupling in explicit form -- the exact shape
being removed.

What is deliberately NOT here
-----------------------------

* **No back door into the request guard.**  ``server.request_guard`` 403s a
  request that arrives without the ``X-PDT-Request`` header and serves only
  loopback hostnames.  Both are production behaviour with a real threat
  model (see ``request_guard.py``'s module docstring), and the second
  tree's route tests exercise them.  The fix is for the tests to talk to
  the server the way a real client does, not for the server to stop
  checking.  Nothing in this file weakens a guard;
  ``install_request_guard_header`` only teaches ``TestClient`` to send the
  header a real client sends.
* **No ``PDT_ALLOW_REAL_STATE_DB``.**  That escape hatch is for a
  deliberate operator audit, not for a suite that has not been told where
  its database is.  Pointing ``PDT_STATE_DB_PATH`` at a throwaway file is
  the fix, and it is the one ``state_machine/tests`` already needed.

Idempotency
-----------

Both conftests call :func:`install_hermetic_env`, and pytest may import
either first, so every assignment here is either a ``setdefault`` or guarded
by an ``if not os.environ.get(...)``.  Calling it twice in one process must
be a no-op the second time -- that is what makes the two roots converge on
ONE session state database instead of racing to mint two and having
whichever was imported second silently steal the first's.  The xdist worker
scoping below is the load-bearing part of that: the controller installs the
root before it forks workers, and each worker must scope itself to
``<root>/<worker id>`` so N workers do not share one ``state.db``.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# The database is named and redirected through the one resolver, exactly as
# every other module that opens it does.  ``test_state_db_path_has_one_resolver``
# exists because nine copies of "``PDT_STATE_DB_PATH`` else a path derived from
# my own ``__file__``" were each correct on the day they were written and two
# had already drifted to a different valid file; a tenth copy written here for
# convenience would be the tenth instance of that shape.  ``STATE_DB_ENV`` is
# the variable's name and ``STATE_DB.name`` its filename, so this module
# follows the resolver if either ever changes.
from config_paths import STATE_DB, STATE_DB_ENV

#: The home the process was *launched* with, recorded before
#: :func:`install_hermetic_env` redirects ``$HOME``.  Named here rather than
#: spelled at the call sites so the variable's name and its meaning cannot
#: drift apart, which is the same reason this module imports ``STATE_DB_ENV``
#: instead of writing ``"PDT_STATE_DB_PATH"`` twice.
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"

__all__ = [
    "HermeticEnv",
    "REAL_HOME_ENV",
    "install_hermetic_env",
    "install_provider_chain_stub",
    "install_request_guard_header",
    "real_provider_chain_from_api",
    "write_minimal_cc_switch_db",
    "xdist_worker_id",
]


@dataclass(frozen=True)
class HermeticEnv:
    """The four scratch roots a pytest session owns.

    Returned rather than re-derived so the caller tears down exactly what was
    created.  ``lock_root`` is deliberately NOT nested under
    ``state_db_dir`` -- ``socket_path`` puts a 32-character socket name
    under it, and ``AF_UNIX`` paths are capped at 104 bytes on macOS;
    nested, the full path measures 108 characters and ``bind()`` fails with
    "AF_UNIX path too long", i.e. the lock layer silently gone.
    """

    state_db_dir: Path
    lock_root: Path
    subagent_root: Path
    home_root: Path
    home_root_owner_pid: int

    #: PID that minted ``lock_root`` / ``state_db_dir`` in this process, or
    #: ``None`` when this process merely inherited them.  Only the minter may
    #: remove them: under xdist every worker inherits one shared
    #: ``PDT_LOCK_ROOT``, and a worker that removes it deletes the broker
    #: sockets of the workers still running.
    lock_root_owner_pid: Optional[int]
    state_root_owner_pid: Optional[int]


#: The environment installed in *this* process.  Ownership of a scratch root
#: is a fact about the process that minted it, so it has to be remembered
#: rather than re-derived: a second call sees ``PDT_LOCK_ROOT`` already set
#: and could only conclude "someone else minted it", losing the answer for
#: the caller that did the minting.  The cache is per-process -- an xdist
#: worker or a nested session re-enters with ``None`` and records its own
#: (non-)ownership, which is exactly the case the rule is about.
_INSTALLED_ENV: Optional[HermeticEnv] = None


def install_request_guard_header() -> None:
    """Make every ``TestClient`` request carry the guard header.

    Wrapping ``TestClient.__init__`` rather than editing call sites: a
    guard that only some of the construction sites satisfy would fail in
    the least informative way available, and the call sites would drift
    back out of sync the first time someone adds one.

    Idempotent -- the ``_pdt_guard_installed`` attribute is what stops a
    second tree's conftest from wrapping an already-wrapped ``__init__``
    in the same process.
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


#: The chain the session-wide stub reports.  ``["parent"]`` is the sentinel
#: the dispatch loop never draws from (``acquire_provider_with_dynamic_
#: capacity`` skips it), so a chain built from it cannot hand a test a
#: provider slot by accident.  ``server.load_providers`` then resolves it
#: to nothing and logs the skip -- the same empty ``STARTUP_PROVIDERS`` a
#: contract-file fixture produced.
_STUB_PROVIDER_CHAIN = ["parent"]

#: ``provider_order._chain_from_api`` as it stood before the first install.
#: Captured once so ``real_provider_chain_from_api`` can hand back the
#: genuine article: a test about where a chain comes from needs the real
#: transport, and cannot ask a session fixture that already replaced it.
_REAL_PROVIDER_CHAIN_FROM_API = None

#: Whether the stub has been installed.  Both conftests call the installer
#: and pytest may import either first, so the second call has to be a no-op
#: -- and specifically must NOT overwrite ``_REAL_PROVIDER_CHAIN_FROM_API``
#: with the stub it installed a moment earlier, which would leave
#: ``real_provider_chain_from_api`` handing back the stub and every test
#: about the real transport testing the stub instead.
_PROVIDER_CHAIN_STUB_INSTALLED = False


def install_provider_chain_stub() -> list:
    """Make the provider-order resolver answer without CC Switch running.

    ``provider_order.load_fallback_order()`` asks CC Switch's HTTP API for
    the chain and RAISES ``ProviderOrderError`` when it does not answer.
    That answer is runtime state of another application -- an HTTP server
    this repository does not start -- so it is absent on a clean CI
    checkout, and a test that boots the app dies in the lifespan for an
    environment reason rather than a code one.

    It stubs the reader the chain resolves through (``_chain_from_api``) and
    leaves ``cc_switch`` untouched, which matters: ``cc_switch`` stays the
    reader of record for endpoint and credentials, so the tests that
    exercise those keep exercising the real lookup.

    Idempotent, for the reason ``_PROVIDER_CHAIN_STUB_INSTALLED`` records.
    The import is lazy so a slimmed test session that does not ship
    ``provider_order`` still gets a usable module.
    """
    global _REAL_PROVIDER_CHAIN_FROM_API, _PROVIDER_CHAIN_STUB_INSTALLED
    if _PROVIDER_CHAIN_STUB_INSTALLED:
        return _STUB_PROVIDER_CHAIN

    import provider_order

    _REAL_PROVIDER_CHAIN_FROM_API = provider_order._chain_from_api
    provider_order._chain_from_api = lambda: list(_STUB_PROVIDER_CHAIN)
    provider_order.cache_clear()
    _PROVIDER_CHAIN_STUB_INSTALLED = True
    return _STUB_PROVIDER_CHAIN


def real_provider_chain_from_api():
    """The genuine ``_chain_from_api``, or ``None`` before any install.

    ``None`` rather than a guess: a caller that gets ``None`` has not called
    :func:`install_provider_chain_stub` yet, which is a different situation
    from "the resolver does not exist" and must not be papered over.
    """
    return _REAL_PROVIDER_CHAIN_FROM_API


#: The stock CC Switch ``providers`` schema.  Every column is one
#: ``cc_switch.REQUIRED_PROVIDERS_COLUMNS`` entry plus the two bookkeeping
#: columns upstream also carries, so ``cc_switch.probe()`` -- the project's
#: own "is this database usable" validator -- accepts what this writes
#: rather than the file merely being openable.  A tenth private copy of this
#: DDL would be the drift the ``STATE_DB_ENV`` note above exists to prevent.
CC_SWITCH_PROVIDERS_DDL = (
    "CREATE TABLE IF NOT EXISTS providers ("
    "id TEXT PRIMARY KEY, app_type TEXT, name TEXT, "
    "settings_config TEXT, created_at INTEGER, sort_index INTEGER, "
    "is_current BOOLEAN DEFAULT 0)"
)


def write_minimal_cc_switch_db(cc_dir: Path) -> Path:
    """Create CC Switch's ``cc-switch.db`` under *cc_dir*; return its path.

    **Empty on purpose.**  The table exists but holds no row, which is what
    makes this a fixture rather than a fake: ``cc_switch.get_provider(name)``
    answers ``None`` for every name, ``server.load_providers`` logs "Skipping
    unknown provider name" and starts with an empty provider list.  A row
    here would hand the suite a provider config with an endpoint and a
    (nonexistent) credential, and any test asserting a provider was resolved
    would then be asserting against this file.  The absence of rows cannot be
    mistaken for a resolved provider.

    Present but empty is also the only state that satisfies both readers at
    once: ``_connect_db`` raises ``CCSwitchDBNotFoundError`` for a missing or
    zero-byte file, and ``load_providers`` re-raises that out of the lifespan,
    so an absent database kills every test that boots the app.
    """
    import sqlite3

    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path = cc_dir / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(CC_SWITCH_PROVIDERS_DDL)
        conn.commit()
    finally:
        conn.close()
    return db_path


def install_hermetic_env() -> HermeticEnv:
    """Install the process-global environment a pytest run must not inherit.

    Every entry below is an environment *contract*, not a convenience: each
    exists because leaving it ambient cost a real incident, and the
    reasoning travelled with the rule when it was lifted out of
    ``backend/tests/conftest.py``.
    """
    global _INSTALLED_ENV
    if _INSTALLED_ENV is not None:
        return _INSTALLED_ENV

    # Prevent a shell-provided CC Switch proxy URL from leaking into tests
    # that exercise provider-specific routing.
    os.environ.pop("ANTHROPIC_BASE_URL", None)

    # Provider mode is a property of the *deployment*, not of one test
    # process.
    #
    # ``ClaudeCodingTool._detect_inherit_mode()`` probes for a CC Switch
    # database under ``$HOME``.  A suite that redirects ``$HOME`` per test
    # would otherwise re-derive an unpinned mode from ambient state and can
    # flip *mid-run*: a test that points HOME at a scratch dir would
    # silently switch the whole pipeline into inherit mode and then fail its
    # own provider-contract assertions.
    #
    # It is worse on a machine without CC Switch -- which is exactly CI, and
    # exactly the audience this repo is for.  There the suite would run
    # *entirely* in inherit mode and ~16 provider-contract tests would fail,
    # while the same commit passed on the maintainer's machine.  A suite
    # whose result depends on the runner's installed apps is not a suite.
    #
    # ``setdefault`` so an explicit outer override still wins; inherit-mode
    # tests set ``PDT_PROVIDER_MODE=inherit`` themselves and are unaffected.
    os.environ.setdefault("PDT_PROVIDER_MODE", "cc-switch")

    # ---------------------------------------------------------------------
    # Talking to the guarded API like a real client would.
    # ---------------------------------------------------------------------
    #
    # ``server.request_guard`` refuses any ``/api/*`` request that arrives
    # without the guard header, and serves only loopback hostnames.  Both are
    # the intended production behaviour, so the suite adapts to the guard
    # rather than the other way round -- a bypass here would leave the guard
    # untested by the very requests that exercise every route.
    #
    #   * ``testserver`` joins the allowed hostnames: that is the ``Host``
    #     Starlette's TestClient sends by default.
    #   * every ``TestClient`` has the guard header injected by
    #     ``install_request_guard_header()``, which keeps every construction
    #     site across both trees unchanged.
    #   * the debug routes are opt-in production; this suite is what
    #     exercises them, so it opts in.
    #
    # All three are set at import, before any test module imports ``server``.
    os.environ.setdefault("PDT_ALLOWED_HOSTS", "testserver")
    os.environ.setdefault("PDT_DEBUG_ROUTES", "1")

    # ---------------------------------------------------------------------
    # Hermetic state database + plans tree.
    # ---------------------------------------------------------------------
    #
    # 2026-09-13: ``backend/tests/perf/test_repo_scheduler_refiner_watchdog.py``
    # resolved its database as ``PDT_STATE_DB_PATH`` *or, failing that,* the
    # repository's own ``state.db``.  The env var is set by a conftest, so the
    # fallback looked inert -- but any invocation that did not collect that
    # conftest wrote straight to production: 200 ``plan_tasks`` rows landed in
    # the operator's database, and the row's plan id (a pytest ``tmp_path``
    # directory name) then surfaced in the plan list as if it were real.
    #
    # ``state_machine.db.connection._refuse_real_state_db_under_pytest`` is the
    # chokepoint guard that catches the fallback; it raises rather than
    # writing.  Pointing the variable at a throwaway file is what keeps either
    # tree from tripping it.
    #
    # Keying the root on the environment makes a second execution reuse the
    # first one's directory, so there is exactly one per process and the one
    # teardown removes is the only one that exists.  An externally-set value
    # wins, which is also what lets a caller point the suite somewhere it
    # owns.
    #
    # Under xdist the key alone is not enough, because the key is inherited
    # rather than per-process.  The controller loads a conftest during
    # ``_preparse`` -- before it starts any worker -- so by the time workers
    # are execnet children that copy its ``os.environ``, the variable is
    # already set and every worker would reuse the controller's one directory:
    # N processes, one ``state.db``, one plans tree.
    #
    # So the key carries the worker id, which is what ``PYTEST_XDIST_WORKER``
    # holds (``gw0``/``gw1``/... in a worker, absent in the controller).  A
    # worker therefore scopes itself to ``<root>/<worker id>`` rather than
    # adopting the root verbatim.  ``_PDT_TEST_SESSION_ROOT_OWNER`` records
    # which process installed the value so that:
    #
    #   * the dotted re-import inside one worker sees its own id and reuses;
    #   * a different worker sees a different id and scopes itself separately;
    #   * a serial run, which has no worker id, mints once and reuses.
    xdist_worker = os.environ.get("PYTEST_XDIST_WORKER") or ""
    session_root_owner = "_PDT_TEST_SESSION_ROOT_OWNER"
    state_root_owner_pid = None
    if not os.environ.get("PDT_TEST_SESSION_ROOT"):
        os.environ["PDT_TEST_SESSION_ROOT"] = tempfile.mkdtemp(prefix="ac-test-state-")
        os.environ[session_root_owner] = ""
        state_root_owner_pid = os.getpid()
    elif xdist_worker and os.environ.get(session_root_owner) != xdist_worker:
        worker_state_db_dir = Path(os.environ["PDT_TEST_SESSION_ROOT"]) / xdist_worker
        worker_state_db_dir.mkdir(parents=True, exist_ok=True)
        os.environ["PDT_TEST_SESSION_ROOT"] = str(worker_state_db_dir)
        os.environ[session_root_owner] = xdist_worker
        state_root_owner_pid = os.getpid()
    state_db_dir = Path(os.environ["PDT_TEST_SESSION_ROOT"])

    # ``setdefault`` rather than ``[...] = ...``: the sibling tree's conftest
    # may have installed its own value first, and a second unconditional
    # assignment would silently swap the state database out from under
    # whatever read it first.  Idempotent, and one session DB per invocation.
    os.environ.setdefault(STATE_DB_ENV, str(state_db_dir / STATE_DB.name))

    # ``PDT_PLANS_DIR`` mirrors ``server.PLANS_DIR``'s pre-existing override;
    # every leaf resolver now routes through ``config_paths.resolve_plans_dir``.
    # The SQLite redirect above stops the suite writing the live DATABASE;
    # without this second redirect it still wrote the live TREE -- leaf helpers
    # that take no explicit directory (``ExecutionLogger(plan_id)``,
    # ``plan_state.plans_dir``, ``repair_generator.generate_repair_tasks``,
    # ``verification_agent.run_verification``) each defaulted to
    # ``<repo>/plans``, so a fixture plan_id materialised a real
    # ``plans/<fixture-id>/`` directory.  The running server's notifier
    # startup sweep treats every ``plans/<id>/`` that still holds a state JSON
    # as an in-flight plan and pushes a card for it -- which is how the junk
    # got to Feishu even after the DB was clean.
    os.environ.setdefault("PDT_PLANS_DIR", str(state_db_dir / "plans"))

    # The startup credential sweep is a machine-wide rewrite -- it walks the
    # real temp roots and overwrites credentials in the settings payloads it
    # recognises.  In production that is the point (booting is the one moment
    # nothing of ours can legitimately be running).  Under test it would mean
    # the suite silently rewrites the developer's own residue: the lifespan
    # tests enter ``server._lifespan`` directly, and ``test_state_from_db.py``
    # enters it through ``TestClient``'s context manager.
    #
    # Set for the whole session rather than stubbed per test, because the
    # hazard is attached to *booting the app*, not to any one test.
    # ``utils.secret_sweep.default_roots`` and ``sweep`` are untouched, so the
    # pipeline stays exercisable against a ``tmp_path``.
    os.environ.setdefault("PDT_DISABLE_TEMP_SWEEP", "1")

    # Workspace-derived lock state -- the fallback lock directory and the
    # broker socket are keyed by workspace digest, so a suite that gives every
    # case its own ``tmp_path`` mints one lock directory per case.  Left at
    # the default root, that lands in the machine's temp root, for workspaces
    # that were deleted seconds later, and nothing ever collects it.
    #
    # The redirect root gets its **own** short ``mkdtemp`` rather than a
    # subdirectory of ``state_db_dir`` on purpose: ``socket_path`` puts a
    # 32-character socket name under it, and ``AF_UNIX`` paths are capped at
    # 104 bytes on macOS.  Nested under the state dir the full path measures
    # 108 characters and ``bind()`` fails with "AF_UNIX path too long" -- the
    # lock layer silently gone, which is the exact failure ``socket_path``'s
    # docstring exists to warn about.
    if not os.environ.get("PDT_LOCK_ROOT"):
        os.environ["PDT_LOCK_ROOT"] = tempfile.mkdtemp(prefix="lk-")
        lock_root_owner_pid = os.getpid()
    else:
        lock_root_owner_pid = None
    lock_root = Path(os.environ["PDT_LOCK_ROOT"])

    # The broker socket is the second half of the pair above, and it needs the
    # opposite treatment: ``socket_path`` consults ``PDT_LOCK_BROKER`` first and
    # returns it **verbatim**, ignoring ``project_dir`` entirely.  Redirecting
    # the root above is therefore not enough to make the socket hermetic -- an
    # ambient value short-circuits the whole derivation.
    #
    # The value is written by the executor into the environment block of each
    # sub-agent's settings, so it is a process-environment variable inherited
    # by anything the suite launches.  It is derived from one workspace and
    # published verbatim, which is the shape that makes taking it over
    # worthwhile: the override collapses *every* workspace onto that one
    # socket.  Three unrelated ``project_dir`` values hash to three different
    # digests and land on three different sockets, but under the override they
    # all return the same literal path, so tests asserting that workspaces are
    # separated fail against a value no test in the file set.  That is a
    # machine-level variable deciding the gate's verdict.
    #
    # Cleared unconditionally rather than defaulted: there is no suite-owned
    # value to fall back *to* -- an absent variable is precisely the state in
    # which the digest derivation runs and the assertions mean what they say.
    # A caller that genuinely wants a pinned socket for one case sets it with
    # ``monkeypatch.setenv`` inside that case, which outranks this and is
    # undone with it.  The production override semantics are untouched on
    # purpose: pinning a socket is how a deployment opts out of discovery,
    # and the gap was suite-side environment ownership, not the override.
    os.environ.pop("PDT_LOCK_BROKER", None)

    # ---------------------------------------------------------------------
    # The executor's per-task stamps.
    # ---------------------------------------------------------------------
    #
    # ``PDT_ALLOWED_WRITE_FILES`` (the task's declaration),
    # ``PDT_UNDECLARED_WRITE_DIR`` (where the guard writes refusal
    # markers), ``PDT_READONLY_TASK``, ``PDT_REPORT_PATH``,
    # ``PDT_LOCK_TASK_ID``, ``PDT_DECLARED_MANIFEST_HASH`` and
    # ``PDT_LOCK_TIMEOUT`` are stamped into the environment of the process
    # the executor runs ONE task in (see ``coding_tool.py``'s
    # ``_stamp_readonly_task`` / ``_stamp_report_path`` /
    # ``_stamp_allowed_write_files`` and the guard hook script).  A pytest
    # run launched by that executor inherits every one of them, and the
    # suite's own guard tests then answer to a declaration that is not
    # theirs:
    #
    #   * ``tests/test_edit_write_containment_guard.py`` starts the guard
    #     with ``os.environ.copy()`` and pops only the project/plans
    #     variables, so the inherited declaration is still in force.  The
    #     guard refuses the test's own probe file as "this task did not
    #     declare that file" -- and, because ``PDT_UNDECLARED_WRITE_DIR``
    #     is inherited too, writes the refusal marker into the EXECUTOR's
    #     marker directory.
    #   * The executor reads that marker back and classifies the run as an
    #     undeclared write by the agent.  The refusal was the test's and
    #     the marker was aimed at the test's scratch file, but the marker
    #     channel carries no owner, so the run is reported as an agent
    #     violation on the strength of the suite's own negative control.
    #     That is not hypothetical: it is how the 2026-10-21 repair task
    #     was reported as an ``undeclared_write`` -- with a marker path
    #     under the pytest tmp root, named after this suite's own
    #     ``test_allows_edit_inside_project_dir`` case.
    #
    # This is the same disease as ``PDT_LOCK_BROKER`` above and more
    # dangerous: a task-level variable must not decide the suite's result,
    # and here it does not merely fail a test -- it manufactures an
    # accusation against the agent that is being judged.
    #
    # Cleared unconditionally, with no suite-owned fallback: there is no
    # suite meaning for any of these names.  A case that genuinely wants
    # one (the guard tests, which need a declaration to check against)
    # sets it in the environment it hands to the guard itself, which
    # outranks this and is undone with the case.
    for _task_scope_var in (
        "PDT_ALLOWED_WRITE_FILES",
        "PDT_UNDECLARED_WRITE_DIR",
        "PDT_READONLY_TASK",
        "PDT_REPORT_PATH",
        "PDT_LOCK_TASK_ID",
        "PDT_DECLARED_MANIFEST_HASH",
        "PDT_LOCK_TIMEOUT",
    ):
        os.environ.pop(_task_scope_var, None)

    # Credential-payload scratch directories.  Every dispatch mints one
    # ``pdt-subagent-*`` directory through ``secret_files.private_dir``, and
    # the suite dispatches constantly, so left at the default root the suite
    # mints them in the machine's temp root and **nothing there collects
    # them**: the boot-time sweep is switched off above (deliberately -- it
    # would rewrite the developer's own residue), and the directories that do
    # hold a settings payload are non-empty, so the empty-directory prune
    # cannot reach them either.
    #
    # Redirected rather than swept so the suite's scratch never leaves the
    # suite in the first place -- the same shape as the lock root above, and
    # keyed on the variable production reads so there is one source of truth
    # for "where the private directories go".  Nesting it under the session
    # root is safe here, unlike the socket: these are regular paths, with no
    # ``sun_path`` cap to clear.
    subagent_root = state_db_dir / "private"
    os.environ.setdefault("PDT_SECRET_TEMP_ROOT", str(subagent_root))

    # ---------------------------------------------------------------------
    # CC Switch's own database, and the ``$HOME`` it is found under.
    # ---------------------------------------------------------------------
    #
    # ``server._lifespan`` calls ``load_providers()``, which asks
    # ``cc_switch.get_provider()`` for every name in the fallback chain, and
    # ``load_providers`` **re-raises** ``CCSwitchError`` rather than degrade.
    # So on a machine with no CC Switch -- which is every CI runner -- a test
    # that boots the app dies in the lifespan, for an environment reason:
    #
    #   cc_switch.CCSwitchDBNotFoundError: CC Switch database not found:
    #   ~/.cc-switch/cc-switch.db          (on a CI runner: the runner's home)
    #
    # and the suite's result depends on whether the operator happens to have
    # the desktop app installed.  That is the same disease the rest of this
    # file treats: state this repository does not own deciding a test's
    # verdict.
    #
    # ``$HOME`` is the redirect rather than ``PDT_CC_SWITCH_DB``, and the
    # alternatives were measured, not guessed:
    #
    #   * ``PDT_CC_SWITCH_DB`` -- the documented override, but
    #     ``resolve_db_path`` consults it FIRST, so a session-wide value
    #     outranks the ``CC_SWITCH_DB`` that ``tests/test_plan_usage.py`` sets
    #     per test.  Measured: 12 of its 15 tests fail.
    #   * ``CC_SWITCH_DB`` -- the compatibility alias, same outranking for
    #     every test that ``monkeypatch.delenv("PDT_CC_SWITCH_DB")`` without
    #     deleting the alias.  Measured: 4 of the path-resolution tests in
    #     ``tests/unit/test_cc_switch_probe.py`` fail.
    #
    # ``$HOME`` loses neither: those tests all set their own ``HOME``, and a
    # test that names a database through either variable still outranks this
    # one, because ``resolve_db_path`` checks the override before it ever
    # looks at a candidate path.
    #
    # The directory and the database are built in the same statement, in this
    # function, so there is no window in which the database exists somewhere
    # other than the home the resolver will be pointed at.  A per-test
    # ``monkeypatch.setenv("HOME", ...)`` still wins for that test -- it is
    # the test's own home, and it is undone with the test.
    fake_home_env = "PDT_TEST_FAKE_HOME"
    fake_home_owner_env = "PDT_TEST_FAKE_HOME_OWNER"
    if not os.environ.get(fake_home_env):
        fake_home = Path(tempfile.mkdtemp(prefix="home-"))
        write_minimal_cc_switch_db(fake_home / ".cc-switch")
        os.environ[fake_home_env] = str(fake_home)
        os.environ[fake_home_owner_env] = str(os.getpid())
        # Remember the home we were launched with, before the ``HOME``
        # assignment below overwrites it.  A handful of tests need a target
        # path that is provably **outside** tempdir, and the whole point of
        # the redirect is that ``$HOME`` now names a ``mkdtemp`` directory
        # under the temp root -- i.e. inside exactly the zone the Edit/Write
        # containment guard treats as always-writable scratch:
        #
        #     in_tmp_scratch(x) = x.startswith("/tmp/")
        #                        or x.startswith("/private/tmp/")
        #                        or inside(x, tempfile.gettempdir())
        #
        # So a guard test that derives its target from ``Path.home()`` reads
        # as "inside project_dir is fine" long before it reaches the branch
        # it exists to exercise: it asserts rc=2, gets rc=0, and the
        # containment rule it is the only coverage for is untested.  That is
        # a green suite that stopped testing something, which is the failure
        # mode this module exists to prevent elsewhere -- so the real home is
        # published here for those cases to read instead.
        #
        # ``setdefault`` for the nested-session case the owner-pid check
        # below describes: a child pytest inherits this variable *and* an
        # already-redirected ``$HOME``, so an unconditional assignment would
        # record the parent's fake home as the "real" one and make exactly
        # the cases above vacuous again, in every nested run.
        #
        # ``os.environ.get("HOME") or ...`` rather than ``Path.home()``
        # because that is the idiom ``state_machine/tests/conftest.py``
        # already uses for this same capture, and ``Path.home()`` here would
        # be the *redirected* value on any second call.
        os.environ.setdefault(REAL_HOME_ENV, os.environ.get("HOME") or str(Path.home()))
    home_root = Path(os.environ[fake_home_env])
    home_root_owner_pid = int(os.environ[fake_home_owner_env])
    # The owner pid is what keeps a *nested* pytest session from deleting this
    # root.  ``tests/integration/test_reclaim_pairs_driver.py`` starts a second
    # interpreter, which inherits ``PDT_TEST_FAKE_HOME`` through the
    # environment and reuses this directory rather than minting its own -- and
    # its ``pytest_sessionfinish`` would otherwise remove the database out
    # from under the run that created it, which shows up much later as an
    # unrelated ``CCSwitchDBNotFoundError``.  Measured: without the pid check,
    # three tests here and ``test_plan_state_files_are_allowed_outside_scratch``
    # fail as soon as that driver runs first.
    #
    # The state database above does not need this: it is created lazily by
    # whichever module opens it next, so losing it costs nothing.
    # Assigned rather than defaulted: ``HOME`` is always already set, so a
    # ``setdefault`` would be a no-op and the whole point would be lost.  An
    # ambient ``HOME`` is exactly the state in which ``Path.home()`` names the
    # operator's real ``~/.cc-switch``, so it is overwritten rather than
    # honoured -- the same reasoning as the ``PDT_LOCK_BROKER`` pop above.
    os.environ["HOME"] = str(home_root)

    # Rust's toolchain lives under the home that was just replaced: rustup
    # resolves its default toolchain from ``$RUSTUP_HOME`` (default
    # ``$HOME/.rustup``) and cargo's registry from ``$CARGO_HOME`` (default
    # ``$HOME/.cargo``). Pointed at a bare ``mkdtemp`` directory, rustup
    # answers "could not choose a version of cargo to run" instead of running
    # the command. That changes a verdict, not just a log line:
    # ``cargo test --lib <absent module>`` exits 0 with zero tests collected,
    # which is exactly the shape
    # ``AutonomousAgent._enforce_new_subtask_test_command`` exists to discard
    # (a command that can never certify the work). Made to exit non-zero by
    # the missing toolchain it looks falsifiable and is kept. Measured:
    # ``tests/unit/test_new_subtask_test_command_gate.py::test_a_cargo_command_
    # against_a_missing_module_is_discarded`` passes on 7ca1825 and fails with
    # the bare redirect, and so did the ``unit-09`` CI shard.
    #
    # These two name *toolchain locations*, not user state -- the same class
    # as the PATH the runner already has. Filling them in keeps the hermetic
    # home without taking the compiler away. Only when unset and only when the
    # directory exists, so an operator who already points them elsewhere is
    # left alone.
    launch_home = os.environ.get(REAL_HOME_ENV)
    if launch_home:
        for var, sub in (("CARGO_HOME", ".cargo"), ("RUSTUP_HOME", ".rustup")):
            if not os.environ.get(var):
                candidate = Path(launch_home) / sub
                if candidate.is_dir():
                    os.environ[var] = str(candidate)

    env = HermeticEnv(
        state_db_dir=state_db_dir,
        lock_root=lock_root,
        subagent_root=subagent_root,
        home_root=home_root,
        home_root_owner_pid=home_root_owner_pid,
        lock_root_owner_pid=lock_root_owner_pid,
        state_root_owner_pid=state_root_owner_pid,
    )
    _INSTALLED_ENV = env
    return env


def xdist_worker_id() -> str:
    """``PYTEST_XDIST_WORKER`` or ``""`` -- who owns the teardown of the roots.

    The controller loads a conftest (and therefore this module) during
    ``_preparse``, before it forks any worker.  It runs no test, so no
    session-scoped fixture is ever instantiated in it, and its scratch roots
    would otherwise be the one thing per run that survives -- reintroducing
    the leak the idempotency note in :func:`install_hermetic_env` exists to
    prevent.  Holding a worker id is what tells a teardown "someone else
    owns this, leave it alone".
    """
    return os.environ.get("PYTEST_XDIST_WORKER") or ""
