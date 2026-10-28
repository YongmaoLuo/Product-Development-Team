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
import time
from pathlib import Path
from typing import NamedTuple, Optional

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
    # The command is started through the wrapper: the wrapper's own
    # program comes first, and the declared command follows it verbatim
    # -- never re-parsed, never re-quoted.
    argv = spawned["argv"]
    assert argv[0] == (sys.executable or "/usr/bin/python3")
    assert argv[1] == "-c"
    assert argv[3:] == ["/bin/bash", "-lc", "npx next dev -p 3000"]
    # The wrapper makes the session itself, so Popen must not ask for one
    # as well: the child would already be a group leader and the
    # wrapper's own ``setsid`` would fail with EPERM.
    assert "start_new_session" not in spawned["kwargs"]
    assert spawned["kwargs"]["cwd"] == str(project)


def test_detached_argv_hands_the_command_over_verbatim():
    """The wrapper starts the declared command, not a re-quoted copy."""
    start_cmd = "npx next dev -p 3000 --label 'a b'"

    argv = sm._detached_argv(start_cmd)

    assert argv[0] == (sys.executable or "/usr/bin/python3")
    assert argv[1] == "-c"
    assert argv[3:] == ["/bin/bash", "-lc", start_cmd]
    # The wrapper is what separates the session and what keeps the
    # descriptor variables out of the child.
    assert "os.setsid()" in argv[2]
    assert sm._SECRET_FD_ENV_PREFIX in argv[2]


@pytest.mark.timeout(120)
def test_the_wrapper_drops_descriptor_variables_and_leaves_the_session(
    tmp_path, monkeypatch,
):
    """The child must not see the descriptor variables, and must run in a
    session of its own.

    The descriptor variables name a descriptor number, and that number
    only means something in the process that published it. A child that
    inherits the variable reads a number that is its own -- and closes
    it -- which is how the event loop's selector used to die before the
    service was ready.
    """
    import subprocess
    import time as _time

    monkeypatch.setenv("PDT_SECRET_FD_FEISHU_APP_SECRET", "3")
    report = tmp_path / "report.json"
    script = tmp_path / "report.py"
    script.write_text(
        "import json, os, sys\n"
        "out = {\n"
        "    'fd_vars': sorted(\n"
        "        k for k in os.environ\n"
        f"        if k.startswith({sm._SECRET_FD_ENV_PREFIX!r})\n"
        "    ),\n"
        "    'sid': os.getsid(0),\n"
        "    'pgid': os.getpgid(0),\n"
        "}\n"
        "open(sys.argv[1], 'w').write(json.dumps(out))\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        sm._detached_argv(f"{sys.executable} {script} {report}"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = _time.monotonic() + 30.0
        while _time.monotonic() < deadline and not report.exists():
            _time.sleep(0.05)
        assert report.exists(), (
            "the wrapper never started the command it was given"
        )
        payload = json.loads(report.read_text())
        assert payload["fd_vars"] == [], (
            f"the child still sees the descriptor variables: "
            f"{payload['fd_vars']}"
        )
        assert payload["sid"] != os.getsid(0), (
            "the child shares this process' session, so it would not "
            "outlive the process that started it"
        )
        assert payload["pgid"] == payload["sid"], (
            f"the child's group ({payload['pgid']}) is not the one it "
            f"leads ({payload['sid']}), so stopping it by group would "
            f"miss the service"
        )
    finally:
        _force_cleanup(proc.pid)


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


class _StubServer(NamedTuple):
    """What :func:`_spawn_http_server` actually brought up.

    ``port`` is the port the child really bound, which is **not**
    necessarily the port that was asked for: the helper retries on a fresh
    port when the first one is taken. Both halves are returned together so
    a caller cannot pair the right pid with the wrong port.

    ``pid`` stays a plain int so it drops straight into
    :func:`_force_cleanup` and ``sm.ServiceRuntime(...)`` unchanged.
    """

    pid: int
    port: int


def _spawn_http_server(port: int, *, ignore_sigterm: bool = False) -> _StubServer:
    """Start a detached `python -m http.server`; return a :class:`_StubServer`
    holding **the port it actually bound** plus its pid.

    Retries on a bind failure. The port was chosen by :func:`_free_port`,
    which closes its probe socket before returning — so between choosing
    and binding, the port is unowned and any other test running
    concurrently can take it. That window is why these tests failed
    intermittently in a full unit run while passing in isolation: the
    failure has nothing to do with the service-manager code under test.

    The retry used to be invisible: the helper handed back only a pid, so
    a caller kept reasoning about the port it had *asked for*. When a
    retry had happened, the test connected its "lingering client" to the
    squatter, built a ``ServiceRuntime`` pointing at the squatter's port,
    and asked ``stop_service`` to free it — so ``port_free`` came back
    False through no fault of the code under test. Returning the port
    alongside the pid is what closes that; the bind itself still races,
    but the caller is now told the truth about where it landed.

    See :func:`_free_port` for why the window cannot be closed from that
    side.
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
            return _StubServer(proc.pid, port)
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
def test_spawn_http_server_reports_the_port_it_actually_bound():
    """The helper must never hand back a pid without saying where it bound.

    The flake this pins: when the port the caller picked was taken in the
    gap before the child's bind, ``_spawn_http_server`` retried on a fresh
    port and returned only the pid. Every caller then kept the port it had
    *asked for* — so a test would connect its client to the squatter,
    build a ``ServiceRuntime`` aimed at the squatter's port, and assert
    the port came free after killing a process that had never been on it.

    Holding the port here makes the retry deterministic instead of leaving
    it to the scheduler, which is what turns that flake into a test.
    """
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    squatted = holder.getsockname()[1]
    try:
        stub = _spawn_http_server(squatted)
        try:
            assert stub.port != squatted, (
                f"expected the retry to move off the held port {squatted}, "
                f"but the helper reported {stub.port}"
            )
            probe = sm.probe_port(stub.port)
            assert probe.listening, f"nothing is listening on {stub.port}: {probe!r}"
            assert stub.pid in probe.pids, (
                f"the helper reported port {stub.port}, but its own pid "
                f"{stub.pid} is not among the listeners {probe.pids}"
            )
        finally:
            _force_cleanup(stub.pid)
    finally:
        holder.close()


@pytest.mark.timeout(120)
def test_stop_service_releases_a_real_port():
    stub = _spawn_http_server(_free_port())
    pid, port = stub.pid, stub.port
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
    stub = _spawn_http_server(_free_port(), ignore_sigterm=True)
    pid, port = stub.pid, stub.port
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


def _write_ledger(
    plan_dir: Path, entries: list, project_dir: Path = None,
) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    payload = {"plan_id": plan_dir.name, "services": entries}
    if project_dir is not None:
        # The workspace the services were started in — the ledger says
        # whose process a leftover is.
        payload["project_dir"] = str(project_dir)
    sm.ledger_path(plan_dir).write_text(
        json.dumps(payload),
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


def _register_live_owner(plan_dir: Path, prefix: str = "round") -> str:
    """Register an owner token that is alive for the rest of the test."""
    token = sm.new_owner_token(prefix)
    sm.register_service_owner(plan_dir, token)
    return token


def _caller_token() -> str:
    """A token that owns nothing — the caller of an orphan sweep."""
    return sm.new_owner_token("sweep")


def test_reap_skips_entries_owned_by_a_newer_run(tmp_path: Path, monkeypatch):
    """A round that is still running owns its services.

    2026-10-24, live: the plan's api service came up at 15:44:32 and was
    SIGTERMed at 15:45:19, before its own round's first api_test ran at
    15:48:43 — an *older* round's exit-time reap read the newer round's
    entries out of the one ledger the plan directory has and stopped them.
    The ledger now says which round started what, and a reap only touches
    the entries its own round owns (or entries whose owner is gone).
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    newer = _register_live_owner(plan_dir)

    _write_ledger(plan_dir, [_entry(owner_token=newer)])
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []
    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: stopped.append(1) or {"port_free": True, "detail": ""},
    )

    report = sm.reap_services(
        plan_dir, "plan-x", owner_token=sm.new_owner_token("round"),
    )

    assert stopped == [], (
        "an older round's reap stopped the services a live round owns — "
        "that is the 2026-10-24 defect: the api service was killed 47s "
        "after it came up, before its own round's first test ran"
    )
    assert report.reaped == []
    assert report.skipped[0]["reason"] == "owned by a live run"
    # The entry stays in the ledger: its owner is the one that reaps it.
    assert sm.read_ledger(plan_dir)["services"][0]["owner_token"] == newer


def test_reap_still_kills_entries_owned_by_the_caller(tmp_path: Path, monkeypatch):
    """The anti-vacuity control: the owner's own reap still kills."""
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    caller = _register_live_owner(plan_dir)

    _write_ledger(plan_dir, [_entry(owner_token=caller)])
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []
    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: stopped.append(1) or {"port_free": True, "detail": ""},
    )

    report = sm.reap_services(plan_dir, "plan-x", owner_token=caller)

    assert len(stopped) == 1, "the round that started a service must stop it"
    assert report.reaped == ["chart"]
    assert not sm.ledger_path(plan_dir).exists()


def test_reap_leaves_unattributed_entries_alone_while_a_round_runs(
    tmp_path: Path, monkeypatch,
):
    """An entry nobody claims may belong to the round that is running.

    The ledger is written by the *project's* own ``service_manager``
    (see ``verification_agent._load_service_manager``), so a project
    that has not picked up the owner token yet writes unattributed
    entries. While a round is running, those entries may be that
    round's services — stopping them is the 2026-10-24 defect with
    one more step of indirection.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _register_live_owner(plan_dir)

    _write_ledger(plan_dir, [_entry()])
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []
    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: stopped.append(1) or {"port_free": True, "detail": ""},
    )

    report = sm.reap_services(plan_dir, "plan-x")

    assert stopped == [], (
        "an unattributed entry was stopped while a round was running — it "
        "may be that round's service"
    )
    assert report.reaped == []
    assert sm.read_ledger(plan_dir)["services"][0]["name"] == "chart"


def test_reap_kills_entries_whose_owner_is_gone(tmp_path: Path, monkeypatch):
    """An owner that died without reaping leaves services nobody owns.

    The token is deregistered (or its process is gone), so the entry is
    reapable — otherwise a crashed round would strand its services on
    their ports forever.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    gone = sm.new_owner_token("round")
    sm.register_service_owner(plan_dir, gone)
    sm.release_service_owner(plan_dir, gone)

    _write_ledger(plan_dir, [_entry(owner_token=gone)])
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []
    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: stopped.append(1) or {"port_free": True, "detail": ""},
    )

    report = sm.reap_services(plan_dir, "plan-x", owner_token=_caller_token())

    assert len(stopped) == 1, (
        "an entry whose owner is gone is an orphan; nobody else will reap it"
    )
    assert report.reaped == ["chart"]


