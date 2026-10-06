"""The Telegram probes must ask the secret provider, not the environment.

Why this suite exists
---------------------
The bot token is a secret, and a secret belongs behind
:mod:`credentials` rather than in a variable every process of every user
on the machine can read. The notifier's two availability probes
never moved with it: they read ``os.environ`` directly, so an operator
who re-keys the token into the keychain ends up with a channel that
holds a working credential and is still reported as disabled. The probe
is what decides whether the transport is called at all, so the failure
is a channel that is silently dead — the same shape as the module
docstring's item 3, and equally invisible, because "channel disabled"
is also what a genuinely unprovisioned operator looks like.

What is pinned here:

  * both probes take the token from ``credentials.read_secret``, so a
    provider-sourced token with nothing in the environment still counts
    as provisioned;
  * the **chat id** side is untouched — it stays a plain environment
    lookup, per-plan first, with the same precedence and the same
    prefix scan. Rewriting the probe must not quietly re-order, re-key
    or provider-ise the three chat-id reads;
  * with neither a token nor a chat id the probes answer false quietly
    rather than raising, which is the state an unconfigured install is
    in and the state the notifier already knows how to be in;
  * the disabled log no longer tells the operator to go and fix their
    environment, because the token is no longer the environment's to fix;
  * a credential that is simply *absent* is a value the whole chain can
    carry rather than an error any layer has to handle: ``credentials``
    answers ``None`` / ``"missing"`` without raising, and a
    ``FeishuUnavailable`` out of the transport client leaves the notifier
    disabled but still subscribed and still running its worker — a
    service that cannot push a card must still be a service that starts.
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

import credentials
from notifications import feishu_notifier as fn
from notifications.feishu_client import FeishuUnavailable
from notifications.feishu_notifier import FeishuNotifier, PlanCardState

#: Stands in for whatever the provider hands back. Its value is
#: deliberately not token-shaped: a test that only checks truthiness
#: would also pass if the probe fell back to some other truthy value.
SENTINEL_TOKEN = "<sentinel token>"


def _card() -> dict:
    return {
        "header": {"title": {"content": "验收完成"}},
        "plan_id_tag": "p1",
        "sections": [{"text": {"content": "run 1.0 done!"}}],
    }


def _clear_telegram_env(monkeypatch) -> None:
    """Leave the process with no Telegram configuration whatsoever."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    for key in [k for k in os.environ if k.startswith("TELEGRAM_CHAT_ID_")]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_memoised_secret():
    """Give every test a resolution of its own.

    ``credentials`` memoises a secret for the life of the process, on
    the grounds that a secret is a property of the deployment. In a
    test the deployment is whatever the last ``setenv`` made it, so
    without this the suite would be reading a previous test's answer.
    """
    credentials.reset_cache()
    yield
    credentials.reset_cache()


@pytest.fixture
def provider_token(monkeypatch):
    """The provider answers with a token; the environment holds none.

    This is the shape the keychain migration produces, and it is the
    one that broke: env empty, credential present.
    """
    _clear_telegram_env(monkeypatch)

    def _read_secret(name: str):
        return SENTINEL_TOKEN if name == "telegram_bot_token" else None

    monkeypatch.setattr(credentials, "read_secret", _read_secret)
    return SENTINEL_TOKEN


# ---------------------------------------------------------------------------
# The probes take the token from the provider
# ---------------------------------------------------------------------------


def test_provisioned_uses_provider_token(monkeypatch, provider_token):
    """Env has no token, the provider does — the channel is usable."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID_p1", "-100999")

    # The precondition, stated rather than assumed: the token really is
    # absent from the environment, so a passing probe cannot be a probe
    # that still reads env.
    assert "TELEGRAM_BOT_TOKEN" not in os.environ
    assert fn._telegram_channel_provisioned("p1") is True


def test_provisioned_any_uses_provider_token(monkeypatch, provider_token):
    """The plan-agnostic probe is gated on the same source."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")

    assert "TELEGRAM_BOT_TOKEN" not in os.environ
    assert fn._telegram_channel_provisioned_any() is True


