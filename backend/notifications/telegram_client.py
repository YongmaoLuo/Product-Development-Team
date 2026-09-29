"""Telegram transport — self-contained, configured from the environment.

Why this file exists
--------------------
The Telegram mirror of the progress cards used to borrow its transport
from a module **outside this repository**, resolved at call time with
``importlib.import_module("tools.<helper>.transport")``.
Three things were wrong with that, and any one of them is enough to
forbid the pattern:

1. **The module is not here.** ``tools/`` is operator-owned and
   gitignored; its contents are not part of any checkout. On a clean
   clone the import raised ``ModuleNotFoundError``, the notifier logged
   an exception and disabled the channel — so the Telegram mirror was
   dead for everyone except the one machine that happened to have that
   directory.
2. **It made an untracked path a runtime capability.** Whoever can
   create that module path in the
   server's working directory gets arbitrary code executed inside the
   server process, on the next card push, with the server's privileges.
   A module path assembled from a string is not a dependency; it is a
   plugin slot with no owner.
3. **Feishu never had this shape.** ``feishu_client.py`` — the other
   half of the exact same feature — was already localised and reads its
   credentials from the environment. Telegram was the leftover.

So the transport lives in this package now, and is configured exactly
like every other credential in this project: from the environment.

Environment variables consumed
------------------------------
``TELEGRAM_BOT_TOKEN``
    Bot token issued by BotFather. Required.
``TELEGRAM_CHAT_ID``
    Default target chat / channel id. Required unless the caller
    resolves a per-plan id itself (``TELEGRAM_CHAT_ID_<plan_id>``) and
    passes it to :func:`load_telegram_config`.

With no token, or no chat id, every call short-circuits: sends return
``None``, edits return ``False``, and no network call is made. That is
"not provisioned", which the notifier deliberately does **not** count
as a failure.

Contract
--------
``send_or_edit_telegram(text, config, last_message_id=None) -> int | None``
    Edit the given message, falling back to a fresh send; returns the id
    of the message that is now live on the channel, or ``None`` if
    nothing was delivered.

The text is sent **verbatim** with ``parse_mode=MarkdownV2``. Escaping
is the caller's job (:func:`escape_markdown_v2` is provided for it) —
the transport cannot know which characters are intended markup and
which are content, and a MarkdownV2 body with one unescaped reserved
character is rejected by Telegram with HTTP 400.

Nothing in here raises on a transport failure: a Telegram outage must
never be able to fail a plan's push loop, or crash the worker thread
that delivers Feishu cards.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

#: Telegram MarkdownV2 reserves these 18 characters; the API rejects the
#: request unless every occurrence is prefixed with a backslash.
MARKDOWN_V2_SPECIAL_CHARS = r"_*[]()~`>#+-=|{}.!"

#: The character above is not the whole story: a backslash that is not
#: itself the start of an escape sequence has to be doubled, otherwise a
#: body containing ``\.`` becomes an escape rather than a literal
#: ``\``+``.``. Telegram's own escaping advice includes it.
_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")

#: Default timeout for the HTTP POST to api.telegram.org. Module-level so
#: callers and tests can introspect it without importing httpx.
HTTP_TIMEOUT_SECONDS = 10.0

#: Telegram rejects ``sendMessage`` bodies longer than this. The renderer
#: truncates; the limit lives here so it is stated once.
MAX_MESSAGE_CHARS = 4096


def escape_markdown_v2(text: str) -> str:
    """Escape every character MarkdownV2 treats as markup.

    Callers interpolate arbitrary card content into a message body and
    then hand the result to :func:`send_or_edit_telegram` verbatim, so
    this is the one place that has to be exhaustive — a single unescaped
    ``!`` or ``.`` inside a line makes Telegram reject the whole
    message with HTTP 400 ``can't parse entities``.

    Args:
        text: arbitrary content. Must be a ``str``; callers stringify
            anything else first.

    Returns:
        The same text with every reserved character backslash-prefixed.
        The empty string comes back unchanged.
    """
    if not isinstance(text, str):
        raise TypeError(
            f"escape_markdown_v2 expects a str, got {type(text).__name__}"
        )
    if text == "":
        return ""
    return _ESCAPE_RE.sub(r"\\\1", text)


def mask_chat_id(chat_id: Optional[str]) -> Optional[str]:
    """Redact a chat id for the log stream, keeping its last 4 characters.

    A chat id is routing metadata: it can be used to enumerate who is on
    a channel. The logs still need to tell two ids apart while debugging
    a delivery failure, so the tail is kept.
    """
    if chat_id is None:
        return None
    s = str(chat_id)
    if s == "":
        return None
    if len(s) < 4:
        return "***" + s
    return "***" + s[-4:]


def load_telegram_config(chat_id: Optional[str] = None) -> Dict[str, Any]:
    """Build the transport config for one send, from the environment.

    Args:
        chat_id: target chat. Pass the per-plan value
            (``TELEGRAM_CHAT_ID_<plan_id>``) when the caller resolved
            one; when omitted, ``TELEGRAM_CHAT_ID`` is used.

    Returns:
        ``{"bot_token": str | None, "chat_id": str | None,
        "enabled": bool}`` — ``enabled`` is true only when both are
        present, which is what every transport function short-circuits
        on.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or None
    resolved_chat_id = (chat_id or os.environ.get("TELEGRAM_CHAT_ID")) or None
    return {
        "bot_token": bot_token,
        "chat_id": resolved_chat_id,
        "enabled": bool(bot_token) and bool(resolved_chat_id),
    }


