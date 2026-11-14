"""A dispatch-phase failure must TERMINALISE the task, not loop forever.

The accident this pins down
---------------------------
``AutonomousAgent._run_task_with_provider_slot`` calls
``_pre_task_lock_hook`` — the in-memory conflict check plus the OS
file-lock acquisition — before ``_execute_task_with_retry``.  A task
whose ``files_to_modify`` names a DIRECTORY (e.g. ``backend/.venv``)
makes that hook raise ``RuntimeError("files_to_modify must name files,
not directories: ...")``.

That raise used to unwind straight past ``_execute_task_with_retry`` —
the only place that records a task verdict — so the task stayed
``pending`` forever and the outer dispatcher re-picked it on the next
tick.  A plan carrying such a task burned 13.5 hours and made ZERO LLM
calls before anyone noticed: the executor never even started, and the
plan could never converge because nothing ever changed the task's
status.

The contracts pinned here
--------------------------
  1. ``test_directory_declaration_fails_task_and_records_reason``
     The accident itself.  A directory declaration ends as a FAILED task
     whose ``failure_reason`` carries the ORIGINAL message (behind the
     ``dispatch_error: `` prefix), and the call returns ``False``.
  2. ``test_dispatch_exception_retries_then_succeeds``
     A dispatch-phase exception is retried; a retry that succeeds runs
     the executor EXACTLY ONCE (the failed attempt did not cost a
     second execution).
  3. ``test_dispatch_exception_releases_provider_slot``
     A dispatch-phase failure that never recovers still releases the
     provider slot — otherwise every failed task permanently bleeds one
     slot of the run's provider budget.
  4. ``test_dispatch_max_attempts_env_override``
     ``PDT_DISPATCH_MAX_ATTEMPTS`` bounds the attempts, and the verdict
     is recorded EXACTLY ONCE no matter how many attempts were made.
  5. ``test_executor_exception_still_propagates``
     An exception raised by ``_execute_task_with_retry`` is NOT a
     dispatch-phase failure: it propagates unchanged and does NOT mark
     the task failed (the executor owns that verdict, and this guard
     must not steal it).
  6. ``test_cancelled_dispatch_is_not_converted_into_a_failure``
     A cancelled run is not a dispatch failure: ``CancelledError`` is
     neither retried nor turned into a verdict.
  7. ``test_dispatch_defaults_are_exported`` /
     ``test_dispatch_env_knobs_fall_back_on_garbage``
     The two knobs carry their documented defaults, and a malformed
     environment value falls back to the default rather than to "never
     attempt" (which would reproduce the accident).
  8. ``test_non_finite_backoff_falls_back_to_default`` /
     ``test_finite_backoff_values_still_honoured`` /
     ``test_infinite_backoff_env_does_not_hang_dispatch``
     A NON-FINITE backoff (``inf`` / ``nan``) falls back to the default
     too: ``asyncio.sleep(inf)`` never returns, so the retry would hang
     the task in ``pending`` forever — the same accident, reached
     through a different knob.  The legal values (including zero) keep
     working, and a dispatch under ``inf`` still reaches its verdict
     inside the timeout.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import agent as agent_module  # noqa: E402
from agent import AutonomousAgent  # noqa: E402
from file_lock_manager import FileLockManager  # noqa: E402
from task import SubTask  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingController:
    """Provider-slot stub that records every acquire / release.

    Mirrors the real :class:`ProviderConcurrencyController` surface used
    by :meth:`AutonomousAgent._run_task_with_provider_slot`: an
    awaitable ``acquire(provider)`` and a synchronous
    ``release(provider)``.
    """

    def __init__(self) -> None:
        self.acquired: List[str] = []
        self.released: List[str] = []

    async def acquire(self, provider: str) -> None:
        self.acquired.append(provider)

    def release(self, provider: str) -> None:
        self.released.append(provider)


class _StubCodingTool:
    """The coding tool is never reached — the executor body is stubbed."""

    def query_json(self, *args, **kwargs):  # pragma: no cover - guard
        raise AssertionError(
            "the coding tool must not be called: the executor body is stubbed"
        )


class _RecordingLogger:
    """In-memory logger stub capturing every structured event."""

    def __init__(self) -> None:
        self.events: List[dict] = []

    def _record(self, level: str, event: str, message: str, **kwargs) -> None:
        self.events.append({
            "level": level,
            "event": event,
            "message": message,
            **kwargs,
        })

    def debug(self, event: str, message: str, **kwargs) -> None:
        self._record("DEBUG", event, message, **kwargs)

    def info(self, event: str, message: str, **kwargs) -> None:
        self._record("INFO", event, message, **kwargs)

    def warning(self, event: str, message: str, **kwargs) -> None:
        self._record("WARNING", event, message, **kwargs)

    def error(self, event: str, message: str, **kwargs) -> None:
        self._record("ERROR", event, message, **kwargs)

    def critical(self, event: str, message: str, **kwargs) -> None:
        self._record("CRITICAL", event, message, **kwargs)

    def event_names(self) -> List[str]:
        return [e["event"] for e in self.events]


def _git_init(project_dir: Path) -> None:
    """Init a real git repo so ``GitManager`` can bind to it.

    ``AutonomousAgent.__init__`` constructs a ``GitManager``, which
    refuses to bind to a directory that is not inside a git repository.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )


