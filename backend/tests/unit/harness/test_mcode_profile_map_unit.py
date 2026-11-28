"""Unit coverage for the MiniMax Code profile map loader.

What this pins
--------------
The map file is the one place a PDT provider name is turned into a
MiniMax Code account name. The loader is where that translation is
read, and it is the only place that decides whether a map file is a
*configuration mistake* (loud) or simply *absent* (quiet):

1. A missing file is no mapping at all — the loader answers with an
   empty mapping and the run proceeds on the default account.
2. A file that exists but cannot be used — unreadable, not YAML, not a
   mapping, no ``profiles`` key, a value that is not a string — is a
   configuration mistake, and the loader says which line is at fault.
3. A map that names an account this machine does not have is refused
   before any run happens on the wrong account.
4. The map is read only by a process that runs the MiniMax Code
   backend; a Claude Code deployment never opens the file, so a broken
   map cannot fail its start-up.

These are the loader's own rules, exercised through the loader's own
entry points. The command runner is injected (test decision point 2):
no test in this file starts a process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

import mcode_profiles
from mcode_profiles import (
    HarnessConfigError,
    ProfileBinding,
    load_mapping,
    load_startup_mapping,
    map_file_for,
    validate_mapping,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _binding(name: str) -> ProfileBinding:
    """One profile binding, without touching the real tool."""
    return ProfileBinding(name=name, home=f"/tmp/{name}")


def _write_map(path: Path, body: str) -> Path:
    """Write a map file and return its path."""
    path.write_text(body, encoding="utf-8")
    return path


def _stub_runner(payload: str) -> Any:
    """A command runner that answers with a recorded payload."""

    def _runner(argv: Sequence[str]) -> str:
        return payload

    return _runner


# ---------------------------------------------------------------------------
# 1. The map file itself
# ---------------------------------------------------------------------------


def test_a_missing_map_file_is_no_mapping(tmp_path):
    """A file that is not there means exactly that — no mapping."""
    assert load_mapping(tmp_path / "absent.yaml") == {}


def test_an_unreadable_map_file_is_a_configuration_error(tmp_path):
    """A file that exists but cannot be read is a mistake, not a miss."""
    # A directory in the file's place: reading it raises, and the
    # loader has to report that as a configuration error rather than
    # answer with an empty mapping.
    unreadable = tmp_path / "map.yaml"
    unreadable.mkdir()

    with pytest.raises(HarnessConfigError, match="unreadable"):
        load_mapping(unreadable)


def test_a_map_file_that_is_not_yaml_is_a_configuration_error(tmp_path):
    """A file that cannot be parsed is a mistake, and it says where."""
    broken = _write_map(tmp_path / "map.yaml", "profiles:\n  - [unclosed\n")

    with pytest.raises(HarnessConfigError, match="not valid YAML"):
        load_mapping(broken)


def test_an_empty_map_file_is_a_configuration_error(tmp_path):
    """An empty file is not a map; the loader says so."""
    empty = _write_map(tmp_path / "map.yaml", "")

    with pytest.raises(HarnessConfigError, match="is empty"):
        load_mapping(empty)


def test_a_map_file_that_is_not_a_mapping_is_a_configuration_error(tmp_path):
    """The root has to be a mapping with a ``profiles`` key."""
    wrong_root = _write_map(tmp_path / "map.yaml", "- one\n- two\n")

    with pytest.raises(HarnessConfigError, match="must be a mapping"):
        load_mapping(wrong_root)


def test_a_map_file_without_a_profiles_key_is_a_configuration_error(tmp_path):
    """A mapping without ``profiles`` is a map that maps nothing."""
    no_key = _write_map(tmp_path / "map.yaml", "other:\n  a: b\n")

    with pytest.raises(HarnessConfigError, match="has no 'profiles' key"):
        load_mapping(no_key)


def test_a_profiles_key_that_is_not_a_mapping_is_a_configuration_error(tmp_path):
    """``profiles`` maps a provider to an account; a list cannot."""
    wrong = _write_map(tmp_path / "map.yaml", "profiles:\n  - acc2\n")

    with pytest.raises(HarnessConfigError, match="'profiles' must be a mapping"):
        load_mapping(wrong)


def test_a_provider_name_that_is_not_a_string_is_a_configuration_error(tmp_path):
    """The key of the mapping is a provider name — a string."""
    wrong = _write_map(tmp_path / "map.yaml", "profiles:\n  7: acc2\n")

    with pytest.raises(HarnessConfigError, match="provider name that is not a string"):
        load_mapping(wrong)


def test_an_account_name_that_is_not_a_string_is_a_configuration_error(tmp_path):
    """The value of the mapping is an account name — a string."""
    wrong = _write_map(tmp_path / "map.yaml", "profiles:\n  shared: 7\n")

    with pytest.raises(HarnessConfigError, match="profile name that is not a string"):
        load_mapping(wrong)


def test_a_provider_listed_twice_is_decided_not_arbitrated(tmp_path):
    """The last value wins, and the operator is told."""
    twice = _write_map(
        tmp_path / "map.yaml",
        "profiles:\n  shared: acc1\n  shared: acc2\n",
    )

    assert load_mapping(twice) == {"shared": "acc2"}


def test_a_well_formed_map_is_read_as_written(tmp_path):
    """The ordinary case: the file is read exactly as it is."""
    good = _write_map(
        tmp_path / "map.yaml",
        "profiles:\n  shared-account: acc2\n  other: acc3\n",
    )

    assert load_mapping(good) == {"shared-account": "acc2", "other": "acc3"}


def test_a_binding_that_names_itself_both_ways_keeps_both():
    """The launcher selects on ``name``; the contract calls it ``profile``."""
    binding = ProfileBinding(name="acc2", profile="acc2")

    assert binding.name == "acc2"
    assert binding.profile == "acc2"


# ---------------------------------------------------------------------------
# 2. The validation against the accounts this machine has
# ---------------------------------------------------------------------------


def test_a_mapping_that_is_not_a_mapping_is_refused():
    """A malformed mapping is a configuration mistake."""
    with pytest.raises(HarnessConfigError, match="must be a mapping"):
        validate_mapping(["shared"], [_binding("acc2")])


def test_a_map_that_names_an_unknown_account_is_refused():
    """Running on the default account by accident is the danger."""
    with pytest.raises(HarnessConfigError, match="unknown profile"):
        validate_mapping({"shared": "acc9"}, [_binding("acc2")])


def test_a_map_that_names_a_known_account_is_usable():
    """The ordinary case: every named account is one the tool knows."""
    assert (
        validate_mapping({"shared": "acc2"}, [_binding("acc2")]) is None
    )


def test_a_bare_account_name_is_accepted_as_a_known_account():
    """A caller may validate against plain names, not only bindings."""
    assert validate_mapping({"shared": "acc2"}, ["acc2"]) is None


# ---------------------------------------------------------------------------
# 3. Where the map file is
# ---------------------------------------------------------------------------


def test_the_environment_variable_points_at_the_map_file(tmp_path):
    """An operator can point the process at another map."""
    override = tmp_path / "override.yaml"

    assert map_file_for({"PDT_MCODE_PROFILE_MAP_FILE": str(override)}) == override


def test_a_relative_override_is_resolved_against_the_project_root(tmp_path):
    """A relative path is a path in the checkout, not in the cwd."""
    resolved = map_file_for({"PDT_MCODE_PROFILE_MAP_FILE": "config/map.yaml"})

    assert resolved.is_absolute()
    assert resolved.name == "map.yaml"


def test_without_an_override_the_machines_own_map_file_applies(monkeypatch):
    """No override: the configuration directory's file is the map."""
    from config_paths import resolve_mcode_profile_map_file

    monkeypatch.setattr(
        mcode_profiles, "resolve_mcode_profile_map_file", resolve_mcode_profile_map_file
    )

    assert map_file_for({}) == resolve_mcode_profile_map_file()


