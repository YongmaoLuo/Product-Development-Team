"""Unit tests: the scheduler's configuration snapshot and its hot reload.

What this file holds down
-------------------------
The scheduler reads its settings from one YAML file and keeps them as an
**immutable snapshot**.  The snapshot is replaced whole — never patched
field by field — so a reader can never observe a configuration that is
half old and half new (a new ``capacity`` next to an old
``min_runtime_seconds``).

The four contracts this file pins:

1. **Missing means default.** A file without a ``scheduler`` section, or
   with a section that omits a key, yields that key's default.
2. **A reload swaps the whole snapshot.** Changing two keys changes both
   at once, and the snapshot that was handed out before the reload is
   still intact afterwards.
3. **An invalid value keeps the previous snapshot.** A negative number,
   a NaN, a non-number — none of them is allowed to reach the scheduler;
   the last good snapshot stays in place instead.
4. **A string is a number if it spells one.** ``capacity: "4"`` is the
   integer 4.

The reload is driven by the file's mtime, so the tests that change a
file also move its mtime forward.  Without that, a filesystem with a
coarse timestamp would let two edits inside the same tick look like one
and the test would fail for a reason that has nothing to do with the
code under test.
"""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from plan_scheduler.config import SchedulerConfig, SchedulerConfigLoader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` and make the change visible in its mtime.

    The loader watches the file's ``(mtime_ns, size)`` fingerprint.  A
    plain rewrite moves the size only when the text differs in length,
    so the mtime is nudged forward explicitly: the reload this file
    tests has to be observable through the fingerprint, on every
    filesystem the suite runs on.
    """
    path.write_text(text, encoding="utf-8")
    stamp = path.stat().st_mtime + 2
    os.utime(path, (stamp, stamp))


# ---------------------------------------------------------------------------
# 1. Missing section / missing key → defaults
# ---------------------------------------------------------------------------


def test_defaults_when_section_missing(tmp_path: Path) -> None:
    """A file without a ``scheduler`` section yields every default."""
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")

    config = SchedulerConfigLoader(path).current()

    assert config.enabled is False
    assert config.dry_run is False
    assert config.capacity == 4
    assert config.min_runtime_seconds == 600.0
    assert config.tick_seconds == 5.0
    assert config.command_timeout_seconds == 30.0
    assert config == SchedulerConfig()


def test_missing_key_falls_back_to_its_default(tmp_path: Path) -> None:
    """A section that names one key leaves the other five at their defaults."""
    path = tmp_path / "config.yaml"
    _write(path, "scheduler:\n  capacity: 3\n")

    config = SchedulerConfigLoader(path).current()

    assert config.capacity == 3
    assert config.enabled is False
    assert config.dry_run is False
    assert config.min_runtime_seconds == 600.0
    assert config.tick_seconds == 5.0
    assert config.command_timeout_seconds == 30.0


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    """A file that does not exist is not an error — it is all defaults."""
    config = SchedulerConfigLoader(tmp_path / "absent.yaml").current()

    assert config == SchedulerConfig()


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    """A key this module does not know about does not fail the load."""
    path = tmp_path / "config.yaml"
    _write(path, "scheduler:\n  capacity: 4\n  some_future_key: 7\n")

    config = SchedulerConfigLoader(path).current()

    assert config.capacity == 4


# ---------------------------------------------------------------------------
# 2. A reload swaps the whole snapshot
# ---------------------------------------------------------------------------


def test_reload_swaps_whole_snapshot(tmp_path: Path) -> None:
    """Changing two keys changes both at once — no half-old snapshot."""
    path = tmp_path / "config.yaml"
    _write(path, "scheduler:\n  capacity: 4\n  min_runtime_seconds: 600\n")

    loader = SchedulerConfigLoader(path)
    before = loader.current()
    assert before.capacity == 4
    assert before.min_runtime_seconds == 600.0

    _write(path, "scheduler:\n  capacity: 12\n  min_runtime_seconds: 30\n")

    after = loader.current()
    assert after is not before
    assert after.capacity == 12
    assert after.min_runtime_seconds == 30.0
    # The whole snapshot moved: the keys the new file does not mention
    # are the defaults, not the values the old file happened to carry.
    assert after == SchedulerConfig(capacity=12, min_runtime_seconds=30.0)
    # The snapshot handed out earlier is immutable and still says what
    # it said when it was handed out.
    assert before.capacity == 4
    assert before.min_runtime_seconds == 600.0


def test_reload_does_not_mix_old_and_new_values(tmp_path: Path) -> None:
    """A key the new file omits goes back to its default, not to the old value."""
    path = tmp_path / "config.yaml"
    _write(path, "scheduler:\n  capacity: 3\n  min_runtime_seconds: 42\n")

    loader = SchedulerConfigLoader(path)
    assert loader.current().min_runtime_seconds == 42.0

    _write(path, "scheduler:\n  capacity: 9\n")

    after = loader.current()
    assert after.capacity == 9
    assert after.min_runtime_seconds == 600.0


def test_unchanged_file_is_not_reloaded(tmp_path: Path) -> None:
    """An untouched file keeps handing out the same snapshot object."""
    path = tmp_path / "config.yaml"
    _write(path, "scheduler:\n  capacity: 5\n")

    loader = SchedulerConfigLoader(path)

    assert loader.current() is loader.current()


# ---------------------------------------------------------------------------
# 3. An invalid value keeps the previous snapshot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "scheduler:\n  capacity: -1\n",
        "scheduler:\n  capacity: not-a-number\n",
        "scheduler:\n  capacity: .nan\n",
        "scheduler:\n  min_runtime_seconds: -0.5\n",
        "scheduler:\n  tick_seconds: .nan\n",
        "scheduler:\n  command_timeout_seconds: -1\n",
        "scheduler:\n  enabled: maybe\n",
    ],
    ids=[
        "negative_capacity",
        "non_numeric_capacity",
        "nan_capacity",
        "negative_min_runtime",
        "nan_tick",
        "negative_command_timeout",
        "non_boolean_enabled",
    ],
)
def test_invalid_value_keeps_previous(tmp_path: Path, body: str) -> None:
    """A value the scheduler cannot use never reaches it."""
    path = tmp_path / "config.yaml"
    _write(path, "scheduler:\n  capacity: 4\n")

    loader = SchedulerConfigLoader(path)
    before = loader.current()
    assert before.capacity == 4

    _write(path, body)

    after = loader.current()
    assert after is before
    assert after.capacity == 4


def test_truncated_file_keeps_previous(tmp_path: Path) -> None:
    """A half-written file is not a crash — the old snapshot stays."""
    path = tmp_path / "config.yaml"
    _write(path, "scheduler:\n  capacity: 4\n")

    loader = SchedulerConfigLoader(path)
    before = loader.current()

    path.write_text("scheduler:\n  capacity: [1, 2\n", encoding="utf-8")
    stamp = path.stat().st_mtime + 2
    os.utime(path, (stamp, stamp))

    after = loader.current()
    assert after is before
    assert after.capacity == 4


def test_unparseable_file_keeps_previous(tmp_path: Path) -> None:
    """A file that is not YAML at all keeps the old snapshot, without raising."""
    path = tmp_path / "config.yaml"
    _write(path, "scheduler:\n  capacity: 4\n")

    loader = SchedulerConfigLoader(path)
    before = loader.current()

    path.write_text("\tthis: is not: yaml: at all\n", encoding="utf-8")
    stamp = path.stat().st_mtime + 2
    os.utime(path, (stamp, stamp))

    after = loader.current()
    assert after is before
    assert after.capacity == 4


def test_malformed_section_keeps_previous(tmp_path: Path) -> None:
    """A ``scheduler`` section that is not a mapping is refused, not read as absent."""
    path = tmp_path / "config.yaml"
    _write(path, "scheduler:\n  capacity: 4\n")

    loader = SchedulerConfigLoader(path)
    before = loader.current()

    _write(path, "scheduler: 5\n")

    after = loader.current()
    assert after is before
    assert after.capacity == 4


def test_file_appearing_later_is_picked_up(tmp_path: Path) -> None:
    """A file that shows up after the first read is still noticed."""
    path = tmp_path / "config.yaml"
    loader = SchedulerConfigLoader(path)
    assert loader.current() == SchedulerConfig()

    _write(path, "scheduler:\n  capacity: 8\n")

    assert loader.current().capacity == 8


# ---------------------------------------------------------------------------
# 4. A string that spells a number is a number
# ---------------------------------------------------------------------------


def test_string_int_coerced(tmp_path: Path) -> None:
    """``capacity: "4"`` is the integer 4."""
    path = tmp_path / "config.yaml"
    _write(path, 'scheduler:\n  capacity: "4"\n')

    config = SchedulerConfigLoader(path).current()

    assert config.capacity == 4
    assert isinstance(config.capacity, int)


def test_string_float_coerced(tmp_path: Path) -> None:
    """``tick_seconds: "2.5"`` is the float 2.5."""
    path = tmp_path / "config.yaml"
    _write(path, 'scheduler:\n  tick_seconds: "2.5"\n')

    config = SchedulerConfigLoader(path).current()

    assert config.tick_seconds == 2.5
    assert isinstance(config.tick_seconds, float)


def test_snapshot_is_immutable(tmp_path: Path) -> None:
    """A snapshot that could be edited in place would be a shared hazard."""
    path = tmp_path / "config.yaml"
    _write(path, "scheduler:\n  capacity: 6\n")

    config = SchedulerConfigLoader(path).current()

    with pytest.raises(FrozenInstanceError):
        config.capacity = 99  # type: ignore[misc]