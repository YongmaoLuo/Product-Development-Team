"""The scheduler's card detail must be *drawn*, not only structured.

Why this gate exists
--------------------
The detail endpoint answers with a plan's progress as **sections** — a
status header with a colour and an elapsed time, a phase banner, a
progress bar, the running tasks — and the panel draws each of them
through a class of its own.  A class with no rule is not a small
problem: the browser falls back to its own defaults, and the panel ends
up showing the plan's status in the body font, the progress bar in a
proportional font (so the bar does not line up), the elapsed time in
the middle of the row instead of at the end of it, and the failed
tasks in the same colour as the running ones.

So the section styles are pinned here:

* every class the card's sections are drawn with has a rule of its own;
* the status colour is drawn with the palette the panel already has —
  five colours, each mapped to one variable, and a colour the panel
  does not know falls back to the quiet one;
* the progress bar is monospaced, because the bar *is* characters;
* a task's icon is drawn with the variable that says what state the
  task is in;
* nothing new is brought in: no framework, no build step, no new
  dependency.  The panel is a handful of hand-written files and stays
  that way.

Scan target
-----------
``frontend/style.css`` and ``frontend/index.html``, read as source.
These are shape gates, like the rest of ``static_gates/``: they run on
a plain Python environment with no browser, so they pin the contract in
the source rather than rendering the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
_STYLE_CSS = _REPO_ROOT / "frontend" / "style.css"
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"
_FRONTEND_DIR = _REPO_ROOT / "frontend"

#: Every class the card's sections are drawn with.  Each one must have a
#: rule of its own — a class the stylesheet does not know is drawn in
#: whatever the browser's defaults happen to be.
_SECTION_CLASSES = (
    "scheduler-detail-status",
    "scheduler-detail-status-dot",
    "scheduler-detail-phase-badge",
    "scheduler-detail-duration",
    "scheduler-detail-section",
    "scheduler-detail-section-title",
    "scheduler-detail-bar",
    "scheduler-detail-pct",
    "scheduler-detail-muted",
    "scheduler-task-row",
    "scheduler-task-icon",
)

#: The classes the section renderer writes *besides* the ones above.
#: They are part of the same stylesheet block, so they are held to the
#: same rules: the palette, and a rule of their own.
_SIDECAR_CLASSES = (
    "scheduler-detail-plan-id",
    "scheduler-detail-text",
    "scheduler-detail-counts",
    "scheduler-task-id",
    "scheduler-task-title",
)

#: Every class the section styles are allowed to touch.
_STYLED_CLASSES = _SECTION_CLASSES + _SIDECAR_CLASSES

#: The colours a plan's status can be, and the variable each one is
#: drawn in.  The colour is the backend's — the panel draws it, it does
#: not decide it.
_STATUS_COLOR_VARS = {
    "green": "--success",
    "red": "--danger",
    "orange": "--warning",
    "blue": "--info",
    "grey": "--text-dim",
}

#: The colour a status the panel does not know is drawn in.
_FALLBACK_COLOR_VAR = "--text-dim"

#: The states a task row can be in, and the variable each one's icon is
#: drawn in.
_TASK_STATUS_VARS = {
    "in_progress": "--info",
    "blocked": "--warning",
    "failed": "--danger",
}

#: The palette the section styles are drawn from.  Nothing outside it
#: may be used: the panel's own colours are the ones the rest of the
#: page is drawn with, and a new colour is a new design decision that
#: belongs to nobody.
_ALLOWED_VARS = frozenset(
    {
        "--surface",
        "--surface2",
        "--surface3",
        "--border",
        "--text",
        "--text-dim",
        "--success",
        "--warning",
        "--danger",
        "--info",
        "--radius",
        "--radius-sm",
    }
)

#: The artefacts a build step or a new dependency would leave in the
#: front end.  The panel is served as it is written: no bundler, no
#: package manager, no framework.
_BUILD_ARTIFACTS = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "node_modules",
    "vite.config.js",
    "vite.config.ts",
    "webpack.config.js",
    "rollup.config.js",
    "postcss.config.js",
    ".babelrc",
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


def _without_comments(css: str) -> str:
    """Return ``css`` with its comments taken out.

    A comment is prose, not a selector: leaving one in would let a
    comment that mentions a class (or an attribute) look like a rule
    that styles it.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _rules(css: str) -> list[tuple[str, str]]:
    """Return ``(selector, body)`` for every rule in ``css``.

    A rule may be written on its own or share its selector with a
    sibling; a shared selector is returned once per selector, so a
    sibling's declarations are found as well.
    """
    rules: list[tuple[str, str]] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", _without_comments(css)):
        for selector in match.group(1).split(","):
            selector = selector.strip()
            if selector:
                rules.append((selector, match.group(2)))
    return rules


