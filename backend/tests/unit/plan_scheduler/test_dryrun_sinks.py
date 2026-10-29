"""Unit tests: the scheduler's decision sinks.

What this file holds down
-------------------------
The scheduler runs **one** tick pipeline against **one** policy.  What
happens to the decisions that pipeline produces is a question of its
own, and the answers must not change the decisions themselves: a
dry run that decided differently from the live engine would say nothing
at all about the live engine.

The four contracts this file pins:

1. **One decision list, two exits.**  The same list handed to the
   execute sink and to the record sink yields the same ``action`` /
   ``plan_id`` / ``reason`` for every item.
2. **A record is one JSON object per line.**  Three decisions leave
   three complete JSONL lines.
3. **Recording is inert.**  The record sink calls neither the plan
   lifecycle nor a subprocess.
4. **Nothing to say, nothing written.**  An empty decision list leaves
   the target directory exactly as it was.

And the dry run's own exit — the writer that leaves the three
artifacts the cutover gate is replayed against — is a sink like the
other two, and writes down the same decisions.

The lifecycle stand-in is a whole *module* injected into
``sys.modules`` rather than a patched attribute: the record sink is
free to import the lifecycle lazily inside ``apply``, and the stand-in
has to catch that too.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from plan_scheduler.dryrun import DecisionSink, DryRunWriter, ExecuteSink, RecordSink

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: The decisions both sinks are fed.  Plain data, shared by every test:
#: the whole point of the two sinks is that they see the same list.
_DECISIONS: List[Dict[str, Any]] = [
    {
        "ts": "2026-10-27T01:00:00Z",
        "action": "admit",
        "plan_id": "p1",
        "reason": "waiting_for_slot",
    },
    {
        "ts": "2026-10-27T01:00:05Z",
        "action": "admit",
        "plan_id": "p2",
        "reason": "capacity_available",
    },
    {
        "ts": "2026-10-27T01:00:10Z",
        "action": "skip",
        "plan_id": "p3",
        "reason": "min_runtime_not_elapsed",
    },
]


def _decisions() -> List[Dict[str, Any]]:
    """A fresh copy of the shared decision list.

    Every test gets its own dictionaries, so a test that hands a
    decision to something that mutates it cannot reach into the next
    test's data.
    """
    return [dict(decision) for decision in _DECISIONS]


def _echo_executor(
    seen: List[Dict[str, Any]],
) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """An executor that remembers what it was given and echoes it back.

    The echo is what makes the two exits comparable: the executor's
    result carries the same ``action`` / ``plan_id`` / ``reason`` the
    record sink writes down, so a difference between the two is a
    difference between the exits and not between the inputs.
    """

    def executor(decision: Dict[str, Any]) -> Dict[str, Any]:
        seen.append(decision)
        return dict(decision)

    return executor


# ---------------------------------------------------------------------------
# 1. One decision list, two exits
# ---------------------------------------------------------------------------


def test_two_sinks_same_decision_list(tmp_path: Path) -> None:
    """The same decisions come out of both exits, item for item."""
    decisions = _decisions()

    seen: List[Dict[str, Any]] = []
    executed = ExecuteSink(_echo_executor(seen)).apply(decisions)
    recorded = RecordSink(tmp_path / "out").apply(decisions)

    assert len(executed) == len(recorded) == len(_DECISIONS)
    for executed_item, recorded_item in zip(executed, recorded):
        assert executed_item["action"] == recorded_item["action"]
        assert executed_item["plan_id"] == recorded_item["plan_id"]
        assert executed_item["reason"] == recorded_item["reason"]

    # The executor was handed exactly the decisions the recorder wrote
    # down — the exits differ, the decisions do not.
    assert seen == decisions


# ---------------------------------------------------------------------------
# 2. A record is one JSON object per line
# ---------------------------------------------------------------------------


def test_record_sink_appends_jsonl(tmp_path: Path) -> None:
    """Three decisions leave three complete JSONL lines."""
    sink = RecordSink(tmp_path / "out")
    sink.apply(_decisions())

    lines = sink.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line, decision in zip(lines, _decisions()):
        record = json.loads(line)
        assert record == decision
        assert record["action"] == decision["action"]
        assert record["plan_id"] == decision["plan_id"]
        assert record["reason"] == decision["reason"]


def test_record_sink_records_the_canonical_fields(tmp_path: Path) -> None:
    """A record carries ``ts`` / ``action`` / ``plan_id`` / ``reason``.

    The architecture's decision point 7 pins the record's shape: the
    sink appends those four fields, and nothing else.  A decision that
    carries more than the tick needs is recorded as the comparable core.
    """
    decision = dict(_DECISIONS[0])
    decision["expected_slot"] = 3

    sink = RecordSink(tmp_path / "out")
    sink.apply([decision])

    record = json.loads(sink.path.read_text(encoding="utf-8").splitlines()[0])
    assert record == {
        "ts": decision["ts"],
        "action": decision["action"],
        "plan_id": decision["plan_id"],
        "reason": decision["reason"],
    }


# ---------------------------------------------------------------------------
# 3. Recording is inert
# ---------------------------------------------------------------------------


def test_record_sink_never_calls_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording a decision changes nothing but the file it writes."""
    calls: List[str] = []

    class _LifecycleDouble:
        """Stands in for the plan lifecycle and remembers every call."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls.append("PlanLifecycleService()")

        def start_plan(self, *args: Any, **kwargs: Any) -> None:
            calls.append("start_plan")

        def stop_plan(self, *args: Any, **kwargs: Any) -> None:
            calls.append("stop_plan")

    lifecycle_double = types.ModuleType("plan_lifecycle_service")
    lifecycle_double.PlanLifecycleService = _LifecycleDouble
    monkeypatch.setitem(sys.modules, "plan_lifecycle_service", lifecycle_double)

    def _no_subprocess(*args: Any, **kwargs: Any) -> None:
        calls.append("subprocess")

    monkeypatch.setattr(subprocess, "Popen", _no_subprocess)
    monkeypatch.setattr(subprocess, "run", _no_subprocess)

    sink = RecordSink(tmp_path / "out")
    sink.apply(_decisions())

    assert calls == []
    # The sink did its job — it was not inert by refusing to work.
    assert sink.path.is_file()


# ---------------------------------------------------------------------------
# 4. Nothing to say, nothing written
# ---------------------------------------------------------------------------


def test_empty_decisions_writes_nothing(tmp_path: Path) -> None:
    """An empty decision list leaves the target directory alone."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    sink = RecordSink(out_dir)
    assert sink.apply([]) == []
    assert list(out_dir.iterdir()) == []

    # A directory that was never there is not created either.
    absent = tmp_path / "absent"
    assert RecordSink(absent).apply([]) == []
    assert not absent.exists()


