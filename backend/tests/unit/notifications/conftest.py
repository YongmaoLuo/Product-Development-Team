"""Shared fixtures and hooks for the notifier suites.

See ``status_payload.py`` for why these suites still express their
fixtures as three sections while the notifier now makes a single read.

The credential pin below is a **hook**, not a fixture, and the reason is
a property of how pytest binds conftest fixtures rather than a
preference. A conftest is registered as a plugin when pytest first needs
it, but its *fixtures* are not bound to anything at that moment: they go
into a pending map keyed by the conftest's directory, and are attached to
a node only when that directory's collection **finishes**
(``_pytest/fixtures.py`` — ``pytest_make_collect_report`` pops the
pending entry after the collector returns). The test items inside that
directory have already computed their fixture closure by then.

So an autouse fixture in a conftest is not guaranteed to be in the
closure of the tests in its own directory, and whether it is depends on
the order the files are collected in. Hooks have no such deferral — they
are bound to the directory's hook proxy at registration — so the pin
below is expressed as a hook and is in force for every item under this
directory regardless of collection order.
``test_credential_source_isolation.py`` asserts the pin is actually in
force, because a pin that silently does not run is indistinguishable
from no pin at all.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import credentials
from status_payload import build_payload

#: This directory, resolved from this file — never written out: the
#: repository is checked out at a different path on every machine, and a
#: literal one would make the hook fire on an unrelated tree that happens
#: to share the name.
_HERE = Path(__file__).resolve().parent

#: Every environment key that decides where a credential is read from:
#: the plaintext fallback, the keychain index, and the switch itself.
#: Derived from the registry rather than written out, so a secret added
#: to ``SECRET_SPECS`` is covered here without anyone editing this file
#: — and, more importantly, so a secret *removed* from the registry is
#: not scrubbed forever under a name nobody remembers adding.
_CREDENTIAL_ENV_KEYS = tuple(
    key
    for spec in credentials.SECRET_SPECS.values()
    for key in (spec.fallback_env_key, spec.account_env_key)
) + ("PDT_DISABLE_KEYCHAIN_SECRETS",)

#: The switch, and the spelling that turns the keychain off. Written out
#: rather than read from a private name in the provider so the value this
#: file installs is one a reader can check against the provider's own
#: documentation, and so a rename on either side shows up as a failure
#: here instead of as a suite that quietly consults the keychain.
_SWITCH_KEY = "PDT_DISABLE_KEYCHAIN_SECRETS"
_SWITCH_OFF = "1"

#: The snapshot taken when the pin goes on. A ``Stash`` rather than an
#: attribute because it is internal bookkeeping that no test should read,
#: and the stash is the one place pytest guarantees is ours.
_SAVED_ENV = pytest.StashKey[dict]()

#: The marker the pin leaves on each item it covered, so a test can ask
#: whether the pin reached it without importing this module. Named with a
#: leading underscore and specific enough not to collide with anything
#: pytest puts on an item.
PIN_APPLIED_ATTR = "_pdt_credential_pin_applied"



def _is_ours(item) -> bool:
    """Whether ``item`` is a test this conftest governs."""
    path = getattr(item, "path", None)
    return path is not None and _HERE in path.parents


def _apply_pin(item) -> None:
    """Decide where these suites read credentials, here, not on the machine.

    ``backend/server.py`` runs ``load_dotenv(<repo-root>/.env)`` at
    *import* time, and something in this package's import chain reaches
    it. So the developer's own ``.env`` — a per-machine, uncommitted file
    — lands in ``os.environ`` for the rest of the session, and with it
    ``PDT_DISABLE_KEYCHAIN_SECRETS``.

    That switch is what these suites were reading their credentials
    through. On a machine where the developer has followed the keychain
    migration guide it is ``0``, so ``credentials`` consults the OS
    keychain; where this process is not permitted to run
    ``/usr/bin/security`` the lookup fails, and it deliberately does
    *not* fall back to the plaintext variable — so
    ``read_secret("telegram_bot_token")`` returns None while the token
    sits in the environment the test just set.

    A suite whose result depends on a file outside the repository is a
    suite whose green is not evidence, and the failure it produces names
    a transport rather than the credential source underneath it — so the
    reader goes looking for a credential that was never set up.

    Pinning the switch off here restores the two properties these
    suites are written against: the credential comes from the
    environment the test set, and no machine facility is consulted.
    Nothing about the resolution *logic* is stubbed — this decides which
    source is used, not what any source returns.

    The environment is written directly rather than through
    ``monkeypatch`` because the pin is a hook, and a hook has no
    ``monkeypatch`` to borrow. The snapshot is put back in
    ``pytest_runtest_teardown`` *after* the fixture teardown that
    ``monkeypatch`` performs, so a test that opts back into the keychain
    with its own ``monkeypatch`` still wins for the duration of the test
    and is undone before the pin comes off.
    """
    saved = {key: os.environ.get(key) for key in _CREDENTIAL_ENV_KEYS}
    for key in _CREDENTIAL_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ[_SWITCH_KEY] = _SWITCH_OFF
    item.stash[_SAVED_ENV] = saved
    setattr(item, PIN_APPLIED_ATTR, True)


def _remove_pin(item) -> None:
    saved = item.stash.get(_SAVED_ENV, None)
    if saved is None:
        return
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _unmemoise() -> None:
    """Give every test its own resolution of every secret.

    ``credentials`` memoises a secret for the life of the process, which
    is right for a deployment and wrong for a test: the deployment here
    is whatever the previous test's ``setenv`` left behind. Left alone,
    the memo makes these suites order-dependent — a test that clears
    ``TELEGRAM_BOT_TOKEN`` hands the next test a cached "no token", and
    one that sets it hands the next test a cached "token" that no longer
    exists. Both are answers about a process that is not running any
    more.

    Reset on the way in *and* on the way out keeps a resolution from
    escaping into a suite that never asked for it.
    """
    credentials.reset_cache()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    """Pin before any fixture in this package gets to run.

    A wrapper rather than a plain hook so the pin goes on *outside*
    fixture setup: a test that exercises the keychain path sets the
    switch with ``monkeypatch`` in its own body or in one of its
    fixtures, both of which land after this and therefore win.
    """
    if _is_ours(item):
        _unmemoise()
        _apply_pin(item)
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Unpin after the fixtures have been undone.

    The ordering is the reason this is a wrapper: ``monkeypatch``
    restores during fixture teardown, so coming off the pin before that
    would let it restore *to the pinned values* and leave the pin behind
    them for the rest of the session.
    """
    try:
        return (yield)
    finally:
        if _is_ours(item):
            _unmemoise()
            _remove_pin(item)


@pytest.fixture
def patch_status_fetch(monkeypatch):
    """Patch the notifier's single status fetch with fixture sections."""

    def _apply(summary=None, execution=None, verification=None, status=None):
        def _fake(plan_id, base_url=None):
            return build_payload(
                plan_id, summary, execution, verification, status,
            )

        monkeypatch.setattr(
            "notifications.feishu_notifier.fetch_plan_status", _fake,
        )
        return _fake

    return _apply
