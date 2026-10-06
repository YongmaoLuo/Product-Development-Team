"""The delivered credential chain, walked once in the order an operator meets it.

Why this file exists
--------------------
The migration from a plaintext environment variable to the OS keychain is
covered in pieces, and no single piece covers the whole of it. The provider
is pinned in ``tests/unit/test_credentials_switch.py`` and its subprocess
read in ``tests/integration/test_credentials_security_lookup.py``; the
transports are pinned individually in ``tests/unit/notifications/``; the
service-level degradation is pinned in
``tests/integration/test_server_without_notification_secrets.py``; and the
real platform tool, a real keychain and a real child process are pinned in
``tests/e2e/test_keychain_full_delivery_macos.py``.

What none of them is is a **walk**. Each answers "does this layer do its
part" while holding the neighbouring layers in place by fixture. An
acceptance walk holds nothing in place: one deployment, resolved once, and
every claim made about the *same* value as it travels from the keychain to
a transport to a child process. The failure this file exists to catch is
arithmetic across the layers rather than inside one of them — a provider
that resolves from the keychain while a transport reads the variable, a
handoff that works while a consumer reports the channel disabled, a
disabled path that raises where it was meant to go quiet.

Six steps, in the order the chain is met
----------------------------------------
1. the value exists in the keychain and in no environment variable of this
   process;
2. the provider finds it by account alone — no service, no ``-s``;
3. the consumers agree it is configured, and no consumer's status report
   carries the value;
4. a real child receives it on a descriptor, reads the payload, and its own
   environment does not contain it;
5. with the switch closed, every consumer reports disabled, nothing is
   sent, nothing raises, and the CLI says so in its own vocabulary;
6. the account-specific cases skip where the platform tool cannot be
   started, and the strict switch turns that skip into a failure.

Steps 1 through 5 run wherever the e2e lane runs, and they do it with the
keychain **mocked** — a real executable on disk, standing in for the
platform tool, pointed at by ``credentials._SECURITY_BIN``. That is the
whole point of them: the chain under test is the project's, and it has to
be walkable on a Linux runner that has no keychain at all, which is where
most of its deployments live. Mocking the *tool* keeps ``subprocess`` real,
so the command line a reviewer checks is the command line that runs.

Step 6 is the exception and is the only part of this file that needs a
machine with a keychain, because the claim it makes is about the tool the
chain reads through when nothing stands in for it. See "The skip-proof"
below for how the two positions are told apart without waiting for a
machine whose policy happens to refuse a binary.

What is *not* replaced
----------------------
Everything but the keychain tool. ``credentials``, ``cli``, the two
transports and the notifier are the production modules, called as
production calls them. The provider's platform check is patched so that a
Linux runner can take the keychain path at all — and that patch is the
*second* thing this file stands in for, which is why step 5 closes the
switch by hand rather than by moving the platform: a switch closed by the
operator and a keychain the platform never had are different states, and
only the first of them is the one the switch is about.

The child's network is not stubbed, because no child in this file makes a
request. Step 4 asks where the value travels, and the child answers by
reading a descriptor and looking at its own environment.

Resource discipline
-------------------
Every child this file starts is a ``subprocess.run``, so it is reaped
before the call returns; the one that is handed a descriptor registers
that descriptor with ``register_open_fd`` first, because the provider
deliberately hands the read end over and closes only the write end. The
notifier is started and stopped inside the case that starts it, and
unsubscribed from the process-global event bus before that case returns —
the bus outlives the notifier, and a handler left attached to a stopped
notifier keeps accepting events for the rest of the session.

The skip-proof
--------------
The two account-specific cases at the bottom of this file read a **real**
keychain through the real ``/usr/bin/security``, so they carry a
capability gate: a machine that will not start the tool skips them, and a
lane that demands the real run does not get to skip them.

That gate has two positions, and the switch ``PDT_REQUIRE_CREDENTIAL_CHAIN_E2E``
decides which. The default is a skip, because a refusal there is a fact
about the machine rather than a defect in code the case never reached.
The strict position fails, because a lane that asked for the real run and
received six skips — or two, here — has published no evidence while
reporting green.

Which position a given run is in is not written down here; it is on the
reader's own summary line, and the switch is the only difference between
the two. The demonstration at the bottom of this file does not wait for a
machine whose policy refuses a binary, because almost none does. It
supplies the refusal itself — a file in the test's own ``tmp_path`` with no
execute bit, which is a genuine ``EACCES`` from the kernel rather than a
string written into a test — installs the gate built over that refusal in
a **child pytest**, and reads the counts out of the child's summary. That
is the driving construction, and it is a **port rather than an
invention**: it originates in
``tests/e2e/test_keychain_full_delivery_macos.py``, which faces the same
problem with a wider set of gated binaries and already had to solve it.
The two helpers it is built from — the one that manufactures the refusal
and the one that runs the gated cases in a child pytest and returns the
counts — are named and exercised there, and the check at the bottom of
this file re-points at that file to confirm the citation still resolves.

What was ported is the construction, not the evidence. The readings
quoted in that file are readings taken on the machine that took them, and
this file quotes none: a mark whose condition is true and a case pytest
has actually reported as a failure are the same statement until pytest
has acted on it, and the point of the strict position is entirely about
what pytest reports. So the two positions are demonstrated here by driving
both of them, and nothing is written down above as though it had been
observed on the machine reading it.

The child's platform is written before the module under test is imported,
because ``skipif`` reads ``sys.platform`` at import. That is what lets the
strict case be driven on a machine that starts every binary, and what
keeps the demonstration from depending on the host this suite happens to be
running on.

What "the CLI reports DISABLED" means here
------------------------------------------
Step 5 asks the command an operator runs to say, in its own vocabulary,
that the deployment is not configured. That vocabulary is a source label
and an exit code, not a word: ``secrets verify`` prints
``source=missing`` for every secret and exits nonzero. There is no
``DISABLED`` token in the CLI, and this file does not add one — a test
that needed a new string in production would be a second implementation
of the command, which is the one thing an acceptance file is not allowed
to be. What is asserted is the behaviour the token would have stood for:
no row claims the keychain, no row carries a value, and the exit code is
nonzero. On a platform with no keychain tool the same exit code is also
what ``secrets verify`` returns for a machine fact, so the rows — not the
code — are the evidence, and the case says so where a reader would
otherwise read the code as the claim.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import os
import re
import subprocess
import sys
import unittest.mock
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Dict, Iterator, List, Mapping, Optional

import pytest

import credentials

#: ``backend/``, resolved from this file rather than written out: the
#: repository is checked out at a different path on every machine, and this
#: path is handed to a child as its working directory and import path.
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The secret this file follows. Named by its logical name and read out of
#: the provider's own table, so a renamed row is a rename this file
#: follows rather than a literal it has outgrown.
LOGICAL_NAME = "feishu_app_secret"

#: The second row of the same table. Steps 1, 3 and 5 are about "every
#: consumer", and a claim about every consumer that only ever looks at one
#: of the two transports is a claim about half of them. Derived rather than
#: written out so a row added to the table is walked without an edit here.
CONSUMER_NAMES = tuple(credentials.SECRET_SPECS)

#: The keychain this project opens, spelled out here rather than read back
#: out of ``credentials._KEYCHAIN_PATH``.
#:
#: This file's own header calls the stand-in's recorded command line the
#: thing "a reviewer checks", and that is precisely why the expectation
#: has to be independent of the module. Built from
#: ``credentials._KEYCHAIN_PATH``, the expectation is the module's own
#: answer, and the walk would stay green with the read pointed at the
#: login keychain while the recorded ``argv`` — the artefact the whole
#: acceptance claim rests on — attested to the opposite of what it looks
#: like. Written out, a regression in the constant turns the recorded
#: command line red at exactly the argument that carries it.
DEDICATED_KEYCHAIN_FILE = "Library/Keychains/runtime-secrets.keychain-db"

#: The descriptor variable the child is told to read, derived through the
#: provider's entry point. The variable's *name* is what a reviewer can
#: check; its *value* is a descriptor number and never a secret.
FD_ENV_VAR = credentials.secret_fd_env_var(LOGICAL_NAME)

#: How long a child may take to run to completion. Generous, because it
#: covers a loaded CI runner, and bounded, because a child that never
#: finishes must fail the case that started it rather than spend the lane's
#: whole budget.
_CHILD_TIMEOUT_SECONDS = 60

#: How long the CLI may take. Longer than a child's, because the command
#: imports the agent package before it reaches the secrets subcommand.
_CLI_TIMEOUT_SECONDS = 120

#: The words the child writes, so a truncated or interleaved line cannot be
#: mistaken for one.
_READY = "READY"
_CLEAN = "CLEAN"
_LEAKED = "LEAKED"

#: The child's exit code for "I never got the payload" — distinct from any
#: code the interpreter would use itself, so the parent can tell the two
#: apart rather than reporting a syntax error as a broken handoff.
_CHILD_BAD = 3

#: The shortest fragment of the value that counts as a leak. Any shorter
#: and the search starts matching the key names, which are the one thing
#: the diagnostics are supposed to be able to contain.
_LEAK_MIN_FRAGMENT = 6

#: The two environment entries this file adds to conduct the handoff: the
#: name of the variable naming the descriptor, and the logical name the
#: payload is keyed by. Both spell the secret's *name*, and a canary is
#: built out of exactly those words, so a sweep that ran over them would
#: report every child as leaking. Stripping them does not narrow the
#: whole-value assertion, which is computed in the child from the value it
#: actually read.
#:
#: Bare names, not ``name=value`` prefixes. The child matches these
#: against ``os.environb`` keys, which carry no ``=``; a prefix written
#: with one never matches and the entries it was meant to exclude are
#: swept like any other. Membership rather than ``startswith`` for the
#: same reason — an entry this file did not write must not be excluded
#: because its name happens to begin the same way.
_HARNESS_ENTRY_NAMES = (b"PDT_TEST_FD_VAR", b"PDT_TEST_SECRET_NAME")

#: The keychain tool as the provider defines it. **Read, never replaced**,
#: by the two account-specific cases at the bottom of this file: those are
#: the cases whose whole subject is the un-mocked read, and pointing them
#: at a stand-in would make them report green about a machine that does
#: not exist. Steps 1 to 5 replace it through ``monkeypatch`` instead, and
#: only for the duration of their own fixture.
_REAL_SECURITY_BIN = credentials._SECURITY_BIN

#: The home directory this process was started with, captured at import —
#: before any case redirects ``$HOME``. Every delete below is checked
#: against *this* value rather than against ``Path.home()`` read at
#: teardown: teardown is exactly when the redirect has been undone, so a
#: check made there would compare the keychain against the home it was
#: derived from and pass unconditionally.
_REAL_HOME = Path.home()

#: The variable that says the account-specific cases had to actually run
#: rather than be reported as not-run. A lane that sets it closes the skip
#: channel; a lane that does not gets the default position, where a refusal
#: is a fact about the machine.
_STRICT_SWITCH_ENV = "PDT_REQUIRE_CREDENTIAL_CHAIN_E2E"

#: The two spellings that mean "the real run was asked for", and the whole
#: of them. Two rather than a family, because a spelling that is not on this
#: list is a value the function below has to refuse.
_STRICT_SWITCH_ON_VALUES = frozenset({"1", "true"})

#: The attribute a strict-position gate tags the case it wrapped. The
#: default position is a ``skipif`` mark and is identifiable as one; the
#: strict position is a call-time wrapper and leaves no ``pytestmark``
#: behind, so without this tag a strictly gated case is indistinguishable
#: from one that was never gated — and the structural check below would
#: report the gate as missing on exactly the machines it exists for.
_CAPABILITY_GATE_ATTR = "_pdt_capability_gate"


pytestmark = pytest.mark.e2e


def require_real_tool_requested() -> bool:
    """Whether ``_STRICT_SWITCH_ENV`` says the real cases have to run.

    Answers a question about one string and reads nothing else. Read from
    ``os.environ`` on every call rather than captured at import: the gate
    and the cases that check the gate share one process, so a value
    snapshotted at import would answer for the whole session and the two
    positions could never both be exercised in one run.

    Whitespace is stripped and nothing else. The accepted spellings are
    exactly ``1`` and ``true``; a value nobody wrote down as "on" leaves
    the file in its default position, which is the direction that fails
    safe — a spelling that quietly meant "on" would turn somebody's guess
    into a demand that real cases run.
    """
    raw = os.environ.get(_STRICT_SWITCH_ENV)
    if raw is None:
        return False
    return raw.strip() in _STRICT_SWITCH_ON_VALUES


# ---------------------------------------------------------------------------
# The keychain stand-in
# ---------------------------------------------------------------------------
#
# A real program, in ``/bin/sh`` so the suite needs no interpreter of its
# own to run it. It records its whole command line — ``$0`` first, then one
# argument per line, so a test can count invocations *and* read back exactly
# what production asked for — serves the value filed under the account it
# was asked for, and exits nonzero for an account it holds nothing for,
# which is what the real tool does for an item that is not in the keychain.
#
# A real executable rather than a patch of ``subprocess.run``, because a
# mock of the call can only assert that the module called something with
# some arguments. It cannot tell whether those arguments are a command line
# that works, and it cannot fail the way the real tool fails. That is the
# same reason the integration suite uses one.
#
# It is asked for the password and the account, and for nothing else. The
# parsing below accepts a ``-s`` and ignores it rather than rejecting it,
# so a service added to the production command line is answered with a
# value here and caught by the argv assertion in step 2 — a stand-in that
# rejected the flag would fail every case in the file instead of naming the
# one place the answer matters.

_STANDIN_SOURCE = """#!/bin/sh
printf '%s\\n' "$0" >> "$PDT_TEST_ARGV_LOG"
for arg in "$@"; do
    printf '%s\\n' "$arg" >> "$PDT_TEST_ARGV_LOG"
