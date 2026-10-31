"""A service this run started has to be given back -- with what it started.

Why this file exists
--------------------
The CI of 2026-10-31 was killed by its own watchdog at the 3600 s mark,
and the machine kept what that run had started: a ``backend.server``
answered ``/health`` a minute after the watchdog had fired, and
twenty-six ``orphan_reaper --daemon`` processes were still walking the
machine hours later.  Every one of them had been started by a run that
had already ended.

The mechanism is the service's own detachment.  A declared service is
started through a wrapper that calls ``setsid()``, so it leads its own
session; the watchdog, on the other hand, kills the **process group**
of the gate.  The service is not in that group, so it survives -- and
so does the reaper daemon the service itself started, which leads a
session of its own again.

What has to be true now
------------------------
* Every detached process a run starts is **written down** while the run
  is still alive, so the run can find it again afterwards;
* every cleanup path **gives it back**: the gate on its way out, and
  the CI runner after the gate returns -- including when the gate was
  killed and never got to its own cleanup;
* a service that is given back takes the reaper daemon it started with
  it;
* a pid whose number was recycled in the meantime is **not** given
  back: it is somebody else's process now.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402
from verification_ci_runner import run_ci_verification  # noqa: E402

#: The python the gate and the runner use.
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python"

#: The gate script under contract.
GATE_SCRIPT = REPO_ROOT / "scripts" / "run_tests.sh"


# --------------------------------------------------------------------------
# Fixtures that behave like the processes this is about
# --------------------------------------------------------------------------


def _spawn_detached(seconds: float = 600.0) -> subprocess.Popen:
    """A process in a session of its own -- the shape a service has.

    ``start_new_session=True`` is what ``service_manager``'s wrapper
    achieves with ``setsid()``: the child leads its own session and its
    own process group, so a signal aimed at the gate's group does not
    reach it.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(%d)" % int(seconds)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _alive(pid: Optional[int]) -> bool:
    """Whether *pid* is a running process.  A zombie does not count.

    ``kill(pid, 0)`` succeeds for a zombie, and a child of this test
    that has been killed stays a zombie until it is waited for.  The
    process state is what says whether there is anything left to kill.
    """
    if not pid or pid <= 1:
        return False
    try:
        completed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    state = completed.stdout.strip()
    return bool(state) and not state.startswith("Z")


def _kill(proc: Optional[subprocess.Popen]) -> None:
    """Make sure a fixture process is gone, whatever the test did."""
    if proc is None:
        return
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# --------------------------------------------------------------------------
# The registry: what the run started, so the run can give it back
# --------------------------------------------------------------------------


def test_a_service_that_is_reaped_takes_its_reaper_daemon_with_it(
    tmp_path,
):
    """The daemon a service started goes away with the service.

    The daemon is started by the service, in a session of its own, and
    it carries the service's owner marker.  None of that makes it go
    away when the service dies -- so the run has to give it back
    explicitly, and the registry is what lets it: the daemon writes
    itself down when it starts, and the cleanup path gives back
    everything the registry names.
    """
    registry = tmp_path / "spawn_registry.jsonl"
    service = _spawn_detached()
    daemon = _spawn_detached()
    try:
        reaper.register_spawned_process(
            service.pid, pgid=service.pid, kind=reaper.KIND_SERVICE,
            name="backend.server", path=registry,
        )
        reaper.register_spawned_process(
            daemon.pid, pgid=daemon.pid, kind=reaper.KIND_REAPER,
            name="orphan-reaper-daemon", path=registry,
        )

        reaped = reaper.reap_spawned_processes(
            registry, reason="the gate is over", grace_seconds=3.0,
        )

        assert _alive(service.pid) is False, (
            "the service is still running after the run gave it back"
        )
        assert _alive(daemon.pid) is False, (
            "the reaper daemon the service started is still running after "
            "the service was given back"
        )
        assert {int(entry["pid"]) for entry in reaped} == {
            service.pid, daemon.pid,
        }, f"the run gave back something else: {reaped}"
    finally:
        _kill(service)
        _kill(daemon)


