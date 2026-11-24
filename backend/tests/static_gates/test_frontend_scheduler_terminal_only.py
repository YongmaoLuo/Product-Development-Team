"""The terminal table must list terminal plans only.

Why this gate exists
--------------------
The scheduler panel draws three lists: the queue, the running list, and
the **terminal** block. The terminal block exists for the plans that
are in neither of the first two — a failed plan, a stopped plan — so
that a plan the operator can act on is never invisible.

The filter that picked the rows for that block used to ask only one
question: *does this phase have any action?* The action matrix
(``SCHEDULER_ACTIONS_BY_PHASE``) answers that for the queue and the
running list too — ``queued`` has ``promote`` / ``pause`` / ``start`` and
``executing`` has ``pause``. So a **running** plan satisfied the filter
and was drawn in the terminal table, carrying a 「暂停」 button, next to
the running list that already showed the very same plan. A plan that is
executing is not terminal, and a 「暂停」 button in the terminal table
reads as if the plan had already stopped.

The rule
--------
* ``SCHEDULER_TERMINAL_PHASES`` names the phases that are terminal: the
  ones that have come to a stop and can only be acted on again by an
  operator — ``stopped``, ``failed``, ``waiting_for_quota``,
  ``verification_loop_stopped`` and ``verification_failed``.
* The set does **not** name the phases of the queue and of the running
  list (``queued``, ``executing``), nor the phases of a verification or a
  post-processing round that is still going
  (``verification_running``, ``post_processing``).
* ``renderSchedulerTerminalPlans()`` filters with
  ``SCHEDULER_TERMINAL_PHASES.has(phase)`` **and** the action matrix, so
  a row is drawn only when the plan is terminal *and* the operator has
  something to press.

The action matrix itself is untouched: the running row's 「暂停」 button
still comes from it, so a phase can have actions without being
terminal.

Scan target
-----------
``frontend/app.js``, read as source. This is a shape gate, like the rest
of ``static_gates/``: it runs on a plain Python environment with no JS
toolchain, so it pins the contract in the source rather than executing
the page. The set is parsed out of the source, so a table that says one
thing and a renderer that reads another cannot both pass.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"

#: The phases that are terminal: the plan has come to a stop, and only
#: an operator can act on it again.
EXPECTED_TERMINAL_PHASES = (
    "stopped",
    "failed",
    "waiting_for_quota",
    "verification_loop_stopped",
    "verification_failed",
)

#: The phases that are alive — drawn in the queue or in the running
#: list, never in the terminal block.
NON_TERMINAL_PHASES = (
    "queued",
    "executing",
    "verification_running",
    "post_processing",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(path: Path) -> str:
    """Return the text of ``path`` as UTF-8.

    ``errors="replace"`` keeps a stray byte from turning a shape gate
    into an unhandled ``UnicodeDecodeError`` — a gate that dies on a
    malformed byte reports nothing useful about the contract it was
    written to protect.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _normalized(text: str) -> str:
    """Return ``text`` with every run of whitespace collapsed away.

    The gates here read the source as text, so the exact line breaks and
    indentation a change happens to use must not decide whether the
    contract holds.
    """
    return re.sub(r"\s+", "", text)


def _function_body(source: str, name: str) -> str:
    """Return the body text of the top-level ``function name(...)``.

    The body ends at the first line that is exactly ``}`` — the file's
    top-level functions close at column zero, so that line ends the
    function no matter what the body contains. Brace counting would be
    the alternative, and it miscounts the template literals this file is
    full of.

    Raises :class:`AssertionError` when the declaration is absent, so a
    rename that nobody updates the gate with fails loudly instead of
    matching nothing and passing.
    """
    pattern = re.compile(
        r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)",
        re.MULTILINE,
    )
    match = pattern.search(source)
    assert match is not None, f"function {name}() not found in the source"
    lines = source[match.end() :].splitlines()
    collected: list[str] = []
    for line in lines:
        if line.rstrip() == "}":
            return "\n".join(collected)
        collected.append(line)
    raise AssertionError(f"unterminated function {name}()")


