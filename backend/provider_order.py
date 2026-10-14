"""The provider fallback chain, asked of CC Switch.

CC Switch owns the fleet: which providers exist, which one is current,
and the order the operator put them in. So the chain is not something
this repository reconstructs — it is something this repository asks
for, and the whole module is the shape of that question.

The question is ``GET <base>/api/providers``, and **the array order is
the chain**: it is the order CC Switch itself renders, so re-sorting it
here could only disagree with the application the operator is looking
at.

Why the chain is asked only there
---------------------------------
CC Switch publishes the fleet over an HTTP contract. Its SQLite store
is a different thing: another application's private, undocumented
schema. Reading it means depending on table and column names nobody
promised to keep, and re-deriving the provider *order* from that
store's sort columns is a second implementation of a rule CC Switch
already publishes — one free to disagree with the array it actually
serves. So the chain is asked over HTTP and nowhere else.

Failure semantics
-----------------
There is no second source to retreat to, so every way of not getting a
chain is loud. A 4xx is the sharpest case: something *answered*, which
means the configured port or endpoint is wrong, or whatever is
listening there is not CC Switch. That is a configuration fact an
operator has to see, not one to paper over with an answer from
elsewhere. The same goes for a 5xx, a connection that cannot be made,
a body that is not the provider list, and a list whose entries carry no
usable name: each raises :class:`ProviderOrderError` carrying a
``reason`` and the evidence behind it, so a log line says whether the
port is wrong, the service is down, or CC Switch is refusing.

The one soft answer is an empty one. HTTP 200 with ``providers: []`` is
CC Switch saying "I have no providers" — a successful answer to the
question, not a failure to answer it. It is returned as ``[]``, and
``server.load_providers`` degrades to starting with no providers, which
is the behaviour that state has always had.

What this module never does is answer from a list written down here.
A list written down here is the thing that rots: the chain is whatever
CC Switch says it is today, and a reader that answers from anywhere
else is answering a different question than the one it was asked.

The quota is the same question asked of the same owner
-------------------------------------------------------
CC Switch also publishes **quota** — how much of each provider's
rolling 5-hour and weekly windows is spent, and when those windows
come back — at ``GET <base>/api/usage``. :func:`load_provider_quota_map`
asks for it there.

That is a deliberate re-source, and the module used to say so
differently. The quota was once read from a JSON file an external
producer wrote, and the reader was documented as keeping that file
"because re-sourcing the quota is its own change". Two things made
that split untenable: the file is written by a producer this repository
does not run, so nothing here could guarantee it exists; and it is read
**once per process**, so a value that goes stale the moment it lands
still governs every dispatch for the life of the process. Both were
invisible because an unreadable file and a fully-available provider
produce the same answer.

CC Switch is already the source of the chain and already the component
that meters every provider it routes to, so the quota is now asked of
the same host over the same seam. The old file reader stays, with its
signature untouched, because several capacity call sites still take a
plain ``{provider: pct}`` map; what changed is that the **eligibility
verdict** — the decision to dispatch at all — is now made from the
live reading, behind a TTL cache, so a window that refills is noticed
without a restart.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("provider_order")

# Project root used to anchor the 5-hour quota file path.
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()


class ProviderOrderError(Exception):
    """Raised when CC Switch could not be asked for the chain.

    ``reason`` is one of this module's ``REASON_*`` sentinels, ``detail``
    is the evidence behind it — the HTTP status code, the connection
    error's own text, or what was wrong with the body — and ``url`` is
    the endpoint that was actually asked. All three are in the message
    as well as on the attributes, because the exception usually travels
    as a message: a log line reading only "provider chain unavailable"
    cannot tell a misconfigured port from a service that is not running
    from a CC Switch that is refusing, and the URL is what tells the
    first of those apart.
    """

    def __init__(self, reason: str, detail: str = "", url: str = "") -> None:
        message = f"{reason}: {detail}" if detail else reason
        if url:
            message = f"{message} [asked {url}]"
        super().__init__(message)
        self.reason = reason
        self.detail = detail
        self.url = url


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

    Guards the one file this module still opens — the 5-hour quota
    payload, see :func:`_default_order_file`. Rejects two path-traversal
    patterns:

      * Any original path containing a ``..`` component (e.g.
        ``/tmp/../etc/passwd``).
      * Any resolved absolute path that is not under the project root
        or a system temp directory (e.g. ``/etc/passwd``).

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


# ---------------------------------------------------------------------------
# The one source: CC Switch's HTTP provider list
# ---------------------------------------------------------------------------

#: Where CC Switch serves its remote-access API when nothing overrides
#: it. Loopback only: the surface has no authentication, so a
#: non-loopback bind would expose provider inventory — and, on a build
#: that also serves the proxy, live traffic — to anything that can reach
#: the host. An operator who needs a different address sets the variable
#: below rather than editing this line.
_DEFAULT_API_BASE = "http://127.0.0.1:4000"

#: The path appended to whichever base is in force. CC Switch appends
#: this to its own base; nothing here appends anything else, so an
#: override that already carries a path cannot smuggle a second
#: endpoint in.
_PROVIDERS_PATH = "/api/providers"

#: The path appended to the same base for the quota reading. Same
#: single-suffix discipline as :data:`_PROVIDERS_PATH`: the override
#: names a base, and this is the only thing appended to it.
_USAGE_PATH = "/api/usage"

#: Environment variables that redirect the API leg, highest priority
#: first. ``PDT_PROVIDER_ORDER_API_URL`` is this project's own name and
#: follows the ``PDT_`` convention every other resolver here uses;
#: ``CC_SWITCH_API_URL`` and ``PROVIDER_USAGE_CC_SWITCH_API_URL`` are
#: the names the provider optimizer already exports for the same host,
#: honoured so one variable in the environment can move both consumers
#: instead of leaving them pointed at different CC Switches.
_API_BASE_ENV_VARS = (
    "PDT_PROVIDER_ORDER_API_URL",
    "CC_SWITCH_API_URL",
    "PROVIDER_USAGE_CC_SWITCH_API_URL",
)

#: Seconds to wait for the whole request. The chain is needed once at
#: startup and then per dispatch, so a CC Switch that has stopped
#: answering must not be able to stall a dispatch.
_API_TIMEOUT_SEC = 2.0

#: Sentinel reasons for the chain's failures, carried on the
#: :class:`ProviderOrderError` raised out of this module and on the log
#: line's ``extra={"reason": ...}``.
#:
#: Every one of them is terminal. There is no other source to ask, so a
#: reason here never means "look somewhere else" — it means the chain
#: is unavailable and an operator has to fix something. They are kept
#: apart so that "which of the ways did this go wrong" is answerable
#: from the log without reproducing it against the port by hand.
REASON_HTTP_STATUS = "http_status"
REASON_API_UNREACHABLE = "api_unreachable"
REASON_BAD_PAYLOAD = "bad_payload"
REASON_NO_USABLE_NAME = "no_usable_name"

#: Sentinel reason for the *quota* leg's failures. Kept apart from the
#: four above so a log line says which read failed: the chain and the
#: quota are two endpoints of one host, and a line that could be either
#: is a line nobody can grep.
REASON_QUOTA_UNREACHABLE = "quota_api_unreachable"
REASON_QUOTA_BAD_PAYLOAD = "quota_bad_payload"


def _api_base() -> Tuple[str, str]:
    """Return ``(scheme, netloc)`` for CC Switch's control plane.

    The environment variables in :data:`_API_BASE_ENV_VARS` name a
    **base**, not an endpoint — the ones this project inherits from the
    optimizer point at ``/api/usage``, whose path is not ours to keep.
    So only the scheme and authority are taken from the value and each
    reader appends its own single suffix, which makes an override that
    happens to carry a path still resolve rather than silently
    requesting a URL that cannot answer.

    Shared by the chain reader and the quota reader so the two can
    never disagree about *which host* is CC Switch — they differ only
    in the one path they append.
    """
    base = None
    for name in _API_BASE_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            base = value
            break
    if base is None:
        base = _DEFAULT_API_BASE
    elif "://" not in base:
        # ``127.0.0.1:4000`` parses as a scheme and a path without this,
        # which would rebuild the URL as ``127.0.0.1:/api/providers``.
        base = "http://" + base

    parts = urlsplit(base)
    if parts.path.strip("/"):
        log.warning(
            "ignoring the path on the CC Switch API base override (%r); "
            "each reader appends its own endpoint",
            parts.path,
            extra={"reason": REASON_BAD_PAYLOAD},
        )
    return parts.scheme, parts.netloc


def _api_providers_url() -> str:
    """Return the absolute URL of CC Switch's provider list."""
    scheme, netloc = _api_base()
    return urlunsplit((scheme, netloc, _PROVIDERS_PATH, "", ""))


