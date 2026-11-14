"""Unit tests: reaping this plan's **own dead residual** before relocating
(2026-11-13, B.1).

The incident: a round that had to relocate its service left the instance
it started behind on the free port it moved to.  When that instance died,
the round that came after it found the **declared** port held by a
stranger and — with nothing adoptable — relocated around the stranger.
The dead instance's lock file and pin record were still on disk, so the
copy the round started was refused by the instance lock (rc=3): the
round ended up with no service at all.

The fix is **not** to kill anything from this module.  It is to hand the
residual to the **sanctioned** reap path
(:func:`service_manager.reap_services`, which is :mod:`process_reaper`
in the branch's shape) **before** the relocation starts the new
instance — so the lock and the pin go away through the path that knows
what may be signalled.

Two things the reap must not become:

* a way to kill the stranger that holds the declared port — the reap
  *reports* foreign holders, it never signals them;
* a sweep that runs when there is nothing of ours to sweep — no
  candidate means no reap.

This file covers the placement of the reap: adopt → reap → relocate.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
#: Why the reap is asked for.
REAP_REASON = "no adoptable own residual before relocation"
#: Who is asking for the reap.
REAP_CALLER = "service_manager._ensure_services"


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


def _world(tmp_path: Path) -> Tuple[Path, Path]:
    """A project and the plan directory that owns the leftover."""
    project = tmp_path / "project"
    project.mkdir()
    plan_dir = tmp_path / "plans" / "20261113-framework-base-drift"
    plan_dir.mkdir(parents=True)
    return project, plan_dir


class _Recorder:
    """What the round did to the world — in the order it did it."""

    def __init__(self) -> None:
        self.started: List[int] = []
        self.stopped: List[Any] = []
        self.reaped: List[Dict[str, Any]] = []
        self.order: List[str] = []


def _install(
    monkeypatch,
    tmp_path: Path,
    *,
    candidates: bool = True,
    moved_listening: bool = False,
    reap_raises: bool = False,
    foreign_ports: Sequence[int] = (),
) -> _Recorder:
    """Say what the world looks like, and count what the round does to it.

    * the declared port is held by a **stranger**;
    * this plan's own instance sits on ``MOVED_PORT`` — dead by default
      (the port answers nothing), alive when *moved_listening* says so;
    * every start, every stop and every reap is counted, and the order
      they happened in is kept.

    Returns the recorder.
    """
    rec = _Recorder()

    def _fake_start(decl, project_dir, *args, **kwargs):
        rec.started.append(decl.port)
        rec.order.append(f"start:{decl.port}")
        return sm.ServiceRuntime(
            name=decl.name, port=decl.port, pid=5151, pgid=5151,
            start_cmd=decl.start_cmd, cwd=decl.cwd,
            origin="spawned", ready=True, detail="stub",
        )

    def _fake_stop(*args, **kwargs):
        runtime = args[0] if args else kwargs.get("runtime")
        rec.stopped.append(runtime)
        rec.order.append(f"stop:{getattr(runtime, 'pid', None)}")
        return {"port_free": True, "detail": "stub"}

    def _fake_own_instances(plan_dir, decl, project_dir, *, plan_id=""):
        return [_candidate()] if candidates else []

    def _fake_probe(port):
        if port == DECLARED_PORT:
            return _probe(port, FOREIGN_PID, FOREIGN_CMDLINE)
        if port == MOVED_PORT:
            return _probe(
                port, PID, OUR_CMDLINE, listening=moved_listening,
            )
        return PortProbe(port=port, listening=False)

    def _fake_reap(plan_dir, plan_id="", **kwargs):
        rec.reaped.append({
            "plan_dir": plan_dir, "plan_id": plan_id, **kwargs,
        })
        rec.order.append("reap")
        if reap_raises:
            raise RuntimeError("the reap blew up")
        return sm.ReapReport(
            plan_id=str(plan_id or ""),
            reason=str(kwargs.get("reason") or ""),
            foreign_ports=list(foreign_ports),
        )

    monkeypatch.setattr(sm, "start_service", _fake_start)
    monkeypatch.setattr(sm, "stop_service", _fake_stop)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: FALLBACK_PORT)
    monkeypatch.setattr(sm, "own_service_instances", _fake_own_instances)
    monkeypatch.setattr(sm, "probe_port", _fake_probe)
    monkeypatch.setattr(sm, "reap_services", _fake_reap)
    # Keep the search for a leftover of ours inside the test's own tree.
    monkeypatch.setattr(
        sm, "_plans_tree", lambda: tmp_path / "plans-tree",
    )
    return rec


# ---------------------------------------------------------------------------
# The reap happens before the start
# ---------------------------------------------------------------------------


def test_dead_own_instance_is_reaped_before_start(tmp_path, monkeypatch):
    """Our own instance is dead: it is reaped, and then the new one starts.

    The residual is not ours to signal from this module — it is handed to
    the sanctioned reap path, and only after that does the round start
    its copy on a free port.  Starting first is what the instance lock
    refuses (rc=3).
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path)

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert len(rec.reaped) == 1, (
        f"the dead residual must be reaped exactly once; got {rec.reaped!r}"
    )
    call = rec.reaped[0]
    assert Path(call["plan_dir"]) == plan_dir, (
        f"the reap must be told which plan's residual to sweep; "
        f"got {call['plan_dir']!r}"
    )
    assert call["plan_id"] == plan_dir.name
    assert call["reason"] == REAP_REASON
    assert call["caller"] == REAP_CALLER

    runtime = result.runtimes["api"]
    assert runtime.port == FALLBACK_PORT, (
        f"the round relocates around the stranger; got {runtime!r}"
    )
    assert runtime.ready, "the round must still have a service"


