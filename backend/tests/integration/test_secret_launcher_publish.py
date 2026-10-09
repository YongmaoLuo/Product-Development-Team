"""How many times the keychain tool is started: once per secret, per launch.

Why this suite exists
---------------------
The server does not read the keychain. :mod:`backend.secret_launcher`
does, once per secret, at startup — and hands each value to the server
over an anonymous pipe. This suite counts the thing that split exists to
bound: **the number of ``/usr/bin/security`` processes one launch
starts.**

The count is not a wall-clock assertion, and deliberately so. A timing
assertion on a publish loop would pass on a fast machine whether or not
the loop was right, fail on a loaded runner whether or not it was, and be
worth nothing as a statement about the code. A count is the same fact
with the machine taken out of it: two secrets, two processes — not four,
not one, on any hardware at all.

The counter is the stand-in's own. Every invocation writes a file named
after the account it was asked for, into a directory the test owns, and
the assertion is on the number in that file. Nothing on the path is
mocked: the child is a real executable, ``subprocess`` is the real one,
and the loop under test is the one the supervisor starts.

What is pinned
--------------
* **One secret, one process.** The ordinary case, and the reason the
  launcher is a separate process at all: on a keychain that prompts for
  access, a second lookup is a second dialog the operator has to click.
* **The loop is per name, and the names are independent.** A secret the
  keychain has no item for is skipped without disturbing the other, and a
  secret with no *index* starts no process at all — the command could not
  succeed, and a failure costs the same either way.
* **The server resolves from the descriptor, not the keychain.** After a
  launch, ``read_secret`` reports ``"inherited_fd"``, and further reads
  start no process. This is the cutover, stated as a count.
* **A process that has the descriptor *number* but not the descriptor
  gets nothing** — and does not fall back to reading the keychain. Two
  facts in one: the value travels only on the pipe, never in the
  environment; and a handoff that did not arrive is a named miss rather
  than a silent second lookup.
* **Nothing is written outside the test's own directory.** A secret
  spilled to a cache file would make the keychain's copy one of two.

Why integration, not unit
-------------------------
The count is only meaningful against a real process, and the
cross-process case is a second interpreter by definition. It is still
hermetic: the stand-in is a script in ``tmp_path``, the keychain path the
module builds resolves into a redirected ``$HOME`` inside ``tmp_path``,
and no real keychain and no real secret is touched.

A note on what is patched, and where
------------------------------------
The parent fixture points the module at the stand-in the only two ways
the design allows: the binary constant, and the platform predicate (a
suite that only runs where a keychain exists would not be a suite). The
child in the cross-process test re-establishes both itself, because a
``monkeypatch`` does not cross a process boundary. Nothing else is
touched on either side — no clock, no cache, no memo.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import config_paths
import credentials
import secret_launcher

pytestmark = pytest.mark.integration

#: ``backend/``, resolved from this file rather than written out: the
#: repository is checked out at a different path on every machine, and a
#: child process is started with the module under test on its import
#: path, so this has to be right wherever the suite runs.
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The two secrets this suite publishes, named by logical name. Two,
#: because "one process per secret" is a claim about the difference
#: between them and cannot be made with one.
FIRST = "feishu_app_secret"
SECOND = "telegram_bot_token"

#: Every environment variable the stand-in reads. Named here so a test
#: that changes one can see the whole set, and so a reader can see that
#: the stand-in's surface is closed.
_DOUBLE_ENV = (
    "PDT_TEST_COUNTER_DIR",
    "PDT_TEST_PAYLOAD_FILE",
    "PDT_TEST_EXIT_CODE",
    "PDT_TEST_ECHO_ACCOUNT",
    "PDT_TEST_SECURITY_BIN",
)

#: Variables that describe *this deployment* rather than the stand-in, and
#: that therefore have to be cleared for the suite to be hermetic.
#: ``PDT_SECRET_READER_PATH`` arrives in this process out of the
#: project-root ``.env`` before any test runs, so on a workstation that has
#: narrowed its keychain ACL with ``setup.sh adopt`` the launcher below
#: would start the operator's own signed reader instead of the counting
#: stand-in — and the counter would read zero for every secret. CI has no
#: ``.env``, which is the whole problem: green there, red on the machine
#: that actually migrated.
_DEPLOYMENT_ENV = (
    "PDT_SECRET_READER_PATH",
)

#: A real program, in ``/bin/sh`` so the suite needs no interpreter of
#: its own. It is handed the module's whole command line, of which it
#: uses one element — the account, ``$3``, the name the keychain item is
#: looked up by — and does two things with it.
#:
#: **It counts.** One file per account, in a directory the test owns,
#: holding the number of invocations for that account. Per account rather
#: than global because "each secret is read once" is a claim about two
#: independent counts, and a single global counter cannot make it. The
#: count is incremented and rewritten rather than appended to, so the
#: file's *content* is the number — which is what a failure message would
#: print, and what an assertion reads.
#:
#: **It emits a payload**, byte for byte from a file rather than through
#: an environment variable, so a payload that is not valid UTF-8 survives
#: the shell. With ``PDT_TEST_ECHO_ACCOUNT`` set it emits the account it
#: was asked for instead — one value per secret, which is what lets a
#: test tell the two secrets' descriptors apart.
#:
#: The account is sanitised into a filename. It is a value this suite
#: mints, and the sanitising is here so the stand-in is not a
#: shell-quoting hazard for whatever it is next handed.
_DOUBLE_SOURCE = """#!/bin/sh
account=$3
key=$(printf '%s' "$account" | tr -c 'A-Za-z0-9._-' '_')
count=0
if [ -f "$PDT_TEST_COUNTER_DIR/$key" ]; then
    count=$(cat "$PDT_TEST_COUNTER_DIR/$key")
