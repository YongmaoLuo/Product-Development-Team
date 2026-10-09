"""The keychain read itself: the command line, and what happens when it fails.

Why this suite exists
---------------------
The resolution skeleton in `credentials.py` decides *where* a secret comes
from. This suite covers the read that decision ends in — the one place in
the project that starts a process — and it covers it against a **real
executable**, not a mock.

A mock of `subprocess.run` can only assert that the module *called*
something with some arguments. It answers nothing about whether those
arguments are a command line that works, and it cannot fail the way the
real tool fails: it does not return a nonzero exit code for an item that
is not in the keychain, and it does not hang when a keychain is locked
behind a GUI prompt. Those are the two failure modes that turn a keychain
read into an outage, and a mocked subprocess is structurally unable to
produce either. So the stand-in here is a script on disk with a shebang,
marked executable, invoked as a child process — `subprocess` behaviour is
the production code's behaviour, and it is not patched away.

What the stand-in sees is therefore the whole contract: the exact `argv`
the module builds, the child's real exit status, and the bytes it writes
to stdout.

Why integration, not unit
-------------------------
It starts a process, so it is not a pure function and does not belong in
the `unit` layer. It is also hermetic — the stand-in is a script in
`tmp_path`, and the keychain path the module builds is resolved against a
redirected `$HOME` — so it needs no keychain and touches no real secret.
It belongs to the integration layer for the subprocess, not for the keychain.

Which entry point this exercises
--------------------------------
`read_secret_from_keychain` — the **launcher's** call, and the only one
that starts a process any more. The server's `read_secret` resolves from
a descriptor a parent handed down and never reaches this code at all;
the provider-side half of the handoff is covered by the fd suites. The
function is named explicitly in every assertion below, which is what
keeps this suite honest: written against `read_secret`, these would be
asserting the command line of a lookup the provider no longer performs.

What is pinned
--------------
* **The command line.** The item is located by account and by nothing
  else. `security` also accepts `-s` (service), and a command that
  carries one has silently adopted a per-deployment keychain layout — a
  fact about one machine's keychain, written into source. The account is
  the only lookup key this project has, and the argv is asserted element
  by element so a future `-s` cannot be added quietly.
* **The account is the environment's index, verbatim.** Whatever
  `FEISHU_APP_ID` holds is what names the item.
* **No index, no process.** A lookup that has no key must not start a
  process at all. The stand-in's argv log is the counter, and this suite
  asserts it is empty — not merely that the read returned nothing.
* **Failure is a value, not an exception.** An item that is not in the
  keychain, a payload that is not valid UTF-8, a password that arrives
  with a trailing newline, and a `security` that hangs behind a locked
  keychain all have to leave the module answering `None` rather than
  pushing a traceback into a notification send.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import credentials

pytestmark = pytest.mark.integration


#: Every environment variable this suite's stand-in reads. Named here so
#: a test that changes one can see the whole set, and so an operator
#: reading the file can see that the stand-in's surface is closed.
_DOUBLE_ENV = (
    "PDT_TEST_ARGV_LOG",
    "PDT_TEST_PAYLOAD_FILE",
    "PDT_TEST_EXIT_CODE",
    "PDT_TEST_HANG",
)

#: Variables that describe *this deployment* rather than the stand-in, and
#: that therefore have to be cleared for the suite to be hermetic.
#: ``PDT_SECRET_READER_PATH`` is read out of the project-root ``.env``
#: before any test runs, so a workstation that has narrowed its keychain
#: ACL with ``setup.sh adopt`` has it set — and the module would start the
#: operator's own signed reader instead of the stand-in this file just
#: built, with the stand-in's argv log left empty and the read returning
#: nothing. CI has no ``.env``, which is the whole problem: the suite would
#: be green there and red on the machine that actually migrated.
_DEPLOYMENT_ENV = (
    "PDT_SECRET_READER_PATH",
)

#: The keychain this project opens, spelled out here rather than read
#: back from ``credentials._KEYCHAIN_PATH``.
#:
#: This file's stand-in is a *real process*, so the ``argv`` it leaves in
#: ``argv.log`` is the one artefact a reviewer can read without importing
#: anything. That makes it evidence, and evidence has to be spelled
#: independently of the thing it is evidence about: an expectation built
#: from ``credentials._KEYCHAIN_PATH`` compares the module's answer with
#: itself, so it stays green while the module points the read at the login
#: keychain — and the recorded command line, which is what a person
#: actually reads, would then attest to the opposite of what it appears
#: to. Naming the file here is what makes a regression in the constant
#: turn the ``argv`` red.
DEDICATED_KEYCHAIN_FILE = "Library/Keychains/runtime-secrets.keychain-db"

#: A real program, in ``/bin/sh`` so the suite needs no interpreter of
#: its own to run it. It records its whole command line — ``$0`` first,
#: then one argument per line, so the test can count invocations *and*
#: read back exactly what was executed — echoes the payload file byte
#: for byte (so a non-UTF-8 payload survives the shell, unlike a payload
#: passed through an environment variable), and exits with the code it
#: was told to.
_DOUBLE_SOURCE = """#!/bin/sh
printf '%s\\n' "$0" >> "$PDT_TEST_ARGV_LOG"
for arg in "$@"; do
    printf '%s\\n' "$arg" >> "$PDT_TEST_ARGV_LOG"
