"""The shared fixtures provider-secret tests depend on, tested themselves.

Why this suite exists
---------------------
Every test that touches provider credentials reaches for the same three
things, and none of them can be built by the test that needs them:

* **an empty resolution memo** — ``credentials`` memoises a resolved
  secret for the life of the *process*, and pytest shares one process
  across the whole shard. One test that turns the keychain path on and
  successfully reads a value leaves the answer in the memo, and the next
  test to ask inherits it. Which direction that pollutes depends on the
  order the files happened to be collected in, so it is not a failure a
  reader can point at: it is "this test passes alone and fails in the
  suite" (and the other way round), and it moves every time someone adds
  a file.
* **a value that is unmistakably synthetic** — a test needs a secret
  shaped like a real one without being one. A placeholder that looks
  enough like the real thing is a placeholder a secret scanner will
  eventually flag, and one that does not is a placeholder the provider
  under test may reject before the behaviour being tested is reached.
* **a hand-back rule for what a test starts** — publishing a secret
  hands out a pipe descriptor, and a keychain read starts a process.
  Both outlive the call that created them, and the suite's existing rule
  (every test returns what it starts, enforced in
  ``tests/conftest.py::clean_execution_state``) is where they have to be
  enforced, or they are enforced in none of them.

These tests are the unit layer for those fixtures (test design decision
point 1): the fixtures are test infrastructure, and infrastructure that
nothing tests is infrastructure that is wrong in a way only a full-suite
run reveals.

How the two-test contracts are observed
---------------------------------------
A fixture that isolates *process* state cannot be observed from inside
the test it isolates for — the pollution it removes has not happened yet,
or has already been cleaned. So each of the three contracts below is
written as a pair: a polluter that deliberately starts with its hands
full, and, immediately after it in this file, the named test that asserts
the next test started with clean hands. Definition order is the order
pytest runs them in — no ordering plugin is installed — so the
dependency is the order the file is written in, and it is stated in each
docstring rather than left to be discovered.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys

import pytest

import credentials

#: The four kinds of value a credential test needs: the two secrets
#: themselves and the two keychain *indexes* that name the item holding
#: them. An index is not a secret, so the two are separate kinds here for
#: the same reason they are separate rows in ``credentials.SECRET_SPECS``.
_CANARY_KINDS = (
    "feishu_secret",
    "telegram_token",
    "feishu_index",
    "telegram_index",
)

#: The two shapes a real credential of this project's providers has: a
#: bot token is a numeric account id, a colon, then a long opaque tail;
#: an app secret is a bare run of alphanumerics. A canary that matched
#: either would be a string a secret scanner has to be told to ignore —
#: and a scanner that is told to ignore one shape of value is a scanner
#: that will miss the real thing. These are asserted against below
#: precisely so a canary cannot quietly grow into one.
_CREDENTIAL_SHAPES = (
    re.compile(r"[0-9]{8,10}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"[A-Za-z0-9]{32}"),
)

#: What the previous test in this file left behind, for the test that
#: follows it to look at. Module-level on purpose: what has to survive
#: the boundary is process state, and a local variable would not.
_MEMOISED_BY_PREVIOUS_TEST: list = []
_CHILD_LEFT_BY_PREVIOUS_TEST: list = []
_PIPE_LEFT_BY_PREVIOUS_TEST: list = []


def _is_open_pipe(fd: int) -> bool:
    """Return whether ``fd`` is still an open pipe.

    Asked indirectly, through the descriptor's type, rather than through
    ``EBADF``, because "this number is closed" and "this number now names
    something else" are indistinguishable from the outside — and a
    number reused by the next test's temporary file would otherwise read
    as a leaked descriptor. A pipe that leaked is still a pipe; a number
    that was recycled is not.
    """
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return False
    return stat.S_ISFIFO(mode)


@pytest.fixture(autouse=True)
def _no_provider_secrets_from_the_developer(monkeypatch):
    """Start every test here with the provider's own environment cleared.

    A workstation that notifies Feishu really does export
    ``FEISHU_APP_SECRET``, and a test asserting "this resolves to a
    miss" would then read the developer's live secret and fail — while
    passing on a CI runner that has none. That is the wrong way round for
    a suite: the assertion that guards a machine-dependent property is
    itself the machine-dependent one.

    The keys are derived from ``credentials.SECRET_SPECS`` rather than
    written out, so a row added to the table cannot quietly inherit the
    ambient value of the machine the suite runs on.
    """
    monkeypatch.delenv("PDT_DISABLE_KEYCHAIN_SECRETS", raising=False)
    for spec in credentials.SECRET_SPECS.values():
        monkeypatch.delenv(spec.fallback_env_key, raising=False)
        monkeypatch.delenv(spec.account_env_key, raising=False)



# ---------------------------------------------------------------------------
# The module memo
# ---------------------------------------------------------------------------


def test_the_previous_test_memoised_a_secret_for_the_test_below(
    monkeypatch, canary, tmp_path
):
    """Leave a resolved secret in the memo on purpose.

    Not a test of the provider: this is the mess the next test cleans up.
    It asserts only that the memo really is a process-wide one — a
    ``_CACHE`` that stayed empty here would make
    ``test_autouse_fixture_resets_the_module_cache`` pass for the wrong
    reason.
    """
    value = canary("feishu_secret", tmp_path)
    monkeypatch.setenv("FEISHU_APP_SECRET", value)

    assert credentials.read_secret("feishu_app_secret") == value
    assert credentials._CACHE, "nothing was memoised — the test below would prove nothing"

    _MEMOISED_BY_PREVIOUS_TEST.extend(credentials._CACHE.items())


def test_autouse_fixture_resets_the_module_cache():
    """The next test sees a miss, not the previous test's value.

    Three things have to hold for that, and each is asserted separately
    because they fail differently:

      * the memo the previous test filled is **empty** — the fixture ran
        and reset it, rather than the previous test happening to leave
        it clean;
      * the environment variable the previous test set is **gone** —
        the module fixture below cleared whatever the machine running
        the suite exports, and ``monkeypatch`` unwound on top of it, so a
        lookup that did hit the environment would have to read something
        else;
      * a lookup therefore **reports a miss** and returns ``None``,
        rather than handing back a value resolved for a deployment that
        no longer exists.
    """
    assert _MEMOISED_BY_PREVIOUS_TEST, "the polluter above did not run"
    assert "feishu_app_secret" in dict(_MEMOISED_BY_PREVIOUS_TEST)

    assert "FEISHU_APP_SECRET" not in os.environ, "monkeypatch did not unwind"

    assert credentials._CACHE == {}, (
        f"the memo survived the previous test: {credentials._CACHE!r}"
    )
    assert credentials.secret_source("feishu_app_secret") == "missing"
    assert credentials.read_secret("feishu_app_secret") is None
    assert credentials.secret_available("feishu_app_secret") is False


# ---------------------------------------------------------------------------
# Canary values
# ---------------------------------------------------------------------------


def test_canary_values_are_unique_per_tmp_path(canary, tmp_path):
    """Two canaries are never the same string, and the difference is the suffix.

    Uniqueness is derived, not random: a counter makes two calls in one
    test differ even when they are given the same directory, and the
    directory is folded in so two tests cannot mint the same value either.
    The *prefix* is the part that carries meaning, so it has to be
    identical across calls — if the suffix leaked into the prefix, a
    reader could no longer tell what kind of value they are looking at.
    """
    first = canary("telegram_token", tmp_path)
    second = canary("telegram_token", tmp_path)
    elsewhere = canary("telegram_token", tmp_path / "another-test")

    assert first != second, "two canaries for one directory collided"
    assert first != elsewhere
    assert second != elsewhere

    assert first.rpartition("-")[0] == second.rpartition("-")[0]
    assert first.rpartition("-")[0] == elsewhere.rpartition("-")[0]


def test_canary_prefixes_are_recognizable(canary, tmp_path):
    """Four prefixes, four shapes, and none of them looks like a credential.

    A canary's job is to be legible in a place where nobody is looking at
    the test that made it — a log line, a captured environment, a
    screenshot of a notification. So the prefix says three things at once:
    which provider, which of the four kinds, and that the value is
    synthetic. The four are asserted distinct, because a fixture that
    handed out one prefix for everything would make a failure
    impossible to read back to its cause.

    The index sentinels keep the *shape* their real counterparts have
    (``cli_…`` for an app id, ``-100…`` for a chat id) while carrying the
    marker, so a test that is handed one exercises the path a deployment
    would actually take without planting anything shaped like a live
    identifier.
    """
    values = {kind: canary(kind, tmp_path) for kind in _CANARY_KINDS}

    prefixes = {}
    for kind, value in values.items():
        prefix, separator, suffix = value.rpartition("-")

        assert separator, f"{kind} carries no prefix/suffix boundary: {value!r}"
        assert re.fullmatch(r"[0-9a-f]{8}", suffix), (
            f"{kind} suffix is not 8 derived hex characters: {suffix!r}"
        )
        assert "pdt-test" in value, f"{kind} is not marked as test data: {value!r}"
        for shape in _CREDENTIAL_SHAPES:
            assert not shape.fullmatch(value), (
                f"{kind} matches the shape of a real credential: {value!r}"
            )
        prefixes[kind] = prefix

    assert len(set(prefixes.values())) == len(_CANARY_KINDS), (
        f"two kinds share a prefix: {prefixes!r}"
    )

    assert values["feishu_index"].startswith("cli_"), values["feishu_index"]
    assert values["telegram_index"].startswith("-100"), values["telegram_index"]
    assert not values["feishu_index"].startswith("-100")
    assert not values["telegram_index"].startswith("cli_")


def test_canary_rejects_a_kind_it_does_not_know(canary, tmp_path):
    """A misspelled kind fails the test that asked for it, not a later one.

    A factory that answered an unknown kind with a plausible-looking
    value would be worse than one that raises: the test would run to
    completion against a value the fixture invented, and the failure —
    if there was one — would point at the provider instead of at the
    fixture. The message names the kinds so the typo is one read away.
    """
    with pytest.raises(pytest.fail.Exception) as excinfo:
        canary("telegram_index_typo", tmp_path)

    message = str(excinfo.value)
    for kind in _CANARY_KINDS:
        assert kind in message, f"the failure did not list {kind!r}: {message!r}"


# ---------------------------------------------------------------------------
# Resource hand-back
# ---------------------------------------------------------------------------


def test_the_previous_test_spawned_a_child_it_never_waits_for(
    register_child_process,
):
    """Start a child and hand nothing back; the test below says what happened.

    A child that sleeps far longer than this file's runtime cannot have
    exited on its own, so the observer's "it is dead now" is evidence
    about the fixture rather than about the child's timing.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    register_child_process(child)
    _CHILD_LEFT_BY_PREVIOUS_TEST.append(child)


