"""Pins for the recorded negative controls of the pause/retry work.

What is under test
------------------
``docs/development/pause-and-retry-negative-controls.zh.md`` -- the
``## 负控`` section, which records the two negative controls of the
pause/retry work:

* **R2** -- take the busy-retry back out of the pause command, and
  ``test_pause_retries_locked_then_applies`` has to go red.
* **R1** -- put the stop back in front of the pause intent, and
  ``test_intent_written_before_stop`` has to go red.

Why the record needs a pin
--------------------------
Both tests are green on this machine because the fixes are in.  A
green run of a regression test proves very little on its own: the
same test is green on a machine where the defect never appears at
all, and a reader cannot tell the two apart from the output.  The
negative control is what separates them -- take the fix out, and the
same test has to fail.

A control that is not written down is a control nobody can check:
the next reader has to take it on trust, and "we checked once" is not
evidence that can be re-checked.  So the control is recorded in the
document, and these pins say what such a record has to carry.

What a record has to carry
---------------------------
1. The section exists, and it is not empty.
2. The red run: the command that took the fix out, the test case that
   failed, the assertion that failed, and a failure count that is not
   zero -- for **each** of the two controls, in its own record.
3. The restore: the command that puts the fix back, and the evidence
   that the tree is clean again -- ``git diff --stat`` printing
   nothing.
4. Nothing left unfinished.

Run them with:

    cd backend && python3 -m pytest tests/static_gates/test_pause_and_retry_negative_controls.py
"""

from __future__ import annotations

import re
from pathlib import Path

#: The repository root -- ``<repo>``.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The Chinese page of the document under test -- the contract.
_DOC_PATH = (
    _REPO_ROOT / "docs" / "development" / "pause-and-retry-negative-controls.zh.md"
)

#: The heading the negative controls are recorded under.
_HEADING = "负控"

#: The two controls, each with the case it makes fail and the assertion
#: that fails once the fix is taken back out.  The label is what the
#: document calls the control; the case is what a reader re-runs.
_CONTROLS = (
    (
        "R2",
        "test_pause_retries_locked_then_applies",
        'assert result.outcome == "applied"',
    ),
    (
        "R1",
        "test_intent_written_before_stop",
        'assert order == ["write", "stop"]',
    ),
)

#: The command that takes the fix back out.  ``apply -R`` is the
#: reverse one; the command that puts the fix *back* is the plain one.
_REVERT_COMMAND = re.compile(r"git\s+(?:-C\s+\S+\s+)?apply\s+-R\b")

#: The command that puts the fix back.
_RESTORE_COMMAND = re.compile(r"git\s+(?:-C\s+\S+\s+)?apply\s+(?!-R)")

#: The command that shows the tree is clean again.
_DIFF_STAT = re.compile(r"git\s+(?:-C\s+\S+\s+)?diff\s+--stat\b")

#: The failure count the red run reports: ``failed=`` and a number.
_FAILED_COUNT = re.compile(r"failed\s*=\s*(\d+)")

#: The ways a record can say that a command printed nothing.
_EMPTY_OUTPUT = ("无输出", "为空", "empty", "(none)")

#: Markers that mean the record is not finished.
_PLACEHOLDER = re.compile(r"(?i)t[b]d|t[o]do|待\s*定")


def _read() -> str:
    """The document's text."""
    assert _DOC_PATH.is_file(), (
        f"the document {_DOC_PATH.name} does not exist, so the negative "
        f"controls are not recorded anywhere"
    )
    return _DOC_PATH.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of the ``heading`` section, up to the next heading.

    Returns the empty string when there is no such section, so a
    missing section fails the way a missing requirement should.
    """
    pattern = re.compile(
        r"^##\s+" + re.escape(heading) + r"\s*$(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        return ""
    return match.group(1)


def _subsections(section: str) -> list[tuple[str, str]]:
    """The ``###`` subsections of a section, as (heading, body) pairs."""
    parts = re.split(r"^###\s+(.*?)\s*$", section, flags=re.MULTILINE)
    pairs = []
    for index in range(1, len(parts) - 1, 2):
        pairs.append((parts[index], parts[index + 1]))
    return pairs