def test_the_reap_happens_before_the_new_instance_starts(tmp_path, monkeypatch):
    """The order is the whole point: reap, then start.

    A residual that is swept *after* the new instance started is a
    residual that could already have refused it (rc=3).
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path)

    sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert rec.order == ["reap", f"start:{FALLBACK_PORT}"], (
        f"the residual must be reaped before the new instance starts; "
        f"the round did: {rec.order!r}"
    )


# ---------------------------------------------------------------------------
# When there is nothing to reap
# ---------------------------------------------------------------------------


def test_no_candidate_means_no_reap(tmp_path, monkeypatch):
    """Nothing of ours is left behind: no reap runs.

    A sweep that has nothing to sweep is a sweep that can only do harm —
    it takes the services of a live round with it.
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path, candidates=False)

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert rec.reaped == [], (
        f"no candidate means no reap; the round reaped {rec.reaped!r}"
    )
    runtime = result.runtimes["api"]
    assert runtime.port == FALLBACK_PORT
    assert runtime.ready


def test_adopted_instance_is_not_reaped(tmp_path, monkeypatch):
    """A live instance of ours is adopted, and adoption ends the story.

    The reap is for a residual that cannot be used.  An instance that is
    alive and answers is the service the round wants: it is adopted, and
    no sweep runs behind its back.
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path, moved_listening=True)

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert rec.reaped == [], (
        f"an adopted instance is not reaped; the round reaped {rec.reaped!r}"
    )
    runtime = result.runtimes["api"]
    assert runtime.port == MOVED_PORT, (
        f"the round is pointed at our own instance; got {runtime!r}"
    )
    assert runtime.origin == "adopted"
    assert runtime.ready


def test_a_plan_without_a_directory_skips_the_reap(tmp_path, monkeypatch):
    """A plan that does not know its own directory has nothing to reap.

    The reap is told which plan's books to read.  Without a directory
    there is nothing to read, so the round relocates and says so.
    """
    project, _plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path)

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=None, plan_id="",
    )

    assert rec.reaped == [], (
        f"without a plan directory there is nothing to reap; the round "
        f"reaped {rec.reaped!r}"
    )
    runtime = result.runtimes["api"]
    assert runtime.port == FALLBACK_PORT
    assert runtime.ready


# ---------------------------------------------------------------------------
# When the reap itself fails
# ---------------------------------------------------------------------------


def test_reap_failure_still_relocates(tmp_path, monkeypatch, caplog):
    """A broken reap is a warning, not the end of the round's service.

    The round cannot have no service because a sweep failed: the
    stranger is still there, and the round still gets out of its way.
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path, reap_raises=True)

    with caplog.at_level(logging.WARNING, logger="service_manager"):
        result = sm.ensure_services(
            project, {"api": _decl()},
            plan_dir=plan_dir, plan_id=plan_dir.name,
        )

    assert len(rec.reaped) == 1, "the reap was tried"
    runtime = result.runtimes["api"]
    assert runtime.ready, (
        f"the round must still have a service after a failed reap; "
        f"got {runtime!r}"
    )
    assert runtime.port == FALLBACK_PORT
    assert "reap" in caplog.text.lower(), (
        f"a failed reap is recorded as a warning; the log said: "
        f"{caplog.text!r}"
    )


# ---------------------------------------------------------------------------
# The stranger is never signalled
# ---------------------------------------------------------------------------


def test_foreign_process_is_never_killed(tmp_path, monkeypatch):
    """The stranger on the declared port is left completely alone.

    The reap exists to clear **our own** residual.  It must not become a
    second way of killing a process we cannot positively identify — that
    is the rule the whole ownership model rests on.
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path)

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    signalled = [
        runtime for runtime in rec.stopped
        if getattr(runtime, "pid", None) == FOREIGN_PID
    ]
    assert signalled == [], (
        f"a stranger must never be signalled; signalled {signalled!r}"
    )
    assert rec.stopped == [], (
        f"nothing may be signalled on this path; signalled {rec.stopped!r}"
    )
    assert result.runtimes["api"].ready


def test_foreign_ports_are_reported_not_killed(tmp_path, monkeypatch, caplog):
    """A port the reap could not identify is reported, never killed.

    The reap's own rules say this: an unidentified holder is a stranger,
    and a stranger is worse than an orphan.  The round carries on.
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path, foreign_ports=(9999,))

    with caplog.at_level(logging.WARNING, logger="service_manager"):
        result = sm.ensure_services(
            project, {"api": _decl()},
            plan_dir=plan_dir, plan_id=plan_dir.name,
        )

    assert rec.stopped == [], (
        f"a foreign holder is reported, not killed; signalled {rec.stopped!r}"
    )
    assert "9999" in caplog.text, (
        f"the foreign holder is reported; the log said: {caplog.text!r}"
    )
    runtime = result.runtimes["api"]
    assert runtime.ready
    assert runtime.port == FALLBACK_PORT
