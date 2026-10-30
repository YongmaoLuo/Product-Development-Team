"""Unit tests: where the scheduler's settings are read from.

The scheduler's settings live in a **chain** of files, and the first
file that can be read is the one that is read:

1. the file ``PDT_SCHEDULER_FILE`` names;
2. ``<config dir>/scheduler.yaml`` — the operator's own file;
3. ``backend/config.yaml`` — the section the repository ships with;
4. the loader's own defaults, when no file is there at all.

What this file holds down:

1. **The order of the chain.**  Each level wins over the ones below it,
   and the level that won is named in the log — an operator who cannot
   tell which file the scheduler is reading cannot debug it.
2. **A file that is not there is skipped**, and so is one that cannot be
   read: the chain steps over it and the next level answers.
3. **Nothing raises.**  A scheduler that cannot find its settings still
   starts, and it starts with the defaults — which say
   ``enabled: false``, so a scheduler nobody configured schedules
   nothing.

The chain itself is a pure function over a list of candidates, so the
cases drive it directly; the server's own list of candidates is checked
beside it, because a chain nobody wires up is a chain nobody has.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import pytest
import yaml

from plan_scheduler.config import (
    SOURCE_BACKEND,
    SOURCE_CONFIG_DIR,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SchedulerConfigLoader,
    resolve_scheduler_config_source,
)


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _write(path: Path, **scheduler: object) -> Path:
    """Write a settings file and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"scheduler": scheduler}), encoding="utf-8"
    )
    return path


class _Chain:
    """The four levels of the chain, each one under the case's control.

    Attributes:
        env_file: the file the environment names.
        config_dir: the operator's configuration directory.
        config_file: the settings file inside that directory.
        backend_file: the backend's own configuration file.
    """

    def __init__(
        self,
        env_file: Path,
        config_dir: Path,
        config_file: Path,
        backend_file: Path,
    ) -> None:
        self.env_file = env_file
        self.config_dir = config_dir
        self.config_file = config_file
        self.backend_file = backend_file

    def candidates(self) -> List[Tuple[str, Path]]:
        """The chain, in order."""
        return [
            (SOURCE_ENV, self.env_file),
            (SOURCE_CONFIG_DIR, self.config_file),
            (SOURCE_BACKEND, self.backend_file),
        ]