def test_status_reports_enabled_from_a_provider_sourced_token(
    monkeypatch, provider_token,
):
    """The operator-facing surface follows the probe, not the env."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID_p1", "-100999")

    stats = FeishuNotifier().stats()

    assert stats["telegram_enabled"] is True
    assert not stats.get("telegram_disabled_reason")


def test_probe_is_unchanged_when_the_keychain_switch_is_off(monkeypatch):
    """With the switch off the provider reads the environment.

    This is the fail-closed default — a CI runner, a container, Linux
    — and it has to behave exactly as it did before the provider was
    consulted, or every non-keychain install loses its channel.
    """
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "1")
    credentials.reset_cache()
    assert fn._telegram_channel_provisioned("p1") is False

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_p1", "-100999")
    credentials.reset_cache()

    assert fn._telegram_channel_provisioned("p1") is True
    assert fn._telegram_channel_provisioned_any() is True


# ---------------------------------------------------------------------------
# The chat-id side is untouched
# ---------------------------------------------------------------------------


def test_provisioned_any_scans_per_plan_prefix_ids(monkeypatch, provider_token):
    """A per-plan id alone is a supported configuration."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID_p1", "-100999")

    assert fn._telegram_channel_provisioned_any() is True


def test_resolve_telegram_chat_id_priority_is_unchanged(
    monkeypatch, provider_token,
):
    """Per-plan beats shared beats nothing — and the token is not one.

    The provider is standing in with a value throughout, so this also
    pins that the chat-id lookup was not quietly routed through it: a
    bot token must never be reachable as a chat id.
    """
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100shared")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_p1", "-100per-plan")
    assert fn._resolve_telegram_chat_id("p1") == "-100per-plan"

    monkeypatch.delenv("TELEGRAM_CHAT_ID_p1")
    assert fn._resolve_telegram_chat_id("p1") == "-100shared"

    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert fn._resolve_telegram_chat_id("p1") is None


# ---------------------------------------------------------------------------
# Nothing configured
# ---------------------------------------------------------------------------


def test_probes_false_when_nothing_configured(monkeypatch):
    """No token, no chat id: false, and no exception on the way there."""
    _clear_telegram_env(monkeypatch)
    monkeypatch.setattr(credentials, "read_secret", lambda name: None)

    assert fn._telegram_channel_provisioned("p1") is False
    assert fn._telegram_channel_provisioned_any() is False


def test_token_without_a_chat_id_is_not_provisioned(
    monkeypatch, provider_token,
):
    """The token was never sufficient on its own; keep it that way."""
    assert fn._telegram_channel_provisioned("p1") is False
    assert fn._telegram_channel_provisioned_any() is False


# ---------------------------------------------------------------------------
# The operator-facing log
# ---------------------------------------------------------------------------


def test_disabled_log_does_not_blame_the_environment(monkeypatch, caplog):
    """The disabled message must not send the operator to fix env.

    The token is no longer a variable they can set, so an instruction
    to do so is not advice — it is a dead end. The chat ids *are*
    still environment variables, so those must stay named.
    """
    _clear_telegram_env(monkeypatch)
    monkeypatch.setattr(credentials, "read_secret", lambda name: None)
    monkeypatch.setattr(fn, "send_or_edit_telegram", lambda *a, **k: None)

    with caplog.at_level(logging.INFO, logger=fn.logger.name):
        pushed = FeishuNotifier()._push_to_telegram(
            PlanCardState(plan_id="p1"), _card(),
        )

    message = "\n".join(r.getMessage() for r in caplog.records)
    assert pushed is False
    assert "telegram channel disabled" in message
    assert "until env" not in message
    assert "TELEGRAM_BOT_TOKEN" not in message
    # What the operator *can* still act on stays actionable.
    assert "TELEGRAM_CHAT_ID" in message


# ---------------------------------------------------------------------------
# An absent credential, end to end
# ---------------------------------------------------------------------------
#
# Everything above is about a credential that is *found* somewhere
# other than the environment. What follows is the other half: the
# credential that is nowhere. That is the state of a fresh checkout, a
# CI runner, and every operator who has not finished wiring a channel,
# so it is the state the code meets most often — and it is the one that
# must not cost the process its life.
#
# The three layers answer it in order, and each has to answer it alone:
#
#   1. ``credentials`` turns a failed lookup into a value —
#      ``None`` from ``read_secret``, ``"missing"`` from
#      ``secret_source`` — and never into an exception;
#   2. the transport client names the same condition, and the notifier
#      catches *that* name, records why, and goes on;
#   3. the notifier's disabled state costs it the push, not its
#      subscription or its worker, because a notifier that stops
#      listening cannot tell "nothing happened" from "we were not
#      watching", and the operator gets the second one.

