"""Regression: a declared service a case starts has to be given back.

Why this file exists
--------------------
``tests/unit/test_verification_service_freshness.py`` starts a **real**
``backend.server`` -- through the preflight, the way the round starts a
declared service -- and used to leave it running: the case's own
docstring said the service "is left running".

A case that starts a service has to stop it.  The rules this file pins
are the ones the repository holds every other test to:

  * a test reclaims every resource it started, **before it returns**;
  * the reclaim is identified by the **pid the preflight handed back**,
    not by the declaration's command line -- the declared command of the
    pdt service is ``exec python -m backend.server``, which names no
    script for a token match to find, so the leftover sweep cannot see
    it and the service outlives the suite;
  * a reclaim that finds nothing left to stop is not an error: the
    second reclaim of an already-released service is a no-op, not a
    failure;
  * a service the preflight **adopted** -- a live, fresh, matching
    listener that was already there -- is not this file's to stop: its
    pid belongs to a server nobody here started.

The case under test is the one in the freshness module, and it is
**called directly** here -- with the declaration pinned -- so what runs
is the real case and not a copy of it.

The module the case lives in is imported under the name pytest gives it
(``backend.tests.unit.test_verification_service_freshness``), so this
file and the freshness module share **one** module object: a second
copy would be a second set of module-level names, and pinning one
would not pin the other.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from backend.tests.unit import (  # noqa: E402
    test_verification_service_freshness as target,
)


#: The tiny service the reclaim case starts: a real process on a real
#: port, but a small one.  The point of that case is the *reclaim*,
#: not the service behind it -- and a small service keeps the case
#: about what it is about.
_TINY_SERVICE = _BACKEND_DIR / "tests" / "manual" / "tiny_service.py"


def _pdt_declaration():
    """The pdt declaration, mirrored around a free port.

    The same declaration the target case uses when the plan under test
    is not on this machine.  A written-down port is a port another
    checkout may already hold, and the case would then fail for a
    reason that has nothing to do with what it is about.
    """
    from service_declaration import parse_declarations

    mirror = target._pdt_service_mirror()
    return parse_declarations(
        {"services": [mirror]}, target._REPO_ROOT,
    ).by_name()["pdt"]


def _tiny_declaration():
    """A declaration for the tiny service, on a free port."""
    from service_declaration import ServiceDeclaration

    import service_manager as sm

    port = sm.allocate_free_port()
    assert port is not None, "no free port for the reclaim case"
    return ServiceDeclaration(
        name="reclaim-probe",
        port=port,
        start_cmd=(
            f"{shlex.quote(sys.executable)} "
            f"{shlex.quote(str(_TINY_SERVICE))} --port {port}"
        ),
        cwd=".",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=30,
    )


@pytest.mark.integration
def test_target_case_reclaims_its_service(tmp_path: Path, monkeypatch):
    """The target case must give the service back before it returns.

    The case is called directly, with the declaration pinned to one
    this file owns, so the case that runs is the real one: the real
    preflight, the real spawn, the real assertions.

    The port is the witness.  It was held by the service the case
    started, and the case is not allowed to return while it still is.
    """
    import service_manager as sm

    declaration = _pdt_declaration()
    monkeypatch.setattr(
        target, "_pdt_declaration", lambda project_dir: declaration,
    )

    target.test_declared_pdt_service_ready_in_harness_runtime(tmp_path)

    probe = sm.probe_port(declaration.port)
    assert probe.listening is False, (
        f"the case returned with port {declaration.port} still held by "
        f"{probe.pids}; a case that starts a service has to stop it"
    )


@pytest.mark.integration
def test_an_adopted_service_is_not_stopped():
    """A service this round did not start is not this round's to stop.

    The preflight adopts a live, fresh, matching listener instead of
    starting one -- that is how the round avoids taking down a service
    somebody else is serving.  The pid in an adopted runtime is *that*
    service's pid, so a reclaim that stops what the pid names stops a
    server this case never started.

    The process here is a real one, and the witness is that it is still
    running when the reclaim returns.
    """
    import service_manager as sm

    declaration = _tiny_declaration()
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        runtime = sm.ServiceRuntime(
            name=declaration.name,
            port=declaration.port,
            declared_port=declaration.port,
            pid=sleeper.pid,
            origin="adopted",
            ready=True,
        )

        result = target._reclaim_service(
            runtime, declaration,
            caller="test_an_adopted_service_is_not_stopped",
        )

        assert result is None, (
            f"the reclaim acted on an adopted service: {result}"
        )
        assert sleeper.poll() is None, (
            "the reclaim stopped a service the round had adopted -- "
            "that service was not this round's to stop"
        )
    finally:
        sleeper.terminate()
        try:
            sleeper.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - cleanup only
            sleeper.kill()


@pytest.mark.integration
def test_reclaim_is_idempotent():
    """Reclaiming a service that is already gone must not raise.

    A reclaim runs in a ``finally``, so it runs whether the case passed
    or failed -- and it can run twice over the same service: once by
    the case, once by the sweep that finds what the case left behind.
    The second reclaim has nothing to stop, and that is not an error:
    it is the shape every other cleanup in this repository has.
    """
    import service_manager as sm

    declaration = _tiny_declaration()
    runtime = sm.start_service(declaration, _BACKEND_DIR)
    assert runtime.ready is True, runtime.detail

    first = target._reclaim_service(
        runtime, declaration, caller="test_reclaim_is_idempotent",
    )
    assert first is not None
    assert first["port_free"] is True, first

    # The port is free now, and the service is gone.  Reclaiming it a
    # second time must find nothing to do and must not raise.
    second = target._reclaim_service(
        runtime, declaration, caller="test_reclaim_is_idempotent",
    )
    assert second is not None
    assert second["port_free"] is True, second

    assert sm.probe_port(declaration.port).listening is False


def test_freshness_module_regression():
    """The two files belong to one pytest session, and both collect.

    The reclaim is only worth anything if the case it protects still
    runs beside it: two files that cannot be collected into one
    session are two files that never run together, and the pair the
    suite ships is the pair the command has to be able to run.

    What this proves is the *pairing* -- both modules collect into one
    session, and the cases the contract names are in it.  That both
    files are **green** in one run is what the suite's own command
    does (``pytest tests/unit/test_declared_service_reclamation.py
    tests/unit/test_verification_service_freshness.py``); a test cannot
    assert on the session it is running in, so it asserts on the part
    of the claim it *can* reach.
    """
    freshness = (
        _BACKEND_DIR / "tests" / "unit" / "test_verification_service_freshness.py"
    )
    reclamation = Path(__file__).resolve()

    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            str(freshness), str(reclamation),
            "--collect-only", "-q", "-p", "no:cacheprovider",
        ],
        cwd=str(_BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert completed.returncode == 0, (
        f"the two modules do not collect into one session:\n"
        f"{completed.stdout[-4000:]}\n{completed.stderr[-2000:]}"
    )
    collected = completed.stdout
    for node_id in (
        "test_target_case_reclaims_its_service",
        "test_reclaim_is_idempotent",
        "test_freshness_module_regression",
        "test_declared_pdt_service_ready_in_harness_runtime",
    ):
        assert node_id in collected, (
            f"{node_id} is not in the collected set:\n{collected[-4000:]}"
        )


# ---------------------------------------------------------------------------
# The ledger's own record of the holder (2026-11-20)
# ---------------------------------------------------------------------------
#
# A declaration's ``start_cmd`` is a **shell line** — activate a
# virtualenv, export the data plane, seed the fixture, run the program
# — and the process it starts carries only the program.  The ledger
# therefore records the **wrapper** it spawned, while the port is held
# by the program the wrapper ran:
#
#     ledger:  pid 9967, pgid 9967        <- the wrapper
#     holder:  pid 9972, group 9967       <- the program it ran
#
# The reap path has always known how to see that shape (the listener is
# a **descendant** of the recorded process, in the recorded group), and
# a case pins it.  The **ensure** path did not: it asked for the
# recorded pid to be the listener, exactly, and when it was not the
# answer was "not ours" — so the round relocated around its own
# leftover, the reclaim it did attempt had no identity anchor, and the
# stop was refused.  The port stayed held.
#
# The two proofs below are the ledger's **own** words, not a guess:
#
#   * the **data plane** the ledger recorded for the service names the
#     process that answered on that port;
#   * the **process group** the ledger recorded is the group the
#     listener is in — a child inherits its parent's group.
#
# A command line that "looks like ours" is not one of them and is not
# accepted: a process that merely resembles ours is somebody else's.


def _ledger_entry(**over) -> dict:
    """One service entry, in the shape the ledger writes."""
    base = {
        "name": "api",
        "port": 8917,
        "declared_port": 8917,
        "pid": 9967,
        "pgid": 9967,
        "start_epoch": 1795118192.383248,
        "start_cmd": "source backend/.venv/bin/activate && python -m backend.server",
        "cwd": ".",
        "log_path": "",
        "origin": "spawned",
        "ready": True,
        "reap_on_exit": True,
        "detail": "",
        "owner_token": "",
        "reclaimed": False,
        "reclaim_reason": "",
        "unusable": False,
        "failure_reason": "",
        "dead": False,
        "exit": None,
        "data_plane": None,
    }
    base.update(over)
    return base


def _write_plan_ledger(plan_dir: Path, entries: list, project_dir: Path = None):
    """The plan's ledger, in the shape the reader understands."""
    import service_manager as sm

    plan_dir.mkdir(parents=True, exist_ok=True)
    payload = {"plan_id": plan_dir.name, "services": entries}
    if project_dir is not None:
        payload["project_dir"] = str(project_dir)
    sm.ledger_path(plan_dir).write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _api_declaration(port: int = 8917):
    from service_declaration import ServiceDeclaration

    return ServiceDeclaration(
        name="api",
        port=port,
        start_cmd=(
            "source backend/.venv/bin/activate && "
            "python -m backend.server"
        ),
        cwd=".",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=30,
    )


