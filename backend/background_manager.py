"""
Background Process Manager
==========================

Manages long-running background processes.
"""

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, List

from utils.process import _signallable_pgid


@dataclass
class ProcessState:
    """State of a background process."""
    pid: int
    command: str
    start_time: datetime
    last_output_time: datetime
    output_buffer: List[str] = field(default_factory=list)
    status: str = 'running'  # 'running', 'completed', 'failed', 'stuck', 'timeout'


class BackgroundManager:
    """Manages long-running background processes."""

    STUCK_THRESHOLD_SECONDS = 180  # 3 minutes without output = stuck
    MAX_LIFETIME_SECONDS = 1800    # 30 minutes absolute max = timeout

    def __init__(self):
        self.processes: Dict[str, ProcessState] = {}
        self._process_handles: Dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._output_threads: Dict[str, threading.Thread] = {}

    def start_process(self, task_id: str, command: str, cwd: str) -> str:
        """
        Start a background process for a task.

        Args:
            task_id: Task identifier
            command: Shell command to execute
            cwd: Working directory

        Returns:
            Process ID as string
        """
        with self._lock:
            # Kill any existing process for this task
            if task_id in self._process_handles:
                self._kill_process_internal(task_id)

            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=cwd,
                start_new_session=True,
            )

            now = datetime.utcnow()
            state = ProcessState(
                pid=process.pid,
                command=command,
                start_time=now,
                last_output_time=now,
                output_buffer=[],
                status='running'
            )

            self.processes[task_id] = state
            self._process_handles[task_id] = process

            # Start thread to read output
            output_thread = threading.Thread(
                target=self._read_output,
                args=(task_id, process),
                daemon=True
            )
            output_thread.start()
            self._output_threads[task_id] = output_thread

            return str(process.pid)

    def check_process(self, task_id: str) -> Optional[ProcessState]:
        """
        Check process status and output.

        Args:
            task_id: Task identifier

        Returns:
            ProcessState or None if not found
        """
        with self._lock:
            state = self.processes.get(task_id)
            if not state:
                return None

            process = self._process_handles.get(task_id)
            if not process:
                return state

            # Update status if process has ended
            poll_result = process.poll()
            if poll_result is not None:
                if poll_result == 0:
                    state.status = 'completed'
                else:
                    state.status = 'failed'

            # Check for stuck or timeout
            now = datetime.utcnow()
            elapsed_since_output = (now - state.last_output_time).total_seconds()
            elapsed_since_start = (now - state.start_time).total_seconds()

            if state.status == 'running':
                if elapsed_since_start > self.MAX_LIFETIME_SECONDS:
                    state.status = 'timeout'
                elif elapsed_since_output > self.STUCK_THRESHOLD_SECONDS:
                    state.status = 'stuck'

            return state

    def is_stuck(self, task_id: str) -> bool:
        """
        Check if process has been silent for too long.

        Args:
            task_id: Task identifier

        Returns:
            True if process is stuck
        """
        state = self.check_process(task_id)
        return state is not None and state.status == 'stuck'

    def is_timeout(self, task_id: str) -> bool:
        """
        Check if process has exceeded max lifetime.

        Args:
            task_id: Task identifier

        Returns:
            True if process has timed out
        """
        state = self.check_process(task_id)
        return state is not None and state.status == 'timeout'

    def kill_process(self, task_id: str) -> bool:
        """
        Terminate a stuck or timed-out process.

        Args:
            task_id: Task identifier

        Returns:
            True if process was killed successfully
        """
        with self._lock:
            return self._kill_process_internal(task_id)

    def get_output(self, task_id: str) -> str:
        """
        Get accumulated output from process.

        Args:
            task_id: Task identifier

        Returns:
            Combined output string
        """
        with self._lock:
            state = self.processes.get(task_id)
            if not state:
                return ""
            return "".join(state.output_buffer)

    def cleanup(self, task_id: str):
        """
        Clean up resources for a completed process.

        Args:
            task_id: Task identifier
        """
        with self._lock:
            self._kill_process_internal(task_id)
            if task_id in self.processes:
                del self.processes[task_id]
            if task_id in self._output_threads:
                del self._output_threads[task_id]

    def cleanup_all(self):
        """Clean up all processes."""
        with self._lock:
            for task_id in list(self._process_handles.keys()):
                self._kill_process_internal(task_id)
            self.processes.clear()
            self._output_threads.clear()

    def _read_output(self, task_id: str, process: subprocess.Popen):
        """Read output from process in background thread."""
        try:
            while True:
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue

                with self._lock:
                    state = self.processes.get(task_id)
                    if state:
                        state.output_buffer.append(line)
                        state.last_output_time = datetime.utcnow()
        except Exception:
            pass

    def _kill_process_internal(self, task_id: str) -> bool:
        """Internal method to kill process (must be called with lock held).

        Uses SIGTERM on the process group (started with start_new_session=True)
        so the entire tree is terminated, then falls back to single-process kill.
        """
        process = self._process_handles.get(task_id)
        if not process:
            return False
        try:
            # Kill the entire process group first
            try:
                pgid = os.getpgid(process.pid)
                if _signallable_pgid(pgid) is not None:
                    os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass
            del self._process_handles[task_id]
            return True
        except Exception:
            return False