def _terminal_phases(source: str) -> set[str]:
    """Parse ``SCHEDULER_TERMINAL_PHASES`` into a set of phase names.

    The declaration is a ``new Set([...])`` literal, so the members are
    the quoted strings inside its brackets.

    Raises :class:`AssertionError` when the declaration is absent, so a
    rename fails loudly instead of matching nothing and passing.
    """
    match = re.search(
        r"\bconst\s+SCHEDULER_TERMINAL_PHASES\s*=\s*new\s+Set\s*\(\s*\[",
        source,
    )
    assert match is not None, (
        "SCHEDULER_TERMINAL_PHASES is not declared as a `new Set([...])` "
        "in the source"
    )
    start = match.end()
    end = source.find("]", start)
    assert end != -1, "SCHEDULER_TERMINAL_PHASES has no closing bracket"
    return set(re.findall(r'"([^"]*)"', source[start:end]))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_terminal_phases_set_exists_and_names_the_five_terminal_phases() -> None:
    """The terminal phases must be named, and the five must be there.

    A terminal plan is one that has come to a stop: the failed plan, the
    stopped plan, the plan waiting for quota, and the two phases that
    exist because the verification round did not pass. Those are the
    plans the terminal block exists for — the ones the queue and the
    running list cannot show.
    """
    phases = _terminal_phases(_source(_APP_JS))

    for phase in EXPECTED_TERMINAL_PHASES:
        assert phase in phases, (
            f"SCHEDULER_TERMINAL_PHASES does not name {phase!r}. A plan "
            "in that phase is terminal and belongs in the terminal table; "
            "without it the plan is invisible to the operator."
        )


def test_terminal_phases_exclude_the_alive_phases() -> None:
    """The queue's and the running list's phases must not be terminal.

    This is the defect the gate was written for. ``queued`` and
    ``executing`` both have actions in the action matrix (that is where
    the running row's 「暂停」 comes from), so a filter that asked only
    "does this phase have actions?" drew the running plans in the
    terminal table too — the same plan twice, the second time with a
    pause button that reads as if it had already stopped.

    ``verification_running`` and ``post_processing`` are alive too: a
    round that is still going is not a plan that has come to a stop.
    """
    phases = _terminal_phases(_source(_APP_JS))

    for phase in NON_TERMINAL_PHASES:
        assert phase not in phases, (
            f"SCHEDULER_TERMINAL_PHASES names {phase!r}. That phase is "
            "alive — it is drawn in the queue or in the running list — "
            "so a plan in it must not be drawn in the terminal table."
        )


def test_terminal_renderer_filters_on_the_terminal_phases() -> None:
    """The renderer must actually consult the set, and consult both.

    The set existing is not the contract — the row filter is. A
    renderer that keeps asking only "does this phase have actions?"
    passes a gate that only reads the declaration, and the running
    plans stay in the terminal table.

    Both conditions are required: the phase must be terminal *and* the
    operator must have a button to press. A terminal phase with no
    action has nothing to act on, and a phase with an action that is
    not terminal does not belong in this table.
    """
    body = _normalized(
        _function_body(_source(_APP_JS), "renderSchedulerTerminalPlans")
    )

    assert "SCHEDULER_TERMINAL_PHASES.has(phase)" in body, (
        "renderSchedulerTerminalPlans() does not filter on "
        "SCHEDULER_TERMINAL_PHASES. A plan that is not terminal (a "
        "running one, say) would still be drawn in the terminal table."
    )

    assert "(SCHEDULER_ACTIONS_BY_PHASE[phase]||[]).length>0" in body, (
        "renderSchedulerTerminalPlans() no longer requires the phase to "
        "have an action. A terminal phase with nothing to press has "
        "nothing to draw."
    )

    terminal_at = body.index("SCHEDULER_TERMINAL_PHASES.has(phase)")
    actions_at = body.index("(SCHEDULER_ACTIONS_BY_PHASE[phase]||[]).length>0")

    assert terminal_at < actions_at, (
        "renderSchedulerTerminalPlans() checks the actions before it "
        "checks the phase, so the two conditions are not combined the "
        "way the contract says."
    )

    between = body[terminal_at:actions_at]
    assert "&" in between, (
        "the terminal-phase check and the action check are not combined "
        "with `&&`. One of them alone lets the wrong plans in: the "
        "action check alone draws the running plans, the terminal check "
        "alone draws a terminal phase with no button."
    )
