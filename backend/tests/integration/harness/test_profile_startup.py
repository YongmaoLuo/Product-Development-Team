"""TDD spec for the MiniMax Code profile map at start-up.

The map file is the one place where a PDT provider name is translated
into a MiniMax Code account name (arch decision point 5). The two
naming systems stay apart; the file is the narrow bridge between them.
This spec pins the four properties that decide whether a start-up is
safe:

1. The **path** is resolved with an environment override first, so an
   operator can point the process at another map without editing the
   checkout.
2. A **missing** file is not fatal. An operator who never wrote one has
   no mapping, and the process runs on the default profile.
3. A map that names a profile the ``mcode-profile`` tool does not know
   **is** fatal. The dangerous failure is not a crash — it is running
   every provider on the *default* account while the operator believes
   a specific account was selected.
4. The map is read **only** when the process runs the MiniMax Code
   backend. A Claude Code deployment never opens the file, so a broken
   map cannot fail its start-up.

And one property that is about the **production chain** rather than
about the loader: the map has to be read by the process that starts,
not only by the tests that call the loader directly. A loader that
every test calls and no process calls has not been wired in — the file
would never be opened in a real run, and the profile would never be
bound. So the last section goes through the same construction chain the
process uses and reads the map back out of the adapter it built.

The boundary is a real account (test decision point 11): the account
list belongs to the ``mcode-profile`` tool, so the case that needs it
is skipped when the tool is not installed rather than answered with an
invented list. The production cases answer it with a stand-in tool on
``PATH`` instead, because they must run everywhere.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Dict

import pytest

import mcode_profiles
from config_paths import resolve_mcode_profile_map_file
from harness.claude import ClaudeHarness
from harness.contract import TaskInvocation
from harness.mcode import McodeHarness
from harness.registry import resolve_harness
from harness.startup import (
    PROFILE_SOURCE_DEFAULT,
    PROFILE_SOURCE_MAPPED,
    PROFILE_SOURCE_NO_MAP,
    PROFILE_SOURCE_NO_PROVIDER,
    resolve_startup_config,
)
from mcode_profiles import (
    HarnessConfigError,
    ProfileBinding,
    list_profiles,
    load_mapping,
    load_startup_mapping,
    validate_mapping,
)

pytestmark = pytest.mark.integration

#: The tool that owns the MiniMax Code accounts.
PROFILE_TOOL = "mcode-profile"

#: Skipped when the tool is not installed — the account list is a fact
#: only the tool can answer.
requires_profile_tool = pytest.mark.skipif(
    shutil.which(PROFILE_TOOL) is None,
    reason=f"{PROFILE_TOOL} is not installed on this machine",
)

#: The permission disproof only means something for a user that file
#: permissions bind. Root reads a ``chmod 000`` file happily, so there
#: is nothing to disprove there.
requires_unreadable_files = pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0,
    reason="root bypasses file permissions; the permission disproof cannot hold",
)


def _write_map(path: Path, body: str) -> Path:
    """Write a map file and return its path."""
    path.write_text(body, encoding="utf-8")
    return path


def _profile(name: str, home: Path) -> ProfileBinding:
    """One profile binding, without touching the real tool."""
    return ProfileBinding(name=name, home=str(home))


# ---------------------------------------------------------------------------
# 1. The path: environment override first
# ---------------------------------------------------------------------------


def test_env_var_overrides_path(tmp_path, monkeypatch):
    """The environment variable wins over the config directory's file."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_map(
        config_dir / "mcode_profile_map.yaml",
        "profiles:\n  from-config-dir: acc2\n",
    )

    override = _write_map(
        tmp_path / "override.yaml",
        "profiles:\n  from-override: acc3\n",
    )

    monkeypatch.setenv("PDT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("PDT_MCODE_PROFILE_MAP_FILE", str(override))

    # The override is the resolved path ...
    assert resolve_mcode_profile_map_file() == override

    # ... and it is the file the start-up actually reads: the map it
    # loads names the override's provider, not the config directory's.
    monkeypatch.setenv("PDT_HARNESS", "mcode")
    mapping = load_startup_mapping(
        profiles=[_profile("acc3", tmp_path / "acc3-home")]
    )

    assert mapping == {"from-override": "acc3"}


# ---------------------------------------------------------------------------
# 2. A missing file is not fatal
# ---------------------------------------------------------------------------


def test_missing_file_is_not_fatal(tmp_path, monkeypatch):
    """No map file means no mapping — not a failed start-up."""
    missing = tmp_path / "mcode_profile_map.yaml"
    assert not missing.exists()

    monkeypatch.setenv("PDT_HARNESS", "mcode")
    monkeypatch.setenv("PDT_MCODE_PROFILE_MAP_FILE", str(missing))

    # The loader answers with an empty mapping ...
    assert load_mapping(missing) == {}

    # ... and the start-up completes with one.
    assert load_startup_mapping() == {}


# ---------------------------------------------------------------------------
# 3. A map that names an unknown profile fails the start-up
# ---------------------------------------------------------------------------


@requires_profile_tool
def test_unknown_profile_fails_startup(tmp_path, monkeypatch):
    """A profile the tool does not know stops the process."""
    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n  some-provider: pdt-not-a-real-profile\n",
    )

    monkeypatch.setenv("PDT_HARNESS", "mcode")
    monkeypatch.setenv("PDT_MCODE_PROFILE_MAP_FILE", str(map_file))

    with pytest.raises(HarnessConfigError) as excinfo:
        load_startup_mapping()

    # The message names the profile that is missing, so the operator
    # knows which line of the map to fix.
    assert "pdt-not-a-real-profile" in str(excinfo.value)

    # The positive control: a profile the tool really knows is accepted,
    # so the failure above is about the name and not about the plumbing.
    known = list_profiles()[0].name
    _write_map(map_file, f"profiles:\n  some-provider: {known}\n")

    assert load_startup_mapping() == {"some-provider": known}


