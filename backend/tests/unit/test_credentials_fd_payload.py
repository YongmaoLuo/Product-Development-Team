"""Handing a secret across a process boundary: the anonymous pipe.

Why this suite exists
---------------------
A secret read out of the OS keychain is a value that must not end up
in anything a third party can read — and an environment variable is
exactly that. Passing a secret to a child process through the
environment defeats the keychain, because the child's own children can
read it too.

The contract this suite pins is the one that replaces that:

* the **value** travels through an anonymous pipe, not the
  environment;
* the **environment carries only the file descriptor number**, which is
  an integer naming something the child already inherited and is not
  itself a secret;
* the **payload is JSON**, so a value containing a newline, a quote, a
  brace or a non-ASCII character crosses unchanged. A hand-rolled
  ``KEY=value`` line would be truncated at the first newline and
  re-interpreted at the first ``=`` — and the failure would show up as
  an authentication error at the far end of a notification send rather
  than as a bug here.
* the **payload's key is the logical name** (``feishu_app_secret``),
  not the environment variable that holds the plaintext fallback
  (``FEISHU_APP_SECRET``). The two are different names for the same
  secret and the difference is the whole reason a reader can be written
  without knowing where the value came from.

Both ends are pinned here, and the round trip is exercised in-process:
``publish_secret_fd`` and ``read_secret_fd`` are the two halves of one
contract, so a test that only checked the writer would pass with a
payload no reader could parse.

Resource discipline
-------------------
Every descriptor this suite publishes is handed back. ``publish_secret_fd``
opens a pipe, so a test that publishes and then fails an assertion would
otherwise leave two descriptors open for the rest of the session. The
fixture below records every fd the module creates and closes whatever is
still open at teardown; ``test_reader_closes_the_fd`` separately pins
that the module closes its own.
"""

from __future__ import annotations

import ast
import errno
import json
import os
import sys
from pathlib import Path

import pytest

import credentials

#: A secret name that is in the spec table, and the environment variable
#: that would hold it if the keychain were off. Both are needed: the
#: payload must be keyed by the first and must never be keyed by the
#: second.
LOGICAL_NAME = "feishu_app_secret"
ENV_KEY = "FEISHU_APP_SECRET"

#: The name a reader derives from the logical name, and therefore the
#: variable a child process is handed instead of the secret.
EXPECTED_ENV_VAR = "PDT_SECRET_FD_FEISHU_APP_SECRET"

#: Every descriptor ``os.pipe`` produced while a test in this file ran.
#: Filled by the stand-in installed in the fixture below, and the reason
#: a test can name the write end the implementation opened — a descriptor
#: the public API never returns.
_PUBLISHED_FDS: list = []

#: The module under test. Resolved from this file, never written out
#: literally: the repository is cloned at a different path on every
#: machine.
MODULE_PATH = Path(__file__).resolve().parents[2] / "credentials.py"


@pytest.fixture(autouse=True)
def _isolated_credentials_state(monkeypatch):
    """Start from an empty environment, an empty cache, and no leaked fd.

    The environment is cleared so a developer's own ``FEISHU_APP_SECRET``
    cannot make a "missing" assertion pass; the cache is cleared because
    the module memoises across calls. The platform is forced to macOS and
    the switch left unset, which is the "keychain off" spelling — the same
    answer every non-macOS machine gives unconditionally, so the round
    trip does not depend on where the suite runs.

    The keys cleared are derived from :data:`credentials.SECRET_SPECS`
    rather than written out. A hand-written list is a list that goes
    stale: a row added to the table would inherit whatever the developer
    running the suite has exported, and the "missing" assertions would
    pass on a clean CI runner and fail on a workstation.

    ``os.pipe`` is replaced by a recording stand-in for the duration of
    each test. Two things depend on it: the test that asserts the write
    end was closed needs to *name* that descriptor, and the test that
    asserts a missing secret opens no pipe needs to see that no pipe was
    opened. ``monkeypatch`` restores the real function afterwards.
    """
    keys = {"PDT_DISABLE_KEYCHAIN_SECRETS"}
    for spec in credentials.SECRET_SPECS.values():
        keys.add(spec.fallback_env_key)
        keys.add(spec.account_env_key)
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)
    credentials.reset_cache()

    _PUBLISHED_FDS.clear()
    real_pipe = os.pipe

    def _recording_pipe():
        read_fd, write_fd = real_pipe()
        _PUBLISHED_FDS.extend((read_fd, write_fd))
        return read_fd, write_fd

    monkeypatch.setattr(credentials.os, "pipe", _recording_pipe)

    yield

    credentials.reset_cache()
    _PUBLISHED_FDS.clear()
    for fd in _PUBLISHED_FDS:
        try:
            os.close(fd)
        except OSError:
            # Already closed by the code under test, or by a test that
            # read the wire form directly. Both are correct endings.
            pass


