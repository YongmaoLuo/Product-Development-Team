"""The shutdown sweep belongs to the backend — not to every importer.

Why these tests exist
---------------------

``server`` registers its shutdown sweep with ``atexit`` **at import
time**.  That is the right shape for a backstop (the hook has to be
there before the process can die), but it gave every *importer* of the
module the right to signal the whole plans tree: a pytest run, a
one-off script, an executor that imports the server module — each of
them exits normally, and each exit ran
``_sweep_process_ledgers(PLANS_DIR, reason="server_shutdown")`` over
every plan the tree knew about.

The damage was not hypothetical.  The plans tree carries
``killed reason="server_shutdown"`` events written by processes that
were never the backend, and the count of them dwarfs the count of
clean-shutdown backups: the backend's own exit kills a plan's tree on
purpose, but a *test run*'s exit killed the tree of a backend that was
still serving.

Two gates close that, and each is pinned here:

1. **The atexit backstop only runs in a process that started the
   app.**  The registration stays at module level (the hook has to
   exist before the process can die); the *work* is gated on
   ``server._SERVER_PROCESS_STARTED``, which only the lifespan sets.
2. **A ``server_shutdown`` sweep only collects what the departing
   process owned.**  A ledger record whose ``owner_pid`` names a
   *different, live* process is not an orphan — it belongs to a backend
   that is still running — so the reaper skips it and says why
   (``owner_alive``).  The gate is scoped to that one reason: a forced
   start must still be able to clear a stale executor whose owner is
   alive.

The third test pins the directory the sweep reads: it is resolved when
the sweep *runs*, not frozen when the module was imported, so a
redirected ``PDT_PLANS_DIR`` is honoured.

Hermeticity
-----------

The two reaper cases use **real** child processes — a real owner and a
real fake executor — rather than a mocked ``os.kill``: the whole point
of the gate is that it asks the kernel whether the owner is alive, and
a mock would answer the question instead of the kernel.  Both children
are killed on teardown.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import process_identity  # noqa: E402
import process_ledger as ledger  # noqa: E402
import process_reaper as reaper  # noqa: E402
import server  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _spawn(argv, cwd=None, new_session=True):
    """Start a real child process and return the ``Popen`` handle.

    ``new_session`` gives the child its own process group.  The reaper
    refuses to signal a group that is the caller's own, so a test that
    wants the child signalled at all has to detach it — exactly like
    the real tree, where the executor runs in its own session.
    """
    return subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=new_session,
    )


def _cleanup(*procs):
    """Kill and reap every child the test started."""
    for proc in procs:
        if proc is None:
            continue
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:  # noqa: BLE001 - cleanup must not fail the test
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass


def _stable_identity(pid, tries=40, delay=0.05):
    """Probe ``pid`` until two consecutive readings agree.

    On macOS the launcher rewrites ``argv[0]`` shortly after the exec,
    so a reading taken the instant a process is spawned can spell the
    command line differently from a reading taken a moment later.  The
    identity check compares command lines, so the *recorded* one has to
    be the settled one.
    """
    previous = None
    for _ in range(tries):
        ident = process_identity.probe(pid)
        if ident is not None:
            if previous is not None and ident.cmdline == previous.cmdline:
                return ident
            previous = ident
        time.sleep(delay)
    return previous


def _record_for(plan_dir, role, proc, *, owner_pid, plan_id="p1"):
    """Write a spawn record for ``proc`` into the ledger at ``plan_dir``.

    ``owner_pid`` says who started it.  The identity fields come from
    the kernel, so the reaper's own identity check can vouch for the
    record — the gate under test is the *owner* check, and it must not
    be reached through a record the identity check would refuse.
    """
    ident = _stable_identity(proc.pid)
    assert ident is not None, "the test child has no readable identity"
    record = ledger.ProcessRecord(
        plan_id=plan_id,
        role=role,
        pid=proc.pid,
        pgid=ident.pgid if ident.pgid else proc.pid,
        create_time=ident.create_time,
        cmdline=list(ident.cmdline),
        cwd=ident.cwd or "",
        owner_pid=owner_pid,
        owner_role="backend",
    )
    ledger.record_spawn(plan_dir, record)
    return record


def _ledger_events(plan_dir):
    """Every event in the plan's ledger, as dictionaries."""
    path = ledger.ledger_path(plan_dir)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# the atexit backstop
