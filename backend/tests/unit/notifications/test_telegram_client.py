"""Telegram transport: env-configured, in-repo, and never raising.

Why this suite exists
---------------------
The Telegram mirror used to get its transport from a module *outside*
this repository, imported by dotted string. Nothing pinned that
behaviour, and two defects hid behind it:

  * ``tools/`` is operator-owned and gitignored, so on any clean checkout
    the import raised — the channel was dead everywhere except the one
    machine that happened to own that directory;
  * ``PlanCardState.telegram_message_id`` is persisted as a **string**,
    and the transport only accepted an ``int``. Even on the machine where
    the import worked, every push concluded "no message yet" and sent a
    brand-new message instead of editing the card being watched.

So the tests below pin the contract the notifier actually depends on:
escaping, env-only configuration, exactly one live message per card, and
failure paths that return instead of raising (a Telegram outage must not
be able to fail a push loop or kill the notifier's worker thread).
"""

from __future__ import annotations

import json

import httpx
import pytest

from notifications import telegram_client as tc


# ---------------------------------------------------------------------------
# HTTP double — a real httpx client over a MockTransport
# ---------------------------------------------------------------------------


#: Bound at import time: the patch below replaces ``httpx.Client``, so a
#: factory that spelled ``httpx.Client`` would call itself forever.
_REAL_CLIENT = httpx.Client


def _install_transport(monkeypatch, handler):
    """Make ``telegram_client``'s httpx calls go through ``handler``.

    Injects a real ``httpx.Client`` built on ``httpx.MockTransport``
    rather than stubbing the module's own ``_post``: that keeps the
    request construction (URL, payload, ``parse_mode``) under test.
    """
    def factory(**kwargs):
        return _REAL_CLIENT(
            transport=httpx.MockTransport(handler),
            timeout=kwargs.get("timeout"),
        )

    monkeypatch.setattr(tc.httpx, "Client", factory)


