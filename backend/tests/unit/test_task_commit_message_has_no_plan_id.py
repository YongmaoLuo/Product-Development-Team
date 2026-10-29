"""TDD spec: the executor's task-checkpoint commit must not carry a plan id.

Background
----------

``AutonomousAgent._commit_task_changes`` builds the checkpoint commit
message with ``RollbackManager.create_task_commit``, which pastes the
task's free-text ``description`` into the message verbatim. A repair
task's description is written by the verification loop and routinely
names the plan it belongs to -- the report path it must write, the
plan directory it lives under. That description then lands in a
**commit message**, which is a surface the repository's own gates
forbid: ``scripts/check_commit_msg.py`` refuses any message carrying a
date-prefixed plan id, and the static gate
``test_no_plan_ids_in_non_test_source`` scans the whole history for
the same shape.

The refusal is correct and the leak is not. A plan id names a private
project, and a commit message is permanent public history that a
force-push cannot take back: deleting a ref does not delete objects.
So the message has to be scrubbed on the way out, at the one place
every task commit goes through.

What this file pins down:

1. A plan id in the task description does NOT reach the commit message.
2. The scrubbed message carries no plan-id **shape** -- the assertion is
   made with the static gate's own pattern, not with a literal, so the
   two definitions cannot drift apart silently.
3. A message with no plan id is passed through **byte-identical**: the
   scrub is a no-op on clean text.
4. The rest of the message survives -- the task id, the title and the
   file list -- because a checkpoint that cannot be matched back to its
   task is worse than a leaky one.
5. A message that was rewritten says so in the log: the scrub loses
   information, so it must not be silent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

BACKEND_DIR = Path(__file__).resolve().parents[2]
GATES_DIR = BACKEND_DIR / "tests" / "static_gates"
for _path in (str(BACKEND_DIR), str(GATES_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# The static gate's own pattern -- the single definition of "what a plan
# id looks like" for the whole repository. The assertion below is made
# against it rather than against a literal, so a shape that drifts here
# cannot hide behind a shape that drifts there.
from test_no_plan_ids_in_non_test_source import PLAN_ID_SHAPE  # noqa: E402


# A synthetic plan id, assembled from fragments so this file's own source
# never carries one whole -- the same convention the static gate uses for
# its fixtures.
_PLAN_ID = "2026" + "1028-example-topic"


def _make_agent_skeleton():
    """An ``AutonomousAgent`` skeleton with the attributes the commit path uses.

    ``__init__`` is bypassed on purpose: the commit path only touches
    ``git_manager``, ``task_manager``, ``logger`` and ``rollback_manager``,
    and none of them needs a real repo, an LLM client or a plan dir.
    """
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.git_manager = MagicMock()
    agent.task_manager = MagicMock()
    agent.logger = MagicMock()
    agent.rollback_manager = MagicMock()
    agent.git_manager.rev_parse.return_value = "a" * 40
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


def _checkpoint_message(description: str) -> str:
    """The shape ``RollbackManager.create_task_commit`` produces."""
    return (
        "[task-repair-r1-01] root cause for a verification point\n"
        "\n"
        "Task ID: repair-r1-01\n"
        f"Description: {description}\n"
        "Test Command: \n"
        "\n"
        "Files changed:\n"
        "- backend/agent.py\n"
    )


def _commit(agent, description: str) -> str:
    """Run the commit path and return the message handed to git."""
    agent.rollback_manager.create_task_commit.return_value = _checkpoint_message(
        description
    )
    agent._commit_task_changes(_make_task(), ["backend/agent.py"])
    agent.git_manager.commit.assert_called_once()
    return agent.git_manager.commit.call_args[0][0]


def test_a_plan_id_in_the_description_does_not_reach_the_commit_message():
    """The leak this file exists for: the id must not survive into git."""
    agent = _make_agent_skeleton()

    message = _commit(agent, f"write the report to plans/{_PLAN_ID}/reports/x.md")

    assert _PLAN_ID not in message, message


def test_the_committed_message_carries_no_plan_id_shape():
    """The strong form of the same claim, made with the gate's own pattern.

    Asserting on the literal id only proves that *this* id is gone. The
    shape is the rule: any date-prefixed slug in the message fails the
    commit-time hook, so the message has to be free of the shape, not of
    one particular instance of it.
    """
    agent = _make_agent_skeleton()

    message = _commit(agent, f"plans/{_PLAN_ID}/reports/x.md and {_PLAN_ID} too")

    assert not PLAN_ID_SHAPE.search(message), message


def test_a_clean_message_is_passed_through_unchanged():
    """The scrub is a no-op on clean text.

    A checkpoint that silently rewrites a message nobody asked it to
    rewrite would make the history harder to read than the leak it
    prevents.
    """
    agent = _make_agent_skeleton()
    original = _checkpoint_message("a description that names no plan")

    agent.rollback_manager.create_task_commit.return_value = original
    agent._commit_task_changes(_make_task(), ["backend/agent.py"])

    agent.git_manager.commit.assert_called_once()
    assert agent.git_manager.commit.call_args[0][0] == original


def test_the_rest_of_the_message_survives_the_scrub():
    """A checkpoint must still be matchable back to its task.

    The id, the title and the file list are what make a checkpoint
    readable a year later; scrubbing the plan id must not cost them.
    """
    agent = _make_agent_skeleton()

    message = _commit(agent, f"the plan is {_PLAN_ID}")

    assert "[task-repair-r1-01]" in message, message
    assert "repair-r1-01" in message, message
    assert "backend/agent.py" in message, message


def test_scrubbing_is_reported_so_the_loss_is_visible():
    """A message that was rewritten says so.

    The scrub loses information -- the plan id is gone for good -- so the
    event has to be observable rather than silent. The log line names the
    event; the count says how much was removed.
    """
    agent = _make_agent_skeleton()

    _commit(agent, f"the plan is {_PLAN_ID}")

    events = [call[0][0] for call in agent.logger.info.call_args_list]
    assert "task_commit_message_plan_id_scrubbed" in events, events
