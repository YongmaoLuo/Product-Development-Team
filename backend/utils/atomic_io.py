"""Shared atomic JSON I/O utilities.

This module centralises the tempfile + ``os.replace`` pattern that was
previously copy-pasted across the backend (``base_executor``,
``verification_executor``, ``server``, ``storage/state``, ``plan_state``).
A single implementation here means:

  * durability (fsync before rename) is consistent everywhere;
  * crash recovery never sees a torn JSON file;
  * no stray ``.tmp`` files are left on disk after failures;
  * the default behaviour swallows ``OSError`` so a transient disk
    failure does not crash a long-running scheduler mid-loop (the
    in-memory state stays consistent), while callers that need the
    exception can pass ``reraise=True``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]


def atomic_write_json(
    path: PathLike,
    payload: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    fsync: bool = True,
    reraise: bool = False,
    logger: Optional[logging.Logger] = None,
) -> None:
    """Atomically write ``payload`` as JSON to ``path``.

    Writes to a sibling tempfile, optionally ``fsync``s it, then
    ``os.replace``s it into place (POSIX-atomic). On any error the
    original file is left untouched and the tempfile is cleaned up.

    Args:
        path: Destination file path.
        payload: JSON-serialisable object.
        indent: ``json.dump`` indent level.
        ensure_ascii: ``json.dump`` ensure_ascii flag.
        fsync: If True, ``os.fsync`` the tempfile before the rename
            so a crash immediately after the rename does not lose
            the write.
        reraise: If True, surface ``OSError`` to the caller. If False
            (default), log and swallow so schedulers stay running.
        logger: Optional logger to record failures. If omitted, the
            utility's own logger is used. Callers that have a
            module-specific logger (e.g. ``verification_executor``)
            can pass it so failure records appear under their own
            namespace, preserving the historical log contract.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    log = logger if logger is not None else logging.getLogger(__name__)

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=indent, ensure_ascii=ensure_ascii)
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())
            os.replace(tmp_path, str(target))
        except BaseException:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise
    except OSError as exc:
        log.error(
            "atomic_write_json failed for %s: %s", target, exc, exc_info=True
        )
        if reraise:
            raise


def write_jsonl(
    path: PathLike,
    entry: Any,
    *,
    ensure_ascii: bool = False,
    reraise: bool = False,
) -> None:
    """Append one JSON object as a line to ``path``.

    Creates parent directories as needed. A transient ``OSError`` is
    logged and swallowed by default (a stuck log line must not crash
    the scheduler), but can be surfaced with ``reraise=True``.
    """
    target = Path(path)
    line = json.dumps(entry, ensure_ascii=ensure_ascii) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        logger.error("write_jsonl failed for %s: %s", target, exc, exc_info=True)
        if reraise:
            raise


def read_jsonl(path: PathLike) -> Iterator[Any]:
    """Yield each JSON object from a JSON-lines file.

    Blank lines are skipped. Lines that fail to parse are logged and
    skipped (a single corrupt line should not poison the whole read).
    """
    source = Path(path)
    if not source.exists():
        return
    with source.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("read_jsonl: skipping unparseable line in %s", source)
