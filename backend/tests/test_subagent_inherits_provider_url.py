"""Regression test: verification sub-agents must walk the provider_priority
fallback chain (CC Switch DB → shell env) and pick the first available
provider.

This is the CORRECTED bug 3 fix (2026-06-13 B1 verification
retrospective, second attempt).

The wrong fix (bdc99aa) inherited the parent's ``base_url`` /
``auth_token`` into the scoped tool. That masked the symptom
(because the parent happened to use the first-fallback) but
bypassed the priority chain entirely — a future priority change
(e.g. swapping the priority list order)
would NOT be reflected in sub-agents, because they were frozen
on the parent's resolved creds.

The correct fix has two parts:
  1. ``coding_tool._load_provider_from_db`` answers two different
     questions from two different sources. With a provider *name* it
     reads that CC Switch row and nothing else — a name with no row is
     unavailable, never "borrow whatever the shell env points at".
     With no name, the shell env IS the configuration, which is the
     no-CC-Switch path.
  2. The scoped tool no longer inherits parent's base_url/auth_token.
     It walks its own provider chain, like the parent does. So
     provider_priority changes propagate correctly.

This test pins both halves.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Resolve the backend directory relative to this test file so the
# import below always lands on the in-tree ``coding_tool`` /
# ``verification_subagent`` modules rather than a hard-coded sibling
# repo. Earlier versions of this test pinned
# ``a sibling checkout's backend`` on ``sys.path`` which silently
# shadowed the in-tree modules whenever the suite ran from the
# ``autonomous-coding`` checkout (every CI run, every developer
# machine).
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from coding_tool import ClaudeCodingTool  # noqa: E402


class FakeCodingTool:
    """Stand-in for the parent's ClaudeCodingTool."""

    def __init__(self, base_url=None, auth_token=None):
        # In real production, these come from
        # agent._load_provider_info. In the corrected fix, the
        # scoped tool does NOT inherit them.
        self.model = "m2.7"
        self.model_type = "complex"
        self.model_map = {"complex": {"vendor-a-pro": {"default": "m2.7"}}}
        self.provider_priority = ["vendor-a-pro", "vendor-b"]
        self.mcp_config = None
        self.logger = None
        self.settings = None
        self.base_url = base_url
        self.auth_token = auth_token


class TestSubagentWalksProviderChain(unittest.TestCase):
    """Sub-agent must walk the provider_priority chain itself."""

    def setUp(self):
        from verification_subagent import VerificationSubAgent
        self.sub = VerificationSubAgent(
            method="code_review",
            max_retries=1,
            model_complexity="complex",
        )

    def test_scoped_tool_does_not_inherit_parent_base_url(self):
        """Bug 3 correct fix: scoped tool's base_url is NOT inherited
        from the parent. The parent had vendor-a-pro creds but the
        scoped tool must still walk the full priority chain so a
        priority change (e.g. swapping order) takes effect.
        """
        # Construct the parent via ``__new__`` so we skip
        # ``ClaudeCodingTool.__init__``'s subprocess-bootstrap
        # bookkeeping, then set the attributes the SUT reads. The
        # resulting instance is a real ``ClaudeCodingTool`` (and
        # therefore passes the SUT's ``isinstance(_,
        # ClaudeCodingTool)`` guard) while having no real network
        # or subprocess handles attached. A ``MagicMock(spec=...)``
        # would also pass the isinstance check, but ``MagicMock``
        # synthesises a child mock for every spec attribute —
        # including ``query_json`` — which is why earlier
        # iterations of this test fed a MagicMock-returning
        # MagicMock into the verdict parser and crashed on
        # ``json.dumps``.
        parent = ClaudeCodingTool.__new__(ClaudeCodingTool)
        parent.base_url = "https://api.vendor-a.example/anthropic"
        parent.auth_token = "sk-cp-FAKE-vendor-a-pro"
        parent.model = "m2.7"
        parent.model_type = "complex"
        parent.model_map = {"complex": {"vendor-a-pro": {"default": "m2.7"}}}
        parent.provider_priority = ["vendor-a-pro", "vendor-b"]
        parent.mcp_config = None
        parent.logger = None
        parent.settings = None
        # Stub query_json directly on the instance so the SUT's
        # call returns a clean dict (the real method would try to
        # spawn the Claude CLI subprocess).
        parent.query_json = MagicMock(return_value={
            "verdict": "PASSED", "reasons": ["ok"], "evidence": [],
        })
        captured_kwargs = {}

        # Spy on the real ``ClaudeCodingTool.__init__``. Patching
        # ``coding_tool.ClaudeCodingTool`` itself with a subclass
        # (the old approach) breaks the SUT's
        # ``isinstance(coding_tool, ClaudeCodingTool)`` guard —
        # the SUT's local import picks up the subclass, but the
        # ``parent`` we pass in is a direct ``ClaudeCodingTool``
        # instance, so the isinstance check returns False and the
        # scoped tool is never created. Patching ``__init__`` on
        # the real class instead preserves the class identity (and
        # therefore the isinstance check) while letting us record
        # the constructor kwargs and stub ``query_json`` on the
        # returned instance.
        original_init = ClaudeCodingTool.__init__

        def _spy_init(self, **kwargs):
            captured_kwargs.update(kwargs)
            original_init(self, **kwargs)
            self.query_json = MagicMock(return_value={
                "verdict": "PASSED", "reasons": ["ok"], "evidence": [],
            })

        vp_node = {"id": "VP-001", "verification_method": "code_review"}
        with patch.object(ClaudeCodingTool, "__init__", _spy_init):
            import asyncio
            asyncio.run(self.sub._execute_attempt(
                vp_node=vp_node,
                coding_tool=parent,
                settings_path="/tmp/fake_settings.json",
                log_path=Path("/tmp/fake_log.json"),
                attempt=0,
            ))

        # CORRECT fix: NO base_url / auth_token inheritance
        self.assertIsNone(
            captured_kwargs.get("base_url"),
            "scoped tool should NOT inherit base_url — must walk the "
            "priority chain itself so priority changes propagate"
        )
        self.assertIsNone(
            captured_kwargs.get("auth_token"),
            "scoped tool should NOT inherit auth_token — must walk the "
            "priority chain itself so priority changes propagate"
        )
        # But the chain IS preserved (model_map, provider_priority, etc.)
        self.assertEqual(captured_kwargs.get("provider_priority"),
                         ["vendor-a-pro", "vendor-b"])
        self.assertEqual(captured_kwargs.get("model"), "m2.7")


