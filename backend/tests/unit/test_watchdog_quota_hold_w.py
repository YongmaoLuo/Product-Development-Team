"""2026-10-14: a round parked on a closed quota
window must not be read as a dead one, and the hard ceiling is 1 hour.

Production evidence — a 2026-10-12 plan,
round 1, where a healthy, ultimately-successful round was killed 7 min
before it finished:

  * 10:30:19  last line in ``logs/verification_1_*.log``
              (``binary_rebuild_attempted``); the agent then needed its
              next LLM call to go on with the work;
  * 10:42:03  ``provider_quota_window_wait_entered`` in ``execution.log``
              — scene ``verification`` had no dispatchable provider;
  * 10:47 / 10:52 / ... / 11:17  one probe line per 300s wake
              (``_quota_wake_probe_sec``), each in ``execution.log``;
  * 11:15:18  watchdog stamped ``verification_log_stale`` — round-log
              age 2700s, exactly the old 45-min hard ceiling;
  * 11:22:07  ``provider_quota_window_wait_ended``; the round resumed
              (``judgment_heartbeat`` x6) and completed.

The wait was alive the whole time and wrote a line every five minutes.
It wrote those lines to ``execution.log``, which the watchdog's
progress-evidence set did not read: it read ``logs/verification_*.log``,
``logs/vp_attempts/*.log`` and ``verification_execution_results.json``.
Busy, read as dead — the same failure the ``vp_attempts`` glob fixed on
2026-09-13, one file over.

Covered here:
  * the hard ceiling's default is 1 hour, and stays above the threshold
    for every threshold (the invariant that keeps the hold branch alive);
  * a round whose only liveness is the quota-wait heartbeat in
    ``execution.log`` is NOT terminalised, even with the round log past
    the hard ceiling;
  * ``execution.log`` is load-bearing: delete it from that same setup and
    the round IS terminalised — so the heartbeat really is what saves it;
  * a genuinely dead round (no file touched at all) is still caught, past
    the hard ceiling and not before.
"""

from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import server as server_mod


PLAN = "plan-quota-hold"


class _AlwaysDefault(dict):
    """``os.environ`` stand-in whose ``get`` always yields the default,
    so the shipped expression is evaluated on its no-override branch."""

    def get(self, key, default=None):  # noqa: D102 - dict protocol
        return default


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _running_state(thread_alive: bool = True) -> dict:
    thread = Mock()
    thread.is_alive.return_value = thread_alive
    return {
        "verification_status": "running",
        "verification_round": 1,
        "thread": thread,
        "started_at": "2026-10-14T10:28:43",
        "started_at_ts": time.time() - 7200.0,  # well past startup grace
    }


@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    """A running verification whose plan dir holds only what the test writes.

    The watchdog's two constants are deliberately NOT monkeypatched: the
    contract under test is about their production defaults, and pinning
    them here would let the module regress unnoticed.
    """
    plan_dir = tmp_path / "plans" / PLAN
    plan_dir.mkdir(parents=True)
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(server_mod, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(server_mod, "_VP_TMP_ROOT", tmp_root)
    # The liveness probe shells out to ``pgrep``; default it to "no
    # matching process" so the no-VP-in-flight branch is deterministic.
    monkeypatch.setattr(
        server_mod, "_vp_artifact_processes", lambda vp_id: []
    )
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN, None)
    server_mod._verification_state[PLAN] = _running_state()
    yield plan_dir
    server_mod._verification_state.pop(PLAN, None)


def _write_round_log(plan_dir: Path, mtime_age: float) -> Path:
    """Round log holding a COMPLETED VP and nothing in flight — the
    incident's shape: post-execution, so there is no VP for the triage
    probe to find alive."""
    logs = plan_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    p = logs / "verification_1_20261014_102843.log"
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "verification_point_id": "VP-010",
            "event_type": "vp_complete",
            "timestamp": "2026-10-14T10:30:19",
        }, ensure_ascii=False) + "\n")
        f.write(json.dumps({
            "verification_point_id": "VP-011",
            "event_type": "binary_rebuild_attempted",
            "timestamp": "2026-10-14T10:30:19",
        }, ensure_ascii=False) + "\n")
    old = time.time() - mtime_age
    os.utime(p, (old, old))
    return p


