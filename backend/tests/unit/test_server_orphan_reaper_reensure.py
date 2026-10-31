"""The backend puts the orphan reaper's daemon back, periodically.

The reaper's daemon is a **separate, detached process** (see
``backend/orphan_reaper.py``): it has to outlive the backend, because
the orphans it reclaims are the ones that outlived the backend.

Two things follow from that, and together they are the whole reason
this file exists:

  1. The daemon **stops by itself** once the box is idle — candidates
     and owners both at zero, nothing left to reclaim — and it stops
     itself again when its own lifetime ceiling is reached.
  2. The backend used to start the daemon **exactly once**, at boot.
     So the moment the daemon decided it was done, the machine had a
     resident reaper that was no longer running, and nothing would
     start it again until the next restart of the backend.

Between those two moments nothing reclaims anything: an executor that
outlived the plan that spawned it keeps its memory, its port, its
subprocess tree.  The daemon is not a service that stays up — it is a
service that has to be **asked** again.

So the backend asks again, on a timer.  The contract pinned here is
narrow and entirely about the asking:

  * **The asking is periodic and idempotent.**  Every round asks
    ``ensure_daemon``, which itself answers "a daemon is already
    running" by doing nothing.  Asking twice costs nothing.
  * **The asking survives a failure.**  A round that raises is logged
    and the next round still happens — a reaper that cannot be started
    must not cost the backend its thread.
  * **The asking is off when the reaper is off.**  A deployment that
    switched the reaper off gets no thread, and a test process — which
    must never reap the developer's own processes — gets none either.
  * **The thread ends when it is told to.**  It is joined, not left
    running behind a shutdown.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

import server as server_mod


class _Config:
    """The one field of :class:`ReaperConfig` the re-ensure reads."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled


def _run_loop(
    config: _Config,
    ensure_fn,
    *,
    interval: float = 0.05,
    run_for: float = 0.25,
    want: int = 2,
) -> list:
    """Run the re-ensure loop on a thread and stop it after enough rounds.

    The thread is joined and its liveness asserted, so a test that
    passes cannot be passing on a thread that never stopped.

    The case **waits for the rounds it needs** rather than assuming the
    clock was kept: ``threading.Event.wait`` overshoots its timeout on
    some platforms (macOS rounds a timed wait up by a fraction of a
    second), so a fixed sleep would make the case flaky — passing on a
    fast machine and failing on a slow one, for a reason that has
    nothing to do with the reaper.
    """
    stop = threading.Event()
    calls: list = []

    def _ensure(cfg):
        calls.append(cfg)
        ensure_fn(cfg)

    thread = threading.Thread(
        target=server_mod._orphan_reaper_reensure_loop,
        args=(config, stop, interval),
        kwargs={"ensure_fn": _ensure},
        name="orphan-reaper-reensure-test",
        daemon=True,
    )
    thread.start()
    time.sleep(run_for)

    deadline = time.monotonic() + 30
    while len(calls) < want and time.monotonic() < deadline:
        time.sleep(0.02)

    stop.set()
    thread.join(5)

    assert not thread.is_alive(), (
        "the re-ensure thread outlived its stop event — it has to be "
        "joinable, or a shutdown leaves a thread behind"
    )
    return calls


# --------------------------------------------------------------------------
# The loop asks, again and again
# --------------------------------------------------------------------------


def test_the_reensure_loop_calls_ensure_repeatedly():
    """The loop asks the ensure function over and over, with one config.

    The interval is a test-sized one (50 ms), and the case waits for
    the rounds it needs rather than for a fixed span of time (see
    ``_run_loop``).  What is pinned is the *shape* of the loop: it does
    not run once and stop, and every round is handed the **same**
    configuration object — the config the boot read, not a fresh one
    that could disagree with it.
    """
    config = _Config(enabled=True)

    calls = _run_loop(config, lambda cfg: None)

    assert len(calls) >= 2, (
        f"the loop asked {len(calls)} time(s) — it has to keep asking, "
        "not ask once"
    )
    assert all(cfg is config for cfg in calls), (
        "every round must be handed the same configuration object"
    )


def test_the_loop_survives_an_ensure_that_raises(caplog):
    """A round that raises costs the round and nothing else.

    The thread is a background guarantee, not a one-shot action: if a
    single failed round took the thread down with it, the orphans would
    go uncollected from that moment on — the failure would be permanent
    and silent.  So the exception is caught, logged, and the next round
    still happens.
    """
    config = _Config(enabled=True)

    def boom(_cfg):
        raise RuntimeError("the daemon cannot be started")

    with caplog.at_level(logging.WARNING):
        calls = _run_loop(config, boom)

    assert len(calls) >= 2, (
        f"the loop asked {len(calls)} time(s) — a round that raises must "
        "not stop the rounds after it"
    )
    assert any("orphan_reaper" in record.message for record in caplog.records), (
        "a failed round must be logged — a silent failure is a failure "
        "nobody can act on"
    )


# --------------------------------------------------------------------------
# The interval is the operator's to choose
# --------------------------------------------------------------------------


