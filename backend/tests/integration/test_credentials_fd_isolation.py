"""Isolation of the pipe handoff, proved against a real child process.

Why this suite exists
---------------------
:func:`credentials.publish_secret_fd` puts a secret on an anonymous pipe
and hands the read end to a caller. The whole design rests on one
property of that handoff: the descriptor reaches a child **only** when
the spawn declared it in ``pass_fds``. A descriptor is created with
close-on-exec, so an undeclared spawn drops it at the moment of
``exec``, and the child inherits nothing.

That property cannot be observed from inside the process that publishes.
The unit layer (``tests/unit/test_credentials_fd_payload.py``) pins the
round trip and the wire form with the reader in the *same* interpreter —
which is exactly the setup in which the property is absent, because a
descriptor the process still holds is a descriptor it can read whatever a
``Popen`` would have done. Mocking ``Popen`` does not close the gap
either: a mock records the arguments it was called with and has no kernel
behind it, so a test built on one shows that the code *asked* for
``pass_fds`` and says nothing about whether the asking worked.

The only way to see the property hold is to hand the descriptor to a real
interpreter and let it try. So the children here are real —
``sys.executable -c <source>`` — and nothing on the path is patched.
They are short-lived and hermetic: the value is a canary that exists only
in this process, and the environment each child is given is built from
scratch rather than inherited, so nothing a developer has exported can
take part in the result.

The exit-code contract
----------------------
Each child speaks in exit codes, because "could not read" and "read
something else" must never both look like success:

* **0** — the pipe was read to EOF and the payload carried the named
  secret. The value itself is never printed: the child writes a SHA-256
  digest of it and the parent compares that, so a failing assertion
  cannot put a secret into a CI log.
* **3** — there was no descriptor to read. The variable naming it was
  absent, was not an integer, named no open descriptor in this process,
  or named something that is not a pipe. A number in the environment is
  an *address*, not a key; this is the exit code that says so.
* **anything else** — the child broke, which is a different failure and
  is reported as one rather than smoothed into "no secret".

The two numbers are written into the child scripts as literals and
expected here through :data:`EXIT_CANNOT_READ`. Nothing checks that the
two agree by inspection; ``test_undeclared_child_cannot_read`` checks it
by running the child and comparing its real exit status, which is the only
comparison worth having.

What is pinned
--------------
* A spawn that declares the descriptor delivers the secret; the same
  spawn, the same child, the same environment, without it, delivers
  nothing. One argument separates them, so the isolation is
  attributable to that argument and not to anything else in the setup.
* The value survives being relayed. A publishes, B reads and
  republishes on a pipe of its own, C reads: a handoff that only works
  for the process that started it is a handoff that does not work.
* No environment along the chain carries the secret. A is the only level
  holding it in one — put there by the test, so that the stubbed keychain
  standing in for A's has something to return — while every environment
  past the first spawn carries a descriptor number, which is checked
  entry by entry.

Resource discipline
-------------------
Publishing opens a pipe, and every child is a process, so a test that
failed an assertion before joining would leave both behind. Descriptors
go to ``register_open_fd`` and children to ``register_child_process``
(``tests/conftest.py``), whose teardown closes and joins them; the runner
fixture below also asserts each child is reaped before it returns a
result, so the join is observed rather than assumed.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

import credentials

pytestmark = [pytest.mark.integration]


#: ``backend/``, resolved from this file rather than written out: the
#: repository is checked out at a different path on every machine, and a
#: child process is started by an absolute path, so this has to be right
#: wherever the suite runs. Also the directory every child is given on
#: ``PYTHONPATH`` — the child imports the module under test, and an
#: import that only works in the parent proves nothing.
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The secret this suite hands across the boundary, named by its logical
#: name — the name the payload is keyed by and the name the rest of the
#: project passes around.
LOGICAL_NAME = "feishu_app_secret"

#: The variable that carries a descriptor number (never a value) to a
#: child, derived through the module's own entry point rather than
#: written out, so a change to the prefix is a change this file follows.
FD_ENV_VAR = credentials.secret_fd_env_var(LOGICAL_NAME)

#: The variable that carries the value A publishes, read from the spec
#: table so a renamed row cannot leave this suite setting a variable
#: nothing reads. ``publish_secret_fd`` goes to the keychain, so what the
#: fixture does with this name is hand it to the keychain *stand-in* —
#: the value still enters the chain here and nowhere else.
FALLBACK_ENV_KEY = credentials.SECRET_SPECS[LOGICAL_NAME].fallback_env_key

#: How long a child may take. Generous, because it covers a loaded CI
#: runner rather than a child that refuses to finish — and bounded,
#: because a child that never exits must fail the test that started it
#: rather than spend the shard's whole budget. The suite's own per-test
#: ceiling is the backstop behind this. The relay's own copy of the bound
#: is a literal inside its source, for the same reason: a number that
#: cannot be substituted into a script is a number nothing checks.
_CHILD_TIMEOUT_SECONDS = 60

#: What a child exits with when it had no descriptor to read. The
#: expected answer rather than the source of it — see the module
#: docstring — so a test names what it is waiting for rather than
#: repeating a literal.
EXIT_CANNOT_READ = 3


# ---------------------------------------------------------------------------
# The child
# ---------------------------------------------------------------------------
#
# Two scripts, both executed by ``python -c`` in a real interpreter.
#
# ``_CHILD_SRC`` is the last link: it takes a descriptor number from the
# environment, tries to read it, and answers with an exit code. Three
# checks stand between the number and the read, and each closes a way the
# test could be fooling itself:
#
#   * the harness names are read with ``[]``, not ``get``. A missing one
#     means the parent that spawned this child is broken, and a traceback
#     (exit 1) says that where "no secret" would send the reader looking
#     in the wrong place.
#   * the number is asked to be an integer, and the descriptor it names
#     is asked to be an open pipe. The first rejects a value that is not
#     an address; the second rejects an address that happens to name
#     something else this interpreter opened, which would otherwise be
#     read as "the secret was delivered" when nothing was.
#   * only then is the module's own reader used, so a successful read is
#     the real handoff rather than this file's reimplementation of it.
#
# The harness names reach the child through the environment rather than
# through the source, which is what lets the script be one constant with
# nothing interpolated into it: an f-string would have to double every
# brace in the child's own literals, and the first one somebody forgot to
# double would be a syntax error in a file whose failure message is an
# exit code.
#
# The child is told nothing about the value it is expected to find,
# because a child that knew the expected value could not fail the way
# this suite needs it to fail — it would have to be handed the answer,
# and a harness that supplies the answer cannot report the answer being
# wrong. The parent compares the digest afterwards instead.


_CHILD_SRC = """
import hashlib
import os
import stat
import sys