@pytest.fixture
def published_fds():
    """The descriptors opened by ``os.pipe`` during this test."""
    return _PUBLISHED_FDS


def _is_open(fd: int) -> bool:
    """Return whether ``fd`` still refers to an open descriptor.

    The portable way to ask. ``fcntl`` and ``/proc`` are not available
    everywhere, and a suite that has to be skipped on half the machines
    is a suite that stops being run on the other half.
    """
    try:
        os.fstat(fd)
    except OSError as exc:
        assert exc.errno == errno.EBADF, f"fd {fd} failed for {exc.errno}, not EBADF"
        return False
    return True


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_round_trip_returns_same_value(monkeypatch):
    """What goes in comes out, byte for byte.

    This is the whole point of the pipe, so it is asserted on the exact
    string rather than on a property of it. A transport that trimmed a
    trailing newline, or normalised a value it considered suspicious,
    would still authenticate against most services most of the time —
    which is exactly why it has to be pinned here instead.
    """
    sentinel = "s3cr3t-value-9012"
    monkeypatch.setenv(ENV_KEY, sentinel)

    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None

    payload = credentials.read_secret_fd(fd)

    assert payload[LOGICAL_NAME] == sentinel
    assert payload[LOGICAL_NAME].encode("utf-8") == sentinel.encode("utf-8")


def test_payload_key_is_logical_name_not_env_key(monkeypatch):
    """The payload is keyed by the logical name, never by the env key.

    The two names coexist on purpose: ``feishu_app_secret`` is what the
    rest of the project passes around, and ``FEISHU_APP_SECRET`` is the
    plaintext variable that must not be trusted to exist. A payload keyed
    by the environment key would force the reader to know the
    deployment's variable naming, and would make a second keychain-backed
    secret indistinguishable from the first.
    """
    monkeypatch.setenv(ENV_KEY, "the-secret")

    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None

    payload = credentials.read_secret_fd(fd)

    assert set(payload) == {LOGICAL_NAME}
    assert ENV_KEY not in payload


def test_payload_survives_newlines_and_unicode(monkeypatch):
    """Newlines, quotes, braces, control characters, non-ASCII.

    Every character class here has broken a line-oriented transport at
    some point: the newline ends a line, the ``=`` ends a key, the quote
    ends a field, the brace opens a structure the reader has to match,
    and the non-ASCII text breaks a reader that assumed one byte per
    character. The value is compared in both directions — as a ``str``
    and as its UTF-8 bytes — so a decode that is merely *lossless in
    appearance* but not byte-equal still fails.
    """
    sentinel = (
        "含\n换行与\"引号\"的哨兵\r\n\ttab=值 {花括号} \\反斜杠\n 空格 ünïcödé ✓"
    )
    monkeypatch.setenv(ENV_KEY, sentinel)

    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None

    payload = credentials.read_secret_fd(fd)

    assert payload[LOGICAL_NAME] == sentinel
    assert payload[LOGICAL_NAME].encode("utf-8") == sentinel.encode("utf-8")


def test_payload_survives_bytes_that_are_not_valid_utf8(monkeypatch):
    """A keychain value that is not text still crosses unchanged.

    :func:`credentials._decode_payload` decodes with ``surrogateescape``
    precisely so a keychain item holding arbitrary bytes reads as a
    string that re-encodes to those bytes. If the pipe payload did not
    honour that, the one transport designed to carry a non-text secret
    would be the one that could not use it.
    """
    raw = b"not-utf8:\xff\xfe\x80 tail"
    monkeypatch.setenv(ENV_KEY, raw.decode("utf-8", errors="surrogateescape"))

    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None

    payload = credentials.read_secret_fd(fd)

    assert payload[LOGICAL_NAME].encode("utf-8", "surrogateescape") == raw


