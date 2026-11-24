"""The unified waiting lifecycle — one state model for both kinds of wait.

What this module owns
---------------------
The finishing stage (收尾阶段) can wait for a **human** (a merge needs an
approval) or for **something outside** (a CI run on another platform).
The two differ in what they wait *for* and are identical in what they
need from the operator: a deadline, a way to see how much time is left,
a way to push the deadline out, and a way to give up.  So they share
**one** state model (architecture decision point 5)::

    {item_id, wait_type, question, deadline, extensions[]}

plus the status that says where in its life the wait is.  Implementing
them separately would mean two sets of semantics for the same operator
gesture, and the second one would drift.

The bridge to the existing HITL machinery
------------------------------------------
A wait for a human is not a wait the finishing stage can keep to
itself: the question is asked in one process and answered in another,
so it has to be written down somewhere both can reach.  That place
already exists — the ``plan_decisions`` table, the ``session_send``
delivery and the ``hitl_wait`` heartbeat — and this module **uses** it
rather than growing a second one (architecture decision point 5):

* :meth:`WaitRegistry.create_human_wait` writes one ``plan_decisions``
  row through the existing store, delivers the question through the
  channel the caller injects, and registers the heartbeat that keeps
  the watchdog from reading a waiting plan as a dead one;
* :meth:`WaitRegistry.create_approval_request` does the same for the
  three actions the executor refuses to run on its own — a merge into
  ``main``, a destructive command, a command that costs money.  The
  row is asked and nothing more: the system never answers it, and
  never answers a batch of them either;
* :meth:`WaitRegistry.create_external_wait` writes **no** row at all.
  Nothing outside is asked a question, so there is nobody to ask: what
  it registers is the *recheck*;
* :meth:`WaitRegistry.resolve_human_wait` settles a wait with the
  answer, through the existing compare-and-set.

The production wiring
---------------------
A registry is only worth what it is wired to: a registry without a
store cannot write a question down, and a registry without a channel
cannot deliver one.  :func:`production_registry` builds the registry
the finishing stage uses — the existing HITL store on the state
database, and the plan's own ``hitl_config.json`` binding delivered by
the existing hook dispatcher — so the stage asks through the machinery
that already exists instead of through a second one.

The drive
---------
A wait is not a record somebody else has to come and settle.
:meth:`WaitRegistry.drive` settles it **where it waits**: it reads the
answer a human gave (read-only — the system never answers on a
human's behalf), rechecks an external condition at the rhythm, and
fails the wait when its deadline passes.  The drive blocks in the
thread that is already running the stage: it starts no thread and no
timer, and it hands the wait back when its window is over.

What this module does NOT own
-----------------------------
**The HITL semantics.**  The ``auto``/``strict`` vocabularies, their
timeouts and their sweep belong to the existing HITL, and this module
adds no member to them and no branch to them.  The deadline of a
finishing-stage wait is the one in this module's own wait record, so
the HITL row carries no deadline of its own — there is nothing of ours
for the existing sweep to settle.

**The channel.**  The delivery is a callable the caller injects: in
production the plan's own ``hitl_config.json`` binding, carried by the
existing ``session_send`` hook; in a test, a sink that records.  This
module grows no channel of its own.

Why the deadline is kept in the progress file
---------------------------------------------
The architecture puts the deadline in the finishing stage's own
progress record (``postprocess_progress.json``), under the item's
``wait`` field — **not** in the existing HITL timeout configuration.
The HITL ``auto``/``strict`` semantics are used elsewhere in this
backend and must not gain a branch because the finishing stage needed
a longer clock; a week-long wait outlives every process that might
hold it, so it has to survive in a file.

Why the clock is injected
-------------------------
A week is not a thing a test can wait for, and patching
``datetime.now`` globally leaks into every other case in the same
worker — the failure then shows up somewhere else, minutes later, in
a case nobody was looking at.  So the registry takes its clock as an
argument and reads the moment from it, and a case can hand it a clock
that jumps a week in one step.

The lifecycle
-------------
::

    create ──► pending ──answer──► answered
                   │
                   ├──extend──► pending      (deadline pushed out, audited)
                   │
                   ├──terminate► terminated (final; a late answer is refused)
                   │
                   └──expire_due─► failed   (the deadline passed)

Only a **pending** wait can be extended, terminated or answered.
Anything else is a conflict, not a silent no-op: an operator who
extends a wait that was already answered must be told, because the
deadline they moved is a deadline nobody is watching any more.
"""

from __future__ import annotations

import copy
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from config_paths import resolve_state_db_path
from hitl.models import Decision, DecisionRequest
from hitl.service import EVENT_DECISION_CREATED
from hitl.store import HitlStore
from hooks.dispatcher import dispatch
from postprocess.executor import NeedsApproval
from postprocess.pr_ref import extract_pr_ref
from postprocess.progress import mark


logger = logging.getLogger(__name__)


__all__ = [
    "APPROVAL_OPTIONS",
    "DEFAULT_WAIT_SECONDS",
    "HITL_ANSWERED_STATUS",
    "HITL_STRICT_MODE",
    "ITEM_STATUS_BY_WAIT_STATUS",
    "POSTPROCESS_APPROVAL_SOURCE",
    "POSTPROCESS_WAIT_SOURCE",
    "WAIT_OPTIONS",
    "WAIT_POLL_SECONDS",
    "WAIT_SETTLE_SECONDS",
    "WAIT_STATUSES",
    "WAIT_TYPES",
    "WaitConflict",
    "WaitError",
    "WaitRegistry",
    "production_registry",
]


#: The default wait limit — one week, in seconds.  The architecture
#: fixes this ceiling for the finishing stage: a wait that nobody
#: answers within a week is not a wait any more, it is a failure.
DEFAULT_WAIT_SECONDS: int = 604800

#: The rhythm at which a wait is driven — the same two-second scale the
#: existing HITL waiter polls at.  The drive does not invent a rhythm of
#: its own: it reuses the one the backend already keeps.
WAIT_POLL_SECONDS: float = 2.0

#: How long one drive waits before it hands the wait back to the stage.
#: The stage is not held hostage by a wait it cannot settle: it asks,
#: it waits for the rhythm a few times, and then it parks the item and
#: lets the plan be resumed.
WAIT_SETTLE_SECONDS: float = 6.0

