"""The independent judge — five predicate kinds, and evidence sufficiency.

What this file witnesses
------------------------
The judge is the only thing in the finishing stage that says PASS or FAIL,
and it says it from two inputs and nothing else: the item's acceptance
predicate and the evidence records the executor produced.  So this file
pins four contracts:

1. **The truth table.**  Each of the five predicate kinds the architecture
   declares — ``exit_code``, ``stdout_contains``, ``file_exists``,
   ``regex_match``, ``external_status_equals`` — has a PASS side and a
   FAIL side, and the two sides differ **only in the evidence**, never in
   the wording of the predicate.  A judge that answered "PASS" for every
   kind would satisfy the PASS halves of this file and fail the FAIL
   halves; a judge that answered "FAIL" for everything would do the
   reverse.  Only a judge that actually looks satisfies both.

2. **Evidence sufficiency.**  Evidence that cannot decide anything decides
   **FAIL**: no records at all, a record without an exit code, and — for
   the kinds that read the command's output — a record whose two output
   streams are both empty.  A stage that lets a missing exit code pass is
   a stage that passes on nothing having run.  What counts as sufficient
   depends on the kind, because a command that succeeds silently prints
   nothing: for the kinds decided on the exit code alone, an exit code
   with no output is the whole of the evidence.

3. **A self-report is not evidence.**  A record that carries a "the work is
   done" field but no command output fails — the judge reads command
   output, not somebody's opinion about it.

4. **The excerpt is the record's own text.**  ``evidence_excerpt`` is a
   verbatim slice of the judged record's stdout/stderr.  It is never a
   sentence the judge wrote: the report is read by a human deciding
   whether to trust the run, and a paraphrase is a claim, not evidence.

5. **An unknown predicate kind fails with a reason.**  The judge must not
   raise: the finishing stage has to be able to report *why* an item did
   not pass, and an exception is not a report.

Why the item is spelled two ways
---------------------------------
The judge's contract is "the item's ``acceptance_predicate``", and task 3
owns that class.  Until it lands, this file uses a local stand-in with
the same shape (an object with ``id`` and ``acceptance_predicate``) *and*
the shared fixture's dict item, so the judge is pinned against both ways
a caller can hand it an item — and against a change in task 3 that
renames the attribute.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import postprocess.judge as judge_module
from postprocess.judge import judge
from postprocess.models import EvidenceRecord
from tests.fixtures.postprocess.builders import make_item


@dataclass
class _SpecItemLike:
    """The shape task 3's ``SpecItem`` has: an id and a predicate.

    A stand-in, not a copy: it carries the two attributes the judge is
    allowed to read and nothing else, so the test cannot pass by leaning
    on a field the judge has no business reading.
    """

    id: str
    acceptance_predicate: Dict[str, Any]


def _item(predicate: Dict[str, Any], item_id: str = "push") -> _SpecItemLike:
    """An item shaped like the requirement book's element."""
    return _SpecItemLike(id=item_id, acceptance_predicate=dict(predicate))


def _evidence(**overrides: Any) -> EvidenceRecord:
    """One evidence record — a command that ran and said something."""
    fields: Dict[str, Any] = {
        "item_id": "push",
        "command": ["git", "push"],
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
        "git_diff": "",
    }
    fields.update(overrides)
    return EvidenceRecord(**fields)


# ---------------------------------------------------------------------------
# The truth table: five kinds, each with a PASS side and a FAIL side.
# ---------------------------------------------------------------------------


def test_exit_code_zero_passes_and_nonzero_fails():
    """Exit code 0 passes; any other exit code fails."""
    predicate = {"kind": "exit_code", "equals": 0}

    passed = judge(_item(predicate), [_evidence(exit_code=0)])

    assert passed.passed is True
    assert passed.item_id == "push"
    assert passed.reason == "exit_code 0 == 0"
    assert passed.evidence_excerpt == "ok"

    failed = judge(_item(predicate), [_evidence(exit_code=1, stdout="boom")])

    assert failed.passed is False
    assert failed.reason == "exit_code 1 != 0"


def test_stdout_contains_literal():
    """A literal in stdout passes; the same literal missing fails."""
    predicate = {"kind": "stdout_contains", "literal": "pushed"}

    passed = judge(_item(predicate), [_evidence(stdout="branch pushed to origin")])

    assert passed.passed is True

    failed = judge(_item(predicate), [_evidence(stdout="nothing happened")])

    assert failed.passed is False


def test_file_exists(tmp_path):
    """The named file being there passes; it not being there fails."""
    target = tmp_path / "release-notes.md"
    predicate = {"kind": "file_exists", "path": str(target)}

    missing = judge(_item(predicate), [_evidence()])

    assert missing.passed is False

    target.write_text("notes\n", encoding="utf-8")
    present = judge(_item(predicate), [_evidence()])

    assert present.passed is True