@pytest.fixture
def chain(tmp_path, monkeypatch) -> _Chain:
    """A chain whose every level is a file of the case's own."""
    env_file = tmp_path / "env" / "scheduler.yaml"
    config_dir = tmp_path / ".config"
    config_file = config_dir / "scheduler.yaml"
    backend_file = tmp_path / "backend-config.yaml"

    monkeypatch.setenv("PDT_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("PDT_SCHEDULER_FILE", raising=False)
    return _Chain(env_file, config_dir, config_file, backend_file)


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def test_the_environment_file_wins_over_everything_else(chain):
    """The file the environment names is the first level of the chain."""
    _write(chain.env_file, enabled=True)
    _write(chain.config_file, enabled=True)
    _write(chain.backend_file, enabled=False)

    source = resolve_scheduler_config_source(chain.candidates())

    assert source.level == SOURCE_ENV, (
        "the environment's file did not win: "
        f"the chain settled on {source.level!r} ({source.path!r})"
    )
    assert source.path == chain.env_file


def test_the_configuration_directory_wins_over_the_backend_file(chain):
    """The operator's own file wins over the one the repository ships."""
    _write(chain.config_file, enabled=True)
    _write(chain.backend_file, enabled=False)

    source = resolve_scheduler_config_source(chain.candidates())

    assert source.level == SOURCE_CONFIG_DIR, (
        "the configuration directory's file did not win: "
        f"the chain settled on {source.level!r} ({source.path!r})"
    )
    assert source.path == chain.config_file


def test_the_backend_file_is_the_last_file(chain):
    """The repository's own configuration is the last file of the chain."""
    _write(chain.backend_file, enabled=False)

    source = resolve_scheduler_config_source(chain.candidates())

    assert source.level == SOURCE_BACKEND, (
        "the backend's own file did not win: "
        f"the chain settled on {source.level!r} ({source.path!r})"
    )
    assert source.path == chain.backend_file


def test_no_file_at_all_means_the_defaults(chain):
    """With no file there is nothing to read — the defaults answer."""
    source = resolve_scheduler_config_source(chain.candidates())

    assert source.level == SOURCE_DEFAULT, (
        "a chain with no file did not settle on the defaults: "
        f"{source.level!r} ({source.path!r})"
    )
    assert source.path is None, (
        "a chain with no file named a file to read: "
        f"{source.path!r}"
    )


def test_a_file_that_is_not_there_is_stepped_over(chain):
    """A name that points at nothing is not a file."""
    _write(chain.config_file, enabled=True)
    _write(chain.backend_file, enabled=False)
    # ``chain.env_file`` is deliberately never written.

    source = resolve_scheduler_config_source(chain.candidates())

    assert source.level == SOURCE_CONFIG_DIR, (
        "the chain did not step over the file that is not there: "
        f"the chain settled on {source.level!r} ({source.path!r})"
    )


def test_a_file_that_cannot_be_read_is_stepped_over(chain):
    """A file that is not YAML is not a settings file."""
    chain.env_file.parent.mkdir(parents=True, exist_ok=True)
    chain.env_file.write_text("scheduler: [this is not: a mapping\n", encoding="utf-8")
    _write(chain.config_file, enabled=True)

    source = resolve_scheduler_config_source(chain.candidates())

    assert source.level == SOURCE_CONFIG_DIR, (
        "the chain did not step over the file it could not read: "
        f"the chain settled on {source.level!r} ({source.path!r})"
    )


def test_a_file_that_is_not_a_mapping_is_stepped_over(chain):
    """YAML that is not a mapping carries no settings."""
    chain.env_file.parent.mkdir(parents=True, exist_ok=True)
    chain.env_file.write_text("- one\n- two\n", encoding="utf-8")
    _write(chain.config_file, enabled=True)

    source = resolve_scheduler_config_source(chain.candidates())

    assert source.level == SOURCE_CONFIG_DIR, (
        "the chain did not step over the file that is not a mapping: "
        f"the chain settled on {source.level!r} ({source.path!r})"
    )


def test_the_chain_never_raises(chain):
    """A chain that cannot find anything still answers."""
    chain.env_file.parent.mkdir(parents=True, exist_ok=True)
    chain.env_file.write_text("\x00 not yaml at all", encoding="utf-8")

    source = resolve_scheduler_config_source(chain.candidates())

    assert source.level == SOURCE_DEFAULT
    assert source.path is None


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


def test_the_loader_reads_no_file_when_there_is_none():
    """A loader with no file answers the defaults and raises nothing."""
    loader = SchedulerConfigLoader(None)

    snapshot = loader.current()

    assert snapshot.enabled is False, (
        "a loader with no file answered that the scheduler may run"
    )
    assert loader.path is None


def test_the_loader_reads_the_file_the_chain_settled_on(chain):
    """The loader and the chain agree on the file."""
    _write(chain.config_file, enabled=True, capacity=7)

    source = resolve_scheduler_config_source(chain.candidates())
    loader = SchedulerConfigLoader(source.path)

    snapshot = loader.current()

    assert snapshot.enabled is True
    assert snapshot.capacity == 7


# ---------------------------------------------------------------------------
# The server's own chain
# ---------------------------------------------------------------------------


def test_the_server_offers_the_chain_in_order(chain, monkeypatch):
    """The server's list of candidates is the chain, in order."""
    import server

    monkeypatch.setenv("PDT_SCHEDULER_FILE", str(chain.env_file))
    monkeypatch.setattr(server, "BACKEND_CONFIG_YAML", chain.backend_file)

    candidates = server._scheduler_settings_candidates()

    assert [level for level, _ in candidates] == [
        SOURCE_ENV,
        SOURCE_CONFIG_DIR,
        SOURCE_BACKEND,
    ], (
        "the server's chain is not the chain: "
        f"{[level for level, _ in candidates]!r}"
    )
    assert candidates[0][1] == chain.env_file
    assert candidates[1][1] == chain.config_file
    assert candidates[2][1] == chain.backend_file


def test_the_server_leaves_the_environment_level_out_when_it_is_not_set(
    chain, monkeypatch
):
    """Without the environment variable there is no environment level."""
    import server

    monkeypatch.delenv("PDT_SCHEDULER_FILE", raising=False)
    monkeypatch.setattr(server, "BACKEND_CONFIG_YAML", chain.backend_file)

    candidates = server._scheduler_settings_candidates()

    assert [level for level, _ in candidates] == [
        SOURCE_CONFIG_DIR,
        SOURCE_BACKEND,
    ], (
        "the server offered a level nobody asked for: "
        f"{[level for level, _ in candidates]!r}"
    )


def test_the_server_says_which_level_it_used(chain, monkeypatch, caplog):
    """The log names the file the scheduler reads and the level it came from.

    An operator who cannot tell which file the scheduler is reading
    cannot debug it — and there are three files it could be.
    """
    import server

    monkeypatch.setattr(server, "BACKEND_CONFIG_YAML", chain.backend_file)
    _write(chain.config_file, enabled=False)

    with caplog.at_level("WARNING", logger="server"):
        runtime = server._build_scheduler_engine()
    try:
        messages = [
            record.getMessage()
            for record in caplog.records
            if "settings" in record.getMessage()
        ]
        assert messages, (
            "the server did not say where its settings came from: "
            f"{[r.getMessage() for r in caplog.records]!r}"
        )
        assert any(SOURCE_CONFIG_DIR in message for message in messages), (
            "the log does not name the level the settings came from: "
            f"{messages!r}"
        )
        assert any(str(chain.config_file) in message for message in messages), (
            "the log does not name the file the settings come from: "
            f"{messages!r}"
        )
    finally:
        if runtime.engine is not None:
            runtime.engine.stop(timeout=5.0)