fd_var = os.environ["PDT_TEST_FD_VAR"]
logical = os.environ["PDT_TEST_SECRET_NAME"]

raw = os.environ.get(fd_var)
if raw is None:
    sys.exit(3)
try:
    fd = int(raw)
except ValueError:
    sys.exit(3)

try:
    mode = os.fstat(fd).st_mode
except OSError:
    sys.exit(3)
if not stat.S_ISFIFO(mode):
    sys.exit(3)

import credentials

payload = credentials.read_secret_fd(fd)
value = payload.get(logical)
if value is None:
    sys.exit(3)

sys.stdout.write(hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest())
sys.exit(0)
"""


# ---------------------------------------------------------------------------
# The relay's middle level
# ---------------------------------------------------------------------------
#
# B is the link that makes this suite an integration test rather than a
# pair of assertions: it receives a secret it was never told the value
# of, and hands it on. A handoff that only works for the process that
# started it is not a handoff, and nothing short of a second real process
# can tell the two apart.
#
# B cannot use ``publish_secret_fd`` to re-publish: that function reads
# the keychain, and B's environment carries no keychain index and no
# value, so it would return ``None`` — the correct answer, and a dead
# end. B therefore writes the wire form itself, through the
# module's own encoder, deliberately: the bytes C receives are then the
# bytes ``publish_secret_fd`` would have written. A relay that re-encoded
# the payload by hand would be exercising a second, private format, and
# the failure that could hide behind it is the one this file exists to
# rule out. The encoder is private to the module; if it is renamed, the
# relay dies with an ``AttributeError`` in a real process, which is
# loud and points at the rename.
#
# B forwards its own environment to C and overwrites one variable in it,
# which is what a real relay does — the child needs the same import path
# and the same harness names, and a fresh environment built from nothing
# would have to reconstruct both. The environment B reports back is the
# one it actually handed over, so the deepest level of the chain can be
# inspected without anybody having to take B's account of it on trust.


_RELAY_BODY = """
import json
import os
import subprocess
import sys

fd_var = os.environ["PDT_TEST_FD_VAR"]
logical = os.environ["PDT_TEST_SECRET_NAME"]

