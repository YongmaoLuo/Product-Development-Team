"""Regression tests: the exec-progress watch keeps the card fresh
during the ``executing`` window.

2026-09-14: the Feishu card sat at "正在跑 tasks" for 30+ minutes while
the executor was
actually completing tasks one after another.

Root cause: the executor runs as a SUBPROCESS, so its
``STATE_EVENT_BUS`` publishes land in the subprocess's own in-process
bus and never reach the server's ``FeishuNotifier``. The card only
refreshed on server-side events (phase transitions, verification),
leaving the whole executing window silent.

2026-09-17 — the first fix watched the wrong thing. It stat'ed
``{project_dir}/tasks.json``, i.e. the *target repository's* file,
while the executor's ``--tasks-file`` is ``plans/<plan_id>/tasks.json``.
On an earlier plan the probe watched a file that had gone stale while
the real one changed, and the card froze for the whole task: the
header said "执行中" and named no task. Two defects, both pinned here:

  * **wrong path** — the proxy file was a foreign, stale copy;
  * **proxy at all** — the watched file's *content* carries no runtime
    state (``save_tasks`` writes static fields only); its mtime moved
    only because ``update_task_status`` happens to call ``save_tasks()``
    unconditionally, so any "skip the write when nothing changed"
    optimisation would have killed the probe.

The watch now polls the *state* — ``/api/execution/{id}/progress``,
the read model over the shared SQLite store the executor writes to —
and enqueues a synthetic ``plan_phase_changed`` when the fingerprint
moves. The existing min-interval (60 s) and fingerprint-dedup gates
keep unchanged cards from pushing.

Contract pinned here:

  * first sight records the baseline WITHOUT enqueuing (the startup
    sweep already pushed one fresh card per active plan);
  * a task status flip enqueues exactly one synthetic event carrying
    ``sub_kind="exec_progress_watch"`` — with NO file touched;
  * a task *starting* (the in-progress id changes) is also a change,
    so the card's "current task" line catches up instead of racing the
    first push by seconds;
  * an unchanged state enqueues nothing;
  * an unreadable / empty progress payload is skipped without crashing.
"""

from __future__ import annotations

import queue
from typing import Optional

import pytest

from notifications.feishu_notifier import FeishuNotifier
from notifications.state_events import KIND_PLAN_PHASE_CHANGED

PLAN_ID = "plan-watch"


@pytest.fixture
def notifier() -> FeishuNotifier:
    return FeishuNotifier(coalesce_seconds=0.05, min_interval_seconds=0.0)


def _drain(notifier: FeishuNotifier) -> list:
    events = []
    while True:
        try:
            events.append(notifier._queue.get_nowait())
        except queue.Empty:
            return events


def _progress(tasks, current: Optional[str] = None) -> dict:
    """A progress payload shaped like ``/api/execution/{id}/progress``."""
    counts = {"total": len(tasks)}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {
        "plan_id": PLAN_ID,
        "tasks": tasks,
        "current": {"id": current} if current else None,
        "counts": counts,
    }


_ONE_PENDING = [{"id": "1", "status": "pending"}]
_ONE_RUNNING = [{"id": "1", "status": "in_progress"}]
_ONE_DONE = [{"id": "1", "status": "completed"}]


def _setup(monkeypatch, payloads, plan_id: str = PLAN_ID) -> None:
    """Point the watch at a scripted progress feed (no files involved)."""
    state = {"payload": payloads[0] if payloads else None}
    monkeypatch.setattr(
        "notifications.feishu_notifier.list_active_plan_ids",
        lambda: [plan_id],
    )

    def _fetch(pid, base_url=None):
        return state["payload"]

    monkeypatch.setattr(
        "notifications.feishu_notifier.fetch_execution_progress", _fetch,
    )
    state["set"] = lambda p: state.__setitem__("payload", p)
    return state


def test_first_sight_baselines_without_enqueue(notifier, monkeypatch):
    _setup(monkeypatch, [_progress(_ONE_PENDING)])
    notifier._watch_execution_progress()
    assert _drain(notifier) == []
    assert notifier._exec_watch_refreshes == 0


