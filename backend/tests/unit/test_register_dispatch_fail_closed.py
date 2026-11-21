"""A plan dispatch that cannot be registered must not run (2026-11-20).

Why this file exists
--------------------
``_register_dispatch`` used to swallow every registration failure and
return ``(None, None)``, on the reasoning that "the registry is a
watchdog aid and a dispatch that cannot be watched must still run".
That reasoning is exactly what the incident was made of: the dispatch
registered nothing, so the operator's ``/stop`` had no handle to reach,
so the stop could not kill the process the dispatch had just started.
The process ran on, unwatched, after the operator had asked for it to
stop.

So the registration is **fail-closed** for a plan dispatch:

* a round the operator has already cancelled raises before the registry
  is touched at all — there is nothing to register and nothing to run;
* a registration that fails raises instead of returning an empty pair.

The failure is raised as :class:`verification_cancel.VerificationCancelled`
because that is the type every layer above the dispatch already knows how
to treat as "the operator stopped this" rather than "this broke".

A dispatch with **no plan** attached is unchanged — it still returns
``(None, None)`` and still runs.  The fail-closed rule is scoped to the
plan dispatches, which are the ones the watchdog is responsible for.

Two tests elsewhere pin the contract this change replaces
-----------------------------------------------------------
``backend/tests/unit/test_llm_call_cancellation.py`` still carries the
pre-2026-11-20 contract, and the two assertions below now fail on
purpose:

* ``test_a_registration_failure_does_not_affect_the_dispatch`` asserts
  that a broken registry still lets the dispatch answer ``{"ok": True}``
  — the exact "登记不上也要照跑" behaviour G4 replaces;
* ``test_a_cancelled_dispatch_raises_and_is_not_retried`` cancels the
  plan *before* the dispatch and then expects a registered handle and a
  launched child — but a cancelled plan now raises before the registry
  is touched, so there is no handle and no child.

Both need to be re-written against the fail-closed contract (the first
becomes "a registration failure raises"; the second has to cancel the
plan *while* the dispatch is in flight to keep covering the stop path).
That file is outside the change's declared scope, so it is reported
rather than edited here.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import coding_tool  # noqa: E402
import verification_cancel  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402
from sub_agent_registry import sub_agent_registry  # noqa: E402
from verification_cancel import VerificationCancelled  # noqa: E402

PLAN = "plan-register-dispatch-fail-closed"

#: A fake CLI that stays alive until it is killed. It ignores the EOF on
#: its stdin, so the only thing that can end it is the dispatch's own
#: cleanup — which is what the reaping test below is about.
_LIVING_CLAUDE = """#!/bin/sh
sleep 300
"""



class _StandInTool:
    """The part of a coding tool the registration path actually reads.

    The dispatch records *the process the tool just started*, so the
    only thing that matters here is that the tool can be asked for it.
    """

    def __init__(self, process, cwd: str):
        self._current_process = process
        self.cwd = cwd


def _spawn_holder():
    """A real process that stays alive until it is told to stop.

    Real, because the registration records what the *kernel* says: the
    process group and the start time are read from the running process,
    not made up.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _reap(process) -> None:
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except Exception:
        pass


def _install_fake_claude(tmp_path, monkeypatch, script: str) -> None:
    """Put an executable ``claude`` first on ``PATH``."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", "")
    )


@pytest.fixture(autouse=True)
def _hermetic_state(tmp_path, monkeypatch):
    """A hermetic registry, a hermetic plans root, and no leftover flags.

    The plans root is redirected because a registration writes the
    process onto the plan's ledger — that write has to land in
    ``tmp_path`` and not in the live operator state.
    """
    plans_root = tmp_path / "plans"
    (plans_root / PLAN).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_root))
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)
    verification_cancel.clear(PLAN)

    before = {
        plan_id: list(handles)
        for plan_id, handles in sub_agent_registry._by_plan.items()
    }
    yield
    for plan_id, handles in list(sub_agent_registry._by_plan.items()):
        for handle in handles:
            sub_agent_registry.unregister(plan_id, handle)
    sub_agent_registry._by_plan.clear()
    sub_agent_registry._by_plan.update(before)
    verification_cancel.clear(PLAN)


# ---------------------------------------------------------------------------
# 1. A failed registration stops the dispatch.
# ---------------------------------------------------------------------------
def test_registration_failure_raises_instead_of_running(monkeypatch):
    """A registry that cannot answer is a failed registration.

    The old behaviour — swallow the error and return ``(None, None)`` —
    let the dispatch run with nothing on record, which is how a stopped
    round kept running.
    """
    asked = []

    def boom(plan_id):
        asked.append(plan_id)
        raise RuntimeError("registry down")

    monkeypatch.setattr(sub_agent_registry, "all_handles", boom)

    process = _spawn_holder()
    tool = _StandInTool(process, str(BACKEND_DIR))
    try:
        with pytest.raises(VerificationCancelled) as excinfo:
            coding_tool._register_dispatch(tool, PLAN)
    finally:
        _reap(process)

    assert asked == [PLAN], (
        "the registry was never asked — the test did not reach the "
        "registration at all"
    )
    assert excinfo.value.plan_id == PLAN
    assert str(excinfo.value) == (
        f"verification cancelled for plan {PLAN!r} while registering "
        "the dispatch"
    ), (
        "the failure has to name the plan and the step it happened in, "
        f"so the log says what went wrong; it says {str(excinfo.value)!r}"
    )


# ---------------------------------------------------------------------------
# 2. A cancelled round never reaches the registry.
# ---------------------------------------------------------------------------
def test_cancelled_plan_raises_before_registry_touch(monkeypatch):
    """The operator's stop is honoured before anything is registered."""
    touched = []

    def spy(plan_id):
        touched.append(plan_id)
        return []

    monkeypatch.setattr(sub_agent_registry, "all_handles", spy)
    verification_cancel.request_cancel(PLAN)

    process = _spawn_holder()
    tool = _StandInTool(process, str(BACKEND_DIR))
    try:
        with pytest.raises(VerificationCancelled) as excinfo:
            coding_tool._register_dispatch(tool, PLAN)
    finally:
        _reap(process)

    assert touched == [], (
        "the registry was touched after the round was cancelled — the "
        "cancellation has to be read before the registration starts"
    )
    assert str(excinfo.value) == (
        f"verification cancelled for plan {PLAN!r} while registering "
        "the dispatch"
    )