def test_ledger_holder_evidence_recognises_the_data_plane_listener(
    tmp_path: Path, monkeypatch,
):
    """The ledger's data-plane record names the process that answers.

    The ledger recorded two pids for one service: the wrapper it
    spawned (9967) and, in the data plane it wrote down, the process
    that answered on the port (9972).  The port is held by the second
    one, and the entry has to say so — otherwise the round cannot tell
    its own leftover from a stranger, and the stranger is what it gets
    out of the way for.

    What the evidence carries matters as much as that it exists: the
    **pid** has to be the one that will be signalled, the **group** is
    the one the ledger recorded (never one read off the process table),
    and the **anchor** is the start time of the process being
    signalled — the ledger never recorded this pid, so the anchor has
    to come from the live process.
    """
    import service_manager as sm
    from service_freshness import PortProbe

    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_plan_ledger(
        plan_dir,
        [_ledger_entry(data_plane={
            "status": "ok",
            "plan_routing_rows": 1,
            "boot_id": "222db6ca95fe42b7843639fcb128558c",
            "state_db_path": str(project_dir / "state.db"),
            "pid": 9972,
            "pgid": 9967,
            "verdict": "proved",
            "error": "",
        })],
        project_dir=project_dir,
    )

    decl = _api_declaration()
    probe = PortProbe(
        port=decl.port,
        listening=True,
        pids=[9972],
        cmdlines=["/usr/bin/python3 -m backend.server"],
        process_start_epoch=1795118193.5,
    )
    monkeypatch.setattr(
        sm, "_process_group_of", lambda pid: 9967 if pid == 9972 else pid,
    )
    # The anchor is measured on the process that is going to be
    # signalled — not read off the port-wide probe, which is the
    # earliest start among *all* the listeners.
    monkeypatch.setattr(
        sm, "_process_start_epoch",
        lambda pid: 1795118193.75 if pid == 9972 else None,
    )

    evidence = sm._ledger_holder_evidence(plan_dir, decl, probe)

    assert evidence is not None, (
        "the ledger's own data-plane record names the holder, and the "
        "entry was not read as ours"
    )
    assert evidence["pid"] == 9972, evidence
    assert evidence["pgid"] == 9967, evidence
    assert evidence["start_epoch"] == 1795118193.75, (
        f"the anchor has to be the start time of the process being "
        f"signalled; got {evidence['start_epoch']!r}"
    )
    assert evidence["start_epoch"] != 1795118192.383248, (
        "the anchor is the recorded wrapper's start time, and the wrapper "
        "is not the process that would be signalled"
    )

    # And the negative control: a holder the ledger says nothing about
    # is not ours, however much its command line resembles ours.
    stranger = PortProbe(
        port=decl.port,
        listening=True,
        pids=[4242],
        cmdlines=["/usr/bin/python3 -m backend.server"],
        process_start_epoch=1795119000.0,
    )
    monkeypatch.setattr(
        sm, "_process_group_of", lambda pid: 9999 if pid == 4242 else pid,
    )
    assert sm._ledger_holder_evidence(plan_dir, decl, stranger) is None, (
        "a holder the ledger records nothing about was taken for ours"
    )


