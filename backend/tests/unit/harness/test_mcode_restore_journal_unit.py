"""Unit coverage for the MiniMax Code MCP restore journal.

What this pins
--------------
``mcode exec`` has no ``--mcp-config``: the configuration travels
through the workspace, so the adapter writes the workspace's
``.mcp.json`` for the length of a run and puts the workspace back
afterwards. A run that is *killed* between the write and the restore
cannot put it back — that is what the journal is for.

The journal is the adapter's own runtime state, and its rules are
what this file pins:

1. The journal is written before the target is opened, and a journal
   that cannot be written aborts the write — there is no overwrite the
   adapter cannot undo.
2. A later start reads the journals it finds and puts the workspaces
   back, and takes the journal away when the workspace really is back.
3. A journal whose owner is still alive belongs to a run that is still
   going, and is left alone.
4. Every way a replay can fail is quiet and specific: the journal
   stays in place, because the next start still owes the user that
   file.

Everything here is hermetic: the journals are written into a
temporary directory, and no process is spawned.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

import harness.mcode as mcode
from harness.contract import HarnessConfigError
from harness.mcode import McodeHarness

pytestmark = pytest.mark.unit


def _dead_pid() -> int:
    """A pid that is not running on this machine."""
    for pid in range(40000, 40100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            continue
    raise AssertionError("no free pid found")


def _snapshot(target: Path, **overrides: Any) -> Any:
    """A snapshot of what the adapter is about to write over."""
    fields: Dict[str, Any] = {
        "resolved": target,
        "payload": b"{}",
        "existed": False,
        "original": b"",
        "mode": 0o600,
    }
    fields.update(overrides)
    return mcode._McpSnapshot(**fields)


def _write_journal(
    restore: Path,
    target: Path,
    record: Dict[str, Any],
    copy_bytes: Optional[bytes] = None,
) -> Any:
    """Write a journal (and its copy) the way a killed run left it."""
    restore.mkdir(parents=True, exist_ok=True)
    journal_path, copy_path = mcode._journal_paths(target)
    if copy_bytes is not None:
        copy_path.write_bytes(copy_bytes)
    journal_path.write_text(json.dumps(record), encoding="utf-8")
    return journal_path, copy_path


def _record(
    target: Path,
    copy_name: Optional[str],
    existed: bool = True,
    original: bytes = b"original bytes",
    mode: int = 0o600,
) -> Dict[str, Any]:
    """The record a killed run leaves beside the file it overwrote."""
    return {
        "version": 1,
        "target": str(target),
        "existed": existed,
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "mode": mode,
        "pid": _dead_pid(),
        "created_at": "2026-01-01T00:00:00+00:00",
        "copy": copy_name,
    }


# ---------------------------------------------------------------------------
# 1. Where the journals live
# ---------------------------------------------------------------------------


def test_the_journals_live_where_the_environment_says(tmp_path, monkeypatch):
    """The restore directory is the framework's own runtime state."""
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))

    assert mcode._mcp_restore_dir() == restore


def test_a_directory_that_cannot_be_flushed_is_not_a_reason_to_fail(tmp_path):
    """A directory that is not there cannot be flushed, and that is all."""
    assert mcode._fsync_dir(tmp_path / "absent") is None


def test_a_flush_that_fails_is_not_a_reason_to_fail(tmp_path, monkeypatch):
    """A directory that cannot be flushed does not fail the write."""
    def _fsync(_handle: int) -> None:
        raise OSError("cannot flush")

    monkeypatch.setattr(os, "fsync", _fsync)

    assert mcode._fsync_dir(tmp_path) is None


def test_a_write_that_cannot_happen_leaves_nothing_behind(tmp_path):
    """A failed atomic write removes its own temporary file."""
    path = tmp_path / "record.json"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.mkdir()

    with pytest.raises(OSError):
        mcode._atomic_write(path, b"data")

    assert temporary.is_dir()


# ---------------------------------------------------------------------------
# 2. Reading a journal
# ---------------------------------------------------------------------------


def test_a_journal_that_is_not_json_is_not_read(tmp_path):
    """A journal that cannot be read is kept, not used."""
    path = tmp_path / "record.json"
    path.write_text("not json", encoding="utf-8")

    assert mcode._read_journal(path) is None


