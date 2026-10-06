"""The residual-secret probe in ``conftest.py``, tested on itself.

Why this suite exists
---------------------
The invariant being moved towards is "a provider secret is never handed to
a child process through ``os.environ``". No change inside this repository
can enforce that on its own: the value arrives from *outside* — an
``export`` in the parent shell, a ``launchd`` plist, a CI job definition
— and a child inherits whatever its parent was given. So the check has to
be a probe that reports what the environment already contains, and the
acceptance is two-staged: the code-side assertions come first, and the
operational migration that empties the ambient environment is what the
probe lets somebody verify afterwards (test design decision point 9).

That makes the probe's own properties load-bearing. It is the thing a
reader turns to when a security assertion fails, so:

* it must **report per key**. A single boolean over the whole set cannot
  say *which* provider leaked, and the answer decides which shell
  profile gets edited.
* it must **never carry a value**. The report is destined for a failure
  message and a CI log; a probe that printed the secret it found would
  be the leak it exists to detect. A canary injected for these tests
  stands in for that value, so a substring check against the rendered
  report is a real check rather than a formality.
* it must **not clear anything**. A probe that popped the key it found
  would make the very next test pass — a green suite that had removed
  the evidence, on a machine whose residue it never touched.

How the four contracts are observed
-----------------------------------
Each test is self-contained: it arranges the ambient environment it
needs through ``monkeypatch``, which unwinds at the end of the test
whatever it asserted. There is no polluter-then-observer pairing here —
a probe that reads ``os.environ`` can be pointed at any state from
inside the test that wants it, so nothing has to survive a boundary for
the next test to look at.

These are the unit layer (test design decision point 1): the probe is
test infrastructure, and infrastructure nothing tests is infrastructure
that is wrong in a way only a real failure reveals.
"""

from __future__ import annotations

import os

import pytest

import credentials

#: The two provider secret variable names this probe is pointed at. They
#: are the fallback environment keys of the two providers whose notifier
#: clients are being moved off ``os.environ``.
_SECRET_ENV_KEYS = ("FEISHU_APP_SECRET", "TELEGRAM_BOT_TOKEN")

#: The shortest fragment of an injected value that counts as a leak. Any
#: shorter and the check starts matching the key names themselves, which
#: is the one thing the report is *supposed* to contain.
_LEAK_MIN_FRAGMENT = 6


@pytest.fixture(autouse=True)
def _no_provider_secrets_from_the_developer(monkeypatch):
    """Start every test here with the provider's own environment cleared.

    A workstation that notifies Feishu really does export
    ``FEISHU_APP_SECRET``, and a test asserting "the report says this key
    is absent" would then read the developer's live value and fail — while
    passing on a CI runner that has none. That is the wrong way round for
    a suite: the assertion that guards a machine-dependent property is
    itself the machine-dependent one.

    The keys are derived from ``credentials.SECRET_SPECS`` and unioned
    with the two this file names, so neither a row added to the table nor
    a rename of one of these two can leave a test reading ambient state.
    """
    keys = set(_SECRET_ENV_KEYS)
    for spec in credentials.SECRET_SPECS.values():
        keys.add(spec.fallback_env_key)
        keys.add(spec.account_env_key)
    for key in keys:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Per-key reporting
# ---------------------------------------------------------------------------


