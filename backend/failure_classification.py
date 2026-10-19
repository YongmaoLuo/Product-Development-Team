"""
Failure classification for the refiner split gate.

Why this module exists
======================

The refiner used to split a task on *every* failure, unconditionally. A
production plan therefore ran one task chain down to the 5-level
breakdown cap: 4 tasks at depth 4 went straight to ``failed``, 6
downstream tasks were permanently ``deferred``, and the executor
stopped with ``No schedulable micro-layer found``.

The dominant failure in that incident was ``audit_second_pass_failed``
-- the second-pass reviewer refused the *evidence* the subagent
reported. The code may well have been correct. Splitting it further
cannot fix a report-shape problem; it only reproduces the same failure
in the children, one level deeper each time.

The gate is "split only on the SECOND occurrence of the SAME failure".
That word *same* is what this module supplies: a stable signature, so
the executor can count occurrences of one error across a task lineage
and treat every other error as a first occurrence. The rules are pure
functions with no I/O so they can be unit-tested directly, with no
agent, no config and no database.

The split that matters most is ``report_format`` (the answer's shape was
rejected, the code may be fine) versus ``test_failure`` (the code
genuinely does not pass) -- only the latter is evidence that the task is
too big for one subagent.
"""

from dataclasses import dataclass
import hashlib
import re

#: The answer's shape was rejected -- truncated evidence, a claimed
#: ``TEST_RESULT: PASSED`` the run did not support, a vacuous test
#: command. Splitting does not address this.
REPORT_FORMAT = "report_format"

#: The subagent changed nothing. Addressed by telling it which tool to
#: use, not by decomposition.
EMPTY_DIFF = "empty_diff"

#: Tests genuinely ran and genuinely failed. The one case where a
#: smaller subtask is a plausible next move.
TEST_FAILURE = "test_failure"

#: Timeouts and provider/environment problems. Not a decomposition
#: problem at all.
INFRA = "infra"

#: Two tasks wanted the same file at the same time (2026-10-19). Splitting
#: does not remove a contention between two tasks — it reproduces it in
#: the children, which inherit their parents' declarations — so this must
#: never open the gate. What it needs is a different declaration or a
#: different schedule, both of which are a decision the planner has to
#: make, not a decomposition.
LOCK_CONFLICT = "lock_conflict"

#: The task tried to write a path its ``files_to_modify`` never named, and
#: the write was refused (2026-10-19). Also not a decomposition problem:
#: the children would each carry the same incomplete declaration. It is a
#: one-path fix, which is why ``detail`` carries the refused path.
UNDECLARED_WRITE = "undeclared_write"

#: No error text, or text that matches no rule above.
UNKNOWN = "unknown"

#: Kinds that must never be split, whatever their occurrence count.
#:
#: The gate's premise is "the same failure twice means the task is too big
#: for one subagent". That premise does not hold for a contention or a
#: contract violation: repeating them only repeats the same mistake. The
#: executor asks this question separately (``occurrence >= 2 and
#: failure.kind != 'infra'``); these two extend that to the failures that
#: arrived with the write contract.
_UNSPLITTABLE_KINDS = frozenset({LOCK_CONFLICT, UNDECLARED_WRITE})

#: ``undeclared_write: <path>[, <path>...]`` — the executor's own
#: spelling, carrying the paths the guard refused.
_UNDECLARED_WRITE_RE = re.compile(
    r"undeclared_write:\s*(?P<paths>.+)", re.IGNORECASE | re.DOTALL
)

#: The guard's own refusal sentence, used when the message DID survive the
#: trip through the sub-agent. The quoted path is between "declare: " and
#: the first ". " that ends the sentence.
_GUARD_REFUSAL_RE = re.compile(
    r"refusing to edit a file this task did not declare:\s*"
    r"(?P<paths>.+?)\s*\.\s*This is not a lock conflict",
    re.IGNORECASE | re.DOTALL,
)


def _split_allowed(failure: "Failure") -> bool:
    """Whether the refiner may break this task down.

    Kept next to the kinds it depends on rather than spelled out at the
    call site, so a new un-splittable kind cannot be forgotten there.
    """
    return failure.kind not in _UNSPLITTABLE_KINDS and failure.kind != INFRA


def _extract_detail(text: str) -> str:
    """The machine-readable payload of a failure, or ``""``.

    Only ``undeclared_write`` carries one today, and it exists because
    the refiner cannot act on "the declaration was incomplete" alone: the
    one action that fixes it is a task whose ``files_to_modify`` names
    the specific refused path, and the refiner cannot guess which file
    that was.
    """
    m = _UNDECLARED_WRITE_RE.search(text or "")
    if m:
        return m.group("paths").strip()
    m = _GUARD_REFUSAL_RE.search(text or "")
    if m:
        return m.group("paths").strip()
    return ""

#: Upper bound for ``Failure.label``. It is rendered into the refiner
#: prompt, so it must stay one readable line.
_LABEL_MAX_CHARS = 120


