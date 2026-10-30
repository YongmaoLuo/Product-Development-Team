"""Read the tail of an execution log and normalise its lines.

The status endpoint answers "what is this plan doing right now?" and
the answer it could give was empty: the in-process log buffer is
created on start and never written to, so every process that did not
start the run — a fresh boot, a second process, a plan whose directory
the boot scan never walked — answered with an empty list while the
activity sat on disk in ``plans/{plan_id}/execution.log``.

This module is the reading half of that repair, and it is deliberately
stateless: no registry, no database, no server. It takes a path and
gives back lines. The route decides whether to use it; the route also
decides what to do when it returns nothing.

Two things are normalised here
------------------------------
**The timestamp.** The log writer stamps entries with
``datetime.utcnow().isoformat()`` — a naive string that means UTC by
convention, not by declaration. The console printer writes the same
moment with the same convention. A reader that took those strings at
face value would compare "the same instant" as two different moments,
and the offset would be wrong by whatever the writer's local zone
happened to be. Every line therefore leaves this module as
``[<ISO-8601 with an explicit UTC offset>] <body>``, and a naive
timestamp is read as UTC — the convention the writer used.

**The shape.** A line from disk and a line from the process buffer
have to be indistinguishable to whoever reads them, so both are
rendered the same way: a bracket with the UTC stamp, then the body.

What the two limits are for
----------------------------
``MAX_TAIL_LINES`` bounds how many lines come back and
``MAX_TAIL_BYTES`` bounds how much of the file is read to find them.
The reader seeks backwards from the end of the file, so the cost is
the same whether the log is a kilobyte or a gigabyte — a long run's
log grows without bound and this is read on a path that renders a
card.

The two limits are not independent, and the byte cap wins when they
disagree: a log whose lines are long enough that fewer than
``MAX_TAIL_LINES`` of them fit inside ``MAX_TAIL_BYTES`` yields fewer
lines. The cap is a bound on the work done, not a promise about how
many lines fit inside it.

Nothing here raises. A log that is missing, empty, unreadable or not
a file at all is an empty list: the endpoint that renders a card must
not fail because a file is absent, and "there is no log" and "the log
could not be read" are the same answer to the question this module
exists to answer.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: How many lines the tail reader returns by default.
MAX_TAIL_LINES = 200

#: How many bytes are read from the end of the log file by default.
#: 256 KiB — enough for the tail of any run that is still interesting,
#: small enough that a megabyte-long log is never read whole.
MAX_TAIL_BYTES = 262144


#: A timestamp in any of the shapes the two writers produce: the ISO
#: form with a ``T`` separator, the space-separated form, an optional
#: fractional part, and an optional explicit offset (``Z`` or
#: ``+HH:MM``). The offset is optional because the writer omits it —
#: its ``utcnow()`` stamp is naive.
_TIMESTAMP_RE = re.compile(
    r"""^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})
        [ T]
        (?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?
        (?:[.,](?P<fraction>\d+))?
        (?P<offset>Z|z|[+-]\d{2}:?\d{2})?$""",
    re.VERBOSE,
)

#: A line that opens with a bracketed stamp — the console printer's
#: ``[2026-10-28T10:35:13] [INFO] event: message``. ``DOTALL`` because a
#: body may carry a line break of its own: the stamp in front of it is
#: still a stamp, and the break is folded into a space later.
_BRACKETED_STAMP_RE = re.compile(
    r"^\[(?P<stamp>[^\[\]]+)\]\s*(?P<body>.*)$", re.DOTALL
)

#: A line that opens with a bare stamp and a space.
_BARE_STAMP_RE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?"
    r"(?:[.,]\d+)?(?:Z|z|[+-]\d{2}:?\d{2})?)\s+(?P<body>.*)$",
    re.DOTALL,
)


def _parse_timestamp(raw: Any) -> Optional[datetime]:
    """Return the UTC datetime ``raw`` names, or ``None`` if it names none.

    A timestamp without an offset is read as UTC — that is the
    convention the log writer used when it stamped the line, and
    reading it as local time would move every timestamp by the
    writer's zone. A timestamp with an offset is converted to UTC, so
    the answer is one instant expressed one way.
    """
    if not isinstance(raw, str):
        return None
    match = _TIMESTAMP_RE.match(raw.strip())
    if match is None:
        return None

    fraction = match.group("fraction") or ""
    microsecond = int((fraction + "000000")[:6]) if fraction else 0
    try:
        moment = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second") or 0),
            microsecond,
        )
    except ValueError:
        # A well-shaped stamp that names a day that does not exist
        # ("2026-02-30") is not a timestamp.
        return None

    offset = match.group("offset")
    if offset is None:
        return moment.replace(tzinfo=timezone.utc)
    if offset in ("Z", "z"):
        return moment.replace(tzinfo=timezone.utc)
    digits = offset[1:].replace(":", "")
    delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
    if offset[0] == "-":
        delta = -delta
    return moment.replace(tzinfo=timezone(delta)).astimezone(timezone.utc)


def _flatten(text: str) -> str:
    """Fold every line break in ``text`` into a space.

    One line in, one line out: the caller parses the result line by
    line, so a body that carries a newline would split into two lines
    and the second one would look like a line of its own.
    """
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _text(value: Any) -> str:
    """Render a log field as text; a missing field is an empty string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _entry_body(entry: Dict[str, Any]) -> str:
    """Render a JSON log entry's body.

    The body is made of the three fields the entry carries for it —
    level, event and message — each read with a default of the empty
    string: a line that omits one of them is still a line, and a
    missing key is not a reason to drop the whole entry.

    The other fields an entry may carry (``task_id``, ``phase``,
    ``data``) are not part of the body. The body is the shape the
    console printer writes for an entry that has no task of its own,
    and the fields it leaves out stay in the log file, where a reader
    of the file can still see them.
    """
    level = _text(entry.get("level"))
    event = _text(entry.get("event"))
    message = _text(entry.get("message"))
    return f"[{level}] {event}: {message}"


def _compose(stamp: Optional[str], body: str) -> str:
    """Join a normalised stamp and a body into one line."""
    text = _flatten(body).strip()
    if not stamp:
        return text
    return f"[{stamp}] {text}"


def _parse_entry(text: str) -> Optional[Dict[str, Any]]:
    """Return the JSON object ``text`` holds, or ``None`` if it holds none."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def normalize_log_line(raw: str) -> str:
    """Normalise one log line into ``[<UTC stamp>] <body>``.

    The line may come from the log file (a JSON object) or from the
    process buffer (a console line), and the two are rendered the same
    way. A line that carries no timestamp at all is kept as it is
    rather than dropped: a traceback is evidence of what happened, and
    dropping it would hide the very thing the reader opened the log to
    find.
    """
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    text = text.rstrip("\r\n")
    if not text.strip():
        return ""

    entry = _parse_entry(text)
    if entry is not None:
        moment = _parse_timestamp(entry.get("ts"))
        stamp = moment.isoformat() if moment is not None else None
        return _compose(stamp, _entry_body(entry))

    for pattern in (_BRACKETED_STAMP_RE, _BARE_STAMP_RE):
        match = pattern.match(text)
        if match is None:
            continue
        moment = _parse_timestamp(match.group("stamp"))
        if moment is None:
            # The bracket held something that is not a timestamp
            # ("[INFO] ..."), so this is not a stamped line.
            continue
        return _compose(moment.isoformat(), match.group("body"))

    return _flatten(text)


def normalize_status_logs(inline: Sequence[Any]) -> List[str]:
    """Normalise the lines the endpoint already holds.

    The in-process lines are the freshest view of the run, so they are
    what the answer is made of; what this function owes them is the
    same line shape as the disk fallback, so a reader cannot tell
    which source a line came from. Entries may be strings or JSON
    objects, and neither form raises.
    """
    if not inline:
        return []

    normalised: List[str] = []
    for item in inline:
        if item is None:
            continue
        if isinstance(item, dict):
            moment = _parse_timestamp(item.get("ts"))
            stamp = moment.isoformat() if moment is not None else None
            text = _compose(stamp, _entry_body(item))
        else:
            text = normalize_log_line(item if isinstance(item, str) else str(item))
        if text:
            normalised.append(text)
    return normalised


def read_tail_lines(
    log_path: Path,
    max_lines: int = MAX_TAIL_LINES,
    max_bytes: int = MAX_TAIL_BYTES,
) -> List[str]:
    """Return the last lines of the log file at ``log_path``.

    The read starts at ``size - max_bytes`` and walks forward, so the
    cost does not grow with the log. When the read starts inside the
    file, the first fragment is the tail of a line that was cut in
    half — it is dropped, because a half-line is not a line and the
    caller parses one line at a time.

    A log that is missing, empty, unreadable or not a file yields an
    empty list. This function never raises: the endpoint that reads it
    renders a card, and a card must still render when the log is gone.
    """
    try:
        if max_lines <= 0 or max_bytes <= 0:
            return []
    except TypeError:
        # A limit that is not a number is not a limit this reader can
        # honour, and it is not worth failing the caller over.
        return []

    try:
        path = Path(log_path)
    except TypeError:
        return []

    try:
        size = path.stat().st_size
    except (OSError, ValueError):
        return []
    if size <= 0:
        return []

    start = max(0, size - max_bytes)
    try:
        with open(path, "rb") as handle:
            if start:
                handle.seek(start)
            blob = handle.read(max_bytes)
    except OSError:
        return []

    lines = blob.decode("utf-8", errors="replace").split("\n")
    if start:
        # The seek landed inside a line: what is first is the rest of
        # that line, not a line of its own.
        lines = lines[1:]

    kept = [line.rstrip("\r") for line in lines]
    kept = [line for line in kept if line.strip()]
    return kept[-max_lines:]


def read_status_log_tail(
    log_path: Path,
    *,
    max_lines: int = MAX_TAIL_LINES,
    max_bytes: int = MAX_TAIL_BYTES,
) -> List[str]:
    """Return the tail of the log file at ``log_path``, normalised.

    This is the disk fallback for the endpoint's ``logs`` field: the
    lines the process holds win, and this is what is used when it
    holds none. An absent or unreadable log yields an empty list.
    """
    lines = read_tail_lines(log_path, max_lines=max_lines, max_bytes=max_bytes)
    return [text for text in (normalize_log_line(line) for line in lines) if text]
