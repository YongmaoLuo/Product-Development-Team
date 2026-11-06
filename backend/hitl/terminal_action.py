"""Mapping from a terminal-round answer to the action it asks for.

Why this module exists
----------------------
The terminal round of the verification loop stops and asks a human what
happens next.  The question it asks offers three answers — *rerun this
round*, *accept the failure*, *generate repair tasks* — and until this
module existed those three answers lived nowhere but the prompt that
offers them and the front-end text that renders them.

The backend had no vocabulary for them at all.  Every answer came back
through the same door and left through the same one, whatever the human
had chosen: the only action the backend knew was ``resume``, which
means "carry on as if nobody had been asked" — which is precisely the
behaviour the human was trying to replace.  So the semantics of the
three options were prose, and prose is not executable.  A human who
chose *accept the failure* got the same handling as a human who chose
*rerun this round*, and nothing in the system could tell the difference.

This module is the one place where an answer becomes an action.

What this module is — and is not
--------------------------------
A table and two pure functions.  That is all, deliberately:

* **No I/O.**  It reads no file, writes no file, opens no connection.
* **No orchestration imports.**  It does not know how a round is reset
  or how a plan is closed — only which action an answer *asks for*.
  ``reset_and_start`` names the action; carrying it out is somebody
  else's job.
* **No clock, no environment, no randomness.**  The same answer means
  the same action in the executor thread, in a route handler, in a CLI
  and in a test.

That restraint is not asceticism; it is what makes the mapping
answerable at all.  A mapping that reached into the orchestrator would
inherit its cycles, its side effects and its database, and the question
"what does *accept_as_failed* mean?" could no longer be answered
without starting a plan.

The four actions
----------------

``reset_and_start``
    Reset the round's state and start the round again.
``close_as_failed``
    Close the plan as failed — the failure is accepted as the outcome.
``generate_repair_tasks``
    Turn the failed points into tasks that can be worked through.
``agent_interpret``
    Nobody knows yet what the human meant, so the agent reads the free
    text and works it out.  This is the fallback for every answer the
    table does not contain, and it is a *terminal* action like the
    others: the round is decided, only the deciding is left to somebody
    who can read what was written.

What is deliberately absent
---------------------------
``resume`` is not one of the four.  It is what the terminal round used
to do instead of asking, and it is not a decision — it is the absence
of one.  A mapping that could produce it would be able to answer a
human's question by ignoring it.

Matching is exact
-----------------
An option id is matched after stripping surrounding whitespace, and
nothing else: no case folding, no prefix matching, no fuzzy matching.
``RERUN_THIS_ROUND`` is not ``rerun_this_round``; a human who shouted
(or a client that upper-cased the id) has not thereby chosen an option,
and the table cannot guess what they meant.  Free text, an unknown id,
an empty string, ``None``, a number, bytes — all of them mean "the
answer is not in the table", and the table's answer to that is
``agent_interpret``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

__all__ = [
    "TERMINAL_ACTIONS",
    "TERMINAL_ACTION_AGENT",
    "TERMINAL_ACTION_CLOSE_AS_FAILED",
    "TERMINAL_ACTION_GENERATE_REPAIR_TASKS",
    "TERMINAL_ACTION_RESET_AND_START",
    "is_terminal_option",
    "resolve_action",
]


# ---------------------------------------------------------------------------
# The actions
# ---------------------------------------------------------------------------

#: The action *rerun this round* asks for: reset the round's state and
#: start it again.
TERMINAL_ACTION_RESET_AND_START = "reset_and_start"

#: The action *accept the failure* asks for: close the plan as failed.
TERMINAL_ACTION_CLOSE_AS_FAILED = "close_as_failed"

#: The action *generate repair tasks* asks for: turn the failed points
#: into tasks that can be worked through.
TERMINAL_ACTION_GENERATE_REPAIR_TASKS = "generate_repair_tasks"

#: The action every answer the table does not contain asks for: let the
#: agent read the free text and decide what it meant.
TERMINAL_ACTION_AGENT = "agent_interpret"

#: Every action :func:`resolve_action` can return.  A closed
#: vocabulary: a reader that has seen this tuple knows every possible
#: outcome, which is what makes "the terminal round was answered" a
#: decidable fact rather than a guess.
TERMINAL_ACTIONS: Tuple[str, ...] = (
    TERMINAL_ACTION_RESET_AND_START,
    TERMINAL_ACTION_CLOSE_AS_FAILED,
    TERMINAL_ACTION_GENERATE_REPAIR_TASKS,
    TERMINAL_ACTION_AGENT,
)


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------

#: The one mapping point: terminal option id → the action it asks for.
#:
#: The three keys are the option ids the orchestrator offers at the
#: terminal round.  They are written out here rather than imported:
#: this module must not depend on the orchestration layer, and a
#: duplicated list is cheaper than that dependency.
_OPTION_TO_ACTION: Dict[str, str] = {
    "rerun_this_round": TERMINAL_ACTION_RESET_AND_START,
    "accept_as_failed": TERMINAL_ACTION_CLOSE_AS_FAILED,
    "manual_repair_tasks": TERMINAL_ACTION_GENERATE_REPAIR_TASKS,
}


def _normalise(option: Any) -> Optional[str]:
    """Reduce an answer to the string the table is keyed by.

    A non-string answer has no string form to look up — ``None``, a
    number and bytes are all "not an option", and ``None`` says so
    without pretending they were strings.
    """
    if not isinstance(option, str):
        return None
    return option.strip()


def resolve_action(option: Any) -> str:
    """Return the action the given answer asks for.

    The three terminal options map to their own actions; every other
    answer maps to :data:`TERMINAL_ACTION_AGENT`, which is somebody who
    can read what the human wrote.  The returned value is always one of
    :data:`TERMINAL_ACTIONS` — in particular it is never ``"resume"``,
    which is not a decision.
    """
    return _OPTION_TO_ACTION.get(
        _normalise(option), TERMINAL_ACTION_AGENT
    )


def is_terminal_option(option: Any) -> bool:
    """Say whether the given answer is one of the three terminal options.

    Used to tell "the human picked an option" from "the human wrote
    something", which is the difference between asking the agent to
    interpret an answer and asking it to carry one out.
    """
    return _normalise(option) in _OPTION_TO_ACTION