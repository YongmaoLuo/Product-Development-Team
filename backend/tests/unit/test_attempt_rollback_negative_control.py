"""The rollback is load-bearing: take it away and the overstep survives.

Background
----------
The undeclared-write gate fails a task whose attempt wrote outside the
task's declaration.  Before the rollback existed (task 5) that was the
whole of it: the task failed and the oversteped file **stayed in the
work tree**, so the next attempt started from a tree that already
carried the previous attempt's overstep — and the change set the attempt
is judged by was no longer the change set that attempt produced.

Task 5 gave the failure teeth: ``agent.rollback_attempt`` puts the
overstepped paths back against the attempt's baseline and captures what
it rolled back as a patch, so the next attempt starts from a tree the
previous attempt's overstep no longer pollutes.

Why a negative control
----------------------
A green test proves the rollback *runs*.  It does not prove that the
rollback is what makes the overstep disappear — a test that would pass
just as happily with the rollback removed pins nothing about it.  This
file is that missing control: it runs the SAME task twice, once with the
rollback in place and once with it taken away, and shows that the two
runs differ exactly where the rollback is:

* **with the rollback**    — the oversteped file is GONE, and the
  failure names the patch that holds its content;
* **without the rollback** — the oversteped file is STILL THERE, and
  the failure carries no rollback evidence at all;
* **in both runs**         — the task FAILS.  The write contract is
  not negotiable: removing the rollback does not turn the overstep into
  a success, it only turns it into a mess the next attempt inherits.

The attempt is repair-03's shape: the sub-agent writes a file it never
declared and **commits its own work**, so the overstep is a commit of
its own rather than a dirty file.

TDD contract — 4 test cases
---------------------------
1. ``test_with_rollback_undeclared_file_is_gone`` — the control group:
   the oversteped file is gone, and the failure names a real patch.
2. ``test_without_rollback_undeclared_file_survives`` — the negative
   control: with the rollback removed, the file is still there.
3. ``test_without_rollback_failure_has_no_patch_line`` — with the
   rollback removed, the failure text names no patch.
4. ``test_both_runs_still_fail_the_task`` — both runs fail the task.

Boundary conditions
-------------------
* Everything happens under ``tmp_path``: a throw-away git repository and
  a throw-away plan directory.  The repository the suite runs in is
  never touched.
* The negative control removes the rollback **as a whole**: the undo
  (``rollback_attempt``) and the evidence it produces
  (``format_rollback_failure``).  Removing only the undo would leave
  the executor reporting a rollback that never happened — a text with
  no patch behind it.
* The gates around the one under test are mocked out in both runs
  (``_cross_verify_test_result``, ``_commit_task_changes`` and friends),
  so the only thing that can differ between the two runs is the
  rollback.
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

#: The undeclared path the fake sub-agent writes — the shape from the
#: accident: a shell redirection into a script the task never declared.
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


def _make_repo(root: Path) -> Path:
    """A tiny git repository holding one committed, declared file."""
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "readme.md").write_text("original\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "negative-control@example.com")
    _git(root, "config", "user.name", "attempt rollback negative control")
    _git(root, "add", DECLARED_PATH)
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throw-away git repository, one per test."""
    return _make_repo(tmp_path / "repo")


def _make_task():
    """A task that declares exactly one file."""
    from task import SubTask

    return SubTask(
        # Unique per run: the Edit/Write guard's refusal marker is keyed
        # by task id and lives in the system temp dir, so a fixed id
        # would let one run's marker leak into the next one's diagnosis.
        id=f"attempt-rollback-negative-control-{uuid.uuid4().hex[:10]}",
        title="Extend the documentation",
        description="Update the documentation to describe the new flag.",
        test_command="",
        files_to_modify=[DECLARED_PATH],
    )