def _coerce_message_id(value: Any) -> Optional[int]:
    """Return a positive ``int`` message id, or ``None``.

    Accepts a numeric string because ``PlanCardState.telegram_message_id``
    is persisted as a string in the on-disk JSON — a transport that only
    accepted an ``int`` would silently fall back to "send a fresh
    message" on every push, stacking a new card per update instead of
    editing the one the operator is watching.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
    return None


def _post(url: str, payload: Dict[str, Any]) -> Optional[httpx.Response]:
    """POST ``payload`` to ``url``, or ``None`` on any transport failure.

    The failure taxonomy is deliberate and is mirrored in the log lines
    (``timeout`` / ``connect_error`` / ``http_error`` / ``unexpected``):
    "Telegram is slow", "Telegram is unreachable" and "Telegram said no"
    are three different operational problems.
    """
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
            return client.post(url, json=payload)
    except httpx.TimeoutException:
        logger.warning("telegram_transport_fail error_type=timeout")
    except httpx.ConnectError:
        logger.warning("telegram_transport_fail error_type=connect_error")
    except httpx.HTTPError:
        logger.warning("telegram_transport_fail error_type=http_error")
    except Exception:  # noqa: BLE001 — a stray failure must not crash the worker
        logger.exception("telegram_transport_fail error_type=unexpected")
    return None


def send_to_telegram(text: str, config: Dict[str, Any]) -> Optional[int]:
    """Send a new message; return its id, or ``None`` if it was not sent.

    The text goes out verbatim under ``parse_mode=MarkdownV2`` — escape
    it with :func:`escape_markdown_v2` first, or the API answers 400.
    """
    if not isinstance(config, dict) or not config.get("enabled"):
        return None
    bot_token = config.get("bot_token")
    chat_id = config.get("chat_id")
    if not bot_token or not chat_id:
        return None

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
    }
    masked = mask_chat_id(chat_id)
    logger.info("telegram_send_start chat_id=%s", masked)
    started = time.monotonic()
    resp = _post(f"https://api.telegram.org/bot{bot_token}/sendMessage", payload)
    if resp is None:
        logger.warning(
            "telegram_send_fail chat_id=%s status_code=n/a latency_ms=%.1f",
            masked, (time.monotonic() - started) * 1000.0,
        )
        return None

    latency_ms = (time.monotonic() - started) * 1000.0
    if resp.status_code != 200:
        logger.warning(
            "telegram_send_fail chat_id=%s error_type=http_error "
            "status_code=%d latency_ms=%.1f body=%s",
            masked, resp.status_code, latency_ms, resp.text[:200],
        )
        return None

    try:
        message_id = resp.json().get("result", {}).get("message_id")
    except Exception:  # noqa: BLE001 — a malformed body is a failed send
        message_id = None
    if not isinstance(message_id, int):
        logger.warning(
            "telegram_send_ok_but_no_message_id chat_id=%s status_code=%s "
            "latency_ms=%.1f",
            masked, resp.status_code, latency_ms,
        )
        return None

    logger.info(
        "telegram_send_ok chat_id=%s status_code=%s message_id=%d latency_ms=%.1f",
        masked, resp.status_code, message_id, latency_ms,
    )
    return message_id


def edit_telegram_message(
    text: str, config: Dict[str, Any], message_id: Any
) -> bool:
    """Replace the text of an existing message. ``True`` when accepted.

    A ``False`` here is never fatal — the caller falls back to a fresh
    send. The common reasons are all non-fatal: the content did not
    change (HTTP 400 ``message is not modified``), the message was
    deleted, the bot was removed from the chat, or Telegram is down.
    """
    if not isinstance(config, dict) or not config.get("enabled"):
        return False
    bot_token = config.get("bot_token")
    chat_id = config.get("chat_id")
    if not bot_token or not chat_id:
        return False
    resolved_id = _coerce_message_id(message_id)
    if resolved_id is None:
        return False

    payload = {
        "chat_id": chat_id,
        "message_id": resolved_id,
        "text": text,
        "parse_mode": "MarkdownV2",
    }
    masked = mask_chat_id(chat_id)
    logger.info(
        "telegram_edit_start chat_id=%s message_id=%d", masked, resolved_id
    )
    started = time.monotonic()
    resp = _post(
        f"https://api.telegram.org/bot{bot_token}/editMessageText", payload
    )
    latency_ms = (time.monotonic() - started) * 1000.0

    if resp is None:
        logger.warning(
            "telegram_edit_fail chat_id=%s message_id=%d status_code=n/a "
            "latency_ms=%.1f",
            masked, resolved_id, latency_ms,
        )
        return False
    if resp.status_code != 200:
        # ``message is not modified`` lands here and is expected whenever
        # two consecutive renders are byte-identical.
        logger.warning(
            "telegram_edit_no_change chat_id=%s message_id=%d status_code=%d "
            "latency_ms=%.1f body=%s",
            masked, resolved_id, resp.status_code, latency_ms, resp.text[:200],
        )
        return False

    logger.info(
        "telegram_edit_ok chat_id=%s message_id=%d latency_ms=%.1f",
        masked, resolved_id, latency_ms,
    )
    return True


def send_or_edit_telegram(
    text: str, config: Dict[str, Any], last_message_id: Any = None
) -> Optional[int]:
    """Keep one live message per card: edit it, or send a fresh one.

    Returns the id of the message that is now live on the channel — the
    same id when the edit succeeded, the new id when it fell back to a
    send, or ``None`` when neither worked.
    """
    resolved_id = _coerce_message_id(last_message_id)
    if resolved_id is not None:
        if edit_telegram_message(text, config, resolved_id):
            return resolved_id
    return send_to_telegram(text, config)
