"""A descriptor number in the environment is an *address*, not a key.

Why this suite exists
---------------------
The handoff between the launcher and the server puts the *number* of a
pipe in the environment and the *value* on the pipe. The number is
meaningful only in the descriptor table of the process that published
it, and it stops being meaningful the moment that pipe is closed.

The number travels, though. It is a string in the environment, and
every child inherits the string. What the child does **not** inherit is
the pipe behind it — unless it was handed over deliberately, which the
CI gate never does. So a descendant process can easily end up holding
``PDT_SECRET_FD_FEISHU_APP_SECRET=3`` while descriptor 3 in *its* own
table names something else entirely: pytest's saved copy of stdout, the
asyncio selector, the execnet channel of an xdist worker.

Reading descriptor 3 in that situation is what this suite is about.
Three failure shapes were observed, and all three come from the same
mistake — treating the number as a key that can be used as-is:

* **A hang.** Descriptor 3 was a pipe whose writer was still alive, so
  a blocking ``os.read`` waited forever. The xdist worker was killed
  by the per-test timeout, and the gate reported ``worker 'gwNN'
  crashed`` for every test the worker had been given.
* **A closed stranger.** Descriptor 3 was pytest's saved copy of
  stdout. The read succeeded, the descriptor was closed on the way out
  — and pytest's own teardown then restored stdout from a descriptor
  that no longer existed, so the whole run exited non-zero with
  ``OSError: [Errno 9] Bad file descriptor`` even with every test
  green.
* **A dead event loop.** The same close, one layer over, took out the
  selector an asyncio loop was using, and the service died of
  ``Bad file descriptor`` before it was ready.

The contract this suite pins
----------------------------
* A number that does not name a published secret pipe is **refused**:
  the secret is reported as missing and the caller falls through to the
  next source.
* A refused descriptor is **left exactly as it was** — still open, and
  still in the blocking mode it had. A descriptor this module does not
  own must not disappear because this module read it.
* A **real** published pipe is still read, still returns its payload,
  and is still closed afterwards — the "the descriptor is spent"
  contract the handoff depends on.

The refusals are deliberately narrow. Anything wider would break the
handoff itself, which is the one path where the number really does
name a pipe this module published.
"""

from __future__ import annotations

import errno
import os
import tempfile

import pytest

import credentials

#: A secret that is in the spec table. The suite needs a name the
#: module will actually look up, and one the launcher would publish.
LOGICAL_NAME = "feishu_app_secret"

#: The variable the launcher writes the descriptor number into. Derived
#: through the module's own entry point, so a change to the prefix is a
#: change this file follows rather than a break it.
FD_ENV_VAR = credentials.secret_fd_env_var(LOGICAL_NAME)

#: The plaintext fallback, read when the keychain is switched off. It is
#: what a refused descriptor has to fall through to.
FALLBACK_ENV_KEY = credentials.SECRET_SPECS[LOGICAL_NAME].fallback_env_key


@pytest.fixture(autouse=True)
def _clean_credentials_state(monkeypatch):
    """Start from an environment that says nothing about any secret.

    The descriptor variables are cleared because the machine running
    this suite may well have them exported — the launcher exports them
    to its own children, and every process it starts inherits them.
    A suite that read the ambient value would be testing the developer's
    shell, not the contract.

    The fallback variable is cleared for the same reason, and set
    explicitly by the tests that need it.

    The keychain is switched off by forcing the platform to macOS and
    leaving the switch unset, which is the "keychain off" spelling.
    That is what a CI runner and a container answer, so the fall-through
    assertions hold on every machine.
    """
    for spec in credentials.SECRET_SPECS.values():
        monkeypatch.delenv(spec.fallback_env_key, raising=False)
        monkeypatch.delenv(spec.account_env_key, raising=False)
        monkeypatch.delenv(credentials.secret_fd_env_var(spec.logical_name), raising=False)
    monkeypatch.delenv("PDT_DISABLE_KEYCHAIN_SECRETS", raising=False)
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)
    credentials.reset_cache()

    # ``publish_secret_fd`` reads the keychain **directly** — that is
    # the launcher's read, not the server's resolver's. So a test that
    # wants a value on a pipe has to supply a keychain, not an
    # environment variable. This stand-in answers with whatever the
    # test put in the fallback variable, which keeps the assertions
    # written in the same terms they are written in above, with the
    # read that actually happens in production now in between.
    def _keychain_holding_what_the_test_exported(spec):
        value = os.environ.get(spec.fallback_env_key)
        if value:
            return (value, credentials.SOURCE_KEYCHAIN)
        return (None, credentials.SOURCE_MISSING)

    monkeypatch.setattr(
        credentials, "_resolve_from_keychain", _keychain_holding_what_the_test_exported
    )

    yield
    credentials.reset_cache()