def _record_for(section: str, case: str) -> str:
    """The body of the one subsection that records ``case``.

    One record per control: a case that is written down twice has no
    single red run behind it, and a case that is written down nowhere
    has none at all.
    """
    bodies = [body for _heading, body in _subsections(section) if case in body]
    assert len(bodies) == 1, (
        f"expected exactly one record naming {case!r}, found {len(bodies)}; "
        "each control needs a single record of its own red run"
    )
    return bodies[0]


def _lines_with(record: str, pattern: re.Pattern[str]) -> list[str]:
    """The lines of ``record`` that carry a match of ``pattern``."""
    return [line for line in record.splitlines() if pattern.search(line)]


# ---------------------------------------------------------------------------
# 1. The section exists
# ---------------------------------------------------------------------------


def test_negative_control_section_exists():
    """The document carries a negative-control section, and it is not empty."""
    text = _read()
    assert re.search(
        r"^##\s+" + re.escape(_HEADING) + r"\s*$", text, re.MULTILINE
    ), (
        f"the document has no '## {_HEADING}' section, so the negative "
        f"controls are not recorded anywhere a reader can find them"
    )

    section = _section(text, _HEADING)
    assert section.strip(), (
        f"the '## {_HEADING}' section is empty; a heading with nothing "
        f"under it records no control at all"
    )


# ---------------------------------------------------------------------------
# 2. The red runs
# ---------------------------------------------------------------------------


def test_records_red_run_for_retry_and_intent():
    """Each control records its own red run: revert, case, assertion, count."""
    section = _section(_read(), _HEADING)
    assert section.strip(), (
        f"no '## {_HEADING}' section, so there is no red run to record"
    )

    records = {}
    for label, case, assertion in _CONTROLS:
        record = _record_for(section, case)
        records[label] = record

        assert _REVERT_COMMAND.search(record), (
            f"the {label} record does not say how the fix was taken back "
            "out; a red run without the command that produced it cannot be "
            "re-run"
        )

        assert assertion in record, (
            f"the {label} record does not carry the failing assertion "
            f"({assertion!r}), so it says that something failed without "
            f"saying what"
        )

        counts = [int(count) for count in _FAILED_COUNT.findall(record)]
        assert counts, (
            f"the {label} record does not report a failure count, so it "
            "cannot show that the red run failed"
        )
        assert max(counts) > 0, (
            f"every failure count in the {label} record is zero ({counts!r}); "
            f"a control that reports no failure is not a red run"
        )

    assert records["R2"] != records["R1"], (
        "the two controls share one record, so one failure count cannot "
        "vouch for both of them"
    )


# ---------------------------------------------------------------------------
# 3. The restores
# ---------------------------------------------------------------------------


def test_records_restore_and_clean_tree():
    """Each control records how the fix goes back, and a clean tree."""
    section = _section(_read(), _HEADING)
    assert section.strip(), (
        f"no '## {_HEADING}' section, so there is no restore to record"
    )

    for label, case, _assertion in _CONTROLS:
        record = _record_for(section, case)

        revert_lines = _lines_with(record, _REVERT_COMMAND)
        restore_lines = _lines_with(record, _RESTORE_COMMAND)
        assert restore_lines, (
            f"the {label} record does not carry the command that puts the "
            "fix back; a control that leaves the tree reverted breaks "
            "everything after it"
        )
        assert not set(revert_lines) & set(restore_lines), (
            f"the {label} record puts the revert and the restore on one "
            f"line ({revert_lines!r}); the two commands have to be told "
            "apart to be re-run"
        )

        stat_lines = _lines_with(record, _DIFF_STAT)
        assert stat_lines, (
            f"the {label} record does not mention 'git diff --stat', so it "
            "does not show that the tree is clean again after the restore"
        )
        assert any(
            marker in line
            for line in stat_lines
            for marker in _EMPTY_OUTPUT
        ), (
            f"the {label} record mentions 'git diff --stat' but does not "
            f"say what it printed; a diff that is not empty means the "
            f"restore did not put the tree back: {stat_lines!r}"
        )


# ---------------------------------------------------------------------------
# 4. Nothing left unfinished
# ---------------------------------------------------------------------------


def test_has_no_placeholder():
    """The record says what it has to say, and nothing is left open."""
    text = _read()
    match = _PLACEHOLDER.search(text)
    assert match is None, f"unfinished marker {match.group(0)!r}"
