"""The scheduler page must carry the 「待你审核」 block.

Why this gate exists
--------------------
Architecture decision R5 asks for one block on the scheduler page that
aggregates the three kinds of thing waiting for the operator's answer:
a pull request to merge, a HITL decision, and an approval.  The
backend answers it as one document under the ``review_queue`` key
(``tests/api/plan_scheduler/test_review_queue_api.py``), and this gate
pins the half of the contract that lives in the page: the block exists,
it has its three groups, an empty group is not drawn, and every row can
be traced back to the plan it is about.

Nothing here fails loudly.  A block that is not on the page is a block
that is simply not drawn; the panel keeps working, the operator just
does not see what is waiting for them.

The rules
---------
* ``index.html`` must carry ``#scheduler-review-queue`` and its three
  groups — ``#review-queue-pr-merge``, ``#review-queue-hitl`` and
  ``#review-queue-approval``.  The groups are named in the markup
  because the renderer finds them by name: a group that is not in the
  markup cannot be hidden, and a group that is always visible is a
  group that always says "nothing here" in the operator's face.
* ``app.js`` must read the queue out of the snapshot and draw one row
  per entry, each row carrying ``data-review-plan`` so that a test —
  and the operator's eye — can find it again.
* **An empty group is hidden.**  The renderer hides it with
  ``style.display = "none"``; the three groups are always present in
  the markup, so "empty" and "missing" are two different things and
  only one of them may be shown.
* ``renderScheduler()`` must call the renderer: a block that is not
  repainted with the read model is a block that shows the last answer
  forever.

Scan target
-----------
``frontend/index.html`` and ``frontend/app.js``, read as source.  These
are shape gates, like the rest of ``static_gates/``: they run on a
plain Python environment with no JS toolchain, so they pin the contract
in the source rather than executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"

#: The three groups of the review queue, and the ids they are drawn in.
#:
#: The key is the group's name in the read model (``pr_merge``); the id
#: is the block it is drawn in (``review-queue-pr-merge``).  The two are
#: spelled differently on purpose — the id is the panel's own naming, and
#: the key is the server's.
_GROUPS = (
    ("pr_merge", "review-queue-pr-merge"),
    ("hitl", "review-queue-hitl"),
    ("approval", "review-queue-approval"),
)

#: The id of the scheduler view the review queue is drawn in.
_VIEW_ID = "scheduler-view"

#: The heading of the review queue's own block.
_REVIEW_HEADING = "待你审核"

#: The headings the panel's two-block scan counts: the ones whose class
#: attribute is **exactly** ``scheduler-subtitle``.  A heading that wears
#: an extra class is not one of them — which is how the waiting and the
#: terminal blocks stay out of that scan.
#:
#: This is the same pattern the panel's own layout gates use; it is
#: repeated here so that this gate fails with its own message instead of
#: leaving the breakage to be found in somebody else's gate.
_SUBTITLE_RE = re.compile(
    r"<h3[^>]*class=\"scheduler-subtitle\"[^>]*>(.*?)</h3>", re.DOTALL
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


def _function_body(source: str, name: str) -> str:
    """Return the body text of the top-level ``function name(...)``.

    Raises :class:`AssertionError` when the declaration is absent, so a
    rename that nobody updates the gate with fails loudly instead of
    matching nothing and passing.

    Brace counting is naive — it does not know about strings or
    comments — which is adequate for the handful of short, uniformly
    indented functions this file inspects.
    """
    pattern = re.compile(
        r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
        re.MULTILINE,
    )
    match = pattern.search(source)
    assert match is not None, f"function {name}() not found in the source"
    body_start = match.end() - 1  # index of the opening '{'
    depth = 0
    for index in range(body_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[body_start + 1 : index]
    raise AssertionError(f"unbalanced braces in function {name}()")


def _scheduler_view(html: str) -> str:
    """Return the scheduler view's markup, as the panel's gates see it.

    The panel's layout gates read the view as the raw markup between
    its opening tag and the **first closing section tag**: they scan
    the source, so a nested section — or even the words of one — inside
    the view cuts their scan short.
    """
    match = re.search(
        r"<section[^>]*\bid=\"" + re.escape(_VIEW_ID) + r"\"[^>]*>", html
    )
    assert match is not None, f'<section id="{_VIEW_ID}"> not found'
    end = html.find("</section>", match.end())
    assert end != -1, f'<section id="{_VIEW_ID}"> is never closed'
    return html[match.start() : end]


def _tag_of(html: str, element_id: str) -> str:
    """Return the opening tag of the element ``element_id``.

    Raises :class:`AssertionError` when no element carries the id.
    """
    match = re.search(
        r"<[a-zA-Z][^>]*\bid=\"" + re.escape(element_id) + r"\"[^>]*>", html
    )
    assert match is not None, f'id="{element_id}" not found'
    return match.group(0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_review_queue_section_present() -> None:
    """The page must carry the block and its three groups.

    The groups are named in the markup because the renderer finds them
    by name.  A group that is not in the markup cannot be hidden, and a
    group that is never hidden says 「没有待审核的条目」 to an operator
    who has three questions waiting.
    """
    html = _source(_INDEX_HTML)

    assert 'id="scheduler-review-queue"' in html, (
        "the scheduler page has no #scheduler-review-queue, so the "
        "「待你审核」 block is not on the page at all"
    )

    for _key, group_id in _GROUPS:
        assert f'id="{group_id}"' in html, (
            f"the review queue has no #{group_id} group, so the "
            "renderer has nowhere to draw that group — and no way to "
            "hide it when it is empty"
        )


def test_review_queue_group_hidden_when_empty() -> None:
    """An empty group is hidden, and a row says which plan it is about.

    Three groups are always in the markup, so "empty" and "missing"
    have to be two different things: the group that is empty is hidden
    (``style.display = "none"``), and the group that has rows is
    shown.  Every row carries ``data-review-plan``, which is how the
    row is traced back to the plan it is about.
    """
    body = _function_body(_source(_APP_JS), "renderReviewQueue")

    assert re.search(r"style\.display\s*=\s*[\"']none[\"']", body), (
        "renderReviewQueue() never hides an empty group, so a group with "
        "nothing in it is drawn as an empty box on the operator's screen"
    )

    for _key, group_id in _GROUPS:
        assert group_id in body, (
            f"renderReviewQueue() never touches the {group_id} "
            "group, so it is neither drawn nor hidden"
        )

    assert "data-review-plan" in body, (
        "no row carries `data-review-plan`, so a row cannot be traced "
        "back to the plan it is about"
    )


def test_review_queue_is_read_from_the_snapshot() -> None:
    """The block must be drawn from the read model.

    The backend answers one document; the page draws it.  A renderer
    that read the queue from somewhere else would be a second answer to
    "what is waiting for me".
    """
    scheduler = _function_body(_source(_APP_JS), "renderScheduler")

    assert re.search(r"review_queue", scheduler), (
        "renderScheduler() does not read the `review_queue` key out of "
        "the read model, so the rows the block draws cannot come from it"
    )

    body = _function_body(_source(_APP_JS), "renderReviewQueue")

    assert re.search(r"data-review-plan", body), (
        "renderReviewQueue() draws rows without saying which plan they "
        "are about"
    )


def test_render_scheduler_draws_the_review_queue() -> None:
    """``renderScheduler()`` must draw the block.

    The panel is repainted from the read model every five seconds.  A
    block that is not repainted with it shows the last answer for ever.
    """
    body = _function_body(_source(_APP_JS), "renderScheduler")

    assert re.search(r"renderReviewQueue\s*\(", body), (
        "renderScheduler() never calls renderReviewQueue(), so the "
        "「待你审核」 block is never repainted with the read model"
    )


def test_review_queue_survives_a_missing_queue() -> None:
    """A read model without the key must hide the groups and throw nothing.

    The panel polls the endpoint every five seconds.  A read model that
    does not carry the key — an older server, a partial answer — must
    cost the operator the block, not the panel.  The renderer reads the
    queue defensively: a queue that is not an object reads as empty, and
    a group that is not a list reads as empty, so all three groups end
    up hidden instead of the panel throwing on every poll.
    """
    body = _function_body(_source(_APP_JS), "renderReviewQueue")

    assert re.search(r"typeof\s+queue\s*===\s*[\"']object[\"']", body), (
        "renderReviewQueue() does not check that the queue is an object, "
        "so a read model without the `review_queue` key throws inside the "
        "panel instead of drawing an empty block"
    )
    assert re.search(r"Array\.isArray\(data\[key\]\)", body), (
        "renderReviewQueue() does not check that a group is a list, so a "
        "group that is missing from the queue reads as undefined instead "
        "of empty"
    )


def test_review_queue_does_not_cut_the_panel_scan_short() -> None:
    """The block must not break the scheduler view's own layout scan.

    The panel's layout gates read the scheduler view as the raw markup
    between its opening tag and the first closing section tag, and they
    count the headings whose class attribute is exactly
    ``scheduler-subtitle``.  A review queue written as a nested
    ``<section>`` cuts that scan short, and a heading wearing the bare
    subtitle class adds a third block to it — and the panel's contract
    goes red over a block that is not even part of it.

    The review queue is a third block of the panel, and it is written
    so that the panel's own scan does not see it as one of its two.
    """
    html = _source(_INDEX_HTML)

    view = _scheduler_view(html)

    subtitles = [
        match.group(1).strip() for match in _SUBTITLE_RE.finditer(view)
    ]
    assert subtitles == ["在跑", "队列"], (
        "the scheduler view's block headings are "
        f"{subtitles}; the panel's layout contract says exactly 在跑 and "
        "队列, so the review queue must not be counted as one of them"
    )

    tag = _tag_of(html, "scheduler-review-queue")
    assert not tag.startswith("<section"), (
        "the review queue is written as a nested <section>, so the "
        "panel's scan of the scheduler view stops at the review queue "
        "instead of at the end of the view"
    )
