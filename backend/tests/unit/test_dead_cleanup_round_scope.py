"""The judged-dead cleanup reaches the round's own coding-tool child (2026-11-01).

What this file pins
-------------------
A verification round that is judged dead has to take **everything** the
round left behind with it.  Two of the three populations were already
covered: the registered sub-agent handles and the disowned ``pytest``
orphans.  The third was not — the coding tool the **orchestrator**
spawned, whose ``Popen`` lives in ``tool._current_process`` and is
reachable only through the tool.  On 2026-10-31 that is what kept an
LLM call alive for 1h46m after the plan had been written off as failed.

The kill has to be *scoped*, and the plan is too coarse a scope: the
rounds of one plan share one plan directory.  The scope is the round,
named by its ``round_token`` — the same token the workflow lease was
taken with, and the token the round was opened under in the round-scoped
process ledger.

The criteria this file pins, one test each:

* the cleanup kills the round's own coding-tool subprocess;
* the cleanup does not touch another round's process (same plan);
* the cleanup does not touch another plan's process (same token, other
  plan directory);
* the kill is recorded with the token, the pid and the signal — a kill
  nobody can read is a kill nobody can audit;
* an unnamed round (``round_token=""``) leaves the round's processes
  alone — the other two populations are cleaned up exactly as before;
* a kill that fails is recorded and the judgement carries on.

Hermeticity
------------
Everything happens in ``tmp_path``: the plan directories are under it and
``server.PLANS_DIR`` is pointed at it, so no plan directory is created
inside the repository.  The only processes are the children this file
spawns itself, and they are spawned in their own session
(``start_new_session=True``) so a group signal reaches them and only
them — the coding tool spawns its child the same way, and a child in
the *test's* process group would take the test session down with it.
The round ledger is process-wide state, so it is reset around every
test.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server as srv  # noqa: E402
import verification_loop as vl  # noqa: E402
import round_process_ledger  # noqa: E402
import process_ledger  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeCodingTool:
    """A coding tool that owns a real subprocess, the way the real one does.

    The ledger reaches a tool's process through ``_current_process``, and
    it takes it out under ``_process_lock`` — the same lock the real
    coding tool holds it under.  The fake keeps both, so the kill takes
    the same path it takes in production.
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self._current_process: Any = proc
        self._process_lock = threading.Lock()


def _wait_dead(proc: subprocess.Popen, timeout: float = 10.0) -> bool:
    """True when the child is gone within ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return proc.poll() is not None


@pytest.fixture
def spawn() -> Callable[[], subprocess.Popen]:
    """Spawn a child that sleeps, and kill it again when the test is over."""
    children: List[subprocess.Popen] = []

    def _spawn() -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(proc)
        return proc

    yield _spawn

    for proc in children:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # pragma: no cover - defensive
                pass
        try:
            proc.wait(timeout=5)
        except Exception:  # pragma: no cover - defensive
            pass


@pytest.fixture
def plans_root(tmp_path: Path, monkeypatch: Any) -> Path:
    """The plans root, redirected into ``tmp_path``.

    ``server._plan_dir`` resolves a plan id against the module-level
    ``PLANS_DIR``; pointing it at the test's own tree keeps the plan
    directories this file creates out of the repository.
    """
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(srv, "PLANS_DIR", root)
    return root


def _plan_dir(plans_root: Path, plan_id: str) -> Path:
    """The plan's directory — the round ledger is keyed by it."""
    path = plans_root / plan_id
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(autouse=True)
def _clean_ledger():
    """The round ledger is process-wide state — no test may lend its round
    to the next one."""
    round_process_ledger.reset_for_tests()
    yield
    round_process_ledger.reset_for_tests()


# ---------------------------------------------------------------------------
# The criteria
# ---------------------------------------------------------------------------


def test_cleanup_kills_the_rounds_orchestrator_query_process(
    plans_root: Path, spawn: Callable[[], subprocess.Popen],
) -> None:
    """The round's own coding-tool child is dead after the cleanup.

    The child is a real process: the assertion is that it stopped
    running, not that a mock was called.
    """
    plan_id = "plan-round-kill"
    plan_dir = _plan_dir(plans_root, plan_id)
    token = "verification_loop-1-ab"

    proc = spawn()
    round_process_ledger.begin_round(plan_dir, token)
    round_process_ledger.register_tool(plan_dir, _FakeCodingTool(proc))

    vl._cleanup_dead_verification_processes(
        plan_id, ["VP-001"], round_token=token,
    )

    assert _wait_dead(proc), (
        "the round's coding-tool child survived the judged-dead cleanup"
    )