def _is_open(fd: int) -> bool:
    """Return whether ``fd`` still refers to an open descriptor."""
    try:
        os.fstat(fd)
    except OSError as exc:
        assert exc.errno == errno.EBADF, f"fd {fd} failed for {exc.errno}, not EBADF"
        return False
    return True


# ---------------------------------------------------------------------------
# A number that names something else
# ---------------------------------------------------------------------------


def test_a_number_naming_a_regular_file_is_refused(monkeypatch):
    """A descriptor that is not a pipe cannot be a published secret.

    A regular file is what a captured stdout looks like from the inside:
    pytest saves the real stdout with ``os.dup`` and restores it at the
    end of the run. The number that names that saved copy is a number in
    somebody else's descriptor table, and reading it as a secret is
    reading the wrong file.

    The refusal is the whole contract here. What the file contains is
    irrelevant — a pipe check alone would not be enough in general, and
    the payload check alone would not be enough either, because the file
    could contain anything.
    """
    with tempfile.TemporaryFile() as foreign:
        foreign.write(b"not a secret, just a file")
        foreign.flush()
        monkeypatch.setenv(FD_ENV_VAR, str(foreign.fileno()))

        assert credentials.read_secret(LOGICAL_NAME) is None
        assert credentials.secret_source(LOGICAL_NAME) == credentials.SOURCE_MISSING


def test_a_refused_descriptor_is_not_closed(monkeypatch):
    """The descriptor belongs to somebody else, so it must survive.

    This is the assertion that would have caught the original defect.
    The reader closed whatever number it was given, and a number that
    named pytest's saved stdout left pytest unable to restore it — so
    the run exited non-zero on ``os.dup2`` at teardown with every test
    green, and nobody could tell why.

    Closing a descriptor this module does not own is not a cleanup. It
    is damage: the owner gets ``Bad file descriptor`` at the next
    ``os.write`` or ``os.dup2`` and has no way to know why.
    """
    with tempfile.TemporaryFile() as foreign:
        fd = foreign.fileno()
        monkeypatch.setenv(FD_ENV_VAR, str(fd))

        credentials.read_secret(LOGICAL_NAME)

        assert _is_open(fd) is True, (
            "the reader closed a descriptor it did not open; the owner "
            "will get EBADF at its next use"
        )


def test_a_refused_descriptor_keeps_its_blocking_mode(monkeypatch):
    """The reader must put back what it changed.

    The refusal path sets the descriptor non-blocking so that the read
    cannot hang. That flag lives on the open file description, which
    every duplicate of the descriptor shares — so leaving it set would
    silently change the behaviour of the process that owns it. A
    descriptor handed back different from how it was found is a
    descriptor the caller cannot trust.
    """
    read_fd, write_fd = os.pipe()
    try:
        assert os.get_blocking(read_fd) is True
        monkeypatch.setenv(FD_ENV_VAR, str(read_fd))

        credentials.read_secret(LOGICAL_NAME)

        assert os.get_blocking(read_fd) is True, (
            "the reader left the descriptor non-blocking; the flag is on "
            "the open file description and every duplicate shares it"
        )
        assert _is_open(read_fd) is True
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_a_number_naming_a_pipe_with_a_live_writer_is_refused(monkeypatch):
    """A pipe whose writer is still alive is not the published pipe.

    :func:`credentials.publish_secret_fd` closes the write end before
    it hands the read end over, so the reader it wrote for meets EOF
    immediately. A pipe that still has a writer is somebody else's
    channel — an execnet worker's, for instance — and reading it would
    block until the per-test timeout killed the whole worker.

    The timeout marker is the assertion: without a non-blocking read
    this test hangs rather than fails, and a hang in a gate is the
    failure this suite exists to prevent.
    """
    read_fd, write_fd = os.pipe()
    try:
        monkeypatch.setenv(FD_ENV_VAR, str(read_fd))

        assert credentials.read_secret(LOGICAL_NAME) is None
        assert credentials.secret_source(LOGICAL_NAME) == credentials.SOURCE_MISSING
        assert _is_open(read_fd) is True
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.timeout(30)
def test_a_pipe_with_a_live_writer_does_not_hang_the_reader(monkeypatch):
    """The refusal must be a refusal, not a wait.

    Pinned separately from the case above because the two failures are
    different in kind. A hang is not a wrong answer, it is no answer:
    the process sits in ``os.read`` until something kills it, and every
    test sharing that worker dies with it. The timeout marker turns
    that into a failure this suite can report.
    """
    read_fd, write_fd = os.pipe()
    try:
        monkeypatch.setenv(FD_ENV_VAR, str(read_fd))
        credentials.read_secret(LOGICAL_NAME)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_a_standard_stream_number_is_refused(monkeypatch):
    """0, 1 and 2 are never a published secret pipe.

    The published pipe comes from ``os.pipe()`` in a process that
    already holds the three standard streams, so it is never one of
    them. A number that says otherwise is a number that arrived from
    somewhere else — and descriptor 1 in particular is the one that
    breaks the whole run when it is closed, because everything writes
    to it.
    """
    for fd in (0, 1, 2):
        monkeypatch.setenv(FD_ENV_VAR, str(fd))

        assert credentials.read_secret(LOGICAL_NAME) is None
        assert credentials.secret_source(LOGICAL_NAME) == credentials.SOURCE_MISSING

    # And the streams themselves are untouched.
    assert _is_open(1) is True
    os.write(1, b"")


