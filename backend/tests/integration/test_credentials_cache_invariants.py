"""How many times one secret is read: at most once per process, per secret.

Why this suite exists
---------------------
``credentials.py`` memoises a resolved secret so that a deployment
pushes, forgets about it, and pushes again. The memo is a performance
decision, and a performance decision that is never measured is a decision
nobody can tell from the outside. So this suite counts the thing the memo
exists to bound: **the number of times the keychain tool is started.**

The count is not a wall-clock assertion, and deliberately so. A timing
assertion on a five-call loop would pass on a fast machine whether or not
the memo exists, fail on a loaded runner whether or not it does, and be
worth nothing as a statement about the code. A count is the same fact
with the machine taken out of it: five reads that start one process, and
not five, on any hardware at all.

The counter is the stand-in's own. Every invocation writes a file named
after the account it was asked for, into a directory the test owns, and
the assertion is on the number in that file. Nothing on the path is
mocked: the child is a real executable, ``subprocess`` is the real one,
and the memo under test is the one the notifiers go through.

What is pinned
--------------
* **Five reads, one process.** The ordinary case.
* **A failed lookup is cached too.** This is the half that is easy to get
  wrong, and the half with a cost: an item that is not in the keychain
  makes the tool exit nonzero, and an uncached failure is re-run on
  *every* push. A deployment whose secret was never configured would then
  pay a keychain lookup per notification forever, and — on a keychain
  that prompts — would be prompted per notification forever.
* **The memo is per logical name.** Reading one secret must not fill in
  another's slot; a cache keyed by something other than the name would
  hand one provider another provider's answer, which is worse than
  re-running a lookup.
* **The memo is per process.** A child that reads the same logical name
  starts its own lookup. Shared state across processes would need a file
  or a socket, and a secret written to either is a secret on disk.
* **Nothing is written outside the test's own directory.** The memo is
  in memory; a secret spilled to a cache file would make a "safe to
  cache" claim into a new place the secret lives.

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

import credentials

pytestmark = pytest.mark.integration

#: ``backend/``, resolved from this file rather than written out: the
#: repository is checked out at a different path on every machine, and a
#: child process is started with the module under test on its import
#: path, so this has to be right wherever the suite runs.
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The two secrets this suite reads, named by logical name. Two, because
#: "the memo is per name" is a claim about the difference between them
#: and cannot be made with one.
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

#: A real program, in ``/bin/sh`` so the suite needs no interpreter of
#: its own. It is handed the module's whole command line, of which it
#: uses one element — the account, ``$3``, the name the keychain item is
#: looked up by — and does two things with it.
#:
#: **It counts.** One file per account, in a directory the test owns,
#: holding the number of invocations for that account. Per account rather
#: than global because "the memo is per logical name" is exactly the
#: claim that two secrets have independent counts, and a single global
#: counter cannot say it. The count is incremented and rewritten rather
#: than appended to, so the file's *content* is the number — which is
#: what a failure message would print, and what an assertion reads.
#:
#: **It emits a payload**, byte for byte from a file rather than through
#: an environment variable, so a payload that is not valid UTF-8 survives
#: the shell. With ``PDT_TEST_ECHO_ACCOUNT`` set it emits the account it
#: was asked for instead — one value per secret, which is what lets a
#: test tell two secrets' answers apart. See
#: ``test_cache_is_per_logical_name`` for why the counts alone cannot.
#:
#: The account is sanitised into a filename. It is a value this suite
#: mints, and the sanitising is here so the stand-in is not a shell-quoting
#: hazard for whatever it is next handed.
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

#: What the child exits with, so a failure names which of its checks
#: failed rather than just "the child disagreed".
EXIT_CHILD_OK = 0
#: A read returned something other than the payload.
EXIT_CHILD_WRONG_VALUE = 4
#: The five reads did not all return the same thing — the memo is not
#: even self-consistent, which is a different bug from a wrong answer.
EXIT_CHILD_UNSTABLE = 5


# ---------------------------------------------------------------------------
# The child
# ---------------------------------------------------------------------------
#
# It does exactly what the parent did, in a process of its own, and
# speaks in exit codes. A child that printed the value would put a secret
# on a pipe into the parent's captured stdout for the rest of the session
# to hold; the codes carry the same information and nothing else.
#
# The child is told which binary to run and which secret to read, and is
# told nothing about the answer — the value it must get back is handed
# over as the *payload file* the stand-in itself reads, so a harness
# that supplied the answer could not report the answer being wrong.


_CHILD_SRC = """
import os
import sys

