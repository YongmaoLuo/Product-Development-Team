"""The kill audit module, tested on its own.

``kill_audit`` is the single recorder every process that sends signals
can install.  Its contract is small, and each part of it is a way for
the evidence to go missing:

* where the records go (the environment, or the platform temp dir);
* what one record contains (the target, the signal, the outcome);
* the cap, and the promise that a record that cannot be written never
  costs the signal it describes.

The recorder installs its hook at import time.  That is deliberate --
the hook has to be in place before the first signal -- so these tests
talk to the pure record functions and to the directory resolution
rather than re-installing it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import kill_audit  # noqa: E402


def _records(directory: Path) -> list:
    """Every record under *directory*, oldest first."""
    records = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def test_dir_honours_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path / "audit"))
    assert kill_audit.kill_audit_dir() == tmp_path / "audit"


def test_dir_falls_back_to_the_platform_temp_dir(monkeypatch):
    monkeypatch.delenv(kill_audit.KILL_AUDIT_DIR_ENV, raising=False)
    assert kill_audit.kill_audit_dir() == (
        Path(tempfile.gettempdir()) / kill_audit.KILL_AUDIT_DIR_NAME
    )


def test_write_kill_audit_writes_one_json_line(tmp_path, monkeypatch):
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    kill_audit.write_kill_audit("kill", 1234, 15)

    records = _records(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "kill"
    assert record["target"] == 1234
    assert record["signal"] == 15
    assert record["outcome"] == kill_audit.OUTCOME_ATTEMPTED
    assert record["pid"] == os.getpid()
    assert record["stack"], "a record without a caller names no sender"


def test_write_kill_outcome_carries_outcome_and_errno(tmp_path, monkeypatch):
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    kill_audit.write_kill_outcome(
        "killpg", 4321, 9, kill_audit.OUTCOME_REFUSED, errno=1,
    )

    record = _records(tmp_path)[-1]
    assert record["kind"] == "killpg"
    assert record["target"] == 4321
    assert record["signal"] == 9
    assert record["outcome"] == kill_audit.OUTCOME_REFUSED
    assert record["errno"] == 1


def test_the_cap_stops_writing_after_the_limit(tmp_path, monkeypatch):
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(kill_audit, "KILL_AUDIT_MAX_RECORDS", 2)
    monkeypatch.setattr(kill_audit, "_kill_audit_count", 0)

    for index in range(5):
        kill_audit.write_kill_audit("kill", index, 15)

    assert len(_records(tmp_path)) == 2


def test_an_unwritable_directory_does_not_raise(tmp_path, monkeypatch):
    """A record nobody can write must not cost the signal it describes."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(
        kill_audit.KILL_AUDIT_DIR_ENV, str(blocker / "audit"),
    )

    # The path's parent is a regular file: mkdir and open both fail.
    kill_audit.write_kill_audit("kill", 1, 15)
    kill_audit.write_kill_outcome("kill", 1, 15, kill_audit.OUTCOME_SENT)
