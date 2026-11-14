"""Unit tests: the placement boundaries that must **not** be relaxed
(2026-11-13, B.2).

The 2026-11-13 work changed **rule 4** of :mod:`service_manager` — the
order in which a round answers "the declared port is held by something we
cannot identify" — and made this plan's own live instance adoptable.
Both changes sit on top of the ownership model that has been in place
since 2026-09-18, and that model is a set of **safety boundaries**:

  1. a port nobody holds → the service is started on the **declared**
     port;
  2. a port held by a listener that **matches** the declaration → it is
     adopted, not restarted;
  3. a port held by a process we cannot identify → it is **never
     signalled**; the round gets out of the way instead.

This file is the **negative control** for those boundaries.  It changes
no production code: it exists so that a later change which widens one of
them fails here, and so that "not relaxed" is a runnable statement rather
than a promise.

The case at the centre is B.2 — a **foreign** process (one that belongs
to no plan of ours and whose command line does not match the
declaration) is never killed, and when this plan has **no residual** of
its own the round still relocates to a free port instead of stranding
itself.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402
from service_freshness import PortProbe  # noqa: E402


#: The port the plan declared.
DECLARED_PORT = 3000
#: The free port the round relocates to when the declared one is taken.
FREE_PORT = 38921
#: The pid of the stranger that holds the declared port.
FOREIGN_PID = 9999
#: The pid of the listener that matches the declaration.
MATCHING_PID = 8888
#: The pid of this plan's own instance (the residual).
PID = 4242
#: The command the declaration starts, on the declared port.
START_CMD = "npx next dev -p 3000"
#: The command line of the stranger: it matches nothing in the declaration.
FOREIGN_CMDLINE = "node /elsewhere/server.js"
#: The command line of the listener that matches the declaration.
MATCHING_CMDLINE = "npx next dev -p 3000"
#: The command line our own instance carries (it runs on the free port).
OUR_CMDLINE = "npx next dev -p 38921"


def _decl(**overrides) -> ServiceDeclaration:
    """The declaration of the ``api`` service."""
    base = dict(name="api", port=DECLARED_PORT, start_cmd=START_CMD, cwd=".")
    base.update(overrides)
    return ServiceDeclaration(**base)


def _probe(
    port: int, pid: int, cmdline: str, *, listening: bool = True,
) -> PortProbe:
    """A probe that says "somebody is listening on *port*"."""
    return PortProbe(
        port=port, listening=listening, pids=[pid], cmdlines=[cmdline],
    )


def _candidate(**overrides) -> Dict[str, Any]:
    """What :func:`service_manager.own_service_instances` finds."""
    base = {
        "name": "api",
        "pid": PID,
        "port": FREE_PORT,
        "declared_port": DECLARED_PORT,
        "source": "pin",
        "start_epoch": 1760000000.0,
        "owner_token": "",
    }
    base.update(overrides)
    return base


def _world(tmp_path: Path) -> Tuple[Path, Path]:
    """A project and the plan directory that owns the residual."""
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
        self.probed: List[int] = []
        self.reaped: List[Dict[str, Any]] = []
        self.order: List[str] = []


def _install(
    monkeypatch,
    tmp_path: Path,
    *,
    declared_holder: Optional[str] = None,
    own_residual: bool = False,
    residual_listening: bool = True,
) -> _Recorder:
    """Say what the world looks like, and count what the round does to it.

    * *declared_holder* says who holds the declared port: ``None`` (nobody
      listens), ``"foreign"`` (a stranger whose command line matches
      nothing) or ``"matching"`` (a listener that matches the
      declaration);
    * *own_residual* says whether this plan's books name an instance of
      its own — on the free port, alive when *residual_listening* says so;
    * every start, stop, probe and reap is counted.

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
        return [_candidate()] if own_residual else []

    def _fake_probe(port):
        rec.probed.append(port)
        if port == DECLARED_PORT:
            if declared_holder == "foreign":
                return _probe(port, FOREIGN_PID, FOREIGN_CMDLINE)
            if declared_holder == "matching":
                return _probe(port, MATCHING_PID, MATCHING_CMDLINE)
            return PortProbe(port=port, listening=False)
        if port == FREE_PORT:
            return _probe(
                port, PID, OUR_CMDLINE, listening=residual_listening,
            )
        return PortProbe(port=port, listening=False)

    def _fake_reap(plan_dir, plan_id="", **kwargs):
        rec.reaped.append({
            "plan_dir": plan_dir, "plan_id": plan_id, **kwargs,
        })
        rec.order.append("reap")
        return sm.ReapReport(
            plan_id=str(plan_id or ""),
            reason=str(kwargs.get("reason") or ""),
            foreign_ports=[],
        )

    monkeypatch.setattr(sm, "start_service", _fake_start)
    monkeypatch.setattr(sm, "stop_service", _fake_stop)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: FREE_PORT)
    monkeypatch.setattr(sm, "own_service_instances", _fake_own_instances)
    monkeypatch.setattr(sm, "probe_port", _fake_probe)
    monkeypatch.setattr(sm, "reap_services", _fake_reap)
    # The data-plane probe answers over the network; the placement rules
    # are not about it, and the verdict must not depend on what happens
    # to be listening on the machine that runs the suite.
    monkeypatch.setattr(
        sm, "data_plane_evidence", lambda port, **kwargs: sm.DataPlaneEvidence(),
    )
    # Keep the search for a leftover of ours inside the test's own tree.
    monkeypatch.setattr(
        sm, "_plans_tree", lambda: tmp_path / "plans-tree",
    )
    return rec


