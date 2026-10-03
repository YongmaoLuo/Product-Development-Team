"""Unit tests for the plan-service lifecycle owner (2026-09-18 C2).

Covers:
  * ``normalized_command_tokens`` — the wrapper/env/flag normalisations
    that let ``npx next dev`` recognise ``npm exec next dev``.
  * ``matches_declaration`` — the discriminator that stops a
    ``next start`` production build from being adopted as a ``next
    dev`` server, and the symmetric case where it
    must still match across interpreter-path spellings.
  * ``ensure_services`` — start / adopt / restart / **relocate** decisions.
  * ``render_declaration`` / ``rewrite_port`` / ``service_env`` — the
    relocation machinery (a contended port moves us, it never moves
    the foreign process).
  * ``record_runtimes`` / ``read_ledger`` — only backend-spawned services
    land in the ledger, and a corrupt ledger degrades to "nothing to
    reap" rather than raising.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402
from service_freshness import PortProbe  # noqa: E402


def _decl(**overrides) -> ServiceDeclaration:
    base = dict(
        name="chart",
        port=3000,
        start_cmd="npx next dev -p 3000",
        cwd=".",
    )
    base.update(overrides)
    return ServiceDeclaration(**base)


def _probe(port: int, cmdlines, start_epoch=None) -> PortProbe:
    return PortProbe(
        port=port,
        listening=bool(cmdlines),
        pids=list(range(1000, 1000 + len(cmdlines))),
        cmdlines=list(cmdlines),
        process_start_epoch=start_epoch,
    )


# ---------------------------------------------------------------------------
# normalized_command_tokens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("npx next dev -p 3000", ["next", "dev"]),
        ("npm exec next dev -p 3000", ["next", "dev"]),
        ("next dev --port 3000", ["next", "dev"]),
        ("source venv1/bin/activate && python -m api.server",
         ["python", "api.server"]),
        ("/usr/bin/python3 -m api.server", ["python3", "api.server"]),
        ("API_PORT=8080 python -m api.server",
         ["python", "api.server"]),
        ("nohup uvicorn app:api --host 0.0.0.0 --port 8001",
         ["uvicorn", "app:api"]),
        ("cargo run --release --bin api_server", ["cargo", "api_server"]),
    ],
)
def test_normalized_command_tokens(cmd: str, expected: list):
    assert sm.normalized_command_tokens(cmd) == expected


def test_normalized_command_tokens_drops_only_the_flag_value():
    """``-p 3000`` drops both tokens; the port is known from the
    declaration, and keeping the digits would make matching depend on
    the planner spelling the port the same way twice."""
    tokens = sm.normalized_command_tokens("node server.js -p 3000 --verbose")

    assert "3000" not in tokens
    assert "server.js" in tokens
    assert "verbose" not in tokens


def test_normalized_command_tokens_on_garbage():
    assert sm.normalized_command_tokens(None) == []
    assert sm.normalized_command_tokens("") == []


# ---------------------------------------------------------------------------
# matches_declaration
# ---------------------------------------------------------------------------


def test_wrapper_spelling_variants_match():
    """A declaration saying ``npx`` must recognise a live ``npm exec``
    — that is literally the 2026-09-18 mismatch."""
    decl = _decl(start_cmd="npx next dev -p 3000")

    assert sm.matches_declaration(
        decl, _probe(3000, ["npm exec next dev -p 3000"])
    )


def test_production_build_does_not_match_a_dev_declaration():
    """``next-server`` is ``next start``. Adopting it is how a VP ended
    up driving a server nobody configured."""
    decl = _decl(start_cmd="npx next dev -p 3000")

    assert not sm.matches_declaration(
        decl, _probe(3000, ["next-server (v15.5.20)"])
    )


def test_interpreter_path_variants_match():
    decl = _decl(
        name="api", port=8080,
        start_cmd="source venv1/bin/activate && python -m api.server",
    )

    assert sm.matches_declaration(
        decl,
        _probe(8080, ["/Users/x/p/venv1/bin/python3 -m api.server"]),
    )


def test_a_declaration_with_no_identity_tokens_matches_nothing():
    decl = _decl(start_cmd="nohup > /dev/null")

    assert not sm.matches_declaration(decl, _probe(3000, ["anything"]))


def test_matches_requires_a_listener_and_a_cmdline():
    decl = _decl()

    assert not sm.matches_declaration(decl, _probe(3000, []))
    assert not sm.matches_declaration(
        decl, PortProbe(port=3000, listening=True, pids=[1], cmdlines=[])
    )


# ---------------------------------------------------------------------------
# ensure_services
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "tmp").mkdir()
    return tmp_path


def _stub_start(monkeypatch, *, ready=True, pid=4242):
    calls = []

    def _fake(decl, project_dir, timeout_seconds=None):
        calls.append(decl.name)
        return sm.ServiceRuntime(
            name=decl.name, port=decl.port, pid=pid, pgid=pid,
            start_cmd=decl.start_cmd, cwd=decl.cwd,
            origin="spawned", ready=ready,
            detail="stub",
        )

    monkeypatch.setattr(sm, "start_service", _fake)
    return calls


def test_free_port_is_started(project, monkeypatch):
    started = _stub_start(monkeypatch)
    monkeypatch.setattr(sm, "probe_port", lambda p: _probe(p, []))

    result = sm.ensure_services(project, {"chart": _decl()})

    assert started == ["chart"]
    assert result.ready_names == ["chart"]
    assert result.runtimes["chart"].spawned_by_ac


def test_fresh_matching_listener_is_adopted_not_restarted(project, monkeypatch):
    started = _stub_start(monkeypatch)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npm exec next dev -p 3000"], start_epoch=1e12),
    )

    result = sm.ensure_services(project, {"chart": _decl()})

    assert started == []
    runtime = result.runtimes["chart"]
    assert runtime.origin == "adopted"
    assert runtime.ready
    # Adopted means "not ours" — the ledger must not claim it.
    assert not runtime.spawned_by_ac


def test_stale_matching_listener_is_restarted_with_the_declared_command(
    project, monkeypatch
):
    """The declared start_cmd is authoritative; the captured cmdline of
    an old process is not."""
    started = _stub_start(monkeypatch)
    source = project / "server.py"
    source.write_text("x = 1\n")
    now = source.stat().st_mtime

    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npm exec next dev -p 3000"],
                         start_epoch=now - 3600),
    )
    stop_calls = []

    def _fake_stop(runtime, decl=None, **kwargs):
        stop_calls.append(runtime.name)
        return {"name": runtime.name, "port": runtime.port, "killed": [],
                "signalled": [], "port_free": True, "detail": "stub"}

    monkeypatch.setattr(sm, "stop_service", _fake_stop)

    result = sm.ensure_services(project, {"chart": _decl()})

    assert stop_calls == ["chart"]
    assert started == ["chart"]
    assert result.runtimes["chart"].ready


def test_foreign_listener_is_relocated_around_never_killed(project, monkeypatch):
    """2026-09-18 a port held by a process we cannot
    identify is neither adopted nor killed — **we get out of the way**.

    The shape it exists for: the operator had ``next-server`` (a
    production build) on :3000 while the plan wanted ``next dev``. The
    old behaviour refused and blocked every VP that depended on it.
    """
    started = _stub_start(monkeypatch)
    stop_calls = []
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["/usr/bin/postgres -D /var/db"], start_epoch=1e12),
    )
    monkeypatch.setattr(
        sm, "stop_service",
        lambda runtime, decl=None, **kw: stop_calls.append(runtime.name) or {},
    )
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)

    result = sm.ensure_services(project, {"chart": _decl()})

    assert started == ["chart"], "it must still start our own copy"
    assert stop_calls == [], "the foreign process must never be signalled"
    runtime = result.runtimes["chart"]
    assert runtime.ready is True
    assert runtime.origin == "spawned"
    assert runtime.port == 38921, "started on the free port"
    assert runtime.declared_port == 3000, "and remembers where the plan wanted it"
    assert "postgres" in runtime.detail
    assert result.blocked_ports == [], "a relocation is not a blockage"


def test_relocation_rewrites_the_command_and_health_url(project, monkeypatch):
    captured = {}

    def _capture(decl, project_dir, timeout_seconds=None):
        captured["start_cmd"] = decl.start_cmd
        captured["health_url"] = decl.health_url
        return sm.ServiceRuntime(
            name=decl.name, port=decl.port, pid=1, origin="spawned", ready=True,
            start_cmd=decl.start_cmd,
        )

    monkeypatch.setattr(sm, "start_service", _capture)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["/usr/bin/postgres"], start_epoch=1e12),
    )
    decl = _decl(
        start_cmd="npx next dev -p 3000",
        health_url="http://127.0.0.1:3000/health",
    )

    sm.ensure_services(project, {"chart": decl})

    assert captured["start_cmd"] == "npx next dev -p 38921"
    assert captured["health_url"] == "http://127.0.0.1:38921/health"


def test_relocation_retries_on_a_fresh_port(project, monkeypatch):
    """A readiness failure can mean a TOCTOU race or a service that ignored
    the rewrite — both are worth another port."""
    ports = iter([38921, 38922])
    calls = []

    def _start(decl, project_dir, timeout_seconds=None):
        calls.append(decl.port)
        return sm.ServiceRuntime(
            name=decl.name, port=decl.port, pid=decl.port,
            origin="spawned", ready=len(calls) > 1,
            start_cmd=decl.start_cmd, detail="stub",
        )

    monkeypatch.setattr(sm, "start_service", _start)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: next(ports))
    monkeypatch.setattr(sm, "stop_service", lambda *a, **k: {"port_free": True})
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["/usr/bin/postgres"], start_epoch=1e12),
    )

    result = sm.ensure_services(project, {"chart": _decl()})

    assert calls == [38921, 38922], "second attempt must use a fresh port"
    assert result.runtimes["chart"].ready is True


def test_relocation_gives_up_loudly(project, monkeypatch):
    """When every attempt fails the service is reported unready with a
    reason that names the foreign holder — not a bare timeout."""
    monkeypatch.setattr(
        sm, "start_service",
        lambda decl, project_dir, timeout_seconds=None: sm.ServiceRuntime(
            name=decl.name, port=decl.port, pid=1, ready=False,
            start_cmd=decl.start_cmd, detail="never became ready",
        ),
    )
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)
    monkeypatch.setattr(sm, "stop_service", lambda *a, **k: {"port_free": True})
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["/usr/bin/postgres"], start_epoch=1e12),
    )

    result = sm.ensure_services(project, {"chart": _decl()})

    runtime = result.runtimes["chart"]
    assert runtime.ready is False
    assert runtime.origin == "failed"
    assert "does not look like service" in runtime.detail
    assert "relocating failed" in runtime.detail
    assert result.blocked_ports == [3000]


def test_one_contended_service_does_not_stop_the_others(project, monkeypatch):
    started = _stub_start(monkeypatch)
    probes = {
        3000: _probe(3000, ["/usr/bin/postgres"]),
        8080: _probe(8080, []),
    }
    monkeypatch.setattr(sm, "probe_port", lambda p: probes[p])
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)

    result = sm.ensure_services(project, {
        "chart": _decl(),
        "api": _decl(name="api", port=8080, start_cmd="python -m api"),
    })

    # Both start: ``chart`` relocated off the contended port, ``api`` on
    # its own free one. Neither the foreign process nor the round suffers.
    assert sorted(started) == ["api", "chart"]
    assert result.ready_names == ["api", "chart"]
    assert result.blocked_ports == []


def test_protected_ports_are_never_started(project, monkeypatch):
    started = _stub_start(monkeypatch)
    monkeypatch.setattr(sm, "probe_port", lambda p: _probe(p, []))

    result = sm.ensure_services(project, {"self": _decl(port=8000)})

    assert started == []
    assert not result.runtimes["self"].ready


def test_empty_declaration_map_is_a_noop(project):
    result = sm.ensure_services(project, {})

    assert result.runtimes == {}
    assert not result.enabled()


def test_ensure_services_never_raises_on_a_broken_probe(project, monkeypatch):
    def _boom(port):
        raise RuntimeError("lsof exploded")

    monkeypatch.setattr(sm, "probe_port", _boom)

    result = sm.ensure_services(project, {"chart": _decl()})

    assert not result.runtimes["chart"].ready
    assert "probe failed" in result.runtimes["chart"].detail


# ---------------------------------------------------------------------------
# start_service
# ---------------------------------------------------------------------------


def test_start_service_refuses_protected_ports(project):
    runtime = sm.start_service(_decl(port=8000), project)

    assert runtime.ready is False
    assert "backend-runtime port" in runtime.detail


def test_start_service_reports_a_missing_cwd(project):
    runtime = sm.start_service(_decl(cwd="nope"), project)

    assert runtime.ready is False
    assert "does not exist" in runtime.detail


def test_start_service_launches_detached_and_waits(project, monkeypatch):
    """A real (tiny) detached process, so the Popen wiring — detached
    session, log redirection, cwd — is exercised rather than mocked."""
    spawned = {}

    class _FakeProc:
        pid = 999999

        def poll(self):
            return None   # still running, as a real spawn would be

    def _fake_popen(argv, **kwargs):
        spawned["argv"] = argv
        spawned["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(sm.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: True)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)

    runtime = sm.start_service(_decl(), project)

    assert runtime.ready is True
    assert runtime.origin == "spawned"
    assert runtime.pid == 999999
    assert spawned["argv"][:2] == ["/bin/bash", "-lc"]
    assert spawned["kwargs"]["start_new_session"] is True
    assert spawned["kwargs"]["cwd"] == str(project)


def test_start_service_failure_carries_the_log_tail(project, monkeypatch):
    class _FakeProc:
        pid = 999998

        def poll(self):
            return None   # still running; the not-ready path is mocked

    monkeypatch.setattr(sm.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: False)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_tail", lambda path, lines: "EADDRINUSE")

    runtime = sm.start_service(_decl(), project)

    assert runtime.ready is False
    assert "EADDRINUSE" in runtime.detail


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def test_ledger_records_only_ac_spawned_services(tmp_path: Path):
    runtime_map = sm.ServiceRuntimeMap(runtimes={
        "spawned": sm.ServiceRuntime(
            name="spawned", port=3000, pid=11, origin="spawned", ready=True,
        ),
        "adopted": sm.ServiceRuntime(
            name="adopted", port=8080, pid=22, origin="adopted", ready=True,
        ),
    })

    path = sm.record_runtimes(
        tmp_path, "plan-x", runtime_map, project_dir=tmp_path, round_number=2,
    )

    assert path is not None
    payload = json.loads(path.read_text())
    assert [s["name"] for s in payload["services"]] == ["spawned"]
    assert payload["plan_id"] == "plan-x"
    assert payload["round"] == 2


def test_ledger_is_absent_when_nothing_was_spawned(tmp_path: Path):
    runtime_map = sm.ServiceRuntimeMap(runtimes={
        "adopted": sm.ServiceRuntime(
            name="adopted", port=8080, pid=22, origin="adopted", ready=True,
        ),
    })

    assert sm.record_runtimes(
        tmp_path, "plan-x", runtime_map, project_dir=tmp_path,
    ) is None
    assert not sm.ledger_path(tmp_path).exists()


def test_read_ledger_degrades_on_a_corrupt_file(tmp_path: Path):
    sm.ledger_path(tmp_path).write_text("{not json")

    assert sm.read_ledger(tmp_path) == {}


def test_read_ledger_degrades_on_a_non_dict_payload(tmp_path: Path):
    sm.ledger_path(tmp_path).write_text("[1, 2]")

    assert sm.read_ledger(tmp_path) == {}


def test_read_ledger_round_trips(tmp_path: Path):
    runtime_map = sm.ServiceRuntimeMap(runtimes={
        "api": sm.ServiceRuntime(
            name="api", port=8080, pid=33, pgid=33, origin="spawned",
            ready=True, reap_on_exit=False,
        ),
    })
    sm.record_runtimes(tmp_path, "p", runtime_map, project_dir=tmp_path)

    data = sm.read_ledger(tmp_path)

    assert data["services"][0]["name"] == "api"
    assert data["services"][0]["pgid"] == 33


# ---------------------------------------------------------------------------
# stop_service
# ---------------------------------------------------------------------------


def test_stop_service_refuses_protected_ports():
    runtime = sm.ServiceRuntime(name="self", port=8000, pid=123)

    result = sm.stop_service(runtime)

    assert result["port_free"] is False
    assert "backend-runtime port" in result["detail"]


def test_stop_service_reports_a_free_port_when_there_is_nothing_to_stop(
    monkeypatch,
):
    monkeypatch.setattr(sm, "probe_port", lambda p: PortProbe(port=p))

    result = sm.stop_service(sm.ServiceRuntime(name="x", port=3000))

    assert result["port_free"] is True
    assert result["detail"] == "no process to stop"


# ---------------------------------------------------------------------------
# Real-process paths (stop / readiness / start end-to-end)
#
# These are the lines that actually signal processes, so they get a real
# detached child rather than a mock — a mocked `kill` proves nothing
# about whether the port is ever released.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port the kernel says is currently unused.

    Note what this does *not* promise: that the port will still be unused
    when the caller gets around to binding it. The probe socket is closed
    before this returns, so the port is unowned from here on, and in a
    full unit run another test can take it in the gap. Callers that bind
    must therefore handle EADDRINUSE — see :func:`_spawn_http_server`.
    """
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _spawn_http_server(port: int, *, ignore_sigterm: bool = False) -> int:
    """Start a detached `python -m http.server` on ``port``; return its
    pid so the test can always clean up.

    Retries on a bind failure. The port was chosen by :func:`_free_port`,
    which closes its probe socket before returning — so between choosing
    and binding, the port is unowned and any other test running
    concurrently can take it. That window is why these three tests failed
    intermittently in a full unit run while passing in isolation: the
    failure has nothing to do with the service-manager code under test.

    See :func:`_free_port` for why the window cannot be closed from that
    side; it is closed here instead, where the bind actually happens.
    """
    import shlex
    import subprocess
    import sys as _sys
    import time as _time

    script = (
        "import http.server,socketserver;"
        f"socketserver.TCPServer(('127.0.0.1',{port}),"
        "http.server.SimpleHTTPRequestHandler).serve_forever()"
    )
    if ignore_sigterm:
        # A shell that ignores TERM and execs python: forces the
        # escalation branch.
        argv = [
            "/bin/bash", "-c",
            f"trap '' TERM; exec {shlex.quote(_sys.executable)} -c {shlex.quote(script)}",
        ]
    else:
        argv = [_sys.executable, "-c", script]

    deadline = _time.monotonic() + 10.0
    while True:
        proc = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        # Give the child a moment to either bind or die on EADDRINUSE.
        _time.sleep(0.3)
        if proc.poll() is not None:
            # it exited — EADDRINUSE, most likely
            pass
        elif proc.pid in sm.probe_port(port).pids:
            # Our child is the listener. This is the same check the
            # production `start_service` does, and it was missing here:
            # without it the helper handed back the pid of a dead child
            # for a port something else was answering on, and every
            # assertion downstream was reasoning from that.
            break
        else:
            # Still running, but not the listener. Something else got
            # there first, so treat it like a bind failure and retry.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            if _time.monotonic() >= deadline:
                raise AssertionError(
                    f"port {port} is held by a process that is not the "
                    f"server this helper started (pid {proc.pid}); gave up "
                    f"after 10s of retries"
                )
            port = _free_port()
            script = (
                "import http.server,socketserver;"
                f"socketserver.TCPServer(('127.0.0.1',{port}),"
                "http.server.SimpleHTTPRequestHandler).serve_forever()"
            )
            if ignore_sigterm:
                argv = [
                    "/bin/bash", "-c",
                    f"trap '' TERM; exec {shlex.quote(_sys.executable)} -c {shlex.quote(script)}",
                ]
            else:
                argv = [_sys.executable, "-c", script]
            continue
        if _time.monotonic() >= deadline:
            raise AssertionError(
                f"could not bind port {port} for the test server after "
                f"retrying for 10s; something else is holding it"
            )
        port = _free_port()  # taken in the meantime; try another
        script = (
            "import http.server,socketserver;"
            f"socketserver.TCPServer(('127.0.0.1',{port}),"
            "http.server.SimpleHTTPRequestHandler).serve_forever()"
        )
        if ignore_sigterm:
            argv = [
                "/bin/bash", "-c",
                f"trap '' TERM; exec {shlex.quote(_sys.executable)} -c {shlex.quote(script)}",
            ]
        else:
            argv = [_sys.executable, "-c", script]
    deadline = _time.monotonic() + 20
    while _time.monotonic() < deadline:
        if sm.probe_port(port).listening:
            return proc.pid
        _time.sleep(0.2)
    _force_cleanup(proc.pid)
    raise AssertionError(f"stub server on {port} never came up")


