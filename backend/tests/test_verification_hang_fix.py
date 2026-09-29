"""Regression tests for the 2026-09-07 verification hang fix.

Two bugs, two layers:

1. **Event-loop freeze** — ``VerificationSubAgent._execute_attempt``
   awaited the *synchronous* ``query_json`` directly, freezing the
   whole asyncio event loop for the duration of the LLM call. Every
   other VP's ``asyncio.wait_for`` timeout, the parallel scheduler and
   the watchdog kill path were dead while one VP's call ran (observed
   2026-09-07 16:11: VP-034 froze round 2; its 120 s timeout never
   fired; the remaining VPs starved for 15 min until the staleness
   watchdog force-terminated the plan).

   Fix: the blocking call now runs in a worker thread via
   ``asyncio.to_thread``, and the per-VP timeout is forwarded into
   ``query_json`` so the inner watcher SIGTERMs the subprocess at the
   same deadline.

2. **Stale card timestamps** — ``server._read_latest_vp_start`` sorted
   round logs *ascending* and returned the first match (the OLDEST
   round), so the Feishu card showed "current VP started at" from a
   previous day. A descending round-number sort is still wrong because
   the round counter resets on restart; mtime is the only monotonic
   signal. The sibling ``_read_all_vp_starts`` had the same
   round-keyed non-monotonicity.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

# 2026-09-08: outer 1-hour cap now raises HardTimeoutError for
# auto-split routing. Tests below catch it (in addition to asserting
# the original FAILED-verdict behaviour as a fallback).
from coding_tool import HardTimeoutError

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_subagent import VerificationSubAgent, StuckAgentError


# ---------------------------------------------------------------------------
# 1) Event-loop no longer freezes during a slow query_json
# ---------------------------------------------------------------------------


class _SlowSyncTool:
    """Mimics ClaudeCodingTool: query_json is SYNC and slow (3 s)."""

    def __init__(self, delay: float = 3.0):
        self.delay = delay
        self.received_timeout = None

    def query_json(self, prompt=None, system_instruction=None, timeout=None, **kw):
        self.received_timeout = timeout
        time.sleep(self.delay)
        return {"verdict": "PASSED", "reasons": [], "evidence": []}


class _MinimalSubAgent(VerificationSubAgent):
    """Bare-bones agent exposing _execute_attempt without LLM plumbing."""

    def __init__(self):
        self.max_retries = 0
        self.plan_id = "hang-test"
        self.registry = None
        self.model_complexity = "test"
        self._watchdog_retry_used = False
        self.method = "automated_test"
        self.template = ""
        # 2026-09-08: sub-agent now emits ``[HARD TIMEOUT]`` log
        # markers via ``self.logger``. Tests use a no-op logger.
        import logging
        self.logger = logging.getLogger("hang-test")
        # Shrink the 1-hour hard cap so the outer-cap test fires in
        # seconds, not in an hour. With cap=2 the flat outer cap is
        # 2s — fires well before the 4s hung query_json returns.
        # (2026-09-13: flat cap, no QUERY_ABANDON_GRACE_SECONDS.)
        self.HARD_WALL_CLOCK_CAP_SECONDS = 2

    def _build_prompt(self, vp_node, attempt):
        return "prompt"

    def _write_log(self, log_path, event, payload):
        pass


@pytest.mark.asyncio
async def test_slow_query_json_does_not_freeze_event_loop(tmp_path):
    """While one VP's sync LLM call runs in a worker thread, another
    coroutine on the loop must keep making progress (the loop is not
    frozen). Before the fix this test would hang: the sleep blocked
    the loop so the second coroutine never ran until the first
    finished."""
    sub = _MinimalSubAgent()
    slow_tool = _SlowSyncTool(delay=3.0)
    vp_node = {"id": "VP-T", "title": "test", "timeout_seconds": 30}

    progress_ticks = []

    async def heartbeat():
        for _ in range(10):
            progress_ticks.append(time.monotonic())
            await asyncio.sleep(0.1)

    hb = asyncio.create_task(heartbeat())
    # 2026-09-08: outer cap (now 2s in _MinimalSubAgent) fires
    # before the 3s slow tool returns. The await raises StuckAgentError
    # (outer cap → summarizer) — accept it; a Verdict(FAILED) fallback
    # is also tolerated.
    try:
        verdict = await sub._execute_attempt(
            vp_node=vp_node,
            coding_tool=slow_tool,
            settings_path="/tmp/fake_settings.json",
            log_path=tmp_path / "attempt.log",
            attempt=0,
            project_dir=tmp_path,
        )
        assert verdict.verdict == "PASSED"
    except StuckAgentError:
        # Expected when the outer cap fires before the slow tool returns.
        pass
    except HardTimeoutError:
        # Legacy routing fallback also acceptable.
        pass
    await hb
    # The heartbeat coroutine must have ticked DURING the 3 s call
    # (i.e. before the call finished), proving the loop stayed live.
    assert len(progress_ticks) >= 5, (
        "event loop froze during the sync query_json call — the "
        "asyncio.to_thread bridge is missing or broken"
    )


@pytest.mark.asyncio
async def test_query_json_receives_no_per_vp_timeout(tmp_path):
    """2026-09-13: per-VP timeout interface deleted — query_json
    must be called with ``timeout=None`` so the inner 15-min idle
    detector (``coding_tool.DEFAULT_TOTAL_TIMEOUT=900``) applies.
    A legacy plan's ``timeout_seconds`` value must NOT be forwarded."""
    sub = _MinimalSubAgent()
    slow_tool = _SlowSyncTool(delay=0.2)
    vp_node = {"id": "VP-T", "title": "test", "timeout_seconds": 1800}

    await sub._execute_attempt(
        vp_node=vp_node,
        coding_tool=slow_tool,
        settings_path="/tmp/fake_settings.json",
        log_path=tmp_path / "attempt.log",
        attempt=0,
        project_dir=tmp_path,
    )
    assert slow_tool.received_timeout is None


