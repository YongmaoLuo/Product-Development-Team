"""The exit code table both adapters share.

Why this module exists
=======================

Two backends drove two CLIs, and each of them answered "what did this
exit code mean" on its own. The claude adapter had a table; the mcode
adapter had none, so the runner's fallback decided for it — and the
fallback knows nothing about mcode, so a model error, a configuration
error and a timeout all came out as ``unknown``. A verdict the layer
above cannot act on is not a verdict: the layer above branches on it,
and it does not know which CLI produced the run.

So the table lives here, once, and both adapters read it. The two
adapters differ in how they *spell* a command line and how they
translate an event stream; they do not differ in what an exit code
means. This module is that shared answer, and it is the only place
that writes it down.

The table
=========

============  ==========================================================
Exit code    Verdict
============  ==========================================================
``0``        the run finished
``1``        the model failed
``2``        the run could not be set up
``124``      the run ran out of time
``130``      the run was signalled (``SIGINT``)
``143``      the run was signalled (``SIGTERM``)
negative     the run was killed by that signal
anything     ``unknown`` -- the envelope is kept, not thrown away
============  ==========================================================

A negative code is not a table entry: it is what a process reports when
it is *killed* rather than when it exits. ``-15`` is a child that was
sent ``SIGTERM``, which is the same fact as a ``143`` — a run that was
cut short by a signal — so it is the same verdict.

One event overrides one code
============================

A CLI exits zero on a run whose answer was a failure, so reading the
exit code alone would call that run a success. A result event that
reports ``is_error`` turns a zero exit code into a model error. The
caller says which event reports the failure, because the two adapters
read it out of different places: the claude adapter reads it out of
the CLI's own payload, and the mcode adapter reads it out of the
``exec.completed`` event the run ended with.

An unknown verdict keeps the envelope
=====================================

The payload that came with the run is quoted in the message, because
it is the only thing left that says what happened: the exit code is one
nothing describes. The envelope is quoted as it was, so the message can
be read back against the stream that produced it.
"""

from __future__ import annotations

import json
import signal
from typing import Any, Dict, Mapping, Optional, Sequence

from .contract import (
    HarnessEvent,
    HarnessOutcome,
    OUTCOME_CONFIG_ERROR,
    OUTCOME_MODEL_ERROR,
    OUTCOME_OK,
    OUTCOME_SIGNALED,
    OUTCOME_TIMEOUT,
    OUTCOME_UNKNOWN,
)

__all__ = [
    "EXIT_CODE_KINDS",
    "SIGNAL_NAMES",
    "classify_exit",
    "extract_result",
]


#: The exit code -> outcome kind table. A code that is not in it is
#: not guessed at -- it is :data:`~harness.contract.OUTCOME_UNKNOWN`,
#: which is a verdict the runner can still act on.
EXIT_CODE_KINDS: Dict[int, str] = {
    0: OUTCOME_OK,
    1: OUTCOME_MODEL_ERROR,
    2: OUTCOME_CONFIG_ERROR,
    124: OUTCOME_TIMEOUT,
    130: OUTCOME_SIGNALED,
    143: OUTCOME_SIGNALED,
}

#: The signals the two signalled exit codes carry. A process killed by
#: a signal exits with ``128 + the signal number``.
SIGNAL_NAMES: Dict[int, str] = {130: "SIGINT", 143: "SIGTERM"}


def extract_result(events: Sequence[HarnessEvent]) -> Optional[Dict[str, Any]]:
    """The payload of the run's terminal ``result`` event.

    Both CLIs end a run with a result event: the summary of what the
    run did, whether it failed, what it cost. This hands back that
    payload -- the envelope -- as it was written.

    Args:
        events: The events of one run, in the order they arrived.

    Returns:
        The result event's payload, or ``None`` when the stream never
        reached one.
    """
    for event in reversed(list(events)):
        if event.type == "result":
            raw = event.raw
            if isinstance(raw, dict):
                return raw
    return None


def classify_exit(
    code: int,
    *,
    backend: str,
    envelope: Optional[Mapping[str, Any]] = None,
    failed_result: bool = False,
) -> HarnessOutcome:
    """Say what an exit code meant.

    The verdict comes from :data:`EXIT_CODE_KINDS`: a code that is in
    the table names its own outcome, and a code that is not in it is
    :data:`~harness.contract.OUTCOME_UNKNOWN`. A negative code is a
    child that was killed rather than one that exited, so it is a
    signalled run.

    One event overrides one code -- a result event that reports a
    failure turns a zero exit code into a model error, because the CLI
    exits zero on a run whose answer was a failure.

    An unknown verdict keeps the envelope: the payload that came with
    the run is quoted in the message, because it is the only thing left
    that says what happened.

    Args:
        code: The process's exit code.
        backend: The name of the CLI the run came from. It is what the
            message says the run called itself.
        envelope: The payload the run ended with. Defaults to nothing,
            which means the verdict has nothing to quote.
        failed_result: Whether the run's result reported a failure.

    Returns:
        The verdict on what the run meant.
    """
    kind = EXIT_CODE_KINDS.get(code)

    if kind is None and isinstance(code, (int, float)) and code < 0:
        # A child that was killed reports the negative of its signal.
        kind = OUTCOME_SIGNALED

    if kind is None:
        return HarnessOutcome(
            kind=OUTCOME_UNKNOWN, message=_unknown_message(code, backend, envelope)
        )

    if kind == OUTCOME_OK and failed_result:
        return HarnessOutcome(
            kind=OUTCOME_MODEL_ERROR,
            message=(
                f"{backend} exited with code {code} but its result event "
                f"reported is_error=true"
            ),
        )

    return HarnessOutcome(kind=kind, message=_outcome_message(code, kind, backend))


def _signal_name(code: int) -> str:
    """The signal an exit code carries.

    Args:
        code: The exit code, signalled or killed.

    Returns:
        The signal's name, or the number it carries when it is not one
        this platform knows.
    """
    name = SIGNAL_NAMES.get(code)
    if name is not None:
        return name

    number = abs(code) if code < 0 else code - 128
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _outcome_message(code: int, kind: str, backend: str) -> str:
    """The message that explains a verdict."""
    if kind == OUTCOME_SIGNALED:
        return f"{backend} terminated by {_signal_name(code)} (exit code {code})"
    return f"{backend} exited with code {code}"


def _unknown_message(
    code: int, backend: str, envelope: Optional[Mapping[str, Any]]
) -> str:
    """The message that explains an unmapped exit code.

    The envelope is quoted because it is the only thing left that says
    what the run did: the exit code is one nothing describes.
    """
    message = f"{backend} exited with unmapped code {code}"
    if envelope is None:
        return message
    return (
        f"{message}; result envelope: "
        f"{json.dumps(envelope, ensure_ascii=False, sort_keys=True)}"
    )
