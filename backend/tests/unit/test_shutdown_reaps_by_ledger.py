"""The shutdown sweep has to reach the processes the memory lost.

What these tests pin
--------------------

``_shutdown_all_executions`` used to walk ``_execution_state`` — the
in-memory handles — and nothing else.  A backend that is stopped with
SIGTERM while a plan's executor is running loses those handles before
the sweep runs: the ledger still names the process, the sweep never
looked at the ledger, and the executor stayed alive after the backend
that started it was gone.

The sweep is delegated now.  ``service_manager.reap_all_plans`` is the
one place that knows how to walk a plans directory and how to reap what
a ledger names (identity check, children before parents, the backend
itself and Docker excluded), so ``_sweep_process_ledgers`` only has to
hand it the directory and the reason — and hand back what it says.

What the tests hold down:

* the delegation happens — exactly once, with the right directory and
  the right reason, over every role in the tree;
* the outcome is passed back unchanged — the caller decides what to do
  with it, the sweep does not filter it;
* a directory with no ledger produces no reports — the common case;
* the shutdown path reaches the ledger sweep *after* the in-memory
  handles, so a plan whose handle is gone is still covered.

Hermeticity
-----------

Nothing here starts a process or signals one.  The delegation is
observed through a stand-in for ``service_manager.reap_all_plans``, and
the one test that uses the real one points it at a ``tmp_path`` with no
ledger in it.
"""

from __future__ import annotations

from pathlib import Path

import server
from service_manager import ReapReport


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _recorder(calls, reports):
    """A stand-in for ``service_manager.reap_all_plans``.

    The signature matches the real one exactly, so a caller that starts
    passing a new keyword is caught here rather than at runtime.
    """

    def _fake(plans_dir, *, reason="", grace_seconds=10.0, roles=None):
        calls.append(
            {
                "plans_dir": Path(plans_dir),
                "reason": reason,
                "grace_seconds": grace_seconds,
                "roles": roles,
            }
        )
        return list(reports)

    return _fake


# ---------------------------------------------------------------------------
# the delegation
# ---------------------------------------------------------------------------

def test_sweep_delegates_to_reap_all_plans(tmp_path, monkeypatch):
    """One call, the right directory, the right reason, every role."""
    calls: list[dict] = []
    monkeypatch.setattr(server, "_reap_all_plans", _recorder(calls, []))

    server._sweep_process_ledgers(tmp_path, reason="server_shutdown")

    assert len(calls) == 1, (
        "the ledger sweep must reach service_manager.reap_all_plans "
        f"exactly once; it was called {len(calls)} times"
    )
    call = calls[0]
    assert call["plans_dir"] == tmp_path
    assert call["reason"] == "server_shutdown"
    # ``roles=None`` is every role in the tree: the executor, the
    # sub-agents and the services all have to go with the backend.
    assert call["roles"] is None, (
        f"the shutdown sweep must not narrow the roles; it passed {call['roles']!r}"
    )


def test_sweep_returns_outcomes_unchanged(tmp_path, monkeypatch):
    """Whatever the reaper says is what the caller gets back."""
    reports = [
        ReapReport(plan_id="p1", reason="server_shutdown", reaped=["201", "202"]),
        ReapReport(plan_id="p2", reason="server_shutdown", reaped=[]),
    ]
    calls: list[dict] = []
    monkeypatch.setattr(server, "_reap_all_plans", _recorder(calls, reports))

    out = server._sweep_process_ledgers(tmp_path, reason="server_shutdown")

    assert out == reports
    assert [r for r in out] == [r for r in reports], (
        "the sweep must hand back the very reports the reaper produced, "
        "not a copy with something dropped"
    )


def test_sweep_ignores_dirs_without_ledger(tmp_path):
    """A plans directory with no ledger reaps nothing."""
    # A plan that never started anything: the directory exists, the
    # ledger does not.
    (tmp_path / "p1").mkdir()

    reports = server._sweep_process_ledgers(tmp_path, reason="server_shutdown")

    assert reports == []


def test_sweep_without_service_manager_is_a_no_op(tmp_path, monkeypatch):
    """A missing ``service_manager`` disables the sweep, it does not raise."""
    monkeypatch.setattr(server, "_reap_all_plans", None)

    assert server._sweep_process_ledgers(tmp_path, reason="server_shutdown") == []


# ---------------------------------------------------------------------------
# the call site
# ---------------------------------------------------------------------------

def test_shutdown_sweeps_ledgers_after_the_in_memory_handles(monkeypatch):
    """The ledger sweep runs *after* the handles, so a lost handle is covered."""
    events: list[tuple] = []

    class _FakeProc:
        pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(server, "_execution_state", {"p1": {"process": _FakeProc()}})
    monkeypatch.setattr(server, "_verification_state", {})
    monkeypatch.setattr(
        server,
        "_kill_process_tree",
        lambda proc, *a, **k: events.append(("kill", proc.pid)),
    )

    def _fake_sweep(plans_dir, *, reason, **sweep_options):
        # The sweep's own options (``release_own_owners``, ``caller``)
        # are part of its signature: the shutdown path hands them over,
        # and a fake that does not accept them would answer "the sweep
        # did not run" for a reason that has nothing to do with the
        # order this test is about.
        events.append(("sweep", Path(plans_dir), reason, sweep_options))
        return []

    monkeypatch.setattr(server, "_sweep_process_ledgers", _fake_sweep)

    server._shutdown_all_executions()

    kinds = [e[0] for e in events]
    assert kinds == ["kill", "sweep"], (
        f"the shutdown path must reap the in-memory handles first and the "
        f"ledgers second; the order was {kinds!r}"
    )
    assert events[1][1] == server.PLANS_DIR
    assert events[1][2] == "server_shutdown"
    assert events[1][3]["release_own_owners"] is True
    assert events[1][3]["caller"] == "server.shutdown_sweep"
