"""These suites must not read credentials from the machine they run on.

What is under test
------------------
``tests/unit/notifications/conftest.py`` pins the credential source for
this package, and this module is the reason that pin is visible rather
than merely documented. A pin that decides something nobody can assert is
a comment with a runtime cost.

The leak it exists to stop
~~~~~~~~~~~~~~~~~~~~~~~~~~
``backend/server.py`` runs ``load_dotenv(<repo-root>/.env)`` at *import*
time, and something in this package's import chain reaches it. So the
developer's own ``.env`` — per-machine, uncommitted — lands in
``os.environ`` for the rest of the session, and with it
``PDT_DISABLE_KEYCHAIN_SECRETS``.

On a machine that has followed the keychain migration guide that switch
reads ``0``. ``credentials`` then consults the OS keychain, the lookup
fails wherever this process may not execute ``/usr/bin/security``, and it
deliberately does **not** fall back to the plaintext variable — so
``read_secret("telegram_bot_token")`` returns ``None`` while the token
sits in the environment the test just set — and the failure that
produces reports a transport rather than the credential source
underneath it.

What the pin does and does not do
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
It pins the switch to a disabling value and clears the credential keys
around each test. It does not stub ``credentials``: no resolution branch
is replaced, nothing about what a source *returns* is faked. The only
thing decided here is which source is consulted.

The contract is:

* the switch is off during these tests, whatever the machine says;
* the pin reached this item — a separate question from the one above,
  and the reason it is asked separately is in that test;
* a secret the test sets resolves from the environment;
* a test that deliberately opts back into the keychain path can, and the
  pin does not stand in its way — this is the one that would fail if the
  pin were ever made heavier-handed than "decide the source".

Nothing here runs ``/usr/bin/security``: the keychain path is asserted
through :func:`credentials.keychain_disabled` and
:func:`credentials.secret_source`, which is where a developer's machine
shows up.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import credentials
# The name the pin marks each item it covered with. Read from the
# conftest rather than written out here so that renaming the marker
# cannot leave this module asserting an attribute nothing sets — the two
# would otherwise drift and the assertion would pass vacuously.
from tests.unit.notifications import conftest as suite_conftest

#: The two spellings that mean "do not disable". Any other value — and
#: an absent one — leaves the keychain off.
_ENABLING = ("0", "false")


@pytest.fixture(autouse=True)
def _clean_cache():
    credentials.reset_cache()
    yield
    credentials.reset_cache()


class TestTheMachineDoesNotChoose:
    def test_the_pin_decided_the_switch_rather_than_leaving_it_to_chance(
        self,
    ):
        """The pin is in force because it *ran*, not because nothing
        happened to be set.

        ``keychain_disabled() is True`` cannot tell those apart: an
        absent switch also reads as disabled, so a pin that scrubbed the
        keys and then failed to set the switch would satisfy it. That
        regression is invisible on any platform, which is how it can
        survive review — the behaviour looks right and the guard cannot
        see it. Asserting the pinned spelling separates "off by
        decision" from "off by accident", and holds identically on
        macOS and on a Linux runner.
        """
        import os

        assert os.environ.get("PDT_DISABLE_KEYCHAIN_SECRETS") == "1"

    def test_the_pin_is_in_force_for_this_test(self, request):
        """The pin reached *this* item, without this test asking for it.

        Under a fixture the question needed ``request.fixturenames``:
        a conftest fixture is not guaranteed into the closure of the
        tests in its own directory, so "the guard is present" and "the
        guard ran" had to be asked separately. The pin is a hook now
        (see the conftest's docstring for why), and a hook is either
        registered for this item's directory or it is not — there is no
        third state to ask about.

        So the probe is the effect the pin is supposed to have on this
        item, read from the item itself: the marker is written by the
        pin and by nothing else, so a pin that was deleted, renamed, or
        stopped covering this directory leaves it absent while every
        behavioural test in this class stays green.
        """
        assert getattr(request.node, suite_conftest.PIN_APPLIED_ATTR, False) is True

    def test_the_keychain_is_off_during_these_tests(self):
        """The invariant in one line, stated as a property of the module
        rather than of the pin's implementation.

        Reading ``os.environ[SWITCH]`` directly would look stricter and
        be worse: it asserts *how* the pin does its job, and it raises
        ``KeyError`` rather than failing informatively the moment anything
        removes the variable — including a future pin that achieves the
        same result by not setting it at all. The property these suites
        need is the one the module exposes; the case above is what pins
        down the half the module cannot see.
        """
        assert credentials.keychain_disabled() is True

    def test_the_switch_is_not_left_in_an_enabling_spelling(self):
        """Read with ``.get``, so an absent variable is reported rather
        than raised — and paired with the assertion above, which is the
        one that cannot pass vacuously."""
        import os

        assert os.environ.get("PDT_DISABLE_KEYCHAIN_SECRETS") not in _ENABLING

    def test_a_secret_the_test_sets_resolves_from_the_environment(
        self, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")

        assert credentials.read_secret("telegram_bot_token") == "123456:test-token"
        assert credentials.secret_source("telegram_bot_token") == "os.environ"

    def test_a_developers_own_values_cannot_satisfy_a_lookup(
        self, monkeypatch
    ):
        """The index is what names the item, and the pin clears it.

        Left in place, an index exported by a developer's ``.env`` would
        let a lookup proceed against a keychain this process cannot
        read — turning "not configured" into "configured, and wrong",
        which is the harder failure to see.
        """
        import os

        for spec in credentials.SECRET_SPECS.values():
            assert spec.account_env_key not in os.environ


class TestThePinDoesNotOverreach:
    def test_a_test_may_opt_back_into_the_keychain_path(self, monkeypatch):
        """The pin decides the default; it does not forbid the choice.

        The pin goes on around fixture setup, so a test that means to
        exercise the keychain sets the switch in its own body or in one
        of its own fixtures — both of which land after the pin — and
        wins. If this ever fails, the pin has stopped being a default and
        started being a prohibition, which is the failure mode that would
        quietly stop the keychain tests from testing anything.

        Stated as "is the keychain consulted iff this platform has one"
        rather than as a bare ``is False``. The platform is read from
        ``sys.platform`` and never patched: a patched platform check
        tests the patch, and the switch's effect is only meaningful on a
        machine that has a keychain to switch on. Written this way the
        case asserts something true — and different — on every runner,
        instead of being skipped everywhere it is not macOS.
        """
        monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")

        on_macos = sys.platform == "darwin"
        assert credentials.keychain_disabled() is (not on_macos)

    def test_the_resolution_order_itself_is_untouched(self, monkeypatch):
        """The keychain is consulted before the environment — a property
        several tests in this package rely on.

        This one needs a real keychain branch to exist, so it skips
        where there is none, for the reason
        ``test_feishu_notifier_secret_source`` already gives: the branch
        is macOS-only, and faking the platform check would test the
        fake. The pin that matters — that the ordering is not the pin's
        doing — is covered on every platform by the case above.
        """
        if sys.platform != "darwin":
            pytest.skip("the keychain branch exists only on macOS")

        monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env")

        # With the keychain on and the lookup unable to produce a value,
        # the answer is "missing" rather than a silent downgrade to the
        # plaintext variable. That is the behaviour every migration in
        # this repository depends on, and it is asserted here so that a
        # future change to the pin cannot quietly remove it.
        assert credentials.secret_source("telegram_bot_token") == "missing"


class TestThePlatformRule:
    """What the keychain path does where there is no keychain.

    This is the branch a Linux runner actually executes. It is stated
    against the real ``sys.platform`` rather than a patched check, so
    each case asserts what is true where it runs — and the macOS-only
    direction skips rather than pretending.
    """

    @pytest.mark.skipif(
        sys.platform == "darwin", reason="asserts the off-macOS behaviour"
    )
    def test_off_macos_the_keychain_is_off_whatever_the_switch_says(
        self, monkeypatch
    ):
        monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")

        assert credentials.keychain_disabled() is True

    @pytest.mark.skipif(
        sys.platform == "darwin", reason="asserts the off-macOS behaviour"
    )
    def test_off_macos_the_environment_is_used_even_with_a_keychain_index(
        self, monkeypatch
    ):
        """The failure this prevents is silent: a test that sets a
        keychain-shaped environment and expects the keychain to be
        consulted would, on Linux, get its answer from the environment
        and report success."""
        monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env")
        credentials.reset_cache()

        assert credentials.secret_source("telegram_bot_token") == "os.environ"


def test_the_pinned_keys_cover_the_whole_registry():
    """The scrub list is derived, not written out — prove it still is.

    A hand-copied list is a second thing to forget, and it is forgotten
    in the direction that leaves a credential readable while the pin
    still reads as thorough.
    """
    declared = set(suite_conftest._CREDENTIAL_ENV_KEYS)
    for spec in credentials.SECRET_SPECS.values():
        assert spec.fallback_env_key in declared
        assert spec.account_env_key in declared
    assert "PDT_DISABLE_KEYCHAIN_SECRETS" in declared


class _Item:
    """The two things the pin touches on a pytest item.

    A real item would do, but a stub says exactly what is required of
    one — and a test that needs the real ``Item`` to check a dictionary
    round-trip is a test that has stopped being about the round-trip.
    """

    def __init__(self, path):
        self.path = path
        self.stash = pytest.Stash()


class TestThePinMechanismItself:
    """The pin's own contract, now that it is a hook rather than a fixture.

    A fixture got its undo from ``monkeypatch``. A hook has to take the
    snapshot and put it back itself, and that is new code on the path
    every test in this package runs through — so it is stated here
    rather than left to be discovered by whichever test happens to notice
    a leaked variable first.
    """

    def test_the_environment_comes_back_exactly_as_it_was_found(self, monkeypatch):
        """Both directions: a key that had a value, and a key that had none.

        Restoring ``None`` as the string ``"None"`` is the mistake worth
        naming — it leaves a variable defined that the developer never
        had, which then reads as configuration to whatever runs next.
        """
        import os

        from tests.unit.notifications import conftest as pin

        monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "was-here")
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        before = {k: os.environ.get(k) for k in pin._CREDENTIAL_ENV_KEYS}

        item = _Item(Path(__file__))
        pin._apply_pin(item)
        assert os.environ["PDT_DISABLE_KEYCHAIN_SECRETS"] == pin._SWITCH_OFF
        assert "TELEGRAM_BOT_TOKEN" not in os.environ

        pin._remove_pin(item)
        after = {k: os.environ.get(k) for k in pin._CREDENTIAL_ENV_KEYS}
        assert after == before
        assert "TELEGRAM_CHAT_ID" not in os.environ, (
            "a key that was absent came back defined — the next suite "
            "would read it as configuration"
        )

    def test_it_marks_the_item_it_covered(self):
        from tests.unit.notifications import conftest as pin

        item = _Item(Path(__file__))
        pin._apply_pin(item)

        assert getattr(item, pin.PIN_APPLIED_ATTR, False) is True

    def test_it_declines_items_outside_this_directory(self, tmp_path):
        """The scope check, stated both ways.

        A pin that reached every item in the suite would be a pin that
        decides the credential source for tests that never asked it to —
        including the ones that deliberately exercise the keychain.
        """
        from tests.unit.notifications import conftest as pin

        assert pin._is_ours(_Item(Path(__file__))) is True
        assert pin._is_ours(_Item(tmp_path / "elsewhere.py")) is False