def test_the_shutdown_sweep_reaps_what_the_leaving_process_started(
    tmp_path: Path, monkeypatch,
):
    """The backend's own rounds are over when the backend exits.

    The owner tokens name a pid — and while the backend is shutting
    down, that pid is still alive. Without releasing this process'
    owners before the sweep, the sweep would treat the backend's own
    services as the property of a running round and leave them holding
    their ports after the backend exited (the C3 promise).
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    token = sm.new_owner_token("verification_loop")
    sm.register_service_owner(plan_dir, token)

    _write_ledger(plan_dir, [_entry(owner_token=token)])
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []
    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: stopped.append(1) or {"port_free": True, "detail": ""},
    )

    reports = sm.reap_all_plans(
        tmp_path, reason="server_shutdown", release_own_owners=True,
    )

    assert len(stopped) == 1, (
        "the backend exited leaving the service it started on its port"
    )
    assert reports[0].reaped == ["chart"]


def test_the_startup_sweep_leaves_another_processs_round_alone(
    tmp_path: Path, monkeypatch,
):
    """A second backend may be running rounds against the same tree."""
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    other = sm.new_owner_token("verification_loop")
    # pid 1 is always alive and is never this process — the shape of
    # "another backend is running rounds against this plans tree".
    sm.register_service_owner(plan_dir, other, pid=1)

    _write_ledger(plan_dir, [_entry(owner_token=other)])
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []
    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: stopped.append(1) or {"port_free": True, "detail": ""},
    )

    sm.reap_all_plans(tmp_path, reason="startup_sweep")

    assert stopped == [], (
        "the startup sweep stopped a service another backend process is "
        "still using"
    )


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
    # The ledger is written directly, not through ``record_runtimes``:
    # ``record_runtimes`` also *pins* the process (a pin says "a live
    # round is using this one right now"), and this test is about
    # reaping an orphan, not about a round's own service.
    _write_ledger(tmp_path, [runtime.to_dict()])
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
#
# Both take their port from the `_StubServer` the spawn returned rather
# than from `_free_port()` directly. That is what keeps them deterministic
# under contention: the port they connect to, the port they build the
# `ServiceRuntime` from, and the port the server really bound on are the
# same number, whichever one the spawn ended up choosing.


def test_stop_reports_the_port_free_despite_a_lingering_client(tmp_path):
    """A connected client is not a listener (T2)."""
    stub = _spawn_http_server(_free_port())
    pid, port = stub.pid, stub.port
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
    stub = _spawn_http_server(_free_port())
    pid, port = stub.pid, stub.port
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        runtime = sm.ServiceRuntime(
            name="stub", port=port, pid=pid, pgid=sm._process_group_of(pid),
            start_cmd="stub", cwd=str(tmp_path),
            origin="spawned",
        )
        # The ledger is written directly: a pin means "a live round is
        # using this process", and this test is about the reaping of an
        # orphan (see ``test_reap_end_to_end_frees_a_real_port``).
        _write_ledger(tmp_path, [runtime.to_dict()])
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


# ---------------------------------------------------------------------------
# 2026-10-25 — our own leftover on the declared port is reclaimed
# ---------------------------------------------------------------------------
#
# The shape this exists for (plan 20261024-vp-quota-parked, 2026-10-24):
# the declared port was held by an instance an *earlier round of the same
# plan* had started.  That instance served another data plane, so every
# request to it answered "404 Plan not found" — and the round's effective
# declarations still pointed at the declared port, because the framework
# had relocated this round's own copy to a free port and left the
# declaration where it was.  The VPs therefore talked to the leftover.
#
# The old answer was rule 3: "never kill a process we cannot identify, so
# relocate around it".  The new answer is: identify *our own* leftovers
# and take the declared port back, so the effective port stays stable.


def _shell_decl(**overrides) -> ServiceDeclaration:
    """A declaration shaped like the ones production writes.

    A shell line: setup first (activate a virtualenv, export the data
    plane), the program last.  The running process only carries the
    program — so the *whole line* can never match it, which is exactly
    why the leftover used to look like a stranger.
    """
    base = dict(
        name="api",
        port=3000,
        start_cmd=(
            "source venv/bin/activate && export PDT_PLANS_DIR=/old/plans "
            "&& exec python -m api.server"
        ),
        cwd=".",
    )
    base.update(overrides)
    return ServiceDeclaration(**base)


def _holder_probe(port: int, pid: int, cmdline: str) -> PortProbe:
    """A probe that says "somebody is listening on *port*"."""
    return PortProbe(
        port=port, listening=True, pids=[pid], cmdlines=[cmdline],
    )


def _ledger_with_holder(
    plan_dir: Path,
    *,
    port: int,
    pid: int,
    project_dir: Path,
    plan_id: str = "20261024-vp-quota-parked",
    owner_token: str = "",
    start_cmd: str = (
        "source venv/bin/activate && export PDT_PLANS_DIR=/old/plans "
        "&& exec python -m api.server"
    ),
) -> None:
    """Write the ledger an earlier round of this plan would have left.

    The entry carries the *declared* start command, the way production
    writes it — so the data plane the leftover was serving is readable
    from the ledger.

    The ledger is written **directly**, not through ``record_runtimes``:
    this fixture is a leftover of a round that is *over*, and a round
    that is over leaves no live pin behind (a pin says "this process is
    in use right now").  Writing it through ``record_runtimes`` would
    pin the very process these tests are about to reclaim.
    """
    runtime = sm.ServiceRuntime(
        name="api", port=port, pid=pid, pgid=pid,
        start_cmd=start_cmd, cwd=".",
        origin="spawned", ready=True, owner_token=owner_token,
    )
    _write_ledger(plan_dir, [runtime.to_dict()], project_dir=project_dir)


def _stub_reclaim_spy(monkeypatch) -> tuple:
    """Record the stops and the starts ``ensure_services`` performs.

    Returns ``(stopped, started)`` — the pids that were signalled and the
    ports this round's own copy was started on.
    """
    stopped: list = []
    started: list = []

    def _fake_stop(runtime, decl=None, **kwargs):
        stopped.append(runtime.pid)
        return {
            "name": runtime.name, "port": runtime.port, "killed": [],
            "signalled": [f"pid {runtime.pid}"], "port_free": True,
            "detail": "stub",
        }

    def _fake_start(decl, project_dir, timeout_seconds=None):
        started.append(decl.port)
        return sm.ServiceRuntime(
            name=decl.name, port=decl.port, pid=5151, pgid=5151,
            start_cmd=decl.start_cmd, cwd=decl.cwd,
            origin="spawned", ready=True, detail="stub",
        )

    monkeypatch.setattr(sm, "stop_service", _fake_stop)
    monkeypatch.setattr(sm, "start_service", _fake_start)
    return stopped, started


def test_our_own_leftover_is_reclaimed_instead_of_relocated(tmp_path, monkeypatch):
    """The declared port comes back to us — the round does not move.

    The ledger of this plan says the holder of the port is a process we
    started in an earlier round.  It is ours, so it is ours to stop, and
    this round's copy takes the *declared* port: the effective
    declarations the VPs read stay valid.
    """
    project = tmp_path / "project"
    project.mkdir()
    plan_dir = tmp_path / "plans" / "20261024-vp-quota-parked"
    plan_dir.mkdir(parents=True)
    _ledger_with_holder(
        plan_dir, port=3000, pid=4242, project_dir=project,
    )

    decl = _shell_decl()
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _holder_probe(3000, 4242, "/usr/bin/python3 -m api.server"),
    )
    stopped, started = _stub_reclaim_spy(monkeypatch)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)

    result = sm.ensure_services(
        project, {"api": decl}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert stopped == [4242], (
        f"our own leftover must be stopped; stopped {stopped!r}"
    )
    assert started == [3000], (
        f"this round's copy must take the DECLARED port, so the effective "
        f"declarations stay valid; started on {started!r}"
    )
    runtime = result.runtimes["api"]
    assert runtime.port == 3000
    assert runtime.declared_port == 3000
    assert runtime.reclaimed is True, (
        f"the reclaim must be recorded on the runtime; got {runtime.to_dict()!r}"
    )
    assert runtime.reclaim_reason, "a reclaim without a reason is not an audit trail"
    assert runtime.ready
    assert result.blocked_ports == []


def test_a_leftover_of_another_workspace_is_relocated_not_reclaimed(
    tmp_path, monkeypatch,
):
    """Another workspace's process is not ours to stop — rule 3 stands."""
    project = tmp_path / "project"
    project.mkdir()
    other_workspace = tmp_path / "other-project"
    other_workspace.mkdir()
    plan_dir = tmp_path / "plans" / "20261024-vp-quota-parked"
    plan_dir.mkdir(parents=True)
    _ledger_with_holder(
        plan_dir, port=3000, pid=4242, project_dir=other_workspace,
    )

    decl = _shell_decl()
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _holder_probe(3000, 4242, "/usr/bin/python3 -m api.server"),
    )
    stopped, started = _stub_reclaim_spy(monkeypatch)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)

    result = sm.ensure_services(
        project, {"api": decl}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert stopped == [], "a process of another workspace must never be signalled"
    assert started == [38921], "and the round gets out of its way instead"
    runtime = result.runtimes["api"]
    assert runtime.reclaimed is False
    assert runtime.port == 38921
    assert runtime.declared_port == 3000


def test_a_service_a_live_round_still_owns_is_not_reclaimed(tmp_path, monkeypatch):
    """A running round's service is not a leftover — it is in use."""
    project = tmp_path / "project"
    project.mkdir()
    plan_dir = tmp_path / "plans" / "20261024-vp-quota-parked"
    plan_dir.mkdir(parents=True)
    # A round that is running right now: its owner is this process, so it
    # is alive.
    sm.register_service_owner(plan_dir, "run-live", current=True)
    _ledger_with_holder(
        plan_dir, port=3000, pid=4242, project_dir=project,
        owner_token="run-live",
    )

    decl = _shell_decl()
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _holder_probe(3000, 4242, "/usr/bin/python3 -m api.server"),
    )
    stopped, started = _stub_reclaim_spy(monkeypatch)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)

    result = sm.ensure_services(
        project, {"api": decl}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert stopped == [], (
        f"a service a live round owns must not be stopped; stopped {stopped!r}"
    )
    assert started == [38921]
    assert result.runtimes["api"].reclaimed is False


def test_a_leftover_of_our_own_program_in_our_workspace_is_reclaimed(
    tmp_path, monkeypatch,
):
    """No ledger, but the shape still says "ours".

    The holder runs the program our declaration starts, out of our own
    workspace.  That is a leftover of this workspace — the round needs
    the declared port more than the leftover does.
    """
    project = tmp_path / "project"
    project.mkdir()
    plan_dir = tmp_path / "plans" / "20261024-vp-quota-parked"
    plan_dir.mkdir(parents=True)

    decl = _shell_decl()
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _holder_probe(3000, 4242, "/usr/bin/python3 -m api.server"),
    )
    monkeypatch.setattr(sm, "_process_cwd", lambda pid: str(project))
    stopped, started = _stub_reclaim_spy(monkeypatch)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)

    result = sm.ensure_services(
        project, {"api": decl}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert stopped == [4242], (
        f"a leftover of our own program in our own workspace must be "
        f"stopped; stopped {stopped!r}"
    )
    assert started == [3000], "and the declared port must come back to us"
    assert result.runtimes["api"].reclaimed is True


def test_a_stranger_on_the_declared_port_is_still_relocated(tmp_path, monkeypatch):
    """The safety rule is untouched: a stranger is never signalled."""
    project = tmp_path / "project"
    project.mkdir()
    plan_dir = tmp_path / "plans" / "20261024-vp-quota-parked"
    plan_dir.mkdir(parents=True)

    decl = _shell_decl()
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _holder_probe(
            3000, 4242, "/usr/bin/postgres -D /var/db",
        ),
    )
    monkeypatch.setattr(sm, "_process_cwd", lambda pid: str(project))
    stopped, started = _stub_reclaim_spy(monkeypatch)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)

    result = sm.ensure_services(
        project, {"api": decl}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert stopped == [], "a stranger must never be signalled"
    assert started == [38921], "the round relocates around it, as before"
    runtime = result.runtimes["api"]
    assert runtime.reclaimed is False
    assert runtime.port == 38921


def test_the_reclaim_reason_names_the_data_plane_the_leftover_served(
    tmp_path, monkeypatch,
):
    """The reason says *why* the leftover was in the way.

    A leftover that serves another plans dir is the whole incident: it
    cannot see this plan's fixture, so every request to it answers
    "404 Plan not found".  The reason has to name that, because it is
    the fact that made the reclaim necessary.
    """
    project = tmp_path / "project"
    project.mkdir()
    plan_dir = tmp_path / "plans" / "20261024-vp-quota-parked"
    plan_dir.mkdir(parents=True)
    _ledger_with_holder(
        plan_dir, port=3000, pid=4242, project_dir=project,
    )

    decl = _shell_decl(
        start_cmd=(
            "source venv/bin/activate && export PDT_PLANS_DIR=/new/plans "
            "&& exec python -m api.server"
        ),
    )
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _holder_probe(3000, 4242, "/usr/bin/python3 -m api.server"),
    )
    _stub_reclaim_spy(monkeypatch)

    result = sm.ensure_services(
        project, {"api": decl}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    runtime = result.runtimes["api"]
    assert "reclaimed" in runtime.reclaim_reason
    assert "PDT_PLANS_DIR" in runtime.reclaim_reason, (
        f"the reason must name the data plane the leftover was serving: "
        f"{runtime.reclaim_reason!r}"
    )
    assert "/old/plans" in runtime.reclaim_reason
    assert "/new/plans" in runtime.reclaim_reason
    assert runtime.reclaim_reason in runtime.detail, (
        f"the reason must reach the detail the operator reads: "
        f"{runtime.detail!r}"
    )


def test_the_reclaimed_instance_is_ours_to_reap(tmp_path, monkeypatch):
    """A reclaimed instance is spawned by us — the ledger must claim it."""
    project = tmp_path / "project"
    project.mkdir()
    plan_dir = tmp_path / "plans" / "20261024-vp-quota-parked"
    plan_dir.mkdir(parents=True)
    _ledger_with_holder(
        plan_dir, port=3000, pid=4242, project_dir=project,
    )

    decl = _shell_decl()
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _holder_probe(3000, 4242, "/usr/bin/python3 -m api.server"),
    )
    _stub_reclaim_spy(monkeypatch)

    result = sm.ensure_services(
        project, {"api": decl}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    runtime = result.runtimes["api"]
    assert runtime.spawned_by_ac, (
        f"the reclaimed instance was started by this round, so the ledger "
        f"has to claim it: {runtime.to_dict()!r}"
    )
    assert runtime.to_dict()["reclaimed"] is True
    assert runtime.to_dict()["reclaim_reason"]


def test_a_live_rounds_service_is_not_reclaimed_even_when_it_looks_like_ours(
    tmp_path, monkeypatch,
):
    """A running round's service is in use — the shape must not override it.

    The holder here looks exactly like a leftover: it runs our program,
    out of our workspace, on a port our ledger records.  But the ledger
    says a round that is **running right now** owns it, so it is not a
    leftover — it is a service another round is using.  The verdict the
    ledger gives has to hold: the weaker "same program, same workspace"
    shape must not talk its way past it, because that is how a running
    round loses its api server.
    """
    project = tmp_path / "project"
    project.mkdir()
    plan_dir = tmp_path / "plans" / "20261024-vp-quota-parked"
    plan_dir.mkdir(parents=True)
    sm.register_service_owner(plan_dir, "run-live", current=True)
    _ledger_with_holder(
        plan_dir, port=3000, pid=4242, project_dir=project,
        owner_token="run-live",
    )

    decl = _shell_decl()
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _holder_probe(3000, 4242, "/usr/bin/python3 -m api.server"),
    )
    # The holder's cwd is our workspace — the shape a leftover has.
    monkeypatch.setattr(sm, "_process_cwd", lambda pid: str(project))
    stopped, started = _stub_reclaim_spy(monkeypatch)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)

    result = sm.ensure_services(
        project, {"api": decl}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert stopped == [], (
        f"a service a live round owns must not be signalled, whatever it "
        f"looks like; stopped {stopped!r}"
    )
    assert started == [38921], (
        f"the round gets out of its way instead; started on {started!r}"
    )
    assert result.runtimes["api"].reclaimed is False


# ---------------------------------------------------------------------------
# The declared service stays healthy until the last VP (2026-10-25, repair-r1-22)
# ---------------------------------------------------------------------------
#
# The 2026-10-24 production round started the plan's ``api`` service and
# then had twelve ``api_test`` VPs fail with a connection refusal --
# against an address the round had never verified.  The address was
# re-derived from the runtime map every time the plan was resolved, and
# the map is a *record*: anything that merges into it may rewrite it.
#
# So the round now binds every service to the instance its own
# ``ensure`` produced, and each VP's request is built from that
# instance.  This is the end-to-end version of that contract, with a
# **real** service: the round brings up an HTTP server, the runtime map
# is then rewritten to an address the round never verified, and three
# ``api_test`` VPs are dispatched.  The VPs must reach the instance the
# ``ensure`` produced -- before the fix they followed the map to the
# foreign port and every one of them failed with a connection refusal --
# and the service must still be serving when the last VP is done.


@pytest.mark.timeout(180)
def test_declared_service_stays_healthy_until_the_last_vp(tmp_path: Path):
    """The service the round ensured serves every VP, and is still up
    after the last one."""
    import sys as _sys

    from verification_agent import VerificationAgent

    port = _free_port()
    script = (
        "import http.server,socketserver;"
        f"socketserver.TCPServer(('127.0.0.1',{port}),"
        "http.server.SimpleHTTPRequestHandler).serve_forever()"
    )
    decl = ServiceDeclaration(
        name="api",
        port=port,
        start_cmd=f"{shlex.quote(_sys.executable)} -c {shlex.quote(script)}",
        cwd=".",
        health_url=f"http://127.0.0.1:{port}/",
        ready_timeout_seconds=30,
    )

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    agent = VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=None,
    )
    agent._service_declarations = {"api": decl}
    agent.start_verification_round(1)

    runtime = None
    try:
        runtime_map = agent._preflight_declared_services({"services": []})
        runtime = runtime_map.runtimes["api"]
        assert runtime.ready is True, runtime.detail
        assert runtime.port == port

        # The map is a **record** of what the ensure found; anything that
        # merges into it may rewrite it.  The address the VPs are
        # dispatched to must not follow it.
        foreign_port = _free_port()
        agent._service_runtime_map.runtimes["api"] = sm.ServiceRuntime(
            name="api",
            port=foreign_port,
            declared_port=port,
            pid=999,
            pgid=999,
            start_epoch=1.0,
            start_cmd=decl.start_cmd,
            cwd=".",
            origin="spawned",
            ready=True,
        )

        plan = {
            "verification_points": [
                {
                    "id": f"VP-{index:03d}",
                    "verification_method": "api_test",
                    "uses_services": ["api"],
                    "request": {"method": "GET", "url": "{{svc.api.url}}/"},
                    "assertions": [{"status": 200}],
                }
                for index in (1, 2, 3)
            ]
        }
        resolved = agent._resolve_service_placeholders(plan)

        for vp in resolved["verification_points"]:
            verdict = agent._run_api_test_vp(vp)
            assert verdict["status"] == "PASSED", verdict
            artifact = json.loads(
                (
                    plan_dir / "vp_artifacts" / vp["id"] / "api_response.json"
                ).read_text(encoding="utf-8"),
            )
            assert artifact["request"]["url"] == f"http://127.0.0.1:{port}/"

        # The service that served them is still up, and it is still the
        # one the round is bound to.
        assert sm.probe_port(port).listening is True
        assert agent._service_bindings["api"].port == port
    finally:
        if runtime is not None and runtime.pid:
            _force_cleanup(runtime.pid)


