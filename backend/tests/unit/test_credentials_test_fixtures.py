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

Where the process-state contracts went
--------------------------------------
The first two bullets above are about *process* state, and a fixture that
isolates process state cannot be observed from inside the test it isolates
for — the pollution it removes has not happened yet, or has already been
cleaned. Those contracts were therefore written as polluter/checker pairs,
relying on the order the file is written in.

That reliance is gone. Definition order is not a property a parallel
scheduler honours, and a three-seed shuffled run of ``tests/unit`` caught
the checkers landing on a different worker from their polluter. With the
file pinned by ``@pytest.mark.xdist_group`` the order would have been
bought at the price of ``--dist loadgroup`` — ``xdist/remote.py`` applies
the group suffix only in that mode, and ``worksteal`` ignores the marker
entirely — so the pairs were **encapsulated instead**: they now live in
``tests/integration/reclaim_pairs.py``, run by exactly one atomic test in
``tests/integration/test_reclaim_pairs_driver.py``, in a session of their
own where the order is fixed and nothing else's is.

What is left here runs in any order, on any worker: the canary values are
pure functions of ``tmp_path`` and hold no state across the boundary.
"""

from __future__ import annotations

import re

import pytest

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
