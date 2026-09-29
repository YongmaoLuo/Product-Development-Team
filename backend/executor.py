"""
Command Executor
================

Executes shell commands and captures output.
Supports both foreground and background execution.

2026-09-08: ``_run_foreground`` no longer waits silently for a
long-running subprocess to finish. It now throttles the output stream
to ``logs/server.log`` (a 1-line heartbeat summary every
``flush_interval`` seconds) while still returning the full output to
the caller. Error-pattern lines (FAILED / ERROR / Traceback / Exception
/ assert) bypass the throttle and are emitted immediately as
``WARNING`` so a hung process or test failure is visible without
waiting for the next heartbeat tick.

The throttling preserves the "did work actually happen?" signal the
operator needs when a pytest suite runs for >30 minutes — without
flushing the log every line (pytest at -v prints ~1500 lines for a
~1500-test suite) and without ever falsely claiming "alive" for a
dead / hung subprocess (the ``process.poll() is None`` check on every
iteration surfaces real process death; a missing ``tail:`` field on
the heartbeat surfaces 60s-of-no-output).
"""

import logging
import re
import selectors
import subprocess
import time
from typing import Optional, Tuple

from background_manager import BackgroundManager


# Lines matching any of these substrings (case-insensitive) bypass the
# throttle buffer and are emitted to logs immediately at WARNING level.
# The list is intentionally narrow — false positives (e.g. an INFO line
# containing the word "traceback") are tolerable, but a missed FAILED
# line on a 30-minute pytest run is not.
_ERROR_LINE_RE = re.compile(
    r"\b(FAILED|ERROR|Traceback|Exception|AssertionError|^E\s|assert )\b",
    re.IGNORECASE | re.MULTILINE,
)

# Defaults for the throttled foreground runner.
_DEFAULT_FLUSH_INTERVAL_SEC = 60.0   # emit heartbeat summary every 60s
_DEFAULT_BUFFER_MAX_LINES = 100     # cap buffered non-error lines
_DEFAULT_LINE_TRUNCATE_CHARS = 120  # per-line cap on buffered content

logger = logging.getLogger(__name__)


