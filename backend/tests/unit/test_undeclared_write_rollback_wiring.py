"""An undeclared write is rolled back — and the task still fails.

What this pins
--------------
The undeclared-write gate (material R1) already FAILED a task that wrote a
file it never declared.  Failing it is not enough: the overstep is still
**in the tree**.  The next attempt starts from a tree that carries the
previous attempt's overstep, the next reviewer sees a file nobody
declared, and the change set the attempt is judged by is no longer the
change set this attempt produced.

So the overstep has to be *undone*, and undone in a way that can be
checked afterwards:

* the undeclared file is gone from the working tree;
* the declared file the same attempt wrote is **kept** — the rollback
  undoes the overstep, not the task's own work;
* a **patch** of what was rolled back is written under the plan's
  ``.pdt/evidence/rollbacks/<task id>/<attempt>.patch`` — the content
  has to be captured *before* the tree is put back, because a patch is
  the difference between two states and once the tree is put back that
  difference no longer exists;
* the failure text names both the offending path and the patch, and
  keeps the ``undeclared_write:`` prefix ``failure_classification``
  parses;
* the task still fails and is still not committed.

The refusal marker of the Edit/Write guard is a *separate* channel: the
guard refuses a write the sub-agent attempted through a tool, the gate
catches the ones that arrived by shell.  When both happened, the text
the guard produced must not overwrite the text that names the patch.

These cases drive the real ``_execute_task_with_retry`` against a real
(tmp) git repository, because a rollback that only exists in a copy of
the branch cannot be observed.
"""

from __future__ import annotations

import subprocess
import sys
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


@pytest.fixture()
def plan_dir(tmp_path: Path) -> Path:
    """The plan directory — ``tasks.json``'s parent, and the state root."""
    plan = tmp_path / "plan"
    plan.mkdir(parents=True, exist_ok=True)
    return plan


def _make_task():
    """A task that declares exactly one file.

    The id is unique per run: the Edit/Write guard's refusal marker is
    keyed by task id and lives in the system temp dir, so a fixed id
    would let one run's marker leak into the next one's diagnosis.
    """
    from task import SubTask

    return SubTask(
        id=f"repair-{uuid.uuid4().hex[:8]}",
        title="Extend the documentation",
        description="Update the documentation to describe the new flag.",
        test_command="",
        files_to_modify=[DECLARED_PATH],
    )


def _make_agent(repo: Path, plan_dir: Path, *, writes: dict[str, str]):
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
    agent.task_manager.tasks_file = plan_dir / "tasks.json"
    # No split happened, so the failure is recorded as a failure and not
    # as a hand-over to children.
    agent.task_manager.record_task_superseded.return_value = False
    agent.plan_id = "pdt-undeclared-write-rollback"
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

    # Everything around the rollback under test is neutralised so a
    # failure can only come from the undeclared write.
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


def _recorded_failures(agent) -> list[str]:
    """Every failure text the run recorded, in the order it recorded them."""
    texts: list[str] = []
    for call in agent.task_manager.record_task_failure.call_args_list:
        if len(call.args) >= 2 and isinstance(call.args[1], str):
            texts.append(call.args[1])
    return texts


def _events(agent, name: str) -> list[dict]:
    """The ``data`` of every logged event called ``name``."""
    calls = (
        agent.logger.info.call_args_list
        + agent.logger.warning.call_args_list
        + agent.logger.error.call_args_list
    )
    out: list[dict] = []
    for call in calls:
        if call.args and call.args[0] == name:
            out.append(call.kwargs.get("data") or {})
    return out


def test_undeclared_write_rolls_back_and_still_fails(repo: Path, plan_dir: Path):
    """The overstep is undone, and the task still fails without a commit."""
    agent = _make_agent(
        repo, plan_dir, writes={UNDECLARED_PATH: "#!/bin/sh\nexit 0\n"}
    )
    task = _make_task()

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is False, (
        "the sub-agent wrote a file the task never declared and the task "
        "still reported success"
    )
    assert not (repo / UNDECLARED_PATH).exists(), (
        "the undeclared file is still in the working tree — the overstep "
        "was reported but never undone, so the next attempt starts from a "
        "tree that carries it"
    )
    assert agent._commit_task_changes.call_count == 0, (
        "no git checkpoint may be written for a task that wrote outside "
        "its declaration"
    )
    commits = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert commits == "1", (
        f"the repository holds {commits} commits — the overstep was "
        "committed even though the task failed"
    )


def test_declared_change_is_kept(repo: Path, plan_dir: Path):
    """The rollback undoes the overstep, not the task's own work."""
    agent = _make_agent(
        repo,
        plan_dir,
        writes={
            DECLARED_PATH: "updated\n",
            UNDECLARED_PATH: "#!/bin/sh\nexit 0\n",
        },
    )
    task = _make_task()

    agent._execute_task_with_retry(task, max_retries=1)

    assert (repo / DECLARED_PATH).read_text(encoding="utf-8") == "updated\n", (
        "the declared file was rolled back — the rollback threw away the "
        "task's own work along with the overstep"
    )
    assert not (repo / UNDECLARED_PATH).exists()