def test_status_flip_enqueues_synthetic_event_without_any_file(
    notifier, monkeypatch,
):
    """The 2026-09-17 regression: state moves, no file is involved.

    The old implementation only noticed a change when a specific file's
    mtime moved. Here nothing on disk exists at all — a status flip must
    still schedule a refresh.
    """
    state = _setup(monkeypatch, [_progress(_ONE_PENDING)])
    notifier._watch_execution_progress()  # baseline
    _drain(notifier)

    state["set"](_progress(_ONE_DONE))
    notifier._watch_execution_progress()

    events = _drain(notifier)
    assert len(events) == 1
    assert events[0].kind == KIND_PLAN_PHASE_CHANGED
    assert events[0].plan_id == PLAN_ID
    assert events[0].payload.get("sub_kind") == "exec_progress_watch"
    assert notifier._exec_watch_refreshes == 1


def test_task_starting_is_a_change(notifier, monkeypatch):
    """A task flipping pending → in_progress must refresh the card.

    That flip is what puts the task name on the card's "current task"
    line. Missing it is exactly why that run card showed "正在跑 tasks"
    with no task for the whole run.
    """
    state = _setup(monkeypatch, [_progress(_ONE_PENDING)])
    notifier._watch_execution_progress()
    _drain(notifier)

    state["set"](_progress(_ONE_RUNNING, current="1"))
    notifier._watch_execution_progress()

    assert len(_drain(notifier)) == 1
    assert notifier._exec_watch_refreshes == 1


def test_unchanged_state_enqueues_nothing(notifier, monkeypatch):
    _setup(monkeypatch, [_progress(_ONE_PENDING)])
    notifier._watch_execution_progress()
    _drain(notifier)
    notifier._watch_execution_progress()
    assert _drain(notifier) == []
    assert notifier._exec_watch_refreshes == 0


def test_end_ts_only_churn_is_not_a_change(notifier, monkeypatch):
    """Status identical → no push, even if a row's timestamps moved."""
    state = _setup(monkeypatch, [_progress([{
        "id": "1", "status": "completed", "end_ts": "2026-09-17T01:00:00Z",
    }])])
    notifier._watch_execution_progress()
    _drain(notifier)
    state["set"](_progress([{
        "id": "1", "status": "completed", "end_ts": "2026-09-17T02:00:00Z",
    }]))
    notifier._watch_execution_progress()
    assert _drain(notifier) == []


@pytest.mark.parametrize("payload", [None, {}, {"tasks": []}, {"tasks": "x"}])
def test_unreadable_progress_is_skipped_silently(
    notifier, monkeypatch, payload,
):
    _setup(monkeypatch, [payload])
    notifier._watch_execution_progress()
    assert _drain(notifier) == []
    assert notifier._exec_watch_refreshes == 0


def test_fetch_failure_is_skipped_silently(notifier, monkeypatch):
    monkeypatch.setattr(
        "notifications.feishu_notifier.list_active_plan_ids",
        lambda: [PLAN_ID],
    )

    def _boom(pid, base_url=None):
        raise RuntimeError("backend down")

    monkeypatch.setattr(
        "notifications.feishu_notifier.fetch_execution_progress", _boom,
    )
    notifier._watch_execution_progress()  # must not raise
    assert _drain(notifier) == []
    assert notifier._exec_watch_refreshes == 0


# ---------------------------------------------------------------------------
# The fingerprint helper itself
# ---------------------------------------------------------------------------


def test_fingerprint_excludes_timestamps_and_includes_current():
    sig_a = FeishuNotifier._execution_state_fingerprint(_progress(
        [{"id": "1", "status": "completed", "end_ts": "t1"}], current=None,
    ))
    sig_b = FeishuNotifier._execution_state_fingerprint(_progress(
        [{"id": "1", "status": "completed", "end_ts": "t2"}], current=None,
    ))
    assert sig_a == sig_b

    sig_c = FeishuNotifier._execution_state_fingerprint(_progress(
        [{"id": "1", "status": "completed", "end_ts": "t1"}], current="1",
    ))
    assert sig_c != sig_a


def test_fingerprint_none_for_contentless_payloads():
    for payload in (None, {}, {"tasks": []}, {"tasks": "nope"}, []):
        assert FeishuNotifier._execution_state_fingerprint(payload) is None


def test_watch_no_longer_stats_any_file():
    """Guard against a regression to the mtime-proxy design.

    The 2026-09-17 bug was that the probe resolved a *file path* from
    the plan's target repository and compared its mtime. State is the
    only thing that should drive this watch.
    """
    import inspect

    src = inspect.getsource(FeishuNotifier._watch_execution_progress)
    assert "fetch_execution_progress" in src
    assert "st_mtime" not in src
