"""The judgment / repair-generation LLM call must be STOPPABLE and RESUMABLE.

What this file pins
-------------------
The verification round has two LLM calls that used to be invisible to
the operator: the round's *judgment* and the *repair-task generation*
that follows it. Both run in the server process, in the auto-loop
thread, after the round has already written its report — and neither
of them registered anywhere, so ``POST /stop`` could not see them. A
plan stopped during the repair call kept burning tokens until the call
finished on its own, and the call's failure was then recorded as a
*failed round* — a verdict on a round the operator had paused on
purpose.

The contracts here:

1. **The dispatch registers.** A dispatch that belongs to a plan
   registers a handle in ``sub_agent_registry`` while it is in flight,
   and drops it when it is over. The handle's ``scoped_tool`` is the
   tool, which is how the stop's existing kill path reaches the
   ``Popen``.
2. **A registration failure stops the dispatch.** Since 2026-11-20 the
   registration is **fail-closed**: a plan dispatch that cannot register
   itself raises ``VerificationCancelled`` instead of running unwatched.
   (Before that it was best-effort, and that is what the incident was
   made of — a dispatch nobody could watch kept running after the
   operator had asked for it to stop.)
3. **A cancelled dispatch raises ``VerificationCancelled`` and is NOT
   retried.** "The operator stopped this" is not "this failed" — and
   since 2026-11-20 the refusal happens **before** the spawn, so a
   cancelled plan gets no process at all.
4. **The negative control for (3).** With no cancel flag, an abnormal
   subprocess exit still takes the ORIGINAL failure path — the
   empty-response retry and all. (3) is not unconditional.
5. **The stop can reach the handle.** The production kill function, the
   one the stop route calls, takes the subprocess down through the
   registry.

Plus the resume half, which is the point of the stable session id: the
repair-content step runs under a session id derived from the plan and
the step, so a ``/start`` in another process re-opens the conversation
the stopped round was having.

How the dispatch is driven
--------------------------
Every test here goes through the PRODUCTION entry —
``ClaudeCodingTool.query_json`` → ``_query_json_internal`` →
``_run_interactive_resilient`` → ``_run_claude_interactive`` →
``subprocess.Popen`` — with a ``claude`` shell script on ``PATH``
standing in for the CLI. Nothing calls an internal method directly, so
a change that moved the registration somewhere the production path
does not reach would fail here rather than pass.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_cancel  # noqa: E402
import coding_tool  # noqa: E402
from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    EmptyResponseError,
    query_json_in_session,
)
from sub_agent_registry import sub_agent_registry  # noqa: E402
from verification_cancel import VerificationCancelled  # noqa: E402

#: The checkout these tests run in.  Every coding tool built
#: in this file names it as its working root — the tool cannot be
#: born without one, and a root that is not named runs the child
#: in the backend server's directory instead.
_REPO_ROOT = Path(__file__).resolve().parents[3]

PLAN = "plan-llm-cancel"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state():
    """The registry and the cancel flag are process-global."""
    verification_cancel._reset_for_tests()
    _drop_handles(PLAN)
    yield
    verification_cancel._reset_for_tests()
    _drop_handles(PLAN)


def _drop_handles(plan_id: str) -> None:
    for handle in list(sub_agent_registry.all_handles(plan_id)):
        try:
            sub_agent_registry.unregister(plan_id, handle)
        except Exception:  # noqa: BLE001 — a broken registry is not the test's job
            pass


def _install_fake_claude(
    tmp_path, monkeypatch, *, body: str = "", announce: bool = True,
    answer: bool = True, preamble: str = "",
) -> tuple:
    """Put an executable ``claude`` first on ``PATH``.

    Returns ``(launches_log, args_log)``. The script speaks the
    stream-json protocol the read loop expects: an optional ``system
    init`` event (carrying whichever session id it was handed),
    whatever ``body`` does, and an optional ``result`` event. ``body``
    is the seam the tests use to make the dispatch hang (until a
    release file appears) or to make it die.

    ``preamble`` runs **before** the script's first line — before the
    ``launch`` record is written.  It is the seam for "the CLI must not
    get as far as running at all": a preamble that blocks forever keeps
    the launch record empty for as long as the child lives, so "the
    child never started" is a fact about the process and not a race.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    launches = tmp_path / "launches.log"
    args_log = tmp_path / "args.log"
    launches.write_text("", encoding="utf-8")
    args_log.write_text("", encoding="utf-8")

    parts = ["#!/bin/sh"]
    if preamble:
        parts.append(preamble)
    parts += [
        f"echo launch >> '{launches}'",
        f"echo \"$@\" >> '{args_log}'",
        'sid=""',
        'prev=""',
        'for a in "$@"; do',
        '  if [ "$prev" = "--session-id" ] || [ "$prev" = "--resume" ];'
        ' then sid="$a"; fi',
        '  prev="$a"',
        "done",
    ]
    if announce:
        parts.append(
            'if [ -n "$sid" ]; then'
            ' echo "{\\"type\\":\\"system\\",\\"subtype\\":\\"init\\",'
            '\\"session_id\\":\\"$sid\\"}"; fi'
        )
    if body:
        parts.append(body)
    if answer:
        parts.append(
            "echo '{\"type\":\"result\",\"subtype\":\"success\","
            "\"result\":\"{\\\"ok\\\": true}\"}'"
        )
    fake = bindir / "claude"
    fake.write_text("\n".join(parts) + "\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", "")
    )
    return launches, args_log