def test_a_process_that_is_already_gone_is_forgotten(tmp_path):
    """A process that is not there any more is not a process to reap.

    The registry is a record of what the run started, and the run
    starts a lot of short-lived things.  An entry for a process that
    has already exited is dropped rather than kept: a registry that
    only grows is a registry nobody can read.
    """
    registry = tmp_path / "spawn_registry.jsonl"
    gone = _spawn_detached()
    _kill(gone)

    reaper.register_spawned_process(
        gone.pid, pgid=gone.pid, kind=reaper.KIND_SERVICE,
        name="backend.server", path=registry,
    )

    reaped = reaper.reap_spawned_processes(registry, grace_seconds=1.0)

    assert reaped == [], f"a process that was already gone was reaped: {reaped}"
    assert reaper.read_spawn_registry(registry) == [], (
        "the registry still holds an entry for a process that is gone"
    )


def test_a_recycled_pid_is_not_signalled(tmp_path):
    """A pid whose number came back is somebody else's process.

    A pid is not an identity.  The registry records the start time
    next to the pid for exactly this reason, and a pid whose start
    time no longer matches is left alone: killing it would kill a
    process this run never started.
    """
    registry = tmp_path / "spawn_registry.jsonl"
    other = _spawn_detached()
    try:
        reaper.register_spawned_process(
            other.pid, pgid=other.pid, kind=reaper.KIND_SERVICE,
            name="not ours", path=registry,
        )
        # The entry claims a different start time: the number came back.
        entries = reaper.read_spawn_registry(registry)
        entries[0]["start_epoch"] = "1"
        registry.write_text(
            json.dumps(entries[0], ensure_ascii=False) + "\n", encoding="utf-8",
        )

        reaped = reaper.reap_spawned_processes(registry, grace_seconds=1.0)

        assert reaped == [], f"a recycled pid was reaped: {reaped}"
        assert _alive(other.pid) is True, (
            "a process this run never started was killed"
        )
    finally:
        _kill(other)


def _record_signals(monkeypatch) -> list:
    """Record every signal the reaper sends, and send none of them.

    The reaper reaches the kernel through the ``os`` module it
    imported, so patching ``os.kill`` / ``os.killpg`` is what "did the
    reaper signal anything?" is answered by.
    """
    calls: list = []

    def _kill(pid, sig):
        calls.append(("kill", pid, sig))

    def _killpg(pgid, sig):
        calls.append(("killpg", pgid, sig))

    monkeypatch.setattr(reaper.os, "kill", _kill)
    monkeypatch.setattr(reaper.os, "killpg", _killpg)
    return calls


def test_an_unknown_start_epoch_is_skipped(monkeypatch):
    """An entry whose start time was never recorded is not signalled.

    The recording can fail -- the platform told the run nothing about
    when the process started.  The entry then says *unknown*, and
    unknown is not "different": the pid may already have come back to
    somebody else, and a pid that came back must not be killed.
    """
    calls = _record_signals(monkeypatch)
    entry = {
        "pid": 4242, "pgid": None, "kind": "service", "name": "backend.server",
        "owner_pid": 123, "start_epoch": "",
    }

    status = reaper._reap_registry_entry(
        entry, grace_seconds=1.0, reason="the gate is over",
        probe=lambda pid: "100", alive=lambda pid: True,
    )

    assert status == "unknown", (
        f"an entry with no recorded start time was not skipped: {status!r}"
    )
    assert calls == [], (
        f"the run signalled a process it could not identify: {calls}"
    )


def test_an_unobservable_start_epoch_is_skipped(monkeypatch):
    """An entry whose start time cannot be observed now is not signalled.

    The entry was written down with a start time, but the platform
    cannot tell the run the process' start time any more.  The run
    therefore cannot tell a live owner from a recycled pid, and a pid
    that might belong to somebody else is not one to kill.
    """
    calls = _record_signals(monkeypatch)
    entry = {
        "pid": 4242, "pgid": None, "kind": "service", "name": "backend.server",
        "owner_pid": 123, "start_epoch": "100",
    }

    status = reaper._reap_registry_entry(
        entry, grace_seconds=1.0, reason="the gate is over",
        probe=lambda pid: "", alive=lambda pid: True,
    )

    assert status == "unknown", (
        f"an entry whose start time cannot be observed was not skipped: "
        f"{status!r}"
    )
    assert calls == [], (
        f"the run signalled a process whose identity it could not confirm: "
        f"{calls}"
    )


