"""The reaper's configuration surface and the spawn tags it judges by.

Two things decide whether the orphan reaper kills anything, and both are
pinned here:

* the *configuration* — every knob is an environment variable, and every
  knob's default is the safe value.  A deployment that configures nothing
  must not be able to kill anything by accident, and a typo in a value
  must fall back to that default instead of raising (a reaper that
  refuses to start because someone wrote ``PDT_ORPHAN_REAPER_INTERVAL=30s``
  is a reaper that never reclaims the memory it exists to reclaim).
* the *spawn tags* — ``PDT_SPAWN_*`` is the **only** evidence the reaper
  is allowed to act on.  If the tags are wrong, the reaper either kills
  processes it did not create or never kills the ones it did.

Two properties of the tags are load-bearing and easy to break:

1. Tags are re-stamped at every spawn level (owner = the process that
   actually spawned the child), so an *inherited* tag must never survive
   a new owner — that is what makes a grandchild an orphan when its
   parent dies.
2. Nothing in this file may leave ``PDT_SPAWN_*`` behind in the pytest
   process.  A leaked tag would make the pytest process itself look like
   a PDT child to a later test, which is exactly the kind of
   cross-test contamination the hermetic environment exists to prevent.
   Every test that touches ``os.environ`` therefore goes through
   ``monkeypatch`` (which restores the previous state on teardown).
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import (  # noqa: E402
    DEFAULT_PROTECT_PORTS,
    KIND_AGENT,
    KIND_FIXTURE,
    KIND_REAPER,
    KIND_SERVICE,
    KIND_SUBPROCESS,
    SPAWN_KIND,
    SPAWN_OWNER_PID,
    SPAWN_OWNER_START,
    ReaperConfig,
    apply_spawn_tags,
    config_from_env,
    default_plans_root,
    install_owner_marks,
    spawn_tags,
)

ALL_KINDS = (
    KIND_SERVICE,
    KIND_FIXTURE,
    KIND_SUBPROCESS,
    KIND_AGENT,
    KIND_REAPER,
)


@pytest.fixture
def clean_spawn_env(monkeypatch):
    """Remove any ``PDT_SPAWN_*`` marker for the duration of one test.

    ``monkeypatch`` restores the previous state on teardown — including
    the *absence* of the variable — so a test that stamps the pytest
    process cannot leak a marker into the next test.

    The ``setenv``/``delenv`` pair is deliberate.  ``monkeypatch.delenv``
    on a variable that is *not* set records nothing to restore, so a
    marker written by the test afterwards would survive the teardown.
    Setting it first (which records "was not set") and deleting it
    again (which records the temporary value) makes the undo restore
    the original state in both directions.
    """
    for key in (SPAWN_OWNER_PID, SPAWN_OWNER_START, SPAWN_KIND):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    return monkeypatch


# --------------------------------------------------------------------------
# Spawn tags
# --------------------------------------------------------------------------


def test_apply_spawn_tags_overwrites_inherited():
    """A new owner must replace the inherited marker, never merge with it."""
    env = {
        SPAWN_OWNER_PID: "999",
        SPAWN_OWNER_START: "stale",
        SPAWN_KIND: KIND_SERVICE,
    }

    apply_spawn_tags(env, KIND_SUBPROCESS, owner_pid=42, owner_start="17")

    assert env[SPAWN_OWNER_PID] == "42"
    assert env[SPAWN_OWNER_START] == "17"
    assert env[SPAWN_KIND] == KIND_SUBPROCESS


def test_apply_spawn_tags_returns_the_same_mapping():
    """The caller's mapping is updated in place and handed back."""
    env = {}

    result = apply_spawn_tags(env, KIND_SERVICE, owner_pid=42, owner_start="17")

    assert result is env
    assert result == {
        SPAWN_OWNER_PID: "42",
        SPAWN_OWNER_START: "17",
        SPAWN_KIND: KIND_SERVICE,
    }


def test_spawn_tags_default_to_the_current_process():
    """Without explicit arguments the tag names *this* process."""
    tags = spawn_tags(KIND_SERVICE)

    assert tags[SPAWN_OWNER_PID] == str(os.getpid())
    assert tags[SPAWN_KIND] == KIND_SERVICE
    assert set(tags) == {SPAWN_OWNER_PID, SPAWN_OWNER_START, SPAWN_KIND}


def test_unknown_kind_is_rejected():
    """An unrecognised kind is a programming error, not a silent default."""
    with pytest.raises(ValueError):
        spawn_tags("not-a-kind")

    with pytest.raises(ValueError):
        apply_spawn_tags({}, "not-a-kind")


def test_every_declared_kind_is_accepted():
    for kind in ALL_KINDS:
        assert spawn_tags(kind)[SPAWN_KIND] == kind


# --------------------------------------------------------------------------
# Owner marks
# --------------------------------------------------------------------------


def test_install_owner_marks_sets_self(clean_spawn_env):
    """The installed marks identify this process, not the one that made it."""
    expected_start = reaper.process_start_epoch(os.getpid())

    install_owner_marks()

    assert os.environ[SPAWN_OWNER_PID] == str(os.getpid())
    assert os.environ[SPAWN_OWNER_START] == expected_start


