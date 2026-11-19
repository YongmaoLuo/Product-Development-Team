"""The frontend must speak the merged waiting phase: ``queued``.

Why this gate exists
--------------------
The waiting phase used to be called ``ready``. It is now called
``queued`` — one word for one situation, decided in
``backend/phase_vocabulary.py`` (the vocabulary) and labelled in
``backend/plan_state.PHASE_LABELS`` (the label 「排队中」). The backend
writes the new word; ``frontend/app.js`` still knows only the old one,
and none of the mismatches fails loudly:

1. ``phaseLabel`` has no ``queued`` entry, so a queued plan renders as
   the raw string ``queued`` in the sidebar instead of 「排队中」.
2. ``phaseClass`` does not list ``queued``, so the sidebar pill falls
   back to ``phase-default`` — the grey used for phases nobody knows.
3. The plan card's status map has no ``queued`` entry, so the card
   prints the raw status where every other status prints a Chinese
   label.
4. ``resumePlan`` routes only ``ready`` into the tasks view, so a plan
   that is queued for execution opens on the interview screen — the
   screen that has nothing to do with where the plan actually is.
5. ``resumeVerificationPanel`` computes ``canStartVerification`` from
   ``["ready", "executing"]``, so the verification panel stays hidden
   for a queued plan.

The page is not broken; it lies. Every one of these is a place where
the operator is shown a phase word the rest of the system does not
use, which is exactly the confusion the merge was meant to remove.

The rule
--------
``queued`` is the waiting phase in the frontend, and it renders as
「排队中」. The historical alias ``ready`` keeps **no** entry in the
label maps: the backend does not report it any more, so a table that
still maps it to 「就绪」 shows the operator a word nobody else uses.
It still works as a *phase* on the read path — a plan row written
before the merge still reads ``ready`` — but it must never be rendered.

The same rule, one step further
------------------------------
The execution phases — ``running``, ``failed``, ``completed``,
``stopped``, ``verification``, ``conflict`` and ``archived`` — are
named in the sidebar as well. A plan in one of them used to render as
the raw English word (``running``) and to fall back to the grey used
for phases nobody knows (``phase-default``): the operator was shown
``running`` where every other surface said 「执行中」.

The rules below pin the word each of those phases wears, the colour it
wears it in, and the fact that the colours are the sheet's own
variables — a colour spelled with a variable the sheet does not define
is an unstyled pill, and it looks like a phase nobody knows.

Scan target
-----------
``frontend/app.js`` and ``frontend/style.css``, read as source. These
are shape gates, like the rest of ``static_gates/``: they run on a
plain Python environment with no JS toolchain, so they pin the
contract in the source rather than executing the page. A ``queued``
pill without a CSS rule renders as an unstyled chip, which is why the
stylesheet is checked too.

Requests are not checked here: ``frontend/app.js`` reaching the API
through bare ``fetch`` is already covered by
``backend/tests/static_gates/test_frontend_uses_api_wrapper.py``.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"
_STYLE_CSS = _REPO_ROOT / "frontend" / "style.css"

#: The label the backend publishes for the waiting phase
#: (``backend/plan_state.PHASE_LABELS['queued']``). The frontend keeps
#: its own copy of the map — there is no build step to import a Python
#: constant from — so the two are pinned to the same string here.
_QUEUED_LABEL = 'queued: "排队中"'

#: The legacy label the frontend must no longer carry. The waiting
#: phase is ``queued`` now and the backend never reports ``ready``, so
#: a map that still maps it to 「就绪」 shows a word nobody else uses.
_LEGACY_READY_LABEL = 'ready: "就绪"'

#: The phases a plan spends the rest of its life in, and the words the
#: sidebar must show for them. The backend writes the phase word; the
#: frontend keeps its own copy of the label map — there is no build
#: step that could import a Python constant from — so the copy is
#: pinned to these strings here.
_EXECUTION_PHASE_LABELS = {
    "running": "执行中",
    "failed": "失败",
    "completed": "已完成",
    "stopped": "已停止",
    "verification": "验证中",
    "conflict": "冲突",
    "archived": "已归档",
}

#: The colour each of those phases wears in the sidebar pill, spelled
#: with the variables the sheet already defines. A phase with no rule
#: of its own falls back to ``phase-default`` — the grey used for
#: phases nobody knows — so a plan in the middle of a run would look
#: exactly like a plan nobody has heard of.
_EXECUTION_PHASE_COLOURS = {
    "running": ("var(--primary-bg)", "var(--primary)"),
    "failed": ("var(--danger-bg)", "var(--danger)"),
    "completed": ("var(--success-bg)", "var(--success)"),
    "stopped": ("var(--warning-bg)", "var(--warning)"),
    "verification": ("var(--info-bg)", "var(--info)"),
    "conflict": ("var(--danger-bg)", "var(--danger)"),
    "archived": ("var(--surface2)", "var(--text-dim)"),
}


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


def _function_body(source: str, name: str) -> str:
    """Return the body text of the top-level ``function name(...)``.

    The body ends at the first line that is exactly ``}`` — the file's
    top-level functions close at column zero, so that line ends the
    function no matter what the body contains. Brace counting would be
    the alternative, and it miscounts the template literals this file
    is full of.

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