def test_a_closed_number_is_refused(monkeypatch):
    """A number that names nothing is a number that names nothing.

    The launcher publishes, and then ``exec``\\s the server; a pipe
    whose writer died before the child read it leaves exactly this
    state. It must be reported as missing rather than raised, because
    the caller has a next source to try.
    """
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)

    monkeypatch.setenv(FD_ENV_VAR, str(read_fd))

    assert credentials.read_secret(LOGICAL_NAME) is None
    assert credentials.secret_source(LOGICAL_NAME) == credentials.SOURCE_MISSING


def test_a_non_integer_number_is_refused(monkeypatch):
    """A number that is not a number is not an address.

    The launcher writes a decimal integer. Anything else in the
    variable means the value was written by something that is not the
    launcher, and there is no descriptor to look up.
    """
    monkeypatch.setenv(FD_ENV_VAR, "not-a-number")

    assert credentials.read_secret(LOGICAL_NAME) is None
    assert credentials.secret_source(LOGICAL_NAME) == credentials.SOURCE_MISSING


# ---------------------------------------------------------------------------
# Falling through
# ---------------------------------------------------------------------------


def test_a_refused_number_falls_through_to_the_environment(monkeypatch):
    """A refused descriptor costs the deployment nothing.

    The resolution order is inherited descriptor, then environment,
    then missing. A number that cannot be used must not end the
    sequence — otherwise a deployment whose parent exported a stale
    number would lose the plaintext fallback it was configured with,
    and the notifier would silently go quiet.
    """
    monkeypatch.setenv(FALLBACK_ENV_KEY, "the-plaintext-fallback")
    with tempfile.TemporaryFile() as foreign:
        monkeypatch.setenv(FD_ENV_VAR, str(foreign.fileno()))

        assert credentials.read_secret(LOGICAL_NAME) == "the-plaintext-fallback"
        assert credentials.secret_source(LOGICAL_NAME) == credentials.SOURCE_ENVIRONMENT


def test_a_refused_number_is_reported_once_at_warning_level(monkeypatch, caplog):
    """The silent downgrade is why this survived so long.

    A number that cannot be used is a deployment problem: the launcher
    published a descriptor that never arrived, or some ancestor exported
    a number that means nothing here. Both are worth a line, and the
    line has to say which number it was about and what that number
    turned out to be — otherwise the operator has a server that quietly
    resolved its secrets from somewhere else and no way to find out.
    """
    import logging

    with tempfile.TemporaryFile() as foreign:
        fd = foreign.fileno()
        monkeypatch.setenv(FD_ENV_VAR, str(fd))

        with caplog.at_level(logging.WARNING, logger="credentials"):
            credentials.read_secret(LOGICAL_NAME)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    message = warnings[0].getMessage()
    assert str(fd) in message
    assert "address" in message


# ---------------------------------------------------------------------------
# The real handoff still works
# ---------------------------------------------------------------------------


def test_a_published_pipe_is_still_read(monkeypatch):
    """The refusal must not swallow the real thing.

    Every check added to refuse a stranger is also a chance to refuse
    the pipe this module published. The published pipe is the one path
    where the number really does name something, and it has to keep
    working — a reader that refuses everything is a server with no
    secrets and no error to show for it.
    """
    monkeypatch.setenv(FALLBACK_ENV_KEY, "the-published-secret")
    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None
    monkeypatch.setenv(FD_ENV_VAR, str(fd))

    assert credentials.read_secret(LOGICAL_NAME) == "the-published-secret"
    assert credentials.secret_source(LOGICAL_NAME) == credentials.SOURCE_INHERITED_FD


def test_a_published_pipe_is_still_spent(monkeypatch):
    """The descriptor is still closed after the read.

    A pipe has one reader. Leaving it open would hand it to whatever
    reads a descriptor next in this process, whose read would then wait
    for a writer that had already gone. Closing it on the way out is
    what turns the second read into a fast failure instead of a hang.
    """
    monkeypatch.setenv(FALLBACK_ENV_KEY, "the-published-secret")
    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None
    assert _is_open(fd) is True

    payload = credentials.read_secret_fd(fd)

    assert payload[LOGICAL_NAME] == "the-published-secret"
    assert _is_open(fd) is False
