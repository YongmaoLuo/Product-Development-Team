"""Integrity contract for the second batch of the HITL fixture corpus.

What this file is
=================

The HITL loop has to reach the same decision on a failure it has seen
before as on one it has never seen.  The four failure classes
(:mod:`verification.failure_triage`) each carry a disposition, and the
disposition is what decides whether a human is interrupted, whether the
deliverable is reworked and whether the same-cause counter moves.

That decision is only trustworthy if the historical samples it was
tuned on are still replayable.  This file is the integrity contract for
the **second** batch of that corpus: the two ``out_of_scope`` samples
and the one ``deliverable_defect`` sample under
``backend/tests/fixtures/hitl/cases/``.

Why a second file rather than one file for the whole corpus
============================================================

The corpus is written in batches, and each batch gets its own
integrity file.  One file for the whole corpus would mean two tasks
editing the same file, and the merge of two independently written
contracts is where a corpus silently loses half its cases.  A file per
batch keeps each contract whole.

The corpus **directory** is shared — one JSON file per case, so two
tasks working on it cannot lose one another's case to a merge — but
each contract names its own cases and reads nothing else.  The first
batch's contract (``test_fixture_corpus_integrity.py``) covers the
``infra`` and ``criterion_defect`` samples; this one covers the rest.
A sample added by either batch therefore cannot break the other's
contract, and neither file has to know about the other's cases.

The schema
==========

The three samples carry the same shape as the first batch's, so a
reader who knows one sample of the corpus knows the next:

``case``
    The sample's id, which is also its file stem.
``description``
    What the sample is, in prose.
``source``
    The stage the sample is replayed from.  This is the one key the
    first batch does not carry, and it is here because this batch
    needs it: ``source`` is what decides **where** a human's question
    attaches — an execution-stage question hangs off the task being
    run, a verification-stage question off the check that found the
    problem.
``verdict``
    The verdict record: the class, the raw output, and the quote that
    anchors the class in that output.
``failure_history``
    The earlier rounds of the same point, each a verdict record of its
    own.  The counter reads them, so a round without a class cannot
    join — or be kept out of — a same-cause group.
``verification_plan``
    The verification points the sample talks about, with the diff of
    the failed attempt on the point the repair would touch.
``expected_disposition``
    What the loop is expected to do with the sample.

The dispositions, and why they are not the first batch's
==========================================================

The first batch pins the *triage* disposition — the one
:func:`verification.failure_triage.disposition_for` returns:
``retry_not_counted`` for an environment failure,
``escalate_counted`` for a broken criterion.  Those say what happens
to the **attempt**.

This batch pins the *loop* disposition — what happens to the **plan**:
a question in front of a human (``hitl_pending``) or a repair task
that redoes the work (``repair_task_generated``).  Those say what the
plan does next.

The two vocabularies are related but not interchangeable, and the
table below carries both: the loop disposition each class asks for,
and the triage disposition it comes from.  The triage side is checked
against production rather than against a copy of it, so the two layers
cannot drift apart unnoticed.

What each sample is for
=======================

``out_of_scope_scope_change``
    The scope changed and a task now asks for something the plan never
    agreed to build.  Raised at the **execution** stage: the agent
    sees the out-of-scope demand before it does the work.
``out_of_scope_env_missing``
    A verification point needs an environment (a live service) that the
    plan never provisioned.  Raised at the **verification** stage.

    The distinction from ``infra`` is the point of this sample: an
    ``infra`` failure is an environment that *broke* and is worth
    retrying, while this one is a requirement that was never in scope
    and is not worth retrying at all.
``deliverable_schema_mismatch``
    The deliverable itself has the wrong shape — the report it
    produced is missing a field the check reads.  The criterion is
    fine, so the work is not: the remedy is a repair task, and the
    repair has to know which files to touch.  Hence the seed.

The seed, and why it is not decoration
======================================

``backend/test_command_rules.derive_files_to_modify`` is the shared
rule that answers "which files did the failed attempt touch?", and
``backend/repair_generator.py`` calls it with the failed verification
point's failure text and its diff.  A repair task with no edit scope is
dropped downstream, so a deliverable sample that cannot derive a file
is a sample that cannot produce the repair it claims to describe.  The
seed therefore has to be derivable, and both of its sources have to
agree with the rule rather than with a second copy of it.

The privacy contract
====================

Every sample is synthetic: no absolute path, no operator name, no
private project, no plan id.  A fixture that leaks one of those is a
leak with a longer life than the run that produced it, so the samples
name only relative paths inside an imaginary project.  The quote is
quoted **verbatim** from the raw output — the tier-B rule
(:func:`verification.failure_triage.validate_evidence_quote`) is
strict about that on purpose, and a fixture that normalised its own
quote would train the corpus to accept a paraphrase.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

import pytest


# Ensure both roots are importable regardless of the runner's entry
# point: ``backend/`` for the modules under test (``test_command_rules``
# and the ``verification`` package), and the repository root for the
# ``backend.`` package the verification stack imports.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _root in (_BACKEND_DIR, _REPO_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from test_command_rules import derive_files_to_modify  # noqa: E402
from verification import failure_triage  # noqa: E402


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# The corpus this file owns
# ---------------------------------------------------------------------------
#
#: Where the samples live.  A path relative to the repository, not to the
#: machine it was written on: the corpus is replayed on other people's
#: checkouts.
CASES_DIR = _BACKEND_DIR / "tests" / "fixtures" / "hitl" / "cases"

#: The samples this batch owns, named by the case id (which is also the
#: file stem).  Listing them here rather than globbing the directory is
#: deliberate: the first batch's samples live in the same directory and
#: are pinned by the first batch's own contract, so a glob here would
#: make this file answer for samples it does not own.
BATCH2_CASES: Tuple[str, ...] = (
    "out_of_scope_scope_change",
    "out_of_scope_env_missing",
    "deliverable_schema_mismatch",
)

#: The failure class each sample is for.  The file name says what the
#: sample is about, and the sample has to agree with it: a corpus
#: whose names lie is a corpus nobody can navigate.
EXPECTED_CLASS_BY_CASE: Dict[str, str] = {
    "out_of_scope_scope_change": failure_triage.OUT_OF_SCOPE,
    "out_of_scope_env_missing": failure_triage.OUT_OF_SCOPE,
    "deliverable_schema_mismatch": failure_triage.DELIVERABLE_DEFECT,
}

#: The loop disposition each failure class asks for — what happens to
#: the plan.
EXPECTED_DISPOSITION_BY_CLASS: Dict[str, str] = {
    # The check asks for something nobody agreed to build: skip it, do
    # not count it, and put the question in front of a human.
    failure_triage.OUT_OF_SCOPE: "hitl_pending",
    # The deliverable is wrong: rework it.  The rework is a repair
    # task, so the plan moves on by itself.
    failure_triage.DELIVERABLE_DEFECT: "repair_task_generated",
}

#: The triage disposition each of those comes from, as
#: :func:`verification.failure_triage.disposition_for` reports it.
TRIAGE_DISPOSITION_BY_CLASS: Dict[str, str] = {
    failure_triage.OUT_OF_SCOPE: failure_triage.Disposition.SKIP_NOT_COUNTED,
    failure_triage.DELIVERABLE_DEFECT: failure_triage.Disposition.RETRY_COUNTED,
}

#: The stage a sample is replayed from.  It is what decides where a
#: human's question attaches: an execution-stage question hangs off the
#: task being run, a verification-stage question off the check that
#: found the problem.
SOURCES: Tuple[str, ...] = ("execution", "verification")

#: The keys every sample carries.  A sample missing one of them cannot
#: be replayed: there is nothing to read the failure out of, or nowhere
#: to read the expected answer from.
REQUIRED_CASE_KEYS: Tuple[str, ...] = (
    "case",
    "description",
    "source",
    "verdict",
    "failure_history",
    "verification_plan",
    "expected_disposition",
)

#: The keys every verdict record carries — the top-level one and every
#: entry of ``failure_history`` alike.
REQUIRED_VERDICT_KEYS: Tuple[str, ...] = (
    "verdict",
    "failure_class",
    "failure_class_source",
    "actual_result",
    "evidence_quote",
)

#: The keys every failure-history entry carries beyond the verdict ones:
#: which round it was.
REQUIRED_HISTORY_KEYS: Tuple[str, ...] = ("round",)

#: The keys every verification point carries.  ``id`` is what a question
#: or a repair task points at; ``verification_method`` says how the
#: criterion was checked; ``diff`` is the seed of the failed attempt.
REQUIRED_POINT_KEYS: Tuple[str, ...] = (
    "id",
    "verification_method",
    "criterion",
    "request",
    "replacement",
    "diff",
)

#: The keys every ``expected_disposition`` carries.  ``target_vp_id`` is
#: the point the question or the repair hangs off, and is ``null`` when
#: the sample is raised at the execution stage and hangs off a task
#: instead.
REQUIRED_DISPOSITION_KEYS: Tuple[str, ...] = (
    "disposition",
    "defaulted",
    "target_vp_id",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_cases() -> Dict[str, Dict[str, Any]]:
    """The batch's samples, keyed by case id.

    Raises:
        AssertionError: when a sample is missing or is not JSON.  A
            corpus with a hole in it is a hole in the replay, and it
            has to be reported as a failed expectation rather than as
            a collection error that names nothing.
    """
    cases: Dict[str, Dict[str, Any]] = {}
    for name in BATCH2_CASES:
        path = CASES_DIR / f"{name}.json"
        assert path.exists(), f"corpus sample is missing: {path.name}"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise AssertionError(f"{path.name} is not valid JSON: {exc}") from exc
        assert isinstance(payload, dict), f"{path.name} must hold a JSON object"
        cases[name] = payload
    return cases


def _points(case: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The verification points a sample carries."""
    plan = case.get("verification_plan") or {}
    points = plan.get("verification_points") or []
    return list(points)