def test_ledger_holder_evidence_recognises_a_descendant_in_the_recorded_group(
    tmp_path: Path, monkeypatch,
):
    """The listener is a descendant of the recorded process.

    This is the ``next dev`` shape the reap path already answers: the
    ledger records the process it spawned, the port is held by a
    **child** of it, and the child inherits the group the parent
    created.  The group is what connects the two — the command line of
    the child matches nothing at all, because such a process rewrites
    its own title.

    The same rule has to hold on the ensure path, or the round
    relocates around its own leftover and the port it declared stays
    held by the process it started.
    """
    import service_manager as sm
    from service_freshness import PortProbe

    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_plan_ledger(
        plan_dir,
        [_ledger_entry(
            name="chart", port=3000, declared_port=3000,
            pid=4242, pgid=4242, start_epoch=1.0,
            start_cmd="npx next dev -p 3000",
        )],
        project_dir=project_dir,
    )

    from service_declaration import ServiceDeclaration

    decl = ServiceDeclaration(
        name="chart",
        port=3000,
        start_cmd="npx next dev -p 3000",
        cwd=".",
        health_url="http://127.0.0.1:3000/health",
        ready_timeout_seconds=30,
    )
    probe = PortProbe(
        port=decl.port,
        listening=True,
        pids=[1000],
        cmdlines=["next-server (v15.5.20)"],
        process_start_epoch=42.5,
    )
    monkeypatch.setattr(
        sm, "_process_group_of", lambda pid: 4242 if pid == 1000 else pid,
    )
    monkeypatch.setattr(
        sm, "_process_start_epoch",
        lambda pid: 42.75 if pid == 1000 else None,
    )

    evidence = sm._ledger_holder_evidence(plan_dir, decl, probe)

    assert evidence is not None, (
        "the listener is in the group the ledger recorded, and the entry "
        "was not read as ours"
    )
    assert evidence["pid"] == 1000, evidence
    assert evidence["pgid"] == 4242, evidence
    assert evidence["start_epoch"] == 42.75, (
        f"the anchor has to be the start time of the process being "
        f"signalled; got {evidence['start_epoch']!r}"
    )

    # And the negative control: a holder in another group is a
    # stranger, and the ledger's group does not make it ours.
    monkeypatch.setattr(
        sm, "_process_group_of", lambda pid: 9999 if pid == 1000 else pid,
    )
    assert sm._ledger_holder_evidence(plan_dir, decl, probe) is None, (
        "a holder in another group was taken for ours"
    )
