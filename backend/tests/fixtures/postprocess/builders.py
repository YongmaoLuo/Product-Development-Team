"""Shared sample shapes for the post-processing (收尾) stage.

What this module is
-------------------
Twenty-odd tasks downstream all need the same handful of sample shapes:
a requirement item, a requirement book, a progress file, a report, an
evidence record, a clock they can push forward, and a notifier they can
read back.  Written inline in every test file those shapes drift — one
case spells the item with six keys, the next with seven — and a red
test then means "my sample was wrong" instead of "the code is wrong".

So the samples are built **here, once**, and every case takes the same
shapes from the same place.

What this module is NOT
-----------------------
It is not a model of the finishing stage.  It imports nothing from
``postprocess`` — the modules that own these structures do not exist
yet, and a fixture that imported them would fail to import until the
last of them lands.  Every builder here is a **plain dict factory**:
it assembles the keys the architecture declares and validates nothing
beyond "is this a key I know".  Deciding whether a requirement book is
*legal* is the requirement-loading task's job; deciding whether an
item *passes* is the judge's job.  This module only makes samples.

The one rule every builder enforces
-----------------------------------
An unknown key is an error (:class:`KeyError`), never a silently added
extra.  A fixture that quietly accepted ``extenal=True`` would produce
a sample with a typo'd field, the code under test would ignore it, and
the case would pass while testing nothing.  Refusing the key turns
that into a loud failure at the point of the typo.

Why the clock and the notifier are here too
--------------------------------------------
Both exist because the alternative is worse.  A test that needs "seven
days later" either sleeps for seven days or monkeypatches
``datetime.now`` globally — and a global patch leaks into every other
test in the same worker, so the failure shows up somewhere else,
minutes later, in a case nobody was looking at.  The clock is
therefore **injected**: it moves only where the code under test asks
it to, and the wall clock behind it never jumps.  The notifier is a
sink: it records what would have been sent and sends nothing, so a
case can assert the notification's content without a channel, a
delivery, or a real recipient.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional


__all__ = [
    "ITEM_KEYS",
    "FakeClock",
    "FakeNotifier",
    "fake_notifier",
    "make_evidence",
    "make_item",
    "make_progress",
    "make_report",
    "make_spec",
]


#: The keys one requirement item carries — the architecture's data model
#: for ``postprocess.json``'s ``items[]`` element.  Kept here so a case
#: can talk about "an item" without re-spelling the list; the *test*
#: spells it out on its own, because a list compared against itself
#: cannot witness that it did not shrink.
ITEM_KEYS: tuple[str, ...] = (
    "id",
    "action",
    "acceptance_predicate",
    "evidence_requirement",
    "external",
    "required",
    "wait",
)

#: The placeholder fingerprint of a frozen requirement book.  A real
#: digest comes from the freezing task; this one only has to *look*
#: like a sha256 so that a case checking "the progress file carries a
#: fingerprint" is not satisfied by an empty string.
DEFAULT_SPEC_SHA256: str = "0" * 64

#: The placeholder start time of a progress file.
DEFAULT_STARTED_AT: str = "2026-01-01T00:00:00+00:00"

#: The default predicate: "the command exited 0".  It is the one kind
#: every executor can satisfy and every judge can read.
DEFAULT_ACCEPTANCE_PREDICATE: Dict[str, Any] = {"kind": "exit_code"}

#: The default evidence requirement.  The shape of this field is owned
#: by the requirement-validation layer; the fixture only pins a
#: plausible starting value, and a case that needs another shape passes
#: its own.
DEFAULT_EVIDENCE_REQUIREMENT: Dict[str, Any] = {"kind": "exit_code"}

#: The default wait: no waiting at all.
DEFAULT_WAIT: Dict[str, Any] = {"type": "none"}

#: The default per-item progress record.
DEFAULT_PROGRESS_RECORD: Dict[str, Any] = {
    "status": "pending",
    "attempts": 0,
    "last_evidence_ref": None,
}

#: The default per-item report record (one item's verdict + audit).
DEFAULT_REPORT_RECORD: Dict[str, Any] = {
    "id": "",
    "verdict": "PASS",
    "evidence_excerpt": "",
    "audit": {"action": "", "command": "", "target": ""},
}


def _reject_unknown(builder: str, unknown: Mapping[str, Any]) -> None:
    """Raise :class:`KeyError` naming the keys the builder does not know.

    The message names the builder and every offending key: the failure
    has to point at the typo, and "unexpected keyword argument" does
    not say which argument.
    """
    if not unknown:
        return
    raise KeyError(
        f"{builder} received unknown key(s): {', '.join(sorted(unknown))}"
    )


def make_item(
    *,
    id: str = "item",
    action: str = "...",
    acceptance_predicate: Optional[Mapping[str, Any]] = None,
    evidence_requirement: Optional[Mapping[str, Any]] = None,
    external: bool = False,
    required: bool = True,
    wait: Optional[Mapping[str, Any]] = None,
    **unknown: Any,
) -> Dict[str, Any]:
    """One requirement item — the seven keys the architecture declares.

    Args:
        id: The item's identifier, unique inside one requirement book.
        action: What the item does, in words.
        acceptance_predicate: How the judge decides the item passed.
        evidence_requirement: Which evidence the item must produce.
        external: Whether the item performs an outward-facing action.
        required: Whether the item's failure fails the whole book.
        wait: The wait shape — ``{"type": "none"|"human"|"external"}``.

    Returns:
        A fresh dict carrying exactly :data:`ITEM_KEYS`.  Two calls never
        share a nested dict: mutating one item's ``wait`` cannot reach
        the next item's default.

    Raises:
        KeyError: A keyword this builder does not know was passed.
    """
    _reject_unknown("make_item", unknown)

    return {
        "id": id,
        "action": action,
        "acceptance_predicate": copy.deepcopy(
            DEFAULT_ACCEPTANCE_PREDICATE
            if acceptance_predicate is None
            else acceptance_predicate
        ),
        "evidence_requirement": copy.deepcopy(
            DEFAULT_EVIDENCE_REQUIREMENT
            if evidence_requirement is None
            else evidence_requirement
        ),
        "external": external,
        "required": required,
        "wait": copy.deepcopy(DEFAULT_WAIT if wait is None else wait),
    }


def make_spec(
    items: Optional[List[Mapping[str, Any]]] = None,
    **unknown: Any,
) -> Dict[str, Any]:
    """A requirement book: ``{"items": [...]}``.

    An empty book is constructible on purpose — the requirement-loading
    task needs an empty book to prove it *rejects* one, and a builder
    that refused to build it would push that case to spell the dict by
    hand, which is where sample drift starts.

    Args:
        items: The items, in execution order.

    Returns:
        A fresh dict with one key, ``items``.  The items are copied, so
        the book and the caller's items do not alias each other.

    Raises:
        KeyError: A keyword this builder does not know was passed.
    """
    _reject_unknown("make_spec", unknown)

    return {
        "items": [copy.deepcopy(dict(item)) for item in (items or [])],
    }


def make_progress(
    items: Optional[Mapping[str, Mapping[str, Any]]] = None,
    spec_sha256: str = DEFAULT_SPEC_SHA256,
    started_at: str = DEFAULT_STARTED_AT,
    **unknown: Any,
) -> Dict[str, Any]:
    """A progress file: ``{spec_sha256, started_at, items: {...}}``.

    The progress file is what a resumed run reads to decide what to
    retry — only the entries that failed.  Its per-item record is
    therefore filled in from a default (pending, no attempts, no
    evidence yet) and the caller's partial record is layered on top, so
    a case can say ``{"status": "failed"}`` and get a complete record
    without spelling the rest.

    Args:
        items: Per-item records, keyed by item id.
        spec_sha256: The frozen requirement book's fingerprint.
        started_at: When the finishing stage started.

    Returns:
        A fresh dict with the three keys the architecture declares.

    Raises:
        KeyError: A keyword this builder does not know was passed.
    """
    _reject_unknown("make_progress", unknown)

    records: Dict[str, Dict[str, Any]] = {}
    for item_id, partial in (items or {}).items():
        record = copy.deepcopy(DEFAULT_PROGRESS_RECORD)
        record.update(copy.deepcopy(dict(partial)))
        records[item_id] = record

    return {
        "spec_sha256": spec_sha256,
        "started_at": started_at,
        "items": records,
    }


def make_report(
    items: Optional[List[Mapping[str, Any]]] = None,
    spec_sha256: str = DEFAULT_SPEC_SHA256,
    terminal_status: str = "passed",
    failure_context_ref: Optional[str] = None,
    **unknown: Any,
) -> Dict[str, Any]:
    """A terminal report: verdicts, evidence excerpts and audit triples.

    Args:
        items: Per-item report records; each is layered on top of the
            default record, so a case can pass only the fields it cares
            about.
        spec_sha256: The frozen requirement book's fingerprint.
        terminal_status: The finishing stage's terminal status.
        failure_context_ref: Where a human finds the context of a
            failure — the progress file's path.

    Returns:
        A fresh dict with the four keys the architecture declares.

    Raises:
        KeyError: A keyword this builder does not know was passed.
    """
    _reject_unknown("make_report", unknown)

    records: List[Dict[str, Any]] = []
    for partial in (items or []):
        record = copy.deepcopy(DEFAULT_REPORT_RECORD)
        record.update(copy.deepcopy(dict(partial)))
        records.append(record)

    return {
        "spec_sha256": spec_sha256,
        "terminal_status": terminal_status,
        "items": records,
        "failure_context_ref": failure_context_ref,
    }


def make_evidence(
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    git_diff: str = "",
    external_status: Optional[str] = None,
    ref: Optional[str] = None,
    **unknown: Any,
) -> Dict[str, Any]:
    """One evidence record — everything the judge is allowed to read.

    The judge reads ``(acceptance_predicate, [EvidenceRecord])`` and
    nothing else: no natural-language conclusion, no free-form comment
    from whoever ran the command.  A record therefore carries the exit
    code, the two output excerpts, the product-code diff, and — for an
    item that waits on something outside — the external status that was
    observed.

    Args:
        exit_code: The command's exit code.
        stdout: The command's standard output excerpt.
        stderr: The command's standard error excerpt.
        git_diff: The product-code diff observed around the command.
        external_status: The observed status of an external condition.
        ref: Where this record lives, for the progress file's
            ``last_evidence_ref``.

    Returns:
        A fresh dict with the six keys above.

    Raises:
        KeyError: A keyword this builder does not know was passed.
    """
    _reject_unknown("make_evidence", unknown)

    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "git_diff": git_diff,
        "external_status": external_status,
        "ref": ref,
    }


class FakeClock:
    """A clock the test moves instead of waiting for.

    The clock holds a start moment and an offset.  ``advance`` moves
    the offset; ``now`` reads start + offset.  Nothing here touches
    the process clock, so a case can stand seven days ahead of the
    wall clock without the wall clock, or any other test in the same
    worker, noticing.

    Args:
        start: The moment the clock starts at.  Defaults to the current
            wall-clock moment, captured once, at construction — so the
            clock is frozen from the first read.
    """

    def __init__(self, start: Optional[datetime] = None) -> None:
        self._start = start if start is not None else datetime.now(timezone.utc)
        self._offset = timedelta(0)

    def now(self) -> datetime:
        """The current moment, as this clock sees it."""
        return self._start + self._offset

    def advance(self, seconds: float) -> datetime:
        """Move this clock forward by ``seconds`` and return the new now.

        Args:
            seconds: How far to move forward.  May be fractional.

        Returns:
            The clock's new current moment.
        """
        self._offset += timedelta(seconds=seconds)
        return self.now()

    def monotonic(self) -> float:
        """Seconds this clock has been advanced by — a monotonic reading."""
        return self._offset.total_seconds()


class FakeNotifier:
    """A notification sink that records instead of sending.

    The finishing stage notifies through a real channel, which is
    exactly what a test must not do: the message would leave the
    machine, and the case would assert on something the reader cannot
    re-check.  This sink records ``{plan_id, kind, payload}`` per call
    and returns, so a case can assert the count and the content.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def notify(
        self,
        plan_id: str,
        kind: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record one notification and return the recorded call.

        Args:
            plan_id: The plan the notification is about.
            kind: What the notification is — a failure, a terminal
                state, a question.
            payload: The notification's body.

        Returns:
            The recorded call, so a caller can assert on it directly.
        """
        record = {
            "plan_id": plan_id,
            "kind": kind,
            "payload": {} if payload is None else copy.deepcopy(dict(payload)),
        }
        self.calls.append(record)
        return record

    @property
    def count(self) -> int:
        """How many notifications were recorded."""
        return len(self.calls)


def fake_notifier() -> FakeNotifier:
    """A fresh notification sink."""
    return FakeNotifier()