import credentials

try:
    payload = credentials.read_secret_fd(int(os.environ[fd_var]))
except (KeyError, ValueError, OSError):
    sys.exit(3)
value = payload.get(logical)
if value is None:
    sys.exit(3)

read_fd, write_fd = os.pipe()
try:
    blob = credentials._encode_payload(logical, value)
    sent = 0
    while sent < len(blob):
        sent += os.write(write_fd, blob[sent:])
finally:
    os.close(write_fd)

child_env = dict(os.environ)
child_env[fd_var] = str(read_fd)
try:
    child = subprocess.run(
        [sys.executable, "-c", CHILD_SRC],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
        pass_fds=(read_fd,),
        timeout=60,
    )
finally:
    os.close(read_fd)

sys.stdout.write(json.dumps({
    "exit": child.returncode,
    "digest": child.stdout.decode("utf-8", "replace").strip(),
    "stderr": child.stderr.decode("utf-8", "replace"),
    "child_env": child_env,
}))
"""

#: B's complete source: the child script bound to a name, then the body
#: that spawns it. The braces in the body are never parsed as
#: placeholders — they arrive as an argument, not as part of the
#: template — so the only two ``{}`` in the template are the two holes.
#: The name is spelled without this module's leading underscore because
#: it is B's global, not one of this module's: a script that referred to
#: ``_CHILD_SRC`` would be relying on a naming convention that stopped
#: mattering the moment the text crossed into another process.
_RELAY_SRC = "CHILD_SRC = {}\n{}".format(repr(_CHILD_SRC), _RELAY_BODY)


class ChildResult(NamedTuple):
    """What a child left behind, after it was joined."""

    returncode: int
    stdout: bytes
    stderr: bytes

    def text(self) -> str:
        """stdout decoded, never raising on bytes that are not text."""
        return self.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _a_stubbed_keychain_and_no_developer_secrets(monkeypatch):
    """Give the parent a keychain that holds exactly what the test exported.

    Three things, and each closes a way this file could pass for the
    wrong reason.

    **The keys derived from ``SECRET_SPECS`` are cleared**, so a
    workstation that really does export a provider secret cannot make a
    "could not read" result pass. The list is derived, never written out:
    a row added to the table would otherwise inherit whatever the machine
    running the suite has exported, and the assertions here would pass on
    a CI runner and fail on a workstation.

    **The platform keychain is switched off, and the switch is asserted**
    rather than assumed. ``publish_secret_fd`` is the *launcher's* read
    now — it goes to the keychain directly and does not consult this
    switch — so what the switch guards here is the other direction: the
    real ``/usr/bin/security`` must never be the thing that answers, on
    any machine this suite runs on.

    **The keychain read is stubbed**, at the process boundary rather than
    above it, so everything the handoff does on top of the read stays
    real. The stand-in answers from the spec's own fallback variable,
    which is what lets the four tests below keep saying "put a value
    here, publish it" in exactly the terms they always did — with the
    keychain read that now sits in front of the publish in between. That
    the value arrives from a stand-in rather than a real keychain is not
    a property any of them is about; they are about the pipe.

    The memo is not touched. ``tests/conftest.py`` already empties it
    around every test, autouse, and a second reset would be a second way
    to do the same thing.
    """
    monkeypatch.delenv(credentials._SWITCH_ENV_KEY, raising=False)
    for spec in credentials.SECRET_SPECS.values():
        monkeypatch.delenv(spec.fallback_env_key, raising=False)
        monkeypatch.delenv(spec.account_env_key, raising=False)

    assert credentials.keychain_disabled() is True, (
        "the keychain is not off, so a lookup here would consult the "
        "platform instead of the environment this file controls"
    )

    def _stubbed_keychain(spec):
        value = os.environ.get(spec.fallback_env_key)
        if value:
            return (value, credentials.SOURCE_KEYCHAIN)
        return (None, credentials.SOURCE_MISSING)

    monkeypatch.setattr(credentials, "_resolve_from_keychain", _stubbed_keychain)


@pytest.fixture
def run_child(register_child_process):
    """Return a function that starts one real interpreter and joins it.

    ``subprocess.run`` would join too, but it keeps the process handle
    to itself, and the suite's rule is that a test hands back what it
    starts. Every child is therefore registered for the teardown that
    already reclaims a test's workers, and this function additionally
    waits for it and asserts the wait happened — so a child that ignored
    its own timeout is reported as the failure it is, and the
    registration is the backstop for a test that fails before reaching
    that assertion.

    ``pass_fds`` is the argument under test throughout this file, so it
    is a parameter of the runner rather than something a caller has to
    reach past the runner to set.
    """
    def _run(
        source: str,
        *,
        env: dict,
        pass_fds: tuple = (),
        timeout: int = _CHILD_TIMEOUT_SECONDS,
    ) -> ChildResult:
        proc = subprocess.Popen(
            [sys.executable, "-c", source],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(_BACKEND_DIR),
            pass_fds=tuple(pass_fds),
        )
        register_child_process(proc)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail(
                "a child was still running after {}s and had to be killed; "
                "it never reached its own exit code".format(timeout)
            )
        assert proc.poll() is not None, "the child was handed back before it exited"
        return ChildResult(proc.returncode, stdout, stderr)

    return _run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _digest(value: str) -> str:
    """The fingerprint a child reports instead of the value it read."""
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


def _handoff_env(fd: Optional[int] = None) -> dict:
    """The environment this file hands to a child process.

    Built from nothing rather than copied from ``os.environ``, and that
    is the point of it. A copy would carry whatever the machine running
    the suite has exported — including, on a workstation that really
    notifies, the provider secret itself — and a test asserting that a
    child cannot reach the value would then be asserting something about
    the developer's shell. Four entries are all a child needs: a
    ``PATH`` (the interpreter is started by absolute path, but anything
    a child shells out to would want one), the directory it imports the
    module under test from, and the two names that tell it which
    variable to read and which key the payload will carry.

    ``fd`` is optional so a caller can build the environment of a child
    that was told about a descriptor without being given one — the
    undeclared case, where the address travels and the descriptor does
    not.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_BACKEND_DIR),
        "PDT_TEST_FD_VAR": FD_ENV_VAR,
        "PDT_TEST_SECRET_NAME": LOGICAL_NAME,
    }
    if fd is not None:
        env[FD_ENV_VAR] = str(fd)
    return env


