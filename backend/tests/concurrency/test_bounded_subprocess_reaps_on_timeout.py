"""Regression — ``run_bounded`` must reap **every** process it times out.

Why this file exists
--------------------
``backend/bounded_subprocess.py::run_bounded()`` is the contract every
task runner and every verification runner depends on to make a
``TimeoutExpired`` mean "the work is gone", not "the work was
abandoned".  The unit suite already pins the single-command case
(``tests/unit/test_bounded_subprocess.py``), but the *concurrency*
contract — the one that actually bit the CI runner — is only visible
when several timeouts race:

  1. ``test_timeout_kills_whole_process_group`` — a single timeout must
     reap **the marked grandchild**, not just the direct shell.  Killing
     ``/bin/sh`` is not the same as killing what ``sh`` spawned.
  2. ``test_concurrent_timeouts_all_reclaimed`` — eight timeouts
     simultaneously.  Each must be reaped independently; one group's
     kill must never bleed into another.
  3. ``test_no_resource_outlives_the_test`` — the thread / fd / marker
     bookkeeping the test creates must all be reclaimed by the time
     the test ends, or a later test inherits a leaked grandchild.

Marker-grandchild technique
---------------------------
The naive test ("the direct child of the shell is dead after the
timeout") passes by accident: ``start_new_session=True`` puts the
shell in its own group, ``os.killpg`` reaches it, it exits.  The real
hazard is the **grandchild** — a process the shell forked off before
the timeout fired.  The test injects ``sleep N & echo $! > <marker>;
wait`` so the shell writes the grandchild's pid to a file; the test
then checks ``os.kill(pid, 0)`` after the timeout.  If reap regresses
to killing only the shell, the grandchild stays alive and the marker
keeps resolving.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List

import pytest

# Backend root is two levels up from this file's directory.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from bounded_subprocess import (  # noqa: E402
    run_bounded,
    terminate_process_tree,
)


# ---------------------------------------------------------------------------
# Public helpers — task 19's E2E resource-reclamation assertions reuse these
# ---------------------------------------------------------------------------


def reap_probe(pid: int, *, timeout: float = 2.0) -> bool:
    """True iff ``pid`` is no longer alive within ``timeout`` seconds.

    ``os.kill(pid, 0)`` is the POSIX signal-zero idiom: it raises
    ``ProcessLookupError`` when the pid is gone and is otherwise a
    no-op.  Polling — instead of one shot — covers the (tiny) window
    between ``killpg`` returning and the kernel actually reaping the
    descendant; on macOS and Linux that gap is sub-millisecond, but the
    sleep keeps the test stable on loaded CI runners.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            return True
        time.sleep(0.01)
    return False


def _marker_pid_alive(marker_file: Path, timeout: float = 2.0) -> bool:
    """True when the pid inside ``marker_file`` is still alive.

    The file is the channel the shell uses to hand the grandchild's
    pid back to the test — by the time this function reads it the
    shell is dead and the write has been flushed (``os.replace``-style
    durability is unnecessary because the shell held the write end
    open via ``echo``).  Returns False when the file is missing or
    empty (the shell died before it could write — that is itself a
    "no leftover grandchild" signal).
    """
    if not marker_file.exists():
        return False
    try:
        raw = marker_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not raw:
        return False
    try:
        pid = int(raw.split()[0])
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.01)
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            return False
    return True


def _marker_pid(marker_file: Path) -> int | None:
    """Read the grandchild pid the shell wrote; ``None`` if absent."""
    if not marker_file.exists():
        return None
    try:
        raw = marker_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw.split()[0])
    except ValueError:
        return None


def _shell_with_marker(
    marker_file: Path, *, sleep_seconds: int = 600,
) -> str:
    """A shell that forks a grandchild and writes its pid to ``marker_file``.

    The shell is the direct child of ``run_bounded`` and therefore the
    process whose exit ``os.killpg`` is first asked to wait for.  The
    grandchild (a sleep) is what would leak if the wrapper degraded
    to ``proc.kill()`` — and what the regression we are pinning
    against actually leaks.
    """
    return (
        f"{shlex.quote(sys.executable)} -c "
        f"'import time; time.sleep({sleep_seconds})' "
        f"& echo $! > {shlex.quote(str(marker_file))}; "
        f"wait"
    )


# ---------------------------------------------------------------------------
# The four contracts this file pins
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_run_bounded_raises_timeout_expired(tmp_path: Path) -> None:
    """A timeout raises ``subprocess.TimeoutExpired`` carrying drained output.

    The contract callers rely on — ``verification_ci_runner`` and
    ``verification_evidence`` both read ``exc.output`` / ``exc.stderr``
    to attach partial output to a verdict.  A wrapper that returns a
    ``CompletedProcess`` with a ``timed_out`` flag would silently
    break that.
    """
    cmd = "echo before-timeout; sleep 600"

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        run_bounded(cmd, cwd=str(tmp_path), timeout=1.0)

    assert exc_info.value.timeout == 1.0
    captured = exc_info.value.output or ""
    assert "before-timeout" in captured, (
        f"TimeoutExpired should carry the partial stdout; got {captured!r}"
    )


