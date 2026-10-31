"""Negative controls for the process-ownership work: N1-N5.

The unit suites pin each module on its own — the ledger's fold, the
reaper's identity check, the lock's contention, the two routes'
decisions.  What they cannot pin is the thing that actually broke in
production: **a real process tree**.  Every negative control here is
therefore run against real processes — a forked executor, a sub-agent
with a tool of its own, a VP service — in a plan directory of its own,
and every assertion is made against the pids the ledger recorded.

The five controls
-----------------

N1  A backend that is killed with SIGKILL leaves its tree behind.  The
    next backend boot has to collect it, and the orphan has to stop
    writing: a sub-agent whose transcript keeps growing is a sub-agent
    that is still burning tokens.

N2  A backend that is asked to stop has to *say* what it did: named
    events naming the pid, the command line, the working directory,
    the role — and how long the process lived.

N3  An executor that is asked to stop has to take its children with it
    and give the plan's lock back.

N4  A stop that has lost its handle — the backend restarted — still has
    to find the plan's processes in the ledger, and it may only answer
    "stopped" once they are *confirmed* dead.

N5  One plan, one executor: a second start is refused, and an executor
    started by hand against a plan that is already running refuses to
    start at all.

Two rules hold the whole file together:

* **The ledger is the only list.**  Every assertion is made against
  the pids the ledger recorded, never against a scan of the machine.
  The fake processes carry a ``pdt-fake-<uuid>`` marker in their
  command line so that a stray one can be recognised — and the last
  test pins that a marked process the ledger does not know is never
  touched.

* **Everything started is handed back.**  Every test kills and reaps
  the processes it started, in a ``finally``, whether it passed or not.

Nothing here binds a port, starts a container, or reaches a network.
The fake VP service is deliberately port-less: these controls are about
*process ownership*, and a port would only be one more way to collide
with the machine the tests run on.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from fastapi.testclient import TestClient

import executor_lock
import process_identity
import process_ledger as ledger
import process_reaper
import server
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository


pytestmark = pytest.mark.integration

#: ``backend/`` — resolved from this file, because the checkout lives
#: at a different path on every machine.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The liveness question the reaper asks.  Captured here so that a
#: test which deliberately fools the reaper does not fool the test's
#: own bookkeeping with it.
_ALIVE = process_reaper._alive

#: The roles the fake tree is made of.
_TREE_ROLES = ("executor", "subagent", "tool", "vp_service")


# ---------------------------------------------------------------------------
# The fake process tree
# ---------------------------------------------------------------------------
#
# Two scripts, both written into the plan's own directory at test time:
#
#   fake_node.py     one layer of the tree: the executor, the
#                    sub-agent, the tool, the VP service
#   fake_backend.py  the backend that owns the tree
#
# The topology is the one the real backend builds:
#
#     fake backend ── executor ── sub-agent ── tool (the test process)
#                  └─ vp_service
#
# Every layer detaches into a session of its own — except the tool,
# which stays inside the sub-agent's process group, because the group
# kill is what takes it down.
#
# The scripts are written as files rather than piped into the
# interpreter so that their command lines are real: the identity check
# compares command lines, and a ``python -c`` one-liner would not look
# like the tree the reaper has to recognise.

_NODE_SOURCE = r'''
"""One node of a fake PDT process tree.

Run as::

    python fake_node.py <config.json> <marker> <role>

The config says what this node is: which plan it belongs to, where it
works, which child it starts, and where it writes the file that tells
the test who it is.  The marker is in the command line on purpose — a
fake process has to be recognisable in ``ps`` output, and the last
negative control is about a marked process the ledger does not know.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _settled_identity(process_identity, pid, tries=60, delay=0.05):
    """Probe ``pid`` until two consecutive readings agree.

    The launcher rewrites ``argv[0]`` shortly after the exec, so a
    reading taken the instant a process is spawned can spell the
    command line differently from a reading taken a moment later.  The
    identity check compares command lines, so the *recorded* one has
    to be the settled one.
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


def main():
    config_path = sys.argv[1]
    marker = sys.argv[2]
    role = sys.argv[3]

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))

    backend_dir = str(config["backend_dir"])
    sys.path.insert(0, backend_dir)
    sys.path.insert(0, str(Path(backend_dir).parent))

    import executor_lock
    import process_identity
    import process_ledger as ledger
    import process_reaper

    plan_dir = Path(config["plan_dir"])
    plan_id = config["plan_id"]

    work_dir = Path(config["cwd"])
    work_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(str(work_dir))

    stderr_path = config.get("stderr")
    if stderr_path:
        sys.stderr = open(stderr_path, "a", buffering=1, encoding="utf-8")

    # The executor is the only layer that takes the plan's lock, and
    # the only one that answers SIGTERM with a sweep of its own tree.
    lock = None
    if role == "executor":
        lock = executor_lock.acquire(plan_dir)
        if lock is None:
            sys.stderr.write("the plan lock is already held\n")
            sys.exit(4)
        process_reaper.install_executor_signal_handlers(
            plan_dir, plan_id, lock=lock,
        )

    # The child this node starts.  ``detach`` says whether the child
    # gets a session of its own: the sub-agent does (it is a layer of
    # its own), the tool does not (it lives in the sub-agent's group,
    # and the group kill is what takes it down).
    child_spec = config.get("child")
    child_pid = None
    if child_spec:
        child_config = json.loads(
            Path(child_spec["config"]).read_text(encoding="utf-8")
        )
        child_err = open(child_config["stderr"], "ab")
        child = subprocess.Popen(
            [
                sys.executable, "-u",
                str(config["node_script"]),
                str(child_spec["config"]),
                marker,
                child_spec["role"],
            ],
            stdout=subprocess.DEVNULL,
            stderr=child_err,
            start_new_session=bool(child_spec.get("detach", True)),
            cwd=str(work_dir),
        )
        identity = _settled_identity(process_identity, child.pid)
        if identity is None:
            sys.stderr.write("the child has no readable identity\n")
            sys.exit(5)
        # The parent records the child, the way the backend records the
        # executor it started.
        ledger.record_spawn(
            plan_dir,
            ledger.ProcessRecord(
                plan_id=plan_id,
                role=child_spec["role"],
                pid=child.pid,
                pgid=identity.pgid or child.pid,
                create_time=identity.create_time or 0.0,
                cmdline=list(identity.cmdline),
                cwd=identity.cwd or str(work_dir),
                owner_pid=os.getpid(),
                owner_role=role,
            ),
        )
        child_pid = child.pid

    # Tell the test who this node is.
    announce = Path(config["announce"])
    announce.parent.mkdir(parents=True, exist_ok=True)
    announce.write_text(
        json.dumps({
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "child_pid": child_pid,
            "role": role,
        }),
        encoding="utf-8",
    )

    lifetime = float(config.get("lifetime") or 0.0)
    if lifetime > 0:
        time.sleep(lifetime)
        sys.exit(0)

    transcript = config.get("transcript")
    if role == "subagent" and transcript:
        # The sub-agent writes a line every 0.2 s: the size of this
        # file is the clock the test reads to see whether the orphan
        # is still running.
        while True:
            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write("tick %d\n" % time.time())
                handle.flush()
            time.sleep(0.2)

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
'''


_BACKEND_SOURCE = r'''
"""A fake PDT backend.

