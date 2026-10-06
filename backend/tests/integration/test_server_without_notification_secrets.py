"""A service with no notification credentials still starts, and still serves.

What this suite is for
----------------------
Both transports this project can notify through authenticate with a
secret, and on any machine where that secret is absent — a fresh
checkout, a CI runner, an operator who has not finished wiring a
channel — the question that matters is not "is the channel configured"
but "does the absence take the service down with it". A tool that
answers only once, by a person, at a desk where the credentials are
already there, has not answered it at all.

The miss can land in three places, and each has to degrade rather than
block:

  * the transport client's construction raises a *named* error
    (``FeishuUnavailable``) instead of an import error, a keychain
    prompt, or a null dereference;
  * the notifier catches that one error, records why, and stays inert
    — a worker that still drains the event bus and never pushes, so
    "we received N events and could not deliver them" stays countable;
  * the lifespan wraps the whole construction, so even a failure no one
    anticipated leaves the app running with no notifier attached rather
    than aborting startup.

This suite is the service-level evidence for that chain, and it is the
only layer that can be. A unit test of the notifier can show the reason
gets recorded; only a booted application can show the port is still
answering afterwards. That is why it lives under ``integration/`` and
why it enters the ``TestClient`` as a context manager rather than
merely constructing one: the lifespan is the code under test, and a
client built without it never runs the startup path at all — the
notifier would never be constructed, so there would be no disabled
state to observe and the assertions would pass against nothing.

Which endpoint, and why that one
--------------------------------
``GET /api/health``. It reads no notification configuration and no plan
state, so a 200 from it means exactly two things: the process is up and
the request path is whole. Any endpoint that also depends on a plan
existing would fold a second question into the answer — whether a plan
with that id is present would decide the verdict, and a failure would
point at the wrong subsystem. The point of the assertion is that the
*notification* configuration is irrelevant to this response, so the
probe must not read any.

Hermetic by construction
------------------------
Nothing here depends on what the machine running the suite happens to
have configured. Every notification variable is emptied — including the
per-plan Telegram overrides, which are open-ended and so have to be
found by prefix — and the OS keychain is switched off explicitly, so a
developer's own keychain item cannot answer a question this suite
asserts is empty. The keychain switch matters twice over: without it a
macOS machine with the item present would report the channel *enabled*
and the suite would fail for an environment reason, while a machine
without it would pass; the switch is what makes the two agree.

The provider-order fixture is here for the same class of reason. Booting
the app resolves the provider fallback chain, which reads an optimiser
file and the CC Switch database — neither of which ships in a checkout.
A suite whose startup assertions depend on those being present is
asserting about the runner, not about the code.

What each test pins
-------------------
  * the service answers 200 with the credential miss in effect;
  * ``stats()`` reports the channel disabled **and says why** — a
    boolean alone leaves an operator with a card that stopped moving and
    no way to learn the cause;
  * the Telegram probe reports disabled on the same footing;
  * nothing raised and nothing logged a traceback along the way.
"""

from __future__ import annotations

import logging
import os

import pytest

pytestmark = pytest.mark.integration


#: The environment variables the two transports are configured from.
#: Written out rather than discovered at call time so a reader can see
#: the whole surface being emptied, and so a source added later has one
#: obvious place to be added.
_NOTIFICATION_ENV_VARS = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)

#: Prefixes of the per-plan Telegram overrides. A prefix rather than a
#: list of names, because the plan ids are open-ended — and a literal
#: plan id here would be one deployment's plans baked into a file every
#: other checkout reads.
_NOTIFICATION_ENV_PREFIXES = ("TELEGRAM_CHAT_ID_",)

#: The switch that keeps the OS keychain out of the answer. See
#: ``credentials.keychain_disabled``: it is fail-closed, so every value
#: outside ``{"0", "false"}`` disables the keychain. This one is spelled
#: to read as the intent rather than as a default.
_KEYCHAIN_SWITCH = "PDT_DISABLE_KEYCHAIN_SECRETS"

#: The loggers whose silence is part of the contract. The chain under
#: test is client → notifier → credentials, and a traceback from any of
#: the three is a traceback the service would have printed for a missing
#: credential. Scoped to them deliberately: a startup sweep that logs
#: its own traceback for some unrelated reason is a different bug, and
#: folding it in here would make this suite fail for a reason that has
#: nothing to do with notification credentials.
_NOTIFICATION_LOGGERS = (
    "notifications.feishu_notifier",
    "notifications.feishu_client",
    "credentials",
)


