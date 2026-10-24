"""Pins for the recorded negative control of the service-spawn fix.

What is under test
------------------
``docs/development/service-spawn-root-cause.zh.md`` -- the ``## 负控``
section, which records what happens when the fix is taken back out:
the integration test goes red, and the fix goes back in.

Why the record needs a pin
--------------------------
The integration test (``tests/integration/test_service_spawn_repro.py``)
is green on this machine because the fix is in.  A green run of a
reproduction proves very little on its own: the same test is green on
a machine where the defect never appears at all, and a reader cannot
tell the two apart from the output.  The negative control is what
separates them -- take the fix out, and the same test has to fail.

A control that is not written down is a control nobody can check: the
next reader has to take it on trust, and "we checked once" is not
evidence that can be re-checked.  So the control is recorded in the
document, and these pins say what such a record has to carry.

What a record has to carry
---------------------------
1. The section exists, and it is not empty.
2. The red run: the command that took the fix out, the test case that
   failed, the assertion that failed, and a failure count that is not
   zero.
3. The restore: the command that puts the fix back, and the evidence
   that the tree is clean again -- ``git diff --stat`` printing
   nothing.
4. Nothing left unfinished.

Run them with:

    cd backend && .venv/bin/python3 -m pytest tests/unit/test_spawn_negative_control_recorded.py
"""

from __future__ import annotations

import re
from pathlib import Path

#: The repository root -- ``<repo>``.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The Chinese page of the document under test -- the contract.
_DOC_PATH = (
    _REPO_ROOT / "docs" / "development" / "service-spawn-root-cause.zh.md"
)

#: The heading the negative control is recorded under.
_HEADING = "负控"

#: The test case the negative control makes fail.
_TEST_CASE = "test_three_consecutive_spawns_all_ready"

#: The assertion that fails once the fix is taken out.
_ASSERTION = "assert failed == 0"

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
        f"control is not recorded anywhere a reader can find it"
    )

    section = _section(text, _HEADING)
    assert section.strip(), (
        f"the '## {_HEADING}' section is empty; a heading with nothing "
        f"under it records no control at all"
    )


# ---------------------------------------------------------------------------
# 2. The red run
# ---------------------------------------------------------------------------


def test_negative_control_records_red_run():
    """The record names the red run: the revert, the case, the assertion."""
    section = _section(_read(), _HEADING)
    assert section.strip(), (
        f"no '## {_HEADING}' section, so there is no red run to record"
    )

    assert _REVERT_COMMAND.search(section), (
        "the record does not say how the fix was taken back out; a red "
        "run without the command that produced it cannot be re-run"
    )

    assert _TEST_CASE in section, (
        f"the record does not name the test case that failed "
        f"({_TEST_CASE!r}), so a reader cannot tell which run went red"
    )

    assert _ASSERTION in section, (
        f"the record does not carry the failing assertion "
        f"({_ASSERTION!r}), so it says that something failed without "
        f"saying what"
    )

    counts = [int(count) for count in _FAILED_COUNT.findall(section)]
    assert counts, (
        "the record does not report a failure count, so it cannot show "
        "that the red run failed *and* failed on the spawns"
    )
    assert max(counts) > 0, (
        f"every failure count in the record is zero ({counts!r}); a "
        f"control that reports no failure is not a red run"
    )


# ---------------------------------------------------------------------------
# 3. The restore
# ---------------------------------------------------------------------------


def test_negative_control_records_restore():
    """The record says how the fix goes back, and that the tree is clean."""
    section = _section(_read(), _HEADING)
    assert section.strip(), (
        f"no '## {_HEADING}' section, so there is no restore to record"
    )

    assert _RESTORE_COMMAND.search(section), (
        "the record does not carry the command that puts the fix back; a "
        "control that leaves the tree reverted breaks everything after it"
    )

    stat_lines = [
        line for line in section.splitlines() if _DIFF_STAT.search(line)
    ]
    assert stat_lines, (
        "the record does not mention 'git diff --stat', so it does not "
        "show that the tree is clean again after the restore"
    )
    assert any(
        marker in line
        for line in stat_lines
        for marker in _EMPTY_OUTPUT
    ), (
        "the record mentions 'git diff --stat' but does not say what it "
        f"printed; a diff that is not empty means the restore did not "
        f"put the tree back: {stat_lines!r}"
    )


# ---------------------------------------------------------------------------
# 4. Nothing left unfinished
# ---------------------------------------------------------------------------


def test_negative_control_has_no_placeholder():
    """The record says what it has to say, and nothing is left open."""
    text = _read()
    match = _PLACEHOLDER.search(text)
    assert match is None, f"unfinished marker {match.group(0)!r}"