#: The text the transport client fails with. What the assertions below
#: actually depend on is that the *exception's own text* is what reaches
#: ``_disabled_reason`` — so a notifier that swallowed the error and
#: substituted a generic string of its own would fail them, which is the
#: point: the reason an operator reads has to be the reason that
#: happened.
UNAVAILABLE_TEXT = "Feishu is not configured: no app id, no resolvable secret"

#: The loggers on the chain under test, from the notifier down to the
#: secret provider. Named rather than "all records" so the assertion
#: below is about this chain and not about whatever else the process
#: happens to be doing.
CHAIN_LOGGERS = (
    "notifications.feishu_notifier",
    "notifications.feishu_client",
    "credentials",
)


@pytest.fixture
def client_raises_unavailable(monkeypatch):
    """Make the transport client's construction fail, by name.

    The failure has to be the named ``FeishuUnavailable``, because that
    is the class the notifier is written to catch and the only one whose
    handler continues into the subscriber registration. A bare
    ``Exception`` stand-in would take the generic handler instead, and
    the assertions would pass against a notifier that had in fact
    given up — the one behaviour this whole section exists to rule out.
    """
    def _unavailable(*_args, **_kwargs):
        raise FeishuUnavailable(UNAVAILABLE_TEXT)

    monkeypatch.setattr(fn, "FeishuClient", _unavailable)


@pytest.fixture
def no_plans(monkeypatch):
    """Keep the worker's tick off the developer's own plans.

    The tick asks which plans are active before it does anything else,
    and left alone it would read the plan directory the machine running
    the suite actually uses. An empty list is the true answer for a
    notifier that has been handed no plans, and it keeps the thread on
    no filesystem at all.
    """
    monkeypatch.setattr(fn, "list_active_plan_ids", lambda: [])


@pytest.fixture
def started_notifier():
    """Start a notifier and hand its worker back before the test ends.

    ``start()`` spawns the worker whether or not the transport client
    could be built — that is the behaviour under test — so every test
    here begins a thread and every test here owes it back. The teardown
    stops and joins each one even when an assertion failed, which is the
    only reason a thread started here cannot go on reading plan state
    into whichever test is running next.
    """
    started = []

    def _start(**kwargs):
        notifier = FeishuNotifier(**kwargs)
        started.append(notifier)
        notifier.start()
        return notifier

    yield _start

    for notifier in started:
        notifier.stop(timeout=5.0)


@pytest.mark.parametrize(
    "switch",
    ["1", "0"],
    ids=["keychain-off", "keychain-on-no-index"],
)
def test_an_absent_feishu_secret_is_a_value_not_an_exception(monkeypatch, switch):
    """Criterion one, at the bottom of the stack: no value, no raise.

    Both switch positions are covered because they reach the answer by
    different routes. With the keychain off, the plaintext variable is
    the only place a secret could be and it is empty. With the keychain
    on, there is no index naming an item, so no lookup is even possible.
    The observable answer is the same either way, and that sameness is
    the property worth pinning: a caller writes one branch for "not
    configured" instead of one per platform and one per switch.

    What is *not* pinned here is the neighbouring policy — that a
    keychain read which finds nothing must not be answered from the
    plaintext variable instead. That is the resolver's own contract, it
    needs a populated variable to be observable at all, and nothing in
    this case can tell the two apart.

    The keychain branch only exists on macOS — ``keychain_disabled`` is
    fail-closed on every other platform, so a Linux runner would take the
    environment path twice and the second case would assert nothing new.
    Skipped rather than faked: patching the platform check would test the
    patch.
    """
    on_macos = sys.platform == "darwin"
    if switch == "0" and not on_macos:
        pytest.skip("the keychain branch exists only on macOS")

    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", switch)
    credentials.reset_cache()

    # Which resolver this case reached, stated rather than assumed — a
    # parametrisation whose two cases quietly took the same branch
    # would be one test wearing two ids. The switch is a *disable*:
    # "1" turns the keychain off, and only "0" turns it back on.
    if on_macos:
        assert credentials.keychain_disabled() is (switch == "1")

    # A missing credential must not cost a subprocess, and on a desktop
    # a keychain *prompt* would cost far more than that — it blocks
    # until a person answers a dialog nobody asked for. Recorded rather
    # than merely un-patched, so the assertion below holds on both
    # switch positions instead of only on the one that skips.
    lookups = []
    monkeypatch.setattr(
        credentials, "_run_security",
        lambda argv, timeout: lookups.append(argv) or None,
    )

    assert credentials.read_secret("feishu_app_secret") is None
    assert credentials.secret_source("feishu_app_secret") == "missing"
    assert lookups == [], (
        "a secret that is absent must not be looked up; a keychain "
        "read here is a subprocess, and on a desktop a prompt"
    )