class _Recorder:
    """Captures every request a handler sees."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def echo_ok(self, message_id: int = 42):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})
        return handler


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    return tc.load_telegram_config()


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


def test_escape_covers_every_markdown_v2_reserved_character():
    """One unescaped reserved character makes Telegram reject the message.

    The list is Telegram's own; if the API ever grows a 19th character,
    this test is where it should be noticed.
    """
    for char in tc.MARKDOWN_V2_SPECIAL_CHARS:
        assert tc.escape_markdown_v2(f"a{char}b") == f"a\\{char}b"


def test_escape_doubles_a_literal_backslash():
    """``\\`` must be escaped too, or it swallows the next character."""
    assert tc.escape_markdown_v2("a\\b") == "a\\\\b"


def test_escape_leaves_ordinary_text_alone():
    assert tc.escape_markdown_v2("2 failed, 3 passed\nVP-001 ok") == (
        "2 failed, 3 passed\nVP\\-001 ok"
    )
    assert tc.escape_markdown_v2("") == ""


def test_escape_rejects_non_strings():
    with pytest.raises(TypeError):
        tc.escape_markdown_v2(42)


def test_mask_chat_id_keeps_only_the_tail():
    assert tc.mask_chat_id("-1001234567890") == "***7890"
    assert tc.mask_chat_id("12") == "***12"
    assert tc.mask_chat_id("") is None
    assert tc.mask_chat_id(None) is None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_config_is_disabled_without_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = tc.load_telegram_config()
    assert config == {"bot_token": None, "chat_id": None, "enabled": False}


def test_config_is_disabled_with_a_token_but_no_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert tc.load_telegram_config()["enabled"] is False


def test_config_accepts_a_per_plan_chat_id_on_its_own(monkeypatch):
    """``TELEGRAM_CHAT_ID_<plan>`` is a supported setup by itself.

    The notifier resolves the per-plan id and passes it in; the shared
    variable is only the fallback.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = tc.load_telegram_config(chat_id="-100999")
    assert config["enabled"] is True
    assert config["chat_id"] == "-100999"


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def test_send_does_not_touch_the_network_when_not_provisioned(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    def explode(request):  # pragma: no cover — must not be reached
        raise AssertionError("network call made for a disabled channel")

    _install_transport(monkeypatch, explode)
    assert tc.send_to_telegram("hi", tc.load_telegram_config()) is None


def test_send_posts_markdown_v2_and_returns_the_message_id(monkeypatch, configured):
    recorder = _Recorder()
    _install_transport(monkeypatch, recorder.echo_ok(1402))

    assert tc.send_to_telegram("*title*", configured) == 1402

    request = recorder.requests[0]
    assert str(request.url).endswith("/bot123456:test-token/sendMessage")
    payload = json.loads(recorder.requests[0].read())
    assert payload["parse_mode"] == "MarkdownV2"
    assert payload["chat_id"] == "-1001234567890"
    assert payload["text"] == "*title*"


def test_send_returns_none_on_a_rejected_request(monkeypatch, configured):
    def handler(request):
        return httpx.Response(400, json={"ok": False, "description": "can't parse entities"})

    _install_transport(monkeypatch, handler)
    assert tc.send_to_telegram("oops", configured) is None


def test_send_returns_none_when_the_response_has_no_message_id(monkeypatch, configured):
    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": {}})

    _install_transport(monkeypatch, handler)
    assert tc.send_to_telegram("hi", configured) is None


def test_send_returns_none_on_timeout(monkeypatch, configured):
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    _install_transport(monkeypatch, handler)
    assert tc.send_to_telegram("hi", configured) is None


def test_send_returns_none_on_an_unexpected_exception(monkeypatch, configured):
    """The last-resort catch: a stray error must not reach the worker."""
    def handler(request):
        raise ValueError("something nobody anticipated")

    _install_transport(monkeypatch, handler)
    assert tc.send_to_telegram("hi", configured) is None


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def test_edit_accepts_the_string_id_the_state_file_holds(monkeypatch, configured):
    """This is the regression that made every push a new message.

    ``PlanCardState`` persists ``telegram_message_id`` as a string, so a
    transport that insists on ``int`` silently degrades "edit the card" to
    "send another card" forever.
    """
    recorder = _Recorder()
    _install_transport(monkeypatch, recorder.echo_ok())

    assert tc.edit_telegram_message("body", configured, "1402") is True
    assert str(recorder.requests[0].url).endswith("/editMessageText")
    assert json.loads(recorder.requests[0].read())["message_id"] == 1402


@pytest.mark.parametrize("bad", [None, 0, -3, "abc", "", True])
def test_edit_rejects_unusable_message_ids(monkeypatch, configured, bad):
    def explode(request):  # pragma: no cover — must not be reached
        raise AssertionError("network call made with an unusable message id")

    _install_transport(monkeypatch, explode)
    assert tc.edit_telegram_message("body", configured, bad) is False


def test_edit_returns_false_on_a_rejected_edit(monkeypatch, configured):
    """``message is not modified`` and friends are expected, not fatal."""
    def handler(request):
        return httpx.Response(400, json={"ok": False, "description": "message is not modified"})

    _install_transport(monkeypatch, handler)
    assert tc.edit_telegram_message("body", configured, 1402) is False


# ---------------------------------------------------------------------------
# send_or_edit — exactly one live message per card
# ---------------------------------------------------------------------------


def test_send_or_edit_edits_the_live_message(monkeypatch, configured):
    recorder = _Recorder()
    _install_transport(monkeypatch, recorder.echo_ok())

    assert tc.send_or_edit_telegram("body", configured, "1402") == 1402
    assert len(recorder.requests) == 1
    assert str(recorder.requests[0].url).endswith("/editMessageText")


def test_send_or_edit_falls_back_to_a_send_when_the_edit_fails(monkeypatch, configured):
    """A deleted message must not silence the card."""
    seen: list[str] = []

    def handler(request):
        seen.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("editMessageText"):
            return httpx.Response(400, json={"ok": False, "description": "message to edit not found"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7777}})

    _install_transport(monkeypatch, handler)
    assert tc.send_or_edit_telegram("body", configured, "1402") == 7777
    assert seen == ["editMessageText", "sendMessage"]


def test_send_or_edit_sends_when_there_is_no_message_yet(monkeypatch, configured):
    recorder = _Recorder()
    _install_transport(monkeypatch, recorder.echo_ok(555))
    assert tc.send_or_edit_telegram("body", configured, None) == 555
    assert len(recorder.requests) == 1


def test_send_or_edit_returns_none_when_both_paths_fail(monkeypatch, configured):
    def handler(request):
        return httpx.Response(500, json={"ok": False})

    _install_transport(monkeypatch, handler)
    assert tc.send_or_edit_telegram("body", configured, "1402") is None
