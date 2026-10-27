"""The task's session binding is written BEFORE the dispatch can block.

Background
----------
2026-10-26. The ``task_session_bound`` event is the only durable record
of which session a task was bound to. The stop path reads it out of the
plan's ``execution.log`` (``pause_state.collect_task_sessions``) and
re-opens that session when the operator resumes the run.

The event used to be written only AFTER ``query_in_session`` returned.
A dispatch that blocks — a provider that hangs, a CLI that waits for
input — therefore left the log with NO binding at all, so a stop
landing in that window could only answer ``no_session_id`` and fall
back to a full re-run of the plan.

The fix: the binding is written before the dispatch starts, so a log
read while the dispatch is still in flight already names the session
the task is bound to. The record written after the dispatch returns is
still written — a provider fallback may have switched the id, and the
reader takes the LAST binding for a task — so the two records have the
same shape (``_log_task_session_bound`` writes both).

TDD spec
--------
* ``test_binding_exists_while_dispatch_blocks`` — while the dispatch is
  blocked, the execution log already names this task's session, and
  the id is not empty.
* ``test_binding_precedes_task_completion`` — after the block is
  released and the task completes, the binding is still in the log and
  is still the one the production reader resolves.
* ``test_provider_switch_is_covered_by_the_last_binding`` — a provider
  fallback that answers under a different session id is covered by the
  LAST binding, so a resume re-opens the session the answer came from.

Harness
-------
A copy of ``test_agent_execute_task.py``'s ``_run_end_to_end``
structure — a private git repo, a fake CC Switch database behind a
private ``$HOME``, a pre-baked ``tasks.json``, and a mocked coding tool
so no LLM is called — with two differences that matter here:

* the logger is a REAL ``ExecutionLogger`` writing a REAL
  ``execution.log``, because the thing under test is what that file
  contains at a given moment; and
* the dispatch is a fake that BLOCKS on an event, so the test can read
  the log from disk while the dispatch is still in flight — the exact
  window a stop used to land in.

The test file does not import ``test_agent_execute_task``: the harness
is copied, not shared, so a change to one cannot silently change the
other.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Make ``agent.py`` and its siblings importable in isolation.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: The task id the harness writes into ``tasks.json``.
TASK_ID = "1"

#: The plan directory name the harness gives the project. The agent
#: derives its plan id from the tasks file's parent, so this name is
#: also what the pinned session id is derived from.
PROJECT_DIR_NAME = "bound-before-dispatch-proj"

#: The plan id used for the execution log. It is NOT the agent's plan
#: id: the logger only needs a directory to write into, and keeping the
#: two apart makes it obvious in a failure which file was read.
LOG_PLAN_ID = "bound-before-dispatch-log"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


# Provider fixture used by every test. Two providers so the first-match-wins
# semantic of ``_load_provider_info`` is exercised end-to-end.
_FAKE_PROVIDERS = {
    "vendor-a-pro": {
        "base_url": "https://api.vendor-a.example/anthropic",
        "api_key": "sk-cp-fake-vendor-a-key-1234567890",
    },
    "vendor-b": {
        "base_url": "https://api.vendor-b.example/anthropic",
        "api_key": "a8ff.fake-vendor-b-key.1234567890",
    },
}


def _git_init(project_dir: Path) -> None:
    """Run a real ``git init`` so GitManager(search_parent_directories=True)
    finds the repo at the leaf — not a sibling checkout's ancestor repo.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    if not (project_dir / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=True,
        )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "Bound Before Dispatch Test"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
    )