logical = os.environ["PDT_TEST_SECRET_NAME"]
payload_file = os.environ["PDT_TEST_PAYLOAD_FILE"]

import credentials

# The two seams the parent fixture sets, re-established here: a
# monkeypatch does not cross a process boundary, and the module resolves
# its source from module-level state on every call.
credentials._is_macos = lambda: True
credentials._SECURITY_BIN = os.environ["PDT_TEST_SECURITY_BIN"]

with open(payload_file, "rb") as handle:
    raw = handle.read()
# The payload file is the bytes the tool prints, and the tool prints one
# newline after the password. Stripping that one newline here is the
# module's documented rule, restated because the child cannot import the
# private helper it implements — and it has to be restated, because a
# child that expected the *untrimmed* bytes would be asserting a bug
# rather than a value.
if raw.endswith(b"\\n"):
    raw = raw[:-1]
want = raw.decode("utf-8", "surrogateescape")

values = [credentials.read_secret(logical) for _ in range(5)]

if len(set(values)) != 1:
    # The memo answered differently to the same question twice in one
    # process. Reported separately from a wrong answer so a failure says
    # which of the two it is.
    sys.exit(5)
if values[0] != want:
    sys.exit(4)
sys.exit(0)
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
_PROBE_NAME = ".pdt-cache-invariance-probe"


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

    def count_all(self) -> int:
        """Return how many processes were started for any account."""
        if not self.counter_dir.is_dir():
            return 0
        return sum(
            int(entry.read_text(encoding="utf-8"))
            for entry in self.counter_dir.iterdir()
            if entry.is_file()
        )

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
        apart. Both stand-ins echoing the same bytes would make "the memo
        handed back the wrong secret" indistinguishable from "the memo
        handed back the right one".
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

    def account(self, name: str) -> str:
        """Return the index currently exported for the secret ``name``."""
        return os.environ[credentials.SECRET_SPECS[name].account_env_key]


@pytest.fixture
def keychain_double(tmp_path, monkeypatch):
    """Point the module at a real executable standing in for ``security``.

    The four things the production code reads are redirected here: the
    platform (so the keychain is not off because the suite happens to run
    elsewhere), the switch (so the keychain is on), ``$HOME`` (so the
    keychain path the module builds is inside ``tmp_path`` rather than
    resolving into this machine's own keychains), and the binary constant
    (so the tool it starts is the counter in this file rather than the
    platform's).

    Every file the stand-in creates is under ``tmp_path``: the count
    files, the payload, and the executable itself. A file outside it
    would be a finding, and one of the tests here is watching for that.
    """
    for key in _DOUBLE_ENV:
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
    """Return the environment a child needs to do what the parent did.

    Built from nothing rather than copied from ``os.environ``, and that is
    the point of it: a copy would carry whatever the machine running the
    suite has exported — including, on a workstation that really notifies,
    the provider secret itself — and a test counting keychain lookups
    would then be counting the developer's shell. Five entries are all a
    child needs: the import path, the redirected home, the switch, the
    index it will look up, and the stand-in's own three names.
    """
    spec = credentials.SECRET_SPECS[logical_name]
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_BACKEND_DIR),
        "HOME": str(keychain_double.home),
        credentials._SWITCH_ENV_KEY: "0",
        spec.account_env_key: account,
        "PDT_TEST_COUNTER_DIR": str(keychain_double.counter_dir),
        "PDT_TEST_SECURITY_BIN": keychain_double.binary,
        "PDT_TEST_SECRET_NAME": logical_name,
        "PDT_TEST_PAYLOAD_FILE": str(keychain_double.payload_file(tmp_path)),
    }


# ---------------------------------------------------------------------------
# The count
# ---------------------------------------------------------------------------