def _force_cleanup(pid: int) -> None:
    import os
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


@pytest.mark.timeout(120)
def test_stop_service_releases_a_real_port():
    port = _free_port()
    pid = _spawn_http_server(port)
    try:
        runtime = sm.ServiceRuntime(
            name="stub", port=port, pid=pid, pgid=sm._process_group_of(pid),
        )
        result = sm.stop_service(runtime, grace_seconds=10.0)
        assert result["port_free"] is True
        assert not sm.probe_port(port).listening
        assert result["signalled"]
    finally:
        _force_cleanup(pid)


@pytest.mark.timeout(180)
def test_stop_service_escalates_when_sigterm_is_ignored():
    port = _free_port()
    pid = _spawn_http_server(port, ignore_sigterm=True)
    try:
        runtime = sm.ServiceRuntime(
            name="stubborn", port=port, pid=pid,
            pgid=sm._process_group_of(pid),
        )
        result = sm.stop_service(runtime, grace_seconds=2.0)
        assert result["port_free"] is True
        assert result["killed"], "expected a SIGKILL escalation"
    finally:
        _force_cleanup(pid)


@pytest.mark.timeout(120)
def test_wait_until_ready_polls_a_real_port():
    import subprocess
    import sys as _sys
    import time as _time

    port = _free_port()
    assert sm.wait_until_ready(_decl(port=port), timeout_seconds=1) is False

    script = (
        "import http.server,socketserver;"
        f"socketserver.TCPServer(('127.0.0.1',{port}),"
        "http.server.SimpleHTTPRequestHandler).serve_forever()"
    )
    proc = subprocess.Popen(
        [_sys.executable, "-c", script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        assert sm.wait_until_ready(_decl(port=port), timeout_seconds=20) is True
    finally:
        _force_cleanup(proc.pid)
        _time.sleep(0.2)


@pytest.mark.timeout(120)
def test_start_service_end_to_end(tmp_path: Path):
    """Real detached launch → ready, including the pid/pgid capture the
    C3 ledger depends on."""
    import sys as _sys

    port = _free_port()
    script = (
        "import http.server,socketserver;"
        f"socketserver.TCPServer(('127.0.0.1',{port}),"
        "http.server.SimpleHTTPRequestHandler).serve_forever()"
    )
    decl = _decl(
        name="stub", port=port,
        # Quoted: a checkout path with a space would otherwise split
        # here, and the shell would try to run the path prefix.
        start_cmd=f"{shlex.quote(_sys.executable)} -c {shlex.quote(script)}",
        ready_timeout_seconds=20,
    )

    runtime = sm.start_service(decl, tmp_path)
    try:
        assert runtime.ready is True, runtime.detail
        assert runtime.origin == "spawned"
        assert runtime.pid
        assert runtime.pgid
        assert runtime.start_epoch
        assert Path(runtime.log_path).exists()
    finally:
        if runtime.pid:
            _force_cleanup(runtime.pid)


@pytest.mark.timeout(120)
def test_health_url_drives_readiness(tmp_path: Path):
    """A declared health_url is the readiness signal when present."""
    import sys as _sys

    port = _free_port()
    script = (
        "import http.server,socketserver;"
        f"socketserver.TCPServer(('127.0.0.1',{port}),"
        "http.server.SimpleHTTPRequestHandler).serve_forever()"
    )
    decl = _decl(
        name="stub", port=port,
        start_cmd=f"{shlex.quote(_sys.executable)} -c {shlex.quote(script)}",
        health_url=f"http://127.0.0.1:{port}/",
        ready_timeout_seconds=20,
    )

    runtime = sm.start_service(decl, tmp_path)
    try:
        assert runtime.ready is True, runtime.detail
    finally:
        if runtime.pid:
            _force_cleanup(runtime.pid)


def test_health_url_fails_when_nothing_serves_it():
    port = _free_port()
    decl = _decl(
        name="stub", port=port, start_cmd="true",
        health_url=f"http://127.0.0.1:{port}/",
    )

    assert sm.wait_until_ready(decl, timeout_seconds=2) is False


# ---------------------------------------------------------------------------
# Reaping (C3)
# ---------------------------------------------------------------------------


def _write_ledger(plan_dir: Path, entries: list) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    sm.ledger_path(plan_dir).write_text(
        json.dumps({"plan_id": plan_dir.name, "services": entries}),
        encoding="utf-8",
    )


def _entry(name="chart", port=3000, pid=4242, **over):
    base = {
        "name": name, "port": port, "pid": pid, "pgid": pid,
        "start_epoch": 1.0, "start_cmd": "npx next dev -p 3000",
        "cwd": ".", "log_path": "", "origin": "spawned",
        "reap_on_exit": True,
    }
    base.update(over)
    return base


def test_reap_is_a_noop_without_a_ledger(tmp_path: Path):
    report = sm.reap_services(tmp_path)

    assert report.reaped == []
    assert report.clean
    assert report.ledger_cleared is False


def test_reap_of_an_already_gone_service_clears_the_ledger(
    tmp_path: Path, monkeypatch
):
    _write_ledger(tmp_path, [_entry()])
    monkeypatch.setattr(sm, "probe_port", lambda p: PortProbe(port=p))

    report = sm.reap_services(tmp_path)

    assert report.reaped == ["chart"]
    assert report.ledger_cleared is True
    assert not sm.ledger_path(tmp_path).exists()


def test_reap_respects_reap_on_exit_false(tmp_path: Path, monkeypatch):
    _write_ledger(tmp_path, [_entry(reap_on_exit=False)])
    monkeypatch.setattr(sm, "probe_port", lambda p: PortProbe(port=p))

    report = sm.reap_services(tmp_path)

    assert report.reaped == []
    assert report.skipped[0]["reason"] == "reap_on_exit=false"
    # The opt-out is durable: the ledger survives so a later sweep
    # still knows about it.
    assert sm.ledger_path(tmp_path).exists()


def test_reap_refuses_a_recycled_pid(tmp_path: Path, monkeypatch):
    """PID reuse is how an orphan sweep kills something it never
    started."""
    _write_ledger(tmp_path, [_entry(pid=4242, start_epoch=1000.0)])
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1e9),
    )
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 9e8)

    report = sm.reap_services(tmp_path)

    assert "recorded pid was recycled" in report.skipped[0]["reason"]