# ---------------------------------------------------------------------------
# Round pins (2026-10-25, repair-r1-23)
# ---------------------------------------------------------------------------
#
# 2026-10-25, production: the plan's api service came up at 21:54:08
# (pid 585, port 58396) and was SIGTERMed at 21:54:11 — three seconds
# later, by a sweep that did not know a round was running.  The ledger
# entry named the process, but its ``owner_token`` was **empty**, so the
# sweep read the entry as an orphan: "nobody claims it" is what a
# crashed round looks like too.
#
# A **pin** is that fact written the other way round.  The round pins the
# processes it is using (pid, pgid, boot id, port); the pin expires on
# its own; and it does not need an owner token to be effective — which is
# the whole point, because the empty token is exactly what let the sweep
# through.  A reap reads the pins before it decides to kill.
#
# The pin file is a sibling of the ledger and of the owner registry, so
# the two module instances (the project's copy of ``service_manager`` and
# the harness' one) share it the same way: through the disk.


def _pin(
    pid: int = 4242,
    port: int = 3000,
    *,
    round_token: str = "",
    writer_pid: Optional[int] = None,
    ttl: float = 3600.0,
    name: str = "api",
) -> dict:
    """One pin, in the shape a round writes it."""
    now = time.time()
    return {
        "name": name,
        "pid": pid,
        "pgid": pid,
        "boot_id": f"{pid}@{int(now)}",
        "port": port,
        "declared_port": port,
        "round_token": round_token,
        "writer_pid": os.getpid() if writer_pid is None else writer_pid,
        "created_at": now,
        "expires_at": now + ttl,
    }