def test_file_exists_resolves_a_relative_path_against_the_root(tmp_path):
    """A relative path is read against the caller's root, not the CWD."""
    (tmp_path / "notes.md").write_text("notes\n", encoding="utf-8")

    verdict = judge(
        _item({"kind": "file_exists", "path": "notes.md"}),
        [_evidence()],
        root=tmp_path,
    )

    assert verdict.passed is True


def test_regex_match_stdout():
    """A pattern that matches stdout passes; one that does not fails."""
    predicate = {"kind": "regex_match", "pattern": r"pr-\d+ merged"}

    passed = judge(_item(predicate), [_evidence(stdout="pr-42 merged")])

    assert passed.passed is True

    failed = judge(_item(predicate), [_evidence(stdout="pr-x merged")])

    assert failed.passed is False


def test_external_status_equals():
    """The observed external status decides; another status fails."""
    predicate = {"kind": "external_status_equals", "equals": "merged"}

    passed = judge(_item(predicate), [_evidence(external_status="merged")])

    assert passed.passed is True

    failed = judge(_item(predicate), [_evidence(external_status="open")])

    assert failed.passed is False


# ---------------------------------------------------------------------------
# Evidence sufficiency: no evidence is a FAIL, never a PASS.
# ---------------------------------------------------------------------------


def test_no_evidence_must_fail():
    """Zero evidence records decide nothing, so they decide FAIL."""
    verdict = judge(_item({"kind": "exit_code", "equals": 0}), [])

    assert verdict.passed is False
    assert verdict.evidence_excerpt == ""
    assert "evidence" in verdict.reason.lower()


def test_record_without_exit_code_must_fail():
    """A record that never reported an exit code is not evidence."""
    verdict = judge(_item({"kind": "exit_code", "equals": 0}), [_evidence(exit_code=None)])

    assert verdict.passed is False
    assert verdict.evidence_excerpt == ""


def test_record_without_any_output_must_fail():
    """A record whose two output streams are empty is not evidence.

    For a kind that reads the command's output there is nothing to
    match an empty stream against, so such a record decides nothing and
    decides FAIL.
    """
    verdict = judge(
        _item({"kind": "stdout_contains", "literal": "ok"}),
        [_evidence(stdout="", stderr="")],
    )

    assert verdict.passed is False
    assert verdict.evidence_excerpt == ""


def test_conclusion_field_is_ignored_and_fails():
    """A self-report is not a command output — it cannot pass an item."""
    record = _evidence(stdout="", stderr="")
    record.conclusion = "done"

    verdict = judge(_item({"kind": "stdout_contains", "literal": "ok"}), [record])

    assert verdict.passed is False
    assert verdict.evidence_excerpt == ""


def test_a_mapping_evidence_with_a_self_report_field_also_fails():
    """The same case as a plain mapping — the extra field is not read."""
    mapping = {
        "item_id": "push",
        "command": ["git", "push"],
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "git_diff": "",
        "external_status": None,
        "conclusion": "done",
    }

    verdict = judge(_item({"kind": "stdout_contains", "literal": "ok"}), [mapping])

    assert verdict.passed is False


def test_stderr_does_not_overrule_a_predicate_that_only_reads_stdout():
    """A noisy stderr does not make the judge change its mind."""
    predicate = {"kind": "stdout_contains", "literal": "pushed"}

    verdict = judge(_item(predicate), [_evidence(stdout="", stderr="fatal: not pushed")])

    assert verdict.passed is False


def test_the_latest_attempt_decides():
    """A retried item is decided by its last evidence, not its first."""
    failed_attempt = _evidence(exit_code=1, stdout="failed")
    passed_attempt = _evidence(exit_code=0, stdout="ok")

    verdict = judge(
        _item({"kind": "exit_code", "equals": 0}),
        [failed_attempt, passed_attempt],
    )

    assert verdict.passed is True
    assert verdict.evidence_excerpt == "ok"


# ---------------------------------------------------------------------------
# A command that succeeds silently: the exit code is the whole evidence.
# ---------------------------------------------------------------------------


def test_exit_code_zero_passes_when_the_command_printed_nothing():
    """Exit code 0 with empty streams passes — a silent success is a success."""
    verdict = judge(
        _item({"kind": "exit_code", "equals": 0}),
        [_evidence(exit_code=0, stdout="", stderr="")],
    )

    assert verdict.passed is True
    assert verdict.reason == "exit_code 0 == 0"
    assert verdict.evidence_excerpt == ""


def test_exit_code_nonzero_fails_when_the_command_printed_nothing():
    """Exit code 1 with empty streams still fails — and says why."""
    verdict = judge(
        _item({"kind": "exit_code", "equals": 0}),
        [_evidence(exit_code=1, stdout="", stderr="")],
    )

    assert verdict.passed is False
    assert verdict.reason == "exit_code 1 != 0"


