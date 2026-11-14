"""The parity assertion must compare one snapshot with itself.

Why this file exists
--------------------
The operator's case compared the duration the panel draws with the
``elapsed_sec`` of a read model it had just fetched, and let the two
differ by two seconds.  Two things were wrong with that at once:

* the panel draws the **plan** clock (``running_sec ?? elapsed_sec`` —
  ``frontend/app.js:2223``), so the comparison was between two
  different clocks;
* the panel draws the read model it read at its **last poll**, so the
  drawing on the screen may be a whole poll period older than the
  model the case fetched — and a two-second tolerance does not cover
  that.

Widening the tolerance would have hidden both.  What the comparison
needs is the read model **the panel was fed**: the rows on the screen
belong to that model and to no other.  So the case waits until the
panel has drawn that model, compares the drawing with it, and checks
that the read model read afterwards is the same clock read a moment
later — no further apart than one poll period plus the gap between
the two readings.

The rules
---------
* The comparison is against the read model the panel was fed
  (``report["panel_state"]``), and not against a model fetched at the
  moment of the comparison.
* The clock the comparison asks for is the clock the panel draws:
  ``_panel_clock_seconds``.  The slot clock (``elapsed_sec``) is not
  what the panel draws, so comparing against it compares two different
  facts.
* The case waits for the panel to draw that model before it compares
  anything: ``_wait_for_panel_parity``.
* How far apart two readings of one clock may be is
  ``_panel_parity_bound`` — the panel's own poll period and the
  measured gap — and not a number written down here.
* The read model is chosen out of the answers the server gave the
  panel **before** the panel was read.  An answer that lands during
  the reading was never fed to the panel, so it cannot be the model
  the drawing on the screen was drawn from.

Scan target
-----------
``backend/tests/e2e/plan_scheduler/test_scheduler_operator_e2e.py``,
read as source.  These are shape gates like the rest of
``static_gates/``: they run on a plain Python environment with no
browser, so they pin the contract in the source rather than executing
the page.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List


_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The operator's end-to-end case: the one that drives the panel and
#: compares it with the read model.
_OPERATOR_E2E = (
    _REPO_ROOT
    / "backend"
    / "tests"
    / "e2e"
    / "plan_scheduler"
    / "test_scheduler_operator_e2e.py"
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


def _module_ast(path: Path) -> ast.Module:
    """The parsed module of ``path``."""
    return ast.parse(_source(path), filename=str(path))


def _functions(source: str) -> Dict[str, str]:
    """Every function of *source*, by name, with its own text.

    The walk is over the whole tree and not only over the top level:
    the driver's handlers are defined inside the ``with`` block that
    drives the browser, not beside it.
    """
    bodies: Dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef):
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                bodies[node.name] = segment
    return bodies


def _case_function(name: str) -> str:
    """The text of the case's own function *name*."""
    source = _source(_OPERATOR_E2E)
    bodies = _functions(source)
    assert name in bodies, (
        "the operator's end-to-end case has no {0!r}. The case is what "
        "compares the panel with the read model; a case that does not "
        "define it has nothing to compare.".format(name)
    )
    return bodies[name]


def _driver_source() -> str:
    """The browser driver's own source, read out of the case's module.

    The driver is a string inside the case's module, so a source scan
    is the only way to see what it does.  It is read as a module of its
    own afterwards, so the functions inside it can be looked at one by
    one.
    """
    for node in _module_ast(_OPERATOR_E2E).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_PANEL_DRIVER_SOURCE":
                return str(ast.literal_eval(node.value))
    raise AssertionError(
        "the operator's end-to-end case has no _PANEL_DRIVER_SOURCE. The "
        "driver is the panel, as an operator uses it; a case without one "
        "cannot compare the panel with anything."
    )


def _arguments(function: str) -> List[str]:
    """The names of the arguments of the function *function*."""
    node = ast.parse(function).body[0]
    assert isinstance(node, ast.FunctionDef), "not a function"
    return [argument.arg for argument in node.args.args]


def _driver_parity_branch() -> List[ast.stmt]:
    """The driver's branch that reads the panel without waiting for it.

    The driver waits for the panel to draw the read model it was fed
    when the case asks for a parity reading, and reads the panel as it
    stands otherwise.  It is the second branch that has to count the
    answers the server gave **before** it reads.
    """
    for node in ast.walk(ast.parse(_driver_source())):
        if isinstance(node, ast.If) and "parity" in ast.dump(node.test):
            return list(node.orelse)
    raise AssertionError(
        "the browser driver has no branch on the parity flag. The driver "
        "reads the panel either after it waited for the panel to draw the "
        "read model it was fed, or as it stands; there is no third way."
    )


