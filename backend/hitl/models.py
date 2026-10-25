"""The HITL decision data model.

Two objects, two closed vocabularies
------------------------------------
:class:`DecisionRequest` is what a caller *asks* for: a question, the
answers that are on the table, the answer to fall back to when nobody
replies, and how long to wait.  :class:`Decision` is what is *stored*:
the same thing plus the state it is in, the answer, who gave it and
when.

The state a decision can be in is a closed vocabulary —
``pending`` / ``answered`` / ``defaulted`` / ``paused`` / ``unroutable``
— and so is the way it was asked — ``auto`` / ``strict``.  "Closed" is
the operative word: a third value reaching the table would leave every
reader guessing what to do with it, so an illegal value is rejected
here, at construction, with ``ValueError``.  It never reaches the
database to be discovered later by a reader that has never heard of
it.

The two modes differ in what happens when the deadline passes:

  * ``auto``   — the ``default_option`` is used and the decision ends
    ``defaulted``.  The work continues without a human.
  * ``strict`` — there is no substitute for a human, so the decision
    ends ``unroutable``: the caller is told nobody answered.

A decision with no ``default_option`` cannot be ``auto``-resolved, so
it ends ``unroutable`` either way.

Why this module knows nothing about SQLite
------------------------------------------
The model is imported by request handlers, worker threads and CLIs
alike, and none of them should need a database connection to build a
request.  The mapping to the ``plan_decisions`` row therefore lives on
the model itself (:meth:`Decision.to_row` / :meth:`Decision.from_row`)
rather than in a repository that would have to be imported — and
threaded — to construct a value object.

Timestamps are ISO-8601 with an explicit UTC offset.  The clock is
injected (:meth:`Decision.from_request` takes a ``now``) so a test can
assert a deadline without sleeping for half an hour.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

__all__ = [
    "Decision",
    "DecisionMode",
    "DecisionRequest",
    "DecisionStatus",
]


class DecisionStatus(str, Enum):
    """The closed vocabulary of decision states.

    ``pending``
        Asked, not yet answered.  The only legal starting state.
    ``answered``
        A human gave an answer.
    ``defaulted``
        Nobody answered in time and the ``default_option`` was used.
    ``paused``
        The decision was suspended — the asker is not waiting any
        more, but the question has not been resolved either.
    ``unroutable``
        The decision cannot be continued: nobody answered and there was
        no default to fall back to.
    """

    PENDING = "pending"
    ANSWERED = "answered"
    DEFAULTED = "defaulted"
    PAUSED = "paused"
    UNROUTABLE = "unroutable"


class DecisionMode(str, Enum):
    """The closed vocabulary of ways a decision can be asked.

    ``auto``
        A missed deadline falls back to the ``default_option``.
    ``strict``
        A missed deadline ends the decision ``unroutable`` — a human
        cannot be substituted.
    """

    AUTO = "auto"
    STRICT = "strict"


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
#
# Every one of these raises ValueError.  The reason is the same in all
# cases: a value that is not in the vocabulary is a programmer error,
# and the place to report a programmer error is where the mistake was
# made — not where a reader eventually trips over the result.


def _require_text(value: Any, name: str) -> str:
    """Return ``value`` if it is a non-empty string, else raise."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string; got {value!r}")
    return value


def _normalise_mode(mode: Any) -> str:
    """Return the canonical ``mode`` string, or raise ``ValueError``.

    Both the enum member and its string value are accepted, so a
    caller may write ``mode=DecisionMode.STRICT`` or
    ``mode="strict"``.  The match is exact: the vocabulary is two
    lower-case words wide and ``"AUTO"`` is not one of them.
    """
    if isinstance(mode, DecisionMode):
        return mode.value
    if isinstance(mode, str):
        try:
            return DecisionMode(mode).value
        except ValueError:
            raise ValueError(
                f"mode must be one of "
                f"{[m.value for m in DecisionMode]!r}; got {mode!r}"
            ) from None
    raise ValueError(
        f"mode must be a string or a DecisionMode; got {type(mode).__name__}"
    )


