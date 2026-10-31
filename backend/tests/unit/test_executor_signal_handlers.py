"""The executor answers its own SIGTERM/SIGINT — reaps, releases, exits.

Why these tests exist
---------------------
An executor that is killed leaves its whole tree behind. Every layer of
PDT's tree detaches from its parent on purpose — a sub-agent runs in
its own session, a VP service in another — so the executor's process
group holds none of them, and a ``kill`` on the executor reaches the
executor and nothing else. The sub-agent keeps its tools, the VP
service keeps its port, and nobody is left to own them.

The executor is therefore the last process that knows the tree
exists, and until now it had no signal handler at all. This is where
it says goodbye, and the order is the whole point:

  * **the tree is collected first** — ``reap_plan`` sweeps the ledger
    in the ledger's own order (child before parent), so the sub-agent
    goes before the executor it belongs to;
  * **the lock is released** — the single-instance guard is released
    by the kernel when the process dies, but a *clean* release is what
    lets the next executor start at once instead of waiting for the
    kernel to notice;
  * **the exit code says what happened** — 143 for SIGTERM and 130 for
    SIGINT, the shell's own convention (``128 + signum``), so whatever
    watches the executor can tell a kill from a crash.

And the two things the handler must *not* do:

  * **re-enter the sweep.** A second Ctrl-C must not start a second
    reap: the first one is still collecting the tree, and a second
    sweep would signal processes the first one has already collected.
  * **touch the backend's handlers.** The backend runs under uvicorn,
    which owns the SIGTERM/SIGINT handlers of the process it is in; an
    executor's handler installed in the backend would replace them and
    change how the server shuts down. So the installer is called by the
    executor's own entry point and by nothing else — ``server.py`` and
    ``routes/`` do not even mention it — and importing the CLI installs
    nothing.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import executor_lock  # noqa: E402
import process_identity  # noqa: E402
import process_ledger as ledger  # noqa: E402
import process_reaper as reaper  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _spawn(argv, cwd=None):
    """Start a real child process and return the ``Popen`` handle.

    The child gets its own session, which is what a detached PDT layer
    looks like: the reaper refuses to signal a group that shares the
    caller's own, so a test that wants the child signalled at all has
    to detach it — exactly like the real tree.
    """
    return subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
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

    On macOS the framework launcher rewrites ``argv[0]`` shortly after
    the exec, so a reading taken the instant a process is spawned can
    spell the command line differently from one taken a moment later.
    The identity check compares command lines, so the *recorded* one
    has to be the settled one.
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


def _record(plan_dir, role, proc, *, plan_id="p1"):
    """Write a spawn record for ``proc`` into the ledger at ``plan_dir``."""
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
        owner_pid=os.getpid(),
        owner_role="backend",
    )
    ledger.record_spawn(plan_dir, record)
    return record


def _fire(signum):
    """Deliver ``signum`` to the handler that is installed right now.

    The handler is called directly rather than by raising a real
    signal: the code under test cannot tell the two apart, and a real
    signal delivered to the pytest process would be delivered to
    whatever else is listening too.
    """
    handler = signal.getsignal(signum)
    assert callable(handler), "no handler is installed for {}".format(signum)
    return handler(signum, None)


@pytest.fixture
def restored_signal_handlers():
    """Put the process's own SIGTERM/SIGINT handlers back afterwards.

    The installer replaces them for the duration of a test, and a test
    that leaves a handler behind changes how the rest of the suite
    behaves when something raises SIGTERM.
    """
    saved = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        yield
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


# ---------------------------------------------------------------------------
# the tree is collected before the executor leaves
# ---------------------------------------------------------------------------

def test_sigterm_reaps_children_before_exit(tmp_path, restored_signal_handlers):
    """SIGTERM collects the tree, child first, and then exits 143.

    The sub-agent goes before the executor that started it: killing the
    parent first would leave exactly the processes the sweep is trying
    to collect running with no owner. The exit code is 143 — ``128 +
    15`` — because that is what a shell reports for a process killed
    by SIGTERM, and whatever watches the executor reads that code.
    """
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()
    subagent = _spawn(["sleep", "30"], cwd=tmp_path)
    executor = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record(plan_dir, "subagent", subagent)
        _record(plan_dir, "executor", executor)

        reaper.install_executor_signal_handlers(plan_dir, "p1")

        with pytest.raises(SystemExit) as excinfo:
            _fire(signal.SIGTERM)

        assert excinfo.value.code == 143

        killed = [
            event for event in ledger.read_events(plan_dir)
            if event["event"] == "killed"
        ]
        assert [event["role"] for event in killed] == ["subagent", "executor"]

        # Both children are actually gone, not merely recorded as gone.
        assert subagent.poll() is not None
        assert executor.poll() is not None
    finally:
        _cleanup(subagent, executor)


def test_sigint_exits_130(tmp_path, restored_signal_handlers):
    """SIGINT exits 130 — ``128 + 2``, the shell's convention."""
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()

    reaper.install_executor_signal_handlers(plan_dir, "p1")

    with pytest.raises(SystemExit) as excinfo:
        _fire(signal.SIGINT)

    assert excinfo.value.code == 130