def test_the_interval_comes_from_the_environment_with_a_300s_default(monkeypatch):
    """Five minutes by default; the environment may say otherwise.

    A malformed value falls back to the default rather than to nothing:
    a reaper that never runs because of a typo in a deployment's
    environment is worse than one that runs on the default schedule.
    """
    env_name = server_mod.ORPHAN_REAPER_ENSURE_INTERVAL_ENV

    monkeypatch.delenv(env_name, raising=False)
    assert server_mod._orphan_reaper_reensure_interval() == 300

    monkeypatch.setenv(env_name, "45")
    assert server_mod._orphan_reaper_reensure_interval() == 45

    monkeypatch.setenv(env_name, "abc")
    assert server_mod._orphan_reaper_reensure_interval() == 300


# --------------------------------------------------------------------------
# A reaper that is switched off gets nothing
# --------------------------------------------------------------------------


def test_nothing_is_started_when_the_reaper_is_off(monkeypatch):
    """``PDT_ORPHAN_REAPER=0`` means no thread, no asking, no nothing.

    ``_orphan_reaper_may_act`` is patched to True on purpose: the point
    of the case is the *deployment's* switch, so the test-suite switch
    must not be what stops the thread — otherwise the case would pass
    for the wrong reason.
    """
    monkeypatch.setattr(server_mod, "_orphan_reaper_may_act", lambda: True)
    monkeypatch.setenv("PDT_ORPHAN_REAPER", "0")

    started = []
    monkeypatch.setattr(
        server_mod, "_orphan_reaper_reensure_loop", lambda *a, **k: started.append(a)
    )

    assert server_mod._start_orphan_reaper_reensure() is None
    assert started == [], "the re-ensure loop must not run for a switched-off reaper"


def test_nothing_is_started_from_a_test_process(monkeypatch):
    """A test process must not start a reaper, let alone keep one.

    The suite boots the app — the lifespan tests enter ``_lifespan``
    directly — and a boot that left a detached daemon behind would be
    a test suite with side effects on the machine it runs on.
    """
    monkeypatch.setattr(server_mod, "_orphan_reaper_may_act", lambda: False)

    assert server_mod._start_orphan_reaper_reensure() is None


# --------------------------------------------------------------------------
# The asking is idempotent
# --------------------------------------------------------------------------


def test_ensure_does_not_spawn_a_second_daemon(monkeypatch, tmp_path):
    """Asking when a daemon is already running starts nothing.

    This is the property the periodic asking rests on.  If ``ensure``
    were not idempotent, the periodic asking would fill the machine
    with daemons — each one holding the lock the others need, each one
    judging the same processes.  The lock is the only source of truth
    for "is a daemon running", so the ask answers "yes" and does
    nothing.

    ``popen`` is replaced by a stub that raises: if a second daemon
    were started, the case would fail loudly instead of quietly
    leaving a stray process behind.
    """
    spawned = []

    def boom_popen(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("a second daemon must not be spawned")

    monkeypatch.setattr(
        server_mod.orphan_reaper, "daemon_alive", lambda plans_root: True
    )

    config = server_mod.orphan_reaper.ReaperConfig(plans_root=tmp_path)

    assert server_mod.orphan_reaper.ensure_daemon(config, popen=boom_popen) is None
    assert spawned == [], "the ensure must not have launched a daemon"


# --------------------------------------------------------------------------
# The thread is stoppable, and stopping it twice is fine
# --------------------------------------------------------------------------


def test_stopping_without_starting_is_a_no_op():
    """Stopping a re-ensure that was never started must not raise.

    The shutdown path calls the stop unconditionally — a lifespan that
    never started the thread still runs the teardown — so "there was
    nothing to stop" has to be an ordinary outcome, not an error.
    """
    server_mod._stop_orphan_reaper_reensure()
    server_mod._stop_orphan_reaper_reensure()


def test_stopping_the_reensure_thread_joins_it(monkeypatch):
    """The stop event ends the thread, and the thread is joined.

    A thread that is asked to stop but is still running would keep
    asking about the daemon in a world that is being torn down.  The
    stop is a request; the join is the guarantee.
    """
    monkeypatch.setattr(server_mod, "_orphan_reaper_may_act", lambda: True)
    monkeypatch.setenv("PDT_ORPHAN_REAPER", "1")
    monkeypatch.setenv(
        server_mod.ORPHAN_REAPER_ENSURE_INTERVAL_ENV, "0.05"
    )

    rounds = []
    original_loop = server_mod._orphan_reaper_reensure_loop

    def _record(config, stop_event, interval, **kwargs):
        def _ensure(_cfg):
            rounds.append(_cfg)

        return original_loop(
            config, stop_event, interval, ensure_fn=_ensure, **kwargs
        )

    monkeypatch.setattr(server_mod, "_orphan_reaper_reensure_loop", _record)

    stop = server_mod._start_orphan_reaper_reensure()
    assert stop is not None

    time.sleep(0.25)

    # Wait for the rounds rather than assuming the clock was kept — see
    # the note in ``_run_loop``.
    deadline = time.monotonic() + 30
    while len(rounds) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)

    server_mod._stop_orphan_reaper_reensure()
    # Stopping twice must not raise — the teardown may run twice.
    server_mod._stop_orphan_reaper_reensure()

    assert len(rounds) >= 2, (
        f"the started thread asked {len(rounds)} time(s) — it was not "
        "asking at all"
    )
