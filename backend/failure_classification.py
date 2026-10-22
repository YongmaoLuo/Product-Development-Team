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
import os
import pathlib
import re
import tempfile

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

#: The verifier did not finish inside its budget (2026-10-22).
#:
#: This is NOT an infrastructure hiccup, and the difference is the whole
#: point of the kind. A verifier timeout means there is NO evidence about
#: what the subagent did — neither that it worked nor that it lied. Filed
#: as ``infra`` it would be "retry the same thing", which with the same
#: budget produces the same non-answer; filed as a split candidate it
#: would be broken into children that cannot be verified either. What it
#: calls for is a re-verification with a bigger budget.
VERIFIER_TIMEOUT = "verifier_timeout"

#: The executor's own spelling, carrying the free-text reason after the
#: colon. Only this exact prefix buys the kind: a generic timeout in the
#: middle of a message stays ``infra``.
VERIFIER_TIMEOUT_PREFIX = VERIFIER_TIMEOUT + ":"

# ---------------------------------------------------------------------------
# The remedy: what to DO about a failure (2026-10-22)
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. The kinds answered "WHAT went wrong?" and the split
# gate answered "MAY we split?" from a SECOND, parallel list. Two lists
# drift: a kind added to the first is silently absent from the second, and
# the answer for that kind becomes whatever the code happened to default
# to — which is how a failure nobody thought about ends up splittable.
#
# The remedy table collapses the two into one. Every kind names the action
# it calls for; the split gate asks a single question of that action. The
# table is the single source of truth, so a new kind cannot be splittable
# in one place and un-splittable in another.

#: Retry the task as it is. If it fails the SAME way again, splitting is
#: a legitimate next move — the task really may be too big for one
#: subagent.
RETRY_THEN_SPLIT = "retry_then_split"

#: Do not split: the failure carries a machine-readable ``detail`` and
#: the fix is a targeted refinement (declare this path, use this tool).
#: The children of a split would inherit the same incomplete declaration
#: and all reach for the same undeclared path.
REFINE_WITH_DETAIL = "refine_with_detail"

#: Do not split, whatever the occurrence count. The gate's premise —
#: "the same failure twice means the task is too big" — does not hold for
#: these: repeating them only repeats the same mistake. A contention
#: reproduces itself in the children; an infrastructure problem is not a
#: decomposition problem at all.
NEVER_SPLIT = "never_split"

#: Do not split: re-verify the same work with a bigger budget. The
#: verification did not produce a verdict, so there is nothing to
#: decompose — there is a missing measurement.
REVERIFY_WITH_MORE_BUDGET = "reverify_with_more_budget"

#: The remedy for every kind the module owns. TOTAL on purpose: a kind
#: with no row raises instead of defaulting, so a newly added kind that
#: forgets to register its remedy is RED rather than silently inheriting
#: whatever the old code did for an unknown kind.
REMEDY_BY_KIND = {
    # A rejected report may be a shape problem (fix the report) or a
    # genuinely too-big task (split it). The occurrence rule decides,
    # and the occurrence rule is the executor's.
    REPORT_FORMAT: RETRY_THEN_SPLIT,
    # Nothing changed. A second attempt with the same instructions
    # changes nothing either — but a split, whose children each carry
    # their own instructions, may.
    EMPTY_DIFF: RETRY_THEN_SPLIT,
    # The one case where a smaller subtask is a plausible next move.
    TEST_FAILURE: RETRY_THEN_SPLIT,
    # No evidence at all. Splitting on an absence of information
    # authorises a split on nothing; retrying gathers the information.
    UNKNOWN: RETRY_THEN_SPLIT,
    # One path, precisely named, is missing from the declaration.
    UNDECLARED_WRITE: REFINE_WITH_DETAIL,
    # Timeouts and provider/environment problems are not decomposition
    # problems at all.
    INFRA: NEVER_SPLIT,
    # Two tasks wanted the same file. The children inherit their
    # parents' declarations and want it again.
    LOCK_CONFLICT: NEVER_SPLIT,
    # No verdict was reached at all — the verification needs a bigger
    # budget, not a smaller task.
    VERIFIER_TIMEOUT: REVERIFY_WITH_MORE_BUDGET,
}