def test_spawned_children_are_reaped_by_the_existing_contract(
    reclaimed,
):
    """A registered child is joined by the teardown that already reclaims workers.

    The rule is the suite's own — a test hands back every resource it
    starts — and it is enforced in one place,
    ``tests/conftest.py::clean_execution_state``, which already releases
    and joins the workers a test leaves behind. This pins that the
    provider's child processes ride in that same teardown rather than
    needing a second, provider-only mechanism that the next test would
    have to remember.

    ``returncode`` is the assertion that carries the weight: a child that
    was merely *terminated* and never waited on is a zombie, and a
    zombie reports a return code to ``poll()`` as readily as a reaped
    one. What distinguishes them here is that the child cannot have
    finished its own 120 seconds between two tests, so a non-None
    return code means the teardown stopped it and collected it.
    """
    assert _CHILD_LEFT_BY_PREVIOUS_TEST, "the polluter above did not run"
    (child,) = _CHILD_LEFT_BY_PREVIOUS_TEST

    assert child in reclaimed()["children"], (
        "the teardown never saw the child the test registered"
    )
    assert child.returncode is not None, "the child outlived its own test"
    assert child.poll() is not None, "the child is still running"


def test_the_previous_test_published_a_secret_and_left_the_read_end_open(
    monkeypatch, canary, tmp_path, register_open_fd,
):
    """Publish a real pipe and hand the read end to nobody; the test below reads it.

    Publishes through the provider rather than opening a pipe by hand,
    because the descriptor this work derives is the one the provider
    hands out — a test of a stand-in pipe would pass while the real
    handoff leaked.

    The value is made to come from a stubbed **keychain**, not from an
    environment variable. ``publish_secret_fd`` is the launcher's read
    and the launcher reads the keychain; setting ``FEISHU_APP_SECRET``
    and expecting a descriptor back is the shape the provider had before
    the handoff moved. The descriptor is what this test is about either
    way.
    """
    secret = canary("feishu_secret", tmp_path)
    # The index too: a keychain read is aimed *by* the account, and with
    # none set the provider answers "missing" without ever running the
    # stubbed tool — which would make this test pass for the wrong
    # reason (no descriptor leaked because none was ever opened).
    monkeypatch.setenv(
        credentials.SECRET_SPECS["feishu_app_secret"].account_env_key,
        "test-account-for-the-fixture",
    )
    monkeypatch.setattr(
        credentials,
        "_run_security",
        lambda argv, timeout: secret.encode("utf-8") + b"\n",
    )
    credentials.reset_cache()

    fd = credentials.publish_secret_fd("feishu_app_secret")
    assert fd is not None, "no secret to publish, so no descriptor to leak"

    register_open_fd(fd)
    _PIPE_LEFT_BY_PREVIOUS_TEST.append(fd)


def test_open_fds_are_reaped(reclaimed):
    """No pipe read end outlives the test that was handed one.

    A pipe has one reader and the descriptor is spent by reading it, but
    a test that publishes and then fails an assertion never gets there —
    and a descriptor left open is not a descriptor the next test can
    find, let alone close. The teardown that already reclaims a test's
    workers closes it, and the ``fds`` record says the teardown saw this
    one at all, so a fixture that logged the descriptor without closing
    it would still fail here.
    """
    assert _PIPE_LEFT_BY_PREVIOUS_TEST, "the polluter above did not run"
    (fd,) = _PIPE_LEFT_BY_PREVIOUS_TEST

    assert fd in reclaimed()["fds"], (
        "the teardown never saw the descriptor the test registered"
    )
    assert not _is_open_pipe(fd), (
        f"fd {fd} is still an open pipe after its test ended"
    )
