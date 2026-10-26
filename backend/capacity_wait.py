"""How long to wait for a provider that just refused.

:class:`dynamic_provider_concurrency.classify_capacity_failure` already
answers *what kind* of refusal a provider made: a spent plan
(``exhausted``), a transient rate limit (``rate_limited``), a bad
request (``fatal``) or anything else (``other``). It does not answer
*how long to wait*, and one number for every kind of refusal is wrong
in both directions at once. A transient rate limit clears in minutes, so
parking the provider for half an hour throws away a healthy provider's
throughput for no reason. A hard cap — a balance that a person has to
top up — never clears on a timer at all, so waiting for it is waiting
for somebody else to notice.

This module turns the classification bucket plus CC Switch's live quota
reading into one answer: how long to wait, and whether a person has to
do something first.

Three shapes of answer
-----------------------

``window``
    The budget comes back on a timer and the reading says when. The
    wait is exactly the time left on that window, because the window
    is a fact and a constant is a guess. There is no upper bound here:
    a weekly window can be days out, and the caller owns the budget
    that bounds the whole wait.

``manual_topup``
    The budget is gone because it was *paid for* and is not coming
    back on its own. No timer fixes this, so the answer says so
    (``needs_manual_topup``) instead of pretending otherwise.

``rate_limited``
    The provider asked us to slow down. That clears in minutes.

``unknown``
    We cannot tell which of the above it is. The wait is then the
    default budget, and — this is the part that matters — it is *not*
    a guess that a person has to top something up. An unknown answer
    that claimed ``needs_manual_topup`` would page somebody for a
    rate limit.

The kind is written down
------------------------

The shape of the answer is a *fact about the run*, and the run leaves
a marker behind: the providers it is parked on, whether a person has
to act, and how long was waited out.  Those are numbers, and numbers
have to be combined before they mean anything — so every reader that
combines them has to reach the same answer, and a reader that reaches
a different one either pages somebody for a window or sits out a hard
cap waiting for a timer that does not exist.

So the kind travels with the numbers: :func:`quota_wait_kind` decides
it once, :func:`quota_wait_event_data` writes it into the marker, and
the reader hands it back as it was written.  The flag is derived from
the kind on the way out, so the two spellings of one fact cannot
contradict each other.

How the reading is judged
-------------------------

By *tier*, not by shape. The parser records
``exhausted_tier = window_exhausted_tier or balance_exhausted_tier``
(:mod:`provider_order`), so a provider whose 5h window still has room
but whose balance is empty reads as a rolling row while naming a
balance tier. The balance tier's name comes from the vendor's own
payload (``sorted(balance_tiers)[0]``) and is not necessarily called
``balance``, so the only safe question is "is this tier one of the
gated windows?" (:data:`provider_order.GATED_TIERS`).

And a ``resets_at`` on the row does not outrank the tier: a balance
is topped up by a person, not by a timer, so a stamp attached to a
balance tier describes nothing this module may wait for.

One classifier, never two
-------------------------

The bucket comes from :func:`classify_capacity_failure` and from
nowhere else. Re-deriving the verdict here would recreate exactly the
bug this codebase already fixed once — the same vendor text classified
differently at two call sites. The live reading reaches the verdict
only through that function's own ``live_exhausted`` argument, which
its documented order puts first: the meter is not a guess, so it
outranks the prose the provider sent alongside it. What this module
adds is not a second opinion about *what* happened but the missing
half of the answer: *how long*.

The module depends on the standard library plus
:mod:`dynamic_provider_concurrency` and :mod:`provider_order`, and on
nothing else — in particular not on :mod:`coding_tool` or
:mod:`agent`, which would make it part of an import cycle. That is also
why the "is this a capacity failure at all?" question stays with the
caller: :func:`coding_tool.is_capacity_error` sits behind that
import, so the caller answers it first and hands this module a
refusal already known to be worth waiting on.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from dynamic_provider_concurrency import classify_capacity_failure
from provider_order import GATED_TIERS, SHAPE_UNREADABLE, ProviderQuota

logger = logging.getLogger(__name__)

#: The budget is spent and comes back on a timer the reading named.
WAIT_WINDOW = "window"

#: The provider asked us to slow down; it clears on its own.
WAIT_RATE_LIMITED = "rate_limited"

#: The budget is spent and a person has to put it back.
WAIT_MANUAL_TOPUP = "manual_topup"

#: We could not tell what happened.
WAIT_UNKNOWN = "unknown"

#: The field an execution wait event spells its kind in.  The kind is
#: written down by the writer rather than derived by every reader: a
#: reader that has to *reverse-engineer* the kind from the numbers is
#: a reader that can get it wrong, and getting it wrong here means
#: telling an operator that a hard cap is a window that reopens by
#: itself.
WAIT_KIND_FIELD = "kind"

#: How long a transient rate limit is waited out. A rate limit clears
#: in minutes, and a provider that is asked to slow down is a healthy
#: provider — parking it for half an hour costs throughput that the
#: next attempt could have used.
RATE_LIMIT_WAIT_SEC: float = 300.0

#: How long to wait when the refusal reads as "out of budget" but the
#: reading does not corroborate a window. This is the import path's
#: fixed "no ``resets_at``" wait: without a moment to count down to,
#: the only honest length is the standard half hour.
UNCONFIRMED_WAIT_SEC: float = 1800.0

#: The default wait when there is no reading at all and therefore no
#: kind to wait for.
DEFAULT_CAPACITY_WAIT_SEC: float = 1800.0

#: The default gap between two looks at the provider while waiting.
DEFAULT_CAPACITY_WAIT_POLL_SEC: float = 30.0

#: The shortest gap between two looks. A zero-length gap would spin.
MIN_CAPACITY_WAIT_POLL_SEC: float = 1.0

#: How long a provider that needs a top-up is parked before somebody
#: is asked about it.
DEFAULT_TOPUP_PARK_SEC: float = 3600.0

#: The shortest top-up park. Shorter than this and the same person is
#: asked again before they could possibly have topped anything up.
MIN_TOPUP_PARK_SEC: float = 60.0

#: The longest we are willing to wait when we cannot tell what the
#: refusal is. ``0`` means "do not wait at all": escalate now.
ENV_WAIT_MAX_SEC = "PDT_CAPACITY_WAIT_MAX_SEC"

#: The gap between two looks at a provider while waiting for it.
ENV_WAIT_POLL_SEC = "PDT_CAPACITY_WAIT_POLL_SEC"

#: How long a provider that needs a top-up is parked.
ENV_QUOTA_TOPUP_PARK_SEC = "PDT_QUOTA_TOPUP_PARK_SEC"


@dataclass(frozen=True)
class QuotaWait:
    """One answer to "how long do we wait, and does a person have to act?".

    Attributes:
        kind: One of :data:`WAIT_WINDOW`, :data:`WAIT_RATE_LIMITED`,
            :data:`WAIT_MANUAL_TOPUP`, :data:`WAIT_UNKNOWN`.
        seconds: How long to wait. Never negative.
        needs_manual_topup: True only for :data:`WAIT_MANUAL_TOPUP` —
            that is, when the budget is gone and only a person can put
            it back. An unknown answer does not claim this: claiming it
            would page somebody for a rate limit.
    """

    kind: str
    seconds: float = 0.0
    needs_manual_topup: bool = False

    def __post_init__(self) -> None:
        if self.seconds < 0:
            raise ValueError(
                f"a wait cannot be negative: {self.seconds!r} seconds"
            )


class QuotaWaitExhausted(RuntimeError):
    """The wait is over and the provider still cannot serve the call.

    This is the answer a caller raises when it has waited out the
    budget and the provider is still refusing — the point where
    "wait and retry" has stopped being a plan.

    It derives from :class:`RuntimeError` and deliberately *not* from
    :class:`coding_tool.ApiError`: the agent catches ``ApiError`` and
    treats a capacity failure as "rotate to another provider and try
    again". An exhausted wait is the opposite of that — it is the
    final answer, and it has to reach the caller instead of being
    swallowed by that branch and retried forever.

    Attributes:
        providers: The providers that were waited on.
        needs_manual_topup: True when the answer was "somebody has to
            top this up" rather than "try again later".
        waited_sec: How long was actually waited.
        kind: Which kind of wait this was — :data:`WAIT_MANUAL_TOPUP`,
            :data:`WAIT_WINDOW` or :data:`WAIT_UNKNOWN`.  It is the
            answer in one word, and it is the *same* answer the marker
            and the log event carry, so nobody downstream has to
            derive it from the two numbers and get it wrong.
    """

    def __init__(
        self,
        providers: Optional[List[str]] = None,
        needs_manual_topup: bool = False,
        waited_sec: float = 0.0,
        kind: Optional[str] = None,
    ) -> None:
        self.providers: List[str] = list(providers or [])
        self.waited_sec = float(waited_sec)
        if kind is None:
            # No label was given: the two facts this exception already
            # carries decide it, in one place (see
            # :func:`quota_wait_kind`).
            kind = quota_wait_kind(needs_manual_topup, self.waited_sec)
        else:
            # A label WAS given, and it is the authority: the flag is
            # derived from it rather than written beside it.  Two
            # spellings of one fact are two chances to contradict each
            # other, and the contradiction is the bug this closes.
            kind = str(kind)
            needs_manual_topup = kind == WAIT_MANUAL_TOPUP
        self.kind: str = kind
        self.needs_manual_topup = bool(needs_manual_topup)
        super().__init__(self._message())

    def _message(self) -> str:
        names = ", ".join(self.providers) if self.providers else "<none>"
        need = "; manual top-up required" if self.needs_manual_topup else ""
        return (
            f"provider capacity wait exhausted after {self.waited_sec:g}s"
            f"{need}: {names}"
        )


def _env_float(name: str) -> Optional[float]:
    """The environment value for ``name`` as a float, or ``None``.

    ``None`` means "there is no usable number here": the variable is
    unset, blank, not a number, or not finite. A caller that gets
    ``None`` decides what its own default is — the three knobs do not
    share one policy, because one of them treats ``0`` as a value and
    the other two treat it as below their minimum.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def capacity_wait_budget_sec() -> float:
    """How long to wait when the refusal cannot be pinned down.

    Reads :data:`ENV_WAIT_MAX_SEC` on every call, so an operator who
    lowers it mid-run is obeyed without a restart. ``0`` is a value,
    not a gap in the settings: it means "do not wait, escalate now".
    A negative or unreadable value is illegal and falls back to
    :data:`DEFAULT_CAPACITY_WAIT_SEC` — a negative wait is not "wait a
    very long time", it is a mistake.
    """
    value = _env_float(ENV_WAIT_MAX_SEC)
    if value is None or value < 0:
        return DEFAULT_CAPACITY_WAIT_SEC
    return float(value)


