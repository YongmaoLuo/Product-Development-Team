"""Backend-agnostic contract types for the harness layer.

Why this module exists
======================

Every way this codebase drives a coding agent ends the same way: build
an argv, spawn a process, read a stream of events out of it, and decide
what the exit code *meant*. Each harness did all four steps inline, in
its own slightly different shape, so a change to one of them had to be
written three times and the three copies drifted — a flag that one
harness spelled ``--allowed-tools`` and another spelled
``--allowedTools``, an event type one harness reported as ``result`` and
another reported as ``assistant_done``.

This module is the one place where those four steps are described as
*data* instead of as code. It holds no I/O, no process handling and no
provider knowledge. An adapter (any object satisfying :class:`Harness`)
turns a :class:`TaskInvocation` into a :class:`HarnessInvocation`,
parses the child's output into :class:`HarnessEvent` objects, and
classifies the exit into a :class:`HarnessOutcome`. The runner above it
never learns which harness it is driving.

Three layers of contract
------------------------

1. **Request** — :class:`TaskInvocation` says what the caller wants
   done, in terms no single harness owns.
2. **Process** — :class:`HarnessInvocation` says how to spawn it;
   :class:`HarnessEvent` and :class:`HarnessResult` say what came back.
3. **Verdict** — :class:`HarnessOutcome` says what the run *meant*, over
   a closed set of kinds, so the runner can branch on the verdict
   instead of pattern-matching free text.

Immutability
-------------

Every dataclass here is frozen except for one field:
:class:`HarnessEvent.raw`, the untouched payload an event was parsed
out of. Everything else raises ``FrozenInstanceError`` on assignment,
because a contract object that can be rewritten after the fact is a
contract nobody can rely on — the runner hands one to a handler, and a
handler able to rewrite it turns the contract into a suggestion.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError, dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

__all__ = [
    "SessionMode",
    "SessionRef",
    "TaskInvocation",
    "HarnessInvocation",
    "HarnessEvent",
    "HarnessCapabilities",
    "HarnessOutcome",
    "HarnessResult",
    "ProfileBinding",
    "Harness",
    "HarnessConfigError",
    "OUTCOME_OK",
    "OUTCOME_MODEL_ERROR",
    "OUTCOME_TIMEOUT",
    "OUTCOME_PERMISSION_DENIED",
    "OUTCOME_CONFIG_ERROR",
    "OUTCOME_SIGNALED",
    "OUTCOME_UNKNOWN",
    "OUTCOME_KINDS",
]


class HarnessConfigError(RuntimeError):
    """A harness is configured in a way it cannot run.

    Raised while an adapter is being built — a missing binary, a
    settings file that does not exist, a profile bound to a harness that
    does not implement it. It is deliberately its own type and not a
    ``ValueError``: the caller can catch it and report a configuration
    problem instead of a bad argument, and the runner maps it onto the
    ``config_error`` outcome rather than onto a generic failure.
    """


class SessionMode(str, Enum):
    """How a harness is expected to obtain the conversation.

    Attributes:
        CREATE: Start a brand-new conversation. A
            :class:`SessionRef` carried along with this mode names the id
            the new conversation must be created *under*, when the
            harness supports pinning one.
        RESUME: Re-open an existing conversation by id. A
            :class:`SessionRef` carried along with this mode names the
            conversation to continue.
    """

    CREATE = "create"
    RESUME = "resume"


@dataclass(frozen=True)
class SessionRef:
    """Which conversation a request belongs to.

    Attributes:
        session_id: The conversation itself. Under
            :attr:`SessionMode.CREATE` it is the id the new
            conversation must be created under; under
            :attr:`SessionMode.RESUME` it is the conversation to
            re-open. Empty means the harness picks.
        pinned_session_id: The id the caller insists on, even when the
            harness would be free to choose its own. Empty means the
            caller is not pinning anything.
    """

    session_id: Optional[str] = None
    pinned_session_id: Optional[str] = None


@dataclass(frozen=True)
class TaskInvocation:
    """What the caller wants a harness to do, in harness-neutral terms.

    Every field is something a harness may or may not be able to honour;
    :class:`HarnessCapabilities` says which of them a given harness
    supports, so the caller can ask rather than guess.

    Attributes:
        prompt: The task text handed to the model. The only required
            field — everything else is a refinement of how the harness
            should run it.
        system_instruction: Extra instructions appended to the harness's
            own system prompt. Requires the ``append_system_prompt``
            capability.
        mcp_config: An inline MCP server configuration.
        mcp_config_path: A path to an MCP server configuration file.
            Requires the ``mcp_injection`` capability.
        allowed_tools: The tools the harness may use. Requires the
            ``tool_whitelist`` capability.
        model: The model to run, in whatever spelling the harness
            accepts.
        settings_path: A settings file the harness should load.
        cwd: The working directory to run in.
        session: The conversation this request belongs to. Empty means
            the harness starts a fresh one.
        mode: Whether to create or resume that conversation.
        multi_turn: Whether the harness may keep the conversation open
            for more than one turn.
    """

    prompt: str
    system_instruction: Optional[str] = None
    mcp_config: Optional[str] = None
    mcp_config_path: Optional[str] = None
    allowed_tools: Tuple[str, ...] = ()
    model: Optional[str] = None
    settings_path: Optional[str] = None
    cwd: Optional[str] = None
    session: Optional[SessionRef] = None
    mode: SessionMode = SessionMode.CREATE
    multi_turn: bool = False


@dataclass(frozen=True)
class HarnessInvocation:
    """How to spawn a harness process.

    Attributes:
        argv: The command line, argv[0] included. The adapter owns the
            spelling of every flag; nothing above this layer knows it.
        env_delta: Environment variables to set *on top of* the inherited
            environment. The adapter's own additions (credentials, a
            per-run temp dir) belong here, so the runner can show what
            a run was given.
        cwd: The working directory to run in. Empty means "inherit the
            runner's".
        prepare: An optional context manager entered just before the
            process is spawned and exited just after it is reaped. It is
            how a harness reserves a resource the child needs (a
            settings file written to a temp dir, a lock held) without
            the runner knowing what that resource is.
        multi_turn: Whether the run has to produce a *stream* — the
            assistant events of a conversation — rather than only its
            terminal envelope. The adapter owns this fact: it is the
            one that wrote the command line, so it is the one that
            copies it here. The runner only reads it, and refuses a
            multi-turn run that produced no stream.
    """

    argv: Tuple[str, ...]
    env_delta: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    prepare: Optional[Callable[[], AbstractContextManager[None]]] = None
    multi_turn: bool = False


@dataclass(init=False)
class HarnessEvent:
    """One event from a harness's output stream, normalized.

    The same shape whatever the harness spoke: the adapter maps its own
    event vocabulary onto :attr:`type`, and anything it could not map is
    still delivered as an event of some type rather than dropped.

    Every field is frozen except :attr:`raw`. ``raw`` holds the payload
    the event was parsed out of, and it stays mutable on purpose: it is
    the escape hatch for information this contract has no field for, and
    an adapter (or a handler reading the stream) can attach to it
    without the contract having to grow a field per harness.

    Attributes:
        type: The normalized event type — ``"system"``, ``"assistant"``,
            ``"result"``, ``"error"`` and anything else the adapter
            defines.
        raw: The payload as the harness produced it.
        text: Text carried by the event, with the harness's own
            formatting already stripped.
        session_id: The conversation the event belongs to, when the
            harness reports one.
        usage: Token / cost accounting carried by the event, when the
            harness reports it.
        usage_partial: Whether :attr:`usage` is a partial count — the
            adapter that wrote the event knew that a field of it was
            never reported, and said so on the event itself. The flag
            travels with the usage it describes, so a reader never has
            to re-derive it.
        is_error: Whether the event reports a failure.
    """

    type: str
    raw: Dict[str, Any]
    text: str
    session_id: Optional[str]
    usage: Optional[Dict[str, Any]]
    usage_partial: bool
    is_error: bool

    def __init__(
        self,
        type: str,
        raw: Optional[Dict[str, Any]] = None,
        text: str = "",
        session_id: Optional[str] = None,
        usage: Optional[Dict[str, Any]] = None,
        usage_partial: bool = False,
        is_error: bool = False,
    ) -> None:
        # ``init=False`` because the generated ``__init__`` assigns through
        # ``__setattr__``, and the ``__setattr__`` below refuses every
        # field but ``raw``. Construction therefore goes through
        # ``object.__setattr__``, which bypasses the guard.
        object.__setattr__(self, "type", type)
        object.__setattr__(self, "raw", {} if raw is None else raw)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "usage", usage)
        object.__setattr__(self, "usage_partial", usage_partial)
        object.__setattr__(self, "is_error", is_error)

    def __setattr__(self, name: str, value: Any) -> None:
        """Allow ``raw`` to be replaced; refuse every other field."""
        if name == "raw":
            object.__setattr__(self, name, value)
            return
        raise FrozenInstanceError(
            f"cannot assign to field {name!r} of a frozen HarnessEvent "
            f"(only 'raw' is mutable)"
        )


@dataclass(frozen=True)
class HarnessCapabilities:
    """What a harness is able to do.

    Every field is required on purpose. A flag that defaulted to
    ``False`` would let a caller ask for something the harness cannot do
    and find out at spawn time; a flag that defaulted to ``True`` would
    have the adapter advertise a feature it never wired up. Either
    default turns a capability declaration into a guess.

    Attributes:
        append_system_prompt: The harness can append
            :attr:`TaskInvocation.system_instruction` to its system
            prompt.
        mcp_injection: The harness can be given MCP server
            configuration.
        tool_whitelist: The harness can be restricted to a given tool
            list.
        stream_events: The harness reports a stream of events while it
            runs, rather than only its final answer.
        usage: The harness reports token / cost accounting.
        session_create: The harness can start a conversation under a
            caller-chosen id.
        session_resume: The harness can re-open an existing
            conversation.
    """

    append_system_prompt: bool
    mcp_injection: bool
    tool_whitelist: bool
    stream_events: bool
    usage: bool
    session_create: bool
    session_resume: bool


#: The run finished and produced its answer.
OUTCOME_OK = "ok"

#: The model answered, and the answer was a failure (a refusal, an
#: upstream error, a malformed response).
OUTCOME_MODEL_ERROR = "model_error"

#: The run exceeded its time budget.
OUTCOME_TIMEOUT = "timeout"

#: The run was refused — the harness would not perform the request.
OUTCOME_PERMISSION_DENIED = "permission_denied"

#: The harness could not be run as configured.
OUTCOME_CONFIG_ERROR = "config_error"

#: The process was killed by a signal.
OUTCOME_SIGNALED = "signaled"

#: The run ended in a way nothing else describes.
OUTCOME_UNKNOWN = "unknown"

#: The closed set of outcome kinds. A verdict outside this set is not a
#: verdict this contract can carry.
OUTCOME_KINDS: FrozenSet[str] = frozenset(
    {
        OUTCOME_OK,
        OUTCOME_MODEL_ERROR,
        OUTCOME_TIMEOUT,
        OUTCOME_PERMISSION_DENIED,
        OUTCOME_CONFIG_ERROR,
        OUTCOME_SIGNALED,
        OUTCOME_UNKNOWN,
    }
)


@dataclass(frozen=True)
class HarnessOutcome:
    """What a run meant.

    Attributes:
        kind: One of :data:`OUTCOME_KINDS`. Nothing else is accepted —
            the runner branches on this value, so an open vocabulary
            would mean every caller re-parsed free text.
        message: A short, operator-facing explanation. Free text, and
            never parsed.
    """

    kind: str
    message: str = ""

    def __post_init__(self) -> None:
        if self.kind not in OUTCOME_KINDS:
            raise ValueError(
                f"unknown outcome kind {self.kind!r}; "
                f"expected one of {sorted(OUTCOME_KINDS)}"
            )


@dataclass(frozen=True)
class HarnessResult:
    """Everything a finished run produced.

    Attributes:
        exit_code: The process's exit code.
        events: The normalized event stream, in the order it arrived.
        final_text: The harness's final answer.
        usage: Token / cost accounting, when the harness reported it.
        usage_partial: Whether :attr:`usage` is a partial count — the
            run was cut short, so the number understates the cost.
        is_error: Whether the harness itself considered the run failed.
        session_id: The conversation the run happened in, when known.
        sandbox_state: What the sandbox did with this run, as a stable
            token — ``"not_configured"``, ``"applied"``,
            ``"inherited"``.
        harness: The name of the harness that produced this result.
        outcome: The verdict on what the run meant.
    """

    exit_code: int
    events: Tuple[HarnessEvent, ...] = ()
    final_text: str = ""
    usage: Optional[Dict[str, Any]] = None
    usage_partial: bool = False
    is_error: bool = False
    session_id: Optional[str] = None
    sandbox_state: Optional[str] = None
    harness: str = ""
    outcome: HarnessOutcome = field(
        default_factory=lambda: HarnessOutcome(kind=OUTCOME_UNKNOWN)
    )


@dataclass(frozen=True)
class ProfileBinding:
    """An operator-facing profile bound to one harness.

    A profile is the operator's name for a bundle of launch parameters
    — which model, which settings file, which environment. The binding
    says which harness that bundle applies to, so one profile can be
    written down once and used by every adapter that speaks the same
    harness.

    Attributes:
        profile: The operator-facing name of the profile.
        harness: The harness this profile is bound to.
        model: The model the profile runs.
        settings_path: The settings file the profile loads.
        env: Environment variables the profile contributes.
        argv_prefix: Arguments to put in front of the harness's own
            command line — a launcher, a sandbox wrapper.
    """

    profile: str
    harness: str
    model: Optional[str] = None
    settings_path: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    argv_prefix: Tuple[str, ...] = ()


@runtime_checkable
class Harness(Protocol):
    """What an adapter must be able to do.

    Four members, and the four steps every harness shares:

    * :meth:`capabilities` — what this harness can do, so a caller can
      ask for something it supports.
    * :meth:`build_invocation` — turn a request into a spawn
      description.
    * :meth:`parse_stream` — turn the child's output into normalized
      events.
    * :meth:`classify_exit` — turn the exit code and the events into a
      verdict.
    """

    def capabilities(self) -> HarnessCapabilities:
        """Describe what this harness supports."""
        ...

    def build_invocation(self, req: TaskInvocation) -> HarnessInvocation:
        """Turn a request into the command line that runs it."""
        ...

    def parse_stream(self, lines: Iterable[str]) -> List[HarnessEvent]:
        """Turn the child's output lines into normalized events."""
        ...

    def classify_exit(
        self, code: int, events: Sequence[HarnessEvent]
    ) -> HarnessOutcome:
        """Turn an exit code and the events it produced into a verdict."""
        ...