def _build_agent(project_dir: Path, logger=None) -> AutonomousAgent:
    """Build an ``AutonomousAgent`` bound to ``project_dir``.

    No LLM is involved: every test stubs the executor body, and the
    coding tool raises if it is ever reached.
    """
    _git_init(project_dir)
    return AutonomousAgent(
        requirement="a dispatch-phase failure must terminalise the task",
        project_dir=project_dir,
        coding_tool=_StubCodingTool(),
        logger=logger,
    )


def _make_task(
    task_id: str = "T1",
    files: List[str] | None = None,
) -> SubTask:
    """A pending task with a concrete file declaration."""
    return SubTask(
        id=task_id,
        title="dispatch failure task",
        description="a task whose dispatch may fail",
        test_command="",
        status="pending",
        provider="test-provider",
        files_to_modify=list(files) if files is not None else ["src/app.py"],
    )


def _register(agent: AutonomousAgent, task: SubTask) -> SubTask:
    """Put ``task`` into the task manager so the verdict has a subject.

    ``TaskManager.record_task_failure`` matches by id against
    ``task_manager.tasks``; a task the manager does not know about
    cannot be failed, which is exactly the "the raise never reached the
    verdict" accident this file is about.
    """
    agent.task_manager.tasks = [task]
    return task


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Zero the inter-attempt backoff so the tests do not sleep.

    The backoff itself is a separate knob
    (``PDT_DISPATCH_RETRY_BACKOFF_SECONDS``); the contracts pinned here
    are about the attempt COUNT and the verdict, not about wall-clock.
    """
    monkeypatch.setenv("PDT_DISPATCH_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.delenv("PDT_DISPATCH_MAX_ATTEMPTS", raising=False)


# ---------------------------------------------------------------------------
# 1. The accident: a directory declaration must fail the task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_directory_declaration_fails_task_and_records_reason(tmp_path):
    """A directory in ``files_to_modify`` ends as a FAILED task.

    Uses the REAL pre-task lock hook: the directory check lives there,
    and it is the production path that produced 13.5 hours of zero LLM
    calls.
    """
    project_dir = tmp_path / "project"
    (project_dir / "backend" / ".venv").mkdir(parents=True)

    logger = _RecordingLogger()
    agent = _build_agent(project_dir, logger=logger)
    task = _register(agent, _make_task(files=["backend/.venv"]))

    controller = _RecordingController()
    result = await agent._run_task_with_provider_slot(task, controller, None)

    assert result is False, "a dispatch failure must not report success"
    assert task.status == "failed", (
        f"the task must reach a terminal status, got {task.status!r}"
    )
    assert task.failure_reason is not None
    assert task.failure_reason.startswith(
        "dispatch_error: files_to_modify must name files"
    ), (
        "the failure_reason must carry the ORIGINAL hook message behind "
        f"the dispatch_error prefix, got {task.failure_reason!r}"
    )
    assert "task_dispatch_failed" in logger.event_names(), (
        "the terminal dispatch failure must be logged as task_dispatch_failed"
    )


# ---------------------------------------------------------------------------
# 2. A dispatch exception is retried, and a successful retry runs once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_exception_retries_then_succeeds(tmp_path):
    """First attempt raises, second succeeds → the executor runs once."""
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    logger = _RecordingLogger()
    agent = _build_agent(project_dir, logger=logger)
    task = _register(agent, _make_task())

    hook_calls: List[int] = []
    executor_calls: List[str] = []

    def _flaky_hook(_task):
        hook_calls.append(1)
        if len(hook_calls) == 1:
            raise RuntimeError("transient dispatch failure")
        return [], FileLockManager()

    def _stub_executor(exec_task, max_retries=5, timeout=None):
        executor_calls.append(exec_task.id)
        return True

    agent._pre_task_lock_hook = _flaky_hook
    agent._execute_task_with_retry = _stub_executor

    controller = _RecordingController()
    result = await agent._run_task_with_provider_slot(task, controller, None)

    assert result is True, "a recovered dispatch must report success"
    assert len(hook_calls) == 2, (
        f"the hook must be retried once, got {len(hook_calls)} calls"
    )
    assert executor_calls == [task.id], (
        "the executor must run EXACTLY ONCE — a failed dispatch attempt "
        f"must not double-execute, got {executor_calls!r}"
    )
    assert task.status == "pending", (
        "a recovered dispatch must not mark the task failed"
    )
    assert "task_dispatch_retry" in logger.event_names(), (
        "the retried attempt must be logged as task_dispatch_retry"
    )
    assert "task_dispatch_failed" not in logger.event_names(), (
        "a recovered dispatch must NOT be logged as task_dispatch_failed"
    )


# ---------------------------------------------------------------------------
# 3. The provider slot is released even when the dispatch never recovers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_exception_releases_provider_slot(tmp_path):
    """Every acquire is matched by a release, even on total failure."""
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    agent = _build_agent(project_dir)
    task = _register(agent, _make_task())

    def _always_fails(_task):
        raise RuntimeError("dispatch is broken")

    agent._pre_task_lock_hook = _always_fails

    controller = _RecordingController()
    result = await agent._run_task_with_provider_slot(task, controller, None)

    assert result is False
    assert controller.acquired, "the provider slot must have been taken"
    assert controller.released == controller.acquired, (
        "every acquired provider slot must be released: "
        f"acquired={controller.acquired!r} released={controller.released!r}"
    )


# ---------------------------------------------------------------------------
# 4. The attempt budget is operator-configurable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_max_attempts_env_override(tmp_path, monkeypatch):
    """``PDT_DISPATCH_MAX_ATTEMPTS`` bounds the attempts; one verdict."""
    monkeypatch.setenv("PDT_DISPATCH_MAX_ATTEMPTS", "3")

    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    agent = _build_agent(project_dir)
    task = _register(agent, _make_task())

    hook_calls: List[int] = []
    failures: List[str] = []
    original_record = agent.task_manager.record_task_failure

    def _counting_record(task_id, error):
        failures.append(error)
        original_record(task_id, error)

    def _always_fails(_task):
        hook_calls.append(1)
        raise RuntimeError("dispatch is broken")

    agent.task_manager.record_task_failure = _counting_record
    agent._pre_task_lock_hook = _always_fails

    controller = _RecordingController()
    result = await agent._run_task_with_provider_slot(task, controller, None)

    assert result is False
    assert len(hook_calls) == 3, (
        f"the env override must bound the attempts at 3, got {len(hook_calls)}"
    )
    assert len(failures) == 1, (
        f"the verdict must be recorded EXACTLY ONCE, got {len(failures)}"
    )
    assert failures[0].startswith("dispatch_error: ")
    assert task.status == "failed"


# ---------------------------------------------------------------------------
# 5. An executor exception is NOT a dispatch failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_exception_still_propagates(tmp_path):
    """``_execute_task_with_retry`` failures keep their own semantics."""
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    agent = _build_agent(project_dir)
    task = _register(agent, _make_task())

    failures: List[str] = []
    agent.task_manager.record_task_failure = (
        lambda task_id, error: failures.append(error)
    )

    def _boom(exec_task, max_retries=5, timeout=None):
        raise RuntimeError("executor exploded")

    agent._pre_task_lock_hook = lambda _task: ([], FileLockManager())
    agent._execute_task_with_retry = _boom

    controller = _RecordingController()

    with pytest.raises(RuntimeError, match="executor exploded"):
        await agent._run_task_with_provider_slot(task, controller, None)

    assert failures == [], (
        "the executor owns its own verdict — a dispatch guard must not "
        f"mark the task failed, got {failures!r}"
    )
    assert task.status == "pending"
    assert controller.released == controller.acquired, (
        "the provider slot must still be released on the way out"
    )


# ---------------------------------------------------------------------------
# 6. A cancelled run is not a dispatch failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_dispatch_is_not_converted_into_a_failure(tmp_path):
    """``asyncio.CancelledError`` must tear the run down, not retry it.

    A cancelled run is the operator pulling the plug.  Turning that
    into a "failed task" verdict would manufacture a failure the
    operator did not cause — and it would keep retrying a hook whose
    cancellation was deliberate.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    agent = _build_agent(project_dir)
    task = _register(agent, _make_task())

    failures: List[str] = []
    hook_calls: List[int] = []
    agent.task_manager.record_task_failure = (
        lambda task_id, error: failures.append(error)
    )

    def _cancelled(_task):
        hook_calls.append(1)
        raise asyncio.CancelledError()

    agent._pre_task_lock_hook = _cancelled

    controller = _RecordingController()

    with pytest.raises(asyncio.CancelledError):
        await agent._run_task_with_provider_slot(task, controller, None)

    assert len(hook_calls) == 1, (
        "a cancellation must not be retried, got "
        f"{len(hook_calls)} hook calls"
    )
    assert failures == [], (
        "a cancellation is not a dispatch failure, got " f"{failures!r}"
    )
    assert task.status == "pending"
    assert controller.released == controller.acquired, (
        "the provider slot must still be released on the way out"
    )


