"""Table 2 — the command-kind exemption, read from the projected verdict.

Why this table exists
---------------------
The second table of §4.2 is the same judgment read from the shape the
verdict really has by the time it reaches the gate.  A verdict's
evidence is flattened into ``{"details": str, "raw": list[str]}``, and
the deterministic runner's evidence dict survives inside ``raw`` as one
JSON string.  A line of ``raw`` that parses back into an object *is* a
record; a prose line, a stream excerpt or a truncated tail is not.

That shape is the one on the real path: reading only the flat shape
made the command exemption dead code (2026-11-18), and a command point
whose declared entry had just exited 0 was downgraded for "answering
another question" — because the log of what ran names the test files
and never names the referenced script.

The judgment is
:func:`verification_evidence._projected_ran_the_declared_entry`.  It is
a Tier A point: its value reaches the verdict, and a wrong ``True``
lets a PASSED stand that was never earned.

What the table pins
-------------------
Every row states a concrete value, the rows reach every branch of the
function, and every id says why the row is there:

* ``projected-record-exempts`` — the runner's own record of the
  declared entry, with the exit code the framework read: the question
  was answered;
* ``browser-kind-never-exempts`` — a browser point's evidence is the
  sub-agent's prose, and prose is not a runner artifact, so it buys no
  exemption;
* ``record-without-exit-code-fails`` — a record of the entry without
  the exit code is half a proof;
* ``prose-line-is-not-a-record`` — a line that does not parse back into
  an object is a log line, not a record;
* ``other-command-record-does-not-exempt`` — a record of some *other*
  command proves nothing about this point.

Scope, stated honestly: this file **reads** the function it pins.  It
never writes to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: The backend package root — this file lives at ``backend/tests/unit/``.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_evidence  # noqa: E402
from tests.judgment_tables import Case, assert_decision_table  # noqa: E402


#: The command a command-kind point declares it is run by.
_ENTRY = "pytest backend/tests/unit -q"

#: Another command entirely — a record of *this* one is not a record of
#: the declared entry.
_OTHER_ENTRY = "pytest backend/tests/integration -q"

#: A command-kind point: the framework runs the entry and grades the
#: exit code, so its evidence is machine-written.
_COMMAND_POINT = {
    "verification_method": "e2e",
    "e2e_kind": "command",
    "entry_command": _ENTRY,
}

#: A browser-kind point: the sub-agent drives the browser, so its
#: evidence is the sub-agent's prose.
_BROWSER_POINT = {
    "verification_method": "e2e",
    "e2e_kind": "browser",
    "target_url": "http://127.0.0.1:8000/",
    "entry_command": _ENTRY,
}


#: The table itself (§4.2).  Every row names a judgment value, and the
#: rows between them reach every branch of the function.
CASES = [
    Case(
        id="projected-record-exempts",
        inputs=(
            _COMMAND_POINT,
            {
                "raw": [
                    json.dumps({"entry_command": _ENTRY, "exit_code": 0})
                ]
            },
            _ENTRY,
        ),
        expected=True,
    ),
    Case(
        id="browser-kind-never-exempts",
        inputs=(
            _BROWSER_POINT,
            {
                "raw": [
                    json.dumps({"entry_command": _ENTRY, "exit_code": 0})
                ]
            },
            _ENTRY,
        ),
        expected=False,
    ),
    Case(
        id="record-without-exit-code-fails",
        inputs=(
            _COMMAND_POINT,
            {"raw": [json.dumps({"entry_command": _ENTRY})]},
            _ENTRY,
        ),
        expected=False,
    ),
    Case(
        id="prose-line-is-not-a-record",
        inputs=(
            _COMMAND_POINT,
            {"raw": ["the suite ran and every case passed"]},
            _ENTRY,
        ),
        expected=False,
    ),
    Case(
        id="other-command-record-does-not-exempt",
        inputs=(
            _COMMAND_POINT,
            {
                "raw": [
                    json.dumps(
                        {"entry_command": _OTHER_ENTRY, "exit_code": 0}
                    )
                ]
            },
            _ENTRY,
        ),
        expected=False,
    ),
]


def test_decision_table() -> None:
    """The table is the test: every row states a value and every branch is reached.

    The helper checks three things at once — the rows are usable (ids
    are unique and say something), every row's value is the value the
    row expects (compared by type as well as by equality), and the rows
    reach every branch of
    :func:`_projected_ran_the_declared_entry`.  A table that left a
    branch unreached would be a table that let a new branch in
    unnoticed.
    """
    assert_decision_table(
        verification_evidence._projected_ran_the_declared_entry, CASES
    )
