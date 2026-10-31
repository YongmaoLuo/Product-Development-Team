"""The 「可操作的计划」 block is gone from every surface: no page and no
end-to-end test may name its ids any more.

Why this gate exists
--------------------
The scheduler view used to stack three blocks: 队列 (the queue), 在跑
(the running plans) and 「可操作的计划」 (the plans an operator can act
on).  The third one was a *superset* of the two above it — every plan it
showed was already named in the queue or in the running list — and it
was built out of a second request (``GET /api/plans``) and a second
phase filter.  It is gone from the markup now, and the two blocks that
remain are the whole of the panel.

A block that is gone from the markup is not gone from the repository.
Two things can still name it:

* **the page** — a renderer or a loader that selects
  ``#scheduler-plans-list`` finds nothing, and the panel throws on the
  next line, so the blocks that are still wanted go blank with it;
* **the end-to-end tests** — a driver that waits for
  ``#scheduler-plans-list tr`` waits for a selector that no page draws
  any more, and the case fails on a timeout instead of on a fact.

The second one is the quieter failure of the two: the end-to-end suite
does not go red on the day the block is removed (it needs a browser to
notice), it goes red on the day somebody runs it — which is the day the
suite stops being evidence.  So the residue is pinned here, where a
plain Python environment can see it.

The rule
--------
* ``frontend/`` names neither ``scheduler-plans-list`` nor
  ``scheduler-plans-table``.
* ``backend/tests/e2e/`` names neither of them either — the panel the
  end-to-end tests drive is read through the two blocks that remain,
  ``#scheduler-queue-list`` and ``#scheduler-running-list``.
* The two end-to-end files that used to read the deleted block read the
  queue instead: the operator case names ``#scheduler-queue-list``, and
  the shutdown case waits for a queue row.

Scan target
-----------
``frontend/`` and ``backend/tests/e2e/``, read as source.  These are
shape gates, like the rest of ``static_gates/``: they run on a plain
Python environment with no JS toolchain, so they pin the contract in the
source rather than executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Tuple


_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The page's own directory.  Every file in it is read: the ids of the
#: deleted block could be named by the markup, by the script, or by the
#: stylesheet, and only one of the three is obvious from the outside.
_FRONTEND_DIR = _REPO_ROOT / "frontend"

#: The end-to-end directory.  Every Python file in it is read: the
#: driver of a browser case is a string inside a Python file, so a
#: source scan is the only way to see what it selects.
_E2E_DIR = _REPO_ROOT / "backend" / "tests" / "e2e"

#: The two ids the deleted block was made of.  The empty state
#: (``scheduler-plans-empty``) is not in this list: it was never
#: selected by anything, and a name that nothing selects cannot be
#: left behind by accident.
_REMOVED_ELEMENT_IDS = (
    "scheduler-plans-list",
    "scheduler-plans-table",
)

#: The two blocks that remain, and the ids the panel's two tables are
#: drawn into.
_QUEUE_LIST_ID = "scheduler-queue-list"
_RUNNING_LIST_ID = "scheduler-running-list"

#: The operator's end-to-end case: the one that drives the four actions
#: through the panel.
_OPERATOR_E2E = _E2E_DIR / "plan_scheduler" / "test_scheduler_operator_e2e.py"

#: The shutdown case: the one that shows the panel to a reader.
_SHUTDOWN_E2E = _E2E_DIR / "test_shutdown_checkpoint_e2e.py"

#: A ``wait_for_selector`` call and the selector it waits for.
_WAIT_FOR_SELECTOR = re.compile(
    r"wait_for_selector\(\s*[\"']([^\"']+)[\"']", re.MULTILINE
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


def _text_files(directory: Path) -> List[Path]:
    """Every text file under ``directory``, in a stable order.

    The frontend's own files are read whole; the end-to-end directory
    is read as Python, because a driver is a string inside a Python
    file and nothing else in the directory is worth reading.
    """
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix in (".html", ".js", ".css", ".py")
    )


def _occurrences(path: Path, needle: str) -> List[Tuple[int, str]]:
    """Every ``(line number, line)`` in ``path`` that carries ``needle``."""
    found: List[Tuple[int, str]] = []
    for number, line in enumerate(_source(path).splitlines(), start=1):
        if needle in line:
            found.append((number, line.strip()))
    return found


def _findings(needle: str, paths: Iterable[Path]) -> List[str]:
    """Every place in ``paths`` that names ``needle``, as one line each."""
    findings: List[str] = []
    for path in paths:
        for number, line in _occurrences(path, needle):
            findings.append(
                "{0}:{1}: {2}".format(
                    path.relative_to(_REPO_ROOT).as_posix(), number, line
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_plans_block_in_any_surface() -> None:
    """No page and no end-to-end test may name the deleted block.

    The block is gone from the markup; a name that outlives it is a
    name that resolves to nothing.  In the page that is a null
    selector and a thrown exception; in an end-to-end driver it is a
    wait that never comes back — and the case fails on the timeout
    rather than on the fact it was written to prove.
    """
    paths = _text_files(_FRONTEND_DIR) + _text_files(_E2E_DIR)
    assert paths, (
        "neither frontend/ nor backend/tests/e2e/ could be read: the gate "
        "would pass on an empty scan, and an empty scan proves nothing."
    )

    for element_id in _REMOVED_ELEMENT_IDS:
        findings = _findings(element_id, paths)
        assert not findings, (
            "the 「可操作的计划」 block is gone, and {0!r} names it:\n  {1}\n"
            "The page has no such element, so the selector resolves to "
            "nothing — and an end-to-end driver that waits for it waits "
            "forever.".format(element_id, "\n  ".join(findings))
        )


def test_operator_e2e_reads_queue_rows() -> None:
    """The operator's case must read the panel through the queue.

    The case drives the four actions and then reads the panel back; the
    rows it reads are the ones the panel still draws.  The queue is
    where a waiting plan is named, so a case that reads the queue can
    say what the panel says about a plan that is waiting.
    """
    source = _source(_OPERATOR_E2E)

    assert _QUEUE_LIST_ID in source, (
        "the operator's end-to-end case never names {0!r}. The panel's "
        "queue is where a waiting plan is drawn, so this is the table "
        "the case has to read.".format(_QUEUE_LIST_ID)
    )
    assert _RUNNING_LIST_ID in source, (
        "the operator's end-to-end case never names {0!r}. The running "
        "list is where a plan that holds a slot is drawn, so this is the "
        "other table the case has to read.".format(_RUNNING_LIST_ID)
    )


def test_shutdown_e2e_waits_for_queue_rows() -> None:
    """The shutdown case must wait for a queue row, not for a deleted one.

    The case opens the scheduler view and waits for the rows it is
    going to read.  A wait for a row the panel no longer draws does not
    fail — it times out, thirty seconds later, with a message about a
    selector rather than about the product.
    """
    source = _source(_SHUTDOWN_E2E)

    selectors = _WAIT_FOR_SELECTOR.findall(source)
    assert selectors, (
        "the shutdown case never waits for a selector, so there is "
        "nothing here to check: either the wait is gone or it moved "
        "somewhere this gate cannot see."
    )

    queue_waits = [
        selector for selector in selectors if _QUEUE_LIST_ID in selector
    ]
    assert queue_waits, (
        "the shutdown case waits for {0!r} instead of a queue row. The "
        "queue is the block that holds a waiting plan, and it is the "
        "block the panel still draws.".format(
            ", ".join(repr(selector) for selector in selectors)
        )
    )

    for element_id in _REMOVED_ELEMENT_IDS:
        stale = [selector for selector in selectors if element_id in selector]
        assert not stale, (
            "the shutdown case still waits for {0!r}: {1}. The block it "
            "belongs to is gone.".format(element_id, stale)
        )