def _assigned_names(body: List[ast.stmt]) -> Dict[str, ast.stmt]:
    """The assignments of *body*, by the name they bind."""
    assigned: Dict[str, ast.stmt] = {}
    for statement in body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name):
                assigned[target.id] = statement
    return assigned


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_parity_uses_panel_clock() -> None:
    """The comparison must ask the clock the panel draws.

    The panel draws ``item.running_sec ?? item.elapsed_sec`` — the plan
    clock when the read model carries one.  A comparison that asks the
    read model for the slot clock alone compares two different facts:
    the plan clock keeps counting across a scheduler restart, while the
    slot clock starts again from zero.

    And the model the comparison is made against is the model **the
    panel was fed**: the rows on the screen belong to that model and to
    no other, so a comparison with a model fetched at the moment of the
    comparison is a comparison of two moments.
    """
    body = _case_function("test_panel_and_api_parity")

    assert "_panel_clock_seconds(" in body, (
        "the parity case does not ask for the clock the panel draws "
        "(_panel_clock_seconds). The panel draws the plan clock, and a "
        "comparison that asks for another clock compares two different "
        "facts."
    )
    assert 'report["panel_state"]' in body, (
        "the parity case does not read the read model the panel was fed "
        '(report["panel_state"]). The panel draws the model it read at its '
        "last poll, so the rows on the screen belong to that model; a "
        "comparison against a model read at another moment is a "
        "comparison of two moments."
    )


def test_parity_does_not_compare_slot_clock() -> None:
    """The comparison must not fall back to the slot clock.

    ``elapsed_sec`` is the slot clock: it is measured from the moment
    the slot was acquired, so it starts again from zero whenever the
    scheduler restarts.  It is not what the panel draws, and reading it
    here would put the slot clock back into a comparison that is
    about the panel's own clock.
    """
    body = _case_function("test_panel_and_api_parity")

    assert 'carried["elapsed_sec"]' not in body, (
        "the parity case reads the slot clock (carried[\"elapsed_sec\"]) "
        "again. The panel draws the plan clock, and the slot clock is a "
        "different clock: a comparison against it cannot be about what "
        "the panel draws."
    )


def test_waits_for_served_model() -> None:
    """The case must wait for the panel to draw the model it was fed.

    The panel is fed a model every few seconds and draws the one it
    was fed last.  Reading the panel in the middle of that cycle reads
    a drawing of an older model — the very race that made the case
    red on CI, where the panel said ``2s`` and the read model said
    ``5s``.

    So the case waits until the panel has drawn the model it was fed,
    and the comparison is made after that wait.  The wait is the
    driver's, because the driver holds the page: the driver waits, and
    it waits by the case's own rule.
    """
    bodies = _functions(_source(_OPERATOR_E2E))

    assert "_wait_for_panel_parity" in bodies, (
        "the operator's end-to-end case has no _wait_for_panel_parity. "
        "The panel draws the read model it read at its last poll, so the "
        "case has to wait until the panel has drawn the model it was fed "
        "before it compares the two."
    )

    arguments = _arguments(bodies["_wait_for_panel_parity"])
    assert "served_states" in arguments, (
        "the wait does not take the models the panel was fed "
        "(served_states). A wait that cannot see which model the panel "
        "was fed cannot tell whether the panel has drawn it."
    )
    assert "timeout" in arguments, (
        "the wait has no timeout. A wait without one waits forever, and a "
        "panel that never draws the model it was fed is a failure the "
        "case has to report."
    )

    driver = _driver_source()
    assert "_wait_for_panel_parity(" in driver, (
        "the browser driver never waits for the panel to draw the read "
        "model it was fed. The driver holds the page, so the driver is "
        "where the wait has to happen."
    )


