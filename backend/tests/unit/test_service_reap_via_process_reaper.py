"""The VP reap goes through the one reaper.

Why these tests exist
---------------------
``service_manager.reap_services`` used to carry its own identity and
port rules: its own pid-reuse check, its own declaration matching, its
own ``stop_service`` call. The reaper (``backend/process_reaper.py``)
carries a second copy of the same rules — and two copies of one rule
are two rules that drift apart. The question "may I kill this pid?" has
to have exactly one answer, and it is the reaper's.

So the VP reap delegates: :func:`service_manager.reap_services` asks
:func:`process_reaper.reap_plan` to do the killing, and keeps only the
part the *service view* knows — the operator's ``reap_on_exit: false``
opt-out, and the ports that are still held by a process that is not
ours.

Each test below pins one property of that split:

* the delegation happens, and it is narrowed to the VP services;
* a port held by somebody else is reported and never killed;
* the ledger stops claiming a process that is gone;
* the operator's opt-out survives the sweep, and stays in the ledger;
* one broken plan does not stop the sweep for the others.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import process_identity  # noqa: E402
import process_ledger as ledger  # noqa: E402
import process_reaper as reaper  # noqa: E402
import service_manager as sm  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _spawn(argv, cwd=None):
    """Start a real child process in its own session.

    A detached process group is what a real PDT layer looks like, and the
    reaper refuses to signal the caller's own group — so a test that
    wants the child signalled at all has to detach it.
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


