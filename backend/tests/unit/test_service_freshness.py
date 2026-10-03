"""Unit tests for the service-freshness deterministic core.

Covers:
  * ``extract_service_ports`` — URL / host:port / flag / env spellings,
    dedup+sort, protected-port (backend runtime) filtering, both plan schemas.
  * ``probe_port`` — listener / no-listener / probe-failure / protected.
  * ``classify_probe`` — the restart decision matrix.
  * ``newest_project_source_mtime`` — pruning of venv/target/data dirs
    and non-source extensions.
  * ``wait_for_ready`` — real socket accept, timeout on closed port,
    protected-port refusal.
  * ``assess_service_freshness`` — end-to-end with a stubbed prober.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# extract_service_ports
# ---------------------------------------------------------------------------

def _plan_with_commands(*commands: str) -> dict:
    return {
        "verification_points": [
            {"id": f"VP-{i:03d}", "test_command": cmd}
            for i, cmd in enumerate(commands, start=1)
        ]
    }


def test_extract_http_url_port() -> None:
    from service_freshness import extract_service_ports
    plan = _plan_with_commands(
        "curl -s 'http://127.0.0.1:8080/api/items/1min/EXAMPLE' | python check.py",
    )
    assert extract_service_ports(plan) == [8080]


def test_extract_localhost_and_flags() -> None:
    from service_freshness import extract_service_ports
    plan = _plan_with_commands(
        "curl http://localhost:9100/metrics",
        "uvicorn api.api_server:app --port 8080",
        "API_PORT=8080 ./run.sh",
    )
    assert extract_service_ports(plan) == [8080, 9100]


def test_extract_dedup_and_sort() -> None:
    from service_freshness import extract_service_ports
    plan = _plan_with_commands(
        "curl http://127.0.0.1:9200/a",
        "curl http://127.0.0.1:8080/b",
        "curl http://127.0.0.1:9200/c",
    )
    assert extract_service_ports(plan) == [8080, 9200]


def test_extract_filters_protected_ports() -> None:
    """Ports owned by the backend runtime must never enter the restart set."""
    from service_freshness import extract_service_ports
    plan = _plan_with_commands(
        "curl http://127.0.0.1:8000/api/plans",   # the backend
        "curl http://127.0.0.1:8002/health",      # scheduler/supervisor
        "curl http://127.0.0.1:8003/rank",        # external helper
        "curl http://127.0.0.1:8080/api/items",
    )
    assert extract_service_ports(plan) == [8080]


def test_extract_no_ports() -> None:
    from service_freshness import extract_service_ports
    plan = _plan_with_commands("cargo test --workspace", "pytest -q")
    assert extract_service_ports(plan) == []


def test_extract_scans_vp_vps_schema_and_text_fields() -> None:
    from service_freshness import extract_service_ports
    plan = {
        "description": "endpoint check against http://localhost:8080/",
        "vps": [{"id": "VP-1", "title": "api server on 127.0.0.1:8080"}],
    }
    assert extract_service_ports(plan) == [8080]


def test_extract_rejects_out_of_range() -> None:
    from service_freshness import extract_service_ports
    plan = _plan_with_commands("curl http://127.0.0.1:99999/x")  # > 65535
    assert extract_service_ports(plan) == []


# ---------------------------------------------------------------------------
# probe_port (subprocess stubbed)
# ---------------------------------------------------------------------------

def _stub_run(monkeypatch, lsof_out, ps_cmd_out, ps_lstart_out):
    from service_freshness import _run_probe_cmd

    def fake(argv, timeout=10):
        joined = " ".join(argv)
        # Matched on the program name, not on the exact flag spelling.
        # It used to be `argv[:2] == ["lsof", "-ti"]`, and when the query
        # became listener-only that predicate stopped matching — the stub
        # fell through to `return None`, and the probe tests went on
        # passing against a probe that had been stubbed out. A stub that
        # silently stops matching is worse than no stub.
        if argv and argv[0] == "lsof":
            return lsof_out
        if joined.endswith("command="):
            return ps_cmd_out
        if joined.endswith("lstart="):
            return ps_lstart_out
        return None

    monkeypatch.setattr(
        "service_freshness._run_probe_cmd", fake,
    )


def test_probe_listening_process(monkeypatch) -> None:
    from service_freshness import probe_port
    _stub_run(monkeypatch, "4984\n", "/proj/venv1/bin/uvicorn api.api_server:app",
              "Thu Sep  4 09:32:01 2026")
    probe = probe_port(8080)
    assert probe.listening is True
    assert probe.pids == [4984]
    assert "uvicorn" in probe.cmdlines[0]
    assert probe.process_start_epoch is not None
    assert probe.error == ""


def test_probe_no_listener(monkeypatch) -> None:
    from service_freshness import probe_port
    _stub_run(monkeypatch, "", None, None)
    probe = probe_port(8080)
    assert probe.listening is False
    assert probe.pids == []
    assert probe.error == ""


def test_probe_lsof_failure_sets_error(monkeypatch) -> None:
    from service_freshness import probe_port
    monkeypatch.setattr("service_freshness._run_probe_cmd", lambda *a, **k: None)
    probe = probe_port(8080)
    assert probe.listening is False
    assert "lsof" in probe.error


def test_probe_protected_port_refused() -> None:
    from service_freshness import probe_port
    probe = probe_port(8000)
    assert probe.listening is False
    assert "protected" in probe.error


def test_parse_ps_lstart_macos_format() -> None:
    from service_freshness import _parse_ps_lstart
    epoch = _parse_ps_lstart("Thu Sep  4 09:32:01 2026")
    assert epoch is not None
    assert epoch > 0
    assert _parse_ps_lstart("not a date") is None
    assert _parse_ps_lstart("") is None


# ---------------------------------------------------------------------------
# classify_probe
# ---------------------------------------------------------------------------

def _mk_probe(listening=False, error="", start=None):
    from service_freshness import PortProbe
    return PortProbe(
        port=8080, listening=listening, pids=[1] if listening else [],
        process_start_epoch=start, error=error,
    )


def test_classify_no_listener_restarts() -> None:
    from service_freshness import classify_probe, DECISION_RESTART
    assert classify_probe(_mk_probe(), None) == DECISION_RESTART


def test_classify_probe_error_restarts() -> None:
    from service_freshness import classify_probe, DECISION_RESTART
    assert classify_probe(
        _mk_probe(listening=True, error="lsof probe failed"), None,
    ) == DECISION_RESTART


def test_classify_stale_process_restarts() -> None:
    from service_freshness import classify_probe, DECISION_RESTART
    # process started at t=100, newest source at t=200 → stale
    assert classify_probe(_mk_probe(listening=True, start=100.0), 200.0) == DECISION_RESTART


def test_classify_fresh_process_healthy() -> None:
    from service_freshness import classify_probe, DECISION_HEALTHY
    assert classify_probe(_mk_probe(listening=True, start=300.0), 200.0) == DECISION_HEALTHY


def test_classify_unknown_start_time_is_healthy() -> None:
    """No start-time evidence → don't restart a live listener blindly."""
    from service_freshness import classify_probe, DECISION_HEALTHY
    assert classify_probe(_mk_probe(listening=True, start=None), 200.0) == DECISION_HEALTHY


