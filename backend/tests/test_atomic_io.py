"""Tests for the shared atomic JSON I/O utilities."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from utils.atomic_io import (
    atomic_write_json,
    write_jsonl,
    read_jsonl,
)


def test_atomic_write_json_creates_new_file(tmp_path: Path) -> None:
    """A brand-new path is written atomically."""
    target = tmp_path / "state.json"
    atomic_write_json(target, {"plan_id": "p1", "round": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "plan_id": "p1",
        "round": 2,
    }


def test_atomic_write_json_replaces_existing(tmp_path: Path) -> None:
    """An existing file is fully replaced, never partially merged."""
    target = tmp_path / "state.json"
    target.write_text('{"old": true}', encoding="utf-8")

    atomic_write_json(target, {"new": True})

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"new": True}
    assert "old" not in data


def test_atomic_write_json_preserves_unicode(tmp_path: Path) -> None:
    """``ensure_ascii=False`` keeps CJK characters readable."""
    target = tmp_path / "zh.json"
    atomic_write_json(target, {"label": "验证阶段"})
    assert "验证阶段" in target.read_text(encoding="utf-8")


def test_atomic_write_json_creates_parent_dirs(tmp_path: Path) -> None:
    """Missing parent directories are created."""
    target = tmp_path / "nested" / "deep" / "state.json"
    atomic_write_json(target, {"ok": True})
    assert target.is_file()


def test_atomic_write_json_no_tmp_left_on_disk(tmp_path: Path) -> None:
    """No ``.tmp`` files remain after a successful write."""
    target = tmp_path / "state.json"
    atomic_write_json(target, {"a": 1})
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_atomic_write_json_keeps_original_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the write fails, the original file is left intact."""
    target = tmp_path / "state.json"
    target.write_text('{"version": 1}', encoding="utf-8")

    def boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise OSError("disk full")

    monkeypatch.setattr("utils.atomic_io.tempfile.mkstemp", boom)

    with pytest.raises(OSError):
        atomic_write_json(target, {"version": 2}, reraise=True)

    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1}


def test_atomic_write_json_default_swallows_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """By default an OSError is logged and swallowed (existing in-memory state stays consistent)."""
    target = tmp_path / "state.json"
    target.write_text('{"version": 1}', encoding="utf-8")

    def boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise OSError("disk full")

    monkeypatch.setattr("utils.atomic_io.tempfile.mkstemp", boom)

    atomic_write_json(target, {"version": 2})  # no raise
    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1}


def test_write_jsonl_appends_lines(tmp_path: Path) -> None:
    """Each call appends one JSON line."""
    log = tmp_path / "events.jsonl"
    write_jsonl(log, {"event": "start", "n": 1})
    write_jsonl(log, {"event": "end", "n": 2})

    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"event": "start", "n": 1}
    assert json.loads(lines[1]) == {"event": "end", "n": 2}


def test_write_jsonl_creates_parent_dirs(tmp_path: Path) -> None:
    """Missing parent directories are created."""
    log = tmp_path / "logs" / "events.jsonl"
    write_jsonl(log, {"event": "x"})
    assert log.is_file()


def test_write_jsonl_preserves_unicode(tmp_path: Path) -> None:
    """CJK content is not escaped."""
    log = tmp_path / "zh.jsonl"
    write_jsonl(log, {"msg": "验证开始"})
    assert "验证开始" in log.read_text(encoding="utf-8")


def test_read_jsonl_roundtrip(tmp_path: Path) -> None:
    """read_jsonl parses what write_jsonl wrote."""
    log = tmp_path / "events.jsonl"
    write_jsonl(log, {"a": 1})
    write_jsonl(log, {"b": 2})
    entries = list(read_jsonl(log))
    assert entries == [{"a": 1}, {"b": 2}]


def test_read_jsonl_skips_blank_lines(tmp_path: Path) -> None:
    """Trailing blank lines are tolerated."""
    log = tmp_path / "events.jsonl"
    log.write_text('{"a": 1}\n\n{"b": 2}\n\n', encoding="utf-8")
    entries = list(read_jsonl(log))
    assert entries == [{"a": 1}, {"b": 2}]


def test_write_jsonl_swallows_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient OSError on append does not crash the caller."""
    log = tmp_path / "events.jsonl"

    real_open = open

    def flaky_open(path, mode="r", *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if "w" in mode and isinstance(path, (str, Path)) and str(path).endswith("events.jsonl"):
            raise OSError("read-only filesystem")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)
    write_jsonl(log, {"event": "x"})  # no raise