done
if [ -n "$PDT_TEST_PAYLOAD_FILE" ] && [ -f "$PDT_TEST_PAYLOAD_FILE" ]; then
    cat "$PDT_TEST_PAYLOAD_FILE"
fi
if [ -n "$PDT_TEST_HANG" ]; then
    sleep 30
fi
exit "${PDT_TEST_EXIT_CODE:-0}"
"""


class KeychainDouble:
    """The stand-in's control surface, as the test sees it.

    Attribute access rather than a fixture per concern, so each test can
    set only the one variable it is about and the rest stay at their
    neutral defaults.
    """

    def __init__(self, monkeypatch, home: Path, argv_log: Path) -> None:
        self._monkeypatch = monkeypatch
        self.home = home
        self.argv_log = argv_log

    @property
    def keychain_file(self) -> str:
        """The keychain path the module is expected to build.

        Spelled from this file's own literal rather than from
        ``credentials._KEYCHAIN_PATH``, so every assertion resting on
        this property is asserting what the module *did*, not what the
        module would have done.
        """
        return str(self.home / DEDICATED_KEYCHAIN_FILE)

    def argv(self):
        """Return the arguments of the invocations so far.

        Absent log means no invocation happened at all, which is the
        state the "no index, no process" test needs to be able to
        distinguish from "invoked with no arguments".
        """
        if not self.argv_log.exists():
            return []
        return [
            line for line in self.argv_log.read_text(encoding="utf-8").splitlines()
        ]

    def invocations(self) -> int:
        """Return how many arguments were recorded — a proxy for count.

        Every invocation contributes its ``$0`` plus at least the four
        fixed arguments this module always passes, so a non-zero total
        means at least one process was started and a zero total means
        none was.
        """
        return len(self.argv())

    def write_payload(self, tmp_path: Path, payload: bytes) -> None:
        """Have the stand-in emit exactly ``payload`` on stdout."""
        payload_file = tmp_path / "payload.bin"
        payload_file.write_bytes(payload)
        self._monkeypatch.setenv("PDT_TEST_PAYLOAD_FILE", str(payload_file))

    def exit_code(self, code: int) -> None:
        """Have the stand-in exit with ``code``."""
        self._monkeypatch.setenv("PDT_TEST_EXIT_CODE", str(code))

    def hang(self) -> None:
        """Have the stand-in block until it is killed."""
        self._monkeypatch.setenv("PDT_TEST_HANG", "1")

    def set_index(self, name: str, value: str) -> None:
        """Export the keychain *index* for the secret ``name``.

        Set through ``monkeypatch`` rather than assigned to
        ``os.environ`` so the change is undone at teardown. A test that
        assigns directly leaves the value in place for every test that
        runs after it in the session — including a developer's own real
        app id, which one of these tests deletes outright.
        """
        self._monkeypatch.setenv(
            credentials.SECRET_SPECS[name].account_env_key, value
        )

    def clear_index(self, name: str) -> None:
        """Unset the keychain *index* for the secret ``name``."""
        self._monkeypatch.delenv(
            credentials.SECRET_SPECS[name].account_env_key, raising=False
        )

    def set_fallback(self, name: str, value: str) -> None:
        """Export the plaintext variable the keychain is meant to replace."""
        self._monkeypatch.setenv(
            credentials.SECRET_SPECS[name].fallback_env_key, value
        )


@pytest.fixture
def keychain_double(tmp_path, monkeypatch):
    """Point the module at a real executable standing in for ``security``.

    The four things the production code reads from the environment are
    all redirected here: the platform (so the keychain is not off
    because the suite happens to run elsewhere), the switch (so the
    keychain is on), ``$HOME`` (so the keychain path the module builds
    is a path inside ``tmp_path`` rather than a real login keychain),
    and ``PDT_SECRET_READER_PATH`` (cleared, so the read goes through
    the stand-in rather than through whatever reader this deployment's
    ``.env`` names). The module's own constant for the binary is pointed
    at the stand-in, and the timeout is pulled down to half a second so
    the hanging test costs half a second rather than the production
    value.
    """
    for key in _DOUBLE_ENV + _DEPLOYMENT_ENV:
        monkeypatch.delenv(key, raising=False)

    home = tmp_path / "home"
    (home / "Library" / "Keychains").mkdir(parents=True)

    binary = tmp_path / "security"
    binary.write_text(_DOUBLE_SOURCE, encoding="utf-8")
    binary.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)
    monkeypatch.setattr(credentials, "_SECURITY_BIN", str(binary))
    # A second, not the production five: long enough that a loaded
    # machine still lets the stand-in reach its ``sleep`` before the
    # bound, and short enough that the hanging test costs a second.
    monkeypatch.setattr(credentials, "_KEYCHAIN_TIMEOUT_SECONDS", 1.0)

    argv_log = tmp_path / "argv.log"
    monkeypatch.setenv("PDT_TEST_ARGV_LOG", str(argv_log))

    credentials.reset_cache()
    yield KeychainDouble(monkeypatch, home, argv_log)
    credentials.reset_cache()


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def _flag_value(argv, flag):
    """Return the argument that follows ``flag``, or fail saying what was there.

    The stand-in logs one argument per line, so a flag's value is simply
    the next line. Returning ``None`` rather than raising here keeps the
    caller's assertion the thing that fails, and failing on *this* is
    better than failing on a membership test that passed by accident.
    """
    if flag not in argv:
        raise AssertionError(
            "{!r} is not on the recorded command line at all, so it has no "
            "value to check.\nargv: {!r}".format(flag, argv)
        )
    index = argv.index(flag)
    if index + 1 >= len(argv):
        raise AssertionError(
            "{!r} is the last argument, so nothing follows it and the read "
            "was not told which keychain to open.\nargv: {!r}".format(flag, argv)
        )
    return argv[index + 1]


def test_argv_carries_account_and_path_but_no_service(keychain_double, tmp_path):
    """Account and keychain, named exactly — and never a service.

    `security` finds an item by account, by service, or by both. Only
    the account is this project's to decide on: it is the app id the
    notifier already reads, it is not a secret, and it is the same on
    every machine. A service name would be a different thing entirely —
    a per-deployment keychain layout, asserted by source, that happens
    to hold for exactly one operator's keychain and for nobody else's.
    So the flag is pinned absent, not merely unused.

    The keychain file is passed explicitly for the same reason from the
    other direction: an unqualified lookup searches the default
    keychain, which is whatever this process happens to have unlocked,
    and that is not a decision source code should be making on the
    operator's behalf.
    """
    keychain_double.write_payload(tmp_path, b"the-secret")
    keychain_double.set_index("feishu_app_secret", "cli_sentinel_a1")

    assert credentials.read_secret_from_keychain("feishu_app_secret") == "the-secret"

    argv = keychain_double.argv()
    assert argv, "no subprocess was started, so there is no argv to check"

    # argv[0] is the binary itself; the rest is what the module asked for.
    assert argv[0].endswith("security")
    assert argv[1] == "find-generic-password"
    assert "-a" in argv
    # The keychain is pinned as the *value of -w*, not merely as a
    # member: a bare membership test is satisfied by the path appearing
    # anywhere on the line, so it would survive the flag being dropped
    # and the read falling back to whichever keychain this process
    # happened to have open — the exact failure the sentence above is
    # about, passing.
    assert _flag_value(argv, "-w") == keychain_double.keychain_file
    assert "-s" not in argv
    assert "find-internet-password" not in argv


def test_the_recorded_command_line_names_the_dedicated_keychain(
    keychain_double, tmp_path
):
    """The whole ``argv``, asserted against a literal, is the evidence.

    The assertion above says the right things about the shape of the
    command line. This one says what the command line *is*, in full,
    against a spelling written in this file — so the recorded process is
    itself the record. Nothing here is derived from
    ``credentials``: point the module at the login keychain and this is
    the line that goes red, with the offending path in the diff.
    """
    keychain_double.write_payload(tmp_path, b"the-secret")
    keychain_double.set_index("feishu_app_secret", "cli_sentinel_a1")

    assert credentials.read_secret_from_keychain("feishu_app_secret") == "the-secret"

    argv = keychain_double.argv()
    assert argv, "no subprocess was started, so there is no argv to check"

    assert argv == [
        str(credentials._SECURITY_BIN),
        "find-generic-password",
        "-a",
        "cli_sentinel_a1",
        "-w",
        str(keychain_double.home / DEDICATED_KEYCHAIN_FILE),
    ], (
        "the recorded command line does not name this project's dedicated "
        "keychain. It is the artefact a reviewer reads, so it has to be "
        "the artefact that carries the fact.\nargv: {!r}".format(argv)
    )

    # Stated over the record itself, so a reader of a failed run sees the
    # property rather than having to infer it from a diff.
    assert "login" not in argv[-1].lower(), (
        "the read was pointed at the login keychain: {!r}".format(argv[-1])
    )


def test_account_value_equals_env_index_value(keychain_double, tmp_path):
    """The account is the environment's index, byte for byte.

    The env variable holds the *name* of the keychain item, so whatever
    the operator exported is what has to reach ``-a``. Stripping,
    lowercasing, or prefixing it here would find nothing on a real
    keychain while looking perfectly correct in a test that only checks
    the flag is present.
    """
    keychain_double.write_payload(tmp_path, b"the-secret")
    account = "cli_MiXeD-Case_42.x"
    keychain_double.set_index("feishu_app_secret", account)

    assert credentials.read_secret_from_keychain("feishu_app_secret") == "the-secret"

    argv = keychain_double.argv()
    assert argv[argv.index("-a") + 1] == account


def test_missing_index_key_makes_zero_subprocess_calls(keychain_double, tmp_path):
    """No index means no process is started at all.

    The keychain item is found by account name, so with no account there
    is no lookup to perform. Starting one anyway would mean shelling out
    to a command that cannot succeed, on a path that is hit by every
    secret read — and, because the read is memoised, the count that
    matters is the count in the *log*, not the count in a return value.

    The plain variable is deliberately populated: a keychain deployment
    that also has a stale plaintext variable must not have the missing
    index quietly become a reason to use it.
    """
    keychain_double.clear_index("feishu_app_secret")
    keychain_double.set_fallback("feishu_app_secret", "the-plaintext-secret")

    assert credentials.read_secret_from_keychain("feishu_app_secret") is None
    assert keychain_double.invocations() == 0
    assert not keychain_double.argv_log.exists()


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_not_found_falls_back_without_raising(keychain_double, tmp_path):
    """A nonzero exit is "not there", reported as missing, never raised.

    `security` exits 44 when the named item is not in the keychain —
    the ordinary answer for an operator who enabled the switch and
    forgot to add the item. A caller of `read_secret` is asking whether
    a transport is configured, and it already handles the answer "no"
    (an unconfigured notifier is skipped). It does not handle a
    traceback from a lookup it had every reason to believe was total.

    So a failure resolves to the same `(missing, None)` pair a missing
    index produces, and never reaches the plaintext variable: the
    operator who asked for a keychain gets told the keychain has no such
    item, not a silent downgrade to a secret every process can read.
    """
    keychain_double.set_index("feishu_app_secret", "cli_sentinel_a1")
    keychain_double.set_fallback("feishu_app_secret", "the-plaintext-secret")
    keychain_double.exit_code(44)

    assert credentials.read_secret_from_keychain("feishu_app_secret") is None
    assert keychain_double.invocations() > 0, "the lookup never ran"


def test_non_utf8_and_trailing_newline_payloads(keychain_double, tmp_path):
    """Both payload shapes survive the read byte for byte.

    Two things the real tool does that a shell variable cannot model.

    **A trailing newline.** `security -w` prints the password and a
    newline. Taking the output as the password would append a character
    to every secret, and the failure would surface as an authentication
    error at the far end of a notification send rather than as a bug
    here. Exactly one newline is removed: a password that legitimately
    ends in something else is not trimmed by this.

    **Bytes that are not text.** A keychain item holds arbitrary bytes,
    and the platform's own tools have been known to store them.
    Decoding strictly would turn a readable secret into a failed lookup
    and, with it, a disabled transport — the worst possible trade for a
    value that was there all along. So the payload is decoded so that
    every byte has a spelling and none of them is lost, and the value
    that comes back encodes to the bytes that went in.

    Both classes are asserted by re-encoding, because comparing strings
    would pass for any value that merely *looks* right and would not
    notice a decode that quietly replaced undecodable bytes with
    U+FFFD. The expected value in each case is the password itself: for
    the second payload that is the bytes minus the one newline the tool
    added on its way out, which is exactly the character that must not
    end up in the secret.
    """
    non_utf8 = b"secret-\xff\xfe-\x80-bytes"
    trailing_newline = b"secret-with-trailing-newline\n"
    cases = [
        (non_utf8, non_utf8),
        (trailing_newline, trailing_newline[:-1]),
    ]

    for payload, expected in cases:
        credentials.reset_cache()
        keychain_double.write_payload(tmp_path, payload)
        keychain_double.set_index("feishu_app_secret", "cli_sentinel_a1")

        value = credentials.read_secret_from_keychain("feishu_app_secret")

        assert value is not None, f"{payload!r} resolved to no value at all"
        assert value.encode("utf-8", errors="surrogateescape") == expected, (
            f"{payload!r} did not survive the read"
        )


def test_timeout_falls_back_quietly(keychain_double, tmp_path):
    """A `security` that never answers resolves to missing, not a hang.

    A locked keychain is the ordinary reason `security` does not return:
    the first call can block on a GUI unlock prompt that nobody is
    looking at, and on a headless box there is no prompt to look at. A
    read with no timeout would hold the notification send for as long as
    the child lives — a hang, not an error, which is the harder of the
    two to diagnose from a log.

    The module therefore bounds the call and treats expiry as "no
    value". The child is killed and reaped by the same call that
    observed the timeout, so the stand-in does not outlive the test.
    """
    keychain_double.set_index("feishu_app_secret", "cli_sentinel_a1")
    keychain_double.hang()

    assert credentials.read_secret_from_keychain("feishu_app_secret") is None
    assert keychain_double.invocations() > 0, "the lookup never ran"


# ---------------------------------------------------------------------------
# The documented answer
# ---------------------------------------------------------------------------


def test_successful_lookup_returns_the_value(keychain_double, tmp_path):
    """A good item, read end to end through a real subprocess.

    The *label* is deliberately not asserted beside the value, because
    no single function answers both questions any more. The launcher
    reads the keychain and logs which source it used; the server
    resolves from the descriptor it was handed and reports
    ``"inherited_fd"``. A label asserted here would be a label for one
    of those processes, written as though it were the other's.
    """
    keychain_double.write_payload(tmp_path, b"the-secret\n")
    keychain_double.set_index("feishu_app_secret", "cli_sentinel_a1")

    assert credentials.read_secret_from_keychain("feishu_app_secret") == "the-secret"


def test_standin_would_fail_the_test_if_the_module_never_ran_it(
    keychain_double, tmp_path
):
    """The stand-in is a real process, not a stub that echoes anything.

    A fixture that cannot fail is a fixture that proves nothing: if the
    stand-in were replaced by something that always printed the expected
    payload and exited zero, the assertions above would keep passing
    after the command line — the part that is actually broken — changed.
    So the double is exercised directly, once, outside the module. It
    records an argument, writes a byte sequence, and returns a nonzero
    status; the first of those is the property the argv assertions rest
    on.
    """
    keychain_double.write_payload(tmp_path, b"probe")
    keychain_double.exit_code(44)
    completed = subprocess.run(
        [str(credentials._SECURITY_BIN), "find-generic-password", "-a", "probe"],
        capture_output=True,
        timeout=30,
    )

    assert completed.returncode == 44
    assert completed.stdout == b"probe"
    assert keychain_double.argv() == [
        str(credentials._SECURITY_BIN),
        "find-generic-password",
        "-a",
        "probe",
    ]