def _api_usage_url() -> str:
    """Return the absolute URL of CC Switch's quota reading.

    Same host and same override resolution as the chain reader; the
    suffix is the one difference, which is why the base is resolved by
    the shared :func:`_api_base` rather than duplicated.
    """
    scheme, netloc = _api_base()
    return urlunsplit((scheme, netloc, _USAGE_PATH, "", ""))


def _chain_from_api() -> List[str]:
    """Return the chain CC Switch serves over HTTP.

    Read-only by construction: one ``GET``, no body, no query string,
    and no endpoint other than :data:`_PROVIDERS_PATH`. Anything that
    writes — reordering included — is CC Switch's business, reached
    through its own UI.

    Returns ``[]`` only for a 200 carrying a valid but empty
    ``providers`` list: that is CC Switch reporting that it has no
    providers, which is an answer to the question rather than a failure
    to answer it. Every other way of not producing a chain raises.

    Raises:
        ProviderOrderError: the request could not be completed, the
            status was not 200, or the body did not carry a usable
            provider list.
    """
    url = _api_providers_url()
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_API_TIMEOUT_SEC) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # HTTPError subclasses URLError, so it is caught ahead of it.
        # A status line means *something answered*: a 4xx says the
        # configured port or endpoint is wrong or that the thing
        # listening there is not CC Switch, and a 5xx says the thing
        # that is CC Switch is refusing. Neither is a question this
        # module may answer any other way.
        raise ProviderOrderError(
            REASON_HTTP_STATUS, f"HTTP {exc.code}", url
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # URLError: connection refused / DNS / TLS. OSError: the
        # timeout, and the resets a half-written response produces.
        # The exception's own type is carried alongside its text so
        # "refused" and "timed out" stay distinguishable in the log.
        raise ProviderOrderError(
            REASON_API_UNREACHABLE, f"{type(exc).__name__}: {exc}", url
        ) from exc

    if status != 200:
        raise ProviderOrderError(REASON_HTTP_STATUS, f"HTTP {status}", url)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        # ValueError covers json.JSONDecodeError. A proxy that answered
        # with HTML reaches this too, which is why the payload is
        # parsed rather than trusted by status alone.
        raise ProviderOrderError(
            REASON_BAD_PAYLOAD, f"{type(exc).__name__}: {exc}", url
        ) from exc

    if not isinstance(payload, dict):
        raise ProviderOrderError(
            REASON_BAD_PAYLOAD,
            f"root is {type(payload).__name__}, expected object",
            url,
        )
    rows = payload.get("providers")
    if not isinstance(rows, list):
        raise ProviderOrderError(
            REASON_BAD_PAYLOAD,
            f"'providers' is {type(rows).__name__}, expected a list",
            url,
        )

    if not rows:
        # The one soft answer there is: CC Switch is serving and says it
        # has no providers. Not a failure to ask the question.
        return []

    names: List[str] = []
    seen: set = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)
        names.append(name)

    if not names:
        # Distinct from the empty list above: there *are* rows here, and
        # none of them is one this reader can dispatch on. A CC Switch
        # whose response shape has moved is a fault to report, not a
        # fleet of zero.
        raise ProviderOrderError(
            REASON_NO_USABLE_NAME,
            f"{len(rows)} provider row(s), none carried a usable name",
            url,
        )
    return names


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Caching strategy for ``load_fallback_order``:
#   The public function delegates to ``_load_fallback_order_cached``,
#   which is decorated with :func:`functools.lru_cache` (``maxsize=1``).
#   Once-per-process, parameterless — there is one CC Switch and one
#   chain, so there is nothing to key on — droppable via
#   :func:`cache_clear`.
#
# Caching strategy for ``load_provider_5h_usage``:
#   Uses a custom per-key dict + in-flight lock, because it is still
#   keyed on the path of the file it reads and can be pointed at
#   different files within one process.
#
# Concurrency model:
#   * ``_cache_lock`` protects ``_usage_cache`` (the dict) and
#     ``_cache_version`` (a monotonic counter bumped on every
#     ``cache_clear`` so stale cache entries become invisible to
#     threads that lose the race).
#   * ``_in_flight_locks`` keyed by cache key, plus a meta lock, gives
#     us a per-key serialization barrier. Threads racing on the same
#     cold key all wait on one leader; the leader reads, populates the
#     cache, releases, and the followers re-read the populated cache.
#     Both readers share this barrier, which is why the chain takes a
#     key too rather than riding the ``lru_cache`` alone.
_cache_lock = threading.Lock()
_usage_cache: Dict[str, Dict[str, float]] = {}
_cache_version = 0
_in_flight_locks: Dict[str, threading.Lock] = {}
_in_flight_meta_lock = threading.Lock()

