"""Reset one task's execution bookkeeping — the pure logic layer.

What is being pinned here
-------------------------------------------
The reset writes **execution bookkeeping** (``status`` /
``failure_reason`` / ``end_ts`` / ``commit_sha`` / ``attempt``) and
nothing else.  The task's **specification** (``title`` /
``description`` / ``files_to_modify`` / ``test_command`` /
``depends_on`` / ``project_dir``) is what the task *is*; the bookkeeping
is what the last run *did*.  Rewriting the bookkeeping does not change
what the task is asked to do, so it does not break the repository's
"task data may only be voided or created, never rewritten" rule (see
the verification-reset docstring in ``backend/routes/verification.py``)
— that rule protects the specification, and the module docstring of
``backend/task_reset.py`` says so at length.

These tests are the contract for that split: the reset field set is
exactly five columns and names no specification field; the ``cleared``
echo reports the previous values; and the audit log only ever grows.
"""

import json
import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import task_reset  # noqa: E402
from task_reset import (  # noqa: E402
    BOOKKEEPING_FIELDS,
    RESET_LOG_FILENAME,
    SPEC_FIELDS,
    TaskResetRequest,
    append_reset_log,
    build_reset_fields,
    cleared_payload,
    read_reset_log,
)


_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_build_reset_fields_writes_exactly_five_columns():
    """The reset writes exactly the five bookkeeping columns."""
    fields = build_reset_fields()

    assert set(fields) == set(BOOKKEEPING_FIELDS)
    assert len(fields) == 5
    assert fields == {
        "status": "pending",
        "failure_reason": None,
        "end_ts": None,
        "commit_sha": None,
        "attempt": 0,
    }


def test_build_reset_fields_names_no_spec_field():
    """A reset never touches the task's specification."""
    fields = build_reset_fields()

    assert set(fields).isdisjoint(set(SPEC_FIELDS))


def test_cleared_payload_reports_previous_values():
    """The ``cleared`` echo carries the values the reset wiped."""
    before = {
        "status": "failed",
        "failure_reason": "boom",
        "end_ts": "2026-11-12T01:00:00Z",
        "commit_sha": "a" * 40,
        "attempt": 3,
        "title": "keep me",
    }

    assert cleared_payload(before) == {
        "status": "failed",
        "failure_reason": "boom",
        "end_ts": "2026-11-12T01:00:00Z",
        "commit_sha": "a" * 40,
        "attempt": 3,
    }


def test_cleared_payload_uses_none_for_missing_keys():
    """A missing bookkeeping key reads as ``None``, never as a KeyError."""
    payload = cleared_payload({})

    assert set(payload) == set(BOOKKEEPING_FIELDS)
    assert all(value is None for value in payload.values())


def test_append_reset_log_keeps_previous_entries(tmp_path: Path):
    """The audit log grows; it never rewrites what is already there."""
    first = {
        "task_id": "T-1",
        "from_status": "failed",
        "to_status": "pending",
        "reason": "rerun after flaky test",
        "caller": "http",
        "forced": False,
    }
    second = {
        "task_id": "T-2",
        "from_status": "completed",
        "to_status": "pending",
        "reason": None,
        "caller": "operator",
        "forced": True,
    }

    append_reset_log(tmp_path, first)
    append_reset_log(tmp_path, second)

    entries = read_reset_log(tmp_path)
    assert len(entries) == 2
    for key, value in first.items():
        assert entries[0][key] == value
    for key, value in second.items():
        assert entries[1][key] == value


def test_append_reset_log_stamps_utc_at(tmp_path: Path):
    """Every entry carries a UTC timestamp in the agreed format."""
    append_reset_log(tmp_path, {"task_id": "T-1"})

    entries = read_reset_log(tmp_path)
    assert len(entries) == 1
    assert _AT_PATTERN.match(entries[0]["at"])


def test_read_reset_log_returns_empty_when_absent(tmp_path: Path):
    """No log file yet — the reader returns an empty list, not an error."""
    assert read_reset_log(tmp_path) == []