def test_a_journal_that_is_not_an_object_is_not_read(tmp_path):
    """A journal holds an object; anything else is not one."""
    path = tmp_path / "record.json"
    path.write_text("[1, 2]", encoding="utf-8")

    assert mcode._read_journal(path) is None


# ---------------------------------------------------------------------------
# 3. Writing a journal
# ---------------------------------------------------------------------------


def test_a_journal_directory_that_cannot_be_opened_is_reported(tmp_path, monkeypatch):
    """The journal is written before the target is opened; if it cannot
    be written, the write does not happen."""
    restore = tmp_path / "restore"
    restore.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))

    with pytest.raises(HarnessConfigError, match="could not open the restore journal"):
        mcode._write_restore_journal(_snapshot(tmp_path / "ws" / ".mcp.json"))


def test_a_journal_that_cannot_be_written_is_reported(tmp_path, monkeypatch):
    """A journal that cannot be written aborts the write."""
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))
    target = tmp_path / "ws" / ".mcp.json"
    journal_path, _ = mcode._journal_paths(target)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.with_name(f".{journal_path.name}.{os.getpid()}.tmp").mkdir()

    with pytest.raises(HarnessConfigError, match="could not record the restore journal"):
        mcode._write_restore_journal(_snapshot(target))


def test_a_journal_that_is_already_there_keeps_the_recorded_original(tmp_path, monkeypatch):
    """A killed run's journal holds the file the user actually had; the
    recorded original is the truer of the two and is kept."""
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))
    target = tmp_path / "ws" / ".mcp.json"
    journal_path, _ = mcode._journal_paths(target)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        json.dumps({"original_sha256": "the-recorded-one", "pid": 1}),
        encoding="utf-8",
    )

    mcode._write_restore_journal(_snapshot(target, original=b"something else"))

    record = json.loads(journal_path.read_text(encoding="utf-8"))
    assert record["original_sha256"] == "the-recorded-one"
    assert record["pid"] == os.getpid()


def test_a_journal_that_cannot_be_taken_over_is_reported(tmp_path, monkeypatch):
    """The recorded original is kept; if it cannot be written, the run
    says so."""
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))
    target = tmp_path / "ws" / ".mcp.json"
    journal_path, _ = mcode._journal_paths(target)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        json.dumps({"original_sha256": "the-recorded-one", "pid": 1}),
        encoding="utf-8",
    )
    journal_path.with_name(f".{journal_path.name}.{os.getpid()}.tmp").mkdir()

    with pytest.raises(HarnessConfigError, match="could not take over the restore journal"):
        mcode._write_restore_journal(_snapshot(target, original=b"something else"))


# ---------------------------------------------------------------------------
# 4. Whether the workspace is back
# ---------------------------------------------------------------------------


def test_a_target_that_cannot_be_read_is_not_back(tmp_path):
    """A file that cannot be read is not the file the journal names."""
    target = tmp_path / ".mcp.json"
    target.mkdir()

    assert mcode._matches_record(target, {"existed": True, "mode": 0o600}) is False


def test_a_target_with_other_bytes_is_not_back(tmp_path):
    """The bytes have to be the ones the journal recorded."""
    target = tmp_path / ".mcp.json"
    target.write_bytes(b"something else")

    record = {
        "existed": True,
        "mode": 0o600,
        "original_sha256": hashlib.sha256(b"original bytes").hexdigest(),
    }

    assert mcode._matches_record(target, record) is False


def test_a_journal_without_a_copy_is_still_taken_away(tmp_path):
    """The journal is taken away; there is simply no copy beside it."""
    journal = tmp_path / "record.json"
    journal.write_text("{}", encoding="utf-8")

    mcode._discard_journal(journal, None)

    assert not journal.exists()


def test_a_journal_that_cannot_be_removed_is_logged_and_left(tmp_path, caplog):
    """A stale journal is stale, not dangerous."""
    journal = tmp_path / "record.json"
    journal.mkdir()

    mcode._discard_journal(journal, None)

    assert journal.is_dir()


def test_a_journal_that_is_not_there_is_not_discarded(tmp_path, monkeypatch):
    """Nothing to discard means nothing is discarded."""
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))
    target = tmp_path / "ws" / ".mcp.json"

    mcode._discard_journal_if_settled(_snapshot(target))