done

account=""
want_password=0
while [ $# -gt 0 ]; do
    case "$1" in
        -a) account="$2"; shift 2 ;;
        -w) want_password=1; shift ;;
        *) shift ;;
    esac
done

if [ "$want_password" -ne 1 ]; then
    echo "find-generic-password: the -w option is required" >&2
    exit 2
fi

item="$PDT_TEST_KEYCHAIN_DIR/$account"
if [ ! -f "$item" ]; then
    echo "The specified item could not be found in the keychain." >&2
    exit 44
fi

cat "$item"
"""


class Keychain(object):
    """A deployment whose secrets live in a stand-in keychain, and its controls.

    Three things live here and all three are the deployment's own: the
    account each row of the spec table is filed under, the value filed
    under it, and the argv log the stand-in writes. A case asks the object
    what the provider resolved; it never reaches past it into
    ``os.environ`` to check the provider's work, because a test that
    compares a resolution against the environment it set up is comparing
    two things it controls rather than two things the code decided.
    """

    def __init__(
        self,
        accounts: Dict[str, str],
        values: Dict[str, str],
        argv_log: Path,
        home: Path,
        switch_value: str,
        written: Dict[str, str],
    ):
        self.accounts = accounts
        self.values = values
        self.argv_log = argv_log
        self.home = home
        self.switch_value = switch_value
        #: Every variable the fixture set, by name. The chain's own text,
        #: as opposed to the eighty-odd inherited ones a developer's shell
        #: or a CI job's ``env:`` block happens to contribute — see the
        #: two-tier sweep in step 1 for why that distinction decides how
        #: hard each can be searched.
        self.written = written

    def value(self, name: str = LOGICAL_NAME) -> str:
        return self.values[name]

    def account(self, name: str = LOGICAL_NAME) -> str:
        return self.accounts[name]

    def argv(self) -> List[str]:
        """The recorded command lines, one flat list of arguments.

        An absent log means no process was started at all, which is a
        state a case has to be able to tell apart from "invoked with no
        arguments" — so the empty list is returned rather than an error.
        """
        if not self.argv_log.exists():
            return []
        return self.argv_log.read_text(encoding="utf-8").splitlines()

    def env(self, extra: Optional[dict] = None) -> dict:
        """The environment the CLI subprocess is started with.

        Built from nothing rather than copied from ``os.environ``. A copy
        would carry whatever the machine running the suite has exported,
        and a case asserting that a deployment exports no plaintext would
        then be asserting something about the developer's shell.
        """
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(_BACKEND_DIR),
            "HOME": str(self.home),
            credentials._SWITCH_ENV_KEY: self.switch_value,
        }
        for name, account in self.accounts.items():
            env[credentials.SECRET_SPECS[name].account_env_key] = account
        if extra:
            env.update(extra)
        return env


def _canary_kind(name: str, role: str) -> str:
    """The ``canary`` fixture kind for ``name``'s ``role``.

    The fixture declares four kinds, and the spec table declares two rows,
    so the mapping between them is a decision rather than a derivation.
    A secret is minted from the row's own kind and its index from the
    matching one, so the two halves of a row are minted the same way and a
    test that asserts "the account names the item" is asserting about a
    pair this file built together.
    """
    first = LOGICAL_NAME
    return {
        (first, "secret"): "feishu_secret",
        (first, "index"): "feishu_index",
        ("telegram_bot_token", "secret"): "telegram_token",
        ("telegram_bot_token", "index"): "telegram_index",
    }[(name, role)]


@pytest.fixture
def keychain(tmp_path, monkeypatch, canary):
    """Point the provider at a real executable standing in for the keychain tool.

    The four things production reads are all redirected here: the platform
    (so a Linux runner can take the keychain path at all), the switch (so
    the keychain is on), ``$HOME`` (so the keychain path the provider
    builds is a path inside this test's ``tmp_path``), and the binary
    itself. The timeout is left at the production value because the
    stand-in answers immediately and a shortened one would be a bound this
    file does not need.

    Every row of the spec table gets an account and a value, and **no
    plaintext fallback is exported at all**. That last part is what makes
    the walk mean something: with a fallback set, "it resolved" and "it
    resolved from the keychain" are the same observation, and the source
    label would be the only thing telling them apart.
    """
    for key in ("PDT_TEST_ARGV_LOG", "PDT_TEST_KEYCHAIN_DIR"):
        monkeypatch.delenv(key, raising=False)

    home = tmp_path / "home"
    (home / "Library" / "Keychains").mkdir(parents=True)
    items = tmp_path / "items"
    items.mkdir()

    accounts = {name: canary(_canary_kind(name, "index"), tmp_path) for name in CONSUMER_NAMES}
    values = {name: canary(_canary_kind(name, "secret"), tmp_path) for name in CONSUMER_NAMES}
    for name, account in accounts.items():
        (items / account).write_text(values[name], encoding="utf-8")

    binary = tmp_path / "security"
    binary.write_text(_STANDIN_SOURCE, encoding="utf-8")
    binary.chmod(0o755)

    argv_log = tmp_path / "argv.log"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(credentials._SWITCH_ENV_KEY, "0")
    monkeypatch.setenv("PDT_TEST_ARGV_LOG", str(argv_log))
    monkeypatch.setenv("PDT_TEST_KEYCHAIN_DIR", str(items))
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)
    monkeypatch.setattr(credentials, "_SECURITY_BIN", str(binary))
    written = {
        "HOME": str(home),
        credentials._SWITCH_ENV_KEY: "0",
        "PDT_TEST_ARGV_LOG": str(argv_log),
        "PDT_TEST_KEYCHAIN_DIR": str(items),
    }
    for name in CONSUMER_NAMES:
        key = credentials.SECRET_SPECS[name].account_env_key
        monkeypatch.setenv(key, accounts[name])
        written[key] = accounts[name]
        # The whole point of the walk, in one line: with a fallback
        # exported, "it resolved" and "it resolved from the keychain" are
        # the same observation.
        monkeypatch.delenv(credentials.SECRET_SPECS[name].fallback_env_key, raising=False)

    credentials.reset_cache()

    deployment = Keychain(accounts, values, argv_log, home, "0", written)
    yield deployment

    credentials.reset_cache()


@pytest.fixture
def keychain_closed(keychain, monkeypatch):
    """The same deployment with the keychain switch closed by the operator.

    The platform check stays patched: the keychain is *available* here and
    an operator has chosen not to use it, which is the state the switch is
    about. Leaving the platform alone would make this the Linux state
    instead — one the switch never got a say in — and the walk would end up
    covering two different things under one name.

    The accounts stay exported, because an account with the keychain
    switched off is not an error: it is an index nothing is going to look
    up, and a case that cleared it would be testing a deployment with no
    configuration rather than a deployment that declined one.
    """
    credentials.reset_cache()
    monkeypatch.setenv(credentials._SWITCH_ENV_KEY, "1")
    credentials.reset_cache()
    keychain.switch_value = "1"
    return keychain


@pytest.fixture
def published(keychain, register_open_fd):
    """Return ``(value, fd)`` for a handoff out of the stand-in keychain.

    The descriptor is registered before it is handed on, because the
    provider deliberately closes only the write end: the read end belongs
    to whoever is given it, and until that is spent it is a descriptor
    this test opened and this test has to hand back.
    """
    value = keychain.value()
    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None, (
        "nothing was published, so there is no handoff to walk: the "
        "provider resolved no value for {} from the keychain".format(LOGICAL_NAME)
    )
    register_open_fd(fd)
    return value, fd


# ---------------------------------------------------------------------------
# Sweeping
# ---------------------------------------------------------------------------


def leaked_fragments(text: str, value: str) -> List[str]:
    """Every fragment of ``value`` that ``text`` carries, six characters up.

    A substring search rather than an equality test, and deliberately: the
    failure this rules out is a value that travelled *inside* a larger one
    — prefixed, suffixed, or a whole environment serialised into a single
    variable — and equality would call that clean.

    Never used in a failure message. A reader is told *which* entry and
    *how much*, and the fragments themselves are not printed, because a
    failure message is a thing that gets pasted into an issue.
    """
    return [
        value[start:start + length]
        for start in range(len(value))
        for length in range(_LEAK_MIN_FRAGMENT, len(value) - start + 1)
        if value[start:start + length] in text
    ]


def offending_entries(environment: Mapping[str, str], value: str) -> Dict[str, int]:
    """``{variable: longest fragment it carries}`` for the entries that match.

    Reported one entry at a time rather than as one blob, because a sweep
    over the whole environment can only say *that* a fragment was found. A
    reader who cannot name the variable cannot go and look at the thing
    that put it there, and a sweep that can only produce an unattributable
    failure is one whose first response is to be loosened until it passes.

    The variable *name* and a length are reported; the fragment is not. A
    name is not a secret and a length locates the match, and neither ends
    up in a paste-able summary.
    """
    found = {}
    for key, text in environment.items():
        hits = leaked_fragments(str(text), value)
        if hits:
            found[key] = max(len(fragment) for fragment in hits)
    return found


def _values_text(obj) -> str:
    """Every leaf *value* anywhere inside ``obj``, as one searchable blob.

    A status report is nested — a reason, a list, a counter beside each —
    and an assertion that only looked at the top level would pass on a
    value carried one level down. Walking the whole structure is what
    makes "the value is not in there" a claim about the report rather than
    about its top-level keys.

    Keys are deliberately not included, and the reason is a canary rather
    than a convenience. The shared canary fixture gives a secret and its
    index prefixes that begin with the same provider name, so the label
    ``telegram_enabled`` in a report contains eight characters of the
    Telegram token's own canary. A fragment sweep over labels would
    therefore report every healthy report as a leak, and a test that has
    to be quieted to pass is a test nobody reads. Truncation is checked
    against the whole report instead, keys included — see the case.
    """
    parts = []
    stack = [obj]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set)):
            stack.extend(item)
        elif item is not None:
            parts.append(str(item))
    return "\n".join(parts)


def _all_text(obj) -> str:
    """Every string anywhere inside ``obj``, keys and values alike.

    Wider than :func:`_values_text` and used for the one check that has to
    see the whole thing: whether the value appears **intact**. A key named
    after a secret is a leak this catches and the values-only sweep does
    not.
    """
    parts = []
    stack = [obj]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set)):
            stack.extend(item)
        elif item is not None:
            parts.append(str(item))
    return "\n".join(parts)


def _without_published_indexes(environment: Mapping[str, str], keychain) -> Dict[str, str]:
    """The environment minus the indexes this deployment publishes.

    An account index is *supposed* to be in the environment — it is the
    key that names the item, and keeping it there is the entire reason the
    keychain is reachable. It is also a canary sharing its first
    sixteen characters with that row's own secret canary, so sweeping the
    environment while it is present would flag the correct configuration.

    Only the indexes this fixture wrote are removed, and they are removed
    as whole strings rather than as prefixes. A secret that reached the
    environment by way of an index — the two sharing a value, say — would
    still be there in the text, and the sweep over what is left is the one
    that would catch it.

    Returned as a mapping rather than a string so a match can still be
    attributed to the variable it came from.
    """
    return {
        key: value
        for key, value in environment.items()
        if not any(value == account for account in keychain.accounts.values())
    }


# ---------------------------------------------------------------------------
# Step 1 — the value is in the keychain and in no environment variable
# ---------------------------------------------------------------------------


def test_the_value_is_in_the_keychain_and_in_no_environment_variable(keychain):
    """Step 1: the secret exists, and this process is not how it arrives.

    The label is checked alongside the sweep, and both are needed. A sweep
    on its own would pass on a process where the value was nowhere at all,
    including a provider that resolved nothing — which is precisely the
    state every later step is trying to distinguish from this one. So the
    value has to be readable *and* named as keychain-sourced before the
    absence of it from the environment means anything.

    The sweep covers the whole environment rather than only the two
    fallback variables named in the spec table: "not in the process
    environment" is the claim the keychain exists to make, and a check
    scoped to the names production knows about would let a value that
    arrived by some other route read as clean.

    Two tiers, because the environment is two different kinds of text and
    one strength of search is wrong for both.

    *Every* variable is searched for the value **intact**. That is the
    real claim, it has no coincidental form — the canary's suffix is
    eight derived hex characters — and it covers the inherited variables
    that a developer's shell or a CI job's ``env:`` block contributed,
    which this file neither wrote nor can enumerate.

    Only the variables the chain itself sets are searched for **fragments**
    down to six characters. That is where a partial transport matters —
    a value that travelled inside a larger one — and it is safe here for a
    reason it is not safe over the whole environment: the text is the
    fixture's own, so a coincidental match is not a thing that can happen.
    Searching all eighty-odd inherited variables at that floor is not a
    stricter test, it is a coin flip that fails on whichever unrelated
    variable happens to share six characters with a canary, and its first
    response on a machine that fails is to be loosened until it passes.
    """
    value = keychain.value()

    assert credentials.secret_source(LOGICAL_NAME) == credentials.SOURCE_KEYCHAIN, (
        "the provider did not name the keychain as the source of {}, so the "
        "sweep below would be reporting a secret this process never had".format(
            LOGICAL_NAME
        )
    )
    assert credentials.read_secret(LOGICAL_NAME) == value, (
        "the provider reported a keychain source and returned a different "
        "value, which is the disagreement this walk exists to catch"
    )
    assert credentials.secret_available(LOGICAL_NAME) is True

    whole = os.environ
    for name in CONSUMER_NAMES:
        secret = keychain.value(name)
        carriers = [
            key for key, text in whole.items() if secret in str(text)
        ]
        assert not carriers, (
            "the value for {} is in this process's environment, in {}. This "
            "process stands in for the server, and every process it starts "
            "inherits this.".format(name, ", ".join(sorted(carriers)))
        )

    # The variables the chain set, minus the indexes it is meant to publish.
    controlled = {
        key: text
        for key, text in os.environ.items()
        if key in keychain.written
    }
    offenders = offending_entries(_without_published_indexes(controlled, keychain), value)
    assert not offenders, (
        "the value reached one of the variables this deployment sets, which is "
        "the part of the environment the chain is responsible for. {} of {} "
        "carry a fragment of it, the longest {} characters, in: {}".format(
            len(offenders),
            len(controlled),
            max(offenders.values()),
            ", ".join(sorted(offenders)),
        )
    )

    for name in CONSUMER_NAMES:
        spec = credentials.SECRET_SPECS[name]
        assert spec.fallback_env_key not in os.environ, (
            "{} is exported, so a value resolved from the environment would be "
            "indistinguishable from one resolved from the keychain".format(
                spec.fallback_env_key
            )
        )


# ---------------------------------------------------------------------------
# Step 2 — the lookup is by account alone
# ---------------------------------------------------------------------------


def test_the_provider_asks_for_the_account_and_nothing_else(keychain):
    """Step 2: the item is located by the account, and by no service name.

    The argv is read out of the stand-in's own log rather than out of a
    patch of ``subprocess.run``, so what is checked is the command line
    that ran. It is asserted element by element and the ``-s`` flag is
    asserted *absent* rather than merely unused: ``security`` also finds an
    item by service, and a command carrying one has adopted a keychain
    layout that is a fact about one machine's keychain, written into this
    repository's source and true for exactly that operator.

    The keychain file is required to be named, for the opposite reason: an
    unqualified lookup searches whichever keychain the process happens to
    have open, and "whichever that is" is not a decision source code
    should be making on the operator's behalf.

    Every row of the table is walked, because the claim is about the
    lookup and the table is where the lookup is declared — a chain that
    reads one secret by account and the other by service is half-migrated
    with nothing in the outside showing it.
    """
    for name in CONSUMER_NAMES:
        credentials.read_secret(name)
    credentials.reset_cache()
    for name in CONSUMER_NAMES:
        credentials.read_secret(name)

    argv = keychain.argv()
    assert argv, (
        "no process was started, so there is no command line to check: the "
        "provider resolved every secret without consulting the keychain"
    )

    for name in CONSUMER_NAMES:
        account = keychain.account(name)
        assert account in argv, (
            "the account exported for {} ({!r}) never reached the command "
            "line, so the item was not looked up by the index this project "
            "publishes for it".format(name, account)
        )

    assert "find-generic-password" in argv, (
        "the provider asked the tool for something other than a generic "
        "password.\nargv: {!r}".format(argv)
    )
    assert "-s" not in argv, (
        "the command line carries a service name. The account is the only "
        "lookup key this project has; a service is part of one machine's "
        "keychain layout and does not belong in source.\nargv: {!r}".format(argv)
    )
    assert "find-internet-password" not in argv, (
        "the provider looked for an internet password, which is a different "
        "store with a different set of fields.\nargv: {!r}".format(argv)
    )

    # Spelled from this file's own literal. Deriving it from
    # ``credentials._KEYCHAIN_PATH`` would make this the strongest-looking
    # assertion in the walk and the weakest: the module would be asked
    # whether it had done what it said it would do. The recorded ``argv``
    # is the artefact an acceptance claim is read off, so it is spelled
    # against a constant this file owns.
    expected = str(keychain.home / DEDICATED_KEYCHAIN_FILE)
    assert expected in argv, (
        "the keychain file is not named on the command line, so the lookup "
        "searched whichever keychain this process happened to have open "
        "rather than this project's own.\nargv: {!r}".format(argv)
    )

    # As the value of ``-w``, not merely as a member of the line. The
    # login keychain holds every credential the account has ever saved,
    # so a walk that confirmed "some keychain was named" without
    # confirming *which* one would pass on the one answer it exists to
    # rule out.
    assert argv[-1] == expected, (
        "the last argument is not the dedicated keychain this project "
        "opens.\nargv: {!r}".format(argv)
    )
    assert "login" not in argv[-1].lower(), (
        "the read was pointed at the login keychain, which every process "
        "able to enumerate it can enumerate this project's secrets "
        "through.\nargv: {!r}".format(argv)
    )


# ---------------------------------------------------------------------------
# Step 3 — the consumers agree it is configured, and do not print it
# ---------------------------------------------------------------------------


class _Notified(object):
    """A started notifier and the two ways a case has to hand it back.

    The notifier owns a worker thread and a subscription on a
    process-global bus, and neither is reclaimed by dropping the object.
    Bundling the unsubscribe into the same handle the case used to start it
    is what keeps a case from leaving a handler attached to a stopped
    notifier, which would keep accepting events for the rest of the
    session.
    """

    def __init__(self, notifier):
        self.notifier = notifier

    def __enter__(self):
        return self.notifier

    def __exit__(self, exc_type, exc, tb):
        from notifications.state_events import STATE_EVENT_BUS

        self.notifier.stop(timeout=5.0)
        STATE_EVENT_BUS.unsubscribe(self.notifier._on_event)
        return False


def test_every_consumer_reports_configured_and_leaks_no_value(keychain, monkeypatch):
    """Step 3: three consumers, one resolved value, three "configured".

    Each transport is asked in its own vocabulary, because a single shared
    flag would hide which of them is wrong. Telegram's probe is its own
    (a token and a chat id are both required); the Feishu client's answer
    is that it constructed at all, which is the only signal it has; and
    the notifier's is the ``enabled`` field of its status report, which is
    what an operator actually reads.

    The status report is then swept for the value. ``stats()`` is the one
    place in this project that renders deployment state for a human, and
    it is a dictionary rather than a log line, so a value that reached it
    would be served by the debug endpoint and quoted into an incident
    report without ever passing through a line someone thought about.

    The client is constructed rather than its constructor being trusted:
    ``FeishuClient`` resolves the secret *inside* ``__init__`` and refuses
    to build without one, so a client that constructed is a client that
    read the value this file published.
    """
    from notifications.feishu_client import FeishuClient
    from notifications.feishu_notifier import FeishuNotifier
    from notifications.telegram_client import load_telegram_config

    value = keychain.value()

    telegram = load_telegram_config()
    assert telegram["enabled"] is True, (
        "the Telegram transport reports the channel unconfigured while the "
        "provider has just resolved its token: {!r}".format(
            {k: v for k, v in telegram.items() if k != "bot_token"}
        )
    )
    assert telegram["bot_token"] == keychain.value("telegram_bot_token"), (
        "the Telegram transport is enabled and is holding a token that is not "
        "the one the provider resolved — a different source, and therefore a "
        "different secret"
    )

    client = FeishuClient()
    assert client.app_secret == value, (
        "the Feishu client constructed and is holding a secret that is not the "
        "one the provider resolved"
    )

    with _Notified(FeishuNotifier()) as notifier:
        notifier.start()
        stats = notifier.stats()
        assert stats["enabled"] is True, (
            "the notifier reports itself disabled with the credential "
            "configured: {!r}".format(stats["disabled_reason"])
        )
        assert stats["telegram_enabled"] is True, (
            "the notifier's own Telegram probe disagrees with "
            "``load_telegram_config`` about the same environment"
        )

    report_values = _values_text(stats)
    report_all = _all_text(stats)
    for name in CONSUMER_NAMES:
        secret = keychain.value(name)
        leaked = leaked_fragments(report_values, secret)
        assert not leaked, (
            "the notifier's status report carries {} of the value for {}: "
            "{} fragment(s), the longest {} characters".format(
                "part" if len(leaked) < len(secret) else "all",
                name,
                len(leaked),
                max(len(f) for f in leaked),
            )
        )
        assert secret not in report_all, (
            "the value for {} appears in the notifier's status report, keys "
            "included".format(name)
        )


# ---------------------------------------------------------------------------
# Step 4 — a real child receives the value on a descriptor
# ---------------------------------------------------------------------------

_CHILD_SRC_TEMPLATE = """
import hashlib
import os
import sys

READY = {ready!r}
CLEAN = {clean!r}
LEAKED = {leaked!r}
CANNOT_READ = {cannot_read}
MIN_FRAGMENT = {min_fragment}
HARNESS_NAMES = {harness!r}
SET_ENTRIES = {set_entries!r}

fd_var = os.environ["PDT_TEST_FD_VAR"]
logical = os.environ["PDT_TEST_SECRET_NAME"]

raw = os.environ.get(fd_var)
if raw is None:
    sys.exit(CANNOT_READ)
try:
    fd = int(raw)
except ValueError:
    sys.exit(CANNOT_READ)

import credentials

try:
    payload = credentials.read_secret_fd(fd)
except (OSError, ValueError):
    sys.exit(CANNOT_READ)
value = payload.get(logical)
if value is None:
    sys.exit(CANNOT_READ)

# The child's own view of its initial environment, as the kernel handed it
# over, entry by entry rather than joined: a sweep that can only say "a
# fragment was found" is a sweep whose first response on a machine that
# fails is to be loosened, and naming the variable is what stops that.
#
# The two entries that conduct the handoff are skipped entirely: both spell
# the secret's *name*, and a canary is built out of those words, so a sweep
# running over them would report every child as leaking. Matched by whole
# name — os.environb keys carry no "=" — so no entry this file did not
# write is excluded by resemblance to one it did.
entries = [
    (name, text)
    for name, text in os.environb.items()
    if name not in HARNESS_NAMES
]

# Two tiers, for the reason step 1 gives: the value intact is searched for
# everywhere, because that is the real claim and it has no coincidental
# form; fragments down to MIN_FRAGMENT are searched for only in the entries
# this harness set, where the text is the test's own and a six-character
# collision is not a thing that can happen. PATH is inherited from whatever
# machine is running the suite and is searched at the first tier only.
encoded = value.encode("utf-8", "surrogateescape")

verdict = CLEAN
for name, text in entries:
    found = encoded in text
    if not found and name in SET_ENTRIES:
        found = any(
            encoded[start:start + length] in text
            for start in range(len(encoded))
            for length in range(MIN_FRAGMENT, len(encoded) - start + 1)
        )
    if found:
        # The name, never the value and never the fragment.
        verdict = LEAKED + ":" + name.decode("ascii", "replace")
        break

sent = hashlib.sha256(encoded).hexdigest()
sys.stdout.write(READY + " " + sent + " " + verdict + "\\n")
sys.exit(0)
"""


def _child_source(set_entries: set) -> str:
    """The child program, with the names this file chose filled in.

    Formatted per call rather than once at import, because the set of
    names depends on which entries the caller put in the environment — and
    a program that reported a sweep over a set the caller did not ask for
    would be reporting on a different environment than the one it was
    started with.
    """
    return _CHILD_SRC_TEMPLATE.format(
        ready=_READY,
        clean=_CLEAN,
        leaked=_LEAKED,
        cannot_read=_CHILD_BAD,
        min_fragment=_LEAK_MIN_FRAGMENT,
        harness=_HARNESS_ENTRY_NAMES,
        set_entries=sorted(set_entries),
    )


def child_env(fd: Optional[int] = None, extra: Optional[dict] = None) -> dict:
    """The environment this file hands to a child, and the set of names it chose.

    Built from nothing, and that is the point rather than an omission.
    There is no ``$HOME``, no keychain switch and no account index in it:
    the child resolves nothing — it reads a descriptor it was handed — and
    a home directory and an account name would be a keychain address and
    an item index added to the very environment this file asserts is
    clean.

    The two names a caller can trust this file wrote are returned
    alongside, and the child searches them harder than it searches the two
    it inherited. ``PATH`` and ``PYTHONPATH`` are the machine's, not this
    file's: a six-character fragment of a random canary turning up in a
    directory somewhere on a ``PATH`` is a coincidence with no failure
    behind it, and a check that reports those has to be turned off before
    it can be believed on the entries that do matter.

    ``extra`` is how the positive control puts the value where a real leak
    would have put it: the child's own initial environment, under the very
    variable this project is removing. A control that put it anywhere else
    would not be a control on the sweep.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_BACKEND_DIR),
        "PDT_TEST_FD_VAR": FD_ENV_VAR,
        "PDT_TEST_SECRET_NAME": LOGICAL_NAME,
    }
    chosen = {"PYTHONPATH", "PDT_TEST_FD_VAR", "PDT_TEST_SECRET_NAME"}
    if fd is not None:
        env[FD_ENV_VAR] = str(fd)
        chosen.add(FD_ENV_VAR)
    if extra:
        env.update(extra)
        chosen.update(extra)
    return env, {name.encode("ascii", "replace") for name in chosen}