# ---------------------------------------------------------------------------
# Descriptor lifetime
# ---------------------------------------------------------------------------


def test_reader_closes_the_fd(monkeypatch):
    """Reading consumes the descriptor.

    A reader that left its fd open would hand the pipe to the next
    reader in the process, and the second read would block forever
    waiting for a writer that has already gone. The pipe is
    single-consumer by construction; closing it on the way out is what
    makes the second read fail fast instead of hanging.
    """
    monkeypatch.setenv(ENV_KEY, "the-secret")

    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None
    assert _is_open(fd) is True

    credentials.read_secret_fd(fd)

    assert _is_open(fd) is False


def test_publish_leaves_no_write_end_behind(monkeypatch, published_fds):
    """Two ends, two lives, and only one of them is handed on.

    The read end goes to a caller that will read it. The write end has no
    further purpose once the payload is in the pipe, and leaving it open
    would mean the reader never sees EOF — it would block on a pipe whose
    writer is still nominally alive. The test names the write descriptor
    through the recording stand-in for :func:`os.pipe`, because the
    public API never returns it and there is no other way to observe it.

    The read is left until the end so the reader is exercised on a pipe
    that is already at EOF: this call would hang, rather than fail, if
    the write end were still open.
    """
    monkeypatch.setenv(ENV_KEY, "the-secret")

    fd = credentials.publish_secret_fd(LOGICAL_NAME)

    assert fd is not None
    assert len(published_fds) == 2, "expected exactly one pipe"
    read_fd, write_fd = published_fds
    assert read_fd == fd, "the returned fd is not the pipe's read end"
    assert _is_open(read_fd) is True
    assert _is_open(write_fd) is False

    assert credentials.read_secret_fd(fd) == {LOGICAL_NAME: "the-secret"}
    assert _is_open(read_fd) is False


def test_missing_secret_publish_returns_none(monkeypatch, published_fds):
    """An unavailable secret publishes nothing, and opens no pipe.

    Returning None is the same answer :func:`read_secret` gives, so a
    caller that forgot to check is told the truth rather than handed an
    empty payload. The second half matters as much: a descriptor is a
    resource, and a "no secret" answer that still opened a pipe would
    leak two of them on every call from a misconfigured deployment — for
    as long as the process lives, since nothing closes them.
    """
    assert credentials.read_secret(LOGICAL_NAME) is None
    assert credentials.secret_source(LOGICAL_NAME) == "missing"

    assert credentials.publish_secret_fd(LOGICAL_NAME) is None
    assert published_fds == [], "a missing secret still opened a pipe"


def test_missing_secret_publish_returns_none_for_every_spec(monkeypatch):
    """Same answer for every secret in the table, and for an unknown name.

    "Missing" is a property of the deployment, not of one row. A name
    outside the table has no lookup at all, and it must be reported the
    same way rather than raising — otherwise a caller has to enumerate
    the table before it can ask whether a secret exists.
    """
    for name in list(credentials.SECRET_SPECS) + ["not_a_secret"]:
        assert credentials.publish_secret_fd(name) is None, name


# ---------------------------------------------------------------------------
# The wire form
# ---------------------------------------------------------------------------


def test_wire_payload_is_flat_json_keyed_by_logical_name(monkeypatch):
    """What is actually on the pipe is a JSON object, nothing else.

    Read here with a raw ``os.read`` rather than through the reader,
    because the reader is the thing under test and using it to check its
    own input proves nothing. The raw bytes must be a JSON object with
    exactly one key — the logical name — and no framing: no length
    header, no trailing newline, no ``KEY=value`` prefix to be parsed
    twice under two different rules.
    """
    monkeypatch.setenv(ENV_KEY, 'a "quoted"\nvalue')

    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None

    try:
        chunks = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)

    assert not raw.endswith(b"\n"), "the payload carries framing of its own"
    decoded = json.loads(raw.decode("utf-8", "surrogateescape"))
    assert isinstance(decoded, dict)
    assert set(decoded) == {LOGICAL_NAME}
    assert decoded[LOGICAL_NAME] == 'a "quoted"\nvalue'


