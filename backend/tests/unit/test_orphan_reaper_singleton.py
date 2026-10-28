"""One reaper, one lock: the singleton guarantee and the daemon launch.

The orphan reaper kills processes.  A second copy of the reaper would
not merely be wasteful — two scanners would race over the same
candidates, both would ``SIGTERM`` the same pid, and the grace period
between ``SIGTERM`` and ``SIGKILL`` would be applied twice by two
independent actors.  Worse, a *reaper* that reaped another *reaper*
would be reaping itself.

So the daemon is a singleton, and the mechanism is the only one that
survives the thing it is protecting against: an ``flock`` on a file
under the plan-artifact root.  A lock is released by the kernel when
the holding process dies — no cleanup, no heartbeat, no "did the
previous owner exit politely" question.  A pid file cannot do this: the
pid in it may have been reused.

The lock is also the *only* honest way to ask "is the daemon
running?": there is no separate state to fall out of sync with the
process.  If the lock can be taken, nobody holds it, so there is no
daemon; if it cannot, somebody holds it, so there is one.  This file
pins that equivalence, because the alternative — a pid file, or a
"started at" timestamp — is exactly the kind of state that lies after a
crash.

The second half of the file is the launch.  The daemon is started
**detached**: it must outlive the backend that started it (the backend
is precisely the thing that tends to die), so it gets its own session
and it must carry the ``reaper`` marker so that it is never a candidate
for its own judgement.

What is *not* pinned here is the scan loop — the daemon's body lands in
a later stage.  What is pinned is the contract the body will be
started under, so that the loop cannot be written against a different
one.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import (  # noqa: E402
    KIND_REAPER,
    SPAWN_KIND,
    ReaperConfig,
)


class _FakePopen:
    """Records how the daemon was launched instead of launching it.

    The real thing would fork a long-lived process; the tests only
    care about the *contract* of the call, so the call is captured and
    a pid is handed back.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(pid=self.pid)

    @property
    def only(self):
        assert len(self.calls) == 1, "expected exactly one launch, got %d" % len(
            self.calls
        )
        return self.calls[0]


def _config(plans_root: Path, **overrides) -> ReaperConfig:
    kwargs = {"enabled": True, "interval": 17, "plans_root": plans_root}
    kwargs.update(overrides)
    return ReaperConfig(**kwargs)


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def test_lock_path_lives_under_plans_root(tmp_path):
    """The lock lives beside the plan artifacts, not in a temp dir.

    Its location is part of the contract: the daemon is started with a
    ``--plans-root``, and the lock has to follow that root, otherwise
    two daemons pointed at different roots would each believe they are
    alone.
    """
    path = reaper.lock_path(tmp_path)

    assert path == tmp_path / ".orphan_reaper" / "reaper.lock"


def test_second_lock_holder_gets_none(tmp_path):
    """The second holder is refused — immediately, without blocking.

    ``flock`` is taken with ``LOCK_NB``: a caller that has to wait is
    a caller that can hang the backend on a lock nobody will ever
    release promptly.  Refusal is expressed as ``None``, not as an
    exception, so a caller that only wants to *ask* (see
    :func:`daemon_alive`) does not have to catch anything.
    """
    first = reaper.acquire_lock(tmp_path)
    assert first is not None, "the first holder must get the lock"

    try:
        assert reaper.acquire_lock(tmp_path) is None
    finally:
        first.close()

    # Releasing the lock makes it available again — the refusal above
    # was about the lock being held, not about the file being broken.
    again = reaper.acquire_lock(tmp_path)
    assert again is not None
    again.close()


def test_daemon_alive_reflects_lock(tmp_path):
    """``daemon_alive`` is the lock, read backwards.

    Holding the lock means "somebody is the daemon" → alive.  Being
    able to take it means "nobody is" → not alive.  There is no second
    source of truth that could disagree with the first.
    """
    handle = reaper.acquire_lock(tmp_path)
    assert handle is not None
    try:
        assert reaper.daemon_alive(tmp_path) is True
    finally:
        handle.close()

    assert reaper.daemon_alive(tmp_path) is False


# ---------------------------------------------------------------------------
# The launch
# ---------------------------------------------------------------------------