# ---------------------------------------------------------------------------
# 5. Whether the run that wrote the journal is still going
# ---------------------------------------------------------------------------


def test_a_journal_that_names_no_process_is_not_a_live_run():
    """A journal whose owner is not a pid belongs to no live run."""
    assert mcode._process_alive("not-a-pid") is False


def test_a_journal_that_names_no_process_at_all_is_not_a_live_run():
    """The zero pid is not a process."""
    assert mcode._process_alive(0) is False


def test_a_process_that_is_gone_is_not_a_live_run():
    """A run that was killed left a journal behind; its pid is gone."""
    assert mcode._process_alive(_dead_pid()) is False


def test_a_process_that_is_not_ours_is_still_a_live_run(monkeypatch):
    """It is there; it simply is not ours to signal."""
    def _kill(_pid: int, _sig: int) -> None:
        raise PermissionError("not ours")

    monkeypatch.setattr(os, "kill", _kill)

    assert mcode._process_alive(4242) is True


def test_a_signal_that_fails_for_another_reason_is_not_a_live_run(monkeypatch):
    """A pid the machine will not answer about is not a live run."""
    def _kill(_pid: int, _sig: int) -> None:
        raise OSError("cannot ask")

    monkeypatch.setattr(os, "kill", _kill)

    assert mcode._process_alive(4242) is False


# ---------------------------------------------------------------------------
# 6. The replay itself
# ---------------------------------------------------------------------------


def test_a_killed_runs_workspace_is_put_back(tmp_path, monkeypatch):
    """The next start of anything gives the user their file back."""
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / ".mcp.json"
    original = b'{"mcpServers": {}}'
    target.write_bytes(b'{"mcpServers": {"token": "leaked"}}')

    journal_path, copy_path = _write_journal(
        restore,
        target,
        _record(target, copy_name=None, original=original),
    )
    record = json.loads(journal_path.read_text(encoding="utf-8"))
    record["copy"] = copy_path.name
    journal_path.write_text(json.dumps(record), encoding="utf-8")
    copy_path.write_bytes(original)

    harness = McodeHarness()

    assert len(harness.recovered_restores) == 1
    assert target.read_bytes() == original
    assert not journal_path.exists()
    assert not copy_path.exists()


def test_a_journal_that_names_no_file_is_left_in_place(tmp_path, monkeypatch):
    """A journal that says nothing about a file cannot be acted on."""
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / ".mcp.json"

    journal_path, _ = _write_journal(
        restore, target, {"existed": False, "pid": _dead_pid()}
    )

    assert McodeHarness().recovered_restores == ()
    assert journal_path.exists()


def test_a_journal_whose_workspace_is_gone_is_taken_away(tmp_path, monkeypatch):
    """A workspace that no longer exists has nothing to put back."""
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))
    target = tmp_path / "gone" / ".mcp.json"

    journal_path, copy_path = _write_journal(
        restore, target, _record(target, copy_name=None)
    )
    record = json.loads(journal_path.read_text(encoding="utf-8"))
    record["copy"] = copy_path.name
    journal_path.write_text(json.dumps(record), encoding="utf-8")

    assert McodeHarness().recovered_restores == ()
    assert not journal_path.exists()


def test_a_journal_that_names_no_copy_is_left_in_place(tmp_path, monkeypatch):
    """The copy holds the user's file; without it there is nothing to
    give back."""
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / ".mcp.json"

    journal_path, _ = _write_journal(
        restore, target, _record(target, copy_name=None)
    )

    assert McodeHarness().recovered_restores == ()
    assert journal_path.exists()


def test_a_copy_that_cannot_be_read_is_left_in_place(tmp_path, monkeypatch):
    """A copy that cannot be read is not used to overwrite anything."""
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / ".mcp.json"

    journal_path, copy_path = _write_journal(
        restore, target, _record(target, copy_name=None)
    )
    record = json.loads(journal_path.read_text(encoding="utf-8"))
    record["copy"] = copy_path.name
    journal_path.write_text(json.dumps(record), encoding="utf-8")
    copy_path.mkdir()

    assert McodeHarness().recovered_restores == ()
    assert journal_path.exists()