def capacity_wait_poll_sec() -> float:
    """The gap between two looks at a provider while waiting for it.

    Reads :data:`ENV_WAIT_POLL_SEC` on every call. A value below
    :data:`MIN_CAPACITY_WAIT_POLL_SEC` is raised to it — a zero-length
    gap would spin — while an unreadable or negative value falls back
    to :data:`DEFAULT_CAPACITY_WAIT_POLL_SEC`.
    """
    value = _env_float(ENV_WAIT_POLL_SEC)
    if value is None or value < 0:
        return DEFAULT_CAPACITY_WAIT_POLL_SEC
    return max(float(value), MIN_CAPACITY_WAIT_POLL_SEC)


def topup_park_sec() -> float:
    """How long a provider that needs a top-up is parked.

    Reads :data:`ENV_QUOTA_TOPUP_PARK_SEC` on every call. A value
    below :data:`MIN_TOPUP_PARK_SEC` is raised to it, because the
    person who has to top the balance up cannot do it faster than
    that; an unreadable or negative value falls back to
    :data:`DEFAULT_TOPUP_PARK_SEC`.
    """
    value = _env_float(ENV_QUOTA_TOPUP_PARK_SEC)
    if value is None or value < 0:
        return DEFAULT_TOPUP_PARK_SEC
    return max(float(value), MIN_TOPUP_PARK_SEC)


