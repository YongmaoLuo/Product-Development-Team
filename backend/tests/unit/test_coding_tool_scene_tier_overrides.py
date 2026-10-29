"""Unit tests for the scene → tier MODEL / EFFORT overrides.

Contract (2026-10-28 provider-tier feature, subtask 3):

  * ``provider_routing.resolve_provider_overrides(scene, provider)``
    answers "does this scene pin a model / reasoning effort for the
    provider it just picked?". It is a PURE config read that returns
    ``{}`` when nothing is pinned.
  * ``_run_claude_interactive`` asks it ONLY on the scene path — a
    scene is in effect, the scene chain selected a provider, and the
    provider is NOT served in inherit mode. Every other path (no
    scene, the legacy priority walk, inherit mode) must not consult
    the resolver and must not write a single new key.
  * A pinned ``model`` stamps ALL FOUR model variables of the child
    env — ``ANTHROPIC_MODEL`` plus the three
    ``ANTHROPIC_DEFAULT_{SONNET,HAIKU,OPUS}_MODEL`` — replacing the
    CC Switch row's own per-role values.
  * A pinned ``effort`` lands in TWO places, because the CLI reads
    both: the child process env (``CLAUDE_CODE_EFFORT_LEVEL``) and
    the per-call settings payload (top-level ``effortLevel``). It is
    NOT smuggled into ``settings["env"]``.
  * No override → ZERO writes: the CC Switch row's model values stay
    byte-for-byte, and neither ``CLAUDE_CODE_EFFORT_LEVEL`` nor
    ``effortLevel`` appears anywhere.
  * An override NEVER touches the endpoint or the credentials — it
    selects a model and an effort, nothing else.

The resolver is reached through the ``provider_routing`` MODULE
OBJECT (``provider_routing.resolve_provider_overrides(...)``), so
``monkeypatch.setattr(provider_routing, ...)`` is the seam these
tests control. A ``from provider_routing import
resolve_provider_overrides`` would take a by-value snapshot of the
function and silently bypass the patch — the very failure mode this
suite exists to catch.

The Popen subprocess is mocked; the CC Switch DB lookups are patched
at the ``ClaudeCodingTool`` seam so no real DB and no network is
touched.
"""

import copy
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool as coding_tool_module
import provider_routing
from coding_tool import ClaudeCodingTool


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


# The synthetic CC Switch row the fake DB serves. Its model values are
# DISTINCT per role so an assertion that "the four model variables all
# equal the override" cannot accidentally pass on the untouched row.
CC_SWITCH_ROW = {
    "base_url": "https://vendor-a-pro.test/anthropic",
    "api_key": "sk-vendor-a-hw-test",
    "models": {
        "default": "cc-default-model",
        "sonnet": "cc-sonnet-model",
        "haiku": "cc-haiku-model",
        "opus": "cc-opus-model",
    },
}


def _make_mock_process() -> MagicMock:
    """A MagicMock that satisfies the ``_run_claude_interactive`` read loop."""
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter([
        json.dumps({
            "type": "system", "subtype": "init",
            "session_id": "00000000-0000-0000-0000-000000000000",
        }) + "\n",
        json.dumps({
            "type": "result", "result": "ok", "is_error": False,
        }) + "\n",
    ])
    mock_proc.stderr.read.return_value = ""
    mock_proc.wait.return_value = 0
    mock_proc.poll.return_value = 0
    return mock_proc


@pytest.fixture
def fake_db(monkeypatch):
    """Patch the name-keyed availability check to consult the fake row.

    ``_check_provider_availability(name)`` is the single seam the
    dispatch consults for a candidate, so patching it here gives these
    tests a synthetic CC Switch with no real DB and no network. The
    "current provider" shortcut is pinned closed as well: it reads the
    real ``~/.cc-switch`` and would otherwise let the machine a test
    runs on decide the outcome.
    """

    def _by_name(provider_name: str):
        if provider_name == "Vendor A Pro":
            return True, copy.deepcopy(CC_SWITCH_ROW)
        return False, {}

    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability", staticmethod(_by_name)
    )
    monkeypatch.setattr(
        ClaudeCodingTool,
        "_load_cc_switch_current_provider",
        staticmethod(lambda: None),
    )
    return CC_SWITCH_ROW


