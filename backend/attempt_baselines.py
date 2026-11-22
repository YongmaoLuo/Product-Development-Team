"""Persistent attempt baselines — the on-disk half of "record once, reuse".

Why this module exists
----------------------
A task's *attempt baseline* is the commit its attempt started from. It is
the thing that lets a later attempt say "these files are mine, that
commit is not".

If the baseline is re-read from "whatever HEAD is right now" on every
attempt, the second attempt of a task starts from a HEAD that already
contains the first attempt's committed work. The baseline has then
swallowed the previous attempt's work without anyone noticing: the
attempt can no longer tell what it actually changed, and a partial
rollback would roll back the wrong thing.

So the baseline has to be *recorded once* — on the first attempt — and
*reused* afterwards. This module is the file layer that makes that
possible: one JSON document per plan directory, written atomically and
read back by key. It knows nothing about executors, tasks or attempts;
it moves a string to disk and back, and that is the whole job — which is
also what makes it testable without a plan.

The document
------------
::

    {
      "version": 1,
      "lineages": {
        "repair": {              # the lineage root ("3", "repair")
          "repair-03": "a1b2c3" # the task id's own baseline
        }
      }
    }

The nesting is two levels deep on purpose. A lineage is a *group* of
tasks (the refiner's children keep the parent's id as a prefix, so
``"3-2"`` belongs to lineage ``"3"``), but the baseline belongs to the
**task**, not to the group: if the baseline were stored per lineage, the
sibling that finished first would be folded into the baseline of the
sibling that runs later, and the second one would again be comparing
itself against its own prior work.

Degradation is the default, never an exception
----------------------------------------------
Every read here answers ``""`` rather than raising, and every write
failure is swallowed:

* a missing, unreadable or corrupt document reads as ``""`` — "no
  baseline recorded yet" must always be a reachable answer, because
  the alternative is an attempt that cannot start;
* a document written by a newer schema version is treated as
  unreadable, and is **not** overwritten — refusing to write is the
  only way not to destroy a document this code does not understand;
* a write that fails returns the sha it was handed and raises nothing.
  Losing the baseline is bad; killing the attempt that was otherwise
  fine is worse.

The write is atomic (tempfile + ``os.replace``), so a crash mid-write
leaves the previous document intact rather than a half-written one that
the next attempt would have to make sense of.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple, Union

from utils.atomic_io import atomic_write_json

try:  # POSIX only. The lock is an optimisation, never a requirement.
    import fcntl
except ImportError:  # pragma: no cover - platforms without flock
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]

#: Schema version of the document. A document carrying any other version
#: belongs to a different (newer or older) writer and is not touched.
SCHEMA_VERSION = 1

#: Why a document could not be read. Only ``_FOREIGN_VERSION`` changes
#: what a write does; the other two mean "there is nothing usable here".
_ABSENT = "absent"
_UNREADABLE = "unreadable"
_FOREIGN_VERSION = "foreign_version"


def _new_document() -> Dict[str, Any]:
    """A fresh, empty document (a copy — the constant must not be shared)."""
    return {"version": SCHEMA_VERSION, "lineages": {}}


def _text(value: Any) -> str:
    """The value as the string the document stores.

    JSON object keys are strings, so a caller that hands over an int
    task id must still find its entry again after a round trip through
    the file. Coercing on the way in keeps ``put`` and ``get`` talking
    about the same key.
    """
    return value if isinstance(value, str) else str(value)


def _lookup(document: Dict[str, Any], lineage: str, task_id: str) -> str:
    """The recorded baseline for ``(lineage, task_id)``, or ``""``.

    Every step of the walk is checked: a document written by something
    else may have a ``lineages`` value that is not a mapping, or a
    bucket that is not a mapping, or a value that is not a string. None
    of those are worth an exception — they are all answers of ``""``.
    """
    lineages = document.get("lineages")
    if not isinstance(lineages, dict):
        return ""
    bucket = lineages.get(lineage)
    if not isinstance(bucket, dict):
        return ""
    value = bucket.get(task_id)
    if not isinstance(value, str) or not value:
        return ""
    return value


def _record(
    document: Dict[str, Any], lineage: str, task_id: str, sha: str
) -> None:
    """Write ``sha`` into ``document`` under ``(lineage, task_id)``."""
    lineages = document.get("lineages")
    if not isinstance(lineages, dict):
        lineages = {}
        document["lineages"] = lineages
    bucket = lineages.get(lineage)
    if not isinstance(bucket, dict):
        bucket = {}
        lineages[lineage] = bucket
    bucket[task_id] = sha


class BaselineStore:
    """Record-once baselines, keyed by lineage and task id.

    The store is a thin, durable key/value view over one JSON document.
    It holds no in-memory state between calls: every read goes to disk,
    so a store handed to another process (or another attempt, days
    later) sees the same thing.
    """

    def __init__(self, path: PathLike) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """Where the document lives."""
        return self._path

    def get(self, lineage: str, task_id: str) -> str:
        """The recorded baseline for ``(lineage, task_id)``, or ``""``.

        ``""`` covers every unusable case: the document is absent,
        unreadable, not JSON, or shaped in a way this code does not
        read. Callers therefore have one "no baseline yet" answer to
        handle and no exception to catch.
        """
        document, _reason = self._read()
        if document is None:
            return ""
        return _lookup(document, _text(lineage), _text(task_id))

    def put_if_absent(self, lineage: str, task_id: str, sha: str) -> str:
        """Record ``sha`` if this task has no baseline yet; return the baseline.

        The first call for a task records ``sha`` and returns it. Every
        later call returns the **first** recorded baseline and leaves the
        document alone — that is the whole contract: the baseline is
        pinned to the first attempt, so a later attempt cannot absorb
        the earlier attempt's work into its own starting point.

        A failed write is not an error: the passed ``sha`` is returned
        and nothing is raised. An empty ``sha`` is not a baseline and is
        therefore not recorded, so the next attempt can still record a
        real one.
        """
        value = _text(sha)
        lineage = _text(lineage)
        task_id = _text(task_id)

        with self._write_lock():
            document, reason = self._read()
            if document is None:
                if reason == _FOREIGN_VERSION:
                    # A document from another schema version. Writing
                    # our version over it would destroy whatever the
                    # other writer put there, so we do not write — and
                    # we hand back the sha we were given, exactly as a
                    # failed write would.
                    logger.warning(
                        "attempt_baselines_foreign_version: refusing to "
                        "write baseline document %s written by another "
                        "schema version",
                        self._path,
                    )
                    return value
                document = _new_document()

            existing = _lookup(document, lineage, task_id)
            if existing:
                return existing
            if not value:
                # Nothing worth recording.
                return value
            _record(document, lineage, task_id, value)
            self._write(document)
            return value

    # -- internals ---------------------------------------------------------

    def _read(self) -> Tuple[Optional[Dict[str, Any]], str]:
        """The stored document, plus why it is unusable when it is.

        ``(None, reason)`` means "there is nothing usable here";
        ``(document, "ok")`` means the document is readable.
        """
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None, _ABSENT
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            # ``ValueError`` covers ``json.JSONDecodeError``. A document
            # torn by a crash, or written by something else, costs a
            # baseline — and nothing else.
            logger.warning(
                "attempt_baselines_unreadable: ignoring unreadable "
                "baseline document %s (%s)",
                self._path, type(exc).__name__,
            )
            return None, _UNREADABLE

        if not isinstance(data, dict):
            return None, _UNREADABLE
        version = data.get("version")
        if version != SCHEMA_VERSION:
            logger.warning(
                "attempt_baselines_version_mismatch: ignoring baseline "
                "document %s with version %r",
                self._path, version,
            )
            return None, _FOREIGN_VERSION
        if not isinstance(data.get("lineages"), dict):
            return None, _UNREADABLE
        return data, "ok"

    def _write(self, document: Dict[str, Any]) -> bool:
        """Write the document atomically. ``False`` when it did not land."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "attempt_baselines_dir_uncreatable: cannot create %s (%s); "
                "the baseline will not be written",
                self._path.parent, type(exc).__name__,
            )
            return False
        try:
            atomic_write_json(self._path, document, logger=logger, reraise=True)
        except Exception as exc:  # noqa: BLE001 - a failed write never raises
            # The contract is that a write failure costs the baseline and
            # nothing else, so nothing from the write may escape. The
            # payload is JSON-shaped by construction, so this is belt
            # and braces rather than an expected path.
            logger.warning(
                "attempt_baselines_write_failed: cannot write baseline "
                "document %s (%s)",
                self._path, type(exc).__name__,
            )
            return False
        return True

    def _lock_path(self) -> Path:
        """The sidecar lock file guarding the read-modify-write."""
        return self._path.with_name(self._path.name + ".lock")

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        """Hold an exclusive lock over the read-modify-write.

        Sibling tasks of one plan are executed in parallel, so two
        stores can be recording two task ids of the same lineage at the
        same time. Without a lock the later write would drop the
        earlier one's entry — and the task that lost its entry would
        record a fresh (wrong) baseline on its next attempt.

        The lock is a sidecar ``<document>.lock`` file, because the
        document itself is replaced by ``os.replace`` and an ``flock``
        lives on the inode, not on the path. It is **best effort**: if
        the lock file cannot be created (read-only directory, missing
        parent) the write proceeds without it rather than failing —
        a store that cannot write is going to fail to write anyway,
        and the caller must not be told about it twice.
        """
        handle: Optional[int] = None
        if fcntl is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                handle = os.open(
                    str(self._lock_path()), os.O_CREAT | os.O_RDWR, 0o600,
                )
                fcntl.flock(handle, fcntl.LOCK_EX)
            except OSError as exc:
                logger.warning(
                    "attempt_baselines_lock_unavailable: proceeding without "
                    "the baseline lock for %s (%s)",
                    self._path, type(exc).__name__,
                )
                if handle is not None:
                    try:
                        os.close(handle)
                    except OSError:
                        pass
                    handle = None
        try:
            yield
        finally:
            if handle is not None:
                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(handle)
                except OSError:
                    pass