def _declares_class(selector: str, class_name: str) -> bool:
    """Whether ``selector`` styles the class ``class_name``.

    The class must be the whole name: ``.scheduler-detail-status`` does
    not style ``.scheduler-detail-status-dot``.
    """
    return re.search(r"\." + re.escape(class_name) + r"(?![\w-])", selector) is not None


def _bodies_for_class(css: str, class_name: str) -> list[str]:
    """Return the bodies of every rule that styles ``class_name``."""
    return [
        body for selector, body in _rules(css) if _declares_class(selector, class_name)
    ]


def _plain_bodies_for_class(css: str, class_name: str) -> list[str]:
    """Return the bodies of the rules that style ``class_name`` on its own.

    A rule that keys on an attribute — a status colour, a task state —
    only applies when the element carries that attribute, so it is not
    the class's own rule and cannot stand in for it.
    """
    return [
        body
        for selector, body in _rules(css)
        if _declares_class(selector, class_name) and "[" not in selector
    ]


def _rules_for_attribute(css: str, attribute: str) -> list[tuple[str, str]]:
    """Return ``(selector, body)`` for every rule keyed on ``attribute``."""
    return [
        (selector, body)
        for selector, body in _rules(css)
        if attribute in selector
    ]


def _declarations(body: str) -> set[str]:
    """Return the normalised declarations of a CSS rule body."""
    return {
        " ".join(declaration.split())
        for declaration in body.split(";")
        if declaration.strip()
    }


def _vars_in(bodies: list[str]) -> set[str]:
    """Return the custom properties the given rule bodies use."""
    found: set[str] = set()
    for body in bodies:
        for name in re.findall(r"var\(\s*(--[\w-]+)", body):
            found.add(name)
    return found


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def check_every_section_class_is_styled(css: str) -> None:
    """Every class the card's sections are drawn with must have a rule.

    The class is how the panel says what a piece of the detail *is*.
    A class the stylesheet does not know is a piece of the detail drawn
    in the browser's defaults: the operator cannot tell it from the
    text around it.
    """
    for class_name in _SECTION_CLASSES:
        bodies = _bodies_for_class(css, class_name)
        assert bodies, (
            f"style.css has no .{class_name} rule. The card's sections "
            "are drawn with this class, and without a rule the piece of "
            "the detail it carries is drawn in the browser's defaults."
        )
        assert any(body.strip() for body in bodies), (
            f"style.css has an empty .{class_name} rule. A rule that says "
            "nothing draws the same as no rule at all."
        )


def check_status_colors_use_existing_vars(css: str) -> None:
    """The status colours must be drawn with the palette's own variables.

    The colour is the backend's — it says what state the plan is in —
    so the panel has to have a colour for each of the five it sends,
    and each of them has to be one of the panel's own variables.  A
    colour the panel does not know falls back to the quiet one, so an
    unknown state is drawn as "nothing to see here" rather than as
    something loud.
    """
    for color, variable in _STATUS_COLOR_VARS.items():
        attribute = f'data-status-color="{color}"'
        rules = _rules_for_attribute(css, attribute)
        assert rules, (
            f"style.css has no rule for {attribute}. The backend sends "
            f"this colour, and without a rule for it the status is drawn "
            "in whatever colour the element happens to have."
        )
        used = _vars_in([body for _, body in rules])
        assert variable in used, (
            f"the {attribute} rule does not use {variable}; it uses "
            f"{sorted(used)}. Each of the five colours the backend sends "
            "is drawn in its own variable."
        )

    dot_bodies = _plain_bodies_for_class(css, "scheduler-detail-status-dot")
    assert dot_bodies, (
        "style.css has no .scheduler-detail-status-dot rule of its own. "
        "The colour rules only apply when the element carries a "
        "data-status-color, so a colour the panel does not know would "
        "have nothing to fall back to."
    )
    fallback = f"background: var({_FALLBACK_COLOR_VAR})"
    declarations = {
        declaration for body in dot_bodies for declaration in _declarations(body)
    }
    assert fallback in declarations, (
        f"the .scheduler-detail-status-dot rule does not fall back to "
        f"{fallback!r}; it declares {sorted(declarations)}. A status the "
        "panel does not know must fall back to the quiet colour."
    )

    scoped = [
        body
        for selector, body in _rules(css)
        if any(_declares_class(selector, name) for name in _STYLED_CLASSES)
    ]
    used = _vars_in(scoped)
    unknown = sorted(used - _ALLOWED_VARS)
    assert not unknown, (
        f"the card's section styles use {unknown}, which are not the "
        f"panel's own variables ({sorted(_ALLOWED_VARS)}). A new colour "
        "is a new design decision, and it belongs to the panel as a "
        "whole — not to one card."
    )