def test_report_lists_each_key_separately(
    residual_secret_env, monkeypatch, canary, tmp_path
):
    """One key present and one absent is two separate answers.

    The whole point of the probe is telling an operator *which* provider's
    variable is still being exported, because that is the one file they
    have to edit. A report that collapsed to a single boolean — or that
    answered the same way for every key it was asked about — would leave
    the diagnosis pointing at "the environment" in general, which is the
    one answer that fixes nothing.

    Every requested key is present in the result, in the order asked, so
    a caller reading the report line by line can map a ``True`` back to
    the variable it names.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", canary("telegram_token", tmp_path))

    report = residual_secret_env(_SECRET_ENV_KEYS)

    assert report == {"FEISHU_APP_SECRET": False, "TELEGRAM_BOT_TOKEN": True}
    assert list(report) == list(_SECRET_ENV_KEYS), (
        f"the report dropped or reordered the keys it was asked about: {report!r}"
    )
    assert all(isinstance(present, bool) for present in report.values()), (
        f"the report is not a mapping of key -> bool: {report!r}"
    )


# ---------------------------------------------------------------------------
# The report carries no value
# ---------------------------------------------------------------------------


def test_report_output_contains_no_injected_value(
    residual_secret_env, monkeypatch, canary, tmp_path
):
    """Neither the value nor any fragment of it appears in the report.

    The report's destination is a failure message and a CI log, so a probe
    that echoed what it found would publish the secret it exists to
    detect — and it would do so at the moment somebody is already looking
    at it. Existence is the finding; the value is never part of it.

    The check is over every fragment of the value from
    ``_LEAK_MIN_FRAGMENT`` characters up, not just the whole string: a
    report that printed the value's tail — a truncation, a prefix, the
    derived suffix on its own — has leaked exactly as much as one that
    printed all of it. The value used here is a canary, so a fragment
    match is this test's own doing and not a coincidental hit on a real
    credential shape.
    """
    value = canary("feishu_secret", tmp_path)
    monkeypatch.setenv("FEISHU_APP_SECRET", value)

    report = residual_secret_env(_SECRET_ENV_KEYS)

    assert report["FEISHU_APP_SECRET"] is True, "the value was not found to begin with"
    assert set(report) == set(_SECRET_ENV_KEYS), (
        f"the report is not scoped to the keys it was asked about: {report!r}"
    )

    rendered = "{} {}".format(report, sorted(report.items()))
    leaked = [
        value[start:start + length]
        for start in range(len(value))
        for length in range(
            _LEAK_MIN_FRAGMENT, len(value) - start + 1
        )
        if value[start:start + length] in rendered
    ]
    assert not leaked, (
        "the report carries part of the value it found: {}".format(leaked)
    )


# ---------------------------------------------------------------------------
# The positive control
# ---------------------------------------------------------------------------


def test_report_flips_when_a_key_is_injected_and_removed(
    residual_secret_env, monkeypatch, canary, tmp_path
):
    """Absent, then present, then absent again — the answer tracks reality.

    Without this, a probe that returned ``False`` for everything would
    satisfy the "no secret in the environment" assertion that motivates
    it, and the security tests that depend on it would pass forever
    against a machine whose residue was never cleared. The report has to
    be able to say ``True``, or "no residue" and "no instrument" are the
    same observation.

    Both keys are checked at the middle step, so the pair cannot be
    satisfied by one key flipping and the other stuck.
    """
    assert residual_secret_env(_SECRET_ENV_KEYS) == {
        "FEISHU_APP_SECRET": False,
        "TELEGRAM_BOT_TOKEN": False,
    }, "the probe reports a residue that was never put there"

    monkeypatch.setenv(
        "TELEGRAM_BOT_TOKEN", canary("telegram_token", tmp_path)
    )

    injected = residual_secret_env(_SECRET_ENV_KEYS)
    assert injected == {"FEISHU_APP_SECRET": False, "TELEGRAM_BOT_TOKEN": True}

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")

    removed = residual_secret_env(_SECRET_ENV_KEYS)
    assert removed == {"FEISHU_APP_SECRET": False, "TELEGRAM_BOT_TOKEN": False}, (
        f"the key is gone from the environment but the report says: {removed!r}"
    )


# ---------------------------------------------------------------------------
# The probe changes nothing
# ---------------------------------------------------------------------------


def test_report_never_clears_the_environment(
    residual_secret_env, monkeypatch, canary, tmp_path
):
    """A key the probe reports as present is still present afterwards.

    A probe that popped what it found would delete the evidence and leave
    the machine exactly as dirty as before — the next test in the shard
    would read a clean environment, the security assertion downstream
    would pass, and the residue would still be exported to every child
    process the operator starts. The report is the deliverable; clearing
    is somebody else's decision, made on a machine the suite does not own.

    The comparison is over the whole environment rather than the two keys
    under test, so a probe that tidied anything else on its way past
    would fail here too.
    """
    value = canary("telegram_token", tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", value)
    before = dict(os.environ)

    report = residual_secret_env(_SECRET_ENV_KEYS)

    assert report["TELEGRAM_BOT_TOKEN"] is True
    assert dict(os.environ) == before, (
        "the probe changed the environment it was asked to read"
    )
    assert os.environ["TELEGRAM_BOT_TOKEN"] == value, (
        "the probe rewrote the value it found"
    )