@pytest.mark.timeout(60)
def test_timeout_kills_whole_process_group(tmp_path: Path) -> None:
    """A timed-out ``run_bounded`` reaps the marked grandchild.

    The marker grandchild (``python -c 'import time; time.sleep(600)'``)
    is what the test asserts on.  Killing only the shell is not
    enough: ``subprocess.run`` with a timeout does that already, and
    is precisely what ``run_bounded`` exists to *fix*.
    """
    marker = tmp_path / "grandchild.pid"
    cmd = _shell_with_marker(marker)

    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded(cmd, cwd=str(tmp_path), timeout=1.0)

    pid = _marker_pid(marker)
    assert pid is not None, (
        f"shell exited before writing grandchild pid to {marker}; "
        "the test premise is broken — investigate before re-running"
    )

    assert reap_probe(pid), (
        f"grandchild pid={pid} survived the timeout; the wrapper "
        "killed the shell but left the work it spawned running"
    )


@pytest.mark.timeout(120)
def test_concurrent_timeouts_all_reclaimed(tmp_path: Path) -> None:
    """Eight concurrent timeouts: every group reaped, no cwd residue.

    The race that this test pins: ``terminate_process_tree`` reads
    ``os.getpgid(proc.pid)`` and calls ``os.killpg(pgid, ...)``.  Under
    one timeout there is nothing for those calls to collide with.
    Under eight, two regressions become possible:

      * the wrapper degrades to a per-proc ``kill`` (only the direct
        child of *this* call dies; eight grandchildren leak);
      * the wrapper reuses a cached pgid across calls and signals
        the wrong group (one call's reap kills another call's
        grandchildren, then everything is leaked).

    Both produce a "left-behind grandchild" that this test catches
    via the marker file.  We also assert cwd is empty so a future
    regression that writes a control file into the workspace is
    caught at the same time.

    The marker files live OUTSIDE the cwd being asserted on: the shell
    is asked to write them via an absolute path so the cwd's iterdir
    is free to be checked for residue.
    """
    n_workers = 8
    # cwd: where ``run_bounded`` drops its temp files / work.  Must be
    # empty after the test passes — any file there is a leak signal.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # markers: outside cwd so the residue assertion stays meaningful.
    markers_dir = tmp_path / "markers"
    markers_dir.mkdir()
    markers: List[Path] = [markers_dir / f"gc{i}.pid" for i in range(n_workers)]
    threads: List[threading.Thread] = []
    errors: List[BaseException] = []

    def _run_one(idx: int) -> None:
        cmd = _shell_with_marker(markers[idx])
        try:
            run_bounded(cmd, cwd=str(cwd), timeout=1.0)
        except subprocess.TimeoutExpired:
            return
        except BaseException as exc:  # noqa: BLE001 - capture for the test
            errors.append(exc)

    for i in range(n_workers):
        t = threading.Thread(target=_run_one, args=(i,), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=20.0)

    assert not errors, (
        f"concurrent timeouts leaked unexpected errors: {errors!r}"
    )
    assert not any(t.is_alive() for t in threads), (
        "a worker thread is still running 20s after start — "
        "the bounded call did not return"
    )

    leaked_pids: List[int] = []
    for marker in markers:
        pid = _marker_pid(marker)
        if pid is None:
            continue
        if not reap_probe(pid):
            leaked_pids.append(pid)

    assert not leaked_pids, (
        f"concurrent timeouts left {len(leaked_pids)} grandchildren "
        f"alive (pids={leaked_pids}); at least one worker regressed "
        "from a group kill to a single-process kill"
    )

    residue = sorted(p.name for p in cwd.iterdir())
    assert residue == [], (
        f"cwd contains unexpected files after concurrent timeouts: "
        f"{residue}; the workers wrote something they should not have"
    )


@pytest.mark.timeout(60)
def test_no_resource_outlives_the_test(tmp_path: Path) -> None:
    """The test's own threads are reclaimed by teardown.

    ``clean_execution_state`` in ``tests/conftest.py`` is the global
    enforcer, but this test gives the *bounded_subprocess* path its
    own assertion: the thread count must return to the baseline after
    several timeouts ran.  If the wrapper ever grows a helper thread
    and forgets to join it, the assertion fails before the global
    one does, and the local context makes the fix cheap.
    """
    baseline_threads = {t.ident for t in threading.enumerate() if t.is_alive()}
    baseline_alive = len(baseline_threads)

    n_workers = 4
    markers: List[Path] = [tmp_path / f"thr{i}.pid" for i in range(n_workers)]
    threads: List[threading.Thread] = []
    for i in range(n_workers):
        cmd = _shell_with_marker(markers[i])
        def _run(_cmd: str = cmd) -> None:
            try:
                run_bounded(_cmd, cwd=str(tmp_path), timeout=1.0)
            except subprocess.TimeoutExpired:
                return
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=15.0)

    for t in threads:
        assert not t.is_alive(), (
            f"worker thread {t.name!r} did not finish within 15s; "
            "the bounded call returned nothing and the thread is hung"
        )

    final_alive = sum(1 for t in threading.enumerate() if t.is_alive())
    assert final_alive == baseline_alive, (
        f"thread count grew {baseline_alive} -> {final_alive} during the "
        f"test; live threads = "
        f"{[t.name for t in threading.enumerate() if t.is_alive()]!r}"
    )

    # And the grandchildren they spawned — the global teardown catches
    # threads, not processes; the bounded_subprocess reaps those.
    leaked = []
    for marker in markers:
        pid = _marker_pid(marker)
        if pid is None:
            continue
        if not reap_probe(pid):
            leaked.append(pid)
    assert not leaked, (
        f"baseline-only grandchildren survived: pids={leaked}; "
        "the test teardown handed them back, but the wrapper did not"
    )