def test_read_reset_log_returns_empty_when_not_a_list(tmp_path: Path):
    """A log file holding something other than a list reads as empty."""
    (tmp_path / RESET_LOG_FILENAME).write_text(
        json.dumps({"not": "a list"}), encoding="utf-8"
    )

    assert read_reset_log(tmp_path) == []


def test_append_reset_log_writes_the_expected_file(tmp_path: Path):
    """The log lands in ``task_reset_log.json`` inside the plan dir."""
    append_reset_log(tmp_path, {"task_id": "T-1"})

    log_path = tmp_path / RESET_LOG_FILENAME
    assert log_path.exists()
    assert RESET_LOG_FILENAME == "task_reset_log.json"
    assert json.loads(log_path.read_text(encoding="utf-8"))[0]["task_id"] == "T-1"


def test_task_reset_request_defaults():
    """The request body is optional in every field but has stable defaults."""
    empty = TaskResetRequest()

    assert empty.reason is None
    assert empty.force is False

    long_reason = TaskResetRequest(reason="x" * 900)
    assert long_reason.reason is not None
    assert len(long_reason.reason) == 800


def test_build_reset_fields_returns_a_fresh_dict():
    """Two calls give two dicts — a caller mutating one does not poison the next."""
    first = build_reset_fields()
    first["status"] = "mutated"

    assert build_reset_fields()["status"] == "pending"


def test_read_reset_log_returns_empty_when_not_json(tmp_path: Path):
    """A log file that is not JSON at all reads as empty, not as an error."""
    (tmp_path / RESET_LOG_FILENAME).write_text("{not json", encoding="utf-8")

    assert read_reset_log(tmp_path) == []


def test_append_reset_log_uses_atomic_write_with_reraise(monkeypatch, tmp_path):
    """The log is written through ``atomic_write_json`` and never swallows errors."""
    calls = []

    def spy(path, payload, **kwargs):
        calls.append((path, payload, kwargs))

    monkeypatch.setattr(task_reset, "atomic_write_json", spy)

    append_reset_log(tmp_path, {"task_id": "T-1"})

    assert len(calls) == 1
    written_path, written_payload, written_kwargs = calls[0]
    assert Path(written_path) == tmp_path / RESET_LOG_FILENAME
    assert written_payload[0]["task_id"] == "T-1"
    assert written_kwargs.get("reraise") is True


def test_append_reset_log_surfaces_os_error(monkeypatch, tmp_path):
    """A failing write reaches the caller — a silent audit gap is not acceptable."""

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(task_reset, "atomic_write_json", boom)

    with pytest.raises(OSError):
        append_reset_log(tmp_path, {"task_id": "T-1"})


def test_append_reset_log_keeps_entries_it_cannot_read(tmp_path: Path):
    """Rewriting the table does not drop what is already in the file."""
    log_path = tmp_path / RESET_LOG_FILENAME
    log_path.write_text(json.dumps(["not-a-dict"]), encoding="utf-8")

    append_reset_log(tmp_path, {"task_id": "T-1"})

    raw = json.loads(log_path.read_text(encoding="utf-8"))
    assert raw[0] == "not-a-dict"
    assert raw[1]["task_id"] == "T-1"


def test_append_reset_log_refuses_to_replace_an_unreadable_log(monkeypatch, tmp_path):
    """A log we cannot read is not a log we may overwrite with a fresh one."""
    log_path = tmp_path / RESET_LOG_FILENAME
    original = json.dumps([{"task_id": "T-0"}])
    log_path.write_text(original, encoding="utf-8")

    real_read_text = Path.read_text

    def deny(self, *args, **kwargs):
        if self == log_path:
            raise PermissionError("denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny)

    with pytest.raises(OSError):
        append_reset_log(tmp_path, {"task_id": "T-1"})

    # the log is untouched — read it around the patched reader
    assert real_read_text(log_path, encoding="utf-8") == original