#: The single key the parameterless chain cache serializes on.
_CACHE_KEY = "chain"

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
    """Drop every cached read this module owns.

    Operator escape hatch: after re-ordering the fleet inside CC
    Switch, :func:`cache_clear` makes the next
    :func:`load_fallback_order` ask CC Switch again instead of
    returning the chain it saw at startup.

    Clears the :func:`functools.lru_cache` wrapper around the chain
    read, the per-key custom cache for :func:`load_provider_5h_usage`,
    and the TTL cache for :func:`load_provider_quota_map`, so a single
    call resets every read cache this module owns.

    The quota cache expires on its own, so this is only needed by an
    operator who has just fixed something CC Switch was refusing.

    Idempotent and thread-safe.
    """
    # Clear the lru_cache(maxsize=1) wrapper that fronts
    # ``load_fallback_order``. After this call the next invocation is
    # guaranteed to re-ask CC Switch instead of returning the cached
    # tuple.
    _load_fallback_order_cached.cache_clear()
    # The 5-hour usage reader keeps the per-key custom cache (its key
    # is a file path, which lru_cache's single slot cannot hold). Drop
    # its entries too.
    global _cache_version, _quota_refreshed_at
    with _cache_lock:
        _usage_cache.clear()
        _cache_version += 1
    # Drop the in-flight lock table too — any thread still waiting
    # for a leader will see the cleared cache when it re-checks.
    with _in_flight_meta_lock:
        _in_flight_locks.clear()
    # The live quota reading, same reason as the chain's: an operator
    # who has just fixed CC Switch should not have to wait a TTL out.
    with _quota_cache_lock:
        _quota_cache.clear()
        _quota_refreshed_at = 0.0


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
def _load_fallback_order_cached() -> Tuple[str, ...]:
    """Ask CC Switch for the chain, and return it.

    Decorated with :func:`functools.lru_cache` (``maxsize=1``) so the
    backend asks at most once per process: the chain is read at startup
    and re-read per dispatch, and the same answer every time is the
    point. There is nothing to key on — one CC Switch, one chain — so
    the cache is parameterless.

    The in-flight barrier in :func:`load_fallback_order` means a
    concurrent miss costs one round trip rather than one per thread.

    Returns a tuple (not a list) so the cache value is hashable and
    immutable: callers that mutate the returned list will not poison
    the cache.
    """
    try:
        return tuple(_chain_from_api())
    except ProviderOrderError as exc:
        # Logged here rather than only raised, because the raise
        # travels through callers that may log it as a bare string or
        # not at all. The reason token is the greppable part.
        log.error(
            "cannot resolve the provider chain from CC Switch "
            "(reason=%s, detail=%s, url=%s)",
            exc.reason,
            exc.detail,
            exc.url,
            extra={"reason": exc.reason, "detail": exc.detail},
        )
        raise