def _run_child(source: str, *, env: dict, pass_fds: tuple = ()) -> str:
    """Start a real interpreter, reap it, and return what it wrote.

    ``subprocess.run`` rather than a ``Popen`` this file has to close: the
    call waits for the child and reports a timeout as an exception, so
    every child here is joined before the case that started it returns.
    The resource rule this suite is held to has no exception for a child
    that exits on its own.
    """
    completed = subprocess.run(
        [sys.executable, "-c", source],
        env=env,
        cwd=str(_BACKEND_DIR),
        pass_fds=tuple(pass_fds),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_CHILD_TIMEOUT_SECONDS,
    )
    stdout = completed.stdout.decode("utf-8", "replace").strip()
    if completed.returncode != 0:
        pytest.fail(
            "the child exited {} where it was handed a descriptor to "
            "read.\nstdout: {!r}\nstderr: {}".format(
                completed.returncode,
                stdout,
                completed.stderr.decode("utf-8", "replace"),
            )
        )
    return stdout


def test_a_real_child_reads_the_payload_and_finds_no_value_in_its_own_environment(
    published,
):
    """Step 4: the value arrives on a pipe, and the environment says nothing.

    Non-vacuous by construction, in two independent ways. The child is
    told nothing about the value it is expected to find — the parent's
    digest is compared afterwards — and the sweep is computed *inside* the
    child, by a process holding the value at the moment it looks. So
    "the environment does not contain it" is a statement by a process
    carrying the secret, not about one that never received it, and the
    sentence cannot be true of a child that read nothing.

    The child's environment is built from nothing rather than inherited,
    because an inherited one would carry whatever the machine running the
    suite has exported, and a child with a real secret in its environment
    for unrelated reasons would turn this case into a reading of the
    runner rather than of the handoff.

    What is asserted is the whole value in every entry the child was
    started with, plus every fragment of it down to six characters in the
    entries this file chose. The fragments are what rule out a value that
    travelled inside a larger one, and they are confined to the entries
    this file wrote because ``PATH`` and ``PYTHONPATH`` belong to the
    machine: a random canary sharing six characters with a directory
    somewhere on a ``PATH`` is a coincidence, and a check that reports
    those has to be switched off before it can be believed on the entries
    that matter.
    """
    value, fd = published

    env, chosen = child_env(fd)
    line = _run_child(_child_source(chosen), env=env, pass_fds=(fd,))
    parts = line.split()

    assert len(parts) == 3 and parts[0] == _READY, (
        "the child announced {!r} where '{} <digest> <verdict>' was expected, "
        "so nothing below can be read as a report about the payload".format(
            line or "<nothing>", _READY
        )
    )
    assert parts[1] == hashlib.sha256(
        value.encode("utf-8", "surrogateescape")
    ).hexdigest(), (
        "the child did not read the value this file published, so the "
        "verdict it reports is a statement about nothing"
    )
    assert parts[2] == _CLEAN, (
        "a child holding the secret found {} of it in its own initial "
        "environment. The handoff is a descriptor, and a descriptor number "
        "in a variable is not the value.".format(parts[2])
    )