@dataclass(frozen=True)
class Failure:
    """A classified failure.

    Attributes:
        kind: One of the module-level constants above.
        signature: Stable, short identity of the *error*, scoped by
            kind. Two occurrences of the same error always produce the
            same signature; two different errors never do. This is the
            key the executor counts occurrences on, so it must survive
            incidental churn (attempt numbers, hashes, ids) in the
            error text and must not collapse distinct errors together.
        label: One human-readable line, at most ``_LABEL_MAX_CHARS``
            chars, for the prompt and the logs. Readability lives HERE
            rather than in the signature: a readable slug such as
            ``report_format:audit-second-pass`` cannot satisfy
            "different errors must get different signatures", because
            two different ``audit_second_pass_failed`` rejections would
            then share one key and the second would be miscounted as a
            confirmation of the first.
        detail: Optional[str], the machine-readable payload behind the
            label — today, the path(s) a write was refused for. Empty for
            every other kind. Carried in its own field rather than
            appended to ``label`` because ``label`` is a display string
            that is truncated to ``_LABEL_MAX_CHARS`` and rendered into
            the prompt, and a truncated path is worse than no path: the
            refiner would act on a partial one.
    """

    kind: str
    signature: str
    label: str
    detail: str = ""


# A run of hex characters. After lower-casing, a maximal run of >= 8
# ``[0-9a-f]`` characters is treated as a digest / uuid fragment.
_HEX_RUN_RE = re.compile(r"[0-9a-f]{8,}")
_HAS_HEX_LETTER_RE = re.compile(r"[a-f]")
_NUMBER_RE = re.compile(r"\d+")


def _mask_hex(match):
    """Replace a hex-looking run with ``<hex>``.

    A run of digits only is deliberately left alone so the number pass
    can render it ``<n>`` instead -- ``deadbeef`` is a digest,
    ``12345678`` is a count, and the two should not share a mask.
    """
    run = match.group(0)
    if not _HAS_HEX_LETTER_RE.search(run):
        return run
    return "<hex>"


def _normalize(error):
    """Collapse incidental churn so the same error hashes the same.

    Lower-cases, masks hex digests and bare numbers, and squeezes
    whitespace. Two occurrences of one failure differ only in their
    task id, attempt number and blob hash; none of those may reach the
    signature.
    """
    lowered = (error or "").lower()
    masked = _HEX_RUN_RE.sub(_mask_hex, lowered)
    masked = _NUMBER_RE.sub("<n>", masked)
    return " ".join(masked.split())


def _classify_kind(error):
    """Map an error string onto a kind. Order is significant.

    ``empty_diff`` is tested before ``infra`` because the empty-diff
    sentinel is generated by the executor itself and must never be
    mistaken for a timeout. The lock kinds (2026-10-19) sit ahead of
    ``infra`` for the same reason, plus one more: a file-lock TIMEOUT
    contains the word "timeout", so without this the contention would be
    filed as a generic infrastructure hiccup — a failure the refiner is
    told is fine to keep retrying, which is precisely what it must not do.
    """
    if not error:
        return UNKNOWN
    # A refusal by the second-pass audit: the evidence was rejected.
    if error.startswith("audit_second_pass_failed"):
        return REPORT_FORMAT
    # The subagent claimed PASSED; the actual run did not support it.
    if error.startswith("Subagent test-report mismatch"):
        return REPORT_FORMAT
    # Evidence that certifies nothing.
    if "ran no tests" in error or "cannot certify this task" in error:
        return REPORT_FORMAT
    if error.startswith("empty_diff_no_changes"):
        return EMPTY_DIFF
    # A refused write outside the declaration. Checked before the lock
    # kinds because its message is the most specific one there is — it
    # names a path — while the other two only say "you could not have
    # the file right now".
    if (
        error.startswith("undeclared_write:")
        or "refusing to edit a file this task did not declare" in error
    ):
        return UNDECLARED_WRITE
    if (
        "File lock deadlock" in error
        or "File lock timeout" in error
        or "another task is editing this file" in error
    ):
        return LOCK_CONFLICT
    if (
        "HARD TIMEOUT" in error
        or "timed out" in error
        or "Timeout" in error
    ):
        return INFRA
    return TEST_FAILURE


def _build_label(kind, error):
    """One line, at most ``_LABEL_MAX_CHARS`` chars."""
    flat = " ".join((error or "").strip().split())
    return f"{kind}: {flat}"[:_LABEL_MAX_CHARS]


def classify_failure(error):
    """Classify a failure reason into kind + stable signature.

    Args:
        error: The failure text the executor recorded for a task.

    Returns:
        A frozen :class:`Failure`. Never raises: an empty or odd-shaped
        input yields ``UNKNOWN`` rather than taking down the executor.
    """
    text = error or ""
    kind = _classify_kind(text)
    digest = hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()[:12]
    return Failure(
        kind=kind,
        signature=f"{kind}:{digest}",
        label=_build_label(kind, text),
        detail=_extract_detail(text),
    )


def is_split_allowed(failure: Failure) -> bool:
    """Whether ``failure`` may open the refiner's split gate.

    Exported so the executor asks the module that OWNS the kinds, rather
    than re-encoding the rule at the one call site that uses it (where a
    future kind would be added to neither).
    """
    return _split_allowed(failure)
