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
import time
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


def _wait_until_dead(pid: Optional[int], timeout: float = 10.0) -> bool:
    """Wait, bounded, for *pid* to be gone and say whether it is.

    The reap escalates TERM to KILL and then waits a short, fixed
    moment for the process to leave.  On a loaded runner that moment
    can expire before the kernel has finished tearing the process
    down, and the very next instant is not enough to read the answer.

    The contract these cases assert is that the service does **not
    outlive** the run that started it — not that it is gone within the
    same instant the runner returns.  So the wait here is bounded and
    the assertion is still "it is gone": a service that genuinely
    survives is still caught, it is just given the same few seconds a
    person watching the machine would give it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


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
    assert _wait_until_dead(pid), (
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
    assert _wait_until_dead(pid), (
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


# --------------------------------------------------------------------------
# Who sent the signal
# --------------------------------------------------------------------------
#
# ``Terminated: 15`` says that something was killed and not what.  The
# gate's way out is a place where that question is asked: the gate gives
# back what it started, and the processes it gives back are the ones it
# started -- and nothing else.
#
# So the gate's way out has to be able to say two things:
#
# * which signals it sent, and to whom; and
# * that the process it did *not* signal was left alone on purpose, and
#   not by accident.
#
# The second one is the harder half.  "The process is still running" is
# what a signal that missed looks like as well as what a signal that was
# never sent looks like, so the liveness alone cannot tell the two apart.
# The audit can: it says what was signalled, so "no signal was sent" and
# "a signal was sent and did not land" are two different readings of the
# same file.


#: The fields a kill record carries.  The audit the service manager
#: writes and the audit the reaper writes are read together, so the two
#: have to carry the same fields.
#:
#: The record the audit hook writes is the one that says a signal was
#: *about* to be sent; the sender writes the outcome of that attempt
#: next to it, and that second record adds the errno the kernel
#: answered with.  Both shapes are the audit's contract (see
#: ``kill_audit.write_kill_audit`` / ``kill_audit.write_kill_outcome``),
#: so both are pinned here.
AUDIT_RECORD_FIELDS = {
    "kind", "target", "signal", "outcome", "pid", "ts", "stack",
}
AUDIT_OUTCOME_RECORD_FIELDS = AUDIT_RECORD_FIELDS | {"errno"}


def _audit_files(directory: Path) -> List[Path]:
    """The audit files under *directory*."""
    return sorted(directory.glob("kill-audit-*.jsonl"))


def _records_in(path: Path) -> List[dict]:
    """The kill records in one audit file."""
    records: List[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _audit_records(directory: Path) -> List[dict]:
    """Every kill record under *directory*."""
    records: List[dict] = []
    for path in _audit_files(directory):
        records.extend(_records_in(path))
    return records


def _signals_sent(records: List[dict]) -> List[dict]:
    """The records that name a signal that was actually delivered.

    A liveness probe is ``kill(pid, 0)``: it asks whether a process is
    there and it delivers nothing.  It is in the audit because it is a
    call to ``os.kill``, and it is not a signal -- so the records that
    count as "a signal was sent" are the ones whose signal is not zero.
    """
    return [r for r in records if int(r.get("signal") or 0) != 0]


def test_the_gate_gives_back_its_own_service_and_nobody_elses(
    tmp_path, monkeypatch,
):
    """The gate's way out takes its own processes and nobody else's.

    The shape the production run hit: the registry carries an entry that
    belongs to a **live** owner -- another run's service -- and an entry
    that belongs to this run.  The gate gives its own back and leaves the
    other one alone, because the owner of that one is still there and may
    still want it.

    "Leaves it alone" is not the same as "did not kill it by accident": a
    signal that was sent and missed looks exactly like a signal that was
    never sent, judged by liveness alone.  So the assertion is made
    twice -- once on the audit, which says what was signalled, and once on
    the recorded calls, which say what the reaper decided.
    """
    registry = tmp_path / "spawn_registry.jsonl"
    audit_dir = tmp_path / "kill-audit"
    pid_file = tmp_path / "service.pid"
    stub = tmp_path / "fake_pytest.py"
    stub.write_text(
        _STUB_SOURCE.format(
            backend_dir=str(BACKEND_DIR), pid_file=str(pid_file),
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)

    # The owner of the foreign entry: a process of its own, and alive.
    foreign_owner = _spawn_detached()
    # The process the foreign entry names.
    foreign_service = _spawn_detached()
    try:
        # ---- the registry the gate is going to sweep ------------------
        reaper.register_spawned_process(
            foreign_service.pid, pgid=foreign_service.pid,
            kind=reaper.KIND_SERVICE, name="another run's service",
            path=registry,
        )
        entries = reaper.read_spawn_registry(registry)
        # The entry belongs to another run: the process that wrote it
        # down is that run's owner, and that owner is still alive.
        entries[0]["owner_pid"] = foreign_owner.pid
        registry.write_text(
            json.dumps(entries[0], ensure_ascii=False) + "\n", encoding="utf-8",
        )

        # ---- the gate's own way out ------------------------------------
        env = dict(os.environ)
        env.pop("PDT_RUN_TESTS_DRY_RUN", None)
        env["PYTEST_BIN"] = str(stub)
        env[reaper.SPAWN_REGISTRY_ENV] = str(registry)
        env["PDT_KILL_AUDIT_DIR"] = str(audit_dir)

        completed = subprocess.run(
            ["bash", str(GATE_SCRIPT), "--lane", "unit"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
            timeout=300,
        )
        assert completed.returncode == 0, (
            f"the gate exited {completed.returncode}:\n"
            + completed.stdout + completed.stderr
        )
        own_pid = int(pid_file.read_text(encoding="utf-8").strip())

        # ---- what the gate did ----------------------------------------
        assert _alive(own_pid) is False, (
            f"the gate left the service it started running (pid {own_pid})"
        )
        assert _alive(foreign_service.pid) is True, (
            "the gate took another run's service: its owner (pid "
            f"{foreign_owner.pid}) was still alive, so the entry was not "
            f"the gate's to give back (pid {foreign_service.pid})"
        )

        # ---- what the gate sent ---------------------------------------
        records = _audit_records(audit_dir)
        signals = _signals_sent(records)
        assert signals, (
            "the gate sent a signal and wrote nothing down about it; the "
            f"records under {audit_dir} were: {records!r}"
        )
        assert any(int(r.get("target") or 0) == own_pid for r in signals), (
            "the gate did not record a signal for the service it gave "
            f"back (pid {own_pid}); the records were: {records!r}"
        )
        assert not any(
            int(r.get("target") or 0) == foreign_service.pid for r in signals
        ), (
            "the gate sent a signal to another run's service; the records "
            f"were: {records!r}"
        )

        # ---- the shape of the records ---------------------------------
        # The two audits are read together, so they have to look alike.
        for record in records:
            fields = set(record)
            assert fields in (
                AUDIT_RECORD_FIELDS, AUDIT_OUTCOME_RECORD_FIELDS,
            ), (
                "a kill record does not carry the fields the audit is "
                f"read by: {record!r}"
            )
            assert record.get("kind") in ("kill", "killpg"), (
                f"a kill record does not say what it did: {record!r}"
            )
            assert record.get("outcome"), (
                f"a kill record does not say what became of the signal: "
                f"{record!r}"
            )
        for path in _audit_files(audit_dir):
            named = int(path.stem[len("kill-audit-"):])
            for record in _records_in(path):
                assert int(record.get("pid") or 0) == named, (
                    f"the record in {path.name} names pid "
                    f"{record.get('pid')}, and the file is named after {named}"
                )

        # ---- the decision, with the reaper's own injection points ------
        # The same shape, one level down.  Here the signals are recorded
        # instead of being sent, so "no signal was sent" and "a signal
        # was sent and missed" are not guessed at: they are the recorded
        # calls, and they are told apart.
        calls = _record_signals(monkeypatch)
        decision_registry = tmp_path / "decision_registry.jsonl"
        decision_registry.write_text(
            json.dumps(
                {
                    "pid": 424242, "pgid": None, "kind": "service",
                    "name": "another run's service",
                    "owner_pid": foreign_owner.pid, "start_epoch": "100",
                },
                ensure_ascii=False,
            ) + "\n" + json.dumps(
                {
                    "pid": 424243, "pgid": None, "kind": "service",
                    "name": "this run's service",
                    "owner_pid": 424299, "start_epoch": "100",
                },
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )

        reaper.reap_spawned_processes(
            decision_registry,
            reason="the gate is over",
            grace_seconds=0.1,
            probe=lambda pid: "100",
            alive=lambda pid: True,
            owner_alive=lambda pid: pid == foreign_owner.pid,
        )

        sent = {int(call[1]) for call in calls if int(call[2]) != 0}
        assert 424242 not in sent, (
            "the reaper signalled a process whose owner was still there: "
            f"{calls!r}"
        )
        assert 424243 in sent, (
            "the reaper did not signal the process that was its own to "
            f"give back: {calls!r}"
        )
    finally:
        # The signals are recorded, not sent: the real kill has to come
        # back before the fixtures are cleaned up.
        monkeypatch.undo()
        _kill(foreign_service)
        _kill(foreign_owner)