def _install_fake_cc_switch_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Build a private ``~/.cc-switch/cc-switch.db`` and redirect
    ``$HOME`` to it for the test.
    """
    import sqlite3

    fake_home = tmp_path / "home"
    cc_dir = fake_home / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path = cc_dir / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS provider_configs ("
        "id TEXT PRIMARY KEY, url TEXT, model TEXT, extra_params TEXT"
        ")"
    )
    for provider_id, cfg in _FAKE_PROVIDERS.items():
        extra = json.dumps({"ANTHROPIC_AUTH_TOKEN": cfg["api_key"]})
        conn.execute(
            "INSERT INTO provider_configs (id, url, model, extra_params) "
            "VALUES (?, ?, ?, ?)",
            (provider_id, cfg["base_url"], "fake-model", extra),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HOME", str(fake_home))
    return db_path


def _write_tasks_file(project_dir: Path, test_command: str) -> Path:
    """Write a ``tasks.json`` with one pending task.

    The shape matches what ``TaskManager.save_tasks`` writes so the
    agent's ``load_tasks`` path is exercised end-to-end.
    """
    tasks_payload = {
        "requirement": "test task_session_bound before dispatch",
        "stop_reason": None,
        "reason_detail": None,
        "tasks": [
            {
                "id": TASK_ID,
                "title": "Implement the add helper",
                "description": "Write impl.py so the test suite passes.",
                "test_command": test_command,
                "test_commands": [],
                "status": "pending",
                "updated_time": None,
                "failure_reason": None,
                "project_dir": None,
                "model_type": "medium",
            }
        ],
    }
    tasks_file = project_dir / "tasks.json"
    tasks_file.write_text(json.dumps(tasks_payload, indent=2), encoding="utf-8")
    return tasks_file


def _write_test_file(project_dir: Path) -> Path:
    """Write the test the task's ``test_command`` runs.

    It fails until ``impl.py`` exists, which is what the fake dispatch
    writes. That keeps the run honest: the pre-flight check cannot
    skip the task (the test is red before the dispatch), and the
    cross-verification after the dispatch sees it green.
    """
    test_file = project_dir / "test_impl.py"
    test_file.write_text(
        "from impl import add\n"
        "\n"
        "\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    return test_file


def _install_claude_query_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``ClaudeCodingTool.query`` / ``query_json``.

    The dispatch itself is faked (see ``_install_blocking_dispatch``),
    but other code paths on the run reach for the coding tool too, and
    none of them may call a real model.
    """
    from coding_tool import ClaudeCodingTool

    monkeypatch.setattr(
        ClaudeCodingTool, "query",
        lambda self, *args, **kwargs: "TEST_RESULT: PASSED\n",
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "query_json",
        lambda self, *args, **kwargs: {"tasks": []},
    )


def _read_binding_entries(
    log_file: Path, task_id: str = TASK_ID
) -> List[Dict[str, Any]]:
    """The ``task_session_bound`` entries for ``task_id`` in the log.

    Read straight off disk, one JSON line at a time, so the answer is
    what a reader of ``execution.log`` would find — not what the
    logger believes it wrote. Malformed lines are skipped, exactly as
    ``pause_state`` skips them.
    """
    entries: List[Dict[str, Any]] = []
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("event") != "task_session_bound":
                    continue
                if str(entry.get("task_id")) != str(task_id):
                    continue
                entries.append(entry)
    except FileNotFoundError:
        return []
    return entries


class _BlockedDispatch:
    """The state of one run whose dispatch is blocked on an event."""

    def __init__(self, log_file: Path, project_dir: Path):
        self.log_file = log_file
        self.project_dir = project_dir
        #: Set by the fake dispatch once it has been ENTERED — that is,
        #: once the dispatch is in flight and has not returned yet.
        self.entered = threading.Event()
        #: Set by the test to let the blocked dispatch return.
        self.release = threading.Event()
        #: The bindings the log held at the moment the dispatch was
        #: entered. This is the "while it blocks" snapshot.
        self.entry_snapshots: List[List[Dict[str, Any]]] = []
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[BaseException] = None
        self.finished = threading.Event()

    def release_and_join(self, timeout: float = 120.0) -> None:
        """Let the blocked dispatch finish and wait for the run."""
        self.release.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)
        self.finished.wait(timeout=timeout)
        if self.error is not None:
            raise self.error