def test_install_owner_marks_writes_no_kind_and_is_idempotent(clean_spawn_env):
    """Only the two owner marks are written, and writing them twice is a no-op.

    ``KIND`` is deliberately not touched: the owner marks answer "who
    spawned this", the kind answers "what is this".  A process that
    installs owner marks is not thereby a service, a fixture or a reaper.
    """
    first = install_owner_marks()
    second = install_owner_marks()

    assert first == second
    assert SPAWN_KIND not in first
    assert SPAWN_KIND not in os.environ
    assert os.environ[SPAWN_OWNER_PID] == str(os.getpid())


def test_install_owner_marks_can_target_an_explicit_mapping(clean_spawn_env):
    """Passing a mapping leaves ``os.environ`` untouched."""
    env = {}

    install_owner_marks(env)

    assert env[SPAWN_OWNER_PID] == str(os.getpid())
    assert SPAWN_OWNER_PID not in os.environ


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_config_defaults():
    """An empty environment yields the documented, safe defaults."""
    cfg = config_from_env({})

    assert isinstance(cfg, ReaperConfig)
    assert cfg.enabled is True
    assert cfg.interval == 30
    assert cfg.grace == 5
    assert cfg.idle_exit == 1800
    assert cfg.dry_run is False
    assert cfg.pid_scope is None
    assert cfg.protect_ports == DEFAULT_PROTECT_PORTS
    assert len(cfg.protect_ports) == 5


def test_protect_ports_union():
    """Configured ports are *added* to the defaults, and junk is ignored."""
    cfg = config_from_env({"PDT_ORPHAN_REAPER_PROTECT_PORTS": "4321,bad"})

    assert 4321 in cfg.protect_ports
    assert DEFAULT_PROTECT_PORTS <= cfg.protect_ports
    assert "bad" not in cfg.protect_ports


def test_reaper_can_be_disabled_by_environment():
    """``PDT_ORPHAN_REAPER=0`` is the off switch and it must actually work."""
    assert config_from_env({"PDT_ORPHAN_REAPER": "0"}).enabled is False
    assert config_from_env({"PDT_ORPHAN_REAPER": "1"}).enabled is True


def test_invalid_numeric_values_fall_back_to_defaults():
    """A malformed number must not raise — the reaper falls back, it does not fail."""
    cfg = config_from_env(
        {
            "PDT_ORPHAN_REAPER_INTERVAL": "not-a-number",
            "PDT_ORPHAN_REAPER_GRACE": "soon",
            "PDT_ORPHAN_REAPER_IDLE_EXIT": "",
            "PDT_ORPHAN_REAPER_BURST_THRESHOLD": "many",
            "PDT_ORPHAN_REAPER_SWAP_THRESHOLD": "high",
        }
    )

    assert cfg.interval == 30
    assert cfg.grace == 5
    assert cfg.idle_exit == 1800
    assert cfg.burst_threshold == 50
    assert cfg.swap_threshold == 90


def test_numeric_values_are_honoured_when_valid():
    cfg = config_from_env(
        {
            "PDT_ORPHAN_REAPER_INTERVAL": "7",
            "PDT_ORPHAN_REAPER_GRACE": "1.5",
            "PDT_ORPHAN_REAPER_IDLE_EXIT": "60",
            "PDT_ORPHAN_REAPER_BURST_THRESHOLD": "10",
            "PDT_ORPHAN_REAPER_SWAP_THRESHOLD": "95",
            "PDT_ORPHAN_REAPER_DRY_RUN": "1",
        }
    )

    assert cfg.interval == 7
    assert cfg.grace == 1.5
    assert cfg.idle_exit == 60
    assert cfg.burst_threshold == 10
    assert cfg.swap_threshold == 95
    assert cfg.dry_run is True


def test_pid_scope_is_unset_by_default_and_parsed_when_given():
    """``pid_scope`` is the test-scope gate: unset means "look at everything"."""
    assert config_from_env({}).pid_scope is None

    cfg = config_from_env({"PDT_ORPHAN_REAPER_PID_SCOPE": "11,22,bad"})

    assert cfg.pid_scope == (11, 22)


# --------------------------------------------------------------------------
# Plans root
# --------------------------------------------------------------------------


def test_plans_root_from_env(monkeypatch, tmp_path):
    """``PDT_PLANS_DIR`` wins over the repository default."""
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path))

    cfg = config_from_env()

    assert cfg.plans_root == tmp_path


def test_default_plans_root_prefers_the_env_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path))

    assert default_plans_root() == tmp_path


def test_default_plans_root_falls_back_to_the_repository(monkeypatch):
    """Without the override the root is ``<repo>/plans``, derived from the file."""
    monkeypatch.delenv("PDT_PLANS_DIR", raising=False)

    module_path = Path(reaper.__file__).resolve()
    assert default_plans_root() == module_path.parents[1] / "plans"


# --------------------------------------------------------------------------
# Module shape
# --------------------------------------------------------------------------


def test_module_imports_only_the_standard_library():
    """The module must run standalone: ``python -m backend.orphan_reaper``.

    Importing any sibling backend module would drag in the whole server
    (config, database, credentials) and make the reaper unusable as a
    standalone process — and would make it impossible to run it from a
    checkout where the rest of the backend cannot even be imported.
    """
    source = Path(reaper.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pytest.fail("relative import in a stdlib-only module")
            if node.module:
                imported.add(node.module.split(".")[0])

    outside_stdlib = sorted(
        name for name in imported if name not in sys.stdlib_module_names
    )

    assert outside_stdlib == []
