"""The plan detail card must draw the failure review as a foldable block.

What this gate witnesses
------------------------
The detail endpoint hands the panel a section of its own for a plan
that failed: ``kind = "failure_review"``, carrying the announcement's
own ``problem`` and ``suggestion``.  The panel draws it as a **foldable
block** — a ``<details>`` whose summary says 「失败分析」 — so the
operator opens the analysis when they want to read it, and the card
keeps the shape it had when they do not.

Three things are pinned here:

1. **The block exists.**  The section renderer has a branch of its own
   for the failure review, and that branch draws a ``<details>`` whose
   title is 「失败分析」.
2. **The wording is escaped.**  Both texts come from the server, so
   both go through ``escapeHtml()`` on the way into the markup — a
   value that reaches ``innerHTML`` verbatim is a script the server
   (or anything it echoes) can run in the operator's browser.
3. **Nothing else moved.**  ``problem`` and ``suggestion`` are read
   inside that one branch only: no other section's rendering reads
   them, so the branch cannot have changed what the other sections
   draw.

Why the shape is checked on the source
--------------------------------------
The repo has no JS toolchain in its test lanes, so this is a **shape
gate**: it reads ``frontend/app.js`` as source and pins the contract in
it (test decision point R8).

Non-vacuity
------------
Every assertion below reads the *current* asset.  The helper that
extracts a function body raises when the declaration is absent, so a
rename cannot pass by matching nothing; the frozen-pin test compares
the **bytes on disk** with the hash the scope gate pins, so a stale
pin fails here too.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


#: The repository root.  ``backend/tests/static_gates/`` → up three
#: levels is the checkout, and the frontend assets live at its top.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"
_SCOPE_GATES = _REPO_ROOT / "backend" / "tests" / "static_gates" / (
    "test_verification_mode_scope_gates.py"
)

#: The kind the backend's failure-review section carries.  It is the
#: server's own vocabulary (``backend/routes/scheduler.py`` builds the
#: section), so the panel spells it exactly.
_FAILURE_REVIEW = "failure_review"

#: The guard that picks the failure review's own renderer.
_GUARD = 'kind === "' + _FAILURE_REVIEW + '"'

#: The title the block is drawn under.
_TITLE = "失败分析"

#: The two texts the block carries.  Both come from the server.
_PROBLEM = "problem"
_SUGGESTION = "suggestion"


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
    function no matter what the body contains.  Brace counting would be
    the alternative, and it miscounts the template literals this file is
    full of.

    Raises :class:`AssertionError` when the declaration is absent, so a
    rename that nobody updates this gate with fails loudly instead of
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


def _after_guard(body: str) -> str:
    """Return the part of ``body`` that follows the failure review's guard.

    Raises :class:`AssertionError` when the guard is absent, so a
    renderer that lost the branch fails here instead of matching
    nothing further down.
    """
    if _GUARD not in body:
        raise AssertionError(
            f"schedulerSectionHtml() has no branch for the "
            f"{_FAILURE_REVIEW!r} section"
        )
    return body[body.index(_GUARD) :]


def _frozen_pin(gate_source: str, rel_path: str) -> str:
    """Return the sha256 the scope gate pins for ``rel_path``.

    Raises :class:`AssertionError` when the file is not pinned there at
    all — a file the gate does not hold is a file nothing keeps still.
    """
    match = re.search(
        r'"' + re.escape(rel_path) + r'":\s*\(\s*"([0-9a-f]{64})"',
        gate_source,
    )
    assert match is not None, f"{rel_path} is not pinned in the scope gate"
    return match.group(1)


# ---------------------------------------------------------------------------
# 1. The block is drawn
# ---------------------------------------------------------------------------


def test_section_renders_details_block() -> None:
    """The failure review is drawn as a foldable block.

    The branch is the panel's own: a section the panel does not know
    would be drawn as a generic block carrying its own wording, and the
    operator would read a paragraph instead of a block they can open.
    The title is the one the requirement names — 「失败分析」.
    """
    body = _function_body(_source(_APP_JS), "schedulerSectionHtml")
    tail = _after_guard(body)

    assert "<details" in tail, (
        "schedulerSectionHtml() 没有把失败分析画成可折叠的 <details> 块"
    )
    assert _TITLE in tail, (
        f"schedulerSectionHtml() 的失败分析块没有标题「{_TITLE}」"
    )
    assert "<summary" in tail, (
        "schedulerSectionHtml() 的失败分析块没有 <summary>，打不开也合不上"
    )


# ---------------------------------------------------------------------------
# 2. The wording is escaped
# ---------------------------------------------------------------------------


def test_section_escapes_text() -> None:
    """Both of the block's texts go through ``escapeHtml()``.

    The problem and the suggestion are the server's own words, and they
    reach the DOM as markup — so they are escaped on the way in, like
    every other value this renderer draws.
    """
    body = _function_body(_source(_APP_JS), "schedulerSectionHtml")
    tail = _after_guard(body)

    problem_at = tail.index(_PROBLEM)
    suggestion_at = tail.index(_SUGGESTION)

    # The fragment each text lives in — the problem's own fragment runs
    # up to the suggestion, the suggestion's runs to the end of the
    # branch.
    problem_fragment = tail[problem_at:suggestion_at]
    suggestion_fragment = tail[suggestion_at:]

    for name, fragment in (
        (_PROBLEM, problem_fragment),
        (_SUGGESTION, suggestion_fragment),
    ):
        assert "escapeHtml(" in fragment, (
            f"schedulerSectionHtml() 没有把 {name} 经 escapeHtml() 转义"
        )

    # The escaping is on the value that reaches the markup, not on
    # something else that happens to sit in the same fragment.
    for name in (_PROBLEM, _SUGGESTION):
        assert re.search(
            r"escapeHtml\(\s*(?:data\.)?" + name + r"\s*\)", tail
        ), (
            f"schedulerSectionHtml() 没有把 {name} 放进 escapeHtml() 里"
        )

    # And the raw value never reaches the markup on its own.
    for name in (_PROBLEM, _SUGGESTION):
        assert "${" + name + "}" not in body, (
            f"schedulerSectionHtml() 把未经转义的 {name} 拼进了 markup"
        )
        assert "${data." + name + "}" not in body, (
            f"schedulerSectionHtml() 把未经转义的 data.{name} 拼进了 markup"
        )


# ---------------------------------------------------------------------------
# 3. Nothing else moved
# ---------------------------------------------------------------------------


def test_other_sections_unchanged() -> None:
    """Only the failure review's branch reads the two texts.

    The two texts belong to the failure review and to nothing else, so
    their first mention in the renderer is inside that branch — before
    it, the renderer knows nothing about them, and the other sections
    cannot have started reading them.
    """
    body = _function_body(_source(_APP_JS), "schedulerSectionHtml")
    guard_at = body.index(_GUARD)

    for name in (_PROBLEM, _SUGGESTION):
        if name not in body:
            raise AssertionError(
                f"schedulerSectionHtml() 没有读过 {name}"
            )
        if body.index(name) <= guard_at:
            raise AssertionError(
                f"schedulerSectionHtml() 在失败分析分支之外就读了 {name}"
            )


# ---------------------------------------------------------------------------
# 4. The frozen pin matches the bytes on disk
# ---------------------------------------------------------------------------


def test_app_js_pin_matches_disk() -> None:
    """The scope gate's pin is the hash of the file on disk.

    ``frontend/app.js`` is frozen byte for byte by the verification-mode
    scope gate.  This change is approved, so the pin moves; what must
    not happen is a pin that names a hash no file has — that is a gate
    that has stopped guarding anything.
    """
    gate_source = _source(_SCOPE_GATES)
    on_disk = hashlib.sha256(_APP_JS.read_bytes()).hexdigest()
    pinned = _frozen_pin(gate_source, "frontend/app.js")

    if on_disk not in gate_source:
        raise AssertionError(
            "test_verification_mode_scope_gates.py 里没有 frontend/app.js 的磁盘哈希：\n"
            f"  {on_disk}"
        )
    if pinned != on_disk:
        raise AssertionError(
            "frontend/app.js 的冻结哈希与磁盘不一致：\n"
            f"  钉住的：{pinned}\n"
            f"  磁盘的：{on_disk}"
        )
