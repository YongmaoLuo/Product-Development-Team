"""An unapplied pick must not outlive the view that shows it.

Why this gate exists
--------------------
The capacity control holds **two** numbers while the operator is
deciding: the number in force (the server's) and the number the
operator picked.  The pick is the operator's until it is written, so
the five-second poll leaves the control alone while the pick waits
(``dataset.pending`` is the mark that says so).

But the mark was never given an end of life.  Leaving the scheduler
view did not clear it, so the draft survived every trip away from the
panel: the operator came back to a control that showed a number the
scheduler was **not** running with, and the pick could be applied
long after the operator had gone.  Worse, there was no second copy of
the server's number to fall back on — only the pending mark — so once
the draft was dropped the control could wait for the next read
(nothing) or keep showing the stale pick (worse).

So the server's number is recorded next to the pick
(``dataset.serverValue``), and one routine ends the draft's life:
it drops the mark and puts the server's number back on the control.
The routine is called on the three paths that settle a pick — applied,
put back to the configured number, and the view switch — and it is the
view switch (``goHome()``, the one place every trip back to the plan
list goes through) that was missing.

The rule
--------
* ``renderSchedulerCapacity()`` records the server's number in
  ``select.dataset.serverValue`` on every read — the poll included —
  so a dropped draft has something to fall back on.
* ``renderSchedulerCapacity()`` still leaves the control alone while
  the pending mark is there: the operator's pick survives the poll.
* ``schedulerClearCapacityDraft()`` drops the mark **and** puts the
  recorded server's number back on the control.  With no draft held
  it does nothing; with no recorded server number it drops the mark
  and leaves the control's number alone.
* ``goHome()`` calls it: every path that leaves the view ends the
  draft.

Scan target
-----------
``frontend/app.js``, read as source.  This is a shape gate, like the
rest of ``static_gates/``: it runs on a plain Python environment with
no JS toolchain, so it pins the contract in the source rather than
executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"

#: The name of the routine that ends a draft's life.
_CLEAR_FN = "schedulerClearCapacityDraft"

#: The dataset attribute that marks the select as holding a pick the
#: operator has not applied yet.
_PENDING_MARK = "dataset.pending"

#: The value that mark carries while the pick is unapplied.
_PENDING_VALUE = '"1"'

#: The dataset attribute that carries the server's number.
_SERVER_VALUE = "dataset.serverValue"


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
    function no matter what the body contains.

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_render_records_the_server_value() -> None:
    """The render must record the server's number, poll included.

    The draft is only half the state of the control: the server's
    number is the other half, and it is what the control falls back to
    once the draft is dropped.  Without a recorded number a dropped
    draft leaves the control showing the operator's pick — a number
    the scheduler is not running with — until the next read, which may
    never come.

    The number recorded is the server's ``capacity.total``, never the
    operator's pick: the whole point of the record is to be able to
    put the server's number back.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerCapacity")

    assignment = re.search(
        r"select\." + re.escape(_SERVER_VALUE) + r"\s*=\s*([A-Za-z_$][\w$]*)\s*;",
        body,
    )
    assert assignment is not None, (
        f"renderSchedulerCapacity() never writes select.{_SERVER_VALUE}, "
        "so there is no server number to put back on the control once "
        "the operator's pick is dropped."
    )

    recorded = assignment.group(1)
    assert re.search(
        r"(?:const|let|var)\s+" + re.escape(recorded) + r"\s*=\s*String\(total\)",
        body,
    ), (
        f"renderSchedulerCapacity() writes select.{_SERVER_VALUE} from "
        f"{recorded!r}, which is not the server's capacity.total. The "
        "record exists so the server's number can be put back — a "
        "number that is not the server's would put the wrong number "
        "back."
    )


def test_go_home_clears_the_draft() -> None:
    """Leaving the view must end the draft.

    ``goHome()`` is where every trip back to the plan list goes: the
    sidebar home button, the 「返回计划列表」 buttons and the
    scheduler's own back button all call it.  It is therefore the one
    place where the draft can be ended for every path out of the
    scheduler view — a draft that survives the switch shows the
    operator a number the scheduler is not running with, and can be
    applied long after the operator has gone.
    """
    body = _function_body(_source(_APP_JS), "goHome")

    assert _CLEAR_FN + "(" in body, (
        f"goHome() never calls {_CLEAR_FN}(), so a pick the operator "
        "never applied survives the view switch: the control keeps "
        "showing it, and it can be applied long after the operator "
        "left the panel."
    )


def test_clear_restores_the_server_value() -> None:
    """Ending the draft must put the server's number back.

    Dropping the mark alone is not enough: the control would keep
    showing the operator's pick, and the pick is not what the
    scheduler is running with.  The control goes back to the recorded
    server's number — the number in force — so what is on screen is
    always what the server says.
    """
    body = _function_body(_source(_APP_JS), _CLEAR_FN)

    assert re.search(
        r"delete\s+select\." + re.escape(_PENDING_MARK),
        body,
    ), (
        f"{_CLEAR_FN}() never deletes select.{_PENDING_MARK}, so the "
        "mark outlives the draft: the five-second poll would keep "
        "leaving the control alone, and the control would never follow "
        "the server again."
    )

    assert re.search(
        r"select\.value\s*=\s*select\." + re.escape(_SERVER_VALUE),
        body,
    ), (
        f"{_CLEAR_FN}() never puts the recorded server's number back on "
        f"the control (select.value = select.{_SERVER_VALUE}). Ending a "
        "draft must leave the control showing the number in force, not "
        "the pick the operator never applied."
    )


def test_render_keeps_the_draft_while_pending() -> None:
    """The poll must still leave the operator's pick alone.

    The draft is the operator's until it is settled: the poll arrives
    every five seconds, and a poll that wrote the server's number into
    the control would wipe the pick without a word — the operator
    would then press 「应用」 and write the server's old number.

    Recording the server's number must not change that: the record is
    what the control falls back to *after* the draft is dropped, not a
    reason to overwrite the pick while it is held.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerCapacity")

    assert re.search(
        r"select\." + re.escape(_PENDING_MARK) + r"\s*!==\s*" + re.escape(_PENDING_VALUE),
        body,
    ), (
        f"renderSchedulerCapacity() no longer tests select.{_PENDING_MARK} "
        f"against {_PENDING_VALUE}, so the poll overwrites the "
        "operator's unapplied pick and pressing 「应用」 writes the "
        "server's old number."
    )