def test_a_child_handed_the_value_through_the_environment_is_caught(published):
    """The control for the case above, and the reason it can be believed.

    The value is put in the child's own initial environment under the
    very variable this project is removing — the exact shape of the leak
    being ruled out, produced deliberately — and the child's own sweep has
    to find it.

    If this case ever fails, the sweep in the child is not reading the
    environment it was started with, and every "the value is absent" claim
    in this file is answering a question about nothing. That is the
    failure mode a file of negative assertions cannot see about itself,
    and it is why the control is here rather than left implicit.
    """
    value, fd = published
    fallback = credentials.SECRET_SPECS[LOGICAL_NAME].fallback_env_key

    env, chosen = child_env(fd, extra={fallback: value})
    line = _run_child(_child_source(chosen), env=env, pass_fds=(fd,))
    parts = line.split()

    assert parts[0] == _READY, "the control child never announced its report"
    assert parts[1] == hashlib.sha256(
        value.encode("utf-8", "surrogateescape")
    ).hexdigest(), (
        "the control child did not read the payload, so it is not a control "
        "on the sweep — it is a second case that happens to look for a value"
    )
    assert parts[2].startswith(_LEAKED), (
        "the child did not find a value that was in its own initial "
        "environment, so its report that the value is absent cannot be "
        "trusted to mean anything. It reported {!r}".format(parts[2] if parts else line)
    )
    assert parts[2] == "{}:{}".format(_LEAKED, fallback), (
        "the child found the planted value somewhere other than the variable "
        "it was planted in, naming {!r}. A sweep that reports a leak in the "
        "wrong place is not yet a sweep that can be trusted to report one in "
        "the right one.".format(parts[2][len(_LEAKED) + 1:])
    )