def _write_pins(plan_dir: Path, pins: list) -> None:
    """Write the pin file the way a round does."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / ".round_pins.json").write_text(
        json.dumps({"updated_at": "2026-10-25T00:00:00", "pins": pins}),
        encoding="utf-8",
    )


def _pinned_runtime(pid: int = 4242, port: int = 3000) -> "sm.ServiceRuntime":
    """A runtime as the round sees it: started by us, and ready."""
    return sm.ServiceRuntime(
        name="api", port=port, pid=pid, pgid=pid, start_epoch=1.0,
        origin="spawned", ready=True,
    )


def _alive_process(monkeypatch, *pids) -> None:
    """Say which pids are live processes.

    The pids in these tests are made up (4242, 900 …), so a test that
    means "a live round is using this process" has to say so: a pin
    protects a *process*, and there is nothing to protect when the
    process is gone.  This process (the one that writes the pin) is
    always alive — it is the round's own process.
    """
    alive = {int(p) for p in pids} | {os.getpid()}
    monkeypatch.setattr(sm, "_pid_alive", lambda pid: int(pid) in alive)


def test_own_stale_holder_is_reclaimed_on_the_declared_port(tmp_path, monkeypatch):
    """Our own leftover comes back to us — a *live* one does not.

    The declared port is what the VPs' effective declarations point at,
    so a leftover of our own has to be taken back (rule 3) rather than
    relocated around: the leftover served another data plane, so every
    request to it answered "404 Plan not found".

    "Stale" is what the round pin decides.  A holder a **live** round
    has pinned is in use, not a leftover — stopping it is how a running
    round loses its api server between two VPs (2026-10-25, repair-r1-23).
    """
    project = tmp_path / "project"
    project.mkdir()
    decl = _shell_decl()

    # --- the stale leftover -------------------------------------------
    plan_dir = tmp_path / "plans" / "20261024-vp-quota-parked"
    plan_dir.mkdir(parents=True)
    _ledger_with_holder(
        plan_dir, port=3000, pid=4242, project_dir=project,
    )
    # The round that started it is long over: its pin has expired.
    _write_pins(plan_dir, [_pin(pid=4242, port=3000, ttl=-1.0)])

    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _holder_probe(3000, 4242, "/usr/bin/python3 -m api.server"),
    )
    stopped, started = _stub_reclaim_spy(monkeypatch)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)

    result = sm.ensure_services(
        project, {"api": decl}, plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert stopped == [4242], (
        f"a stale leftover of ours must be stopped; stopped {stopped!r}"
    )
    assert started == [3000], (
        f"this round's copy must take the DECLARED port, so the effective "
        f"declarations stay valid; started on {started!r}"
    )
    assert result.runtimes["api"].reclaimed is True

    # --- the holder a live round is using ------------------------------
    # The same leftover, but a live round has pinned it.  The ledger
    # entry carries no owner token — that is the production shape — so
    # the pin is the only thing that says "this one is in use".
    live_dir = tmp_path / "plans" / "20261024-vp-quota-live"
    live_dir.mkdir(parents=True)
    _ledger_with_holder(
        live_dir, port=3000, pid=4242, project_dir=project,
    )
    live_token = "run-live"
    sm.register_service_owner(live_dir, live_token, current=True)
    _write_pins(
        live_dir, [_pin(pid=4242, port=3000, round_token=live_token)],
    )
    # The pinned process is running: a pin protects a live process.
    _alive_process(monkeypatch, 4242)

    stopped, started = _stub_reclaim_spy(monkeypatch)

    live_result = sm.ensure_services(
        project, {"api": decl}, plan_dir=live_dir, plan_id=live_dir.name,
    )

    assert stopped == [], (
        f"a service a live round has pinned is in use, not a leftover; "
        f"stopped {stopped!r}"
    )
    assert started == [38921], "the round gets out of its way instead"
    assert live_result.runtimes["api"].reclaimed is False


def test_reap_skips_pinned_pid(tmp_path, monkeypatch):
    """The production shape: the ledger has no owner, the pin has the round.

    The 2026-10-25 sweep found the ledger entry with an empty
    ``owner_token`` and read it as an orphan.  The pin is the answer to
    that: it names the process, and it does not need a token.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _write_ledger(plan_dir, [_entry(pid=4242, pgid=4242, owner_token="")])
    # The round that is using the service pins it.  The pin was written
    # by a process that is still alive, so the pin holds.
    sm.pin_services_for_round(
        plan_dir, "", [_pinned_runtime()], ttl_sec=600,
    )
    _alive_process(monkeypatch, 4242)
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []
    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: stopped.append(1) or {"port_free": True, "detail": ""},
    )

    report = sm.reap_services(plan_dir, "plan-x")

    assert stopped == [], (
        "a pinned service was stopped — the round that is using it would "
        "lose it between two VPs (2026-10-25: pid 585, 3s after ready)"
    )
    assert report.reaped == []
    assert report.reap_skipped_pinned, report.to_dict()
    assert report.skipped[0]["reason"] == "reap_skipped_pinned"
    # The entry stays in the ledger: the round that owns it reaps it.
    assert sm.read_ledger(plan_dir)["services"]


def test_startup_sweep_respects_live_pin(tmp_path, monkeypatch):
    """The tree-wide sweep reads the pins before it kills anything."""
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / "plan-x"
    plan_dir.mkdir(parents=True)
    _write_ledger(plan_dir, [_entry(pid=4242, pgid=4242, owner_token="")])
    sm.pin_services_for_round(
        plan_dir, "", [_pinned_runtime()], ttl_sec=600,
    )
    _alive_process(monkeypatch, 4242)
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []
    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: stopped.append(1) or {"port_free": True, "detail": ""},
    )

    reports = sm.reap_all_plans(plans_dir, reason="startup_sweep")

    assert len(reports) == 1, reports
    assert stopped == [], "the startup sweep stopped a pinned service"
    assert reports[0].reaped == []
    assert reports[0].reap_skipped_pinned, reports[0].to_dict()


def test_pin_expiry_allows_reclaim(tmp_path, monkeypatch):
    """An expired pin protects nothing — the orphan is reaped."""
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _write_ledger(plan_dir, [_entry(pid=4242, pgid=4242, owner_token="")])
    sm.pin_services_for_round(
        plan_dir, "", [_pinned_runtime()], ttl_sec=0.0,
    )
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []

    def _fake_stop(runtime, *args, **kwargs):
        stopped.append(runtime.pid)
        return {"port_free": True, "detail": "stopped"}

    monkeypatch.setattr(sm, "stop_service", _fake_stop)

    report = sm.reap_services(plan_dir, "plan-x")

    assert stopped == [4242], (
        "an expired pin must not keep a service alive for ever"
    )
    assert report.reaped == ["chart"]
    assert report.reap_skipped_pinned == []


def test_pin_removed_after_round_exit(tmp_path, monkeypatch):
    """The round's own exit reap stops its service — and drops its pin."""
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    token = _register_live_owner(plan_dir)
    _write_ledger(plan_dir, [_entry(pid=4242, pgid=4242, owner_token=token)])
    sm.pin_services_for_round(
        plan_dir, token, [_pinned_runtime()], ttl_sec=600,
    )
    assert sm.round_pins_path(plan_dir).exists()
    _alive_process(monkeypatch, 4242)
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []
    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: stopped.append(1) or {"port_free": True, "detail": ""},
    )

    report = sm.reap_services(plan_dir, "plan-x", owner_token=token)

    assert report.reaped == ["chart"]
    assert not sm.round_pins_path(plan_dir).exists(), (
        "the round is over — its pin must not keep the service alive"
    )


def test_a_stale_pin_does_not_shield_the_next_rounds_service(
    tmp_path, monkeypatch,
):
    """The pin of the round before must not shield the next round.

    The rounds of one plan share one plan directory, so the pin of the
    round before is still in the file when the next round starts.
    Matched by *port* it would look like a shield, and the new round's
    service would survive its own exit reap — the service would outlive
    the round that started it.

    The pid is the stronger witness: it names the process, the port
    only the socket.  So the pin that names the ledger's process wins,
    and the one that only shares the port does not.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _write_ledger(plan_dir, [_entry(pid=900, pgid=900, owner_token="run-2")])
    _write_pins(plan_dir, [
        # The round before: its writer is still alive, so its pin is in
        # force — but it names a process of its own.
        _pin(pid=4242, port=3000, round_token=""),
        # This round's own pin: the process the ledger names.
        _pin(pid=900, port=3000, round_token="run-2"),
    ])
    _alive_process(monkeypatch, 4242, 900)
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []

    def _fake_stop(runtime, *args, **kwargs):
        stopped.append(runtime.pid)
        return {"port_free": True, "detail": "stopped"}

    monkeypatch.setattr(sm, "stop_service", _fake_stop)

    report = sm.reap_services(plan_dir, "plan-x", owner_token="run-2")

    assert stopped == [900], (
        "the round's own service must be reaped by the round's own exit "
        f"reap; stopped {stopped!r}"
    )
    assert report.reaped == ["chart"]
    assert report.reap_skipped_pinned == []


def test_a_pin_whose_process_is_gone_protects_nothing(
    tmp_path, monkeypatch,
):
    """A pin protects a *process* — when that process is gone, it is spent.

    The pin of a round that died can outlive the round (its writer is
    the backend, which lives on).  A spent pin must not go on shielding
    whatever lands on the port next.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _write_ledger(plan_dir, [_entry(pid=4242, pgid=4242, owner_token="")])
    sm.pin_services_for_round(
        plan_dir, "", [_pinned_runtime()], ttl_sec=600,
    )
    # The pinned process is gone; the process that wrote the pin (this
    # one) is not.
    _alive_process(monkeypatch)
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []

    def _fake_stop(runtime, *args, **kwargs):
        stopped.append(runtime.pid)
        return {"port_free": True, "detail": "stopped"}

    monkeypatch.setattr(sm, "stop_service", _fake_stop)

    report = sm.reap_services(plan_dir, "plan-x")

    assert stopped == [4242], (
        "a pin of a process that is gone protects nothing; "
        f"stopped {stopped!r}"
    )
    assert report.reaped == ["chart"]
    assert report.reap_skipped_pinned == []
    assert not sm.round_pins_path(plan_dir).exists(), (
        "a spent pin must be dropped, not left to shield the next round"
    )


