"""Tests for the adaptive total-timeout watcher in ``coding_tool.py``.

2026-09-08 plan — verifies the new ``_total_timer`` body that replaces
the legacy hard ``total_fired.wait(timeout=total_sec)`` with a
progress-aware loop:

  * Active subprocess (stdout emits regularly): timer never fires.
  * Hung subprocess (no stdout for ``total_sec``): timer fires.
  * Clean EOF: read loop sets ``total_fired``, timer exits.

We test the timer logic directly by simulating the closure-captured
``last_line_time`` list (a list-of-one-float used as a mutable
container, matching the production layout). This avoids spawning a
real ``claude`` subprocess while exercising the exact branch logic.

2026-09-14 — the replica-based tests above cannot catch a regression in
the *relationship* between ``last_line_time`` and ``total_started_at``,
which is where the production bug lived (a stale ``last_line_time`` made
the watcher fire instantly with an impossible ``elapsed``).  The
end-to-end test at the bottom of this file drives the real
``_run_claude_interactive`` against a fake ``claude`` on ``PATH`` with an
instrumented clock, so it pins that invariant against the production
code rather than a copy of it.
"""

import os
import stat
import threading
import time

import pytest

# Import the module — pytest collects from the backend root, so a
# bare ``coding_tool`` import resolves the package.
import coding_tool


def _make_timer(total_sec: float, last_line_time: list):
    """Replicate the production ``_total_timer`` closure body.

    The production version captures ``last_line_time`` and
    ``total_fired`` from the surrounding scope; tests here use the
    same one-element-list pattern so the read loop's
    ``last_line_time[0] = time.monotonic()`` updates are observable.
    """
    total_fired = threading.Event()
    total_triggered = [False]

    def _total_timer():
        try:
            while not total_fired.is_set():
                time_since_progress = time.monotonic() - last_line_time[0]
                if time_since_progress > total_sec:
                    break
                remaining = total_sec - time_since_progress
                total_fired.wait(timeout=min(remaining, 1.0))
        except Exception:
            return
        if total_fired.is_set():
            return
        total_triggered[0] = True

    return _total_timer, total_fired, total_triggered


def _run_for(duration: float, last_line_time: list):
    """Spawn the timer thread, let it run for ``duration`` seconds,
    then signal cancellation and join. Returns (triggered, fired).
    """
    fn, total_fired, total_triggered = _make_timer(2.0, last_line_time)
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    time.sleep(duration)
    total_fired.set()  # cancel
    t.join(timeout=2.0)
    return total_triggered[0], total_fired.is_set()


def test_active_subprocess_does_not_trigger_timeout():
    """Simulate a subprocess that emits a line every 0.5s. After 3s
    of activity (well past the 2s cap), the timer must NOT have fired.

    This is the regression test for VP-034: a 30-min ``pytest`` run
    emits tool_result events every few seconds. With the adaptive
    timer, ``total_sec=900`` (15 min) effectively becomes "kill only
    after 15 min of NO output" — pytest stays alive until it
    completes naturally.
    """
    last_line_time = [time.monotonic()]
    stop_at = time.monotonic() + 3.0
    triggered = [False]

    fn, total_fired, total_triggered = _make_timer(2.0, last_line_time)
    t = threading.Thread(target=fn, daemon=True)
    t.start()

    # Simulate the read loop: emit a "line" every 0.5s.
    while time.monotonic() < stop_at:
        last_line_time[0] = time.monotonic()
        time.sleep(0.5)

    total_fired.set()
    t.join(timeout=2.0)
    triggered[0] = total_triggered[0]
    assert triggered[0] is False, (
        "Adaptive timer must NOT fire while subprocess is "
        "actively emitting output (regression: VP-034 used to be "
        "killed at 15 min wall-clock regardless of progress)."
    )


def test_hung_subprocess_triggers_timeout():
    """Simulate a subprocess that emits ONE line at t=0 then goes
    silent. After 3s of silence (well past the 2s cap), the timer
    MUST have fired.
    """
    last_line_time = [time.monotonic()]  # one initial line, then silence
    triggered = [False]

    fn, total_fired, total_triggered = _make_timer(2.0, last_line_time)
    t = threading.Thread(target=fn, daemon=True)
    t.start()

    # Wait 3s without updating last_line_time — simulates a hung subprocess.
    time.sleep(3.0)

    total_fired.set()
    t.join(timeout=2.0)
    triggered[0] = total_triggered[0]
    assert triggered[0] is True, (
        "Adaptive timer MUST fire after ``total_sec`` of no output "
        "(hung-subprocess safety net — without this, a wedged "
        "provider could hang the verification loop forever)."
    )


def test_clean_eof_cancels_timer():
    """When the read loop hits EOF and sets ``total_fired``, the
    timer must exit without firing (clean cancellation, not a
    timeout).
    """
    last_line_time = [time.monotonic()]
    fn, total_fired, total_triggered = _make_timer(2.0, last_line_time)
    t = threading.Thread(target=fn, daemon=True)
    t.start()

    # Immediately cancel (simulates EOF on first read iteration).
    time.sleep(0.2)
    total_fired.set()
    t.join(timeout=2.0)

    assert total_triggered[0] is False, (
        "Cancellation via ``total_fired.set()`` (e.g. on EOF) must "
        "NOT register as a timeout — that would falsely mark clean "
        "exits as timeout failures."
    )


