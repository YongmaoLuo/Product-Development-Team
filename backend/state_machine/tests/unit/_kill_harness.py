"""Kill harness for state-machine crash-recovery tests.

This module provides :func:`kill_subprocess_at`, the single helper
that every crash-recovery test in
``tests/unit/test_crash_recovery.py`` uses to simulate a hard
process kill (``SIGKILL`` / ``kill -9``) at one of the five named
"cut points" the architecture decision point 5 contract pins:

  1. ``commit_before``  — IMMEDIATE transaction has been opened but
                          COMMIT has NOT yet been issued. The kill
                          arrives between ``BEGIN IMMEDIATE`` and
                          the COMMIT. The on-disk state must
                          reflect the pre-txn state (no
                          advancement).
  2. ``commit_after``   — COMMIT has been issued but the
                          subsequent side-effect (e.g. a second
                          table write) has NOT yet run. The
                          on-disk state must reflect the post-
                          COMMIT state (advancement IS visible).
  3. ``mid_execution``  — the autonomous agent is in the middle of
                          a long-running execution loop. The kill
                          arrives at an arbitrary iteration; the
                          test asserts the on-disk progress
                          equals the LAST successfully committed
                          snapshot (not "in-flight" state).
  4. ``mid_verification`` — the verification worker is mid-round
                          (VP-N has just been observed but the
                          round-completion commit has not landed).
                          The on-disk verdict count equals the
                          LAST committed verdict count.
  5. ``mid_scheduler_tick`` — the scheduler has computed its
                          decision (which plans to schedule) in
                          memory but has not yet issued the
                          UPDATE on ``plan_execution``. The
                          on-disk state must NOT show the
                          decision (the previous tick's state
                          wins).

The harness deliberately works at the **Python subprocess**
level rather than the in-process level: the kill must take down
the process so file descriptors and SQLite caches are gone, and
the test then re-opens the on-disk database from cold to verify
the invariants. This matches the production crash semantics.

Design constraints
-------------------

  * The subprocess MUST hold its OWN sqlite3 connection (no
    connection shared with the parent test process). The 8000
    socket guard from ``state_machine/tests/conftest.py``
    applies only to ``127.0.0.1:8000``; we deliberately use
    ``127.0.0.1:0`` (ephemeral port) for any coordination socket.

  * The kill is a real SIGKILL (``signal.SIGKILL`` on POSIX,
    ``signal.SIGTERM`` followed by ``TerminateProcess`` on
    Windows). SIGTERM would let the subprocess clean up; we want
    the "no cleanup" semantics of kill -9.

  * The harness spawns a small Python script (NOT a full
    backend server). The script runs the IMMEDIATE txn or the
    loop iteration the test wants to interrupt, then sleeps in a
    marker line so the test can issue the kill in a controlled
    window.

  * The harness records a "checkpoint" file in the subprocess's
    ``--marker-dir`` directory each time the subprocess crosses a
    marker line. This lets the test verify *where* the kill landed
    after the fact (e.g. ``mid_scheduler_tick`` must show a
    ``pre_update`` marker but no ``post_update`` marker).

Implementation
--------------

The harness is a single public function (:func:`kill_subprocess_at`)
plus one private helper (:func:`_spawn_marker_runner`) that runs
the marker script. The marker script is generated inline (rather
than shipped as a file) so the cut-point semantics stay
self-contained and the test command never references an external
file that could drift between branches.

The marker script covers every cut point in one body; each cut
point is a labelled branch (``if cut_point == "commit_before"``,
etc.). This is a single Python file the harness writes to a
tempdir, executes with the test's venv Python, and removes when
the kill lands (best-effort).
"""

from __future__ import annotations

import os
import signal
import sqlite3
import string
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Optional, Union

__all__ = ["kill_subprocess_at", "CUT_POINTS"]


# The five named cut points the architecture decision pins.  Kept
# as a tuple so ``test_crash_recovery.py`` can iterate it for the
# parametrised invariants test.  ``frozenset`` would be slightly
# more idiomatic for membership tests but iteration is the
# primary use here.
CUT_POINTS: tuple[str, ...] = (
    "commit_before",
    "commit_after",
    "mid_execution",
    "mid_verification",
    "mid_scheduler_tick",
)


