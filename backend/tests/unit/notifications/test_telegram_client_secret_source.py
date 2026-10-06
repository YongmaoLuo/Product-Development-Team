"""Unit: the Telegram bot token is resolved by the credentials provider.

Why this suite exists
---------------------
The bot token is a **secret**, so it stopped being read out of the
environment by this module and went through
:func:`credentials.read_secret`, which decides — once, in one table —
between the OS keychain and the plaintext variable. The chat id is not a
secret, it is an *index*: the keychain is looked up by it, and routing
metadata has to be readable by whatever decides where a card goes. So
exactly one of the two values on this function moved sources and the
other did not.

The change is one line, and one line is exactly what hides a defect. Two
shapes of the same mistake are pinned here:

* the old ``os.environ.get("TELEGRAM_BOT_TOKEN") or <provider>`` — an
  operator with a keychain item and an *empty* variable left over from
  the pre-keychain setup gets an empty token, because ``or`` is a
  fallback operator and the migration was wired in as one;
* reading the variable again here at all, so the two sources can
  disagree inside a single process and the channel is provisioned or not
  depending on which question was asked first.

Both would be invisible from the outside — the send either works or
quietly does not — so the assertions below name the value that must come
out, not the shape of the code that produced it.

Scope note: this is a **unit** suite. The provider is patched at its own
public seam rather than being made to shell out to a keychain, so no test
here reads a real credential or starts a real process. The suite that
pins the provider's own keychain call is a separate one.
"""

from __future__ import annotations

import pytest

import credentials
from notifications import telegram_client as tc

#: The logical name the provider registers the token under. Spelled here
#: so that a rename on either side fails the test rather than silently
#: resolving a name that is in no spec table — and a name in no spec table
#: answers ``None``, which is the "not provisioned" branch of every
#: assertion below.
BOT_TOKEN_SECRET = "telegram_bot_token"


@pytest.fixture(autouse=True)
def _empty_secret_memo():
    """Keep the provider's process-lifetime memo out of these tests.

    :func:`credentials.read_secret` memoises a resolution for the life of
    the process, so a test that resolves a token would otherwise hand its
    answer — or its ``None`` — to every test that runs after it. Cleared
    on both sides of the test: what a test reads must not outlive it.
    """
    credentials.reset_cache()
    yield
    credentials.reset_cache()


@pytest.fixture
def provider_gives(monkeypatch):
    """Make the provider answer ``value``, and report what it was asked."""

    def _apply(value):
        asked: list[str] = []

        def _fake_read_secret(name):
            asked.append(name)
            return value

        monkeypatch.setattr(credentials, "read_secret", _fake_read_secret)
        return asked

    return _apply


# ---------------------------------------------------------------------------
# The token comes from the provider
# ---------------------------------------------------------------------------


def test_bot_token_comes_from_provider(monkeypatch, provider_gives):
    """The config's token is whatever the provider resolved.

    A provider stub, not a real lookup: this asserts *that the provider is
    the source and is asked for the right logical name*, which is the
    whole of the migration.
    """
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    asked = provider_gives("123456:sentinel-from-provider")

    config = tc.load_telegram_config()

    assert asked == [BOT_TOKEN_SECRET], "the token was not read from the provider"
    assert config["bot_token"] == "123456:sentinel-from-provider"
    assert config["enabled"] is True


