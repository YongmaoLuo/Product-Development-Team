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
  * A provider missing from the DB is skipped (no crash).
  * While a scene is in effect its chain is the ALLOWLIST: if it selects
    nothing the call fails (:class:`SceneProviderUnavailable`) instead of
    falling through to the priority walk or the parent environment.
    Callers that pass no ``scene`` are unaffected — there is no allowlist
    to violate, so the walk and the parent fallback run as they always
    have.
  * ``query_json(scene=...)`` / ``query(scene=...)`` override the
    instance scene for that one call (refiner / audit / summarizer
    call-sites rely on this).

The Popen subprocess is mocked; the CC Switch DB lookups are monkey-
patched at the ``ClaudeCodingTool`` seam so no real DB or network is
touched.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool as coding_tool_module
import provider_routing
import usage_registry
from coding_tool import ClaudeCodingTool, SceneProviderUnavailable


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


def _capture_dispatch_env(monkeypatch) -> dict:
    """Capture the exact ``env`` dict the dispatch builds for its child.

    ``_run_claude_interactive`` starts with ``env = os.environ.copy()`` and
    every provider decision is then a mutation of that dict. The dict is
    local, so the only way to assert on it without spawning a subprocess
    is to intercept the copy itself. Wrapping rather than replacing means
    the dispatch behaves normally — this is a spy on a seam, not a stub.

    Returns a holder whose ``env`` key is the dict, or ``None`` if the
    dispatch never got that far.
    """
    holder: dict = {"env": None}
    real_copy = os.environ.copy

    def _spy_copy(*a, **k):
        copied = real_copy(*a, **k)
        holder["env"] = copied
        return copied

    monkeypatch.setattr(os.environ, "copy", _spy_copy, raising=False)
    return holder


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

    def test_empty_scene_chain_fails_closed_instead_of_using_the_priority_walk(
        self, fake_db, scene_chain, monkeypatch
    ):
        """Zero scene candidates → the call fails, it does not walk.

        The priority chain is the optimizer's list — the whole CC Switch
        set, with no tier filtering — so "rescue this scene from the
        priority walk" means "serve it from a provider the scene never
        approved". The chain is the scene's allowlist, and an allowlist
        that yields nothing is a configuration fact the caller needs to
        see, not a gap to paper over.
        """
        scene_chain["chain"] = []
        monkeypatch.setattr(
            ClaudeCodingTool, "_get_live_provider_priority", lambda *a, **k: ["Vendor B Pro"],
        )
        tool = ClaudeCodingTool(scene="execution")
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            with pytest.raises(SceneProviderUnavailable) as excinfo:
                tool._run_claude_interactive("hi")
        assert calls == [], (
            "no subprocess may be spawned: a provider outside the scene's "
            "tier table served the call"
        )
        assert excinfo.value.scene == "execution"
        assert excinfo.value.reason == "no_tier_match"

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