fi
printf '%s' "$((count + 1))" > "$PDT_TEST_COUNTER_DIR/$key"
if [ -n "$PDT_TEST_ECHO_ACCOUNT" ]; then
    printf '%s' "$account"
elif [ -n "$PDT_TEST_PAYLOAD_FILE" ] && [ -f "$PDT_TEST_PAYLOAD_FILE" ]; then
    cat "$PDT_TEST_PAYLOAD_FILE"
fi
exit "${PDT_TEST_EXIT_CODE:-0}"
"""

#: How long a child may take. Generous, because it covers a loaded CI
#: runner rather than a child that refuses to finish — and bounded,
#: because a child that never exits must fail the test that started it
#: rather than spend the shard's whole budget.
_CHILD_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# The child
# ---------------------------------------------------------------------------
#
# It re-runs the server's side of the handoff, in a process of its own,
# and prints three things about it rather than the value: whether the
# descriptor variable is in its environment, which source the provider
# reports, and whether a value came back at all. Printing the value would
# put a secret on a pipe into the parent's captured stdout for the rest
# of the session to hold; these three carry the same information and
# nothing else.

_CHILD_SRC = """
import os
import sys

import credentials

# The two seams the parent fixture set, re-established here: a
# monkeypatch does not cross a process boundary, and the module resolves
# both on every call.
credentials._is_macos = lambda: True
credentials._SECURITY_BIN = os.environ["PDT_TEST_SECURITY_BIN"]

name = os.environ["PDT_TEST_SECRET_NAME"]
variable = credentials.secret_fd_env_var(name)
value = credentials.read_secret(name)

