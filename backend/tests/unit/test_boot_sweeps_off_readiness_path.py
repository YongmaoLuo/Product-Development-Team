"""The boot sweeps must not sit on the readiness path.

Booting the backend used to run two housekeeping sweeps inline in the
FastAPI lifespan:

  * the **ledger sweep** — stop the services a plan declared and can
    still identify, left behind by a backend that was killed mid-round;
  * the **credential-residue sweep** — redact the settings payloads
    that still hold a live credential, left behind by a dispatch that
    died before its own cleanup.

The credential sweep shells out to ``ps`` and waits for it, and on a
machine with many processes that took 18-21 s.  uvicorn binds the
socket only *after* the lifespan returns, so every one of those
seconds was a second in which the API did not answer — a test server
that boots the app simply timed out.

Both sweeps are housekeeping: they clean up after a *previous* life of
the backend, and the next boot finishes whatever this one did not get
to.  Nothing about serving an API depends on them, so they run on a
daemon thread and the boot does not wait for them.

Four properties are pinned here:

  1. **The boot does not wait for them.**  Two slow sweeps must not
     delay the lifespan by anything like their own runtime.
  2. **They still run — each exactly once.**  Moving them off the
     readiness path must not turn into "not running at all".
  3. **The thread is a named daemon.**  A daemon thread cannot keep
     the interpreter alive at shutdown, and the name says what the
     thread is in a thread dump.
  4. **The machine-wide rewrite stays switchable off.**
     ``PDT_DISABLE_TEMP_SWEEP=1`` makes the sweep a no-op, so a test
     suite can never rewrite the developer's own credentials.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

import notifications.feishu_notifier as feishu_notifier_module
import server as server_mod


class _FakeFeishuNotifier:
    """A stand-in for the Feishu notifier.

    The real notifier's client pulls the whole Feishu SDK in on
    ``start()`` and then asks the OS keychain for the secret — tens of
    seconds on a loaded machine, and all of it on the boot path the
    measurement below is about.  The notifier has nothing to do with
    the boot sweeps, so it is replaced rather than waited for.
    """

    _min_interval_seconds = 60.0

    def start(self) -> None:
        """There is no worker to start."""

    def stop(self, timeout: float = 5.0) -> None:
        """There is no worker to stop."""


def _enter_lifespan(monkeypatch: pytest.MonkeyPatch) -> threading.Thread:
    """Run the lifespan to completion; return the sweep thread it started.

    The recovery helpers and the heartbeat are stubbed so the lifespan
    body does not depend on the live state database; what is under test
    is the boot path, not the recovery semantics.

    The thread is captured rather than merely started, because a test
    that leaves a sweep thread behind is a test that leaves a
    background sweep of this machine behind.
    """
    monkeypatch.setattr(server_mod, "_recover_execution_states", lambda plans_dir: None)
    monkeypatch.setattr(server_mod, "_recover_verification_states", lambda plans_dir: None)
    monkeypatch.setattr(server_mod, "load_providers", lambda: [])
    monkeypatch.setattr(server_mod, "heartbeat_monitor", MagicMock())
    monkeypatch.setattr(
        feishu_notifier_module, "FeishuNotifier", _FakeFeishuNotifier
    )

    # The shutdown path sweeps the same ledger the boot sweep does (with
    # ``reason="server_shutdown"``), and it does so synchronously on the
    # way out of the lifespan.  It is a different sweep — the contract
    # under test is the *boot* one — and letting it run here would both
    # put its seconds on the measurement and count as a second run of
    # the ledger sweep.
    monkeypatch.setattr(server_mod, "_shutdown_all_executions", lambda: None)

    started: list[threading.Thread] = []
    original_start = server_mod._start_boot_sweeps

    def _capture() -> threading.Thread:
        thread = original_start()
        started.append(thread)
        return thread

    monkeypatch.setattr(server_mod, "_start_boot_sweeps", _capture)

    fake_app = MagicMock()
    fake_app.state = MagicMock()

    async def _enter() -> None:
        async with server_mod._lifespan(fake_app):
            pass

    asyncio.run(_enter())

    assert len(started) == 1, (
        f"the lifespan must start the boot sweeps exactly once, "
        f"started {len(started)}"
    )
    return started[0]


def test_startup_does_not_wait_for_slow_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow sweep must not delay the boot.

    Both stand-ins sleep for three seconds — twice the bound the boot
    has to meet — and the lifespan still returns well inside it.  That
    is the whole point of the change: the readiness path cannot depend
    on how long a housekeeping sweep takes.
    """
    def _slow_reap(*args, **kwargs):
        time.sleep(3)
        return []

    def _slow_sweep(apply=True):
        time.sleep(3)
        return ([], 0, [])

    monkeypatch.setattr(server_mod, "_reap_all_plans", _slow_reap)
    monkeypatch.setattr(server_mod, "_sweep_credential_residue", _slow_sweep)

    began = time.monotonic()
    thread = _enter_lifespan(monkeypatch)
    elapsed = time.monotonic() - began

    try:
        assert elapsed < 1.5, (
            f"the boot waited {elapsed:.3f}s for the sweeps — the "
            f"lifespan must not block on housekeeping"
        )
        assert thread.is_alive(), (
            "the sweep thread had already finished when the boot "
            "returned — the boot cannot have waited for it"
        )
    finally:
        thread.join(timeout=10)
        assert not thread.is_alive(), (
            "the sweep thread outlived the test — it must be joined"
        )