# ---------------------------------------------------------------------------
# The rest of the contract
# ---------------------------------------------------------------------------


def test_record_sink_creates_the_directory_it_was_given(tmp_path: Path) -> None:
    """A missing directory is created when the sink writes to it."""
    out_dir = tmp_path / "nested" / "deep"

    sink = RecordSink(out_dir)
    sink.apply(_decisions())

    assert out_dir.is_dir()
    assert sink.path.is_file()


def test_record_sink_appends_and_never_overwrites(tmp_path: Path) -> None:
    """A second run adds to the record instead of replacing it."""
    sink = RecordSink(tmp_path / "out")

    sink.apply(_decisions())
    sink.apply(_decisions())

    lines = sink.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6
    assert lines[:3] == lines[3:]


def test_execute_sink_calls_the_executor_once_per_decision(tmp_path: Path) -> None:
    """The executor is called for every decision, in order."""
    seen: List[Dict[str, Any]] = []

    results = ExecuteSink(_echo_executor(seen)).apply(_decisions())

    assert len(results) == len(_DECISIONS)
    assert seen == _decisions()


def test_both_sinks_share_one_interface(tmp_path: Path) -> None:
    """Both exits are the same kind of thing, and the interface is abstract."""
    assert issubclass(ExecuteSink, DecisionSink)
    assert issubclass(RecordSink, DecisionSink)

    assert isinstance(ExecuteSink(lambda decision: dict(decision)), DecisionSink)
    assert isinstance(RecordSink(tmp_path / "out"), DecisionSink)

    with pytest.raises(TypeError):
        DecisionSink()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# 5. The dry run's own exit: the writer that leaves the three artifacts
# ---------------------------------------------------------------------------


def test_the_dry_run_exit_is_a_decision_sink(tmp_path: Path) -> None:
    """The writer that leaves the three artifacts is a sink like the others.

    The dry run's exit is the writer: it records the decisions and
    leaves ``ticks.jsonl``, ``windows.json`` and ``diff.md`` behind.
    It is the same kind of thing as the other two exits, so the tick
    holds one type and never asks "are we dry right now?".
    """
    writer = DryRunWriter(tmp_path / "out")

    assert isinstance(writer, DecisionSink)
    assert issubclass(DryRunWriter, DecisionSink)


def test_the_dry_run_exit_writes_the_same_decisions_as_the_record_sink(
    tmp_path: Path,
) -> None:
    """The two dry-run exits write down the same decisions.

    The writer is the dry run's exit in production; the record sink is
    the plain one.  They have to see the same decisions, or the
    artifact that is compared is not the artifact that was recorded.
    """
    writer = DryRunWriter(tmp_path / "writer")
    recorder = RecordSink(tmp_path / "recorder")

    written = writer.apply(_decisions())
    recorded = recorder.apply(_decisions())

    assert written == recorded
    assert [record["action"] for record in written] == [
        decision["action"] for decision in _decisions()
    ]


def test_the_dry_run_exit_leaves_the_three_artifacts(tmp_path: Path) -> None:
    """The writer's artifacts are the three the gate is replayed against."""
    writer = DryRunWriter(tmp_path / "out")

    writer.apply(_decisions())

    day = writer.day_dir
    assert day.is_dir()
    for name in ("ticks.jsonl", "windows.json", "diff.md"):
        path = day / name
        assert path.is_file(), "{0} was not written".format(path)
        assert path.stat().st_size > 0, "{0} is empty".format(path)