# ---------------------------------------------------------------------------
# Step 5 — the switch closed
# ---------------------------------------------------------------------------


def test_with_the_switch_closed_every_consumer_reports_disabled(
    keychain_closed, monkeypatch
):
    """Step 5: closed means every consumer goes quiet, and none of them raises.

    Four consumers, four vocabularies, one state. Telegram reports
    ``enabled`` false; the Feishu client refuses to build, by the one named
    exception its callers are documented to handle; the notifier records
    why and stays inert; and the provider itself reports ``missing`` for
    every row. A chain where three of the four go quiet and the fourth
    raises is a chain that takes the service down on the machine where
    nothing is configured, which is exactly the machine most people first
    install this on.

    "Nothing is sent" is asserted rather than assumed: the one function
    both Telegram entry points funnel into is replaced with a stand-in that
    fails the case if it is reached, so the short-circuit is checked as a
    short-circuit and not merely inferred from a ``None`` return. The
    notifier's own counters are read for the same reason on the Feishu
    side.

    The reason the notifier recorded is required to be a non-empty string.
    A boolean alone is the same value an operator sees when a channel is
    switched off on purpose, and the difference between "I cannot notify
    because nobody configured this" and "I cannot notify because
    something is broken" is the one they cannot otherwise tell.
    """
    from notifications import telegram_client
    from notifications.feishu_client import FeishuClient, FeishuUnavailable
    from notifications.feishu_notifier import FeishuNotifier

    assert credentials.keychain_disabled() is True, (
        "the switch was set to a disabling value and this process is on a "
        "platform the case patched to macOS, so the keychain must be off"
    )

    def _unexpected_post(*args, **kwargs):
        pytest.fail(
            "the Telegram transport reached the network with the switch "
            "closed, so a disabled channel is still sending"
        )

    monkeypatch.setattr(telegram_client, "_post", _unexpected_post)

    for name in CONSUMER_NAMES:
        assert credentials.secret_source(name) == credentials.SOURCE_MISSING, (
            "{} is reported as coming from {} with the switch closed and no "
            "fallback exported".format(name, credentials.secret_source(name))
        )
        assert credentials.read_secret(name) is None
        assert credentials.secret_available(name) is False

    config = telegram_client.load_telegram_config()
    assert config["enabled"] is False, (
        "the Telegram transport reports the channel configured with the "
        "switch closed: {!r}".format(
            {k: v for k, v in config.items() if k != "bot_token"}
        )
    )
    assert config["bot_token"] is None, (
        "the Telegram transport is holding a token the provider says it has "
        "no value for — a second source, and therefore a secret this "
        "deployment never asked to have"
    )
    assert telegram_client.send_to_telegram("ping", config) is None

    with pytest.raises(FeishuUnavailable):
        FeishuClient()

    with _Notified(FeishuNotifier()) as notifier:
        notifier.start()
        stats = notifier.stats()
    assert stats["enabled"] is False, (
        "the notifier reports itself configured with the switch closed"
    )
    assert stats["telegram_enabled"] is False
    reason = stats["disabled_reason"]
    assert isinstance(reason, str) and reason.strip(), (
        "the notifier is disabled but recorded no reason, which leaves an "
        "operator with a card that stopped moving and no way to learn why"
    )
    assert stats["pushes_ok"] == 0 and stats["pushes_failed"] == 0, (
        "the notifier counted a push on a deployment with no credential"
    )


