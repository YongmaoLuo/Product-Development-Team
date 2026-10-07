"""Provider secrets: one place that decides where a secret is read from.

Why this module exists
----------------------
Two of the transports this project can notify through authenticate
with a secret, and each of those secrets has exactly one safe answer
per deployment. On a workstation the secret belongs in the operating
system keychain: an environment variable is readable by every process
of every user on the machine, and it survives into shell history,
crash reports and process listings. On a CI runner, in a container, on
Linux, there is no keychain at all, and the environment is the only
place a secret can come from.

Reading that decision inline at each call site is how the two answers
drift apart — one transport gets a keychain lookup, the other keeps
reading a plaintext variable, and a deployment ends up half-migrated
with no way to tell from the outside. This module makes the decision
once, in one table and one switch, and gives the rest of the codebase
three questions to ask: is this secret configured (``secret_available``),
where does it come from (``secret_source``), and what is its value
(``read_secret``).

The spec table
--------------
Each row names two environment variables and nothing else:

``fallback_env_key``
    holds the secret itself, and is read when the keychain is off.

``account_env_key``
    holds the *index* the keychain item is looked up by — the app id,
    the chat id. An index is not a secret, which is the whole reason
    the keychain is reachable at all: the environment keeps the key
    that names the item, the keychain keeps the value.

There is deliberately no ``service`` field. A keychain service name
is a per-deployment layout choice, and a table of them in source
compiles one installation's keychain into everybody's checkout. The
two rows below are properties of *this* project's providers; the
layout of a keychain is a property of the machine.

The switch
----------
``PDT_DISABLE_KEYCHAIN_SECRETS`` decides whether the keychain is
consulted. It is **fail-closed**: only the two spellings below mean
"do not disable", and everything else — unset, empty, ``True``,
``Yes``, a stray space, a typo — leaves the keychain switched off.
The asymmetry is deliberate. Reading the wrong way means shelling out
per lookup and, when the value is not where the keychain thinks it
is, means a lookup that fails in a way the operator has to debug. The
cost of guessing wrong in the other direction is a secret that stays
in the environment a little longer, which is the state the
installation is already in.

The platform is part of the same decision and is not overridable: the
keychain is a macOS facility, so on anything else the answer is
"disabled" for every spelling of the switch.

Neither the switch nor the platform is cached. They are read on every
call, because a value bound at import time belongs to the process and
not to the environment it was started in.

The lookup
----------
An enabled keychain is read by running the platform's own tool:
``security find-generic-password -a <account> -w <keychain>``. The
account is the only lookup key the command is given. ``security`` also
accepts a *service* name, and adding one would be the wrong kind of
decision for this module to make: a service is part of one machine's
keychain layout, and a table of them in source compiles one
installation's keychain into everybody's checkout. The account is
already a value the deployment exports and already unique.

The keychain file is passed explicitly for a related reason. An
unqualified lookup searches whichever keychain the process happens to
have open, and "whichever that is" is not a fact this module should be
deciding on the operator's behalf — so the file is named, resolved
against the home directory at call time so no absolute path is carried
in source.

The keychain named is this project's own, not the login keychain. That
choice is the isolation: the login keychain holds everything the user
has ever saved, so anything able to enumerate it can enumerate these
credentials with everything else, and a lookup by account is not a
boundary. A dedicated file holds only what is filed into it. Which file
that is belongs to the installation rather than to this repository, so
``PDT_KEYCHAIN_PATH`` redirects the read for a deployment whose layout
differs; an unset variable falls back to the dedicated default.

Every way the read can fail resolves to the same answer: no value.
An item that is not in the keychain, a payload that is not valid UTF-8,
and a tool that blocks behind a locked keychain are all "the secret is
not available", and a secret that is not available is a state the
notifiers already handle. None of them raises, because the caller of
:func:`read_secret` is asking whether a transport is configured, not
requesting a parse. The timeout exists for the same reason: a locked
keychain makes the first call block on an unlock prompt, which on a
headless machine is a hang rather than an error, and a hang holds the
notification send for as long as the child lives.

Caching
-------
A resolved secret is memoised in :data:`_CACHE` for the life of the
process: it is a property of the deployment, not of the call site, and
re-resolving it per call would re-read the environment on every push
and re-run the keychain lookup with it. The memo therefore has a
lifetime the caller cannot control, so :func:`reset_cache` is public
— without it the only way to observe a changed environment would be a
restart.

Handing a secret to another process
------------------------------------
Everything above resolves a secret *here*. A caller that has to run
somewhere else — a child process, a helper, a second copy of this
program — cannot have the value in its environment, because an
environment variable is readable by every process of every user on the
machine, survives into shell history, crash reports and process
listings, and is inherited by whatever that child spawns in turn. That
is the same objection that put the secret in the keychain, and handing
it over this way would undo that in one line.

The handoff is therefore an **anonymous pipe**, with three parts:

``publish_secret_fd``
    writes ``{logical_name: value}`` as JSON into the write end of a
    fresh pipe, closes it, and returns the read end. Nothing is left
    open behind it, and nothing is left in the environment.

``secret_fd_env_var``
    returns the name of the variable that tells the child which
    descriptor to read — ``PDT_SECRET_FD_`` plus the logical name
    upper-cased. The variable's *value* is the descriptor number, which
    is an integer naming a pipe the child already inherited. It is not
    the secret, and it is not a path, and it stops being meaningful the
    moment the pipe is closed.

``read_secret_fd``
    reads the pipe to EOF, closes the descriptor, and returns the
    decoded object. One consumer per pipe: the descriptor is spent.

Why JSON, and why keyed by logical name. A ``KEY=value`` line would be
shorter, and it is wrong in three ways at once — a value containing a
newline ends the line early, a value containing ``=`` splits in the
wrong place, and neither the writer nor the reader can tell the
resulting fragment from a shorter secret. The symptom would be an
authentication error at the far end of a notification send rather than
a bug at the handoff. JSON escapes all of it, and its escaping is
already in the standard library, so the correctness is free.

The key is the **logical name** (``feishu_app_secret``), not the
environment variable that holds the plaintext fallback
(``FEISHU_APP_SECRET``). The two coexist by design: the first is what
the rest of the project passes around and is the same on every machine,
the second is a per-deployment name for a source this module exists to
move away from. A reader keyed by the logical name therefore needs to
know nothing about how the value was found.

Scope
-----
This module reads no files and opens no socket. It does start one
process — the platform's keychain tool — and that call is the only
place in the project where a secret is fetched by shelling out, which is
why its command line and its failure modes are pinned by a test that
invokes a real executable rather than a mock. It is stdlib-only, and it
imports nothing from the notifier package: a credentials lookup that
pulled in an HTTP client could not be used by the code that *populates*
the credentials.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger("credentials")

#: The system binary that reads the macOS keychain. Named once so the
#: command line a reviewer checks is the command line that runs.
_SECURITY_BIN = "/usr/bin/security"

#: This project's own keychain, relative to the user's home directory.
#: Joining it onto the home directory happens where the read happens, so
#: the constant itself is the same on every machine and no absolute path
#: is carried in source.
#:
#: Dedicated, and not the login keychain, on purpose. The login keychain
#: is where macOS keeps everything the user has ever saved, so any process
#: that can enumerate it can enumerate this project's credentials
#: alongside everybody else's — the isolation this path exists to provide
#: is a property of the container, not of the lookup. An item filed under
#: one account in a keychain of its own is visible to nothing but the
#: lookups that name it.
#:
#: The file is named rather than searched for, so a deployment that has
#: not created one gets a lookup that misses rather than a silent fall
#: through to some other keychain.
_KEYCHAIN_PATH = "Library/Keychains/runtime-secrets.keychain-db"

#: Environment variable that redirects the read to a different keychain
#: file, for an installation whose layout is not the default. Unset — and
#: empty, which is the same thing to a caller — falls back to
#: :data:`_KEYCHAIN_PATH`, so the default an operator who never sets this
#: gets is still the dedicated keychain.
_KEYCHAIN_PATH_ENV_KEY = "PDT_KEYCHAIN_PATH"

#: How long the keychain tool may take before the read gives up. A
#: locked keychain makes the first call block on an unlock prompt, and
#: on a headless machine there is no prompt at all — so without a bound
#: the call is not slow but stuck. The tool is killed and reaped by the
#: same call that observes the expiry, so the child does not outlive
#: the read.
#:
#: 60 seconds, not 5, and the difference is the whole point. A locked
#: keychain turns this read into a *human* interaction: macOS puts a
#: password dialog on screen and the value comes back only after
#: somebody types into it. Five seconds is shorter than it takes to
#: read a dialog and start typing, so the bound expires with the
#: password half-entered — the tool is killed, the read reports "no
#: value", and the notifier reports itself unconfigured. The operator
#: sees a dialog that either outlived the process that opened it or
#: closed on a correct password that no longer had a reader.
#:
#: The bound still has a job: it caps the stuck case, which is the one
#: that motivated it. Sixty seconds of a blocked startup on a headless
#: box is a nuisance; an unbounded wait is a hang with a GUI attached.
_KEYCHAIN_TIMEOUT_SECONDS = 60.0

#: The three things the lock probe can establish. They are kept apart
#: because they call for opposite responses: one is fixed in Keychain
#: Access, and the other cannot be fixed there at all — it means this
#: process is not allowed to run the tool, so the advice is "run it
#: somewhere else", not "unlock something".
KEYCHAIN_UNLOCKED = "unlocked"
KEYCHAIN_LOCKED = "locked"
KEYCHAIN_UNAVAILABLE = "unavailable"

#: The bound on the lock probe below. It asks a question the keychain
#: answers without a dialog, so it is given only enough room to start a
#: process and fail — a longer bound would extend the wait the primary
#: read already risked, on the path where somebody is already waiting.
_LOCK_PROBE_TIMEOUT_SECONDS = 10.0

#: The only two values that mean "do not disable the keychain". Every
#: other value, including an unset variable, disables it.
_SWITCH_ENV_KEY = "PDT_DISABLE_KEYCHAIN_SECRETS"
_SWITCH_ENABLING_VALUES = frozenset({"0", "false"})

#: The three labels :func:`secret_source` can return.
SOURCE_KEYCHAIN = "keychain"
SOURCE_ENVIRONMENT = "os.environ"
SOURCE_MISSING = "missing"

#: The prefix of the variable that carries a secret's descriptor number
#: to a child process. Named once so the string a reviewer checks is the
#: string that reaches a child's environment.
_FD_ENV_PREFIX = "PDT_SECRET_FD_"

#: How many bytes :func:`read_secret_fd` asks for per read. The value is
#: read to EOF, so this only decides how many syscalls a payload costs;
#: a credential fits in the first one.
_FD_READ_CHUNK = 4096


@dataclass(frozen=True)
class SecretSpec:
    """How one provider secret is named, and how it is found.

    ``logical_name`` is the key this spec is registered under and the
    only name callers pass around; the two ``*_env_key`` fields are the
    environment variables involved — one holding the secret, one
    holding the keychain index.
    """

    logical_name: str
    fallback_env_key: str
    account_env_key: str


#: Every secret this project knows how to look up. A row here is a
#: decision about which of this project's secrets may live in a
#: keychain, so the table is short on purpose and grows by review.
SECRET_SPECS: Mapping[str, SecretSpec] = MappingProxyType(
    {
        "feishu_app_secret": SecretSpec(
            logical_name="feishu_app_secret",
            fallback_env_key="FEISHU_APP_SECRET",
            account_env_key="FEISHU_APP_ID",
        ),
        "telegram_bot_token": SecretSpec(
            logical_name="telegram_bot_token",
            fallback_env_key="TELEGRAM_BOT_TOKEN",
            account_env_key="TELEGRAM_CHAT_ID",
        ),
    }
)

#: Resolved answers, keyed by logical name. See "Caching" in the module
#: docstring; :func:`reset_cache` is how a caller ends a memo's life.
_CACHE: Dict[str, Tuple[str, Optional[str]]] = {}


def _is_macos() -> bool:
    """Return whether this process is running on macOS.

    Read from ``sys`` on every call rather than bound at import: a
    module-level constant would make the switch untestable on any
    platform but one, and a test that patches the platform would be
    testing the patch.
    """
    return sys.platform == "darwin"


def keychain_disabled() -> bool:
    """Return whether the keychain must not be consulted.

    Fail-closed: the keychain is consulted only on macOS and only when
    the switch carries one of the two spellings that mean "do not
    disable". Unset, empty, and every unrecognised value disable it.
    """
    if not _is_macos():
        return True
    return os.environ.get(_SWITCH_ENV_KEY) not in _SWITCH_ENABLING_VALUES


def _env_value(key: str) -> Optional[str]:
    """Return the environment value for ``key``, or None when unusable.

    An absent variable and an empty one are the same thing to a
    caller: there is no value. Both are reported as None so that a
    keychain index exported as an empty string is treated as missing
    rather than as an item named "".
    """
    value = os.environ.get(key)
    if not value:
        return None
    return value


def _keychain_file() -> str:
    """Return the keychain file to read, for the environment in effect.

    ``PDT_KEYCHAIN_PATH`` wins when it carries a value; an absolute one is
    taken as written, a relative one is joined onto the home directory
    exactly as the default is. Unset or empty falls back to
    :data:`_KEYCHAIN_PATH`, so the keychain this project keeps its secrets
    in is what an installation gets without configuring anything.

    Resolved on every call rather than bound at import, for the same
    reason the switch is: the home directory and the override are both
    properties of the environment this process was started in, and a value
    computed at import belongs to the process instead.
    """
    override = _env_value(_KEYCHAIN_PATH_ENV_KEY)
    if override is None:
        return str(Path.home() / _KEYCHAIN_PATH)
    path = Path(override)
    if path.is_absolute():
        return str(path)
    return str(Path.home() / path)


def _run_security(argv: Sequence[str], timeout: float) -> Optional[bytes]:
    """Run the keychain tool, returning its stdout — or None on any failure.

    One place decides what a failed read looks like, and the answer is
    always the same: None. The tool exits nonzero for an item that is
    not in the keychain (the ordinary case for an operator who turned
    the switch on and never added the item); it can be killed, or
    blocked past ``timeout``, or absent entirely on a machine where the
    module was reached by some route other than the platform check.
    Every one of those is "no value", and distinguishing them here would
    mean every caller of :func:`read_secret` had to handle a taxonomy
    none of them can act on differently.

    The output is returned as bytes rather than decoded here, because
    the decode has a requirement — see :func:`_decode_payload` — that
    does not belong mixed in with the process call.
    """
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # A missing binary is an OSError; a timeout and a failed wait
        # are SubprocessError. Both are read failures, not errors the
        # caller can do anything about differently from a nonzero exit.
        log.debug("keychain read did not complete: %s", exc)
        return None

    if completed.returncode != 0:
        log.debug(
            "keychain read exited %s for %s",
            completed.returncode, argv[3] if len(argv) > 3 else "?",
        )
        return None

    return completed.stdout


def _decode_payload(raw: bytes) -> str:
    """Return the password ``security`` wrote, without the newline it adds.

    Two deliberate choices.

    **One trailing newline, removed.** The tool prints the password
    followed by a newline. Taking the output verbatim would append a
    character to every secret, and the symptom would be an
    authentication error at the far end of a notification send rather
    than a bug at the read. Only that one newline is stripped — a
    password ending in anything else is left alone.

    **Lossy decodes are not a failure.** A keychain item holds bytes,
    and not every byte sequence is text. Decoding strictly would turn a
    secret that is present into a read that failed, and with it a
    disabled transport: the worst trade available, since the value was
    readable the whole time. Decoding with ``surrogateescape`` gives
    every undecodable byte a spelling that re-encodes to the original
    byte, so the value survives the round trip intact.
    """
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    return raw.decode("utf-8", errors="surrogateescape")


def _keychain_state() -> str:
    """Return what the lock probe could establish about the keychain.

    ``security show-keychain-info`` succeeds only on an unlocked
    keychain, so a nonzero exit means it could not confirm one. Which of
    the two remaining explanations that is — *locked*, or *the tool could
    not be run at all* — is the whole reason this returns a label rather
    than a boolean.

    Collapsing them would produce the most expensive kind of wrong
    answer available here. A process that is not permitted to execute
    ``/usr/bin/security`` — a sandbox, some CI runners, an agent
    session — fails this probe for a reason that has nothing to do with
    the keychain's state, and reporting that as "locked" sends an
    operator to Keychain Access to unlock a keychain that is already
    open. The advice would be unfollowable, and it would be attached to
    a real symptom, so it would be believed.

    Run only after a read has already failed. On the happy path this
    would be a second process started for no information, and a process
    per lookup is exactly the cost the resolution memo exists to avoid.
    """
    if not _is_macos():
        return KEYCHAIN_UNAVAILABLE
    argv: List[str] = [_SECURITY_BIN, "show-keychain-info", _keychain_file()]
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_LOCK_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # A binary this process may not execute, one that is not there,
        # and one that outran the bound all mean the same thing to a
        # caller: the keychain cannot be consulted from here, and no
        # amount of unlocking will change that.
        log.debug("keychain probe could not run: %s", exc)
        return KEYCHAIN_UNAVAILABLE
    return KEYCHAIN_UNLOCKED if completed.returncode == 0 else KEYCHAIN_LOCKED


def _resolve_from_keychain(spec: SecretSpec) -> Tuple[Optional[str], str]:
    """Return ``(value, source)`` for ``spec``, reading the OS keychain.

    The account is the lookup key, and it is read from the environment
    here rather than by the caller so that "no index" and "no value"
    cannot be confused. With no account there is no item to look up, and
    **no process is started** — the command could not succeed, and a
    failed read is exactly the state this reports anyway, so running it
    would cost a subprocess per secret for an answer already known.

    A failure anywhere below — a nonzero exit, an undecodable payload, a
    timeout — resolves to ``(None, "missing")``. Note what it does not
    do: it does not fall back to the plaintext variable. An operator who
    turned the keychain on gets told the keychain has nothing for this
    secret, rather than a quiet downgrade to a value every process of
    every user on the machine can read.

    A locked keychain is the one failure worth a log line, and it is
    logged here rather than left to the caller. Every other failure
    collapses into "no value" on purpose, but a locked keychain produces
    a caller that reports itself unconfigured with nothing in its own
    logs to say why — which reads as "this credential was never set up"
    and sends the operator to the wrong place entirely. See
    :func:`diagnose_secret`, which is the same question asked on demand.
    """
    account = _env_value(spec.account_env_key)
    if account is None:
        return (None, SOURCE_MISSING)

    argv: List[str] = [
        _SECURITY_BIN,
        "find-generic-password",
        "-a",
        account,
        "-w",
        _keychain_file(),
    ]
    raw = _run_security(argv, _KEYCHAIN_TIMEOUT_SECONDS)
    if raw is None:
        state = _keychain_state()
        if state == KEYCHAIN_LOCKED:
            log.warning(
                "%s could not be read: the keychain %s is locked. Unlock it "
                "(Keychain Access, or `security unlock-keychain %s`) and "
                "restart. A read against a locked keychain can also block for "
                "up to %d seconds waiting on an unlock prompt that nobody is "
                "there to answer.",
                spec.logical_name,
                _keychain_file(),
                _keychain_file(),
                int(_KEYCHAIN_TIMEOUT_SECONDS),
            )
        elif state == KEYCHAIN_UNAVAILABLE:
            log.warning(
                "%s could not be read and this process cannot run "
                "/usr/bin/security to say why — it is missing or not "
                "permitted here, which is the case inside some sandboxes. "
                "Unlocking the keychain will not help; run the process "
                "somewhere the keychain tool is allowed to run.",
                spec.logical_name,
            )
        return (None, SOURCE_MISSING)

    return (_decode_payload(raw), SOURCE_KEYCHAIN)


def _resolve_from_env(spec: SecretSpec) -> Tuple[Optional[str], str]:
    """Return ``(value, source)`` for ``spec``, reading the environment.

    The path taken when the keychain is off: a CI runner, a container,
    Linux, anywhere the keychain does not exist. An absent variable and
    an empty one are both no value, so both report ``"missing"``.
    """
    value = _env_value(spec.fallback_env_key)
    if value is None:
        return (None, SOURCE_MISSING)
    return (value, SOURCE_ENVIRONMENT)


def _resolve(name: str) -> Tuple[str, Optional[str]]:
    """Return ``(source, value)`` for ``name``, consulting the memo.

    The single place the three public entry points agree: they answer
    three questions about one secret, and answering them from three
    separate resolutions is how they would come to disagree — with the
    disagreement depending on which one a caller happened to call
    first.
    """
    cached = _CACHE.get(name)
    if cached is not None:
        return cached

    spec = SECRET_SPECS.get(name)
    if spec is None:
        # A name with no spec has no lookup, which is what "missing"
        # already means. Raising here would push the "is this secret
        # configured" question onto every caller, and one of them
        # would answer it differently.
        resolved = (SOURCE_MISSING, None)
    elif keychain_disabled():
        value, source = _resolve_from_env(spec)
        resolved = (source, value)
    else:
        # Enabled: the keychain is the source, and the index decides
        # whether the lookup can happen at all. With no index there is
        # nothing to look up, and the plaintext variable is not
        # substituted for it — an operator who asked for a keychain
        # gets told the keychain is not configured, rather than a
        # quiet downgrade to a secret every process can read.
        value, source = _resolve_from_keychain(spec)
        resolved = (source, value)

    _CACHE[name] = resolved
    return resolved


def secret_source(name: str) -> str:
    """Return where the secret ``name`` is read from.

    One of ``"keychain"``, ``"os.environ"`` or ``"missing"``. The
    label answers "where did this come from", so a caller can log it
    without knowing which platform it is running on. A name that is
    not in :data:`SECRET_SPECS` is ``"missing"``.
    """
    return _resolve(name)[0]


def read_secret(name: str) -> Optional[str]:
    """Return the value of the secret ``name``, or None.

    None means the secret is not available: the keychain is disabled
    and no fallback variable is populated, the keychain is enabled and
    no index names the item, or the name is not in the spec table. It
    is not an error — an unconfigured transport is a state the
    notifiers already handle — so this function does not raise.
    """
    return _resolve(name)[1]


def secret_available(name: str) -> bool:
    """Return whether a source is configured for the secret ``name``.

    This reports configuration, not readability: it is
    ``secret_source(name) != "missing"``, so a keychain-sourced secret
    is available as soon as its index is present.
    """
    return _resolve(name)[0] != SOURCE_MISSING


def reset_cache() -> None:
    """Empty the resolution memo.

    The cache has a lifetime the process does not control, so this is
    how a caller — a test, or an operator who re-keys a secret — says
    "look again" without a restart. It is safe to call at any time.
    """
    _CACHE.clear()


def diagnose_secret(name: str) -> Optional[str]:
    """Return why ``name`` has no value, or None when it has one.

    :func:`read_secret` answers "is there a value" and collapses every
    way of not having one into ``None``, because the caller asking it is
    asking whether a transport is configured, not what went wrong. That
    collapse is what makes it safe to call from a hot path, and it is
    also what makes a locked keychain invisible: a notifier reports
    itself unconfigured, and nothing in its own logs distinguishes "this
    was never set up" from "the keychain was locked when we looked" —
    two problems an operator fixes in completely different places, and
    the second one also costs a minute of blocking first.

    So the two questions are separate functions. This one is for a human
    reading a log or running a command, and it names the specific thing
    to change.

    Resolution runs first, so the answer describes the same state the
    caller would have observed rather than a second, possibly different
    one. It never raises: a name that is not registered is a sentence,
    not an exception.
    """
    spec = SECRET_SPECS.get(name)
    if spec is None:
        return (
            f"no secret named {name!r} is registered in SECRET_SPECS; "
            f"known names: {sorted(SECRET_SPECS)}"
        )

    if _resolve(name)[0] != SOURCE_MISSING:
        return None

    # In the order the resolution used, so the sentence names the first
    # thing that would have to change.
    if keychain_disabled():
        return (
            f"{name}: the keychain is switched off, so "
            f"{spec.fallback_env_key} was used and it is not set either. "
            f"Set {_SWITCH_ENV_KEY}=0 to consult the keychain, or populate "
            f"{spec.fallback_env_key}."
        )

    if _env_value(spec.account_env_key) is None:
        return (
            f"{name}: {_SWITCH_ENV_KEY} says the keychain is on, but "
            f"{spec.account_env_key} is not set, so there is no account to "
            f"look the item up by. The index is configuration, not a secret "
            f"— it stays in .env."
        )

    state = _keychain_state()
    if state == KEYCHAIN_LOCKED:
        return (
            f"{name}: the keychain {_keychain_file()} is locked. Unlock it "
            f"(Keychain Access, or `security unlock-keychain "
            f"{_keychain_file()}`) and restart. A read against a locked "
            f"keychain can block for up to "
            f"{int(_KEYCHAIN_TIMEOUT_SECONDS)} seconds first."
        )

    if state == KEYCHAIN_UNAVAILABLE:
        return (
            f"{name}: /usr/bin/security could not be run by this process, so "
            f"the keychain could not be consulted at all. It is missing or "
            f"not permitted here — some sandboxes and CI runners cannot "
            f"execute it. Unlocking the keychain will not change this; the "
            f"process has to run somewhere the tool is allowed to run."
        )

    return (
        f"{name}: the keychain is unlocked but holds no item with the account "
        f"{spec.account_env_key}. File one with "
        f"`security add-generic-password -U -a <{spec.account_env_key}> -w`."
    )


# ---------------------------------------------------------------------------
# The handoff: a secret to another process
# ---------------------------------------------------------------------------


def secret_fd_env_var(name: str) -> str:
    """Return the environment variable that tells a child which fd to read.

    ``"PDT_SECRET_FD_" + name`` upper-cased and with every character that
    is not a letter or a digit turned into ``_``. The result is a *name*,
    never a value: what goes into this variable is the descriptor number
    :func:`publish_secret_fd` returned, which is an integer naming a pipe
    the child already inherited and nothing a reader of the process
    environment could reconstruct a secret from.

    Deriving the name from the logical name is what lets a child find the
    variable without a second table mapping one name onto the other.
    A parallel ``Dict[str, str]`` would hold the same information and
    would be free to disagree with :data:`SECRET_SPECS`; the name cannot,
    because it is the name.

    Characters outside ``[A-Za-z0-9]`` become ``_`` rather than passing
    through. A logical name is this project's to choose, and a
    ``.`` or ``-`` in it would produce a variable name that a shell
    cannot export without quoting — a failure at the handoff, discovered
    by the child that was told to look.
    """
    slug = "".join(char if char.isalnum() else "_" for char in name).upper()
    return _FD_ENV_PREFIX + slug


def _encode_payload(name: str, value: str) -> bytes:
    """Return the JSON object ``{name: value}`` as bytes for the pipe.

    Two choices, and both are about bytes rather than text.

    ``ensure_ascii=False`` keeps non-ASCII characters as themselves
    instead of turning each into a ``\\uXXXX`` escape. It is not a size
    optimisation, it is what makes the payload decodable at all for a
    value that is not valid UTF-8: :func:`_decode_payload` hands those
    back as lone surrogates, and the escaped spelling would encode those
    surrogates as six ASCII characters that decode to the text
    ``"\\udcff"`` instead of to the byte it stands for. Written out
    literally they re-encode to the original bytes, so a keychain item
    holding arbitrary data crosses the pipe intact.

    No trailing newline. A pipe is read to EOF, not to a line, so a
    terminator would be a byte the reader has to strip — and stripping
    it is exactly the mistake :func:`_decode_payload` exists to avoid,
    repeated one layer up.
    """
    return json.dumps({name: value}, ensure_ascii=False).encode(
        "utf-8", "surrogateescape"
    )


def publish_secret_fd(name: str) -> int | None:
    """Put the secret ``name`` on a fresh pipe; return the read end.

    The read end is what a caller passes to a child — directly, or as
    the number in the variable :func:`secret_fd_env_var` names. The
    value itself is never written anywhere else: no environment
    variable, no temporary file, no argument vector where a process
    listing would show it.

    ``None`` means there is no secret to publish, which is the same
    answer :func:`read_secret` gives and the same one
    :func:`secret_source` labels ``"missing"``. No pipe is opened in
    that case: a "no secret" answer that still allocated two descriptors
    would leak them on every call, for the life of the process, since
    nothing else would ever close them.

    The write end is closed before the read end is returned, and that is
    not a formality — it is what puts the reader at EOF. A write end
    left open is a pipe whose writer is still nominally alive, and the
    read on the other side would wait for it forever.

    Raises ``ValueError`` for a payload too large for the pipe's
    buffer. The write is non-blocking so that case surfaces as an error
    instead of a hang: with no reader yet, a blocking write of more than
    the buffer's capacity would never return, and a secret that
    enormous is not a secret but a mistake worth naming.
    """
    value = read_secret(name)
    if value is None:
        return None

    payload = _encode_payload(name, value)
    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(write_fd, False)
        written = 0
        while written < len(payload):
            try:
                written += os.write(write_fd, payload[written:])
            except BlockingIOError:
                raise ValueError(
                    f"secret {name!r} is too large to hand over through a pipe"
                ) from None
    finally:
        os.close(write_fd)

    return read_fd


def read_secret_fd(fd: int) -> dict[str, str]:
    """Read a published secret from ``fd``; return it, and close ``fd``.

    The descriptor is spent by this call. A pipe has exactly one reader,
    and leaving the descriptor open would hand it to the next reader in
    the process — whose read would then block forever, waiting on a
    writer that went away the moment the payload was written. Closing on
    the way out turns that into a fast failure.

    The return is a flat object of strings, keyed by logical name. Flat
    because the only thing that goes through this pipe is a secret
    resolved by one name; a nested structure would be a shape nothing in
    this module produces and nothing here knows how to validate.

    Raises ``ValueError`` for a payload that is not such an object.
    That is a contract violation between the two halves of this module
    rather than a deployment state, so it is reported rather than
    smoothed over: returning ``{}`` for a payload the reader could not
    understand would turn a bug at the handoff into an authentication
    error at the far end of a notification send.
    """
    try:
        chunks: List[bytes] = []
        while True:
            chunk = os.read(fd, _FD_READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        # Closed on every path, including a decode failure below. The
        # caller handed over a descriptor and expects it spent; a
        # descriptor this function leaves behind is a descriptor the
        # caller has no way to find again.
        os.close(fd)

    payload = json.loads(raw.decode("utf-8", "surrogateescape"))
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in payload.items()
    ):
        raise ValueError("published secret payload is not a flat object of strings")
    return payload
