"""Unit tests for scene-based provider routing in ``ClaudeCodingTool``.

Contract (2026-09-13 provider-routing feature, subtask 2):

  * ``ClaudeCodingTool(scene=...)`` resolves the provider candidate list
    for the given workflow scene via ``provider_routing.resolve_provider_chain``
    (scene → tier → ordered regexes over CC Switch display names).
  * The scene chain is re-resolved on EVERY ``_run_claude_interactive``
    call (hot-reload friendly — same pattern as
    ``_get_live_provider_priority``), not frozen at construction.
  * The dispatch walk prefers the scene chain: each candidate display
    name is checked by name through ``_check_provider_availability``
    before falling back to the ``provider_priority`` walk.
  * A provider missing from the DB is skipped (no crash), and when the
    scene chain yields nothing the priority walk runs unchanged — zero
    regression for callers that don't pass ``scene``.
  * ``query_json(scene=...)`` / ``query(scene=...)`` override the
    instance scene for that one call (refiner / audit / summarizer
    call-sites rely on this).

The Popen subprocess is mocked; the CC Switch DB lookups are monkey-
patched at the ``ClaudeCodingTool`` seam so no real DB or network is
touched.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool as coding_tool_module
from coding_tool import ClaudeCodingTool


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_mock_process() -> MagicMock:
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


# Fake CC Switch DB rows keyed by display name — the seam that
# ``_check_provider_availability`` ultimately hits.
FAKE_DB = {
    "Vendor D API": {
        "base_url": "https://api.vendor-d.test/anthropic",
        "api_key": "sk-vendor-d-test",
    },
    "Vendor B Pro": {
        "base_url": "https://api.vendor-b.example/api",
        "api_key": "sk-vendor-b-test",
    },
    "Vendor A": {
        "base_url": "https://api.vendor-a.test/anthropic",
        "api_key": "sk-vendor-a-test",
    },
    "Vendor A Pro": {
        "base_url": "https://vendor-a-pro.test/anthropic",
        "api_key": "sk-vendor-a-hw-test",
    },
}


@pytest.fixture
def fake_db(monkeypatch):
    """Patch the name-keyed availability check to consult FAKE_DB.

    ``_check_provider_availability(name)`` is the single seam the
    dispatch consults for a candidate — the scene chain and the
    ``provider_priority`` walk both hand it CC Switch names verbatim —
    so patching it here gives these tests a synthetic CC Switch with no
    real DB and no network.

    The CC Switch "current provider" shortcut is pinned closed as well.
    It answers ahead of the priority walk whenever the scene chain is
    empty, and it reads the *real* ``~/.cc-switch``; leaving it live
    would let the machine a test runs on decide the outcome.
    """

    def _by_name(provider_name: str):
        cfg = FAKE_DB.get(provider_name)
        if not cfg:
            return False, {}
        return True, {
            "base_url": cfg["base_url"],
            "api_key": cfg["api_key"],
            "models": {},
        }

    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability", staticmethod(_by_name)
    )
    monkeypatch.setattr(
        ClaudeCodingTool,
        "_load_cc_switch_current_provider",
        staticmethod(lambda: None),
    )
    # No time-dependent policy seam is needed here any more: the peak-hour
    # ranking lives in the optimizer's rule engine and reaches the walk as
    # the chain ORDER, which these tests control directly.
    return FAKE_DB


@pytest.fixture
def scene_chain(monkeypatch):
    """Patch provider_routing.resolve_provider_chain with a controllable seam.

    Returns a holder dict; tests set ``holder['chain']`` to control what
    the resolver returns.
    """
    import provider_routing

    holder = {"chain": []}
    monkeypatch.setattr(
        provider_routing,
        "resolve_provider_chain",
        lambda scene, config_path=None, db_path=None: list(holder["chain"]),
    )
    return holder


def _capture_popen_env_and_args():
    """Context-managed Popen patch returning (mock, calls)."""
    calls = []

    def _popen(cmd, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs.get("env", {})})
        return _make_mock_process()

    return patch.object(coding_tool_module.subprocess, "Popen", side_effect=_popen), calls


# ---------------------------------------------------------------------------
# Construction-time scene
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSceneConstructor:
    def test_scene_chain_provider_wins_over_priority_walk(
        self, fake_db, scene_chain, monkeypatch
    ):
        """The scene chain's first available provider is selected even
        though the priority walk would have picked a different one."""
        scene_chain["chain"] = ["Vendor A Pro"]
        # The priority chain would start with Vendor B Pro; make sure the
        # scene chain beats it.
        monkeypatch.setattr(
            ClaudeCodingTool, "_get_live_provider_priority", lambda *a, **k: ["Vendor B Pro"],
        )
        tool = ClaudeCodingTool(scene="execution")
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")

        assert calls, "subprocess must be spawned"
        env = calls[0]["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://vendor-a-pro.test/anthropic"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-vendor-a-hw-test"

    def test_scene_provider_missing_from_db_falls_to_next_in_chain(
        self, fake_db, scene_chain
    ):
        scene_chain["chain"] = ["Ghost Provider", "Vendor A"]
        tool = ClaudeCodingTool(scene="execution")
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")
        env = calls[0]["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://api.vendor-a.test/anthropic"

    def test_empty_scene_chain_falls_back_to_legacy_walk(
        self, fake_db, scene_chain, monkeypatch
    ):
        """Zero scene candidates → the priority walk runs unchanged."""
        scene_chain["chain"] = []
        monkeypatch.setattr(
            ClaudeCodingTool, "_get_live_provider_priority", lambda *a, **k: ["Vendor B Pro"],
        )
        tool = ClaudeCodingTool(scene="execution")
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")
        env = calls[0]["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://api.vendor-b.example/api"

    def test_no_scene_kwarg_preserves_legacy_behaviour(
        self, fake_db, monkeypatch
    ):
        """Callers that never pass ``scene`` must be bit-for-bit unaffected."""
        monkeypatch.setattr(
            ClaudeCodingTool, "_get_live_provider_priority", lambda *a, **k: ["Vendor B Pro"],
        )
        tool = ClaudeCodingTool()
        assert tool.scene is None
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")
        env = calls[0]["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://api.vendor-b.example/api"


# ---------------------------------------------------------------------------
# Per-call scene override
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPerCallSceneOverride:
    def test_run_interactive_scene_kwarg_overrides_instance(
        self, fake_db, scene_chain
    ):
        scene_chain["chain"] = ["Vendor A Pro"]
        tool = ClaudeCodingTool(scene="execution")
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            # The instance scene would select Vendor A Pro; the per-call
            # override selects Vendor B Pro (chain from the override call).
            scene_chain["chain"] = ["Vendor B Pro"]
            tool._run_claude_interactive("hi", scene="refiner")
        env = calls[0]["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://api.vendor-b.example/api"

    def test_query_json_forwards_scene(self, fake_db, scene_chain, monkeypatch):
        scene_chain["chain"] = ["Vendor B Pro"]
        monkeypatch.setattr(
            ClaudeCodingTool, "_get_live_provider_priority", lambda *a, **k: ["Vendor B Pro"],
        )
        tool = ClaudeCodingTool(scene="verification")
        captured = {}

        real_interactive = ClaudeCodingTool._run_claude_interactive

        def _spy(prompt, *args, **kwargs):
            captured["scene"] = kwargs.get("scene")
            # Short-circuit: don't spawn a real subprocess for this test;
            # the routing contract is that scene reaches the walk. Return
            # valid JSON so query_json's parse step succeeds.
            captured["base_url"] = None
            return '{"a": 1}', None, {}

        monkeypatch.setattr(
            ClaudeCodingTool, "_run_claude_interactive", _spy,
        )
        tool.query_json('{"a": 1}', scene="audit_second_pass")
        assert captured["scene"] == "audit_second_pass"

    def test_scene_chain_reresolved_each_call_hot_reload(
        self, fake_db, scene_chain
    ):
        """The chain holder mutates between two calls; the second call
        must pick the NEW chain (no construction-time freezing)."""
        scene_chain["chain"] = ["Vendor A Pro"]
        tool = ClaudeCodingTool(scene="execution")
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("call-1")
            scene_chain["chain"] = ["Vendor D API"]
            tool._run_claude_interactive("call-2")
        assert calls[0]["env"]["ANTHROPIC_BASE_URL"] == "https://vendor-a-pro.test/anthropic"
        assert calls[1]["env"]["ANTHROPIC_BASE_URL"] == "https://api.vendor-d.test/anthropic"


# ---------------------------------------------------------------------------
# Observability contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSceneObservability:
    def test_selected_provider_log_carries_scene(self, fake_db, scene_chain):
        scene_chain["chain"] = ["Vendor A Pro"]
        logger = MagicMock()
        tool = ClaudeCodingTool(scene="execution", logger=logger)
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")
        logged = [c for c in logger.info.call_args_list
                  if c.args and c.args[0] == "provider_selected"]
        assert logged, "provider_selected must be logged"
        data = logged[0].kwargs.get("data") or logged[0].kwargs.get("data", {})
        assert data.get("scene") == "execution"
        assert data.get("provider") == "Vendor A Pro"


# ---------------------------------------------------------------------------
# Single source of truth (2026-09-14)
#
# The provider mapping (scene → tier → chain) resolved on every call is
# the ONLY authority for endpoint + credentials. The constructor's
# ``base_url`` / ``auth_token`` / ``api_key`` are stamped once at
# construction from the *run-level* provider — ``agent.autonomous_coding``
# resolves exactly one for the whole run via ``load_fallback_order()``,
# which is the flat optimizer chain and has no scene/tier awareness — and
# used to be applied unconditionally on top of the per-call selection.
#
# The failure mode: the ``execution`` scene correctly resolves ``Vendor A``
# (tier ``medium`` → ``^Vendor A``) and writes
# ``ANTHROPIC_API_KEY`` = Vendor A's key, then the run-level overrides
# replace the endpoint + bearer with Vendor C's. ``self.api_key`` is empty —
# the caller passes ``auth_token=`` but never ``api_key=`` — so the
# Vendor A key survives untouched. The subprocess therefore carries Vendor C's
# endpoint + Vendor C's bearer + Vendor A's key and every dispatch dies with
# ``API Error: 401``.
#
# The misleading part: ``provider_fallback`` logged
# ``vendor-a-pro failed (401)``, because that event reports
# ``current_call_provider`` (who the walk *chose*) rather than the
# endpoint the request actually reached.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProviderMappingIsSingleSourceOfTruth:
    @pytest.fixture(autouse=True)
    def _no_redaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Read the settings file as *written*, not as redacted.

        Redaction rewrites credentials in that file once the child is
        reaped; these tests assert the provider triple that was written,
        which is a separate contract (see ``test_secret_files.py``).
        """
        monkeypatch.setattr("coding_tool.redact_all", lambda *a, **k: 0)

    def test_run_level_credentials_do_not_override_scene_provider(
        self, fake_db, scene_chain
    ):
        """The selected provider's triple must survive to the subprocess env."""
        scene_chain["chain"] = ["Vendor A"]
        tool = ClaudeCodingTool(
            scene="execution",
            base_url="https://api.vendor-c.test/coding/",
            auth_token="sk-vendor-c-runlevel",
        )
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")

        env = calls[0]["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://api.vendor-a.test/anthropic"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-vendor-a-test"
        assert env["ANTHROPIC_API_KEY"] == "sk-vendor-a-test", (
            "the run-level base_url/auth_token must not re-point the "
            "endpoint at Vendor C while Vendor A's key stays in ANTHROPIC_API_KEY"
        )

    def test_settings_tmpfile_carries_single_provider_triple(
        self, tmp_path, fake_db, scene_chain
    ):
        """The ``--settings`` payload must be coherent too.

        ``claude --settings`` env outranks the process env, so this file —
        not ``env`` — is what the subprocess actually authenticates with.
        """
        base_settings = tmp_path / "base_settings.json"
        base_settings.write_text(
            json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "https://api.vendor-c.test/coding/",
                    "ANTHROPIC_AUTH_TOKEN": "sk-vendor-c-runlevel",
                    "ANTHROPIC_API_KEY": "sk-vendor-c-runlevel",
                }
            }),
            encoding="utf-8",
        )

        scene_chain["chain"] = ["Vendor A"]
        tool = ClaudeCodingTool(
            scene="execution",
            settings=base_settings,
            base_url="https://api.vendor-c.test/coding/",
            auth_token="sk-vendor-c-runlevel",
        )
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")

        cmd = calls[0]["cmd"]
        settings_path = Path(cmd[cmd.index("--settings") + 1])
        try:
            written = json.loads(settings_path.read_text(encoding="utf-8"))
        finally:
            settings_path.unlink(missing_ok=True)

        wenv = written["env"]
        assert wenv["ANTHROPIC_BASE_URL"] == "https://api.vendor-a.test/anthropic"
        assert wenv["ANTHROPIC_AUTH_TOKEN"] == "sk-vendor-a-test"
        assert wenv["ANTHROPIC_API_KEY"] == "sk-vendor-a-test", (
            "a cross-provider triple here is exactly the 401 shape: Vendor C "
            "endpoint + Vendor C bearer + Vendor A key"
        )