def test_with_the_switch_closed_the_cli_says_the_deployment_is_not_configured(
    keychain_closed,
):
    """Step 5, the operator's view of it: the command says so, and says nothing else.

    ``secrets verify`` is run as a process rather than called as a
    function, for the same reason its sibling files run it that way: it is
    the interface an operator actually uses, its resolution memo starts
    empty, and it resolves ``$HOME`` in a process that inherited nothing
    from this one.

    What is asserted is the behaviour, not a word. The command has no
    ``DISABLED`` token — its vocabulary is a source label and an exit code
    — so the case asks for the two things that token would have stood for:
    no row claims the keychain, and every row says the secret is not
    resolvable. The exit code is checked as well but is explicitly not the
    evidence, because on a platform with no keychain tool the command is
    nonzero for a machine fact as well; the rows are what separate the two,
    and a reader who took the code for the claim would be reading the
    platform.

    The value is checked for absence in the output. The command reports
    where a secret is read from, and a run that printed it would be
    publishing a credential into the log this failure is read from.
    """
    completed = subprocess.run(
        [sys.executable, str(_BACKEND_DIR / "cli.py"), "secrets", "verify"],
        cwd=str(_BACKEND_DIR),
        env=keychain_closed.env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_CLI_TIMEOUT_SECONDS,
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")

    assert "source=keychain" not in stdout, (
        "the command reported a keychain source with the switch closed, so the "
        "operator would be told to look at a facility this deployment is not "
        "using.\nstdout:\n{}".format(stdout)
    )

    for name in CONSUMER_NAMES:
        rows = [row for row in stdout.splitlines() if row.split()[:1] == [name]]
        assert len(rows) == 1, (
            "`secrets verify` printed {} row(s) for {}, where exactly one is "
            "what a configured table produces.\nstdout:\n{}".format(
                len(rows), name, stdout
            )
        )
        assert "source=missing" in rows[0], (
            "`secrets verify` did not report {} as unresolvable with the switch "
            "closed and no fallback exported:\n{!r}".format(name, rows[0])
        )

    assert completed.returncode != 0, (
        "the command exited 0 on a deployment where no secret resolves. Its "
        "own verdict is part of what this case checks, even though the rows "
        "above are the evidence that the *switch* is what turned it off.\n"
        "stdout:\n{}stderr:\n{}".format(stdout, stderr)
    )

    for name in CONSUMER_NAMES:
        value = keychain_closed.value(name)
        assert value not in stdout, (
            "`secrets verify` printed the value of {}. The command reports "
            "where a secret is read from, never the secret.".format(name)
        )


# ---------------------------------------------------------------------------
# Step 6 — the skip-proof
# ---------------------------------------------------------------------------
#
# The two cases below read a keychain through the real platform tool, with
# nothing standing in for it. That is the only part of this file that needs
# a machine with a keychain, and it is the part whose claim is about the
# machine: a tool that cannot be started here cannot answer whether the
# real command line works, and a case that failed there would be reporting
# a defect in code it never reached.
#
# So the gate has two positions. The default is a skip that names what was
# refused; the strict position is a failure, because a lane that asked for
# the real run and received skips has published no evidence while
# reporting green. The demonstration at the bottom drives a refusal itself,
# so "the lane cannot report green by skipping" is observed on every
# machine rather than on the rare one carrying an endpoint-security
# product.


def _tool_is_runnable(path: str) -> tuple:
    """``(runnable, reason)`` for ``path``, by starting it.

    Starting the binary is the probe rather than ``os.access``, because
    the question is whether the kernel will ``execve`` it, and an
    ``access(2)`` answer is not that: a policy that refuses the execution
    leaves the file's mode intact, so a mode-based check reports a
    capability the machine does not have.

    The verdicts are kept apart because a skip message has to tell them
    apart too — absent, present but refused, and runnable all look like
    "no" to a boolean, and only one of them is something a reader can fix
    by changing a permission.
    """
    try:
        completed = subprocess.run(
            [path, "-h"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except FileNotFoundError:
        return False, "{} does not exist on this machine".format(path)
    except OSError as exc:
        return False, "{} cannot be started here: {}".format(
            path, exc.strerror or exc
        )
    except subprocess.TimeoutExpired:
        # It started and then failed to answer. That is not an inability
        # to run it, and treating it as one would skip a machine whose
        # tool is merely slow — which the cases below would then report
        # as a pass over a timeout.
        return True, "{} started but did not answer -h within 30s".format(path)
    return True, "{} started (exit {} for -h)".format(path, completed.returncode)


#: The refusal this machine's probe found, evaluated once at import. A
#: policy that lifts mid-suite is not a thing this file can act on, and
#: the probe starts a process each time it is asked.
_UNRUNNABLE = [
    reason
    for path in (_REAL_SECURITY_BIN,)
    for runnable, reason in [_tool_is_runnable(path)]
    if not runnable
]

#: The account-specific cases, each with the gated binary that case is
#: about. This mapping is the whole list of them: the gate reads each
#: case's entry out of it, the structural check below walks it, and a case
#: added without an entry is a case no gate can reason about.
_ACCOUNT_SPECIFIC_CASES: Mapping[str, tuple] = MappingProxyType(
    {
        "test_the_real_tool_returns_the_item_it_was_asked_for": (_REAL_SECURITY_BIN,),
        "test_the_real_lookup_is_asked_for_the_account_alone": (_REAL_SECURITY_BIN,),
    }
)


def _capability_reason(unrunnable=None) -> str:
    """Why this machine cannot run the account-specific cases, or ``""``.

    ``unrunnable`` is a parameter so a check can ask what the reason
    *would* be for a refusal this machine does not have, which is the only
    way to test the skip text without waiting for a policy to be installed
    somewhere.
    """
    if unrunnable is None:
        unrunnable = _UNRUNNABLE
    if not unrunnable:
        return ""
    return (
        "this machine will not start {} so the real keychain read cannot "
        "happen here: {}. Nothing in these cases is replaced, so a machine "
        "that refuses the tool cannot answer the question either way; the "
        "run is reported as not-run rather than as a failure in the code, "
        "which was never reached.".format(_REAL_SECURITY_BIN, "; ".join(unrunnable))
    )


def _strict_reason(unrunnable) -> str:
    """The failure a strict-position run carries.

    The same evidence as the skip reason and the same consequence for the
    reader — which binary, and what the kernel said about it — with one
    thing changed: it is a failure rather than a skip, because the switch
    said the real run was required and a machine that cannot perform it has
    not satisfied that.

    The switch is named in the message. A lane that turns red on this has
    to be able to connect the failure to its own configuration, and a
    message that only said "the tool would not start" would leave the
    reader wondering which of the two positions they were in.
    """
    return (
        "{} is set, so the real keychain read was required to run rather than "
        "be reported as not-run, and this machine cannot run it: {}. Nothing in "
        "these cases is replaced — {} is the binary production reads through — "
        "so a machine that refuses it cannot answer the question either way, "
        "and the skip that would otherwise have covered this is the failure it "
        "was always underneath.".format(
            _STRICT_SWITCH_ENV, "; ".join(unrunnable), _REAL_SECURITY_BIN
        )
    )


def _strict_capability_gate(unrunnable):
    """A decorator that fails at call time, naming the tool it could not start.

    A *call-time* failure rather than an import-time one. Raising during
    import would abort collection of the whole file, so the account-specific
    cases would be reported as one collection error and the cases that
    check the gate itself — precisely the ones that would have told a
    reader the gate was stuck — would be gone too.

    The wrapper drops the signature rather than passing the call through.
    The gated cases take fixtures — a real keychain, a redirected home —
    and on a machine that cannot start the tool those fixtures would each
    fail during setup, which pytest reports as an *error* on the case. An
    error reads as a defect in the harness, and this file has no harness
    defect to report: it has a machine that would not ``execve`` a binary.

    The wrapper also tags what it produced, so the structural check can
    recognise this gate at all — see :data:`_CAPABILITY_GATE_ATTR`.
    """

    def decorate(func):
        @functools.wraps(func)
        def gated(*args, **kwargs):
            pytest.fail(_strict_reason(unrunnable), pytrace=False)

        # An empty signature is what stops pytest setting the case's
        # fixtures up. ``functools.wraps`` would otherwise advertise them.
        gated.__signature__ = inspect.Signature()
        setattr(gated, _CAPABILITY_GATE_ATTR, "strict")
        return gated

    return decorate


def _capability_gate(unrunnable, strict: bool):
    """The gate for one position: a skip by default, a failure when strict.

    Split out from the module-level decorator so both positions can be
    constructed and inspected on any machine. A gate built only where the
    tool happens to be runnable has one reachable position, and the other
    is untested on every machine that is not the one the strict position
    was written for.
    """
    if strict and unrunnable:
        return _strict_capability_gate(unrunnable)
    return pytest.mark.skipif(bool(unrunnable), reason=_capability_reason(unrunnable))


#: The gate, as a decorator. Separate from the platform mark below so the
#: cases that check the gate itself are not themselves gated by it — a gate
#: that cannot be examined because it is what examines everything else is a
#: gate nobody can tell is stuck.
account_specific = _capability_gate(_UNRUNNABLE, require_real_tool_requested())

#: A keychain is a macOS facility, and off macOS the read answers "no
#: value" by design, so a case here would be testing the skip reason. The
#: mark is a decorator rather than part of the module's ``pytestmark``
#: because the rest of this file is not macOS-bound and a module-level
#: platform mark would take the whole walk with it.
_requires_macos_keychain = pytest.mark.skipif(
    sys.platform != "darwin",
    reason=(
        "this case reads a real keychain through /usr/bin/security, which is a "
        "macOS facility; off macOS the read answers 'no value' by design. The "
        "rest of this file walks the same chain against a stand-in tool and "
        "runs on both platforms"
    ),
)


# ---------------------------------------------------------------------------
# Running the real tool
# ---------------------------------------------------------------------------


def _security(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run the platform keychain tool and return what it did.

    The binary is read from the provider's own constant at call time and
    never pointed at anything else: the claim these two cases make is
    about the real read, and a stand-in would turn them into a second copy
    of steps 1 and 2.

    Failures raise rather than return, because here a nonzero exit has
    exactly one meaning: the case cannot say what it came to say. Silence
    would turn a broken read into a negative assertion — a check that found
    nothing because it never asked.
    """
    argv = [_REAL_SECURITY_BIN, *args]
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except OSError as exc:
        pytest.fail(
            "{} could not be run: {}\nThese cases replace nothing — the read "
            "under test is the one production runs. A machine where that "
            "binary cannot be executed cannot answer the question, and "
            "skipping quietly would be the one way this file could report "
            "success without evidence.".format(_REAL_SECURITY_BIN, exc)
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "{} {} did not finish within 60s. A keychain that blocks here is "
            "usually waiting on an unlock prompt this machine has no way to "
            "show.".format(_REAL_SECURITY_BIN, args[0] if args else "")
        )
    if check and completed.returncode != 0:
        pytest.fail(
            "{} {} exited {}\nstderr: {}\nstdout: {}".format(
                _REAL_SECURITY_BIN,
                args[0] if args else "",
                completed.returncode,
                completed.stderr.decode("utf-8", "replace").strip(),
                completed.stdout.decode("utf-8", "replace").strip(),
            )
        )
    return completed


def _assert_disposable(home: Path, keychain: Path) -> None:
    """Fail rather than destroy a keychain that is not this file's own.

    Every keychain created here is created to be destroyed, and the only
    reason that is true is that ``home`` is a directory under ``tmp_path``.
    The check is made here, against the home captured at import, because the
    cost of being wrong is now unrecoverable: ``credentials._KEYCHAIN_PATH``
    names this project's dedicated keychain, the container that holds the
    real credentials, and no code in this project can put an item back into
    it once the file is gone.

    Two ways to be wrong, both refused. A home that *is* the real one, and a
    home that is disposable while the keychain resolves somewhere under the
    real home — the second being what a half-applied redirect looks like.
    """
    if home == _REAL_HOME:
        pytest.fail(
            "refusing to delete {}: its home directory is this machine's "
            "own, so the keychain is not one this file created. The login "
            "keychain used to be rebuilt on demand; this project's dedicated "
            "keychain is not, and the credentials it holds cannot be "
            "restored from here.".format(keychain)
        )
    if _REAL_HOME in keychain.parents:
        pytest.fail(
            "refusing to delete {}: it resolves inside this machine's home "
            "directory ({}), so it is not a keychain this file created. A "
            "disposable home of {} does not make a real keychain "
            "disposable.".format(keychain, _REAL_HOME, home)
        )


@contextmanager
def _real_keychain(home: Path) -> Iterator[Path]:
    """Yield a real, empty keychain at the path a redirected ``$HOME`` implies.

    Named by ``credentials._keychain_file`` rather than invented here: the
    point of redirecting the home directory is that the provider resolves
    the same path this case created, with its own resolution and its own
    constant, so the read cannot quietly point somewhere else.

    Created with no password, which is a real keychain in its unlocked
    state — a headless runner cannot answer an unlock prompt, and a prompt
    here would be a hang rather than a failure.

    Destroyed in a ``finally`` with ``check=False``: cleanup runs while an
    exception is propagating, and a cleanup that raised would replace the
    assertion that fired with a message about the cleanup, which is the
    one failure this file must never misreport.
    """
    keychain = home / credentials._KEYCHAIN_PATH
    keychain.parent.mkdir(parents=True, exist_ok=True)
    _security("create-keychain", str(keychain))
    if not keychain.exists():
        pytest.fail(
            "{} reported success but {} does not exist, so the provider's "
            "read would resolve a path this case never created".format(
                _REAL_SECURITY_BIN, keychain
            )
        )
    try:
        yield keychain
    finally:
        _assert_disposable(home, keychain)
        _security("delete-keychain", str(keychain), check=False)


@pytest.fixture
def real_deployment(tmp_path, monkeypatch, canary):
    """A deployment whose secrets really live in a real, temporary keychain.

    Redirects what the provider reads and touches nothing else. The
    platform is *not* patched: these cases are macOS-gated, so
    ``sys.platform`` already says what the read needs it to say, and
    patching it here would make the one claim this file makes about these
    two cases — that nothing is replaced — false in the very place the
    claim is made.
    """
    home = tmp_path / "home"
    (home / "Library" / "Keychains").mkdir(parents=True)

    accounts = {
        name: canary(_canary_kind(name, "index"), tmp_path) for name in CONSUMER_NAMES
    }
    values = {
        name: canary(_canary_kind(name, "secret"), tmp_path) for name in CONSUMER_NAMES
    }

    with _real_keychain(home) as keychain:
        for name, account in accounts.items():
            _security(
                "add-generic-password",
                "-a",
                account,
                "-s",
                "pdt-acceptance-item",
                "-w",
                values[name],
                str(keychain),
            )

        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv(credentials._SWITCH_ENV_KEY, "0")
        for name, account in accounts.items():
            monkeypatch.setenv(
                credentials.SECRET_SPECS[name].account_env_key, account
            )
            monkeypatch.delenv(
                credentials.SECRET_SPECS[name].fallback_env_key, raising=False
            )
        credentials.reset_cache()

        assert credentials.keychain_disabled() is False, (
            "the switch is on and the platform is macOS, so the keychain must "
            "not be reported as unavailable — if it is, nothing below is "
            "testing what these cases claim to test"
        )
        yield accounts, values

    credentials.reset_cache()


# ---------------------------------------------------------------------------
# The guard on the delete
# ---------------------------------------------------------------------------
#
# ``_real_keychain`` destroys what it creates, and the only reason that is
# safe is that ``home`` is a directory under ``tmp_path``. That assumption
# used to be cheap to get wrong: the path resolved to the login keychain,
# which macOS rebuilds on demand. ``credentials._KEYCHAIN_PATH`` now names
# this project's dedicated keychain — the container holding the real
# credentials — and nothing in this project can put an item back into it
# once the file is gone. So the delete checks first, against a home captured
# at import rather than read at teardown.


def test_the_delete_guard_refuses_the_real_home_directory():
    """A keychain under the real home is refused, whatever else is true."""
    with pytest.raises(pytest.fail.Exception, match="delete"):
        _assert_disposable(_REAL_HOME, _REAL_HOME / credentials._KEYCHAIN_PATH)


def test_the_delete_guard_refuses_a_real_home_with_a_redirected_file(tmp_path):
    """A disposable home does not make a real-home keychain disposable."""
    with pytest.raises(pytest.fail.Exception, match="delete"):
        _assert_disposable(
            tmp_path / "home",
            _REAL_HOME / "Library" / "Keychains" / "runtime-secrets.keychain-db",
        )


def test_the_delete_guard_allows_a_keychain_under_the_temporary_directory(tmp_path):
    """The case this file actually runs is allowed through."""
    _assert_disposable(
        tmp_path / "home", tmp_path / "home" / credentials._KEYCHAIN_PATH
    )


def test_the_cleanup_checks_disposability_before_it_deletes(tmp_path, monkeypatch):
    """The guard is on the path out, not merely defined somewhere nearby.

    Driven with the keychain tool replaced by a recorder, so the ordering and
    the wiring are asserted without creating a keychain: the guard has to
    run before the delete, and a delete that ran first could not be taken
    back.
    """
    order = []

    def _fake_security(*args, **kwargs):
        order.append(args[0])
        if args[0] == "create-keychain":
            Path(args[1]).parent.mkdir(parents=True, exist_ok=True)
            Path(args[1]).touch()
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(sys.modules[__name__], "_security", _fake_security)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_assert_disposable",
        lambda home, keychain: order.append("guard"),
    )

    with _real_keychain(tmp_path / "home"):
        pass

    assert order == ["create-keychain", "guard", "delete-keychain"], (
        "the cleanup must check that the keychain is disposable and then "
        "delete it, in that order. Got: {}".format(order)
    )


def test_a_refused_keychain_is_not_deleted(monkeypatch):
    """When the guard refuses, the delete does not happen at all."""
    deleted = []

    def _fake_security(*args, **kwargs):
        if args[0] == "delete-keychain":
            deleted.append(args[1])
        if args[0] == "create-keychain":
            Path(args[1]).parent.mkdir(parents=True, exist_ok=True)
            Path(args[1]).touch()
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(sys.modules[__name__], "_security", _fake_security)

    with pytest.raises(pytest.fail.Exception, match="delete"):
        with _real_keychain(_REAL_HOME / "not-a-redirect"):
            pass

    assert deleted == [], (
        "the guard refused, so nothing may have been deleted. Deleted: "
        "{}".format(deleted)
    )


@_requires_macos_keychain
@account_specific
def test_the_real_tool_returns_the_item_it_was_asked_for(real_deployment):
    """Step 6's un-mocked half: the real tool hands the value back.

    Steps 1 and 2 walked this chain against a stand-in. This case asks the
    question that stand-in cannot answer — whether the command line the
    provider builds is a command line the platform's own tool accepts —
    with ``credentials._SECURITY_BIN`` read rather than replaced and a
    keychain created, written and destroyed by that tool.

    The item is filed under a service name, and found without one. That is
    not a contradiction: ``add-generic-password`` requires a service, and
    the provider's lookup passes no ``-s``, so this case is the one place
    where the two halves of the same item meet.
    """
    accounts, values = real_deployment

    for name in CONSUMER_NAMES:
        assert credentials.secret_source(name) == credentials.SOURCE_KEYCHAIN, (
            "{} did not resolve from the real keychain".format(name)
        )
        assert credentials.read_secret(name) == values[name], (
            "the real tool returned a different value than the one this case "
            "filed for {} — a read that answers something other than what was "
            "asked for".format(name)
        )


@_requires_macos_keychain
@account_specific
def test_the_real_lookup_is_asked_for_the_account_alone(real_deployment):
    """Step 6's other half: the tool is asked for the account and no service.

    The same assertion step 2 makes, made here against a tool that has not
    been written by this repository. A command line that works against a
    stand-in it was written for and fails against the real tool is exactly
    the failure a stand-in cannot produce, and that is the reason this
    case exists rather than a second reading of step 2.
    """
    accounts, values = real_deployment

    captured = []
    # Bound before the patch, because the patch replaces the name this
    # lookup would otherwise go through — and the whole point of the case
    # is that the real tool still does the work. Only the command line is
    # copied out; the read itself is production's, unchanged.
    real_run = credentials._run_security

    def _record(argv, timeout):
        captured.append(list(argv))
        return real_run(argv, timeout)

    with unittest.mock.patch.object(
        credentials, "_run_security", side_effect=_record
    ):
        credentials.reset_cache()
        for name in CONSUMER_NAMES:
            assert credentials.read_secret(name) == values[name]

    argv = [argument for command in captured for argument in command]
    assert argv, "the provider resolved every secret without starting the tool"
    for name in CONSUMER_NAMES:
        assert accounts[name] in argv, (
            "the account for {} never reached the real tool".format(name)
        )
    assert "-s" not in argv, (
        "the real command line carries a service name.\nargv: {!r}".format(argv)
    )


# ---------------------------------------------------------------------------
# The two positions, told apart without waiting for a policy
# ---------------------------------------------------------------------------


def _refused_tool(tmp_path: Path) -> tuple:
    """A path this machine genuinely refuses to start, and the probe's verdict.

    A file in this test's own ``tmp_path`` with no execute bit. That is
    not a string written into a test — it is a real ``EACCES`` from the
    kernel, produced by the same probe the file's own gate uses, which is
    the only way to watch the gate work on a machine that starts
    everything.

    No assertion in this file starts a system binary to make one refuse.
    """
    refused = tmp_path / "refused-by-policy"
    refused.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    refused.chmod(0o644)
    path = str(refused)
    _runnable, reason = _tool_is_runnable(path)
    assert not _runnable, (
        "{} started on this machine, so there is no refusal to drive the gate "
        "with. The probe and this case disagree.".format(path)
    )
    return path, reason


def _report_counts(output: str) -> dict:
    """The per-outcome counts pytest printed in its summary line.

    Read from the summary rather than from a parsed report object, because
    the claim under test is the one a CI job reads: what a human sees when
    a lane goes red, and whether a line says "skipped" or "failed".
    """
    counts = {}
    for kind in ("failed", "passed", "skipped", "error", "errors"):
        for match in re.findall(r"(\d+) {}".format(kind), output):
            counts[kind] = int(match)
    return counts


def _run_gated_module(tmp_path: Path, *, unrunnable, strict: bool, platform="darwin") -> str:
    """Run this file's account-specific cases in one gate position, in a child pytest.

    A real child process rather than an in-process ``pytest.main``:
    re-entering pytest from inside a test leaves plugin state and collection
    caches installed for the rest of the session, and the thing being asked
    is a question about a *run*. The child gets its own rootdir and a file
    of its own, so the only thing it shares with this process is the module
    under test.

    The generated module carries no fixtures, which is why a refusal passed
    in here has to be one the gate cannot place against a gated path:
    every case stays gated, so none of them needs one. That is not a
    limitation of the strict position — the gate judges a case by the
    tools it declares, and both of these declare the one tool — and it is
    also why this file's account-specific cases all depend on the same
    binary.

    The child is reaped by ``subprocess.run`` before this returns, so
    nothing it started outlives the case.
    """
    e2e_dir = Path(__file__).resolve().parent
    source = (
        "import sys\n"
        "import sysconfig, zoneinfo\n"
        "\n"
        "# Both of these initialise lazily, from the *build* platform's config\n"
        "# module, and a flipped ``sys.platform`` makes them look for a config\n"
        "# module that was never built for this interpreter — so the child would\n"
        "# die importing the module under test and report a collection error\n"
        "# instead of the run being asked about. Forced here, while the platform\n"
        "# is still the real one, so the flip below is the only thing that is\n"
        "# untrue about this process.\n"
        "sysconfig.get_config_vars()\n"
        "zoneinfo.ZoneInfo('UTC')\n"
        "\n"
        "sys.platform = {platform!r}\n"
        "sys.path[:0] = [{backend!r}, {e2e!r}]\n"
        "import pytest\n"
        "import test_credential_chain_acceptance as m\n"
        "\n"
        "pytestmark = m.pytestmark\n"
        "\n"
        "_gate = m._capability_gate({unrunnable!r}, {strict!r})\n"
        "for _name in m._ACCOUNT_SPECIFIC_CASES:\n"
        "    globals()[_name] = _gate(getattr(m, _name))\n"
    ).format(
        platform=platform,
        backend=str(_BACKEND_DIR),
        e2e=str(e2e_dir),
        unrunnable=list(unrunnable),
        strict=strict,
    )
    module = tmp_path / "test_gate_position.py"
    module.write_text(source, encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(module),
            "-q",
            "--tb=line",
            "-rs",
            "-p",
            "no:cacheprovider",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=_CHILD_TIMEOUT_SECONDS,
        cwd=str(tmp_path),
    )
    return completed.stdout.decode("utf-8", "replace")


@_requires_macos_keychain
def test_the_strict_switch_turns_the_account_specific_skip_into_a_failure(tmp_path):
    """Step 6: the lane cannot report green by skipping.

    Both positions of the same gate, on the same machine, on a refusal this
    case supplies. The default reports the two account-specific cases as
    skipped; the strict switch reports the same two as failed, and the
    counts are read out of two real child pytest runs rather than asserted
    over a mark — "carries a mark whose condition is true" and "was
    reported as a failure" are the same statement until pytest has acted
    on it, and the entire point of the strict position is what pytest
    reports.

    The refusal is a file with no execute bit, so the evidence the
    failures quote is a real errno the kernel returned. A case that
    waited for a machine whose operating-system policy refuses the keychain
    tool would be a case that never runs on almost any of them, and the
    property it is here to establish would go unwitnessed.
    """
    path, reason = _refused_tool(tmp_path)
    expected = len(_ACCOUNT_SPECIFIC_CASES)

    default = _report_counts(
        _run_gated_module(tmp_path, unrunnable=[reason], strict=False)
    )
    assert default.get("skipped", 0) == expected, (
        "the default position did not skip the {} account-specific case(s) on "
        "a machine that cannot start the tool. A case that fails there is "
        "reporting a defect in code it never reached.\n{}".format(expected, default)
    )
    assert default.get("failed", 0) == 0, (
        "the default position turned a machine's refusal into a failure. The "
        "developer's laptop is not the machine the acceptance is about, and a "
        "red item about a policy on it teaches a reader to pass -k past the "
        "cases they most need.\n{}".format(default)
    )

    strict = _report_counts(
        _run_gated_module(tmp_path, unrunnable=[reason], strict=True)
    )
    assert strict.get("skipped", 0) == 0, (
        "{} is set and something was still skipped. A switch that says 'the "
        "real run was required' and then skips is the failure the position "
        "exists to prevent, and it is the one a job cannot see.\n{}".format(
            _STRICT_SWITCH_ENV, strict
        )
    )
    assert strict.get("failed", 0) == expected, (
        "expected all {} account-specific cases to be reported as failures "
        "under the strict position.\n{}".format(expected, strict)
    )
    assert strict.get("error", 0) == 0 and strict.get("errors", 0) == 0, (
        "the strict position has to fail at call time. A failure raised while "
        "the item is being collected or set up is reported as an error, which "
        "reads as a defect in the harness rather than as a machine that cannot "
        "answer the question — and this file has no harness defect to report, "
        "it has a machine that refused a binary.\n{}".format(strict)
    )
    assert path in reason, (
        "the refusal does not name the path the probe refused, so the failure "
        "a reader sees could not be traced back to it.\nreason: {!r}".format(reason)
    )


@_requires_macos_keychain
def test_every_account_specific_case_declares_the_tool_it_starts():
    """The gate can only be reasoned about if every case it covers declares itself.

    A case added to the table with a name no longer in the file, or added
    to the file with no entry in the table, is a case the gate cannot
    judge: in the first direction the gate silently covers nothing, and in
    the second it covers a case it has no opinion about. Walking both
    directions is what makes the table a contract rather than a list.

    The second direction also catches a case that declares a tool this
    file never runs, which is a gate keyed on something that cannot
    answer the question it was written to ask.
    """
    missing = [
        name for name in _ACCOUNT_SPECIFIC_CASES if not hasattr(sys.modules[__name__], name)
    ]
    assert not missing, (
        "the gate's table names case(s) this file does not define, so the "
        "gate covers nothing: {}".format(missing)
    )

    for name, tools in _ACCOUNT_SPECIFIC_CASES.items():
        case = getattr(sys.modules[__name__], name)
        assert callable(case), "{} is not a case".format(name)
        for tool in tools:
            assert tool in (_REAL_SECURITY_BIN,), (
                "{} declares {} as a gated tool, which is not a binary this "
                "file runs. A gate keyed on something the cases never start is "
                "a gate that cannot answer for them.".format(name, tool)
            )


@_requires_macos_keychain
def test_the_default_position_is_a_skip_that_names_the_refusal(tmp_path):
    """The default gate is still a ``skipif`` after all of that, and it still says why.

    A gate that fails in the strict position and quietly stops skipping in
    the default one would move the problem rather than fix it. Checked as
    carefully as the strict position for that reason, and against the
    probe's own verdict rather than a paraphrase of it: a skip that does
    not say what it skipped *for* is not actionable, and one that says
    something other than what the probe found is worse than no reason.
    """
    path, reason = _refused_tool(tmp_path)

    gate = _capability_gate([reason], False)
    mark = getattr(gate, "mark", None)
    assert mark is not None and mark.name == "skipif", (
        "the default position is no longer a skipif: {!r}".format(gate)
    )
    assert mark.args == (True,), (
        "the default position's skipif condition is {!r} where True was "
        "required, so a machine that cannot start {} would not skip".format(
            mark.args, path
        )
    )
    assert path in mark.kwargs["reason"], (
        "the skip reason does not name the tool that could not be started.\n"
        "reason: {}".format(mark.kwargs["reason"])
    )
    assert reason in mark.kwargs["reason"], (
        "the skip reason does not carry the probe's own verdict, so a reader "
        "cannot tell a policy refusal from a missing file.\nprobe said: "
        "{!r}\nreason: {}".format(reason, mark.kwargs["reason"])
    )


# ---------------------------------------------------------------------------
# The citation the skip-proof owes
# ---------------------------------------------------------------------------
#
# Step 6 is a port, not an invention. The refusal-driven demonstration —
# a real file with no execute bit, the gate built over the probe's own
# verdict of it, and the two positions' counts read out of a child pytest —
# was written in the whole-delivery macOS file, and this file carries its
# own copy of it. A copy that does not say whose it is stops being a
# reading of anything: the next reader cannot tell a construction that was
# adopted deliberately from one that drifted in, and when the original
# changes, nothing points at the thing that now disagrees with it.


def _docstring_section(heading: str) -> str:
    """The block of this module's docstring filed under ``heading``.

    Read from the docstring as written rather than from a copy of the text
    held beside it, so a section that is renamed or dropped is reported as
    missing instead of being quietly answered by the thing that was
    supposed to be checking it. The underline of ``.rst`` headings is what
    terminates each block, which is also what makes the walk above the
    body-terminating one unambiguous.
    """
    lines = (sys.modules[__name__].__doc__ or "").splitlines()

    def is_underline(text: str) -> bool:
        stripped = text.strip()
        return bool(stripped) and set(stripped) == {"-"}

    for index in range(len(lines) - 1):
        if lines[index].strip() != heading or not is_underline(lines[index + 1]):
            continue
        body = []
        for cursor in range(index + 2, len(lines)):
            following = lines[cursor + 1] if cursor + 1 < len(lines) else ""
            if body and is_underline(following):
                break
            body.append(lines[cursor])
        return "\n".join(body).strip()
    return ""


#: Where the demonstration came from, resolved through this file rather
#: than written out: the repository is checked out at a different path on
#: every machine, and this one is read as source.
_SKIP_PROOF_SOURCE = (
    _BACKEND_DIR / "tests" / "e2e" / "test_keychain_full_delivery_macos.py"
)

#: The two pieces of it this file actually ports. Checked for rather than
#: assumed, so a citation that has stopped resolving is reported as a
#: citation pointing at nothing — which is the same shape of defect, in a
#: pointer, that the section it documents would otherwise hide.
_PORTED_HELPERS = ("_refused_tool", "_run_gated_module")


def test_the_skip_proof_cites_the_file_the_refusal_construction_came_from():
    """The section that runs the demonstration says whose demonstration it is.

    Not platform-bound, and deliberately so: this reads prose and a source
    file rather than the platform's keychain, so it answers on the Linux
    runner the same as on this one. It is the one claim in the file that is
    about the file rather than the chain, and it is here because every
    other section's provenance is already written down — the layers this
    walk composes are each named in "Why this file exists" — and this one
    was not.

    The second half resolves the citation. Naming a file is cheap, and a
    name that has stopped holding the construction it is cited for is
    worse than no name: it tells a reader the port has an upstream when
    the upstream has moved on.
    """
    section = _docstring_section("The skip-proof")
    assert section, (
        "this module's docstring has no section headed 'The skip-proof', so the "
        "two positions of the capability gate are described somewhere this "
        "case cannot read"
    )
    assert _SKIP_PROOF_SOURCE.name in section, (
        "the section describing the refusal-driven demonstration does not name "
        "the file it was ported from.\nThe construction originates in {}. A "
        "section that uses it without saying so leaves a reader unable to tell "
        "a demonstration adopted deliberately from one that drifted in, and "
        "leaves nothing pointing at the file to re-check when it changes.".format(
            _SKIP_PROOF_SOURCE.name
        )
    )

    source = _SKIP_PROOF_SOURCE.read_text(encoding="utf-8")
    for helper in _PORTED_HELPERS:
        assert "def {}(".format(helper) in source, (
            "{} no longer defines {}(), so this file's skip-proof cites a file "
            "that no longer holds the construction it is citing. Re-point it at "
            "wherever the refusal-driven demonstration now lives.".format(
                _SKIP_PROOF_SOURCE.name, helper
            )
        )