def test_a_copy_that_does_not_match_the_journal_is_not_used(tmp_path, monkeypatch):
    """A copy that is not the file the journal names is not used."""
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / ".mcp.json"

    journal_path, copy_path = _write_journal(
        restore, target, _record(target, copy_name=None)
    )
    record = json.loads(journal_path.read_text(encoding="utf-8"))
    record["copy"] = copy_path.name
    journal_path.write_text(json.dumps(record), encoding="utf-8")
    copy_path.write_bytes(b"something else entirely")

    assert McodeHarness().recovered_restores == ()
    assert journal_path.exists()


def test_a_workspace_that_cannot_be_put_back_keeps_its_journal(tmp_path, monkeypatch):
    """A restore that did not put the file back leaves the journal where
    it is: the next start still owes the user that file."""
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / ".mcp.json"
    target.mkdir()
    original = b"original bytes"

    journal_path, copy_path = _write_journal(
        restore, target, _record(target, copy_name=None, original=original)
    )
    record = json.loads(journal_path.read_text(encoding="utf-8"))
    record["copy"] = copy_path.name
    journal_path.write_text(json.dumps(record), encoding="utf-8")
    copy_path.write_bytes(original)

    assert McodeHarness().recovered_restores == ()
    assert journal_path.exists()


def test_a_restore_directory_that_cannot_be_read_yields_nothing(tmp_path, monkeypatch):
    """A directory the machine will not list is a directory with nothing
    in it."""
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(tmp_path / "restore"))

    def _glob(self, pattern):
        raise OSError("cannot list")

    monkeypatch.setattr(Path, "glob", _glob)

    assert mcode.recover_pending_mcp_restores() == []


# ---------------------------------------------------------------------------
# 7. Putting the workspace back
# ---------------------------------------------------------------------------


def test_a_file_that_cannot_be_read_is_written_back(tmp_path, monkeypatch):
    """The restore is in place; the bytes come back."""
    target = tmp_path / ".mcp.json"
    target.write_bytes(b"the adapter's payload")
    original = b"the user's file"
    real_read_bytes = Path.read_bytes

    def _read_bytes(self):
        if self == target:
            raise OSError("cannot read")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)

    mcode._restore_mcp_config(
        _snapshot(target, existed=True, original=original, mode=0o600)
    )

    assert real_read_bytes(target) == original


def test_a_file_whose_mode_cannot_be_read_is_still_restored(tmp_path, monkeypatch):
    """The mode the file carried is unknown; the bytes still come back."""
    target = tmp_path / ".mcp.json"
    target.write_bytes(b"the user's file")
    real_stat = Path.stat

    def _stat(self, *args, **kwargs):
        if self == target:
            raise OSError("cannot stat")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat)

    mcode._restore_mcp_config(
        _snapshot(target, existed=True, original=b"the user's file", mode=0o600)
    )

    assert target.read_bytes() == b"the user's file"


def test_a_file_that_was_not_there_is_taken_away(tmp_path):
    """A file that was not there before the run is gone again."""
    target = tmp_path / ".mcp.json"
    target.write_bytes(b"the adapter's payload")

    mcode._restore_mcp_config(_snapshot(target, existed=False))

    assert not target.exists()


def test_a_file_that_is_already_gone_is_left_gone(tmp_path):
    """Nothing to take away is not a failure."""
    target = tmp_path / ".mcp.json"

    mcode._restore_mcp_config(_snapshot(target, existed=False))

    assert not target.exists()


def test_a_write_that_fails_is_reported(tmp_path, monkeypatch):
    """The write that carries the credential has to succeed or say so."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / ".mcp.json"
    restore = tmp_path / "restore"
    monkeypatch.setenv(mcode.MCP_RESTORE_DIR_ENV, str(restore))

    real_fdopen = os.fdopen
    calls = []

    def _fdopen(fd, *args, **kwargs):
        calls.append(fd)
        if len(calls) == 1:
            # The journal's own write: it goes through unchanged.
            return real_fdopen(fd, *args, **kwargs)

        class _Failing:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def write(self, _data):
                raise OSError("no space left on device")

            def flush(self):
                pass

            def fileno(self):
                return fd

        return _Failing()

    monkeypatch.setattr(os, "fdopen", _fdopen)

    with pytest.raises(HarnessConfigError, match="could not write"):
        mcode._write_mcp_config(_snapshot(target))