def load_fallback_order() -> List[str]:
    """Return the live provider fallback chain, as CC Switch orders it.

    Asks CC Switch over its HTTP provider list and returns the array
    order it served, unfiltered. Names that turn out not to resolve are
    the consumer's to skip — ``server.load_providers`` already reports
    and drops them.

    Caching: once per process, dropped by :func:`cache_clear`. A chain
    re-ordered inside CC Switch therefore reaches a running backend on
    its next restart, or immediately after a ``cache_clear()``.

    Returns
    -------
    list[str]
        Provider display names, in chain order. May be empty, and only
        for one reason: CC Switch answered 200 with a valid but empty
        ``providers`` list, i.e. it reports that it has no providers.
        ``server.load_providers`` degrades that to starting with none.

    Raises
    ------
    ProviderOrderError
        CC Switch could not be asked, or answered with something this
        reader cannot use. ``reason`` names which of those it was and
        ``detail`` carries the evidence — the HTTP status code, the
        connection error's text, or what was wrong with the body — so
        the log distinguishes a misconfigured port from a service that
        is not running from a CC Switch that is refusing. There is no
        other source to fall back on, by design: a list written down
        here is the one answer that cannot be corrected by operating
        the application that owns the fleet.
    """
    # Serialize concurrent cache misses through the in-flight lock.
    # ``lru_cache`` is documented as thread-safe, but it only
    # serializes cache *state*: without this barrier N simultaneous
    # misses all run the body, which here means N concurrent HTTP
    # requests to CC Switch for an answer they will all receive
    # identically.
    in_flight_lock, _version = _get_or_create_in_flight_lock(_CACHE_KEY)
    with in_flight_lock:
        return list(_load_fallback_order_cached())


# ---------------------------------------------------------------------------
# 5-hour quota file — the legacy payload, still read from disk
# ---------------------------------------------------------------------------
#
# The chain is asked of CC Switch; the per-provider 5-hour quota used to
# come from a different datum with a different producer, and
# :func:`load_provider_5h_usage` still reads it from the optimizer's
# payload. That reader is why this module opens a file at all.
#
# It is now the *legacy* leg. The quota is re-sourced to CC Switch's
# ``/api/usage`` by :func:`load_provider_quota_map` further down — that
# re-source is exactly the change this block used to defer, and it is
# not deferred any more. The file reader stays because several capacity
# call sites take a plain ``{provider: pct}`` map and this one still
# produces it; what it no longer does is decide *whether to dispatch*.
# Do not add new consumers of this reader for that decision: an
# unreadable file and a provider with a full window produce the same
# answer here, and only one of those two should stop a dispatch.


#: Environment variable naming the quota payload. It is *not* the chain
#: seam that used to live here: the chain is read from CC Switch and
#: takes no path, so this names only the quota file.
_PROVIDER_USAGE_FILE_ENV_VAR = "PDT_PROVIDER_USAGE_FILE"