# A literal guard against accidental typos in test code.  The
# harness accepts any string but emits a ``ValueError`` if the
# string is not one of the canonical five — failing fast keeps
# the test's intent explicit.
_VALID_CUT_POINTS: frozenset[str] = frozenset(CUT_POINTS)


# Marker-script template. Uses ``string.Template`` (``$VAR`` syntax)
# so the embedded JSON braces in the subprocess source do NOT
# conflict with ``str.format``'s ``{...}`` placeholders.  The
# harness substitutes these before writing the file.
#
# ``PLAN_ID`` is the plan the subprocess operates on.  The test
# must have already inserted the row (and any sibling rows) on
# the parent side; the subprocess only does the write that the
# cut point is meant to interrupt.
_MARKER_SCRIPT_TEMPLATE = string.Template(textwrap.dedent(
    '''
    """Auto-generated marker script for :func:`kill_subprocess_at`.

    Cut point: $cut_point
    Plan id:   $plan_id
    DB path:   $db_path
    Marker dir:$marker_dir
    """
    from __future__ import annotations

    import json
    import os
    import sqlite3
    import sys
    import time
    from pathlib import Path

    PLAN_ID = $plan_id_literal
    DB_PATH = Path($db_path_literal)
    MARKER_DIR = Path($marker_dir_literal)

    def _write_marker(name):
        # Each marker is a tiny JSON file the parent test reads
        # post-kill to verify the subprocess reached that point.
        # ``flush=True`` so the file is durable on the parent's
        # filesystem even if the subprocess is killed before
        # the buffer is flushed.
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        path = MARKER_DIR / (name + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"cut_point": $cut_point_literal, "name": name, "pid": os.getpid()}, fh)
            fh.flush()

    def _open_conn():
        # Open a FRESH connection in the subprocess so the
        # "kill -9 + cold restart" semantics the test pins are
        # realistic — file descriptors and sqlite3 caches are
        # bounded by the subprocess lifetime.
        conn = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def cut_commit_before(conn):
        # 1. Begin IMMEDIATE, write a NEW version, do NOT commit.
        # On SIGKILL the txn rolls back (the journal contains
        # the abort marker) and the on-disk state is unchanged.
        _write_marker("begin_immediate")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            # 2026-09-17 (schema v5): one workflow-state column.
            # The sentinel values are deliberate markers the tests
            # assert on, not real phases.
            "UPDATE plan_routing SET version = version + 1, "
            "current_phase = 'commit_before_failed', "
            "updated_at = '9999-01-01T00:00:00Z' "
            "WHERE plan_id = ?",
            (PLAN_ID,),
        )
        _write_marker("pre_commit")
        # Hold the txn open so SIGKILL definitely lands between
        # the WRITE and the COMMIT.
        time.sleep($kill_hold_seconds)
        _write_marker("post_commit_attempt")  # only reachable if SIGKILL misses

    def cut_commit_after(conn):
        # 1. Begin IMMEDIATE, write, COMMIT.
        # 2. Sleep BEFORE the second-table side-effect.
        # On SIGKILL at the sleep the routing row IS advanced
        # but the verification row is NOT.
        _write_marker("begin_immediate")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE plan_routing SET version = version + 1, "
            "current_phase = 'commit_after_advanced', "
            "updated_at = '9999-01-02T00:00:00Z' "
            "WHERE plan_id = ?",
            (PLAN_ID,),
        )
        conn.execute("COMMIT")
        _write_marker("post_commit")
        time.sleep($kill_hold_seconds)
        _write_marker("post_side_effect")  # only reachable if SIGKILL misses

    def cut_mid_execution(conn):
        # Long-running "execution" loop. Each iteration writes a
        # task_progress JSON; the LAST successfully committed
        # iteration is the one the test asserts on.
        _write_marker("loop_start")
        for i in range($exec_loop_iters):
            conn.execute("BEGIN IMMEDIATE")
            payload = json.dumps({"current": "1-{}".format(i + 1), "completed": i + 1, "total": $exec_loop_iters})
            conn.execute(
                "UPDATE plan_execution SET task_progress = ?, "
                "updated_at = ? WHERE plan_id = ?",
                (payload, "9999-02-01T00:00:0{}Z".format(i), PLAN_ID),
            )
            conn.execute("COMMIT")
            _write_marker("iter_committed_" + str(i))
        # The kill MUST land BEFORE this final sleep returns.
        time.sleep($kill_hold_seconds)
        _write_marker("loop_end")  # only reachable if SIGKILL misses

    def cut_mid_verification(conn):
        # Append verdicts one by one; the LAST committed verdict
        # is the one the test asserts on.
        _write_marker("round_start")
        for i in range($verdict_count):
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT verdicts FROM plan_verification WHERE plan_id = ?",
                (PLAN_ID,),
            ).fetchone()
            raw = row[0] if row and row[0] else "[]"
            verdicts = json.loads(raw)
            verdicts.append({"vp_id": "VP-{}".format(i), "i": i})
            conn.execute(
                "UPDATE plan_verification SET verdicts = ?, "
                "updated_at = ? WHERE plan_id = ?",
                (
                    json.dumps(verdicts),
                    "9999-03-01T00:00:0{}Z".format(i),
                    PLAN_ID,
                ),
            )
            conn.execute("COMMIT")
            _write_marker("verdict_committed_" + str(i))
        time.sleep($kill_hold_seconds)
        _write_marker("round_end")  # only reachable if SIGKILL misses

    def cut_mid_scheduler_tick(conn):
        # The scheduler reads the table, computes its decision
        # set, and sleeps BEFORE writing the UPDATE back. The
        # kill must land in the sleep window so the on-disk
        # state stays at the pre-decision values.
        _write_marker("tick_start")
        cur = conn.execute(
            "SELECT next_run_at FROM plan_execution WHERE plan_id = ?",
            (PLAN_ID,),
        )
        row = cur.fetchone()
        _write_marker("decision_computed")
        # The "decision" lives only in this variable — it never
        # reaches the DB. The kill lands in the sleep that
        # follows the read.
        _decision = ("would_reschedule", row[0] if row else None)
        time.sleep($kill_hold_seconds)
        _write_marker("pre_update")  # never reached on kill
        conn.execute(
            "UPDATE plan_execution SET next_run_at = ? "
            "WHERE plan_id = ?",
            ("9999-04-01T00:00:00Z", PLAN_ID),
        )
        _write_marker("post_update")  # never reached on kill

    def main():
        conn = _open_conn()
        _write_marker("connected")
        try:
            cut = $cut_point_literal
            if cut == "commit_before":
                cut_commit_before(conn)
            elif cut == "commit_after":
                cut_commit_after(conn)
            elif cut == "mid_execution":
                cut_mid_execution(conn)
            elif cut == "mid_verification":
                cut_mid_verification(conn)
            elif cut == "mid_scheduler_tick":
                cut_mid_scheduler_tick(conn)
            else:
                raise SystemExit("unknown cut_point: " + cut)
        finally:
            try:
                conn.close()
            except sqlite3.OperationalError:
                pass

    main()
    '''
).strip())


