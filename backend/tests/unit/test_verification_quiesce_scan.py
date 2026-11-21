"""Which of a plan's verification processes are still alive.

2026-11-20 — a stop could only kill the processes it could enumerate
at the moment of the call: the repair executor (which the registry
never sees) and anything that started after that moment kept running
while the plan was reported stopped.  ``verification_quiesce`` is the
reusable answer to "does this plan still have live work?", and this
file pins the three properties that answer depends on:

1. **Liveness is the process, not a status field.**  A record that
   says ``running`` whose process is gone is not a survivor.
2. **Both sources are read** — the registry handles *and* the repair
   executor.
3. **A source that cannot be read costs that source, not the scan.**
   The scan answers with what it could read; it never raises.

The liveness probe is faked (``process_reaper._alive``) so the tests
say what they mean: "this pid is alive" / "this pid is gone".
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Set

import pytest

import process_reaper
import verification_quiesce
from verification_quiesce import Survivor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_module_state():
    """The scanner keeps no state of its own; the hook is still called
    on both sides so a test that swaps the server stand-in cannot leak
    it into the next test."""
    verification_quiesce._reset_for_tests()
    yield
    verification_quiesce._reset_for_tests()


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is a process-global singleton — leave it as found."""
    from sub_agent_registry import sub_agent_registry

    def _clear():
        for plan_id in list(sub_agent_registry._by_plan.keys()):
            sub_agent_registry.cleanup_for_plan(plan_id)

    _clear()
    yield
    _clear()


@pytest.fixture
def registry():
    from sub_agent_registry import sub_agent_registry

    return sub_agent_registry


@pytest.fixture
def live_pids(monkeypatch) -> Set[int]:
    """Decide liveness by pid: the pids in the set are alive.

    The probe itself is ``process_reaper._alive`` — the same predicate
    the reaper and the stop path use — so faking it here fakes exactly
    what production asks.
    """
    live: Set[int] = set()

    def _fake_alive(pid: int) -> bool:
        try:
            return int(pid) in live
        except (TypeError, ValueError):
            return False

    monkeypatch.setattr(process_reaper, "_alive", _fake_alive)
    return live


def _fake_server_module(execution_state) -> types.ModuleType:
    """A stand-in for the ``server`` module carrying an execution state."""
    module = types.ModuleType("server")
    module._execution_state = execution_state
    return module


# ---------------------------------------------------------------------------
# 1) the registry source
# ---------------------------------------------------------------------------


def test_live_registry_handle_is_a_survivor(registry, live_pids):
    """A registered sub-agent whose process is alive is a survivor, and
    the survivor names the verification point that is still running."""
    live_pids.add(4242)
    registry.register_sub_agent("p1", "llm-dispatch-7", 1, pid=4242)

    survivors = verification_quiesce.scan_plan_processes(
        "p1", execution_state={},
    )

    assert survivors == [
        Survivor(pid=4242, kind="registry", detail="llm-dispatch-7"),
    ]


def test_dead_pid_is_not_a_survivor(registry, live_pids):
    """The handle is still registered, but its process is gone: there is
    nothing running that the handle names."""
    registry.register_sub_agent("p1", "llm-dispatch-8", 1, pid=4243)

    assert verification_quiesce.scan_plan_processes(
        "p1", execution_state={},
    ) == []


def test_unknown_plan_has_no_survivors(live_pids):
    """No handles, no execution record — nothing is running."""
    assert verification_quiesce.scan_plan_processes(
        "p-unknown", execution_state={},
    ) == []


# ---------------------------------------------------------------------------
# 2) the repair executor source
# ---------------------------------------------------------------------------


def test_repair_executor_counted_only_when_pid_alive(live_pids):
    """The repair executor is a process the registry never sees, so it
    is read from the execution record — and it counts only while its
    process is actually alive.  A record that still says ``running``
    after the process died is not a survivor."""
    live_pids.add(4244)
    running = {
        "p1": {
            "pid": 4244,
            "_source": "repair_execution",
            "status": "running",
        },
    }

    alive = verification_quiesce.scan_plan_processes(
        "p1", execution_state=running,
    )
    assert alive == [Survivor(pid=4244, kind="repair_executor", detail="")]

    live_pids.discard(4244)
    dead = verification_quiesce.scan_plan_processes(
        "p1", execution_state=running,
    )
    assert dead == []


