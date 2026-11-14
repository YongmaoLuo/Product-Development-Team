"""The end-to-end cases read the scheduler panel's plans as cards, and
do not go back to the table rows the panel used to draw.

Why this gate exists
--------------------
The scheduler panel draws two blocks: 队列 (the queue) and 在跑 (the
running plans).  Both of them used to be tables, and the end-to-end
cases read the plans out of those tables::

    page.query_selector_all("#scheduler-queue-list tr")

The panel draws cards now — one card per plan, folded, opening on
demand — and the tables are still in the markup but carry no rows any
more.  A driver that asks for ``#scheduler-queue-list tr`` therefore
reads an **empty set**: not an error, not a failure, just nothing.
The case that waits for such a selector waits until it times out, and
the case that reads it compares two empty lists and finds them equal.
Both look like the panel working.

That is the quiet failure this gate is for.  The end-to-end suite does
not go red on the day the rendering changes (it needs a browser to
notice); it goes red on the day somebody runs it — which is the day the
suite stops being evidence.  So the residue is pinned here, where a
plain Python environment can see it: the four files that drive the
panel are read as source, and the selectors they name are checked.

The rule
--------
* None of the four cases names ``#scheduler-queue-list tr`` or
  ``#scheduler-running-list tr`` any more.
* Each of the four names the cards the panel draws:
  ``#scheduler-queue-cards .scheduler-plan-card`` for the queue, and
  ``#scheduler-running-cards .scheduler-plan-card`` for the running
  plans.

A card is read by asking for the card, and the plan a card is about is
read from the card's own ``data-plan-id`` — the table's second column
is not a column any more.

Scan target
-----------
The four files below, read as source, with their paths written out one
by one.  The scan is deliberately not a walk of the tree: the rule is
about *these* cases, and a scan that grew with the tree would stop
being a statement about them.  These are shape gates, like the rest of
``static_gates/``: they run on a plain Python environment with no
browser, so they pin the contract in the source rather than executing
the page.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple


_REPO_ROOT = Path(__file__).resolve().parents[3]
_E2E_DIR = _REPO_ROOT / "backend" / "tests" / "e2e"

#: The four cases that read the panel.  Their paths are written out one
#: by one: this is a rule about these cases, not about every case that
#: might be written later.
_E2E_FILES: Tuple[Path, ...] = (
    _E2E_DIR / "plan_scheduler" / "test_scheduler_operator_e2e.py",
    _E2E_DIR / "plan_scheduler" / "test_scheduler_panel_ux_e2e.py",
    _E2E_DIR / "plan_scheduler" / "test_scheduler_ui_auto_refresh_e2e.py",
    _E2E_DIR / "test_shutdown_checkpoint_e2e.py",
)

#: The selectors the panel used to draw its plan rows with.  The tables
#: are still in the markup, and their containers still carry their ids,
#: but no plan row is drawn into them any more.
_TABLE_ROW_SELECTORS: Tuple[str, ...] = (
    "#scheduler-queue-list tr",
    "#scheduler-running-list tr",
)

#: The selectors the panel draws its plans with.  One per block: the
#: queue's cards and the running plans' cards.
_CARD_SELECTORS: Tuple[str, ...] = (
    "#scheduler-queue-cards .scheduler-plan-card",
    "#scheduler-running-cards .scheduler-plan-card",
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
    assert path.is_file(), (
        "the case this gate reads is not there: {0}. The gate would pass "
        "on a scan of nothing, and a scan of nothing proves nothing.".format(
            path.relative_to(_REPO_ROOT).as_posix()
        )
    )
    return path.read_text(encoding="utf-8", errors="replace")


def _findings(needle: str, sources: List[Tuple[Path, str]]) -> List[str]:
    """Every place that names ``needle``, as one line each."""
    findings: List[str] = []
    for path, source in sources:
        for number, line in enumerate(source.splitlines(), start=1):
            if needle in line:
                findings.append(
                    "{0}:{1}: {2}".format(
                        path.relative_to(_REPO_ROOT).as_posix(),
                        number,
                        line.strip(),
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_table_row_selectors() -> None:
    """No case may read the panel's plan rows out of the tables.

    The tables carry no rows any more, so a driver that asks them for
    one reads an empty set: a wait that times out, and a comparison
    between two empty lists that agrees with itself.  Neither is a fact
    about the panel.
    """
    sources = [(path, _source(path)) for path in _E2E_FILES]

    for selector in _TABLE_ROW_SELECTORS:
        findings = _findings(selector, sources)
        assert not findings, (
            "the panel draws its plans as cards, and {0!r} still asks for "
            "the table rows that are not drawn any more:\n  {1}\n"
            "The selector resolves to nothing, so the case reads an empty "
            "set rather than the panel.".format(
                selector, "\n  ".join(findings)
            )
        )


def test_card_selectors_present() -> None:
    """Every case must name the cards the panel draws.

    A case that drives the panel and reads nothing of it proves
    nothing about it.  Each of the four names the cards of the block it
    reads — the queue's, or the running plans'.
    """
    for path in _E2E_FILES:
        source = _source(path)
        named = [
            selector for selector in _CARD_SELECTORS if selector in source
        ]
        assert named, (
            "{0} names none of the cards the panel draws ({1}). The panel "
            "draws a plan as a card, so this is the selector the case has "
            "to read.".format(
                path.relative_to(_REPO_ROOT).as_posix(),
                ", ".join(repr(selector) for selector in _CARD_SELECTORS),
            )
        )