def test_the_registry_is_where_the_environment_says_it_is(tmp_path, monkeypatch):
    """The registry is named by the environment, so children inherit it.

    The service is started by one process and the reaper daemon by
    another; the record of both has to reach the same file, and the
    only thing both of them share is the environment they inherited.
    """
    registry = tmp_path / "spawn_registry.jsonl"
    monkeypatch.setenv(reaper.SPAWN_REGISTRY_ENV, str(registry))

    written = reaper.register_spawned_process(
        os.getpid(), kind=reaper.KIND_SERVICE, name="backend.server",
    )

    assert written == registry
    assert [entry["pid"] for entry in reaper.read_spawn_registry()] == [
        os.getpid(),
    ], "the registry was not written where the environment named it"


def test_without_a_registry_nothing_is_written(monkeypatch):
    """A run that does not name a registry does not get one.

    The backend's own sessions never name one, and they must not grow
    a registry file behind the operator's back.
    """
    monkeypatch.delenv(reaper.SPAWN_REGISTRY_ENV, raising=False)

    assert reaper.register_spawned_process(os.getpid()) is None
    assert reaper.read_spawn_registry() == []
    assert reaper.reap_spawned_processes() == []


# --------------------------------------------------------------------------
# The service is written down when it is started
# --------------------------------------------------------------------------


class _FakeProc:
    """A child that was never really started."""

    pid = 424242

    def poll(self) -> None:
        return None


def test_a_started_service_is_written_down(tmp_path, monkeypatch):
    """``start_service`` records the service it started.

    Without the record, the run that started the service has no way to
    take it back -- and a service nobody takes back is a service that
    outlives the run, which is the shape the CI of 2026-10-31 left on
    the machine.
    """
    registry = tmp_path / "spawn_registry.jsonl"
    monkeypatch.setenv(reaper.SPAWN_REGISTRY_ENV, str(registry))

    real_popen = subprocess.Popen

    def _fake_popen(argv, **kwargs):
        if argv and os.path.basename(str(argv[0])) == "ps":
            # The reaper asking the system when a process started.
            return real_popen(argv, **kwargs)
        return _FakeProc()

    monkeypatch.setattr(sm.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: True)
    monkeypatch.setattr(sm, "_await_exit", lambda proc, timeout=2.0: (False, None))
    monkeypatch.setattr(sm, "_take_data_plane", lambda port: None)
    monkeypatch.setattr(sm, "_SPAWN_MARKER_WARNED", set(), raising=False)

    decl = ServiceDeclaration(
        name="api", port=3999, start_cmd="echo hello", cwd=".",
    )
    sm.start_service(decl, tmp_path)

    entries = reaper.read_spawn_registry(registry)
    assert [int(entry["pid"]) for entry in entries] == [_FakeProc.pid], (
        f"the service was not written down: {entries}"
    )
    assert entries[0]["kind"] == reaper.KIND_SERVICE
    assert entries[0]["name"] == "api"


# --------------------------------------------------------------------------
# The gate that was killed
# --------------------------------------------------------------------------


#: A gate that starts a service and then hangs, so the watchdog fires.
#:
#: The service is started detached -- its own session -- so the
#: watchdog's kill of the gate's process group does not reach it.
#: That is the shape of the 2026-10-31 failure, reproduced.
_HANGING_GATE_SOURCE = '''#!/usr/bin/env python3
import subprocess
import sys
import time

sys.path.insert(0, {backend_dir!r})

import orphan_reaper

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    start_new_session=True,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
orphan_reaper.register_spawned_process(
    child.pid, pgid=child.pid, kind=orphan_reaper.KIND_SERVICE,
    name="backend.server",
)
print("SERVICE_PID=%d" % child.pid, flush=True)
time.sleep(600)
'''