def test_boot_sweeps_run_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off the readiness path, but not off the run.

    Each sweep is counted, and the counts are waited for with an event
    rather than with a sleep: both sweeps have to have run within ten
    seconds, and each of them exactly once.  A sweep that ran twice
    would reap twice, and a sweep that never ran would leave the
    residue of the previous life on the machine forever.
    """
    counts = {"reap": 0, "sweep": 0}
    done = threading.Event()

    def _mark() -> None:
        if counts["reap"] and counts["sweep"]:
            done.set()

    def _reap(*args, **kwargs):
        counts["reap"] += 1
        _mark()
        return []

    def _sweep(apply=True):
        counts["sweep"] += 1
        _mark()
        return ([], 0, [])

    monkeypatch.setattr(server_mod, "_reap_all_plans", _reap)
    monkeypatch.setattr(server_mod, "_sweep_credential_residue", _sweep)

    thread = _enter_lifespan(monkeypatch)
    try:
        assert done.wait(timeout=10), (
            f"the boot sweeps did not both run within 10s: {counts}"
        )
        thread.join(timeout=10)
        assert not thread.is_alive(), (
            "the sweep thread outlived the test — it must be joined"
        )
        assert counts == {"reap": 1, "sweep": 1}, (
            f"each boot sweep must run exactly once, got {counts}"
        )
    finally:
        thread.join(timeout=10)


def test_boot_sweeps_thread_is_daemon_named_boot_sweeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The thread is a named daemon.

    A daemon thread cannot keep the interpreter alive at shutdown — the
    backend must not hang on a sweep that is still running when it is
    asked to stop — and the name says what the thread is in a thread
    dump.
    """
    monkeypatch.setattr(server_mod, "_reap_all_plans", lambda *a, **k: [])
    monkeypatch.setattr(
        server_mod, "_sweep_credential_residue", lambda apply=True: ([], 0, 0)
    )

    thread = server_mod._start_boot_sweeps()

    assert thread.name == "boot-sweeps", (
        f"the sweep thread is called {thread.name!r}, not 'boot-sweeps'"
    )
    assert thread.daemon is True, (
        "the sweep thread must be a daemon — a non-daemon thread would "
        "keep the interpreter alive at shutdown"
    )

    thread.join(timeout=10)
    assert not thread.is_alive(), (
        "the sweep thread outlived its work — it must be joined"
    )


def test_sweep_default_roots_is_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PDT_DISABLE_TEMP_SWEEP=1`` makes the sweep touch nothing.

    The sweep is a machine-wide rewrite of the temp roots, and the
    boot-time moment is the only moment nothing of ours can
    legitimately be running.  A test suite that booted the app must not
    perform that rewrite, so the switch exists — and it has to return
    the same shape as a real run, so the caller does not have to care
    whether it did anything.
    """
    from utils.secret_sweep import sweep_default_roots

    monkeypatch.setenv("PDT_DISABLE_TEMP_SWEEP", "1")

    findings, failures, pruned = sweep_default_roots()

    assert (findings, failures, pruned) == ([], 0, []), (
        "a disabled sweep must report that it found nothing, failed "
        "nothing and pruned nothing"
    )
