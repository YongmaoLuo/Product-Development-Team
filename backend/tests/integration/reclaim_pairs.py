"""The three process-state contracts, as a session driven from a driver test.

Why this file is not named ``test_*.py``
----------------------------------------
Every contract below is a **pair**: a polluter that deliberately ends with
its hands full, and, immediately after it, the test that asserts the next
test started with clean hands. The property under test is "the autouse
teardown in ``tests/conftest.py`` reclaimed what the previous test left",
and it cannot be observed from inside a single test — by the time one test
is running, the teardown it would need to watch has either not happened
yet or already finished.

That makes the pair *definition order*, and definition order is not a
property a parallel scheduler can honour: ``-n`` splits a file across
workers, and ``--dist worksteal`` moves tests between them once the run is
under way. A checker that lands on a different worker finds an empty
module and fails for a reason that has nothing to do with the code under
test.

So the pair is **encapsulated rather than spread across the shard**. The
filename deliberately does not match ``python_files = test_*.py``: pytest
will not collect it in any lane, and the only way it runs is the driver
in ``test_reclaim_pairs_driver.py`` naming it explicitly. Inside that
nested session the order is the one written here, and the outer suite sees
a single atomic test with no ordering dependency at all.

The alternative — leaving these in ``tests/unit`` and pinning the file
with ``@pytest.mark.xdist_group`` — was rejected because it only works
under ``--dist loadgroup``: `xdist/remote.py` applies the group suffix
only when ``loadgroup`` is the distribution mode, and ``worksteal``
ignores the marker entirely. Pinning would have bought order at the price
of the scheduler.

Nothing here is new. The three pairs and their docstrings are moved
unchanged from ``tests/unit/test_credentials_test_fixtures.py``; what
changed is who starts them.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys

import pytest

import credentials

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

    The reader is cleared here too, and for the same reason: a
    developer workstation whose project-root ``.env`` names a reader is
    one where the keychain pair below passes without having been made to,
    and a CI runner — which has no ``.env`` — is one where it fails. The
    polluter therefore names its own reader rather than inheriting one,
    so what this file exercises is the test's own setup rather than the
    ambient configuration of the machine it ran on.
    """
    monkeypatch.delenv("PDT_DISABLE_KEYCHAIN_SECRETS", raising=False)
    monkeypatch.delenv(credentials._SECRET_READER_ENV_KEY, raising=False)
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
    # The reader too, for the same reason as the index below: the
    # provider has no fallback to the system tool, so with no reader
    # named it attempts no read and hands back no descriptor at all —
    # and a pair that leaked nothing because nothing was ever opened
    # passes for the wrong reason.
    monkeypatch.setenv(
        credentials._SECRET_READER_ENV_KEY, "reader-named-by-this-test"
    )
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