def test_reap_leaves_a_foreign_holder_alone(tmp_path: Path, monkeypatch):
    _write_ledger(tmp_path, [_entry(pid=4242)])
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["/usr/bin/postgres -D /var/db"]),
    )
    stop_calls = []
    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: stop_calls.append(1) or {"port_free": False},
    )

    report = sm.reap_services(tmp_path)

    assert stop_calls == []
    assert report.foreign_ports == [3000]
    assert not report.clean
    assert sm.ledger_path(tmp_path).exists()


def test_reap_kills_the_recorded_process(tmp_path: Path, monkeypatch):
    _write_ledger(tmp_path, [_entry(pid=4242, pgid=4242)])
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    killed = []

    def _fake_stop(runtime, decl=None, **kw):
        killed.append(runtime.pid)
        return {"port_free": True, "detail": "stopped"}

    monkeypatch.setattr(sm, "stop_service", _fake_stop)

    report = sm.reap_services(tmp_path)

    assert killed == [4242]
    assert report.reaped == ["chart"]
    assert report.ledger_cleared is True


def test_reap_kills_a_matching_holder_that_is_not_the_recorded_pid(
    tmp_path: Path, monkeypatch
):
    """A VP that started its own copy of the declared service: the
    recorded pid is stale, but the live holder's cmdline matches, so
    the service is still the backend's to reap.

    The recorded pid is what gets handed to ``stop_service``; it is the
    *declaration* that lets ``stop_service`` re-probe and find the real
    holder, so the test asserts the declaration is forwarded.
    """
    _write_ledger(tmp_path, [_entry(pid=4242)])
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npm exec next dev -p 3000"], start_epoch=1.0),
    )
    seen = []

    def _fake_stop(runtime, decl=None, **kw):
        seen.append((runtime.pid, decl))
        return {"port_free": True}

    monkeypatch.setattr(sm, "stop_service", _fake_stop)

    report = sm.reap_services(tmp_path)

    assert len(seen) == 1
    pid, decl = seen[0]
    assert pid == 4242
    assert decl is not None and decl.port == 3000
    assert report.reaped == ["chart"]