#: The two kinds of wait that share this lifecycle.  They are told apart
#: by what they wait *for*; everything the operator does to them —
#: reading the remaining time, extending, terminating — is the same
#: operation on the same record.
WAIT_TYPES: Tuple[str, ...] = ("human", "external")

#: The closed vocabulary of wait statuses.
WAIT_STATUSES: Tuple[str, ...] = ("pending", "answered", "terminated", "failed")

#: The ``source`` recorded on a decision the finishing stage asked a
#: plain question through.  It says who asked, so a reader of the
#: audit mirror can tell a finishing-stage question from the
#: verification round's own.
POSTPROCESS_WAIT_SOURCE = "postprocess_wait"

#: The ``source`` recorded on a decision that asks a human to approve
#: an action the executor refused to run on its own.
POSTPROCESS_APPROVAL_SOURCE = "postprocess_approval"

#: The ``source`` recorded on a wait for something outside.  Such a
#: wait asks nobody anything, so it has no approval behind it — this
#: says what it waits for instead.
EXTERNAL_WAIT_SOURCE = "external"

#: The sources the finishing stage asks its questions under.  A row is
#: looked up by its source as well as by its item, so a question of the
#: verification round's own is never mistaken for one of ours.
_BRIDGE_SOURCES = frozenset({POSTPROCESS_WAIT_SOURCE, POSTPROCESS_APPROVAL_SOURCE})

#: The keys the shared state model of decision point 5 is made of — the
#: six keys **both** kinds of wait carry.  A wait *record* carries these
#: and the source the wait came from and the pull request it is about
#: (:meth:`_Wait.to_record`); the model itself is the part every kind
#: of wait has, and it is what a caller that only wants to know "what
#: is this wait" is handed.
SHARED_STATE_KEYS: Tuple[str, ...] = (
    "item_id",
    "wait_type",
    "question",
    "deadline",
    "extensions",
    "status",
)

#: The answers a wait offers.  The answer itself is stored as it is
#: given — the option set is what the question advertises, not what
#: the store polices.
WAIT_OPTIONS: Tuple[str, ...] = ("proceed", "stop")

#: The answers an approval request offers.
APPROVAL_OPTIONS: Tuple[str, ...] = ("approve", "reject")

#: The status a HITL decision carries once a human answered it.  It is
#: spelled out here as a plain word on purpose: the finishing stage
#: *reads* the existing HITL's statuses, and never adds a member to
#: them.
HITL_ANSWERED_STATUS = "answered"

#: The mode the finishing stage's questions carry.  ``strict`` is the
#: existing HITL's own word for "there is no substitute for a human":
#: an ``auto`` question is a question with a default answer, and a
#: default answer is an answer the system may give on the human's
#: behalf.  A question the operator has to answer is therefore asked
#: as a strict one — and the existing sweep leaves strict rows alone,
#: which is what keeps the finishing stage out of the ``auto``/``strict``
#: semantics it must not change.
HITL_STRICT_MODE = "strict"

#: The wait is live: it can still be extended, answered or terminated.
PENDING: str = "pending"

#: The wait was answered — somebody said yes (or no) and the item is
#: ready to be executed again.
ANSWERED: str = "answered"

#: The wait was given up on.  This is final: a late answer is refused
#: and the wait is never expired.
TERMINATED: str = "terminated"

#: The deadline passed without an answer.
FAILED: str = "failed"

#: The item status each wait status corresponds to, in the progress
#: file.  A live wait makes its item *waiting*; a settled wait leaves
#: the item where the finishing stage's own lifecycle puts it — a
#: wait that ended without the item succeeding is a **failed** item,
#: which is the status the retry round picks up.
ITEM_STATUS_BY_WAIT_STATUS: Dict[str, str] = {
    PENDING: "waiting",
    ANSWERED: "pending",
    TERMINATED: "failed",
    FAILED: "failed",
}


class WaitError(ValueError):
    """The wait could not be read or created as asked.

    Raised for an unknown item, a malformed item, an extension that is
    not a positive number of seconds, and an operator that is not named.

    A subclass of :class:`ValueError` — the same choice
    :class:`postprocess.progress.ProgressError` makes — so a caller
    that already treats bad input as a value error keeps working,
    while a caller that wants to tell "this argument is wrong" from
    "this wait is in the wrong state" can catch
    :class:`WaitConflict` instead.
    """


class WaitConflict(WaitError):
    """The wait is not in a state where this operation is legal.

    Raised when an operation that only a live wait accepts — extending,
    terminating, answering — is attempted on a wait that has already
    been answered, terminated or expired.  The conflict is reported
    rather than ignored: an operator who extends a wait that was
    already answered must be told, because the deadline they moved is
    a deadline nobody is watching any more.

    The conflict carries the status the API layer reports it as
    (:attr:`status_code`): a request that asks for something the
    wait is not in a state to give is a conflict — not a bad request,
    and never a success.
    """

    #: The HTTP status a conflict is reported as.  Every conflict this
    #: module raises is one thing — "the resource is not in a state
    #: where this operation is legal" — so they all carry the same
    #: status.
    status_code: int = 409


class _SystemClock:
    """The real clock — the one used when the caller injects none."""

    def now(self) -> datetime:
        """The current moment, in UTC."""
        return datetime.now(timezone.utc)