class TestLoadProviderFromDbChain(unittest.TestCase):
    """_load_provider_from_db must consult CC Switch DB → env (first hit wins)."""

    def setUp(self):
        # Use a temp file path so tests don't pollute real
        # ~/.cc-switch. We monkey-patch Path.home() to return
        # a temp dir.
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ws-load-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_cc_switch_db(self, provider_name, base_url, api_key):
        """Create a temp cc-switch.db with one providers row."""
        db_path = self.tmpdir / ".cc-switch" / "cc-switch.db"
        db_path.parent.mkdir(parents=True)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE providers (name TEXT, settings_config TEXT)"
        )
        conn.execute(
            "INSERT INTO providers VALUES (?, ?)",
            (provider_name, json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": base_url,
                    "ANTHROPIC_AUTH_TOKEN": api_key,
                }
            })),
        )
        conn.commit()
        conn.close()
        return db_path

    def test_db_only_finds_db(self):
        """When only the cc-switch DB has the provider, it returns DB cfg."""
        # The argument IS the CC Switch row label — no intermediate
        # mapping, so the DB query matches on the name itself.
        self._make_cc_switch_db(
            "Vendor A Pro",
            "https://db-host.example/v1",
            "sk-from-db",
        )

        with patch("pathlib.Path.home", return_value=self.tmpdir):
            cfg = ClaudeCodingTool._load_provider_from_db("Vendor A Pro")
        self.assertEqual(cfg["base_url"], "https://db-host.example/v1")
        self.assertEqual(cfg["api_key"], "sk-from-db")

    def test_env_fallback_rejects_proxy_managed(self):
        """Shell env answers the nameless question, but PROXY MANAGED is
        rejected — it would silently route through cc-switch's
        currently-active provider, defeating the explicit ordering."""
        with patch.dict("os.environ", {
            "ANTHROPIC_BASE_URL": "https://parent-proxy.invalid/anthropic",
            "ANTHROPIC_AUTH_TOKEN": "PROXY MANAGED",
        }, clear=False):
            with patch("pathlib.Path.home", return_value=self.tmpdir):
                cfg = ClaudeCodingTool._load_provider_from_db()
        # PROXY MANAGED rejected → returns empty
        self.assertEqual(cfg, {})

    def test_a_named_lookup_never_borrows_the_shell_env(self):
        """The sentinel here is irrelevant: a name is never answered from env.

        Same environment as the test above, minus the sentinel — the
        result must still be empty, because the environment does not say
        which provider it belongs to.
        """
        with patch.dict("os.environ", {
            "ANTHROPIC_BASE_URL": "https://real-host.example/v1",
            "ANTHROPIC_AUTH_TOKEN": "sk-real-not-proxy",
        }, clear=False):
            with patch("pathlib.Path.home", return_value=self.tmpdir):
                cfg = ClaudeCodingTool._load_provider_from_db("Vendor A Pro")
        self.assertEqual(cfg, {})

    def test_env_fallback_accepts_real_value(self):
        """Shell env with REAL (non-sentinel) values is the answer when no
        provider name was asked for — the no-CC-Switch path."""
        with patch.dict("os.environ", {
            "ANTHROPIC_BASE_URL": "https://real-host.example/v1",
            "ANTHROPIC_AUTH_TOKEN": "sk-real-not-proxy",
        }, clear=False):
            with patch("pathlib.Path.home", return_value=self.tmpdir):
                cfg = ClaudeCodingTool._load_provider_from_db()
        self.assertEqual(cfg["base_url"], "https://real-host.example/v1")
        self.assertEqual(cfg["api_key"], "sk-real-not-proxy")

    def test_unknown_provider_returns_empty(self):
        """A provider name with no row returns an empty dict."""
        with patch("pathlib.Path.home", return_value=self.tmpdir):
            cfg = ClaudeCodingTool._load_provider_from_db("totally-unknown-provider")
        self.assertEqual(cfg, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
