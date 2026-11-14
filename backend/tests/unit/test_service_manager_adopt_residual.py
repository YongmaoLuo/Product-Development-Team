"""Unit tests: adopting this plan's **own** leftover instance (2026-11-13).

The incident (2026-11-13): a round that had to relocate its service
left the instance it started behind on the free port it moved to.  That
instance still holds the plan's data-root lock, so the next attempt to
start a service on the same root is refused with rc=3 — and rule 4,
which answers "the declared port is held by a process we cannot
identify", used to answer it by **starting a copy** on that same root.

:func:`service_manager.own_service_instances` finds the leftover (the
finding half, 2026-11-13).  This file covers the other half: when that
instance is alive, it is **adopted** — the round is pointed at it, no
second server is started on the same data root, and the stranger that
holds the declared port is never signalled.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402
from service_freshness import PortProbe  # noqa: E402


#: The port the plan declared.
DECLARED_PORT = 3000
#: The port the earlier round had to move to when the declared one was taken.
MOVED_PORT = 38921
#: The port the relocation of the fallback path tries.
FALLBACK_PORT = 40123
#: The pid of the instance this plan started.
PID = 4242
#: The pid of the stranger that holds the declared port.
FOREIGN_PID = 5150
#: The command the declaration starts, on the declared port.
START_CMD = "npx next dev -p 3000"
#: The command line our own instance carries (it runs on the moved port).
OUR_CMDLINE = "npx next dev -p 38921"
#: The command line of the stranger that holds the declared port.
FOREIGN_CMDLINE = "node /elsewhere/server.js"


def _decl(**overrides) -> ServiceDeclaration:
    """The declaration of the ``api`` service."""
    base = dict(name="api", port=DECLARED_PORT, start_cmd=START_CMD, cwd=".")
    base.update(overrides)
    return ServiceDeclaration(**base)


def _candidate(**overrides) -> Dict[str, Any]:
    """What :func:`service_manager.own_service_instances` finds."""
    base = {
        "name": "api",
        "pid": PID,
        "port": MOVED_PORT,
        "declared_port": DECLARED_PORT,
        "source": "pin",
        "start_epoch": 1760000000.0,
        "owner_token": "",
    }
    base.update(overrides)
    return base


def _probe(
    port: int, pid: int, cmdline: str, *, listening: bool = True,
) -> PortProbe:
    """A probe that says "somebody is listening on *port*"."""
    return PortProbe(
        port=port, listening=listening, pids=[pid], cmdlines=[cmdline],
    )


def _world(
    tmp_path: Path,
) -> tuple:
    """A project and the plan directory that owns the leftover."""
    project = tmp_path / "project"
    project.mkdir()
    plan_dir = tmp_path / "plans" / "20261113-framework-base-drift"
    plan_dir.mkdir(parents=True)
    return project, plan_dir


def _install(
    monkeypatch,
    *,
    candidates: bool = True,
    moved_listening: bool = True,
    moved_cmdline: str = OUR_CMDLINE,
) -> tuple:
    """Say what the world looks like, and count what the round does to it.

    * the declared port is held by a **stranger**;
    * this plan's own instance sits on ``MOVED_PORT``;
    * every start and every stop is counted, so the tests can say that
      neither happened.

    Returns ``(stopped, started)`` — the runtimes that were signalled and
    the ports this round's own copy was started on.
    """
    stopped: List[Any] = []
    started: List[int] = []

    def _fake_start(decl, project_dir, *args, **kwargs):
        started.append(decl.port)
        return sm.ServiceRuntime(
            name=decl.name, port=decl.port, pid=5151, pgid=5151,
            start_cmd=decl.start_cmd, cwd=decl.cwd,
            origin="spawned", ready=True, detail="stub",
        )

    def _fake_stop(*args, **kwargs):
        stopped.append(args[0] if args else kwargs.get("runtime"))
        return {"port_free": True, "detail": "stub"}

    def _fake_own_instances(plan_dir, decl, project_dir, *, plan_id=""):
        return [_candidate()] if candidates else []

    def _fake_probe(port):
        if port == DECLARED_PORT:
            return _probe(port, FOREIGN_PID, FOREIGN_CMDLINE)
        if port == MOVED_PORT:
            return _probe(
                port, PID, moved_cmdline, listening=moved_listening,
            )
        return PortProbe(port=port, listening=False)

    monkeypatch.setattr(sm, "start_service", _fake_start)
    monkeypatch.setattr(sm, "stop_service", _fake_stop)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: FALLBACK_PORT)
    monkeypatch.setattr(sm, "own_service_instances", _fake_own_instances)
    monkeypatch.setattr(sm, "probe_port", _fake_probe)
    return stopped, started


# ---------------------------------------------------------------------------
# The adoption
# ---------------------------------------------------------------------------


def test_own_instance_is_adopted(tmp_path, monkeypatch):
    """Our own instance, alive on 38921, is the service the round wants.

    The declared port belongs to a stranger, so rule 4 used to answer
    by starting a *second* copy of the service.  But this plan already
    has a live instance of the very same service — on the port it moved
    to — and that instance is the one the round should talk to.
    """
    project, plan_dir = _world(tmp_path)
    _install(monkeypatch)

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    runtime = result.runtimes["api"]
    assert runtime.port == MOVED_PORT, (
        f"the round must be pointed at our own instance; got {runtime!r}"
    )
    assert runtime.origin == "adopted", (
        f"an adopted instance says so; got {runtime.origin!r}"
    )
    assert runtime.ready, "the instance is live — the service is ready"
    assert runtime.pid == PID


def test_no_second_server_on_the_same_data_root(tmp_path, monkeypatch):
    """Nothing is started: the instance we adopted is already running.

    A second server on the same data root is exactly what the instance
    lock refuses (rc=3), and starting one is what made the 2026-11-13
    round fail.
    """
    project, plan_dir = _world(tmp_path)
    _stopped, started = _install(monkeypatch)

    sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert started == [], (
        f"no second server may be started on the same data root; "
        f"started {started!r}"
    )


def test_foreign_holder_is_not_signalled(tmp_path, monkeypatch):
    """The stranger on the declared port is left alone.

    Rule 4 exists so that a process we cannot identify is never killed;
    adopting our own instance must not change that.
    """
    project, plan_dir = _world(tmp_path)
    stopped, _started = _install(monkeypatch)

    sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert stopped == [], (
        f"a stranger must never be signalled; stopped {stopped!r}"
    )


def test_declared_port_is_still_recorded(tmp_path, monkeypatch):
    """The runtime names the port the plan declared — and the one in use.

    Both are needed: the declared port says what the plan asked for, the
    effective port says where the answer comes from.  A reader that only
    has the declared port would point a VP at the stranger.
    """
    project, plan_dir = _world(tmp_path)
    _install(monkeypatch)

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    runtime = result.runtimes["api"]
    assert runtime.declared_port == DECLARED_PORT
    assert runtime.port == MOVED_PORT


def test_the_reused_address_event_is_written(tmp_path, monkeypatch):
    """The adoption is written down like every other address decision.

    The stream is how an operator tells "we moved" from "we found it",
    and an adoption is a decision about an address like any other.
    """
    project, plan_dir = _world(tmp_path)
    _install(monkeypatch)

    sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    events = sm.read_address_events(plan_dir)
    reused = [event for event in events if event.get("event") == "reused"]
    assert reused, f"no reused event was written; events: {events!r}"
    assert reused[0].get("new_port") == MOVED_PORT
    assert reused[0].get("declared_port") == DECLARED_PORT


# ---------------------------------------------------------------------------
# The fallbacks
# ---------------------------------------------------------------------------


def test_unreachable_candidate_falls_back_to_relocation(tmp_path, monkeypatch):
    """The instance went away between the books being read and now.

    Then there is nothing to adopt, and the round does what it always
    did: it gets out of the way of the stranger.
    """
    project, plan_dir = _world(tmp_path)
    _install(monkeypatch, moved_listening=False)

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    runtime = result.runtimes["api"]
    assert runtime.port == FALLBACK_PORT, (
        f"the round relocates around the stranger; got {runtime!r}"
    )
    assert runtime.origin == "spawned"
    assert runtime.declared_port == DECLARED_PORT


def test_a_stranger_on_the_candidate_port_is_not_adopted(tmp_path, monkeypatch):
    """Our books say 38921; something else is answering there.

    The books are not enough — the live command line is the identity.
    A process that does not run our program is not adopted, however
    confidently the plan's ledger names the port.
    """
    project, plan_dir = _world(tmp_path)
    _install(monkeypatch, moved_cmdline="node /elsewhere/other.js")

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    runtime = result.runtimes["api"]
    assert runtime.port == FALLBACK_PORT, (
        f"a stranger is not adopted; got {runtime!r}"
    )
    assert runtime.origin == "spawned"


def test_no_candidate_falls_back_to_relocation(tmp_path, monkeypatch):
    """Nothing of ours is alive: the round relocates, as before."""
    project, plan_dir = _world(tmp_path)
    _install(monkeypatch, candidates=False)

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    runtime = result.runtimes["api"]
    assert runtime.port == FALLBACK_PORT
    assert runtime.origin == "spawned"


def test_a_plan_without_a_directory_adopts_nothing(tmp_path, monkeypatch):
    """A plan that does not know its own directory has no books to read."""
    project, _plan_dir = _world(tmp_path)
    _install(monkeypatch)

    adopted = sm._adopt_own_live_instance(
        _decl(), project, plan_dir=None, plan_id="",
    )

    assert adopted is None


# ---------------------------------------------------------------------------
# The function itself
# ---------------------------------------------------------------------------


def test_the_adoption_names_what_it_adopted(tmp_path, monkeypatch):
    """The runtime says which instance it adopted, and where."""
    project, plan_dir = _world(tmp_path)
    _install(monkeypatch)

    adopted = sm._adopt_own_live_instance(
        _decl(), project, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert adopted is not None
    assert adopted.port == MOVED_PORT
    assert adopted.declared_port == DECLARED_PORT
    assert adopted.origin == "adopted"
    assert adopted.ready
    assert adopted.pid == PID
    assert str(MOVED_PORT) in adopted.detail