def test_five_reads_trigger_one_security_call(keychain_double, tmp_path, canary):
    """Five reads, one process — and a counter that really counts.

    The invariant, and the reason the memo exists. Every push reads every
    configured secret, so a per-push lookup is a per-push keychain call,
    and on a keychain that prompts for access that is a per-push prompt:
    the operator clicks through the same five notifications on a
    workstation where nothing has changed since the first one.

    The count is one, and the *value* is the same on all five reads, so
    the memo is serving a value rather than merely skipping a call.

    The last two reads happen after an explicit ``reset_cache()`` and
    move the counter to two. That is the control that keeps this test
    honest: it shows the counter is live and reachable, so a count of one
    is the memo holding the count down rather than a stand-in that only
    ever counts once. It is also the documented contract of
    ``reset_cache()`` — the memo has a lifetime the process does not
    control, and this is the seam that ends it.
    """
    account = canary("feishu_index", tmp_path)
    keychain_double.write_payload(tmp_path, b"the-secret\n")
    keychain_double.set_index(FIRST, account)

    values = [credentials.read_secret(FIRST) for _ in range(5)]

    assert values == ["the-secret"] * 5, "the memo served a different value each read"
    assert keychain_double.count(account) == 1
    assert keychain_double.raw_count(account) == "1", (
        "the counter file does not hold the number 1; it holds "
        "{!r}".format(keychain_double.raw_count(account))
    )

    credentials.reset_cache()
    credentials.read_secret(FIRST)

    assert keychain_double.count(account) == 2, (
        "reset_cache() did not empty the memo, so the count of 1 above "
        "cannot be distinguished from a stand-in that only counts once"
    )


def test_failed_lookup_is_cached_too(keychain_double, tmp_path, canary):
    """A lookup that found nothing is remembered as having found nothing.

    The half of the memo that is easy to leave out, because "no value"
    feels like nothing to store. It is not: a miss is an answer, and an
    answer that is recomputed on every call is the expensive kind. The
    tool exits nonzero for an item that is not in the keychain — the
    ordinary state of a deployment that turned the switch on and never
    added the item — and a deployment in that state pays the lookup on
    every single push, forever, for a value that is not there.

    So the miss is cached, the five reads return the same thing, and the
    count stays at one.

    The plaintext fallback is deliberately populated. A cached *miss* is
    only correct if it is a miss, and this is the state that could turn a
    cached miss into a silent downgrade: an operator who asked for the
    keychain must be told the keychain has nothing, not handed the
    variable the keychain was meant to replace.
    """
    account = canary("feishu_index", tmp_path)
    keychain_double.write_payload(tmp_path, b"the-secret\n")
    keychain_double.set_index(FIRST, account)
    keychain_double.exit_code(44)
    monkey = keychain_double._monkeypatch
    monkey.setenv(credentials.SECRET_SPECS[FIRST].fallback_env_key, "the-plaintext")

    values = [credentials.read_secret(FIRST) for _ in range(5)]

    assert keychain_double.count(account) == 1, (
        "a failed lookup was repeated on every read; an unconfigured "
        "secret would cost one keychain call per push forever"
    )
    assert values == [None] * 5, "a failed lookup returned something other than None"
    assert all(value is values[0] for value in values), (
        "the five reads did not return the identical object"
    )
    assert credentials.secret_source(FIRST) == "missing"
    assert credentials.secret_available(FIRST) is False


def test_cache_is_per_logical_name(keychain_double, tmp_path, canary):
    """One secret's read does not answer for the other — in either direction.

    The memo is keyed by logical name, and the key is the only thing
    standing between two providers' answers. A cache that was not keyed
    by name — a single slot, a key derived from the wrong field, a dict
    keyed by the environment variable rather than the logical name — would
    hand one transport another transport's secret, and the symptom would
    be an authenticated send to the wrong chat rather than anything that
    looks like a caching bug.

    **The counts alone cannot catch that,** which is why the stand-in is
    told to echo the account it was asked for. A single shared slot also
    starts *no* extra process for the second secret — it answers from the
    first one's entry — so a test that only counted would see the counts
    it expects and pass. What separates the two designs is the value: the
    second secret must come back as itself.

    Both directions are exercised, because "keyed by name" and "keyed by
    whichever was read first" are different implementations that agree on
    the first half.
    """
    first_account = canary("feishu_index", tmp_path)
    second_account = canary("telegram_index", tmp_path)
    keychain_double.echo_account()
    keychain_double.set_index(FIRST, first_account)
    keychain_double.set_index(SECOND, second_account)

    # One cache lifetime, both secrets. A shared slot only shows itself
    # when two names are looked up against the *same* memo — reset in
    # between and the defect hides behind the reset.
    for _ in range(5):
        assert credentials.read_secret(FIRST) == first_account

    assert keychain_double.count(first_account) == 1
    assert keychain_double.count(second_account) == 0, (
        "reading one secret started a process for another; the memo is "
        "not keyed by logical name"
    )
    assert keychain_double.raw_count(second_account) is None, (
        "a counter file exists for a secret nobody read, so the zero "
        "above is a count rather than an absence"
    )

    for _ in range(5):
        assert credentials.read_secret(SECOND) == second_account, (
            "the second secret was answered with the first one's value; "
            "the memo is not keyed by logical name"
        )

    assert keychain_double.count(second_account) == 1
    assert keychain_double.count(first_account) == 1, (
        "reading the second secret went back to the keychain for the first"
    )

    # And the other order, from an empty memo, so a cache that answers
    # from "whatever was read first" cannot pass on the ordering above.
    credentials.reset_cache()
    for _ in range(5):
        assert credentials.read_secret(SECOND) == second_account
    for _ in range(5):
        assert credentials.read_secret(FIRST) == first_account, (
            "the first secret was answered with the second one's value; "
            "the memo is not keyed by logical name"
        )

    assert keychain_double.count(first_account) == 2
    assert keychain_double.count(second_account) == 2