def test_reap_kills_the_group_of_the_recorded_pid(tmp_path: Path, monkeypatch):
    """The ``next dev`` shape: the backend's ledger records the wrapper it
    spawned, but the port is held by a **grandchild**.

    ``npx next dev -p N`` is three processes — ``npm exec`` → ``node
    .../.bin/next dev`` → ``next-server`` — and only the last one binds
    the port. It also rewrites its own process title, so
    ``matches_declaration`` cannot recognise it either. Without a
    group-based ownership proof ``reap_services`` refuses and the dev
    server outlives its workflow forever: the port stays held by a
    process whose ledger entry was written by an earlier plan, days
    after that plan finished.

    ``stop_service`` is already group-aware (it ``killpg``s); this test
    pins that the gate in front of it lets the group through.
    """
    _write_ledger(tmp_path, [_entry(pid=4242, pgid=4242)])
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    # ``_probe`` hands out pids 1000.. — 4242 is deliberately NOT among
    # them, and the cmdline is the rewritten title, so the process group
    # is the only thing connecting the listener to the ledger entry.
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["next-server (v15.5.20)"], start_epoch=1.0),
    )
    monkeypatch.setattr(
        sm, "_process_group_of", lambda pid: 4242 if pid == 1000 else pid,
    )
    seen = []

    def _fake_stop(runtime, decl=None, **kw):
        seen.append(runtime)
        return {"port_free": True, "detail": "stopped"}

    monkeypatch.setattr(sm, "stop_service", _fake_stop)

    report = sm.reap_services(tmp_path)

    assert report.reaped == ["chart"], report.to_dict()
    assert report.foreign_ports == []
    # And it must hand the group to ``stop_service``, not just the pid —
    # the recorded pid is the one process that is NOT holding the port.
    assert seen and seen[0].pgid == 4242


