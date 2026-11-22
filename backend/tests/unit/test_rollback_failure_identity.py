"""The rollback evidence must not change a failure's identity.

Task 5 makes an undeclared write leave evidence behind, so the failure
text is no longer one line but four -- the executor's own spelling on
the first line, then where the evidence patch was written, what was
rolled back, and what could not be::

    undeclared_write: a.py, b.py
    rollback_patch: /plan/.pdt/evidence/rollbacks/t1/1.patch
    rollback_rolled_back: a.py
    rollback_failed: (none)

Those three trailing lines are facts about the RUN, not about the
failure. The patch file is named after the attempt, so the very same
overstep produces a different text on attempt 1 and on attempt 2.
``classify_failure`` hashes the text into a signature and counts
occurrences on the fingerprint, so an identity that moves with the
attempt number reports one failure as a first occurrence every time:
the counter never reaches 2, and the targeted refiner that is supposed
to fix THIS task is never told what went wrong.

So the identity is computed from the text WITHOUT the evidence lines,
and the payload the refiner acts on (``detail``) is the first line
alone -- the refused paths, and nothing else. The label keeps the
original text: it is shown to a human, and a human wants to see where
the evidence is.

The KIND must not move either: a refused path outside the project dir
still downgrades to ``unknown``, because the "declare this path"
remedy would name a file this plan must not touch.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from attempt_rollback import format_rollback_failure  # noqa: E402
from failure_classification import (  # noqa: E402
    UNDECLARED_WRITE,
    UNKNOWN,
    classify_failure,
)


class _RollbackOutcome:
    """The outcome of a rollback, in the shape the formatter reads."""

    def __init__(self, patch_path: str, rolled_back=(), failed=()):
        self.patch_path = patch_path
        self.rolled_back = list(rolled_back)
        self.failed = list(failed)


def _failure_text(patch: str, paths=("a.py", "b.py")) -> str:
    """The failure text the executor records, built by its producer.

    The text comes from ``attempt_rollback.format_rollback_failure``
    rather than from a literal here: the format is the producer's
    contract, and a test that spelled it out itself would keep passing
    after the producer changed.
    """
    return format_rollback_failure(paths, _RollbackOutcome(patch))


# ---------------------------------------------------------------------------
# detail: the payload the refiner acts on
# ---------------------------------------------------------------------------
def test_detail_ignores_rollback_lines():
    """The payload is the refused paths -- not the evidence trail."""
    failure = classify_failure(_failure_text("/p/1.patch"))
    assert failure.detail == "a.py, b.py", failure.detail
    assert failure.kind == UNDECLARED_WRITE, failure.kind


# ---------------------------------------------------------------------------
# signature: the text identity
# ---------------------------------------------------------------------------
def test_signature_stable_across_attempt_numbers():
    """The same overstep is the same failure on every attempt.

    The evidence patch is named after the attempt (``1.patch``,
    ``2.patch``), so a signature computed over the whole text would
    make attempt 2 look like a first occurrence.
    """
    one = classify_failure(_failure_text("/p/1.patch"))
    two = classify_failure(_failure_text("/p/2.patch"))
    assert one.signature == two.signature, (
        f"the attempt number moved the signature: {one.signature!r} "
        f"!= {two.signature!r}"
    )
    # ... and the reason it does not: the identity is computed over the
    # text without the evidence lines, so it equals the identity of the
    # first line alone.
    assert one.signature == classify_failure("undeclared_write: a.py, b.py").signature


# ---------------------------------------------------------------------------
# fingerprint: the occurrence key
# ---------------------------------------------------------------------------
def test_fingerprint_stable_across_attempt_numbers():
    """The occurrence key must survive the evidence trail.

    The fingerprint is built from the structured tokens of the text, and
    the patch path is one of them: without this, every attempt carries
    its own key and the split gate can never open.
    """
    one = classify_failure(_failure_text("/p/1.patch"))
    two = classify_failure(_failure_text("/p/2.patch"))
    assert one.fingerprint == two.fingerprint, (
        f"the attempt number moved the fingerprint: {one.fingerprint!r} "
        f"!= {two.fingerprint!r}"
    )


# ---------------------------------------------------------------------------
# the kind: a foreign path still downgrades
# ---------------------------------------------------------------------------
def test_foreign_path_still_downgrades(tmp_path):
    """A refused path outside the project dir is still ``unknown``.

    The evidence lines are not the only reason a path can be foreign,
    and dropping them from the identity must not resurrect the
    write-contract reading for a file this plan does not own.
    """
    project = tmp_path / "repo"
    project.mkdir()
    foreign = tmp_path / "other" / "x.py"
    failure = classify_failure(
        _failure_text("/p/1.patch", paths=(str(foreign),)),
        project_dir=project,
    )
    assert failure.kind == UNKNOWN, failure.kind
    assert failure.scope_violation == [str(foreign)], failure.scope_violation


# ---------------------------------------------------------------------------
# the label: what a human reads
# ---------------------------------------------------------------------------
def test_label_still_names_paths():
    """The label keeps the original text, evidence included."""
    failure = classify_failure(_failure_text("/p/1.patch"))
    assert "undeclared_write" in failure.label, failure.label
    assert "a.py" in failure.label, failure.label