def _wait_for_release(release: Path) -> str:
    """Shell body that blocks until ``release`` exists."""
    return f"while [ ! -f '{release}' ]; do sleep 0.05; done"


def _block_forever() -> str:
    """Shell body that never ends on its own — only a kill ends it."""
    return "while true; do sleep 1; done"


def _launches(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").split())


def _wait_for(predicate, timeout: float = 30.0):
    """Poll ``predicate`` until it is truthy; return its value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return predicate()


def _dispatch_in_background(tool, box: dict) -> threading.Thread:
    """Run ``tool.query_json`` on its own thread; land the outcome in ``box``."""

    def _run():
        try:
            box["result"] = tool.query_json(
                prompt="hello", system_instruction="sys",
            )
        except BaseException as exc:  # noqa: BLE001 — surfaced by the test
            box["error"] = exc

    thread = threading.Thread(target=_run, name="dispatch", daemon=True)
    thread.start()
    return thread


def _kill_through_the_registry(handle) -> None:
    """Kill a handle the way the stop route does.

    ``verification_loop._kill_sub_agent_process`` is the function
    ``_cleanup_dead_verification_processes`` calls for every registered
    handle, and it reaches the subprocess through
    ``handle.scoped_tool._current_process``.
    """
    import server  # noqa: F401 — seeds sys.modules["server"] for the import below
    from verification_loop import _kill_sub_agent_process

    _kill_sub_agent_process(handle)


# ---------------------------------------------------------------------------
# 1. the dispatch registers, and drops the handle when it is over
# ---------------------------------------------------------------------------


def test_a_plan_dispatch_registers_a_handle_and_unregisters_it(
    tmp_path, monkeypatch,
):
    """A dispatch in flight is visible to the registry; a finished one is not.

    The pair is the whole contract: a handle that outlives the dispatch
    would have the heartbeat killing a subprocess that is already gone,
    and a handle that is never registered would leave the stop with
    nothing to kill.
    """
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)
    release = tmp_path / "release"
    _install_fake_claude(
        tmp_path, monkeypatch, body=_wait_for_release(release),
    )

    tool = ClaudeCodingTool( cwd=_REPO_ROOT)
    box: dict = {}
    thread = _dispatch_in_background(tool, box)
    try:
        handles = _wait_for(
            lambda: sub_agent_registry.all_handles(PLAN) or None
        )
        assert handles, (
            "the dispatch never registered a handle — the stop cannot see "
            "a dispatch it cannot find"
        )
        assert len(handles) == 1, (
            f"one dispatch, one handle; got {len(handles)}"
        )
        handle = handles[0]
        assert handle.scoped_tool is tool, (
            "the kill path reaches the subprocess through the handle's "
            "scoped_tool._current_process"
        )
        assert handle.vp_id.startswith("llm-dispatch-"), (
            f"the handle's vp_id is {handle.vp_id!r}; the registry's "
            "idempotency key is (plan_id, vp_id, attempt), so a FIXED id "
            "would make two concurrent dispatches overwrite each other"
        )
    finally:
        release.write_text("go", encoding="utf-8")
        thread.join(timeout=60)

    assert "error" not in box, f"the dispatch failed: {box.get('error')!r}"
    assert box.get("result") == {"ok": True}
    assert sub_agent_registry.all_handles(PLAN) == [], (
        "the handle outlived the dispatch — the heartbeat would go on "
        "offering to kill a subprocess that is already gone"
    )


def test_two_concurrent_dispatches_get_two_distinct_handles(
    tmp_path, monkeypatch,
):
    """The registry's idempotency key is ``(plan_id, vp_id, attempt)``.

    A fixed ``vp_id`` would make the second registration REFRESH the
    first handle instead of adding one — so the first dispatch's
    subprocess would become unreachable through the registry, which is
    the one thing the handle exists for. The ids therefore have to be
    unique per dispatch.
    """
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)
    release = tmp_path / "release"
    _install_fake_claude(
        tmp_path, monkeypatch, body=_wait_for_release(release),
    )

    tools = [ClaudeCodingTool( cwd=_REPO_ROOT), ClaudeCodingTool( cwd=_REPO_ROOT)]
    boxes: list = [{}, {}]
    threads = [
        _dispatch_in_background(tool, box)
        for tool, box in zip(tools, boxes)
    ]
    try:
        handles = _wait_for(
            lambda: (
                sub_agent_registry.all_handles(PLAN)
                if len(sub_agent_registry.all_handles(PLAN)) >= 2 else None
            )
        )
        assert handles and len(handles) == 2, (
            f"two concurrent dispatches must be two handles; got {handles!r}"
        )
        vp_ids = [h.vp_id for h in handles]
        assert len(set(vp_ids)) == 2, (
            f"the two handles share a vp_id ({vp_ids!r}) — the second "
            "registration refreshed the first instead of adding a handle"
        )
        assert {h.scoped_tool for h in handles} == set(tools), (
            "each handle must point at the tool whose process it owns"
        )
    finally:
        release.write_text("go", encoding="utf-8")
        for thread in threads:
            thread.join(timeout=60)


def test_no_plan_means_no_registration(tmp_path, monkeypatch):
    """A dispatch with no plan behind it registers nothing.

    Every non-plan scenario (a CLI with no plan, the unit tests, the
    vendor tools) must behave exactly as it did before.
    """
    monkeypatch.delenv("PDT_PLAN_ID", raising=False)
    _install_fake_claude(tmp_path, monkeypatch)

    tool = ClaudeCodingTool( cwd=_REPO_ROOT)
    assert tool.query_json(prompt="hello", system_instruction="sys") == {
        "ok": True
    }
    assert sub_agent_registry.all_handles(PLAN) == []


# ---------------------------------------------------------------------------
# 2. a registration failure stops the dispatch (fail-closed)
# ---------------------------------------------------------------------------


def test_a_registration_failure_does_not_affect_the_dispatch(
    tmp_path, monkeypatch,
):
    """A registry that cannot answer is a failed registration — fail closed.

    **The contract changed on 2026-11-20.**  The old one — "the registry
    is a watchdog aid; a dispatch that cannot be watched still runs" —
    was the incident's own reasoning: a dispatch that registered nothing
    left the operator's ``/stop`` with no handle to reach, so the stop
    could not kill the process the dispatch had just started, and that
    process ran on after the operator had asked for it to stop.

    So a **plan** dispatch that cannot register itself now raises
    :class:`VerificationCancelled` instead of running unwatched.  The
    guarantee this test pins is the new one: the registration is
    attempted, the refusal reaches the caller as itself, and the dispatch
    does not run the work.

    The registration is asserted to have been ATTEMPTED: without that,
    the test would also pass on a build that never registers at all,
    and would say nothing about the behaviour it names.

    The CLI is blocked before its first line, so an empty launch record
    means the child never got to run — the dispatch was refused, not
    merely reported afterwards.  The child the dispatch had already
    spawned (the registration runs *after* the ``Popen``) must be gone
    when the refusal surfaces: an unwatched subprocess is exactly what
    the operator's stop could not reach.
    """
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)
    launches, _args = _install_fake_claude(
        tmp_path, monkeypatch, preamble=_block_forever(),
    )
    # The graceful shutdown waits for the child before it escalates; a
    # child that never ends would make the test wait out the whole
    # grace period.
    monkeypatch.setattr(ClaudeCodingTool, "GRACEFUL_STOP_TIMEOUT", 1)

    attempts = []

    def _boom(*args, **kwargs):
        attempts.append(args)
        raise RuntimeError("the registry is broken")

    monkeypatch.setattr(sub_agent_registry, "register_sub_agent", _boom)

    tool = ClaudeCodingTool( cwd=_REPO_ROOT)
    try:
        with pytest.raises(VerificationCancelled) as excinfo:
            tool.query_json(prompt="hello", system_instruction="sys")
    finally:
        process = tool._current_process
        if process is not None and process.poll() is None:
            process.kill()

    assert attempts, (
        "the dispatch never tried to register — a build that registers "
        "nothing passes this test for the wrong reason"
    )
    assert excinfo.value.plan_id == PLAN
    assert "registering the dispatch" in str(excinfo.value), (
        "the refusal has to name the step it happened in, so the log says "
        f"what went wrong; it says {str(excinfo.value)!r}"
    )
    assert _launches(launches) == 0, (
        "the dispatch ran the CLI even though it could not register "
        "itself — the fail-closed rule is what keeps an unwatched "
        "subprocess from running"
    )
    assert process is not None, (
        "the dispatch spawned a child, so there is a child to reap"
    )
    assert process.poll() is not None, (
        "the child the dispatch spawned is still running — the call site "
        "did not reap it, and a subprocess nobody can reach is what the "
        "operator's stop cannot kill"
    )
    assert id(tool) not in coding_tool._LIVE_DISPATCH_TOOLS, (
        "the live-dispatch entry outlived the dispatch — a later stop "
        "would count a child that is already dead"
    )


def test_an_unregistration_failure_does_not_affect_the_dispatch(
    tmp_path, monkeypatch,
):
    """The same for the cleanup: a handle that cannot be dropped is logged."""
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)
    _install_fake_claude(tmp_path, monkeypatch)

    attempts = []

    def _boom(*args, **kwargs):
        attempts.append(args)
        raise RuntimeError("the registry is broken")

    monkeypatch.setattr(sub_agent_registry, "unregister", _boom)

    tool = ClaudeCodingTool( cwd=_REPO_ROOT)
    assert tool.query_json(prompt="hello", system_instruction="sys") == {
        "ok": True
    }
    assert attempts, (
        "the dispatch never tried to unregister — a build that never "
        "registers passes this test for the wrong reason"
    )


# ---------------------------------------------------------------------------
# 3. a cancelled dispatch raises, and is not retried
# ---------------------------------------------------------------------------


def test_a_cancelled_dispatch_raises_and_is_not_retried(
    tmp_path, monkeypatch, caplog,
):
    """The operator stopped this plan: say so, and do not run it again.

    **The contract changed on 2026-11-20.**  The refusal moved *before*
    the spawn: a plan that is being stopped gets no process at all —
    there is nothing to reap, nothing to watch, and nothing left over
    for a later stop to count.  So the cancelled dispatch does not need
    a kill to end it: it never starts.  What it still has to do is raise
    :class:`VerificationCancelled` — "the operator stopped this" and
    not "this broke" — and it must not be run again.

    The retry is the thing that matters. An empty reply is normally
    retried (the provider may have hiccuped), and a timeout is normally
    retried on the next provider — but a stop is a decision, and
    re-running the same work on another provider is exactly what the
    operator asked not to happen.  The refusal is therefore *counted*:
    exactly one refusal, and no CLI launch at all.
    """
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)
    launches, _args = _install_fake_claude(tmp_path, monkeypatch)
    verification_cancel.request_cancel(PLAN)

    tool = ClaudeCodingTool( cwd=_REPO_ROOT)
    with caplog.at_level("WARNING", logger="coding_tool"):
        with pytest.raises(VerificationCancelled) as excinfo:
            tool.query_json(prompt="hello", system_instruction="sys")

    assert excinfo.value.plan_id == PLAN, (
        f"the refusal has to name the plan it is about; it names "
        f"{excinfo.value.plan_id!r}"
    )
    assert _launches(launches) == 0, (
        f"the cancelled dispatch ran the CLI {_launches(launches)} time(s) "
        "— a plan that is being stopped must not get a process at all"
    )
    refusals = [
        record for record in caplog.records
        if "[spawn_gate] rejected" in record.getMessage()
    ]
    assert len(refusals) == 1, (
        "the dispatch must be refused exactly once: refused twice means it "
        "was retried on another provider, refused zero times means it was "
        f"never gated; got {len(refusals)}"
    )
    assert sub_agent_registry.all_handles(PLAN) == [], (
        "a cancelled dispatch must not register a handle — there is no "
        "child for a handle to point at"
    )


# ---------------------------------------------------------------------------
# 4. negative control — no cancel flag, the original failure path stands
# ---------------------------------------------------------------------------


def test_without_a_cancel_flag_an_abnormal_exit_still_fails_and_retries(
    tmp_path, monkeypatch,
):
    """The cancellation is not unconditional.

    With no cancel flag, a subprocess that dies without answering is
    still an empty response, and the empty response is still retried.
    If the cancellation arm had swallowed that, every provider hiccup
    would have looked like an operator stop.
    """
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)
    launches, _args = _install_fake_claude(
        tmp_path, monkeypatch, body="exit 3", announce=False, answer=False,
    )

    tool = ClaudeCodingTool( cwd=_REPO_ROOT)
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    with pytest.raises(EmptyResponseError):
        tool.query_json(prompt="hello", system_instruction="sys")

    assert _launches(launches) == 1 + ClaudeCodingTool.MAX_EMPTY_RESPONSE_RETRIES, (
        "the original empty-response retry must still run when nothing "
        "was cancelled"
    )


# ---------------------------------------------------------------------------
# 5. the stop reaches the handle through the registry
# ---------------------------------------------------------------------------


def test_the_stop_reaches_the_subprocess_through_the_registry(
    tmp_path, monkeypatch,
):
    """The production kill function, driven by the registry, kills the child.

    This is the path the stop route takes: ``_cleanup_dead_verification_
    processes`` iterates ``all_handles(plan_id)`` and hands each handle
    to ``_kill_sub_agent_process``, which reaches the ``Popen`` through
    ``handle.scoped_tool._current_process``. The test drives the same
    function with the same handle, so a registration that did not carry
    the tool would leave the subprocess running.
    """
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)
    release = tmp_path / "release"
    _install_fake_claude(
        tmp_path, monkeypatch, body=_wait_for_release(release),
    )

    tool = ClaudeCodingTool( cwd=_REPO_ROOT)
    box: dict = {}
    thread = _dispatch_in_background(tool, box)
    try:
        handles = _wait_for(
            lambda: sub_agent_registry.all_handles(PLAN) or None
        )
        assert handles, "the dispatch never registered a handle"
        handle = handles[0]

        process = tool._current_process
        assert process is not None, (
            "the tool publishes its subprocess for the kill path to reach"
        )
        assert process.poll() is None, "the subprocess is not running yet"

        _kill_through_the_registry(handle)

        deadline = time.monotonic() + 20.0
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process.poll() is not None, (
            "the kill reached nothing — the subprocess is still running"
        )
    finally:
        release.write_text("go", encoding="utf-8")
        thread.join(timeout=60)


# ---------------------------------------------------------------------------
# 6. the resume half — a stable session id for the repair-content step
# ---------------------------------------------------------------------------


def test_the_repair_content_step_creates_then_resumes_one_stable_session(
    tmp_path, monkeypatch,
):
    """The step's session id is derived from the plan and the step.

    A stable id is what makes the step resumable across a ``/stop`` and
    a ``/start``: the first dispatch CREATES the session under that id
    (``--session-id``), every later dispatch RE-OPENS it (``--resume``),
    and the id is written into the plan's session sidecar so the next
    process can find it.
    """
    from plan_sessions import load_session_id
    from repair_generator import (
        persist_repair_content_session,
        repair_content_session,
        stable_session_id,
    )

    plan_dir = tmp_path / "plans" / PLAN
    plan_dir.mkdir(parents=True)

    # --- process #1: no recorded id, so the step asks for a session
    #     under the id DERIVED from the plan and the step.
    session = repair_content_session(plan_dir)
    assert session.session_id is None, (
        "a plan with no recorded session has no live session yet"
    )
    pinned = session.pinned_session_id
    assert pinned, (
        "the first dispatch must ask for a session under a KNOWN id, so "
        "that a later process can re-open it"
    )
    assert pinned == stable_session_id(PLAN, "repair_content")
    assert pinned == stable_session_id(PLAN, "repair_content"), (
        "the id must be derived, not random — two processes that both "
        "start this step for this plan have to land in the same session"
    )

    _install_fake_claude(tmp_path, monkeypatch)
    tool = ClaudeCodingTool( cwd=_REPO_ROOT)
    assert query_json_in_session(
        tool, session, prompt="hello", system_instruction="sys",
    ) == {"ok": True}

    args = (tmp_path / "args.log").read_text(encoding="utf-8")
    assert f"--session-id {pinned}" in args, (
        f"the first dispatch must CREATE the session under the derived "
        f"id; the CLI was called with: {args!r}"
    )
    assert "--resume" not in args, (
        f"the first dispatch must not resume a session that does not "
        f"exist yet; the CLI was called with: {args!r}"
    )
    assert session.session_id == pinned, (
        "the handle must carry the id the dispatch actually used"
    )

    persist_repair_content_session(plan_dir, session)
    assert load_session_id(plan_dir, "repair_content") == pinned, (
        "the id must reach the plan directory, or a /start in another "
        "process cannot find the session"
    )

    # --- process #2: a fresh in-process cache, reading the sidecar.
    import repair_generator

    repair_generator._REPAIR_CONTENT_SESSIONS.clear()
    resumed = repair_content_session(plan_dir)
    assert resumed.session_id == pinned, (
        "a new process must re-open the recorded session, not start a "
        "new one"
    )
    assert resumed.pinned_session_id is None, (
        "a session that exists is re-opened, not created"
    )

    (tmp_path / "args.log").write_text("", encoding="utf-8")
    assert query_json_in_session(
        tool, resumed, prompt="hello again", system_instruction="sys",
    ) == {"ok": True}
    args2 = (tmp_path / "args.log").read_text(encoding="utf-8")
    assert f"--resume {pinned}" in args2, (
        f"the second dispatch must RE-OPEN the recorded session; the CLI "
        f"was called with: {args2!r}"
    )