# ---------------------------------------------------------------------------
# B.2 — the foreign holder is relocated, never killed
# ---------------------------------------------------------------------------


def test_foreign_holder_is_relocated_never_killed(tmp_path, monkeypatch):
    """A stranger holds the declared port: it is left alone, and the round
    gets out of the way.

    The stranger belongs to no plan of ours and its command line matches
    nothing in the declaration, so the round cannot positively identify it
    — and a process that cannot be identified is never signalled.  With no
    residual of our own to adopt, the round relocates: our own copy is
    started on a free port and every VP is pointed at that.
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path, declared_holder="foreign")

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert rec.stopped == [], (
        f"a foreign process must never be signalled; stopped {rec.stopped!r}"
    )
    assert rec.started == [FREE_PORT], (
        f"the round must start exactly one copy, on the free port; "
        f"started {rec.started!r}"
    )

    runtime = result.runtimes["api"]
    assert runtime.port == FREE_PORT, (
        f"the round must run on the free port; got {runtime!r}"
    )
    assert runtime.declared_port == DECLARED_PORT, (
        f"the declared port is still what the plan asked for; got "
        f"{runtime.declared_port!r}"
    )
    assert runtime.reclaimed is False, (
        f"nothing was reclaimed — the declared port belongs to somebody "
        f"else; got {runtime.reclaimed!r}"
    )


def test_the_foreign_holder_is_not_even_reaped(tmp_path, monkeypatch):
    """The sanctioned reap must not become a second way of killing it.

    The reap exists to clear **our own** residual.  With no residual of
    our own there is nothing to sweep, and a sweep that runs anyway can
    only take the services of a live round with it.
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path, declared_holder="foreign")

    sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert rec.reaped == [], (
        f"no residual of ours means no reap; the round reaped {rec.reaped!r}"
    )
    assert rec.stopped == [], (
        f"nothing may be signalled on this path; stopped {rec.stopped!r}"
    )


# ---------------------------------------------------------------------------
# Rule 1 — a free declared port is used as it stands
# ---------------------------------------------------------------------------