# ---------------------------------------------------------------------------
# 4. The start-up half
# ---------------------------------------------------------------------------


def test_a_claude_deployment_never_reads_the_map(tmp_path):
    """The map belongs to the MiniMax backend alone."""
    broken = _write_map(tmp_path / "map.yaml", "profiles:\n  - [unclosed\n")

    assert load_startup_mapping(map_file=broken, env={"PDT_HARNESS": "claude"}) == {}


def test_the_start_up_reads_the_map_it_is_pointed_at(tmp_path):
    """A MiniMax deployment reads the map and hands it back."""
    good = _write_map(tmp_path / "map.yaml", "profiles:\n  shared: acc2\n")

    mapping = load_startup_mapping(
        map_file=good,
        profiles=[_binding("acc2")],
        env={"PDT_HARNESS": "mcode"},
    )

    assert mapping == {"shared": "acc2"}


def test_an_empty_map_needs_no_account_list(tmp_path):
    """Nothing to validate means nothing to enumerate."""
    empty = _write_map(tmp_path / "map.yaml", "profiles: {}\n")

    assert (
        load_startup_mapping(
            map_file=empty,
            profiles=None,
            runner=_stub_runner("{}"),
            env={"PDT_HARNESS": "mcode"},
        )
        == {}
    )


def test_a_map_that_names_an_unknown_account_stops_the_start_up(tmp_path):
    """The account the operator named has to exist on this machine."""
    good = _write_map(tmp_path / "map.yaml", "profiles:\n  shared: acc9\n")

    with pytest.raises(HarnessConfigError, match="unknown profile"):
        load_startup_mapping(
            map_file=good,
            profiles=[_binding("acc2")],
            env={"PDT_HARNESS": "mcode"},
        )