# ---------------------------------------------------------------------------
# Fail closed: a scene that approves nobody serves nobody
#
# The contract these pin is "the scene's tier table is an allowlist". While
# a scene is in effect the ONLY providers that may serve the call are the
# ones that chain returned. Everything else the dispatch could reach is
# outside the table:
#
#   * the ``provider_priority`` walk — the optimizer chain, i.e. the whole
#     CC Switch list with no tier filtering;
#   * ``ClaudeCodingTool(base_url=..., auth_token=...)`` — the run-level
#     provider, resolved once for the whole run;
#   * the parent process env — whatever the shell exported.
#
# Falling through to any of them made the dispatch succeed against a
# provider no tier named, while the usage ledger recorded the call as
# ``provider="parent"``: a label naming where the configuration came from,
# not who answered. The tier table then silently stopped being a
# constraint for every provider it happened not to cover.
#
# The fail-closed side has to be observable, not merely different: no
# subprocess, nothing written to disk, ``current_call_provider`` unset
# rather than mislabelled, and a ledger row that says "failed" instead of
# naming a provider. The last test is the one that would catch a
# regression to the old behaviour, because the old behaviour was
# invisible precisely in that column.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSceneFailsClosed:
    def test_empty_chain_does_not_stamp_the_run_level_provider(
        self, fake_db, scene_chain, monkeypatch
    ):
        """The run-level provider must not reach ``env``, and nothing spawns.

        ``env`` is read here rather than inferred: ``subprocess.Popen`` is
        not the only consumer of it (the settings-file write copies
        ``ANTHROPIC_*`` out of it too), so the test wraps
        ``os.environ.copy`` — the statement that produces ``env`` — and
        inspects the very dict the dispatch built. Both sinks are then
        closed explicitly below, because "the env was clean" is only
        meaningful if it was also never handed anywhere.
        """
        scene_chain["chain"] = []
        # A priority chain that WOULD have answered, if the walk were
        # still reachable from a scene-active call. It must not be.
        monkeypatch.setattr(
            ClaudeCodingTool, "_get_live_provider_priority",
            lambda *a, **k: ["Vendor B Pro"],
        )
        run_level_url = "https://run-level-provider.invalid/anthropic"
        run_level_key = "sk-run-level-key"
        tool = ClaudeCodingTool(
            scene="execution",
            base_url=run_level_url,
            auth_token=run_level_key,
        )

        built_env = _capture_dispatch_env(monkeypatch)
        written = []
        monkeypatch.setattr(
            coding_tool_module, "write_private_json",
            lambda path, payload: written.append((str(path), payload)),
        )
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            with pytest.raises(SceneProviderUnavailable) as excinfo:
                tool._run_claude_interactive("hi")

        assert calls == [], (
            "a subprocess was spawned for a scene whose chain approved "
            "nobody: the call was dispatched against an unapproved provider"
        )
        assert written == [], (
            f"settings file(s) written for an undispatched call: {written!r}"
        )
        env = built_env["env"]
        assert env is not None, "the dispatch never built its env dict"
        for key, leaked in (
            ("ANTHROPIC_BASE_URL", run_level_url),
            ("ANTHROPIC_AUTH_TOKEN", run_level_key),
            ("ANTHROPIC_API_KEY", run_level_key),
        ):
            assert leaked not in env.values(), (
                f"{key}={env.get(key)!r} reached the child env for a scene "
                f"call that approved nobody — this is the defect: the "
                f"run-level provider is not on any scene's allowlist"
            )
        assert excinfo.value.scene == "execution"

    def test_the_failure_names_the_scene_and_why_its_chain_was_empty(
        self, fake_db, scene_chain, monkeypatch
    ):
        """The message has to carry the diagnosis, not just "it failed".

        An operator reading a dead task cannot act on "provider selection
        failed". What they can act on is *which* scene, which tier, and
        which of the ways the chain came up empty it was.
        """
        scene_chain["chain"] = []
        tool = ClaudeCodingTool(scene="refiner")
        with pytest.raises(SceneProviderUnavailable) as excinfo:
            tool._run_claude_interactive("hi")

        exc = excinfo.value
        assert exc.scene == "refiner"
        assert exc.reason == "no_tier_match"
        text = str(exc)
        assert "refiner" in text, f"scene missing from the message: {text!r}"
        assert "tier" in text, f"tier missing from the message: {text!r}"
        assert exc.to_dict()["type"] == "scene_provider_unavailable"

    def test_a_filtered_out_chain_reports_that_rather_than_an_empty_tier(
        self, fake_db, scene_chain, monkeypatch
    ):
        """"The tier named rows and every one was skipped" is a different bug.

        Both end in "no provider", and they are fixed in different places:
        a tier that matches nothing is a routing-config problem, while a
        tier whose rows are all excluded or parked is an availability or
        cooldown problem. Collapsing them into one message sends the
        operator to the wrong file.
        """
        # Stub the chain resolver itself rather than the provider-chain
        # seam, so the chain this test reasons about is exactly the
        # chain it declared and nothing the machine running it happens to
        # have in CC Switch can leak in.
        monkeypatch.setattr(
            provider_routing, "resolve_provider_chain",
            lambda scene, *a, **k: ["Vendor A Pro", "Vendor A"],
        )
        tool = ClaudeCodingTool(scene="execution")
        with pytest.raises(SceneProviderUnavailable) as excinfo:
            # Excluding every candidate is the cheapest way to make the
            # chain resolvable-but-unusable without touching the fake DB.
            tool._run_claude_interactive(
                "hi", excluded_providers=["Vendor A Pro", "Vendor A"],
            )
        exc = excinfo.value
        assert exc.reason == "all_candidates_skipped", exc.reason
        assert exc.candidates == ["Vendor A Pro", "Vendor A"], exc.candidates
        assert "Vendor A Pro" in str(exc), (
            "the message should name what the chain offered, so the reader "
            "can tell an exclusion from a config gap"
        )

    def test_no_scene_keeps_the_run_level_fallback(self, fake_db, monkeypatch):
        """Reverse control: with no scene there is no allowlist to violate.

        Same run-level provider, same unresolvable priority chain, no
        scene — the call dispatches, and ``env`` carries the run-level
        endpoint and key. If this ever raises, the gate has grown past
        "a scene was in effect and approved nobody", and every caller that
        runs without a scene (which is a documented mode, not an edge
        case) has been broken.
        """
        monkeypatch.setattr(
            ClaudeCodingTool, "_get_live_provider_priority", lambda *a, **k: [],
        )
        run_level_url = "https://run-level-provider.invalid/anthropic"
        run_level_key = "sk-run-level-key"
        tool = ClaudeCodingTool(
            base_url=run_level_url,
            auth_token=run_level_key,
        )
        assert tool.scene is None

        built_env = _capture_dispatch_env(monkeypatch)
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")

        assert calls, "a scene-less call must still dispatch"
        assert built_env["env"]["ANTHROPIC_BASE_URL"] == run_level_url
        assert built_env["env"]["ANTHROPIC_AUTH_TOKEN"] == run_level_key
        assert tool.current_call_provider == "parent", (
            "without a scene the parent label is the documented outcome and "
            "must be unchanged"
        )

    def test_no_scene_and_no_fallback_leaves_env_to_claude_code(
        self, fake_db, monkeypatch
    ):
        """The other scene-less shape: nothing configured at all.

        ``--settings`` is omitted entirely and nothing ``ANTHROPIC_*`` is
        stamped, so the CLI resolves its own configuration. Unchanged by
        the fail-closed gate, and pinned because it is the shape inherit
        mode relies on.
        """
        monkeypatch.setattr(
            ClaudeCodingTool, "_get_live_provider_priority", lambda *a, **k: [],
        )
        tool = ClaudeCodingTool()

        built_env = _capture_dispatch_env(monkeypatch)
        ppatch, calls = _capture_popen_env_and_args()
        with ppatch:
            tool._run_claude_interactive("hi")

        assert calls, "the call must still dispatch"
        env = built_env["env"]
        # "Unchanged", not "absent": ``env`` starts as a copy of the parent
        # environment, and a shell that exports ANTHROPIC_* keys is a
        # supported deployment. What must not happen is the dispatch
        # *writing* to them — so each value must still be exactly what the
        # parent carried.
        for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_API_KEY"):
            assert env.get(key) == os.environ.get(key), (
                f"{key} was stamped onto a call with no provider and no "
                f"scene: {env.get(key)!r} (parent had "
                f"{os.environ.get(key)!r})"
            )
        assert "--settings" not in calls[0]["cmd"], (
            "with no template settings file the dispatch must not pass "
            f"--settings at all: {calls[0]['cmd']!r}"
        )
        assert tool.current_call_provider == "parent"

    def test_the_failure_is_not_recorded_as_parent_in_the_ledger(
        self, fake_db, scene_chain, monkeypatch, tmp_path
    ):
        """The ledger row must say the call failed, not name a provider.

        This is the assertion that pins the old defect. Recording
        ``provider="parent"`` is what made an unapproved dispatch
        indistinguishable from a legitimate parent-env dispatch in the
        usage stats — the two have different endpoints and different
        costs, and the column said they were the same thing. So: a real
        ``llm_sessions.jsonl`` is written (private plans dir, private tmp
        root) and no row in it may carry ``"parent"``.
        """
        monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))
        monkeypatch.setenv("PDT_SECRET_TEMP_ROOT", str(tmp_path / "scratch"))
        scene_chain["chain"] = []
        monkeypatch.setattr(
            ClaudeCodingTool, "_get_live_provider_priority",
            lambda *a, **k: ["Vendor B Pro"],
        )
        tool = ClaudeCodingTool(
            scene="execution",
            base_url="https://run-level-provider.invalid/anthropic",
            auth_token="sk-run-level-key",
        )

        ppatch, calls = _capture_popen_env_and_args()
        with ppatch, usage_registry.plan_usage_context(
            plan_id="ledger-scene-failclosed",
        ):
            with pytest.raises(SceneProviderUnavailable):
                tool.query_json("hi")

        assert calls == [], "the failed call must not have dispatched"
        rows = usage_registry.read_llm_calls("ledger-scene-failclosed")
        assert rows, (
            "the failure left no trace in llm_sessions.jsonl — an operator "
            "reading the ledger would not know the call happened"
        )
        for row in rows:
            assert row.get("provider") != "parent", (
                f"ledger row names parent for a call no provider served: "
                f"{row!r}"
            )
            assert row.get("ok") is False, f"ledger row claims success: {row!r}"
            assert row.get("scene") == "execution", (
                f"ledger row lost the scene, which is the one field that "
                f"identifies which allowlist was empty: {row!r}"
            )
            assert row.get("error") == "SceneProviderUnavailable", (
                f"ledger row does not name the failure type: {row!r}"
            )