def _install_blocking_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    state: _BlockedDispatch,
    switch_session_id: Optional[str] = None,
) -> None:
    """Replace the dispatch with one that blocks, then answers.

    The fake reads the execution log at its ENTRY — before it returns
    anything — and records what it saw. Whatever is in that snapshot
    was already in the log while the dispatch was in flight.

    It then blocks until the test releases it, so the test can read
    the log from disk in the middle of a dispatch that has not
    returned. That is the window a stop used to land in.

    ``switch_session_id`` simulates a provider fallback: the dispatch
    answers under a session id other than the one it was ASKED for.
    The handle is the same object the agent holds, so this is exactly
    what a fallback does to it.
    """
    import agent as agent_module

    def fake_query_in_session(tool, session, /, *args, **kwargs):
        state.entry_snapshots.append(
            _read_binding_entries(state.log_file, TASK_ID)
        )
        state.entered.set()
        if not state.release.wait(120):
            raise AssertionError(
                "the blocked dispatch was never released by the test"
            )
        if switch_session_id:
            # A provider fallback: the answer comes back under a
            # different id than the one the dispatch was asked for.
            session.session_id = switch_session_id
        # The "implementation" the task owes: the test file goes green
        # only after this exists.
        (state.project_dir / "impl.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        return "TEST_RESULT: PASSED\n"

    monkeypatch.setattr(
        agent_module, "query_in_session", fake_query_in_session
    )


def _start_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    switch_session_id: Optional[str] = None,
) -> _BlockedDispatch:
    """Start one run whose dispatch blocks, and return its state.

    The run happens on a background thread so the test thread is free
    to look at the log while the dispatch is in flight.
    """
    from execution_logger import ExecutionLogger

    plans_dir = tmp_path / "plans"
    logger = ExecutionLogger(LOG_PLAN_ID, plans_dir=plans_dir)
    log_file = plans_dir / LOG_PLAN_ID / "execution.log"

    project_dir = tmp_path / PROJECT_DIR_NAME
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    test_command = f"{sys.executable} -m pytest -q test_impl.py"
    _write_tasks_file(project_dir, test_command)
    _write_test_file(project_dir)
    _install_claude_query_mock(monkeypatch)
    # Pin the provider chain so the test does not depend on the
    # production ``provider-order.json`` or on the time-of-day
    # vendor-b peak-hour degradation rule.
    monkeypatch.setenv("PDT_PROVIDER_PRIORITY", "vendor-a-pro,vendor-b")
    # No recovery mapping: this is a fresh run, so the session id is
    # derived and pinned rather than resumed.
    monkeypatch.delenv("PDT_RESUME_SESSIONS", raising=False)

    state = _BlockedDispatch(log_file=log_file, project_dir=project_dir)
    _install_blocking_dispatch(monkeypatch, state, switch_session_id)

    import agent as agent_module

    def _run() -> None:
        try:
            agent_module.autonomous_coding(
                requirement="test task_session_bound before dispatch",
                project_dir=str(project_dir),
                recover=True,
                max_tasks=None,
                config_name="coding",
                tool="claude",
                logger=logger,
            )
        except BaseException as exc:  # noqa: BLE001 — reported to the test
            state.error = exc
        finally:
            state.finished.set()

    state.thread = threading.Thread(
        target=_run, name="bound-before-dispatch", daemon=True
    )
    state.thread.start()
    return state


def _read_persisted_task_statuses(plan_id: str) -> Dict[str, str]:
    """Read per-task statuses from the SQLite ``plan_tasks`` mirror.

    ``save_tasks`` strips ``status`` from the on-disk ``tasks.json``,
    so after the run the only persisted per-task status is the
    ``plan_tasks`` row. The per-test hermetic DB path comes from the
    ``PDT_STATE_DB_PATH`` env var the conftest sets.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = Path(os.environ["PDT_STATE_DB_PATH"])
    conn = open_db(db_path)
    try:
        migrate(conn)
        rows = conn.execute(
            "SELECT task_id, status FROM plan_tasks WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return {task_id: status for task_id, status in rows}


# ---------------------------------------------------------------------------
# 2 TDD tests
# ---------------------------------------------------------------------------


def test_binding_exists_while_dispatch_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """While the dispatch is blocked, the log already names the session.

    The failure this pins: the binding was written only after
    ``query_in_session`` returned, so a dispatch that blocked left the
    log empty of bindings. A stop in that window could only answer
    ``no_session_id`` — and re-ran the whole plan.

    The assertion is made against the file on disk while the dispatch
    is in flight, not against the logger's own idea of what it wrote.
    """
    state = _start_run(monkeypatch, tmp_path)
    try:
        assert state.entered.wait(120), (
            "the dispatch never started — the run did not reach the "
            "coding tool at all"
        )
        # The dispatch is in flight right now: it has been entered and
        # has not returned. Read the log from disk.
        entries = _read_binding_entries(state.log_file, TASK_ID)
        assert entries, (
            "the execution log holds no task_session_bound entry for "
            f"task {TASK_ID} while the dispatch is blocked — the binding "
            "is written after the dispatch returns, so a stop in this "
            "window has no session to resume. Log contents: "
            f"{state.log_file.read_text(encoding='utf-8') if state.log_file.exists() else '<no log file>'}"
        )
        binding = entries[-1]
        data = binding.get("data")
        assert isinstance(data, dict), (
            f"the binding must carry a data mapping, got {data!r}"
        )
        session_id = data.get("session_id")
        assert isinstance(session_id, str) and session_id.strip(), (
            "the binding must name the session the dispatch runs under; "
            f"got {session_id!r}"
        )
        assert data.get("attempt") == 1, (
            f"the binding must carry the attempt it belongs to; got "
            f"{data.get('attempt')!r}"
        )
        assert data.get("stage") == "pinned", (
            "a fresh run derives its session id and pins it, so the "
            f"binding must say so; got {data.get('stage')!r}"
        )
        # The production reader resolves the same id out of the same
        # file — that is the reader a resume uses.
        from pause_state import collect_task_sessions

        resolved = collect_task_sessions(state.log_file)
        assert resolved.get(TASK_ID) == session_id, (
            "the resume reader does not resolve the binding that was "
            f"written before the dispatch: {resolved!r}"
        )
        # And the snapshot the dispatch itself took at its entry agrees.
        assert state.entry_snapshots, "the dispatch was never entered"
        assert state.entry_snapshots[0], (
            "the dispatch's own entry snapshot holds no binding — the "
            "binding was not in the log when the dispatch started"
        )
    finally:
        state.release_and_join()


def test_binding_precedes_task_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After the block is released and the task completes, the binding stays.

    Two things are pinned here. First, the binding survives the
    dispatch: the record written before the dispatch and the record
    written after it have the same shape, and the reader takes the
    LAST one — so the id a resume re-opens is the id the dispatch
    actually used. Second, the task really completed, so the binding
    is not the record of an abandoned attempt.
    """
    state = _start_run(monkeypatch, tmp_path)
    try:
        assert state.entered.wait(120), (
            "the dispatch never started — the run did not reach the "
            "coding tool at all"
        )
        state.release_and_join()
        assert state.error is None, (
            f"the run raised: {state.error!r}"
        )
        # The task completed — the persisted status is the truth (the
        # on-disk tasks.json strips it).
        statuses = _read_persisted_task_statuses(PROJECT_DIR_NAME)
        assert statuses.get(TASK_ID) == "completed", (
            f"expected persisted status=completed for task {TASK_ID}, "
            f"got {statuses!r}"
        )
        # The binding is still in the log, and it is still the one the
        # reader resolves.
        entries = _read_binding_entries(state.log_file, TASK_ID)
        assert entries, (
            "the execution log holds no task_session_bound entry for "
            f"task {TASK_ID} after the task completed"
        )
        binding = entries[-1]
        data = binding.get("data") or {}
        session_id = data.get("session_id")
        assert isinstance(session_id, str) and session_id.strip(), (
            f"the last binding must name a session; got {session_id!r}"
        )
        from pause_state import collect_task_sessions

        resolved = collect_task_sessions(state.log_file)
        assert resolved.get(TASK_ID) == session_id, (
            "the resume reader does not resolve the binding that "
            f"survived the completion: {resolved!r}"
        )
    finally:
        state.release.set()
        if state.thread is not None:
            state.thread.join(timeout=30)