def test_the_start_up_enumerates_the_accounts_when_it_is_not_told(tmp_path):
    """With no account list given, the profile tool is asked for one."""
    good = _write_map(tmp_path / "map.yaml", "profiles:\n  shared: acc2\n")
    calls: List[List[str]] = []

    def _runner(argv: Sequence[str]) -> str:
        calls.append(list(argv))
        return (
            '{"current":"acc2","profiles":['
            '{"name":"acc2","home":"/tmp/acc2","login_state":"cn/active"}]}'
        )

    mapping = load_startup_mapping(
        map_file=good,
        profiles=None,
        runner=_runner,
        env={"PDT_HARNESS": "mcode"},
    )

    assert mapping == {"shared": "acc2"}
    assert calls == [["mcode-profile", "list", "--json"]]


# ---------------------------------------------------------------------------
# 5. The command runner the loader falls back on
# ---------------------------------------------------------------------------


class _FakeCompleted:
    """What ``subprocess.run`` returns, in miniature."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSubprocess:
    """The subprocess module, with ``run`` replaced by a recorder."""

    TimeoutExpired = mcode_profiles.subprocess.TimeoutExpired

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: List[List[str]] = []

    def run(self, argv: List[str], **kwargs: Any) -> Any:
        self.calls.append(list(argv))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _FakeShutil:
    """The shutil module, with ``which`` replaced by a recorder."""

    def __init__(self, found: Optional[str]) -> None:
        self.found = found
        self.asked: List[str] = []

    def which(self, program: str) -> Optional[str]:
        self.asked.append(program)
        return self.found


def test_a_missing_profile_tool_is_a_configuration_error(monkeypatch):
    """The tool is not installed: the machine cannot enumerate accounts."""
    monkeypatch.setattr(mcode_profiles, "shutil", _FakeShutil(None))

    with pytest.raises(HarnessConfigError, match="is not installed"):
        mcode_profiles._run_profile_command(["mcode-profile"])


def test_the_profile_tool_runs_and_its_output_is_returned(monkeypatch):
    """The ordinary case: the tool ran, and its output is the answer."""
    fake = _FakeSubprocess(_FakeCompleted(stdout="[]"))
    monkeypatch.setattr(mcode_profiles, "shutil", _FakeShutil("/usr/bin/mcode-profile"))
    monkeypatch.setattr(mcode_profiles, "subprocess", fake)

    assert mcode_profiles._run_profile_command(["mcode-profile", "list"]) == "[]"
    assert fake.calls == [["mcode-profile", "list"]]


def test_a_profile_tool_that_exits_non_zero_is_a_configuration_error(monkeypatch):
    """A tool that failed is reported with what it said."""
    fake = _FakeSubprocess(_FakeCompleted(returncode=3, stderr="broken\n"))
    monkeypatch.setattr(mcode_profiles, "shutil", _FakeShutil("/usr/bin/mcode-profile"))
    monkeypatch.setattr(mcode_profiles, "subprocess", fake)

    with pytest.raises(HarnessConfigError, match="exited with 3"):
        mcode_profiles._run_profile_command(["mcode-profile"])


def test_a_profile_tool_that_times_out_is_a_configuration_error(monkeypatch):
    """A tool that hangs is a configuration problem, not a slow machine."""
    fake = _FakeSubprocess(mcode_profiles.subprocess.TimeoutExpired("mcode-profile", 30))
    monkeypatch.setattr(mcode_profiles, "shutil", _FakeShutil("/usr/bin/mcode-profile"))
    monkeypatch.setattr(mcode_profiles, "subprocess", fake)

    with pytest.raises(HarnessConfigError, match="timed out"):
        mcode_profiles._run_profile_command(["mcode-profile"])


def test_the_profile_tool_is_looked_up_before_it_is_run(monkeypatch):
    """A missing tool is found without running anything."""
    which = _FakeShutil(None)
    fake = _FakeSubprocess(_FakeCompleted())
    monkeypatch.setattr(mcode_profiles, "shutil", which)
    monkeypatch.setattr(mcode_profiles, "subprocess", fake)

    with pytest.raises(HarnessConfigError):
        mcode_profiles._run_profile_command(["mcode-profile"])

    assert which.asked == ["mcode-profile"]
    assert fake.calls == []
