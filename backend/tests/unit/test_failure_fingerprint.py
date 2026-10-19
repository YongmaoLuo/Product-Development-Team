"""The occurrence key must survive a paraphrase (2026-10-19).

The gate this module feeds counts how many times *one* failure has
happened in a task lineage, and splits on the second. It counted on
``signature``, which hashes the failure TEXT — and a sub-agent retrying
the same broken test does not print the same words twice:

    attempt 1: "pytest exited 1"
    attempt 2: "process exited with status 1"

Same tool, same exit code, same exception, same test id, same file. Two
text signatures, so two "first occurrences", so the counter never
reaches 2 and the gate never opens. The refiner is then held whole not
by the rule this gate exists to enforce but by a miscount inside it —
and the next runaway it fails to stop is stopped by accident.

``fingerprint`` is the replacement key: the tool, the exit code, the
exception classes, the test ids and the paths. Those are the facts a
re-wording cannot change. The tests below pin both halves of the
trade — paraphrases must COLLIDE (so the counter works) and genuinely
different failures must NOT (or the gate opens on two unrelated
failures, which is the incident this whole mechanism came from).

``test_failure_classification.py`` owns the text signature and is
unchanged; this file owns the counter's key.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from failure_classification import (  # noqa: E402
    REPORT_FORMAT,
    TEST_FAILURE,
    classify_failure,
)

#: One real failure, told two ways. Every structured token is identical;
#: the prose is not. This pair is the entire reason ``fingerprint``
#: exists.
FIRST_WORDING = (
    "claude run failed: tests/unit/test_thing.py::test_thing_adds_two "
    "raised ValueError while editing backend/calc.py (exit code 1)"
)
SECOND_WORDING = (
    "the claude subagent hit the same wall: "
    "tests/unit/test_thing.py::test_thing_adds_two -> ValueError on "
    "backend/calc.py; rc: 1"
)


def _fp(text, project_dir=None):
    return classify_failure(text, project_dir=project_dir).fingerprint


# ---------------------------------------------------------------------------
# the paraphrase must collapse
# ---------------------------------------------------------------------------
def test_two_wordings_of_one_failure_share_a_fingerprint():
    """The regression, stated as the two strings a retry actually produces."""
    first = _fp(FIRST_WORDING)
    second = _fp(SECOND_WORDING)
    assert first == second, (
        f"one failure told two ways counted twice, so the split gate can "
        f"never reach 2: {first!r} vs {second!r}"
    )
    assert first.startswith(f"{TEST_FAILURE}:"), first


def test_the_two_wordings_still_have_different_signatures():
    """Why the key had to move rather than the signature changing.

    The signature is what the logs show and what the refiner block and
    the run record carry; making it paraphrase-stable would mean
    overwriting two real, different observations of one failure with a
    single string, and an operator could no longer see what the second
    attempt actually printed.
    """
    assert (
        classify_failure(FIRST_WORDING).signature
        != classify_failure(SECOND_WORDING).signature
    )


def test_a_project_relative_path_does_not_move_the_key(tmp_path):
    """The reason ``classify_failure`` takes ``project_dir`` at all.

    pytest gives every run a different tmpdir. An absolute path token
    therefore makes the same failure count as a new one every time —
    the same bug as the paraphrase, arriving from a different direction.
    """
    project = tmp_path / "project"
    (project / "backend").mkdir(parents=True)
    in_project = str(project / "backend" / "calc.py")

    first = _fp(
        f"AssertionError: expected 3 got 2 in {in_project} (exit code 1)",
        project_dir=project,
    )
    second = _fp(
        f"the run refused {in_project} again: AssertionError, exit code 1",
        project_dir=project,
    )
    assert first == second, (
        "one file, two wordings, two keys: the counter is being reset by "
        "the tmpdir rather than by the failure"
    )


def test_an_absolute_tmp_path_collapses_to_a_placeholder(tmp_path, monkeypatch):
    """A path under a per-run temp root cannot spell the failure at all.

    With no ``project_dir`` there is nothing to relativize against, so
    the only stable spelling left is the tempdir replaced by a
    placeholder. Two different tmpdirs of the same shape then agree.
    """
    import tempfile as tempfile_mod

    roots = []
    for name in ("run-1", "run-2"):
        root = tmp_path / name
        (root / "backend").mkdir(parents=True)
        roots.append(root)

    def _fp_under(root):
        monkeypatch.setattr(tempfile_mod, "gettempdir", lambda: str(root))
        return _fp(f"ValueError while writing {root / 'backend' / 'calc.py'}")

    first = _fp_under(roots[0])
    second = _fp_under(roots[1])
    assert first == second, (
        f"the same file under two tmpdirs must be one failure: "
        f"{first!r} vs {second!r}"
    )


# ---------------------------------------------------------------------------
# and genuinely different failures must NOT collapse
# ---------------------------------------------------------------------------
def test_a_different_exception_class_is_a_different_failure():
    assert _fp(FIRST_WORDING) != _fp(
        FIRST_WORDING.replace("ValueError", "TypeError")
    )


def test_a_different_exit_code_is_a_different_failure():
    assert _fp(FIRST_WORDING) != _fp(
        FIRST_WORDING.replace("exit code 1", "exit code 2")
    )


def test_a_different_test_id_is_a_different_failure():
    """Two different tests failing is two findings, not one that repeated.

    Collapsing them would hand the refiner a "the same thing happened
    twice" licence to split on the strength of two unrelated bugs.
    """
    assert _fp(FIRST_WORDING) != _fp(
        FIRST_WORDING.replace("test_thing_adds_two", "test_thing_subtracts_one")
    )


def test_a_different_file_is_a_different_failure():
    assert _fp(FIRST_WORDING) != _fp(
        FIRST_WORDING.replace("backend/calc.py", "backend/db.py")
    )


def test_a_different_kind_is_a_different_failure():
    """Same tail, two rules that claim it.

    ``_classify_kind`` reads the PREFIX, so the same test tail filed
    under a report rejection and under a test failure must not share a
    counter entry — they call for opposite refiner instructions.
    """
    tail = "claude reported exit code 1 while editing backend/calc.py"
    report = _fp("audit_second_pass_failed: " + tail)
    genuine = _fp(tail)
    assert report != genuine
    assert report.startswith(f"{REPORT_FORMAT}:")
    assert genuine.startswith(f"{TEST_FAILURE}:")


# ---------------------------------------------------------------------------
# no structured token: behave exactly as before
# ---------------------------------------------------------------------------
def test_text_with_no_structured_token_falls_back_to_the_signature():
    """A rejection that is pure prose keeps the old key.

    Nothing to key on but the words, so the text signature IS the best
    available identity. Hashing five empty strings instead would give
    every such failure in a lineage the same key — the gate would open
    on the second unrelated rejection, which is the failure mode this
    mechanism exists to prevent.
    """
    failure = classify_failure(
        "audit_second_pass_failed: evidence table truncated at row 40 of 118"
    )
    assert failure.fingerprint == failure.signature


def test_two_different_prose_rejections_still_do_not_collide():
    """The fallback must not be a blanket "everything with no tokens is one key"."""
    one = classify_failure(
        "audit_second_pass_failed: evidence table truncated at row 40 of 118"
    )
    two = classify_failure(
        "audit_second_pass_failed: quoted exit code 0 but the run exited 1"
    )
    assert one.fingerprint != two.fingerprint


@pytest.mark.parametrize(
    "text",
    [
        "",
        "empty_diff_no_changes",
        "HARD TIMEOUT after 1800s while running the test_command",
    ],
)
def test_an_empty_or_unstructured_failure_still_classifies(text):
    """Never raises, and never invents a key out of nothing."""
    failure = classify_failure(text)
    assert failure.fingerprint
    assert failure.fingerprint == failure.signature


# ---------------------------------------------------------------------------
# the counter, end to end through the executor
# ---------------------------------------------------------------------------
def test_the_executor_counts_a_reworded_repeat_as_a_repeat():
    """The production symptom, driven through the real occurrence table.

    Without this the module could pass every test above while
    ``_note_failure`` still keyed on ``signature``, and the gate would
    behave exactly as it does today. The observation point is the
    returned occurrence, which is the number the split decision reads.
    """
    from agent import AutonomousAgent
    from task import SubTask

    executor = AutonomousAgent.__new__(AutonomousAgent)
    executor.logger = None
    executor.project_dir = Path("/tmp/_failure_fingerprint_dummy")
    executor._failure_occurrences = {}
    task = SubTask(
        id="1", title="t", description="d", test_command="echo ok",
        status="pending",
    )

    _, first = executor._note_failure(task, FIRST_WORDING)
    _, second = executor._note_failure(task, SECOND_WORDING)

    assert first == 1, first
    assert second == 2, (
        "the second, differently-worded attempt at the same failure must "
        "read as a REPEAT; otherwise the split gate is unreachable for "
        "exactly the failures it was built for"
    )
