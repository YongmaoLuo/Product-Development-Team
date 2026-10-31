"""Unit tests for the service address event log (2026-10-25, repair-r1-25).

The criterion this file pins
----------------------------
VP-005 failed on a round that talked to a port (51725) that nothing had
ever listened on, and **nothing on disk said where that number came
from**.  The declared port, the port the round was relocated to and the
port it finally used each existed for a while, and none of them left a
trace: the ledger records only the *last* address, so the chain that
produced it could not be reconstructed.

So every address change is written down, as it happens, in one file —
``<plan_dir>/logs/service_address_events.jsonl`` — and the whole chain
under one run is one file read.

The four tests the criterion names:

  * ``test_every_port_assignment_emits_event`` — declared → relocated →
    bound → reap leaves connected events, and the event is on disk
    before the ledger names the address.
  * ``test_event_contains_run_and_round_token`` — every event names the
    run and the round it belongs to.
  * ``test_address_events_survive_reap`` — the reap does not truncate
    the history.
  * ``test_trace_reconstructs_51725`` — given the port nobody listened
    on, the stream says which call allocated it and which pid did.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402
from service_freshness import PortProbe  # noqa: E402


#: The port the plan declared.
DECLARED_PORT = 18090

#: The port the round was moved to when a stranger held the declared one.
RELOCATED_PORT = 58396

#: The port that ended up in the failing request — the one this file
#: exists to make traceable.
THE_PORT_NOBODY_LISTENED_ON = 51725

#: The event schema the criterion names.  Exactly these keys, one JSON
#: object per line.
EVENT_KEYS = {
    "ts", "run_id", "round_token", "event", "old_port", "new_port",
    "declared_port", "pid", "boot_id", "reason",
}


def _decl(**overrides) -> ServiceDeclaration:
    base = dict(
        name="api",
        port=DECLARED_PORT,
        start_cmd=f"npx next dev -p {DECLARED_PORT}",
        cwd=".",
    )
    base.update(overrides)
    return ServiceDeclaration(**base)


def _probe(port: int, cmdlines, pids=None, start_epoch=None) -> PortProbe:
    return PortProbe(
        port=port,
        listening=bool(cmdlines),
        pids=list(
            pids if pids is not None else range(1000, 1000 + len(cmdlines))
        ),
        cmdlines=list(cmdlines),
        process_start_epoch=start_epoch,
    )


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    path = tmp_path / "plan-20261024-429"
    path.mkdir()
    return path


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    (path / "tmp").mkdir(parents=True)
    return path


@pytest.fixture
def round_token(plan_dir: Path) -> str:
    """A live round owns the plan, so the events carry its token."""
    token = "run-4242-abcdef012345"
    sm.register_service_owner(plan_dir, token, current=True)
    return token


def _stub_start(monkeypatch, *, ready=True, pid=4242, port=None):
    """A ``start_service`` that spawns nothing but says it did.

    The address events are written by the code that *picks* the address
    and by the code that *records* the ledger, so a stub is enough to
    drive the chain — and it keeps the test off the process table.
    """
    calls = []

    def _fake(decl, project_dir, **kwargs):
        calls.append((decl.name, decl.port))
        return sm.ServiceRuntime(
            name=decl.name,
            port=port if port is not None else decl.port,
            pid=pid,
            pgid=pid,
            start_cmd=decl.start_cmd,
            cwd=decl.cwd,
            origin="spawned",
            ready=ready,
            detail="stub",
        )

    monkeypatch.setattr(sm, "start_service", _fake)
    return calls


def _events(plan_dir: Path) -> List[Dict[str, Any]]:
    return sm.read_address_events(plan_dir)


def _of_kind(events: List[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    return [e for e in events if e.get("event") == kind]


def _index_of(events: List[Dict[str, Any]], kind: str) -> int:
    for index, event in enumerate(events):
        if event.get("event") == kind:
            return index
    raise AssertionError(f"no {kind!r} event in {events}")


# ---------------------------------------------------------------------------
# The four criteria
# ---------------------------------------------------------------------------


def test_every_port_assignment_emits_event(
    plan_dir, project_dir, round_token, monkeypatch
):
    """declared → relocated → bound → reap leaves connected events.

    The chain has to *connect*: the address that was allocated is the
    address the relocation moved to, and the address that was reaped is
    the one the round used.  A log that records four unrelated events
    would not answer "where did this port come from".

    The service is a **real** process here, and the reap is the real
    one: the VP services are reaped through the unified ledger's
    reaper, which kills the process the ledger names and then asks the
    port whether the address came back.  A stubbed service would make
    the second half of that question meaningless — there is no process
    to kill, so the port can only ever answer "somebody else holds it".
    """
    holder = {"pid": None}

    def _probe_stub(port: int) -> PortProbe:
        if port == DECLARED_PORT:
            # A stranger holds the declared port: the round relocates.
            return _probe(
                port, ["/usr/bin/postgres -D /var/db"], start_epoch=1e12,
            )
        # Our own service holds the relocated port — for as long as it
        # lives.  The reap kills it and then asks the port whether the
        # address came back, so the answer has to follow the process.
        #
        # ``poll()`` is the honest answer: it collects the child, so a
        # process that was killed but not yet reaped does not read as
        # alive — which is exactly what ``os.kill(pid, 0)`` would say.
        if holder["pid"] and service.poll() is None:
            return _probe(
                port, [f"npx next dev -p {RELOCATED_PORT}"],
                pids=[holder["pid"]],
                start_epoch=1e12,
            )
        return _probe(port, [])

    monkeypatch.setattr(sm, "probe_port", _probe_stub)
    monkeypatch.setattr(sm, "allocate_free_port", lambda: RELOCATED_PORT)

    # A real service process, in a session of its own — the shape of a
    # detached PDT layer, and the shape the reaper is willing to signal.
    service = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def _real_start(decl, project, **kwargs):
        return sm.ServiceRuntime(
            name=decl.name,
            port=RELOCATED_PORT,
            pid=service.pid,
            pgid=service.pid,
            start_cmd=decl.start_cmd,
            cwd=decl.cwd,
            origin="spawned",
            ready=True,
            detail="real process",
        )

    monkeypatch.setattr(sm, "start_service", _real_start)

    try:
        runtime_map = sm.ensure_services(
            project_dir, {"api": _decl()},
            plan_dir=plan_dir, plan_id=plan_dir.name,
        )
        runtime = runtime_map.runtimes["api"]
        assert runtime.ready is True, runtime.detail
        assert runtime.port == RELOCATED_PORT
        assert runtime.declared_port == DECLARED_PORT
        holder["pid"] = runtime.pid

        # The ledger write is the state change; the events are on disk
        # before it.
        sm.record_runtimes(
            plan_dir, plan_dir.name, runtime_map, project_dir=project_dir,
        )
        ledger = sm.read_ledger(plan_dir)
        assert ledger["services"][0]["port"] == RELOCATED_PORT

        # The reap: our own process holds the port, so it is ours to
        # stop.  The reaper kills it through the unified ledger and
        # then asks the port whether the address came back.
        report = sm.reap_services(
            plan_dir, plan_id=plan_dir.name, reason="round over",
            owner_token=round_token,
        )
        assert report.reaped == ["api"]
        assert service.poll() is not None, (
            "the reap answered 'reaped' while the process is still running"
        )

        events = _events(plan_dir)
        allocated = _of_kind(events, "allocated")
        relocated = _of_kind(events, "relocated")
        bound = _of_kind(events, "bound")
        reaped = _of_kind(events, "reaped")
        assert allocated, f"the allocated address left no event: {events}"
        assert relocated, f"the relocation left no event: {events}"
        assert bound, f"the bound address left no event: {events}"
        assert reaped, f"the reap left no event: {events}"

        assert allocated[0]["new_port"] == RELOCATED_PORT
        assert allocated[0]["declared_port"] == DECLARED_PORT
        assert relocated[0]["old_port"] == DECLARED_PORT
        assert relocated[0]["new_port"] == RELOCATED_PORT
        assert relocated[0]["declared_port"] == DECLARED_PORT
        assert bound[0]["new_port"] == RELOCATED_PORT
        assert reaped[0]["old_port"] == RELOCATED_PORT

        # The order is the order of the round: the address was taken,
        # then the round was bound to it, then the round gave it back.
        assert _index_of(events, "allocated") < _index_of(events, "bound")
        assert _index_of(events, "bound") < _index_of(events, "reaped")
    finally:
        if service.poll() is None:
            service.kill()
        try:
            service.wait(timeout=10)
        except Exception:  # noqa: BLE001 — cleanup must not fail the test
            pass


def test_event_contains_run_and_round_token(
    plan_dir, project_dir, round_token, monkeypatch
):
    """Every event names the run and the round it belongs to.

    Two rounds of one plan share one plan directory, so the run alone
    does not say which round moved the address.
    """
    _stub_start(monkeypatch)
    monkeypatch.setattr(sm, "probe_port", lambda p: _probe(p, []))
    runtime_map = sm.ensure_services(
        project_dir, {"api": _decl()},
        plan_dir=plan_dir, plan_id=plan_dir.name,
    )
    sm.record_runtimes(
        plan_dir, plan_dir.name, runtime_map, project_dir=project_dir,
    )
    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: {"port_free": True, "detail": "stub"},
    )
    sm.reap_services(
        plan_dir, plan_id=plan_dir.name, reason="round over",
        owner_token=round_token,
    )

    events = _events(plan_dir)
    assert events, "no address event was written at all"
    for event in events:
        assert event["run_id"], f"an event without a run: {event}"
        assert event["round_token"], f"an event without a round: {event}"
        assert event["run_id"] == plan_dir.name
        assert event["round_token"] == round_token


def test_address_events_survive_reap(
    plan_dir, project_dir, round_token, monkeypatch
):
    """The reap clears the ledger; it must not touch the history.

    The whole point of the file is the chain *across* the round — the
    events that explain how the address got there are worthless if the
    round's exit truncates them.
    """
    _stub_start(monkeypatch)
    monkeypatch.setattr(sm, "probe_port", lambda p: _probe(p, []))
    runtime_map = sm.ensure_services(
        project_dir, {"api": _decl()},
        plan_dir=plan_dir, plan_id=plan_dir.name,
    )
    sm.record_runtimes(
        plan_dir, plan_dir.name, runtime_map, project_dir=project_dir,
    )
    before = _events(plan_dir)
    assert before

    monkeypatch.setattr(
        sm, "stop_service",
        lambda *a, **k: {"port_free": True, "detail": "stub"},
    )
    sm.reap_services(
        plan_dir, plan_id=plan_dir.name, reason="round over",
        owner_token=round_token,
    )

    after = _events(plan_dir)
    assert len(after) >= len(before), "the reap truncated the history"
    for event in before:
        assert event in after, f"the reap dropped an event: {event}"
    assert _of_kind(after, "reaped"), "the reap left no event"


def test_trace_reconstructs_51725(
    plan_dir, project_dir, round_token, monkeypatch
):
    """Given the port nobody listened on, the stream says who allocated it.

    This is the question the 2026-10-24 round could not answer: the
    request went to 51725 and *nothing* said where that number came
    from.  The answer has to be one file read away.
    """
    _stub_start(monkeypatch, pid=31337)
    monkeypatch.setattr(sm, "probe_port", lambda p: _probe(p, []))
    runtime_map = sm.ensure_services(
        project_dir,
        {"api": _decl(port=THE_PORT_NOBODY_LISTENED_ON)},
        plan_dir=plan_dir, plan_id=plan_dir.name,
    )
    sm.record_runtimes(
        plan_dir, plan_dir.name, runtime_map, project_dir=project_dir,
    )

    events = _events(plan_dir)
    allocations = [
        e for e in _of_kind(events, "allocated")
        if e["new_port"] == THE_PORT_NOBODY_LISTENED_ON
    ]
    assert len(allocations) == 1, (
        f"the allocation of {THE_PORT_NOBODY_LISTENED_ON} is not in the "
        f"event stream: {events}"
    )
    allocation = allocations[0]
    assert allocation["pid"] == 31337
    assert allocation["declared_port"] == THE_PORT_NOBODY_LISTENED_ON
    assert allocation["boot_id"]
    assert allocation["run_id"] == plan_dir.name
    assert allocation["round_token"] == round_token
    # The call that allocated it is named, not just its number.
    assert "api" in allocation["reason"]


# ---------------------------------------------------------------------------
# The file itself
# ---------------------------------------------------------------------------


def test_the_event_file_lives_under_the_plan_and_is_json_lines(
    plan_dir, round_token
):
    sm.record_address_event(
        plan_dir, "allocated",
        run_id=plan_dir.name, round_token=round_token,
        old_port=None, new_port=51725, declared_port=18090,
        pid=4242, boot_id="4242@1750000000", reason="because",
    )

    path = plan_dir / "logs" / "service_address_events.jsonl"
    assert path.exists(), "the address events are not where they belong"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) == EVENT_KEYS
    assert payload["event"] == "allocated"
    assert payload["new_port"] == 51725
    assert payload["declared_port"] == 18090
    assert payload["pid"] == 4242
    assert payload["boot_id"] == "4242@1750000000"
    assert payload["reason"] == "because"
    assert payload["ts"]


def test_the_event_is_on_disk_before_the_ledger_names_the_address(
    plan_dir, project_dir, round_token, monkeypatch
):
    """The event comes first; the ledger is the state it describes.

    The ledger write is made to fail here — so the only thing that can
    put the address on disk is the event.
    """
    _stub_start(monkeypatch)
    monkeypatch.setattr(sm, "probe_port", lambda p: _probe(p, []))
    runtime_map = sm.ensure_services(
        project_dir, {"api": _decl()},
        plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    # A ledger whose parent is a file cannot be written.
    blocked = plan_dir / "ledger-is-a-file"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        sm, "ledger_path", lambda p: blocked / "managed_services.json",
    )

    sm.record_runtimes(
        plan_dir, plan_dir.name, runtime_map, project_dir=project_dir,
    )

    bound = _of_kind(_events(plan_dir), "bound")
    assert bound, "the address was never written down"
    assert bound[0]["new_port"] == DECLARED_PORT


def test_a_failed_event_write_costs_one_stderr_line_not_the_round(
    plan_dir, capsys
):
    """A broken log must not take the round down — but it must be loud."""
    # ``logs`` is a file, so the event file cannot be created.
    (plan_dir / "logs").write_text("not a directory", encoding="utf-8")

    ok = sm.record_address_event(
        plan_dir, "allocated", new_port=51725, reason="because",
    )

    assert ok is False
    captured = capsys.readouterr()
    assert "service_address_events.jsonl" in captured.err
    assert "allocated" in captured.err
    assert captured.err.strip().count("\n") == 0, (
        "one line, not a stack trace"
    )


def test_a_corrupt_line_does_not_hide_the_events_around_it(plan_dir):
    sm.record_address_event(plan_dir, "allocated", new_port=1)
    path = sm.address_events_path(plan_dir)
    with path.open("a", encoding="utf-8") as fp:
        fp.write("{not json at all\n")
    sm.record_address_event(plan_dir, "reaped", old_port=1)

    events = _events(plan_dir)

    assert [e["event"] for e in events] == ["allocated", "reaped"]


def test_an_adopted_address_is_recorded_as_reused(
    plan_dir, project_dir, round_token, monkeypatch
):
    """A listener that was already there was *reused*, not allocated."""
    _stub_start(monkeypatch)
    monkeypatch.setattr(
        sm, "probe_port",
        lambda p: _probe(
            p, [f"npx next dev -p {DECLARED_PORT}"], start_epoch=1e12,
        ),
    )

    runtime_map = sm.ensure_services(
        project_dir, {"api": _decl()},
        plan_dir=plan_dir, plan_id=plan_dir.name,
    )

    assert runtime_map.runtimes["api"].origin == "adopted"
    reused = _of_kind(_events(plan_dir), "reused")
    assert reused, "the reused address left no event"
    assert reused[0]["new_port"] == DECLARED_PORT


def test_start_service_records_the_allocation_it_made(
    plan_dir, project_dir, round_token
):
    """The code that picks the address is the code that writes it down.

    This one really starts a process — the allocation event is worth
    exactly as much as the truth of the allocation, and a stubbed
    start would not prove that.
    """
    port = sm.allocate_free_port()
    assert port, "could not get a free port for the test"
    decl = ServiceDeclaration(
        name="api",
        port=port,
        start_cmd=(
            f"{shlex.quote(sys.executable)} -m http.server {port}"
        ),
        cwd=".",
        ready_timeout_seconds=20,
    )

    with sm.address_event_context(
        plan_dir, run_id=plan_dir.name, round_token=round_token,
    ):
        runtime = sm.start_service(decl, project_dir)
    try:
        assert runtime.ready, runtime.detail
        allocated = _of_kind(_events(plan_dir), "allocated")
        assert len(allocated) == 1, (
            f"the allocation was not recorded: {_events(plan_dir)}"
        )
        assert allocated[0]["new_port"] == port
        assert allocated[0]["pid"] == runtime.pid
        assert allocated[0]["boot_id"]
        assert allocated[0]["run_id"] == plan_dir.name
        assert allocated[0]["round_token"] == round_token
    finally:
        sm.stop_service(runtime, decl, grace_seconds=5.0)


def test_the_round_records_the_addresses_it_dispatched_to(
    plan_dir, project_dir, round_token, monkeypatch
):
    """The last link: the round says which address it sent traffic to.

    The ledger names the address; the earlier events say how it got
    there; the dispatch binding says the round actually used it.  A
    refused connection on 51725 is only explicable once all three are
    in one file.
    """
    import verification_loop as vl

    _stub_start(monkeypatch, pid=31337)
    monkeypatch.setattr(sm, "probe_port", lambda p: _probe(p, []))
    runtime_map = sm.ensure_services(
        project_dir,
        {"api": _decl(port=THE_PORT_NOBODY_LISTENED_ON)},
        plan_dir=plan_dir, plan_id=plan_dir.name,
    )
    sm.record_runtimes(
        plan_dir, plan_dir.name, runtime_map, project_dir=project_dir,
    )

    vl._record_dispatch_bindings(plan_dir, plan_dir.name)

    dispatched = _of_kind(_events(plan_dir), "dispatch_bound")
    assert dispatched, "the dispatch left no event"
    assert dispatched[0]["new_port"] == THE_PORT_NOBODY_LISTENED_ON
    assert dispatched[0]["declared_port"] == THE_PORT_NOBODY_LISTENED_ON
    assert dispatched[0]["pid"] == 31337
    assert dispatched[0]["run_id"] == plan_dir.name
    assert dispatched[0]["round_token"] == round_token


def test_a_broken_dispatch_log_does_not_break_the_round(plan_dir, monkeypatch):
    """A ledger that cannot be read costs a log line, not a round."""
    import verification_loop as vl

    def _boom(plan_dir):
        raise OSError("the ledger is unreadable")

    monkeypatch.setattr(sm, "read_ledger", _boom)

    vl._record_dispatch_bindings(plan_dir, plan_dir.name)  # must not raise