# ---------------------------------------------------------------------------
# Across a process boundary
# ---------------------------------------------------------------------------


def test_cache_is_not_shared_across_processes(
    keychain_double, tmp_path, canary, run_child
):
    """A child reading the same name starts its own lookup.

    The memo is a module-level dict, so it lives in one interpreter. A
    child process gets none of it, and a child that inherited one would
    mean the memo is somewhere else — a file, a socket, a shared store —
    and every one of those writes a secret to a place the keychain was
    supposed to be the only copy of.

    The counter is shared between the two processes precisely because it
    has to be: the same stand-in, the same count file. The parent reads
    five times and the count goes to one. The child reads five times and
    the count goes to two — not to six, which is what sharing a memo
    would *not* produce but what failing to cache in the child would, and
    not to one, which is what inheriting the parent's memo would.

    The child's exit code says which of its own two checks failed, so a
    failure names "the value was wrong" or "the five reads disagreed"
    rather than "the child returned non-zero".
    """
    account = canary("feishu_index", tmp_path)
    keychain_double.write_payload(tmp_path, b"the-secret\n")
    keychain_double.set_index(FIRST, account)

    for _ in range(5):
        assert credentials.read_secret(FIRST) == "the-secret"
    assert keychain_double.count(account) == 1

    child = run_child(
        _CHILD_SRC, env=_child_env(keychain_double, tmp_path, FIRST, account)
    )

    assert child.returncode == EXIT_CHILD_OK, (
        "the child exited {} ({} = wrong value, {} = the five reads "
        "disagreed); stderr:\n{}".format(
            child.returncode,
            EXIT_CHILD_WRONG_VALUE,
            EXIT_CHILD_UNSTABLE,
            child.stderr.decode("utf-8", "replace"),
        )
    )
    assert keychain_double.count(account) == 2, (
        "the child did not run its own lookup; the memo is being shared "
        "across processes, which means it is stored somewhere a secret "
        "would outlive the process"
    )


# ---------------------------------------------------------------------------
# The read-only promise
# ---------------------------------------------------------------------------


def test_provider_creates_no_files_outside_tmp_path(
    keychain_double, tmp_path, canary
):
    """Every file this path creates is inside the test's own directory.

    A memo is a good place for a secret precisely because it is not a
    place. Anything that spilled one — a memo file under a cache
    directory, a lock beside the source, a scratch file in ``/tmp`` — would
    make the cache the second copy of the secret the keychain exists to
    be the only copy of, and it would be a copy with a readable lifetime
    nobody chose.

    Three roots are watched, because three are where a cache goes: the
    working tree, the keychain directory the module names, and the
    system temporary directory. Byte-compiled modules are excluded from
    the first, and the exclusion is named in ``_NOT_THE_MODULES``
    because this suite starts a child that imports the module under test
    and the interpreter — not the module — writes those.

    Two controls keep the assertion from being vacuous. The counter file
    is asserted to exist and to hold one, so the watch is over a run that
    demonstrably wrote something. And a probe file is created in the
    working tree and removed again, so the walker is shown to notice a
    write in the very place it is claiming to watch.
    """
    account = canary("feishu_index", tmp_path)
    keychain_double.write_payload(tmp_path, b"the-secret\n")
    keychain_double.set_index(FIRST, account)

    # The places a cache actually goes, and the two that are not walked
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

    for _ in range(5):
        assert credentials.read_secret(FIRST) == "the-secret"

    after = snapshot()

    # Control one: the run wrote something, so "nothing new" is a fact
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
        "the read path created files outside the test's directory:\n{}".format(
            "\n".join(sorted(after - before))
        )
    )
