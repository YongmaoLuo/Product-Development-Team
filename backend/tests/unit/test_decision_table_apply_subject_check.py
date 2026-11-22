"""Table 4 — the PASSED that answered another question is downgraded.

Why this table exists
---------------------
:func:`verification_evidence.apply_subject_check_to_verdict` is the
one place where the subject gate reaches the verdict.  It is a Tier A
point: what it returns *is* the verdict the report is built from, and
a wrong ``True`` there would let a PASSED about the wrong file stand.

Its discipline is worth stating before its branches are: it only ever
**downgrades**.  A PASSED that answered another question becomes
FAILED; a FAILED stays FAILED; a verdict that is not a dict is handed
back untouched.  Nothing here turns a failure into a pass, and nothing
here adds a repair semantic — the check says the verdict did not
answer this question, and the point can be looked at again next round.

What the table pins
-------------------
Every row states a concrete value, and every id says why the row is
there:

* ``passed-off-subject-downgrades`` — the whole downgrade: the status
  flips to FAILED, the reason is appended to the reasons that were
  there, the verdict says what happened, and the evidence carries the
  reason for an auditor;
* ``passed-on-subject-untouched`` — a PASSED that answered the right
  question is handed back exactly as it came in;
* ``failed-never-touched`` — a FAILED is never rewritten: the check
  refuses to let a wrong pass through, and it does not dress a failure
  up as anything else;
* ``non-dict-passthrough`` — something that is not a verdict at all
  comes back unchanged.

The subject files
-----------------
``backend/x.py`` (the point's subject) and ``backend/y.py`` (another
file) are created by the test under pytest's ``tmp_path``.  They exist
only while the test runs and are never written into the repository.

Scope, stated honestly: this file **reads** the function it pins.  It
never writes to it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

#: The backend package root — this file lives at ``backend/tests/unit/``.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_evidence  # noqa: E402
from tests.judgment_tables import Case, assert_decision_table  # noqa: E402


class _SubjectRoot:
    """The tree the table's paths resolve against.

    The subject files exist only while the test runs: the test creates
    them under pytest's ``tmp_path``, and nothing is ever written into
    the repository.  The table is a module-level list, so the root
    travels through the rows as this holder — the function under test
    reads it through ``str()``, and the test fills it in before the
    rows run.
    """

    def __init__(self) -> None:
        self.path: Optional[Path] = None

    def __str__(self) -> str:
        return "" if self.path is None else str(self.path)


#: The tree the subject files live in.
_ROOT = _SubjectRoot()

#: The reason the subject gate gives when the answer is about another
#: file — the same words the gate itself uses.
_MISMATCH_REASON = (
    "本验证点点名 backend/x.py，"
    "而结论只涉及 backend/y.py"
    " —— 裁决回答的不是这道验证点"
)

#: A code-review point about ``backend/x.py``.
_CODE_REVIEW_POINT = {
    "id": "VP-028",
    "verification_method": "code_review",
    "expected_result": "检查 backend/x.py",
}

#: A PASSED about another file — the verdict the check has to catch.
_PASSED_OFF_SUBJECT = {
    "status": "PASSED",
    "reasons": ["我看了 backend/y.py"],
    "evidence": {"details": "看了 backend/y.py"},
}

#: A PASSED about the file the point named.
_PASSED_ON_SUBJECT = {
    "status": "PASSED",
    "reasons": ["我看了 backend/x.py"],
    "evidence": {"details": "看了 backend/x.py"},
}

#: A FAILED verdict — the check must not touch it.
_FAILED = {
    "status": "FAILED",
    "reasons": ["backend/x.py 的门禁不满足"],
    "evidence": {"details": "门禁没过"},
}


#: The table itself (§4.2).  Every row names a judgment value, and the
#: rows between them reach every branch of the function.
CASES = [
    Case(
        id="passed-off-subject-downgrades",
        inputs=(
            _PASSED_OFF_SUBJECT,
            _CODE_REVIEW_POINT,
            _ROOT,
            None,
        ),
        expected={
            "status": "FAILED",
            "reasons": ["我看了 backend/y.py", _MISMATCH_REASON],
            "actual_result": (
                "code_review 判定为 PASSED，但结论回答的不是这道验证点，"
                f"改判 FAILED：{_MISMATCH_REASON}"
            ),
            "evidence": {
                "details": "看了 backend/y.py",
                "subject_check": {"reason": _MISMATCH_REASON},
            },
        },
    ),
    Case(
        id="passed-on-subject-untouched",
        inputs=(
            _PASSED_ON_SUBJECT,
            _CODE_REVIEW_POINT,
            _ROOT,
            None,
        ),
        # Spelled out again rather than reusing the input: a row that
        # compares a value with itself cannot fail, and a function that
        # rewrote the verdict in place would slip through it.
        expected={
            "status": "PASSED",
            "reasons": ["我看了 backend/x.py"],
            "evidence": {"details": "看了 backend/x.py"},
        },
    ),
    Case(
        id="failed-never-touched",
        inputs=(
            _FAILED,
            _CODE_REVIEW_POINT,
            _ROOT,
            None,
        ),
        expected={
            "status": "FAILED",
            "reasons": ["backend/x.py 的门禁不满足"],
            "evidence": {"details": "门禁没过"},
        },
    ),
    Case(
        id="non-dict-passthrough",
        inputs=(
            "not a verdict",
            _CODE_REVIEW_POINT,
            _ROOT,
            None,
        ),
        expected="not a verdict",
    ),
]


def _subject_tree(tmp_path: Path) -> Path:
    """A tree with the two files the table talks about.

    The subject of the verification point is ``backend/x.py``; the file
    some other question is about is ``backend/y.py``.  Both live under
    pytest's ``tmp_path``, so they exist only for this test.
    """
    project = tmp_path / "project"
    (project / "backend").mkdir(parents=True)
    (project / "backend" / "x.py").write_text(
        "def subject():\n    return 1\n", encoding="utf-8",
    )
    (project / "backend" / "y.py").write_text(
        "def other():\n    return 2\n", encoding="utf-8",
    )
    return project


def test_decision_table(tmp_path: Path) -> None:
    """The table is the test: every row states a value and every branch is reached.

    The helper checks three things at once — the rows are usable (ids
    are unique and say something), every row's value is the value the
    row expects (compared by type as well as by equality), and the rows
    reach every branch of
    :func:`apply_subject_check_to_verdict`.  A table that left a branch
    unreached would be a table that let a new branch in unnoticed.
    """
    project = _subject_tree(tmp_path)
    _ROOT.path = project

    assert_decision_table(
        verification_evidence.apply_subject_check_to_verdict, CASES
    )