def _service_pid(output: str) -> Optional[int]:
    """The pid the gate printed, if it printed one."""
    for line in output.splitlines():
        if line.startswith("SERVICE_PID="):
            try:
                return int(line.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None


def test_a_gate_that_was_killed_leaves_no_live_service(tmp_path):
    """The watchdog kills the gate; the service the gate started goes too.

    This is the round-2 failure, in miniature: the watchdog fires, the
    gate is killed from the outside, and the service it started -- in
    a session of its own -- is still there afterwards.  The runner is
    the process that outlives the gate, so the runner is the process
    that gives the service back.
    """
    gate = tmp_path / "hanging_gate.py"
    gate.write_text(
        _HANGING_GATE_SOURCE.format(backend_dir=str(BACKEND_DIR)),
        encoding="utf-8",
    )

    result = run_ci_verification(
        {
            "id": "VP-016",
            "verification_method": "full_ci",
            "verification_phase": 2,
            "ci_entry": f"{VENV_PYTHON} {gate}",
            "ci_timeout_seconds": 5,
        },
        project_dir=tmp_path,
    )

    assert result.evidence["timed_out"] is True, (
        "the gate was not killed by the watchdog; the test did not "
        "reproduce the failure it is about"
    )
    pid = _service_pid(result.evidence.get("stdout_tail", ""))
    assert pid is not None, (
        "the gate never said which service it started:\n"
        + str(result.evidence.get("stdout_tail"))
    )
    assert _alive(pid) is False, (
        f"the service (pid {pid}) outlived the gate that was killed"
    )
    reaped = result.evidence.get("spawned_processes_reaped")
    assert isinstance(reaped, list) and pid in [
        int(entry["pid"]) for entry in reaped
    ], f"the runner did not record giving the service back: {reaped}"


def test_a_gate_that_finished_leaves_no_live_service(tmp_path):
    """The same, for a gate that ended on its own.

    A gate that ends normally has services behind it too, and they
    have to go with it: the next run starts on a machine that is
    already carrying the previous run's services.
    """
    gate = tmp_path / "gate.py"
    gate.write_text(
        _HANGING_GATE_SOURCE.format(backend_dir=str(BACKEND_DIR)).replace(
            "time.sleep(600)", "time.sleep(0.2)",
        ),
        encoding="utf-8",
    )

    result = run_ci_verification(
        {
            "id": "VP-016",
            "verification_method": "full_ci",
            "verification_phase": 2,
            "ci_entry": f"{VENV_PYTHON} {gate}",
            "ci_timeout_seconds": 60,
        },
        project_dir=tmp_path,
    )

    pid = _service_pid(result.evidence.get("stdout_tail", ""))
    assert pid is not None, (
        "the gate never said which service it started:\n"
        + str(result.evidence.get("stdout_tail"))
    )
    assert _alive(pid) is False, (
        f"the service (pid {pid}) outlived the gate that finished"
    )


# --------------------------------------------------------------------------
# The gate's own way out
# --------------------------------------------------------------------------


#: A stand-in for pytest: it starts a service, writes it down, and
#: exits.  The service is detached, exactly as a declared service is.
_STUB_SOURCE = '''#!/usr/bin/env python3
import subprocess
import sys

sys.path.insert(0, {backend_dir!r})

import orphan_reaper

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    start_new_session=True,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
orphan_reaper.register_spawned_process(
    child.pid, pgid=child.pid, kind=orphan_reaper.KIND_SERVICE,
    name="backend.server",
)
with open({pid_file!r}, "w", encoding="utf-8") as handle:
    handle.write(str(child.pid))
'''


def test_the_gate_gives_back_what_it_started(tmp_path):
    """The gate's own way out is a cleanup path too.

    The gate is the process that starts the services, so the gate is
    the process that has to give them back.  A gate that ends without
    doing so leaves the machine carrying its services into the next
    run -- which is how one run's leftovers make the next run slower.
    """
    registry = tmp_path / "spawn_registry.jsonl"
    pid_file = tmp_path / "service.pid"
    stub = tmp_path / "fake_pytest.py"
    stub.write_text(
        _STUB_SOURCE.format(
            backend_dir=str(BACKEND_DIR), pid_file=str(pid_file),
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)

    env = dict(os.environ)
    env.pop("PDT_RUN_TESTS_DRY_RUN", None)
    env["PYTEST_BIN"] = str(stub)
    env[reaper.SPAWN_REGISTRY_ENV] = str(registry)

    completed = subprocess.run(
        ["bash", str(GATE_SCRIPT), "--lane", "unit"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
        timeout=300,
    )

    assert completed.returncode == 0, (
        f"the gate exited {completed.returncode}:\n"
        + completed.stdout + completed.stderr
    )
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    assert _alive(pid) is False, (
        f"the gate left the service it started running (pid {pid})"
    )