def test_classify_protected_port_never_restarts() -> None:
    from service_freshness import classify_probe, DECISION_HEALTHY, PortProbe
    probe = PortProbe(port=8000)
    assert classify_probe(probe, None) == DECISION_HEALTHY


# ---------------------------------------------------------------------------
# newest_project_source_mtime
# ---------------------------------------------------------------------------

def test_newest_source_mtime_prunes_generated_dirs(tmp_path: Path) -> None:
    from service_freshness import newest_project_source_mtime
    src = tmp_path / "src"
    src.mkdir()
    old = src / "app.py"
    old.write_text("# old\n")
    os.utime(old, (1_000_000, 1_000_000))

    # A file inside target/ with a NEWER mtime must be ignored.
    target = tmp_path / "target" / "release"
    target.mkdir(parents=True)
    artefact = target / "libfoo.so"
    artefact.write_text("binary")
    os.utime(artefact, (2_000_000, 2_000_000))

    # venv content is also ignored.
    venv = tmp_path / "venv1" / "lib"
    venv.mkdir(parents=True)
    site = venv / "site.py"
    site.write_text("# venv\n")
    os.utime(site, (3_000_000, 3_000_000))

    assert newest_project_source_mtime(tmp_path) == 1_000_000


def test_newest_source_mtime_ignores_non_source_extensions(tmp_path: Path) -> None:
    from service_freshness import newest_project_source_mtime
    data = tmp_path / "data.csv"
    data.write_text("1,2,3\n")
    os.utime(data, (5_000_000, 5_000_000))
    code = tmp_path / "main.rs"
    code.write_text("// x\n")
    os.utime(code, (1_500_000, 1_500_000))
    assert newest_project_source_mtime(tmp_path) == 1_500_000


def test_newest_source_mtime_empty_tree(tmp_path: Path) -> None:
    from service_freshness import newest_project_source_mtime
    assert newest_project_source_mtime(tmp_path) is None


# ---------------------------------------------------------------------------
# wait_for_ready
# ---------------------------------------------------------------------------

