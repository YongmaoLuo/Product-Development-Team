"""TDD spec for the watchdog signal store.

Architecture decision point 3 (refiner exhausts 3 rounds), decision
point 4 (dispatcher auto_fix still fails) and decision point 6
(watchdog validate-through fails) all converge on a single shared
exit point: ``framework/watchdog_signal.py``.

The module provides:

* ``SIGNAL_SCHEMA`` — a JSON schema describing the on-disk signal
  file. Used both by writers (validate before serialise) and by
  readers (validate after deserialise).
* ``write_signal(plan_id, source, reason, detail) -> Path`` —
  atomically writes ``plans/{plan_id}/_watchdog_signal.json`` using
  ``tmp + os.replace`` so a concurrent reader never sees a
  half-written file.
* ``read_signal(plan_id) -> dict | None`` — returns the parsed
  signal for ``plan_id``, or ``None`` if no signal file exists.

The plan id is validated against ``framework.ids::validate_plan_id``
to reject ``..``, absolute paths and other traversal patterns.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from framework.ids import InvalidPlanIdError, validate_plan_id
from framework.watchdog_signal import (
    SIGNAL_SCHEMA,
    read_signal,
    write_signal,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plans_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override the global plans root so tests are hermetic.

    The framework module exposes ``PLANS_ROOT`` as a module-level
    constant (resolved lazily). Tests patch it directly via
    ``monkeypatch.setattr`` so we don't have to re-import the module
    after each change.
    """
    import framework.watchdog_signal as ws

    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    monkeypatch.setattr(ws, "PLANS_ROOT", plans_dir)
    return plans_dir


# ---------------------------------------------------------------------------
# TDD spec 1 — write_signal produces a schema-valid file
# ---------------------------------------------------------------------------


def test_write_signal_creates_schema_valid_file(plans_root: Path) -> None:
    """A written signal file must exist and validate against SIGNAL_SCHEMA."""
    plan_id = "20260101-watchdog-signal-spec"
    detail = {"refiner_round": 3, "validation_errors": ["x", "y"]}

    target = write_signal(
        plan_id,
        source="refiner",
        reason="refiner exhausted 3 rounds",
        detail=detail,
    )

    assert target.exists()
    assert target == plans_root / plan_id / "_watchdog_signal.json"

    payload = json.loads(target.read_text(encoding="utf-8"))
    jsonschema.validate(payload, SIGNAL_SCHEMA)

    assert payload["plan_id"] == plan_id
    assert payload["source"] == "refiner"
    assert payload["reason"] == "refiner exhausted 3 rounds"
    assert payload["detail"] == detail
    assert payload["schema_version"] == "1"
    assert "created_at" in payload