def _as_seconds(value: Any) -> float:
    """A wait length as a float — an unreadable one is zero.

    A length nobody can read is not a wait that happened, so it counts
    as no wait at all.  That is the same answer the reader gives a
    payload whose ``waited_sec`` cannot be parsed, so the two do not
    disagree about a park nobody can measure.
    """
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def quota_wait_kind(needs_manual_topup: bool, waited_sec: float) -> str:
    """Which kind of wait a park was — in one word.

    A park is one of three things, and the two numbers it carries say
    which:

    * somebody has to top the balance up — the budget was *paid for*
      and is spent, and no timer brings it back;
    * a window was waited out — the budget comes back on a timer, and
      the wait was measured against that timer;
    * neither is known — the two numbers do not decide.

    The order matters, and it is the PRD's order: the flag is asked
    first, because a hard cap is a hard cap even when something was
    waited out before it ran into the wall.  The other order would
    dress a hard cap up as a window that reopens by itself, which is
    exactly what the PRD forbids.

    This is the same rule the log reader applies to a payload it did
    not write, so a park carries one word and not two.

    Args:
        needs_manual_topup: Whether a person has to act.
        waited_sec: How long was waited out.

    Returns:
        :data:`WAIT_MANUAL_TOPUP`, :data:`WAIT_WINDOW` or
        :data:`WAIT_UNKNOWN`.
    """
    if needs_manual_topup:
        return WAIT_MANUAL_TOPUP
    if _as_seconds(waited_sec) > 0:
        return WAIT_WINDOW
    return WAIT_UNKNOWN


