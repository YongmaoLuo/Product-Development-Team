"""The zero-progress guard: what counts as progress, and when to stop.

The dispatch loop runs one *layer* at a time and reacts to a finished
layer by dispatching the next one immediately.  A layer that ends with
the task list exactly as it started therefore turns into a spin: the
loop sees the same tasks, dispatches the same work, and gets the same
nothing back, with nothing in the state to show that it happened.

:class:`progress_guard.ZeroProgressGuard` is what breaks that spin.  It
compares two snapshots of the task list, counts the layers that changed
nothing, waits longer before each retry, and finally says *stop* — with
a named reason, so a run that gave up can be told apart from a run that
finished.

What these tests pin
---------------------

* what counts as progress (a status change, a new task, a commit) and
  what does not (a layer that ends where it started);
* that the counter goes back to zero the moment the run makes progress
  again — a run that is working is allowed an occasional quiet layer;
* that the backoff doubles and is capped, so a broken layer does not
  turn into a thousand fast retries;
* that both knobs are named, that the environment can set them, and that
  an illegal value is not obeyed: a limit of zero layers would stop the
  run before it starts, and a negative wait is a mistake, not a long
  wait.  A backoff of zero *is* legal — it means "do not wait", which is
  what a test wants.

Every test here is pure: no server, no provider, no network, no clock.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from progress_guard import (  # noqa: E402
    DEFAULT_MAX_ZERO_PROGRESS_LAYERS,
    DEFAULT_ZERO_PROGRESS_BACKOFF_SECONDS,
    ENV_MAX_ZERO_PROGRESS_LAYERS,
    ENV_ZERO_PROGRESS_BACKOFF_SECONDS,
    MAX_ZERO_PROGRESS_BACKOFF_SECONDS,
    ZERO_PROGRESS_STOP_REASON,
    ZeroProgressGuard,
    task_progress_snapshot,
)


def _task(task_id: str, status: str = "pending", commit_sha=None):
    """One task in the shape the dispatcher hands around.

    ``SimpleNamespace`` stands in for ``task.SubTask``: the guard reads
    the three fields it cares about by attribute, so anything with
    those attributes is a task as far as it is concerned.
    """
    return SimpleNamespace(id=task_id, status=status, commit_sha=commit_sha)


def test_identical_snapshots_count_as_zero_progress():
    """Two layers that change nothing are two zero-progress layers.

    The first one is not a reason to stop — a single quiet layer can be
    a coincidence.  The second one, with the limit set to two, is the
    limit, and the guard says so.
    """
    assert ZERO_PROGRESS_STOP_REASON == "zero_progress"

    guard = ZeroProgressGuard(max_zero_progress_layers=2, backoff_seconds=5)
    before = task_progress_snapshot([_task("1"), _task("2")])

    assert guard.observe(before, before) is True
    assert guard.zero_progress_layers == 1
    assert guard.should_stop() is False
    assert guard.backoff_delay() == 5

    assert guard.observe(before, before) is True
    assert guard.zero_progress_layers == 2
    assert guard.should_stop() is True


def test_progress_resets_counter():
    """A layer that changes something wipes the zero-progress count.

    A run that is making progress is allowed an occasional quiet layer,
    and the counter that decides when to give up has to forget the
    quiet layers that came before the progress.
    """
    guard = ZeroProgressGuard(max_zero_progress_layers=2, backoff_seconds=5)
    before = task_progress_snapshot([_task("1"), _task("2")])
    guard.observe(before, before)
    assert guard.zero_progress_layers == 1

    after = task_progress_snapshot(
        [_task("1", status="completed", commit_sha="abc123"), _task("2")]
    )
    assert guard.observe(before, after) is False
    assert guard.zero_progress_layers == 0
    assert guard.should_stop() is False
    assert guard.backoff_delay() == 0


def test_backoff_grows_and_is_capped():
    """The wait doubles per zero-progress layer, and stops at the cap.

    The layers that follow a broken one are likely to be broken too, so
    the wait grows; but it grows to a ceiling and no further, because an
    unbounded backoff is a run that never comes back.
    """
    assert MAX_ZERO_PROGRESS_BACKOFF_SECONDS == 60.0

    guard = ZeroProgressGuard(max_zero_progress_layers=20, backoff_seconds=5)
    snapshot = task_progress_snapshot([_task("1")])

    delays = []
    for _ in range(7):
        guard.observe(snapshot, snapshot)
        delays.append(guard.backoff_delay())

    assert delays == [5, 10, 20, 40, 60, 60, 60]


def test_env_overrides_are_named_and_bounded(monkeypatch):
    """Both knobs are named, the environment sets them, and bad values lose.

    The names are part of the contract — an operator has to be able to
    find them — so they are spelled out here rather than read from the
    module and compared with themselves.
    """
    assert ENV_MAX_ZERO_PROGRESS_LAYERS == "PDT_ZERO_PROGRESS_MAX_LAYERS"
    assert ENV_ZERO_PROGRESS_BACKOFF_SECONDS == "PDT_ZERO_PROGRESS_BACKOFF_SECONDS"

    monkeypatch.delenv(ENV_MAX_ZERO_PROGRESS_LAYERS, raising=False)
    monkeypatch.delenv(ENV_ZERO_PROGRESS_BACKOFF_SECONDS, raising=False)

    # The environment is obeyed ...
    monkeypatch.setenv(ENV_MAX_ZERO_PROGRESS_LAYERS, "5")
    monkeypatch.setenv(ENV_ZERO_PROGRESS_BACKOFF_SECONDS, "2.5")
    guard = ZeroProgressGuard()
    assert guard.max_zero_progress_layers == 5
    assert guard.backoff_seconds == 2.5

    # ... a backoff of zero is a value, not a gap: it means "do not wait".
    monkeypatch.setenv(ENV_ZERO_PROGRESS_BACKOFF_SECONDS, "0")
    assert ZeroProgressGuard().backoff_seconds == 0.0

    # ... and an illegal value falls back to the documented default.
    for illegal in ("-1", "abc", ""):
        monkeypatch.setenv(ENV_MAX_ZERO_PROGRESS_LAYERS, illegal)
        assert (
            ZeroProgressGuard().max_zero_progress_layers
            == DEFAULT_MAX_ZERO_PROGRESS_LAYERS
        )
        monkeypatch.setenv(ENV_ZERO_PROGRESS_BACKOFF_SECONDS, illegal)
        assert (
            ZeroProgressGuard().backoff_seconds
            == DEFAULT_ZERO_PROGRESS_BACKOFF_SECONDS
        )


def test_reset_clears_the_counter():
    """``reset()`` forgets the quiet layers, so a fresh start is quiet."""
    guard = ZeroProgressGuard(max_zero_progress_layers=1, backoff_seconds=5)
    snapshot = task_progress_snapshot([_task("1")])
    guard.observe(snapshot, snapshot)
    assert guard.should_stop() is True

    guard.reset()
    assert guard.zero_progress_layers == 0
    assert guard.should_stop() is False
    assert guard.backoff_delay() == 0


def test_snapshot_reads_identity_status_and_commit():
    """The snapshot is the durable work, and nothing that moves on its own.

    A task that changes state, a task that appears and a task that
    commits are all progress.  A task that is merely *rewritten* — same
    state, same commit — is not, and the snapshot has to say so, or the
    guard would watch a spin look busy.
    """
    pending = task_progress_snapshot([_task("1"), _task("2")])

    # Order does not matter: the same work is the same snapshot.
    assert task_progress_snapshot([_task("2"), _task("1")]) == pending

    # A state change is progress.
    assert task_progress_snapshot([_task("1", status="completed"), _task("2")]) != pending

    # A new task is progress.
    assert task_progress_snapshot([_task("1"), _task("2"), _task("3")]) != pending

    # A commit is progress.
    assert (
        task_progress_snapshot([_task("1", commit_sha="abc123"), _task("2")]) != pending
    )

    # A row read as a mapping is a task too, not a special case.
    assert (
        task_progress_snapshot(
            [
                {"id": "1", "status": "pending", "commit_sha": None},
                {"id": "2", "status": "pending"},
            ]
        )
        == pending
    )

    # A field the row does not carry is empty -- not a guess at a
    # default, and not a value read off some other row.
    assert task_progress_snapshot([{"id": "1"}]) == (("1", "", ""),)