def _write_execution_log(plan_dir: Path, mtime_age: float) -> Path:
    """The plan's ``execution.log`` carrying the quota-wait heartbeat at
    the real 300s probe cadence, in the shape ``ExecutionLogger`` writes
    (JSON lines, ``ts``/``level``/``event``/``message``/``data``)."""
    p = plan_dir / "execution.log"
    with open(p, "w", encoding="utf-8") as f:
        for n in range(8):
            f.write(json.dumps({
                "ts": "2026-10-14T02:42:%02d" % (3 + 5 * n),
                "level": "INFO",
                "event": "provider_quota_window_wait",
                "message": (
                    "Probing 300.0s for a quota window to reopen before "
                    "re-deciding 'verification'"
                ),
                "data": {
                    "scene": "verification",
                    "quota_exhausted": ["MiniMax Huawei"],
                    "wait_sec": 300.0,
                    "wake_rounds": n + 1,
                },
            }, ensure_ascii=False) + "\n")
    old = time.time() - mtime_age
    os.utime(p, (old, old))
    return p


def _run_lazy_check() -> Mock:
    """Drive the REAL watchdog entry point with its terminal-persist call
    observable. ``_lazy_check_verification`` is what the server heartbeat
    calls once per running plan (the ``for plan_id in
    list(_verification_state.keys())`` loop in ``server.py``) — this is
    the production decision, not an internal helper."""
    with patch.object(server_mod, "_persist_verification_terminal") as persist, \
         patch.object(server_mod, "_mark_verification_failed_dead"), \
         patch.object(server_mod, "_update_plan_state_to_terminal"), \
         patch.object(server_mod, "_kill_orphaned_vp_processes",
                      return_value=[]), \
         patch.object(server_mod, "_kill_sub_agent_process"), \
         patch.object(server_mod, "_cleanup_dead_verification_processes") \
         as cleanup:
        server_mod._lazy_check_verification(PLAN)
    persist._cleanup = cleanup  # type: ignore[attr-defined]
    return persist


# ---------------------------------------------------------------------------
# the 1-hour hard ceiling
# ---------------------------------------------------------------------------

def test_hard_cap_default_is_one_hour():
    """The operator's number, asserted as the number and not as
    "bigger than the threshold" — a weak relative assertion would pass
    just as happily at 90 min."""
    assert server_mod.VERIFICATION_WATCHDOG_STALENESS_SECONDS == 900.0, (
        "the 15-min log-staleness threshold is unchanged by this contract"
    )
    assert server_mod.VERIFICATION_HARD_STALE_SECONDS == 3600.0, (
        "the hard ceiling must be 1 hour, was 2700s (45 min)"
    )


def _shipped_hard_cap_default() -> str:
    """The default expression ``server.py`` actually ships for
    ``VERIFICATION_HARD_STALE_SECONDS``, read out of its AST.

    Retyping the formula in the test would make it a tautology: it would
    go on passing after ``server.py`` changed to an absolute 3600, which
    is the very edit the invariant exists to prevent. Parsing the module
    is what ties this assertion to the code that ships.
    """
    tree = ast.parse(Path(server_mod.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name)
                    and target.id == "VERIFICATION_HARD_STALE_SECONDS"):
                continue
            # The value is ``float(os.environ.get(KEY, DEFAULT))`` — the
            # default is the second argument of the inner get, wherever
            # the wrapping float() happens to sit.
            for inner in ast.walk(node.value):
                if (isinstance(inner, ast.Call)
                        and ast.unparse(inner.func) == "os.environ.get"
                        and len(inner.args) >= 2):
                    return ast.unparse(inner.args[1])
            raise AssertionError(
                "VERIFICATION_HARD_STALE_SECONDS is no longer an "
                "os.environ.get(KEY, DEFAULT)"
            )
    raise AssertionError("VERIFICATION_HARD_STALE_SECONDS not assigned")


def _eval_shipped_hard_cap(threshold: float) -> float:
    """Evaluate the shipped default with ``threshold`` bound and no env
    override — i.e. the value the module would compute for that
    threshold. ``os.environ.get`` is stubbed to return its default."""
    expr = _shipped_hard_cap_default()
    fake_os = SimpleNamespace(
        environ=_AlwaysDefault(),
    )
    scope = {
        "os": fake_os,
        "VERIFICATION_WATCHDOG_STALENESS_SECONDS": threshold,
        "float": float, "str": str, "max": max,
    }
    return float(eval(expr, scope))  # noqa: S307 - module's own expression