def test_free_declared_port_starts_normally(tmp_path, monkeypatch):
    """Nobody holds the declared port: the service starts **on it**.

    Rule 1 is untouched by the 2026-11-13 work, and it is the rule that
    keeps the effective declarations the VPs read valid — a round that
    relocated for no reason would point them at a port the plan never
    asked for.
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path)

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert rec.started == [DECLARED_PORT], (
        f"a free declared port must be used as it stands; started "
        f"{rec.started!r}"
    )
    runtime = result.runtimes["api"]
    assert runtime.port == DECLARED_PORT
    assert runtime.origin == "spawned", (
        f"the round started the service itself; got {runtime.origin!r}"
    )
    # A service that runs on the port the plan declared leaves
    # ``declared_port`` empty — there is nothing to distinguish it from.
    # The record that is written down falls back to the port itself, so
    # the effective declarations the VPs read stay valid.
    assert runtime.to_dict()["declared_port"] == DECLARED_PORT
    assert rec.stopped == [], (
        f"there was nothing to stop; stopped {rec.stopped!r}"
    )
    # A port nobody holds leaves nothing of ours behind, so there is
    # nothing to sweep — and a sweep that runs anyway can only take the
    # services of a live round with it.  The round must neither reap
    # directly nor reach the reap through the leftover path.
    assert rec.reaped == [], (
        f"a free declared port means no reap at all — the round reaped "
        f"{rec.reaped!r}"
    )


# ---------------------------------------------------------------------------
# Rule 2 — a matching listener is adopted, not restarted
# ---------------------------------------------------------------------------


def test_matching_listener_is_still_adopted(tmp_path, monkeypatch):
    """The listener on the declared port matches the declaration: it is
    adopted.

    This is the case the whole safety model is built around — a process
    whose command line matches the declaration is one we started, so it
    may be managed.  A later change must not turn "adopt" into "restart"
    or "relocate".
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path, declared_holder="matching")

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    runtime = result.runtimes["api"]
    assert runtime.origin == "adopted", (
        f"a matching listener is adopted; got {runtime.origin!r}"
    )
    assert runtime.port == DECLARED_PORT
    assert runtime.pid == MATCHING_PID
    assert rec.started == [], (
        f"an adopted listener is not started again; started {rec.started!r}"
    )
    assert rec.stopped == [], (
        f"a fresh matching listener is not stopped; stopped {rec.stopped!r}"
    )


# ---------------------------------------------------------------------------
# The residual of our own
# ---------------------------------------------------------------------------


def test_adopted_own_residual_is_not_stopped(tmp_path, monkeypatch):
    """Our own live instance is adopted: nothing is stopped, nothing is
    started.

    The 2026-11-13 change added this path, and it is the one place where
    the round touches a process it owns.  It must **adopt**, not kill:
    the instance is the service the round wants, on a port that works.
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(
        monkeypatch, tmp_path,
        declared_holder="foreign",
        own_residual=True,
        residual_listening=True,
    )

    result = sm.ensure_services(
        project, {"api": _decl()}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert rec.stopped == [], (
        f"an adopted instance must not be stopped; stopped {rec.stopped!r}"
    )
    assert rec.started == [], (
        f"an adopted instance must not be started again; started "
        f"{rec.started!r}"
    )
    runtime = result.runtimes["api"]
    assert runtime.origin == "adopted", (
        f"the round must be pointed at its own instance; got {runtime!r}"
    )
    assert runtime.port == FREE_PORT
    assert runtime.declared_port == DECLARED_PORT
    assert runtime.reclaimed is False, (
        f"adoption is not a reclaim; got {runtime.reclaimed!r}"
    )


# ---------------------------------------------------------------------------
# Nothing to do
# ---------------------------------------------------------------------------


def test_no_declarations_returns_empty(tmp_path, monkeypatch):
    """No declarations: the round asks nothing of the world.

    An empty declaration map is not a reason to probe a port — and a
    probe that reaches for a port nobody declared is a probe that can
    read a port somebody else owns.
    """
    project, plan_dir = _world(tmp_path)
    rec = _install(monkeypatch, tmp_path)

    result = sm.ensure_services(
        project, {}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert result.runtimes == {}, (
        f"there is nothing to bring up; got {result.runtimes!r}"
    )
    assert rec.probed == [], (
        f"no declaration means no port is probed; probed {rec.probed!r}"
    )
    assert rec.started == [], (
        f"no declaration means nothing is started; started {rec.started!r}"
    )
    assert rec.stopped == [], (
        f"no declaration means nothing is stopped; stopped {rec.stopped!r}"
    )


# ---------------------------------------------------------------------------
# The controls must be sensitive to the mutation they police
# ---------------------------------------------------------------------------
#
# A negative control that cannot go red is a comment.  The cases above
# say what must not happen; this section proves that one of them is
# actually able to notice.
#
# The mutation is the mistake the free-declared-port control exists to
# catch: a sweep that runs although there is nothing of ours to sweep.
# A copy of :mod:`service_manager` that reaps before it does anything
# else is that mistake, made executable.

#: The mutant: a copy of ``service_manager.py`` with one block appended.
#: It is a **wrapper** rather than an edit of the body on purpose — the
#: copy does everything the production module does, and the only
#: difference between the two is the sweep.  A mutant that rewrote the
#: body could differ from the production code in more ways than the one
#: under test, and the proof would then be about the rewrite.
_MUTANT_SUFFIX = '''

# --- VP-008 mutation (M7): a sweep before every round, wanted or not ---
_VP008_UNDISTURBED_ENSURE_SERVICES = ensure_services


def ensure_services(*args, **kwargs):
    """The mutant: sweep first, then do what the round was going to do."""
    reap_services(
        kwargs.get("plan_dir"),
        plan_id=kwargs.get("plan_id", ""),
        reason="vp008-mutation",
    )
    return _VP008_UNDISTURBED_ENSURE_SERVICES(*args, **kwargs)
'''

#: The driver: install the mutant as the module named ``service_manager``
#: and run one test against it.  It always exits 0 — what it found is in
#: the JSON file it writes.  A driver that exited non-zero would make
#: "the mutant did not even import" look the same as "the mutant was
#: caught", and the second of those is the only one that proves anything.
_DRIVER_SOURCE = '''"""Install a mutated ``service_manager`` and run one test against it.