def test_widgets_are_read_from_the_draw_that_was_waited_for() -> None:
    """The widgets must be read after the panel has drawn the model.

    The panel draws the capacity widgets, the line and the running
    plans in **one** pass, out of **one** read model
    (``renderScheduler`` in ``frontend/app.js``).  So the drawing on the
    screen is a single fact, and a reading taken before the panel has
    drawn that model is a reading of nothing at all.

    That is not a theoretical worry.  The server answers the read
    model's endpoint with the *empty* document when it cannot read the
    scheduler's snapshot
    (``backend/routes/scheduler.py``: ``_empty_snapshot()``), and the
    empty document says ``used = 0``.  A case that reads the widget
    before the panel has drawn a real model reads that zero and calls
    it a disagreement with the read model.

    So the widgets are read after the wait — the wait being what
    guarantees that the panel has drawn a model whose running plans are
    the ones that are really running.
    """
    driver = _driver_source()

    wait_at = driver.find("_wait_for_panel_parity(")
    assert wait_at != -1, (
        "the driver never waits for the panel to draw the read model it "
        "was fed, so there is no draw for the widgets to be read from."
    )

    for selector in ("#scheduler-capacity", "#scheduler-view-capacity"):
        read_at = driver.find('read_capacity(page, "{0}")'.format(selector))
        assert read_at != -1, (
            "the driver never reads the capacity widget {0!r}. The case "
            "compares the panel with the read model, and the widget is "
            "one of the things it compares.".format(selector)
        )
        assert read_at > wait_at, (
            "the driver reads the capacity widget {0!r} before it waits "
            "for the panel to draw the read model it was fed. The widget "
            "is drawn in the same pass as the running plans, so a reading "
            "taken before that wait may read the panel's first frame — "
            "which says nothing about the model it was fed.".format(selector)
        )


def test_the_model_is_taken_before_the_panel_is_read() -> None:
    """The read model must be chosen before the panel is read.

    A poll answer that lands while the panel is being read belongs to a
    **later** moment than the drawing on the screen: the panel drew the
    model it was fed at its last poll, and a model that arrived during
    the reading was never fed to the panel.

    So the number of the answers the server gave is counted **before**
    the panel is read, and the model is chosen out of those answers
    only.  A model chosen out of the whole list — counted afterwards —
    is a model the panel may not have drawn at all.
    """
    driver = _driver_source()

    assert "served_state(served_states," not in driver, (
        "the driver chooses the read model the panel was fed out of the "
        "whole list of answers, taken after the panel was read. An answer "
        "that lands during the reading was never fed to the panel, so a "
        "model chosen out of it is not the model the panel drew."
    )

    body = _driver_parity_branch()
    branch = _assigned_names(body)
    for name in ("served_count", "running", "panel_state"):
        assert name in branch, (
            "the driver's own branch never binds {0!r}. The driver reads "
            "the panel without waiting for it, and that is the reading "
            "whose read model has to be chosen before the panel is read."
            .format(name)
        )

    order = ["served_count", "running", "panel_state"]
    positions = [body.index(branch[name]) for name in order]
    assert positions == sorted(positions), (
        "the driver reads the panel before it counts the answers the "
        "server gave it. An answer that lands during the reading was "
        "never fed to the panel, so the count has to be taken first: the "
        "count, then the reading, then the model chosen out of the "
        "answers counted."
    )

    counted = ast.dump(branch["served_count"].value)
    assert "served_states" in counted and "len" in counted, (
        "the driver does not count the answers the server gave the panel "
        "(len(served_states)). Without the count, the model the panel was "
        "fed is chosen out of a list that may have grown while the panel "
        "was being read."
    )

    chosen = ast.dump(branch["panel_state"].value)
    assert "Subscript" in chosen and "served_count" in chosen, (
        "the driver does not choose the read model out of the answers it "
        "counted. The model has to be chosen out of the answers the "
        "server gave the panel **before** the panel was read, and not out "
        "of the whole list."
    )


def test_parity_bound_is_not_a_written_down_number() -> None:
    """How far apart two readings may be must come from the panel.

    The bound is the panel's own poll period plus the gap between the
    two readings, and it is computed by ``_panel_parity_bound``.  A
    number written into the comparison instead is a number nobody can
    defend: it says how old the panel's drawing may be without saying
    why.
    """
    body = _case_function("test_panel_and_api_parity")

    assert "_panel_parity_bound(" in body, (
        "the parity case does not compute how far apart two readings of "
        "one clock may be (_panel_parity_bound). The bound is the "
        "panel's own poll period plus the measured gap, and a number "
        "written down instead is a tolerance nobody can defend."
    )

    driver = _driver_source()
    assert "PANEL_POLL_SECONDS" in _source(_OPERATOR_E2E), (
        "the operator's end-to-end case does not name the panel's poll "
        "period (PANEL_POLL_SECONDS). How old the panel's drawing may be "
        "is a property of the panel, and a case that does not name it "
        "cannot say how far apart two readings may be."
    )
