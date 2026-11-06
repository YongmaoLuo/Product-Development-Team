"""Unit tests for the terminal-round answer → action mapping.

Background
----------
The terminal round of the verification loop stops and asks a human what
happens next.  The question offers three answers — *rerun this round*,
*accept the failure*, *generate repair tasks* — and those three answers
used to live only in the prompt that offers them and in the front-end
text that renders them.  The backend had no vocabulary for them: every
answer came back through the same door and left through the same one,
whatever the human had chosen.

That is the defect this module's tests pin shut.  The semantics of the
three options were never written down in code, so nothing could act on
them, and the only action the backend had was ``resume`` — which means
"carry on as if nobody had been asked", i.e. exactly the answer the
human was trying to replace.

What is pinned here
-------------------

1. **Each option maps to its own action.**  The three option ids are
   the ones the orchestrator offers, and each one means one thing.
2. **Anything else is the agent's business.**  Free text, a wrong case,
   an unknown id, ``None``, a number, bytes — all of them mean "a human
   said something the table does not contain", and the table's answer to
   that is ``agent_interpret``: somebody who can read Chinese has to
   work out what was meant.
3. **No action is ``resume``.**  ``resume`` is not one of the terminal
   actions, and no mapping may produce it.  This is the assertion that
   would have caught the original defect, so it is stated directly
   rather than left implicit in the other tests.
4. **The mapping is a pure function.**  It reads no file, writes no
   file, and imports nothing from the orchestration layer — so the same
   answer means the same action in every process that asks.

The module under test is imported inside the test bodies on purpose: a
module that does not exist (or does not export the contract) then shows
up as a test FAILURE carrying the real reason, instead of a collection
ERROR that hides which contract broke.

Test command::

    pytest backend/tests/unit/test_terminal_action_mapping.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _terminal_action():
    """The module under test.

    Imported inside the test bodies so a missing module is a FAILURE
    with the real reason rather than a collection ERROR.
    """
    try:
        from hitl import terminal_action
    except ImportError as exc:  # pragma: no cover - the RED phase
        pytest.fail(f"hitl.terminal_action is not importable: {exc}")
    return terminal_action


# ---------------------------------------------------------------------------
# 1. Each option maps to its own action
# ---------------------------------------------------------------------------


def test_three_options_map_to_their_actions():
    """The three terminal options each map to their own action."""
    module = _terminal_action()

    assert module.resolve_action("rerun_this_round") == "reset_and_start", (
        "rerun_this_round must reset the round and start it again"
    )
    assert module.resolve_action("accept_as_failed") == "close_as_failed", (
        "accept_as_failed must close the plan as failed"
    )
    assert module.resolve_action(
        "manual_repair_tasks"
    ) == "generate_repair_tasks", (
        "manual_repair_tasks must generate repair tasks"
    )

    # The table itself is the one mapping point; the three ids above are
    # exactly its keys.
    assert module._OPTION_TO_ACTION == {
        "rerun_this_round": "reset_and_start",
        "accept_as_failed": "close_as_failed",
        "manual_repair_tasks": "generate_repair_tasks",
    }, (
        "the option → action table must carry exactly the three terminal "
        f"options; got {module._OPTION_TO_ACTION!r}"
    )


# ---------------------------------------------------------------------------
# 2. Anything else is the agent's business
# ---------------------------------------------------------------------------


def test_free_text_maps_to_agent():
    """Free text is not an option — the agent reads it and decides."""
    module = _terminal_action()

    assert module.resolve_action(
        "先别重跑，看看是不是环境问题"
    ) == "agent_interpret", (
        "free text must not be matched against the option table; it is "
        "the agent's job to read it"
    )


def test_case_and_unknown_are_not_options():
    """A different case, and an unknown id, are both not options."""
    module = _terminal_action()

    assert module.resolve_action("RERUN_THIS_ROUND") == "agent_interpret", (
        "the option ids are matched exactly — a shouted version of an "
        "option id is not one"
    )
    assert module.resolve_action("delete_plan") == "agent_interpret", (
        "an unknown id must not resolve to an action"
    )

    assert module.is_terminal_option("RERUN_THIS_ROUND") is False
    assert module.is_terminal_option("delete_plan") is False


def test_none_and_non_string_map_to_agent():
    """``None``, a number and bytes are not options."""
    module = _terminal_action()

    for option in (None, 42, b"x"):
        assert module.resolve_action(option) == "agent_interpret", (
            f"a non-string answer must not resolve to an action; "
            f"{option!r} did"
        )
        assert module.is_terminal_option(option) is False, (
            f"a non-string answer must not be an option; {option!r} was"
        )


def test_whitespace_is_stripped_and_empty_is_not_an_option():
    """Padding around an option id is ignored; an empty answer is not."""
    module = _terminal_action()

    assert module.resolve_action(
        "  rerun_this_round  "
    ) == "reset_and_start", (
        "surrounding whitespace must not stop an option from resolving"
    )
    assert module.is_terminal_option("  accept_as_failed  ") is True

    assert module.resolve_action("") == "agent_interpret", (
        "an empty answer is not an option"
    )
    assert module.resolve_action("   ") == "agent_interpret", (
        "an answer that is only whitespace is not an option"
    )
    assert module.is_terminal_option("") is False


def test_is_terminal_option_recognises_only_the_three_options():
    """``is_terminal_option`` is true for the three options, false else."""
    module = _terminal_action()

    for option in ("rerun_this_round", "accept_as_failed", "manual_repair_tasks"):
        assert module.is_terminal_option(option) is True, (
            f"{option!r} is one of the three terminal options"
        )

    for option in ("agent_interpret", "resume", "先别重跑，看看是不是环境问题"):
        assert module.is_terminal_option(option) is False, (
            f"{option!r} is an action or free text, not a terminal option"
        )


# ---------------------------------------------------------------------------
# 3. No action is "resume"
# ---------------------------------------------------------------------------


def test_no_action_is_resume():
    """``resume`` is not a terminal action, and nothing maps to it."""
    module = _terminal_action()

    assert module.TERMINAL_ACTIONS == (
        "reset_and_start",
        "close_as_failed",
        "generate_repair_tasks",
        "agent_interpret",
    ), (
        "the terminal action vocabulary must be exactly these four; got "
        f"{module.TERMINAL_ACTIONS!r}"
    )
    assert "resume" not in module.TERMINAL_ACTIONS, (
        "resume is not a terminal action — it is what the terminal round "
        "used to do instead of asking"
    )

    # Every action the table can produce is one of the four.
    for option, action in module._OPTION_TO_ACTION.items():
        assert action in module.TERMINAL_ACTIONS, (
            f"the action for {option!r} ({action!r}) is not one of the "
            "terminal actions"
        )

    # And so is every action the resolver can return, including the
    # fallback.
    for option in (
        "rerun_this_round",
        "accept_as_failed",
        "manual_repair_tasks",
        "先别重跑，看看是不是环境问题",
        "RERUN_THIS_ROUND",
        None,
        42,
    ):
        assert module.resolve_action(option) in module.TERMINAL_ACTIONS, (
            f"resolving {option!r} produced an action outside the terminal "
            "vocabulary"
        )


# ---------------------------------------------------------------------------
# 4. The mapping is a pure function
# ---------------------------------------------------------------------------


def test_module_imports_nothing_from_the_orchestration_layer():
    """The mapping must not import the layers it is supposed to feed.

    ``reset_and_start`` means "reset the round and start it again" — but
    only to somebody who knows how a round is reset.  This module does
    not: it says which action an answer asks for, and nothing about how
    the action is carried out.  A module that imported the orchestrator
    (or a route handler, or the plan scheduler) would take on their
    dependencies, their cycles and their side effects, and the mapping
    would stop being answerable in isolation.
    """
    module = _terminal_action()
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

    forbidden = {
        "routes",
        "server",
        "plan_scheduler",
        "orchestrator",
        "engine",
        "coding_tool",
        "hitl",
    }
    offending = imported & forbidden
    assert not offending, (
        f"the mapping must not import the orchestration layer; it imports "
        f"{sorted(offending)!r}"
    )

    assert imported <= {"typing", "__future__"}, (
        "the mapping must depend on the standard library only; it imports "
        f"{sorted(imported)!r}"
    )