def test_hard_cap_stays_above_threshold_for_every_threshold():
    """The invariant the derived default exists to protect: cap > threshold,
    or the "stale hold" branch is dead code and every trip is terminal.

    The default is a FLOOR on the derived figure, so this has to hold for
    thresholds other than the default one — an absolute 3600 could not
    promise it (threshold 3600 would put them level, which is exactly the
    first-deploy bug)."""
    for threshold in (0.0, 300.0, 900.0, 1800.0, 3599.0,
                      3600.0, 3601.0, 7200.0, 86400.0):
        cap = _eval_shipped_hard_cap(threshold)
        assert cap > threshold, (
            f"threshold={threshold} cap={cap} would make the hold branch "
            "dead code"
        )
        assert cap >= 3600.0, (
            f"threshold={threshold} cap={cap} would drop the ceiling "
            "below the contracted hour"
        )


# ---------------------------------------------------------------------------
# the quota window wait
# ---------------------------------------------------------------------------

def test_quota_window_wait_is_not_judged_log_stale(plan_env):
    """The incident, as a test.

    The round log has been silent for 70 min — past the 1h hard ceiling,
    so nothing about the ceiling could save this round. The only thing
    writing anything is ``execution.log``, refreshed every 300s by the
    quota-wait heartbeat. The watchdog must not terminalise it.
    """
    _write_round_log(plan_env, mtime_age=4200.0)   # 70 min: past the cap
    _write_execution_log(plan_env, mtime_age=60.0)  # 1 min: alive

    persist = _run_lazy_check()

    persist.assert_not_called(), (
        "a round parked on a closed quota window writes execution.log "
        "every 300s; that is progress evidence and must not be stamped "
        "verification_log_stale"
    )
    persist._cleanup.assert_not_called()  # type: ignore[attr-defined]


def test_execution_log_is_load_bearing_for_that_verdict(plan_env):
    """Same setup, ``execution.log`` removed — now the round IS dead.

    Without this the test above could be passing for the wrong reason
    (e.g. the hold branch, or a threshold that never tripped), and the
    ``execution.log`` glob could be doing nothing at all.
    """
    _write_round_log(plan_env, mtime_age=4200.0)

    persist = _run_lazy_check()

    persist.assert_called_once()
    assert persist.call_args[0][2] == "verification_log_stale", (
        "with nothing but a silent round log past the hard ceiling this "
        "round is genuinely dead and must still be terminalised"
    )


def test_quota_wait_also_survives_the_old_45_minute_ceiling(plan_env):
    """Pinned at the moment the incident was actually killed: a round log
    silent for exactly the old 45-min ceiling, with the quota heartbeat
    the only live signal. It must not be stamped."""
    _write_round_log(plan_env, mtime_age=2700.0)   # 45 min: the old cap
    _write_execution_log(plan_env, mtime_age=30.0)

    persist = _run_lazy_check()

    persist.assert_not_called()


# ---------------------------------------------------------------------------
# the watchdog is not simply switched off
# ---------------------------------------------------------------------------

def test_truly_dead_round_is_still_caught_past_one_hour(plan_env):
    """Every file silent past the hard ceiling: nothing is writing, so
    the wait exemption cannot be what is carrying this round."""
    _write_round_log(plan_env, mtime_age=4200.0)
    _write_execution_log(plan_env, mtime_age=4200.0)

    persist = _run_lazy_check()

    persist.assert_called_once()
    assert persist.call_args[0][1] == "failed"
    assert persist.call_args[0][2] == "verification_log_stale"


def test_silent_round_below_one_hour_is_held_not_flagged(plan_env):
    """Nothing written for 20 min — past the 15-min threshold, inside the
    hour. The hold branch must absorb it: this pins the ceiling AT an
    hour, so a future edit that quietly moves it fails here."""
    _write_round_log(plan_env, mtime_age=1200.0)
    _write_execution_log(plan_env, mtime_age=1200.0)

    persist = _run_lazy_check()

    persist.assert_not_called(), (
        "inside the hard ceiling the patience branch must still hold"
    )