def test_handler_releases_executor_lock(tmp_path, restored_signal_handlers):
    """The single-instance lock is released on the way out.

    The kernel releases an ``flock`` when the holder dies, so the guard
    survives a crash — but a *clean* release is what lets the next
    executor start at once. A lock that is still held when the next
    executor asks for it is a plan that looks busy for no reason.
    """
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()
    lock = executor_lock.acquire(plan_dir)
    assert lock is not None
    assert executor_lock.is_held(plan_dir)

    reaper.install_executor_signal_handlers(plan_dir, "p1", lock=lock)

    with pytest.raises(SystemExit):
        _fire(signal.SIGTERM)

    assert not executor_lock.is_held(plan_dir)


def test_repeated_signal_does_not_reap_twice(
    tmp_path, monkeypatch, restored_signal_handlers
):
    """A second signal does not start a second sweep.

    The first signal's sweep is still collecting the tree when the
    second one arrives. Running it again would signal processes the
    first sweep has already collected — and the second sweep would
    wait out its own grace window on top of the first one's.
    """
    calls = []
    original = reaper.reap_plan

    def counting_reap(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(reaper, "reap_plan", counting_reap)

    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()

    reaper.install_executor_signal_handlers(plan_dir, "p1")

    with pytest.raises(SystemExit):
        _fire(signal.SIGTERM)
    with pytest.raises(SystemExit):
        _fire(signal.SIGTERM)

    assert len(calls) == 1


def test_install_from_a_non_main_thread_is_a_warning_not_a_failure(
    tmp_path, restored_signal_handlers
):
    """A worker thread cannot own the process's signals, and says so.

    ``signal.signal`` only works on the main thread; asking it to do
    otherwise raises. An executor that runs its work on a worker thread
    still has to start — a warning is a fact, a refusal is an outage.
    """
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()
    before = signal.getsignal(signal.SIGTERM)
    errors = []

    def install():
        try:
            reaper.install_executor_signal_handlers(plan_dir, "p1")
        except Exception as exc:  # noqa: BLE001 - the point of the test
            errors.append(exc)

    thread = threading.Thread(target=install)
    thread.start()
    thread.join(timeout=30)

    assert errors == []
    assert signal.getsignal(signal.SIGTERM) is before


# ---------------------------------------------------------------------------
# the backend's own handlers are not touched
# ---------------------------------------------------------------------------

def test_cli_import_installs_no_signal_handlers():
    """Importing the CLI installs nothing.

    The CLI is imported by the backend's own tooling, and a handler
    installed at import time would replace the SIGTERM/SIGINT handlers
    of whatever process imported it — the backend included, where
    uvicorn owns them. The check runs in a fresh interpreter: in this
    one, pytest has already imported the module and the state being
    observed is not the state an import would produce.
    """
    code = (
        "import signal, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "before = (signal.getsignal(signal.SIGTERM),\n"
        "          signal.getsignal(signal.SIGINT))\n"
        "import cli\n"
        "after = (signal.getsignal(signal.SIGTERM),\n"
        "         signal.getsignal(signal.SIGINT))\n"
        "assert before == after, (before, after)\n"
        "print('handlers-unchanged')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(_BACKEND_DIR)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "handlers-unchanged" in result.stdout


def test_backend_does_not_call_install_executor_signal_handlers():
    """The backend never installs the executor's handlers.

    The backend runs under uvicorn, which owns the SIGTERM/SIGINT
    handlers of its process. A handler installed there would change
    how the server shuts down — a change nobody asked for and nobody
    would notice until the server stopped shutting down cleanly. The
    executor is a different process, and this is its own handler.
    """
    targets = [_BACKEND_DIR / "server.py"]
    targets.extend(sorted((_BACKEND_DIR / "routes").rglob("*.py")))

    offenders = []
    for path in targets:
        text = path.read_text(encoding="utf-8")
        if "install_executor_signal_handlers" in text:
            offenders.append(str(path.relative_to(_BACKEND_DIR)))

    assert offenders == []


def test_a_cli_run_puts_the_handlers_back(tmp_path, monkeypatch):
    """A run of the CLI leaves the process's own handlers in place.

    The handlers belong to the run. Anything that calls the CLI inside
    a process that already has handlers — a test does, today — would
    otherwise leave the executor's handlers behind after the run ends,
    and the process would go on answering signals that were never
    meant for it. The executor is a process of its own, so nothing
    needs its handlers after the run.
    """
    import cli

    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()
    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text('{"tasks": []}', encoding="utf-8")

    monkeypatch.setattr(cli, "autonomous_coding", lambda **kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli", "-w", str(tmp_path), "--tasks-file", str(tasks_file)],
    )

    before = signal.getsignal(signal.SIGTERM)
    cli.main()

    assert signal.getsignal(signal.SIGTERM) is before
