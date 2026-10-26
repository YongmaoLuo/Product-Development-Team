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
