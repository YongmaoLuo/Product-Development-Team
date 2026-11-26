"""The independent judge — PASS or FAIL, from the predicate and the evidence.

What this module is
-------------------
The judge is the only component in the finishing stage that says PASS or
FAIL, and it says it from **two** inputs and nothing else:

* the item's ``acceptance_predicate`` — a small, closed vocabulary of
  machine-checkable claims; and
* the evidence records the executor produced — exit codes, output
  streams, the observed product-code diff, an observed external status.

Everything else is out of reach by construction.  This module does not
import the executor, does not read the requirement book's prose file, and
reads no field of an evidence record other than the ones listed above.
That is deliberate: an executor that grades its own homework can always
find a way to say "done", and the whole point of splitting the two is
that the party that did the work does not get to say whether the work was
done.

What "no evidence" means
-------------------------
Evidence that cannot decide anything decides **FAIL**:

* no evidence records at all;
* a record that never reported an exit code;
* a record whose two output streams are both empty — for the kinds whose
  decision rests on the command's output.

The first two hold for every kind: a record with no exit code is not a
record of anything, and the absence of evidence is not evidence of
success.  The third is **kind-dependent**, because not every kind reads
the output.  ``exit_code``, ``file_exists`` and ``git_diff_empty`` are
decided from the exit code and the diff, so for those kinds a record
that reported an exit code is usable **even when it printed nothing** —
a command that succeeds silently (``git fetch origin``, ``git reset
--soft origin/main``) prints nothing by nature, and demanding output
would fail exactly the commands that succeeded.  ``stdout_contains``,
``regex_match`` and ``external_status_equals`` still need output: there
is nothing to match an empty stream against.

The truth table
---------------
Six predicate kinds, each with a PASS side and a FAIL side:

==========================  ==========================================
kind                        passes when
==========================  ==========================================
``exit_code``               the exit code equals ``equals`` (0 by default)
``stdout_contains``         stdout contains the literal
``file_exists``             the named path exists
``regex_match``             the pattern matches the named stream
``external_status_equals``  the observed status equals ``equals``
``git_diff_empty``          the observed product-code diff is empty
==========================  ==========================================

``git_diff_empty`` is A5's own criterion: a requirement book that wants
the finishing stage to leave the product code alone says so with it, and
the diff the evidence carries decides — empty passes, a non-empty diff
fails.  It is a criterion **about the diff**, not about a command's exit
code, so it is a kind of its own rather than a spelling of another one.

A kind the judge does not know is a FAIL with a readable reason, never an
exception: the finishing stage has to be able to report *why* an item did
not pass, and a traceback is not a report.

Which record decides
--------------------
An item may have been attempted more than once (the stage retries a
failed item once).  The **last** usable record decides, because that is
the newest fact about the item; an earlier attempt's output is history.

What the excerpt may carry
--------------------------
The excerpt is a copy of what the command printed, and a command that
printed a credential puts it straight into the report, the panel's
terminal summary and the failure notification.  So the copy is
redacted on the way out: the four credential shapes the security review
named are replaced with ``<redacted>``, and everything else — an item's
id, a path, an exit code — is left exactly as it was.  The boundary is
drawn here, where the text is **produced**, so the raw value never
exists outside this function.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

from postprocess.models import Verdict
from postprocess.notify import redact


#: The predicate kinds this judge knows.  The vocabulary is closed: a
#: requirement book that names a kind outside it gets a FAIL with a
#: reason, not a guess.
KNOWN_PREDICATE_KINDS: Tuple[str, ...] = (
    "exit_code",
    "stdout_contains",
    "file_exists",
    "regex_match",
    "external_status_equals",
    "git_diff_empty",
)

#: Spellings that mean one of the kinds above.  The requirement book is
#: written by a planner, and the same claim is spelled two ways in the
#: wild; both spellings mean the same thing here.
_KIND_ALIASES: Dict[str, str] = {
    "contains": "stdout_contains",
    "regex": "regex_match",
    "regex_match_stdout": "regex_match",
    "external_status": "external_status_equals",
    "diff_empty": "git_diff_empty",
    "git_diff_is_empty": "git_diff_empty",
    "product_code_untouched": "git_diff_empty",
}

#: How many characters of the record's own output an excerpt may carry.
#: Long output is clipped from the right, so the excerpt stays a
#: contiguous slice of what the command printed.
EXCERPT_LIMIT: int = 500

#: The predicate kinds whose decision rests on the exit code alone.
#:
#: These kinds read the exit code (and, for ``file_exists`` and
#: ``git_diff_empty``, the world they describe) — they never read the
#: command's output.  So for them a record that reported an exit code
#: **is** the evidence, output or no output: a command that succeeds
#: silently prints nothing, and a record that printed nothing is exactly
#: what a silent success looks like.  Demanding output here would fail
#: the commands that worked.
_EXIT_CODE_ONLY_KINDS: FrozenSet[str] = frozenset({
    "exit_code",
    "file_exists",
    "git_diff_empty",
})


# ---------------------------------------------------------------------------
# Reading fields without assuming a shape
# ---------------------------------------------------------------------------


def _read(source: Any, name: str, default: Any = None) -> Any:
    """One field of a record or an item — by key, or by attribute.

    The judge accepts both an object with attributes (the requirement
    book's item) and a plain mapping (a record dumped to disk), because
    both are the same thing as far as a decision is concerned.

    Args:
        source: The record or item to read from.
        name: The field to read.
        default: What to return when the field is absent.

    Returns:
        The field's value, or ``default``.
    """
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _first_present(mapping: Mapping[str, Any], names: Sequence[str], default: Any = None) -> Any:
    """The first of ``names`` the mapping carries, else ``default``."""
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _stream_text(record: Any, stream: Any) -> Optional[str]:
    """The text of one output stream.

    Args:
        record: The evidence record.
        stream: The stream's name — ``stdout``, ``stderr``, or both.

    Returns:
        The stream's text, or ``None`` when the stream is not one this
        judge knows.
    """
    name = str(stream or "stdout").strip().lower()
    if name in ("stdout", "out"):
        return str(_read(record, "stdout", "") or "")
    if name in ("stderr", "err"):
        return str(_read(record, "stderr", "") or "")
    if name in ("both", "combined", "all"):
        stdout = str(_read(record, "stdout", "") or "")
        stderr = str(_read(record, "stderr", "") or "")
        return stdout + stderr
    return None


def _clip(text: str, limit: int = EXCERPT_LIMIT) -> str:
    """Shorten a slice of text without breaking its contiguity."""
    if len(text) <= limit:
        return text
    return text[:limit]


def _excerpt(record: Any) -> str:
    """A redacted slice of the record's own output.

    The excerpt is what a reader of the report compares against the
    command that produced it, so it is copied, never written: the record's
    stdout if it has one, its stderr otherwise, and nothing at all when
    the record printed nothing.

    A copy of what a command printed is also where a credential that
    command printed ends up, so the copy is redacted on the way out.
    The redactor replaces the four credential shapes with
    ``<redacted>`` and leaves everything else — an item's id, a path, an
    exit code — exactly as it was; a credential is replaced, never
    dropped, so the reader can still see that something was there.

    Redacting **here**, where the text is produced, is what makes the
    boundary hold: the verdict never carries the raw value, so nothing
    downstream of it can republish it.
    """
    for name in ("stdout", "stderr"):
        text = str(_read(record, name, "") or "")
        if text.strip():
            return redact(_clip(text.strip()))
    return ""


def _is_usable(record: Any, canonical_kind: str) -> bool:
    """Whether a record carries evidence this predicate kind can rest on.

    What "usable" means depends on the kind, because different kinds read
    different fields of the record:

    * For the kinds that decide on the exit code alone — ``exit_code``,
      ``file_exists``, ``git_diff_empty`` — a record that reported an
      exit code is usable **even when it printed nothing**.  Those kinds
      never read the output, so the exit code is the whole of their
      evidence; and a command that succeeds silently prints nothing, so
      requiring output here would fail exactly the commands that
      succeeded.
    * For the kinds that read the command's output — ``stdout_contains``,
      ``regex_match`` — and for ``external_status_equals``, a record
      qualifies when it reported an exit code **and** printed something.
      Both halves matter there: there is nothing to match an empty stream
      against, and output without an exit code says only that some text
      was produced.

    In every case a record that never reported an exit code is not
    usable: that is the absence of evidence, and it is not evidence of
    success.
    """
    if _read(record, "exit_code", None) is None:
        return False
    if canonical_kind in _EXIT_CODE_ONLY_KINDS:
        return True
    for name in ("stdout", "stderr"):
        if str(_read(record, name, "") or "").strip():
            return True
    return False


# ---------------------------------------------------------------------------
# One function per predicate kind
# ---------------------------------------------------------------------------


def _judge_exit_code(
    predicate: Mapping[str, Any], record: Any, root: Optional[Path]
) -> Tuple[bool, str]:
    """The command exited with the code the predicate names."""
    expected = _first_present(predicate, ("equals", "value", "expected"), 0)
    actual = _read(record, "exit_code", None)
    if actual is None:
        return False, "evidence carries no exit code"
    try:
        wanted = int(expected)
    except (TypeError, ValueError):
        return False, f"exit_code predicate names a non-integer exit code: {expected!r}"
    try:
        got = int(actual)
    except (TypeError, ValueError):
        return False, f"evidence carries a non-integer exit code: {actual!r}"
    if got == wanted:
        return True, f"exit_code {got} == {wanted}"
    return False, f"exit_code {got} != {wanted}"


def _judge_stdout_contains(
    predicate: Mapping[str, Any], record: Any, root: Optional[Path]
) -> Tuple[bool, str]:
    """stdout contains the literal the predicate names."""
    literal = _first_present(
        predicate, ("literal", "value", "contains", "text", "needle")
    )
    if literal is None:
        return False, "stdout_contains predicate names no literal"
    stdout = str(_read(record, "stdout", "") or "")
    if str(literal) in stdout:
        return True, f"stdout contains {str(literal)!r}"
    return False, f"stdout does not contain {str(literal)!r}"


def _judge_file_exists(
    predicate: Mapping[str, Any], record: Any, root: Optional[Path]
) -> Tuple[bool, str]:
    """The file the predicate names is there.

    A relative path is read against ``root`` — the plan directory the
    stage runs in — when the caller supplies one, and against the
    process's working directory otherwise.
    """
    raw = _first_present(predicate, ("path", "file", "value"))
    if not raw:
        return False, "file_exists predicate names no path"
    path = Path(str(raw))
    if root is not None and not path.is_absolute():
        path = Path(root) / path
    if path.exists():
        return True, f"file exists: {path}"
    return False, f"file does not exist: {path}"


def _judge_regex_match(
    predicate: Mapping[str, Any], record: Any, root: Optional[Path]
) -> Tuple[bool, str]:
    """The pattern matches the stream the predicate names."""
    pattern = _first_present(predicate, ("pattern", "regex", "value"))
    if pattern is None:
        return False, "regex_match predicate names no pattern"
    stream = _first_present(predicate, ("stream", "target", "in"), "stdout")
    text = _stream_text(record, stream)
    if text is None:
        return False, f"regex_match predicate names an unknown stream: {stream!r}"
    try:
        compiled = re.compile(str(pattern))
    except re.error as exc:
        return False, f"regex_match pattern is invalid: {exc}"
    name = str(stream or "stdout").strip().lower()
    if compiled.search(text):
        return True, f"regex {str(pattern)!r} matched in {name}"
    return False, f"regex {str(pattern)!r} did not match in {name}"


def _judge_external_status_equals(
    predicate: Mapping[str, Any], record: Any, root: Optional[Path]
) -> Tuple[bool, str]:
    """The observed external status is the one the predicate names."""
    expected = _first_present(predicate, ("equals", "value", "expected", "status"))
    actual = _read(record, "external_status", None)
    if actual is None:
        return False, "evidence carries no external status"
    if str(actual) == str(expected):
        return True, f"external_status {actual!r} == {expected!r}"
    return False, f"external_status {actual!r} != {expected!r}"


def _judge_git_diff_empty(
    predicate: Mapping[str, Any], record: Any, root: Optional[Path]
) -> Tuple[bool, str]:
    """The product-code diff observed around the command is empty.

    A5's criterion: a requirement book that wants the finishing stage to
    leave the product code alone says so with this predicate.  The diff
    the evidence carries decides — empty passes, a non-empty diff fails,
    because a product code that was touched is exactly what the
    criterion forbids.
    """
    diff = str(_read(record, "git_diff", "") or "")
    if not diff.strip():
        return True, "the observed product-code diff is empty"
    return False, "the observed product-code diff is not empty"


#: The predicate kinds, each with the function that decides them.
_PREDICATE_JUDGES: Dict[str, Callable[[Mapping[str, Any], Any, Optional[Path]], Tuple[bool, str]]] = {
    "exit_code": _judge_exit_code,
    "stdout_contains": _judge_stdout_contains,
    "file_exists": _judge_file_exists,
    "regex_match": _judge_regex_match,
    "external_status_equals": _judge_external_status_equals,
    "git_diff_empty": _judge_git_diff_empty,
}


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------


def judge(
    item: Any,
    evidence_records: Sequence[Any],
    *,
    root: Optional[Path] = None,
) -> Verdict:
    """Decide one requirement item from its predicate and its evidence.

    Args:
        item: The requirement item — anything carrying an ``id`` and an
            ``acceptance_predicate``, as an object or as a mapping.
        evidence_records: The evidence the executor produced, oldest
            first.  The last usable record decides; earlier ones are
            history from earlier attempts.
        root: The directory a relative path in a ``file_exists``
            predicate is read against.  Defaults to the process's working
            directory.

    Returns:
        A :class:`~postprocess.models.Verdict`.  It says FAIL whenever
        the evidence cannot decide — no records, a record without an exit
        code, a record that printed nothing where the kind reads the
        output, a predicate kind this judge does not know — and it never
        raises.
    """
    item_id = str(_read(item, "id", "") or "")

    predicate = _read(item, "acceptance_predicate", None)
    if not isinstance(predicate, Mapping):
        return Verdict(
            item_id=item_id,
            passed=False,
            evidence_excerpt="",
            reason="the item carries no acceptance predicate",
        )

    kind = str(predicate.get("kind") or "").strip()
    canonical = _KIND_ALIASES.get(kind, kind)
    if canonical not in _PREDICATE_JUDGES:
        return Verdict(
            item_id=item_id,
            passed=False,
            evidence_excerpt="",
            reason=f"unknown acceptance predicate kind: {kind!r}",
        )

    records = list(evidence_records or [])
    if not records:
        return Verdict(
            item_id=item_id,
            passed=False,
            evidence_excerpt="",
            reason="no evidence records for this item",
        )

    usable = [record for record in records if _is_usable(record, canonical)]
    if not usable:
        return Verdict(
            item_id=item_id,
            passed=False,
            evidence_excerpt="",
            reason=(
                "evidence is insufficient: no record carries both an exit "
                "code and command output"
            ),
        )

    record = usable[-1]
    passed, reason = _PREDICATE_JUDGES[canonical](predicate, record, root)
    return Verdict(
        item_id=item_id,
        passed=passed,
        evidence_excerpt=_excerpt(record),
        reason=reason,
    )