"""The secret must not be in a running child's environment — checked on the process itself.

Why this suite exists
---------------------
The whole migration rests on one runtime property: **a provider secret never
reaches a child process through the environment.** The design that replaced it
is a pipe — :func:`credentials.publish_secret_fd` hands the read end to the
child and the environment carries a descriptor *number* — and every other layer
of evidence about the migration is weaker than that property in a specific way:

* A static gate can prove the code never *writes* an environment variable
  holding a secret (``tests/static_gates/test_notification_secrets_stay_out_of_environ.py``).
  It cannot prove the writing never happens, because it reads the source, not
  the running process. A gate that says "this module does not set the variable"
  is fully compatible with an inheritance, an ``os.environ.update`` it does not
  recognise, or a deployment whose parent shell exported the value long before
  any code of ours ran.
* The integration suite (``tests/integration/test_credentials_fd_isolation.py``)
  proves the descriptor is the thing that crosses, and that a child handed the
  number *without* the descriptor in ``pass_fds`` cannot read it. That is about
  the pipe. It inspects the environment this repository *builds* for a child,
  which is a dict — the parent's own opinion of what will be there.

Neither can see a child's environment, because by the time a child is running,
the environment is a property of the *kernel's copy of the process image*, not
of anything this repository still holds a reference to. So the property is
asserted here against a real child process, through a probe the kernel answers.

The probe, and why this file is Linux-only
-----------------------------------------
``/proc/<pid>/environ`` is the initial environment block of a live process:
what ``execve`` was handed, byte for byte. It is the closest available
equivalent to "what a child on this machine can read out of its own
environment", and it is the same question asked of a running process rather
than of a dict. It exists on Linux and nowhere else, which is where the e2e
lane runs and, more to the point, where there is no keychain: the CI runner
resolves provider secrets from the plaintext fallback, so this is the one place
the migration's whole premise — *the value is not ambient here* — is true by
construction. The suite therefore skips off Linux rather than inventing a
weaker probe for a platform where the property is not the one under test; the
macOS keychain cases live in their own job.

Why a negative assertion needs a positive control
-------------------------------------------------
"The child's environment does not contain the secret" is satisfied by a broken
probe, a child that died before ``exec``, a permission error swallowed into an
empty string, and a sample taken from the wrong pid. Every one of those makes
this suite green forever against a machine that is leaking. So:

* the sampler refuses to return a sample it cannot show is the child's — it
  has to contain a marker this file put in the child's initial environment;
* one case deliberately hands a child an environment that *does* carry a
  canary secret, through the very variable this project is removing, and
  requires the probe to see it. That is the control: a probe that cannot see
  the leak cannot be trusted to report its absence.

The residual precondition
-------------------------
This file can only certify what it is able to see. If the process running it
inherited ``FEISHU_APP_SECRET`` from a shell or a launchd job, then every
child on that machine inherits it too — not because of any code here, and not
in a way any code here can undo. A green run in that state would read as "the
migration is done", which is the one conclusion it must never support. So the
residual check is a precondition on every case in this file (test design
decision point 9): a machine that has not cleared its shell and its launchd
side gets a failure that says so, rather than a pass that means nothing.

Resource discipline
-------------------
The child here is a real process, and this file starts one per case. Every one
is handed to ``register_child_process`` (``tests/conftest.py``), whose teardown
terminates and joins it, and the runner additionally closes the child's stdin
— which is what releases it from its parked read — and joins it inside the
case, asserting that the join happened. A child that never exits fails the case
that started it instead of being discovered by the next one.
"""

from __future__ import annotations

import hashlib
import os
import select
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

import credentials

#: ``backend/``, resolved from this file rather than written out: the
#: repository is checked out at a different path on every machine, and this
#: path is handed to a child as its working directory and its import path.
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The secret this file hands across the boundary, named by its logical name.
LOGICAL_NAME = "feishu_app_secret"

#: The variable that carries a descriptor *number* to the child, derived
#: through the module's own entry point rather than written out, so a change
#: to the prefix is a change this file follows rather than outgrows.
FD_ENV_VAR = credentials.secret_fd_env_var(LOGICAL_NAME)

#: The variable the *parent* has to carry for the module to resolve anything
#: at all. On Linux there is no keychain, so the plaintext fallback is the
#: only source there is — which is exactly why this file runs there.
FALLBACK_ENV_KEY = credentials.SECRET_SPECS[LOGICAL_NAME].fallback_env_key