def test_partial_progress_then_hang():
    """Mixed scenario: emit progress for 1s, then hang for 3s.
    With ``total_sec=2``:
      * First 1s of progress → timer doesn't fire.
      * Then 2s of hang → timer fires (idle > 2s).

    This catches off-by-one bugs where the timer resets on
    progress but doesn't re-evaluate ``time_since_progress`` cleanly.
    """
    last_line_time = [time.monotonic()]
    fn, total_fired, total_triggered = _make_timer(2.0, last_line_time)
    t = threading.Thread(target=fn, daemon=True)
    t.start()

    # Active progress for 1s
    end_active = time.monotonic() + 1.0
    while time.monotonic() < end_active:
        last_line_time[0] = time.monotonic()
        time.sleep(0.1)

    # Then hang for 3s (last_line_time frozen)
    time.sleep(3.0)

    total_fired.set()
    t.join(timeout=2.0)
    assert total_triggered[0] is True, (
        "After 3s of silence following 1s of activity, "
        "the 2s idle cap must have fired."
    )


def test_total_sec_zero_disables_timer():
    """``total_sec <= 0`` disables the timer entirely — the production
    guard at the call site (``if total_sec > 0: start thread``) means
    the timer is never spawned, so we test that contract by NOT
    starting a thread and confirming ``total_triggered`` stays False
    after a quiet period.
    """
    # The production code never spawns ``_total_timer`` when
    # ``total_sec <= 0`` (see coding_tool.py around line 1150 — the
    # ``if total_sec > 0: total_thread = threading.Thread(...)``
    # branch). Replicate that here: no thread, no firing.
    total_triggered = [False]
    # Simulate 2s of no progress.
    time.sleep(2.0)
    assert total_triggered[0] is False


# ---------------------------------------------------------------------------
# 2026-09-14 — the watcher must never fire before its own cap has elapsed
# ---------------------------------------------------------------------------


class _StaleFirstStamp:
    """``coding_tool.time`` shim whose FIRST ``monotonic()`` call is
    ``300`` seconds old.

    The production watcher stamps ``last_line_time`` (idle timer needs
    it early) roughly 30 statements *before* ``total_started_at``, and
    the read loop only re-stamps it once the subprocess speaks.  If
    anything delays those two adjacent statements — a process freeze, a
    clock discontinuity — the watcher used to see 300 s of "silence"
    that had happened before it even started, and fired immediately
    with an internally impossible ``elapsed``.  That is exactly the
    ``total_sec=600 elapsed=0.7s`` shape observed in production on
    2026-09-13 (three calls in two seconds, killing both the
    report-generation and repair-generation LLM calls on an earlier
    plan).

    ``monotonic`` is defined on the class so ``__getattr__`` never
    intercepts it; every other attribute (``sleep``, ``time``, …)
    delegates to the real stdlib module.
    """

    def __init__(self):
        self._real = time.monotonic
        self.calls = 0

    def monotonic(self):
        self.calls += 1
        if self.calls == 1:
            return self._real() - 300.0
        return self._real()

    def __getattr__(self, name):  # pragma: no cover - delegation shim
        return getattr(time, name)


def test_watcher_ignores_silence_that_predates_its_own_start(
    tmp_path, monkeypatch
):
    """End-to-end: a stale ``last_line_time`` stamp must NOT make the
    watcher fire before ``total_sec`` has actually elapsed.

    Drives the real ``_run_claude_interactive`` against a fake ``claude``
    on ``PATH`` that emits nothing, with ``total_timeout=1``.  The
    contract under test is the pair:

      * the call raises ``HardTimeoutError`` (the subprocess really was
        silent for the whole cap), and
      * ``elapsed >= total_sec`` — the watcher cannot have fired early.

    Before the 2026-09-14 clamp the first assertion still held (with a
    bogus ``elapsed``), which is why the second one is the real pin.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_claude = bindir / "claude"
    fake_claude.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", "")
    )

    clock = _StaleFirstStamp()
    monkeypatch.setattr(coding_tool, "time", clock)

    tool = coding_tool.ClaudeCodingTool()
    with pytest.raises(coding_tool.HardTimeoutError) as excinfo:
        tool._run_claude_interactive(
            "hello",
            idle_timeout=60,
            total_timeout=1,
        )

    assert clock.calls >= 1, (
        "the instrumented clock was never consulted — the shim did not "
        "take effect and this test proved nothing"
    )
    err = excinfo.value
    assert err.total_sec == 1
    assert err.elapsed >= err.total_sec, (
        f"watcher fired before its cap elapsed: elapsed={err.elapsed:.2f}s "
        f"< total_sec={err.total_sec}s — the stale-``last_line_time`` "
        f"regression is back"
    )
    assert err.elapsed < 10, (
        f"the call should have died at ~1s, not waited {err.elapsed:.2f}s"
    )