def _guard_condition(body: str, call: str) -> str:
    """Return the ``else if (...)`` condition that guards ``call``.

    The condition is the nearest preceding line that opens a branch —
    the branch that decides *which view* a plan opens on. Returns the
    raw line so the caller can read both phase names off it.
    """
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if call not in line:
            continue
        for previous in range(index - 1, -1, -1):
            stripped = lines[previous].strip()
            if stripped.startswith("} else if") or stripped.startswith("else if"):
                return stripped
    raise AssertionError(f"no branch found guarding {call!r}")


def _css_rule_bodies(css: str, class_name: str) -> list[str]:
    """Return the declaration bodies of every rule selecting ``class_name``.

    A rule may be written on its own (``.phase-queued { ... }``) or
    share a selector with its sibling (``.phase-ready, .phase-queued {
    ... }``); both are found, and both count as "this class is styled".
    """
    bodies: list[str] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector = match.group(1)
        if class_name in selector:
            bodies.append(match.group(2))
    return bodies


def _declarations(body: str) -> set[str]:
    """Return the normalised declarations of a CSS rule body."""
    return {
        " ".join(declaration.split())
        for declaration in body.split(";")
        if declaration.strip()
    }


def _root_variables(css: str) -> set[str]:
    """Return the custom properties the sheet defines in ``:root``.

    A colour spelled with a variable the sheet does not define is not
    a colour: the browser drops the declaration and the pill renders
    unstyled — which is what a phase nobody knows looks like anyway.

    Raises :class:`AssertionError` when the sheet has no ``:root``
    block, so a renamed selector fails loudly instead of matching
    nothing and passing.
    """
    match = re.search(r":root\s*\{([^}]*)\}", css)
    assert match is not None, (
        "style.css has no :root block, so it defines no custom "
        "properties and no phase rule can be spelled with one."
    )
    return set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:", match.group(1)))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_phase_label_maps_queued_to_chinese() -> None:
    """``phaseLabel('queued')`` must be 「排队中」, not the raw phase name.

    The label lives in the backend (``plan_state.PHASE_LABELS``); the
    frontend keeps its own copy because there is no build step that
    could import it. Two copies of one string is exactly the shape that
    drifts, so the copy is pinned to the backend's spelling here.

    ``ready`` must be **gone** from the map: the backend does not
    report it any more, so a map that still carries it can only render
    a word nobody else uses.
    """
    body = _function_body(_source(_APP_JS), "phaseLabel")

    assert _QUEUED_LABEL in body, (
        "phaseLabel() has no `queued` entry, so a queued plan renders as "
        "the raw string `queued` in the sidebar. The backend labels this "
        "phase 「排队中」; the frontend must show the same word."
    )

    assert _LEGACY_READY_LABEL not in body, (
        "phaseLabel() still maps `ready` to 「就绪」. The backend never "
        "reports `ready` any more — the waiting phase is `queued` — so "
        "this entry can only show a word the system does not use."
    )


def test_phase_class_knows_queued() -> None:
    """``phaseClass('queued')`` must be ``phase-queued``, not the default.

    The class carries the colour. A phase that is not in the ``known``
    list falls back to ``phase-default`` — the grey used for phases
    nobody knows. The enumeration must not name ``ready``: the waiting
    phase is ``queued`` now, and a phase nobody reports gets no colour.
    """
    body = _function_body(_source(_APP_JS), "phaseClass")

    match = re.search(r"known\s*=\s*\[(.*?)\]", body, re.DOTALL)
    assert match is not None, (
        "phaseClass() no longer declares its `known` list of phases; the "
        "gate cannot tell which phases get their own colour."
    )

    known = match.group(1)
    assert '"queued"' in known or "'queued'" in known, (
        "phaseClass() does not know the `queued` phase, so a queued plan "
        "falls back to `phase-default` — the grey used for unknown "
        "phases."
    )

    assert '"ready"' not in known and "'ready'" not in known, (
        "phaseClass() still lists `ready` as a known phase; the phase "
        "enumeration must not name the merged-away word."
    )