def _point_by_id(case: Mapping[str, Any], vp_id: str) -> Mapping[str, Any]:
    """The verification point with this id."""
    for point in _points(case):
        if str(point.get("id")) == vp_id:
            return point
    raise AssertionError(f"verification point {vp_id!r} is not in the sample")


def _verdict_records(case: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Every verdict record in a sample: the current one, then the history.

    The history entries are verdict records in the same sense the
    current one is — a round that failed, with a class and a raw
    result — so the anchor invariant applies to them exactly as it
    applies to the verdict being replayed.
    """
    return [case["verdict"], *case["failure_history"]]


def _failure_record(case: Mapping[str, Any]) -> Dict[str, str]:
    """The sample's failure text in the shape the scope rule reads.

    Mirrors ``backend/repair_generator.py``'s ``_failure_record``: the
    raw output is the stack, the cited output is the output.  Kept
    here rather than imported because that helper is private to the
    repair generator, and a corpus contract should not depend on a
    private name that may be renamed.
    """
    verdict = case["verdict"]
    return {
        "stack": str(verdict.get("actual_result") or ""),
        "output": "",
    }


# ---------------------------------------------------------------------------
# 1. The schema every sample shares
# ---------------------------------------------------------------------------


def test_case_schema_batch2() -> None:
    """Every sample carries the keys a replay needs, and agrees with its name.

    The keys are checked one by one rather than as a set, because a
    missing key and a key spelled differently are the same failure for
    the replay and very different failures for the reader.
    """
    cases = _load_cases()

    for name, case in cases.items():
        missing = [key for key in REQUIRED_CASE_KEYS if key not in case]
        assert not missing, f"{name}: missing {missing}"

        # The case id is the file stem: the corpus is navigated by name.
        assert case["case"] == name, f"{name}: declares case {case['case']!r}"

        # The stage decides where the question attaches.
        assert case["source"] in SOURCES, (
            f"{name}: source {case['source']!r} is not one of {SOURCES}"
        )

        assert str(case["description"]).strip(), f"{name}: the sample needs a description"

        # -- the verdict ------------------------------------------------
        verdict = case["verdict"]
        assert isinstance(verdict, dict), f"{name}: verdict must be an object"
        missing = [key for key in REQUIRED_VERDICT_KEYS if key not in verdict]
        assert not missing, f"{name}: verdict is missing {missing}"

        assert verdict["verdict"] == "FAILED", (
            f"{name}: the corpus holds failures, got {verdict['verdict']!r}"
        )
        assert verdict["failure_class"] in failure_triage.ALL_FAILURE_CLASSES, (
            f"{name}: {verdict['failure_class']!r} is not one of the four classes"
        )
        assert verdict["failure_class_source"] in (
            failure_triage.SOURCE_LLM,
            failure_triage.SOURCE_DETERMINISTIC,
            failure_triage.SOURCE_DEFAULTED,
        ), f"{name}: unknown failure_class_source {verdict['failure_class_source']!r}"
        assert str(verdict["actual_result"]).strip(), (
            f"{name}: the raw output the quote anchors in must be present"
        )
        assert str(verdict["evidence_quote"]).strip(), f"{name}: the quote must be present"

        # -- the failure history ---------------------------------------
        history = case["failure_history"]
        assert isinstance(history, list), f"{name}: failure_history must be a list"
        for entry in history:
            assert isinstance(entry, dict), f"{name}: a history entry must be an object"
            missing = [
                key
                for key in (*REQUIRED_VERDICT_KEYS, *REQUIRED_HISTORY_KEYS)
                if key not in entry
            ]
            assert not missing, f"{name}: a history entry is missing {missing}"
            assert entry["failure_class"] in failure_triage.ALL_FAILURE_CLASSES, (
                f"{name}: history class {entry['failure_class']!r} is not one of the four"
            )

        # -- the verification plan -------------------------------------
        plan = case["verification_plan"]
        assert isinstance(plan, dict), f"{name}: verification_plan must be an object"
        seen: set = set()
        for point in _points(case):
            assert isinstance(point, dict), f"{name}: a verification point must be an object"
            missing = [key for key in REQUIRED_POINT_KEYS if key not in point]
            assert not missing, f"{name}: a verification point is missing {missing}"
            assert point["id"] not in seen, (
                f"{name}: duplicate verification point {point['id']!r}"
            )
            seen.add(point["id"])

        # -- the expected disposition ----------------------------------
        expected = case["expected_disposition"]
        assert isinstance(expected, dict), f"{name}: expected_disposition must be an object"
        missing = [key for key in REQUIRED_DISPOSITION_KEYS if key not in expected]
        assert not missing, f"{name}: expected_disposition is missing {missing}"
        assert expected["disposition"] in EXPECTED_DISPOSITION_BY_CLASS.values(), (
            f"{name}: unknown disposition {expected['disposition']!r}"
        )
        assert isinstance(expected["defaulted"], bool), (
            f"{name}: defaulted must say whether the class was earned"
        )

        # A repair task is pointed at a verification point; a question
        # is pointed at one only when it was raised by a check.
        if expected["disposition"] == "repair_task_generated":
            target = expected.get("target_vp_id")
            assert target, f"{name}: a repair task must name the point it repairs"
            assert target in seen, f"{name}: {target!r} is not in the sample's plan"
        if case["source"] == "verification":
            target = expected.get("target_vp_id")
            assert target, (
                f"{name}: a verification-stage question must name the point it hangs off"
            )
            assert target in seen, f"{name}: {target!r} is not in the sample's plan"
        if case["source"] == "execution":
            assert expected.get("target_vp_id") is None, (
                f"{name}: an execution-stage question hangs off the task,"
                f" not off a verification point"
            )

    # The two out-of-scope samples must attach at different stages, or
    # the corpus never exercises the choice the ``source`` field exists
    # to make.
    out_of_scope_sources = {
        case["source"]
        for name, case in cases.items()
        if EXPECTED_CLASS_BY_CASE[name] == failure_triage.OUT_OF_SCOPE
    }
    assert out_of_scope_sources == set(SOURCES), (
        f"the out-of-scope samples must cover both stages, got {out_of_scope_sources}"
    )


# ---------------------------------------------------------------------------
# 2. The disposition follows from the class
# ---------------------------------------------------------------------------


def test_disposition_matches_class() -> None:
    """The disposition a sample expects is the one its class asks for.

    Four things have to hold together, and each is checked against
    production rather than against a copy of it: the class the sample
    claims, the loop disposition that class maps to, the triage
    disposition the loop one comes from, and the evidence that earns
    the class in the first place.
    """
    cases = _load_cases()

    for name, case in cases.items():
        verdict = case["verdict"]
        failure_class = verdict["failure_class"]

        # The file name says what the sample is for; the sample agrees.
        assert failure_class == EXPECTED_CLASS_BY_CASE[name], (
            f"{name}: carries {failure_class!r},"
            f" expected {EXPECTED_CLASS_BY_CASE[name]!r}"
        )

        # The loop's answer follows from the class.
        expected = case["expected_disposition"]["disposition"]
        assert expected == EXPECTED_DISPOSITION_BY_CLASS[failure_class], (
            f"{name}: {failure_class} expects"
            f" {EXPECTED_DISPOSITION_BY_CLASS[failure_class]!r}, got {expected!r}"
        )

        # ... and it is the loop answer the triage table asks for.
        assert failure_triage.disposition_for(failure_class) == (
            TRIAGE_DISPOSITION_BY_CLASS[failure_class]
        ), f"{name}: the triage disposition of {failure_class!r} moved"

        # The class was earned, not assumed: the quote is in the raw
        # output, character for character.  A sample whose quote does
        # not anchor would be replayed as a degraded (counted) class
        # and would teach the corpus the wrong thing.  The same holds
        # for the rounds in the history.
        for record in _verdict_records(case):
            assert failure_triage.validate_evidence_quote(
                record["evidence_quote"], record["actual_result"]
            ), f"{name}: an evidence quote is not verbatim in its raw output"

        classified, source = failure_triage.classify_verdict(verdict)
        assert classified == failure_class, (
            f"{name}: the verdict classifies as {classified!r},"
            f" not {failure_class!r}"
        )
        assert source == verdict["failure_class_source"], (
            f"{name}: the classification source moved"
        )

        # A sample whose quotes all anchor has earned every class it
        # claims, so it must not declare itself defaulted.
        assert case["expected_disposition"]["defaulted"] is False, (
            f"{name}: every quote anchors, so nothing here is defaulted"
        )

        # The history does not join the same-cause group, so the
        # disposition above is the one the counter still allows.  An
        # entry of the same class on the same point would move the
        # counter, and the loop would stop at the sample's expected
        # answer and escalate instead.
        target_vp = case["expected_disposition"].get("target_vp_id")
        if target_vp and case["failure_history"]:
            current = failure_triage.decision_key(target_vp, failure_class)
            for entry in case["failure_history"]:
                assert failure_triage.decision_key(
                    target_vp, entry["failure_class"]
                ) != current, (
                    f"{name}: a history entry of {entry['failure_class']!r}"
                    f" joins the same-cause group of {current!r}"
                )


# ---------------------------------------------------------------------------
# 3. The deliverable sample can say which files to repair
# ---------------------------------------------------------------------------


def test_deliverable_has_file_seed() -> None:
    """A deliverable sample derives the files its repair has to touch.

    The shared rule (:func:`test_command_rules.derive_files_to_modify`)
    answers that from two sources — the failure text and the diff — and
    the repair generator calls it with exactly those.  A deliverable
    sample that derives nothing describes a repair that cannot be
    dispatched, so the seed is checked for derivability rather than
    taken on trust.
    """
    cases = _load_cases()

    deliverable_cases = {
        name: case
        for name, case in cases.items()
        if EXPECTED_CLASS_BY_CASE[name] == failure_triage.DELIVERABLE_DEFECT
    }
    assert deliverable_cases, "this batch must carry a deliverable_defect sample"

    for name, case in deliverable_cases.items():
        target_vp = case["expected_disposition"].get("target_vp_id")
        assert target_vp, f"{name}: a deliverable sample must name its point"
        point = _point_by_id(case, target_vp)

        diff = point.get("diff") or []
        assert isinstance(diff, list), f"{name}: diff must be a list"
        failure = _failure_record(case)

        # At least one of the two seeds is present.  A sample with
        # neither is a sample that cannot produce a repair.
        assert diff or failure["stack"], (
            f"{name}: a deliverable sample needs a stack or a diff"
        )

        derived = derive_files_to_modify(failure, diff)
        assert derived, (
            f"{name}: the seed derives no file, so the repair it describes"
            f" cannot be dispatched"
        )

        # The merged answer is the union of what the two sources say,
        # and nothing else.  Checked against the rule itself rather
        # than against a second copy of the path-reading logic.
        from_failure = derive_files_to_modify(failure, [])
        from_diff = derive_files_to_modify({}, diff)
        assert set(derived) == set(from_failure) | set(from_diff), (
            f"{name}: the derived files {derived} are not the union of the"
            f" failure text {from_failure} and the diff {from_diff}"
        )

    # At least one sample of this batch exercises both seeds, so the
    # merge — the part that had two copies and drifted — is covered.
    both_seeds = [
        name
        for name, case in deliverable_cases.items()
        if (_point_by_id(
            case, case["expected_disposition"]["target_vp_id"]
        ).get("diff") or [])
        and derive_files_to_modify(_failure_record(case), [])
    ]
    assert both_seeds, "no deliverable sample exercises both the stack and the diff"