#: The variables whose continued presence in the ambient environment means
#: the operational half of the migration has not happened. Derived from
#: ``SECRET_SPECS`` so a row added to the table is checked and a renamed one
#: cannot leave a stale key unexamined.
#:
#: Only ``fallback_env_key`` is listed. ``account_env_key`` holds a keychain
#: *index* — a value with no power of its own — and the migration does not
#: ask anybody to unset those, so including them would fail cases over
#: residue the migration never claimed to remove.
RESIDUAL_KEYS = tuple(
    sorted({spec.fallback_env_key for spec in credentials.SECRET_SPECS.values()})
)

#: The shortest fragment of an injected value that counts as a leak. Any
#: shorter and the check starts matching the key names themselves, which is
#: the one thing the diagnostics are supposed to contain.
_LEAK_MIN_FRAGMENT = 6

#: How long a child may take to report that it holds the payload, and how
#: long it may take to exit once its stdin is closed. Generous, because
#: these cover a loaded CI runner rather than a child that refuses to
#: finish; bounded, because a child that never exits must fail the case
#: that started it rather than spend the lane's whole budget.
_HANDSHAKE_TIMEOUT_SECONDS = 30
_CHILD_TIMEOUT_SECONDS = 60

#: The two strings the child writes on stdout once it has read the
#: payload. A sentinel, so a truncated or interleaved line cannot be
#: mistaken for one, and a digest rather than the value — a failing
#: assertion must never put a secret into a CI log.
_READY = "READY"

#: The two environment entries this file adds to conduct the handoff.
#: They are excluded from the fragment sweep — see :func:`sweepable_text`
#: for why, which is the whole of the reason this constant exists.
_HARNESS_ENTRY_PREFIXES = (b"PDT_TEST_FD_VAR=", b"PDT_TEST_SECRET_NAME=")


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason=(
            "this case reads a running child's environment out of "
            "/proc/<pid>/environ, which is a Linux facility; the e2e lane "
            "runs on Linux, where the provider secret is resolved from the "
            "plaintext fallback and this property is the one under test"
        ),
    ),
]


# ---------------------------------------------------------------------------
# The child
# ---------------------------------------------------------------------------
#
# A real interpreter — ``sys.executable -c <source>`` — with nothing on the
# path patched. The child takes the descriptor number from its environment,
# reads the payload through the module's own reader, announces what it read as
# a digest, and then parks on stdin.
#
# The parking is the reason this file is shaped the way it is. The parent has
# to sample ``/proc/<pid>/environ`` while the child is *alive*: once it exits,
# the ``/proc`` entry is gone and the sample is unanswerable. A child that
# reads and exits on its own would have to be sampled from a second process
# started to watch it, and the race between "the child is still there" and
# "the child is gone" would be settled by timing. Instead the child announces
# readiness, holds still until the parent closes the pipe — at which point
# ``sys.stdin.read()`` returns at EOF and the child exits — and the parent
# holds the closing end of that pipe. The window in which the sample must be
# taken is therefore a rendezvous, not a race.
#
# The harness names reach the child through its environment rather than
# through interpolated source, so the script is one constant with nothing
# spliced into it: an f-string would have to double every brace in the child's
# own literals, and the first one somebody forgot to double would be a syntax
# error reported as an exit code.
#
# The child is told nothing about the value it is expected to find. A child
# that knew the answer could not fail the way this file needs it to fail — the
# parent would be handing over the answer it is asking for. The digest is
# compared here, after the fact.

_CHILD_SRC = """
import hashlib
import os
import sys

READY = {ready!r}
CANNOT_READ = {cannot_read}

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

digest = hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()
sys.stdout.write(READY + " " + digest + "\\n")
sys.stdout.flush()

# Park until the parent closes the pipe. read() returns at EOF, so the
# closing of stdin is the signal to finish; nothing is read from it.
sys.stdin.read()
sys.exit(0)
""".format(ready=_READY, cannot_read=3)

#: A child that never finishes on its own, used only by the case that proves
#: the suite's teardown is what stops it. No pipes: a parked child holding a
#: parent-side pipe open would be a descriptor this file leaks in order to
#: prove it does not leak processes.
_PARKED_SRC = "import time; time.sleep(300)"


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


