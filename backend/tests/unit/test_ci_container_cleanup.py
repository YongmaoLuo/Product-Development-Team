"""The cleanup script takes away the gate containers a killed driver left.

Why this file exists
--------------------
``scripts/run_ci_lanes_in_container.sh`` starts one container per lane
with ``--rm``, so a container normally disappears with its lane.  Two
things defeat that: a driver that is SIGKILLed runs no trap at all, and
a restarted docker daemon forgets the containers it was running.
Either way the container stays, holding a slice of the machine's memory
and disk for as long as nobody removes it.

``scripts/cleanup_ci_containers.sh`` is what removes them -- and the
whole value of it is in the containers it does *not* remove.  It runs as
the user who owns the leftovers, and it must never take away:

* a container that belongs to somebody else,
* a container that carries no pdt labels at all,
* a container that is still running,
* a container that is younger than two hours (a lane may still be in
  it), and
* -- by default -- anything: without ``--apply`` it reports and stops.

Every assertion here runs the script against a stand-in ``docker`` on
``PATH``.  No docker daemon is needed: the stand-in prints a ``ps``
listing the test wrote, records every call, and can be told to fail.

The listing the stand-in prints is tab separated and carries the four
columns the script reads: name, state, pdt-owner, pdt-created.  The
``pdt-ci=1`` label itself is not a column -- the script restricts the
listing with ``docker ps --filter label=pdt-ci=1``, so a container
without that label is never in the list to begin with.

How a container is removed
--------------------------
A plain ``docker rm`` is enough for a container that has exited, and it
is refused for a container that is running -- which is what is wanted.
The daemon refuses a paused or a dead container as well, and those are
not running either: they are leftovers like any other.  So the state is
asked for once more, right before anything is forced, and a container
that is running (or restarting) at that moment is left alone -- the
refusal is reported instead.  The stand-in answers ``docker inspect``
the way the daemon does, including the detail that a paused container is
reported as running (``{{.State.Running}}`` is true for it) while its
status stays ``paused``.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

#: The repository root, resolved from this file rather than from the
#: working directory: pytest is often launched with cwd=``backend/``.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: The script under test.
CLEANUP = PROJECT_ROOT / "scripts" / "cleanup_ci_containers.sh"

#: The user the script runs as.  The driver labels every container it
#: starts with ``pdt-owner=<uid>``, so this is the owner the script is
#: allowed to remove containers for.
CURRENT_UID = str(os.getuid())

#: How old a container has to be before the script treats it as a
#: leftover: two hours.
MIN_AGE_SECONDS = 7200


# ---------------------------------------------------------------------------
# The stand-in docker
# ---------------------------------------------------------------------------

#: A stand-in for ``docker``.
#:
#: It records every call it receives in ``PDT_CI_TEST_DOCKER_LOG``, and
#: every container it really removed in ``PDT_CI_TEST_REMOVED``.  A
#: ``ps`` call prints the listing in ``PDT_CI_TEST_PS_TABLE`` -- or
#: fails, when ``PDT_CI_TEST_PS_FAIL`` says so.
#:
#: Two of the daemon's refusals are emulated, because the script has to
#: cope with them:
#:
#:   ``PDT_CI_TEST_RM_FAILS``  the containers a plain ``docker rm``
#:                            refuses -- a paused or a dead container;
#:                            ``docker rm -f`` removes them.
#:   ``PDT_CI_TEST_RUNNING``   the containers that are running right
#:                            now, whatever the listing said about
#:                            them: the daemon refuses to remove those
#:                            without ``-f``, and the script must not
#:                            force them.
#:
#: The stand-in answers ``docker inspect`` the way the daemon does,
#: including the detail that a paused container is reported as running
#: (``{{.State.Running}}`` is true for it) while its status stays
#: ``paused``.
_FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$PDT_CI_TEST_DOCKER_LOG"

if [ "$1" = "ps" ]; then
    if [ "${PDT_CI_TEST_PS_FAIL:-0}" = "1" ]; then
        echo "Cannot connect to the Docker daemon" >&2
        exit 1
    fi
    if [ -n "${PDT_CI_TEST_PS_TABLE:-}" ] && [ -f "${PDT_CI_TEST_PS_TABLE}" ]; then
        cat "${PDT_CI_TEST_PS_TABLE}"
    fi
    exit 0
fi

if [ "$1" = "inspect" ]; then
    format=""
    name=""
    previous=""
    for argument in "$@"; do
        if [ "$previous" = "-f" ]; then
            format="$argument"
        fi
        name="$argument"
        previous="$argument"
    done
    is_running=0
    if [ -n "${PDT_CI_TEST_RUNNING:-}" ] && [ -f "${PDT_CI_TEST_RUNNING}" ] \\
        && grep -qx "$name" "${PDT_CI_TEST_RUNNING}"; then
        is_running=1
    fi
    is_paused=0
    if [ -n "${PDT_CI_TEST_RM_FAILS:-}" ] && [ -f "${PDT_CI_TEST_RM_FAILS}" ] \\
        && grep -qx "$name" "${PDT_CI_TEST_RM_FAILS}"; then
        is_paused=1
    fi
    case "$format" in
        *Running*)
            # The daemon calls a paused container running: it is only
            # paused, after all.
            if [ "$is_running" -eq 1 ]; then echo "true"; else echo "false"; fi
            ;;
        *Status*)
            if [ "$is_running" -eq 1 ]; then
                echo "running"
            elif [ "$is_paused" -eq 1 ]; then
                echo "paused"
            else
                echo "exited"
            fi
            ;;
        *)
            echo ""
            ;;
    esac
    exit 0
fi

if [ "$1" = "rm" ]; then
    force=0
    name=""
    for argument in "$@"; do
        case "$argument" in
            -f|--force) force=1 ;;
            *) name="$argument" ;;
        esac
    done
    if [ "$force" -eq 0 ] && [ -n "${PDT_CI_TEST_RM_FAILS:-}" ] \\
        && [ -f "${PDT_CI_TEST_RM_FAILS}" ] \\
        && grep -qx "$name" "${PDT_CI_TEST_RM_FAILS}"; then
        echo "Error response from daemon: cannot remove container \\"$name\\": container is paused and must be unpaused first" >&2
        exit 1
    fi
    printf '%s\\n' "$name" >> "${PDT_CI_TEST_REMOVED}"
    exit 0
fi

exit 0
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(name: str, state: str, owner: str, created: str) -> str:
    """One line of a ``ps`` listing: the four columns the script reads."""
    return f"{name}\t{state}\t{owner}\t{created}"


def _environment(
    tmp_path: Path,
    rows: list[str],
    rm_fails: list[str] | None = None,
    running: list[str] | None = None,
    **overrides: str,
) -> dict[str, str]:
    """The environment the script runs with: a stand-in docker on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "docker"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)

    table = tmp_path / "ps-table"
    table.write_text("".join(f"{row}\n" for row in rows), encoding="utf-8")

    rm_fails_file = tmp_path / "rm-fails"
    rm_fails_file.write_text(
        "".join(f"{name}\n" for name in (rm_fails or [])), encoding="utf-8"
    )
    running_file = tmp_path / "running"
    running_file.write_text(
        "".join(f"{name}\n" for name in (running or [])), encoding="utf-8"
    )
    removed_file = tmp_path / "removed"
    removed_file.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PDT_CI_TEST_DOCKER_LOG"] = str(tmp_path / "docker.log")
    env["PDT_CI_TEST_PS_TABLE"] = str(table)
    env["PDT_CI_TEST_RM_FAILS"] = str(rm_fails_file)
    env["PDT_CI_TEST_RUNNING"] = str(running_file)
    env["PDT_CI_TEST_REMOVED"] = str(removed_file)
    env.update(overrides)
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run the cleanup script with the stand-in docker on PATH."""
    return subprocess.run(
        ["bash", str(CLEANUP), *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _docker_calls(env: dict[str, str]) -> list[str]:
    """Every call the stand-in docker received."""
    log = Path(env["PDT_CI_TEST_DOCKER_LOG"])
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def _calls_to(env: dict[str, str], verb: str) -> list[str]:
    """The calls the script made to ``docker <verb>``."""
    return [call for call in _docker_calls(env) if call.split()[:1] == [verb]]


def _removed(env: dict[str, str]) -> list[str]:
    """The containers the script really removed."""
    removed = Path(env["PDT_CI_TEST_REMOVED"])
    if not removed.exists():
        return []
    return [
        line
        for line in removed.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _old_enough_timestamp() -> str:
    """A creation timestamp comfortably older than the two hour threshold."""
    return str(int(time.time()) - MIN_AGE_SECONDS - 100)


def _recent_timestamp() -> str:
    """A creation timestamp younger than the two hour threshold."""
    return str(int(time.time()) - 60)


# ---------------------------------------------------------------------------
# The dry run removes nothing
# ---------------------------------------------------------------------------


def test_dry_run_never_removes_anything(tmp_path: Path) -> None:
    """Without ``--apply`` the script reports and stops.

    Removing a container is not something to do by accident: the
    listing may be stale, the threshold may be wrong, and the container
    may be one somebody is about to look at.  So the default run says
    what it would do and does nothing -- the daemon is not asked to
    remove a single container.
    """
    env = _environment(
        tmp_path,
        [_row("pdt-ci-unit-123", "exited", CURRENT_UID, _old_enough_timestamp())],
    )

    result = _run(env)

    assert result.returncode == 0, (
        f"the script failed: {result.stdout!r} / {result.stderr!r}"
    )
    assert "would remove pdt-ci-unit-123" in result.stdout, (
        f"the script did not report the container it would remove: {result.stdout!r}"
    )
    assert _removed(env) == [], (
        "the dry run removed a container: "
        f"{_docker_calls(env)!r}"
    )
    assert _calls_to(env, "rm") == [], (
        "the dry run asked the daemon to remove a container: "
        f"{_docker_calls(env)!r}"
    )


# ---------------------------------------------------------------------------
# --apply removes only what matches
# ---------------------------------------------------------------------------


def test_apply_removes_only_our_old_stopped_container(tmp_path: Path) -> None:
    """``--apply`` removes the container that matches all four conditions.

    The four conditions are the whole point of the script: the
    container carries ``pdt-ci=1``, it carries ``pdt-owner`` with this
    user's id, it carries a ``pdt-created`` timestamp older than two
    hours, and it is not running.  The other three containers in the
    listing fail one of them each, and none of them may be touched.
    """
    env = _environment(
        tmp_path,
        [
            # The one that matches all four conditions.
            _row("pdt-ci-unit-123", "exited", CURRENT_UID, _old_enough_timestamp()),
            # Too young.
            _row("pdt-ci-unit-124", "exited", CURRENT_UID, _recent_timestamp()),
            # Somebody else's.
            _row("pdt-ci-unit-125", "exited", "0", _old_enough_timestamp()),
            # Still running.
            _row("pdt-ci-unit-126", "running", CURRENT_UID, _old_enough_timestamp()),
        ],
    )

    result = _run(env, "--apply")

    assert result.returncode == 0, (
        f"the script failed: {result.stdout!r} / {result.stderr!r}"
    )
    assert _removed(env) == ["pdt-ci-unit-123"], (
        f"the script removed the wrong containers: {_removed(env)!r}"
    )
    assert "removed pdt-ci-unit-123" in result.stdout, (
        f"the script did not report the removal: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# The containers that are never removed
# ---------------------------------------------------------------------------


def test_a_running_container_is_never_removed(tmp_path: Path) -> None:
    """A running container belongs to somebody right now.

    Its age says nothing: a lane may have been running for hours, and
    the container is still in use.  It is not a leftover, so it stays.
    """
    env = _environment(
        tmp_path,
        [_row("pdt-ci-unit-123", "running", CURRENT_UID, _old_enough_timestamp())],
    )

    result = _run(env, "--apply")

    assert result.returncode == 0, (
        f"the script failed: {result.stdout!r} / {result.stderr!r}"
    )
    assert _removed(env) == [], (
        f"the script removed a running container: {_docker_calls(env)!r}"
    )
    assert _calls_to(env, "rm") == [], (
        f"the script asked the daemon to remove a running container: "
        f"{_docker_calls(env)!r}"
    )
    assert "pdt-ci-unit-123" not in result.stdout, (
        f"the script reported a running container as removable: {result.stdout!r}"
    )


def test_a_paused_container_is_removed(tmp_path: Path) -> None:
    """A paused container is not running, so it is a leftover to remove.

    The daemon refuses to remove a paused container as it is -- it has
    to be unpaused first -- but "paused" is not "running": nothing is
    using that container, and it matches the other three conditions.
    So the script removes it, forcing the removal when the plain one
    is refused.
    """
    env = _environment(
        tmp_path,
        [_row("pdt-ci-unit-123", "paused", CURRENT_UID, _old_enough_timestamp())],
        rm_fails=["pdt-ci-unit-123"],
    )

    result = _run(env, "--apply")

    assert result.returncode == 0, (
        f"the script failed: {result.stdout!r} / {result.stderr!r}"
    )
    assert _removed(env) == ["pdt-ci-unit-123"], (
        f"the script did not remove the paused container: {_docker_calls(env)!r}"
    )
    assert "removed pdt-ci-unit-123" in result.stdout, (
        f"the script did not report the removal: {result.stdout!r}"
    )


def test_a_container_that_started_running_again_is_not_removed(tmp_path: Path) -> None:
    """A container that is running again is not removed, even forced.

    The listing is a snapshot: between reading it and removing the
    container, the container may have been started again.  Forcing the
    removal would kill whatever is running in there, so the script
    asks the daemon once more before it forces anything -- and a
    container that is running now stays, with the refusal reported.
    """
    env = _environment(
        tmp_path,
        [_row("pdt-ci-unit-123", "exited", CURRENT_UID, _old_enough_timestamp())],
        rm_fails=["pdt-ci-unit-123"],
        running=["pdt-ci-unit-123"],
    )

    result = _run(env, "--apply")

    assert _removed(env) == [], (
        f"the script killed a container that was running again: "
        f"{_docker_calls(env)!r}"
    )
    assert result.returncode == 1, (
        f"the script did not report the refusal: {result.stdout!r} / {result.stderr!r}"
    )
    assert "could not remove pdt-ci-unit-123" in result.stderr, (
        f"the script did not say why it left the container: {result.stderr!r}"
    )


def test_another_users_container_is_never_removed(tmp_path: Path) -> None:
    """A container owned by somebody else is not ours to remove.

    The driver labels every container it starts with ``pdt-owner=<uid>``.
    A container carrying somebody else's id was started by another
    user -- on this machine, by another session.  It may be old and
    stopped, and it still is not ours.
    """
    other_uid = str(int(CURRENT_UID) + 1)
    env = _environment(
        tmp_path,
        [_row("pdt-ci-unit-123", "exited", other_uid, _old_enough_timestamp())],
    )

    result = _run(env, "--apply")

    assert result.returncode == 0, (
        f"the script failed: {result.stdout!r} / {result.stderr!r}"
    )
    assert _removed(env) == [], (
        f"the script removed another user's container: {_docker_calls(env)!r}"
    )


def test_an_unlabelled_container_is_never_removed(tmp_path: Path) -> None:
    """A container without the pdt labels is not one of ours.

    A container that carries no ``pdt-owner`` and no ``pdt-created``
    label was not started by the gate driver -- it belongs to
    something else on the machine.  The script cannot tell how old it
    is, and a container whose age is unknown is not a leftover it may
    remove.
    """
    env = _environment(
        tmp_path,
        [_row("some-other-container", "exited", "", "")],
    )

    result = _run(env, "--apply")

    assert result.returncode == 0, (
        f"the script failed: {result.stdout!r} / {result.stderr!r}"
    )
    assert _removed(env) == [], (
        f"the script removed an unlabelled container: {_docker_calls(env)!r}"
    )


def test_a_container_without_a_usable_timestamp_is_never_removed(tmp_path: Path) -> None:
    """A missing or malformed ``pdt-created`` label means "unknown age".

    The two hour threshold is the whole reason the script may remove a
    container at all.  Without a timestamp to compare against, that
    reason is gone -- so the container stays.
    """
    env = _environment(
        tmp_path,
        [
            _row("pdt-ci-unit-123", "exited", CURRENT_UID, ""),
            _row("pdt-ci-unit-124", "exited", CURRENT_UID, "not-a-number"),
        ],
    )

    result = _run(env, "--apply")

    assert result.returncode == 0, (
        f"the script failed: {result.stdout!r} / {result.stderr!r}"
    )
    assert _removed(env) == [], (
        f"the script removed a container of unknown age: {_docker_calls(env)!r}"
    )


# ---------------------------------------------------------------------------
# A listing that cannot be read
# ---------------------------------------------------------------------------


def test_a_failing_docker_ps_exits_with_one(tmp_path: Path) -> None:
    """When the listing cannot be read, the script says why and stops.

    A daemon that is down, or a socket that moved, must not turn into
    "there is nothing to clean up" -- that would report success for a
    cleanup that never happened.  The script exits 1 and puts the
    reason on stderr.
    """
    env = _environment(tmp_path, [], PDT_CI_TEST_PS_FAIL="1")

    result = _run(env)

    assert result.returncode == 1, (
        f"the script did not report the failure: {result.stdout!r} / {result.stderr!r}"
    )
    assert "Cannot connect to the Docker daemon" in result.stderr, (
        f"the script did not print the reason: {result.stderr!r}"
    )
    assert _removed(env) == [], (
        f"the script removed a container after a failed listing: "
        f"{_docker_calls(env)!r}"
    )


# ---------------------------------------------------------------------------
# The listing the script asks for
# ---------------------------------------------------------------------------


def test_the_listing_is_restricted_to_gate_containers(tmp_path: Path) -> None:
    """The script asks docker for the gate containers and nothing else.

    The ``pdt-ci=1`` filter is what keeps the unlabelled containers of
    the machine out of the listing in the first place; the script never
    sees them, so it never has to decide about them.
    """
    env = _environment(tmp_path, [])

    result = _run(env)

    assert result.returncode == 0, (
        f"the script failed: {result.stdout!r} / {result.stderr!r}"
    )
    calls = _docker_calls(env)
    assert calls, "the script did not talk to docker at all"
    assert calls[0].startswith("ps "), f"unexpected first call: {calls[0]!r}"
    assert "--filter label=pdt-ci=1" in calls[0], (
        f"the listing was not restricted to gate containers: {calls[0]!r}"
    )
    assert "-a" in calls[0], (
        f"the listing did not include the stopped containers: {calls[0]!r}"
    )