def _default_order_file() -> Path:
    """Return the quota payload's path for :func:`load_provider_5h_usage`.

    ``PDT_PROVIDER_USAGE_FILE`` when set, otherwise the
    ``provider-order.json`` in the local config directory — the same
    location the optimizer writes, resolved through the ``config_paths``
    leaf utility so this module pulls in neither FastAPI nor the server.

    That "never import ``server``" rule is load-bearing, not tidiness:
    ``python -m backend.server`` executes the server body as
    ``__main__``, so a ``from server import ...`` here ran the whole
    body a *second* time under the name ``server`` from inside lifespan
    startup, re-running every module-level side effect — including
    ``signal.signal(SIGTERM, _signal_handler)``, which replaced
    uvicorn's ``Server.handle_exit`` and turned SIGTERM into a no-op.
    """
    override = os.environ.get(_PROVIDER_USAGE_FILE_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()

    from config_paths import resolve_config_dir

    return resolve_config_dir() / "provider-order.json"


# Sentinel reasons for the quota payload's read failures. Distinct from
# the chain reasons above so a log line says which read failed.
REASON_FILE_MISSING = "file_missing"
REASON_PERMISSION_DENIED = "permission_denied"
REASON_JSON_INVALID = "json_invalid"
REASON_NOT_A_DICT = "not_a_dict"


def _read_payload(file_path: Union[str, os.PathLike]) -> Optional[Dict[str, Any]]:
    """Read the quota payload and return the parsed dict, or None on error.

    Errors are *not* raised — the caller decides what a missing or
    malformed payload means. For :func:`load_provider_5h_usage` the
    answer is "no quota data", which its caller reads as a fully
    available provider.
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
# Once-per-process, keyed on the resolved absolute file path, dropped by
# :func:`cache_clear`. Concurrent first-time callers are serialized
# through the same per-key in-flight lock the chain reader uses.


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
      2. Read JSON from disk. This is the module's **only** file read,
         and it is not the fallback chain: the chain is asked of CC
         Switch, while this payload is a quota report a producer writes
         alongside it. The path is checked by
         :func:`_validate_provider_order_file_path`.
      3. Extract ``providers.<id>.five_hour_remaining_pct`` per
         provider via :func:`_parse_5h_usage_payload`.
      4. Cache the result keyed on the resolved absolute path; the
         next caller from the same process returns the cached map.

    Failure semantics — every failure mode returns an empty map:

      * File missing              → ``{}``
      * Malformed JSON            → ``{}``
      * ``providers`` not a dict  → ``{}``

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


# ---------------------------------------------------------------------------
# Quota — CC Switch's own reading of every provider's windows
# ---------------------------------------------------------------------------
#
# Three shapes come back from ``/api/usage``, and the dispatch decision
# differs for all three, so they are distinguished by ``unit`` /
# ``success`` rather than by any one field's value:
#
#   * **rolling window** — tiers whose ``unit`` is ``"%"``. Both gated
#     ones are checked: ``five_hour`` is the burst window and
#     ``weekly_limit`` is what is left of the account, and a provider
#     can be inside either.
#   * **balance** — a tier whose ``unit`` is a currency. Its
#     ``used_percent`` is degenerate: CC Switch divides by a ``total``
#     of ``0``, so the field is *always* ``0.0`` and carries no
#     information at all. Reading that field is how a funded account
#     gets declared empty — which is worse than useless, because such an
#     account often ranks first. ``remaining > 0`` is the only question
#     to ask of this shape.
#   * **no reader** — ``success: false``, and no ``tiers`` key at all.
#     CC Switch could not meter that provider. This is not evidence of
#     anything, so it is *not* evidence against the provider: the
#     reading is absent, the provider stays selectable, and a real
#     refusal still gets caught by the runtime 429 path.
#
# The two readable shapes are not exclusive. A row may carry a currency
# and a window together, and then both budgets are independent and
# *both* are read — a row that carried one of each would otherwise be
# decided by whichever happened to be walked first, and a provider with
# an empty balance would pass on the strength of a window with room in
# it.
#
# No error text is ever parsed. ``"Unknown balance provider"`` and
# ``"当前用户不存在coding plan"`` are two vendors' phrasings of the same
# shape and a third will phrase it differently; a reader that keys on
# those strings is a reader that has to be patched per vendor. The
# ``success`` flag is the contract, and the flag is what is read.


#: The two percentage tiers that gate a dispatch. ``five_hour`` is the
#: rolling burst window; ``weekly_limit`` is what is left of the account
#: this week. Order is the lookup order for "which window ran out", so
#: a provider with both full is attributed to the shorter one — the
#: other one keeps it ineligible on its own regardless.
GATED_TIERS: Tuple[str, ...] = ("five_hour", "weekly_limit")

#: How long a *readable* quota reading is trusted before it is asked
#: for again. One number, not a knob: CC Switch answers this endpoint by
#: asking every provider's own API, so a request here is a fan-out
#: rather than a lookup, and a dispatch path that asks on every dispatch
#: turns into an outbound request per dispatch.
#:
#: This is also what makes the reading live without a cache clear: the
#: old file reader was consulted once per process, so a window that
#: refilled three minutes into a run stayed "full" for the rest of it.
_QUOTA_TTL_SEC: float = 60.0

#: Probe shapes, as recorded on a :class:`ProviderQuota`. Not an enum —
#: they travel into log lines and JSON-ish payloads, where a string is
#: cheaper to read than a qualified name.
SHAPE_ROLLING = "rolling"
SHAPE_BALANCE = "balance"
SHAPE_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class ProviderQuota:
    """What CC Switch's reading says about one provider's budget.

    ``remaining_pct`` and ``exhausted`` answer two different questions
    and both are kept, because callers use them for two different jobs:
    ``remaining_pct`` scales the concurrency cap (a provider at 8% left
    takes about a tenth of its slots), while ``exhausted`` decides
    whether it may be dispatched to at all.

    Attributes:
        provider: The CC Switch display name, spelled as CC Switch
            spells it — the same spelling the chain reader returns.
        shape: One of :data:`SHAPE_ROLLING`, :data:`SHAPE_BALANCE`,
            :data:`SHAPE_UNREADABLE`. A row carrying both a currency and
            a window reads as :data:`SHAPE_ROLLING`. Recorded rather
            than re-derived, so a log line can say *why* a verdict was
            reached.
        remaining_pct: Headroom in ``[0, 100]``, or ``None`` when the
            reading says nothing. For a balance provider this is
            ``100.0`` while a positive balance remains and ``0.0``
            otherwise, because a currency amount has no percentage.
        exhausted: True only when the reading affirmatively shows the
            provider has no budget. ``False`` covers both "there is
            budget" and "there is no reading" — those are different
            facts and :attr:`unreadable` separates them.
        exhausted_tier: Name of the tier that tripped the gate
            (``five_hour`` / ``weekly_limit``), or ``None``. Drives
            which ``resets_at`` becomes the cooldown length.
        exhausted_resets_at: That tier's ``resets_at``, or ``None`` when
            absent, unparseable, or already in the past.
        unreadable: True when CC Switch reported no usable meter for
            this provider. The verdict still admits it; what changes is
            how long this reading is trusted (see :func:`probe_floor_sec`).
    """

    provider: str
    shape: str = SHAPE_UNREADABLE
    remaining_pct: Optional[float] = None
    exhausted: bool = False
    exhausted_tier: Optional[str] = None
    exhausted_resets_at: Optional[datetime] = None
    unreadable: bool = False


def parse_resets_at(value: Any) -> Optional[datetime]:
    """Parse a CC Switch ``resets_at`` into an aware datetime, or ``None``.

    CC Switch writes that timestamp three ways in one payload: with an
    explicit UTC offset, with a bare ``Z``, and as JSON ``null``. The
    offset form parses directly; ``Z`` is rewritten before parsing
    rather than assumed, because the same backend is imported by a
    system interpreter whose :func:`datetime.fromisoformat` is older
    than the ``Z`` support. A naive stamp is read as UTC, which is what
    CC Switch means by it — the alternative, reading it as local time,
    silently shifts a cooldown by the machine's offset.

    ``None`` means "this reading cannot say", and every caller has a
    defined fallback for that; nothing raises.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def tier_used_percent(tier: Mapping[str, Any], now: datetime) -> Optional[float]:
    """Headroom-relevant ``used_percent`` for one tier, or ``None``.

    The stale-window rule lives here and nowhere else: a tier whose
    ``resets_at`` has already passed describes a window that has closed,
    and its ``used_percent`` is then last window's number. Reading it
    literally disqualifies a provider that is in fact freshly reset, and
    it stays that way for as long as the stale number is trusted — which
    is forever, because a 100% window does not update until the window
    rolls. So a closed window reports ``0.0``.

    ``NaN`` and the infinities are rejected as "no reading" rather than
    passed on: ``json.loads`` accepts all three, and a meter that cannot
    be read must cost the *reading*, not the provider.
    """
    used = tier.get("used_percent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    used = float(used)
    if used != used or used in (float("inf"), float("-inf")):
        return None

    resets_at = parse_resets_at(tier.get("resets_at"))
    if resets_at is not None and resets_at <= now:
        return 0.0
    return used


def _tier_map(record: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    """``{tier name: tier}`` from one ``/api/usage`` provider record."""
    tiers = record.get("tiers")
    if not isinstance(tiers, list):
        return {}
    out: Dict[str, Mapping[str, Any]] = {}
    for tier in tiers:
        if isinstance(tier, dict):
            name = tier.get("name")
            if isinstance(name, str) and name:
                out[name] = tier
    return out


def _no_reading(provider: str) -> ProviderQuota:
    """The verdict for a provider CC Switch could not meter.

    ``unreadable=True`` is what earns this reading the long probe floor
    in :func:`probe_floor_sec`, so it is set here rather than at each
    of the four call sites that can reach this verdict — an absent
    reading reached by a malformed row and one reached by a healthy
    fetch of a meter-less provider are the same fact, and only one of
    them being treated as cheap to re-ask would be a bug.
    """
    return ProviderQuota(
        provider=provider, shape=SHAPE_UNREADABLE, unreadable=True,
    )


def _quota_from_record(
    provider: str, record: Any, now: datetime
) -> ProviderQuota:
    """Classify one ``/api/usage`` row into a :class:`ProviderQuota`.

    Never raises. A row this reader cannot make sense of is an *absent*
    reading, which admits the provider — the same verdict a successful
    fetch of a meter-less provider produces, reached by a different
    route.
    """
    if not isinstance(record, dict):
        return _no_reading(provider)

    # No reader: CC Switch said it could not meter this provider, and
    # carried no tiers. Explicitly not a refusal.
    if not record.get("success", False):
        return _no_reading(provider)

    tiers = _tier_map(record)
    if not tiers:
        return _no_reading(provider)

    # Every tier is classified by its own ``unit`` rather than by a
    # single shape for the whole row, because a row that carries both a
    # currency and a window has two independent budgets and one answer.
    # Reading only the shape that happens to come first would let an
    # empty balance pass a provider whose window is full.
    window_tiers = {
        name: tier
        for name, tier in tiers.items()
        if str(tier.get("unit", "%")).strip() == "%"
    }
    balance_tiers = {
        name: tier
        for name, tier in tiers.items()
        if str(tier.get("unit", "")).strip() != "%"
    }

    # (1) The rolling windows. Both gated tiers are read: the burst
    #     window and the account's remaining week are independent
    #     budgets, and a provider with either one empty cannot be
    #     dispatched to.
    worst: Optional[float] = None
    window_exhausted_tier: Optional[str] = None
    window_resets_at: Optional[datetime] = None
    for name in GATED_TIERS:
        tier = window_tiers.get(name)
        if tier is None:
            continue
        used = tier_used_percent(tier, now)
        if used is None:
            continue
        worst = used if worst is None else max(worst, used)
        if used >= 100.0 and window_exhausted_tier is None:
            window_exhausted_tier = name
            window_resets_at = parse_resets_at(tier.get("resets_at"))

    # (2) The balance. ``used_percent`` is 0.0 for every funded account
    #     this shape produces (the payload divides by a ``total`` of 0),
    #     so that field carries no information at all and the amount is
    #     the only thing worth reading.
    balance_funded: Optional[bool] = None
    balance_exhausted_tier: Optional[str] = None
    balance_resets_at: Optional[datetime] = None
    if balance_tiers:
        readings = [
            float(tier["remaining"])
            for tier in balance_tiers.values()
            if isinstance(tier.get("remaining"), (int, float))
            and not isinstance(tier.get("remaining"), bool)
        ]
        if readings:
            balance_funded = min(readings) > 0.0
            if not balance_funded:
                balance_exhausted_tier = sorted(balance_tiers)[0]
                balance_resets_at = parse_resets_at(
                    balance_tiers[balance_exhausted_tier].get("resets_at")
                )

    if worst is None and balance_funded is None:
        # Tiers exist but none of them carried a usable number.
        return _no_reading(provider)

    exhausted = (worst is not None and worst >= 100.0) or (
        balance_funded is False
    )

    # A row carrying both reads as the rolling shape, which is the
    # stricter of the two labels: a currency amount is not a window and
    # has no moment attached to it.
    shape = SHAPE_ROLLING if worst is not None else SHAPE_BALANCE

    if worst is None:
        headroom = 100.0 if balance_funded else 0.0
    elif balance_funded is False:
        headroom = 0.0
    else:
        headroom = 100.0 - worst

    return ProviderQuota(
        provider=provider,
        shape=shape,
        remaining_pct=max(0.0, min(100.0, headroom)),
        exhausted=exhausted,
        # A window is preferred over a balance as the thing to name,
        # because a window is the one with a moment attached: a balance
        # is topped up by a person, so its length is a guess either way.
        exhausted_tier=window_exhausted_tier or balance_exhausted_tier,
        exhausted_resets_at=window_resets_at or balance_resets_at,
    )


def parse_usage_payload(
    payload: Any, now: Optional[datetime] = None
) -> Dict[str, ProviderQuota]:
    """Turn one ``/api/usage`` body into ``{provider: ProviderQuota}``.

    Split out from the HTTP leg so the three shapes can be tested
    against a recorded body without a server anywhere in sight.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("providers")
    if not isinstance(rows, list):
        return {}
    out: Dict[str, ProviderQuota] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("provider")
        if not isinstance(name, str) or not name or name in out:
            continue
        out[name] = _quota_from_record(name, row, now)
    return out


def _quota_from_api() -> Dict[str, ProviderQuota]:
    """Read every provider's quota from CC Switch.

    One ``GET`` to :data:`_USAGE_PATH`, no body, no query string. The
    request is a fan-out on CC Switch's side — it asks each provider's
    own API — so this is the call :data:`_QUOTA_TTL_SEC` exists to bound.

    Raises:
        ProviderOrderError: the request could not be completed, the
            status was not 200, or the body was not a usage payload.
            The same failure vocabulary as the chain reader, with the
            quota-specific reasons so a log line says which leg failed.
    """
    url = _api_usage_url()
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_API_TIMEOUT_SEC) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ProviderOrderError(
            REASON_QUOTA_UNREACHABLE, f"HTTP {exc.code}", url
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderOrderError(
            REASON_QUOTA_UNREACHABLE, f"{type(exc).__name__}: {exc}", url
        ) from exc

    if status != 200:
        raise ProviderOrderError(
            REASON_QUOTA_UNREACHABLE, f"HTTP {status}", url
        )

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderOrderError(
            REASON_QUOTA_BAD_PAYLOAD, f"{type(exc).__name__}: {exc}", url
        ) from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
        raise ProviderOrderError(
            REASON_QUOTA_BAD_PAYLOAD, "'providers' is missing or not a list", url
        )
    return parse_usage_payload(payload)


#: ``{provider: (refreshed_at_monotonic, quota)}``. Separate from
#: ``_usage_cache`` because the two hold different value types and have
#: different lifetimes: the file cache lives for the process, this one
#: expires on its own.
_quota_cache: Dict[str, Tuple[float, ProviderQuota]] = {}
_quota_cache_lock = threading.Lock()
_quota_refreshed_at: float = 0.0
#: Single key for the in-flight barrier: there is one CC Switch.
_QUOTA_CACHE_KEY = "quota"


def probe_floor_sec(quota: Optional[ProviderQuota]) -> float:
    """Seconds this reading is trusted before it is asked for again.

    A provider with a meter is trusted for :data:`_QUOTA_TTL_SEC`.
    A provider with **no** meter is trusted for the longer
    ``DEFAULT_QUOTA_COOLDOWN_SEC`` — which is exactly the half-hour the
    quota cooldown already uses, and is reused rather than redeclared so
    the two numbers cannot drift.

    The asymmetry is the point. A meter that says "empty" is worth
    acting on quickly, because acting on it stops dispatches. A meter
    that says nothing is worth re-asking rarely: it is the reading most
    likely to cost a request and return the same nothing, and this
    endpoint fans out to every provider's API on every call. Fail-open
    already keeps such a provider selectable, so nothing is lost by
    asking slowly.
    """
    if quota is None or not quota.unreadable:
        return _QUOTA_TTL_SEC
    try:
        from dynamic_provider_concurrency import DEFAULT_QUOTA_COOLDOWN_SEC
    except Exception:  # pragma: no cover - import cycle guard
        return _QUOTA_TTL_SEC
    return float(DEFAULT_QUOTA_COOLDOWN_SEC)


def load_provider_quota_map(*, force: bool = False) -> Dict[str, ProviderQuota]:
    """Return every provider's live quota, as CC Switch currently reads it.

    Cached for :data:`_QUOTA_TTL_SEC` per provider, with the floor per
    provider being :func:`probe_floor_sec` rather than the TTL alone, so
    a meter-less provider is re-asked only every half hour. Concurrent
    misses are serialized through the same per-key in-flight lock the
    chain reader uses, so N dispatches racing on a cold cache cost one
    fan-out rather than N.

    Args:
        force: Skip the cache and ask CC Switch again. ``False`` — the
            default, and what every dispatch wants — is unchanged.

            ``True`` is for a caller that has *deliberately waited* and
            needs to know what happened during the wait: a cache entry
            is stamped when it was fetched, so a read issued after a
            sleep shorter than :data:`_QUOTA_TTL_SEC` is handed a verdict
            from before the sleep and cannot report a window that
            reopened during it. The TTL exists to keep the dispatch path
            from asking once per dispatch, and a wake is not a dispatch
            — it is at most a handful per blocked call.

            It bypasses the two cache-hit checks and nothing else. The
            in-flight barrier is still taken, so concurrent forced reads
            cost one fan-out between them rather than one each, and the
            per-provider floor still applies: a meter-less verdict is
            carried forward as before even when the fetch was forced.

    Returns:
        ``{provider: ProviderQuota}``. **A provider missing from this map
        is not disqualified.** There is no second source to fall back
        on, so a failed read is reported as no reading at all and every
        provider stays selectable — a provider that genuinely cannot be
        called is caught by its own refusal at dispatch time, which is
        an observation rather than an inference from a missing file.

    Failure semantics — every failure mode returns ``{}``:

      * CC Switch unreachable / non-200 / unparseable → ``{}``, logged
        with :data:`REASON_QUOTA_UNREACHABLE` or
        :data:`REASON_QUOTA_BAD_PAYLOAD` so the line is greppable and
        distinguishable from the chain's own failure reasons.
      * No provider in the map → still ``{}``.

    A failed read is *not* cached as an empty answer: the next call
    re-asks, because the failure is the one case where "it is still
    broken" and "it just came back" have to be distinguishable and the
    cadence is bounded by the fan-out the next call costs. That holds
    for a forced read too — forcing buys a fresher answer, not a
    remembered one.
    """
    global _quota_refreshed_at
    now = time.monotonic()
    with _quota_cache_lock:
        if (
            not force
            and _quota_cache
            and now - _quota_refreshed_at < _QUOTA_TTL_SEC
        ):
            return {
                name: quota for name, (_, quota) in _quota_cache.items()
            }

    in_flight_lock, _version = _get_or_create_in_flight_lock(_QUOTA_CACHE_KEY)
    with in_flight_lock:
        now = time.monotonic()
        with _quota_cache_lock:
            if (
                not force
                and _quota_cache
                and now - _quota_refreshed_at < _QUOTA_TTL_SEC
            ):
                return {
                    name: quota for name, (_, quota) in _quota_cache.items()
                }

        try:
            fetched = _quota_from_api()
        except ProviderOrderError as exc:
            # Degraded, not empty-because-empty. The reason is logged
            # beside the chain's own reasons rather than folded into
            # them: "CC Switch is down" and "CC Switch has no usage
            # endpoint" are different operator problems, and one
            # another's reason token would hide both.
            log.warning(
                "cannot resolve the provider quota from CC Switch "
                "(reason=%s, detail=%s, url=%s); every provider stays "
                "selectable and quota cooldowns fall back to their "
                "half-hour default",
                exc.reason,
                exc.detail,
                exc.url,
                extra={"reason": exc.reason, "detail": exc.detail},
            )
            # Deliberately not cached: the next call re-asks. An outage
            # that ends must be noticed on the next dispatch, and the
            # cost of asking is one request, not a storm.
            with _quota_cache_lock:
                return {}
        except Exception:  # pragma: no cover - defensive
            log.warning(
                "the provider quota reader failed unexpectedly; every "
                "provider stays selectable",
                exc_info=True,
                extra={"reason": REASON_QUOTA_BAD_PAYLOAD},
            )
            with _quota_cache_lock:
                return {}

        # A meter-less reading is sticky for its own floor. The fetch
        # above already asked CC Switch about every provider, so this
        # does not reduce that fan-out — what it controls is *how long
        # a "no meter" verdict is carried forward*, which is the
        # promise this module makes: a provider that reports nothing is
        # re-examined at most once per half hour.
        stamp = time.monotonic()
        merged: Dict[str, Tuple[float, ProviderQuota]] = {}
        with _quota_cache_lock:
            for name, quota in fetched.items():
                previous = _quota_cache.get(name)
                floor = probe_floor_sec(quota)
                if (
                    quota.unreadable
                    and previous is not None
                    and previous[1].unreadable
                    and stamp - previous[0] < floor
                ):
                    merged[name] = previous
                    continue
                merged[name] = (stamp, quota)
            _quota_cache.clear()
            _quota_cache.update(merged)
            _quota_refreshed_at = stamp
        return {name: quota for name, (_, quota) in merged.items()}
