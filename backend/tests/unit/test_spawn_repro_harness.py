"""Pins for the spawn reproduction core.

What is under test
------------------
``backend/tests/manual/spawn_repro.py`` -- the harness that spawns a
child the way ``service_manager.start_service`` does, with three
candidate parameters switchable, and that decides readiness and tears
the tree down afterwards.

Why the harness is loaded by path
----------------------------------
``backend/tests/manual/`` is a namespace-package directory (PEP 420 --
no ``__init__.py``), so it is not an ordinary package to import.  The
module is loaded from its file, which makes the test independent of
which ``tests`` package the interpreter happens to resolve first.  The
module is also importable as ``tests.manual.spawn_repro`` for a caller
that wants it as a package.

The pins
--------
1. The spawn the harness performs is the spawn production performs --
   the same keyword arguments, in the same shape.  Without this, a
   difference in outcome could be a difference in the harness.
2. ``stdin_mode`` names exactly one :mod:`subprocess` constant.
3. A batch counts its ready and failed runs.
4. Stopping a tree leaves no process behind.
5. "The port answers" is not "the service is up": a dead child that
   left a listener behind is not a ready service.
6. A descriptor count that could not be taken is ``None``, not zero.
7. The child the harness starts does not inherit the descriptor
   variables -- the harness starts it the way production does, and
   production's wrapper takes those variables out of the environment
   it hands on.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

#: The backend package root -- ``<repo>/backend``.  ``service_manager``
#: and ``service_declaration`` live there.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

#: The harness under test.
_HARNESS_PATH = Path(__file__).resolve().parents[1] / "manual" / "spawn_repro.py"


def _load_harness() -> ModuleType:
    """Import the harness from its file."""
    spec = importlib.util.spec_from_file_location("spawn_repro", _HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["spawn_repro"] = module
    spec.loader.exec_module(module)
    return module


spawn_repro = _load_harness()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _listen_after(port: int, delay: float, stop: threading.Event) -> threading.Thread:
    """Answer on *port*, but only starting *delay* seconds from now.

    The delay is what makes the readiness test deterministic: the
    listener must not be answering before the child has died, or the
    "port answers but the child is gone" shape cannot be built.
    """
    def _serve() -> None:
        if stop.wait(delay):
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(8)
            sock.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                conn.close()
        finally:
            sock.close()

    thread = threading.Thread(target=_serve, name="spawn-repro-listener",
                              daemon=True)
    thread.start()
    return thread


@pytest.fixture
def delayed_listener():
    """Yield a factory that starts listeners, and stop them afterwards.

    A thread this application starts may not outlive the test that
    started it, so the listeners are stopped and joined on the way out.
    """
    started: list = []

    def _start(port: int, delay: float) -> threading.Thread:
        stop = threading.Event()
        thread = _listen_after(port, delay, stop)
        started.append((stop, thread))
        return thread

    yield _start

    for stop, _ in started:
        stop.set()
    for _, thread in started:
        thread.join(timeout=5)


def _config(tmp_path: Path, **overrides):
    """A spawn config rooted in *tmp_path*."""
    params = {
        "start_cmd": "/bin/bash -lc true",
        "port": _free_port(),
        "cwd": tmp_path,
        "log_path": tmp_path / "svc.log",
        "ready_timeout": 2.0,
    }
    params.update(overrides)
    return spawn_repro.SpawnConfig(**params)


# ---------------------------------------------------------------------------
# 1. The spawn mirrors production
# ---------------------------------------------------------------------------


def test_popen_kwargs_mirror_service_manager(tmp_path, monkeypatch):
    """The harness must spawn the way ``service_manager`` spawns.

    Both sides are run against one fake ``Popen``, so the two sets of
    keyword arguments are compared directly rather than against a
    description of what they ought to be.

    The comparison covers the whole argument list and the whole set of
    keyword arguments: a harness that adds a keyword production does
    not pass changes the spawn, and so does one that drops one.  The
    single exception is ``close_fds``, which production leaves at the
    subprocess default and the harness spells out so that the matrix
    can turn it off.
    """
    from service_declaration import ServiceDeclaration
    import service_manager

    captured = []

    class _FakePopen:
        def __init__(self, args, **kwargs):
            captured.append((args, kwargs))
            self.args = args
            self.kwargs = kwargs
            self.pid = os.getpid()
            self.returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    # The readiness probe and the exit check are not what this test is
    # about, and both of them would otherwise spend real seconds.
    monkeypatch.setattr(
        service_manager, "wait_until_ready",
        lambda decl, timeout_seconds=None: True,
    )
    monkeypatch.setattr(
        service_manager, "_await_exit", lambda proc, timeout: (False, None),
    )

    decl = ServiceDeclaration(
        name="repro", port=_free_port(), start_cmd="true", cwd=".",
    )
    service_manager.start_service(decl, tmp_path)
    assert len(captured) == 1, "production did not spawn"
    prod_args, prod_kwargs = captured[0]

    cfg = spawn_repro.SpawnConfig(
        start_cmd=decl.start_cmd,
        port=decl.port,
        cwd=tmp_path,
        log_path=tmp_path / "harness.log",
        env_extra=service_manager.service_env(decl),
    )
    spawn_repro.spawn_child(cfg)
    assert len(captured) == 2, "the harness did not spawn"
    harness_args, harness_kwargs = captured[1]

    assert harness_args == prod_args
    assert set(harness_kwargs) - set(prod_kwargs) == {"close_fds"}
    assert not set(prod_kwargs) - set(harness_kwargs)
    assert harness_kwargs["cwd"] == prod_kwargs["cwd"]
    assert harness_kwargs["env"] == prod_kwargs["env"]
    assert harness_kwargs["stderr"] is prod_kwargs["stderr"]
    assert harness_kwargs["stdin"] is prod_kwargs["stdin"]
    assert harness_kwargs["stdout"].mode == prod_kwargs["stdout"].mode == "ab"
    # The session is the wrapper's business: production's wrapper calls
    # ``setsid()`` itself, so neither side asks ``Popen`` for a session
    # -- and a child that is already a group leader gets ``EPERM`` from
    # its own ``setsid()``.
    assert "start_new_session" not in prod_kwargs
    assert "start_new_session" not in harness_kwargs


# ---------------------------------------------------------------------------
# 2. stdin_mode names one constant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("devnull", subprocess.DEVNULL),
        ("inherit", None),
        ("pipe", subprocess.PIPE),
    ],
)
def test_stdin_mode_maps_to_subprocess_constant(mode, expected):
    """Each mode maps to the constant it names -- and to no other."""
    assert spawn_repro.stdin_constant(mode) == expected


def test_unknown_stdin_mode_is_refused():
    """A mode nobody defined is an error, not a silent default."""
    with pytest.raises(ValueError):
        spawn_repro.stdin_constant("closed")


# ---------------------------------------------------------------------------
# 3. A batch counts what happened
# ---------------------------------------------------------------------------


def test_batch_counts_ready_and_failed(tmp_path, monkeypatch):
    """Three ready and seven not ready are counted as three and seven."""
    calls = []

    def _fake_run_once(cfg):
        calls.append(cfg)
        ready = len(calls) <= 3
        return spawn_repro.RunResult(
            ready=ready, pid=len(calls), pgid=len(calls),
            rc=0 if ready else 1, fd_count=3, duration=0.01, log_tail="",
        )

    monkeypatch.setattr(spawn_repro, "run_once", _fake_run_once)
    cfg = _config(tmp_path)

    batch = spawn_repro.run_batch(cfg, 10)

    assert batch.runs == 10
    assert batch.ready == 3
    assert batch.failed == 7
    assert len(batch.results) == 10
    assert sum(1 for result in batch.results if result.ready) == 3


# ---------------------------------------------------------------------------
# 4. Stopping a tree leaves nothing behind
# ---------------------------------------------------------------------------


def test_stop_tree_terminates_group():
    """A stopped tree is gone -- not signalled, gone."""
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        pgid = os.getpgid(proc.pid)
        assert pgid != os.getpgid(0), "the child shares our group"

        spawn_repro.stop_tree(proc.pid, pgid)

        assert proc.wait(timeout=5) is not None
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)
    finally:
        if proc.poll() is None:
            spawn_repro.stop_tree(proc.pid, os.getpgid(proc.pid), grace=0.1)
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# 5. Ready means alive, not merely answering
# ---------------------------------------------------------------------------


def test_ready_requires_live_process(tmp_path, delayed_listener):
    """A port that answers while our child is dead is not a ready service."""
    port = _free_port()
    # The listener starts answering only after the child has had time to
    # die, so the port is genuinely answering by the time it is probed.
    delayed_listener(port, delay=0.25)

    cfg = _config(
        tmp_path,
        start_cmd='/bin/bash -lc "exit 0"',
        port=port,
        ready_timeout=5.0,
    )

    result = spawn_repro.run_once(cfg)

    assert result.ready is False
    assert result.rc is not None, "the child was supposed to have exited"


# ---------------------------------------------------------------------------
# 6. A descriptor count that could not be taken is None
# ---------------------------------------------------------------------------


def test_count_fds_returns_none_when_lsof_missing(monkeypatch):
    """No ``lsof`` means "not measurable", not "zero descriptors"."""
    monkeypatch.setattr(
        spawn_repro, "LSOF_BIN", "/nonexistent/lsof-that-is-not-installed",
    )
    assert spawn_repro.count_fds(os.getpid()) is None


def test_count_fds_counts_a_live_process():
    """A process that exists has a positive descriptor count."""
    count = spawn_repro.count_fds(os.getpid())
    assert count is None or count > 0


# ---------------------------------------------------------------------------
# 7. The child does not inherit the descriptor variables
# ---------------------------------------------------------------------------


def test_child_environment_has_no_descriptor_variables(tmp_path, monkeypatch):
    """The child the harness starts inherits no descriptor variable.

    A ``PDT_SECRET_FD_*`` variable names a descriptor number, and that
    number means something in the process that published it and nothing
    in the child: the child closes it, and it takes the child's own
    event loop down with it.  Production's wrapper takes those
    variables out of the environment it hands on, and the harness has
    to hand over the environment production hands over -- otherwise the
    harness measures a shape that does not exist.

    The variable is put in the environment first, so that "the child's
    environment has no descriptor variable in it" is a statement about
    the spawn and not about a hazard that was never there.
    """
    import service_manager

    name = f"{service_manager._SECRET_FD_ENV_PREFIX}repro_harness"
    monkeypatch.setenv(name, "3")
    assert name in os.environ, "the hazard is not in the environment"

    dump = tmp_path / "child_env.txt"
    script = tmp_path / "dump_env.py"
    script.write_text(
        "import os, sys\n"
        "with open(sys.argv[1], 'w') as fp:\n"
        "    fp.write('\\n'.join(sorted(os.environ)))\n",
        encoding="utf-8",
    )
    cfg = _config(tmp_path, start_cmd=f"{sys.executable} {script} {dump}")

    proc = spawn_repro.spawn_child(cfg)
    proc.wait(timeout=30)

    assert dump.is_file(), "the child wrote no environment"
    child_env = dump.read_text(encoding="utf-8").splitlines()
    assert child_env, "the child's environment is empty"
    assert name not in child_env, (
        "the child inherited the descriptor variable: the harness does "
        "not start the child the way production starts it"
    )