def test_a_pin_on_the_declared_port_does_not_cover_a_relocated_service(
    tmp_path, monkeypatch,
):
    """A pin protects a *process* — the declared port is not it.

    A round that had to relocate serves somewhere else.  The pin of the
    round that holds the declared port says nothing about the relocated
    one, and it must not shield it: the relocated service would outlive
    the round that started it.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _write_ledger(plan_dir, [_entry(
        port=38921, pid=900, pgid=900, owner_token="",
        declared_port=3000,
    )])
    # The round that holds the declared port.
    _write_pins(plan_dir, [_pin(pid=585, port=3000, round_token="")])
    _alive_process(monkeypatch, 585, 900)
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["npx next dev -p 3000"], start_epoch=1.0),
    )
    stopped = []

    def _fake_stop(runtime, *args, **kwargs):
        stopped.append(runtime.pid)
        return {"port_free": True, "detail": "stopped"}

    monkeypatch.setattr(sm, "stop_service", _fake_stop)

    report = sm.reap_services(plan_dir, "plan-x")

    assert stopped == [900], (
        "the pin of another round does not shield the service that was "
        f"relocated; stopped {stopped!r}"
    )
    assert report.reaped == ["chart"]
    assert report.reap_skipped_pinned == []


# ---------------------------------------------------------------------------
# A relocation that could not place the service (2026-10-25, repair-r1-29)
# ---------------------------------------------------------------------------
#
# The production round asked a port (51725) that nothing had ever
# listened on.  The address had come from somewhere, and the round's
# own record did not say where: when a relocation gives up, the runtime
# it hands back still carries the **declared** port and only says
# ``origin="failed"`` in prose.  A downstream reader that asks "is it
# ready?" and does not read the prose has an address in its hand.
#
# So the failure is now **explicit** — a field, not a sentence — and
# every port the relocation tried is written into the address event
# stream, so "where did this port come from" is answerable from the
# file even for the ports that did not work.


def _stub_failed_start(monkeypatch, *, pid=None):
    """A ``start_service`` that never becomes ready."""

    def _fake(decl, project_dir, timeout_seconds=None):
        return sm.ServiceRuntime(
            name=decl.name, port=decl.port, pid=pid, ready=False,
            start_cmd=decl.start_cmd, detail="never became ready",
        )

    monkeypatch.setattr(sm, "start_service", _fake)


def test_a_failed_relocation_is_explicitly_unusable(project, monkeypatch):
    """The runtime says "no address" in a field, not only in prose.

    ``ready=False`` says "the service did not come up".  ``unusable``
    says something stronger: the port this record names is **not an
    address at all** — it is the declared port, and somebody else holds
    it.  The two are different facts, and only the second one may not
    be used as a fallback.
    """
    _stub_failed_start(monkeypatch)
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
    assert runtime.unusable is True, (
        "a relocation that could not place the service must say so in a "
        "field; the port it names is somebody else's"
    )
    assert runtime.failure_reason, "the unusable runtime must say why"
    assert "relocating failed" in runtime.failure_reason
    assert runtime.usable is False


def test_a_relocated_service_is_usable(project, monkeypatch):
    """The other half of the same contract: a service that did come up
    is usable, and says so."""
    _stub_start(monkeypatch)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["/usr/bin/postgres"], start_epoch=1e12),
    )

    result = sm.ensure_services(project, {"chart": _decl()})

    runtime = result.runtimes["chart"]
    assert runtime.ready is True
    assert runtime.unusable is False
    assert runtime.usable is True


def test_a_failed_relocation_still_blocks_the_declared_port(project, monkeypatch):
    """The declared port stays blocked.

    Somebody else holds it, so it is the one port a VP must never be
    sent to.  Marking the runtime unusable must not unblock it.
    """
    _stub_failed_start(monkeypatch)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: 38921)
    monkeypatch.setattr(sm, "stop_service", lambda *a, **k: {"port_free": True})
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["/usr/bin/postgres"], start_epoch=1e12),
    )

    result = sm.ensure_services(project, {"chart": _decl()})

    assert result.blocked_ports == [3000], (
        "the port the stranger holds must stay blocked"
    )


def test_every_candidate_port_a_relocation_tried_is_recorded(
    project, tmp_path, monkeypatch,
):
    """Every port the relocation tried is in the address event stream.

    The chain that produced an address has to be reconstructible from
    the file alone — including the candidates that did **not** work,
    because those are the ports somebody will later ask about.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    ports = iter([38921, 38922, 38923])
    _stub_failed_start(monkeypatch)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: next(ports))
    monkeypatch.setattr(sm, "stop_service", lambda *a, **k: {"port_free": True})
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["/usr/bin/postgres"], start_epoch=1e12),
    )

    runtime_map = sm.ensure_services(
        project, {"chart": _decl()}, plan_dir=plan_dir, plan_id="plan-x",
    )
    sm.record_runtimes(
        plan_dir, "plan-x", runtime_map, project_dir=project,
    )

    attempts = [
        e for e in sm.read_address_events(plan_dir)
        if e.get("event") == "relocate_attempt"
    ]
    assert [e["new_port"] for e in attempts] == [38921, 38922, 38923], (
        "every candidate port must be in the stream; got "
        f"{[e.get('new_port') for e in attempts]!r}"
    )
    for event in attempts:
        assert event["declared_port"] == 3000, (
            "a candidate event must name the port it moved away from"
        )
    assert runtime_map.runtimes["chart"].candidate_ports == [
        38921, 38922, 38923,
    ]


def test_the_failure_reason_names_the_candidate_ports(project, monkeypatch):
    """The reason the relocation failed lists the ports it tried.

    The runtime's own record is what an operator reads when the round
    says "the service could not be placed"; the ports it tried belong
    in that sentence.
    """
    ports = iter([38921, 38922, 38923])
    _stub_failed_start(monkeypatch)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: next(ports))
    monkeypatch.setattr(sm, "stop_service", lambda *a, **k: {"port_free": True})
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(p, ["/usr/bin/postgres"], start_epoch=1e12),
    )

    result = sm.ensure_services(project, {"chart": _decl()})

    reason = result.runtimes["chart"].failure_reason
    for port in (38921, 38922, 38923):
        assert str(port) in reason, (
            f"the failure reason must name the candidate port {port}; "
            f"got {reason!r}"
        )


# ---------------------------------------------------------------------------
# The round does not adopt a service it could not place (2026-10-25, repair-r1-29)
# ---------------------------------------------------------------------------
#
# Marking the runtime unusable is only half of the fix.  The other half
# is that **no reader adopts it**: the round's effective address table,
# the binding its VPs are dispatched to, and the gates a re-ensure has
# to pass all refuse a runtime that says "this names no address".
#
# And the declared port is not a fallback for it.  The declared port is
# the stranger's — falling back to it would aim the round's VPs at the
# process squatting the port, which is how a round ends up graded on
# somebody else's server.


def _unusable_runtime(name="chart", port=3000):
    """The runtime a failed relocation hands back."""
    runtime = sm.ServiceRuntime(
        name=name, port=port, declared_port=port, pid=None,
        start_cmd="npx next dev -p 3000", cwd=".", origin="failed",
        ready=False, detail="the declared port is held by somebody else",
    )
    runtime.unusable = True
    runtime.failure_reason = runtime.detail
    return runtime


def _agent_with(tmp_path: Path, *, runtime=None):
    """A round that already ensured (and failed to ensure) one service."""
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    agent = VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=None,
    )
    agent._service_declarations = {"chart": _decl()}
    if runtime is not None:
        agent._service_runtime_map = sm.ServiceRuntimeMap(
            runtimes={"chart": runtime},
        )
    return agent


def test_the_round_does_not_adopt_an_unusable_runtime(tmp_path: Path):
    """The effective address table has no entry for it.

    Not the declared port: the declared port is the stranger's, and a
    round that falls back to it is a round that talks to the stranger.
    """
    agent = _agent_with(tmp_path, runtime=_unusable_runtime())

    assert "chart" not in agent._effective_declarations()


def test_a_runtime_that_is_only_not_ready_keeps_the_declared_port(
    tmp_path: Path,
):
    """The contrast: "not up yet" is not "not ours".

    A service that simply did not come up **still owns** its declared
    port, so the declared address stays the effective one and the
    blocked-port gate is what says BLOCKED.  The two cases are not
    conflated.
    """
    runtime = sm.ServiceRuntime(
        name="chart", port=3000, declared_port=3000, pid=None,
        start_cmd="npx next dev -p 3000", cwd=".", origin="spawned",
        ready=False, detail="did not come up",
    )
    agent = _agent_with(tmp_path, runtime=runtime)

    assert agent._effective_declarations()["chart"].port == 3000


def test_binding_an_unusable_runtime_is_refused(tmp_path: Path):
    agent = _agent_with(tmp_path)

    assert agent._bind_service_runtime("chart", _unusable_runtime()) is None
    assert "chart" not in agent._service_bindings


def test_a_reensure_reporting_an_unusable_runtime_is_not_adopted(
    tmp_path: Path,
):
    """A re-ensure that reports "could not be placed" is refused — and
    the refusal says *why*, not merely "not ready"."""
    agent = _agent_with(tmp_path)

    passed, detail = agent._service_reensure_gates(
        "chart", _unusable_runtime(),
    )

    assert passed is False
    assert "unusable" in detail