def test_validate_mapping_reports_the_missing_profile(tmp_path):
    """The validator names the profile it could not find."""
    profiles = [_profile("acc2", tmp_path / "acc2-home")]

    with pytest.raises(HarnessConfigError) as excinfo:
        validate_mapping({"a": "nope"}, profiles)

    assert "nope" in str(excinfo.value)

    # A map that names a known profile is usable.
    assert validate_mapping({"a": "acc2"}, profiles) is None


# ---------------------------------------------------------------------------
# 4. The Claude Code path never reads the map
# ---------------------------------------------------------------------------


def test_claude_path_never_reads_map(tmp_path, monkeypatch):
    """A Claude Code deployment does not open the map file at all."""
    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n  some-provider: some-profile\n",
    )
    # Unreadable: if the file were opened, the read would fail here.
    map_file.chmod(0o000)

    monkeypatch.setenv("PDT_HARNESS", "claude")
    monkeypatch.setenv("PDT_MCODE_PROFILE_MAP_FILE", str(map_file))

    resolved: Dict[str, bool] = {}

    def _spy():
        resolved["called"] = True
        return map_file

    monkeypatch.setattr(mcode_profiles, "resolve_mcode_profile_map_file", _spy)

    try:
        if getattr(os, "geteuid", lambda: 0)() != 0:
            # The premise of the test: the file really cannot be read.
            with pytest.raises(PermissionError):
                map_file.read_text(encoding="utf-8")

        # The start-up succeeds ...
        assert load_startup_mapping() == {}
    finally:
        map_file.chmod(0o600)

    # ... because the map is not even looked for.
    assert resolved == {}


# ---------------------------------------------------------------------------
# 5. A broken map file fails the start-up, and says where
# ---------------------------------------------------------------------------


def test_invalid_yaml_fails_startup(tmp_path, monkeypatch):
    """Unparsable YAML stops the process and names the file and line."""
    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n  - [unclosed\n",
    )

    monkeypatch.setenv("PDT_HARNESS", "mcode")
    monkeypatch.setenv("PDT_MCODE_PROFILE_MAP_FILE", str(map_file))

    with pytest.raises(HarnessConfigError) as excinfo:
        load_startup_mapping()

    message = str(excinfo.value)
    assert re.search(rf"{re.escape(str(map_file))}:\d+", message), message


def test_missing_profiles_key_fails_startup(tmp_path, monkeypatch):
    """A map without the ``profiles`` key is a broken file, not an empty one."""
    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "some_other_key: acc2\n",
    )

    monkeypatch.setenv("PDT_HARNESS", "mcode")
    monkeypatch.setenv("PDT_MCODE_PROFILE_MAP_FILE", str(map_file))

    with pytest.raises(HarnessConfigError) as excinfo:
        load_startup_mapping()

    message = str(excinfo.value)
    assert "profiles" in message
    assert re.search(rf"{re.escape(str(map_file))}:\d+", message), message


# ---------------------------------------------------------------------------
# 6. The MiniMax Code path does read the map — and says so when it cannot
# ---------------------------------------------------------------------------


