"""A write the task never declared must FAIL the task, not complete it.

Material R1, round 1 — the negative control
--------------------------------------------

The Edit/Write guard refuses a write to a file the task did not declare
and tells the sub-agent so. The accident this file pins down is what
happens when the sub-agent does NOT go through that guard: it runs
``cat > scripts/ci_gate.sh`` in a shell, the file lands in the working
tree, and nothing in the executor looked at the repository's change set
— only at the tool calls. The task was recorded ``completed`` with an
undeclared file in the tree.

The RULE that judges the change set lives in
:func:`agent.excess_declared_paths` (a pure comparison, pinned by
``test_undeclared_write_gate_paths.py``). This file pins the EXECUTOR
side of it: before a task may complete, the actual change set is
compared against ``files_to_modify``, and a write outside the
declaration fails the task with a message that names the path.

These cases drive the real ``_execute_task_with_retry`` against a real
(tmp) git repository, because a gate that only exists in a copy of the
branch cannot be observed — the same lesson
``test_audit_veto_gates_completion.py`` records for the audit veto.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: The undeclared path the fake sub-agent writes. It is the shape from
#: the accident: a shell redirection into a script the task never
#: declared.
UNDECLARED_PATH = "scripts/ci_gate.sh"

#: The only path the task declares.
DECLARED_PATH = "docs/readme.md"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tiny git repository holding one committed, declared file."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "readme.md").write_text("original\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "gate@example.com")
    _git(root, "config", "user.name", "undeclared write gate")
    _git(root, "add", DECLARED_PATH)
    _git(root, "commit", "-q", "-m", "base")
    return root


def _make_task(declaration=None):
    """A task that declares exactly one file (unless told otherwise)."""
    from task import SubTask

    return SubTask(
        # Unique per run: the Edit/Write guard's refusal marker is keyed
        # by task id and lives in the system temp dir, so a fixed id
        # would let one run's marker leak into the next one's diagnosis.
        id=f"undeclared-write-gate-{uuid.uuid4().hex[:10]}",
        title="Extend the documentation",
        description="Update the documentation to describe the new flag.",
        test_command="",
        files_to_modify=(
            [DECLARED_PATH] if declaration is None else list(declaration)
        ),
    )


def _make_agent(repo: Path, *, writes: dict[str, str]):
    """An ``AutonomousAgent`` skeleton whose sub-agent writes ``writes``.

    The fake coding tool writes the files DIRECTLY (``Path.write_text``),
    which is what a shell redirection looks like from the executor's
    side: no Edit/Write tool call, so the tool guard never sees it and
    never refuses it. That is the whole point of the case.
    """
    from agent import AutonomousAgent
    from git_manager import GitManager

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = repo
    agent.subagent_cfg = None
    agent.logger = MagicMock()
    agent.config = MagicMock()
    agent.config.executor_system_prompt = "fake prompt"
    agent.executor = MagicMock()
    agent.executor.had_previous_timeout.return_value = False
    agent.retry_manager = MagicMock()
    agent.retry_manager.get_retry_prompt_modifier.return_value = ""
    agent.task_manager = MagicMock()
    agent.task_manager.tasks_file = (
        Path(tempfile.mkdtemp(prefix="pdt-undeclared-write-gate-"))
        / "tasks.json"
    )
    # No split happened, so the failure is recorded as a failure and not
    # as a hand-over to children.
    agent.task_manager.record_task_superseded.return_value = False
    agent.plan_id = "pdt-undeclared-write-gate"
    agent.rollback_manager = MagicMock()
    agent._session_task_completed_counts = {}
    agent._failure_occurrences = {}
    agent._refinement_count = 0

    # A REAL git manager over the tmp repository: the executor's own
    # "which files did the sub-agent change" answer has to come from the
    # repository, not from a fixture that agrees with the expectation.
    agent.git_manager = GitManager(str(repo))

    def _fake_query(*_args, **_kwargs) -> str:
        for rel, content in writes.items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return "Done.\nTEST_RESULT: PASSED"

    agent.coding_tool = MagicMock()
    agent.coding_tool.query.side_effect = _fake_query

    # Everything around the gate under test is neutralised so a failure
    # can only come from the undeclared write.
    agent._preflight_test_command_skip = MagicMock(return_value=False)
    agent._parse_test_result = MagicMock(return_value=(True, ""))
    agent._cross_verify_test_result = MagicMock(return_value=(True, ""))
    agent._task_declared_files_exist = MagicMock(return_value=True)
    agent._get_git_diff_stat_for_review = MagicMock(return_value="")
    agent._refine_after_failure = MagicMock()
    agent._persist_task_status = MagicMock()
    agent._commit_task_changes = MagicMock()
    agent._audit_task_second_pass = MagicMock(return_value=(True, ""))
    return agent


def _completed(agent, task) -> bool:
    """Whether the run marked ``task`` ``completed``."""
    return any(
        call.args[:2] == (task.id, "completed")
        for call in agent.task_manager.update_task_status.call_args_list
    )


def _recorded_failures(agent) -> list[str]:
    """Every failure text the run recorded, in the order it recorded them."""
    texts: list[str] = []
    for call in agent.task_manager.record_task_failure.call_args_list:
        if len(call.args) >= 2 and isinstance(call.args[1], str):
            texts.append(call.args[1])
    for call in agent.retry_manager.record_attempt.call_args_list:
        if len(call.args) >= 2 and isinstance(call.args[1], str):
            texts.append(call.args[1])
    return texts


def test_a_shell_write_of_an_undeclared_file_fails_the_task(repo: Path):
    """A write outside the declaration must fail the task.

    Before the gate this task was recorded ``completed`` — the sub-agent
    wrote ``scripts/ci_gate.sh`` with a shell redirection, the tool
    guard never saw a tool call, and nothing compared the repository's
    change set against ``files_to_modify``.
    """
    agent = _make_agent(repo, writes={UNDECLARED_PATH: "#!/bin/sh\nexit 0\n"})
    task = _make_task()

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is False, (
        "the sub-agent wrote a file the task never declared and the task "
        "still reported success — the change set is not being compared "
        "against files_to_modify"
    )
    assert not _completed(agent, task), (
        "a task that wrote an undeclared file must never be marked completed"
    )
    assert agent._commit_task_changes.call_count == 0, (
        "no git checkpoint may be written for a task that wrote outside "
        "its declaration"
    )


def test_the_failure_names_the_undeclared_path(repo: Path):
    """The failure text must name the path, in the executor's own spelling.

    ``undeclared_write: <paths>`` is the spelling
    ``failure_classification`` parses (see
    ``_UNDECLARED_WRITE_RE``), and the path is what lets the refiner
    write a task whose declaration actually covers the file.
    """
    agent = _make_agent(repo, writes={UNDECLARED_PATH: "#!/bin/sh\nexit 0\n"})
    task = _make_task()

    agent._execute_task_with_retry(task, max_retries=1)

    failures = _recorded_failures(agent)
    assert failures, (
        "the failed task recorded no failure text at all — the reason "
        "the write was refused never reached the task record"
    )
    matching = [f for f in failures if f.startswith("undeclared_write:")]
    assert matching, (
        "no recorded failure text starts with the executor's own "
        f"undeclared_write spelling; recorded: {failures!r}"
    )
    assert any(UNDECLARED_PATH in f for f in matching), (
        f"the failure text does not name the undeclared path; recorded: "
        f"{matching!r}"
    )


def test_a_declared_change_still_completes(repo: Path):
    """The gate must not fire on a change the task DID declare."""
    agent = _make_agent(repo, writes={DECLARED_PATH: "updated\n"})
    task = _make_task()

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is True, (
        "a change inside the declaration must still complete the task"
    )
    assert _completed(agent, task), (
        "a task whose only change was declared must be marked completed"
    )


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param([], id="empty-list"),
        pytest.param(["__UNKNOWN_MODIFICATIONS__"], id="unknown-sentinel"),
        pytest.param(["__NO_FILE_CHANGES__"], id="no-changes-sentinel"),
    ],
)
def test_an_unusable_declaration_skips_the_check(repo: Path, declaration):
    """A task that declared nothing usable is not judged by the gate.

    "The task declared nothing" and "the task declared the empty set"
    are different statements, and only the second one would make every
    changed path an undeclared write. Treating them alike would make
    the gate fire on every task whose ``files_to_modify`` was never
    filled in — the exact task shape the runtime already treats as
    read-only.
    """
    agent = _make_agent(repo, writes={UNDECLARED_PATH: "#!/bin/sh\nexit 0\n"})
    task = _make_task(declaration)

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is True, (
        "a task whose declaration cannot be used must not be failed by "
        "the undeclared-write gate"
    )