Run as::

    python fake_backend.py <config.json> <marker>

It is the backend in the smallest form that can be tested: it owns a
plan's process tree, it writes the tree into the plan's ledger, it
sweeps the plans directory when it boots (what a real backend does
after a crash) and when it is asked to stop.

``mode="sweep"`` is the boot that only boots: it sweeps the plans
directory and exits.  That is the process that collects what a killed
backend left behind.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _settled_identity(process_identity, pid, tries=60, delay=0.05):
    """Probe ``pid`` until two consecutive readings agree."""
    previous = None
    for _ in range(tries):
        ident = process_identity.probe(pid)
        if ident is not None:
            if previous is not None and ident.cmdline == previous.cmdline:
                return ident
            previous = ident
        time.sleep(delay)
    return previous


def _write_config(path, payload):
    Path(path).write_text(json.dumps(payload), encoding="utf-8")


def main():
    config_path = sys.argv[1]
    marker = sys.argv[2]
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))

    backend_dir = str(config["backend_dir"])
    sys.path.insert(0, backend_dir)
    sys.path.insert(0, str(Path(backend_dir).parent))

    import process_identity
    import process_ledger as ledger
    import service_manager

    plan_dir = Path(config["plan_dir"])
    plan_id = config["plan_id"]
    plans_dir = Path(config["plans_dir"])
    fake_dir = Path(config["fake_dir"])
    announce_dir = Path(config["announce_dir"])
    work_dir = Path(config["work_dir"])
    announce_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    stderr_path = config.get("stderr")
    if stderr_path:
        sys.stderr = open(stderr_path, "a", buffering=1, encoding="utf-8")

    # --- what a backend does when it boots ----------------------------
    # A backend that was killed leaves its tree behind.  The next boot
    # collects it, from the ledger, before it starts anything.
    service_manager.reap_all_plans(plans_dir, reason="startup_sweep")

    if config.get("mode") == "sweep":
        (fake_dir / "ready.json").write_text(
            json.dumps({"backend_pid": os.getpid(), "swept": True}),
            encoding="utf-8",
        )
        return

    # --- the backend's own shutdown ------------------------------------
    def _on_sigterm(signum, frame):
        service_manager.reap_all_plans(plans_dir, reason="server_shutdown")
        sys.exit(143)

    signal.signal(signal.SIGTERM, _on_sigterm)

    node_script = config["node_script"]

    def _node_config(role, *, cwd, announce, transcript="", lifetime=0.0,
                     child=None):
        payload = {
            "backend_dir": backend_dir,
            "node_script": node_script,
            "plan_dir": str(plan_dir),
            "plan_id": plan_id,
            "cwd": str(cwd),
            "announce": str(announce),
            "transcript": transcript,
            "lifetime": lifetime,
            "stderr": str(fake_dir / (role + ".err")),
            "child": child,
        }
        path = fake_dir / ("node-%s.json" % role)
        _write_config(path, payload)
        return path

    # The tree, from the leaves up.  The tool lives in the sub-agent's
    # group; the sub-agent has a session of its own, and so has the
    # executor.
    tool_config = _node_config(
        "tool", cwd=work_dir / "tool",
        announce=announce_dir / "tool.json",
    )
    subagent_config = _node_config(
        "subagent", cwd=work_dir / "subagent",
        announce=announce_dir / "subagent.json",
        transcript=config.get("transcript", ""),
        child={"role": "tool", "config": str(tool_config), "detach": False},
    )
    executor_config = _node_config(
        "executor", cwd=work_dir / "executor",
        announce=announce_dir / "executor.json",
        child={
            "role": "subagent",
            "config": str(subagent_config),
            "detach": True,
        },
    )
    vp_config = _node_config(
        "vp_service", cwd=work_dir / "vp_service",
        announce=announce_dir / "vp_service.json",
    )
    # A tool the backend owns that ends on its own.  The backend records
    # its exit the way the executor's watcher records an exit, and that
    # event is the one that carries how long the process lived.
    short_tool_config = _node_config(
        "short_tool", cwd=work_dir / "short_tool",
        announce=announce_dir / "short_tool.json",
        lifetime=float(config.get("short_tool_lifetime", 0.8)),
    )

    def _spawn(config_file, role):
        err = open(fake_dir / (role + ".err"), "ab")
        return subprocess.Popen(
            [
                sys.executable, "-u", node_script,
                str(config_file), marker, role,
            ],
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,
            cwd=str(work_dir),
        )

    executor = _spawn(executor_config, "executor")
    vp = _spawn(vp_config, "vp_service")
    short_tool = _spawn(short_tool_config, "short_tool")

    # The backend records what it started, the way the real one does.
    for proc, role in (
        (executor, "executor"),
        (vp, "vp_service"),
        (short_tool, "tool"),
    ):
        identity = _settled_identity(process_identity, proc.pid)
        ledger.record_spawn(
            plan_dir,
            ledger.ProcessRecord(
                plan_id=plan_id,
                role=role,
                pid=proc.pid,
                pgid=(identity.pgid or proc.pid) if identity else proc.pid,
                create_time=(identity.create_time or 0.0) if identity else 0.0,
                cmdline=list(identity.cmdline) if identity else [],
                cwd=(
                    (identity.cwd or str(work_dir)) if identity
                    else str(work_dir)
                ),
                owner_pid=os.getpid(),
                owner_role="backend",
            ),
        )

    short_tool.wait()
    ledger.record_exit(
        plan_dir, short_tool.pid, ledger.EVENT_EXIT, exit_code=0,
    )

    (fake_dir / "ready.json").write_text(
        json.dumps({
            "backend_pid": os.getpid(),
            "executor_pid": executor.pid,
            "vp_service_pid": vp.pid,
            "short_tool_pid": short_tool.pid,
        }),
        encoding="utf-8",
    )

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
'''


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _marker() -> str:
    """A marker every fake process carries in its command line."""
    return "pdt-fake-" + uuid.uuid4().hex[:12]


def _plan_id(prefix: str) -> str:
    """A plan id of this test's own.

    The date prefix keeps the plan out of the archive range, so the
    routes treat it as a live plan.
    """
    return "20261030-nc-{}-{}".format(prefix, uuid.uuid4().hex[:8])


def _process_is_gone(pid: int) -> bool:
    """Whether the pid names no running process any more.

    A zombie counts as gone: it has finished, and only its parent's
    bookkeeping is outstanding.
    """
    return not _ALIVE(int(pid))


def ledger_only_pids(plan_dir: Path) -> List[int]:
    """The pids the ledger still calls live — and that really are running.

    This is the list the negative controls judge by: what the ledger
    says this plan owns, intersected with what the kernel says is
    running.  Nothing here scans the machine.
    """
    return [
        int(pid)
        for pid in sorted(ledger.live_records(Path(plan_dir)))
        if not _process_is_gone(int(pid))
    ]


def _wait_until_gone(pids: Sequence[int], timeout: float = 30.0) -> bool:
    """Wait until every pid is gone.  True when they are."""
    deadline = time.monotonic() + timeout
    while True:
        if all(_process_is_gone(int(pid)) for pid in pids):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _wait_for_json(path: Path, timeout: float = 60.0) -> Dict[str, Any]:
    """Wait for a JSON file to appear and read it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        time.sleep(0.05)
    raise AssertionError("{} never appeared".format(path))


def _kill_process(proc: Optional[subprocess.Popen]) -> None:
    """Kill a child and collect it."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:  # noqa: BLE001 — cleanup must not fail the test
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _kill_pid(pid: int, group: bool = False) -> None:
    """Kill a pid, or the group it leads."""
    if pid <= 1:
        return
    try:
        if group:
            if os.getpgid(pid) != os.getpgrp():
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _stable_identity(pid: int, tries: int = 60, delay: float = 0.05):
    """Probe ``pid`` until two consecutive readings agree."""
    previous = None
    for _ in range(tries):
        ident = process_identity.probe(pid)
        if ident is not None:
            if previous is not None and ident.cmdline == previous.cmdline:
                return ident
            previous = ident
        time.sleep(delay)
    return previous


def _record_in_ledger(
    plan_dir: Path, proc: subprocess.Popen, *, role: str, plan_id: str
) -> ledger.ProcessRecord:
    """Write a spawn record for ``proc`` into the plan's ledger.

    This is what the backend does for the processes it starts, and what
    the test does for the one it starts itself.
    """
    identity = _stable_identity(proc.pid)
    assert identity is not None, "the test child has no readable identity"
    record = ledger.ProcessRecord(
        plan_id=plan_id,
        role=role,
        pid=proc.pid,
        pgid=identity.pgid or proc.pid,
        create_time=identity.create_time or 0.0,
        cmdline=list(identity.cmdline),
        cwd=identity.cwd or "",
        owner_pid=os.getpid(),
        owner_role="backend",
    )
    ledger.record_spawn(plan_dir, record)
    return record


def _write_node_config(
    fake_dir: Path,
    plan_dir: Path,
    plan_id: str,
    role: str,
    *,
    child: Optional[Dict[str, Any]] = None,
    transcript: str = "",
    lifetime: float = 0.0,
) -> Path:
    """Write the config of one node of the fake tree."""
    fake_dir.mkdir(parents=True, exist_ok=True)
    (fake_dir / "announce").mkdir(exist_ok=True)
    (fake_dir / "work").mkdir(exist_ok=True)
    payload = {
        "backend_dir": str(BACKEND_DIR),
        "node_script": str(fake_dir / "fake_node.py"),
        "plan_dir": str(plan_dir),
        "plan_id": plan_id,
        "cwd": str(fake_dir / "work" / role),
        "announce": str(fake_dir / "announce" / (role + ".json")),
        "transcript": transcript,
        "lifetime": lifetime,
        "stderr": str(fake_dir / (role + ".err")),
        "child": child,
    }
    path = fake_dir / ("node-%s.json" % role)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _spawn_node(
    fake_dir: Path, config_path: Path, role: str, marker: str
) -> subprocess.Popen:
    """Start one node of the fake tree."""
    err = open(fake_dir / (role + ".err"), "ab")
    proc = subprocess.Popen(
        [
            sys.executable, "-u", str(fake_dir / "fake_node.py"),
            str(config_path), marker, role,
        ],
        stdout=subprocess.DEVNULL,
        stderr=err,
        start_new_session=True,
        cwd=str(fake_dir / "work"),
    )
    return proc


def _spawn_executor_tree(
    fake_dir: Path,
    plan_dir: Path,
    plan_id: str,
    marker: str,
    *,
    with_children: bool = True,
    transcript: str = "",
) -> Tuple[subprocess.Popen, Path]:
    """Start the executor and, optionally, the tree under it.

    Returns the executor's process and the path of its announcement
    file.  The executor takes the plan's lock and installs the
    executor's own signal handlers, so it is the real thing in every
    respect but the work it does.
    """
    fake_dir.mkdir(parents=True, exist_ok=True)
    (fake_dir / "fake_node.py").write_text(_NODE_SOURCE, encoding="utf-8")

    child = None
    if with_children:
        tool_config = _write_node_config(
            fake_dir, plan_dir, plan_id, "tool",
        )
        subagent_config = _write_node_config(
            fake_dir, plan_dir, plan_id, "subagent",
            transcript=transcript,
            child={"role": "tool", "config": str(tool_config),
                   "detach": False},
        )
        child = {
            "role": "subagent",
            "config": str(subagent_config),
            "detach": True,
        }
    executor_config = _write_node_config(
        fake_dir, plan_dir, plan_id, "executor", child=child,
    )
    proc = _spawn_node(fake_dir, executor_config, "executor", marker)
    return proc, fake_dir / "announce" / "executor.json"


def _announce(fake_dir: Path, role: str, timeout: float = 60.0) -> Dict[str, Any]:
    """What a node of the tree said about itself."""
    return _wait_for_json(fake_dir / "announce" / (role + ".json"), timeout)


class FakeTree:
    """The fake backend and the tree it owns."""

    def __init__(
        self,
        backend: subprocess.Popen,
        plan_dir: Path,
        plan_id: str,
        marker: str,
        fake_dir: Path,
        ready: Dict[str, Any],
    ) -> None:
        self.backend = backend
        self.plan_dir = Path(plan_dir)
        self.plan_id = plan_id
        self.marker = marker
        self.fake_dir = Path(fake_dir)
        self.ready = ready

    def announce(self, role: str, timeout: float = 60.0) -> Dict[str, Any]:
        """What a node of the tree said about itself."""
        return _announce(self.fake_dir, role, timeout)

    def pids(self, timeout: float = 60.0) -> Dict[str, int]:
        """Every pid the tree is made of."""
        return {
            role: int(self.announce(role, timeout)["pid"])
            for role in _TREE_ROLES
        }

    def cleanup(self) -> None:
        """Kill every process of the tree and hand the resources back.

        The backend goes first: it is the one that could start
        something new.  The sub-agent's group goes with it, which is
        what takes the tool down.
        """
        pids: Dict[str, int] = {}
        try:
            pids = self.pids(timeout=5.0)
        except Exception:  # noqa: BLE001 — cleanup must not fail the test
            pass
        _kill_process(self.backend)
        for role, pid in pids.items():
            _kill_pid(pid, group=(role == "subagent"))
        for role, pid in pids.items():
            if role != "subagent":
                _kill_pid(pid)


def spawn_fake_tree(
    plans_dir: Path,
    plan_dir: Path,
    plan_id: str,
    *,
    marker: Optional[str] = None,
    transcript: Optional[Path] = None,
) -> FakeTree:
    """Start the fake backend and the tree it owns.

    The example in the brief: ``fake = spawn_fake_tree(plan_dir, plan_id)``.
    """
    marker = marker or _marker()
    fake_dir = Path(plan_dir) / "fake"
    fake_dir.mkdir(parents=True, exist_ok=True)
    (fake_dir / "work").mkdir(exist_ok=True)
    (fake_dir / "fake_node.py").write_text(_NODE_SOURCE, encoding="utf-8")
    (fake_dir / "fake_backend.py").write_text(
        _BACKEND_SOURCE, encoding="utf-8"
    )

    config = {
        "backend_dir": str(BACKEND_DIR),
        "node_script": str(fake_dir / "fake_node.py"),
        "plan_dir": str(plan_dir),
        "plan_id": plan_id,
        "plans_dir": str(plans_dir),
        "fake_dir": str(fake_dir),
        "announce_dir": str(fake_dir / "announce"),
        "work_dir": str(fake_dir / "work"),
        "transcript": str(transcript) if transcript else "",
        "stderr": str(fake_dir / "backend.err"),
        "mode": "serve",
    }
    config_path = fake_dir / ("tree-%s.json" % marker)
    config_path.write_text(json.dumps(config), encoding="utf-8")

    err = open(fake_dir / "backend.err", "ab")
    backend = subprocess.Popen(
        [
            sys.executable, "-u", str(fake_dir / "fake_backend.py"),
            str(config_path), marker,
        ],
        stdout=subprocess.DEVNULL,
        stderr=err,
        start_new_session=True,
        cwd=str(fake_dir / "work"),
    )
    ready = _wait_for_json(fake_dir / "ready.json")
    return FakeTree(backend, plan_dir, plan_id, marker, fake_dir, ready)


def run_sweeper(
    plans_dir: Path,
    plan_dir: Path,
    plan_id: str,
    *,
    marker: Optional[str] = None,
) -> subprocess.Popen:
    """A backend boot that only does what a boot does: sweep the plans.

    This is the process that collects what a killed backend left
    behind — the same call the real backend makes on startup.
    """
    marker = marker or _marker()
    fake_dir = Path(plan_dir) / "fake"
    fake_dir.mkdir(parents=True, exist_ok=True)
    (fake_dir / "work").mkdir(exist_ok=True)
    if not (fake_dir / "fake_backend.py").exists():
        (fake_dir / "fake_backend.py").write_text(
            _BACKEND_SOURCE, encoding="utf-8"
        )
    config = {
        "backend_dir": str(BACKEND_DIR),
        "node_script": str(fake_dir / "fake_node.py"),
        "plan_dir": str(plan_dir),
        "plan_id": plan_id,
        "plans_dir": str(plans_dir),
        "fake_dir": str(fake_dir),
        "announce_dir": str(fake_dir / "announce"),
        "work_dir": str(fake_dir / "work"),
        "transcript": "",
        "stderr": str(fake_dir / "sweeper.err"),
        "mode": "sweep",
    }
    config_path = fake_dir / ("sweep-%s.json" % marker)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    err = open(fake_dir / "sweeper.err", "ab")
    proc = subprocess.Popen(
        [
            sys.executable, "-u", str(fake_dir / "fake_backend.py"),
            str(config_path), marker,
        ],
        stdout=subprocess.DEVNULL,
        stderr=err,
        start_new_session=True,
        cwd=str(fake_dir / "work"),
    )
    proc.wait(timeout=120)
    assert proc.returncode == 0, (
        "the booting backend failed: {}".format(
            (fake_dir / "sweeper.err").read_text(encoding="utf-8")
        )
    )
    return proc


# ---------------------------------------------------------------------------
# the plan fixtures the two route-level controls need
# ---------------------------------------------------------------------------


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    plan_id: str,
    phase: str = "queued",
    exec_status: str = "running",
) -> Tuple[Path, Path]:
    """A plan with a tasks file, a phase and a run state.

    The default phase is ``queued`` — the waiting phase, and the one
    the start route accepts (``server._EXECUTION_START_SOURCE_PHASES``).
    ``ready`` is a retired input alias: a routing row parked there is
    refused with ``409 stage_mismatch`` before the case can test
    anything, so a seed naming it tests a path the product does not
    have.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "1", "title": "first task"}]}),
        encoding="utf-8",
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": phase,
            "completed_phases": [],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, phase)
        ExecutionRepository(conn).insert(
            plan_id=plan_id,
            project_dir=str(project_dir),
            exec_status=exec_status,
        )
    finally:
        conn.close()

    return plan_dir, project_dir


def _mark_running_without_handle(
    plan_id: str, project_dir: Path, pid: Optional[int] = None
) -> None:
    """The state a backend restart leaves behind: running, no handle.

    The handle died with the backend, and the ledger — not this dict —
    is what says which pids the plan still owns.
    """
    server._execution_state[plan_id] = {
        "status": "running",
        "logs": deque(maxlen=100),
        "project_dir": str(project_dir),
        "process": None,
        "started_at": "2026-10-30T04:20:00",
        "ended_at": None,
        "pid": pid,
        "stop_reason": None,
    }


class _NoThreads:
    """A stand-in for the ``threading`` module that starts nothing.

    The start route starts a watcher thread that waits for the
    executor and then writes the run's end into the state store.  In a
    test that outlives neither the executor nor the database, that
    thread is pure risk: it keeps writing after the test has finished.
    Only ``routes.execution``'s own reference is replaced, so the
    test's own threads are untouched.
    """

    class Thread:  # noqa: D401 - a stub, not a thread
        """A thread that never starts."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: Optional[float] = None) -> None:
            return None


@pytest.fixture
def plan_env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    server._execution_state.clear()
    yield plans_dir, db_path
    server._execution_state.clear()


@pytest.fixture
def client():
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# N1 — a killed backend leaves its tree behind, the next boot collects it
# ---------------------------------------------------------------------------


def test_n1_backend_sigkill_orphans_reaped_within_30s(tmp_path):
    """SIGKILL on the backend: the whole tree has to be gone within 30 s.

    SIGKILL leaves no chance to clean up — no handler, no exit event,
    no goodbye.  What is left is an executor, a sub-agent, the tool in
    the sub-agent's group and a VP service, all still running and all
    still owned by a plan that nobody is watching.

    The next backend boot sweeps the plans directory from the ledger,
    and that sweep is the only thing that can end this: the orphans
    have to be *gone*, not merely reported.
    """
    plan_id = _plan_id("n1")
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir()

    tree = spawn_fake_tree(plans_dir, plan_dir, plan_id)
    try:
        pids = tree.pids()
        assert set(pids) == set(_TREE_ROLES), pids
        for role, pid in pids.items():
            assert _ALIVE(pid), "the {} of the tree is not running".format(role)

        # Every fake process is recognisable in ``ps`` — and every
        # assertion below is made against the ledger's pids, never
        # against a scan of the machine.
        for role, pid in pids.items():
            identity = process_identity.probe(pid)
            assert identity is not None, "the {} has no identity".format(role)
            assert tree.marker in " ".join(identity.cmdline), (
                "the {} does not carry the fake marker: {}".format(
                    role, identity.cmdline
                )
            )

        # SIGKILL: no handler, no cleanup, no chance to write an exit.
        tree.backend.kill()
        tree.backend.wait(timeout=15)
        assert tree.backend.returncode == -signal.SIGKILL

        # The orphans are still there — that is the whole point.
        for role, pid in pids.items():
            assert _ALIVE(pid), "the {} died with the backend".format(role)

        # The next boot collects them.
        started = time.monotonic()
        run_sweeper(plans_dir, plan_dir, plan_id, marker=tree.marker)
        elapsed = time.monotonic() - started

        assert _wait_until_gone(list(pids.values()), 30), (
            "the orphans were still running after the sweep: {}".format(pids)
        )
        assert elapsed < 30, (
            "the sweep took {:.1f}s — the control says 30 seconds".format(
                elapsed
            )
        )
        assert ledger_only_pids(plan_dir) == [], (
            "the ledger still calls a dead process live"
        )
    finally:
        tree.cleanup()


def test_n1_subagent_transcript_stops_growing(tmp_path):
    """A sub-agent whose parent died stops writing once it is collected.

    The sub-agent writes a line every 0.2 s into its transcript.  While
    it runs, the file grows — that is the clock this control reads.

    Killing the executor (the sub-agent's parent) leaves the sub-agent
    running: every layer of PDT's tree detaches on purpose, so the
    parent's death is not the child's death.  What ends it is the
    collection — and the proof that the collection happened is that the
    transcript stopped growing.
    """
    plan_id = _plan_id("n1b")
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir()
    transcript = plan_dir / "subagent-transcript.log"

    tree = spawn_fake_tree(plans_dir, plan_dir, plan_id, transcript=transcript)
    try:
        pids = tree.pids()

        # The clock is running: the transcript grows.
        first = transcript.stat().st_size
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if transcript.stat().st_size > first:
                break
            time.sleep(0.1)
        growing = transcript.stat().st_size
        assert growing > first, (
            "the sub-agent never wrote to its transcript — the clock this "
            "control reads is not running"
        )

        # The sub-agent's parent dies.
        os.kill(pids["executor"], signal.SIGKILL)

        # The orphan is still running: the parent's death is not its own.
        assert _ALIVE(pids["subagent"]), (
            "the sub-agent died with its parent; the whole point of the "
            "ledger is that it does not"
        )

        # The collection is what stops it.
        run_sweeper(plans_dir, plan_dir, plan_id, marker=tree.marker)
        assert _wait_until_gone([pids["subagent"], pids["tool"]], 30), (
            "the orphaned sub-agent was not collected"
        )

        size_a = transcript.stat().st_size
        time.sleep(1.0)
        size_b = transcript.stat().st_size
        assert size_a == size_b, (
            "the transcript kept growing after the orphan was collected "
            "({} -> {})".format(size_a, size_b)
        )
        assert size_a > 0
    finally:
        tree.cleanup()


# ---------------------------------------------------------------------------
# N2 — a backend that is asked to stop says what it did
# ---------------------------------------------------------------------------


def test_n2_backend_sigterm_writes_named_events(tmp_path):
    """SIGTERM on the backend: the ledger names every process it ended.

    A sweep that kills without saying anything leaves an operator with
    a plan that stopped and no way to tell what was killed, what was
    skipped and what was left running.  So the named events have to
    carry the pid, the command line, the working directory, the role —
    and, for a process that ended on its own, how long it lived.
    """
    plan_id = _plan_id("n2")
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir()

    tree = spawn_fake_tree(plans_dir, plan_dir, plan_id)
    try:
        pids = tree.pids()
        short_tool_pid = int(tree.ready["short_tool_pid"])

        tree.backend.terminate()
        tree.backend.wait(timeout=60)
        assert tree.backend.returncode == 143, (
            "the backend did not answer SIGTERM with the executor's own "
            "exit code (got {})".format(tree.backend.returncode)
        )

        events = ledger.read_events(plan_dir)
        assert events, "the ledger says nothing at all"

        for role, pid in pids.items():
            named = [
                event for event in events
                if event.get("pid") == pid
                and event.get("event") in ("killed", "exit")
            ]
            assert named, (
                "the {} left no named event in the ledger".format(role)
            )
            event = named[-1]
            assert event.get("cmdline"), (
                "the {}'s event names no command line: {}".format(role, event)
            )
            assert event.get("cwd"), (
                "the {}'s event names no working directory: {}".format(
                    role, event
                )
            )
            assert event.get("role"), (
                "the {}'s event names no role: {}".format(role, event)
            )

        # The lifetime: the tool the backend owned ended on its own, and
        # the exit event says how long it lived.
        exits = [
            event for event in events
            if event.get("event") == "exit"
            and event.get("pid") == short_tool_pid
        ]
        assert exits, "the finished tool left no exit event"
        ended = exits[0]
        assert ended.get("cmdline"), ended
        assert ended.get("cwd"), ended
        assert ended.get("role") == "tool", ended
        assert ended.get("duration"), (
            "the exit event does not say how long the process lived: "
            "{}".format(ended)
        )
        assert float(ended["duration"]) > 0, ended
    finally:
        tree.cleanup()


# ---------------------------------------------------------------------------
# N3 — an executor that is stopped takes its children with it
# ---------------------------------------------------------------------------


def test_n3_executor_sigterm_kills_children_and_releases_lock(tmp_path):
    """SIGTERM on the executor: the children die and the lock comes back.

    The executor is the last process that knows the tree exists — every
    layer under it detaches into a session of its own.  So the executor's
    own SIGTERM handler has to collect the tree and hand the plan's
    single-instance lock back; a lock that outlives its holder locks the
    plan out for ever.
    """
    plan_id = _plan_id("n3")
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir()
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    fake_dir = plan_dir / "fake"
    marker = _marker()

    executor, _ = _spawn_executor_tree(fake_dir, plan_dir, plan_id, marker)
    try:
        subagent = _announce(fake_dir, "subagent")
        tool = _announce(fake_dir, "tool")
        children = [int(subagent["pid"]), int(tool["pid"])]
        for pid in children:
            assert _ALIVE(pid)

        assert executor_lock.is_held(plan_dir) is True, (
            "the executor never took the plan's lock"
        )

        executor.send_signal(signal.SIGTERM)
        executor.wait(timeout=60)
        assert executor.returncode == 143, (
            "the executor did not answer SIGTERM with 143 (got {})".format(
                executor.returncode
            )
        )

        assert _wait_until_gone(children, 30), (
            "the executor left its children running"
        )
        assert executor_lock.is_held(plan_dir) is False, (
            "the executor died without giving the plan's lock back"
        )
    finally:
        _kill_process(executor)
        for role in ("subagent", "tool"):
            try:
                pid = int(_announce(fake_dir, role, timeout=5)["pid"])
            except Exception:  # noqa: BLE001 — cleanup must not fail
                continue
            _kill_pid(pid, group=(role == "subagent"))
            _kill_pid(pid)


# ---------------------------------------------------------------------------
# N4 — a stop without a handle still finds the tree, and only stops it dead
# ---------------------------------------------------------------------------


def test_n4_stop_without_handle_uses_ledger(plan_env, client, monkeypatch):
    """The handle is gone; the ledger still says who to kill.

    This is the shape a backend restart leaves behind.  The old code
    marked the plan stopped, wrote a checkpoint, parked the phase — and
    killed nothing.

    And the answer has to mean something: "stopped" is only allowed
    once every process the plan owns is *confirmed* dead.  A process
    that survived turns the answer into ``kill_incomplete`` plus the
    pids that are still running.
    """
    plans_dir, db_path = plan_env
    plan_id = _plan_id("n4")
    plan_dir, project_dir = _seed_plan(
        plans_dir, db_path, plan_id=plan_id, phase="executing",
    )
    fake_dir = plan_dir / "fake"
    marker = _marker()

    executor, _ = _spawn_executor_tree(
        fake_dir, plan_dir, plan_id, marker, with_children=False,
    )
    try:
        _record_in_ledger(plan_dir, executor, role="executor",
                           plan_id=plan_id)
        _mark_running_without_handle(plan_id, project_dir, pid=executor.pid)

        resp = client.post("/api/execution/{}/stop".format(plan_id))

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "stopped", resp.json()
        assert _wait_until_gone([executor.pid], 30), (
            "the stop answered 'stopped' but the process the ledger "
            "records for this plan is still running"
        )
        assert ledger_only_pids(plan_dir) == [], (
            "the ledger still calls a dead process live"
        )

        # --- and "stopped" has to mean dead ---------------------------
        # A process that cannot be confirmed dead keeps the plan
        # running: the answer names it.
        plan_id2 = _plan_id("n4b")
        plan_dir2, project_dir2 = _seed_plan(
            plans_dir, db_path, plan_id=plan_id2, phase="executing",
        )
        fake_dir2 = plan_dir2 / "fake"
        executor2, _ = _spawn_executor_tree(
            fake_dir2, plan_dir2, plan_id2, _marker(), with_children=False,
        )
        try:
            _record_in_ledger(plan_dir2, executor2, role="executor",
                               plan_id=plan_id2)
            _mark_running_without_handle(
                plan_id2, project_dir2, pid=executor2.pid,
            )
            # Nothing survives SIGKILL for real, so the sweep is told
            # the process is still there.  That is what a survivor looks
            # like to the reaper.
            monkeypatch.setattr(process_reaper, "_alive", lambda pid: True)
            monkeypatch.setattr(process_reaper, "_KILL_GRACE_SECONDS", 0.05)
            monkeypatch.setattr(
                server, "_resolve_stop_grace_seconds",
                lambda grace_seconds=None: 0.05,
            )

            resp2 = client.post("/api/execution/{}/stop".format(plan_id2))

            assert resp2.status_code == 200, resp2.text
            body = resp2.json()
            assert body["status"] == "kill_incomplete", body
            assert body["surviving_pids"] == [executor2.pid], body
        finally:
            _kill_process(executor2)
            _kill_pid(executor2.pid)
    finally:
        _kill_process(executor)
        _kill_pid(executor.pid)


# ---------------------------------------------------------------------------
# N5 — one plan, one executor
# ---------------------------------------------------------------------------


def test_n5_second_start_409_and_manual_executor_already_running(
    plan_env, client, monkeypatch
):
    """Two starts at once: the second is refused.  A third is refused too.

    The guard is the ledger, so the refusal is only as good as the
    record: the executor that is already running has to be a *live*
    process in the ledger, or a second start would go through.

    And the guard at the door is the lock: an executor started by hand
    against a plan that is already running has to refuse to start, with
    its own exit code — not to fail halfway through a run.
    """
    plans_dir, db_path = plan_env
    plan_id = _plan_id("n5")
    plan_dir, project_dir = _seed_plan(
        plans_dir, db_path, plan_id=plan_id, phase="queued",
    )
    fake_dir = plan_dir / "fake"
    marker = _marker()
    spawned: List[subprocess.Popen] = []

    def _fake_spawn(**kwargs):
        """The executor the start would really spawn — a fake one.

        The real spawn would run the orchestrator, which is not what
        this control is about.  What matters is that a *real* process
        is started, recorded in the ledger, and still running when the
        second start arrives.
        """
        proc, _ = _spawn_executor_tree(
            fake_dir, plan_dir, plan_id, marker, with_children=False,
        )
        spawned.append(proc)
        server._routes_execution._ledger_write_spawn(
            plan_dir, plan_id, proc,
            [str(fake_dir / "fake_node.py")], "executor", fake_dir,
        )
        return proc, fake_dir / "executor.log"

    monkeypatch.setattr(server, "_spawn_executor_subprocess", _fake_spawn)
    # The watcher thread would keep writing into the state store after
    # the test has finished.
    monkeypatch.setattr(server._routes_execution, "threading", _NoThreads())

    try:
        # Two starts at the same time.
        barrier = threading.Barrier(2)
        responses: List[Any] = []

        def _start() -> None:
            barrier.wait(timeout=30)
            responses.append(
                client.post(
                    "/api/execution/{}/start".format(plan_id),
                    json={"project_dir": str(project_dir)},
                )
            )

        threads = [threading.Thread(target=_start) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=180)

        assert len(responses) == 2, (
            "the two starts did not both answer: {}".format(responses)
        )
        codes = sorted(response.status_code for response in responses)
        assert codes == [200, 409], (
            "two concurrent starts answered {} — one plan, one executor".format(
                codes
            )
        )
        assert len(spawned) == 1, (
            "two concurrent starts spawned {} executors".format(len(spawned))
        )

        refused = [r for r in responses if r.status_code == 409][0]
        assert refused.json().get("error") in (
            "already_running", "conflict",
        ), refused.json()

        # The ledger is what refuses the next one, by name.
        again = client.post(
            "/api/execution/{}/start".format(plan_id),
            json={"project_dir": str(project_dir)},
        )
        assert again.status_code == 409, again.text
        body = again.json()
        assert body["error"] == "already_running", body
        assert body["pids"] == [spawned[0].pid], body

        # --- the guard at the door: the lock --------------------------
        # An executor started by hand against a plan that is already
        # running refuses to start at all.
        cli = subprocess.run(
            [
                sys.executable, "-u", str(BACKEND_DIR / "cli.py"),
                "--recover",
                "-w", str(project_dir),
                "--tasks-file", str(plan_dir / "tasks.json"),
            ],
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert cli.returncode == 3, (
            "the second executor did not refuse to start: rc={} "
            "stderr={}".format(cli.returncode, cli.stderr[-2000:])
        )
        assert "already_running" in cli.stderr, cli.stderr[-2000:]
        assert "plan={}".format(plan_id) in cli.stderr, cli.stderr[-2000:]
    finally:
        for proc in spawned:
            _kill_process(proc)
            _kill_pid(proc.pid)


# ---------------------------------------------------------------------------
# the control on the controls: a stranger is never touched
# ---------------------------------------------------------------------------


def test_negative_controls_never_touch_foreign_processes(tmp_path):
    """A marked process the ledger does not know is never touched.

    The reaper decides by the ledger, not by what a process is called.
    This stranger carries the same marker in its command line as the
    fake tree, runs in the same test, and is not in anybody's ledger —
    so the sweep has to walk straight past it.
    """
    plan_id = _plan_id("foreign")
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir()
    marker = _marker()

    foreign = subprocess.Popen(
        [sys.executable, "-u", "-c", "import time; time.sleep(300)",
         marker],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(tmp_path),
    )
    tree = None
    try:
        assert _ALIVE(foreign.pid)
        identity = process_identity.probe(foreign.pid)
        assert identity is not None
        assert marker in " ".join(identity.cmdline), (
            "the control process does not carry the marker — the control "
            "would prove nothing"
        )

        tree = spawn_fake_tree(plans_dir, plan_dir, plan_id, marker=marker)
        pids = tree.pids()

        tree.backend.kill()
        tree.backend.wait(timeout=15)
        run_sweeper(plans_dir, plan_dir, plan_id, marker=marker)

        assert _wait_until_gone(list(pids.values()), 30), (
            "the tree was not collected"
        )
        assert _ALIVE(foreign.pid), (
            "the sweep killed a process the ledger never recorded"
        )
        assert foreign.poll() is None
    finally:
        if tree is not None:
            tree.cleanup()
        _kill_process(foreign)
        _kill_pid(foreign.pid)
