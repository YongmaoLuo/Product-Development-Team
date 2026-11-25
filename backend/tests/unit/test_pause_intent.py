"""The pause intent reaches the disk before anything acts on it.

What this pins
--------------
``backend/pause_state.py`` already keeps the *checkpoint* written when a
plan is interrupted. That is the record of what was caught mid-flight.
The pause **intent** is a different, earlier fact: "someone asked for
this plan to stop, and here is why". The repair round needs that fact on
disk to have an exit code it can defend -- without it the exit path can
only say "failed", because there is no evidence a pause was ever asked
for.

So this file pins the file contract of the intent record only:

* **round trip** -- what the writer stored is what a fresh read gives
  back (``reason`` and ``requested_at`` included);
* **missing** -- a plan that never got a pause intent reads as ``None``;
* **corrupt** -- an intent file torn by a crash reads as ``None``, never
  as an exception, because "no intent on record" must always be a
  reachable answer;
* **no reason** -- a document without a ``reason`` is not an intent and
  reads as ``None`` as well;
* **idempotent clear** -- clearing twice does not raise, because the
  "the pause was honoured" path clears unconditionally;
* **write failure** -- an ``OSError`` on the way to disk is surfaced to
  the caller, not swallowed here: this layer only stores the intent, it
  does not get to decide what a failed store means.

No executor, no scheduler, no routes -- a temp directory and the file
it holds, which is the whole point of putting the intent on disk.
"""

import json

import pytest

from pause_state import (
    PAUSE_INTENT_FILENAME,
    clear_pause_intent,
    intent_path,
    read_pause_intent,
    write_pause_intent,
)


def test_write_then_read_roundtrip(tmp_path):
    """What the writer stored is what a fresh read gives back."""
    plan_dir = tmp_path / "plans" / "p1"

    written = write_pause_intent(plan_dir, reason="user_pause")

    assert written["reason"] == "user_pause"
    assert written["requested_at"]

    read = read_pause_intent(plan_dir)

    assert read is not None
    assert read["reason"] == written["reason"]
    assert read["requested_at"] == written["requested_at"]
    assert read["schema_version"] == written["schema_version"]


def test_write_creates_plan_dir(tmp_path):
    """A plan directory that does not exist yet is created on write."""
    plan_dir = tmp_path / "plans" / "p1"
    assert not plan_dir.exists()

    write_pause_intent(plan_dir, reason="user_pause")

    assert plan_dir.is_dir()
    assert intent_path(plan_dir) == plan_dir / PAUSE_INTENT_FILENAME
    assert intent_path(plan_dir).is_file()


def test_read_missing_returns_none(tmp_path):
    """A plan that never got a pause intent reads as ``None``."""
    plan_dir = tmp_path / "empty"
    plan_dir.mkdir()

    assert read_pause_intent(plan_dir) is None


def test_read_corrupt_returns_none(tmp_path):
    """An intent file torn by a crash reads as ``None``, not an exception."""
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()
    (plan_dir / PAUSE_INTENT_FILENAME).write_text("{bad", encoding="utf-8")

    assert read_pause_intent(plan_dir) is None


def test_read_without_reason_returns_none(tmp_path):
    """A document without a ``reason`` is not an intent."""
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()
    (plan_dir / PAUSE_INTENT_FILENAME).write_text(
        json.dumps({"schema_version": 1, "requested_at": "2026-11-20T08:42:50"}),
        encoding="utf-8",
    )

    assert read_pause_intent(plan_dir) is None


def test_clear_is_idempotent(tmp_path):
    """Clearing twice does not raise, and the intent is gone afterwards."""
    plan_dir = tmp_path / "p1"
    write_pause_intent(plan_dir, reason="user_pause")
    assert intent_path(plan_dir).exists()

    clear_pause_intent(plan_dir)
    assert not intent_path(plan_dir).exists()

    clear_pause_intent(plan_dir)
    assert read_pause_intent(plan_dir) is None


def test_write_oserror_propagates(tmp_path, monkeypatch):
    """A failed write is surfaced -- this layer does not decide its meaning."""
    plan_dir = tmp_path / "p1"
    seen = {}

    def _boom(path, payload, **kwargs):
        seen.update(kwargs)
        raise OSError("disk is full")

    monkeypatch.setattr("pause_state.atomic_write_json", _boom)

    with pytest.raises(OSError):
        write_pause_intent(plan_dir, reason="user_pause")

    # The write must ask for the error to be raised: ``atomic_write_json``
    # swallows ``OSError`` by default, which would hide the very failure
    # this contract is about.
    assert seen.get("reraise") is True


def test_write_on_unusable_plan_dir_propagates(tmp_path):
    """A plan directory that cannot be created surfaces the ``OSError``."""
    plan_dir = tmp_path / "not-a-dir"
    plan_dir.write_text("i am a file", encoding="utf-8")

    with pytest.raises(OSError):
        write_pause_intent(plan_dir, reason="user_pause")