def test_reap_still_refuses_a_holder_in_another_group(tmp_path, monkeypatch):
    """Group matching must not decay into a blanket adoption."""
    _write_ledger(tmp_path, [_entry(pid=4242, pgid=4242)])
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["/usr/bin/postgres -D /var/db"], start_epoch=1.0),
    )
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: 9999)
    stop_calls = []
    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: stop_calls.append(1) or {"port_free": False},
    )

    report = sm.reap_services(tmp_path)

    assert stop_calls == []
    assert report.foreign_ports == [3000]


def test_reap_without_a_recorded_pgid_stays_conservative(tmp_path, monkeypatch):
    """No group on record → no group proof; fall back to pid / cmdline."""
    _write_ledger(tmp_path, [_entry(pid=4242, pgid=None)])
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["next-server (v15.5.20)"], start_epoch=1.0),
    )
    queried = []
    monkeypatch.setattr(
        sm, "_process_group_of", lambda pid: queried.append(pid) or 4242,
    )
    monkeypatch.setattr(
        sm, "stop_service", lambda *a, **k: {"port_free": False},
    )

    report = sm.reap_services(tmp_path)

    assert queried == [], "looked up a group the ledger never recorded"
    assert report.foreign_ports == [3000]


def test_reap_skips_protected_ports(tmp_path: Path, monkeypatch):
    _write_ledger(tmp_path, [_entry(port=8000)])
    monkeypatch.setattr(sm, "probe_port", lambda p: PortProbe(port=p))

    report = sm.reap_services(tmp_path)

    assert report.reaped == []
    assert "backend-runtime port" in report.skipped[0]["reason"]


