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

# The database is named and redirected through the one resolver, exactly as
# every other module that opens it does.  ``test_state_db_path_has_one_resolver``
# exists because nine copies of "``PDT_STATE_DB_PATH`` else a path derived from
# my own ``__file__``" were each correct on the day they were written and two
# had already drifted to a different valid file; a tenth copy written here for
# convenience would be the tenth instance of that shape.  ``STATE_DB_ENV`` is
# the variable's name and ``STATE_DB.name`` its filename, so this module
# follows the resolver if either ever changes.
from config_paths import STATE_DB, STATE_DB_ENV

__all__ = [
    "HermeticEnv",
    "install_hermetic_env",
    "install_request_guard_header",
    "xdist_worker_id",
]


@dataclass(frozen=True)
class HermeticEnv:
    """The three scratch roots a pytest session owns.

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


def install_hermetic_env() -> HermeticEnv:
    """Install the process-global environment a pytest run must not inherit.

    Every entry below is an environment *contract*, not a convenience: each
    exists because leaving it ambient cost a real incident, and the
    reasoning travelled with the rule when it was lifted out of
    ``backend/tests/conftest.py``.
    """
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
    if not os.environ.get("PDT_TEST_SESSION_ROOT"):
        os.environ["PDT_TEST_SESSION_ROOT"] = tempfile.mkdtemp(prefix="ac-test-state-")
        os.environ[session_root_owner] = ""
    elif xdist_worker and os.environ.get(session_root_owner) != xdist_worker:
        worker_state_db_dir = Path(os.environ["PDT_TEST_SESSION_ROOT"]) / xdist_worker
        worker_state_db_dir.mkdir(parents=True, exist_ok=True)
        os.environ["PDT_TEST_SESSION_ROOT"] = str(worker_state_db_dir)
        os.environ[session_root_owner] = xdist_worker
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

    return HermeticEnv(
        state_db_dir=state_db_dir,
        lock_root=lock_root,
        subagent_root=subagent_root,
    )


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