def test_a_vp_that_needs_an_unusable_service_is_blocked(
    tmp_path: Path, monkeypatch,
):
    """A VP that needs a service the round could not place is BLOCKED.

    Not FAILED: there is nowhere to send it.  The service is named by a
    placeholder that cannot resolve, so no port is left to match on —
    the block is by name.
    """
    import asyncio

    agent = _agent_with(tmp_path, runtime=_unusable_runtime())
    agent._blocked_service_ports = {3000}

    # The stranger is still sitting on the declared port: the recovery
    # probes it, finds it alive, and leaves it alone.
    monkeypatch.setattr(
        "service_freshness.probe_port",
        lambda port: _probe(port, ["/usr/bin/postgres"], start_epoch=1e12),
    )

    dispatched: list = []

    async def fake_run_single_vp(vp):
        dispatched.append(vp.get("id"))
        return {"status": "PASSED"}

    monkeypatch.setattr(agent, "_run_single_vp_async", fake_run_single_vp)

    plan = {
        "verification_points": [
            {
                "id": "VP-001",
                "verification_method": "code_review",
                "target_url": "curl {{svc.chart.url}}/api",
            }
        ]
    }
    resolved = agent._resolve_service_placeholders(plan)

    assert "127.0.0.1:3000" not in (
        resolved["verification_points"][0]["target_url"]
    ), (
        "the declared port is the stranger's — no path may fall back to it"
    )

    executor = agent._build_verification_executor(resolved)

    async def drive():
        return [
            await executor.sub_agent_runner(vp)
            for vp in executor.verification_plan["vps"]
        ]

    results = asyncio.run(drive())

    assert dispatched == [], dispatched
    assert results[0]["status"] == "BLOCKED", results[0]
    assert results[0]["evidence"].get("unusable_services") == ["chart"], (
        "the verdict has to name the service that could not be placed — a "
        "blocked port on its own does not say who was missing"
    )


# ---------------------------------------------------------------------------
# The acceptance command names these four (2026-10-25, repair-r1-34)
# ---------------------------------------------------------------------------
#
# The four cases below are the ones this round's acceptance command
# selects.  Each one reaches the same fact — a relocation that could
# not place the service — through a **different door**, so no single
# reader can be broken without one of them going red:
#
#   * the producer marks the runtime unusable;
#   * the round's address table refuses the service;
#   * no reader falls back to the declared port;
#   * the ports the relocation tried are in the log.
#
# The behaviour was implemented in repair-r1-29; what was missing were
# the cases the acceptance command names.  They reach it differently
# from their siblings above: the producer is called **directly** (not
# through ``ensure_services``), and the runtime that reaches the
# consumers is the one a real failed relocation produced rather than a
# hand-built one.


def _failed_relocation(project, monkeypatch, ports=(38921, 38922, 38923)):
    """The runtime a relocation that could not place the service hands back.

    ``_relocate_service`` is called directly — the producer of the
    runtime, without the ``ensure`` around it — so the contract is
    pinned where it is written.
    """
    port_iter = iter(ports)
    _stub_failed_start(monkeypatch)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: next(port_iter))
    monkeypatch.setattr(sm, "stop_service", lambda *a, **k: {"port_free": True})
    return sm._relocate_service(
        _decl(), project, foreign_cmdline="/usr/bin/postgres -D /var/db",
    )


def test_failed_relocation_runtime_is_unusable(project, monkeypatch):
    """The runtime a failed relocation hands back names **no** address.

    ``ready=False`` says "the service did not come up".  ``unusable``
    says something stronger: the port this record carries is the
    **declared** one, and the declared one belongs to somebody else —
    so it is not an address, and no reader may send a request to it.
    """
    runtime = _failed_relocation(project, monkeypatch)

    assert runtime.origin == "failed"
    assert runtime.ready is False
    assert runtime.unusable is True, (
        "a relocation that could not place the service must say so in a "
        "field; the port it names is somebody else's"
    )
    assert runtime.usable is False, (
        "the declared port is not an address — nothing may be sent to it"
    )
    assert runtime.port == 3000, (
        "the runtime names the declared port, which is the stranger's"
    )
    assert runtime.declared_port == 3000
    assert runtime.failure_reason, "the unusable runtime must say why"
    assert "relocating failed" in runtime.failure_reason


def test_effective_declarations_rejects_failed_runtime(
    tmp_path, project, monkeypatch,
):
    """The round's address table has no entry for a service that failed.

    The table the VPs read is built from the runtime map, and a runtime
    that names no address must not appear in it.  The runtime here is
    the one a real failed relocation produced — not a hand-built one —
    so the producer and the consumer are pinned together.
    """
    runtime = _failed_relocation(project, monkeypatch)

    agent = _agent_with(tmp_path, runtime=runtime)

    effective = agent._effective_declarations()
    assert "chart" not in effective, (
        f"a service that could not be placed must not be in the round's "
        f"address table; got {effective!r}"
    )
    assert "chart" in (agent._service_runtime_map.runtimes or {}), (
        "the runtime map still names the service — the table refused it, "
        "it did not simply lose it"
    )


def test_no_fallback_to_declared_port(tmp_path, project, monkeypatch):
    """The declared port is never used as an address for a failed service.

    The declared port is the stranger's.  Every reader that could hand
    it out refuses it: the address table, the binder the VPs are
    dispatched through, and the runtime's own answer to "may a request
    be sent here?".  A fallback would aim the round at the process
    squatting the port — which is how a round ends up graded on
    somebody else's server.
    """
    runtime = _failed_relocation(project, monkeypatch)
    agent = _agent_with(tmp_path, runtime=runtime)

    assert runtime.usable is False, (
        "the runtime's own answer must be 'not an address'"
    )
    assert "chart" not in agent._effective_declarations(), (
        "the address table must not fall back to the declared port"
    )
    assert agent._bind_service_runtime("chart", runtime) is None, (
        "the binder must refuse a runtime that names no address"
    )
    assert "chart" not in agent._service_bindings

    # The binder's own refusal, on the case the ``unusable`` flag
    # exists for: a runtime that says it is **ready** and still names
    # no address — the port it carries is the stranger's.  (A runtime
    # that simply did not come up is refused by the readiness check, so
    # it says nothing about the guard.)
    ready_but_not_ours = sm.ServiceRuntime(
        name="chart", port=3000, declared_port=3000, pid=None,
        start_cmd="npx next dev -p 3000", cwd=".", origin="spawned",
        ready=True, detail="the declared port is held by somebody else",
    )
    ready_but_not_ours.unusable = True
    assert agent._bind_service_runtime("chart", ready_but_not_ours) is None, (
        "a runtime that says 'this names no address' must not be bound, "
        "even when it says it is ready"
    )

    unusable = agent._unusable_services()
    assert "chart" in unusable, (
        f"the service that could not be placed must be named; got "
        f"{unusable!r}"
    )
    assert unusable["chart"]["port"] == 3000, (
        "the service must be reported against the port that is not ours"
    )


def test_retry_candidates_logged(project, tmp_path, monkeypatch):
    """Every port the relocation tried is in the plan's log.

    The chain that produced an address has to be reconstructible from
    the file alone — including the candidates that did **not** work,
    because those are the ports somebody will later ask about.  Each
    event names the port it tried and the declared port it moved away
    from, so the file answers "where did this port come from" on its
    own.
    """
    plan_dir = tmp_path / "plan-candidates"
    plan_dir.mkdir()
    ports = (38921, 38922, 38923)

    runtime = _failed_relocation(project, monkeypatch, ports=ports)
    runtime_map = sm.ServiceRuntimeMap(runtimes={"chart": runtime})
    sm.record_runtimes(
        plan_dir, "plan-candidates", runtime_map, project_dir=project,
    )

    attempts = [
        e for e in sm.read_address_events(plan_dir)
        if e.get("event") == "relocate_attempt"
    ]
    assert [e["new_port"] for e in attempts] == list(ports), (
        f"every candidate port must be in the log; got "
        f"{[e.get('new_port') for e in attempts]!r}"
    )
    for event in attempts:
        assert event["declared_port"] == 3000, (
            "a candidate event must name the port it moved away from"
        )
        assert str(event["new_port"]) in event["reason"], (
            f"the event must name the port it tried; got {event['reason']!r}"
        )
    assert list(runtime.candidate_ports) == list(ports), (
        "the runtime's own record must carry the ports it tried"
    )
    for port in ports:
        assert str(port) in runtime.failure_reason, (
            f"the failure reason must name the candidate port {port}; got "
            f"{runtime.failure_reason!r}"
        )


# ---------------------------------------------------------------------------
# 2026-10-25 — the data plane a service is serving
# ---------------------------------------------------------------------------
#
# "The service is up" and "the service is serving the round's data" are
# two different claims, and the round used to ask only the first one.
#
# The 2026-10-24 production round was graded on exactly that gap: the
# port answered, ``/health`` said 200 ``status: ok``, and the database
# behind it held **zero** routing rows — so every assertion that
# followed ran against an empty table, and the round reported the
# project as broken.
#
# So the readiness wait asks the second question as well, and the
# answer is recorded on the runtime: "it came up" and "it came up
# serving data" have to be two separately readable facts.

#: A real HTTP server that answers ``/health`` and ``/health/data``.
#:
#: It is a *real* server on purpose: the probe has to travel the same
#: road a VP's request travels, and a stubbed function would prove
#: nothing about that.
class _DataPlaneServer:
    def __init__(
        self, *, rows: int = 4, health_status: int = 200,
        data_status: int = 200, state_db_path: str = "",
    ):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.rows = rows
        self.health_status = health_status
        self.data_status = data_status
        #: The ``instance`` block the instance publishes.  It is read
        #: **per request**, so a test may replace it with a hostile one
        #: (no block at all, a null path, a path that is not a string).
        self.instance: Optional[dict] = {
            "boot_id": "boot-1234", "state_db_path": state_db_path,
        }
        #: One entry per request: the path that was asked for.
        self.requests: list = []
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 - the handler API's own name
                server.requests.append(self.path)
                if self.path.split("?")[0].endswith("/health/data"):
                    status = server.data_status
                    payload = {
                        "status": "ok",
                        "plan_routing_rows": server.rows,
                    }
                    if server.instance is not None:
                        payload["instance"] = server.instance
                    body = json.dumps(payload).encode()
                else:
                    status = server.health_status
                    body = b'{"status": "ok"}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # noqa: A003 - silence the log
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _data_plane_decl(server: _DataPlaneServer) -> ServiceDeclaration:
    """A declaration that points at *server*."""
    return _decl(
        name="api",
        port=server.port,
        start_cmd="true",
        health_url=f"http://127.0.0.1:{server.port}/health",
    )