def _clear_notification_env(monkeypatch) -> None:
    """Leave the process with no notification credential of any kind.

    Every variable rather than the two secrets, because a surviving
    *index* changes the answer too: an app id with no secret and a token
    with no chat id are both half-configured states, and half-configured
    is exactly the case that has to degrade rather than block.
    """
    for key in _NOTIFICATION_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    for key in [k for k in os.environ if k.startswith(_NOTIFICATION_ENV_PREFIXES)]:
        monkeypatch.delenv(key, raising=False)

    # Not a defensive measure: without it, a machine that has the item
    # and a machine that does not disagree about whether the channel is
    # configured, and only one of them can be right about the code.
    monkeypatch.setenv(_KEYCHAIN_SWITCH, "1")

    # The memo is emptied so the resolution that happens during the
    # lifespan below is the first one since the environment changed.
    # The conftest fixture resets it around every test as well; this is
    # the half that makes the ordering inside this test obvious.
    import credentials

    credentials.reset_cache()


class _StartedService:
    """A booted app and the notifier its startup attached.

    Both are needed by every test here, and pairing them keeps each test
    to a single fixture argument — the notifier is reachable off
    ``app.state`` only while the lifespan that set it is the one that
    ran for *this* test, so handing it out together with the client is
    what keeps a test from reading a previous test's leftover.
    """

    def __init__(self, client, notifier) -> None:
        self.client = client
        self.notifier = notifier


@pytest.fixture
def service_without_secrets(monkeypatch, fake_cc_switch_home):
    """Boot the app with no notification credential configured.

    ``fake_cc_switch_home`` is the provider chain the lifespan resolves,
    not a notification concern: without it the app refuses to start on a
    clean checkout and every assertion below would be reporting that
    instead.

    The client is entered as a context manager, so the lifespan runs and
    its shutdown runs with it. That shutdown is what reclaims the
    notifier's worker thread, and the bus subscription is dropped
    afterwards: the bus is process-global, and a handler left attached
    to a stopped notifier keeps accepting events for the rest of the
    session.
    """
    import server
    from fastapi.testclient import TestClient

    _clear_notification_env(monkeypatch)

    with TestClient(server.app) as client:
        notifier = getattr(server.app.state, "feishu_notifier", None)
        if notifier is None:
            pytest.fail(
                "no notifier attached to app.state after startup; the "
                "lifespan's own fallback fired, so the assertions below "
                "would be reading a service that never tried to notify"
            )
        yield _StartedService(client, notifier)

    from notifications.state_events import STATE_EVENT_BUS

    STATE_EVENT_BUS.unsubscribe(notifier._on_event)


def test_status_endpoint_returns_200_without_secrets(service_without_secrets):
    """The service answers while both channels are unprovisioned."""
    resp = service_without_secrets.client.get("/api/health")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


def test_stats_reports_disabled_with_reason(service_without_secrets):
    """Disabled, and it says why.

    The reason is the part that earns its keep. A boolean alone is the
    same value an operator sees when the channel is switched off on
    purpose, and the difference — a service that cannot notify because
    nobody configured it, versus one that cannot notify because
    something is broken — is exactly what they cannot otherwise tell.
    """
    stats = service_without_secrets.notifier.stats()

    assert stats["enabled"] is False
    reason = stats["disabled_reason"]
    assert isinstance(reason, str)
    assert reason.strip(), "disabled_reason must name the cause, not be empty"


def test_telegram_enabled_is_false(service_without_secrets):
    """The second transport reports disabled on the same footing.

    Its own probe, not the Feishu one: the two read different variables,
    and a single shared flag would hide which of the two is unconfigured.
    """
    stats = service_without_secrets.notifier.stats()

    assert stats["telegram_enabled"] is False


def test_no_traceback_when_credentials_absent(service_without_secrets, caplog):
    """A missing credential is a value to report, not an error to print.

    Two things are asserted, because they fail differently. Nothing
    raises — which is what keeps the process alive — and nothing logs a
    traceback — which is what keeps the access log readable. The ERROR
    line the notifier emits for the disabled startup is a *handled*
    report: it carries no ``exc_info``, so it survives this assertion
    and the operator still learns the channel is off.

    The notifier is started a second time inside the test body so the
    moment the credential is missing — construction, the only point at
    which anything can raise — falls inside this test's own capture
    window rather than in fixture setup, where a reader would have to
    take the timing on trust. It is stopped and unsubscribed again
    before the test returns, like the one the lifespan started.
    """
    from notifications.feishu_notifier import FeishuNotifier
    from notifications.state_events import STATE_EVENT_BUS

    caplog.set_level(logging.INFO, logger="notifications.feishu_notifier")

    probe = FeishuNotifier()
    try:
        probe.start()
        resp = service_without_secrets.client.get("/api/health")
    finally:
        probe.stop(timeout=5.0)
        STATE_EVENT_BUS.unsubscribe(probe._on_event)

    assert resp.status_code == 200, resp.text

    tracebacks = [
        record
        for record in caplog.records
        if record.name in _NOTIFICATION_LOGGERS and record.exc_info
    ]
    assert tracebacks == [], (
        "a missing notification credential must not produce a traceback; "
        "got: {}".format([r.name for r in tracebacks])
    )