# ---------------------------------------------------------------------------
# 7. The knobs themselves
# ---------------------------------------------------------------------------


def test_dispatch_defaults_are_exported():
    """The two defaults are module-level and carry the documented values."""
    assert agent_module.DEFAULT_DISPATCH_MAX_ATTEMPTS == 2
    assert agent_module.DEFAULT_DISPATCH_RETRY_BACKOFF_SECONDS == 0.5


def test_dispatch_env_knobs_fall_back_on_garbage(tmp_path, monkeypatch):
    """A malformed env value must not disable the guard.

    ``PDT_DISPATCH_MAX_ATTEMPTS=0`` would mean "never attempt" — a task
    that could never run and could never fail either.  A typo in the
    operator's environment must fall back to the default instead.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    agent = _build_agent(project_dir)

    monkeypatch.setenv("PDT_DISPATCH_MAX_ATTEMPTS", "not-a-number")
    assert agent._dispatch_max_attempts() == agent_module.DEFAULT_DISPATCH_MAX_ATTEMPTS

    monkeypatch.setenv("PDT_DISPATCH_MAX_ATTEMPTS", "0")
    assert agent._dispatch_max_attempts() == agent_module.DEFAULT_DISPATCH_MAX_ATTEMPTS

    monkeypatch.setenv("PDT_DISPATCH_RETRY_BACKOFF_SECONDS", "nope")
    assert (
        agent._dispatch_retry_backoff()
        == agent_module.DEFAULT_DISPATCH_RETRY_BACKOFF_SECONDS
    )

    monkeypatch.setenv("PDT_DISPATCH_MAX_ATTEMPTS", "5")
    assert agent._dispatch_max_attempts() == 5

    monkeypatch.setenv("PDT_DISPATCH_RETRY_BACKOFF_SECONDS", "1.5")
    assert agent._dispatch_retry_backoff() == 1.5


def test_non_finite_backoff_falls_back_to_default(tmp_path, monkeypatch):
    """A non-finite (or negative) backoff must fall back to the default.

    ``inf`` is a number as far as ``float()`` is concerned, but it is
    not a wait: ``asyncio.sleep(inf)`` never returns, so the dispatch
    retry hangs forever and the task stays ``pending`` with an empty
    ``failure_reason``.  The whole layer is then held up by the
    ``asyncio.gather`` that waits for it — the exact accident this
    dispatch budget exists to prevent.

    ``nan`` is the same story from the other side: it compares false
    against every bound, so it must not be mistaken for a value either.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    agent = _build_agent(project_dir)

    default = agent_module.DEFAULT_DISPATCH_RETRY_BACKOFF_SECONDS
    for raw in ("inf", "Inf", "infinity", "1e400", "nan", "-1"):
        monkeypatch.setenv("PDT_DISPATCH_RETRY_BACKOFF_SECONDS", raw)
        got = agent._dispatch_retry_backoff()
        assert got == default, (
            f"{raw!r} is not a finite wait and must fall back to the "
            f"default {default!r}, got {got!r}"
        )


