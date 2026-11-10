"""2026-09-14 分诊式看门狗: probe liveness before
declaring a verification dead, and KILL leftovers when it truly is.

Two constraints drive this:

  * "如果 watchdog 真的不杀进程的话，这是不对的" — the
    ``verification_log_stale`` / ``thread_died`` path used to only flip
    status flags; orphaned pytest/bash (disowned PPID-1 children the
    sub-agent registry cannot see) burned CPU forever. Now a truly-dead
    verification kills registered handles AND /tmp-matched orphans.
  * "单个 automated test 居然要 18 分钟" — a full-suite VP legitimately
    outruns the 15-min log-staleness threshold while streaming progress
    to its /tmp tee file (VP-023 was falsely flagged while healthy).
    Now a stale round log with a live VP in flight is suppressed; only
    a 45-min hard ceiling (``VERIFICATION_HARD_STALE_SECONDS``) with no
    liveness signal at all concludes death.

Covered here:
  * fresh /tmp tee artifact suppresses the stale flag;
  * fresh sub_agent_registry handle suppresses it;
  * no signal + age within hard cap → hold (no flag);
  * no signal + age past hard cap → flag AND kill leftovers;
  * no running VP in the newest round log → original 15-min semantics;
  * thread-death → cleanup fires even without staleness.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import server as server_mod
from sub_agent_registry import SubAgentHandle, sub_agent_registry


PLAN = "plan-triage"

#: The stop_reason each trigger must be recorded with.  The assertions
#: below read the expected value out of here and name the trigger they
#: exercise, so an assertion always states the reason of the TRIGGER —
#: never a literal that could belong to a different one.  (Before
#: 2026-11-02 the watchdog recorded ``verification_thread_died_unexpectedly``
#: for every trigger, so a round declared dead because its log went
#: silent was written down as a thread death.)
LOG_STALE_STOP_REASON = "verification_log_stale"
THREAD_DEATH_STOP_REASON = "verification_thread_died_unexpectedly"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _write_round_log(plan_dir: Path, vp_id: str = "VP-023",
                     mtime_age: float = 1200.0) -> Path:
    """Newest-round log with a vp_start and NO vp_complete (VP in flight),
    back-dated ``mtime_age`` seconds so it trips the staleness threshold."""
    logs = plan_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    p = logs / "verification_2_20260914_172324.log"
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "verification_point_id": vp_id,
            "event_type": "vp_start",
            "timestamp": "2026-09-14T17:23:28",
        }, ensure_ascii=False) + "\n")
    old = time.time() - mtime_age
    os.utime(p, (old, old))
    return p


def _running_state(thread_alive: bool = True) -> dict:
    thread = Mock()
    thread.is_alive.return_value = thread_alive
    return {
        "verification_status": "running",
        "verification_round": 2,
        "thread": thread,
        "started_at": "2026-09-14T17:00:00",
        "started_at_ts": time.time() - 3600.0,  # well past startup grace
    }


@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    plan_dir = tmp_path / "plans" / PLAN
    plan_dir.mkdir(parents=True)
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(server_mod, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(server_mod, "_VP_TMP_ROOT", tmp_root)
    monkeypatch.setattr(
        server_mod, "VERIFICATION_WATCHDOG_STALENESS_SECONDS", 900.0
    )
    monkeypatch.setattr(
        server_mod, "VERIFICATION_HARD_STALE_SECONDS", 2700.0
    )
    # The liveness probe shells out to ``pgrep``; on a machine that
    # really is running VP-023 (the live plan this feature was built
    # for) a real match would make every "no signal" case read as
    # alive. Default to "no matching process" and let the dedicated
    # process-signal test opt back in.
    monkeypatch.setattr(
        server_mod, "_vp_artifact_processes", lambda vp_id: []
    )
    # Reset the per-plan dedup clock so the lazy check always acts.
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN, None)
    # 2026-11-10: the thread-death branch now asks whether a background
    # execution is outstanding before it declares a death (a hand-off
    # ends the thread too).  The answer comes from the process-global
    # ``_execution_state``, so the plan's entry is cleared here — a
    # leftover from another test would make a dead thread look like a
    # hand-off.
    server_mod._execution_state.pop(PLAN, None)
    server_mod._verification_state[PLAN] = _running_state()
    yield plan_dir, tmp_root
    server_mod._verification_state.pop(PLAN, None)
    server_mod._execution_state.pop(PLAN, None)
    sub_agent_registry.cleanup_for_plan(PLAN)


def _run_lazy_check() -> Mock:
    """Invoke the lazy check with the terminal-persist call observable."""
    with patch.object(server_mod, "_persist_verification_terminal") as persist, \
         patch.object(server_mod, "_mark_verification_failed_dead") as mark, \
         patch.object(server_mod, "_update_plan_state_to_terminal"), \
         patch.object(server_mod, "_kill_orphaned_vp_processes",
                      return_value=[]) as orphan_kill, \
         patch.object(server_mod, "_kill_sub_agent_process") as handle_kill:
        server_mod._lazy_check_verification(PLAN)
    persist._orphan_kill = orphan_kill  # type: ignore[attr-defined]
    persist._handle_kill = handle_kill  # type: ignore[attr-defined]
    # The mark that writes the in-memory state + the side-channel
    # snapshot.  It is called with the reason the ladder detected, so
    # its call args are the second half of "the record says why".
    persist._mark = mark  # type: ignore[attr-defined]
    return persist


# ---------------------------------------------------------------------------
# triage behavior
# ---------------------------------------------------------------------------

def test_fresh_tee_artifact_suppresses_stale_flag(plan_env):
    """VP mid-flight with a growing /tmp tee file must NOT be flagged at
    the 15-min mark — the round log is silent because the VP streams
    progress elsewhere (the VP-023 false positive)."""
    plan_dir, tmp_root = plan_env
    _write_round_log(plan_dir, mtime_age=1200.0)
    tee = tmp_root / "vp_023_run4_progress.log"
    tee.write_text("tests/test_x.py .... [58%]\n", encoding="utf-8")

    persist = _run_lazy_check()

    persist.assert_not_called(), (
        "a live VP (fresh tee artifact) must suppress the stale flag"
    )


def test_fresh_registry_handle_suppresses_stale_flag(plan_env):
    """A registered sub-agent handle with fresh progress is equally
    alive — e.g. a foreground pytest streaming inside the tool
    subprocess."""
    plan_dir, _ = plan_env
    _write_round_log(plan_dir, mtime_age=1200.0)
    handle = SubAgentHandle(
        plan_id=PLAN, vp_id="VP-023", attempt=0,
        last_progress_ts=time.time(),  # fresh
    )
    sub_agent_registry._by_plan.setdefault(PLAN, []).append(handle)

    persist = _run_lazy_check()

    persist.assert_not_called()


def test_no_signal_within_hard_cap_holds(plan_env):
    """No liveness signal but the round log is only 20 min old (< 45-min
    hard cap): hold off — the agent may not follow the tee naming
    convention. Flag nothing yet."""
    plan_dir, _ = plan_env
    _write_round_log(plan_dir, mtime_age=1200.0)  # 20 min < 45 min

    persist = _run_lazy_check()

    persist.assert_not_called(), (
        "within the hard cap, absence of a signal must not flag stale"
    )


def test_no_signal_past_hard_cap_flags_and_kills(plan_env):
    """No liveness signal and the round log is 50 min old (> hard cap):
    the VP is TRULY dead — flag failed AND kill leftovers (user
    flagging dead without killing leaves zombies)."""
    plan_dir, _ = plan_env
    _write_round_log(plan_dir, mtime_age=3000.0)  # 50 min > 45 min

    persist = _run_lazy_check()

    persist.assert_called_once()
    assert persist.call_args[0][0] == PLAN
    assert persist.call_args[0][1] == "failed"
    # The trigger is the silent round log — that is the reason the
    # round is written off with.
    assert persist.call_args[0][2] == LOG_STALE_STOP_REASON
    # And the in-memory mark is told the same reason.
    assert persist._mark.call_args[0][1] == LOG_STALE_STOP_REASON  # type: ignore[attr-defined]
    persist._orphan_kill.assert_called_once()  # type: ignore[attr-defined]
    assert "VP-023" in persist._orphan_kill.call_args[0][0]  # type: ignore[attr-defined]


def test_no_running_vp_with_live_thread_holds(plan_env):
    """Newest round log has NO in-flight VP — the round is in a
    post-execution phase (report judgment) that runs
    in-process and writes nothing to the plan dir. A LIVE thread there is
    healthy: hold until the hard cap instead of terminalising it.

    Live evidence: a round was stamped ``verification_log_stale`` while
    its judgment sub-agents were still working."""
    plan_dir, _ = plan_env
    logs = plan_dir / "logs"
    logs.mkdir(parents=True)
    p = logs / "verification_2_20260914_172324.log"
    p.write_text(json.dumps({
        "verification_point_id": "VP-006",
        "event_type": "vp_complete",
        "timestamp": "2026-09-14T17:27:35",
    }) + "\n", encoding="utf-8")
    old = time.time() - 1200.0  # past the 900s threshold, under the 2700s cap
    os.utime(p, (old, old))

    persist = _run_lazy_check()

    persist.assert_not_called(), (
        "a live thread in a post-execution phase must not be terminalised"
    )


def test_no_running_vp_with_live_thread_flags_past_hard_cap(plan_env):
    """The patience is bounded: past the hard cap even a live thread is
    declared dead (a genuinely deadlocked thread is still caught)."""
    plan_dir, _ = plan_env
    logs = plan_dir / "logs"
    logs.mkdir(parents=True)
    p = logs / "verification_2_20260914_172324.log"
    p.write_text("{}\n", encoding="utf-8")
    old = time.time() - 3000.0  # > 2700s hard cap
    os.utime(p, (old, old))

    persist = _run_lazy_check()

    persist.assert_called_once()
    # The trigger is the silent round log — that is the reason the
    # round is written off with.
    assert persist.call_args[0][2] == LOG_STALE_STOP_REASON
    assert persist._mark.call_args[0][1] == LOG_STALE_STOP_REASON  # type: ignore[attr-defined]


def test_no_running_vp_with_dead_thread_flags_immediately(plan_env):
    """Thread death outranks the post-execution patience — T1 fires on
    the first tick regardless of how fresh the log is."""
    plan_dir, _ = plan_env
    _write_round_log(plan_dir, mtime_age=60.0)  # fresh log
    server_mod._verification_state[PLAN]["thread"] = (
        _running_state(thread_alive=False)["thread"]
    )

    persist = _run_lazy_check()

    persist.assert_called_once()
    # The trigger is the dead thread — that is the reason the round is
    # written off with.
    assert persist.call_args[0][2] == THREAD_DEATH_STOP_REASON
    assert persist._mark.call_args[0][1] == THREAD_DEATH_STOP_REASON  # type: ignore[attr-defined]


def test_thread_death_cleans_up_processes(plan_env):
    """T1 (thread dead) must kill leftovers immediately — no staleness
    wait — because nothing is left to collect results or unregister."""
    plan_dir, _ = plan_env
    _write_round_log(plan_dir, mtime_age=60.0)  # log itself is FRESH
    server_mod._verification_state[PLAN]["thread"] = (
        _running_state(thread_alive=False)["thread"]
    )
    handle = SubAgentHandle(
        plan_id=PLAN, vp_id="VP-023", attempt=0,
        last_progress_ts=time.time(),
    )
    sub_agent_registry._by_plan.setdefault(PLAN, []).append(handle)

    persist = _run_lazy_check()

    persist.assert_called_once()
    # The trigger is the dead thread — that is the reason the round is
    # written off with.
    assert persist.call_args[0][2] == THREAD_DEATH_STOP_REASON
    assert persist._mark.call_args[0][1] == THREAD_DEATH_STOP_REASON  # type: ignore[attr-defined]
    persist._handle_kill.assert_called_once()  # type: ignore[attr-defined]
    # And the handle must have been unregistered by the cleanup.
    assert sub_agent_registry.all_handles(PLAN) == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_normalized_vp_token():
    assert server_mod._normalized_vp_token("VP-023") == "vp_023"
    assert server_mod._normalized_vp_token("VP-023-1") == "vp_023_1"


def test_hard_cap_exceeds_stale_threshold():
    """2026-09-14 deploy bug: the hard cap defaulted to an ABSOLUTE 2700s
    while ``.env`` sets the staleness threshold to 3600s — the cap was
    below the threshold, so the "hold" branch was dead code and every
    first stale trip became an immediate kill. The default is now
    derived (threshold + 30 min patience) and must always exceed the
    threshold."""
    assert (
        server_mod.VERIFICATION_HARD_STALE_SECONDS
        > server_mod.VERIFICATION_WATCHDOG_STALENESS_SECONDS
    ), "a hard cap below the stale threshold turns the first trip into a kill"


def test_vp_artifact_processes_parses_pgrep_output(monkeypatch):
    """The /tmp-artifact pattern is deliberately narrow (a bare VP id
    would match unrelated processes)."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return Mock(stdout="1234\n5678\nnot-a-pid\n", returncode=0)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)
    pids = server_mod._vp_artifact_processes("VP-023")
    assert pids == [1234, 5678]
    assert captured["args"][:2] == ["pgrep", "-f"]
    assert captured["args"][2] == "/tmp/.*vp_023"