class EnvironSample(NamedTuple):
    """One reading of a live child's environment, as the kernel has it."""

    raw: bytes

    @property
    def text(self) -> str:
        """The bytes decoded, never raising on a value that is not text.

        ``errors="replace"`` rather than a strict decode: the child is
        looking for a *substring*, and a byte sequence that is not valid
        UTF-8 has to still be searchable rather than raising past the
        assertion that was supposed to report it.
        """
        return self.raw.decode("utf-8", "replace")


def read_child_environ(pid: int, must_contain: str) -> EnvironSample:
    """Return what the kernel says the process ``pid`` was started with.

    ``must_contain`` is a value this file put in the child's initial
    environment, and the returned sample has to carry it. That check is the
    whole difference between a reading and a guess: ``/proc/<pid>/environ``
    is missing for a process that has exited, unreadable for a process this
    user may not inspect, and empty for a process whose environment block
    was not captured — and all three arrive at the case below as "no secret
    here" unless they are refused here. A probe that returns bytes without
    proving they are the child's makes every negative assertion in this file
    vacuous.
    """
    path = Path("/proc") / str(pid) / "environ"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        pytest.fail(
            f"{path} does not exist, so there is nothing to sample: the "
            f"child with pid {pid} had already exited. The case parks it on "
            f"a stdin read precisely so this window cannot close early."
        )
    except PermissionError:
        pytest.fail(
            f"{path} could not be read by this user. The probe has to see "
            f"into the child for the absence it reports to mean anything."
        )
    sample = EnvironSample(raw)
    assert must_contain in sample.text, (
        "the sample does not contain {!r}, which this file put in the "
        "child's environment, so it is not a reading of that child's "
        "environment and cannot be used to say the secret is absent"
    ).format(must_contain)
    return sample


# ---------------------------------------------------------------------------
# Spawning
# ---------------------------------------------------------------------------


class Child(NamedTuple):
    """A started child, and the two ways a case interacts with it."""

    proc: subprocess.Popen

    @property
    def pid(self) -> int:
        return self.proc.pid

    def read_ready_line(self, timeout: int = _HANDSHAKE_TIMEOUT_SECONDS) -> str:
        """Return the line the child wrote once it holds the payload.

        Bounded by waiting on the pipe rather than by a timer around a
        blocking read: a child that never writes would otherwise hold the
        case until the suite's own per-case ceiling, which is five minutes
        to learn that a fork never happened. The child writes its one line
        and flushes, so readability means the line is there; a child that
        dies instead makes the pipe readable at EOF, and an empty line is
        reported as the failure it is.

        A watchdog *thread* is the obvious alternative and is not used:
        ``tests/conftest.py`` replaces ``threading.Thread`` for the whole
        test with a recording subclass, and the standard library's own
        ``Timer`` — constructed inside that window — dies on the
        replacement's zero-argument ``super()``. A wait on a descriptor
        needs no thread and is not at the mercy of that.
        """
        readable, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not readable:
            self.proc.kill()
            _, stderr = self.proc.communicate()
            pytest.fail(
                "the child wrote nothing to stdout within {}s and had to be "
                "killed, so it never reached the point where it holds the "
                "payload.\nstderr: {}".format(
                    timeout, stderr.decode("utf-8", "replace")
                )
            )
        line = self.proc.stdout.readline()
        text = line.decode("utf-8", "replace").strip()
        if not text:
            self.proc.kill()
            _, stderr = self.proc.communicate()
            pytest.fail(
                "the child's stdout reached EOF without the {} line, so it "
                "exited instead of parking with the payload in hand.\n"
                "exit: {}\nstderr: {}".format(
                    _READY, self.proc.returncode, stderr.decode("utf-8", "replace")
                )
            )
        return text

    def environ(self) -> EnvironSample:
        """Sample this child's environment while it is still running."""
        return read_child_environ(self.pid, LOGICAL_NAME)

    def finish(self, timeout: int = _CHILD_TIMEOUT_SECONDS) -> int:
        """Close stdin, join the child, and return its exit code.

        Closing stdin is the release: the child is parked in
        ``sys.stdin.read()``, which returns at EOF and lets it exit. Then
        ``communicate`` drains what is left and waits, so the join is an
        ordering guarantee rather than a hope — and a child that ignores
        the release is killed and reported, rather than outliving the case.

        ``proc.stdin`` is cleared afterwards, which is what
        ``communicate`` does to the stream once it has dealt with it. Left
        in place, the next ``communicate`` flushes the file this method
        closed and raises ``ValueError: flush of closed file`` — the
        release would be reported as a defect in the harness rather than
        as the join it is.
        """
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except BrokenPipeError:
                # The child is already gone; the join below reports how.
                pass
            self.proc.stdin = None
        try:
            self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate()
            pytest.fail(
                "the child was still running {}s after its stdin was closed "
                "and had to be killed; it never reached its own exit "
                "code".format(timeout)
            )
        assert self.proc.poll() is not None, (
            "the child was handed back before it exited"
        )
        return self.proc.returncode