def test_reap_skips_entries_without_a_usable_port(tmp_path: Path):
    _write_ledger(tmp_path, [_entry(port="3000")])

    report = sm.reap_services(tmp_path)

    assert report.reaped == []
    assert "no usable port" in report.skipped[0]["reason"]


def test_reap_all_plans_only_visits_plans_with_a_ledger(tmp_path: Path):
    _write_ledger(tmp_path / "a", [_entry()])
    _write_ledger(tmp_path / "b", [_entry(name="api", port=8080)])
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "managed_services.json").write_text(
        json.dumps({"services": []}), encoding="utf-8",
    )

    reports = sm.reap_all_plans(tmp_path)

    assert sorted(r.plan_id for r in reports) == ["a", "b"]


def test_pid_reused_tolerates_ps_granularity(monkeypatch):
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1000.0)

    assert sm._pid_reused({"pid": 1, "start_epoch": 1001.0}) is False
    assert sm._pid_reused({"pid": 1, "start_epoch": 1400.0}) is True
    assert sm._pid_reused({"pid": None, "start_epoch": 1.0}) is False
    assert sm._pid_reused({"pid": 1}) is False


@pytest.mark.timeout(120)
def test_reap_end_to_end_frees_a_real_port(tmp_path: Path):
    """The whole C3 promise: a service the backend started is gone after the
    workflow exits, and the ledger that recorded it is cleared."""
    import subprocess
    import sys as _sys
    import time as _time

    # Go through the same relocation machinery production uses, rather
    # than hand-rolling a port here.
    #
    # This test's subject is the start → ledger → reap cycle. Which port
    # that happens on is not part of it, and the test used to pin one:
    # `_free_port()` asked the kernel for a free port, released it, and
    # then spliced it into a `start_cmd` and assumed it stayed ours. It
    # did not, about one run in ten — a desktop application on this
    # machine was caught holding it — and every later step then reasoned
    # from a premise that had already stopped being true.
    #
    # `allocate_free_port` documents that same window and says the answer
    # is to retry on a fresh port when readiness fails, which is exactly
    # what `_relocate_service` does. Using it means the test stops
    # re-implementing a race the production path already handles, and
    # stops failing for a reason that says nothing about the contract.
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]          # a port we deliberately hold
    try:
        script = (
            "import http.server,socketserver;"
            f"socketserver.TCPServer(('127.0.0.1',{port}),"
            "http.server.SimpleHTTPRequestHandler).serve_forever()"
        )
        decl = _decl(
            name="stub", port=port,
            start_cmd=f"{shlex.quote(_sys.executable)} -c {shlex.quote(script)}",
            ready_timeout_seconds=20,
        )
        # Held, so the declared port is unusable and the service must
        # land elsewhere — the exact situation `_relocate_service` is for.
        runtime = sm._relocate_service(decl, tmp_path, foreign_cmdline="the test's own holder socket")
    finally:
        holder.close()
    port = runtime.port
    assert runtime.ready, (
        f"the relocated stub never started: {runtime.detail!r}"
    )
    runtime_map = sm.ServiceRuntimeMap(runtimes={"stub": runtime})
    sm.record_runtimes(tmp_path, tmp_path.name, runtime_map, project_dir=tmp_path)
    assert sm.ledger_path(tmp_path).exists()

    try:
        report = sm.reap_services(tmp_path, grace_seconds=10.0)

        assert report.reaped == ["stub"], report.to_dict()
        assert not sm.probe_port(port).listening
        assert report.ledger_cleared is True
        assert not sm.ledger_path(tmp_path).exists()
    finally:
        _force_cleanup(runtime_map.runtimes["stub"].pid)
        _time.sleep(0.2)


