"""TDD spec for :mod:`mcode_profiles` — profile enumeration and narrow mapping.

The mapping between a PDT provider name and a MiniMax Code account is
narrow on purpose (arch decision point 5): the two naming systems stay
apart, a mapping file translates between them, and a miss falls back to
the default profile instead of blocking the run. That makes the
*matching* rule the load-bearing part of this module, so the cases
pinned here are about exactness, not convenience:

1. ``mcode-profile list --json`` output parses into profile bindings.
2. The mapping is matched exactly — different case or surrounding
   whitespace does not match, and a miss is not an error.
3. A missing ``mcode-profile`` binary is a configuration error, not an
   empty profile list.
4. A binding turns into exactly the three environment variables the
   MiniMax launcher reads.
5. The provider a start-up looks up is named by the environment, in one
   fixed order — and "no provider" is a miss, not an error.

The external process is stubbed at the function boundary (test decision
point 2): the command runner is injected, so no test spawns a process
and no test mocks ``Popen``.
"""

import json
from pathlib import Path

import pytest

from harness.contract import ProfileBinding as ContractProfileBinding
from mcode_profiles import (
    DEFAULT_PROFILE_CMD,
    PROVIDER_NAME_ENV_VAR,
    PROVIDER_PRIORITY_ENV_VAR,
    HarnessConfigError,
    ProfileBinding,
    list_profiles,
    parse_profiles,
    profile_env,
    resolve_profile,
    resolve_startup_provider,
)

pytestmark = pytest.mark.unit


#: The payload ``mcode-profile list --json`` prints, byte for byte in
#: shape: the current profile first, then one entry per profile with
#: its name, its home directory and its login state.
REAL_LIST_JSON = (
    '{"current":"shared","profiles":['
    '{"name":"shared","home":"/Users/x","login_state":"cn/active"},'
    '{"name":"acc2","home":"/Users/x/.mcode/profiles/acc2/home",'
    '"login_state":"global/active"}]}'
)


def _stub_runner(payload=REAL_LIST_JSON, calls=None):
    """A command runner that answers with ``payload`` and records argv."""

    def _runner(argv):
        if calls is not None:
            calls.append(list(argv))
        return payload

    return _runner


# ---------------------------------------------------------------------------
# 1. Parsing the profile list
# ---------------------------------------------------------------------------


def test_parse_list_output():
    """The real JSON parses into one binding per profile."""
    calls = []

    profiles = list_profiles(runner=_stub_runner(calls=calls))

    # The command is the boundary: the tool, the subcommand and the flag.
    assert calls == [[DEFAULT_PROFILE_CMD, "list", "--json"]]

    assert len(profiles) == 2
    assert [binding.name for binding in profiles] == ["shared", "acc2"]
    assert profiles[1].home == "/Users/x/.mcode/profiles/acc2/home"
    assert profiles[1].login_state == "global/active"

    # A parsed binding is a contract profile binding, so the adapter
    # above this module can pass it around as one.
    assert isinstance(profiles[1], ContractProfileBinding)
    assert profiles[1].profile == "acc2"
    assert profiles[1].harness == "mcode"


def test_parse_profiles_rejects_output_that_is_not_a_profile_list():
    """Garbage from the tool is a configuration error, not an empty list."""
    with pytest.raises(HarnessConfigError):
        parse_profiles("not json at all")

    with pytest.raises(HarnessConfigError):
        parse_profiles(json.dumps({"current": "shared"}))

    with pytest.raises(HarnessConfigError):
        parse_profiles(json.dumps({"profiles": [{"name": "acc2"}]}))

    with pytest.raises(HarnessConfigError):
        parse_profiles(json.dumps(["acc2"]))

    with pytest.raises(HarnessConfigError):
        parse_profiles(json.dumps({"profiles": ["acc2"]}))

    with pytest.raises(HarnessConfigError):
        parse_profiles(json.dumps({"profiles": [{"home": "/Users/x"}]}))

    with pytest.raises(HarnessConfigError):
        parse_profiles(json.dumps({"profiles": [{"name": "acc2", "home": ""}]}))


def test_login_state_is_carried_but_never_required():
    """A profile without a login state still parses; the state is not a gate."""
    payload = json.dumps(
        {"profiles": [{"name": "acc2", "home": "/Users/x", "login_state": 7}]}
    )

    bindings = parse_profiles(payload)

    assert len(bindings) == 1
    assert bindings[0].login_state == ""


# ---------------------------------------------------------------------------
# 2. The mapping is matched exactly
# ---------------------------------------------------------------------------


def test_exact_match_only():
    """Case and surrounding whitespace do not match; a miss is not an error."""
    profiles = list_profiles(runner=_stub_runner())
    mapping = {"shared-account": "acc2"}

    hit = resolve_profile(mapping, "shared-account", profiles)
    assert hit is not None
    assert hit.name == "acc2"

    # The mapped profile name is matched exactly, too.
    assert resolve_profile({"shared-account": "ACC2"}, "shared-account", profiles) is None
    assert resolve_profile({"shared-account": " acc2 "}, "shared-account", profiles) is None

    # And so is the provider name looked up in the mapping.
    assert resolve_profile(mapping, "Shared-Account", profiles) is None
    assert resolve_profile(mapping, " shared-account ", profiles) is None