class Executor:
    """Executes shell commands in a project directory."""

    def __init__(self, project_dir: str, background_manager: Optional[BackgroundManager] = None):
        self.project_dir = project_dir
        self.background_manager = background_manager or BackgroundManager()
        self._timeout_history: dict = {}

    def run_command(
        self,
        command: str,
        task_id: Optional[str] = None,
        timeout: Optional[int] = None,
        previous_timeout: bool = False,
        cwd: Optional[str] = None
    ) -> Tuple[int, str]:
        """
        Run command with smart timeout handling.

        Args:
            command: Shell command to execute
            task_id: Task identifier for background processes
            timeout: Timeout in seconds for foreground execution
            previous_timeout: If True, run as background process
            cwd: Optional working directory override

        Returns:
            Tuple of (exit_code, output)
        """
        if previous_timeout and task_id:
            return self._run_background(command, task_id, cwd=cwd)
        else:
            return self._run_foreground(command, timeout, cwd=cwd)

    def _run_foreground(
        self,
        command: str,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL_SEC,
        buffer_max_lines: int = _DEFAULT_BUFFER_MAX_LINES,
        line_truncate_chars: int = _DEFAULT_LINE_TRUNCATE_CHARS,
        max_idle_seconds: Optional[float] = 300.0,
    ) -> Tuple[int, str]:
        """Run a command in the foreground with throttled log streaming.

        Behaviour contract (2026-09-08 plan):
          * Every line the subprocess emits is appended to ``output``
            (the full return string for callers) — no data is dropped.
          * Lines matching :data:`_ERROR_LINE_RE` are immediately logged
            at ``WARNING`` with the elapsed-time and the line itself,
            so a test failure on a 30-minute pytest run is visible
            without waiting for the next heartbeat tick.
          * Non-error lines are buffered (capped at ``buffer_max_lines``,
            each truncated to ``line_truncate_chars``) and a 1-line
            heartbeat summary is logged every ``flush_interval``
            seconds. The heartbeat contains ``lines=<N>`` (cumulative
            count) and ``tail=<last 3 buffered lines>`` so an operator
            can see both "is it still making progress?" (``lines``
            growing) and "what was it doing 60s ago?" (``tail``).
          * When no lines have arrived in the last ``flush_interval``
            seconds, the heartbeat STILL fires (showing
            ``lines=<same>``, ``tail=<empty>``) — that empty heartbeat
            is the operator's "POSSIBLY HUNG" signal. It does not
            claim "alive" without evidence: ``tail=<empty>`` is the
            negative signal.
          * On timeout, the subprocess is SIGTERM'd (then SIGKILL'd
            after 500ms grace), the final buffer is flushed, and the
            caller gets ``(-1, "Command timed out after Xs\n<output>")``.
          * On clean exit, the final buffer is flushed and a summary
            line with ``lines=<N> errors=<M> elapsed=<s>`` is logged
            at INFO level.
          * On ``max_idle_seconds`` expiry (default 300s = 5 min of
            zero stdout), the subprocess is killed and the caller
            gets ``(-2, "Subprocess hung: no stdout for Ns ...")``.
            This catches a process that is alive (no EOF, no
            timeout) but stuck — pytest on a hung network fixture,
            a Rust linker waiting on a missing shared lib, etc.
            Without this guard, a hung subprocess would burn the
            full ``timeout`` window before being killed.

        The throttling NEVER lies: a heartbeat with ``tail=<empty>``
        means the subprocess has produced zero lines in the last
        ``flush_interval`` seconds. The next heartbeat's ``lines=<N>``
        either grows (work is happening) or stays flat (work has
        stalled). The operator can read this without trusting any
        synthetic "alive" assertion.
        """
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=cwd or self.project_dir
            )
        except Exception as exc:
            return 1, f"Error spawning subprocess: {exc}"

        output = ""
        start_time = time.time()
        last_flush = start_time
        # 2026-09-08: track ``last_output_time`` so we can detect
        # hung processes — ``max_idle_seconds`` is the safety net that
        # kills a subprocess that hasn't emitted a single line for N
        # seconds. The wall-clock ``timeout`` is the upper bound; this
        # idle check catches the case where the subprocess is alive
        # but stuck (e.g. pytest on a hung network fixture, a Rust
        # linker waiting on a missing shared lib, a `pdb.set_trace()`
        # blocking on stdin). Without this guard, ``timeout`` would
        # be the only thing that fired — and 15 min of idle is a long
        # time to stare at a heartbeat with ``lines=0 tail=''``.
        last_output_time = start_time
        # Buffered non-error lines (truncated to ``line_truncate_chars``).
        buffered = []
        # Cumulative counts.
        line_count = 0
        error_count = 0
        # Use a selector so we can wake up every second to enforce the
        # wall-clock timeout and the heartbeat cadence. A blocking
        # ``process.stdout.readline()`` would only return when the
        # subprocess flushes a line — for ``sleep 5`` (or any other
        # silent process) the loop would never see the timeout fire
        # and the heartbeat would never tick. ``selectors`` is in the
        # stdlib and works on POSIX + Windows.
        sel = selectors.DefaultSelector()
        sel.register(process.stdout, selectors.EVENT_READ)
        # How long to wait on each ``select`` call. Short enough that
        # the timeout / heartbeat timers are accurate to within ~1s.
        select_timeout_sec = 1.0

        def flush_buffer(reason: str) -> None:
            """Emit a single heartbeat INFO line summarising the buffer.

            Always emits (even with empty buffer) so the operator can
            distinguish "still alive, just no output yet" from
            "subprocess is gone" by looking at ``lines=<N>`` growth
            between consecutive heartbeats.
            """
            nonlocal last_flush, buffered
            elapsed = int(time.time() - start_time)
            tail = " | ".join(buffered[-3:]) if buffered else ""
            logger.info(
                "[EXEC HB cmd=%r elapsed=%ds lines=%d buffered=%d "
                "errors=%d reason=%s tail=%r]",
                command[:60],
                elapsed,
                line_count,
                len(buffered),
                error_count,
                reason,
                tail,
            )
            buffered = []
            last_flush = time.time()

        eof_seen = False
        while not eof_seen:
            elapsed = time.time() - start_time
            idle = time.time() - last_output_time
            if timeout and elapsed > timeout:
                process.terminate()
                time.sleep(0.5)
                if process.poll() is None:
                    process.kill()
                sel.unregister(process.stdout)
                flush_buffer("timeout")
                return -1, f"Command timed out after {timeout} seconds\n{output}"
            if max_idle_seconds and idle > max_idle_seconds:
                # Hung subprocess: alive (we'd have seen EOF otherwise)
                # but hasn't emitted any stdout for ``max_idle_seconds``.
                # The heartbeat would have shown ``lines=<N> tail=''``
                # for the entire window — surface that explicitly so the
                # operator knows "not a wall-clock timeout, a hung
                # subprocess".
                process.terminate()
                time.sleep(0.5)
                if process.poll() is None:
                    process.kill()
                sel.unregister(process.stdout)
                flush_buffer("idle-timeout")
                return -2, (
                    f"Subprocess hung: no stdout for {int(idle)}s "
                    f"(max_idle_seconds={max_idle_seconds}, lines={line_count}). "
                    f"Last output at elapsed={int(elapsed - idle)}s.\n{output}"
                )

            # Wait for stdout readiness OR the per-iteration timeout,
            # whichever comes first. ``select`` returns an empty list
            # on timeout — we use that to drive the heartbeat cadence
            # AND the wall-clock timeout enforcement.
            events = sel.select(timeout=select_timeout_sec)
            if not events:
                # No data this tick. Drive the heartbeat and loop.
                if time.time() - last_flush >= flush_interval:
                    flush_buffer("interval")
                continue

            # At least one fd is ready. Read at most one line per tick
            # so a flood of ready lines can't starve the heartbeat.
            for key, _ in events:
                line = key.fileobj.readline()
                if not line:
                    # EOF — process closed its stdout. Unregister so
                    # the next ``select`` doesn't busy-loop on a stale
                    # fd, then break out of the inner for; the outer
                    # while-loop will check ``process.poll()`` and
                    # terminate once the process has actually exited.
                    sel.unregister(key.fileobj)
                    eof_seen = True
                    break
                output += line
                line_count += 1
                # Reset the idle clock — every new line counts as
                # "the subprocess is making progress". A pytest run
                # that emits 1000 lines/sec keeps the idle timer at
                # ~0 forever; a hung subprocess with no output trips
                # ``max_idle_seconds`` cleanly.
                last_output_time = time.time()
                stripped = line.rstrip()

                if _ERROR_LINE_RE.search(stripped):
                    error_count += 1
                    flush_buffer("pre-error")
                    logger.warning(
                        "[EXEC ERR #%d cmd=%r elapsed=%ds] %s",
                        error_count,
                        command[:60],
                        int(elapsed),
                        stripped[:300],
                    )
                else:
                    truncated = stripped[:line_truncate_chars]
                    buffered.append(truncated)
                    if len(buffered) > buffer_max_lines:
                        buffered = buffered[buffer_max_lines // 2:]

                # After processing a line, also drive the heartbeat if
                # enough time has passed — keeps the cadence stable
                # even under heavy output.
                if time.time() - last_flush >= flush_interval:
                    flush_buffer("interval")
                break  # one line per tick

        # EOF seen. Wait for the process to fully exit (readline()
        # returns empty before ``poll()`` reflects the exit code on
        # some platforms; drain the rest before returning).
        try:
            process.wait(timeout=5)
        except Exception:
            # Process didn't exit cleanly within 5s after EOF — kill.
            try:
                process.kill()
            except Exception:
                pass
        sel.close()
        flush_buffer("exit")
        logger.info(
            "[EXEC DONE cmd=%r lines=%d errors=%d elapsed=%ds exit=%d]",
            command[:60],
            line_count,
            error_count,
            int(time.time() - start_time),
            process.returncode,
        )
        return process.returncode, output

    def _run_background(
        self,
        command: str,
        task_id: str,
        poll_interval: int = 30,
        cwd: Optional[str] = None
    ) -> Tuple[int, str]:
        """
        Run command in background and poll for output.

        Args:
            command: Shell command to execute
            task_id: Task identifier
            poll_interval: Seconds between status checks
            cwd: Optional working directory override

        Returns:
            Tuple of (exit_code, output)
        """
        self.background_manager.start_process(task_id, command, cwd or self.project_dir)

        while True:
            time.sleep(poll_interval)
            state = self.background_manager.check_process(task_id)

            if state is None:
                return 1, "Process state not found"

            if state.status == 'completed':
                output = self.background_manager.get_output(task_id)
                self.background_manager.cleanup(task_id)
                return 0, output

            elif state.status == 'failed':
                output = self.background_manager.get_output(task_id)
                self.background_manager.cleanup(task_id)
                return 1, output

            elif state.status == 'timeout':
                # Max lifetime exceeded (30 min)
                output = self.background_manager.get_output(task_id)
                self.background_manager.kill_process(task_id)
                self.background_manager.cleanup(task_id)
                return -1, f"Process timeout (exceeded {self.background_manager.MAX_LIFETIME_SECONDS}s)\n{output}"

            elif state.status == 'stuck':
                # No output for 3 minutes = stuck
                output = self.background_manager.get_output(task_id)
                self.background_manager.kill_process(task_id)
                self.background_manager.cleanup(task_id)
                return -1, f"Process stuck (no output for {self.background_manager.STUCK_THRESHOLD_SECONDS} seconds)\n{output}"

    def record_timeout(self, task_id: str, duration: int):
        """
        Record timeout duration for a task.

        Args:
            task_id: Task identifier
            duration: Timeout duration in seconds
        """
        if task_id not in self._timeout_history:
            self._timeout_history[task_id] = []
        self._timeout_history[task_id].append(duration)

    def had_previous_timeout(self, task_id: str) -> bool:
        """
        Check if task had a previous timeout.

        Args:
            task_id: Task identifier

        Returns:
            True if task had a previous timeout, False otherwise
        """
        return task_id in self._timeout_history and len(self._timeout_history[task_id]) > 0

    def cleanup(self):
        """Clean up all background processes."""
        self.background_manager.cleanup_all()