def quota_wait_event_data(
    providers: Optional[Iterable[str]] = None,
    kind: Optional[str] = None,
    waited_sec: float = 0.0,
    at: Optional[str] = None,
    needs_manual_topup: bool = False,
) -> Dict[str, Any]:
    """The payload of an execution wait event — the kind included.

    The kind is *written down* here, by the same call that writes the
    numbers, and :func:`quota_wait_kind` is the one place that
    decides it.  A reader therefore never has to derive the kind from
    the numbers and get it wrong: the numbers are evidence, the kind
    is the answer.

    ``needs_manual_topup`` is *derived* from the kind rather than
    written beside it.  One fact, one spelling: a payload that carried
    both as independent fields could contradict itself, and the
    contradiction is the bug this closes — a hard cap described as a
    window wait (somebody told to wait for a timer that does not
    exist), or a window wait that pages somebody to buy credits.

    Args:
        providers: The providers the run is parked on.  A single name
            may be given as a string; it is one provider, not a string
            of characters.
        kind: The kind of the park.  ``None`` means "the two facts
            decide", which is what :func:`quota_wait_kind` says.
        waited_sec: How long was waited out.
        at: When the run gave up, as an ISO stamp.
        needs_manual_topup: Whether a person has to act.  Only used
            when ``kind`` is ``None`` — a kind that was given is the
            authority and decides the flag.

    Returns:
        The payload, ready to be written into the plan's marker.
    """
    if kind is None:
        kind = quota_wait_kind(needs_manual_topup, waited_sec)
    else:
        kind = str(kind)
    if isinstance(providers, str):
        providers = [providers]
    return {
        WAIT_KIND_FIELD: kind,
        "providers": [str(name) for name in (providers or [])],
        "needs_manual_topup": kind == WAIT_MANUAL_TOPUP,
        "waited_sec": _as_seconds(waited_sec),
        "at": str(at or ""),
    }


# ---------------------------------------------------------------------------
# Reading the marker back
# ---------------------------------------------------------------------------
#
# The marker this module writes is read back by the phase rule: a plan
# whose run parked for capacity says so through its ``tasks.json``, and
# the rule that decides "is this plan waiting for quota" has to be able
# to tell a capacity fact from any other "why did this stop".
#
# So the reading lives HERE, beside the writing, and it is the only
# reader: ``reason_detail`` is a free-form slot, and two readers that
# disagree about what counts as a capacity marker produce two answers
# for one plan — one of them claiming the plan is waiting for capacity
# when it is not.

