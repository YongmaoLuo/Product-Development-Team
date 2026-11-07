"""Mapping from a terminal-round answer to the action it asks for.

Why this module exists
----------------------

The terminal round of the verification loop stops and asks a human what
happens next.  The question it asks offers **two** answers — *rerun this
round* and *revise the criterion* — and until this module existed those
answers lived nowhere but the prompt that offers them and the front-end
text that renders them.

The backend had no vocabulary for them at all.  Every answer came back
through the same door and left through the same one, whatever the human
had chosen: the only action the backend knew was ``resume``, which
means "carry on as if nobody had been asked" — which is precisely the
behaviour the human was trying to replace.  So the semantics of the
options were prose, and prose is not executable.  A human who chose
*rerun this round* got the same handling as a human who chose
*revise the criterion*, and nothing in the system could tell the
difference.

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
"what does *revise_criterion* mean?" could no longer be answered
without starting a plan.

The actions
-----------

``reset_and_start``
    Reset the round's state and start the round again.
``revise_criterion``
    The round's failed points are marked **obsolete** — the criterion
    they carried is wrong, and the new criterion is written outside the
    framework — and the round carries on without them.
``close_as_failed``
    *(retired)* Close the plan as failed — the failure is accepted as
    the outcome.
``generate_repair_tasks``
    *(retired)* Turn the failed points into tasks that can be worked
    through.
``unknown_option``
    The answer names no option at all.  It is **not** a decision, and
    it is not somebody else's to interpret: the executor records the
    failure and stops.

The two retired actions and their option ids are kept because the
decisions recorded before the menu changed are still in the database:
a decision row written on 2026-11-05 says ``accept_as_failed``, and a
human answering it must not hit an error because the table moved on.
What changed is the **menu**, not the **record**: the new question
offers two answers, and the old ones still resolve to the actions they
always named.

What is deliberately absent
---------------------------
``resume`` is not one of the actions.  It is what the terminal round used
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
:data:`TERMINAL_ACTION_UNKNOWN`: *the answer is not a terminal option*.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

__all__ = [
    "TERMINAL_ACTIONS",
    "TERMINAL_ACTION_CLOSE_AS_FAILED",
    "TERMINAL_ACTION_GENERATE_REPAIR_TASKS",
    "TERMINAL_ACTION_RESET_AND_START",
    "TERMINAL_ACTION_REVISE_CRITERION",
    "TERMINAL_ACTION_UNKNOWN",
    "is_legacy_terminal_option",
    "is_terminal_option",
    "resolve_action",
]


# ---------------------------------------------------------------------------
# The actions
# ---------------------------------------------------------------------------

#: The action *rerun this round* asks for: reset the round's state and
#: start it again.
TERMINAL_ACTION_RESET_AND_START = "reset_and_start"

#: The action *revise the criterion* asks for: mark the round's failed
#: points obsolete and carry the round on without them.
TERMINAL_ACTION_REVISE_CRITERION = "revise_criterion"

#: The action *accept the failure* asked for: close the plan as failed.
#:
#: Retired from the menu on 2026-11-06 — *accepting* a failure is giving
#: up, not an option — but kept so a decision recorded before that day
#: still resolves to the action it named.
TERMINAL_ACTION_CLOSE_AS_FAILED = "close_as_failed"

#: The action *generate repair tasks* asked for: turn the failed points
#: into tasks that can be worked through.
#:
#: Retired from the menu on 2026-11-06 — it differed from *rerun this
#: round* only by "with or without a task list" — but kept for the same
#: reason as :data:`TERMINAL_ACTION_CLOSE_AS_FAILED`.
TERMINAL_ACTION_GENERATE_REPAIR_TASKS = "generate_repair_tasks"

#: The action every answer the table does not contain asks for: none.
#:
#: There is no interpreter any more.  A human who writes free text has
#: not chosen an option, and the framework does not guess on their
#: behalf — the executor records the failure and says so.
TERMINAL_ACTION_UNKNOWN = "unknown_option"

#: Every action :func:`resolve_action` can return.  A closed
#: vocabulary: a reader that has seen this tuple knows every possible
#: outcome, which is what makes "the terminal round was answered" a
#: decidable fact rather than a guess.
TERMINAL_ACTIONS: Tuple[str, ...] = (
    TERMINAL_ACTION_RESET_AND_START,
    TERMINAL_ACTION_REVISE_CRITERION,
    TERMINAL_ACTION_CLOSE_AS_FAILED,
    TERMINAL_ACTION_GENERATE_REPAIR_TASKS,
    TERMINAL_ACTION_UNKNOWN,
)


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------

#: The one mapping point: terminal option id → the action it asks for.
#:
#: The two keys are the option ids the orchestrator offers at the
#: terminal round.  They are written out here rather than imported:
#: this module must not depend on the orchestration layer, and a
#: duplicated list is cheaper than that dependency.
_OPTION_TO_ACTION: Dict[str, str] = {
    "rerun_this_round": TERMINAL_ACTION_RESET_AND_START,
    "revise_criterion": TERMINAL_ACTION_REVISE_CRITERION,
}

#: The retired option ids, and the actions they named.
#:
#: The menu changed on 2026-11-06, but the decisions recorded before it
#: are still in the database — and a human answering one of them must
#: not be told their answer is unknown.  These ids are **not** offered
#: by a new question; they only resolve.
_LEGACY_OPTION_TO_ACTION: Dict[str, str] = {
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

    The two terminal options map to their own actions; the retired
    option ids map to the actions they always named; every other answer
    maps to :data:`TERMINAL_ACTION_UNKNOWN`, which is not a decision.
    The returned value is always one of :data:`TERMINAL_ACTIONS` — in
    particular it is never ``"resume"``, which is not a decision.
    """
    key = _normalise(option)
    if key is None:
        return TERMINAL_ACTION_UNKNOWN
    action = _OPTION_TO_ACTION.get(key)
    if action is not None:
        return action
    action = _LEGACY_OPTION_TO_ACTION.get(key)
    if action is not None:
        return action
    return TERMINAL_ACTION_UNKNOWN


def is_terminal_option(option: Any) -> bool:
    """Say whether the given answer is one of the two options on offer.

    This is the *menu*: the answers a new question asks for.  The
    retired ids are not on the menu any more — they are recognised (see
    :func:`is_legacy_terminal_option`), not offered.
    """
    return _normalise(option) in _OPTION_TO_ACTION


def is_legacy_terminal_option(option: Any) -> bool:
    """Say whether the given answer is a retired option id.

    These ids are not offered any more, but a decision recorded before
    the menu changed still carries one, and answering it must not fail.
    """
    return _normalise(option) in _LEGACY_OPTION_TO_ACTION