def test_finite_backoff_values_still_honoured(tmp_path, monkeypatch):
    """Rejecting the non-finite values must not break the legal ones.

    Zero stays legal — "retry immediately" is a legitimate request —
    and a padded value still parses, because the knob is read with
    ``.strip()``.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    agent = _build_agent(project_dir)

    for raw, want in (("0", 0.0), ("0.5", 0.5), ("1.5", 1.5), (" 2 ", 2.0)):
        monkeypatch.setenv("PDT_DISPATCH_RETRY_BACKOFF_SECONDS", raw)
        got = agent._dispatch_retry_backoff()
        assert got == want, f"{raw!r} must yield {want!r}, got {got!r}"


@pytest.mark.asyncio
async def test_infinite_backoff_env_does_not_hang_dispatch(tmp_path, monkeypatch):
    """An ``inf`` backoff must not park the dispatch in an endless sleep.

    Walks the SAME path as
    :func:`test_directory_declaration_fails_task_and_records_reason` —
    a directory in ``files_to_modify`` makes the pre-task lock hook
    raise — with ``PDT_DISPATCH_RETRY_BACKOFF_SECONDS=inf`` in the
    environment.  The dispatch must still converge to the terminal
    verdict, and it must do so well inside the timeout.
    """
    project_dir = tmp_path / "project"
    (project_dir / "backend" / ".venv").mkdir(parents=True)

    monkeypatch.setenv("PDT_DISPATCH_RETRY_BACKOFF_SECONDS", "inf")

    logger = _RecordingLogger()
    agent = _build_agent(project_dir, logger=logger)
    task = _register(agent, _make_task(files=["backend/.venv"]))

    controller = _RecordingController()
    result = await asyncio.wait_for(
        agent._run_task_with_provider_slot(task, controller, None),
        timeout=10,
    )

    assert result is False, "a dispatch failure must not report success"
    assert task.status == "failed", (
        f"the task must reach a terminal status, got {task.status!r}"
    )
    assert task.failure_reason is not None
    assert task.failure_reason.startswith(
        "dispatch_error: files_to_modify must name files"
    ), (
        "the failure_reason must carry the ORIGINAL hook message behind "
        f"the dispatch_error prefix, got {task.failure_reason!r}"
    )
    assert "task_dispatch_failed" in logger.event_names(), (
        "the terminal dispatch failure must be logged as task_dispatch_failed"
    )