def test_ensure_daemon_skips_when_alive(tmp_path):
    """A live daemon is not replaced.

    The check happens *before* the launch, so a second backend that
    starts while the daemon runs must not spawn a second one.
    """
    handle = reaper.acquire_lock(tmp_path)
    assert handle is not None
    try:
        fake = _FakePopen()
        assert reaper.ensure_daemon(_config(tmp_path), popen=fake) is None
        assert fake.calls == [], "no daemon may be launched while one is alive"
    finally:
        handle.close()


def test_ensure_daemon_env_has_reaper_tag(tmp_path):
    """The daemon is marked as a reaper.

    The marker is what stops the reaper from reaping itself: the scan
    finds a process carrying ``PDT_SPAWN_KIND=reaper`` and leaves it
    alone, whatever else is true about it.  A daemon launched without
    the marker is a candidate for its own judgement.
    """
    fake = _FakePopen()

    pid = reaper.ensure_daemon(_config(tmp_path), repo_root=tmp_path, popen=fake)

    assert pid == fake.pid
    env = fake.only[1]["env"]
    assert env[SPAWN_KIND] == KIND_REAPER


def test_ensure_daemon_command_line_and_detachment(tmp_path):
    """The daemon is started detached, with the configuration it needs.

    ``start_new_session=True`` is the whole point: without it the
    daemon stays in the backend's session and is killed with it — and
    the backend dying is exactly the situation the daemon exists for.
    The working directory is the repository root so that
    ``python -m backend.orphan_reaper`` resolves.
    """
    fake = _FakePopen()

    reaper.ensure_daemon(_config(tmp_path), repo_root=tmp_path, popen=fake)

    args, kwargs = fake.only
    cmd = list(args[0])
    assert cmd == [
        sys.executable,
        "-m",
        "backend.orphan_reaper",
        "--daemon",
        "--interval",
        "17",
        "--plans-root",
        str(tmp_path),
    ]
    assert kwargs["start_new_session"] is True
    assert Path(kwargs["cwd"]) == tmp_path
    # stdout and stderr share one append-only log, so the daemon's
    # complaints and its tracebacks land in the same file.
    assert kwargs["stdout"] is kwargs["stderr"]


def test_ensure_daemon_disabled_does_nothing(tmp_path):
    """A disabled reaper is not launched.

    ``PDT_ORPHAN_REAPER=0`` means "do not reap".  Launching a daemon
    anyway would be worse than not honouring the setting at all.
    """
    fake = _FakePopen()

    assert reaper.ensure_daemon(_config(tmp_path, enabled=False), popen=fake) is None
    assert fake.calls == []


def test_ensure_daemon_failure_is_a_warning_not_an_exception(tmp_path, caplog):
    """A failed launch is logged, never raised.

    The backend starts the daemon as a side effect of starting up.  If
    the launch raises, the backend dies of it — and the backend dying
    is the failure mode this whole module exists to survive.  A
    reaper that cannot start must be a warning, not a crash.
    """
    def _explode(*args, **kwargs):
        raise OSError("cannot fork")

    with caplog.at_level(logging.WARNING, logger=reaper.__name__):
        assert reaper.ensure_daemon(_config(tmp_path), popen=_explode) is None

    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_daemon_log_is_appended_under_the_reaper_directory(tmp_path):
    """The daemon's output is captured, not lost.

    A daemon whose stdout is inherited disappears into whatever the
    backend's stdout happened to be, which on a service host is a log
    nobody reads.  The log lives next to the lock, under the same
    plan-artifact root.
    """
    fake = _FakePopen()

    reaper.ensure_daemon(_config(tmp_path), repo_root=tmp_path, popen=fake)

    _, kwargs = fake.only
    log = Path(kwargs["stdout"].name)
    assert log == tmp_path / ".orphan_reaper" / "daemon.log"
    assert log.exists()


@pytest.mark.parametrize("enabled", [True, False])
def test_ensure_daemon_never_raises(tmp_path, enabled):
    """Whatever happens, the caller gets ``None`` or a pid — never an error."""
    result = reaper.ensure_daemon(
        _config(tmp_path, enabled=enabled),
        repo_root=tmp_path,
        popen=_FakePopen(),
    )
    assert result is None or isinstance(result, int)