@pytest.mark.asyncio
async def test_outer_timeout_beats_hung_query_json(tmp_path):
    """A query_json that ignores its timeout (simulating the observed
    hang: subprocess killed but pipe never EOF'd) must be abandoned by
    ``_execute_attempt``'s inner ``wait_for(vp_timeout + 30)`` — the
    await must raise within that grace window, and the loop must stay
    usable afterwards.

    2026-09-08 plan update: outer 1-hour wall-clock cap now raises
    :class:`coding_tool.HardTimeoutError` (not a plain ``TimeoutError``
    that gets wrapped into a FAILED verdict). The caller routes this
    to auto-split / auto-refine. We assert the await raises within
    the grace window (the original observable behaviour: the VP
    finishes promptly instead of hanging the round) but allow either
    a HardTimeoutError or a Verdict(FAILED) — the older FAILED
    behaviour is still permitted as a fallback if a future refactor
    splits the two paths.

    NOTE: the abandoned worker thread (non-daemon under
    ``asyncio.to_thread``) keeps sleeping in the background; in the
    long-lived server process that is harmless (the inner total-timeout
    watcher SIGTERMs the real subprocess), but the *test* interpreter
    would wait on it at exit. We therefore run the assertion first and
    never rely on a clean interpreter shutdown — pytest-asyncio tears
    the loop down while the stray thread is parked.
    """
    from coding_tool import HardTimeoutError
    sub = _MinimalSubAgent()

    class _HungTool:
        def query_json(self, *a, **kw):
            time.sleep(4)  # bounded but far beyond the 1s VP timeout
            return {"verdict": "PASSED", "reasons": [], "evidence": []}

    vp_node = {"id": "VP-T", "title": "test", "timeout_seconds": 1}
    start = time.monotonic()
    try:
        verdict = await sub._execute_attempt(
            vp_node=vp_node,
            coding_tool=_HungTool(),
            settings_path="/tmp/fake_settings.json",
            log_path=tmp_path / "attempt.log",
            attempt=0,
            project_dir=tmp_path,
        )
        # Older behaviour: outer cap wraps to FAILED verdict. Permitted
        # as a fallback for any future refactor that splits the two paths.
        assert verdict.verdict == "FAILED", f"got {verdict.verdict!r}"
    except StuckAgentError:
        # 2026-09-13: the flat outer cap fires → StuckAgentError
        # routes to the summarizer fallback. Expected here.
        pass
    except HardTimeoutError:
        # Legacy routing fallback also acceptable.
        pass
    elapsed = time.monotonic() - start
    # The flat outer cap (2s in _MinimalSubAgent) must abandon the
    # hung call promptly (plus a few seconds of slack for the asyncio
    # machinery and the summarizer fallback path).
    assert elapsed < 2 + 5, (
        f"_execute_attempt took {elapsed:.1f}s to abandon a hung call — "
        "the flat outer cap is broken"
    )
    # The loop must still be live after the abandon.
    t1 = time.monotonic()
    await asyncio.sleep(0.1)
    assert time.monotonic() - t1 < 1


# ---------------------------------------------------------------------------
# 2) _read_latest_vp_start picks the mtime-newest log
# ---------------------------------------------------------------------------


def _write_vp_start(path: Path, vp_id: str, ts: str) -> None:
    entry = {
        "verification_point_id": vp_id,
        "event_type": "vp_start",
        "timestamp": ts,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def test_latest_vp_start_prefers_mtime_newest_log(tmp_path):
    """A stale round-3 file from YESTERDAY must not outrank a fresh
    round-2 file from TODAY (round counter resets on restart)."""
    from server import _read_latest_vp_start

    logs = tmp_path / "logs"
    logs.mkdir()
    old_round3 = logs / "verification_3_20260906_101050.log"
    new_round2 = logs / "verification_2_20260907_161038.log"
    _write_vp_start(old_round3, "VP-034", "2026-09-06T10:11:19")
    _write_vp_start(new_round2, "VP-034", "2026-09-07T16:11:01")
    # Make the mtimes match reality: old file written yesterday.
    old_ts = time.time() - 86400
    os.utime(old_round3, (old_ts, old_ts))

    got = _read_latest_vp_start(tmp_path, "VP-034")
    assert got == "2026-09-07T16:11:01", (
        f"got {got!r} — the round-keyed sort resurfaced a stale "
        "yesterday timestamp; expected the mtime-newest log's entry"
    )


def test_read_all_vp_starts_latest_file_wins(tmp_path):
    """Sibling scanner: the newest file's vp_start must win the
    overwrite, not the highest round number."""
    from server import _read_all_vp_starts

    logs = tmp_path / "logs"
    logs.mkdir()
    old_round3 = logs / "verification_3_20260906_101050.log"
    new_round2 = logs / "verification_2_20260907_161038.log"
    _write_vp_start(old_round3, "VP-034", "2026-09-06T10:11:19")
    _write_vp_start(new_round2, "VP-034", "2026-09-07T16:11:01")
    old_ts = time.time() - 86400
    os.utime(old_round3, (old_ts, old_ts))

    got = _read_all_vp_starts(tmp_path)
    assert got.get("VP-034") == "2026-09-07T16:11:01", (
        f"got {got!r} — highest-round key beat the newest mtime"
    )
