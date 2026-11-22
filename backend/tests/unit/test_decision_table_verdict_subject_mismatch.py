"""Table 3 — is this verdict about a different question?

Why this table exists
---------------------
2026-11-07, round 4: a verification point asked about
``backend/verification_executor.py`` and the sub-agent handed back a
PASSED whose reasons were all about ``backend/routes/verification.py`` —
a *different* point's subject.  Nothing in the framework asked whether
the answer was about this question, so a verdict about the wrong file
reached the report as a verified pass.

:func:`verification_evidence.verdict_subject_mismatch` is the narrow
gate that closes that hole.  It is deliberately reluctant: it names a
reason only when it can *prove* the answer is about something else.
It is a Tier A point — what it returns decides whether a PASSED stands
— so this table states every value it can return (``None`` or the
reason), and reaches every branch of the function.

What the table pins
-------------------
Every row states a concrete value, and every id says why the row is
there:

* ``non-evidence-method-is-not-judged`` — a method the framework
  grades by itself (``full_ci``) owes no judgement, so the gate never
  looks at it;
* ``command-record-exempts`` — a command-kind point asks "does this
  command exit 0", and a verdict recording that command and its exit
  code has answered it by construction;
* ``other-file-only-mismatches`` — the point names one file and the
  answer names another: this is the mismatch the gate exists for, and
  the reason names both sides;
* ``no-existing-expected-path-is-null`` — a path that does not exist
  in this tree names no subject, so there is nothing to compare;
* ``bare-filename-rescues`` — an answer that calls the point's own
  file by its bare name has answered the question;
* ``missing-root-is-null`` — without a tree to resolve paths against,
  the gate cannot tell anything.

The rows between them reach the branches the six above do not: an
answer that names no file at all, an answer that names only a bare
file name, a point that names only a bare file name, an answer that
names the right file, a citation that names yet another file, and a
verdict that is not a dict at all.

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

#: A tree that is not there — the gate cannot resolve paths without one.
_ABSENT_ROOT = _SubjectRoot()

#: The command a command-kind point declares it is run by.
_ENTRY = "pytest backend/tests/unit -q"

#: The reason the gate gives when the answer is about another file.
_MISMATCH_REASON = (
    "本验证点点名 backend/x.py，"
    "而结论只涉及 backend/y.py"
    " —— 裁决回答的不是这道验证点"
)

#: The same reason, with the cited file counted into the answer.
_CITED_MISMATCH_REASON = (
    "本验证点点名 backend/x.py，"
    "而结论只涉及 backend/y.py、backend/z.py"
    " —— 裁决回答的不是这道验证点"
)

#: A code-review point about ``backend/x.py``.
_CODE_REVIEW_POINT = {
    "id": "VP-028",
    "verification_method": "code_review",
    "expected_result": "检查 backend/x.py",
}

#: A point the framework grades by itself — no judgement is owed.
_FRAMEWORK_GRADED_POINT = {
    "id": "VP-031",
    "verification_method": "full_ci",
    "expected_result": "检查 backend/x.py",
}

#: A point that names a file this tree does not have.
_POINT_ABOUT_A_MISSING_FILE = {
    "id": "VP-032",
    "verification_method": "code_review",
    "expected_result": "检查 backend/never_written.py",
}

#: A command-kind point: the framework runs the entry and grades the
#: exit code, so the question is about the command, not about a file.
_COMMAND_POINT = {
    "id": "VP-033",
    "verification_method": "e2e",
    "e2e_kind": "command",
    "entry_command": _ENTRY,
}


#: The table itself (§4.2).  Every row names a judgment value, and the
#: rows between them reach every branch of the function.
CASES = [
    Case(
        id="non-evidence-method-is-not-judged",
        inputs=(
            _FRAMEWORK_GRADED_POINT,
            {"reasons": ["我看了 backend/y.py"]},
            _ROOT,
            None,
        ),
        expected=None,
    ),
    Case(
        id="command-record-exempts",
        inputs=(
            _COMMAND_POINT,
            {
                "reasons": ["命令跑完了"],
                "evidence": {"entry_command": _ENTRY, "exit_code": 0},
            },
            _ROOT,
            None,
        ),
        expected=None,
    ),
    Case(
        id="other-file-only-mismatches",
        inputs=(
            _CODE_REVIEW_POINT,
            {"reasons": ["我看了 backend/y.py"]},
            _ROOT,
            None,
        ),
        expected=_MISMATCH_REASON,
    ),
    Case(
        id="no-existing-expected-path-is-null",
        inputs=(
            _POINT_ABOUT_A_MISSING_FILE,
            {"reasons": ["我看了 backend/y.py"]},
            _ROOT,
            None,
        ),
        expected=None,
    ),
    Case(
        id="bare-filename-rescues",
        inputs=(
            _CODE_REVIEW_POINT,
            {"reasons": ["我看了 backend/y.py，也看了 x.py"]},
            _ROOT,
            None,
        ),
        expected=None,
    ),
    Case(
        id="missing-root-is-null",
        inputs=(
            _CODE_REVIEW_POINT,
            {"reasons": ["我看了 backend/y.py"]},
            _ABSENT_ROOT,
            None,
        ),
        expected=None,
    ),
    Case(
        id="verdict-naming-no-file-is-not-judged",
        inputs=(
            _CODE_REVIEW_POINT,
            {"reasons": ["整体看下来没有问题"]},
            _ROOT,
            None,
        ),
        expected=None,
    ),
    Case(
        id="verdict-naming-only-a-bare-file-name-is-not-judged",
        inputs=(
            _CODE_REVIEW_POINT,
            {"reasons": ["我看了 x.py"]},
            _ROOT,
            None,
        ),
        expected=None,
    ),
    Case(
        id="expected-naming-only-a-bare-file-name-is-null",
        inputs=(
            {
                "id": "VP-034",
                "verification_method": "code_review",
                "expected_result": "检查 x.py",
            },
            {"reasons": ["我看了 backend/y.py"]},
            _ROOT,
            None,
        ),
        expected=None,
    ),
    Case(
        id="verdict-naming-the-expected-file-is-on-subject",
        inputs=(
            _CODE_REVIEW_POINT,
            {"reasons": ["我看了 backend/x.py"]},
            _ROOT,
            None,
        ),
        expected=None,
    ),
    Case(
        id="citation-of-another-file-still-mismatches",
        inputs=(
            _CODE_REVIEW_POINT,
            {"reasons": ["我看了 backend/y.py"]},
            _ROOT,
            ["backend/z.py"],
        ),
        expected=_CITED_MISMATCH_REASON,
    ),
    Case(
        id="verdict-that-is-not-a-dict-is-not-judged",
        inputs=(
            _CODE_REVIEW_POINT,
            "not a verdict",
            _ROOT,
            None,
        ),
        expected=None,
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
    :func:`verdict_subject_mismatch`.  A table that left a branch
    unreached would be a table that let a new branch in unnoticed.
    """
    project = _subject_tree(tmp_path)
    _ROOT.path = project
    _ABSENT_ROOT.path = project / "no-tree-here"

    assert_decision_table(
        verification_evidence.verdict_subject_mismatch, CASES
    )