def _env_names_carrying(env: dict, secret: str) -> list:
    """Return the names of the variables whose value contains ``secret``.

    A substring search rather than an equality test, and deliberately:
    the failure this rules out is a secret that travelled *inside* a
    larger value — prefixed, suffixed, or a whole environment serialised
    into one variable — and equality would call that clean.
    """
    return sorted(name for name, value in env.items() if secret in value)


def _relay(run_child, fd: int) -> dict:
    """Run A → B → C and return B's report of what C answered.

    B is a real process, so a failure at the middle level is reported
    with B's own stderr rather than swallowed: a relay that dies is a
    different defect from a relay that delivers the wrong thing, and both
    would otherwise arrive as one missing digest.
    """
    result = run_child(_RELAY_SRC, env=_handoff_env(fd), pass_fds=(fd,))
    assert result.returncode == 0, (
        "the relay's middle level exited {}, expected it to read the "
        "descriptor and pass it on.\nstderr: {}".format(
            result.returncode, result.stderr.decode("utf-8", "replace")
        )
    )
    return json.loads(result.text())


# ---------------------------------------------------------------------------
# The declaration is the whole mechanism
# ---------------------------------------------------------------------------


def test_declared_pass_fds_child_reads_the_payload(
    monkeypatch, canary, tmp_path, register_open_fd, run_child
):
    """A spawn that declares the descriptor delivers the secret.

    The positive half of the isolation, asserted twice over: the exit
    code says the child read a payload, and the digest says the payload
    was *this* value. Either assertion alone would pass on a child that
    read a pipe carrying something else.
    """
    secret = canary("feishu_secret", tmp_path)
    monkeypatch.setenv(FALLBACK_ENV_KEY, secret)

    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None, "no secret was published, so no descriptor exists"
    register_open_fd(fd)

    result = run_child(_CHILD_SRC, env=_handoff_env(fd), pass_fds=(fd,))

    assert result.returncode == 0, (
        "a child that was given the descriptor in pass_fds could not read "
        "it.\nexit: {}\nstderr: {}".format(
            result.returncode, result.stderr.decode("utf-8", "replace")
        )
    )
    assert result.text() == _digest(secret)