#: The fields a capacity marker carries.  A detail that parses into an
#: object with NONE of them is not a capacity fact: it is some other
#: reason the run stopped, and the slot says so.
CAPACITY_MARKER_KEYS = frozenset({
    "providers", "needs_manual_topup", "waited_sec", "at",
})


def parse_capacity_marker(tasks_payload: Any) -> Optional[Dict[str, Any]]:
    """The capacity marker in this payload — or ``None``.

    The single entry point for reading the marker.  It answers one
    question: **is this a capacity fact?**

    ``None`` means "no marker", and it covers every shape that is not
    one: no payload at all, no ``reason_detail``, a detail that is not
    JSON, a detail that is not an object, an empty object, and an object
    that carries something else entirely.

    Args:
        tasks_payload: The plan's parsed ``tasks.json`` — the whole
            document, because the marker lives in its TOP level
            (``stop_reason`` / ``reason_detail``), where the executor
            writes it.  A marker that has already been parsed is
            accepted too, so a caller that holds one does not have to
            pretend it is a payload.

    Returns:
        The parsed marker, or ``None`` when there is none.
    """
    if not isinstance(tasks_payload, Mapping):
        return None
    detail: Any = tasks_payload.get("reason_detail")
    if isinstance(detail, str):
        if not detail.strip():
            return None
        try:
            detail = json.loads(detail)
        except (ValueError, TypeError):
            return None
    if isinstance(detail, Mapping) and _carries_marker_fields(detail):
        return dict(detail)
    # The payload may BE the marker (a caller that already parsed it).
    # A payload that carries a ``reason_detail`` is never read that
    # way: its detail is what decides, and a detail that is not a
    # capacity fact is not one however the payload looks.
    if "reason_detail" not in tasks_payload and _carries_marker_fields(
        tasks_payload,
    ):
        return dict(tasks_payload)
    return None


def _carries_marker_fields(detail: Mapping[str, Any]) -> bool:
    """Does this mapping carry any of the capacity marker's fields?"""
    return any(key in detail for key in CAPACITY_MARKER_KEYS)


def _unknown(seconds: float) -> QuotaWait:
    """The answer for a refusal this module cannot pin down."""
    return QuotaWait(WAIT_UNKNOWN, float(seconds), False)