def test_provider_value_survives_empty_env_value(monkeypatch, provider_gives):
    """An empty ``TELEGRAM_BOT_TOKEN`` must not swallow the provider's value.

    This is the state a machine is in halfway through the migration: the
    keychain item exists, and the plaintext variable is still exported as
    an empty string. Read as a fallback, the empty string wins and the
    channel silently reports "not provisioned" while the operator has a
    working keychain entry.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    provider_gives("123456:sentinel-from-provider")

    config = tc.load_telegram_config()

    assert config["bot_token"] == "123456:sentinel-from-provider"
    assert config["bot_token"], "an empty variable short-circuited the provider"
    assert config["enabled"] is True


# ---------------------------------------------------------------------------
# The disabled path keeps its old semantics
# ---------------------------------------------------------------------------


def test_disabled_path_keeps_empty_env_semantics(monkeypatch):
    """With the keychain off, an empty variable still means "not provisioned".

    The provider is **not** patched here: this exercises the real switch
    and the real fallback read, because "an empty variable is not a value"
    is a rule the provider owns, and a stubbed provider would only assert
    that this function believes the rule. Disabled is a value that does
    not mean "do not disable" — the switch is fail-closed, so this
    spelling leaves the keychain off on any platform.
    """
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")

    config = tc.load_telegram_config()

    assert config["bot_token"] is None
    assert config["chat_id"] == "-1001234567890", "the chat id is not a secret"
    assert config["enabled"] is False


def test_disabled_path_still_reads_the_fallback_variable(monkeypatch):
    """Keychain off, variable populated: the transport is provisioned.

    The other half of the disabled path. Without it, "an empty variable
    disables the channel" would also be satisfied by a function that
    never reads the variable at all.
    """
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:from-the-environment")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")

    config = tc.load_telegram_config()

    assert config["bot_token"] == "123456:from-the-environment"
    assert config["enabled"] is True


def test_nothing_configured_is_disabled_and_does_not_raise(
    monkeypatch, provider_gives
):
    """Neither side has anything: disabled, and no exception.

    "Not provisioned" is a state the notifier handles, not an error. A
    migration that lets a missing secret propagate would take down the
    card push for a deployment that never asked for Telegram at all.
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    provider_gives(None)

    config = tc.load_telegram_config()

    assert config == {"bot_token": None, "chat_id": None, "enabled": False}


# ---------------------------------------------------------------------------
# What did not change
# ---------------------------------------------------------------------------


def test_returned_key_set_is_unchanged(monkeypatch, provider_gives):
    """Exactly ``bot_token``, ``chat_id``, ``enabled`` — no more, no less.

    Every transport function reads this dict by key, so an added key is
    invisible and a removed one is a ``KeyError`` at send time rather than
    at the call that changed the shape.
    """
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    provider_gives("123456:sentinel-from-provider")

    config = tc.load_telegram_config()

    assert set(config) == {"bot_token", "chat_id", "enabled"}


def test_chat_id_resolution_is_untouched(monkeypatch, provider_gives):
    """Per-plan id beats the shared one; the shared one is the fallback.

    The chat id stayed on ``os.environ`` because it is an index rather
    than a secret, and this pins the properties that a careless edit to
    the line next door would break: the caller's value wins, the shared
    variable is consulted only when the caller passed nothing, and an
    empty value on both sides is ``None`` rather than ``""`` — the
    transports short-circuit on falsiness, and an empty chat id is a
    channel nobody is in.
    """
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    provider_gives("123456:sentinel-from-provider")

    per_plan = tc.load_telegram_config(chat_id="-100999")
    assert per_plan["chat_id"] == "-100999"
    assert per_plan["enabled"] is True

    shared = tc.load_telegram_config()
    assert shared["chat_id"] == "-1001234567890"

    # An empty caller value is falsy, so it falls through to the shared
    # variable rather than overriding it with "".
    empty_falls_through = tc.load_telegram_config(chat_id="")
    assert empty_falls_through["chat_id"] == "-1001234567890"

    # With nothing on either side it normalises to None, not to "".
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert tc.load_telegram_config(chat_id="")["chat_id"] is None


def test_chat_id_does_not_come_from_the_provider(monkeypatch, provider_gives):
    """A provider that answers for every name still cannot supply the id.

    The provider is asked for the token and nothing else: a chat id is
    not a secret, and routing it through the keychain would make a
    non-secret part of the deployment unreadable by the tools that
    decide where cards go.
    """
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    asked = provider_gives("123456:sentinel-from-provider")

    config = tc.load_telegram_config(chat_id="-100999")

    assert asked == [BOT_TOKEN_SECRET]
    assert config["chat_id"] == "-100999"
