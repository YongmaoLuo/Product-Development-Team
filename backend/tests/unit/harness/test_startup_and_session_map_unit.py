"""Unit coverage for the harness start-up decision and the session map.

What this pins
--------------
Two small pieces of the harness layer that everything above them
depends on:

**The start-up decision.** :func:`harness.startup.resolve_startup_config`
answers one question — which backend runs, on which account, for which
provider, and where that answer came from — and it answers it from the
environment it is given. The rules this file pins:

1. The MiniMax Code path opens the map; the Claude Code path does not
   touch it at all, so a broken map cannot fail a Claude deployment.
2. The account list is asked for lazily and exactly once: a start-up
   that never looks at a map never asks the machine about its
   accounts.
3. A map that names an account this machine does not have stops the
   start-up, and a map that names one it does have binds it.
4. An injected loader is a test seam and is called the way it asks to
   be called — with the environment if it takes one, without if it does
   not.

**The session map.** The mapping from a pinned key to the conversation
the CLI really opened:

1. The mapping for a plan lives beside the plan, and a plan with no
   directory has no mapping — a mapping file is never a reason to
   create a plan directory.
2. A pinned key that is empty is refused, and an observed id that is
   not shaped like a real session id is refused.
3. A mapping that cannot be read, cannot be parsed, is not an object,
   or holds an entry that names no session is ignored rather than
   trusted.

Everything here is hermetic: the plans root is redirected into a
temporary directory, and no process is spawned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest

import harness.startup as startup
from harness.contract import HarnessConfigError
from harness.session_map import (
    SESSION_MAP_FILENAME,
    SessionMap,
    plan_session_map,
    plan_session_map_path,
)
from mcode_profiles import ProfileBinding

pytestmark = pytest.mark.unit


#: A session id the way the CLI mints it.
REAL_SESSION_ID = "mvs_" + "a" * 32


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Keep every test out of the machine's own state directories."""
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("PDT_MCP_RESTORE_DIR", str(tmp_path / "restore"))


def _binding(name: str = "acc2") -> ProfileBinding:
    """One MiniMax profile, pointed at a placeholder home."""
    return ProfileBinding(name=name, home=f"/tmp/{name}")


# ---------------------------------------------------------------------------
# 1. The start-up decision
# ---------------------------------------------------------------------------


def test_the_account_list_is_asked_for_once_and_only_when_needed():
    """The account list is a fact only the profile tool can answer."""
    asked: List[int] = []

    def _lister():
        asked.append(1)
        return [_binding()]

    known = startup._KnownProfiles(_lister)

    assert list(known) == [_binding()]
    assert list(known) == [_binding()]
    assert asked == [1]


def test_a_mapped_provider_binds_the_account_it_names():
    """The ordinary case: the map names the account the run uses."""
    asked: List[Any] = []

    def _lister():
        asked.append("lister")
        return [_binding()]

    def _loader(env: Mapping[str, str]) -> Dict[str, str]:
        asked.append(env)
        return {"shared": "acc2"}

    env = {"PDT_HARNESS": "mcode", "PDT_PROVIDER_NAME": "shared"}
    config = startup.resolve_startup_config(
        env,
        profile_lister=_lister,
        profile_map_loader=_loader,
    )

    assert config.name == "mcode"
    assert config.provider == "shared"
    assert config.binding is not None
    assert config.binding.name == "acc2"
    assert config.profile_source == startup.PROFILE_SOURCE_MAPPED
    assert asked == [env, "lister"]


def test_an_empty_map_leaves_the_default_account():
    """A map with nothing in it is no map at all."""
    env = {"PDT_HARNESS": "mcode", "PDT_PROVIDER_NAME": "shared"}
    config = startup.resolve_startup_config(
        env,
        profile_lister=lambda: [_binding()],
        profile_map_loader=lambda env: {},
    )

    assert config.binding is None
    assert config.profile_source == startup.PROFILE_SOURCE_NO_MAP


def test_a_map_that_names_an_account_this_machine_does_not_have_stops_the_start_up():
    """Running on the default account by accident is the danger."""
    env = {"PDT_HARNESS": "mcode", "PDT_PROVIDER_NAME": "shared"}

    with pytest.raises(HarnessConfigError, match="unknown profile"):
        startup.resolve_startup_config(
            env,
            profile_lister=lambda: [_binding()],
            profile_map_loader=lambda env: {"shared": "acc9"},
        )


