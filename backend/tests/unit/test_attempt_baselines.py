"""The attempt baseline is recorded once and reused by every later attempt.

What this pins
--------------
A task's attempt baseline is the commit the attempt started from. If it
is re-read from "whatever HEAD is right now" on every attempt, then the
second attempt of a task sees the *first* attempt's committed work as
part of its own starting point -- the baseline has silently swallowed
the previous attempt's成果, and the attempt can no longer tell what it
actually changed.

``backend/attempt_baselines.py`` is the file layer that closes that
gap: one JSON document per plan directory, written atomically, read
back by key. These five tests pin the *file* contract only -- no
executor, no scheduler, no routes:

* **first put** -- the first attempt's sha is recorded and lands in the
  file under its lineage and task id;
* **second put** -- a later attempt with a *different* sha gets the
  first one back, so the baseline is pinned to the first attempt;
* **sibling** -- two tasks in the same lineage keep separate entries,
  so a sibling that finished first is not folded into the baseline of
  the task that runs later;
* **unusable file** -- a missing or corrupt document reads as ``""``,
  never as an exception, because "no baseline yet" must always be a
  reachable answer;
* **failed write** -- a store that cannot write (read-only directory)
  returns the sha it was handed and raises nothing, because losing the
  baseline must not take down the attempt that was otherwise fine.

The document shape is pinned too, since it is the contract between this
module and whoever reads it next::

    {"version": 1, "lineages": {"<root>": {"<task_id>": "<sha>"}}}
"""

import json
import os
from pathlib import Path

from attempt_baselines import BaselineStore


def _read_document(path: Path) -> dict:
    """The stored document, read straight off disk."""
    return json.loads(path.read_text(encoding="utf-8"))


def test_first_put_records_and_persists(tmp_path: Path) -> None:
    """The first attempt's sha is returned and lands in the file."""
    path = tmp_path / ".pdt" / "attempt_baselines.json"
    store = BaselineStore(path)

    assert store.put_if_absent("repair", "repair-03", "a1b2c3") == "a1b2c3"

    document = _read_document(path)
    assert document["version"] == 1
    assert document["lineages"]["repair"]["repair-03"] == "a1b2c3"
    assert store.get("repair", "repair-03") == "a1b2c3"


def test_second_put_reuses_first_baseline(tmp_path: Path) -> None:
    """A later attempt is handed the *first* attempt's baseline back."""
    path = tmp_path / "attempt_baselines.json"
    store = BaselineStore(path)

    assert store.put_if_absent("repair", "repair-03", "a1b2c3") == "a1b2c3"
    # The second attempt starts from a different HEAD -- that is the
    # whole point of the feature. It must not become the baseline.
    assert store.put_if_absent("repair", "repair-03", "d4e5f6") == "a1b2c3"
    assert store.get("repair", "repair-03") == "a1b2c3"
    assert _read_document(path)["lineages"]["repair"]["repair-03"] == "a1b2c3"


def test_sibling_task_gets_its_own_entry(tmp_path: Path) -> None:
    """Two tasks in one lineage keep separate baselines."""
    path = tmp_path / "attempt_baselines.json"
    store = BaselineStore(path)

    assert store.put_if_absent("repair", "repair-01", "aaa111") == "aaa111"
    assert store.put_if_absent("repair", "repair-03", "bbb222") == "bbb222"

    # A sibling that finished first must not end up inside the other
    # sibling's baseline.
    assert store.get("repair", "repair-01") == "aaa111"
    assert store.get("repair", "repair-03") == "bbb222"

    # ... and neither sibling can move the other's recorded baseline.
    assert store.put_if_absent("repair", "repair-01", "ccc333") == "aaa111"
    assert store.put_if_absent("repair", "repair-03", "ddd444") == "bbb222"

    document = _read_document(path)
    assert document["lineages"]["repair"] == {
        "repair-01": "aaa111",
        "repair-03": "bbb222",
    }


def test_missing_or_corrupt_file_returns_empty(tmp_path: Path) -> None:
    """A missing or corrupt document reads as ``""`` and never raises."""
    path = tmp_path / "attempt_baselines.json"
    store = BaselineStore(path)

    # Missing file -- nothing has been recorded yet.
    assert store.get("repair", "repair-03") == ""

    # Corrupt file -- a document torn by a crash, or written by
    # something else. "No baseline" must be a reachable answer.
    path.write_text("{not json at all", encoding="utf-8")
    assert store.get("repair", "repair-03") == ""

    # A document whose shape is not the one this code writes.
    path.write_text(json.dumps({"version": 1, "lineages": "not-a-dict"}),
                    encoding="utf-8")
    assert store.get("repair", "repair-03") == ""


def test_write_failure_does_not_raise(tmp_path: Path) -> None:
    """A store that cannot write returns the sha and raises nothing."""
    # (a) The classic "the disk says no": a read-only directory.
    read_only = tmp_path / "read-only"
    read_only.mkdir()
    read_only.chmod(0o555)
    try:
        store = BaselineStore(read_only / "attempt_baselines.json")
        assert store.put_if_absent("repair", "repair-03", "a1b2c3") == "a1b2c3"
        if os.geteuid() != 0:
            # The write really did fail (root ignores the permission
            # bits, so this assertion only holds for a normal user).
            assert not (read_only / "attempt_baselines.json").exists()
    finally:
        read_only.chmod(0o755)

    # (b) A parent path that cannot hold a file at all -- the write
    # fails on every user, root included.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    store = BaselineStore(blocker / "attempt_baselines.json")
    assert store.put_if_absent("repair", "repair-03", "a1b2c3") == "a1b2c3"