The mutant goes into ``sys.modules`` under the name the test imports
(``service_manager``) *before* pytest collects anything, so the test
under it runs against the mutant and not against the production module.
The driver always exits 0: the outcome is in the JSON file it writes, so
that a mutant which failed to import cannot be mistaken for a mutant
which was caught.
"""
import importlib.util
import json
import pathlib
import sys

mutant_path = pathlib.Path(sys.argv[1])
backend_dir = pathlib.Path(sys.argv[2])
target = sys.argv[3]
result_path = pathlib.Path(sys.argv[4])

result = {"module_file": None, "pytest_rc": None, "import_error": None}

if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

try:
    spec = importlib.util.spec_from_file_location(
        "service_manager", mutant_path,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["service_manager"] = module
    spec.loader.exec_module(module)
except Exception as exc:  # noqa: BLE001 - the caller reads the reason
    result["import_error"] = f"{type(exc).__name__}: {exc}"
else:
    import pytest

    result["module_file"] = module.__file__
    result["pytest_rc"] = int(
        pytest.main([target, "-q", "-p", "no:cacheprovider", "--timeout=120"])
    )

result_path.write_text(json.dumps(result), encoding="utf-8")
raise SystemExit(0)
'''


def test_the_free_port_control_is_mutation_sensitive(tmp_path):
    """The free-declared-port control must go red on a sweep that has no
    business running.

    A control that cannot fail is a comment.  This case proves that the
    control in :func:`test_free_declared_port_starts_normally` is
    sensitive: a copy of :mod:`service_manager` that reaps before it
    does anything else — the mistake that control exists to catch —
    makes it fail.

    The mutant is installed under the module name the test imports, so
    the test really runs against the mutant, and the proof runs in a
    child process because a module cannot be replaced inside the
    process that is running the test.
    """
    mutant_dir = tmp_path / "mutant"
    mutant_dir.mkdir()
    mutant = mutant_dir / "service_manager.py"
    mutant.write_text(
        (_BACKEND_DIR / "service_manager.py").read_text(encoding="utf-8")
        + _MUTANT_SUFFIX,
        encoding="utf-8",
    )

    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER_SOURCE, encoding="utf-8")
    result_path = tmp_path / "result.json"

    target = (
        f"{Path(__file__).resolve()}::test_free_declared_port_starts_normally"
    )
    proc = subprocess.run(
        [
            sys.executable, str(driver), str(mutant), str(_BACKEND_DIR),
            target, str(result_path),
        ],
        cwd=str(_BACKEND_DIR.parent),
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert proc.returncode == 0, (
        f"the driver always exits 0 — it reports through the JSON file; "
        f"it exited {proc.returncode}\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert result_path.exists(), (
        f"the driver wrote no result file\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))

    assert result["import_error"] is None, (
        f"the mutant did not import, so nothing was proved; "
        f"{result['import_error']}"
    )
    assert Path(result["module_file"]).resolve() == mutant.resolve(), (
        f"the control must run against the mutant; it imported "
        f"{result['module_file']!r} instead of {str(mutant)!r}"
    )
    assert result["pytest_rc"] != 0, (
        f"the mutant survived: the free-declared-port control did not go "
        f"red on a sweep that had no business running\n{proc.stdout}"
    )
    assert "test_free_declared_port_starts_normally" in proc.stdout, (
        f"the run did not report on the control under test\n{proc.stdout}"
    )
    assert "AssertionError" in proc.stdout, (
        f"the control must go red on its own assertion, not on an error "
        f"somewhere else\n{proc.stdout}"
    )