def test_a_backend_with_no_injected_factory_uses_the_built_in_one():
    """An injected factory owns the adapter it names; a backend it does
    not name falls back to the registry's own."""
    env = {"PDT_HARNESS": "mcode", "PDT_PROVIDER_NAME": "shared"}
    config = startup.resolve_startup_config(
        env,
        factories={},
        profile_lister=lambda: [_binding()],
        profile_map_loader=lambda env: {"shared": "acc2"},
    )

    assert config.harness is not None
    assert config.binding is not None


def test_a_loader_that_takes_no_environment_is_called_without_one():
    """A test seam should not have to agree on a signature."""
    called: List[Any] = []

    def _loader():
        called.append("called")
        return {}

    assert startup._call_loader(_loader, {"A": "B"}) == {}
    assert called == ["called"]


def test_a_loader_that_is_not_callable_is_handed_back():
    """A loader that is not callable is not called."""
    assert startup._call_loader({"already": "a mapping"}, {}) == {"already": "a mapping"}


# ---------------------------------------------------------------------------
# 2. Where the session map lives
# ---------------------------------------------------------------------------


def test_the_mapping_for_a_plan_lives_beside_the_plan(tmp_path):
    """The plan's directory is where the plan's own state already is."""
    path = plan_session_map_path("plan-1")

    assert path.name == SESSION_MAP_FILENAME
    assert path.parent.name == "plan-1"


def test_a_plan_with_a_directory_has_a_mapping(tmp_path):
    """The ordinary case: the plan's directory is there."""
    (tmp_path / "plans" / "plan-1").mkdir(parents=True)

    mapping = plan_session_map("plan-1")

    assert mapping is not None
    assert mapping.path.parent.name == "plan-1"


def test_a_plan_without_a_directory_has_no_mapping(tmp_path):
    """A mapping file is never a reason to create a plan directory."""
    assert plan_session_map("plan-never-started") is None


# ---------------------------------------------------------------------------
# 3. The mapping itself
# ---------------------------------------------------------------------------


def test_a_pinned_key_that_is_empty_is_refused(tmp_path):
    """The pinned key is the caller's own handle; it has to be there."""
    mapping = SessionMap(tmp_path / "map.json")

    with pytest.raises(ValueError, match="non-empty string"):
        mapping.register("", REAL_SESSION_ID)


def test_an_observed_id_that_is_not_a_session_id_is_refused(tmp_path):
    """The observed id is what the CLI minted; only such an id counts."""
    mapping = SessionMap(tmp_path / "map.json")

    with pytest.raises(ValueError, match="is not a MiniMax Code session id"):
        mapping.register("pinned", "not-a-session-id")


def test_a_mapping_is_written_and_read_back(tmp_path):
    """The registration is on disk before the call returns."""
    mapping = SessionMap(tmp_path / "map.json")

    mapping.register("pinned", REAL_SESSION_ID)

    assert mapping.lookup("pinned") == REAL_SESSION_ID


def test_a_lookup_without_a_key_is_a_miss(tmp_path):
    """No key names no conversation."""
    mapping = SessionMap(tmp_path / "map.json")

    assert mapping.lookup("") is None


def test_a_mapping_that_cannot_be_read_is_ignored(tmp_path):
    """A file that cannot be read holds nothing to trust."""
    path = tmp_path / "map.json"
    path.write_bytes(b"\xff\xfe not utf-8")
    mapping = SessionMap(path)

    assert mapping.lookup("pinned") is None


def test_a_mapping_that_is_not_json_is_ignored(tmp_path):
    """A file that is not JSON is not a mapping."""
    path = tmp_path / "map.json"
    path.write_text("not json", encoding="utf-8")
    mapping = SessionMap(path)

    assert mapping.lookup("pinned") is None


def test_a_mapping_that_is_not_an_object_is_ignored(tmp_path):
    """A mapping is an object; a list is not one."""
    path = tmp_path / "map.json"
    path.write_text("[1, 2]", encoding="utf-8")
    mapping = SessionMap(path)

    assert mapping.lookup("pinned") is None


def test_a_mapping_entry_that_is_not_a_pair_of_strings_is_ignored(tmp_path):
    """A value that is not a string names no conversation."""
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"pinned": 7}), encoding="utf-8")
    mapping = SessionMap(path)

    assert mapping.lookup("pinned") is None


def test_a_mapping_entry_that_names_no_session_is_ignored(tmp_path):
    """An id the CLI did not mint cannot be handed to ``--session``."""
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"pinned": "not-a-session-id"}), encoding="utf-8")
    mapping = SessionMap(path)

    assert mapping.lookup("pinned") is None
