"""End-to-end acceptance for the scene → tier MODEL / EFFORT overrides.

Contract (2026-10-28 provider-tier feature, subtask 5):

This suite is the FINAL acceptance of the feature. Where subtask 3
proved the override plumbing with a stubbed resolver, this suite walks
the whole path with the REAL pieces:

    the SHIPPED ``example/provider_routing.yaml.example``
        → the REAL ``provider_routing.resolve_provider_chain``
        → the REAL ``provider_routing.resolve_provider_overrides``
        → the REAL settings payload written to disk
        → the REAL child environment handed to ``Popen``.

Only the two edges are stubbed, and neither of them decides anything:

  * ``subprocess.Popen`` — so no model is ever called. The child env is
    still the REAL one the dispatch built.
  * the CC Switch database — ``_list_display_names`` /
    ``_optimizer_order`` / ``_check_provider_availability`` /
    ``_load_cc_switch_current_provider`` — so the test never reads a
    real provider database and never touches the network.

What the suite pins:

  * A CONFIGURED tier (ultra / high / medium / low) stamps the pinned
    model into ALL FOUR model variables and the pinned effort into BOTH
    landing places (the child env's ``CLAUDE_CODE_EFFORT_LEVEL`` and
    the settings payload's top-level ``effortLevel``).
  * An UNCONFIGURED tier (the ``max`` tier's bare-string entry) writes
    NOTHING: the CC Switch row's own model values survive byte for
    byte, and neither effort key appears anywhere.
  * The SHIPPED template is what drives the scene → tier mapping — it
    is the format contract, so the delivery artifact is exercised
    directly rather than a fixture that merely resembles it.
  * Editing the config between two dispatches takes effect on the NEXT
    dispatch, with no cache clear: the hot-reload path is the promise
    the template's header makes, so it is proved rather than assumed.

The settings payload is located with ``rglob('subagent_settings_*.json')``
rather than by a hard-coded name: the per-call payload lives in a
``mkdtemp`` directory whose name and whose uuid suffix are both
generated at dispatch time, and ``effective_settings_path`` is a local
of ``_run_claude_interactive`` that the caller cannot see.
"""

from __future__ import annotations

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
# Constants
# ---------------------------------------------------------------------------

# Locate the repository root. This file lives at
# ``backend/tests/unit/test_coding_tool_tier_overrides_e2e.py``, so
# parents[3] is the repo root (unit/ -> tests/ -> backend/ -> root).
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The SHIPPED routing template — the artifact this suite accepts.
_TEMPLATE = _REPO_ROOT / "example" / "provider_routing.yaml.example"

#: The CC Switch display names the fake database serves. These are the
#: names the shipped template's tier patterns are written against, so
#: the tier → provider resolution is the one an operator would get.
FAKE_DISPLAY_NAMES = ["Example Strong", "Example Balanced", "Example Cheap"]

#: The synthetic CC Switch row every fake provider serves. Its per-role
#: models are DISTINCT so an assertion that "all four model variables
#: equal the pinned model" cannot pass on the untouched row.
FAKE_ROW = {
    "base_url": "https://example.test/anthropic",
    "api_key": "sk-example-test",
    "models": {
        "default": "cc-default",
        "sonnet": "cc-sonnet",
        "haiku": "cc-haiku",
        "opus": "cc-opus",
    },
}

#: The four model variables of the child environment. A pinned model
#: replaces ALL FOUR — leaving any of them on the CC Switch row's value
#: would hand the CLI two different answers about which model the call
#: runs on.
MODEL_ENV_VARS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def _routing_cache_guard():
    """Keep the routing module's parsed-config cache out of the way.

    The cache is a module-level singleton, so a test that leaves an
    entry behind would decide the next test's outcome. It is dropped
    before AND after every test.

    The hot-reload test relies on the cache NOT being cleared between
    its two dispatches — the fingerprint (mtime, size) is what notices
    the edit, and clearing the cache by hand would prove nothing about
    it. The teardown below runs after that test's body, so the
    guarantee it makes is untouched.
    """
    provider_routing.clear_routing_cache()
    yield
    provider_routing.clear_routing_cache()


@pytest.fixture(autouse=True)
def _no_inherited_effort(monkeypatch):
    """Drop an inherited ``CLAUDE_CODE_EFFORT_LEVEL`` from the parent.

    The child env is ``os.environ.copy()``, so a machine that already
    exports the variable (this repo's own Claude Code session does)
    would hand it to every child — and the "the dispatch wrote nothing"
    assertions would then be satisfied by an inherited value rather
    than by the absence of a write. Deleting it first is what makes the
    absence mean anything.
    """
    monkeypatch.delenv("CLAUDE_CODE_EFFORT_LEVEL", raising=False)