def test_wait_for_ready_real_socket() -> None:
    from service_freshness import wait_for_ready
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert wait_for_ready(port, timeout_seconds=5) is True
    finally:
        srv.close()


def test_wait_for_ready_times_out_on_closed_port() -> None:
    from service_freshness import wait_for_ready
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()  # port now closed
    start = time.monotonic()
    assert wait_for_ready(port, timeout_seconds=2, poll_interval=0.2) is False
    assert time.monotonic() - start < 5


def test_wait_for_ready_refuses_protected_port() -> None:
    from service_freshness import wait_for_ready
    start = time.monotonic()
    assert wait_for_ready(8000, timeout_seconds=30) is False
    # Must short-circuit, not burn the 30s timeout.
    assert time.monotonic() - start < 2


# ---------------------------------------------------------------------------
# assess_service_freshness (prober stubbed)
# ---------------------------------------------------------------------------

def test_assess_no_ports_is_noop(tmp_path: Path) -> None:
    from service_freshness import assess_service_freshness
    report = assess_service_freshness(tmp_path, {"verification_points": []})
    assert report.action == "noop"
    assert report.needs_restart == []


def test_assess_marks_dead_ports_restart_pending(tmp_path: Path, monkeypatch) -> None:
    from service_freshness import assess_service_freshness, PortProbe
    (tmp_path / "main.py").write_text("# x\n")
    monkeypatch.setattr(
        "service_freshness.probe_port", lambda port: PortProbe(port=port),
    )
    plan = _plan_with_commands("curl http://127.0.0.1:8080/api/items")
    report = assess_service_freshness(tmp_path, plan)
    assert report.action == "restart_pending"
    assert report.needs_restart == [8080]


def test_assess_healthy_when_listener_fresh(tmp_path: Path, monkeypatch) -> None:
    from service_freshness import assess_service_freshness, PortProbe
    src = tmp_path / "main.py"
    src.write_text("# x\n")
    os.utime(src, (1_000_000, 1_000_000))
    monkeypatch.setattr(
        "service_freshness.probe_port",
        lambda port: PortProbe(
            port=port, listening=True, pids=[7],
            process_start_epoch=2_000_000,
        ),
    )
    plan = _plan_with_commands("curl http://127.0.0.1:8080/api/items")
    report = assess_service_freshness(tmp_path, plan)
    assert report.action == "healthy"
    assert report.needs_restart == []


# ---------------------------------------------------------------------------
# T1 — the probe's own contract, against a real kernel
# ---------------------------------------------------------------------------
#
# Everything above stubs `_run_probe_cmd`, so it cannot catch the probe
# being wrong about lsof's semantics — it only pins what this code asks
# for. This one runs the real query against a real socket.
#
# The scenario is the one that was silently breaking callers: a client
# that connected, whose peer has since closed. The kernel leaves its
# socket in CLOSE_WAIT with the *server's* port as the remote port, and
# `lsof -ti :PORT` matches that. Before the listener-only query this
# reported a listener for a port the kernel refuses a connection to.


def test_probe_does_not_mistake_a_close_wait_client_for_a_listener():
    """A lingering client must not read as a process listening.

    This is the shape a container runtime produces in bulk: a few hundred
    `CLOSED` sockets whose *remote* port is some number, none of which
    are listening. Measured shadow rate on a developer machine, 4.8-8.0%
    of ephemeral ports — high enough to make a per-port decision a coin
    flip, and `stop_service` acted on it.
    """
    import socket as _socket

    from service_freshness import probe_port

    server = _socket.socket()
    server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server_port = server.getsockname()[1]

    client = _socket.create_connection(("127.0.0.1", server_port), timeout=5)
    client_port = client.getsockname()[1]

    # The listener closes first; the client is left in CLOSE_WAIT still
    # holding `server_port` as its remote port.
    server.close()

    try:
        # The kernel is the arbiter: nothing is listening on client_port.
        with pytest.raises(OSError):
            _socket.create_connection(("127.0.0.1", client_port), timeout=2).close()

        probe = probe_port(client_port)
        assert not probe.listening, (
            f"probe_port({client_port}) claims a listener, but the kernel "
            f"refuses a connection. The pid list was {probe.pids} — those "
            f"are the client, in CLOSE_WAIT with a remote port that happens "
            f"to equal {client_port}."
        )
        assert probe.pids == []
    finally:
        client.close()


def test_probe_still_finds_a_real_listener():
    """The same query must not have been narrowed into uselessness."""
    import socket as _socket

    from service_freshness import probe_port

    server = _socket.socket()
    server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        probe = probe_port(server.getsockname()[1])
        assert probe.listening, "a real LISTEN socket was not found"
    finally:
        server.close()