def _normalise_status(status: Any) -> str:
    """Return the canonical ``status`` string, or raise ``ValueError``."""
    if isinstance(status, DecisionStatus):
        return status.value
    if isinstance(status, str):
        try:
            return DecisionStatus(status).value
        except ValueError:
            raise ValueError(
                f"status must be one of "
                f"{[s.value for s in DecisionStatus]!r}; got {status!r}"
            ) from None
    raise ValueError(
        f"status must be a string or a DecisionStatus; got {type(status).__name__}"
    )


def _coerce_options(options: Any) -> List[str]:
    """Return ``options`` as a list of strings, tolerating an empty list.

    Used for the *stored* form: a row on disk is a fact to be reported,
    not a command to be policed, so an option set that cannot be read
    back does not stop a reader from reporting on the decision.
    """
    if options is None:
        return []
    if isinstance(options, str):
        return [options]
    try:
        return list(options)
    except TypeError:
        return []


def _require_options(options: Any) -> List[str]:
    """Return ``options`` as a list of non-empty strings, else raise.

    A decision with no options cannot be answered, so an empty option
    set is rejected at the point the question is asked.
    """
    if options is None:
        raise ValueError("options must be a non-empty sequence of option strings")
    if isinstance(options, str):
        raise ValueError(
            "options must be a sequence of option strings, not a single string"
        )
    try:
        values = list(options)
    except TypeError:
        raise ValueError(
            f"options must be a sequence of option strings; got {type(options).__name__}"
        ) from None
    if not values:
        raise ValueError(
            "options must not be empty; a decision needs at least one answer"
        )
    normalised: List[str] = []
    for option in values:
        if not isinstance(option, str) or not option.strip():
            raise ValueError(
                f"each option must be a non-empty string; got {option!r}"
            )
        normalised.append(option)
    return normalised


def _normalise_timeout(timeout_sec: Any) -> Optional[int]:
    """Return the timeout in seconds, or raise ``ValueError``.

    ``None`` means "no deadline" — a decision that waits for a human
    indefinitely.  A negative timeout is nonsense and is rejected.
    """
    if timeout_sec is None:
        return None
    if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, int):
        raise ValueError(
            f"timeout_sec must be an int or None; got {timeout_sec!r}"
        )
    if timeout_sec < 0:
        raise ValueError(
            f"timeout_sec must not be negative; got {timeout_sec!r}"
        )
    return timeout_sec


def _normalise_round(round_number: Any) -> Optional[int]:
    """Return the round number, or raise ``ValueError``.

    ``None`` means "not part of a round" — a question asked outside
    the verification loop has no round to belong to.  A round is a
    counter, so it is a non-negative integer; a negative one is a
    mistake about what a round is, and is rejected where the mistake
    was made.
    """
    if round_number is None:
        return None
    if isinstance(round_number, bool) or not isinstance(round_number, int):
        raise ValueError(
            f"round must be an int or None; got {round_number!r}"
        )
    if round_number < 0:
        raise ValueError(
            f"round must not be negative; got {round_number!r}"
        )
    return round_number


def _normalise_note(note: Any) -> Optional[str]:
    """Return the note as text, or raise ``ValueError``.

    A note is free-form prose, so it is not policed beyond being a
    string: a note that is *not* a string is a value the writer did
    not write, and the row reads every note back as text.
    """
    if note is None:
        return None
    if not isinstance(note, str):
        raise ValueError(f"note must be a string or None; got {note!r}")
    return note


def _decode_json(raw: Any, default: Any) -> Any:
    """Decode a JSON column, falling back to ``default``.

    A value that is not valid JSON is returned unchanged rather than
    dropped: the fact that something undecodable was stored is itself
    worth seeing.
    """
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