def test_provider_switch_is_covered_by_the_last_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A provider switch is covered by the LAST binding, not the first.

    The record written before the dispatch names the id the dispatch was
    ASKED for (the pinned one). A provider fallback may answer under a
    different id, and the record written after the dispatch returns
    names THAT one. The reader takes the last binding for a task, so a
    resume re-opens the session the answer actually came from — which
    is the whole point of recording the binding twice.

    Pinning this here keeps the early write from being "the only
    record": the moment the early record existed alone, a provider
    switch would have been recorded as the pinned id and the resume
    would have re-opened a conversation that never said anything.
    """
    from plan_sessions import stable_session_id

    switched_id = "b1e1d0c2-0000-4000-8000-000000000001"
    pinned_id = stable_session_id(PROJECT_DIR_NAME, f"task:{TASK_ID}")

    state = _start_run(monkeypatch, tmp_path, switch_session_id=switched_id)
    try:
        assert state.entered.wait(120), (
            "the dispatch never started — the run did not reach the "
            "coding tool at all"
        )
        state.release_and_join()
        assert state.error is None, f"the run raised: {state.error!r}"

        entries = _read_binding_entries(state.log_file, TASK_ID)
        assert len(entries) >= 2, (
            "the run must record the binding twice — once before the "
            f"dispatch and once after it; got {len(entries)} record(s): "
            f"{entries!r}"
        )
        # The FIRST record names the id the dispatch was asked for.
        first = entries[0].get("data") or {}
        assert first.get("session_id") == pinned_id, (
            "the binding written before the dispatch must name the "
            f"pinned id {pinned_id!r}; got {first.get('session_id')!r}"
        )
        # The LAST record names the id the dispatch actually used.
        last = entries[-1].get("data") or {}
        assert last.get("session_id") == switched_id, (
            "the binding written after the dispatch must name the id the "
            f"dispatch actually used ({switched_id!r}); got "
            f"{last.get('session_id')!r}"
        )
        # And the reader resolves the switched id: it takes the LAST
        # binding for a task.
        from pause_state import collect_task_sessions

        resolved = collect_task_sessions(state.log_file)
        assert resolved.get(TASK_ID) == switched_id, (
            "the resume reader must resolve the id the dispatch actually "
            f"used, not the one it was asked for: {resolved!r}"
        )
    finally:
        state.release.set()
        if state.thread is not None:
            state.thread.join(timeout=30)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
