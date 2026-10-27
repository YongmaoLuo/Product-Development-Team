"""The ``task_session_bound`` event, written by one helper.

Background
----------
A task's session binding is worth recording twice: once when the
dispatch is about to start (the id it was *asked* for) and once when
the dispatch returns (the id it *actually* used — a provider fallback
may have switched it). Both records must have the same shape, because
a reader that has to understand two shapes will eventually understand
neither: the reader takes the LAST binding for a task and uses it.

``_log_task_session_bound`` is the one place that shape is written. It
takes the session handle, resolves the id it names, and writes one
``task_session_bound`` event carrying the session id, the attempt and
the stage the binding came from (``"pinned"`` — the id is derived and
the dispatch creates the session under it; ``"resumed"`` — the id names
a session that exists and is re-opened).

Nothing about this may cost a task. The event is a *record*: a missing
logger, a handle with no id at all, or a logger that raises while
writing all cost the record and nothing else.

TDD spec — 6 contract tests
---------------------------
1. ``test_writes_pinned_binding``: only a pinned id + ``stage="pinned"``
   → ``data.session_id`` is that id and ``data.stage == "pinned"``.
2. ``test_writes_resumed_binding``: ``session_id`` has a value →
   ``data.session_id`` is it.
3. ``test_skips_when_no_logger``: ``logger=None`` → no exception.
4. ``test_skips_when_no_session_id``: both ids empty → no event.
5. ``test_stage_defaults_to_unknown``: ``stage=None`` →
   ``data.stage == "unknown"``.
6. ``test_swallows_logger_failure``: a logger whose ``info`` raises →
   no exception.
"""

import sys
from pathlib import Path

import pytest

# Ensure backend/ is on sys.path so ``import agent`` works regardless of
# which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import agent as agent_module  # noqa: E402
from coding_tool import JsonSession  # noqa: E402

TASK_ID = "3"


class _RecordingLogger:
    """An execution logger that keeps what it was asked to write."""

    def __init__(self):
        self.entries = []

    def info(self, event, message, **kwargs):
        self.entries.append({"event": event, "message": message, **kwargs})

    @property
    def bindings(self):
        """Only the ``task_session_bound`` entries."""
        return [e for e in self.entries if e["event"] == "task_session_bound"]


class _ExplodingLogger:
    """A logger whose ``info`` fails — a broken logger is not a broken task."""

    def info(self, event, message, **kwargs):
        raise RuntimeError("the logger is broken")


def test_writes_pinned_binding():
    """A pinned id — one the dispatch will CREATE the session under.

    The handle has no ``session_id`` yet: the id is derived, and the
    dispatch creates the session under it. The event still names that
    id, because that is what the next process has to re-open.
    """
    logger = _RecordingLogger()

    agent_module._log_task_session_bound(
        logger,
        TASK_ID,
        JsonSession(pinned_session_id="sid-pinned", stage="pinned"),
        1,
    )

    assert len(logger.bindings) == 1, (
        f"expected exactly one binding, got {logger.entries!r}"
    )
    binding = logger.bindings[0]
    assert binding["task_id"] == TASK_ID
    assert binding["data"]["session_id"] == "sid-pinned", (
        "the event must name the id the dispatch is bound to; got "
        f"{binding['data'].get('session_id')!r}"
    )
    assert binding["data"]["stage"] == "pinned"
    assert binding["data"]["attempt"] == 1


def test_writes_resumed_binding():
    """A resumed id — one that names a session that already exists."""
    logger = _RecordingLogger()

    agent_module._log_task_session_bound(
        logger,
        TASK_ID,
        JsonSession(session_id="sid-resumed", stage="resumed"),
        2,
    )

    assert len(logger.bindings) == 1, (
        f"expected exactly one binding, got {logger.entries!r}"
    )
    binding = logger.bindings[0]
    assert binding["data"]["session_id"] == "sid-resumed", (
        "the event must name the session the dispatch re-opened; got "
        f"{binding['data'].get('session_id')!r}"
    )
    assert binding["data"]["stage"] == "resumed"
    assert binding["data"]["attempt"] == 2


def test_skips_when_no_logger():
    """No execution logger → no event, and no exception.

    A run without an execution log is a normal run; the binding simply
    has nowhere to be written.
    """
    agent_module._log_task_session_bound(
        None,
        TASK_ID,
        JsonSession(session_id="sid-1", stage="resumed"),
        1,
    )


def test_skips_when_no_session_id():
    """A handle with no id at all → nothing is written.

    Two shapes of nothing: an empty handle (both ids empty) and no
    handle at all (``session=None``). Neither names a session, so
    neither may produce an event a reader would take for a binding.
    """
    logger = _RecordingLogger()

    agent_module._log_task_session_bound(
        logger, TASK_ID, JsonSession(stage="resumed"), 1
    )
    agent_module._log_task_session_bound(logger, TASK_ID, None, 1)

    assert logger.bindings == [], (
        f"a handle with no id must not produce a binding, got {logger.entries!r}"
    )


def test_stage_defaults_to_unknown():
    """A handle with no stage → ``"unknown"``.

    The stage says where the id came from. A handle that does not say
    must not be recorded as ``"pinned"`` or ``"resumed"`` — those name
    a specific way the id was produced, and guessing one would be a
    lie the reader would act on.
    """
    logger = _RecordingLogger()

    agent_module._log_task_session_bound(
        logger, TASK_ID, JsonSession(session_id="sid-1"), 1
    )

    assert len(logger.bindings) == 1, (
        f"expected exactly one binding, got {logger.entries!r}"
    )
    assert logger.bindings[0]["data"]["stage"] == "unknown"


def test_swallows_logger_failure():
    """A logger that raises while writing → no exception.

    The event is a record of a binding that already happened. A logger
    that cannot write it costs the record, never the task.
    """
    agent_module._log_task_session_bound(
        _ExplodingLogger(),
        TASK_ID,
        JsonSession(session_id="sid-1", stage="resumed"),
        1,
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