sys.stdout.write("{}|{}|{}".format(
    "1" if variable in os.environ else "0",
    credentials.secret_source(name),
    "VALUE" if value is not None else "",
))
"""


def _snapshot_tree(root: Path, exclude: set, *, recursive: bool = True) -> set:
    """Return the paths under ``root``, as strings, minus ``exclude``.

    ``exclude`` is matched against a path's *parts* rather than its
    string, so excluding ``{".venv"}`` drops a directory four levels
    down as readily as one at the top.

    ``recursive`` is off for the directories that are shared with every
    other process on the machine. Those are watched at their own top
    level, where a spill lands: a cache file is written into the
    directory it names, not into a subdirectory of it. Walking them all
    the way down would be slow, and — worse for a test — it would
    attribute another process's writes to this one.
    """
    found = set()
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in exclude]
        for entry in filenames:
            path = here / entry
            if any(part in exclude for part in path.parts):
                continue
            found.add(str(path))
        if not recursive:
            break
    return found


#: Directories that are not the module's to write and are not the
#: module's to be judged on: the virtualenv, the checkout metadata, and
#: the byte-compiled modules a *child interpreter* creates when it
#: imports ``credentials`` — which this suite deliberately does. Counting
#: that as a write by the module under test would be wrong, and ignoring
#: it silently would be worse, so it is named.
_NOT_THE_MODULES = {".venv", ".git", "__pycache__", "node_modules", ".pytest_cache"}

#: The real ``$HOME``, captured at import — before any fixture can
#: redirect it. The ``keychain_double`` fixture points ``$HOME`` inside
#: ``tmp_path`` precisely so the module never touches the real one, which
#: makes the real one the directory worth watching: a watch on the
#: redirected home would be watching the test's own directory.
_REAL_HOME = Path.home()

#: The name of the file the working-tree watch is proved with. Dot-
#: prefixed so it is obvious in a directory listing if a test dies
#: between creating it and removing it.
_PROBE_NAME = ".pdt-launcher-publish-probe"


class KeychainDouble:
    """The stand-in's control surface, as the test sees it.

    Attribute access rather than a fixture per concern, so a test can set
    only the one variable it is about and the rest stay neutral.
    """

    def __init__(self, monkeypatch, home: Path, counter_dir: Path) -> None:
        self._monkeypatch = monkeypatch
        self.home = home
        self.counter_dir = counter_dir

    @property
    def binary(self) -> str:
        """The executable the stand-in actually is."""
        return str(credentials._SECURITY_BIN)

    def count(self, account: str) -> int:
        """Return how many processes were started for ``account``.

        An account with no file has no invocations: the count is zero
        because the stand-in was never asked, which is a different fact
        from "asked and the file was lost" and is asserted as a zero
        rather than as a missing file wherever the count matters.
        """
        path = self.counter_dir / self._key(account)
        if not path.exists():
            return 0
        return int(path.read_text(encoding="utf-8"))

    def raw_count(self, account: str) -> str | None:
        """Return the counter file's *content*, verbatim.

        The file holds a bare number, so this is what an assertion
        about "the counter file's content is 1" reads, and what a
        failure message prints. ``None`` when no process ran at all,
        which is deliberately distinguishable from ``"0"``.
        """
        path = self.counter_dir / self._key(account)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _key(account: str) -> str:
        """Return the counter file's name for ``account``.

        The same sanitising the stand-in applies, kept in step with it by
        being the same rule: a test that looked for a different filename
        would read a count of zero for an invocation that happened.
        """
        return "".join(c if (c.isalnum() or c in "._-") else "_" for c in account)

    def write_payload(self, tmp_path: Path, payload: bytes) -> None:
        """Have the stand-in emit exactly ``payload`` on stdout."""
        payload_file = tmp_path / "payload.bin"
        payload_file.write_bytes(payload)
        self._monkeypatch.setenv("PDT_TEST_PAYLOAD_FILE", str(payload_file))

    def payload_file(self, tmp_path: Path) -> Path:
        """Return the file the stand-in was told to echo."""
        return tmp_path / "payload.bin"

    def exit_code(self, code: int) -> None:
        """Have the stand-in exit with ``code`` — 44 is "item not found"."""
        self._monkeypatch.setenv("PDT_TEST_EXIT_CODE", str(code))

    def echo_account(self) -> None:
        """Have the stand-in return the account it was asked for.

        One value per secret, so a test can tell two secrets' answers
        apart. Both stand-ins echoing the same bytes would make "the
        loop crossed the two secrets over" indistinguishable from "it
        published the right one".
        """
        self._monkeypatch.setenv("PDT_TEST_ECHO_ACCOUNT", "1")

    def set_index(self, name: str, value: str) -> None:
        """Export the keychain *index* for the secret ``name``.

        Through ``monkeypatch`` rather than assigned to ``os.environ``,
        so the change is undone at teardown. A test that assigns
        directly leaves the value in place for every test that runs after
        it in the session — including a developer's own app id, which one
        of these would then have looked up in a stand-in keychain.
        """
        self._monkeypatch.setenv(
            credentials.SECRET_SPECS[name].account_env_key, value
        )

    def clear_index(self, name: str) -> None:
        """Export no index for the secret ``name``.

        Through ``monkeypatch`` for the same reason ``set_index`` is: the
        assertion this supports is "no lookup ran", and a developer whose
        shell happens to carry ``TELEGRAM_CHAT_ID`` would otherwise get a
        lookup that makes it fail on their machine and pass on CI.
        """
        self._monkeypatch.delenv(credentials.SECRET_SPECS[name].account_env_key, raising=False)

    def account(self, name: str) -> str:
        """Return the index currently exported for the secret ``name``."""
        return os.environ[credentials.SECRET_SPECS[name].account_env_key]


@pytest.fixture
def keychain_double(tmp_path, monkeypatch):
    """Point the module at a real executable standing in for ``security``.

    The five things the production code reads are redirected here: the
    platform (so the keychain is not off because the suite happens to run
    elsewhere), the switch (so the keychain is on), ``$HOME`` (so the
    keychain path the module builds is inside ``tmp_path`` rather than
    resolving into this machine's own keychains), the binary constant
    (so the tool it starts is the counter in this file rather than the
    platform's), and ``PDT_SECRET_READER_PATH`` (cleared, so the read
    goes through that counter rather than through whatever reader this
    deployment's ``.env`` names).

    Every file the stand-in creates is under ``tmp_path``: the count
    files, the payload, and the executable itself. A file outside it
    would be a finding, and one of the tests here is watching for that.
    """
    for key in _DOUBLE_ENV + _DEPLOYMENT_ENV:
        monkeypatch.delenv(key, raising=False)

    home = tmp_path / "home"
    (home / "Library" / "Keychains").mkdir(parents=True)

    counter_dir = tmp_path / "counts"
    counter_dir.mkdir()
    monkeypatch.setenv("PDT_TEST_COUNTER_DIR", str(counter_dir))

    binary = tmp_path / "security"
    binary.write_text(_DOUBLE_SOURCE, encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PDT_TEST_SECURITY_BIN", str(binary))

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)
    monkeypatch.setattr(credentials, "_SECURITY_BIN", str(binary))

    credentials.reset_cache()
    yield KeychainDouble(monkeypatch, home, counter_dir)
    credentials.reset_cache()


@pytest.fixture
def launcher_publishes(register_open_fd):
    """Run the launcher's publish loop, and hand back what it started.

    Two things a launch leaves behind that a test has to give back, and
    neither is given back by the code under test — deliberately, because
    in production the process ``exec``\\s away immediately and both are
    meant to survive:

    * a **descriptor** per published secret, which the teardown closes
      once the loop has named it;
    * an entry in ``os.environ`` naming that descriptor. ``publish_secrets``
      writes it with a plain assignment rather than through
      ``monkeypatch``, so the fixture removes it here. Removing it
      unconditionally is right: these variables belong to a launcher
      process, and a test process holding one is holding a number that
      names a closed pipe.
    """
    def _publish() -> int:
        count = secret_launcher.publish_secrets()
        for name in credentials.SECRET_SPECS:
            raw = os.environ.get(credentials.secret_fd_env_var(name))
            if raw is not None and raw.isdigit():
                register_open_fd(int(raw))
        return count

    yield _publish

    for name in credentials.SECRET_SPECS:
        os.environ.pop(credentials.secret_fd_env_var(name), None)
    credentials.reset_cache()


@pytest.fixture
def run_child(register_child_process):
    """Return a function that starts one real interpreter and joins it.

    ``subprocess.run`` would join too, but it keeps the process handle to
    itself, and the suite's rule is that a test hands back what it starts.
    Every child is therefore registered for the teardown that reclaims a
    test's workers, and this function additionally waits for it and
    asserts the wait happened — so a child that ignored its own timeout
    is reported as the failure it is, and the registration is the
    backstop for a test that fails before reaching that assertion.
    """

    def _run(source: str, *, env: dict) -> subprocess.CompletedProcess:
        proc = subprocess.Popen(
            [sys.executable, "-c", source],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(_BACKEND_DIR),
        )
        register_child_process(proc)
        try:
            stdout, stderr = proc.communicate(timeout=_CHILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail(
                "a child was still running after {}s and had to be killed; "
                "it never reached its own exit code".format(
                    _CHILD_TIMEOUT_SECONDS
                )
            )
        assert proc.poll() is not None, "the child was handed back before it exited"
        return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)

    return _run


def _child_env(keychain_double, tmp_path, logical_name: str, account: str) -> dict:
    """Return the environment a child needs to re-run the server's half.

    Built from nothing rather than copied from ``os.environ``, with one
    deliberate exception: the descriptor variable, which is read out of
    the parent's environment because **that is the handoff being tested**.
    A child that was not given the variable would prove nothing about a
    child that was.

    The rest is the minimum a child needs to be a fair test of the
    fallback instead: the import path, the redirected home, the switch
    left **on** (so a keychain read is available to it), the index it
    would look up, and the stand-in's own three names — so that a child
    which *did* fall back to the keychain would be counted rather than
    silently failing to run. Copying the whole environment would carry
    whatever the machine running the suite has exported — including, on a
    workstation that really notifies, the provider secret itself.
    """
    spec = credentials.SECRET_SPECS[logical_name]
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_BACKEND_DIR),
        "HOME": str(keychain_double.home),
        credentials._SWITCH_ENV_KEY: "0",
        spec.account_env_key: account,
        credentials.secret_fd_env_var(logical_name): os.environ.get(
            credentials.secret_fd_env_var(logical_name), ""
        ),
        "PDT_TEST_COUNTER_DIR": str(keychain_double.counter_dir),
        "PDT_TEST_SECURITY_BIN": keychain_double.binary,
        "PDT_TEST_SECRET_NAME": logical_name,
        "PDT_TEST_PAYLOAD_FILE": str(keychain_double.payload_file(tmp_path)),
    }


# ---------------------------------------------------------------------------
# The count
# ---------------------------------------------------------------------------


def test_the_launcher_reads_each_secret_exactly_once(
    keychain_double, tmp_path, canary, launcher_publishes
):
    """One secret, one process — and a counter that really counts.

    The invariant, and the reason the launcher is a separate process.
    Every start reads every configured secret, so a second lookup per
    secret is a second subprocess inside startup, and on a keychain that
    prompts for access a second dialog the operator has to click through
    before the service comes up.

    Both secrets come back with a count of one, and the counts are
    separate files: a loop that published the same descriptor twice, or
    asked one account twice and never asked the other, would show it
    here. The two accounts are different values, so a stand-in that was
    handed one of them for both lookups could not pass either.
    """
    first_account = canary("feishu_index", tmp_path)
    second_account = canary("telegram_index", tmp_path)
    keychain_double.echo_account()
    keychain_double.set_index(FIRST, first_account)
    keychain_double.set_index(SECOND, second_account)

    published = launcher_publishes()

    assert published == 2, "the loop did not publish both secrets"
    assert keychain_double.count(first_account) == 1, (
        "the first secret was read {} times; a launch may read it once".format(
            keychain_double.count(first_account)
        )
    )
    assert keychain_double.count(second_account) == 1, (
        "the second secret was read {} times; a launch may read it once".format(
            keychain_double.count(second_account)
        )
    )
    assert keychain_double.raw_count(first_account) == "1", (
        "the counter file does not hold the number 1; it holds "
        "{!r}".format(keychain_double.raw_count(first_account))
    )


def test_a_secret_the_keychain_has_no_item_for_is_skipped(
    keychain_double, tmp_path, canary, launcher_publishes
):
    """A miss costs one lookup, is not published, and does not stop the rest.

    The ordinary state of a deployment that turned the keychain on and
    never added one of the two items. Two things are asserted, and the
    second is the one that is easy to lose: the *other* secret is still
    published. A loop that raised on the first miss, or that published an
    empty pipe in place of a value, would leave the server with a
    descriptor that decodes to nothing — and a notifier that reports
    itself configured and fails at the far end of a send.

    The count of one is asserted too, rather than only the absence of a
    descriptor: "not published" and "never looked up" are different
    facts, and only the second would mean the operator is told nothing
    about the item they forgot to add.
    """
    present_account = canary("feishu_index", tmp_path)
    missing_account = canary("telegram_index", tmp_path)
    keychain_double.echo_account()
    keychain_double.set_index(FIRST, present_account)
    keychain_double.set_index(SECOND, missing_account)
    keychain_double.exit_code(44)  # every lookup: "item not found"

    published = launcher_publishes()

    assert published == 0, "a miss was published as though it were a value"
    assert keychain_double.count(present_account) == 1
    assert keychain_double.count(missing_account) == 1, (
        "the miss was never looked up, so the operator is told nothing "
        "about the item that is missing"
    )
    assert credentials.secret_fd_env_var(FIRST) not in os.environ
    assert credentials.secret_fd_env_var(SECOND) not in os.environ


def test_a_secret_with_no_index_starts_no_process(
    keychain_double, tmp_path, canary, launcher_publishes
):
    """No account means no lookup, not a lookup that fails.

    The index is what the keychain item is *named* by, so a secret
    without one has nothing to ask for — the command cannot succeed, and
    a failure is what the provider reports either way. Starting a process
    to discover that would be a subprocess per unconfigured secret on
    every start, for an answer already known.

    The other secret is configured, so the zero below is a fact about
    this one name and not about a loop that never ran.
    """
    account = canary("feishu_index", tmp_path)
    keychain_double.echo_account()
    keychain_double.set_index(FIRST, account)
    keychain_double.clear_index(SECOND)

    published = launcher_publishes()

    assert published == 1, "only the configured secret should be published"
    assert keychain_double.count(account) == 1
    assert len(list(keychain_double.counter_dir.iterdir())) == 1, (
        "a process was started for the secret that has no index, so the "
        "zero above is a count rather than an absence"
    )


def test_each_secret_is_published_on_its_own_descriptor(
    keychain_double, tmp_path, canary, launcher_publishes
):
    """Two secrets, two pipes, two values — and neither carries the other.

    The failure this rules out is a cross-delivery: one transport
    authenticating with another transport's credential. It would not look
    like a handoff bug from the outside — it would look like a provider
    rejecting a valid token, or, worse, a message arriving in the wrong
    chat.

    The counts alone cannot rule it out, which is why the stand-in is
    told to echo the account it was asked for. A loop that published the
    first secret's descriptor twice would start **two** processes, one
    for each account, and every count in this file would read one. What
    separates the two designs is the value on each descriptor.
    """
    first_account = canary("feishu_index", tmp_path)
    second_account = canary("telegram_index", tmp_path)
    keychain_double.echo_account()
    keychain_double.set_index(FIRST, first_account)
    keychain_double.set_index(SECOND, second_account)

    assert launcher_publishes() == 2

    first_fd = int(os.environ[credentials.secret_fd_env_var(FIRST)])
    second_fd = int(os.environ[credentials.secret_fd_env_var(SECOND)])
    assert first_fd != second_fd, "both secrets were published on one pipe"

    # Read through the provider's own resolver rather than off the pipe,
    # so this also pins that the server finds the descriptors by logical
    # name — a payload keyed by the environment variable would leave the
    # reader here with nothing.
    assert credentials.read_secret(FIRST) == first_account
    assert credentials.read_secret(SECOND) == second_account


def test_the_server_resolves_from_the_descriptor(
    keychain_double, tmp_path, canary, launcher_publishes
):
    """After a launch, the server reads pipes — and starts nothing.

    This is the cutover, stated as a count. Five reads, after the one
    lookup the launch performed, leave the counter at one: the server is
    not consulting the keychain at all, and the repeated reads are served
    the value the descriptor carried.

    The source label is asserted rather than only the value, because the
    value alone would also be returned by a server that had gone back to
    the keychain — and going back is the regression this whole change
    exists to prevent. ``"inherited_fd"`` is the one-word evidence that
    the handoff happened, and it is deliberately distinct from
    ``"keychain"``: a server that reported ``"keychain"`` would be
    naming a lookup it cannot perform.
    """
    account = canary("feishu_index", tmp_path)
    keychain_double.write_payload(tmp_path, b"the-secret\n")
    keychain_double.set_index(FIRST, account)

    assert launcher_publishes() >= 1
    assert keychain_double.count(account) == 1

    values = [credentials.read_secret(FIRST) for _ in range(5)]

    assert values == ["the-secret"] * 5, "the memo served a different value each read"
    assert credentials.secret_source(FIRST) == credentials.SOURCE_INHERITED_FD, (
        "the server reports the keychain as the source, which means it "
        "went back to reading it"
    )
    assert keychain_double.count(account) == 1, (
        "the server started {} extra keychain process(es); the descriptor "
        "was supposed to be the only read".format(
            keychain_double.count(account) - 1
        )
    )


# ---------------------------------------------------------------------------
# Across a process boundary
# ---------------------------------------------------------------------------


def test_a_child_with_the_variable_but_not_the_descriptor_gets_nothing(
    keychain_double, tmp_path, canary, launcher_publishes, run_child
):
    """The number in the environment is not the secret, and is not a way in.

    Two claims, and they are the same claim from opposite ends.

    **The value is not in the environment.** A child that copies the
    parent's environment sees the descriptor *number* and nothing else —
    it reads nothing, from either source — so a secret scanner pointed at
    a process listing finds a small integer where the secret used to be.

    **A handoff that did not arrive is a miss, not a fallback.** The child
    has the keychain *available* — the switch is on, the index is set, the
    stand-in is on its path — and still reports ``"missing"`` rather than
    running a lookup of its own. That is the no-silent-downgrade rule
    seen from the far side: a server whose launcher failed to publish is
    told the credential is missing, loudly, instead of quietly
    re-deriving it in a process that was designed not to.

    Why the child does not inherit the descriptor: ``subprocess`` passes
    ``close_fds=True``, which closes every descriptor above 2 that is not
    named in ``pass_fds``. The inheritable flag :func:`publish_secret_fd`
    sets is for the *other* handoff — ``os.execv``, which honours it —
    and this test is what keeps the two from being confused. A child that
    is meant to receive a descriptor has to be told so explicitly, which
    is the whole difference between an inherited descriptor and a
    leaked one.

    Asserted on the child's own report, so a failure says which of the
    three facts was wrong rather than "the child returned non-zero".
    """
    account = canary("feishu_index", tmp_path)
    keychain_double.write_payload(tmp_path, b"the-secret\n")
    keychain_double.set_index(FIRST, account)

    assert launcher_publishes() >= 1
    assert keychain_double.count(account) == 1

    child = run_child(
        _CHILD_SRC, env=_child_env(keychain_double, tmp_path, FIRST, account)
    )

    report = child.stdout.decode("utf-8", "replace")
    assert report == "1|missing|", (
        "the child reported {!r}; expected '1|missing|' — the variable is "
        "present, no value is readable, and no source claims to have one. "
        "stderr:\n{}".format(
            report, child.stderr.decode("utf-8", "replace")
        )
    )
    assert keychain_double.count(account) == 1, (
        "the child started a keychain lookup of its own; a descriptor "
        "that did not arrive must be a miss, not a fallback"
    )


# ---------------------------------------------------------------------------
# The switch the launcher reasons about is the server's switch
# ---------------------------------------------------------------------------


def test_the_launcher_loads_the_deployment_env_before_deciding(
    keychain_double, tmp_path, canary, monkeypatch, register_open_fd
):
    """The launcher and the server must reach the same verdict on the switch.

    ``PDT_DISABLE_KEYCHAIN_SECRETS`` is fail-closed, and the value that
    turns the keychain *on* normally lives in the project-root ``.env``
    rather than in the environment the supervisor spawns this process
    with. ``backend.server`` loads that file at its own startup; if the
    launcher does not, the two reach opposite conclusions from one
    deployment:

    * the launcher sees an unset switch, calls the keychain off, and
      publishes nothing;
    * the server loads ``.env``, sees the switch on, and — correctly —
      refuses the plaintext fallback, because a deployment that asked for
      a keychain must not be quietly downgraded.

    The notifier then reports itself unconfigured on a machine whose
    keychain holds the credential. That is not a hypothetical; it is what
    shipped on 2026-10-08, and it is invisible from either process alone,
    which is why the assertion is about the pair.

    The deployment is built the way the real one is: the switch is
    **absent from the environment** and present **only in the file**. A
    test that exported it would pass with no load at all.
    """
    account = canary("feishu_index", tmp_path)
    keychain_double.write_payload(tmp_path, b"the-secret\n")
    keychain_double.set_index(FIRST, account)
    keychain_double.clear_index(SECOND)

    # The shape of the real deployment: not in the environment, only in
    # the file. Set after the fixture so it is the file that supplies it.
    monkeypatch.delenv(credentials._SWITCH_ENV_KEY, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "{}={}\n".format(credentials._SWITCH_ENV_KEY, "0"), encoding="utf-8"
    )
    monkeypatch.setattr(config_paths, "ENV_FILE", env_file, raising=False)

    # ``execv`` only returns on failure, and running the server here would
    # bind a port. The launcher's work is all done by this point.
    def _stop(*args, **kwargs):
        raise SystemExit(0)

    monkeypatch.setattr(secret_launcher.os, "execv", _stop)

    assert credentials.keychain_disabled() is True, (
        "the switch is set in this process's environment, so nothing below "
        "can tell whether the file was read"
    )

    with pytest.raises(SystemExit):
        secret_launcher.main()

    assert credentials.keychain_disabled() is False, (
        "the launcher decided the keychain was off while the server it "
        "exec'd will decide it is on — the deployment's .env was read after "
        "the switch, or not at all"
    )
    variable = credentials.secret_fd_env_var(FIRST)
    assert variable in os.environ, (
        "the launcher read the deployment's .env and still published "
        "nothing, so the notifier this server starts will report itself "
        "unconfigured"
    )
    register_open_fd(int(os.environ[variable]))


def test_a_real_environment_variable_still_wins_over_the_file(
    keychain_double, tmp_path, monkeypatch
):
    """The file fills in what is missing; it does not overrule the operator.

    ``override=False`` is the server's own spelling and the reason is
    theirs: a CI job or a container sets this variable in the process
    environment, and a checked-out ``.env`` lying around must not be able
    to switch a deployment back to the keychain behind its back. Pinned
    here because the two processes have to agree about the *value* as
    well as about when it is read — a launcher that loaded with
    ``override=True`` would disagree with the server exactly as badly as
    one that never loaded the file.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "{}={}\n".format(credentials._SWITCH_ENV_KEY, "0"), encoding="utf-8"
    )
    monkeypatch.setattr(config_paths, "ENV_FILE", env_file, raising=False)
    monkeypatch.setenv(credentials._SWITCH_ENV_KEY, "1")

    secret_launcher._load_deployment_env()

    assert os.environ[credentials._SWITCH_ENV_KEY] == "1", (
        "the file overruled an explicit environment variable, so a "
        "deployment that switched the keychain off in its job definition "
        "would be switched back on by whatever .env is checked out"
    )


# ---------------------------------------------------------------------------
# The read-only promise
# ---------------------------------------------------------------------------


def test_the_launcher_creates_no_files_outside_tmp_path(
    keychain_double, tmp_path, canary, launcher_publishes
):
    """Every file this path creates is inside the test's own directory.

    A pipe is a good place for a secret precisely because it is not a
    place. Anything that spilled one — a payload file under a cache
    directory, a lock beside the source, a scratch file in ``/tmp`` —
    would make the pipe the second copy of the secret the keychain exists
    to be the only copy of, and it would be a copy with a readable
    lifetime nobody chose.

    Three roots are watched, because three are where a spill goes: the
    working tree, the keychain directory the module names, and the
    system temporary directory. Byte-compiled modules are excluded from
    the first, and the exclusion is named in ``_NOT_THE_MODULES``
    because this suite starts a child that imports the module under test
    and the interpreter — not the module — writes those.

    Two controls keep the assertion from being vacuous. The counter file
    is asserted to exist and to hold one, so the watch is over a launch
    that demonstrably read something. And a probe file is created in the
    working tree and removed again, so the walker is shown to notice a
    write in the very place it is claiming to watch.
    """
    account = canary("feishu_index", tmp_path)
    keychain_double.write_payload(tmp_path, b"the-secret\n")
    keychain_double.set_index(FIRST, account)

    # The places a spill actually goes, and the two that are not walked
    # all the way down because they belong to every other process on the
    # machine as well as to this one.
    roots = [
        (_BACKEND_DIR, True),
        (_REAL_HOME / "Library" / "Keychains", True),
        (_REAL_HOME / "Library" / "Caches", False),
        (_REAL_HOME / ".cache", False),
        (Path(tempfile.gettempdir()), False),
    ]

    def snapshot() -> set:
        found = set()
        for root, recursive in roots:
            found |= _snapshot_tree(root, _NOT_THE_MODULES, recursive=recursive)
        # The test's own directory is expected to grow — and on macOS it
        # sits *inside* the system temporary directory, so without this
        # the temp watch would report the test's own stand-in as a leak.
        return {p for p in found if tmp_path not in Path(p).parents}

    before = snapshot()

    launcher_publishes()

    after = snapshot()

    # Control one: the launch read something, so "nothing new" is a fact
    # about this module and not about a stand-in that never ran.
    assert keychain_double.raw_count(account) == "1"
    assert str(keychain_double.counter_dir).startswith(str(tmp_path)), (
        "the stand-in wrote its counter outside the test's directory, so "
        "the watch below was never watching it"
    )

    # Control two: the walker sees a write in the root it claims to watch.
    probe = _BACKEND_DIR / _PROBE_NAME
    try:
        probe.write_text("probe", encoding="utf-8")
        assert snapshot() - before == {str(probe)}, (
            "the working-tree watch did not notice a file being created "
            "in it, so it cannot be trusted to notice one"
        )
    finally:
        probe.unlink(missing_ok=True)

    assert after - before == set(), (
        "the launch created files outside the test's directory:\n{}".format(
            "\n".join(sorted(after - before))
        )
    )