def test_vp_artifact_processes_survives_pgrep_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("pgrep missing")

    monkeypatch.setattr(server_mod.subprocess, "run", boom)
    assert server_mod._vp_artifact_processes("VP-023") == []


def test_live_process_suppresses_stale_flag(plan_env, monkeypatch):
    """Strongest liveness signal: the VP's own pytest is still running
    even though nothing has been logged recently (e.g. a quiet
    ``docker compose up`` phase)."""
    plan_dir, _ = plan_env
    _write_round_log(plan_dir, mtime_age=3000.0)  # past the hard cap
    monkeypatch.setattr(
        server_mod, "_vp_artifact_processes", lambda vp_id: [4242]
    )

    persist = _run_lazy_check()

    persist.assert_not_called(), (
        "a live process referencing the VP's /tmp artifacts proves the "
        "VP is running — never flag or kill it"
    )


# ---------------------------------------------------------------------------
# 2026-11-10: the hand-off's own log file
# ---------------------------------------------------------------------------

def test_a_fresh_repair_log_suppresses_the_stale_flag(plan_env):
    """A hand-off runs the repair round in ANOTHER process, and that
    round's progress lands in ``logs/repair_*.log``.

    The verification log is then legitimately silent — the verification
    thread handed off and returned — while the repair round is healthy
    and writing.  The staleness set did not read the repair log, so the
    silence of the verification log read as a death: production,
    2026-11-10, a plan was stamped ``failed / verification_log_stale``
    while its repair executor was still running.

    Same shape as the ``vp_attempts`` and ``execution.log`` globs that
    already fix this class of bug: the progress is already being
    written, the watchdog simply was not looking at the file it lands
    in.  So this adds the file to the set — nothing else.
    """
    plan_dir, _ = plan_env
    # The verification log is silent — the round handed off.
    _write_round_log(plan_dir, mtime_age=3000.0)
    # The repair round is alive and writing.
    logs = plan_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    repair = logs / "repair_20261110_101500.log"
    repair.write_text("repair executor running\n", encoding="utf-8")

    persist = _run_lazy_check()

    persist.assert_not_called(), (
        "a fresh repair log is proof the hand-off's repair round is "
        "alive — the silent verification log must not kill it"
    )
