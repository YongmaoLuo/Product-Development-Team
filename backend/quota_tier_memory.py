"""Which quota tiers a provider has ever reported.

Why this module exists
----------------------
CC Switch meters each provider and reports the windows it *currently*
carries.  A provider on a plan with a weekly limit reports a
``weekly_limit`` tier next to its ``five_hour`` burst window; a
provider on a metered API product reports no weekly tier at all.

The failover architecture (decision point 1) settles what a missing
tier means: **a missing weekly tier is treated as an exhausted weekly
tier** — but only for a provider that has *ever* been seen to have
one.  "This provider has no weekly window" and "this provider's weekly
window has gone missing" are different facts, and only history tells
them apart.

So the fact has to outlive the reading that produced it: the process
that observes a weekly tier is not the process that later needs to
know about it, and a reading is a snapshot rather than a record.  This
module is the file layer that carries that history across a restart.
It knows nothing about dispatch, quota readers or routes — it moves a
mapping of provider names to tier names to disk and back, which is the
whole job, and which is also what makes it testable without a
provider.

The document
------------
::

    {
      "MiniMax Token Plan Huawei": ["five_hour", "weekly_limit"]
    }

One key per provider (the CC Switch display name, spelled the way CC
Switch spells it), one list of tier names per provider, in the order
the tiers were first seen.  A flat map rather than a nested one: the
file holds exactly one kind of entry, and a reader can tell what it
holds by looking at it.

Deliberately **not** ``provider-order.json``.  That file is the
optimizer's product: it is rewritten wholesale by the optimizer, on the
optimizer's schedule, so a memory kept inside it would not survive the
next optimizer run — which is the one thing this module exists to
guarantee.  This file is written by this module and by nothing else.

Degradation is the default, never an exception
----------------------------------------------
Every read here answers "no history" rather than raising:

* no file (nothing was ever recorded) → empty memory;
* a truncated, non-JSON, or otherwise torn file → empty memory, logged;
* JSON that is not a provider map → empty memory, logged.

"Empty memory" is the same answer in all three cases, so a caller
needs no ``try``/``except`` and no branching.  That matters because
the caller is a dispatch path: a quota decision that raises because a
memory file is corrupt would fail a dispatch over a piece of
*history*, which is not evidence of anything.

Writes go through :func:`utils.atomic_io.atomic_write_json` (a
sibling tempfile plus ``os.replace``), so a process killed mid-write
leaves the previous document rather than a half-written one, and the
parent directory is created on demand.  A write that still fails is
logged and swallowed: the memory is an optimisation of a decision, and
losing it must not take down the dispatch that was succeeding.

The in-process copy
-------------------
Reads are served from a small cache keyed by the *resolved path* of the
memory file, because the path is resolved per call (the environment
override is the test seam) and two callers can point at two different
files.  The cache carries the file's ``(mtime, size)`` with it and
re-reads when that changes, so a tier recorded by *another* process is
picked up rather than answered from a stale snapshot.
:func:`reset_cache` drops it outright, which is what a test uses to
stand in for a restart.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

from config_paths import PROJECT_ROOT, resolve_config_dir
from utils.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

#: Environment variable that relocates the memory document.  Read on
#: every call (never cached), so a test that sets it after import is
#: still redirected at the file it asked for.
MEMORY_FILE_ENV: str = "PDT_QUOTA_TIER_MEMORY_FILE"

#: Filename of the memory document inside the configuration directory.
MEMORY_FILENAME: str = "quota-tier-memory.json"


class _CachedMemory(NamedTuple):
    """One cached read of a memory document.

    ``fingerprint`` is the file's ``(st_mtime_ns, st_size)`` at the
    time of the read, or ``None`` when the file was not there.  A file
    that changed underneath us has a different fingerprint, so the next
    read re-reads it instead of answering from a stale snapshot.
    """

    fingerprint: Optional[Tuple[int, int]]
    memory: Dict[str, Tuple[str, ...]]


#: The in-process cache, keyed by the resolved path of the memory file.
#:
#: Keyed by path rather than held as one global because the path is
#: resolved per call: a cache that ignored the path would answer one
#: file's question with another file's memory.
_CACHE: Dict[str, _CachedMemory] = {}


def memory_file_path() -> Path:
    """Where the tier memory lives.

    Precedence (first hit wins):

    1. ``PDT_QUOTA_TIER_MEMORY_FILE`` -- the operator/test seam.  An
       absolute path is used as given; a relative one is resolved
       against the repository root, the same rule
       :func:`config_paths.resolve_provider_routing_file` uses.
    2. ``<config dir>/quota-tier-memory.json``.

    Resolution is re-run on every call (never cached) so a test that
    sets the environment variable after import still gets the
    redirected file.
    """
    override = os.environ.get(MEMORY_FILE_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            candidate = (PROJECT_ROOT / candidate).resolve()
        return candidate
    return resolve_config_dir() / MEMORY_FILENAME


def reset_cache() -> None:
    """Forget the in-process copy of the memory document.

    The memory itself lives on disk, so this only drops what this
    process has read; the next read goes back to the file.  A test
    uses it to stand in for a restart, and a caller that has just
    written the file out of band uses it to force a fresh read.
    """
    _CACHE.clear()


def _fingerprint(path: Path) -> Optional[Tuple[int, int]]:
    """``(mtime_ns, size)`` of ``path``, or ``None`` when it is not there."""
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)


def _normalise_tiers(tiers: Iterable[str]) -> Tuple[str, ...]:
    """The tier names worth recording, in order and without duplicates.

    A name that is not a string, or that is blank once trimmed, is
    dropped rather than recorded: an empty tier name is not a tier, and
    recording it would put a fact on disk that no reader could use.
    """
    normalised: List[str] = []
    for tier in tiers or ():
        if not isinstance(tier, str):
            continue
        name = tier.strip()
        if not name or name in normalised:
            continue
        normalised.append(name)
    return tuple(normalised)


def _read_document(path: Path) -> Dict[str, Tuple[str, ...]]:
    """Read the memory document; an empty memory on any failure.

    Missing, torn and "not a provider map" all answer the same way --
    an empty mapping -- because "no history" is the one answer every
    caller can act on, and a raise here would fail a dispatch over a
    piece of history rather than over evidence.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # ``ValueError`` covers ``json.JSONDecodeError`` (and, being the
        # parent of ``UnicodeDecodeError``, a file that is not UTF-8).
        logger.warning(
            "quota_tier_memory_unreadable: ignoring unreadable %s (%s); "
            "the memory reads as empty",
            path, type(exc).__name__,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "quota_tier_memory_not_a_map: %s does not hold a provider map; "
            "the memory reads as empty",
            path,
        )
        return {}

    memory: Dict[str, Tuple[str, ...]] = {}
    for provider, tiers in data.items():
        if not isinstance(provider, str) or not provider:
            continue
        if isinstance(tiers, str):
            # A single tier written as a bare string is still one tier.
            tiers = [tiers]
        if not isinstance(tiers, (list, tuple)):
            continue
        cleaned = tuple(
            tier for tier in tiers if isinstance(tier, str) and tier.strip()
        )
        if cleaned:
            memory[provider] = cleaned
    return memory