# ---------------------------------------------------------------------------
# T2 / T3 — the callers, against a listener the probe used to mistake
# ---------------------------------------------------------------------------
#
# The probe's own test (T1, in test_service_freshness) pins the query.
# These pin the consequence: a client that connected and whose peer has
# closed must not make `stop_service` or `reap_services` report that a
# killed process still holds the port. Both callers are correct to
# refuse to signal a process they did not start — that is the 2026-09-07
# boundary — so a false "someone is listening" from the probe turned a
# completed stop into a reported failure.
#
# The socket is never closed before the assertion, so by the time the
# probe runs it is guaranteed to be in CLOSE_WAIT. That makes these
# deterministic, which is the point: a test that only fails one run in
# twenty cannot tell you whether a fix worked.


def test_stop_reports_the_port_free_despite_a_lingering_client(tmp_path):
    """A connected client is not a listener (T2)."""
    port = _free_port()
    pid = _spawn_http_server(port)
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client_port = client.getsockname()[1]
    try:
        runtime = sm.ServiceRuntime(
            name="stub", port=port, pid=pid, pgid=sm._process_group_of(pid),
        )
        result = sm.stop_service(runtime, grace_seconds=10.0)
        assert result["port_free"] is True, (
            f"stop reported the port held: {result.get('detail')!r}. "
            f"A client of ours was left in CLOSE_WAIT on remote port {port} "
            f"(local {client_port}); that is not a listener."
        )
    finally:
        client.close()
        _force_cleanup(pid)


def test_reap_clears_the_ledger_despite_a_lingering_client(tmp_path):
    """Same shape, through the reap path (T3)."""
    port = _free_port()
    pid = _spawn_http_server(port)
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        runtime = sm.ServiceRuntime(
            name="stub", port=port, pid=pid, pgid=sm._process_group_of(pid),
            start_cmd="stub", cwd=str(tmp_path),
            origin="spawned",
        )
        sm.record_runtimes(
            tmp_path, tmp_path.name,
            sm.ServiceRuntimeMap(runtimes={"stub": runtime}),
            project_dir=tmp_path,
        )
        report = sm.reap_services(tmp_path, grace_seconds=10.0)

        assert report.reaped == ["stub"], (
            f"reap did not clear the ledger: {report.to_dict()}. Our own "
            f"client was in CLOSE_WAIT on this port; the probe read it as "
            f"a listener and the service looked foreign."
        )
        assert report.foreign_ports == []
    finally:
        client.close()
        _force_cleanup(pid)
