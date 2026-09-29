"""Read and validate the provider-order.json file.

An external producer atomically writes a contract file
(a provider-order contract file) that the backend
backend reads on startup to discover the live provider fallback chain.
This module owns the read side.

Schema
------
The on-disk schema is::

    {
      "version": 1,
      "updated_at": "2026-06-14T12:34:56+08:00",
      "source": "producer",
      "order": ["vendor-a-pro", "vendor-b", "vendor-c-app"],
      "providers": {
        "vendor-a-pro": {"weekly_reset_at": "2026-06-15T03:00:00+08:00"},
        ...
      }
    }

Failure semantics
-----------------
The optimizer file is the single source of truth for the chain — there
is no hard-coded fallback and no YAML ``provider_priority`` fallback.
When the JSON file is missing, malformed, or fails any of the 4 schema
checks (version≠1, order missing, order empty, updated_at not ISO 8601),
:func:`load_fallback_order` raises :class:`ProviderOrderError` so an
operator can grep the failure mode and decide whether to (a) restore
the contract file or (b) set ``PDT_PROVIDER_PRIORITY``.

Successful reads additionally filter ``order`` through the CC Switch
SQLite consumer layer
(:func:`cc_switch.list_provider_names`) so only
providers actually declared in the database survive. Providers that
exist in the JSON but not in the DB are dropped with a warning.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import cc_switch

log = logging.getLogger("provider_order")

# Project root used to anchor provider-order.json paths. Paths outside
# the project root or the system temp directory are rejected to prevent
# path traversal from a malicious PROVIDER_ORDER_FILE value.
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()


class ProviderOrderError(Exception):
    """Raised when ``provider-order.json`` cannot be loaded as a chain."""


def _is_under(child: Path, parent: Path) -> bool:
    """Return True when ``child`` is ``parent`` or a descendant of it."""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_provider_order_file_path(
    file_path: Union[str, os.PathLike],
) -> Path:
    """Validate ``file_path`` and return its resolved absolute form.

    Rejects two path-traversal patterns:

      * Any original path containing a ``..`` component (e.g.
        ``/tmp/../etc/passwd``).
      * Any resolved absolute path that is not under the project root
        or a system temp directory (e.g. ``/etc/passwd``).

    Allowed roots:

      * The project root (so the default
        the contract file and
        project-relative paths work).
      * The system temp directory reported by :func:`tempfile.gettempdir`.
      * ``/tmp`` and its resolved equivalent (``/private/tmp`` on macOS).

    Raises
    ------
    ValueError
        If the path contains a traversal component or escapes the
        allowed directories.
    """
    path = Path(file_path)
    if ".." in path.parts:
        raise ValueError(
            f"provider order file path contains '..' traversal: {file_path}"
        )

    resolved = path.resolve()

    allowed_roots = [
        _PROJECT_ROOT,
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp").resolve(),
    ]
    for root in allowed_roots:
        if _is_under(resolved, root):
            return resolved

    raise ValueError(
        f"provider order file path outside allowed directories: {file_path}"
    )

# Schema version the reader understands. Bump in lockstep with
# ``the optimizer's order_writer.SCHEMA_VERSION``.
SCHEMA_VERSION = 1

# Sentinel reasons for log.warning ``extra={"reason": ...}``.
REASON_FILE_MISSING = "file_missing"
REASON_PERMISSION_DENIED = "permission_denied"
REASON_JSON_INVALID = "json_invalid"
REASON_NOT_A_DICT = "not_a_dict"
REASON_VERSION_MISMATCH = "version_mismatch"
REASON_ORDER_MISSING = "order_missing"
REASON_INVALID_ORDER_TYPE = "invalid_order_type"
REASON_ORDER_EMPTY = "order_empty"
REASON_UPDATED_AT_INVALID = "updated_at_invalid"
REASON_STALE = "stale"
REASON_PROVIDER_CONFIG_MISSING = "provider_config_missing"
REASON_DB_UNAVAILABLE = "db_unavailable"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Caching strategy for ``load_fallback_order``:
#   The public function delegates to ``_load_fallback_order_cached``
#   which is decorated with :func:`functools.lru_cache` (``maxsize=1``).
#   Once-per-process, keyed on the resolved absolute path string,
#   droppable via :func:`cache_clear`. Two calls in the same process
#   with the same path read the file only once; a third call after
#   ``cache_clear`` re-reads.
#
# Caching strategy for ``load_provider_5h_usage``:
#   Uses a custom per-key dict + in-flight lock. ``lru_cache(maxsize=1)``
#   only stores one path, but the fallback order and the 5h usage
#   reader can be called with different paths in the same process, so
#   mixing them in one lru_cache would clobber each other. The custom
#   cache is keyed on resolved absolute path and supports multiple
#   distinct paths.
#
# Concurrency model for the 5h usage cache:
#   * ``_CACHE_LOCK`` protects ``_USAGE_CACHE`` (the dict) and
#     ``_CACHE_VERSION`` (a monotonic counter bumped on every
#     ``cache_clear`` so stale cache entries become invisible to
#     threads that lose the race).
#   * ``_IN_FLIGHT_LOCKS`` keyed by cache key, plus a meta lock, gives
#     us a per-key serialization barrier. Threads racing on the same
#     cold key all wait on one leader; the leader reads, populates the
#     cache, releases, and the followers re-read the populated cache.
_cache_lock = threading.Lock()
_cache: Dict[str, List[str]] = {}
_cache_version = 0
_in_flight_locks: Dict[str, threading.Lock] = {}
_in_flight_meta_lock = threading.Lock()

# Separate cache for ``load_provider_5h_usage``. Same threading rules as
# ``_cache`` (above): once-per-process, keyed on resolved absolute path,
# cleared by :func:`cache_clear`. A separate dict (rather than a tagged
# entry inside ``_cache``) keeps the value types homogeneous and lets
# the two readers drop their caches independently if that ever becomes
# necessary.
_usage_cache: Dict[str, Dict[str, float]] = {}


def _make_cache_key(file_path: Union[str, os.PathLike]) -> str:
    """Resolve ``file_path`` to a stable absolute string for cache lookup.

    Using ``Path.resolve()`` collapses symlinks, ``..`` segments, and
    relative components so two distinct inputs that point at the same
    on-disk file share a single cache entry. The string form is the
    dict key (paths are hashable, but we want ``Path`` vs ``str``
    symmetry, hence the explicit ``str()``).
    """
    return str(Path(file_path).resolve())


def cache_clear() -> None:
    """Drop every cached fallback chain.

    Operator escape hatch: after manually editing
    the contract file (or
    deleting it) you can call :func:`cache_clear` to force the next
    :func:`load_fallback_order` call to re-read the file instead of
    returning the stale cached chain.

    Clears both the :func:`functools.lru_cache` wrapper around the
    fallback-order read AND the per-key custom cache for
    :func:`load_provider_5h_usage`, so a single call resets every
    once-per-process read cache this module owns.

    Idempotent and thread-safe.
    """
    # Clear the lru_cache(maxsize=1) wrapper that fronts
    # ``load_fallback_order``. After this call the next
    # ``load_fallback_order`` invocation is guaranteed to re-read the
    # file instead of returning the cached tuple.
    _load_fallback_order_cached.cache_clear()
    # The 5-hour usage reader still uses the per-key custom cache
    # (lru_cache stores only one path; mixing the two value types in
    # one cache would need tagged entries). Drop its entries too.
    global _cache_version
    with _cache_lock:
        _cache.clear()
        _usage_cache.clear()
        _cache_version += 1
    # Drop the in-flight lock table too — any thread still waiting
    # for a leader will see the cleared cache when it re-checks.
    with _in_flight_meta_lock:
        _in_flight_locks.clear()


def _get_or_create_in_flight_lock(key: str) -> Tuple[threading.Lock, int]:
    """Return ``(per-key lock, current cache version)``.

    The cache version is captured here so a thread that loses the
    race against a ``cache_clear()`` can detect staleness on its
    second cache check and refetch — preventing a "thundering herd
    refetch" with the pre-clear data.
    """
    with _in_flight_meta_lock:
        lock = _in_flight_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _in_flight_locks[key] = lock
        return lock, _cache_version


@functools.lru_cache(maxsize=1)
def _load_fallback_order_cached(file_path: str) -> Tuple[str, ...]:
    """Read provider-order.json and return the filtered chain.

    Decorated with :func:`functools.lru_cache` (``maxsize=1``) so the backend reads ``provider-order.json`` at most once per process
    per distinct path. Two calls with the same resolved path return
    the cached tuple; a third call (after one path was evicted, or
    after :func:`cache_clear`) re-reads the file.

    The cache key is the *resolved absolute* path string. Callers
    must therefore resolve their path before calling this function —
    :func:`load_fallback_order` does that for the public API.

    Returns a tuple (not a list) so the cache value is hashable and
    immutable: callers that mutate the returned list will not poison
    the cache.
    """
    return tuple(_load_fallback_order_uncached(Path(file_path)))


def load_fallback_order(file_path: Optional[Union[str, os.PathLike]] = None) -> List[str]:
    """Return the live provider fallback chain.

    Resolution order:

      1. Read JSON from ``file_path`` (default: ``PROVIDER_ORDER_FILE``
         exported by ``backend.server``).
      2. Validate the schema. On any failure, raise
         :class:`ProviderOrderError` — there is no hard-coded fallback
         chain and no YAML fallback anymore.
      3. On success, filter the ``order`` list through
         :func:`cc_switch.list_provider_names` so
         only providers actually declared in the CC Switch database
         survive, preserving the JSON order.

    Caching: the underlying read is wrapped in
    :func:`functools.lru_cache` (``maxsize=1``) keyed on the resolved
    absolute path, so the file is read at most once per process per
    distinct path. Call :func:`cache_clear` to force a re-read (e.g.
    after a manual JSON edit).

    Parameters
    ----------
    file_path:
        Optional override for the JSON file location. When ``None``,
        :func:`_default_order_file` resolves it — an already-imported
        ``server`` module's ``PROVIDER_ORDER_FILE`` if there is one,
        otherwise the ``config_paths`` leaf constant (never importing
        ``server``; see :func:`_default_order_file`). Operator escape
        hatch: set the ``PROVIDER_ORDER_FILE`` env var before the
        server starts.

    Returns
    -------
    list[str]
        The provider names in fallback-chain order, filtered through the
        CC Switch consumer layer. May be empty when the JSON ``order``
        is valid but no listed provider exists in the database.

    Raises
    ------
    ProviderOrderError
        If the JSON file is missing, malformed, or fails any of the
        4 schema checks (version≠1, order missing, order empty,
        ``updated_at`` not ISO 8601). This is a hard failure: the
        contract file is the single source of truth and there is no
        fallback list anymore.
    """
    if file_path is None:
        file_path = _default_order_file()

    # Validate the original (un-resolved) path BEFORE ``.resolve()`` so a
    # ``..`` traversal component is still visible to
    # :func:`_validate_provider_order_file_path`. Otherwise
    # ``Path("/tmp/../etc/passwd").resolve()`` collapses to
    # ``/private/etc/passwd`` and the security check at line 101
    # (``if ".." in path.parts``) silently passes.
    _validate_provider_order_file_path(file_path)

    # Resolve to an absolute string so the lru_cache key is stable
    # across Path/str inputs and symlink variants. ``lru_cache``
    # requires hashable args — a string is the right type.
    resolved = str(Path(file_path).resolve())

    # Serialize concurrent cache misses through a per-key in-flight
    # lock. :func:`functools.lru_cache` (``maxsize=1``) is documented
    # as thread-safe but the implementation in CPython 3.9 does NOT
    # serialize body execution for concurrent misses — the lock only
    # protects cache state, so 100 simultaneous cache-miss calls can
    # all run the body before any of them populates the cache. This
    # per-key barrier collapses those racing misses into a single
    # body execution; followers re-check the (now-populated) cache
    # and return without reading the file.
    in_flight_lock, _version = _get_or_create_in_flight_lock(resolved)
    with in_flight_lock:
        return list(_load_fallback_order_cached(resolved))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_fallback_order_uncached(
    file_path: Union[str, os.PathLike],
) -> List[str]:
    """Compute the fallback chain from disk (no caching).

    Identical resolution order to :func:`load_fallback_order` minus
    the cache wrapper. Used internally by the cache miss path so the
    public function stays a thin cache-aware entry point.

    The returned list is a fresh ``list`` each call — callers
    must not mutate it. The cache stores a reference; :func:`load_
    fallback_order` returns a defensive copy so caller mutations
    can't poison subsequent reads.
    """
    payload = _read_payload(file_path)
    if payload is None:
        raise ProviderOrderError(
            f"provider-order.json could not be read at {file_path!s}; "
            "no fallback chain available"
        )
    order, reason = _validate_schema(payload)
    if order is None:
        log.warning(
            "provider-order.json schema invalid (reason=%s); "
            "no fallback chain available",
            reason,
            extra={"reason": reason},
        )
        raise ProviderOrderError(
            f"provider-order.json schema invalid (reason={reason}); "
            "no fallback chain available"
        )

    # Check whether the file is stale before treating it as the live chain.
    updated_at = payload.get("updated_at", "")
    stale_minutes = _staleness_minutes(updated_at) if isinstance(updated_at, str) else None
    if stale_minutes is not None:
        log.warning(
            "provider-order.json is stale (reason=%s, age=%smin); "
            "optimizer may not be running, using JSON order anyway",
            REASON_STALE,
            stale_minutes,
            extra={"reason": REASON_STALE, "age_minutes": stale_minutes},
        )
    else:
        log.debug(
            "provider-order.json is fresh (age <= 30min); using JSON order"
        )

    # provider-order.json is the single source of truth for the fallback
    # chain. Its entries are filtered against the names CC Switch
    # actually has rows for — that is the form the optimizer writes and
    # the form the consumer layer resolves against, so one membership
    # check is enough.
    #
    # The second check that used to live here compared against the
    # hard-coded kebab-case ID table (``_CC_SWITCH_NAME_TO_ID``). It was
    # removed along with the table: it was a whitelist that did not know
    # ``"Vendor D API"`` / ``"Vendor B Pro API"`` / ``"Vendor A API"``, so
    # gating on it truncated a 6-provider order down to 3.
    try:
        available_names = cc_switch.list_provider_names(
            db_path=None
        )
    except cc_switch.CCSwitchError as exc:
        log.warning(
            "provider-order.json cannot be filtered through the CC Switch "
            "consumer layer (reason=%s); no fallback chain available",
            REASON_DB_UNAVAILABLE,
            extra={"reason": REASON_DB_UNAVAILABLE, "error": str(exc)},
        )
        raise ProviderOrderError(
            f"CC Switch database unavailable: {exc}"
        ) from exc

    return _filter_order_to_live_providers(order, available_names)


def _filter_order_to_live_providers(
    order: List[str],
    available_names: Optional[List[str]] = None,
) -> List[str]:
    """Keep only the ``order`` entries that name a live CC Switch provider.

    Entries are CC Switch provider names — the strings the ``providers``
    table keys its rows by, that ``provider_routing.yaml`` matches its
    regexes against, and that ``get_provider`` resolves.
    An entry with no live row is dropped with a warning rather than
    passed downstream, where it would resolve to nothing and be
    indistinguishable from an ordinary end-of-chain.

    Two names that look similar are NOT collapsed: they are distinct
    rows with distinct credentials, and the dispatch loop tries each one
    independently.

    This replaces ``_normalize_order_to_kebab``, whose name described a
    conversion it had already stopped performing — the return value was
    already a list of names. There is no kebab-case vocabulary left to
    normalize *to*, so the misleading name goes with it.
    """
    available_set = set(available_names or ())
    result: List[str] = []
    seen: set = set()
    for entry in order:
        if not isinstance(entry, str) or not entry:
            continue
        if entry not in available_set:
            log.warning(
                "provider-order.json entry %r does not name a provider in "
                "the CC Switch database; dropping from the fallback chain",
                entry,
                extra={
                    "reason": REASON_PROVIDER_CONFIG_MISSING,
                    "entry": entry,
                    "dropped": [entry],
                },
            )
            continue
        if entry in seen:
            continue
        seen.add(entry)
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _default_order_file() -> Path:
    """Resolve the default JSON path without importing ``server``.

    ``server.PROVIDER_ORDER_FILE`` is an import-time snapshot of
    :func:`config_paths.resolve_provider_order_file` (the same
    precedence, documented in ``config_paths`` as "mirroring
    ``server.PROVIDER_ORDER_FILE``"), and ``config_paths`` is a leaf
    utility that pulls in neither FastAPI nor the server.

    Preferring an *already loaded* ``server`` module keeps the historic
    contract intact for callers that boot the server normally and for
    tests that patch ``server.PROVIDER_ORDER_FILE`` — but the module is
    never imported just to read the constant.

    Why that distinction matters (2026-09-14): ``python -m
    backend.server`` executes the server body as ``__main__``. The old
    ``from server import PROVIDER_ORDER_FILE`` therefore ran the whole
    ~10k-line body a *second* time, under the name ``server``, from
    inside the lifespan startup — roughly a second after
    ``uvicorn.run()``. That second execution re-ran every module-level
    side effect, including ``signal.signal(SIGTERM, _signal_handler)``,
    which silently replaced uvicorn's ``Server.handle_exit`` and turned
    SIGTERM into a no-op (see ``server._install_signal_handlers``).
    """
    server_module = sys.modules.get("server")
    if server_module is not None:
        override = getattr(server_module, "PROVIDER_ORDER_FILE", None)
        if isinstance(override, (str, os.PathLike)):
            return Path(override)

    try:
        from config_paths import PROVIDER_ORDER_FILE

        return Path(PROVIDER_ORDER_FILE)
    except Exception as exc:  # ImportError, AttributeError, etc.
        # Last-ditch default — the same ``<repo>/.config/provider-order.json``
        # that ``config_paths.resolve_provider_order_file()`` returns when
        # nothing overrides it. Spelled out rather than imported because
        # reaching this branch *is* the import having failed. The debug
        # line surfaces that failure so an operator can see why the
        # override didn't take effect.
        log.debug(
            "could not import PROVIDER_ORDER_FILE from config_paths "
            "(reason=%s); using hard-coded default",
            exc,
        )
        return Path(__file__).parent.parent / ".config" / "provider-order.json"


def _read_payload(file_path: Union[str, os.PathLike]) -> Optional[Dict[str, Any]]:
    """Read the JSON file and return the parsed dict, or None on any error.

    Errors are *not* raised — the caller decides whether a missing or
    malformed file is fatal. For ``load_fallback_order`` it is: the
    caller raises :class:`ProviderOrderError` and surfaces a single
    greppable failure mode.
    """
    path = _validate_provider_order_file_path(file_path)
    if not path.exists():
        log.warning(
            "provider-order.json not found at %s (reason=%s)",
            path,
            REASON_FILE_MISSING,
            extra={"reason": REASON_FILE_MISSING},
        )
        return None
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except PermissionError as exc:
        # PermissionError is a subclass of OSError; we must catch it
        # BEFORE the generic OSError handler below so we can emit a
        # reason that's specific to permission failures (so operators
        # can grep for "permission_denied" and find ACL issues fast).
        log.warning(
            "provider-order.json could not be read due to permissions "
            "(reason=%s, error=%s)",
            REASON_PERMISSION_DENIED,
            exc,
            extra={"reason": REASON_PERMISSION_DENIED},
        )
        return None
    except (OSError, ValueError) as exc:
        # OSError: file disappeared between exists() check and open()
        # ValueError: json.JSONDecodeError (subclass)
        log.warning(
            "provider-order.json could not be parsed (reason=%s, error=%s)",
            REASON_JSON_INVALID,
            exc,
            extra={"reason": REASON_JSON_INVALID},
        )
        return None

    if not isinstance(data, dict):
        log.warning(
            "provider-order.json root is %s, expected dict (reason=%s)",
            type(data).__name__,
            REASON_NOT_A_DICT,
            extra={"reason": REASON_NOT_A_DICT},
        )
        return None

    return data


def _validate_schema(payload: Dict[str, Any]) -> tuple[Optional[List[str]], Optional[str]]:
    """Validate payload and return ``(order, reason)``.

    ``order`` is ``None`` when validation fails; ``reason`` is the
    sentinel string the caller should log. The ``providers`` dict is
    *not* validated — it's optional metadata and a parse failure on
    it must not cause the whole read to fail (per architecture
    decision point 2: the optimizer's output is an optimization
    signal, not a source of truth).
    """
    # 1. version
    if payload.get("version") != SCHEMA_VERSION:
        return None, REASON_VERSION_MISMATCH

    # 2. order must be present, a list, and non-empty
    order = payload.get("order")
    if "order" not in payload:
        return None, REASON_ORDER_MISSING
    if not isinstance(order, list):
        # ``order`` key is present but has the wrong type (string,
        # dict, int, etc.). This is a different failure mode from
        # the key being absent — emit a dedicated reason so
        # operators can grep for it.
        return None, REASON_INVALID_ORDER_TYPE
    if not order:
        return None, REASON_ORDER_EMPTY
    # Defensive: every entry should be a non-empty string. Strip and
    # filter — if nothing's left, treat as empty.
    cleaned = [str(x).strip() for x in order if isinstance(x, str) and x.strip()]
    if not cleaned:
        return None, REASON_ORDER_EMPTY

    # 3. updated_at must be a parseable ISO 8601 string
    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, str) or not _is_iso8601(updated_at):
        return None, REASON_UPDATED_AT_INVALID

    # 4. providers dict is best-effort. We don't surface its parse
    #    state to the caller — bad metadata is silently ignored, the
    #    order list is the only thing that matters.
    _ = payload.get("providers")  # noqa: F841 — explicitly unused

    return cleaned, None


def _is_iso8601(value: str) -> bool:
    """Return True if ``value`` is a parseable ISO 8601 timestamp.

    Accepts both naive (``2026-06-14T12:34:56``) and timezone-aware
    (``2026-06-14T12:34:56+08:00``, ``2026-06-14T12:34:56Z``) forms.
    Python's :func:`datetime.fromisoformat` covers both as of 3.11;
    on 3.9/3.10 the ``Z`` suffix is not accepted, so we normalize
    ``Z`` → ``+00:00`` before parsing.

    The field is semantically a *timestamp* (a moment in time), not a
    date, so a date-only string like ``2026-06-14`` is rejected
    even though :func:`datetime.fromisoformat` happily parses it.
    We require a time component — either a ``T`` or a space
    separator between the date and the time-of-day.
    """
    candidate = value.strip()
    if not candidate:
        return False
    # Require a time component. ``datetime.fromisoformat`` accepts
    # date-only strings, but ``updated_at`` is supposed to capture
    # when the file was last written — a date without a clock time
    # is ambiguous to the second and so we treat it as invalid.
    if "T" not in candidate and " " not in candidate:
        return False
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return True


def _staleness_minutes(updated_at: str) -> Optional[int]:
    """Return the age of ``updated_at`` in whole minutes, or ``None``.

    ``None`` means either the timestamp is fresh (<= 30 minutes old) or
    its age could not be determined (should not happen after schema
    validation, but the function is defensive).

    The threshold is intentionally hard-coded at 30 minutes: the producer is expected to refresh the contract file at
    least that often. Anything older suggests the optimizer subprocess
    may have stopped or the file is orphaned.
    """
    try:
        candidate = updated_at.strip()
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    age = datetime.now(timezone.utc) - parsed
    age_minutes = int(age.total_seconds() // 60)
    # Negative ages can occur if the clock drifts or the file carries a
    # future timestamp; treat them as fresh rather than emitting a stale
    # warning that would confuse operators.
    if age_minutes <= 30:
        return None
    return age_minutes


# ---------------------------------------------------------------------------
# Dynamic provider order + config resolution
# ---------------------------------------------------------------------------


def get_provider_order_from_optimizer(
    optimizer_output: Any,
    db_path: Optional[Union[str, Path]] = None,
) -> List[str]:
    """Return ordered provider names from optimizer output, filtered by the consumer layer.

    Reads the optimizer's ``order`` list and keeps only providers that
    name a live row in the CC Switch database (per
    :func:`cc_switch.list_provider_names`).

    Parameters
    ----------
    optimizer_output:
        Optimizer output dict. Expected shape: ``{"order": [...]}``.
        Non-dict inputs are treated as empty.
    db_path:
        Optional override for the CC Switch SQLite database path.

    Returns
    -------
    list[str]
        Filtered, de-duplicated provider names in the order they appeared
        in the optimizer output. Empty when the optimizer output has no
        order, the order is empty, or no entries name a live provider.

    Raises
    ------
    cc_switch.CCSwitchError
        Propagated from the consumer layer when the database cannot be read.
    """
    if not isinstance(optimizer_output, dict):
        return []
    order = optimizer_output.get("order")
    if not isinstance(order, list):
        return []

    available = cc_switch.list_provider_names(db_path=db_path)
    result: List[str] = []
    seen: set = set()
    for name in order:
        if not isinstance(name, str) or not name:
            continue
        if name not in available:
            continue
        if name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


# ---------------------------------------------------------------------------
# 5-hour usage reader (dynamic concurrency support)
# ---------------------------------------------------------------------------
#
# The dynamic concurrency strategy (see
# ``dynamic_provider_concurrency.compute_dynamic_limit``) needs each
# provider's 5-hour quota remaining percentage. The optimizer writes
# this into ``provider-order.json`` under ``providers.<id>.five_hour_remaining_pct``
# (a number in ``[0, 100]``). This module owns the read side.
#
# Cache discipline mirrors :func:`load_fallback_order`: once-per-process,
# keyed on the resolved absolute file path, dropped by :func:`cache_clear`.
# Concurrent first-time callers are serialized through the same per-key
# in-flight lock to keep the "no race causing IO doubling" contract.


def _parse_5h_usage_payload(payload: Any) -> Dict[str, float]:
    """Extract the per-provider ``five_hour_remaining_pct`` map.

    Defensive against every shape mismatch in the optimizer's output:

      * ``providers`` missing or non-dict         → ``{}``
      * per-provider value missing the field       → entry skipped
      * per-provider value not a number            → entry skipped
      * value outside ``[0, 100]``                  → clamped

    A failed extraction never raises — the dispatch loop must not crash
    on a malformed JSON. The caller (the dynamic selection module)
    treats a missing entry as "100% remaining" (i.e. fully available),
    which is the safe default: a too-permissive cap is a soft problem
    (more retries hit the rate limiter), while a too-tight cap is a
    hard problem (subagents can't run).
    """
    if not isinstance(payload, dict):
        return {}
    providers = payload.get("providers")
    if not isinstance(providers, dict):
        return {}

    result: Dict[str, float] = {}
    for pid, entry in providers.items():
        if not isinstance(pid, str) or not pid:
            continue
        if not isinstance(entry, dict):
            continue
        raw = entry.get("five_hour_remaining_pct")
        # Accept int or float; reject strings, bools, None, lists.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        # Clamp into [0, 100]. A negative value (CC Switch glitch)
        # would otherwise shrink the cap below zero via
        # ``compute_dynamic_limit``; a value > 100 would silently
        # widen the cap beyond its design ceiling.
        clamped = max(0.0, min(100.0, float(raw)))
        result[pid] = clamped
    return result


def load_provider_5h_usage(
    file_path: Optional[Union[str, os.PathLike]] = None,
) -> Dict[str, float]:
    """Return each provider's 5-hour quota remaining percentage.

    Resolution:

      1. ``file_path`` argument, or :func:`_default_order_file` when
         ``None``.
      2. Read JSON from disk (same on-disk contract as
         :func:`load_fallback_order`).
      3. Extract ``providers.<id>.five_hour_remaining_pct`` per
         provider via :func:`_parse_5h_usage_payload`.
      4. Cache the result keyed on the resolved absolute path; the
         next caller from the same process returns the cached map.

    Failure semantics — every failure mode returns an empty map:

      * File missing              → ``{}``
      * Malformed JSON            → ``{}``
      * ``providers`` not a dict  → ``{}``
      * Schema mismatch (the JSON looks like something other than the
        optimizer's contract) → ``{}``

    An empty map is the safe default: the caller (:func:`select_provider_
    with_dynamic_capacity`) treats missing entries as "100% remaining",
    i.e. the provider has its full base cap. This matches the legacy
    behaviour (no dynamic-cap input → legacy fixed cap), so a missing
    file degrades to the pre-feature state rather than refusing to
    dispatch.

    Returns:
        ``{provider_id: remaining_pct}``. ``remaining_pct`` is in
        ``[0.0, 100.0]``. Missing providers are not in the map
        (callers default them to 100%).
    """
    if file_path is None:
        file_path = _default_order_file()

    key = _make_cache_key(file_path)

    # Fast path: cache hit. Snapshot the version so a concurrent
    # cache_clear() cannot make us serve stale data below.
    with _cache_lock:
        cached = _usage_cache.get(key)
        if cached is not None:
            return dict(cached)

    # Slow path: per-key serialization barrier (same shape as
    # ``load_fallback_order`` — see that function for the rationale).
    in_flight_lock, version_at_entry = _get_or_create_in_flight_lock(key)
    with in_flight_lock:
        with _cache_lock:
            cached = _usage_cache.get(key)
            if cached is not None and _cache_version == version_at_entry:
                return dict(cached)

        payload = _read_payload(file_path)
        result = _parse_5h_usage_payload(payload)
        with _cache_lock:
            if _cache_version == version_at_entry:
                _usage_cache[key] = result
        return dict(result)