def test_other_execution_source_is_not_a_survivor(live_pids):
    """Only the repair path's marker names a repair executor — the main
    execution record is a different thing entirely."""
    live_pids.add(4244)

    survivors = verification_quiesce.scan_plan_processes(
        "p1",
        execution_state={
            "p1": {"pid": 4244, "_source": "main_execution"},
        },
    )

    assert survivors == []


# ---------------------------------------------------------------------------
# 3) both sources, and the dedup between them
# ---------------------------------------------------------------------------


def test_same_pid_deduped(registry, live_pids):
    """One process, one survivor: the registry is the richer record (it
    knows which verification point the pid belongs to), so it wins."""
    live_pids.add(4242)
    registry.register_sub_agent("p1", "llm-dispatch-7", 1, pid=4242)

    survivors = verification_quiesce.scan_plan_processes(
        "p1",
        execution_state={
            "p1": {"pid": 4242, "_source": "repair_execution"},
        },
    )

    assert survivors == [
        Survivor(pid=4242, kind="registry", detail="llm-dispatch-7"),
    ]


def test_both_sources_are_reported_together(registry, live_pids):
    """The registry handle and the repair executor are different
    processes and both are reported."""
    live_pids.update({4242, 4244})
    registry.register_sub_agent("p1", "llm-dispatch-7", 1, pid=4242)

    survivors = verification_quiesce.scan_plan_processes(
        "p1",
        execution_state={
            "p1": {"pid": 4244, "_source": "repair_execution"},
        },
    )

    assert survivors == [
        Survivor(pid=4242, kind="registry", detail="llm-dispatch-7"),
        Survivor(pid=4244, kind="repair_executor", detail=""),
    ]


# ---------------------------------------------------------------------------
# 4) the lazy read of the server's real execution state
# ---------------------------------------------------------------------------


def test_none_execution_state_reads_live_server_state(monkeypatch, live_pids):
    """With no state injected, the scanner reads the server's own
    ``_execution_state`` — otherwise the repair executor would be
    invisible in production, which is the whole reason this scanner
    exists."""
    live_pids.add(4244)
    monkeypatch.setitem(
        sys.modules,
        "server",
        _fake_server_module({
            "p1": {
                "pid": 4244,
                "_source": "repair_execution",
                "status": "running",
            },
        }),
    )

    survivors = verification_quiesce.scan_plan_processes("p1")

    assert survivors == [Survivor(pid=4244, kind="repair_executor", detail="")]


def test_server_import_failure_is_logged_not_raised(monkeypatch, caplog):
    """A server that cannot be imported costs the repair-executor
    source, not the scan."""
    monkeypatch.setitem(sys.modules, "server", None)

    with caplog.at_level(logging.WARNING, logger="verification_quiesce"):
        survivors = verification_quiesce.scan_plan_processes("p1")

    assert survivors == []
    assert any(
        "verification_quiesce" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# 5) a source that cannot be read costs that source only
# ---------------------------------------------------------------------------


def test_registry_read_failure_keeps_the_other_source(
    registry, monkeypatch, live_pids, caplog,
):
    """The registry blowing up must not hide the repair executor."""
    live_pids.add(4244)

    def _boom(plan_id):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(registry, "all_handles", _boom)

    with caplog.at_level(logging.WARNING, logger="verification_quiesce"):
        survivors = verification_quiesce.scan_plan_processes(
            "p1",
            execution_state={
                "p1": {"pid": 4244, "_source": "repair_execution"},
            },
        )

    assert survivors == [Survivor(pid=4244, kind="repair_executor", detail="")]
    assert any(
        "verification_quiesce" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# 6) the JSON-friendly snapshot
# ---------------------------------------------------------------------------


def test_survivors_snapshot_is_json_friendly(registry, live_pids, monkeypatch):
    """The snapshot is what a caller hands to a card or an API, so it
    carries plain values and nothing else."""
    import json

    live_pids.add(4242)
    registry.register_sub_agent("p1", "llm-dispatch-7", 1, pid=4242)
    # The snapshot reads the live execution state; stand in for the
    # server so the test does not import the whole application.
    monkeypatch.setitem(sys.modules, "server", _fake_server_module({}))

    snapshot = verification_quiesce.survivors_snapshot("p1")

    assert snapshot == [
        {"pid": 4242, "kind": "registry", "detail": "llm-dispatch-7"},
    ]
    assert json.loads(json.dumps(snapshot)) == snapshot