# ---------------------------------------------------------------------------

class _FakeFeishuNotifier:
    """A stand-in for the Feishu notifier.

    The real notifier's client pulls the whole Feishu SDK in on
    ``start()`` — tens of seconds — and talks to the OS keychain.  The
    lifespan starts it on the boot path, and this test is about the boot
    path, not about Feishu.
    """

    _min_interval_seconds = 60.0

    def start(self) -> None:
        """There is no worker to start."""

    def stop(self, timeout: float = 5.0) -> None:
        """There is no worker to stop."""


def test_the_lifespan_marks_the_process_as_the_backend(monkeypatch):
    """The lifespan is the production entry that grants the right to sweep.

    Everything else in this module exercises the hook directly; this
    one goes in through the door the backend actually comes through —
    the FastAPI lifespan — and watches the flag it leaves behind.

    The notifier, the provider chain, the recovery passes and the boot
    sweeps are stubbed: they are on the same startup path, but they are
    not what is under test, and the boot sweep in particular would
    sweep this machine's plans tree as a side effect.
    """
    import notifications.feishu_notifier as feishu_notifier_module

    monkeypatch.setattr(server, "_recover_execution_states", lambda plans_dir: None)
    monkeypatch.setattr(
        server, "_recover_verification_states", lambda plans_dir: None
    )
    monkeypatch.setattr(server, "load_providers", lambda: [])
    monkeypatch.setattr(server, "heartbeat_monitor", MagicMock())
    monkeypatch.setattr(
        server, "_start_boot_sweeps",
        lambda: threading.Thread(target=lambda: None, daemon=True),
    )
    monkeypatch.setattr(
        feishu_notifier_module, "FeishuNotifier", _FakeFeishuNotifier
    )
    # The shutdown sweep itself is not what this test is about, and it
    # would signal this machine's plans tree.
    monkeypatch.setattr(server, "_shutdown_all_executions", lambda: None)

    app = MagicMock()
    app.state = MagicMock()
    seen: dict[str, bool] = {}

    async def _enter() -> None:
        async with server._lifespan(app):
            seen["inside"] = server._SERVER_PROCESS_STARTED

    asyncio.run(_enter())
    seen["after"] = server._SERVER_PROCESS_STARTED

    assert seen.get("inside") is True, (
        "the lifespan must mark the process as the backend while it "
        f"serves; the flag was {seen.get('inside')!r}"
    )
    assert seen.get("after") is False, (
        "the process must stop being the backend when the app goes "
        f"down; the flag was {seen.get('after')!r}"
    )



def test_atexit_sweep_does_nothing_without_a_started_server(monkeypatch):
    """A process that only *imported* the module must not sweep.

    This is the regression pin: the registration used to point straight
    at ``_shutdown_all_executions``, so every importer's normal exit
    signalled the plans tree.
    """
    calls: list[int] = []
    monkeypatch.setattr(server, "_SERVER_PROCESS_STARTED", False)
    monkeypatch.setattr(server, "_shutdown_all_executions", lambda: calls.append(1))

    server._atexit_shutdown_sweep()

    assert calls == [], (
        "a process that never started the application must not sweep the "
        f"plans tree; it swept {len(calls)} time(s)"
    )


def test_atexit_sweep_sweeps_once_after_the_server_started(monkeypatch):
    """The backstop still fires in the process that *is* the backend."""
    calls: list[int] = []
    monkeypatch.setattr(server, "_SERVER_PROCESS_STARTED", True)
    monkeypatch.setattr(server, "_shutdown_all_executions", lambda: calls.append(1))

    server._atexit_shutdown_sweep()

    assert calls == [1], (
        f"the atexit backstop must sweep exactly once in the backend "
        f"process; it ran {len(calls)} time(s)"
    )


def test_the_atexit_backstop_is_still_registered():
    """The hook exists at module level — the gate is in the body, not the
    registration.

    Moving the registration *into* the lifespan would leave the process
    without a backstop for the window between "the app started" and
    "the hook was installed".
    """
    tree = ast.parse((_BACKEND_DIR / "server.py").read_text(encoding="utf-8"))
    registered = []
    for node in tree.body:
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "register"
            and isinstance(func.value, ast.Name)
            and func.value.id == "atexit"
        ):
            continue
        if call.args and isinstance(call.args[0], ast.Name):
            registered.append(call.args[0].id)

    assert "_atexit_shutdown_sweep" in registered, (
        "server.py must still register the atexit backstop at module "
        f"level; the module-level registrations are {registered!r}"
    )


