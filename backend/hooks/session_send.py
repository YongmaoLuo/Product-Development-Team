"""The message a HITL decision is delivered as.

What lives here
---------------
:mod:`hooks.dispatcher` decides *whether* a destination gets the event;
this module decides *what the destination is told*.  The event is JSON
— a plan id, a decision id, a question, the answers on the table, the
answer that will be used if nobody replies, a deadline — and the thing
on the other end of a ``cc_connect`` hook is a person reading a message.
JSON is not a message: nobody answers a decision by reading a payload.

:func:`format_message` is the translation, and it is deliberately
pure: one function from an event to a string.  No clock, no
environment, no filesystem, no network.  That is what makes it
reusable — the delivery CLI at the bottom of this module reads the
event from stdin and hands it to ``cc-connect send``, and it formats
it with this function and nothing else, so the wording of the message
is decided in one place and tested without a transport.

The delivery entry point
------------------------

The CLI is the other half of the module, and it is where the message
leaves the building:

.. code-block:: bash

    python -m backend.hooks.session_send --project grp-1 --session s-1 < event.json

It reads the event JSON from stdin, formats it with
:func:`format_message`, and runs ``cc-connect send -p <project> -s
<session> --stdin`` with the message on the child's stdin.  The
message travels down a pipe rather than in an argument because a
decision message is several lines long and contains characters a shell
would be glad to reinterpret.

Failure is the interesting part.  The dispatcher decides whether a
destination was reached by reading the exit code of this command, so
every way of failing has to *be* a failure: a transport that exits
non-zero has its code passed through unchanged, a transport that is
not installed exits non-zero with a line on stderr, and a call that
is malformed — missing arguments, or an event that is not JSON — is
rejected before anything is spawned at all.  Returning zero when
nothing was delivered would tell the dispatcher that a human was
asked, when nobody was asked anything.

The shape of the message
------------------------
::

    **⏳ HITL 决策待处理** · `p1`

    > 是否合入？

    - **决策 ID**：`d1`
    - **选项**：1) accept 2) revise
    - **默认答案**：accept（超时未答时采用）
    - **截止时间**：2026-10-26T12:00:00Z

The message is Markdown, and that is not decoration.  ``cc-connect``
looks at the content it was handed and decides how to render it: a
message with no Markdown markers in it arrives as a plain text bubble,
and a message with them arrives as an interactive card — Feishu schema
2.0, where the title is bold, the question is a quote and the fields
are a list.  The markers are therefore part of the contract with the
reader: a decision that arrives as a wall of plain text is a decision
nobody reads carefully.

Six fields, six places the reader can look: what is being asked, about
what, the answers on the table, the answer that happens by default,
and how long there is to reply.  The options are numbered because the
reply names one — ``1`` means the first option, and the numbering has
to be stable enough for a person to use it.

The message also has to read as plain text, because somebody copies it
somewhere else — another chat, a ticket, a quoting reply — and the
Markdown markers are gone with it.  What is left has to still be
sentences a person can act on, which is why the labels are words and
the question is a sentence of its own.

Degradation is the default, never an exception
----------------------------------------------
The event is somebody else's data: it is built by
:func:`hitl.service._event_payload` and read here.  A field that is
absent, empty, or of an unexpected shape shows :data:`PLACEHOLDER` on
its own line, and an event that is not a mapping at all produces the
same message with every field missing.

That is the whole reason this function cannot raise.  It is called on
the delivery path, and a delivery that raises is a delivery that never
happens: the decision it was carrying would be settled as
``unroutable`` and no human would ever be asked.  A message with
placeholders in it is a bad message; it is still a message, and a bad
message is visible in a way that a lost decision is not.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from typing import Any, List, Optional

#: What a field shows when the event does not carry it.  It is
#: deliberately visible text rather than an empty string or a dash: a
#: blank line and a missing value look identical on a phone screen, and
#: the reader needs to be able to tell "not provided" from "provided
#: and empty".
PLACEHOLDER = "（未提供）"

#: The header: the title of the message, and the plan the decision
#: belongs to.  It opens every message so a reader in a chat group can
#: tell what the message is before they read the question.  The title
#: is bold and the plan id is code — the markers are what make
#: ``cc-connect`` render the message as a card instead of a plain text
#: bubble, and the two of them are what a reader's eye lands on first.
_HEADER_LINE = "**⏳ HITL 决策待处理** · `{plan_id}`"

#: The question, as a quote.  It is the one part of the message that is
#: a sentence rather than a label and a value, and a quote is what
#: separates it from the fields below.
_QUESTION_LINE = "> {question}"

#: The fields, as a list.  Each label is bold so that a reader can find
#: the value without reading the line, and the identifiers are code so
#: that a long id is not mistaken for a word.
_DECISION_LINE = "- **决策 ID**：`{decision_id}`"
_OPTIONS_LINE = "- **选项**：{options}"
_DEFAULT_LINE = "- **默认答案**：{default_option}"
_DEADLINE_LINE = "- **截止时间**：{deadline_at}"

#: What the default answer is for, said next to it.  It is only there
#: when there is a default answer to explain: a line that reads
#: "（未提供）（超时未答时采用）" says nothing twice.
_DEFAULT_NOTE = "（超时未答时采用）"


def format_message(event: Mapping[str, Any]) -> str:
    """The message that carries ``event`` to a human.

    ``event`` is the decision event as JSON: ``plan_id``,
    ``decision_id``, ``question``, ``options``, ``default_option`` and
    ``deadline_at``.  Any of them may be missing.

    The message is Markdown.  It has to be: ``cc-connect`` renders a
    message that carries no Markdown markers as a plain text bubble,
    and one that carries them as an interactive card, and the card is
    what makes a decision readable in a busy chat group.

    The function never raises.  A field that is absent, empty or of an
    unexpected shape shows :data:`PLACEHOLDER` on its own line; an
    event that is not a mapping produces the same message with every
    field missing.  The reason is the delivery path: this function is
    called while a decision is being handed to a destination, and a
    raised exception there turns the decision into one nobody was
    asked about.

    Parameters
    ----------
    event:
        The decision event.  Only a mapping is read; anything else is
        treated as an event with no fields at all.

    Returns
    -------
    str
        The Markdown message: a title line, the question as a quote,
        and one list item per field.
    """
    fields = event if isinstance(event, Mapping) else {}

    plan_id = _text(fields.get("plan_id"))
    decision_id = _text(fields.get("decision_id"))
    question = _text(fields.get("question"))
    options = _options(fields.get("options"))
    default_option = _text(fields.get("default_option"))
    deadline_at = _text(fields.get("deadline_at"))

    default_line = _DEFAULT_LINE.format(default_option=default_option)
    if default_option != PLACEHOLDER:
        default_line += _DEFAULT_NOTE

    return "\n".join(
        [
            _HEADER_LINE.format(plan_id=plan_id),
            "",
            _QUESTION_LINE.format(question=question),
            "",
            _DECISION_LINE.format(decision_id=decision_id),
            _OPTIONS_LINE.format(options=options),
            default_line,
            _DEADLINE_LINE.format(deadline_at=deadline_at),
        ]
    )


def _text(value: Any) -> str:
    """``value`` as the text a reader sees, or :data:`PLACEHOLDER`.

    A missing value (``None``), an empty string and a whitespace-only
    string are all "not provided" — the difference between an empty
    string and an absent key is not one a reader can act on.  Any
    other value is shown as its string form: the event is JSON, so a
    field that is a number is unusual but not impossible, and dropping
    it would lose a fact rather than tidy up a message.
    """
    if value is None:
        return PLACEHOLDER
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text if text else PLACEHOLDER


def _options(value: Any) -> str:
    """The answers on the table, numbered.

    Only a list is a set of answers.  A string is one answer written
    out, and enumerating it would produce one numbered line per
    character — so anything that is not a list is treated as no
    answers at all.  An empty list is the same thing.

    The numbering is positional and stays positional: an empty entry
    keeps its number and shows the placeholder, so the number a person
    replies with still refers to the same answer the decision stored.
    """
    if not isinstance(value, list) or not value:
        return PLACEHOLDER

    return " ".join(
        f"{index}) {_text(option)}"
        for index, option in enumerate(value, start=1)
    )


# ---------------------------------------------------------------------------
# The delivery CLI
# ---------------------------------------------------------------------------

#: The environment variable that pins which ``cc-connect`` executable
#: to run.  It is an override, not a requirement: with it unset the
#: executable is looked up on ``PATH`` under its own name.
BIN_ENV_VAR = "PDT_CC_CONNECT_BIN"

#: The exit code for a call that is wrong — a missing argument, an
#: event that is not JSON.  Two is the conventional code for a usage
#: error, and it is the one :mod:`argparse` uses for the same thing.
EXIT_USAGE = 2

#: The exit code for "the transport is not there".  It is a different
#: number from :data:`EXIT_USAGE` so that the two are told apart by
#: whatever reads the code: a broken caller and a missing program are
#: different problems with different fixes.
EXIT_NO_BINARY = 3


class _UsageError(Exception):
    """A command line that cannot be used.

    :mod:`argparse` reports a bad command line by exiting the
    process.  This CLI has to *return* an exit code instead — the
    dispatcher reads it — so the parser raises and :func:`main`
    catches.
    """


class _Parser(argparse.ArgumentParser):
    """An argument parser that raises instead of exiting the process."""

    def error(self, message: str) -> None:
        raise _UsageError(message)


def resolve_binary() -> Optional[str]:
    """The ``cc-connect`` executable to run, or ``None`` if there is none.

    There are two ways to name it.  :data:`BIN_ENV_VAR` pins one, and
    the environment variable wins over everything else: an operator who
    pinned an executable meant that executable, and a machine with two
    versions of ``cc-connect`` on it should deliver to the one that
    was chosen.  Without the variable, the executable is looked up on
    ``PATH`` under its own name.

    A pinned value that is a path has to exist.  A path to a program
    that is not there is a configuration error, and falling back to
    whatever happens to be on ``PATH`` would send the message
    somewhere the operator did not choose.  A pinned value that is
    only a name — no path separator in it — is looked up on ``PATH``
    like any other command.

    Returns
    -------
    Optional[str]
        The executable to run, or ``None`` when there is none.  The
        caller turns ``None`` into a non-zero exit; it never becomes a
        silent success.
    """
    configured = os.environ.get(BIN_ENV_VAR, "").strip()

    if configured:
        if os.sep in configured or (os.altsep and os.altsep in configured):
            return configured if os.path.isfile(configured) else None
        return shutil.which(configured)

    return shutil.which("cc-connect")


def build_command(binary: str, project: str, session: str) -> List[str]:
    """The argument list that sends one message to one destination.

    ``cc-connect send`` names the project (``-p``) and the session
    (``-s``) as options and reads the message itself from stdin
    (``--stdin``).

    The result is a list rather than a command string, and the caller
    runs it without a shell: nothing in it is re-parsed, so a project
    or session name containing a space or a quote is still one
    argument.
    """
    return [binary, "send", "-p", project, "-s", session, "--stdin"]


def main(
    argv: Optional[List[str]] = None, stdin: Optional[Any] = None
) -> int:
    """Deliver one decision event, and report whether it was delivered.

    ``argv`` is the command line without the program name and defaults
    to ``sys.argv[1:]``.  ``stdin`` is the stream the event JSON is
    read from and defaults to ``sys.stdin``.  Both are parameters so
    that the CLI can be driven without a subprocess, and so that a
    test can hand it a stream it made.

    The order of the checks is the order of what can be wrong.  A call
    that names no destination, or that carries an event that is not
    JSON, is rejected before anything is spawned — half a message
    delivered to a real group is worse than no message, because it
    looks like somebody was asked.  A transport that is not installed
    is the next failure, and it is reported on stderr so that the
    reason is visible next to the exit code.  Everything after that is
    the transport's own answer, and its exit code is this function's
    exit code.

    Returns
    -------
    int
        Zero when the transport delivered the message; the transport's
        own exit code when it did not; :data:`EXIT_USAGE` for a
        malformed call; :data:`EXIT_NO_BINARY` when there is no
        transport to run.
    """
    parser = _build_parser()

    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    except _UsageError as exc:
        _error(str(exc))
        return EXIT_USAGE
    except SystemExit as exc:  # --help, and --version if it is ever added
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    try:
        event = json.loads(_read_stdin(stdin))
    except ValueError:
        _error("stdin is not valid JSON")
        return EXIT_USAGE

    binary = resolve_binary()
    if binary is None:
        _error(
            "cc-connect not found: set {0} to the executable, "
            "or put cc-connect on PATH".format(BIN_ENV_VAR)
        )
        return EXIT_NO_BINARY

    command = build_command(binary, args.project, args.session)
    message = format_message(event)

    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
    except OSError as exc:
        _error("cannot run cc-connect: {0}".format(exc))
        return EXIT_NO_BINARY

    try:
        process.communicate(input=message.encode("utf-8"))
    except OSError:
        # The child closed its stdin before the whole message was
        # written.  Its exit code is still the answer to "was it
        # delivered?", so it is read rather than invented.
        pass

    if process.returncode is None:
        process.wait()
    return int(process.returncode)


def _build_parser() -> argparse.ArgumentParser:
    """The command line this CLI accepts."""
    parser = _Parser(
        prog="session_send",
        description="Deliver a HITL decision event with cc-connect.",
    )
    parser.add_argument(
        "--project", required=True, help="the cc-connect project to send to"
    )
    parser.add_argument(
        "--session", required=True, help="the cc-connect session key to send to"
    )
    return parser


def _read_stdin(stream: Optional[Any]) -> str:
    """The event JSON, read from ``stream``.

    ``None`` means the process's own stdin.  A stream that is already
    text is read as it is; a stream of bytes is decoded as UTF-8 with
    replacement, because a byte sequence that is not UTF-8 is a
    malformed event, and a malformed event is reported as one rather
    than as a crash.
    """
    if stream is None:
        stream = sys.stdin

    if isinstance(stream, (str, bytes)):
        data: Any = stream
    else:
        buffer = getattr(stream, "buffer", None)
        try:
            data = buffer.read() if buffer is not None else stream.read()
        except (OSError, ValueError):
            data = b""

    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data or ""


def _error(message: str) -> None:
    """Write one line of diagnostics to stderr."""
    sys.stderr.write("session_send: {0}\n".format(message))
    sys.stderr.flush()


if __name__ == "__main__":  # pragma: no cover - run as a subprocess
    sys.exit(main())