def spawn_child(
    register_child_process,
    source: str,
    *,
    env: dict,
    pass_fds: tuple = (),
    stdin=None,
    stdout=None,
    stderr=None,
) -> Child:
    """Start one real interpreter and hand it to the suite's teardown.

    Every child goes through ``register_child_process``, so it rides the
    teardown that already reclaims a test's workers rather than needing a
    second, file-only mechanism. That registration is the backstop for a
    case that fails before it reaches :meth:`Child.finish`; the finish is
    what the case itself relies on.
    """
    kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if stdin is not None:
        kwargs["stdin"] = stdin
    if stdout is not None:
        kwargs["stdout"] = stdout
    if stderr is not None:
        kwargs["stderr"] = stderr
    proc = subprocess.Popen(
        [sys.executable, "-c", source],
        env=env,
        cwd=str(_BACKEND_DIR),
        pass_fds=tuple(pass_fds),
        **kwargs,
    )
    register_child_process(proc)
    return Child(proc)


# ---------------------------------------------------------------------------
# The residual precondition
# ---------------------------------------------------------------------------


def residual_failure_message(report: dict) -> Optional[str]:
    """Return why this run may not be trusted, or ``None`` if it may.

    A reporter, in the sense ``conftest.residual_secret_env_report`` is
    one: it says what is wrong and leaves the decision alone. It exists as
    a function rather than as an ``assert`` inside a fixture so that the
    case which checks the precondition can drive both of its answers —
    clean and blocked — from inside itself.

    The message names each offending variable, because the finding is acted
    on by editing a shell profile or a job definition and *which* file that
    is depends on which variable is still exported. It never carries a
    value: it is destined for a CI log, and a diagnostic that printed the
    secret it found would publish it at the moment somebody is reading
    about it. The report it embeds is per key and boolean for the same
    reason.
    """
    present = [name for name, is_present in report.items() if is_present]
    if not present:
        return None
    return (
        "the environment this run inherited still exports: {}\n"
        "every process started from this shell inherits those too, so no "
        "assertion in this file can mean what it says until they are gone — "
        "and nothing in this repository can remove them: os.environ is this "
        "process's own copy, and the shell or the launchd job that exported "
        "them is out of its reach.\n"
        "先按迁移方案的 B2 / B3 清除父 shell 与 launchd 侧残留: unset 上面列出的变量"
        "(检查 shell 的 rc / profile 文件), 并从已注册的 launchd "
        "job 与 CI job 的 env 中移除; 然后开一个新的 shell 重跑本用例.\n"
        "residual report (per key, existence only): {report!r}"
    ).format(", ".join(present), report=report)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_residual_provider_secrets(residual_secret_env):
    """Fail this whole file when a provider secret is still ambient.

    Autouse, and it runs before the body of any case — which is the only
    moment at which the *ambient* environment can be read, because the
    body is where the canary is injected to give the provider something to
    publish. A canary is this file's own fixture value and a residual is
    somebody's shell: reading the second one through the first would either
    always fail or never fail, and neither is a check.

    It is a precondition rather than a case because the property it guards
    is a property of the run, not of any one assertion. On a machine that
    has not cleared its shell and its launchd side, every case below would
    still pass — the children here are handed a purpose-built
    environment — and that green would be read as "the migration is done".
    The one thing this file can contribute to that situation is to refuse
    to be the evidence.

    The keychain switch is asserted alongside it, for the same reason the
    suite is Linux-only: the value has to be coming from the plaintext
    fallback, and a machine where that is not so is a machine whose answer
    to "where did this secret come from" is a different one.
    """
    report = residual_secret_env(RESIDUAL_KEYS)
    message = residual_failure_message(report)
    assert message is None, message
    assert credentials.keychain_disabled() is True, (
        "the keychain is not off, so a lookup here would consult the "
        "platform instead of the plaintext fallback this file's Linux-only "
        "premise rests on"
    )
    return report