# ---------------------------------------------------------------------------
# the owner gate
# ---------------------------------------------------------------------------

def test_server_shutdown_leaves_a_process_whose_owner_is_alive(tmp_path):
    """A live owner means the record is not an orphan — it is left alone.

    The fake executor is a real child process; the owner is another
    real child process that stays alive for the whole test.  The sweep
    must not signal the executor, must say why it did not, and must
    write that down.
    """
    owner = _spawn(["sleep", "30"], cwd=tmp_path)
    executor = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        assert owner.pid != os.getpid(), (
            "the owner must be a different, live process than the reaper"
        )
        _record_for(tmp_path, "executor", executor, owner_pid=owner.pid)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="server_shutdown", grace_seconds=2.0
        )

        assert executor.poll() is None, (
            f"the sweep signalled pid {executor.pid}, whose owner "
            f"({owner.pid}) is still alive — that process belongs to a "
            "backend that is still running"
        )
        assert outcome.reaped == [], (
            f"the sweep reaped {outcome.reaped!r}; a live owner means the "
            "record is not an orphan"
        )
        assert outcome.skipped == [{
            "pid": executor.pid,
            "role": "executor",
            "reason": "owner_alive",
        }], (
            "the sweep must report the record it left alone, with the "
            f"reason 'owner_alive'; it reported {outcome.skipped!r}"
        )

        skipped_events = [
            e for e in _ledger_events(tmp_path)
            if e.get("event") == "skipped"
            and e.get("reason") == "owner_alive"
        ]
        assert len(skipped_events) == 1, (
            "the ledger must carry exactly one 'skipped owner_alive' "
            f"event for the record; it carries {len(skipped_events)}"
        )
        assert skipped_events[0].get("pid") == executor.pid
    finally:
        _cleanup(owner, executor)


def test_start_force_still_reaps_a_process_whose_owner_is_alive(tmp_path):
    """The gate is scoped to ``server_shutdown`` — a forced start is not.

    A stale executor whose owner is alive has to be collectable: the
    forced start is exactly the path that clears a tree the previous
    backend left behind, and it must not be narrowed by the shutdown
    rule.
    """
    owner = _spawn(["sleep", "30"], cwd=tmp_path)
    executor = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record_for(tmp_path, "executor", executor, owner_pid=owner.pid)

        outcome = reaper.reap_plan(
            tmp_path, "p1", reason="start_force", grace_seconds=2.0
        )

        assert executor.pid in outcome.reaped, (
            f"a forced start must still reap a stale executor; it reaped "
            f"{outcome.reaped!r} and skipped {outcome.skipped!r}"
        )
        assert outcome.skipped == [], (
            f"the forced start must not skip the record; it skipped "
            f"{outcome.skipped!r}"
        )
        executor.wait(timeout=10)
        assert executor.poll() is not None, (
            "the executor process was still running after the forced reap"
        )
    finally:
        _cleanup(owner, executor)


# ---------------------------------------------------------------------------
# the directory the sweep reads
# ---------------------------------------------------------------------------

def test_shutdown_sweep_resolves_the_plans_dir_at_call_time(
    tmp_path, monkeypatch
):
    """The sweep reads the plans root when it runs, not when it was imported.

    ``server.PLANS_DIR`` is frozen at import time; a process that
    redirects ``PDT_PLANS_DIR`` afterwards would otherwise sweep the
    tree the import-time value named.
    """
    target = tmp_path / "plans-at-call-time"
    target.mkdir()

    seen: list[Path] = []

    def _fake_sweep(plans_dir, *, reason, **sweep_options):
        seen.append(Path(plans_dir))
        return []

    monkeypatch.setenv("PDT_PLANS_DIR", str(target))
    monkeypatch.setattr(server, "_execution_state", {})
    monkeypatch.setattr(server, "_verification_state", {})
    monkeypatch.setattr(server, "_sweep_process_ledgers", _fake_sweep)

    server._shutdown_all_executions()

    assert seen == [target], (
        f"the sweep must read the plans root at call time; it swept "
        f"{seen!r} instead of {str(target)!r}"
    )
    assert seen[0] != Path(server.PLANS_DIR), (
        "the call-time directory and the import-time one are the same in "
        "this test, so the assertion above proves nothing"
    )