class _Wait:
    """One wait, held in the shape the registry keeps it in.

    The public record (:meth:`to_record`) is exactly the shared state
    model; the fields this class keeps *besides* it — the creation
    moment, the answer, the termination — are private to the registry
    and never leak into the record that goes to the progress file.

    Args:
        item_id: The requirement item this wait belongs to.
        wait_type: ``human`` or ``external``.
        question: The question put to whoever answers the wait.
        created_at: When the wait was created.
        deadline: When the wait is due.
        source: Where the wait came from — the approval's own source
            for a request a human has to approve, the external wait's
            own source for a wait for something outside, and nothing at
            all for an ordinary question.
        pr_number: The pull request the wait is about, when it is about
            one.
        pr_url: The link of that pull request, when the command named
            the repository it lives in.
    """

    __slots__ = (
        "item_id",
        "wait_type",
        "question",
        "created_at",
        "deadline",
        "extensions",
        "status",
        "answer",
        "termination",
        "source",
        "pr_number",
        "pr_url",
    )

    def __init__(
        self,
        item_id: str,
        wait_type: str,
        question: str,
        created_at: datetime,
        deadline: datetime,
        source: str = "",
        pr_number: Optional[int] = None,
        pr_url: Optional[str] = None,
    ) -> None:
        self.item_id = item_id
        self.wait_type = wait_type
        self.question = question
        self.created_at = created_at
        self.deadline = deadline
        self.extensions: List[Dict[str, str]] = []
        self.status = PENDING
        self.answer: Any = None
        self.termination: Optional[Dict[str, str]] = None
        self.source = source
        self.pr_number = pr_number
        self.pr_url = pr_url

    def to_record(self) -> Dict[str, Any]:
        """This wait as the shared record — a copy, never the live state.

        The record carries the shared state model — the item, the kind
        of wait, the question, the deadline, the extension audit and
        the status — **and** where the wait came from and the pull
        request it is about (architecture decision points R5 and R6).
        The answer is **not** part of it: the answer is the registry's
        own state, and it never lands in the record that goes to the
        progress file.

        Returns:
            A fresh dict carrying exactly those nine keys.
        """
        return {
            "item_id": self.item_id,
            "wait_type": self.wait_type,
            "question": self.question,
            "deadline": self.deadline.isoformat(),
            "extensions": copy.deepcopy(self.extensions),
            "status": self.status,
            "source": self.source,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
        }

    def to_shared_state(self) -> Dict[str, Any]:
        """The shared state model alone — the six keys both kinds share.

        This is what :meth:`WaitRegistry.create_wait` answers with: the
        part of the record that says what the wait **is** — its item,
        its kind, its question, its deadline, its extension audit and
        its status.  The rest of the record — the source the wait came
        from and the pull request it is about — is read back with
        :meth:`WaitRegistry.get`.
        """
        record = self.to_record()
        return {key: record[key] for key in SHARED_STATE_KEYS}