@pytest.fixture
def run_child(register_child_process):
    """Return a function that starts a parked reader of the handoff.

    The child this starts reads the published descriptor, reports the
    digest of what it read, and waits to be released by :meth:`Child.finish`
    — which is the shape every case in this file needs, and the shape the
    sample depends on.
    """
    def _run(source: str, *, env: dict, pass_fds: tuple = ()) -> Child:
        return spawn_child(register_child_process, source, env=env, pass_fds=pass_fds)

    return _run


@pytest.fixture
def published_secret(monkeypatch, canary, tmp_path, register_open_fd):
    """Return ``(secret, fd)`` for a real published handoff.

    The value is a canary — synthetic, per-test, and never written to
    disk — because the assertion here is about where a value *travels*, and
    a real credential would put a real credential into every failure
    message this file can produce.

    It is placed where :func:`credentials.publish_secret_fd` actually
    reads, and that is the keychain: the function is the *launcher's* read,
    and the launcher has no environment fallback to offer, because a
    deployment that asked for a keychain must not be quietly served from a
    plaintext variable. So the canary goes behind the one call that would
    reach this machine's own keychain, and the account index names it.
    ``monkeypatch`` unwinds both at the end of the case whatever the case
    asserted, so the next one reads the ambient environment the fixture
    above checked rather than this one's scaffolding.

    Leaving it in the fallback variable instead — which is what this
    fixture used to do — reads as a handoff only where a real keychain item
    happens to be filed under the ambient index, and publishes nothing at
    all where there is neither. That is a green run on the machine that
    filed the item and a failure on the runner that did not.
    """
    secret = canary("feishu_secret", tmp_path)
    monkeypatch.setenv(
        credentials.SECRET_SPECS[LOGICAL_NAME].account_env_key, "cli_e2e_handoff"
    )

    def _the_keychain_holds_it(argv, timeout):
        return secret.encode("utf-8") + b"\n"

    monkeypatch.setattr(credentials, "_run_security", _the_keychain_holds_it)

    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None, (
        "the provider published nothing, so there is no handoff to inspect"
    )
    register_open_fd(fd)
    return secret, fd