def test_failure_text_names_patch_and_paths(repo: Path, plan_dir: Path):
    """The failure names the offending path AND the evidence, and keeps
    the spelling ``failure_classification`` parses."""
    from failure_classification import classify_failure

    agent = _make_agent(
        repo, plan_dir, writes={UNDECLARED_PATH: "#!/bin/sh\nexit 0\n"}
    )
    task = _make_task()

    agent._execute_task_with_retry(task, max_retries=1)

    failures = _recorded_failures(agent)
    assert failures, (
        "the failed task recorded no failure text at all — the reason the "
        "write was refused never reached the task record"
    )
    text = failures[-1]
    assert text.startswith("undeclared_write:"), (
        "the failure text lost the executor's own undeclared_write "
        f"spelling; recorded: {text!r}"
    )
    assert UNDECLARED_PATH in text, (
        f"the failure text does not name the undeclared path; recorded: "
        f"{text!r}"
    )
    assert "rollback_patch:" in text, (
        "the failure text does not say where the evidence patch is; "
        f"recorded: {text!r}"
    )
    # The prefix is not decoration: it is what the classification reads,
    # and a text the classifier no longer recognises as an undeclared
    # write would be split like a code problem.
    assert classify_failure(text, project_dir=repo).kind == "undeclared_write", (
        "the failure text is no longer classified as an undeclared write; "
        f"recorded: {text!r}"
    )


def test_patch_exists_under_plan_state(repo: Path, plan_dir: Path):
    """The evidence patch lands under the plan's ``.pdt/`` state root."""
    agent = _make_agent(
        repo, plan_dir, writes={UNDECLARED_PATH: "#!/bin/sh\nexit 0\n"}
    )
    task = _make_task()

    agent._execute_task_with_retry(task, max_retries=1)

    patch = plan_dir / ".pdt" / "evidence" / "rollbacks" / task.id / "1.patch"
    assert patch.exists(), (
        f"no rollback evidence was written at {patch} — the content of the "
        "overstep is lost once the tree is put back"
    )
    body = patch.read_text(encoding="utf-8")
    assert UNDECLARED_PATH in body, (
        f"the evidence patch does not mention the rolled-back path; "
        f"content: {body!r}"
    )


def test_rollback_event_is_logged(repo: Path, plan_dir: Path):
    """The rollback is reported as an event of its own."""
    agent = _make_agent(
        repo, plan_dir, writes={UNDECLARED_PATH: "#!/bin/sh\nexit 0\n"}
    )
    task = _make_task()

    agent._execute_task_with_retry(task, max_retries=1)

    events = _events(agent, "task_attempt_rollback")
    assert events, (
        "no task_attempt_rollback event was logged — a rollback that "
        "leaves no trace cannot be reviewed"
    )
    data = events[0]
    assert data.get("attempt") == 1, f"unexpected attempt number: {data!r}"
    assert data.get("rolled_back") == [UNDECLARED_PATH], (
        f"the event does not name what was rolled back: {data!r}"
    )
    assert data.get("failed") == [], (
        f"the event reports paths that could not be rolled back: {data!r}"
    )
    assert str(data.get("patch_path") or "").endswith(
        f"{task.id}/1.patch"
    ), f"the event does not point at the evidence patch: {data!r}"


def test_tool_refusal_marker_does_not_drop_patch_line(
    repo: Path, plan_dir: Path, monkeypatch,
):
    """The guard's refusal marker must not overwrite the rollback text.

    The two channels are different facts: the guard refuses a write the
    sub-agent attempted through a tool, the gate catches the ones that
    arrived by shell.  Overwriting the text that names the patch would
    lose the only pointer to the evidence.
    """
    agent = _make_agent(
        repo, plan_dir, writes={UNDECLARED_PATH: "#!/bin/sh\nexit 0\n"}
    )
    task = _make_task()
    monkeypatch.setattr(
        agent, "_take_undeclared_writes", lambda task_id: [UNDECLARED_PATH]
    )

    agent._execute_task_with_retry(task, max_retries=1)

    failures = _recorded_failures(agent)
    assert failures, "the failed task recorded no failure text at all"
    text = failures[-1]
    assert "rollback_patch:" in text, (
        "the tool-refusal marker overwrote the failure text and the "
        f"pointer to the evidence patch was lost; recorded: {text!r}"
    )
    assert text.startswith("undeclared_write:"), (
        f"the failure text lost the executor's own spelling; recorded: {text!r}"
    )


@pytest.mark.parametrize("outcome", ["raises", "reports-failure"])
def test_a_rollback_that_cannot_run_still_fails_with_the_undeclared_paths(
    repo: Path, plan_dir: Path, monkeypatch, outcome: str,
):
    """A rollback that cannot run is reported, never raised.

    The write contract is violated either way, so the task must still
    fail as an undeclared write, and the failure text must still name
    the paths — that list is the only fact the refiner can act on.  A
    rollback that blows up must not take the executor down with it.
    """
    import agent as agent_module
    from attempt_rollback import RollbackResult

    if outcome == "raises":
        def _rollback(**_kwargs):
            raise RuntimeError("no repository")

    else:
        def _rollback(**_kwargs):
            return RollbackResult(
                rolled_back=[], patch_path="", failed=[UNDECLARED_PATH],
            )

    monkeypatch.setattr(agent_module, "rollback_attempt", _rollback)
    agent = _make_agent(
        repo, plan_dir, writes={UNDECLARED_PATH: "#!/bin/sh\nexit 0\n"}
    )
    task = _make_task()

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is False, (
        "a rollback that could not run let the task pass — the write "
        "contract was violated and the violation must not be excused"
    )
    failures = _recorded_failures(agent)
    assert failures, "the failed task recorded no failure text at all"
    text = failures[-1]
    assert text.startswith("undeclared_write:"), (
        f"the failure text lost the executor's own spelling; recorded: {text!r}"
    )
    assert UNDECLARED_PATH in text, (
        "the failure text lost the list of undeclared paths — that list is "
        f"the only fact the refiner can act on; recorded: {text!r}"
    )
    assert "rollback_failed:" in text, (
        "the failure text does not say that the rollback did not succeed; "
        f"recorded: {text!r}"
    )