# ---------------------------------------------------------------------------
# 3. A dispatch with no plan is unchanged.
# ---------------------------------------------------------------------------
def test_no_plan_still_returns_none_pair():
    """No plan, no cancellation, no registration — and no exception."""
    process = _spawn_holder()
    tool = _StandInTool(process, str(BACKEND_DIR))
    try:
        assert coding_tool._register_dispatch(tool, None) == (None, None)
    finally:
        _reap(process)


# ---------------------------------------------------------------------------
# 4. Fail-closed did not break the normal dispatch.
# ---------------------------------------------------------------------------
def test_happy_path_still_registers():
    """A live registry and a live pid still produce a usable handle."""
    process = _spawn_holder()
    tool = _StandInTool(process, str(BACKEND_DIR))
    try:
        registry, handle = coding_tool._register_dispatch(tool, PLAN)

        assert registry is not None, (
            "the normal dispatch no longer gets a registry back — "
            "fail-closed has eaten the happy path"
        )
        assert handle is not None, (
            "the normal dispatch no longer gets a handle back — the "
            "watchdog would have nothing to reach"
        )
        assert handle in sub_agent_registry.all_handles(PLAN)
    finally:
        _reap(process)


# ---------------------------------------------------------------------------
# 5. The call site reaps the child and re-raises the cancellation.
# ---------------------------------------------------------------------------
def test_the_call_site_reaps_the_child_and_reraises_untouched(
    tmp_path, monkeypatch,
):
    """The failure reaches the caller as itself, and leaves no child behind.

    The registration fails *after* the child was spawned, so somebody
    has to take the child down. That somebody is the dispatch's own
    ``except BaseException`` arm: it reaps the process, drops the
    live-dispatch entry, and re-raises — the cancellation must not be
    swapped for another error, because a replaced exception reads as a
    provider failure and gets retried.
    """
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)
    _install_fake_claude(tmp_path, monkeypatch, _LIVING_CLAUDE)
    # The fake CLI ignores EOF on stdin, so the graceful phase would
    # wait out the full timeout before escalating. Two seconds is
    # enough to prove the escalation reaches it.
    monkeypatch.setattr(ClaudeCodingTool, "GRACEFUL_STOP_TIMEOUT", 2)

    def boom(*args, **kwargs):
        raise RuntimeError("the registry is broken")

    monkeypatch.setattr(sub_agent_registry, "register_sub_agent", boom)

    tool = ClaudeCodingTool()
    with pytest.raises(VerificationCancelled) as excinfo:
        tool.query_json(prompt="hello", system_instruction="sys")

    assert isinstance(excinfo.value.__cause__, RuntimeError), (
        "the registry's own error has to stay on the chain — it is what "
        f"the log points at; the cause is {excinfo.value.__cause__!r}"
    )
    process = tool._current_process
    assert process is not None, (
        "the dispatch spawned a child, so there is a child to reap"
    )
    assert process.poll() is not None, (
        "the child the dispatch spawned is still running — the call site "
        "did not reap it"
    )
    assert id(tool) not in coding_tool._LIVE_DISPATCH_TOOLS, (
        "the live-dispatch entry outlived the dispatch — a later stop "
        "would count a child that is already dead"
    )
