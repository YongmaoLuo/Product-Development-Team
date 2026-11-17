"""The post-processing (收尾) stage's failure notification.

What this module owns
---------------------
Three things, and nothing else:

1. the **three-part failure notification** — plan, failed items, evidence
   path — with no CPEA sections and no other prose;
2. the **delivery audit line**: one JSONL line per *actual* delivery,
   written into the plan's own directory;
3. **credential redaction** for the four credential shapes the security
   review named.

Why the notification is a bare three-part message
--------------------------------------------------
The report (``postprocess_report.json``) is the single source of truth
for the finishing stage: the panel's terminal zone and the notification
read the same file.  A notification that wrapped the report in a CPEA
coat would carry a second, softer version of the truth — and the two
would drift.  So the message names the plan, the items that failed and
the file a human opens to see why, and points there.

Why the audit line exists
-------------------------
"Exactly one notification was sent" is unobservable from outside the
process: a real server cannot be handed a Python sink, so the only
thing an end-to-end round can look at is what the process left on
disk.  Every **actual** delivery therefore appends one JSONL line to
``<plans>/<plan_id>/postprocess_notifications.jsonl`` — one line per
delivery, no more, no fewer.  The file is the plan's record of having
been notified, which is what makes a repeated call a no-op instead of
a second message: the line is already there, so nothing is delivered
and nothing is appended.

Why the channel is injected
---------------------------
The channel is the plan's already-bound HITL route, and this module
does not know how to reach it.  So the channel arrives as a ``sink``
and this module stays a *content* module: it decides what the message
says, delivers it once, and records that it did.  It grows no channel
of its own — no HTTP client, no Feishu/Telegram call — because a
second channel is a second set of credentials to leak, and the
architecture says the message goes out through the route the plan
already has.

Without a sink there is nothing to deliver through, and that is a
failure, not a reason to stay quiet: a swallowed failure notification
is how a plan dies without anybody noticing.  So the call raises
:class:`NotifyError` — and it raises *before* the audit line is
written, because a line in the audit file means "this was delivered",
and nothing was.

Why redaction lives here
------------------------
This module is what puts text on the wire, so this module is where a
credential has to stop.  The four shapes (``ghp_``, ``sk-``, ``xoxb-``
and ``Bearer``) are the ones the security review named; each is
replaced by ``<redacted>`` before the message is handed to the channel
or written to the audit file.

The patterns are anchored so that an ordinary identifier is not
mangled: ``sk-`` inside ``task-1`` is part of a word, not a
credential, and redacting it would hide the very item a human is
being told about.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from config_paths import resolve_plans_dir


#: The audit file's name, inside the plan's own directory.
NOTIFICATION_LOG_NAME = "postprocess_notifications.jsonl"

#: The notification's kind — the failure of the finishing stage.
NOTIFICATION_KIND = "postprocess_failed"

#: The three keys the payload carries, in the order they are written.
PAYLOAD_KEYS = ("plan_id", "failed_items", "evidence_path")

#: What a credential is replaced by.
REDACTED = "<redacted>"

#: The credential shapes that must never leave the process.
#:
#: Each pattern starts with a negative look-behind so that a credential
#: glued to the end of a longer word (``task-1`` contains ``sk-``) is
#: left alone: redacting it would corrupt an identifier the reader is
#: being asked to act on, and the pattern the security review named is
#: the credential, not the letters.
_SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])ghp_[A-Za-z0-9]+"),
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]+"),
    re.compile(r"(?<![A-Za-z0-9_-])xoxb-[A-Za-z0-9-]+"),
    re.compile(r"(?<![A-Za-z0-9_-])Bearer[ \t]+\S+"),
)


class NotifyError(RuntimeError):
    """A failure notification could not be delivered or recorded.

    Raised when there is no channel to deliver through, when the
    channel itself failed, and when the plan's id does not name a
    plan.  It is never swallowed: a failure notification that dies
    quietly is a plan that dies quietly.
    """


def redact(text: str) -> str:
    """Replace every credential in ``text`` with ``<redacted>``.

    Args:
        text: The text to clean — typically a command's stdout excerpt.

    Returns:
        The same text with each credential replaced.  The text around
        the credential is untouched, so the line stays readable.
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def notification_log_path(plan_id: str) -> Path:
    """The audit file of one plan's post-processing notifications.

    Args:
        plan_id: The plan the notifications are about.

    Returns:
        ``<plans root>/<plan_id>/postprocess_notifications.jsonl``.  The
        plans root is resolved the way the rest of the backend resolves
        it (``PDT_PLANS_DIR`` wins), so a test or a harness that
        redirects the plans tree redirects the audit file with it.

    Raises:
        NotifyError: The plan id does not name a plan.
    """
    return resolve_plans_dir() / _validated_plan_id(plan_id) / NOTIFICATION_LOG_NAME


