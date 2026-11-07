"""Unit tests for the terminal-round answer → action mapping.

Background
----------
The terminal round of the verification loop stops and asks a human what
happens next.  The question offers two answers — *rerun this round* and
*revise the criterion* — and those answers used to live only in the
prompt that offers them and in the front-end text that renders them.
The backend had no vocabulary for them: every answer came back through
the same door and left through the same one, whatever the human had
chosen.

That is the defect this module's tests pin shut.  The semantics of the
options were never written down in code, so nothing could act on them,
and the only action the backend had was ``resume`` — which means
"carry on as if nobody had been asked", i.e. exactly the answer the
human was trying to replace.

What is pinned here
-------------------

1. **Each option maps to its own action.**  The two option ids are the
   ones the orchestrator offers, and each one means one thing.
2. **The retired ids still resolve.**  The decisions recorded before the
   menu changed are still in the database; answering one of them must not
   fail, so the retired ids map to the actions they always named — but
   they are not on the menu any more.
3. **Anything else is not an option.**  Free text, a wrong case, an
   unknown id, ``None``, a number, bytes — all of them mean "a human
   said something the table does not contain", and the table's answer to
   that is ``unknown_option``: *the answer is not a terminal option*.
   Nobody interprets it on the human's behalf any more.
4. **No action is ``resume``.**  ``resume`` is not one of the terminal
   actions, and no mapping may produce it.  This is the assertion that
   would have caught the original defect, so it is stated directly
   rather than left implicit in the other tests.
5. **The mapping is a pure function.**  It reads no file, writes no
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


def test_two_options_map_to_their_actions():
    """The two terminal options each map to their own action."""
    module = _terminal_action()

    assert module.resolve_action("rerun_this_round") == "reset_and_start", (
        "rerun_this_round must reset the round and start it again"
    )
    assert module.resolve_action("revise_criterion") == "revise_criterion", (
        "revise_criterion must obsolete the failed points and start the "
        "round again"
    )

    # The table itself is the one mapping point; the two ids above are
    # exactly its keys.
    assert module._OPTION_TO_ACTION == {
        "rerun_this_round": "reset_and_start",
        "revise_criterion": "revise_criterion",
    }, (
        "the option → action table must carry exactly the two terminal "
        f"options; got {module._OPTION_TO_ACTION!r}"
    )


# ---------------------------------------------------------------------------
# 2. The retired ids still resolve — they are just not on the menu
# ---------------------------------------------------------------------------


def test_retired_options_still_resolve_to_their_actions():
    """A decision recorded before the menu changed still answers."""
    module = _terminal_action()

    assert module.resolve_action("accept_as_failed") == "close_as_failed", (
        "accept_as_failed must still close the plan as failed — the "
        "decision rows that name it are still in the database"
    )
    assert module.resolve_action(
        "manual_repair_tasks"
    ) == "generate_repair_tasks", (
        "manual_repair_tasks must still generate repair tasks"
    )


def test_retired_options_are_not_on_the_menu():
    """The retired ids are recognised, not offered."""
    module = _terminal_action()

    for option in ("accept_as_failed", "manual_repair_tasks"):
        assert module.is_terminal_option(option) is False, (
            f"{option!r} is not on the menu any more"
        )
        assert module.is_legacy_terminal_option(option) is True, (
            f"{option!r} is a retired option id"
        )


# ---------------------------------------------------------------------------
# 3. Anything else is not an option
# ---------------------------------------------------------------------------


def test_free_text_is_not_an_option():
    """Free text is not an option, and nobody interprets it any more."""
    module = _terminal_action()

    assert module.resolve_action(
        "先别重跑，看看是不是环境问题"
    ) == "unknown_option", (
        "free text must not be matched against the option table, and "
        "nothing may interpret it on the human's behalf"
    )


def test_case_and_unknown_are_not_options():
    """A different case, and an unknown id, are both not options."""
    module = _terminal_action()

    assert module.resolve_action("RERUN_THIS_ROUND") == "unknown_option", (
        "the option ids are matched exactly — a shouted version of an "
        "option id is not one"
    )
    assert module.resolve_action("delete_plan") == "unknown_option", (
        "an unknown id must not resolve to an action"
    )

    assert module.is_terminal_option("RERUN_THIS_ROUND") is False
    assert module.is_terminal_option("delete_plan") is False


def test_none_and_non_string_are_not_options():
    """``None``, a number and bytes are not options."""
    module = _terminal_action()

    for option in (None, 42, b"x"):
        assert module.resolve_action(option) == "unknown_option", (
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
    assert module.is_terminal_option("  revise_criterion  ") is True

    assert module.resolve_action("") == "unknown_option", (
        "an empty answer is not an option"
    )
    assert module.resolve_action("   ") == "unknown_option", (
        "an answer that is only whitespace is not an option"
    )
    assert module.is_terminal_option("") is False


def test_is_terminal_option_recognises_only_the_two_options():
    """``is_terminal_option`` is true for the two options, false else."""
    module = _terminal_action()

    for option in ("rerun_this_round", "revise_criterion"):
        assert module.is_terminal_option(option) is True, (
            f"{option!r} is one of the two terminal options"
        )

    for option in (
        "accept_as_failed",
        "manual_repair_tasks",
        "unknown_option",
        "resume",
        "先别重跑，看看是不是环境问题",
    ):
        assert module.is_terminal_option(option) is False, (
            f"{option!r} is an action or free text, not a terminal option"
        )


# ---------------------------------------------------------------------------
# 4. No action is "resume"
# ---------------------------------------------------------------------------


def test_no_action_is_resume():
    """``resume`` is not a terminal action, and nothing maps to it."""
    module = _terminal_action()

    assert module.TERMINAL_ACTIONS == (
        "reset_and_start",
        "revise_criterion",
        "close_as_failed",
        "generate_repair_tasks",
        "unknown_option",
    ), (
        "the terminal action vocabulary must be exactly these five; got "
        f"{module.TERMINAL_ACTIONS!r}"
    )
    assert "resume" not in module.TERMINAL_ACTIONS, (
        "resume is not a terminal action — it is what the terminal round "
        "used to do instead of asking"
    )

    # Every action the table can produce is one of the five.
    for table in (module._OPTION_TO_ACTION, module._LEGACY_OPTION_TO_ACTION):
        for option, action in table.items():
            assert action in module.TERMINAL_ACTIONS, (
                f"the action for {option!r} ({action!r}) is not one of the "
                "terminal actions"
            )

    # And so is every action the resolver can return, including the
    # fallback.
    for option in (
        "rerun_this_round",
        "revise_criterion",
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
# 5. The mapping is a pure function
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