def test_cleanup_does_not_touch_another_rounds_process(
    plans_root: Path, spawn: Callable[[], subprocess.Popen],
) -> None:
    """A kill aimed at one round leaves the next round's process running.

    Both rounds belong to the same plan and the same plan directory —
    which is exactly why the kill cannot be scoped to the plan.
    """
    plan_id = "plan-two-rounds"
    plan_dir = _plan_dir(plans_root, plan_id)
    token_a = "verification_loop-1-aa"
    token_b = "verification_loop-2-bb"

    proc_a = spawn()
    proc_b = spawn()
    round_process_ledger.begin_round(plan_dir, token_a)
    round_process_ledger.register_tool(plan_dir, _FakeCodingTool(proc_a))
    round_process_ledger.begin_round(plan_dir, token_b)
    round_process_ledger.register_tool(plan_dir, _FakeCodingTool(proc_b))

    vl._cleanup_dead_verification_processes(plan_id, [], round_token=token_a)

    assert _wait_dead(proc_a), "the round that was aimed at kept its process"
    assert proc_b.poll() is None, (
        "the cleanup killed another round's process"
    )


def test_cleanup_does_not_touch_another_plans_process(
    plans_root: Path, spawn: Callable[[], subprocess.Popen],
) -> None:
    """A kill aimed at one plan leaves another plan's process running.

    The two plans even carry the *same* round token: the token names a
    round, and a round is only meaningful together with its plan
    directory.
    """
    plan_a = "plan-alpha"
    plan_b = "plan-beta"
    dir_a = _plan_dir(plans_root, plan_a)
    dir_b = _plan_dir(plans_root, plan_b)
    token = "verification_loop-1-cc"

    proc_a = spawn()
    proc_b = spawn()
    round_process_ledger.begin_round(dir_a, token)
    round_process_ledger.register_tool(dir_a, _FakeCodingTool(proc_a))
    round_process_ledger.begin_round(dir_b, token)
    round_process_ledger.register_tool(dir_b, _FakeCodingTool(proc_b))

    vl._cleanup_dead_verification_processes(plan_a, [], round_token=token)

    assert _wait_dead(proc_a), "the plan that was aimed at kept its process"
    assert proc_b.poll() is None, (
        "the cleanup killed another plan's process"
    )


def test_cleanup_event_names_token_pid_and_signal(
    plans_root: Path, spawn: Callable[[], subprocess.Popen],
) -> None:
    """The kill is recorded with the token, the pid and the signal.

    Three fields, all of them needed to read the record afterwards:
    *which* round, *which* process, and *which* signal.
    """
    plan_id = "plan-round-event"
    plan_dir = _plan_dir(plans_root, plan_id)
    token = "verification_loop-1-dd"

    proc = spawn()
    round_process_ledger.begin_round(plan_dir, token)
    round_process_ledger.register_tool(plan_dir, _FakeCodingTool(proc))

    vl._cleanup_dead_verification_processes(plan_id, [], round_token=token)

    events = [
        event for event in process_ledger.read_events(plan_dir)
        if event.get("round_token") == token
    ]
    assert events, (
        "the round kill was not recorded in the plan's process ledger"
    )
    event = events[0]
    assert event.get("pid") == proc.pid
    assert event.get("signal") == "SIGTERM"
    assert event.get("round_token") == token


def test_cleanup_without_a_round_token_leaves_the_round_alone(
    plans_root: Path, spawn: Callable[[], subprocess.Popen],
) -> None:
    """An unnamed round is not cleaned up — the old behaviour, unchanged.

    ``round_token=""`` means "no round was named".  The two populations
    the cleanup always covered are still covered; the round's own
    processes are not touched, because a kill that cannot be aimed
    cannot be scoped either.
    """
    plan_id = "plan-no-token"
    plan_dir = _plan_dir(plans_root, plan_id)
    token = "verification_loop-1-ee"

    proc = spawn()
    round_process_ledger.begin_round(plan_dir, token)
    round_process_ledger.register_tool(plan_dir, _FakeCodingTool(proc))

    vl._cleanup_dead_verification_processes(plan_id, [], round_token="")

    assert proc.poll() is None, (
        "an unnamed round had its process killed anyway"
    )


def test_cleanup_survives_a_failing_round_kill(
    plans_root: Path, spawn: Callable[[], subprocess.Popen], monkeypatch: Any,
) -> None:
    """A kill that raises is recorded — and the judgement carries on.

    The round is being written off either way: a kill that fails must
    not turn into an exception that leaves the plan in neither state.
    The other population is still cleaned up, which is what "carries on"
    has to mean.
    """
    plan_id = "plan-kill-boom"
    plan_dir = _plan_dir(plans_root, plan_id)
    token = "verification_loop-1-ff"

    proc = spawn()
    round_process_ledger.begin_round(plan_dir, token)
    round_process_ledger.register_tool(plan_dir, _FakeCodingTool(proc))

    def _boom(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        raise RuntimeError("the round kill failed")

    monkeypatch.setattr(round_process_ledger, "kill_round", _boom)

    swept: Dict[str, Any] = {}

    def _record_sweep(vp_ids: List[str]) -> List[int]:
        swept["vp_ids"] = list(vp_ids)
        return []

    monkeypatch.setattr(srv, "_kill_orphaned_vp_processes", _record_sweep)

    vl._cleanup_dead_verification_processes(
        plan_id, ["VP-002"], round_token=token,
    )

    assert swept.get("vp_ids") == ["VP-002"], (
        "the failed round kill stopped the rest of the cleanup"
    )
    assert proc.poll() is None, (
        "a failed kill must not have killed the process it could not signal"
    )