def notify_failure(
    plan_id: str,
    failed_items: Iterable[str],
    evidence_path: str,
    *,
    sink: Any = None,
) -> Dict[str, Any]:
    """Deliver the three-part failure notification for one plan.

    The message says three things — the plan, the items that failed and
    the file a human opens to see why — and is delivered **once** per
    plan.  A repeated call finds the audit line already written, so it
    delivers nothing, appends nothing, and reports ``already_sent``.

    Args:
        plan_id: The plan whose finishing stage failed.
        failed_items: The ids of the items that failed.
        evidence_path: Where the failure context lives — the progress
            file a human resumes from.
        sink: The channel to deliver through.  Either an object with a
            ``notify(plan_id, kind, payload)`` method (the shape the
            test fixtures' notifier has) or a callable taking the
            payload.

    Returns:
        The delivered payload — ``plan_id``, ``failed_items`` and
        ``evidence_path`` — plus ``already_sent``: ``False`` when this
        call delivered the notification, ``True`` when it was already
        sent.

    Raises:
        NotifyError: There is no channel, the channel failed, the plan
            id does not name a plan, or the failed items were given as
            a bare string.  In every one of those cases **no audit line
            is written** — a line means "delivered", and nothing was
            delivered.
    """
    _validated_plan_id(plan_id)
    if sink is None:
        raise NotifyError(
            f"plan {plan_id!r} has no notification channel: a failure "
            f"notification must never be swallowed"
        )
    if isinstance(failed_items, str):
        # A bare string would be iterated character by character, and the
        # reader would be told that seven items failed instead of one.
        raise NotifyError(
            f"failed_items must be a list of item ids, not the string "
            f"{failed_items!r}"
        )

    payload: Dict[str, Any] = {
        "plan_id": redact(str(plan_id)),
        "failed_items": [redact(str(item)) for item in failed_items],
        "evidence_path": redact(str(evidence_path)),
    }

    log_path = notification_log_path(plan_id)
    if _already_delivered(log_path):
        return {**payload, "already_sent": True}

    _deliver(sink, plan_id, payload)
    _append_audit_line(log_path, payload)
    return {**payload, "already_sent": False}


def _validated_plan_id(plan_id: Any) -> str:
    """Return ``plan_id`` if it names a plan, raise otherwise.

    A plan id becomes a path segment, so an id carrying a separator
    would let a notification — and its audit file — land outside the
    plan's own directory.  The check is cheap and the failure is loud.
    """
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise NotifyError(f"plan id must be a non-empty string, got {plan_id!r}")
    if (
        plan_id in {".", ".."}
        or "/" in plan_id
        or "\\" in plan_id
        or "\x00" in plan_id
    ):
        raise NotifyError(f"plan id must not name a path: {plan_id!r}")
    return plan_id


def _already_delivered(log_path: Path) -> bool:
    """Whether this plan's audit file already records a delivery."""
    if not log_path.exists():
        return False
    try:
        content = log_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.strip() for line in content.splitlines())


def _deliver(sink: Any, plan_id: str, payload: Mapping[str, Any]) -> None:
    """Hand the payload to the channel, or say why it could not be.

    Raises:
        NotifyError: The channel does not accept the payload, or the
            channel failed.
    """
    notifier = getattr(sink, "notify", None)
    try:
        if callable(notifier):
            notifier(plan_id, NOTIFICATION_KIND, payload)
        elif callable(sink):
            sink(payload)
        else:
            raise NotifyError(
                f"notification sink {sink!r} has no notify() method"
            )
    except NotifyError:
        raise
    except Exception as exc:  # noqa: BLE001 — the cause must not vanish
        raise NotifyError(
            f"delivering the failure notification for plan {plan_id!r} "
            f"failed: {exc}"
        ) from exc


def _append_audit_line(log_path: Path, payload: Mapping[str, Any]) -> None:
    """Append one JSONL line recording one actual delivery.

    The file is created private (0600), like the other artifacts that
    may quote a command's output.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(mode=0o600, exist_ok=True)
    os.chmod(log_path, 0o600)
    line = json.dumps(payload, ensure_ascii=False)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")