def child_env(fd: Optional[int] = None, extra: Optional[dict] = None) -> dict:
    """The environment this file hands to a child.

    Built from nothing rather than copied from ``os.environ``, and that is
    the point. A copy would carry whatever the machine running the suite
    has exported, and a case asserting that a child cannot reach the value
    would then be asserting something about the developer's shell — the
    residual precondition above is the check that the shell is clean, and
    copying the environment would make the child a second, uncontrolled
    channel for exactly what that check is about.

    ``extra`` is how the positive control hands a child a secret: the
    control is only a control if it puts the value where a real leak would
    have put it, which is the child's own initial environment.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_BACKEND_DIR),
        "PDT_TEST_FD_VAR": FD_ENV_VAR,
        "PDT_TEST_SECRET_NAME": LOGICAL_NAME,
    }
    if fd is not None:
        env[FD_ENV_VAR] = str(fd)
    if extra:
        env.update(extra)
    return env


def leaked_fragments(text: str, value: str) -> list:
    """Every fragment of ``value`` that ``text`` carries, six characters up.

    A substring search rather than an equality test, and deliberately: the
    failure this rules out is a secret that travelled *inside* a larger
    value — prefixed, suffixed, or a whole environment serialised into one
    variable — and equality would call that clean. The floor is the same
    one the diagnostics use, below which a match stops being evidence of
    anything and starts being the key names.
    """
    return [
        value[start:start + length]
        for start in range(len(value))
        for length in range(_LEAK_MIN_FRAGMENT, len(value) - start + 1)
        if value[start:start + length] in text
    ]


def sweepable_text(captured: EnvironSample) -> str:
    """The sample with this file's own two bookkeeping entries removed.

    Two entries in a child's environment are the ones this file put there
    to conduct the handoff: the name of the variable that names the
    descriptor, and the logical name the payload is keyed by. Both spell
    the secret's *name* — and a canary is built out of exactly those
    words, so a fragment search that ran over them would report every
    child as leaking, which is the same mistake the static gate's
    whitelist exists to prevent: a check that fires on correct code gets
    switched off.

    Stripping those two entries does not narrow the whole-value assertion,
    which runs against the sample as it was read: what must never appear
    is the *value*, and a value smuggled inside one of the two stripped
    entries would be a defect the primary assertion still catches.
    """
    entries = [entry for entry in captured.raw.split(b"\0") if entry]
    kept = [
        entry
        for entry in entries
        if not entry.startswith(_HARNESS_ENTRY_PREFIXES)
    ]
    return b"\0".join(kept).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# The two halves of the assertion
# ---------------------------------------------------------------------------


def test_child_environ_does_not_contain_the_secret(
    published_secret, run_child
):
    """The child holds the secret on a pipe, and its environment says so not at all.

    Non-vacuous by construction. The child really does hold the value —
    the digest is compared before the sample is taken, and a child that
    could not read the descriptor exits before it announces anything — so
    "the environment does not contain it" is a statement about a process
    carrying the secret, not about one that never received it.

    The sample is itself checked: :func:`read_child_environ` refuses to
    return a reading that does not contain a marker this file put in the
    child, so the assertion below is never reached with an empty or
    foreign reading in hand.
    """
    secret, fd = published_secret

    child = run_child(_CHILD_SRC, env=child_env(fd), pass_fds=(fd,))
    assert child.read_ready_line() == "{} {}".format(
        _READY, hashlib.sha256(secret.encode("utf-8", "surrogateescape")).hexdigest()
    ), "the child did not read the published payload, so there is nothing to prove"

    captured = child.environ()
    assert child.finish() == 0, "the child did not exit cleanly once released"

    assert secret not in captured.text, (
        "the running child's environment carries the secret itself"
    )
    leaked = leaked_fragments(sweepable_text(captured), secret)
    assert not leaked, (
        "the running child's environment carries part of the secret: "
        "{}".format(leaked)
    )


def test_child_environ_contains_the_fd_variable_name(published_secret, run_child):
    """What *is* in the child's environment is the descriptor number.

    The negative case above would also pass on a child that was told
    nothing at all, and a handoff that reaches no child is not a handoff.
    This is the other half of the same reading: the child is told which
    descriptor to read, and told it by number. The entry is matched whole —
    name, ``=``, digits — so a variable that had somehow been given
    something other than a number would not satisfy it.
    """
    _, fd = published_secret

    child = run_child(_CHILD_SRC, env=child_env(fd), pass_fds=(fd,))
    assert child.read_ready_line().startswith(_READY), (
        "the child never announced that it read the payload"
    )

    captured = child.environ()
    assert child.finish() == 0, "the child did not exit cleanly once released"

    assert FD_ENV_VAR.encode() in captured.raw, (
        "the child's environment does not name the descriptor variable, so "
        "the handoff is addressed some other way and the absence of the "
        "secret proves nothing about how it is delivered"
    )
    assert "{}={}".format(FD_ENV_VAR, fd).encode() in captured.raw, (
        "the variable does not carry the descriptor number {} that was "
        "published".format(fd)
    )


def test_positive_control_the_probe_does_see_the_secret(
    published_secret, run_child
):
    """A child that *is* handed a secret through the environment is caught.

    The control for the case above, and the reason that case can be
    believed. The sentinel is put in the child's own initial environment
    under the very variable this project is removing — the exact shape of
    the leak being ruled out, produced deliberately — and the probe has to
    see it.

    If this case ever fails, the probe is not reading the child's
    environment, and every "the secret is absent" assertion in this file
    is answering a question about nothing. That is the failure mode a
    purely negative file cannot see, and it is the reason this case is
    here rather than left implicit.
    """
    secret, fd = published_secret

    child = run_child(
        _CHILD_SRC,
        env=child_env(fd, extra={FALLBACK_ENV_KEY: secret}),
        pass_fds=(fd,),
    )
    assert child.read_ready_line().startswith(_READY), (
        "the control child never announced that it read the payload, so it "
        "is not a control on the probe"
    )

    captured = child.environ()
    assert child.finish() == 0, "the control child did not exit cleanly once released"

    assert secret in captured.text, (
        "the probe did not see a sentinel that was in the child's own "
        "environment, so it cannot be trusted to report that a secret is "
        "absent from one"
    )


def test_residual_env_blocks_the_case_with_guidance(
    residual_secret_env, monkeypatch, canary, tmp_path
):
    """With a provider secret still exported, the case refuses to run and says why.

    Both answers of the precondition are driven from here, in order: with
    nothing exported it passes, and with one exported it blocks. A guard
    that can only block would fail every run on a machine that is already
    clean, and the reader would learn to ignore it; a guard that can only
    pass is the failure this file exists to prevent.

    The message is checked for what makes it actionable — it names the
    variable, points at the migration steps, and says which two places
    outside the process still hold it — and for what must never be in it:
    any part of the value. It is destined for a CI log, and the operator it
    addresses is the moment somebody is already looking at a secret.
    """
    assert residual_failure_message(residual_secret_env(RESIDUAL_KEYS)) is None, (
        "the precondition rejected an environment that is already clean, so "
        "it would fail every run rather than catch a residue"
    )

    value = canary("feishu_secret", tmp_path)
    monkeypatch.setenv(FALLBACK_ENV_KEY, value)

    report = residual_secret_env(RESIDUAL_KEYS)
    assert report[FALLBACK_ENV_KEY] is True, "the injected residue was not seen"

    message = residual_failure_message(report)
    assert message is not None, "a residue was exported and the case was not blocked"
    for expected in (FALLBACK_ENV_KEY, "B2", "B3", "launchd"):
        assert expected in message, (
            "the guidance does not mention {!r}, so it does not tell the "
            "reader what to do: {!r}".format(expected, message)
        )
    leaked = leaked_fragments(message, value)
    assert not leaked, (
        "the guidance carries part of the value it found: {}".format(leaked)
    )


# ---------------------------------------------------------------------------
# The children are handed back
# ---------------------------------------------------------------------------
#
# A fixture that isolates *process* state cannot be observed from inside the
# test it isolates for — by the time the teardown has joined the child, the
# test that started it is over. So this contract is written as a pair, the
# way ``tests/unit/test_credentials_test_fixtures.py`` writes its own: a
# polluter that deliberately starts a child and does not wait for it, and
# immediately after it the case that says what the suite's teardown did
# about that. Definition order is the order pytest runs them in — the e2e
# lane runs serially and no ordering plugin is installed — and the
# dependency is stated here rather than left to be discovered.
#
# The polluter parks a child that cannot have finished on its own within the
# run of a single case, so the observer's "it is dead now" is evidence about
# the teardown rather than about the child's timing.

#: The child the polluter below left behind, inspected by the next case.
_CHILD_LEFT_BY_PREVIOUS_TEST: list = []


def test_the_previous_case_left_a_child_running(register_child_process):
    """Start a real child through this file's spawner and never release it.

    The same entry point every other case in this file uses, so what the
    next case observes is this file's own hand-back, not a hand-back that
    was arranged for the occasion.
    """
    child = spawn_child(
        register_child_process,
        _PARKED_SRC,
        env=child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _CHILD_LEFT_BY_PREVIOUS_TEST.append(child)


def test_child_is_reaped_by_the_existing_contract(reclaimed):
    """A child this file started is joined by the teardown that already reclaims workers.

    The rule is the suite's own — a test hands back every resource it
    starts — and it is enforced in one place,
    ``tests/conftest.py::clean_execution_state``, which already releases and
    joins the workers a test leaves behind. This pins that the children
    *this* file starts ride that same teardown, rather than needing a
    second, file-only mechanism the next case would have to remember.

    ``returncode`` carries the weight. A child that was merely terminated
    and never waited on is a zombie, and a zombie reports a return code to
    ``poll()`` as readily as a reaped one. What distinguishes them here is
    that this child asked for five minutes and two cases do not take that
    long, so a non-``None`` return code means the teardown stopped it and
    collected it.
    """
    assert _CHILD_LEFT_BY_PREVIOUS_TEST, "the polluter above did not run"
    (child,) = _CHILD_LEFT_BY_PREVIOUS_TEST

    assert child.proc in reclaimed()["children"], (
        "the teardown never saw the child this file started"
    )
    assert child.proc.returncode is not None, "the child outlived its own case"
    assert child.proc.poll() is not None, "the child is still running"