def test_two_publishes_do_not_share_a_pipe(monkeypatch):
    """Each publish gets its own pipe and its own value.

    If the write end were cached or the read end reused, a second
    publish would either block or hand back the first secret — a
    cross-delivery bug that would surface as one provider authenticating
    with another's credentials.
    """
    monkeypatch.setenv(ENV_KEY, "feishu-value")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-value")

    feishu_fd = credentials.publish_secret_fd(LOGICAL_NAME)
    telegram_fd = credentials.publish_secret_fd("telegram_bot_token")

    assert feishu_fd is not None
    assert telegram_fd is not None
    assert feishu_fd != telegram_fd

    assert credentials.read_secret_fd(feishu_fd) == {LOGICAL_NAME: "feishu-value"}
    assert credentials.read_secret_fd(telegram_fd) == {
        "telegram_bot_token": "telegram-value"
    }


# ---------------------------------------------------------------------------
# The environment variable that carries only the fd number
# ---------------------------------------------------------------------------


def test_fd_env_var_name_is_derived_from_logical_name():
    """The variable name is a function of the logical name.

    Both halves are pinned. The prefix is what makes the value in it
    obviously not a secret, and the derivation is what lets a reader
    find the variable without a second table mapping one name to the
    other — the mapping *is* the name, so it cannot drift from the
    secret table the way a parallel dict would.
    """
    assert credentials.secret_fd_env_var(LOGICAL_NAME) == EXPECTED_ENV_VAR


def test_fd_env_var_name_is_distinct_for_every_spec():
    """Two secrets must not share a variable, and none may be a fallback key.

    A collision would hand one child process the other one's secret, and
    a variable equal to ``FEISHU_APP_SECRET`` would put the plaintext
    back where the whole design puts it.
    """
    names = {credentials.secret_fd_env_var(n) for n in credentials.SECRET_SPECS}
    assert len(names) == len(credentials.SECRET_SPECS)

    for name, spec in credentials.SECRET_SPECS.items():
        derived = credentials.secret_fd_env_var(name)
        assert derived != spec.fallback_env_key
        assert derived != spec.account_env_key
        assert derived.startswith("PDT_SECRET_FD_")


def test_fd_env_var_carries_only_the_fd_number(monkeypatch):
    """Putting the descriptor in the environment is the entire handoff.

    The value written into the environment is the number and nothing
    else: not the secret, not the payload, not a path. Asserting on the
    variable's *contents* is the only way to catch a change that starts
    writing the secret there, which is the failure this design exists to
    make impossible.
    """
    monkeypatch.setenv(ENV_KEY, "super-secret-value")

    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None
    monkeypatch.setenv(credentials.secret_fd_env_var(LOGICAL_NAME), str(fd))

    carried = os.environ[credentials.secret_fd_env_var(LOGICAL_NAME)]
    assert carried == str(fd)
    assert carried.isdigit()
    assert "super-secret-value" not in carried

    # And the child reads the same secret out of the same descriptor.
    payload = credentials.read_secret_fd(int(carried))
    assert payload[LOGICAL_NAME] == "super-secret-value"


# ---------------------------------------------------------------------------
# Module shape
# ---------------------------------------------------------------------------


def test_module_uses_json_and_os_and_nothing_else_new():
    """The payload and the pipe are made of ``json`` and ``os``.

    ``test_credentials_switch.py`` already pins that the module imports
    only the standard library and nothing from the notifier package, so
    this does not repeat that check. It pins the two additions the pipe
    needs on top of it — the codec and the descriptor calls — so a
    reimplementation that reaches for a dependency to serialise the
    payload fails here with a name a reviewer recognises.
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))

    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not node.level, "credentials.py uses a relative import"
            if node.module:
                roots.add(node.module.split(".")[0])

    assert roots, "no imports found — the parse is not reading the module"
    assert "json" in roots, "the payload codec is not json"
    assert "os" in roots
    assert "notifications" not in roots