@requires_unreadable_files
def test_mcode_path_reports_an_unreadable_map(tmp_path, monkeypatch):
    """The counterpart of the claude case: the mcode path reads the file."""
    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n  some-provider: some-profile\n",
    )
    map_file.chmod(0o000)

    monkeypatch.setenv("PDT_HARNESS", "mcode")
    monkeypatch.setenv("PDT_MCODE_PROFILE_MAP_FILE", str(map_file))

    try:
        with pytest.raises(HarnessConfigError) as excinfo:
            load_startup_mapping()
    finally:
        map_file.chmod(0o600)

    message = str(excinfo.value)
    assert str(map_file) in message
    assert "unreadable" in message


def test_duplicate_provider_key_keeps_the_last_value(tmp_path, caplog):
    """A provider listed twice is decided, not silently arbitrated."""
    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n"
        "  some-provider: first-profile\n"
        "  some-provider: second-profile\n",
    )

    with caplog.at_level(logging.WARNING):
        mapping = load_mapping(map_file)

    # The rule is fixed, so it can be asserted.
    assert mapping == {"some-provider": "second-profile"}

    # And the operator is told which key was ambiguous.
    assert "some-provider" in caplog.text


# ---------------------------------------------------------------------------
# 7. The production start-up applies the map
# ---------------------------------------------------------------------------


