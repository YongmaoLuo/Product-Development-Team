"""The scheduler's settings — one immutable snapshot, reloaded on change.

The scheduler reads its settings from the ``scheduler:`` section of a
YAML configuration file.  This module turns that section into an
immutable :class:`SchedulerConfig` snapshot and hands it out through
:class:`SchedulerConfigLoader`.

Three rules shape everything below.

**The snapshot is replaced whole.**  A reader that holds a snapshot holds
a consistent view of every setting: there is no moment at which
``capacity`` has been reloaded and ``min_runtime_seconds`` has not.  The
snapshot is therefore a frozen dataclass, and the loader swaps the
reference to it — never the fields inside it.

**An unusable file never reaches the scheduler.**  A file that cannot be
read, is not valid YAML, or carries a value the scheduler cannot use
(negative, NaN, not a number) leaves the previous snapshot in place.
The loader logs what it refused and keeps going: a scheduler that stops
because somebody half-edited a config file is worse than one that runs
on the settings it already had.

**Absent means default.**  A missing ``scheduler`` section, a missing key
inside it, or a file that does not exist at all means "use the default"
— not "fail".  The defaults are the ones the architecture's decision
table pins, and they are what the scheduler runs with when nobody has
said otherwise.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import yaml

log = logging.getLogger(__name__)

#: The YAML section the scheduler's settings are read from.
SECTION = "scheduler"

#: The defaults.  These are the architecture's decision table: what the
#: scheduler runs with when the configuration file says nothing.
#:
#: ``enabled`` is off among them, and that is the whole point of the
#: number: taking the ``scheduler`` section away is how the scheduler is
#: rolled back, so a section that is taken away must not start it.  A
#: scheduler that runs only when somebody has said so cannot come up
#: next to the old cron by accident.
DEFAULT_ENABLED = False
DEFAULT_DRY_RUN = False
DEFAULT_CAPACITY = 4
DEFAULT_MIN_RUNTIME_SECONDS = 600.0
DEFAULT_TICK_SECONDS = 5.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class SchedulerConfig:
    """The scheduler's settings, as one immutable snapshot.

    Attributes:
        enabled: whether the scheduler may run at all.
        dry_run: run the pipeline but change nothing.
        capacity: how many plans may run at the same time.
        min_runtime_seconds: how long a young run is left alone before
            it may be converged.
        tick_seconds: how long one tick waits before the next one.
        command_timeout_seconds: how long a single command may take.
    """

    enabled: bool = DEFAULT_ENABLED
    dry_run: bool = DEFAULT_DRY_RUN
    capacity: int = DEFAULT_CAPACITY
    min_runtime_seconds: float = DEFAULT_MIN_RUNTIME_SECONDS
    tick_seconds: float = DEFAULT_TICK_SECONDS
    command_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------

#: What the coercion helpers return for a value that cannot be used.
#: ``None`` means "not set" (the default applies); this sentinel means
#: "this value is junk" (the whole snapshot is refused).
_INVALID = object()

#: The words a boolean may be spelled as.
_TRUE_WORDS = frozenset({"true", "yes", "on", "1"})
_FALSE_WORDS = frozenset({"false", "no", "off", "0"})


def _coerce_bool(value: Any) -> Any:
    """Return a ``bool``, ``None`` for "not set", ``_INVALID`` for junk."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return _INVALID


def _coerce_int(value: Any) -> Any:
    """Return an ``int``, ``None`` for "not set", ``_INVALID`` for junk.

    A string that spells a number is a number: ``"4"`` is 4.  A value
    that is not whole (``4.5``) is not an integer and is refused.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # ``True`` is an ``int`` in Python, but a flag is not a count.
        return _INVALID
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        return _INVALID
    if isinstance(value, str):
        word = value.strip()
        if not word:
            return _INVALID
        try:
            return int(word)
        except ValueError:
            pass
        try:
            number = float(word)
        except ValueError:
            return _INVALID
        if not math.isfinite(number) or not number.is_integer():
            return _INVALID
        return int(number)
    return _INVALID


def _coerce_float(value: Any) -> Any:
    """Return a ``float``, ``None`` for "not set", ``_INVALID`` for junk.

    NaN and the infinities are junk here: a duration that is not a
    number is a duration the scheduler cannot wait for.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return _INVALID
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else _INVALID
    if isinstance(value, str):
        word = value.strip()
        if not word:
            return _INVALID
        try:
            number = float(word)
        except ValueError:
            return _INVALID
        return number if math.isfinite(number) else _INVALID
    return _INVALID