def test_start_records_the_reason_and_does_not_raise(
    client_raises_unavailable, no_plans, started_notifier,
):
    """Criterion two, first half: the miss is caught, named, and kept.

    Reaching the first assertion *is* the "does not raise" claim — the
    call is on the line above it, so a notifier that let the error out
    fails the test rather than passing it. What follows pins that the
    error was not merely swallowed: the exception's own text is what
    lands in ``_disabled_reason``, and ``stats()`` reports the channel
    dark *and* why. A boolean alone is what an operator sees when
    somebody switched the channel off on purpose, which is the one
    reading they cannot act on.
    """
    notifier = started_notifier()

    assert notifier._disabled_reason == UNAVAILABLE_TEXT
    # No half-built client is left behind for a later push to use.
    assert notifier._client is None

    stats = notifier.stats()
    assert stats["enabled"] is False
    reason = stats["disabled_reason"]
    assert isinstance(reason, str)
    assert reason.strip(), "disabled_reason must name the cause, not be empty"
    assert UNAVAILABLE_TEXT in reason


def test_disabled_notifier_still_subscribes_and_runs_its_worker(
    client_raises_unavailable, no_plans, monkeypatch, started_notifier,
):
    """Criterion two, second half: disabled means "cannot push", not "gone".

    The disabled branch deliberately falls through to the same
    subscriber registration and the same worker the healthy path uses.
    A notifier that stopped listening would report an empty queue
    forever, and "we received nothing" is indistinguishable from "we
    were not watching" — the same silent failure as a channel that
    quietly stopped delivering, one level up.

    ``_register_subscriber`` is replaced by a recorder rather than left
    to the real bus: the bus is process-global, and a handler attached
    to a notifier this test is about to discard would go on accepting
    events for the rest of the session. What is under test is *that it
    is called*, not what the bus does with it.

    The worker is reclaimed here rather than left to the fixture, so the
    claim is checked: a thread is only handed back if something joins
    it, and a test that starts one and walks away leaves a thread
    reading plan state into the next test.
    """
    registered = []
    monkeypatch.setattr(
        FeishuNotifier, "_register_subscriber",
        lambda self: registered.append(self),
    )

    notifier = started_notifier()

    assert registered == [notifier]
    worker = notifier._worker
    assert worker is not None, "a disabled notifier still needs its worker"
    assert worker.name == "feishu-notifier"
    assert worker.daemon is True
    assert worker.is_alive(), "the worker must be running, not merely created"

    notifier.stop(timeout=5.0)
    assert not worker.is_alive(), "the worker must be joined before the test ends"


def test_an_unavailable_client_logs_no_traceback(
    client_raises_unavailable, no_plans, started_notifier, caplog,
):
    """Criterion two, third half: reported, not dumped.

    Two halves, because they fail in opposite directions. No record on
    the chain carries exception info — a traceback in the access log
    for a credential nobody configured is noise that trains an operator
    to ignore the log. And there is still a line saying the channel went
    dark: silence is not the contract, and a suite that only checked for
    the absence of tracebacks would be satisfied by a notifier that
    logged nothing at all.
    """
    caplog.set_level(logging.DEBUG)

    started_notifier()

    chain = [r for r in caplog.records if r.name in CHAIN_LOGGERS]

    with_exception_info = [
        r for r in chain if r.exc_info is not None or r.exc_text
    ]
    assert with_exception_info == [], (
        "an absent credential is a condition to report, not to print a "
        "traceback for; got it from: {}".format(
            sorted({r.name for r in with_exception_info})
        )
    )

    rendered = "\n".join(r.getMessage() for r in chain)
    assert "Traceback (most recent call last)" not in rendered
    assert "disabled at startup" in rendered
    assert UNAVAILABLE_TEXT in rendered