def test_the_data_plane_probe_reads_the_instance_fact_sheet():
    """The probe answers with the instance's own fact sheet.

    ``/health/data`` publishes three things: the state of the data
    plane, how many routing rows it holds, and — in its ``instance``
    block — which process is answering.
    """
    server = _DataPlaneServer(rows=4)
    try:
        evidence = sm.data_plane_evidence(server.port)
    finally:
        server.close()

    assert evidence.verdict == sm.DATA_PLANE_PROVED, evidence.to_dict()
    assert evidence.proved is True
    assert evidence.blocked is False
    assert evidence.plan_routing_rows == 4
    assert evidence.boot_id == "boot-1234"
    assert evidence.status == "ok"


def test_data_plane_evidence_records_the_state_db_the_instance_reports():
    """The instance names the database it is serving.

    A round has to judge an instance against **its own** database —
    the one its ``/health/data`` names — and not against the one the
    prober happens to have open.  The evidence therefore carries the
    path the instance published, so the reader of the log can see
    which state database answered.
    """
    server = _DataPlaneServer(
        rows=4, state_db_path="/tmp/p/.pdt/verify/state.db",
    )
    try:
        evidence = sm.data_plane_evidence(server.port)
    finally:
        server.close()

    assert evidence.state_db_path == "/tmp/p/.pdt/verify/state.db"
    assert evidence.to_dict()["state_db_path"] == "/tmp/p/.pdt/verify/state.db"


def test_a_missing_or_unusable_state_db_path_leaves_the_evidence_empty():
    """No instance block, no path, a null path, a number: all the same.

    The probe never raises, and an instance that does not say which
    database it serves must not be read as saying "the wrong one":
    the field stays empty and the verdict is untouched.
    """
    hostile = (
        None,                                             # no instance block
        {"boot_id": "boot-1234"},                         # no path at all
        {"boot_id": "boot-1234", "state_db_path": None},   # null
        {"boot_id": "boot-1234", "state_db_path": 17},     # not a string
    )
    for instance in hostile:
        server = _DataPlaneServer(rows=4)
        server.instance = instance
        try:
            evidence = sm.data_plane_evidence(server.port)
        finally:
            server.close()

        assert evidence.state_db_path == "", instance
        assert evidence.to_dict()["state_db_path"] == "", instance
        assert evidence.verdict == sm.DATA_PLANE_PROVED, instance
        assert evidence.boot_id == (
            "boot-1234" if instance else ""
        ), instance


def test_an_empty_data_plane_is_not_a_proof():
    """200 with zero rows is an answer — and it is not a good one."""
    server = _DataPlaneServer(rows=0)
    try:
        evidence = sm.data_plane_evidence(server.port)
    finally:
        server.close()

    assert evidence.verdict == sm.DATA_PLANE_EMPTY, evidence.to_dict()
    assert evidence.plan_routing_rows == 0
    assert evidence.blocked is True


def test_a_service_without_a_data_plane_is_not_gated():
    """404 — the instance does not publish a data plane at all.

    A project service with no database behind it is not a service
    serving the wrong database, so it is never gated on one.
    """
    server = _DataPlaneServer(data_status=404)
    try:
        evidence = sm.data_plane_evidence(server.port)
    finally:
        server.close()

    assert evidence.verdict == sm.DATA_PLANE_ABSENT, evidence.to_dict()
    assert evidence.blocked is False


def test_a_broken_data_plane_is_not_a_proof():
    """The endpoint exists but cannot answer: that is not usable data."""
    server = _DataPlaneServer(data_status=503)
    try:
        evidence = sm.data_plane_evidence(server.port)
    finally:
        server.close()

    assert evidence.verdict == sm.DATA_PLANE_BROKEN, evidence.to_dict()
    assert evidence.blocked is True


def test_readiness_needs_a_data_plane_with_routing_rows(monkeypatch):
    """A service that is up and serving nothing is not ready.

    The round has four routing rows to serve; an instance that reports
    none is not serving them, so it must not become the round's
    address — every assertion that followed would be graded against an
    empty table.
    """
    monkeypatch.setattr(
        "service_freshness.expected_routing_rows", lambda *a, **k: 4,
    )
    server = _DataPlaneServer(rows=0)
    try:
        ready = sm.wait_until_ready(
            _data_plane_decl(server), timeout_seconds=2,
        )
    finally:
        server.close()

    assert ready is False, (
        "a service that serves no routing rows must not be ready"
    )


def test_readiness_accepts_a_service_serving_routing_rows(monkeypatch):
    """The same service, serving the round's rows, is ready."""
    monkeypatch.setattr(
        "service_freshness.expected_routing_rows", lambda *a, **k: 4,
    )
    server = _DataPlaneServer(rows=4)
    try:
        ready, evidence = sm.wait_until_ready_with_evidence(
            _data_plane_decl(server), timeout_seconds=2,
        )
    finally:
        server.close()

    assert ready is True
    assert evidence.verdict == sm.DATA_PLANE_PROVED, evidence.to_dict()
    assert evidence.plan_routing_rows == 4


def test_a_service_without_a_data_plane_is_still_ready(monkeypatch):
    """No data plane of its own → never gated on one."""
    monkeypatch.setattr(
        "service_freshness.expected_routing_rows", lambda *a, **k: 4,
    )
    server = _DataPlaneServer(data_status=404)
    try:
        ready, evidence = sm.wait_until_ready_with_evidence(
            _data_plane_decl(server), timeout_seconds=2,
        )
    finally:
        server.close()

    assert ready is True
    assert evidence.verdict == sm.DATA_PLANE_ABSENT, evidence.to_dict()


def test_the_runtime_carries_the_data_plane(monkeypatch):
    """The round's log has to be able to say "it came up serving data".

    ``services_ensured`` is written from the runtime map, so the
    evidence has to be on the runtime: ``status``,
    ``plan_routing_rows`` and ``boot_id`` — the three facts a reader
    needs to tell "it came up" from "it came up serving *this* data".
    """
    monkeypatch.setattr(
        "service_freshness.expected_routing_rows", lambda *a, **k: 4,
    )
    server = _DataPlaneServer(rows=4)
    try:
        runtime = sm.ServiceRuntime(
            name="api", port=server.port, origin="spawned", ready=True,
        )
        sm._record_data_plane(runtime)
    finally:
        server.close()

    assert runtime.data_plane is not None
    assert runtime.data_plane.plan_routing_rows == 4
    assert runtime.data_plane.boot_id == "boot-1234"

    payload = sm.ServiceRuntimeMap(runtimes={"api": runtime}).to_dict()
    recorded = payload["data_plane"]["api"]
    assert recorded["plan_routing_rows"] == 4
    assert recorded["boot_id"] == "boot-1234"
    assert recorded["status"] == "ok"


# ---------------------------------------------------------------------------
# The data-plane gate judges the database the service itself names
# ---------------------------------------------------------------------------
#
# 2026-10-27, fact A: a copy relocated onto a free port, serving the
# copy's **own** empty state database, was refused for ever.  The gate
# compared the copy's empty database against the *round's* fixture —
# a database that holds rows the copy never had and never would have.
#
# The judgement now comes from the two statements the service made
# about its own data: the database its **declaration** names, and the
# database its **answer** names.