def _wait_for(path, timeout=10.0):
    """Wait until ``path`` exists.

    The fixtures announce themselves through a file rather than through a
    sleep: a sleep is a race with the interpreter's start-up, and a race
    is a flake that shows up on someone else's machine.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def _free_port() -> int:
    """A port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stable_identity(pid, tries=40, delay=0.05):
    """Probe ``pid`` until two consecutive readings agree.

    On macOS the framework launcher rewrites ``argv[0]`` shortly after
    the exec, so a reading taken the instant a process is spawned can
    spell the command line differently from a reading taken a moment
    later. The identity check compares command lines, so the *recorded*
    one has to be the settled one.
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


def _record_service(
    plan_dir: Path,
    proc,
    *,
    name: str = "web",
    port: int = 3000,
    plan_id: str = "p1",
    reap_on_exit: bool = True,
):
    """Record a VP service the way ``record_runtimes`` records one.

    The event carries both halves the readers need: the process record
    (so the reaper can act on it) and the service view (so the callers
    that ask "which services does this plan have?" still get an answer).
    """
    ident = _stable_identity(proc.pid)
    assert ident is not None, "the test child has no readable identity"
    record = ledger.ProcessRecord(
        plan_id=plan_id,
        role="vp_service",
        pid=proc.pid,
        pgid=ident.pgid if ident.pgid else proc.pid,
        create_time=ident.create_time,
        cmdline=list(ident.cmdline),
        cwd=ident.cwd or "",
        owner_pid=os.getpid(),
        owner_role="backend",
    )
    ledger.record_event(
        plan_dir,
        "spawn",
        terminal=False,
        pid=proc.pid,
        role="vp_service",
        plan_id=plan_id,
        project_dir=str(plan_dir),
        round=0,
        record=record.to_dict(),
        services=[{
            "name": name,
            "port": port,
            "declared_port": port,
            "pid": proc.pid,
            "pgid": record.pgid,
            "start_epoch": record.create_time,
            "start_cmd": " ".join(ident.cmdline),
            "cwd": record.cwd,
            "log_path": "",
            "origin": "spawned",
            "ready": True,
            "reap_on_exit": reap_on_exit,
            "detail": "",
        }],
    )
    return record


def _write_whole_file_ledger(plan_dir: Path, plan_id: str, name: str) -> None:
    """Give a plan the retired whole-file ledger.

    That is the shape a plan started before the switch has, and the
    sweep has to find it: a plan whose ledger is invisible is a plan
    whose services are never reaped.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    sm.ledger_path(plan_dir).write_text(
        json.dumps({
            "plan_id": plan_id,
            "services": [{
                "name": name,
                "port": _free_port(),
                "pid": 4242,
                "pgid": 4242,
                "start_epoch": 1.0,
                "start_cmd": "true",
                "cwd": ".",
                "log_path": "",
                "origin": "spawned",
                "ready": True,
                "reap_on_exit": True,
                "detail": "",
            }],
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# the delegation
# ---------------------------------------------------------------------------

def test_reap_services_delegates_to_process_reaper(tmp_path, monkeypatch):
    """The killing is the reaper's job, and it is narrowed to the VP
    services.

    A caller that wants the services must not take the executor down
    with them, so the delegation names the role it covers.
    """
    calls = []

    def _fake_reap_plan(plan_dir, plan_id="", *, reason="", **kwargs):
        calls.append({
            "plan_dir": plan_dir,
            "plan_id": plan_id,
            "reason": reason,
            **kwargs,
        })
        return reaper.ReapOutcome(plan_id=plan_id, reason=reason)

    monkeypatch.setattr(sm, "reap_plan", _fake_reap_plan)

    report = sm.reap_services(tmp_path, "p1", reason="startup_sweep")

    assert len(calls) == 1, "the reap happened somewhere else"
    call = calls[0]
    assert Path(call["plan_dir"]) == tmp_path
    assert call["plan_id"] == "p1"
    assert call["reason"] == "startup_sweep"
    assert call["roles"] == ("vp_service",)
    assert report.plan_id == "p1"
    assert report.reason == "startup_sweep"


# ---------------------------------------------------------------------------
# a port held by somebody else
# ---------------------------------------------------------------------------

def test_foreign_port_holder_is_reported_not_killed(tmp_path):
    """A port held by a process that is not ours is reported, never killed.

    The holder is a stranger: it was never in this plan's ledger, so
    nothing vouches for it. Killing it would be the orphan sweep
    murdering a bystander, and the only thing done with the finding is
    to report it — a stranger is worse than an orphan.
    """
    portfile = tmp_path / "port"
    listener_code = (
        "import socket, time\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', 0))\n"
        "s.listen(1)\n"
        "open({portfile!r}, 'w').write(str(s.getsockname()[1]))\n"
        "time.sleep(60)\n"
    ).format(portfile=str(portfile))
    listener = _spawn([sys.executable, "-c", listener_code], cwd=tmp_path)
    service = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        assert _wait_for(portfile), "the fixture never reported its port"
        port = int(portfile.read_text().strip())
        _record_service(tmp_path, service, name="web", port=port)

        report = sm.reap_services(
            tmp_path, "p1", reason="startup_sweep", grace_seconds=2.0,
        )

        # Our own service is gone...
        assert service.poll() is not None
        # ... and the stranger holding the port is still running.
        assert listener.poll() is None
        assert report.foreign_ports == [port]
        assert not report.clean
    finally:
        _cleanup(listener, service)


# ---------------------------------------------------------------------------
# the ledger after the sweep
# ---------------------------------------------------------------------------

def test_finalise_appends_exit_event(tmp_path):
    """A service that is gone leaves the ledger.

    The ledger is what a later sweep reads to decide what is still
    running. A service that has died but is still listed would be
    collected again on every sweep, and — worse — a sweep that trusts
    the list would go on signalling a pid that has been recycled.
    """
    service = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record_service(tmp_path, service, name="web", port=_free_port())

        report = sm.reap_services(
            tmp_path, "p1", reason="startup_sweep", grace_seconds=2.0,
        )

        assert report.reaped == ["web"], report.to_dict()
        assert service.poll() is not None

        # The ledger records the death...
        deaths = [
            event for event in ledger.read_events(tmp_path)
            if event.get("event") == "exit" and event.get("pid") == service.pid
        ]
        assert deaths, "the ledger still claims a process that is gone"

        # ... and the service view is empty: nothing of this plan is up.
        assert sm.read_ledger(tmp_path) == {}
        assert ledger.live_records(tmp_path) == {}
    finally:
        _cleanup(service)


# ---------------------------------------------------------------------------
# the operator's opt-out
# ---------------------------------------------------------------------------

def test_reap_on_exit_false_entry_survives(tmp_path):
    """``reap_on_exit: false`` is the operator saying "leave this one".

    The service is not signalled, and the ledger keeps saying that it
    is still there — an opt-out that was forgotten by the next sweep
    would be an opt-out that only worked once.
    """
    service = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record_service(
            tmp_path, service, name="web", port=_free_port(),
            reap_on_exit=False,
        )

        report = sm.reap_services(
            tmp_path, "p1", reason="startup_sweep", grace_seconds=2.0,
        )

        # It was not signalled.
        assert service.poll() is None, "the opt-out was killed anyway"

        # The ledger still knows about it.
        view = sm.read_ledger(tmp_path)
        assert [entry["name"] for entry in view["services"]] == ["web"]
        assert view["services"][0]["reap_on_exit"] is False

        # And the sweep says why it left it alone.
        assert "web" not in report.reaped
        reasons = [
            str(entry.get("reason", "")) for entry in report.skipped
        ]
        assert any("reap_on_exit" in reason for reason in reasons), reasons
    finally:
        _cleanup(service)


# ---------------------------------------------------------------------------
# the sweep over every plan
# ---------------------------------------------------------------------------

def test_reap_all_plans_continues_after_one_plan_fails(tmp_path, monkeypatch,
                                                        caplog):
    """One broken plan must not cost the other plans their sweep.

    The tolerance lives here and only here: a plan that raises is
    logged and skipped, and the sweep carries on with the next one.
    """
    plans_dir = tmp_path / "plans"
    _write_whole_file_ledger(plans_dir / "p1", "p1", "web")
    _write_whole_file_ledger(plans_dir / "p2", "p2", "api")

    calls = []

    def _fake_reap_services(plan_dir, plan_id="", **kwargs):
        calls.append(Path(plan_dir).name)
        if Path(plan_dir).name == "p1":
            raise RuntimeError("this plan is broken")
        return sm.ReapReport(
            plan_id=plan_id or Path(plan_dir).name,
            reason=kwargs.get("reason", ""),
        )

    monkeypatch.setattr(sm, "reap_services", _fake_reap_services)

    reports = sm.reap_all_plans(plans_dir, reason="server_shutdown")

    assert calls == ["p1", "p2"], "the second plan was never reached"
    assert [report.plan_id for report in reports] == ["p2"]
    assert any(
        "reap failed" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_reap_all_plans_with_roles_none_covers_the_whole_tree(tmp_path):
    """``roles=None`` means every role — the tree, not just the services.

    The backend's shutdown sweep has to take the executor and the
    sub-agents with it, and it must not need a second way of walking
    the plan directories to do it.
    """
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / "p1"
    plan_dir.mkdir(parents=True)

    service = _spawn(["sleep", "30"], cwd=tmp_path)
    subagent = _spawn(["sleep", "30"], cwd=tmp_path)
    try:
        _record_service(plan_dir, service, name="web", port=_free_port())
        ident = _stable_identity(subagent.pid)
        ledger.record_spawn(plan_dir, ledger.ProcessRecord(
            plan_id="p1",
            role="subagent",
            pid=subagent.pid,
            pgid=ident.pgid if ident and ident.pgid else subagent.pid,
            create_time=ident.create_time if ident else 0.0,
            cmdline=list(ident.cmdline) if ident else (),
            cwd=ident.cwd if ident else "",
            owner_pid=os.getpid(),
            owner_role="backend",
        ))

        reports = sm.reap_all_plans(
            plans_dir, reason="server_shutdown", grace_seconds=2.0,
        )

        assert len(reports) == 1
        # Both layers are gone: the whole tree, not just the services.
        assert service.poll() is not None
        assert subagent.poll() is not None, (
            "roles=None must cover the sub-agents too"
        )
    finally:
        _cleanup(service, subagent)