def _exhausted_wait(quota: Optional[ProviderQuota]) -> QuotaWait:
    """The wait for a refusal that reads as "out of budget".

    The reading decides which of the two exhausted answers this is,
    and it is judged by tier:

    * a tier in :data:`provider_order.GATED_TIERS` is a window — it
      comes back on a timer, and the reading says when;
    * any other tier is a balance that a person has to top up, and no
      timer will do it;
    * no tier at all says nothing about *what* ran out, so it is not
      called a hard cap.

    A reading that is missing, unreadable or not exhausted is not a
    confirmation, and neither is a window with no usable reset time.
    None of those proves a hard cap, so none of them claims one.
    """
    if quota is None:
        # No reading at all: the generic budget, not a hard cap.
        return _unknown(capacity_wait_budget_sec())
    if quota.unreadable or quota.shape == SHAPE_UNREADABLE:
        return _unknown(capacity_wait_budget_sec())
    if not quota.exhausted:
        # The refusal says the budget is gone; the meter says it is
        # not. The meter wins, but it does not name a window to wait
        # for, so there is nothing to wait *out*.
        return _unknown(UNCONFIRMED_WAIT_SEC)

    tier = quota.exhausted_tier
    if tier is None:
        # Exhausted, but with nothing named. A hard cap cannot be
        # proven from this, and guessing it would page somebody for a
        # five-hour window.
        return _unknown(UNCONFIRMED_WAIT_SEC)

    if tier not in GATED_TIERS:
        # A balance that was spent. ``resets_at`` does not outrank
        # the tier: a balance is topped up by a person, not by a timer.
        return QuotaWait(WAIT_MANUAL_TOPUP, topup_park_sec(), True)

    resets_at = quota.exhausted_resets_at
    if resets_at is None:
        return _unknown(UNCONFIRMED_WAIT_SEC)
    if resets_at.tzinfo is None:
        resets_at = resets_at.replace(tzinfo=timezone.utc)
    remaining = (resets_at - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        # A stamp in the past describes a window that has already
        # rolled; it cannot be waited out.
        return _unknown(UNCONFIRMED_WAIT_SEC)
    return QuotaWait(WAIT_WINDOW, float(round(remaining)), False)


def resolve_quota_wait(
    provider: str,
    text: str,
    status: str,
    quota: Optional[ProviderQuota] = None,
) -> QuotaWait:
    """How long to wait for ``provider`` after it refused with ``text``.

    ``text`` and ``status`` are the provider's own refusal, and
    ``quota`` is CC Switch's live reading of that provider's budget
    (``None`` when there is no reading). The reading is passed in
    rather than looked up here, so this function does no I/O and can
    be asked about a provider that is not answering.

    The bucket comes from
    :func:`dynamic_provider_concurrency.classify_capacity_failure` and
    from nowhere else, with the live reading entering through that
    function's ``live_exhausted`` argument. A rate limit waits
    :data:`RATE_LIMIT_WAIT_SEC`; an exhausted budget is judged by the
    reading's tier (:func:`_exhausted_wait`); anything this module
    cannot pin down waits the default budget.

    Attributes of the answer:
        - ``window``: the wait is the time left on the window, with no
          upper bound of its own — the caller owns the budget.
        - ``manual_topup``: a person has to top the balance up.
        - ``rate_limited``: wait a few minutes.
        - ``unknown``: wait the default budget; a person is *not*
          assumed to be needed.

    Args:
        provider: The provider's name, carried for the caller's
            bookkeeping and for the log line.
        text: The provider's error text.
        status: The provider's HTTP status, as a string.
        quota: The live reading, or ``None``.

    Returns:
        The wait answer. Never raises for a capacity refusal: a
        refusal this module cannot read is answered with
        :data:`WAIT_UNKNOWN`, not with an exception.
    """
    live_exhausted = bool(quota is not None and quota.exhausted)
    bucket = classify_capacity_failure(
        text, status, live_exhausted=live_exhausted
    )
    logger.debug(
        "capacity wait resolved",
        extra={
            "provider": provider,
            "bucket": bucket,
            "live_exhausted": live_exhausted,
        },
    )

    if bucket == "rate_limited":
        return QuotaWait(WAIT_RATE_LIMITED, RATE_LIMIT_WAIT_SEC, False)
    if bucket == "exhausted":
        return _exhausted_wait(quota)
    # ``fatal`` is not a capacity refusal at all and ``other`` is the
    # bucket that keeps its old behaviour (rotate to the next
    # provider); neither is a reason to wait. The generic budget is
    # the honest answer for "this module has nothing to say here".
    return _unknown(capacity_wait_budget_sec())


__all__ = [
    "DEFAULT_CAPACITY_WAIT_POLL_SEC",
    "DEFAULT_CAPACITY_WAIT_SEC",
    "DEFAULT_TOPUP_PARK_SEC",
    "ENV_QUOTA_TOPUP_PARK_SEC",
    "ENV_WAIT_MAX_SEC",
    "ENV_WAIT_POLL_SEC",
    "MIN_CAPACITY_WAIT_POLL_SEC",
    "MIN_TOPUP_PARK_SEC",
    "RATE_LIMIT_WAIT_SEC",
    "UNCONFIRMED_WAIT_SEC",
    "WAIT_KIND_FIELD",
    "WAIT_MANUAL_TOPUP",
    "WAIT_RATE_LIMITED",
    "WAIT_UNKNOWN",
    "WAIT_WINDOW",
    "QuotaWait",
    "QuotaWaitExhausted",
    "capacity_wait_budget_sec",
    "capacity_wait_poll_sec",
    "quota_wait_event_data",
    "quota_wait_kind",
    "resolve_quota_wait",
    "topup_park_sec",
]