def test_write_signal_created_at_uses_framework_clock(
    plans_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``created_at`` must come from ``framework.clock.utcnow_iso()``.

    We pin the clock to a known sentinel value and assert the
    signal carries it verbatim — never ``datetime.now()`` directly.

    Note: ``write_signal`` imports ``utcnow_iso`` at module load
    time, so the binding is ``framework.watchdog_signal.utcnow_iso``
    (not ``framework.clock.utcnow_iso``). We patch the
    watchdog_signal module's symbol so the in-function reference
    sees the sentinel.
    """
    import framework.watchdog_signal as ws

    sentinel = "2026-08-07T10:11:12.345678+00:00"

    def fake_utcnow_iso() -> str:
        return sentinel

    monkeypatch.setattr(ws, "utcnow_iso", fake_utcnow_iso)

    target = write_signal(
        "20260101-clock-pinning",
        source="dispatcher",
        reason="auto_fix still failed",
        detail={},
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["created_at"] == sentinel


def test_write_signal_accepts_all_three_sources(plans_root: Path) -> None:
    """All three declared ``source`` values are accepted."""
    for source in ("refiner", "dispatcher", "watchdog"):
        target = write_signal(
            f"20260101-source-{source}",
            source=source,  # type: ignore[arg-type]
            reason=f"{source} raised",
            detail={"x": 1},
        )
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["source"] == source


# ---------------------------------------------------------------------------
# TDD spec 2 — read_signal None-or-dict contract
# ---------------------------------------------------------------------------


def test_read_signal_returns_none_for_missing_plan(plans_root: Path) -> None:
    """A plan with no signal file returns ``None`` (NOT an exception)."""
    assert read_signal("20260101-no-such-plan") is None


def test_read_signal_roundtrip_equals_written_dict(plans_root: Path) -> None:
    """A read-back must equal the dict that was written."""
    plan_id = "20260101-roundtrip"
    detail = {"stages": ["refine", "dispatch"], "ok": False}

    write_signal(
        plan_id,
        source="watchdog",
        reason="validate-through failed",
        detail=detail,
    )

    payload = read_signal(plan_id)
    assert payload is not None
    assert payload["plan_id"] == plan_id
    assert payload["source"] == "watchdog"
    assert payload["reason"] == "validate-through failed"
    assert payload["detail"] == detail
    assert payload["schema_version"] == "1"
    assert "created_at" in payload


# ---------------------------------------------------------------------------
# TDD spec 3 — idempotent overwrite, atomic write
# ---------------------------------------------------------------------------


def test_write_signal_overwrites_without_appending(plans_root: Path) -> None:
    """A second ``write_signal`` for the same plan replaces, not appends."""
    plan_id = "20260101-overwrite"

    first = write_signal(
        plan_id, source="refiner", reason="first", detail={"n": 1}
    )
    second = write_signal(
        plan_id, source="dispatcher", reason="second", detail={"n": 2}
    )

    assert first == second
    payload = json.loads(second.read_text(encoding="utf-8"))
    assert payload["source"] == "dispatcher"
    assert payload["reason"] == "second"
    assert payload["detail"] == {"n": 2}


def test_write_signal_uses_tmp_then_replace_no_partial_write(
    plans_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic rename: ``os.replace`` on a tmp file.

    We monkeypatch ``os.replace`` to wrap the real call and assert
    the source is a tmp file (NOT the final destination) so a
    concurrent reader never observes the partial bytes.
    """
    plan_id = "20260101-atomic"
    seen: dict[str, Any] = {}

    real_replace = os.replace

    def spy_replace(src: Any, dst: Any) -> None:
        seen["src"] = str(src)
        seen["dst"] = str(dst)
        seen["src_is_tmp"] = str(src) != str(dst)
        real_replace(src, dst)

    monkeypatch.setattr("framework.watchdog_signal.os.replace", spy_replace)

    write_signal(plan_id, source="refiner", reason="atomic", detail={})

    assert seen.get("src_is_tmp") is True, (
        "os.replace source must be a tmp file, not the final destination"
    )
    # File is valid JSON after the rename completes.
    target = plans_root / plan_id / "_watchdog_signal.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["source"] == "refiner"


# ---------------------------------------------------------------------------
# TDD spec 4 — invalid source values raise ValueError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_source",
    ["Refiner", "refiners", "auto_fix", "", "watchdog_signal", None],
)
def test_write_signal_rejects_invalid_source(
    plans_root: Path, bad_source: Any
) -> None:
    """Anything outside ``{refiner, dispatcher, watchdog}`` raises ValueError."""
    with pytest.raises(ValueError, match="source"):
        write_signal(
            "20260101-bad-source",
            source=bad_source,
            reason="x",
            detail={},
        )


# ---------------------------------------------------------------------------
# TDD spec 5 — plan_id security boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../../etc/passwd",
        "a/../../b",
        "/etc",
        "/etc/passwd",
        "..",
        "a/b/../../../x",
    ],
)
def test_write_signal_rejects_traversal_plan_id(
    plans_root: Path, bad_id: str
) -> None:
    """Traversal / absolute ``plan_id`` values raise ``InvalidPlanIdError``."""
    with pytest.raises(InvalidPlanIdError):
        write_signal(
            bad_id,  # type: ignore[arg-type]
            source="refiner",
            reason="x",
            detail={},
        )


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../../etc/passwd",
        "a/../../b",
        "/etc",
    ],
)
def test_read_signal_rejects_traversal_plan_id(bad_id: str) -> None:
    """Same validation applies on read — defence in depth."""
    with pytest.raises(InvalidPlanIdError):
        read_signal(bad_id)