def test_plan_card_status_map_includes_queued() -> None:
    """The plan card must print 「排队中」 for a queued plan.

    The card's status map is the second place the sidebar is not: it
    renders the same phase in a different list. A card that prints the
    raw status while the sidebar prints 「排队中」 shows the operator
    two different words for one plan.

    The legacy ``ready`` label must be gone here too: the backend does
    not report it any more, so the card can only show a word nobody
    else uses.
    """
    body = _function_body(_source(_APP_JS), "loadPlans")

    assert _QUEUED_LABEL in body, (
        "the plan card's status map has no `queued` entry, so a queued "
        "plan prints its raw status instead of 「排队中」."
    )

    assert _LEGACY_READY_LABEL not in body, (
        "the plan card's status map still maps `ready` to 「就绪」. The "
        "backend never reports `ready` any more — the waiting phase is "
        "`queued` — so this entry can only show a word nobody else uses."
    )


def test_tasks_view_branch_accepts_queued() -> None:
    """A queued plan must open on the tasks view.

    The branch that routes a plan to the tasks view is keyed on the
    phase. With only ``ready`` in it, a plan that is queued for
    execution falls through to the interview screen — the operator
    resumes a plan that is waiting to start and lands on a screen that
    asks them to re-interview it.

    ``ready`` stays in the branch: a plan row written before the merge
    still reads ``ready``, and the read path only normalises the
    vocabulary for the *backend*'s own checks.
    """
    body = _function_body(_source(_APP_JS), "resumePlan")
    condition = _guard_condition(body, "showTasksView(")

    assert '"queued"' in condition, (
        "the branch that routes a plan to the tasks view does not accept "
        "`queued`, so a queued plan opens on the interview screen. The "
        "waiting phase is `queued` now."
    )

    assert '"ready"' in condition, (
        "the tasks-view branch dropped `ready`. A plan row written "
        "before the merge still reads `ready`, and it must still reach "
        "the tasks view."
    )


def test_tasks_view_branch_accepts_execution_terminal_phases() -> None:
    """A failed or stopped plan must open on the tasks view.

    ``failed`` and ``stopped`` are the **execution** terminal phases:
    the plan ran, and the run ended.  Where it ended and why is exactly
    what the operator opened the plan to see — the verification panel
    carries the stop reason, the round counter, the results and the
    repair list.  A plan whose verdict is already written down is not a
    plan that needs re-interviewing.

    Routing these two phases to the interview screen hides all of it:
    the operator lands on a chat window and is asked to describe the
    requirement again, while the stop reason stays in the database.  The
    interview fallback is for the plans that really have no later stage
    to look at.
    """
    body = _function_body(_source(_APP_JS), "resumePlan")
    condition = _guard_condition(body, "showTasksView(")

    assert '"failed"' in condition, (
        "the branch that routes a plan to the tasks view does not accept "
        "`failed`, so a failed plan opens on the interview screen and the "
        "verification panel — the stop reason and the result — stays out "
        "of sight."
    )

    assert '"stopped"' in condition, (
        "the branch that routes a plan to the tasks view does not accept "
        "`stopped`, so a stopped plan opens on the interview screen. A "
        "stopped run is the one the operator most needs to look at."
    )


def test_can_start_verification_includes_queued() -> None:
    """A queued plan must be able to start the verification branch.

    ``canStartVerification`` decides whether the verification panel is
    shown for a plan that has not started verifying yet. With
    ``ready`` alone in the list, a queued plan is a plan the operator
    cannot verify from the UI — the panel stays hidden even though the
    plan is exactly in the state that is allowed to start.
    """
    body = _function_body(_source(_APP_JS), "resumeVerificationPanel")

    match = re.search(r"canStartVerification\s*=\s*(.+)", body)
    assert match is not None, (
        "resumeVerificationPanel() no longer computes "
        "`canStartVerification`; the gate cannot tell which phases may "
        "start the verification branch."
    )

    expression = match.group(1)
    assert '"queued"' in expression, (
        "`canStartVerification` does not include `queued`, so the "
        "verification panel stays hidden for a plan that is queued and "
        "therefore allowed to start verifying."
    )

    assert '"executing"' in expression, (
        "`canStartVerification` lost `executing`. A running plan is "
        "still allowed to start the verification branch."
    )


