"""Unit tests: the scheduler section in ``backend/config.yaml``.

What this file holds down
-------------------------
``backend/config.yaml`` is the file the operator edits to turn the
scheduler on, to move it into shadow mode, or to roll it back.  This
file pins three contracts about the section it carries:

1. **The key set is fixed, and the values are the documented ones.**
   The section carries exactly the six keys the scheduler reads — no
   more, no less — and each of them holds the default the
   architecture's decision table names.  A key nobody reads is a
   setting nobody believes they changed; a key the scheduler reads
   but the file does not carry is a setting that runs on a default
   nobody chose.
2. **What the file says is what the scheduler gets.** The loader
   reads the section that is written and hands it back item by item,
   so the file is the truth and the snapshot is a copy of it.
3. **Removing the section leaves the scheduler off.** The section
   going missing is a rollback, not a failure: the loader falls back
   to its defaults and ``enabled`` is ``False`` among them, so the
   scheduler cannot come up next to the old cron just because somebody
   deleted a block of YAML.

The section is read from the real file rather than from a fixture —
these tests are about what this repository ships, and a fixture would
only be about what the fixture says.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from plan_scheduler.config import SchedulerConfig, SchedulerConfigLoader

#: The file this file is about: ``backend/config.yaml``.
CONFIG_PATH = Path(__file__).resolve().parents[3] / "config.yaml"

#: The six keys the scheduler section carries.  The set is fixed: these
#: are the keys :mod:`plan_scheduler.config` reads, and no others.
SCHEDULER_KEYS = (
    "enabled",
    "dry_run",
    "capacity",
    "min_runtime_seconds",
    "tick_seconds",
    "command_timeout_seconds",
)

#: The default each key carries — the architecture's decision table for
#: the scheduler's configuration.
DEFAULT_VALUES: Dict[str, Any] = {
    "enabled": False,
    "dry_run": False,
    "capacity": 4,
    "min_runtime_seconds": 600,
    "tick_seconds": 5,
    "command_timeout_seconds": 30,
}


def _read_document() -> Dict[str, Any]:
    """Return ``backend/config.yaml`` parsed as a plain dict."""
    document = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "backend/config.yaml must be a mapping"
    return document


def _read_section() -> Dict[str, Any]:
    """Return the ``scheduler`` section of ``backend/config.yaml``."""
    section = _read_document().get("scheduler")
    assert isinstance(section, dict), "backend/config.yaml must carry a scheduler section"
    return section


def test_config_yaml_has_scheduler_section() -> None:
    """``backend/config.yaml`` carries the scheduler section, with the six defaults."""
    section = _read_section()

    assert set(section) == set(SCHEDULER_KEYS)
    for key, value in DEFAULT_VALUES.items():
        assert section[key] == value, key


def test_loader_reads_written_section() -> None:
    """The loader hands back what the file says, key by key."""
    section = _read_section()

    config = SchedulerConfigLoader(CONFIG_PATH).current()

    for key in SCHEDULER_KEYS:
        assert getattr(config, key) == section[key], key


def test_enabled_defaults_false(tmp_path: Path) -> None:
    """``enabled`` is false by default — as shipped, and with the section gone.

    Two halves of one contract.  The file this repository ships has
    the scheduler **off**, so a fresh checkout never runs the new
    scheduler next to the old cron.  And a file whose ``scheduler``
    section is gone still loads — the section going missing is a
    rollback, not a failure — and the snapshot it falls back to is that
    same "off": a rollback is done by taking the section away, so a
    section that is taken away must not start the scheduler.

    The second half reads the loader's own default for ``enabled``.
    That default is the architecture's decision table's: ``False``.
    """
    shipped = SchedulerConfigLoader(CONFIG_PATH).current()
    assert shipped.enabled is False

    document = _read_document()
    document.pop("scheduler")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    fallback = SchedulerConfigLoader(path).current()
    assert fallback == SchedulerConfig()
    assert fallback.enabled is False