@dataclass
class DecisionRequest:
    """What a caller asks a human.

    The field order mirrors the documented call — ``plan_id``,
    ``source``, ``task_id``, ``question``, ``options``,
    ``default_option``, ``mode``, ``timeout_sec`` — so the object can
    be built positionally or by keyword.  Everything after ``source``
    carries a default only so that the *order* can be preserved;
    :meth:`__post_init__` rejects an empty question or an empty option
    set, so in practice they are required.

    Attributes
    ----------
    plan_id:
        The plan the decision belongs to.
    source:
        Who is asking — ``execution``, ``verification``, a route
        handler, ...  Free-form on purpose: a new caller must not need
        a schema change to ask a question.
    task_id:
        The task the question is about, when it is about one.
    vp_id:
        The verification point the question is about, when it is about
        one.  Together with ``failure_class`` and ``round`` this is
        what makes a decision *about a specific failure* rather than
        about the plan in general: an audit reader has to be able to
        find the decisions taken about one verification point.
    failure_class:
        The class of the failure that raised the question.
    round:
        Which round of the verification loop raised it.
    question:
        The question, in the words shown to the human.
    options:
        The answers that are on the table.  At least one.
    default_option:
        The answer to use when nobody replies.  Must be one of
        ``options`` when given.
    mode:
        ``auto`` or ``strict`` — see :class:`DecisionMode`.
    timeout_sec:
        How long to wait for an answer.  ``None`` means "no deadline".
    answer_note:
        A free-form note recorded with the question.  It is the note
        the decision carries before anybody answers it; a note that
        comes with an answer replaces it.
    """

    plan_id: str
    source: str
    task_id: Optional[str] = None
    vp_id: Optional[str] = None
    failure_class: Optional[str] = None
    round: Optional[int] = None
    question: str = ""
    options: List[str] = field(default_factory=list)
    default_option: Optional[str] = None
    mode: str = DecisionMode.AUTO.value
    timeout_sec: Optional[int] = 1800
    answer_note: Optional[str] = None

    def __post_init__(self) -> None:
        self.plan_id = _require_text(self.plan_id, "plan_id")
        self.source = _require_text(self.source, "source")
        if self.task_id is not None:
            self.task_id = _require_text(self.task_id, "task_id")
        if self.vp_id is not None:
            self.vp_id = _require_text(self.vp_id, "vp_id")
        if self.failure_class is not None:
            self.failure_class = _require_text(self.failure_class, "failure_class")
        self.round = _normalise_round(self.round)
        self.question = _require_text(self.question, "question")
        self.options = _require_options(self.options)
        if self.default_option is not None:
            self.default_option = _require_text(self.default_option, "default_option")
            if self.default_option not in self.options:
                raise ValueError(
                    f"default_option must be one of {self.options!r}; "
                    f"got {self.default_option!r}"
                )
        self.mode = _normalise_mode(self.mode)
        self.timeout_sec = _normalise_timeout(self.timeout_sec)
        self.answer_note = _normalise_note(self.answer_note)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    """A decision as it is stored.

    The field order mirrors the ``plan_decisions`` column order, so a
    row and an object read the same way.

    Unlike :class:`DecisionRequest` — which polices everything a caller
    must supply — a ``Decision`` checks only the two closed
    vocabularies.  It is a *record* of what was asked: it is built
    either from a validated request or read back off disk, and a row
    written by some other version of the backend is a fact to be
    reported, not a command to be rejected.
    """

    decision_id: str = ""
    plan_id: str = ""
    source: str = ""
    task_id: Optional[str] = None
    question: str = ""
    options: List[str] = field(default_factory=list)
    default_option: Optional[str] = None
    mode: str = DecisionMode.AUTO.value
    timeout_sec: Optional[int] = 1800
    status: str = DecisionStatus.PENDING.value
    answer: Any = None
    answered_by: Optional[str] = None
    answered_at: Optional[str] = None
    deadline_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    audit_seq: int = 0
    vp_id: Optional[str] = None
    failure_class: Optional[str] = None
    round: Optional[int] = None
    answer_note: Optional[str] = None

    def __post_init__(self) -> None:
        # The vocabularies are checked first: when both a bad status and
        # a bad mode are wrong, the mode is the first thing the caller
        # wrote and so the first thing they need told about.
        self.mode = _normalise_mode(self.mode)
        self.status = _normalise_status(self.status)

        self.options = _coerce_options(self.options)
        self.timeout_sec = _normalise_timeout(self.timeout_sec)
        # ``vp_id`` / ``failure_class`` / ``round`` / ``answer_note``
        # are carried as they are: they describe what the question is
        # about, and a row written by some other version of the backend
        # is a fact to be reported, not a command to be rejected.  The
        # *request* polices them — see DecisionRequest.
        if not self.decision_id:
            self.decision_id = uuid.uuid4().hex

    # -- construction ------------------------------------------------------

    @classmethod
    def from_request(
        cls,
        request: DecisionRequest,
        *,
        decision_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> "Decision":
        """Build the stored form of ``request``.

        The new decision is ``pending`` — that is the only state a
        question can start in.  The deadline is derived from
        ``timeout_sec`` and stored, so a reader never has to re-derive
        it (and cannot get it wrong).

        Parameters
        ----------
        request:
            The question as asked.
        decision_id:
            The identity of the question.  Minted when not given.
        now:
            The clock reading used for ``created_at`` / ``updated_at``
            and the deadline.  Injected so a test can assert a
            deadline without waiting for it.
        """
        moment = now or _now()
        deadline = None
        if request.timeout_sec is not None:
            deadline = (moment + timedelta(seconds=request.timeout_sec)).isoformat()
        stamp = moment.isoformat()
        return cls(
            plan_id=request.plan_id,
            source=request.source,
            question=request.question,
            options=list(request.options),
            decision_id=decision_id or uuid.uuid4().hex,
            task_id=request.task_id,
            vp_id=request.vp_id,
            failure_class=request.failure_class,
            round=request.round,
            answer_note=request.answer_note,
            default_option=request.default_option,
            mode=request.mode,
            timeout_sec=request.timeout_sec,
            status=DecisionStatus.PENDING.value,
            deadline_at=deadline,
            created_at=stamp,
            updated_at=stamp,
        )

    # -- persistence -------------------------------------------------------

    def to_row(self) -> Dict[str, Any]:
        """Return the ``plan_decisions`` row for this decision.

        ``options`` and ``answer`` are JSON-encoded; everything else
        is stored as-is.
        """
        return {
            "decision_id": self.decision_id,
            "plan_id": self.plan_id,
            "source": self.source,
            "task_id": self.task_id,
            "question": self.question,
            "options": json.dumps(list(self.options), ensure_ascii=False),
            "default_option": self.default_option,
            "mode": self.mode,
            "timeout_sec": self.timeout_sec,
            "status": self.status,
            "answer": (
                None if self.answer is None
                else json.dumps(self.answer, ensure_ascii=False)
            ),
            "answered_by": self.answered_by,
            "answered_at": self.answered_at,
            "deadline_at": self.deadline_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "heartbeat_at": self.heartbeat_at,
            "audit_seq": self.audit_seq,
            "vp_id": self.vp_id,
            "failure_class": self.failure_class,
            "round": self.round,
            "answer_note": self.answer_note,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Decision":
        """Rebuild a decision from a ``plan_decisions`` row.

        Accepts a :class:`sqlite3.Row` or any mapping keyed by column
        name.  A row whose ``options`` cannot be read back as a list
        yields an empty option list rather than an exception — a
        corrupt row should not stop a reader from reporting on the
        decisions that *are* readable.
        """
        data = dict(row)
        options = _decode_json(data.get("options"), [])
        if not isinstance(options, list):
            options = []
        return cls(
            plan_id=data.get("plan_id") or "",
            source=data.get("source") or "",
            question=data.get("question") or "",
            options=options,
            decision_id=data.get("decision_id") or "",
            task_id=data.get("task_id"),
            default_option=data.get("default_option"),
            mode=data.get("mode") or DecisionMode.AUTO.value,
            timeout_sec=data.get("timeout_sec"),
            status=data.get("status") or DecisionStatus.PENDING.value,
            answer=_decode_json(data.get("answer"), None),
            answered_by=data.get("answered_by"),
            answered_at=data.get("answered_at"),
            deadline_at=data.get("deadline_at"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            heartbeat_at=data.get("heartbeat_at"),
            audit_seq=data.get("audit_seq") or 0,
            vp_id=data.get("vp_id"),
            failure_class=data.get("failure_class"),
            round=data.get("round"),
            answer_note=data.get("answer_note"),
        )

    # -- state transitions -------------------------------------------------

    @property
    def is_open(self) -> bool:
        """True while the decision is still waiting for an answer."""
        return self.status == DecisionStatus.PENDING.value