class WaitRegistry:
    """The waiting lifecycle of one plan's finishing stage.

    The registry holds the waits of one plan: it creates them, tells
    how much time is left, pushes a deadline out, gives up on a wait
    and fails the ones that ran out of time.

    A wait for a human is bridged onto the existing HITL machinery: the
    store it writes the question through, and the channel it delivers
    the question by, are both given to the registry rather than built
    inside it.  That is what makes it testable without a database
    nobody owns and without a recipient.

    Args:
        plan_id: The plan whose waits these are.  The wait records are
            persisted under this plan's own progress file.
        clock: The clock the registry reads the moment from.  It must
            offer ``now()`` returning a :class:`datetime`.  Defaults to
            the wall clock; a test injects a clock it can move.
        store: The HITL store the questions are written through — the
            one that owns the ``plan_decisions`` table.  A wait for a
            human needs one: a question that cannot be stored is a
            question nobody can answer.
        deliver: The channel a question is delivered by.  It is called
            with the event the outlet carries — in production the
            plan's own ``hitl_config.json`` binding, delivered by the
            existing ``session_send`` hook; in a test, a sink that
            records.  Without one the question is stored and not
            delivered.
    """

    def __init__(
        self,
        plan_id: str,
        *,
        clock: Optional[Any] = None,
        store: Optional[Any] = None,
        deliver: Optional[Callable[[Mapping[str, Any]], Any]] = None,
    ) -> None:
        self.plan_id = plan_id
        self._clock = clock if clock is not None else _SystemClock()
        self._store = store
        self._deliver = deliver
        self._waits: Dict[str, _Wait] = {}
        self._decision_ids: Dict[str, str] = {}
        self._checks: Dict[str, Callable[[], Any]] = {}
        #: What the last recheck of each external wait said — kept so the
        #: drive can report it without running the recheck again.
        self._last_checks: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def get(self, item_id: str) -> Dict[str, Any]:
        """One wait's record.

        Args:
            item_id: The item the wait belongs to.

        Returns:
            The wait record — a copy, so the caller cannot reach into
            the registry's own state.

        Raises:
            WaitError: When no wait was created for that item.
        """
        return self._require(item_id).to_record()

    def waits(self) -> List[Dict[str, Any]]:
        """Every wait of this plan, as records.

        Returns:
            The records of all waits, in the order they were created.
        """
        return [wait.to_record() for wait in self._waits.values()]

    def decision_id_of(self, item_id: str) -> Optional[str]:
        """The id of the decision row this item's question was stored as.

        The id is remembered when the question is created.  A wait
        outlives the process that created it — a week-long wait does —
        so the row is looked up in the store when the registry does not
        remember it: the row carries the item's id, and its ``source``
        says the finishing stage is who asked.

        Read-only: this accessor never writes, and it never answers a
        question.  What the row carries is read with
        :meth:`decision_by_id`.

        Args:
            item_id: The item the wait belongs to.

        Returns:
            The id of the decision row, or ``None`` when this item never
            had a question stored.
        """
        remembered = self._decision_ids.get(item_id)
        if remembered is not None:
            return remembered

        try:
            decision = self._decision_of(item_id)
        except Exception as exc:  # noqa: BLE001 — 读不到就是没有
            logger.warning(
                "postprocess_wait_decision_unreadable: item=%s: %s", item_id, exc
            )
            return None
        if decision is None:
            return None
        return str(decision.decision_id)

    def decision_by_id(self, decision_id: str) -> Optional[Decision]:
        """The decision row with this id — read-only.

        The row is what a human answered: it carries the question, the
        status the answer left behind and the answer itself.  It is
        read here and nowhere else, so the answer stays where it was
        written — the row in the ``plan_decisions`` table — and never
        lands in a wait record.

        Args:
            decision_id: The id of the row.

        Returns:
            The decision, or ``None`` when there is no such row — no
            store behind the registry, an unknown id, or a store that
            cannot be read.
        """
        if self._store is None or not decision_id:
            return None
        try:
            return self._store.get(decision_id)
        except Exception as exc:  # noqa: BLE001 — 读不到就是没有答复
            logger.warning(
                "postprocess_wait_decision_unreadable: decision=%s: %s",
                decision_id,
                exc,
            )
            return None

    def remaining_seconds(self, item_id: str) -> int:
        """How much time is left on a wait.

        Args:
            item_id: The item the wait belongs to.

        Returns:
            The whole seconds between now and the deadline — never
            negative: a wait that ran out of time has *no* time left,
            not "minus one second".  A wait that was extended past
            what is left reports the extended remainder, because the
            deadline is what the operator last agreed to.
        """
        wait = self._require(item_id)
        remaining = (wait.deadline - self._now()).total_seconds()
        return max(0, int(remaining))

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def create_wait(
        self,
        item: Mapping[str, Any],
        question: str = "",
        *,
        wait_type: Optional[str] = None,
        seconds: Optional[int] = None,
        record_source: str = "",
        pr_number: Optional[int] = None,
        pr_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create the wait of one requirement item.

        The deadline is one week after the moment of creation, unless
        the caller states another limit for this wait.

        Args:
            item: The requirement item the wait belongs to.  Its ``id``
                names the wait; its ``wait.type`` says what the item
                waits for.
            question: The question put to whoever answers the wait.
            wait_type: Overrides the kind of wait the item declares.
            seconds: The limit of this wait, in seconds.  Defaults to
                :data:`DEFAULT_WAIT_SECONDS`.
            record_source: The source the record carries — it says
                where the wait came from.  A wait for something
                outside says so itself when the caller states nothing.
            pr_number: The pull request the wait is about, when it is
                about one.
            pr_url: The link of that pull request.

        Returns:
            The new wait's shared state — the six keys of decision
            point 5.  The record that also carries the source and the
            pull request is read back with :meth:`get`.

        Raises:
            WaitError: When the item has no usable id, the question is
                not a string, the kind of wait is outside
                :data:`WAIT_TYPES`, or the limit is not a positive
                number of seconds.
            WaitConflict: When this item already has a wait.
        """
        item_id = self._item_id_of(item)
        if not isinstance(question, str):
            raise WaitError(f"等待 {item_id} 的问题必须是字符串, got {question!r}")

        if item_id in self._waits:
            raise WaitConflict(f"等待条目 {item_id} 已经有等待，不能再建一个")

        kind = self._wait_type_of(item, wait_type)
        limit = DEFAULT_WAIT_SECONDS if seconds is None else self._checked_seconds(
            seconds
        )

        source = record_source or (EXTERNAL_WAIT_SOURCE if kind == "external" else "")
        created_at = self._now()
        deadline = created_at + timedelta(seconds=limit)
        wait = _Wait(
            item_id=item_id,
            wait_type=kind,
            question=question,
            created_at=created_at,
            deadline=deadline,
            source=source,
            pr_number=pr_number,
            pr_url=pr_url,
        )
        self._waits[item_id] = wait
        self._persist(wait)
        return wait.to_shared_state()

    def extend_deadline(
        self,
        item_id: str,
        seconds: int,
        *,
        operator: str,
    ) -> Dict[str, Any]:
        """Push a wait's deadline out, and record who did it.

        Args:
            item_id: The item the wait belongs to.
            seconds: How much longer the wait may take.  A positive
                number of seconds.
            operator: Who asked for the extension — the audit entry
                names them.

        Returns:
            The wait record, carrying one more extension entry.

        Raises:
            WaitError: When the wait is unknown, or the number of
                seconds is not a positive integer.
            WaitConflict: When the wait is not pending any more.
        """
        wait = self._require(item_id)
        if wait.status != PENDING:
            raise WaitConflict(
                f"等待条目 {item_id} 当前状态为 {wait.status}，只有 pending 可以延长"
            )

        amount = self._checked_seconds(seconds)
        who = self._checked_operator(operator, item_id)

        previous = wait.deadline
        wait.deadline = previous + timedelta(seconds=amount)
        wait.extensions.append(
            {
                "operator": who,
                "when": self._now().isoformat(),
                "from": previous.isoformat(),
                "to": wait.deadline.isoformat(),
            }
        )
        self._persist(wait)
        return wait.to_record()

    def terminate(self, item_id: str, *, operator: str) -> Dict[str, Any]:
        """Give up on a wait.

        A terminated wait is final: it is never expired, and an answer
        that arrives afterwards is refused.

        When the wait has a question stored in the HITL table, the
        question is settled with it.  A wait that was given up on but
        whose question stayed open would let an answer through the
        existing HITL service — a late answer to a closed question
        would land, and land as if it were asked for.

        Args:
            item_id: The item the wait belongs to.
            operator: Who gave up on it.

        Returns:
            The wait record, now terminated.

        Raises:
            WaitError: When the wait is unknown, or the operator is not
                named.
            WaitConflict: When the wait is not pending any more.
        """
        wait = self._require(item_id)
        if wait.status != PENDING:
            raise WaitConflict(
                f"等待条目 {item_id} 当前状态为 {wait.status}，只有 pending 可以终止"
            )

        who = self._checked_operator(operator, item_id)
        self._settle_decision(item_id, note=f"等待条目 {item_id} 已被终止")
        wait.termination = {"operator": who, "when": self._now().isoformat()}
        wait.status = TERMINATED
        self._persist(wait)
        return wait.to_record()

    def answer(self, item_id: str, answer: Any) -> Dict[str, Any]:
        """Answer a wait.

        When the wait has a question stored in the HITL table, the
        answer is written there too — through the existing
        compare-and-set, so two answers racing produce one stored
        answer and the loser is told it lost.

        Args:
            item_id: The item the wait belongs to.
            answer: The answer that was given.

        Returns:
            The wait record, now answered.

        Raises:
            WaitError: When the wait is unknown.
            WaitConflict: When the wait is not pending any more — a
                late answer to a terminated or expired wait is refused
                rather than reviving it.
        """
        wait = self._require(item_id)
        if wait.status != PENDING:
            raise WaitConflict(
                f"等待条目 {item_id} 当前状态为 {wait.status}，不能再提交答案"
            )

        return self._apply_answer(wait, answer).to_record()

    def expire_due(self) -> List[Dict[str, Any]]:
        """Fail every wait whose deadline has passed.

        Only a pending wait can expire.  An answered, terminated or
        already failed wait is left exactly as it is — a wait the
        operator already dealt with does not fail again just because
        the clock kept running.

        Returns:
            The records of the waits that expired, in the order they
            were created.
        """
        now = self._now()
        expired: List[Dict[str, Any]] = []
        for wait in self._waits.values():
            if wait.status != PENDING:
                continue
            if wait.deadline > now:
                continue
            wait.status = FAILED
            self._persist(wait)
            expired.append(wait.to_record())
        return expired

    # ------------------------------------------------------------------
    # The HITL bridge
    # ------------------------------------------------------------------

    def create_human_wait(
        self,
        item: Mapping[str, Any],
        question: str,
        *,
        source: str = POSTPROCESS_WAIT_SOURCE,
        options: Tuple[str, ...] = WAIT_OPTIONS,
        note: str = "",
        record_source: str = "",
        pr_number: Optional[int] = None,
        pr_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Ask a human, on behalf of one requirement item, and wait.

        The question is written down **before** anybody is told about
        it: a question that exists but was never announced is a fact
        the audit mirror can show, while a question that was announced
        but never stored is a question a human is answering into the
        void.  So the row goes in first and the delivery second.

        The row carries **no** default answer and **no** deadline of
        its own.  A question with a default answer is a question the
        system may answer on the human's behalf, and a deadline baked
        into the row would be a second, staler copy of the deadline
        this module keeps — the one that can be pushed out.  The wait's
        own deadline is the one in the wait record.  The row is a
        *strict* one — the existing HITL's own word for "there is no
        substitute for a human" — so the existing sweep has nothing of
        ours to settle.

        Args:
            item: The requirement item the wait belongs to.
            question: The question put to the human, in the words shown
                to them.
            source: The ``source`` the question is asked under.  It
                says who asked, so a reader of the audit mirror can tell
                a plain question from an approval request.
            options: The answers the question offers.
            note: A remark stored with the question — the command an
                approval request is about, for instance.
            record_source: The source the **wait record** carries.  It
                is the row's own business: it says where the wait came
                from, so a reader of the waiting rows can tell an
                approval from an ordinary question.
            pr_number: The pull request the question is about, when it
                is about one.
            pr_url: The link of that pull request.

        Returns:
            The wait record, carrying the id of the decision row the
            question was stored as.

        Raises:
            WaitError: When no HITL store was given — a question that
                cannot be stored is a question nobody can answer — or
                when the question is not a non-empty string.
            WaitConflict: When this item already waits.
        """
        return self._create_decided_wait(
            item,
            question,
            source=source,
            options=options,
            note=note,
            record_source=record_source,
            pr_number=pr_number,
            pr_url=pr_url,
        )

    def create_approval_request(
        self,
        item: Mapping[str, Any],
        question: Any,
        *,
        pr_number: Optional[int] = None,
        pr_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Turn an action the executor refused to run into a question.

        A merge into ``main``, a destructive command and a command that
        costs money are not illegal — they are legal commands that need
        a human to say yes (architecture decision point 4).  So they
        become a *pending row* and nothing else: the executor does not
        run them, the system does not answer them, and it does not
        answer a batch of them either.

        The request **is** a question a human is asked, so it goes
        through the one method that asks: the row, the heartbeat and
        the delivery are the ones a plain question gets, with the
        approval's own source, options and note.

        The record says where the wait came from — an approval request
        is not an ordinary question — and names the pull request it is
        about when the command names one (architecture decision points
        R5 and R6).

        Args:
            item: The requirement item the request belongs to.
            question: Either the question itself, or the
                :class:`~postprocess.executor.NeedsApproval` the
                executor produced — its question is used verbatim and
                the argv it names is recorded with the question.
            pr_number: The pull request the request is about.  When the
                caller states nothing, it is read off the argv the
                executor refused to run.
            pr_url: The link of that pull request.  It is read off the
                same argv when the caller states nothing, and it stays
                empty when the argv names no repository.

        Returns:
            The wait record, carrying the id of the decision row the
            request was stored as.

        Raises:
            WaitError: When no HITL store was given, or the question is
                not a non-empty string.
            WaitConflict: When this item already waits.
        """
        note = ""
        if isinstance(question, NeedsApproval):
            note = "待批准的命令: " + " ".join(str(part) for part in question.command)
            pr_number, pr_url = _pull_request_of(
                question.command, pr_number=pr_number, pr_url=pr_url
            )
            question = question.question
        return self.create_human_wait(
            item,
            question,
            source=POSTPROCESS_APPROVAL_SOURCE,
            options=APPROVAL_OPTIONS,
            note=note,
            record_source=POSTPROCESS_APPROVAL_SOURCE,
            pr_number=pr_number,
            pr_url=pr_url,
        )

    def create_external_wait(
        self,
        item: Mapping[str, Any],
        check: Callable[[], Any],
        *,
        question: str = "",
    ) -> Dict[str, Any]:
        """Register the recheck of something outside.

        Nothing outside is asked a question, so there is nothing to ask
        it with: an external wait writes **no** decision row and
        delivers nothing.  What it registers is the *recheck* — the
        call that says whether the condition is satisfied yet — and the
        wait goes on with the same state model every other wait has.

        Args:
            item: The requirement item the wait belongs to.
            check: The recheck.  A callable that returns a truthy value
                when the condition is satisfied.  A recheck that raises
                is a recheck that did **not** satisfy the condition: an
                exception is not evidence that a CI run passed.
            question: The question shown for this wait.  Defaults to the
                item's own action — an external wait has no human to
                phrase a question for.

        Returns:
            The wait record, carrying no decision id: there is no row.

        Raises:
            WaitError: When the recheck is not callable, or the
                question is not a string.
            WaitConflict: When this item already waits.
        """
        if not callable(check):
            raise WaitError(f"外部复查必须是可调用对象, got {type(check).__name__}")
        item_id = self._item_id_of(item)
        asked = question if question else item.get("action", "")
        self.create_wait(item, asked, wait_type="external")
        self._checks[item_id] = check
        return {**self._waits[item_id].to_record(), "decision_id": None}

    def check_external_wait(self, item_id: str) -> Dict[str, Any]:
        """Run the registered recheck of one external wait.

        The recheck is the only thing an external wait has: it is asked
        no question, so nothing else can move it.  A recheck that
        raises counts as "not satisfied" — the wait goes on, and it is
        not a failure either: a CI page that cannot be read is not a CI
        run that failed.

        Args:
            item_id: The item the wait belongs to.

        Returns:
            The wait record, carrying whether the condition is
            satisfied and, when the recheck raised, what it raised.

        Raises:
            WaitError: When the wait is unknown or has no recheck.
            WaitConflict: When the wait is not pending any more.
        """
        wait = self._require(item_id)
        if wait.status != PENDING:
            raise WaitConflict(
                f"等待条目 {item_id} 当前状态为 {wait.status}，不能再复查"
            )
        check = self._checks.get(item_id)
        if check is None:
            raise WaitError(f"等待条目 {item_id} 没有登记外部复查")

        error: Optional[str] = None
        try:
            satisfied = bool(check())
        except Exception as exc:  # noqa: BLE001 - 复查失败就是"不满足"
            satisfied = False
            error = f"{type(exc).__name__}: {exc}"

        if satisfied:
            wait.answer = True
            wait.status = ANSWERED
            self._persist(wait)

        return {
            **wait.to_record(),
            "decision_id": None,
            "satisfied": satisfied,
            "error": error,
        }

    def resolve_human_wait(self, item_id: str, answer: Any) -> Dict[str, Any]:
        """Settle a human wait with the answer that was given.

        The answer is written through the existing compare-and-set, so
        two answers racing produce one stored answer and the loser is
        told it lost.  An answer somebody else already landed — the
        existing HITL service answers the very same row — is *adopted*
        rather than written twice.

        Args:
            item_id: The item the wait belongs to.
            answer: The answer that was given.

        Returns:
            The wait record, now answered.

        Raises:
            WaitError: When the wait is unknown, has no decision row,
                or its decision row is gone.
            WaitConflict: When the wait is not pending any more — a
                late answer to a terminated wait is refused, never
                honoured.
        """
        wait = self._require(item_id)
        if wait.status != PENDING:
            raise WaitConflict(
                f"等待条目 {item_id} 当前状态为 {wait.status}，不能再提交答案"
            )
        self._require_store()
        if self._decision_of(item_id) is None:
            raise WaitError(f"等待条目 {item_id} 没有对应的 HITL 决策行")

        return self._apply_answer(wait, answer).to_record()

    # ------------------------------------------------------------------
    # The drive
    # ------------------------------------------------------------------

    def drive(
        self,
        item_id: str,
        *,
        window: Optional[float] = None,
        poll: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Settle one wait, if it settles itself within the window.

        The drive is what a wait does **while it is being waited for**.
        It runs in the thread that is already running the finishing
        stage — it starts no thread and no timer of its own — and it
        comes back on its own: when the window is over, the wait is
        still pending and the stage parks the item.

        What it does on every tick:

        * the **deadline** is checked first, through the existing
          :meth:`expire_due` — a wait nobody answered until it was due
          is a failure, and it fails the way it always failed;
        * a **human** wait is settled by *reading* the answer that was
          given.  The read is read-only: the system never answers on a
          human's behalf, and an answer nobody gave is not an answer;
        * an **external** wait is settled by **rechecking** the
          condition.  A recheck that raises is a recheck that did not
          satisfy anything — the wait goes on.

        Nothing a wait does while it is driven is raised to the caller:
        a read that fails and a recheck that raises are both "not
        satisfied yet", and neither of them is the stage's business.

        Args:
            item_id: The item the wait belongs to.
            window: How long the drive may take, in seconds.  Defaults
                to :data:`WAIT_SETTLE_SECONDS`.
            poll: The rhythm between two ticks, in seconds.  Defaults
                to :data:`WAIT_POLL_SECONDS` — the two-second scale the
                backend already keeps.

        Returns:
            The wait's record as it stands when the drive is over.  An
            external wait's record also carries whether the condition
            was satisfied and, when the recheck raised, what it raised.

        Raises:
            WaitError: When no wait was created for that item.
        """
        wait = self._require(item_id)
        if wait.status != PENDING:
            return self._outcome(item_id)

        span = WAIT_SETTLE_SECONDS if window is None else max(0.0, float(window))
        step = WAIT_POLL_SECONDS if poll is None else max(0.0, float(poll))
        until = time.monotonic() + span

        while True:
            if self._expire(item_id):
                return self._outcome(item_id)
            if self._settle(item_id):
                return self._outcome(item_id)
            now = time.monotonic()
            if now >= until:
                break
            time.sleep(min(step, until - now))
        return self._outcome(item_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _outcome(self, item_id: str) -> Dict[str, Any]:
        """The wait's record as it stands, with the last recheck's facts.

        An external wait's record carries whether the condition was
        satisfied and, when the recheck raised, what it raised — the
        two facts a reader needs to tell "not yet" from "the recheck is
        broken".
        """
        record = self._require(item_id).to_record()
        outcome = self._last_checks.get(item_id)
        if outcome is not None:
            record["satisfied"] = outcome.get("satisfied")
            record["error"] = outcome.get("error")
        return record

    def _expire(self, item_id: str) -> bool:
        """Fail the wait if its deadline has passed; report whether it did.

        The expiry is the existing one — :meth:`expire_due` — so a wait
        that ran out of time fails exactly the way it always failed,
        with the same semantics and the same audit.
        """
        expired = self.expire_due()
        return any(record.get("item_id") == item_id for record in expired)

    def _settle(self, item_id: str) -> bool:
        """Settle the wait if it settles itself; report whether it did.

        A human wait settles when somebody answered it — the answer is
        **read**, never written.  An external wait settles when the
        recheck says the condition is satisfied; a recheck that raises
        is a recheck that satisfied nothing.
        """
        wait = self._require(item_id)
        if wait.wait_type == "external":
            try:
                outcome = self.check_external_wait(item_id)
            except WaitError as exc:
                logger.warning(
                    "postprocess_wait_recheck_failed: item=%s: %s", item_id, exc
                )
                return False
            self._last_checks[item_id] = outcome
            return outcome.get("status") == ANSWERED

        answered, answer = self._read_answer(item_id)
        if not answered:
            return False
        self._adopt_answer(item_id, answer)
        return True

    def _read_answer(self, item_id: str) -> Tuple[bool, Any]:
        """The answer that was given for one wait, read-only.

        The row is **read**: the system never answers on a human's
        behalf, and an answer nobody gave is not an answer.  A read
        that fails is "no answer yet" — the wait goes on, and the
        failure is not the stage's business.
        """
        if self._store is None:
            return False, None
        try:
            decision = self._decision_of(item_id)
        except Exception as exc:  # noqa: BLE001 — 读不到答复就是还没答复
            logger.warning(
                "postprocess_wait_answer_unreadable: item=%s: %s", item_id, exc
            )
            return False, None
        if decision is None:
            return False, None
        if decision.status != HITL_ANSWERED_STATUS:
            return False, None
        return True, decision.answer

    def _adopt_answer(self, item_id: str, answer: Any) -> Dict[str, Any]:
        """Settle a wait with the answer that was given — without writing it.

        The store is not touched: the answer is already there, written by
        the human through the existing HITL service.  What this does is
        make the **wait** follow it.
        """
        wait = self._require(item_id)
        wait.answer = answer
        wait.status = ANSWERED
        self._persist(wait)
        return wait.to_record()

    def _adopt_decision(self, item_id: str, decision: Decision) -> Dict[str, Any]:
        """Bind one wait to the question that was already asked.

        The question the human was asked is the one the wait carries —
        a wait that shows a different question than the one in the table
        would be a question about something else.
        """
        wait = self._require(item_id)
        wait.question = decision.question
        self._decision_ids[item_id] = decision.decision_id
        self._persist(wait)
        return {**wait.to_record(), "decision_id": decision.decision_id}

    def _require_store(self) -> Any:
        """The HITL store, or an error naming what is missing.

        A question that cannot be stored is a question nobody can
        answer: it would be asked, delivered and forgotten, and the
        person on the other end would have no way of answering it.
        So the missing store is an error, not a silent skip.
        """
        if self._store is None:
            raise WaitError(
                "没有给出 HITL store：这个问题存不下来，存不下来的问题没有人能回答"
            )
        return self._store

    def _decision_of(self, item_id: str) -> Optional[Decision]:
        """The question stored for one wait, or ``None``.

        The id of the row is remembered when the question is created.
        A wait outlives the process that created it — a week-long wait
        does — so the row is looked up in the store when the registry
        does not remember it: the row carries the item's id, and its
        ``source`` says the finishing stage is who asked.
        """
        if self._store is None:
            return None

        remembered = self._decision_ids.get(item_id)
        if remembered is not None:
            decision = self._store.get(remembered)
            if decision is not None:
                return decision

        for decision in reversed(self._store.list_by_plan(self.plan_id)):
            if decision.task_id != item_id:
                continue
            if decision.source in _BRIDGE_SOURCES:
                return decision
        return None

    def _create_decided_wait(
        self,
        item: Mapping[str, Any],
        question: str,
        *,
        source: str,
        options: Tuple[str, ...],
        note: str = "",
        record_source: str = "",
        pr_number: Optional[int] = None,
        pr_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a wait whose question is stored in the HITL table.

        The order is deliberate.  The wait is created first — that is
        what refuses a second wait for the same item, before anything
        is written or said — then the question is stored, and only
        then is it delivered.  A question that exists but was never
        announced is a fact an auditor can see; a question that was
        announced but never stored is a question a human is answering
        into the void.

        The row carries **no** default answer and **no** deadline of
        its own.  A question with a default answer is a question the
        system may answer on the human's behalf, and a deadline baked
        into the row would be a second, staler copy of the deadline
        this module keeps — the one that can be pushed out.  The row is
        a *strict* one, so the existing sweep leaves it alone.
        """
        store = self._require_store()
        if not isinstance(question, str) or not question.strip():
            raise WaitError("等待的问题必须是非空字符串")

        item_id = self._item_id_of(item)
        self.create_wait(
            item,
            question,
            wait_type="human",
            record_source=record_source,
            pr_number=pr_number,
            pr_url=pr_url,
        )
        record = self._waits[item_id].to_record()

        existing = self._decision_of(item_id)
        if existing is not None:
            # The question was already asked.  A plan that comes back to
            # an open question — the stage is resumed when the answer
            # arrives — finds the question that is already there: a
            # second row and a second delivery would be a second
            # question asked of the same human.
            return self._adopt_decision(item_id, existing)

        request = DecisionRequest(
            plan_id=self.plan_id,
            source=source,
            task_id=item_id,
            question=question,
            options=list(options),
            mode=HITL_STRICT_MODE,
            timeout_sec=None,
            answer_note=note or None,
        )
        decision = Decision.from_request(request, now=self._now())
        store.insert(decision)
        store.mark_heartbeat(decision.decision_id)
        self._decision_ids[item_id] = decision.decision_id

        self._deliver_question(record, decision)
        return {**record, "decision_id": decision.decision_id}

    def _deliver_question(
        self,
        record: Mapping[str, Any],
        decision: Decision,
    ) -> None:
        """Deliver the question through the channel the plan is bound to.

        The event is the *same* one the existing HITL outlet carries —
        the same keys in the same shape — so the channel that already
        exists needs nothing new to deliver a finishing-stage
        question.  The only field that says something new is the
        deadline: it is the wait's own deadline, the one a human is
        racing and the one that can be pushed out, not the row's (the
        row has none).

        Without a channel there is nobody to tell: the question stays
        stored and pending, and the plan's own reading of the pending
        rows is what shows it.
        """
        if self._deliver is None:
            return

        self._deliver(
            {
                "plan_id": decision.plan_id,
                "decision_id": decision.decision_id,
                "task_id": decision.task_id,
                "kind": decision.source,
                "question": decision.question,
                "options": list(decision.options),
                "default_option": decision.default_option,
                "deadline_at": record.get("deadline"),
            }
        )

    def _apply_answer(self, wait: _Wait, answer: Any) -> _Wait:
        """Settle a wait with an answer, keeping its question in step.

        When the wait has a question stored in the HITL table, the
        answer is written through the existing compare-and-set, so two
        answers racing produce one stored answer and the loser is told
        it lost.  An answer somebody else already landed — the existing
        HITL service answers the very same row — is *adopted* rather
        than written twice.
        """
        decision = self._decision_of(wait.item_id)
        if decision is not None:
            if not self._store.answer_if_pending(decision.decision_id, answer):
                settled = self._store.get(decision.decision_id)
                if settled is None:
                    raise WaitError(
                        f"等待条目 {wait.item_id} 的决策行不存在: {decision.decision_id}"
                    )
                if settled.status != HITL_ANSWERED_STATUS:
                    raise WaitConflict(
                        f"决策 {decision.decision_id} 当前状态为 {settled.status}，"
                        "不能再提交答案"
                    )
                answer = settled.answer

        wait.answer = answer
        wait.status = ANSWERED
        self._persist(wait)
        return wait

    def _settle_decision(self, item_id: str, *, note: str) -> None:
        """Settle the question of one wait, if it has one.

        A wait that was given up on must not leave its question open:
        an answer arriving afterwards would be accepted by the
        existing HITL service and land on a question nobody is waiting
        for any more.  The row is settled as ``unroutable`` — the
        question was asked and nobody answered it — so a late answer
        is a conflict rather than an overwrite.
        """
        if self._store is None:
            return
        decision = self._decision_of(item_id)
        if decision is None:
            return
        self._store.mark_unroutable(decision.decision_id, note=note)

    def _require(self, item_id: str) -> _Wait:
        """The wait of one item, or an error naming what is missing."""
        wait = self._waits.get(item_id)
        if wait is None:
            raise WaitError(f"没有找到等待条目: {item_id}")
        return wait

    def _now(self) -> datetime:
        """The current moment, as this registry's clock sees it.

        The moment is normalised to UTC and truncated to whole seconds,
        so the deadline written into the record and the deadline the
        registry keeps are the same instant — a record that says one
        time and a registry that believes another is a wait that
        expires at the wrong moment.
        """
        moment = self._clock.now()
        if not isinstance(moment, datetime):
            raise WaitError(f"时钟必须返回 datetime, got {type(moment).__name__}")
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).replace(microsecond=0)

    @staticmethod
    def _item_id_of(item: Mapping[str, Any]) -> str:
        """The id of a requirement item."""
        if not isinstance(item, Mapping):
            raise WaitError(f"等待条目必须是对象, got {type(item).__name__}")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise WaitError(f"等待条目缺少可用的 id: {item_id!r}")
        return item_id

    @staticmethod
    def _wait_type_of(
        item: Mapping[str, Any],
        explicit: Optional[str],
    ) -> str:
        """The kind of wait an item asks for.

        An item that declares no wait of its own — or declares
        ``none`` — waits for a human: asking a question is what a
        wait *is*, and the executor that asks it is the human one.
        """
        if explicit is not None:
            if explicit not in WAIT_TYPES:
                raise WaitError(
                    f"未知的等待类型 {explicit!r}，必须是 {list(WAIT_TYPES)} 之一"
                )
            return explicit

        declared = item.get("wait")
        if isinstance(declared, Mapping):
            candidate = declared.get("type")
            if candidate in WAIT_TYPES:
                return candidate
        return "human"

    @staticmethod
    def _checked_seconds(seconds: Any) -> int:
        """A number of seconds that may be added to a deadline."""
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            raise WaitError(f"秒数必须是非负整数, got {seconds!r}")
        if seconds <= 0:
            raise WaitError(f"秒数必须是正整数, got {seconds!r}")
        return seconds

    @staticmethod
    def _checked_operator(operator: Any, item_id: str) -> str:
        """The name of whoever acted on a wait.

        The audit entry names the operator, so an unnamed operator is
        refused rather than recorded as an empty string: an audit row
        that says nobody did it is worse than no audit row.
        """
        if not isinstance(operator, str) or not operator:
            raise WaitError(f"等待条目 {item_id} 的操作人必须是非空字符串")
        return operator

    def _persist(self, wait: _Wait) -> None:
        """Write the wait record into the plan's progress file.

        The deadline lives in the finishing stage's own progress record
        (architecture decision point 5) — not in the HITL timeout
        configuration, whose ``auto``/``strict`` semantics this stage
        must not touch.  A week-long wait outlives the process that
        created it, so the record has to be on disk.
        """
        mark(
            self.plan_id,
            wait.item_id,
            ITEM_STATUS_BY_WAIT_STATUS[wait.status],
            wait=wait.to_record(),
        )

# ---------------------------------------------------------------------------
# The production wiring
# ---------------------------------------------------------------------------


def production_registry(
    plan_id: str,
    plan_dir: Any,
    *,
    clock: Optional[Any] = None,
    store: Optional[Any] = None,
) -> WaitRegistry:
    """The registry the finishing stage builds for itself.

    A registry is only worth what it is wired to.  This one is wired to
    the machinery that already exists:

    * the **store** is the existing HITL store on the state database —
      the table that owns ``plan_decisions``, so a question asked here
      is a question the existing answer route can answer;
    * the **channel** is the plan's own ``hitl_config.json`` binding,
      delivered by the existing hook dispatcher under the existing
      ``hitl_decision_created`` event.  A plan nobody is bound to asks
      nobody: the question stays stored and pending, and the plan's own
      reading of the pending rows is what shows it.

    The finishing stage therefore grows no second decision store, no
    second delivery channel and no second timeout vocabulary.

    Args:
        plan_id: The plan whose waits these are.
        plan_dir: The plan's own directory — where its HITL binding
            lives.
        clock: The clock the registry reads the moment from.
        store: The store the questions are written through.  Defaults to
            the existing HITL store on the state database.

    Returns:
        The registry, ready to ask.
    """
    if store is None:
        store = HitlStore(resolve_state_db_path())
    return WaitRegistry(
        plan_id,
        clock=clock,
        store=store,
        deliver=_plan_delivery(Path(plan_dir)),
    )


def _plan_delivery(plan_dir: Path) -> Callable[[Mapping[str, Any]], Any]:
    """The channel one plan's own binding delivers a question through.

    The delivery is the existing hook dispatcher's, under the existing
    event: the outlet that already carries the HITL's questions carries
    this one too.

    A delivery that reaches nobody is **logged, not raised**: the
    question is written down before anybody is told about it, so a
    question nobody was told about is still a question the audit mirror
    can show — and a plan whose channel is broken must not take the
    finishing stage down with it.
    """

    def deliver(payload: Mapping[str, Any]) -> Any:
        try:
            report = dispatch(EVENT_DECISION_CREATED, payload, plan_dir)
        except Exception as exc:  # noqa: BLE001 — 投递失败不是等待失败
            logger.warning(
                "postprocess_wait_undelivered: plan=%s decision=%s: %s",
                payload.get("plan_id"),
                payload.get("decision_id"),
                exc,
            )
            return None
        if report.delivered == 0:
            logger.warning(
                "postprocess_wait_undelivered: plan=%s decision=%s: the "
                "plan's binding reached nobody",
                payload.get("plan_id"),
                payload.get("decision_id"),
            )
        return report

    return deliver


def _pull_request_of(
    command: Sequence[Any],
    *,
    pr_number: Optional[int],
    pr_url: Optional[str],
) -> Tuple[Optional[int], Optional[str]]:
    """The pull request a command is about, and the link to it.

    What the caller stated wins: a caller that read the pull request
    somewhere else knows more than this module does.  Only what the
    caller left open is read off the command — and a command that names
    no pull request at all leaves both of them empty.
    """
    if pr_number is not None and pr_url is not None:
        return pr_number, pr_url
    reference = extract_pr_ref(command)
    if reference is None:
        return pr_number, pr_url
    if pr_number is None:
        pr_number = reference["pr_number"]
    if pr_url is None:
        pr_url = reference["pr_url"]
    return pr_number, pr_url