def _quote_python(value: object) -> str:
    """Render ``value`` as a Python literal for inline substitution.

    Centralised so the rest of the harness does not need to
    think about repr-escaping for strings or paths.
    """
    if isinstance(value, str):
        # repr() handles all the Python-string escaping rules.
        return repr(value)
    return repr(value)


def _render_marker_script(
    cut_point: str,
    plan_id: str,
    db_path: Path,
    marker_dir: Path,
    kill_hold_seconds: float,
    exec_loop_iters: int,
    verdict_count: int,
) -> str:
    """Return the marker-script source with the harness vars substituted.

    The marker script is the only thing the subprocess runs; the
    harness substitutes ``cut_point``, ``plan_id``, ``db_path``,
    and ``marker_dir`` into the :class:`string.Template` and
    writes the result to a tempdir.  Iterations / verdicts are
    kept small (default 3) so the marker runs fast and the kill
    lands cleanly inside the sleep window.
    """
    substitutions = {
        "cut_point": cut_point,
        "plan_id": plan_id,
        "plan_id_literal": repr(plan_id),
        "db_path": str(db_path),
        "db_path_literal": repr(str(db_path)),
        "marker_dir": str(marker_dir),
        "marker_dir_literal": repr(str(marker_dir)),
        "cut_point_literal": repr(cut_point),
        "kill_hold_seconds": kill_hold_seconds,
        "exec_loop_iters": exec_loop_iters,
        "verdict_count": verdict_count,
    }
    return _MARKER_SCRIPT_TEMPLATE.substitute(substitutions)


