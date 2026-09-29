"""Card fingerprint — hash dedup for push gating.

Why this exists
---------------
The notifier receives a flood of events for every state change.
Without dedup, every event produces a Feishu API call. The user's
chat gets spammed with identical cards every few seconds.

The fix: hash the *meaningful* part of a card (everything except
the volatile refresh timestamp and the per-poll heartbeat field).
Two rebuilds that only differ in those two volatile fields hash
the same → notifier skips the push. Two rebuilds that differ in
any real field → notifier pushes.

The volatile regex matches the literal "刷新于 MM-DD HH:MM:SS"
note that ``cards.py`` stamps at the bottom of every card. The
``_heartbeat`` field is the top-level marker added in the same
place. Both change every rebuild but carry no operator-visible
information, so they MUST NOT participate in the dedup hash.

Moved verbatim from ``tools/task_sync/registry.py`` (lines 23-94,
that module was deleted 2026-09-13 together with the polling
task-sync bridge).
The regex string is unchanged — any operator who greps logs for
"刷新于" must still find the same matches in the new code.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Volatile timestamp regex
# ---------------------------------------------------------------------------
#
# Used to strip the "刷新于 MM-DD HH:MM:SS" note that ``cards.py`` stamps
# onto every card on every rebuild. This timestamp advances every cycle
# even when the underlying plan state is unchanged, so it must be
# removed before fingerprint hashing — otherwise unchanged plans would
# push on every rebuild.

_VOLATILE_TIMESTAMP_RE = re.compile(
    r"刷新于\s*\d{1,2}-\d{1,2}\s*\d{1,2}:\d{2}:\d{2}"
)


def _strip_heartbeat(content: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``content`` with the top-level ``_heartbeat`` key removed.

    ``cards.py`` stamps ``_heartbeat`` onto every card on every rebuild
    (see the comment at that call site). The value changes every cycle
    even when the underlying plan state is unchanged, so it MUST NOT
    contribute to the dedup hash.
    """
    return {k: v for k, v in content.items() if k != "_heartbeat"}


def _strip_volatile(obj: Any) -> Any:
    """Recursively remove the ``刷新于`` timestamp substring from every string."""
    if isinstance(obj, str):
        return _VOLATILE_TIMESTAMP_RE.sub("", obj)
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def card_fingerprint(content: Dict[str, Any]) -> str:
    """Return a stable SHA-256 fingerprint of meaningful card content.

    Two contents that differ only in the volatile ``刷新于`` timestamp
    OR in the ``_heartbeat`` field produce the same fingerprint.
    Dict key ordering is normalised via ``sort_keys=True`` so the
    fingerprint is independent of how the dict was constructed.

    The notifier compares this fingerprint against the one captured at
    the last successful push; equal → skip, different → push.
    """
    normalized = _strip_heartbeat(content)
    normalized = _strip_volatile(normalized)
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()