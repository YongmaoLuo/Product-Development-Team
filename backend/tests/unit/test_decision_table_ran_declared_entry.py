"""Table 1 — the command-kind exemption, read from the verdict's evidence.

Why this table exists
---------------------
§4.2 of the judgment-surface design opens with two decision tables, and
the first of them is the one the incident was about (§1.2): a
command-kind verification point asks one question — does *this* command
exit 0 — and a verdict that records that command together with the exit
code the framework read has answered it by construction.

The judgment is :func:`verification_evidence._ran_the_declared_entry`.
It is a Tier A point: its value reaches the verdict, and a wrong
``True`` lets a PASSED stand that was never earned.  A table that only
walked the happy path would let a new branch in unnoticed, so the rows
below reach every branch of the function as well as state every value.

What the table pins
-------------------
Every row states a concrete value, and every id says why the row is
there:

* ``same-command-exit-0-exempts`` — the point's own command and the
  exit code the framework read: the question was answered, so the
  subject check must not downgrade the verdict;
* ``other-command-does-not-exempt`` — a record of some *other* command
  proves nothing about this point;
* ``prose-only-does-not-exempt`` — prose is not a runner artifact;
* ``exit-code-without-command-does-not-exempt`` — half a proof is not a
  proof;
* ``point-without-a-declared-entry-does-not-exempt`` — a point that
  declared no entry cannot have run it;
* ``verdict-without-an-evidence-dict-does-not-exempt`` — there is
  nothing to read;
* ``projected-record-exempts-through-the-flat-entry-point`` — the shape
  that actually reaches the gate (the runner's dict surviving inside
  ``evidence.raw``) exempts here too.  2026-11-18 found that reading
  only the flat shape made this rule dead code on the real path.

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

#: A command-kind point that declared its entry.
_COMMAND_POINT = {
    "verification_method": "e2e",
    "e2e_kind": "command",
    "entry_command": _ENTRY,
}

#: A command-kind point that declared no entry at all.
_POINT_WITHOUT_ENTRY = {
    "verification_method": "e2e",
    "e2e_kind": "command",
}


#: The table itself (§4.2).  Every row names a judgment value, and the
#: rows between them reach every branch of the function.
CASES = [
    Case(
        id="same-command-exit-0-exempts",
        inputs=(
            _COMMAND_POINT,
            {"evidence": {"entry_command": _ENTRY, "exit_code": 0}},
        ),
        expected=True,
    ),
    Case(
        id="other-command-does-not-exempt",
        inputs=(
            _COMMAND_POINT,
            {"evidence": {"entry_command": _OTHER_ENTRY, "exit_code": 0}},
        ),
        expected=False,
    ),
    Case(
        id="prose-only-does-not-exempt",
        inputs=(
            _COMMAND_POINT,
            {"evidence": {"details": "the suite ran and every case passed"}},
        ),
        expected=False,
    ),
    Case(
        id="exit-code-without-command-does-not-exempt",
        inputs=(
            _COMMAND_POINT,
            {"evidence": {"exit_code": 0}},
        ),
        expected=False,
    ),
    Case(
        id="point-without-a-declared-entry-does-not-exempt",
        inputs=(
            _POINT_WITHOUT_ENTRY,
            {"evidence": {"entry_command": _ENTRY, "exit_code": 0}},
        ),
        expected=False,
    ),
    Case(
        id="verdict-without-an-evidence-dict-does-not-exempt",
        inputs=(
            _COMMAND_POINT,
            {"evidence": "the suite ran and every case passed"},
        ),
        expected=False,
    ),
    Case(
        id="projected-record-exempts-through-the-flat-entry-point",
        inputs=(
            _COMMAND_POINT,
            {
                "evidence": {
                    "raw": [
                        json.dumps(
                            {"entry_command": _ENTRY, "exit_code": 0}
                        )
                    ]
                }
            },
        ),
        expected=True,
    ),
]


def test_decision_table() -> None:
    """The table is the test: every row states a value and every branch is reached.

    The helper checks three things at once — the rows are usable (ids
    are unique and say something), every row's value is the value the
    row expects (compared by type as well as by equality), and the rows
    reach every branch of :func:`_ran_the_declared_entry`.  A table that
    left a branch unreached would be a table that let a new branch in
    unnoticed.
    """
    assert_decision_table(
        verification_evidence._ran_the_declared_entry, CASES
    )