def _install_profile_tool(tmp_path: Path, monkeypatch, payload: str) -> None:
    """Put a stand-in ``mcode-profile`` on ``PATH``.

    The account list is a fact only the real tool can answer, and these
    cases must run on a machine that has never installed it: a script
    that prints a recorded payload answers the same question, and the
    start-up cannot tell the two apart.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / PROFILE_TOOL
    tool.write_text(
        "#!/bin/sh\n"
        "cat <<'JSON'\n"
        f"{payload}\n"
        "JSON\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)

    monkeypatch.setenv(
        "PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    )


def _tool_payload(home: Path) -> str:
    """The JSON ``mcode-profile list --json`` prints for one account."""
    return json.dumps(
        {
            "current": "acc2",
            "profiles": [{"name": "acc2", "home": str(home)}],
        }
    )


def test_production_startup_applies_the_profile_mapping_to_mcode_harness(
    tmp_path, monkeypatch
):
    """The process that starts binds the mapped profile to the adapter.

    This is the production chain: the same call the process makes when
    it starts, with the same environment. A loader that only the tests
    call has not been wired in, and the profile would never be bound.
    """
    home = tmp_path / "acc2-home"
    home.mkdir()
    _install_profile_tool(tmp_path, monkeypatch, _tool_payload(home))

    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n  some-provider: acc2\n",
    )

    monkeypatch.setenv("PDT_HARNESS", "mcode")
    monkeypatch.setenv("PDT_MCODE_PROFILE_MAP_FILE", str(map_file))
    monkeypatch.setenv("PDT_PROVIDER_NAME", "some-provider")

    harness = resolve_harness(os.environ)

    assert isinstance(harness, McodeHarness)

    # The account the map names is the one the child runs under.
    env_delta = harness.build_invocation(
        TaskInvocation(prompt="hello")
    ).env_delta

    assert env_delta["MCODE_PROFILE"] == "acc2"
    assert env_delta["HOME"] == str(home)
    assert env_delta["MINIMAX_DATA_DIR"] == str(home / ".minimax")

    # Exactly the three variables the launcher reads — and nothing
    # Anthropic, which would point the run back at the claude provider.
    assert set(env_delta) == {"MCODE_PROFILE", "HOME", "MINIMAX_DATA_DIR"}


def test_production_startup_without_a_map_runs_on_the_default_profile(
    tmp_path, monkeypatch
):
    """No map file means the default profile, and the start-up goes on."""
    home = tmp_path / "acc2-home"
    home.mkdir()
    _install_profile_tool(tmp_path, monkeypatch, _tool_payload(home))

    missing = tmp_path / "mcode_profile_map.yaml"
    assert not missing.exists()

    monkeypatch.setenv("PDT_HARNESS", "mcode")
    monkeypatch.setenv("PDT_MCODE_PROFILE_MAP_FILE", str(missing))
    monkeypatch.setenv("PDT_PROVIDER_NAME", "some-provider")

    harness = resolve_harness(os.environ)

    assert isinstance(harness, McodeHarness)
    assert harness.build_invocation(
        TaskInvocation(prompt="hello")
    ).env_delta == {}


def test_production_startup_with_an_unmapped_provider_uses_the_default_profile(
    tmp_path, monkeypatch
):
    """A provider the map does not name is a miss, not a failed start-up."""
    home = tmp_path / "acc2-home"
    home.mkdir()
    _install_profile_tool(tmp_path, monkeypatch, _tool_payload(home))

    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n  some-provider: acc2\n",
    )

    monkeypatch.setenv("PDT_HARNESS", "mcode")
    monkeypatch.setenv("PDT_MCODE_PROFILE_MAP_FILE", str(map_file))
    monkeypatch.setenv("PDT_PROVIDER_NAME", "another-provider")

    harness = resolve_harness(os.environ)

    assert isinstance(harness, McodeHarness)
    assert harness.build_invocation(
        TaskInvocation(prompt="hello")
    ).env_delta == {}


def test_production_startup_names_the_profile_the_map_misses(
    tmp_path, monkeypatch
):
    """A map that names an account this machine does not have stops the start-up."""
    home = tmp_path / "acc2-home"
    home.mkdir()
    _install_profile_tool(tmp_path, monkeypatch, _tool_payload(home))

    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n  some-provider: pdt-not-a-real-profile\n",
    )

    monkeypatch.setenv("PDT_HARNESS", "mcode")
    monkeypatch.setenv("PDT_MCODE_PROFILE_MAP_FILE", str(map_file))
    monkeypatch.setenv("PDT_PROVIDER_NAME", "some-provider")

    with pytest.raises(HarnessConfigError) as excinfo:
        resolve_harness(os.environ)

    # The message names the profile that is missing, so the operator
    # knows which line of the map to fix.
    assert "pdt-not-a-real-profile" in str(excinfo.value)


def test_the_production_claude_path_never_reads_the_map(tmp_path, monkeypatch):
    """The claude factory does not open the map, so a broken one cannot fail it."""
    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n  - [unclosed\n",
    )

    monkeypatch.setenv("PDT_HARNESS", "claude")
    monkeypatch.setenv("PDT_MCODE_PROFILE_MAP_FILE", str(map_file))
    monkeypatch.setenv("PDT_PROVIDER_NAME", "some-provider")

    # A broken map would raise if it were read. The start-up does not
    # raise, so the file was never opened.
    harness = resolve_harness(os.environ)

    assert isinstance(harness, ClaudeHarness)


# ---------------------------------------------------------------------------
# 8. The start-up module: the decision, made from the environment it is given
# ---------------------------------------------------------------------------
#
# The registry is the public face of the choice; ``harness.startup`` is
# where the choice is actually made, so that it can be made *from an
# environment* rather than from the process environment. The four cases
# below are the ones the map has to get right: a hit, a miss, a broken
# map, and a backend that has no use for the map at all.


def _startup_env(map_file: Path, provider: str = "some-provider") -> Dict[str, str]:
    """The environment a start-up is handed, and nothing else.

    It is deliberately *not* the process environment: the provider and
    the map live in here and nowhere else, so a start-up that answered
    from the process environment would find no provider, open no map
    and bind nothing.
    """
    return {
        "PDT_HARNESS": "mcode",
        "PDT_PROVIDER_NAME": provider,
        "PDT_MCODE_PROFILE_MAP_FILE": str(map_file),
    }


def _isolate_process_env(monkeypatch) -> None:
    """Make the process environment disagree with the start-up's.

    The start-up must answer from the environment it was handed, so the
    process environment is emptied of every variable that could decide
    the answer — otherwise a start-up that peeked at ``os.environ``
    would pass this suite by accident.
    """
    monkeypatch.setenv("PDT_HARNESS", "claude")
    monkeypatch.delenv("PDT_PROVIDER_NAME", raising=False)
    monkeypatch.delenv("PDT_PROVIDER_PRIORITY", raising=False)
    monkeypatch.delenv("PDT_MCODE_PROFILE_MAP_FILE", raising=False)


def test_production_startup_binds_the_mapped_profile_to_the_adapter(
    tmp_path, monkeypatch, caplog
):
    """The start-up binds the mapped profile to the adapter it builds.

    This is the production chain: the module the process starts from,
    asked with the environment the process runs in. The profile has to
    end up **on the adapter** — a binding that stops in a return value
    is a binding nobody launches with.
    """
    home = tmp_path / "acc2-home"
    home.mkdir()
    _install_profile_tool(tmp_path, monkeypatch, _tool_payload(home))

    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n  some-provider: acc2\n",
    )

    _isolate_process_env(monkeypatch)

    with caplog.at_level(logging.INFO):
        config = resolve_startup_config(_startup_env(map_file))

    # The decision is the one the environment asks for ...
    assert config.name == "mcode"
    assert config.provider == "some-provider"
    assert config.profile_source == PROFILE_SOURCE_MAPPED
    assert config.binding is not None
    assert config.binding.name == "acc2"

    # ... and the adapter the process runs carries it.
    env_delta = config.harness.build_invocation(
        TaskInvocation(prompt="hello")
    ).env_delta

    assert env_delta["MCODE_PROFILE"] == "acc2"
    assert env_delta["HOME"] == str(home)
    assert env_delta["MINIMAX_DATA_DIR"] == str(home / ".minimax")
    assert set(env_delta) == {"MCODE_PROFILE", "HOME", "MINIMAX_DATA_DIR"}

    # The audit record says what was decided and why, so "the map is
    # configured but it did not take effect" is a fact in the log rather
    # than a suspicion.
    assert "harness_startup" in caplog.text
    assert "some-provider" in caplog.text
    assert "acc2" in caplog.text


def test_startup_never_opens_the_map_for_the_claude_backend():
    """The claude path must not open the map — not even to look."""
    opened = []

    def _spy(env):
        opened.append(env)
        return {"some-provider": "acc2"}

    config = resolve_startup_config(
        {"PDT_HARNESS": "claude"}, profile_map_loader=_spy
    )

    assert opened == []
    assert config.name == "claude"
    assert config.binding is None
    assert config.map_path is None
    assert config.profile_source == PROFILE_SOURCE_DEFAULT


def test_startup_names_the_profile_the_map_misses(tmp_path, monkeypatch):
    """A map that names an account this machine does not have stops the start-up."""
    home = tmp_path / "acc2-home"
    home.mkdir()
    _install_profile_tool(tmp_path, monkeypatch, _tool_payload(home))

    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n  some-provider: pdt-not-a-real-profile\n",
    )

    _isolate_process_env(monkeypatch)

    with pytest.raises(HarnessConfigError) as excinfo:
        resolve_startup_config(_startup_env(map_file))

    # The message names the profile that is missing, so the operator
    # knows which line of the map to fix.
    assert "pdt-not-a-real-profile" in str(excinfo.value)


def test_startup_falls_back_to_the_default_profile_for_an_unmapped_provider(
    tmp_path, monkeypatch
):
    """A provider the map does not name is a miss, not a failed start-up."""
    home = tmp_path / "acc2-home"
    home.mkdir()
    _install_profile_tool(tmp_path, monkeypatch, _tool_payload(home))

    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n  some-provider: acc2\n",
    )

    _isolate_process_env(monkeypatch)

    config = resolve_startup_config(_startup_env(map_file, "another-provider"))

    assert config.profile_source == PROFILE_SOURCE_DEFAULT
    assert config.binding is None
    assert config.harness.build_invocation(
        TaskInvocation(prompt="hello")
    ).env_delta == {}


def test_startup_without_a_map_runs_on_the_default_profile(tmp_path, monkeypatch):
    """No map file means the default profile, and the start-up goes on."""
    home = tmp_path / "acc2-home"
    home.mkdir()
    _install_profile_tool(tmp_path, monkeypatch, _tool_payload(home))

    missing = tmp_path / "mcode_profile_map.yaml"
    assert not missing.exists()

    _isolate_process_env(monkeypatch)

    config = resolve_startup_config(_startup_env(missing))

    assert config.profile_source == PROFILE_SOURCE_NO_MAP
    assert config.binding is None
    assert config.harness.build_invocation(
        TaskInvocation(prompt="hello")
    ).env_delta == {}


def test_startup_without_a_provider_does_not_open_the_map(tmp_path, monkeypatch):
    """No provider means nothing to look up, so the map is not opened."""
    map_file = _write_map(
        tmp_path / "mcode_profile_map.yaml",
        "profiles:\n  some-provider: acc2\n",
    )

    _isolate_process_env(monkeypatch)

    opened = []

    def _spy(env):
        opened.append(env)
        return {"some-provider": "acc2"}

    config = resolve_startup_config(
        {"PDT_HARNESS": "mcode"}, profile_map_loader=_spy
    )

    assert opened == []
    assert config.profile_source == PROFILE_SOURCE_NO_PROVIDER
    assert config.binding is None
    assert config.map_path is None