"""
Tests for subprocess cleanup guarantees.

Verifies that:
1. BackgroundManager kills entire process groups, not just the lead process
2. AutonomousAgent.run() cleans up executor resources on all exit paths
3. ClaudeCodingTool.cleanup() kills in-flight processes
4. server._shutdown_all_executions() kills running subprocesses
5. server._kill_process_tree() handles process groups correctly
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from background_manager import BackgroundManager, ProcessState


class TestBackgroundManagerProcessGroups:
    """BackgroundManager uses start_new_session=True and kills the whole group."""

    def test_start_process_uses_new_session(self, tmp_path):
        """Popen is called with start_new_session=True so child gets its own group."""
        mgr = BackgroundManager()
        with patch("background_manager.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.stdout = iter([])
            mock_proc.poll.return_value = 0
            mock_popen.return_value = mock_proc

            mgr.start_process("t1", "echo hello", str(tmp_path))

            _, kwargs = mock_popen.call_args
            assert kwargs.get("start_new_session") is True, \
                "start_new_session must be True for process group isolation"

    def test_kill_process_uses_killpg(self, tmp_path):
        """_kill_process_internal sends SIGTERM to the process group via os.killpg."""
        mgr = BackgroundManager()
        with patch("background_manager.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.stdout = iter([])
            mock_proc.poll.return_value = 0
            mock_popen.return_value = mock_proc

            mgr.start_process("t1", "echo hello", str(tmp_path))

            with patch("background_manager.os.getpgid", return_value=99999) as mock_getpgid, \
                 patch("background_manager.os.killpg") as mock_killpg:
                with mgr._lock:
                    mgr._kill_process_internal("t1")

                mock_getpgid.assert_called_once_with(12345)
                mock_killpg.assert_called_once_with(99999, signal.SIGTERM)

    def test_cleanup_all_kills_every_process(self, tmp_path):
        """cleanup_all() terminates all tracked processes."""
        mgr = BackgroundManager()
        with patch("background_manager.subprocess.Popen") as mock_popen:
            processes = []
            for i in range(3):
                mock_proc = MagicMock()
                mock_proc.pid = 10000 + i
                mock_proc.stdout = iter([])
                mock_proc.poll.return_value = 0
                processes.append(mock_proc)

            mock_popen.side_effect = processes
            for i in range(3):
                mgr.start_process(f"t{i}", f"cmd{i}", str(tmp_path))

            assert len(mgr._process_handles) == 3

            with patch("background_manager.os.getpgid", side_effect=[10000, 10001, 10002]), \
                 patch("background_manager.os.killpg"):
                mgr.cleanup_all()

            assert len(mgr._process_handles) == 0
            assert len(mgr.processes) == 0

    def test_cleanup_handles_already_dead_process(self, tmp_path):
        """_kill_process_internal gracefully handles ProcessLookupError."""
        mgr = BackgroundManager()
        with patch("background_manager.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.stdout = iter([])
            mock_proc.poll.return_value = 0
            mock_popen.return_value = mock_proc

            mgr.start_process("t1", "echo hello", str(tmp_path))

            with patch("background_manager.os.getpgid", side_effect=ProcessLookupError), \
                 patch("background_manager.os.killpg") as mock_killpg:
                with mgr._lock:
                    result = mgr._kill_process_internal("t1")

                # Should not raise, process handle should be cleaned up
                assert "t1" not in mgr._process_handles


class TestAgentRunCleanup:
    """AutonomousAgent.run() cleans up on all exit paths."""

    def test_run_calls_cleanup_on_normal_exit(self, tmp_path):
        """When all tasks complete normally, executor.cleanup() is still called."""
        from agent import AutonomousAgent

        with patch("agent.TaskManager") as mock_tm_cls, \
             patch("agent.BackgroundManager") as mock_bm_cls, \
             patch("agent.Executor") as mock_exec_cls, \
             patch("agent.GitManager"):

            mock_tm = MagicMock()
            mock_tm.tasks = []
            mock_tm_cls.return_value = mock_tm

            mock_exec = MagicMock()
            mock_exec_cls.return_value = mock_exec

            mock_bm = MagicMock()
            mock_bm_cls.return_value = mock_bm

            # Create agent with minimal setup
            with patch.object(AutonomousAgent, '_get_provider_controller'):
                agent = AutonomousAgent(
                    requirement="test",
                    project_dir=tmp_path,
                    coding_tool=MagicMock(),
                )
                agent._provider_controller = MagicMock()
                # Empty task list -> _run_async exits immediately
                agent._all_tasks = []

                agent.run()

            # Verify cleanup was called
            mock_exec.cleanup.assert_called_once()

    def test_run_calls_cleanup_on_exception(self, tmp_path):
        """When an exception occurs, executor.cleanup() is still called in finally."""
        from agent import AutonomousAgent

        with patch("agent.TaskManager") as mock_tm_cls, \
             patch("agent.BackgroundManager") as mock_bm_cls, \
             patch("agent.Executor") as mock_exec_cls, \
             patch("agent.GitManager"):

            mock_tm = MagicMock()
            mock_tm.tasks = []
            mock_tm_cls.return_value = mock_tm

            mock_exec = MagicMock()
            mock_exec_cls.return_value = mock_exec

            mock_bm = MagicMock()
            mock_bm_cls.return_value = mock_bm

            with patch.object(AutonomousAgent, '_get_provider_controller'):
                agent = AutonomousAgent(
                    requirement="test",
                    project_dir=tmp_path,
                    coding_tool=MagicMock(),
                )
                agent._provider_controller = MagicMock()
                agent._all_tasks = []

                # Make _run_async raise
                with patch.object(agent, '_run_async', side_effect=RuntimeError("boom")):
                    with pytest.raises(RuntimeError):
                        agent.run()

                # Verify cleanup was called despite exception
                mock_exec.cleanup.assert_called_once()


class TestCodingToolCleanup:
    """ClaudeCodingTool.cleanup() kills in-flight processes."""

    def test_cleanup_kills_inflight_process(self):
        """cleanup() calls _graceful_shutdown on the current in-flight process."""
        from coding_tool import ClaudeCodingTool

        with patch.object(ClaudeCodingTool, "_load_provider_from_db", return_value={}):
            tool = ClaudeCodingTool()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running

        with tool._process_lock:
            tool._current_process = mock_proc

        with patch.object(tool, '_graceful_shutdown') as mock_shutdown:
            tool.cleanup()
            mock_shutdown.assert_called_once_with(mock_proc)

        # _current_process should be cleared
        assert tool._current_process is None

    def test_cleanup_safe_when_no_process(self):
        """cleanup() is safe to call when no process is running."""
        from coding_tool import ClaudeCodingTool

        with patch.object(ClaudeCodingTool, "_load_provider_from_db", return_value={}):
            tool = ClaudeCodingTool()

        assert tool._current_process is None
        tool.cleanup()  # Should not raise

    def test_cleanup_idempotent(self):
        """cleanup() can be called multiple times safely."""
        from coding_tool import ClaudeCodingTool

        with patch.object(ClaudeCodingTool, "_load_provider_from_db", return_value={}):
            tool = ClaudeCodingTool()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        with tool._process_lock:
            tool._current_process = mock_proc

        with patch.object(tool, '_graceful_shutdown'):
            tool.cleanup()
            tool.cleanup()  # Second call should be safe

        assert tool._current_process is None


class TestServerKillProcessTree:
    """server._kill_process_tree() terminates entire process groups."""

    def test_kill_process_tree_sends_sigterm_to_group(self):
        """_kill_process_tree uses os.killpg to kill the group."""
        import server

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.pid = 55555

        with patch("server.os.getpgid", return_value=44444) as mock_getpgid, \
             patch("server.os.killpg") as mock_killpg:
            server._kill_process_tree(mock_proc)

            mock_getpgid.assert_called_once_with(55555)
            mock_killpg.assert_called_once_with(44444, signal.SIGTERM)

    def test_kill_process_tree_handles_dead_process(self):
        """_kill_process_tree returns immediately if process already exited."""
        import server

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # already exited

        with patch("server.os.getpgid") as mock_getpgid:
            server._kill_process_tree(mock_proc)
            mock_getpgid.assert_not_called()

    def test_kill_process_tree_fallback_to_kill(self):
        """If SIGTERM group times out, falls back to process.kill()."""
        import server

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 55555
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("wait", 10), None]

        with patch("server.os.getpgid", return_value=44444), \
             patch("server.os.killpg"):
            server._kill_process_tree(mock_proc)
            mock_proc.kill.assert_called_once()


class TestServerShutdownAllExecutions:
    """server._shutdown_all_executions() cleans up all running subprocesses."""

    def test_shutdown_kills_running_subprocesses(self, tmp_path):
        """All running execution subprocesses are killed during shutdown."""
        import server

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345

        server._execution_state["plan-1"] = {
            "status": "running",
            "process": mock_proc,
        }

        with patch("server._kill_process_tree") as mock_kill:
            server._shutdown_all_executions()
            mock_kill.assert_called_once_with(mock_proc)

        # Cleanup
        server._execution_state.clear()

    def test_shutdown_skips_completed_executions(self, tmp_path):
        """Completed executions are not touched during shutdown."""
        import server

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # already exited

        server._execution_state["plan-1"] = {
            "status": "completed",
            "process": mock_proc,
        }

        with patch("server._kill_process_tree") as mock_kill:
            server._shutdown_all_executions()
            mock_kill.assert_not_called()

        server._execution_state.clear()

    def test_shutdown_handles_empty_state(self, tmp_path):
        """shutdown works when no executions exist."""
        import server
        server._execution_state.clear()
        # Should not raise
        server._shutdown_all_executions()


class TestStopExecutionKillsProcessTree:
    """stop_execution API kills entire process tree."""







