"""Provider quota exhaustion must rotate, not kill the task.

2026-09-22, a production plan
--------------------------------------------------
Vendor A Pro answered ``429 · 已达到 Token Plan 用量上限`` on twelve
separate dispatches. Two things were wrong with how the backend handled it:

  1. **The task died.** ``agent._execute_task_with_retry`` treated every
     ``ApiError`` as "retrying will not help" and returned ``False``
     immediately — no second attempt. The task was marked failed on the
     first refusal; everything depending on it was then permanently
     deferred, and the run ended at "No schedulable micro-layer found"
     with the rest of the plan still pending.

  2. **The rotation had nowhere to go.** ``provider_routing.yaml``
     declares ``medium: ["^Vendor A"]`` — two CC Switch rows. Once both
     answered 429 the dispatch fell through to the *parent process env*,
     which is not a tier member at all but did end the rotation: when it
     also failed, ``except ApiError`` saw ``failed_provider == "parent"``
     and re-raised. Reaching the end of one tier is not the same as
     having nowhere left to go — the chain should keep descending
     through the remaining tiers before it gives up.

  3. **Nothing remembered the refusal**, so every later dispatch
     re-selected the same exhausted provider and paid another failed
     round-trip (~3 min each).

What these tests pin: quota errors are classified as provider-level, the
exhausted provider is parked so the next dispatch skips it, the candidate
walk crosses into the next tier once its own is exhausted, and the agent
retries the attempt instead of failing the task.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from coding_tool import (  # noqa: E402
    ApiError,
    _park_provider_after_error,
    is_capacity_error,
)
from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    ProviderCooldown,
    acquire_provider_with_dynamic_capacity,
    get_shared_cooldown,
    looks_like_quota_exhaustion,
    set_shared_cooldown,
)


@pytest.fixture(autouse=True)
def _fresh_cooldown():
    """Every test gets its own park list; the module default is shared."""
    set_shared_cooldown(ProviderCooldown())
    yield
    set_shared_cooldown(None)


@pytest.fixture(autouse=True)
def _capacity(capacity_config):
    """Ten per pool, so "fill it" is ``range(10)`` throughout."""
    capacity_config([
        ("^vendor-a-pro$", 10),
        ("^vendor a$", 10),
        ("^vendor d", 10),
    ])


# ---------------------------------------------------------------------------
# Classification — "provider is out of budget" vs "the request is wrong"
# ---------------------------------------------------------------------------


class TestCapacityClassification:
    def test_429_is_a_capacity_error(self):
        assert is_capacity_error(ApiError("slow down", status="429"))

    def test_402_and_gateway_overload_are_capacity_errors(self):
        for status in ("402", "503", "529"):
            assert is_capacity_error(ApiError("x", status=status)), status

    def test_the_actual_0921_message_is_a_capacity_error(self):
        """The vendor text arrives verbatim from CC Switch, and the
        status the CLI reports for it is not guaranteed to be a clean
        ``429`` — the message itself has to be enough."""
        exc = ApiError(
            "[429] [API_ERROR:429] API Error: Request rejected (429) · "
            "已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。"
            " (2056)",
            status="unknown",
        )
        assert is_capacity_error(exc)

    def test_an_auth_failure_is_not_a_capacity_error(self):
        """Retrying a bad credential on another provider is pointless —
        this must keep the old hard-fail behaviour."""
        assert not is_capacity_error(ApiError("invalid api key", status="401"))
        assert not is_capacity_error(ApiError("bad request", status="400"))

    def test_quota_markers_are_specific(self):
        assert looks_like_quota_exhaustion("已达到 Token Plan 用量上限")
        assert looks_like_quota_exhaustion("You exceeded your current quota")
        assert not looks_like_quota_exhaustion("rate limit exceeded, retry later")
        assert not looks_like_quota_exhaustion("")


# ---------------------------------------------------------------------------
# Cooldown bookkeeping
# ---------------------------------------------------------------------------


class TestProviderCooldown:
    def test_park_then_is_parked(self):
        cd = ProviderCooldown()
        assert not cd.is_parked("Vendor A Pro")
        cd.park("Vendor A Pro", seconds=60, reason="429")
        assert cd.is_parked("Vendor A Pro")
        assert cd.remaining("Vendor A Pro") > 0

    def test_spellings_share_one_park(self):
        """The scene path spells "Vendor A Pro"; the executor path
        spells "vendor-a-pro". One exhausted quota pool, one park."""
        cd = ProviderCooldown()
        cd.park("Vendor A Pro", seconds=60)
        assert cd.is_parked("vendor-a-pro")

    def test_a_zero_or_negative_park_is_a_no_op(self):
        cd = ProviderCooldown()
        cd.park("X", seconds=0)
        cd.park("Y", seconds=-5)
        assert not cd.is_parked("X")
        assert not cd.is_parked("Y")

    def test_re_parking_extends_but_never_shortens(self):
        cd = ProviderCooldown()
        cd.park("X", seconds=600)
        cd.park("X", seconds=10)
        assert cd.remaining("X") > 60, "a shorter re-park must not shorten it"
        cd.park("X", seconds=900)
        assert cd.remaining("X") > 600

    def test_clear_releases_a_single_provider_or_all(self):
        cd = ProviderCooldown()
        cd.park("X", seconds=60)
        cd.park("Y", seconds=60)
        cd.clear("X")
        assert not cd.is_parked("X")
        assert cd.is_parked("Y")
        cd.clear()
        assert not cd.is_parked("Y")

    def test_snapshot_reports_remaining(self):
        cd = ProviderCooldown()
        cd.park("X", seconds=60)
        snap = cd.snapshot()
        assert "x" in snap and snap["x"] > 0


class TestParkingAfterAnError:
    def test_a_quota_error_parks_the_provider(self):
        exc = ApiError("已达到 Token Plan 用量上限", status="429")
        seconds = _park_provider_after_error("Vendor A Pro", exc)
        assert seconds > 0
        assert get_shared_cooldown().is_parked("Vendor A Pro")
        assert "用量上限" in get_shared_cooldown().reason("Vendor A Pro")

    def test_a_non_capacity_error_parks_nothing(self):
        assert _park_provider_after_error(
            "Vendor A Pro", ApiError("bad key", status="401"),
        ) == 0.0
        assert not get_shared_cooldown().is_parked("Vendor A Pro")

    def test_the_park_duration_is_env_overridable(self, monkeypatch):
        monkeypatch.setenv("PDT_PROVIDER_QUOTA_COOLDOWN_SEC", "42")
        exc = ApiError("quota exceeded", status="429")
        assert _park_provider_after_error("X", exc) == 42.0

    def test_a_bad_env_value_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("PDT_PROVIDER_COOLDOWN_SEC", "not-a-number")
        exc = ApiError("slow down", status="429")
        assert _park_provider_after_error("X", exc) > 0


# ---------------------------------------------------------------------------
# The walk skips parked providers
# ---------------------------------------------------------------------------


class TestSlotAcquisitionSkipsParked:
    def test_a_parked_provider_is_not_selected(self):
        """Without this the very next dispatch re-selects the provider
        that just answered 429 — twelve times, in the 0921 run."""
        tracker = ActiveConcurrencyTracker()
        set_shared_cooldown(ProviderCooldown())
        get_shared_cooldown().park("Vendor A Pro", seconds=600)

        chosen = acquire_provider_with_dynamic_capacity(
            ["Vendor A Pro", "Vendor A"],
            tracker,
            {},
            timeout=0.1,
            poll_interval=0.01,
        )

        assert chosen == "Vendor A"

    def test_every_provider_parked_returns_none(self):
        """``None`` is the caller's signal to fall back — it must not
        block for the whole timeout waiting on a provider that cannot
        serve us either way."""
        tracker = ActiveConcurrencyTracker()
        set_shared_cooldown(ProviderCooldown())
        get_shared_cooldown().park("Vendor A Pro", seconds=600)
        get_shared_cooldown().park("Vendor A", seconds=600)

        chosen = acquire_provider_with_dynamic_capacity(
            ["Vendor A Pro", "Vendor A"],
            tracker,
            {},
            timeout=5.0,
            poll_interval=0.01,
        )

        assert chosen is None

    def test_an_unparked_provider_still_wins_normally(self):
        tracker = ActiveConcurrencyTracker()
        chosen = acquire_provider_with_dynamic_capacity(
            ["Vendor A Pro", "Vendor A"], tracker, {},
            timeout=0.1, poll_interval=0.01,
        )
        assert chosen == "Vendor A Pro"


# ---------------------------------------------------------------------------
# The executor retries the ATTEMPT instead of failing the TASK
# ---------------------------------------------------------------------------


class _LogRecorder:
    def __init__(self) -> None:
        self.events = []

    def _record(self, level):
        def _fn(event, message, **kwargs):
            self.events.append({
                "level": level.upper(), "event": event,
                "message": message, "data": kwargs.get("data"),
            })
        return _fn

    def __getattr__(self, name):
        if name in ("info", "warning", "error", "debug", "critical"):
            return self._record(name)
        raise AttributeError(name)

    def names(self):
        return [e["event"] for e in self.events]


class _TaskStub:
    def __init__(self, tid="14-2"):
        self.id = tid
        self.title = f"task {tid}"
        self.description = "d"
        self.test_command = "pytest -q"
        self.model_type = "medium"
        self.project_dir = None
        self.files_to_modify = []
        self.verification_only = False


def _retry_agent(logger):
    """A minimal agent carrying only what ``_execute_task_with_retry``
    touches before and around the coding-tool call."""
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.logger = logger
    agent.project_dir = Path("/tmp")
    agent._task_progress_repo = None
    agent._session_task_completed_counts = {}

    agent.task_manager = type("TM", (), {
        "update_task_status": lambda *a, **k: None,
        "record_task_failure": lambda *a, **k: None,
    })()
    agent.retry_manager = type("RM", (), {
        "get_retry_prompt_modifier": lambda *a, **k: "",
        "record_attempt": lambda *a, **k: None,
    })()
    agent.executor = type("EX", (), {
        "had_previous_timeout": lambda *a, **k: False,
        "record_timeout": lambda *a, **k: None,
    })()
    agent.config = type("CFG", (), {"executor_system_prompt": "sys"})()
    agent._build_files_to_modify_hint = lambda task: ""
    agent._build_prior_failure_block = lambda task: ""
    agent._clean_test_command = lambda cmd: cmd
    agent._validate_test_command_shape = lambda *a, **k: None
    agent._preflight_test_command_skip = lambda *a, **k: False
    agent._looks_like_audit_task = lambda *a, **k: False
    return agent


class TestCapacityErrorRetriesTheAttempt:
    def test_a_capacity_error_does_not_fail_the_task_on_the_first_hit(self):
        """0921: task 14-2 was marked failed on its very first 429, which
        permanently deferred its two downstream tasks."""
        logger = _LogRecorder()
        agent = _retry_agent(logger)
        task = _TaskStub()
        calls = []

        def _query(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise ApiError("已达到 Token Plan 用量上限", status="429")
            # Second attempt gets a non-capacity error so the loop ends
            # deterministically.
            raise ApiError("invalid api key", status="401")

        agent.coding_tool = type("CT", (), {"query": staticmethod(_query)})()

        result = agent._execute_task_with_retry(task, max_retries=2)

        assert len(calls) == 2, (
            "a 429 must consume a retry, not the task — the first refusal "
            "left the task dead and stranded its dependents"
        )
        assert result is False
        assert "task_api_error_provider_retry" in logger.names()
        assert "task_api_error" in logger.names(), (
            "the second (non-capacity) failure must still be reported as "
            "the hard-fail it is"
        )

    def test_a_non_capacity_error_still_fails_immediately(self):
        """Auth failures must keep the old behaviour — rotating a bad
        credential to another provider cannot help."""
        logger = _LogRecorder()
        agent = _retry_agent(logger)
        task = _TaskStub()
        calls = []

        def _query(*a, **k):
            calls.append(1)
            raise ApiError("invalid api key", status="401")

        agent.coding_tool = type("CT", (), {"query": staticmethod(_query)})()

        result = agent._execute_task_with_retry(task, max_retries=2)

        assert result is False
        assert len(calls) == 1
        assert "task_api_error_provider_retry" not in logger.names()

    def test_the_last_attempt_reports_the_capacity_error(self):
        """When the retries are spent the task does fail — and the log
        must say why, so an operator can tell it apart from a bad task."""
        logger = _LogRecorder()
        agent = _retry_agent(logger)
        task = _TaskStub()
        calls = []

        def _query(*a, **k):
            calls.append(1)
            raise ApiError("quota exceeded", status="429")

        agent.coding_tool = type("CT", (), {"query": staticmethod(_query)})()

        result = agent._execute_task_with_retry(task, max_retries=2)

        assert result is False
        assert len(calls) == 2
        assert "task_api_error" in logger.names()


# ---------------------------------------------------------------------------
# End to end: an exhausted tier widens instead of dropping to the parent
# ---------------------------------------------------------------------------


_OK_SCRIPT = "#!/bin/sh\necho '{\"type\":\"result\",\"result\":\"ok\"}'\n"


def _install_fake_claude(tmp_path, monkeypatch):
    import os
    import stat

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(_OK_SCRIPT, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""),
    )


class TestExhaustedTierWidensInsteadOfDroppingToParent:
    """The 0921 shape, end to end.

    ``medium`` is ``["^Vendor A"]`` in the shipped config — two rows. Both
    answered 429, the walk found nothing left, and the dispatch fell to
    the parent process env (``provider_parent_fallback``), which is not a
    tier member at all. When that failed too, the task died. It must
    instead reach the next tier's providers.
    """

    def _tool_with_two_tiers(self, tmp_path, monkeypatch, tracker):
        import textwrap

        import provider_routing
        from coding_tool import ClaudeCodingTool

        cfg = tmp_path / "provider_routing.yaml"
        cfg.write_text(textwrap.dedent("""
            version: 1
            default_tier: medium
            tiers:
              medium: ["^Vendor A"]
              high:   ["^Vendor D"]
            scenes:
              execution: medium
        """), encoding="utf-8")

        monkeypatch.setattr(
            provider_routing, "_config_path", lambda config_path=None: cfg,
        )
        monkeypatch.setattr(
            provider_routing, "_list_display_names",
            lambda db_path=None: ["Vendor A Pro", "Vendor A", "Vendor D API"],
        )
        monkeypatch.setattr(provider_routing, "_optimizer_order", lambda: [])
        monkeypatch.setattr(
            provider_routing, "resolve_provider_chain",
            lambda scene, *a, **k: ["Vendor A Pro", "Vendor A"],
        )
        monkeypatch.setattr(
            ClaudeCodingTool, "_check_provider_availability",
            staticmethod(lambda name: (True, {
                "base_url": f"https://{name}.test/anthropic",
                "api_key": "sk-fake",
                "models": {"default": "m", "opus": "m",
                           "sonnet": "m", "haiku": "m"},
            })),
        )
        provider_routing.clear_routing_cache()
        return ClaudeCodingTool(
            scene="execution", concurrency_tracker=tracker,
        )

    def test_both_tier_providers_parked_reaches_the_next_tier(
        self, tmp_path, monkeypatch,
    ):
        _install_fake_claude(tmp_path, monkeypatch)
        tracker = ActiveConcurrencyTracker()
        tool = self._tool_with_two_tiers(tmp_path, monkeypatch, tracker)

        cd = get_shared_cooldown()
        cd.park("Vendor A Pro", seconds=600, reason="429")
        cd.park("Vendor A", seconds=600, reason="429")

        tool._run_claude_interactive("hi", idle_timeout=30, total_timeout=30)

        assert tool.current_call_provider == "Vendor D API", (
            "both providers of the scene's own tier were exhausted; the "
            "walk must widen to the next tier, not drop to the parent "
            "process config (which is what killed task 14-2)"
        )

    def test_a_healthy_tier_is_not_widened(self, tmp_path, monkeypatch):
        """Only exhaustion widens. A reachable provider in the scene's
        own tier always wins, so the normal path stays on medium."""
        _install_fake_claude(tmp_path, monkeypatch)
        tracker = ActiveConcurrencyTracker()
        tool = self._tool_with_two_tiers(tmp_path, monkeypatch, tracker)

        tool._run_claude_interactive("hi", idle_timeout=30, total_timeout=30)

        assert tool.current_call_provider == "Vendor A Pro"

    def test_a_busy_tier_queues_rather_than_widening(
        self, tmp_path, monkeypatch,
    ):
        """A pool that is merely FULL must not spill into another tier —
        the 2026-09-17 rule is "degrade on the local semaphore", and
        widening on mere saturation would change which semaphores govern
        the fleet."""
        _install_fake_claude(tmp_path, monkeypatch)
        tracker = ActiveConcurrencyTracker()
        tool = self._tool_with_two_tiers(tmp_path, monkeypatch, tracker)
        # Every medium provider at its dynamic cap (10 at 100% quota).
        for _ in range(10):
            tracker.try_acquire("Vendor A Pro", 10)
        for _ in range(10):
            tracker.try_acquire("Vendor A", 10)

        tool.provider_slot_wait_sec = 0.2
        tool._run_claude_interactive("hi", idle_timeout=30, total_timeout=30)

        assert tool.current_call_provider != "Vendor D API", (
            "saturation must queue on the tier's own semaphore, not widen"
        )
