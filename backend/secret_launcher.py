"""Read the keychain once, hand the values to the server over pipes, exec it.

Why this is a separate process
------------------------------
The server needs two notification secrets. Until this module existed it
got them by shelling out to ``/usr/bin/security`` from its own startup
path — the same process that serves HTTP, runs background threads and
spawns executors. That put a keychain prompt, and a subprocess, inside
the request-serving process, on every start.

This module moves the read one process earlier. It runs, resolves each
secret, writes the values into anonymous pipes, names the descriptors in
the environment, and then **replaces itself** with the server. The server
therefore never runs ``/usr/bin/security`` at all; it reads a descriptor
it inherited. ``credentials.read_secret`` reports ``"inherited_fd"`` for
such a secret, which is the one-line evidence that the cutover happened.

Why ``exec`` and not a fork
---------------------------
``os.execv`` replaces the current process image, so the server keeps this
process's PID. That is not an optimisation: the supervisor tracks the PID
it spawned and treats its disappearance as a crash. A launcher that
forked and exited would look exactly like a service that died on startup,
and the supervisor would respawn it forever.

Why not ``subprocess`` + ``pass_fds``
-------------------------------------
That is how ``autonomous-coding``'s supervisor hands secrets over, and it
is the better shape when the parent stays alive to supervise the child.
Here the parent *is* the child a moment later, so there is no ``Popen``
call to attach ``pass_fds`` to. The inheritance has to come from the
descriptor table instead — which is why ``publish_secret_fd`` marks what
it returns inheritable, and why restoring that flag matters more here
than it does there.

What it does not do
-------------------
It does not stop the server when a secret cannot be read. A missing
notification credential is a state the notifiers already handle by
disabling themselves, and taking the whole backend down over one would
turn a degraded notifier into an outage. The failure is logged here, by
name, and the server reports the same secret as ``"missing"``.

Why it loads the deployment's ``.env`` first
--------------------------------------------
Because the server does, and the two must agree about one thing: whether
this deployment's keychain is switched on. ``PDT_DISABLE_KEYCHAIN_SECRETS``
is fail-closed — anything but ``0``/``false``, including *unset*, means
"do not consult the keychain" — and the value normally lives in the
project-root ``.env`` rather than in the environment the supervisor
spawns this process with.

Skip that load and the two processes reach opposite conclusions from the
same deployment. This one sees an unset switch, concludes the keychain is
off, and publishes nothing; the server then loads ``.env``, sees the
switch is on, and — correctly — refuses the plaintext fallback, because
a deployment that asked for a keychain must not be quietly downgraded.
The result is a notifier that reports itself unconfigured on a machine
whose keychain holds the credential, with nothing anywhere saying why.
That is not hypothetical; it is what this file did on 2026-10-08.

Nothing in this module writes a secret value anywhere: not to a log
line, not to ``argv``, not to a file. Only logical names, descriptor
numbers, and outcomes appear below.
"""

from __future__ import annotations

import logging
import os
import sys

import credentials

log = logging.getLogger("secret_launcher")

#: What this process becomes. The supervisor names the same module it
#: used to name directly, so the config change is one word.
_SERVER_MODULE = "backend.server"


def _handler() -> logging.Handler:
    """A stderr handler.

    Configured here rather than through ``basicConfig`` because the
    supervisor already redirects this process's stdout and stderr into
    the subsidiary's log file, and a line written before the server
    configures logging is a line the operator needs — it is the only
    record of *why* a secret did not arrive.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    return handler


def publish_secrets() -> int:
    """Publish every known secret onto a pipe; return how many succeeded.

    A secret that cannot be read is skipped rather than published as an
    empty pipe, so the server finds no descriptor variable for it and
    reports ``"missing"`` instead of reading a payload with a None in
    it.
    """
    published = 0
    for name in credentials.SECRET_SPECS:
        try:
            fd = credentials.publish_secret_fd(name)
        except Exception:  # noqa: BLE001 — one bad secret must not stop the others
            log.exception("%s could not be published; the server will report it missing", name)
            continue

        if fd is None:
            log.warning(
                "%s not published — the keychain had no value for it "
                "(account index set? item filed? ACL granted?)",
                name,
            )
            continue

        # The number, never the value. ``secret_fd_env_var`` derives the
        # variable from the logical name so the server needs no second
        # table mapping one onto the other.
        os.environ[credentials.secret_fd_env_var(name)] = str(fd)
        published += 1
        log.info("%s published on fd %d", name, fd)

    return published


def _load_deployment_env() -> None:
    """Load the project-root ``.env`` before anything reads the switch.

    The same file, the same call, and the same ``override=False`` that
    ``backend.server`` performs at its own startup. The module docstring
    says why the two processes have to agree; this is the half that makes
    them.

    ``config_paths.ENV_FILE`` is resolved **at call time** rather than
    imported at module scope, so this reads the path the server will
    compute rather than a copy taken before anything could redirect it —
    which is also what lets a test point it somewhere disposable.

    A missing ``.env`` is the ordinary case on a CI runner or in a
    container, where every variable comes from the caller's environment,
    and it is not worth a line. A missing ``python-dotenv`` is the same:
    the server's own load is guarded identically, so falling through here
    leaves the two processes agreeing, which is the only property this
    function owes.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    import config_paths

    if config_paths.ENV_FILE.exists():
        load_dotenv(config_paths.ENV_FILE, override=False)


def main() -> int:
    logging.basicConfig(level=logging.INFO, handlers=[_handler()])

    # Before the switch is read, never after. See "Why it loads the
    # deployment's .env first" in the module docstring.
    _load_deployment_env()

    if credentials.keychain_disabled():
        # CI, a container, Linux, or an operator who switched it off:
        # there is no keychain to read and the server resolves from the
        # plaintext variables as it always has. Not an error, and not
        # worth a warning — but the two conditions are named, because
        # this line is the only place a deployment whose keychain *is*
        # configured ever says which of them it thinks it is in.
        log.info(
            "keychain disabled for this deployment (not macOS, or "
            "PDT_DISABLE_KEYCHAIN_SECRETS is unset / not one of 0, false) — "
            "starting the server with no descriptors; it will resolve from "
            "the environment"
        )
    else:
        count = publish_secrets()
        log.info(
            "%d of %d secret(s) published; exec %s",
            count, len(credentials.SECRET_SPECS), _SERVER_MODULE,
        )

    # ``execv`` rather than a fork: same PID, so the supervisor's view
    # of this service never changes. argv is rebuilt rather than
    # forwarded verbatim because ``sys.argv[0]`` is this module's path,
    # not the interpreter. Extra arguments are passed through so that a
    # hand-started run can still add e.g. ``--port``.
    os.execv(
        sys.executable,
        [sys.executable, "-m", _SERVER_MODULE, *sys.argv[1:]],
    )
    # ``execv`` only returns on failure.
    log.error("exec %s failed", _SERVER_MODULE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