def _make_agent(repo: Path):
    """An ``AutonomousAgent`` skeleton whose sub-agent oversteps.

    The shape is the one of ``test_undeclared_write_gate_enforced``:
    ``AutonomousAgent.__new__`` plus a REAL ``GitManager`` over the tmp
    repository, because a gate that only exists in a copy of the branch
    cannot be observed — the change set the executor judges has to come
    from the repository itself.

    The fake coding tool writes the file DIRECTLY (``Path.write_text``)
    and then commits it, which is what repair-03's sub-agent did: the
    overstep left the work tree in a commit of its own.
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
    # The plan directory lives OUTSIDE the repository, so the executor's
    # own runtime state (the baseline store, the evidence patch) can
    # never show up as a change of the task's own.
    agent.task_manager.tasks_file = (
        Path(tempfile.mkdtemp(prefix="pdt-attempt-rollback-negative-"))
        / "tasks.json"
    )
    agent.task_manager.record_task_superseded.return_value = False
    agent.plan_id = "pdt-attempt-rollback-negative-control"
    agent.rollback_manager = MagicMock()
    agent._session_task_completed_counts = {}
    agent._failure_occurrences = {}
    agent._refinement_count = 0

    agent.git_manager = GitManager(str(repo))

    def _fake_query(*_args, **_kwargs) -> str:
        target = repo / UNDECLARED_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        # repair-03's shape: the sub-agent commits its own work.  The
        # commit is conditional — the executor grants a second attempt
        # after a first failure, and that attempt finds the tree already
        # holding the file, so there is nothing left to commit.
        _git(repo, "add", "-A")
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        if staged != 0:
            _git(repo, "commit", "-q", "-m", "wip: sub-agent self-commit")
        return "Done.\nTEST_RESULT: PASSED"

    agent.coding_tool = MagicMock()
    agent.coding_tool.query.side_effect = _fake_query

    # Everything around the rollback under test is neutralised so the
    # only thing that can differ between the two runs is the rollback.
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
    for call in agent.retry_manager.record_attempt.call_args_list:
        if len(call.args) >= 2 and isinstance(call.args[1], str):
            texts.append(call.args[1])
    return texts


def _failure_lines(agent) -> list[str]:
    """Every line of every failure text the run recorded."""
    lines: list[str] = []
    for text in _recorded_failures(agent):
        lines.extend(text.splitlines())
    return lines


def _patch_lines(agent) -> list[str]:
    """The ``rollback_patch:`` lines among the recorded failure texts."""
    return [
        line for line in _failure_lines(agent)
        if line.startswith("rollback_patch:")
    ]


def _failure_text_without_rollback(undeclared_paths, result) -> str:
    """The failure text the executor wrote BEFORE the rollback existed.

    One line, the executor's own ``undeclared_write:`` spelling, and no
    rollback evidence: with the rollback taken away there is no patch to
    name, so the text must not claim one.
    """
    paths = [str(p) for p in (undeclared_paths or []) if str(p).strip()]
    return "undeclared_write: " + (", ".join(paths) if paths else "(none)")


def _remove_the_rollback(monkeypatch) -> None:
    """Take the rollback out of the executor's path.

    Two seams, one feature: the **undo** (``rollback_attempt``) and the
    **evidence it produces** (``format_rollback_failure``).  With the
    undo stubbed out the overstep is never undone, and with the evidence
    formatter stubbed out the executor falls back to the single-line
    ``undeclared_write:`` text it used before the rollback existed —
    which is the world the negative control has to describe.
    """
    import agent as agent_module
    from attempt_rollback import RollbackResult

    monkeypatch.setattr(
        agent_module,
        "rollback_attempt",
        lambda **kwargs: RollbackResult([], "", []),
    )
    monkeypatch.setattr(
        agent_module,
        "format_rollback_failure",
        _failure_text_without_rollback,
    )


def _run_with_rollback(repo: Path):
    """The task, run with the real rollback in place."""
    agent = _make_agent(repo)
    task = _make_task()
    ok = agent._execute_task_with_retry(task, max_retries=1)
    return agent, task, ok


def _run_without_rollback(repo: Path, monkeypatch):
    """The same task, run with the rollback taken away."""
    _remove_the_rollback(monkeypatch)
    agent = _make_agent(repo)
    task = _make_task()
    ok = agent._execute_task_with_retry(task, max_retries=1)
    return agent, task, ok


def test_with_rollback_undeclared_file_is_gone(repo: Path):
    """The control group: the overstep is UNDONE, and the patch is real.

    This is the assertion the negative control exists to falsify.  If
    the rollback were removed from the executor, this file would still
    be in the work tree and this assertion would go red — which is
    exactly what ``test_without_rollback_undeclared_file_survives``
    shows from the other side.
    """
    agent, _task, ok = _run_with_rollback(repo)

    assert ok is False, (
        "the attempt wrote a file the task never declared, so the task "
        "must fail"
    )
    assert not (repo / UNDECLARED_PATH).exists(), (
        "the rollback is supposed to put the oversteped file back against "
        "the attempt's baseline — the file is still in the work tree, so "
        "the next attempt inherits it and the change set this attempt is "
        "judged by is no longer the one it produced"
    )
    # The evidence the rollback produced has to be real: a failure text
    # that names a patch which is not there would make the negative
    # control's "no patch line" assertion vacuous.
    patches = _patch_lines(agent)
    assert patches, (
        "the rollback recorded no rollback_patch line — the failure text "
        "does not say where the rolled-back content was captured"
    )
    named = patches[0].split("rollback_patch:", 1)[1].strip()
    assert named and named != "(none)", (
        f"the rollback_patch line names no patch: {patches[0]!r}"
    )
    assert Path(named).exists(), (
        f"the failure names a patch that is not there: {named!r}"
    )


def test_without_rollback_undeclared_file_survives(repo: Path, monkeypatch):
    """The negative control: with the rollback gone, the file stays.

    This is the proof that the rollback is what removes the overstep.
    The run is identical to the control group in every respect except
    the rollback, and the file survives — so a green
    ``test_with_rollback_undeclared_file_is_gone`` is not the result of
    some other mechanism quietly tidying the tree.
    """
    agent, _task, ok = _run_without_rollback(repo, monkeypatch)

    assert ok is False, (
        "removing the rollback must not turn the overstep into a success"
    )
    assert (repo / UNDECLARED_PATH).exists(), (
        "the negative control is supposed to leave the oversteped file in "
        "the work tree — it is gone, so the control group's disappearance "
        "of it is NOT caused by the rollback, and the control proves "
        "nothing"
    )


def test_without_rollback_failure_has_no_patch_line(
    repo: Path, monkeypatch
):
    """With the rollback gone, the failure names no patch.

    There is nothing to name: no rollback ran, so no content was
    captured, so there is no patch.  A failure text that still carried a
    ``rollback_patch:`` line would be claiming evidence that does not
    exist — and it would make this control group's difference from the
    control group an artefact of the text rather than of the rollback.
    """
    agent, _task, ok = _run_without_rollback(repo, monkeypatch)

    assert ok is False, (
        "removing the rollback must not turn the overstep into a success"
    )
    assert not _patch_lines(agent), (
        "the failure text names a rollback patch although the rollback was "
        f"removed: {_patch_lines(agent)!r}"
    )


def test_both_runs_still_fail_the_task(tmp_path: Path, monkeypatch):
    """The write contract holds with and without the rollback.

    Removing the rollback must not weaken the gate: the attempt wrote a
    file the task never declared, and that fails the task either way.
    Only the *consequence* changes — with the rollback the overstep is
    undone, without it the overstep is merely reported.

    The two runs get a repository each: a run that shares a repository
    with the other one would start from the other run's commit, and the
    overstep would then be part of its BASELINE rather than a change
    against it — which is a different question than the one asked here.
    """
    first = _make_repo(tmp_path / "repo-with-rollback")
    second = _make_repo(tmp_path / "repo-without-rollback")

    with_rollback = _make_agent(first)
    ok_with = with_rollback._execute_task_with_retry(
        _make_task(), max_retries=1
    )

    without_rollback = _make_agent(second)
    _remove_the_rollback(monkeypatch)
    ok_without = without_rollback._execute_task_with_retry(
        _make_task(), max_retries=1
    )

    assert ok_with is False, (
        "an attempt that wrote an undeclared file must fail the task"
    )
    assert ok_without is False, (
        "an attempt that wrote an undeclared file must fail the task — "
        "with the rollback removed as well; the rollback undoes the "
        "overstep, it does not excuse it"
    )
