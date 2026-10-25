"""The pause checkpoint can name the decision that caused the pause.

What this pins
--------------
A strict-mode verification run that waits for a human and hits its
deadline writes a ``pause_state.json`` so the plan can be resumed
later.  That pause is not anonymous: it was caused by one specific
HITL decision, and the resume path has to be able to find that
decision again -- which it can only do if the checkpoint carries the
``decision_id`` that triggered it.

So this file pins four things:

* **round trip** -- the ``decision_id`` handed to the writer comes back
  from a fresh read, and shows up in the summary a listing reads;
* **legacy** -- a checkpoint written before the key existed is still
  readable, and still summarisable;
* **absent** -- a pause with no decision behind it does not grow a
  ``decision_id`` key: a key that is present-and-null claims something
  different from a key that was never written;
* **fixed keys** -- the document's key set stays fixed, and an unknown
  keyword is still rejected rather than smuggled onto the disk.

The writer is called the way the strict-mode caller calls it --
``kind``/``reason``/``decision_id``/``interrupted_tasks`` and nothing
else -- because that is the shape the pause actually has: a decision
that timed out, with no task caught mid-flight.
"""

import json

import pytest

from pause_state import (
    checkpoint_path,
    checkpoint_summary,
    read_pause_checkpoint,
    write_pause_checkpoint,
)


def _write(plan_dir, **overrides):
    """Write a checkpoint in the shape a strict-mode timeout produces."""
    kwargs = {
        "kind": "verification",
        "reason": "hitl_strict_timeout",
        "interrupted_tasks": [],
    }
    kwargs.update(overrides)
    return write_pause_checkpoint(plan_dir, **kwargs)


def _on_disk(plan_dir):
    """The checkpoint exactly as it sits on the disk."""
    return json.loads(checkpoint_path(plan_dir).read_text(encoding="utf-8"))


def test_decision_id_roundtrip(tmp_path):
    """The decision id written comes back from a fresh read and a summary."""
    written = _write(tmp_path, decision_id="d1")

    assert written["decision_id"] == "d1"
    assert _on_disk(tmp_path)["decision_id"] == "d1"

    read = read_pause_checkpoint(tmp_path)
    assert read["decision_id"] == "d1"

    assert checkpoint_summary(tmp_path)["decision_id"] == "d1"


def test_legacy_checkpoint_ok(tmp_path):
    """A checkpoint written before the key existed is still readable."""
    legacy = {
        "version": 1,
        "kind": "verification",
        "project_dir": "/w/app",
        "paused_at": "2026-10-20T12:00:00+00:00",
        "reason": "hitl_strict_timeout",
        "interrupted_tasks": [],
        "unfinished_tasks": [],
        "verification_round": None,
    }
    checkpoint_path(tmp_path).write_text(json.dumps(legacy), encoding="utf-8")

    read = read_pause_checkpoint(tmp_path)
    assert read is not None
    assert read["kind"] == "verification"
    assert read["reason"] == "hitl_strict_timeout"
    assert "decision_id" not in read

    summary = checkpoint_summary(tmp_path)
    assert summary["kind"] == "verification"
    assert "decision_id" not in summary


def test_absent_decision_id_writes_no_key(tmp_path):
    """No decision behind the pause means no key -- not a null one."""
    written = _write(tmp_path)

    assert "decision_id" not in written
    assert "decision_id" not in _on_disk(tmp_path)
    assert "decision_id" not in checkpoint_summary(tmp_path)


def test_existing_fields_unchanged(tmp_path):
    """kind / reason / interrupted_tasks keep their existing meaning."""
    _write(
        tmp_path,
        kind="verification",
        reason="hitl_strict_timeout",
        interrupted_tasks=[{"task_id": "3", "status": "in_progress"}],
        decision_id="d1",
    )

    read = read_pause_checkpoint(tmp_path)
    assert read["kind"] == "verification"
    assert read["reason"] == "hitl_strict_timeout"
    assert read["interrupted_tasks"][0]["task_id"] == "3"


def test_document_key_set_stays_fixed(tmp_path):
    """Adding the key did not open the document up to arbitrary keys."""
    _write(tmp_path, decision_id="d1")

    assert set(_on_disk(tmp_path)) == {
        "version",
        "kind",
        "project_dir",
        "paused_at",
        "reason",
        "interrupted_tasks",
        "unfinished_tasks",
        "verification_round",
        "decision_id",
    }


def test_unknown_key_rejected(tmp_path):
    """An unknown keyword is a TypeError, not a new on-disk key."""
    with pytest.raises(TypeError):
        write_pause_checkpoint(
            tmp_path,
            kind="verification",
            reason="hitl_strict_timeout",
            interrupted_tasks=[],
            nonsense="x",
        )

    assert not checkpoint_path(tmp_path).exists()