def test_unmapped_provider_falls_back_without_raising():
    """An unmapped provider yields no binding and no environment delta."""
    profiles = list_profiles(runner=_stub_runner())

    assert resolve_profile({}, "shared-account", profiles) is None
    assert resolve_profile({"other-account": "acc2"}, "shared-account", profiles) is None

    # The fallback is the default profile: nothing is injected, and the
    # run is not blocked (PM-02).
    assert profile_env(None) == {}


def test_malformed_mapping_is_a_configuration_error():
    """A mapping that is not a mapping is a broken file, not a miss."""
    with pytest.raises(HarnessConfigError):
        resolve_profile(["acc2"], "shared-account", [])  # type: ignore[arg-type]


def test_resolve_profile_without_any_known_profile_is_a_miss():
    """An empty profile list cannot hit, and it is not an error."""
    assert resolve_profile({"shared-account": "acc2"}, "shared-account", []) is None


def test_a_binding_named_by_either_field_carries_both():
    """The contract name and the launcher name are the same string."""
    from_contract = ProfileBinding(profile="acc2")

    assert from_contract.name == "acc2"
    assert from_contract.profile == "acc2"
    assert from_contract.harness == "mcode"


# ---------------------------------------------------------------------------
# 3. A missing binary is loud
# ---------------------------------------------------------------------------


def test_missing_binary_raises():
    """A missing ``mcode-profile`` is a configuration error."""
    missing = "mcode-profile-not-installed-on-this-machine"

    with pytest.raises(HarnessConfigError) as excinfo:
        list_profiles(cmd=missing)

    # The message names the command, so the operator knows what to install.
    assert missing in str(excinfo.value)


def test_failed_command_is_a_configuration_error():
    """A tool that exits non-zero does not turn into an empty profile list."""

    def _failing_runner(argv):
        raise HarnessConfigError(f"{argv[0]} exited with 1")

    with pytest.raises(HarnessConfigError):
        list_profiles(runner=_failing_runner)


# ---------------------------------------------------------------------------
# 4. The environment delta
# ---------------------------------------------------------------------------


def test_profile_env_shape():
    """A binding becomes exactly the three variables the launcher reads."""
    binding = ProfileBinding(name="acc2", home="/Users/x/.mcode/profiles/acc2/home")

    env = profile_env(binding)

    assert env == {
        "MCODE_PROFILE": "acc2",
        "HOME": binding.home,
        "MINIMAX_DATA_DIR": str(Path(binding.home) / ".minimax"),
    }


def test_profile_env_isolates_each_profile():
    """Two profiles never share a home or a data directory (PM-10)."""
    first = ProfileBinding(name="acc2", home="/Users/x/.mcode/profiles/acc2/home")
    second = ProfileBinding(name="acc3", home="/Users/x/.mcode/profiles/acc3/home")

    first_env = profile_env(first)
    second_env = profile_env(second)

    assert first_env["HOME"] != second_env["HOME"]
    assert first_env["MINIMAX_DATA_DIR"] != second_env["MINIMAX_DATA_DIR"]
    assert first_env["MCODE_PROFILE"] != second_env["MCODE_PROFILE"]


# ---------------------------------------------------------------------------
# 5. The provider a start-up looks up
# ---------------------------------------------------------------------------


def test_the_provider_name_wins_over_the_chain():
    """The named provider is the one the process runs."""
    env = {
        PROVIDER_NAME_ENV_VAR: "shared-account",
        PROVIDER_PRIORITY_ENV_VAR: "other-account,third-account",
    }

    assert resolve_startup_provider(env) == "shared-account"


def test_the_first_entry_of_the_chain_is_the_startup_provider():
    """With no named provider, the chain's first entry is the one."""
    env = {PROVIDER_PRIORITY_ENV_VAR: "first-account,second-account"}

    assert resolve_startup_provider(env) == "first-account"


def test_an_empty_provider_is_not_a_provider():
    """Whitespace is not a name; the chain answers instead."""
    env = {
        PROVIDER_NAME_ENV_VAR: "   ",
        PROVIDER_PRIORITY_ENV_VAR: "first-account",
    }

    assert resolve_startup_provider(env) == "first-account"

    # And a chain whose first entries are empty starts at the first one
    # that actually names something.
    assert (
        resolve_startup_provider({PROVIDER_PRIORITY_ENV_VAR: " , second-account"})
        == "second-account"
    )


def test_no_provider_at_all_is_a_miss():
    """Nothing configured means nothing to look up — not an error."""
    assert resolve_startup_provider({}) is None
    assert resolve_startup_provider({PROVIDER_NAME_ENV_VAR: ""}) is None
    assert resolve_startup_provider({PROVIDER_PRIORITY_ENV_VAR: ""}) is None


def test_the_process_environment_is_the_default(monkeypatch):
    """With no environment given, the process environment answers."""
    monkeypatch.setenv(PROVIDER_NAME_ENV_VAR, "shared-account")

    assert resolve_startup_provider() == "shared-account"