def _spawn_marker_runner(
    script_source: str,
    marker_dir: Path,
    python_executable: Optional[str],
) -> subprocess.Popen:
    """Write ``script_source`` to a tempdir and launch it as a subprocess.

    Returns the :class:`subprocess.Popen` handle.  The subprocess
    is started with ``start_new_session=True`` so SIGKILL on the
    parent does not propagate (and the subprocess does NOT inherit
    a process group whose leader might survive the kill).
    """
    marker_dir.mkdir(parents=True, exist_ok=True)
    script_path = marker_dir / "marker_runner.py"
    script_path.write_text(script_source, encoding="utf-8")

    cmd = [python_executable or sys.executable, str(script_path)]
    # We capture stdout/stderr but never block on them — the
    # subprocess will be SIGKILL'd before it can flush much.
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_for_marker(
    marker_dir: Path,
    marker_name: str,
    timeout_seconds: float,
) -> bool:
    """Block until ``<marker_dir>/<marker_name>.json`` exists or timeout.

    Returns ``True`` when the marker appears, ``False`` on timeout.
    The poll interval is 10ms — short enough that a 5-second sleep
    inside the marker script is interrupted within ~10ms of the
    marker landing.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if (marker_dir / (marker_name + ".json")).exists():
            return True
        time.sleep(0.01)
    return False


def kill_subprocess_at(
    cut_point: str,
    *,
    plan_id: str,
    db_path: Union[str, Path],
    python_executable: Optional[str] = None,
    kill_hold_seconds: float = 2.0,
    exec_loop_iters: int = 3,
    verdict_count: int = 3,
    marker_dir: Optional[Union[str, Path]] = None,
    kill_signal: int = signal.SIGKILL,
    kill_timeout: float = 30.0,
) -> dict:
    """Run a subprocess that exercises ``cut_point``, then SIGKILL it.

    Parameters
    ----------
    cut_point:
        One of :data:`CUT_POINTS` (the five architecture-decision
        cut points).  Any other string raises ``ValueError``.
    plan_id:
        The ``plan_id`` the subprocess operates on.  The parent
        test is responsible for seeding the plan's rows BEFORE
        calling this function.
    db_path:
        Path to the SQLite database the subprocess writes
        against.  Must be the SAME file the parent test will
        re-open post-kill.
    python_executable:
        Path to the Python interpreter the subprocess uses.
        Defaults to ``sys.executable`` (the parent's
        interpreter).  Pass the test's venv Python explicitly
        when running under pytest with a different interpreter.
    kill_hold_seconds:
        How long the subprocess holds the "kill window" open.
        Default 2s — comfortably longer than the SIGKILL
        round-trip on any sane system.
    exec_loop_iters:
        Iteration count for the ``mid_execution`` cut point.
        Default 3; small enough that the loop finishes in <1s
        so the kill window is reached quickly.
    verdict_count:
        Verdict count for the ``mid_verification`` cut point.
        Default 3; same rationale as ``exec_loop_iters``.
    marker_dir:
        Directory the subprocess writes its ``<marker>.json``
        progress files into.  Defaults to a fresh
        :func:`tempfile.mkdtemp` directory.  The test may
        inspect this directory after the kill to verify *where*
        the kill landed.
    kill_signal:
        Signal to send.  Default :data:`signal.SIGKILL` — the
        production crash semantics.  Tests that want to verify
        the cleanup path may pass :data:`signal.SIGTERM` but
        the architecture-decision contract pins SIGKILL.
    kill_timeout:
        Maximum time the helper waits for the subprocess to
        terminate after the kill signal.  Default 30s — well
        above the few-millisecond actual kill time.

    Returns
    -------
    dict
        A small report with the keys ``"cut_point"``,
        ``"pid"``, ``"returncode"``, ``"markers"`` (the list of
        marker file names that landed BEFORE the kill),
        ``"marker_dir"`` (the :class:`pathlib.Path` of the
        marker directory the test can inspect), and
        ``"script_path"`` (the tempdir-relative path of the
        generated marker script).  Tests typically use this
        report to assert "the kill landed in the right window".
    """
    if cut_point not in _VALID_CUT_POINTS:
        raise ValueError(
            f"cut_point must be one of {CUT_POINTS!r}; got {cut_point!r}"
        )

    db_path = Path(db_path)
    if marker_dir is None:
        marker_dir = Path(tempfile.mkdtemp(prefix="kill_harness_"))
    else:
        marker_dir = Path(marker_dir)
        marker_dir.mkdir(parents=True, exist_ok=True)

    # The "wait until this marker exists" anchor.  Every cut
    # point writes this marker right before the kill window so
    # we know the kill will land inside the cut.
    #
    # 2026-09-18: ``mid_execution`` anchors on the FIRST committed
    # iteration, not on ``loop_start``.  The loop writes
    # ``loop_start`` and only then opens its first IMMEDIATE txn, so
    # anchoring there put the SIGKILL in a race the subprocess wins
    # only by scheduling luck: the parent polls every 10ms and
    # signals the instant it sees the marker.  Under a loaded
    # machine the parent won, and
    # ``test_kill_mid_execution_recovers_consistent_progress`` failed
    # on its own precondition ("cut landed before any iteration
    # committed") — a flake, seen once in a full-suite run on
    # 2026-09-18.  Anchoring on ``iter_committed_0`` makes the
    # precondition structural: the kill provably lands after ≥1
    # commit, while still landing at an arbitrary iteration (the
    # assertions accept completed ∈ {1, 2, 3}).
    # 2026-09-18: ``mid_verification`` anchors on the FIRST committed
    # verdict for the same reason as ``mid_execution`` above — both
    # assertions ("the cut landed before any verdict committed" and
    # ``1 <= len(verdicts) <= 3``) require ≥1 commit to have landed,
    # and anchoring on ``round_start`` left that to scheduling luck.
    pre_kill_marker = {
        "commit_before": "pre_commit",
        "commit_after": "post_commit",
        "mid_execution": "iter_committed_0",
        "mid_verification": "verdict_committed_0",
        "mid_scheduler_tick": "decision_computed",
    }[cut_point]

    script_source = _render_marker_script(
        cut_point=cut_point,
        plan_id=plan_id,
        db_path=db_path,
        marker_dir=marker_dir,
        kill_hold_seconds=kill_hold_seconds,
        exec_loop_iters=exec_loop_iters,
        verdict_count=verdict_count,
    )
    proc = _spawn_marker_runner(
        script_source=script_source,
        marker_dir=marker_dir,
        python_executable=python_executable,
    )

    # Block until the pre-kill marker exists so we know SIGKILL
    # lands in the right window.  Cap the wait at kill_timeout so
    # a stuck subprocess does not hang the test forever.
    landed = _wait_for_marker(
        marker_dir=marker_dir,
        marker_name=pre_kill_marker,
        timeout_seconds=kill_timeout,
    )
    try:
        proc.send_signal(kill_signal)
    except ProcessLookupError:
        # The subprocess already exited (e.g. the kill landed
        # just before our signal).  Treat as success.
        pass

    # Reap the subprocess.  ``kill_timeout`` cap mirrors the wait
    # above so the test cannot hang here either.
    try:
        proc.wait(timeout=kill_timeout)
    except subprocess.TimeoutExpired:
        # The subprocess refused to die — escalate to SIGKILL
        # even if a different signal was requested.
        try:
            proc.kill()
            proc.wait(timeout=5.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass

    # Snapshot the markers that landed BEFORE the kill.
    markers: list[str] = []
    if marker_dir.exists():
        for child in sorted(marker_dir.iterdir()):
            if child.is_file() and child.suffix == ".json":
                markers.append(child.stem)

    return {
        "cut_point": cut_point,
        "pid": proc.pid,
        "returncode": proc.returncode,
        "markers": markers,
        "marker_dir": marker_dir,
        "script_path": marker_dir / "marker_runner.py",
        "pre_kill_marker_observed": landed,
    }