def test_undeclared_child_cannot_read(
    monkeypatch, canary, tmp_path, register_open_fd, run_child
):
    """The same child, the same environment, no declaration: nothing.

    Every input is the one the passing test above used — the same
    script, the same value, the same environment, and the same
    descriptor *number* in it. The single difference is that this spawn
    does not declare the descriptor, so the child cannot read it. That
    is what makes the exit code attributable: a script broken in the same
    way for both tests would fail the one above, and a number that had
    travelled alone would have been enough if the address were the key.

    The environment is checked too. A child that could not read the pipe
    and *did* find the value in its environment would be the whole
    failure this design exists to prevent, so the absence is asserted
    rather than left for the exit code to imply.
    """
    secret = canary("feishu_secret", tmp_path)
    monkeypatch.setenv(FALLBACK_ENV_KEY, secret)

    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None, "no secret was published, so no descriptor exists"
    register_open_fd(fd)

    env = _handoff_env(fd)
    result = run_child(_CHILD_SRC, env=env, pass_fds=())

    assert result.returncode == EXIT_CANNOT_READ, (
        "a child that was not given the descriptor read it anyway, or "
        "failed for some other reason.\nexit: {}\nstdout: {!r}\n"
        "stderr: {}".format(
            result.returncode,
            result.stdout,
            result.stderr.decode("utf-8", "replace"),
        )
    )
    assert result.stdout == b"", "the child reported a value it could not have read"
    assert _env_names_carrying(env, secret) == [], (
        "the child could have read the value out of its environment "
        "instead of failing to read the pipe"
    )


# ---------------------------------------------------------------------------
# A → B → C
# ---------------------------------------------------------------------------


def test_three_level_relay_delivers_same_value(
    monkeypatch, canary, tmp_path, register_open_fd, run_child
):
    """The value crosses two hops and arrives as the value that started it.

    A publishes, B reads and republishes on a pipe of its own, C reads.
    The middle level is the whole point: a handoff that only works for
    the process that started it is not a handoff, and the only way to
    see the difference is to have a process in the middle that resolved
    the value for itself and re-published it.

    C's exit code and its digest are both checked, for the same reason
    the two assertions in the first test are both checked: a C that
    exited 0 having read some payload would otherwise satisfy the chain.
    """
    secret = canary("feishu_secret", tmp_path)
    monkeypatch.setenv(FALLBACK_ENV_KEY, secret)

    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None, "no secret was published, so no descriptor exists"
    register_open_fd(fd)

    report = _relay(run_child, fd)

    assert report["exit"] == 0, (
        "the deepest level could not read what the middle level passed "
        "on.\nstderr: {}".format(report["stderr"])
    )
    assert report["digest"] == _digest(secret)


def test_no_level_env_contains_the_secret_value(
    monkeypatch, canary, tmp_path, register_open_fd, run_child
):
    """No environment past the first spawn carries the value.

    What is inspected is the environment each level **hands to the
    next**, which is the only place a value could cross: a child that
    adds an entry to its own environment afterwards has nothing to add
    it from, and an interpreter that sets a locale variable in itself
    (as CPython does under the C locale) is not a leak. The deepest
    level is checked against B's own account of what it passed rather
    than against a reconstruction of it here — a test that rebuilt the
    expected environment would be asserting that B built the
    environment B reported, which is a fact about the test.

    Delivery is asserted first, and that ordering is load-bearing. "No
    environment carries the secret" is also what every environment
    carries when nothing was ever delivered, so without the two
    assertions above it, this test would pass for a chain that had lost
    the value at the first spawn.
    """
    secret = canary("feishu_secret", tmp_path)
    monkeypatch.setenv(FALLBACK_ENV_KEY, secret)

    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None, "no secret was published, so no descriptor exists"
    register_open_fd(fd)

    level_b_env = _handoff_env(fd)
    report = _relay(run_child, fd)

    # Non-vacuous: the chain really did carry the value, so the absence
    # asserted below is a property of the handoff rather than of an
    # empty one.
    assert report["exit"] == 0, "the chain delivered nothing to check"
    assert report["digest"] == _digest(secret), "the chain delivered the wrong value"

    for level, env in (("B", level_b_env), ("C", report["child_env"])):
        offenders = _env_names_carrying(env, secret)
        assert offenders == [], (
            "level {}'s environment carries the secret in {}".format(level, offenders)
        )
        # What *is* there is the address, and only the address: a variable
        # holding anything other than a descriptor number would smuggle
        # the value past a check that only looked for a substring.
        assert env[FD_ENV_VAR].isdigit(), (
            "level {}'s descriptor variable is not a number: {!r}".format(
                level, env[FD_ENV_VAR]
            )
        )