@pytest.fixture
def cc_switch_stub(monkeypatch):
    """Serve a synthetic CC Switch: three rows, no real DB, no network.

    ``_list_display_names`` and ``_optimizer_order`` are the two
    ``provider_routing`` seams that reach out to the outside world; the
    availability check is the ``coding_tool`` seam. All three are
    stubbed here so the tier → provider resolution is driven by the
    REAL routing code over a FAKE world.
    """
    monkeypatch.setenv("PDT_PROVIDER_MODE", "cc-switch")
    monkeypatch.setattr(
        provider_routing,
        "_list_display_names",
        lambda db_path=None: list(FAKE_DISPLAY_NAMES),
    )
    monkeypatch.setattr(provider_routing, "_optimizer_order", lambda: [])

    def _by_name(provider_name: str):
        if provider_name in FAKE_DISPLAY_NAMES:
            return True, copy.deepcopy(FAKE_ROW)
        return False, {}

    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability", staticmethod(_by_name)
    )
    monkeypatch.setattr(
        ClaudeCodingTool,
        "_load_cc_switch_current_provider",
        staticmethod(lambda: None),
    )
    return FAKE_ROW


@pytest.fixture
def private_root(monkeypatch, tmp_path):
    """Keep the per-call settings payload inside ``tmp_path``.

    ``private_dir()`` mints a fresh ``0700`` directory under
    ``$PDT_SECRET_TEMP_ROOT``; pointing that at ``tmp_path`` is what
    lets the test find the payload it wrote without guessing a path
    outside the test's own tree.
    """
    monkeypatch.setenv("PDT_SECRET_TEMP_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def no_redaction(monkeypatch):
    """The post-mortem credential rewrite is not what this suite tests.

    ``redact_all`` replaces the routed provider's key in the payload
    after the child is reaped. Stubbing it keeps the payload on disk
    exactly as the dispatch wrote it, so what the assertions read is
    the dispatch's own output.
    """
    monkeypatch.setattr(
        coding_tool_module, "redact_all", lambda *args, **kwargs: 0
    )


@pytest.fixture
def settings_template(tmp_path):
    """A minimal per-call settings template on disk."""
    path = tmp_path / "settings_template.json"
    path.write_text(
        json.dumps({"env": {}, "permissions": {"allow": [], "deny": []}}),
        encoding="utf-8",
    )
    return path


def _dispatch(tool: ClaudeCodingTool) -> dict:
    """Run one dispatch with Popen mocked; return the child's cmd/env."""
    calls: list = []

    def _popen(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "env": kwargs.get("env", {})})
        return _make_mock_process()

    with patch.object(coding_tool_module.subprocess, "Popen", side_effect=_popen):
        tool._run_claude_interactive("hi")

    assert calls, "the dispatch must have spawned a subprocess"
    return calls[0]


def _settings_files(root: Path) -> set:
    """Every per-call settings payload written under ``root``."""
    return set(root.rglob("subagent_settings_*.json"))