def _routing_db(path: Path, rows: int) -> Path:
    """A state database holding *rows* ``plan_routing`` rows."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE plan_routing (plan_id TEXT PRIMARY KEY)")
        for index in range(rows):
            conn.execute(
                "INSERT INTO plan_routing (plan_id) VALUES (?)",
                (f"plan-{index}",),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _db_decl() -> ServiceDeclaration:
    """A declaration that names the state database it serves."""
    return _decl(
        name="api",
        start_cmd=(
            "export PDT_STATE_DB_PATH=$PWD/.pdt/verify/state.db "
            "&& exec python -m api.server"
        ),
    )


def test_an_instance_is_judged_by_the_db_its_declaration_names(tmp_path):
    """The declaration names the database; the instance names the same one.

    The round's own fixture has rows, but the service serves a
    database it declared for itself, and that database is empty.  An
    empty instance is therefore exactly what the declaration asks for
    — refusing it strands the round (2026-10-27, fact A).
    """
    db = _routing_db(tmp_path / "project" / ".pdt" / "verify" / "state.db", 0)
    evidence = sm.DataPlaneEvidence(
        verdict=sm.DATA_PLANE_EMPTY, plan_routing_rows=0,
        state_db_path=str(db),
    )

    assert sm._data_plane_is_acceptable(evidence, 4, _db_decl()) is True


def test_an_instance_serving_a_foreign_db_is_refused(tmp_path):
    """The instance names a database the declaration never named.

    That is a service serving data the round did not ask for, and it
    is refused whatever the round's own fixture holds.
    """
    _routing_db(tmp_path / "project" / ".pdt" / "verify" / "state.db", 0)
    foreign = _routing_db(tmp_path / "elsewhere" / "state.db", 0)
    evidence = sm.DataPlaneEvidence(
        verdict=sm.DATA_PLANE_EMPTY, plan_routing_rows=0,
        state_db_path=str(foreign),
    )

    assert sm._data_plane_is_acceptable(evidence, 4, _db_decl()) is False


def test_a_declared_db_with_rows_still_refuses_an_empty_instance(tmp_path):
    """"It should have data and it says it has none" is still a refusal.

    The database the declaration names holds rows, so the instance that
    reports none of them is not serving that database — whatever the
    round's own fixture says.
    """
    db = _routing_db(tmp_path / "project" / ".pdt" / "verify" / "state.db", 3)
    evidence = sm.DataPlaneEvidence(
        verdict=sm.DATA_PLANE_EMPTY, plan_routing_rows=0,
        state_db_path=str(db),
    )

    assert sm._data_plane_is_acceptable(evidence, 4, _db_decl()) is False


def test_the_declared_state_db_path_is_read_off_the_declaration():
    """The declaration names the database — in the shape it spells it.

    ``$PWD`` (and ``$(pwd)``, and ``${PWD}``) mean "the directory the
    service runs in", and the quotes are shell quoting.  What is left
    is the path *inside* that directory — the tail the instance's own
    answer carries.
    """
    for spelling in (
        "$PWD/.pdt/verify/state.db",
        "${PWD}/.pdt/verify/state.db",
        "$(pwd)/.pdt/verify/state.db",
        '"$PWD/.pdt/verify/state.db"',
        "$PWD/./.pdt/verify/state.db",
    ):
        decl = _decl(
            start_cmd=(
                f"export PDT_STATE_DB_PATH={spelling} "
                "&& exec python -m api.server"
            ),
        )
        assert sm.declared_state_db_path(decl) == ".pdt/verify/state.db", spelling

    assert sm.declared_state_db_path(
        _decl(start_cmd="npx next dev -p 3000"),
    ) == ""


# ---------------------------------------------------------------------------
# 2026-10-27 — the two call sites get the declaration
# ---------------------------------------------------------------------------
#
# The predicate (:func:`_data_plane_is_acceptable`) judges an instance by
# the database its **declaration** names and the database its **answer**
# names.  The two call sites were still calling it without the
# declaration, so the judgement fell back to the round's own fixture — a
# database that has nothing to do with the one the service serves.


def test_a_relocated_copy_is_ready_when_it_serves_its_own_declared_db(
    tmp_path, monkeypatch,
):
    """A copy on a free port, serving the database it declared, is ready.

    The round's own fixture holds rows the copy never had (2026-10-27,
    fact A): the copy declares the database it is going to serve, and it
    serves it.  An empty database is exactly what that declaration asks
    for, so the wait must accept the copy instead of waiting out the
    timeout.

    The instance does not name its database — that is the shape the
    failing round recorded (``state_db_path`` was not in the answer
    then) — so the **declaration** is the only statement about the data
    plane there is.  The name it carries is relative to the directory
    the service runs in, so the wait runs in that directory: the round
    is asking about the copy's database, not about its own.
    """
    monkeypatch.setattr(
        "service_freshness.expected_routing_rows", lambda *a, **k: 4,
    )
    project = tmp_path / "project"
    _routing_db(project / ".pdt" / "verify" / "state.db", 0)
    monkeypatch.chdir(project)
    server = _DataPlaneServer(rows=0)
    try:
        decl = _decl(
            port=server.port,
            health_url=f"http://127.0.0.1:{server.port}/health",
            start_cmd=(
                "export PDT_STATE_DB_PATH=$PWD/.pdt/verify/state.db && true"
            ),
        )
        ready = sm.wait_until_ready(decl, timeout_seconds=2)
    finally:
        server.close()

    assert ready is True, (
        "a copy that serves the database it declared must be ready, even "
        "when the round's own fixture holds rows the copy never had"
    )
    evidence = sm._take_data_plane(decl.port)
    assert evidence.verdict == sm.DATA_PLANE_EMPTY, evidence.to_dict()


def test_an_adopted_instance_serving_a_foreign_db_is_refused(tmp_path):
    """An adopted instance that serves a database the declaration never named.

    The listener was already there when the round arrived: it is
    adopted, it answers 200 — and it serves somebody else's database.
    The gate has to refuse it (2026-10-27, fact A), which it can only do
    if it is given the service's own declaration: the instance's answer
    names a foreign database, and the declaration says which database
    this service was supposed to serve.
    """
    _routing_db(tmp_path / "project" / ".pdt" / "verify" / "state.db", 0)
    foreign = _routing_db(tmp_path / "elsewhere" / "state.db", 0)
    runtime = sm.ServiceRuntime(
        name="api", port=65001, origin="adopted", ready=True,
    )
    runtime.data_plane = sm.DataPlaneEvidence(
        verdict=sm.DATA_PLANE_EMPTY, plan_routing_rows=0,
        state_db_path=str(foreign),
    )

    sm._apply_data_plane_gate(runtime, 4, _db_decl())

    assert runtime.ready is False, (
        "an adopted instance serving a database the declaration never "
        "named must not be used as the round's address"
    )


# ---------------------------------------------------------------------------
# Fact A, end to end: a stranger on the declared port, a real relocation,
# a real readiness verdict
# ---------------------------------------------------------------------------
#
# Everything above stubs the process table: the "stranger" is a probe
# result and the copy is a stubbed start.  Fact A happened to a **real**
# round — a real process held the declared port, a real copy was started
# on a free one, and a real readiness wait decided whether that copy was
# usable.  No stub can answer that: it cannot hold a port, cannot serve
# a health URL, cannot name a database, and cannot still be alive after
# the round walked away from it.
#
# So this case runs the whole chain on real processes:
#
#   * a stranger — a process this module did not start — holds the
#     declared port;
#   * the round relocates: a copy starts on a free port, serving the
#     database it declared for itself;
#   * the readiness wait judges that copy by **its own** database and not
#     by the round's fixture, which holds rows the copy never had;
#   * the stranger is still alive afterwards — a port held by a process
#     we cannot positively identify is not ours to kill, so the round
#     gets out of the way instead (arch decision 4: R4).


#: The source of the stand-in service.  A file rather than an inline
#: ``-c`` script, because the start command has to name a program the
#: module can recognise (``svc.py``) — a declaration whose identity
#: tokens are empty matches nothing, and a stranger would then be
#: adopted instead of relocated around.
_STANDALONE_SERVICE_SOURCE = '''\
"""A stand-in for a PDT service.

``/health``      -> 200
``/health/data`` -> 200, with the number of ``plan_routing`` rows in the
                    database this process was started on and the path of
                    that database (the instance fact sheet).
"""
import argparse
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _routing_rows(db_path):
    """How many plan_routing rows the database holds."""
    try:
        conn = sqlite3.connect(
            Path(db_path).resolve().as_uri() + "?mode=ro", uri=True,
            timeout=2.0,
        )
    except Exception:
        return 0
    try:
        row = conn.execute("SELECT COUNT(*) FROM plan_routing").fetchone()
    except Exception:
        return 0
    finally:
        conn.close()
    return int(row[0]) if row else 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.split("?")[0].endswith("/health/data"):
            rows = _routing_rows(self.server.db_path)
            payload = {
                "status": "ok",
                "plan_routing_rows": rows,
                "instance": {
                    "boot_id": "stand-in-%d" % os.getpid(),
                    "pid": os.getpid(),
                    "state_db_path": self.server.db_path,
                    "plan_routing_rows": rows,
                },
            }
        else:
            payload = {"status": "ok"}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.db_path = os.path.abspath(args.db)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
'''


def _wait_for_a_listener(port: int, proc, timeout: float = 20.0) -> None:
    """Wait until *proc* is listening on *port*.

    Fails loudly when the process died on the way there — a stranger
    that never came up would make the whole case vacuous.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"the stranger on port {port} exited with {proc.returncode} "
                f"before it started listening"
            )
        if sm.probe_port(port).listening:
            return
        time.sleep(0.1)
    raise AssertionError(
        f"nothing was listening on port {port} after {timeout}s"
    )


@pytest.mark.integration
@pytest.mark.timeout(120)
def test_end_to_end_relocation_around_a_stranger_ends_ready(
    tmp_path: Path, monkeypatch,
):
    """A real stranger, a real relocation, a real readiness verdict.

    The shape fact A had (2026-10-27): the declared port was held by a
    process nobody could identify, so the round started a copy on a free
    port — and the copy serves the database **it** declared, which is
    empty, while the round's own fixture holds rows the copy never had.

    Three things have to hold at once, and only the real chain can show
    them holding at once:

      * the copy ends up **ready** — judged by the database it declared
        and named, not by the round's fixture;
      * the copy is on a **port of its own** — the stranger's port was
        not taken from it;
      * the stranger is **still alive** — the round got out of the way
        instead of killing a process it could not identify (R4).

    And the address event stream has to say where the port came from:
    the "relocated" event names the port that was held and the port the
    copy landed on.
    """
    import subprocess

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    # The round's own fixture: four routing rows.  This is the database
    # the round falls back to when the instance does not say which
    # database it serves — and it is NOT the database this service
    # serves.  It is what made the copy look wrong.
    round_fixture_db = _routing_db(tmp_path / "round_fixture.db", 4)
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(round_fixture_db))
    assert sm._expected_routing_rows() == 4, (
        "the round's own fixture must hold rows for this case to mean "
        "anything: the copy's database is the empty one"
    )

    # The database the service declares for itself: the same table, no
    # rows.  It lives under the directory the service runs in, which is
    # what ``$PWD/.pdt/verify/state.db`` names.
    service_db = _routing_db(
        project_dir / ".pdt" / "verify" / "state.db", 0,
    )
    (project_dir / "svc.py").write_text(_STANDALONE_SERVICE_SOURCE)

    # A stranger holds the declared port.  A real process, in its own
    # session, started by nobody in this module.
    declared_port = sm.allocate_free_port()
    assert declared_port, "could not get a free port for the stranger"
    stranger = subprocess.Popen(
        [
            sys.executable, "-c",
            "import socket,time;s=socket.socket();"
            "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
            f"s.bind(('127.0.0.1',{declared_port}));s.listen(5);"
            "time.sleep(300)",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    runtime = None
    try:
        _wait_for_a_listener(declared_port, stranger)
        assert str(declared_port) not in str(service_db), (
            "the declared port must not appear in the database path: the "
            "relocation rewrites every standalone occurrence of the port"
        )

        decl = _decl(
            name="svc",
            port=declared_port,
            cwd=".",
            start_cmd=(
                "export PDT_STATE_DB_PATH=$PWD/.pdt/verify/state.db && "
                f"{shlex.quote(sys.executable)} svc.py "
                f"--port {declared_port} "
                f"--db {shlex.quote(str(service_db))}"
            ),
            health_url=f"http://127.0.0.1:{declared_port}/health",
            ready_timeout_seconds=30,
        )

        result = sm.ensure_services(
            project_dir, {"svc": decl}, plan_dir=plan_dir,
        )
        runtime = result.runtimes["svc"]

        # The copy came up, on a port of its own, serving its own
        # database — the round's fixture never entered into it.
        assert runtime.ready is True, runtime.detail
        assert runtime.origin == "spawned"
        assert runtime.port != declared_port, (
            "the copy must have moved off the port the stranger holds"
        )
        assert runtime.declared_port == declared_port
        assert runtime.data_plane is not None, (
            "the readiness wait must have recorded what the copy serves"
        )
        assert Path(runtime.data_plane.state_db_path).resolve() == (
            service_db.resolve()
        ), (
            "the copy must serve the database it declared for itself"
        )

        # The stranger is untouched: the round got out of the way.
        assert stranger.poll() is None, (
            "the stranger must still be running: a port held by a process "
            "we cannot positively identify is not ours to kill"
        )

        # And the address event stream says where the port came from.
        events = sm.read_address_events(plan_dir)
        kinds = [event["event"] for event in events]
        assert "relocated" in kinds, kinds
        relocated = [e for e in events if e["event"] == "relocated"][0]
        assert relocated["old_port"] == declared_port
        assert relocated["new_port"] == runtime.port
    finally:
        if runtime is not None:
            sm.stop_service(runtime, grace_seconds=5.0)
        stranger.kill()
        stranger.wait(timeout=10)