def _current_memory(path: Path) -> Dict[str, Tuple[str, ...]]:
    """The memory for ``path``, re-read when the file changed on disk."""
    key = str(path)
    fingerprint = _fingerprint(path)
    cached = _CACHE.get(key)
    if cached is not None and cached.fingerprint == fingerprint:
        return cached.memory
    memory = _read_document(path)
    _CACHE[key] = _CachedMemory(fingerprint, memory)
    return memory


def record_seen_tiers(provider: str, tiers: Iterable[str]) -> None:
    """Record that ``provider`` has reported ``tiers``.

    The record is additive: a tier already recorded stays recorded, and
    the order in which tiers were first seen is kept.

    The document is re-read from disk before the write, so a tier
    recorded by *another* process since this one last looked is merged
    in rather than overwritten.  Without that, two backends writing at
    once would silently drop one another's history -- and the whole
    point of the module is that history outlives a process.

    Nothing is written when there is nothing to say: an empty tier list
    must not create the file, and must not rewrite it either.

    A write that fails is logged and swallowed.  The caller observed
    the tier, so the in-process memory keeps it even when the disk
    refused it, and the next recording of the same tiers writes it out
    again.
    """
    if not provider or not isinstance(provider, str):
        return
    recorded = _normalise_tiers(tiers)
    if not recorded:
        return

    path = memory_file_path()
    memory = dict(_read_document(path))
    merged = list(memory.get(provider, ()))
    for tier in recorded:
        if tier not in merged:
            merged.append(tier)
    memory[provider] = tuple(merged)

    try:
        atomic_write_json(path, memory, logger=logger, reraise=True)
    except OSError as exc:
        logger.warning(
            "quota_tier_memory_unwritable: cannot record %s (%s); "
            "the memory is kept in this process only",
            path, type(exc).__name__,
        )
    _CACHE[str(path)] = _CachedMemory(_fingerprint(path), memory)


def seen_tiers(provider: str) -> Tuple[str, ...]:
    """The tiers ``provider`` has been recorded as reporting.

    Empty for a provider nobody recorded, and empty for a memory file
    that is missing, torn or not a provider map.  The order is the
    order the tiers were first recorded in.
    """
    if not provider or not isinstance(provider, str):
        return ()
    return _current_memory(memory_file_path()).get(provider, ())


def has_seen_tier(provider: str, tier: str) -> bool:
    """Whether ``provider`` has ever been recorded as reporting ``tier``.

    ``False`` covers "never recorded" and "no such tier" alike: both
    mean the same thing to the caller, which is that this provider has
    no history of carrying that tier.
    """
    if not provider or not isinstance(provider, str):
        return False
    if not isinstance(tier, str):
        return False
    name = tier.strip()
    if not name:
        return False
    return name in seen_tiers(provider)