def check_bar_is_monospace(css: str) -> None:
    """The progress bar must be drawn in a monospaced font.

    The bar is characters the backend sends, one column each.  In a
    proportional font the columns do not line up, and the bar reads as
    a smear of blocks instead of a bar.
    """
    bodies = _bodies_for_class(css, "scheduler-detail-bar")
    assert bodies, (
        "style.css has no .scheduler-detail-bar rule. The bar is drawn "
        "with the characters the backend sends, and without a rule it is "
        "drawn in the body font."
    )
    declarations = {
        declaration for body in bodies for declaration in _declarations(body)
    }
    families = [
        declaration
        for declaration in declarations
        if declaration.startswith("font-family:")
    ]
    assert families, (
        f"the .scheduler-detail-bar rule declares no font-family; it "
        f"declares {sorted(declarations)}."
    )
    assert any("monospace" in family for family in families), (
        f"the .scheduler-detail-bar rule does not use a monospaced font; "
        f"it declares {sorted(families)}. The bar's columns have to line "
        "up."
    )


def check_task_status_icons_use_semantic_vars(css: str) -> None:
    """A task's icon must be drawn in the variable of its own state.

    The icon is the only thing that says what state the task is in, so
    it has to be drawn in that state's colour — and the three states
    the panel knows must each have one.
    """
    for status, variable in _TASK_STATUS_VARS.items():
        attribute = f'data-task-status="{status}"'
        rules = _rules_for_attribute(css, attribute)
        assert rules, (
            f"style.css has no rule for {attribute}. The task rows carry "
            "this state, and without a rule for it the icon is drawn in "
            "the same colour as every other row's."
        )
        selectors = [selector for selector, _ in rules]
        assert any(
            _declares_class(selector, "scheduler-task-icon") for selector in selectors
        ), (
            f"the {attribute} rules do not style .scheduler-task-icon; "
            f"they style {sorted(selectors)}. The icon is what says what "
            "state the task is in."
        )
        used = _vars_in([body for _, body in rules])
        assert variable in used, (
            f"the {attribute} rules do not use {variable}; they use "
            f"{sorted(used)}. Each state is drawn in its own variable."
        )