def test_a_silent_fetch_origin_is_evidence():
    """The real case: ``git fetch origin`` prints nothing when it works.

    It exits 0 and says nothing at all, so a judge that demands output
    would call the successful fetch a failure.
    """
    record = EvidenceRecord(
        item_id="fetch-origin",
        command=["git", "fetch", "origin"],
        exit_code=0,
        stdout="",
        stderr="",
        git_diff="",
    )

    verdict = judge(_item({"kind": "exit_code"}, "fetch-origin"), [record])

    assert verdict.passed is True
    assert verdict.reason == "exit_code 0 == 0"


def test_stdout_contains_still_needs_the_command_output():
    """A kind that matches the output cannot decide on an empty stream."""
    verdict = judge(
        _item({"kind": "stdout_contains", "literal": "pushed"}),
        [_evidence(exit_code=0, stdout="", stderr="")],
    )

    assert verdict.passed is False
    assert verdict.evidence_excerpt == ""


def test_regex_match_still_needs_the_command_output():
    """The same for the kind that matches a pattern against a stream."""
    verdict = judge(
        _item({"kind": "regex_match", "pattern": r"pr-\d+ merged"}),
        [_evidence(exit_code=0, stdout="", stderr="")],
    )

    assert verdict.passed is False
    assert verdict.evidence_excerpt == ""


def test_file_exists_passes_when_the_command_printed_nothing(tmp_path):
    """``file_exists`` is decided from the path, not from the output."""
    target = tmp_path / "release-notes.md"
    target.write_text("notes\n", encoding="utf-8")

    verdict = judge(
        _item({"kind": "file_exists", "path": str(target)}),
        [_evidence(exit_code=0, stdout="", stderr="")],
    )

    assert verdict.passed is True


def test_git_diff_empty_passes_when_the_command_printed_nothing():
    """An empty diff is the evidence — and it is exactly an empty string."""
    verdict = judge(
        _item({"kind": "git_diff_empty"}),
        [_evidence(exit_code=0, stdout="", stderr="", git_diff="")],
    )

    assert verdict.passed is True
    assert verdict.reason == "the observed product-code diff is empty"


def test_external_status_equals_still_needs_the_command_output():
    """The external-status kind is unchanged: it wants output too."""
    verdict = judge(
        _item({"kind": "external_status_equals", "equals": "merged"}),
        [_evidence(exit_code=0, stdout="", stderr="", external_status="merged")],
    )

    assert verdict.passed is False
    assert verdict.evidence_excerpt == ""


def test_a_record_without_an_exit_code_is_never_usable():
    """No exit code is no evidence — whatever the kind, whatever the output."""
    silent = judge(
        _item({"kind": "exit_code", "equals": 0}),
        [_evidence(exit_code=None, stdout="", stderr="")],
    )

    assert silent.passed is False
    assert silent.evidence_excerpt == ""

    noisy = judge(
        _item({"kind": "stdout_contains", "literal": "ok"}),
        [_evidence(exit_code=None, stdout="ok", stderr="")],
    )

    assert noisy.passed is False
    assert noisy.evidence_excerpt == ""


# ---------------------------------------------------------------------------
# The excerpt is the record's own text; an unknown kind fails with a reason.
# ---------------------------------------------------------------------------


def test_evidence_excerpt_is_verbatim_substring():
    """The excerpt is a slice of the record's own output, not a retelling."""
    stdout = "line one\nline two\n"

    verdict = judge(
        _item({"kind": "stdout_contains", "literal": "line two"}),
        [_evidence(stdout=stdout)],
    )

    assert verdict.passed is True
    assert verdict.evidence_excerpt in stdout
    assert verdict.evidence_excerpt != verdict.reason, (
        "the excerpt and the reason are different things: the first is the "
        "record's text, the second is the judge's sentence"
    )


def test_unknown_predicate_kind_fails_with_a_reason():
    """An unknown kind is a FAIL with a readable reason — not an exception."""
    verdict = judge(_item({"kind": "telepathy"}), [_evidence()])

    assert verdict.passed is False
    assert "telepathy" in verdict.reason
    assert verdict.evidence_excerpt == ""


def test_an_invalid_regex_fails_with_a_reason():
    """A broken pattern is a FAIL with a reason — not an exception."""
    verdict = judge(_item({"kind": "regex_match", "pattern": "([unclosed"}), [_evidence()])

    assert verdict.passed is False
    assert verdict.reason


def test_the_item_may_arrive_as_a_mapping():
    """The shared fixture's dict item is read the same way."""
    item = make_item(id="push", acceptance_predicate={"kind": "exit_code", "equals": 0})

    verdict = judge(item, [_evidence()])

    assert verdict.passed is True
    assert verdict.item_id == "push"


def test_the_judge_does_not_import_the_executor():
    """Judging and executing must not know each other."""
    tree = ast.parse(Path(judge_module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)

    forbidden = ("executor", "orchestrator", "waits")
    offenders = [
        name
        for name in imported
        if name.rsplit(".", 1)[-1] in forbidden
    ]

    assert not offenders, f"the judge must not import {offenders}"