def test_validate_plan_id_accepts_safe_id() -> None:
    """A normal plan id passes validation."""
    assert validate_plan_id("20260101-watchdog-signal") == "20260101-watchdog-signal"


def test_validate_plan_id_rejects_empty() -> None:
    """Empty string is rejected as it has no safe target."""
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id("")


def test_validate_plan_id_rejects_non_string() -> None:
    """Non-string inputs raise InvalidPlanIdError (defence in depth)."""
    for bad in (123, None, b"bytes", ["list"], {"dict": 1}):
        with pytest.raises(InvalidPlanIdError):
            validate_plan_id(bad)  # type: ignore[arg-type]


def test_validate_plan_id_rejects_unsafe_bytes() -> None:
    """Any byte outside [A-Za-z0-9._-] is rejected."""
    for bad in ("a b", "a\nb", "a;b", "a*b", "a$b", "a`b"):
        with pytest.raises(InvalidPlanIdError):
            validate_plan_id(bad)


def test_validate_plan_id_rejects_windows_absolute() -> None:
    """Windows-style absolute paths are also rejected."""
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id("C:\\Windows")


def test_write_signal_rejects_non_string_reason(plans_root: Path) -> None:
    """Non-string ``reason`` raises ValueError."""
    with pytest.raises(ValueError, match="reason"):
        write_signal(
            "20260101-bad-reason",
            source="refiner",
            reason=123,  # type: ignore[arg-type]
            detail={},
        )


def test_write_signal_rejects_empty_reason(plans_root: Path) -> None:
    """Empty-string ``reason`` raises ValueError."""
    with pytest.raises(ValueError, match="reason"):
        write_signal(
            "20260101-empty-reason",
            source="refiner",
            reason="",
            detail={},
        )


def test_write_signal_rejects_non_dict_detail(plans_root: Path) -> None:
    """Non-dict ``detail`` raises ValueError."""
    with pytest.raises(ValueError, match="detail"):
        write_signal(
            "20260101-bad-detail",
            source="refiner",
            reason="ok",
            detail=["not", "a", "dict"],  # type: ignore[arg-type]
        )


def test_write_signal_handles_rename_failure(
    plans_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``os.replace`` cleans up the tmp file and re-raises."""
    import framework.watchdog_signal as ws

    def boom(src: Any, dst: Any) -> None:
        # Unlink the tmp before raising so we exercise the
        # ``missing_ok=True`` path inside the except branch.
        try:
            os.unlink(src)
        except OSError:
            pass
        raise OSError("simulated rename failure")

    monkeypatch.setattr(ws.os, "replace", boom)

    with pytest.raises(OSError, match="simulated rename failure"):
        write_signal(
            "20260101-rename-fail",
            source="refiner",
            reason="x",
            detail={},
        )
    # And the plan directory contains no leftover .tmp files.
    target_dir = plans_root / "20260101-rename-fail"
    leftovers = list(target_dir.glob(".watchdog_signal.*.json.tmp"))
    assert leftovers == [], f"leftover tmp files: {leftovers}"


def test_write_signal_swallows_unlink_failure_during_cleanup(
    plans_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both rename AND the tmp unlink fail, the OSError is swallowed
    and the original rename error is re-raised (defence in depth on the
    cleanup path)."""
    import framework.watchdog_signal as ws

    def boom_replace(src: Any, dst: Any) -> None:
        raise OSError("simulated rename failure")

    def boom_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        # ``missing_ok=True`` is a kwarg. Even if it's True, force an
        # OSError so we exercise the ``except OSError: pass`` line.
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(ws.os, "replace", boom_replace)
    # ``Path.unlink`` is the real method; patch it on ``Path`` itself.
    monkeypatch.setattr(Path, "unlink", boom_unlink)

    with pytest.raises(OSError, match="simulated rename failure"):
        write_signal(
            "20260101-unlink-fail",
            source="refiner",
            reason="x",
            detail={},
        )