def check_no_new_dependency_or_framework(css: str, html: str) -> None:
    """The front end must stay four hand-written files.

    No stylesheet is imported, no asset is fetched from elsewhere, and
    no build step has appeared.  The panel is served as it is written;
    a framework or a bundler in here would change how every other page
    in the product is built.
    """
    assert "@import" not in css, (
        "style.css imports another stylesheet. The panel's styles are "
        "written in the one file it is served with; an import is how a "
        "framework sneaks in."
    )
    assert re.search(r"url\(\s*['\"]?(?:https?:)?//", css) is None, (
        "style.css fetches an asset from somewhere else. The panel's "
        "styles must not depend on a third party being up."
    )
    assert re.search(
        r"<script\b[^>]*\bsrc\s*=\s*['\"]?(?:https?:)?//", html
    ) is None, (
        "index.html loads a script from somewhere else. The panel is "
        "served as it is written."
    )
    assert re.search(
        r"<link\b[^>]*\bhref\s*=\s*['\"]?(?:https?:)?//", html
    ) is None, (
        "index.html loads a stylesheet or a font from somewhere else. "
        "The panel is served as it is written."
    )
    for artifact in _BUILD_ARTIFACTS:
        assert not (_FRONTEND_DIR / artifact).exists(), (
            f"frontend/{artifact} exists. A build step or a package "
            "manager in the front end is a new dependency, and the panel "
            "is served as it is written."
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_section_class_is_styled() -> None:
    """Every class the card's sections are drawn with must have a rule.

    The card's detail is a stack of sections, and each piece of a
    section — the heading, the dot, the badge, the elapsed time, the
    bar, the percentage, the task rows — is a class of its own.  A
    class with no rule is a piece of the detail the operator cannot
    tell apart from the text around it.
    """
    check_every_section_class_is_styled(_source(_STYLE_CSS))


def test_status_colors_use_existing_vars() -> None:
    """The five status colours must each be drawn in a panel variable.

    The colour is the backend's, so the panel needs a colour for each
    of the five it sends — and each of them has to be one of the
    panel's own variables, so the card is drawn in the same palette as
    the rest of the page.
    """
    check_status_colors_use_existing_vars(_source(_STYLE_CSS))


def test_bar_is_monospace() -> None:
    """The progress bar must be drawn in a monospaced font.

    The bar is characters the backend sends, one column each; in a
    proportional font they do not line up.
    """
    check_bar_is_monospace(_source(_STYLE_CSS))


def test_task_status_icons_use_semantic_vars() -> None:
    """A task's icon must be drawn in the variable of its own state.

    Running, held up and failed are three different things, and the
    icon is the only thing that says which one it is.
    """
    check_task_status_icons_use_semantic_vars(_source(_STYLE_CSS))


def test_no_new_dependency_or_framework() -> None:
    """The front end must stay as it is written.

    No import, no remote asset, no bundler: the panel is served as the
    files in it.
    """
    check_no_new_dependency_or_framework(_source(_STYLE_CSS), _source(_INDEX_HTML))


def test_the_styling_gate_has_teeth() -> None:
    """Negative control: a class with no rule must fail.

    The fixture is the stylesheet with the bar's rule renamed — every
    other class is still styled, so a gate that only looked for "some
    rule somewhere" would accept it.
    """
    css = _source(_STYLE_CSS)

    broken = css.replace(".scheduler-detail-bar {", ".scheduler-detail-bar-elsewhere {", 1)
    assert broken != css, (
        "the fixture no longer renames the bar's rule, so the negative "
        "control would prove nothing."
    )

    with pytest.raises(AssertionError):
        check_every_section_class_is_styled(broken)


def test_the_status_colour_gate_has_teeth() -> None:
    """Negative control: a colour drawn in the wrong variable must fail.

    The fixture is the stylesheet with the green status drawn in the
    danger colour.  The other four colours are still right, so a gate
    that only looked for "some colour somewhere" would accept it.
    """
    css = _source(_STYLE_CSS)

    broken = css.replace(
        '[data-status-color="green"] .scheduler-detail-status-dot '
        "{ background: var(--success); }",
        '[data-status-color="green"] .scheduler-detail-status-dot '
        "{ background: var(--danger); }",
        1,
    )
    assert broken != css, (
        "the fixture no longer draws the green status in the wrong "
        "colour, so the negative control would prove nothing."
    )

    with pytest.raises(AssertionError):
        check_status_colors_use_existing_vars(broken)


def test_the_bar_gate_has_teeth() -> None:
    """Negative control: a bar in the body font must fail.

    The fixture is the stylesheet with the bar's font-family replaced
    by the page's own — the rule is still there, so a gate that only
    looked for the rule would accept it.
    """
    css = _source(_STYLE_CSS)

    broken = css.replace(
        "font-family: ui-monospace, Menlo, monospace;\n    letter-spacing: 1px;",
        "font-family: inherit;\n    letter-spacing: 1px;",
        1,
    )
    assert broken != css, (
        "the fixture no longer replaces the bar's font, so the negative "
        "control would prove nothing."
    )

    with pytest.raises(AssertionError):
        check_bar_is_monospace(broken)


def test_the_task_status_gate_has_teeth() -> None:
    """Negative control: an icon drawn in the wrong variable must fail.

    The fixture is the stylesheet with the failed task's icon drawn in
    the running colour.  The other two states are still right, so a
    gate that only looked for "some state somewhere" would accept it.
    """
    css = _source(_STYLE_CSS)

    broken = css.replace(
        '[data-task-status="failed"] .scheduler-task-icon { color: var(--danger); }',
        '[data-task-status="failed"] .scheduler-task-icon { color: var(--info); }',
        1,
    )
    assert broken != css, (
        "the fixture no longer draws the failed task's icon in the wrong "
        "colour, so the negative control would prove nothing."
    )

    with pytest.raises(AssertionError):
        check_task_status_icons_use_semantic_vars(broken)