#: The settings, in the order the snapshot declares them, with the
#: coercion each one uses and the smallest value it may hold.  A setting
#: whose smallest value is ``None`` is not a number and has no range.
#:
#: The bound is zero, not one: a capacity of zero is a coherent answer
#: ("run nothing"), a negative capacity is not.  The same holds for the
#: durations — zero is a legal wait, a negative one is not a duration at
#: all.
_SETTINGS: Tuple[Tuple[str, Callable[[Any], Any], Optional[float]], ...] = (
    ("enabled", _coerce_bool, None),
    ("dry_run", _coerce_bool, None),
    ("capacity", _coerce_int, 0.0),
    ("min_runtime_seconds", _coerce_float, 0.0),
    ("tick_seconds", _coerce_float, 0.0),
    ("command_timeout_seconds", _coerce_float, 0.0),
)


def _build_snapshot(section: Any) -> Optional[SchedulerConfig]:
    """Turn a ``scheduler`` section into a snapshot, or ``None`` if unusable.

    ``None`` means "this section cannot be used" — the caller keeps the
    snapshot it already has.  An absent section is not that case: it
    means "every setting takes its default", which is what an empty
    snapshot already is.  A section that is present but is not a mapping
    is refused rather than read as absent: somebody wrote something
    there, it is not settings, and quietly running on the defaults
    would hide the mistake.

    The section is read as a whole.  One unusable value refuses the
    whole section rather than one field of it, because a snapshot that
    mixes a fresh value with a stale one is the state this module
    exists to make impossible.
    """
    if section is None:
        section = {}
    if not isinstance(section, dict):
        return None

    values: Dict[str, Any] = {}
    for name, coerce, minimum in _SETTINGS:
        coerced = coerce(section.get(name))
        if coerced is _INVALID:
            return None
        if coerced is None:
            # Not set — the snapshot's own default applies.
            continue
        if minimum is not None and coerced < minimum:
            return None
        values[name] = coerced
    return SchedulerConfig(**values)


class SchedulerConfigLoader:
    """Read the scheduler's settings from a YAML file, reloading on change.

    The loader watches the file's ``(mtime_ns, size)`` fingerprint — the
    same signal the provider routing and capacity readers use — and
    re-reads only when that fingerprint moves.  A file that cannot be
    used leaves the previous snapshot in place; the loader never raises
    at its caller.

    The loader is safe to call from more than one thread: the snapshot
    is swapped under a lock, and a caller always receives a snapshot
    that was complete when it was handed over.
    """

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._snapshot: SchedulerConfig = SchedulerConfig()
        self._fingerprint: Optional[Tuple[int, int]] = None
        self._seen = False

    @property
    def path(self) -> Path:
        """The file this loader reads."""
        return self._path

    def current(self) -> SchedulerConfig:
        """Return the current snapshot, reloading it if the file changed."""
        with self._lock:
            fingerprint = self._fingerprint_of()
            if self._seen and fingerprint == self._fingerprint:
                return self._snapshot

            # Remember what was looked at before reading it: a file that
            # turns out to be broken is not re-read on every call, and a
            # later fix moves the fingerprint and is picked up.
            self._seen = True
            self._fingerprint = fingerprint

            snapshot = self._read_snapshot()
            if snapshot is not None:
                # Whole-snapshot replacement.  The new snapshot carries
                # every setting; there is no field-by-field merge, so a
                # reader can never see a new capacity next to an old
                # minimum runtime.
                self._snapshot = snapshot
            return self._snapshot

    # -- internals ---------------------------------------------------------

    def _fingerprint_of(self) -> Optional[Tuple[int, int]]:
        """The file's ``(mtime_ns, size)``, or ``None`` when it is not there."""
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _read_snapshot(self) -> Optional[SchedulerConfig]:
        """Read the file into a new snapshot, or ``None`` when it cannot be used.

        ``None`` covers every way the file can be unusable: unreadable,
        not YAML, a ``scheduler`` section that is not a mapping, or a
        value the scheduler cannot act on.  The caller keeps the
        snapshot it already had.
        """
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            log.warning(
                "scheduler config at %s is unreadable (%s); keeping the previous snapshot",
                self._path,
                exc,
            )
            return None

        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            log.warning(
                "scheduler config at %s is not valid yaml (%s); keeping the previous snapshot",
                self._path,
                exc,
            )
            return None

        section = document.get(SECTION) if isinstance(document, dict) else None
        snapshot = _build_snapshot(section)
        if snapshot is None:
            log.warning(
                "scheduler config at %s has an unusable %r section; keeping the previous snapshot",
                self._path,
                SECTION,
            )
        return snapshot