@pytest.fixture
def scene_chain(monkeypatch):
    """Pin the scene chain to a single provider.

    ``resolve_provider_chain`` is imported INSIDE ``_run_claude_interactive``
    (``from provider_routing import resolve_provider_chain``), so the
    lookup happens at call time and this patch is honoured.
    """
    monkeypatch.setattr(
        provider_routing,
        "resolve_provider_chain",
        lambda scene, *args, **kwargs: ["Vendor A Pro"],
    )


@pytest.fixture
def overrides(monkeypatch):
    """Control what the override resolver answers, and record its calls.

    ``holder["value"]`` is the mapping the resolver returns; ``holder
    ["calls"]`` records every ``(scene, provider)`` it was asked about,
    so a test can assert it was NOT asked at all.
    """
    holder: dict = {"value": {}, "calls": []}

    def _fake(scene, provider_name, *args, **kwargs):
        holder["calls"].append((scene, provider_name))
        return dict(holder["value"])

    monkeypatch.setattr(provider_routing, "resolve_provider_overrides", _fake)
    return holder


@pytest.fixture
def settings_template(tmp_path):
    """A minimal per-call settings template on disk."""
    path = tmp_path / "settings_template.json"
    path.write_text(
        json.dumps({"env": {}, "permissions": {"allow": [], "deny": []}}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def settings_payload(monkeypatch):
    """Capture the settings payload exactly as it is written.

    The spy wraps the real writer, so the file really lands on disk
    and the dispatch behaves normally. The payload is copied BEFORE
    the post-mortem redaction rewrites the credentials in it.
    """
    holder: dict = {"payload": None}
    real_write = coding_tool_module.write_private_json

    def _spy(path, payload, *args, **kwargs):
        holder["payload"] = copy.deepcopy(payload)
        return real_write(path, payload, *args, **kwargs)

    monkeypatch.setattr(coding_tool_module, "write_private_json", _spy)
    return holder


def _run_dispatch(tool: ClaudeCodingTool) -> dict:
    """Run one dispatch with Popen mocked; return the child's cmd/env."""
    calls: list = []

    def _popen(cmd, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs.get("env", {})})
        return _make_mock_process()

    with patch.object(coding_tool_module.subprocess, "Popen", side_effect=_popen):
        tool._run_claude_interactive("hi")

    assert calls, "the dispatch must have spawned a subprocess"
    return calls[0]


# ---------------------------------------------------------------------------
# The override contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSceneTierOverrides:
    @pytest.fixture(autouse=True)
    def _no_inherited_effort(self, monkeypatch):
        """Drop an inherited ``CLAUDE_CODE_EFFORT_LEVEL`` from the parent.

        The child env is ``os.environ.copy()``, so a machine that
        already exports the variable (this repo's own Claude Code
        session does) would hand it to every child — and the "the
        dispatch wrote nothing" assertions would then be satisfied by
        an inherited value rather than by the absence of a write.
        Deleting it first is what makes the absence mean anything.
        """
        monkeypatch.delenv("CLAUDE_CODE_EFFORT_LEVEL", raising=False)

    def test_model_override_stamps_all_four_model_vars(
        self, fake_db, scene_chain, overrides,
        settings_template, settings_payload,
    ):
        """A pinned model replaces the CC Switch row's model everywhere.

        All FOUR variables get the same value: the row's own per-role
        models (``cc-sonnet-model`` etc.) are what the override
        replaces, so leaving any of them on the old value would hand
        the CLI two different answers about which model this call uses.
        """
        overrides["value"] = {"model": "m-2"}

        tool = ClaudeCodingTool(scene="execution", settings=str(settings_template))
        env = _run_dispatch(tool)["env"]

        assert env["ANTHROPIC_MODEL"] == "m-2"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "m-2"
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "m-2"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "m-2"

    def test_effort_override_lands_in_env_and_settings_payload(
        self, fake_db, scene_chain, overrides,
        settings_template, settings_payload,
    ):
        """A pinned effort reaches the child env AND the settings file.

        The CLI reads the effort from two places, so both are stamped:
        the process env (``CLAUDE_CODE_EFFORT_LEVEL``) and the
        per-call settings payload (top-level ``effortLevel``). It does
        NOT go into ``settings["env"]`` — that block is the ANTHROPIC_*
        endpoint/credential channel, not the effort channel.
        """
        overrides["value"] = {"effort": "max"}

        tool = ClaudeCodingTool(scene="execution", settings=str(settings_template))
        env = _run_dispatch(tool)["env"]

        assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "max"

        payload = settings_payload["payload"]
        assert payload is not None, "no settings payload was written"
        assert payload["effortLevel"] == "max"
        assert "CLAUDE_CODE_EFFORT_LEVEL" not in payload.get("env", {}), (
            "the effort belongs in the top-level 'effortLevel' key, not "
            "in the settings env block"
        )

    def test_no_overrides_leaves_effort_unset_and_keeps_cc_switch_models(
        self, fake_db, scene_chain, overrides,
        settings_template, settings_payload,
    ):
        """No pin → zero writes: the CC Switch row stays byte-for-byte.

        An empty mapping means "nothing is configured", and the
        dispatch then writes not one byte — the row's own model values
        survive and neither effort key appears anywhere.
        """
        overrides["value"] = {}

        tool = ClaudeCodingTool(scene="execution", settings=str(settings_template))
        env = _run_dispatch(tool)["env"]

        # The scene path DID ask — an empty answer is what licenses the
        # zero writes below. Without this the assertions would pass
        # simply because nothing consults the resolver at all.
        assert overrides["calls"] == [("execution", "Vendor A Pro")]

        assert "CLAUDE_CODE_EFFORT_LEVEL" not in env
        assert env["ANTHROPIC_MODEL"] == "cc-default-model"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "cc-sonnet-model"
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "cc-haiku-model"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "cc-opus-model"

        payload = settings_payload["payload"]
        assert payload is not None
        assert "effortLevel" not in payload
        assert "CLAUDE_CODE_EFFORT_LEVEL" not in payload.get("env", {})

    def test_overrides_not_consulted_without_a_scene(
        self, fake_db, scene_chain, overrides, monkeypatch,
        settings_template, settings_payload,
    ):
        """Without a scene there is no tier, so the resolver is not asked.

        The override lookup is keyed by (scene, provider); a call with
        no scene has no tier to look in, so asking would be a
        meaningless read whose answer nobody may act on.
        """
        overrides["value"] = {"model": "m-2", "effort": "max"}
        monkeypatch.setattr(
            ClaudeCodingTool, "_get_live_provider_priority",
            lambda *a, **k: ["Vendor A Pro"],
        )

        # Control: a scene dispatch DOES consult the resolver. Without
        # it the assertion below would pass for the wrong reason — a
        # resolver nobody ever calls satisfies "not consulted without a
        # scene" just as well as a correctly-scoped one does.
        scene_tool = ClaudeCodingTool(scene="execution", settings=str(settings_template))
        _run_dispatch(scene_tool)
        assert overrides["calls"] == [("execution", "Vendor A Pro")]

        # The call under test: no scene → no tier → no lookup.
        tool = ClaudeCodingTool(settings=str(settings_template))
        env = _run_dispatch(tool)["env"]

        assert overrides["calls"] == [("execution", "Vendor A Pro")], (
            "the override resolver must not be consulted without a scene"
        )
        assert "CLAUDE_CODE_EFFORT_LEVEL" not in env
        assert env["ANTHROPIC_MODEL"] == "cc-default-model"

    def test_base_url_and_credentials_unchanged_by_overrides(
        self, fake_db, scene_chain, overrides,
        settings_template, settings_payload,
    ):
        """An override picks a model and an effort — never the endpoint.

        The endpoint and the credentials belong to the CC Switch row
        the scene chain selected. An override that changed them would
        send the call to a provider the scene never approved.
        """
        overrides["value"] = {"model": "m-2", "effort": "max"}

        tool = ClaudeCodingTool(scene="execution", settings=str(settings_template))
        env = _run_dispatch(tool)["env"]

        # The override really was in effect — otherwise "unchanged"
        # would describe a dispatch that never overrode anything.
        assert overrides["calls"] == [("execution", "Vendor A Pro")]
        assert env["ANTHROPIC_MODEL"] == "m-2"
        assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "max"

        assert env["ANTHROPIC_BASE_URL"] == CC_SWITCH_ROW["base_url"]
        assert env["ANTHROPIC_API_KEY"] == CC_SWITCH_ROW["api_key"]
        assert env["ANTHROPIC_AUTH_TOKEN"] == CC_SWITCH_ROW["api_key"]

        payload = settings_payload["payload"]
        file_env = payload.get("env", {})
        assert file_env.get("ANTHROPIC_BASE_URL") == CC_SWITCH_ROW["base_url"]
        assert file_env.get("ANTHROPIC_API_KEY") == CC_SWITCH_ROW["api_key"]
        assert file_env.get("ANTHROPIC_AUTH_TOKEN") == CC_SWITCH_ROW["api_key"]

    def test_inherit_mode_never_consults_the_overrides(
        self, fake_db, scene_chain, overrides, monkeypatch,
        settings_template, settings_payload,
    ):
        """Inherit mode the tier table has no say — not even the model.

        "Inherit" means Claude Code brings its own providers, so the
        dispatch never enters the scene walk at all. The resolver must
        therefore not be asked, and neither effort key may appear.
        """
        monkeypatch.setenv("PDT_PROVIDER_MODE", "inherit")
        overrides["value"] = {"model": "m-2", "effort": "max"}

        tool = ClaudeCodingTool(scene="execution", settings=str(settings_template))
        assert tool.inherit_providers is True

        env = _run_dispatch(tool)["env"]

        assert overrides["calls"] == [], (
            "the override resolver must not be consulted in inherit mode"
        )
        assert "CLAUDE_CODE_EFFORT_LEVEL" not in env

        payload = settings_payload["payload"]
        assert payload is not None
        assert "effortLevel" not in payload

    def test_a_raising_resolver_degrades_to_no_overrides(
        self, fake_db, scene_chain, monkeypatch,
        settings_template, settings_payload,
    ):
        """A resolver that raises must not take the dispatch down.

        An override picks the MODEL of a call; a config read that
        breaks is a reason to serve the call with the CC Switch row's
        own model — never a reason to fail the call. The failure is
        logged so an operator can act on it.
        """

        def _boom(scene, provider_name, *args, **kwargs):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(
            provider_routing, "resolve_provider_overrides", _boom
        )

        logger = MagicMock()
        tool = ClaudeCodingTool(
            scene="execution", settings=str(settings_template), logger=logger,
        )
        env = _run_dispatch(tool)["env"]

        assert env["ANTHROPIC_MODEL"] == "cc-default-model"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "cc-sonnet-model"
        assert "CLAUDE_CODE_EFFORT_LEVEL" not in env

        payload = settings_payload["payload"]
        assert payload is not None
        assert "effortLevel" not in payload

        assert any(
            call.args and call.args[0] == "scene_tier_override_error"
            for call in logger.warning.call_args_list
        ), (
            "the failure must be reported as 'scene_tier_override_error' "
            f"so an operator can act on it; logged: "
            f"{logger.warning.call_args_list!r}"
        )