def _read_settings_payload(root: Path) -> dict:
    """Return the payload the dispatch under test wrote.

    Exactly one payload is expected under ``root``: each dispatch mints
    its own ``private_dir()``, so a test that dispatches once leaves one
    settings file behind. The name is not guessed — it is located by the
    ``subagent_settings_*.json`` shape, because the uuid suffix is
    generated per dispatch and ``effective_settings_path`` is a local of
    ``_run_claude_interactive`` that the caller cannot see.
    """
    files = _settings_files(root)
    assert len(files) == 1, (
        f"expected exactly one settings payload, got {sorted(files)!r}"
    )
    path = next(iter(files))
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The end-to-end contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTierOverridesEndToEnd:
    def test_configured_tier_dispatch_stamps_model_and_effort(
        self, monkeypatch, tmp_path, cc_switch_stub, private_root,
        no_redaction, settings_template,
    ):
        """A configured tier reaches the child env AND the settings file.

        ``verification`` routes to the ``ultra`` tier, whose entry pins
        a model and an effort for ``Example Strong``. The dispatch must
        write the model into ALL FOUR model variables (not just
        ``ANTHROPIC_MODEL``) and the effort into BOTH places the CLI
        reads it from.
        """
        monkeypatch.setenv("PDT_PROVIDER_ROUTING_FILE", str(_TEMPLATE))

        tool = ClaudeCodingTool(scene="verification", settings=str(settings_template))
        env = _dispatch(tool)["env"]

        for var in MODEL_ENV_VARS:
            assert env[var] == "example-model-premium", (
                f"{var} must carry the pinned model, got {env.get(var)!r}"
            )
        assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "max"

        payload = _read_settings_payload(tmp_path)
        assert payload["effortLevel"] == "max"
        # The payload on disk is the DISPATCH's own output: the routed
        # provider's key is still the raw one, which is only true because
        # the post-mortem redaction is stubbed out. If this ever reads
        # "<redacted>", the stub stopped taking effect and the payload
        # on disk is no longer what the dispatch wrote.
        assert payload["env"]["ANTHROPIC_API_KEY"] == "sk-example-test"

    def test_unconfigured_tier_dispatch_keeps_cc_switch_row_values(
        self, monkeypatch, tmp_path, cc_switch_stub, private_root,
        no_redaction, settings_template,
    ):
        """A tier with nothing pinned writes not one byte.

        ``prd`` routes to the ``max`` tier, whose entry is the bare
        string form — membership only. There is no model and no effort
        to apply, so the CC Switch row's own values survive untouched
        and neither effort key appears anywhere.
        """
        monkeypatch.setenv("PDT_PROVIDER_ROUTING_FILE", str(_TEMPLATE))

        tool = ClaudeCodingTool(scene="prd", settings=str(settings_template))
        env = _dispatch(tool)["env"]

        assert env["ANTHROPIC_MODEL"] == "cc-default"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "cc-sonnet"
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "cc-haiku"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "cc-opus"
        assert "CLAUDE_CODE_EFFORT_LEVEL" not in env

        payload = _read_settings_payload(tmp_path)
        assert "effortLevel" not in payload

    def test_medium_scene_dispatch_uses_medium_effort(
        self, monkeypatch, tmp_path, cc_switch_stub, private_root,
        no_redaction, settings_template,
    ):
        """The tier a scene resolves to decides the effort it gets.

        ``execution`` routes to the ``medium`` tier, whose entry pins
        ``example-model-standard`` and ``medium``. A different scene
        landing on a different tier therefore lands on a different
        effort — the tier table is the whole mechanism, and the shipped
        template drives it.
        """
        monkeypatch.setenv("PDT_PROVIDER_ROUTING_FILE", str(_TEMPLATE))

        tool = ClaudeCodingTool(scene="execution", settings=str(settings_template))
        env = _dispatch(tool)["env"]

        assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "medium"
        assert env["ANTHROPIC_MODEL"] == "example-model-standard"

    def test_shipped_template_drives_scene_tier_resolution(
        self, monkeypatch,
    ):
        """The delivery template is what maps a scene to a tier.

        The template ships into the public repository, so it IS the
        format contract: the next reader copies it. A scene → tier
        table that lives only in a test fixture would leave the
        artifact itself unverified.
        """
        monkeypatch.setenv("PDT_PROVIDER_ROUTING_FILE", str(_TEMPLATE))

        assert provider_routing.resolve_scene_tier("verification") == "ultra"
        assert provider_routing.resolve_scene_tier("prd") == "max"
        assert provider_routing.resolve_scene_tier("execution") == "medium"
        assert provider_routing.resolve_scene_tier("summarizer") == "low"

    def test_editing_config_between_dispatches_takes_effect(
        self, monkeypatch, tmp_path, cc_switch_stub, private_root,
        no_redaction, settings_template,
    ):
        """An edit lands on the NEXT dispatch, with no cache clear.

        The template's header promises hot reload, so the promise is
        proved here: the config is copied into ``tmp_path``, edited,
        and a SECOND dispatch — on a fresh tool instance — must see the
        new effort.

        The edit changes the file's LENGTH (``'medium'`` → ``'low'``),
        so the (mtime, size) fingerprint the routing cache keys on
        cannot miss it. ``clear_routing_cache()`` is deliberately NOT
        called between the two dispatches: a hand-cleared cache would
        prove the cache works, not the hot-reload path.

        The shipped template is never modified — only the copy.
        """
        routing_copy = tmp_path / "routing.yaml"
        routing_copy.write_text(
            _TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        monkeypatch.setenv("PDT_PROVIDER_ROUTING_FILE", str(routing_copy))

        first = ClaudeCodingTool(scene="execution", settings=str(settings_template))
        env_before = _dispatch(first)["env"]
        assert env_before["CLAUDE_CODE_EFFORT_LEVEL"] == "medium"

        # Edit the COPY. The shipped template stays untouched.
        text = routing_copy.read_text(encoding="utf-8")
        assert text.count("effort: 'medium'") == 1, (
            "the template is expected to carry exactly one 'medium' effort pin"
        )
        routing_copy.write_text(
            text.replace("effort: 'medium'", "effort: 'low'"), encoding="utf-8"
        )

        second = ClaudeCodingTool(scene="execution", settings=str(settings_template))
        env_after = _dispatch(second)["env"]
        assert env_after["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