def test_style_has_phase_queued_class() -> None:
    """``.phase-queued`` must be styled, and it must look like ready.

    ``phaseClass`` puts the class on the sidebar pill, and the sheet
    has one rule per phase. Without a ``.phase-queued`` rule the pill
    falls back to no background at all — a chip that reads as
    unstyled rather than as the waiting phase.

    The colours are the ones ``.phase-ready`` uses: the two words name
    the same situation, and a plan should not change colour because it
    was written before the merge.
    """
    css = _source(_STYLE_CSS)

    queued_bodies = _css_rule_bodies(css, ".phase-queued")
    assert queued_bodies, (
        "style.css has no .phase-queued rule; the queued pill renders "
        "unstyled."
    )

    ready_bodies = _css_rule_bodies(css, ".phase-ready")
    assert ready_bodies, (
        "style.css has no .phase-ready rule; there is nothing for "
        ".phase-queued to reuse."
    )

    queued = {declaration for body in queued_bodies for declaration in _declarations(body)}
    ready = {declaration for body in ready_bodies for declaration in _declarations(body)}

    assert queued == ready, (
        ".phase-queued does not use .phase-ready's colours. The two "
        f"words name the same situation; expected {sorted(ready)}, got "
        f"{sorted(queued)}."
    )


def test_phase_label_names_the_execution_phases() -> None:
    """Every execution phase must have a Chinese label in the sidebar.

    The backend reports the phase word; the sidebar shows a label. A
    phase with no entry in the map is shown as the raw word it is —
    ``running``, ``failed``, ``archived`` — so the operator reads the
    one surface of the page that speaks English while every other
    surface speaks the system's own vocabulary.

    The words are pinned here because the frontend keeps its own copy
    of the map: there is no build step that could import the backend's
    labels, so two copies of one string is exactly the shape that
    drifts.
    """
    body = _function_body(_source(_APP_JS), "phaseLabel")

    for phase, label in _EXECUTION_PHASE_LABELS.items():
        assert f'{phase}: "{label}"' in body, (
            f"phaseLabel() has no `{phase}` entry, so a plan in that "
            f"phase renders as the raw string `{phase}` in the sidebar "
            f"instead of 「{label}」."
        )


def test_phase_class_knows_the_execution_phases() -> None:
    """Every execution phase must have a class of its own.

    The class carries the colour. A phase that is not in the ``known``
    list falls back to ``phase-default`` — the grey used for phases
    nobody knows — so a plan that is running looks exactly like a plan
    the page has never heard of.
    """
    body = _function_body(_source(_APP_JS), "phaseClass")

    match = re.search(r"known\s*=\s*\[(.*?)\]", body, re.DOTALL)
    assert match is not None, (
        "phaseClass() no longer declares its `known` list of phases; the "
        "gate cannot tell which phases get their own colour."
    )

    known = match.group(1)
    for phase in _EXECUTION_PHASE_LABELS:
        assert f'"{phase}"' in known, (
            f"phaseClass() does not know the `{phase}` phase, so a plan "
            "in that phase falls back to `phase-default` — the grey "
            "used for unknown phases."
        )


def test_style_has_a_colour_for_every_execution_phase() -> None:
    """Every execution phase must be styled, in the sheet's own colours.

    Without a rule of its own the pill has no background at all: an
    unstyled chip that reads as a phase nobody knows. The colours are
    pinned to the variables the sheet already defines — the running
    phase in the primary colour, a failure and a conflict in the
    danger colour, a finished plan in the success colour, a stopped
    run in the warning colour, verification in the info colour, and an
    archived plan in the grey.

    The variables are checked against ``:root``: a colour spelled with
    a variable the sheet does not define is dropped by the browser, so
    the pill would render unstyled while the rule still reads as a
    colour.
    """
    css = _source(_STYLE_CSS)
    variables = _root_variables(css)

    for phase, (background, colour) in _EXECUTION_PHASE_COLOURS.items():
        bodies = _css_rule_bodies(css, f".phase-{phase}")
        assert bodies, (
            f"style.css has no .phase-{phase} rule; the {phase} pill "
            "renders unstyled."
        )

        declarations = {
            declaration for body in bodies for declaration in _declarations(body)
        }
        expected = {f"background: {background}", f"color: {colour}"}
        assert declarations == expected, (
            f".phase-{phase} does not wear the colours the phase wears "
            f"in the rest of the sheet; expected {sorted(expected)}, got "
            f"{sorted(declarations)}."
        )

        for used in re.findall(r"var\(\s*(--[A-Za-z0-9_-]+)", " ".join(declarations)):
            assert used in variables, (
                f".phase-{phase} is spelled with `{used}`, which "
                "style.css does not define in :root. The browser drops "
                "the declaration and the pill renders unstyled."
            )
