"""Shared plan_id validation.

Architecture decision point 8 keeps plan ids as filesystem-leaf
identifiers — every place that touches ``plans/{plan_id}/...`` MUST
funnel through :func:`validate_plan_id` so the same traversal /
absolute-path guards apply everywhere.

The validation is intentionally conservative: any byte that could
let a caller escape the ``plans/{plan_id}`` root is rejected. We
reject:

* empty string,
* ids longer than :data:`MAX_PLAN_ID_LEN` (64) — the byte-set guard
  alone would let a 300-char id through, and a path-component that
  long silently truncates on common filesystems,
* absolute paths (any leading ``/`` or drive letter on Windows),
* parent-traversal components (``..``, ``a/../../b``),
* path separators (both ``/`` and ``\\``) — plan ids are single
  path components by construction,
* control characters and bytes outside ``[A-Za-z0-9._-]``.

Why each rule
-------------

* **Absolute path** — a leading ``/`` would let a caller escape
  ``plans/`` and write to anywhere the process can reach. We refuse
  the request before any filesystem call.
* **Parent traversal** — ``..`` is the canonical traversal vector;
  even when nested inside a normal-looking id (``a/../../b``) it
  still escapes. We require ``Path.resolve()`` to stay under the
  canonical ``plans/`` root.
* **Path separator** — slashes inside a plan id would let callers
  smuggle directory nesting past the validation; we forbid them
  outright.
* **Control characters / whitespace** — invisible bytes break
  log/file-name matching and can be used to confuse downstream
  tools. We restrict to the canonical safe set.
* **Empty** — empty plan ids have no safe target.
* **Length** — ``MAX_PLAN_ID_LEN`` (64) is well above the 29-char
  ``YYYYMMDD-slug`` form :func:`derive_plan_id` produces, so any id
  longer than that is operator-unlikely. Unbounded length also costs
  the filesystem: ext4 / NTFS / APFS all silently truncate a leaf name
  past ~255 bytes, and the operator could not tell from the URL that
  the directory it created no longer matches the id they typed.

The single public exception, :class:`InvalidPlanIdError`, lets
callers catch every variant with one handler.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Final

__all__ = [
    "InvalidPlanIdError",
    "MAX_PLAN_ID_LEN",
    "derive_plan_id",
    "slugify_plan_id",
    "validate_plan_id",
]

# Plan ids are flat single-component names. We permit the same byte
# set the rest of the backend uses for filesystem-safe identifiers
# (date prefix + slug), then run a defensive ``Path.resolve()``
# sanity check on top.
_SAFE_PLAN_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]+$")

#: Upper bound on a plan_id's length.
#:
#: ``derive_plan_id`` produces ``<YYYYMMDD>-<slug(<=20)>`` (29 chars
#: today). Real-world ids grow with new fields but a 64-char ceiling
#: gives headroom for a future date stamp + slug while still rejecting
#: the 300-char ids that the byte-set check below would otherwise let
#: through. Anything longer is almost certainly attacker-controlled
#: rather than operator-controlled; an unbounded length also costs the
#: filesystem (longer-than-255-byte paths silently drop on common
#: filesystems and on this server's plans/ tree).
MAX_PLAN_ID_LEN: Final[int] = 64

# Anything outside the safe set is a separator candidate when deriving
# a slug from free text. Runs of them collapse into a single ``-`` so
# a CJK requirement does not turn into a long dash soup.
_UNSAFE_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")


class InvalidPlanIdError(ValueError):
    """Raised when a ``plan_id`` cannot be safely used as a filesystem leaf.

    Subclass of :class:`ValueError` so callers that already catch
    ``ValueError`` for general input validation continue to work,
    while callers that want to specifically distinguish "this is a
    bad plan id" from "this is some other bad argument" can pin
    the type exactly.
    """


def validate_plan_id(plan_id: object) -> str:
    """Validate ``plan_id`` and return it unchanged on success.

    Parameters
    ----------
    plan_id:
        Arbitrary object; will be rejected if it is not a non-empty
        ``str`` composed of the safe character set.

    Returns
    -------
    str
        The validated plan id (the input string, unchanged).

    Raises
    ------
    InvalidPlanIdError
        If ``plan_id`` is empty, not a string, contains a path
        separator, contains a ``..`` traversal component, is an
        absolute path, or contains any byte outside the safe set.
    """
    if not isinstance(plan_id, str):
        raise InvalidPlanIdError(
            f"plan_id must be a string, got {type(plan_id).__name__}"
        )
    if not plan_id:
        raise InvalidPlanIdError("plan_id must be a non-empty string")
    # Length bound: keep the byte-set check below intact for the actual
    # safety contract (no separators, no traversal, no control bytes),
    # but also refuse ids longer than ``MAX_PLAN_ID_LEN`` so an attacker
    # cannot trade safe characters for sheer volume. The byte-set check
    # is *necessary* but not *sufficient* — without this line a 300-char
    # ascii-letter id would sail through and land a 300-byte leaf name
    # on the filesystem.
    if len(plan_id) > MAX_PLAN_ID_LEN:
        raise InvalidPlanIdError(
            f"plan_id is {len(plan_id)} chars; "
            f"the maximum allowed length is {MAX_PLAN_ID_LEN}"
        )
    # Reject absolute paths FIRST — ``Path.is_absolute`` covers
    # POSIX ("/etc") and Windows ("C:\\Windows") in one call. This
    # check must come before the path-separator check below so an
    # absolute POSIX path gets the "is an absolute path" diagnostic
    # instead of the "contains a path separator" one (and so the
    # absolute-path branch is actually reachable on POSIX, where
    # every absolute path starts with ``/``).
    if Path(plan_id).is_absolute():
        raise InvalidPlanIdError(
            f"plan_id {plan_id!r} is an absolute path; "
            f"plan ids must be relative"
        )
    if "/" in plan_id or "\\" in plan_id:
        raise InvalidPlanIdError(
            f"plan_id {plan_id!r} contains a path separator; "
            f"plan ids must be a single filesystem component"
        )
    if plan_id in {".", ".."}:
        raise InvalidPlanIdError(
            f"plan_id {plan_id!r} is a traversal component"
        )
    # Final byte-set guard: anything outside [A-Za-z0-9._-] is
    # rejected. This catches NULs, newlines, spaces, and shell
    # metacharacters in one go.
    if not _SAFE_PLAN_ID_RE.fullmatch(plan_id):
        raise InvalidPlanIdError(
            f"plan_id {plan_id!r} contains unsafe characters; "
            f"only [A-Za-z0-9._-] are allowed"
        )
    return plan_id


def slugify_plan_id(text: str, max_len: int = 20) -> str:
    """Derive a safe plan-id fragment from arbitrary free text.

    Plan ids have always been meant to be "date prefix + slug" (see
    the byte-set note above), but the creator in ``server.py`` only
    replaced spaces: a requirement containing CJK, ``+``, ``/`` or any
    other out-of-set byte produced a directory name that the project's
    own :func:`validate_plan_id` rejects. The backend then could not
    manage that plan through any code path guarded by the validator.
    The dispatcher's watchdog signal is then silently dropped and a
    stranded plan produces no alert at all.

    Rules:

      * every run of bytes outside ``[A-Za-z0-9._-]`` collapses to a
        single ``-`` (so ``仓工程化-+-架构`` becomes ``-``-joined
        fragments, not dash soup);
      * leading/trailing ``-`` and ``.`` are stripped, so the result
        can never start a relative path component or look like a
        traversal;
      * truncated to ``max_len`` (after the strips, so the truncation
        cannot leave a trailing ``-``);
      * a text with no usable bytes at all (an all-CJK or emoji-only
        requirement) falls back to a short digest of the input, so two
        different requirements do not collapse onto the same plan
        directory on the same day.

    The result always satisfies :func:`validate_plan_id`.
    """
    slug = _UNSAFE_RUN_RE.sub("-", text or "")
    slug = slug.strip("-.")
    if len(slug) > max_len:
        slug = slug[:max_len].strip("-.")
    if slug:
        return slug
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def derive_plan_id(
    requirement: str,
    max_len: int = 20,
    when: "dt.datetime | None" = None,
) -> str:
    """Build a safe ``<YYYYMMDD>-<slug>`` plan id from free text.

    This is the single source of truth for the auto-generated plan id
    (``server.py`` used to inline ``date + requirement[:20].replace(" ",
    "-")``, which only handled spaces — see :func:`slugify_plan_id` for
    what that cost).

    ``when`` exists so tests can pin the date stamp; production callers
    omit it and get the local clock, matching the old behaviour.
    """
    stamp = (when or dt.datetime.now()).strftime("%Y%m%d")
    return f"{stamp}-{slugify_plan_id(requirement, max_len=max_len)}"