def remedy_for(kind: str) -> str:
    """The remedy registered for ``kind``.

    Raises:
        ValueError: ``kind`` has no registered remedy. Deliberate: a
            kind nobody registered has no answer, and returning a
            default here is exactly the silent behaviour this table
            exists to remove.
    """
    try:
        return REMEDY_BY_KIND[kind]
    except KeyError:
        raise ValueError(
            f"no remedy registered for failure kind {kind!r}; "
            f"known kinds: {sorted(REMEDY_BY_KIND)}"
        ) from None


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

    Derived from the failure's REMEDY rather than from a second list of
    kinds: one table is the single source of truth, so a new kind cannot
    be splittable in one place and un-splittable in another. The only
    remedy that opens the gate is ``retry_then_split`` — the others each
    name a more specific action (refine with the detail, re-verify with
    a bigger budget, never split).
    """
    return failure.remedy == RETRY_THEN_SPLIT


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

# ---------------------------------------------------------------------------
# The structured identity of a failure (2026-10-19)
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. ``signature`` hashes the failure TEXT. That is the
# right thing to show a human and the wrong thing to count on. Two
# attempts at the same broken test are not two copies of one string: the
# first run reports "pytest exited 1" and the second reports "process
# exited with status 1", the pytest root differs because the tmpdir
# differs, the attempt number differs. Text-keyed counting therefore
# reports those as two FIRST occurrences forever, the gate never reaches
# 2, and the runaway refiner this module exists to stop is stopped for
# the wrong reason — by never splitting rather than by splitting once.
#
# WHAT A FINGERPRINT IS. Five token classes that a re-worded but
# otherwise identical failure shares: the tool that failed, the exit
# code it returned, the exception classes named, the failing test ids,
# and the paths involved. Deliberately narrow — these are the facts a
# paraphrase cannot change. Anything wider would make two genuinely
# different failures collide, which is the failure mode this gate fears
# more than the one it fixes.
_TOOL_RE = re.compile(r"\b(claude|vendor-c|opencode)\b", re.IGNORECASE)
_EXIT_CODE_RE = re.compile(
    r"(?:exit(?:ed)?\s*code|exit_code|returncode|rc)\s*[=:]?\s*(\d+)",
    re.IGNORECASE,
)
_EXCEPTION_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Timeout))\b")
_TEST_ID_RE = re.compile(
    r"\b((?:tests?|backend)/[\w./-]+\.py::[\w.:\[\]-]+)"
)
_PATH_TOKEN_RE = re.compile(
    r"(?:/[\w.@+-]+){2,}|(?:[\w.@+-]+/)+[\w.@+-]+\.[A-Za-z0-9]+"
)

#: The unit separator, not a space and not a comma: ``"a b" + "c"`` and
#: ``"a" + "b c"`` must not produce the same input to the hash.
_TOKEN_SEP = chr(0x1F)


def _normalize_path_token(token: str, project_dir=None) -> str:
    """One path, spelled the same way every run.

    The tmpdir is the reason a project-relative spelling matters: pytest
    hands every test a fresh root under ``/private/var/folders/...``, so
    an absolute-path fingerprint is different on every run and counts as
    a new failure every time. Relativizing against the project dir (and
    prefixing a leftover absolute tmpdir with ``<tmp>``) keeps the token
    to the thing the failure is actually about.
    """
    token = str(token)
    if project_dir is not None and os.path.isabs(token):
        try:
            # ``relative_to`` and not ``os.path.relpath``: relpath never
            # fails, so an out-of-project path comes back as ``../..``
            # and the ``<tmp>`` rule below stops matching it. A path
            # that is NOT under the project has to keep its absolute
            # spelling to stay comparable at all.
            token = str(
                pathlib.PurePath(os.path.realpath(token)).relative_to(
                    os.path.realpath(str(project_dir))
                )
            )
        except ValueError:
            token = os.path.normpath(token)
    else:
        token = os.path.normpath(token)
    tmp = os.path.normpath(tempfile.gettempdir())
    if token == tmp or token.startswith(tmp + os.sep):
        token = "<tmp>" + token[len(tmp):]
    return token


def _structured_tokens(error, project_dir=None) -> dict:
    """The paraphrase-invariant identity of ``error``.

    Returns a dict with ``tool``, ``exit_code``, ``exceptions``,
    ``test_ids`` and ``paths``. ``tool`` and ``exit_code`` are strings
    (the FIRST occurrence of each, since a re-worded tail often quotes
    the same code twice); the rest are sorted, de-duplicated lists, so
    two mentions of one test id cannot look like two tests.
    """
    text = error or ""
    m_tool = _TOOL_RE.search(text)
    m_code = _EXIT_CODE_RE.search(text)
    return {
        "tool": (m_tool.group(1).lower() if m_tool else ""),
        "exit_code": (m_code.group(1) if m_code else ""),
        "exceptions": sorted({m for m in _EXCEPTION_RE.findall(text)}),
        "test_ids": sorted({m for m in _TEST_ID_RE.findall(text)}),
        "paths": sorted({
            _normalize_path_token(p, project_dir)
            for p in _PATH_TOKEN_RE.findall(text)
        }),
    }


@dataclass(frozen=True)
class Failure:
    """A classified failure.

    Attributes:
        kind: One of the module-level constants above.
        signature: Stable, short identity of the *error text*, scoped
            by kind. Shown in the logs, carried in the refiner block and
            persisted in the run record, and kept for compatibility with
            every existing reader of it. It hashes the words, so it is
            NOT what occurrence counting keys on — two re-wordings of
            one failure have two signatures. See ``fingerprint``.
        fingerprint: The key the executor counts occurrences on: the
            same identity, derived from the structured tokens (tool,
            exit code, exception classes, test ids, paths) rather than
            from the prose, so a paraphrase of one failure keeps one key
            and the split gate can actually reach 2. Falls back to
            ``signature`` when the text carries no structured token at
            all, so a failure with nothing to key on behaves exactly as
            it did before. Both properties still hold: the same failure
            twice gives the same fingerprint, two different failures
            never do.
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
    fingerprint: str = ""

    @property
    def remedy(self) -> str:
        """What to DO about this failure, derived from its kind.

        Read-only and DERIVED, not stored: the remedy table is the
        single source of truth, and a stored copy could disagree with
        it. It is also computed on ACCESS rather than at construction
        time, so building a ``Failure`` never raises — the contract
        ``classify_failure`` promises — while reading ``remedy`` for a
        kind nobody registered does raise ``ValueError`` rather than
        inventing a default.
        """
        return remedy_for(self.kind)


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
    The verifier timeout (2026-10-22) sits ahead of all of them, prefix
    and all, for the same reason: a message that says the run "timed
    out" is not a verdict, and filing it as ``infra`` would have the
    refiner retry what needs a bigger budget.
    """
    if not error:
        return UNKNOWN
    # The verifier did not finish inside its budget. Checked FIRST, and
    # ahead of the audit/mismatch rules below: the executor spells this
    # as an explicit prefix, and the message behind it says "timed out",
    # so a check placed after the generic timeout rule would file every
    # verifier timeout as ``infra`` — a failure the refiner is told to
    # keep retrying, when in truth there is no verdict to act on at all.
    if error.startswith(VERIFIER_TIMEOUT_PREFIX):
        return VERIFIER_TIMEOUT
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


def classify_failure(error, project_dir=None):
    """Classify a failure reason into kind + stable signature + fingerprint.

    Args:
        error: The failure text the executor recorded for a task.
        project_dir: The run's project dir, when known. Used only to
            spell paths relatively in the fingerprint — a pytest tmpdir
            differs on every run, so an absolute path would make one
            failure count as a new one each time.

    Returns:
        A frozen :class:`Failure`. Never raises: an empty or odd-shaped
        input yields ``UNKNOWN`` rather than taking down the executor.
    """
    text = error or ""
    kind = _classify_kind(text)
    digest = hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()[:12]
    signature = f"{kind}:{digest}"
    tokens = _structured_tokens(text, project_dir)
    if not any(tokens.values()):
        # Nothing structured to key on: the text signature IS the best
        # available identity, and inventing a hash of five empty strings
        # would collapse every such failure in a lineage onto one key.
        fingerprint = signature
    else:
        payload = _TOKEN_SEP.join([
            kind,
            tokens["tool"],
            tokens["exit_code"],
            ",".join(tokens["exceptions"]),
            ",".join(tokens["paths"]),
            ",".join(tokens["test_ids"]),
        ])
        fingerprint = (
            f"{kind}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"
        )
    return Failure(
        kind=kind,
        signature=signature,
        label=_build_label(kind, text),
        detail=_extract_detail(text),
        fingerprint=fingerprint,
    )


def is_split_allowed(failure: Failure) -> bool:
    """Whether ``failure`` may open the refiner's split gate.

    Exported so the executor asks the module that OWNS the kinds, rather
    than re-encoding the rule at the one call site that uses it (where a
    future kind would be added to neither).

    Raises:
        ValueError: ``failure.kind`` has no registered remedy. A failure
            that ``classify_failure`` produced never triggers this — every
            kind it can produce is registered — but a hand-built failure
            with a kind nobody registered has no answer, and a default
            here would be a guess.
    """
    return _split_allowed(failure)
