"""TDD spec: the executor must not create a commit its own hook would reject.

Why this file exists
--------------------

``scripts/check_commit_msg.py`` is the repository's commit-message rule:
a trailer may name the tool, never the model; and a message may not carry
a date-prefixed plan id. It is installed as a ``commit-msg`` hook and it
runs in CI.

It does **not** run when the executor commits. ``GitManager.commit`` is
``git add -A`` followed by ``repo.index.commit(message)`` -- GitPython
writes the commit object and updates the ref itself, so no hook is
executed. The rule is therefore enforced on every commit a *person*
makes and on every commit CI inspects, but not on the commits the
executor makes on its own -- which are exactly the commits whose
message is assembled out of a task's free-text description.

That asymmetry is the hole: the executor is the one writer whose message
nobody checks, and its message is the one most likely to carry something
private (a repair task's description names the plan it belongs to).

What this file pins down:

1. The message the executor produces is accepted by the repository's own
   checker -- the real script, not a re-implementation of it.
2. A message the checker rejects is NOT committed. The changes stay in
   the working tree, where the next commit can pick them up.
3. A refusal is loud: it is logged as an error event, not swallowed.
4. A clean message still commits. A guard that blocks everything is not
   a guard, it is an outage.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

BACKEND_DIR = Path(__file__).resolve().parents[2]
GATES_DIR = BACKEND_DIR / "tests" / "static_gates"
REPO_ROOT = BACKEND_DIR.parent
for _path in (str(BACKEND_DIR), str(GATES_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# The rule script the ``commit-msg`` hook runs. The test uses the real
# one on purpose: a re-implementation here would be a second copy of the
# rule, which is the very thing the rule exists to prevent.
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_commit_msg.py"

# A synthetic plan id, assembled from fragments so this file's own source
# never carries one whole.
_PLAN_ID = "2026" + "1028-example-topic"


def _model_trailer_description() -> str:
    """A description carrying an attribution claim the rule refuses.

    The trailer has to be on a line of its own: the rule reads the
    message line by line, and a trailer is a line that *starts* with
    ``Co-Authored-By:``. The executor pastes a description in verbatim,
    so a description that quotes a commit message brings its trailers
    with it -- which is exactly how a real description ends up naming a
    model in a checkpoint.
    """
    return "the work\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>"


def _make_agent_skeleton():
    """An ``AutonomousAgent`` skeleton with the attributes the commit path uses."""
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.git_manager = MagicMock()
    agent.task_manager = MagicMock()
    agent.logger = MagicMock()
    agent.rollback_manager = MagicMock()
    agent.git_manager.rev_parse.return_value = "a" * 40
    # The project dir is where the executor looks for the repository's
    # own commit-message rule -- the same place a person's commit finds
    # it, via the hook.
    agent.project_dir = str(REPO_ROOT)
    return agent


def _make_task():
    from task import SubTask

    return SubTask(
        id="repair-r1-01",
        title="root cause for a verification point",
        description="a description that names no plan",
        test_command="",
        files_to_modify=["backend/agent.py"],
    )


def _commit(agent, description: str) -> str:
    """Run the commit path with a checkpoint message carrying *description*."""
    agent.rollback_manager.create_task_commit.return_value = (
        "[task-repair-r1-01] root cause for a verification point\n"
        "\n"
        "Task ID: repair-r1-01\n"
        f"Description: {description}\n"
        "Test Command: \n"
        "\n"
        "Files changed:\n"
        "- backend/agent.py\n"
        "\n"
        "Co-Authored-By: Autonomous Agent <agent@autonomous.dev>\n"
    )
    agent._commit_task_changes(_make_task(), ["backend/agent.py"])
    return agent.git_manager.commit.call_args[0][0] if agent.git_manager.commit.called else ""


def test_the_check_script_the_executor_uses_is_the_repositorys_own():
    """The executor is held to the same rule a person's commit is.

    If this path ever stops existing, the executor would silently fall
    back to "no check" and the hole would reopen without anyone
    noticing. The test fails loudly instead.
    """
    assert CHECK_SCRIPT.is_file(), CHECK_SCRIPT


def test_a_message_carrying_a_plan_id_is_committed_clean():
    """The end-to-end claim: the repository's own checker accepts the result.

    The description carries a plan id -- exactly what a repair task's
    description does -- and what reaches git must be free of it. The
    oracle is the real ``check_commit_msg.py``, so this is the rule the
    hook and CI enforce, not a restatement of it.
    """
    agent = _make_agent_skeleton()

    message = _commit(agent, f"write the report to plans/{_PLAN_ID}/reports/x.md")

    assert message, "the commit was refused; it should have been made clean and committed"
    assert _PLAN_ID not in message, message
    assert agent.git_manager.commit.called


def test_a_message_the_checker_rejects_is_not_committed():
    """A rejected message never reaches git.

    The changes stay in the working tree, where the next commit picks
    them up -- nothing is lost, and the private trailer is not written
    into permanent history.
    """
    agent = _make_agent_skeleton()

    _commit(agent, _model_trailer_description())

    assert not agent.git_manager.commit.called, "a rejected message was committed"


def test_a_refusal_is_logged_as_an_error():
    """A refusal that nobody can see is indistinguishable from a hang.

    The event name is the contract: a reader grepping the log for
    ``task_commit_message_rejected`` finds every message the executor
    declined to commit, and why.
    """
    agent = _make_agent_skeleton()

    _commit(agent, _model_trailer_description())

    events = [call[0][0] for call in agent.logger.error.call_args_list]
    assert "task_commit_message_rejected" in events, events


def test_a_clean_message_still_commits():
    """The guard blocks the bad, not the good.

    A check that refused every message would leave the repository with
    no checkpoints at all -- a worse failure than the one it prevents.
    """
    agent = _make_agent_skeleton()

    message = _commit(agent, "a description that names no plan")

    assert message, "a clean message was refused"
    assert agent.git_manager.commit.called
