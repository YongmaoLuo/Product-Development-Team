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
    SceneProviderUnavailable,
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
# End to end: an exhausted tier is WAITED OUT, not widened past
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


def _two_tier_tool(tmp_path, monkeypatch, tracker):
    """A scene on ``medium`` with a populated ``high`` tier sitting next to it.

    The second tier is declared and resolvable on purpose: it is the
    thing this class asserts is NOT used, and a test that proved it by
    removing the alternative would prove nothing.
    """
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
        ClaudeCodingTool, "_check_provider_availability",
        staticmethod(lambda name: (True, {
            "base_url": f"https://{name}.test/anthropic",
            "api_key": "sk-fake",
            "models": {"default": "m", "opus": "m",
                       "sonnet": "m", "haiku": "m"},
        })),
    )
    provider_routing.clear_routing_cache()
    return ClaudeCodingTool(scene="execution", concurrency_tracker=tracker)


@pytest.fixture
def served_sleeps(monkeypatch):
    """Record the wait instead of serving it, and release the parks on it.

    A park lasts minutes by design, so a test that served one would be
    measuring the suite's patience. Releasing the parks *when the wait is
    asked for* is also the honest shape: it says the wait ended because
    the tier came back, and lets the dispatch re-derive its candidates
    through the same filters rather than being handed an answer.
    """
    import coding_tool
    from dynamic_provider_concurrency import get_shared_cooldown

    recorded: list = []

    def _serve(seconds):
        recorded.append(seconds)
        get_shared_cooldown().clear()

    monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _serve)
    return recorded


@pytest.mark.unit
class TestAParkedTierIsWaitedOutInsteadOfWidened:
    """The case the widening walk used to cover, now covered by waiting.

    ``medium`` is two rows. Both answered 429, the walk found nothing
    left inside the tier, and the dispatch had one of two answers to
    give: fail the task, or serve it from a tier this scene was never
    put in. The second answer is what the escape hatch did and what this
    class now forbids. The first is what the park wait replaces: the
    tier comes back on its own schedule, so there is a third answer, and
    it is the correct one.
    """

    def test_a_fully_parked_tier_waits_and_then_dispatches_in_tier(
        self, tmp_path, monkeypatch, served_sleeps,
    ):
        """The headline case: every provider here parked, and the call
        still runs — on this tier, once the park expires."""
        _install_fake_claude(tmp_path, monkeypatch)
        tracker = ActiveConcurrencyTracker()
        tool = _two_tier_tool(tmp_path, monkeypatch, tracker)

        cd = get_shared_cooldown()
        cd.park("Vendor A Pro", seconds=600, reason="429")
        cd.park("Vendor A", seconds=600, reason="429")

        result, _session, _meta = tool._run_claude_interactive(
            "hi", idle_timeout=30, total_timeout=30,
        )

        assert result == "ok", (
            "a tier whose rows are all in cooldown has not failed — the "
            "call waits for it, which is the whole point of waiting"
        )
        assert served_sleeps, (
            "no wait was entered, so the dispatch reached an answer with "
            "nothing in its own tier to dispatch to"
        )
        assert tool.current_call_provider in ("Vendor A Pro", "Vendor A"), (
            f"the work went to {tool.current_call_provider!r}; the only "
            f"providers this scene approves are the two in its own tier"
        )

    def test_the_wait_watches_this_tier_and_never_reaches_the_next(
        self, tmp_path, monkeypatch, served_sleeps,
    ):
        """``high`` has a reachable, idle, correctly configured provider.

        It is never dispatched to, and never waited on — it is not in
        the chain, so it is not in the tier that came up empty. Named
        here rather than left implicit because the whole claim is that
        this specific reachable row is not an answer.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        tracker = ActiveConcurrencyTracker()
        tool = _two_tier_tool(tmp_path, monkeypatch, tracker)

        cd = get_shared_cooldown()
        cd.park("Vendor A Pro", seconds=600, reason="429")
        cd.park("Vendor A", seconds=600, reason="429")
        assert not cd.is_parked("Vendor D API"), (
            "the neighbouring tier's provider is reachable and idle: if "
            "this test ever passes by it being busy, it proves nothing"
        )

        tool._run_claude_interactive("hi", idle_timeout=30, total_timeout=30)

        assert tool.current_call_provider != "Vendor D API", (
            "the dispatch crossed into a tier this scene was not put in"
        )

    def test_the_wait_is_asked_for_the_park_and_not_the_quota_probe(
        self, tmp_path, monkeypatch,
    ):
        """Two clocks, kept apart.

        The step comes from the park's own remaining time; the quota
        reading is not consulted, because a park is a deadline this
        process wrote itself while a closed window belongs to somebody
        else's account. A wait that read the other one would be paying a
        network round-trip for a number it already holds exactly.
        """
        import coding_tool
        import provider_order

        reads: list = []
        monkeypatch.setattr(
            provider_order, "load_provider_quota_map",
            lambda *a, **k: reads.append(k.get("force")) or {},
        )
        _install_fake_claude(tmp_path, monkeypatch)

        asked: list = []
        cd = get_shared_cooldown()
        cd.park("Vendor A Pro", seconds=600, reason="429")
        cd.park("Vendor A", seconds=600, reason="429")

        def _sleep(seconds):
            asked.append(seconds)
            # What the reading had been asked for by the time this step
            # was taken. A probe here would be the two clocks wearing
            # each other's name.
            reads_when_waiting.append(list(reads))
            cd.clear()

        reads_when_waiting: list = []
        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _sleep)
        tool = _two_tier_tool(tmp_path, monkeypatch, ActiveConcurrencyTracker())

        tool._run_claude_interactive("hi", idle_timeout=30, total_timeout=30)

        assert asked, "no wait was entered at all"
        assert all(0 < s <= coding_tool.PARK_WAKE_MAX_STEP_SEC for s in asked), (
            f"the step must be bounded and read off the park: {asked}"
        )
        assert reads_when_waiting == [[]], (
            f"the quota payload had been read {reads_when_waiting} times by "
            f"the time the park wait stepped; the park's own remaining time "
            f"is the answer, and reading the other one costs a round-trip "
            f"for a number the process already holds"
        )

    def test_a_tier_with_room_dispatches_without_waiting(
        self, tmp_path, monkeypatch, served_sleeps,
    ):
        """Reverse control: the wait is not entered on the healthy path."""
        _install_fake_claude(tmp_path, monkeypatch)
        tracker = ActiveConcurrencyTracker()
        tool = _two_tier_tool(tmp_path, monkeypatch, tracker)

        tool._run_claude_interactive("hi", idle_timeout=30, total_timeout=30)

        assert served_sleeps == [], (
            f"a tier with a free pool waited {served_sleeps}; the wait "
            f"belongs to the 'nothing here can serve' answer only"
        )
        assert tool.current_call_provider == "Vendor A Pro"

    def test_a_saturated_pool_is_queued_for_and_then_dispatches(
        self, tmp_path, monkeypatch,
    ):
        """Reverse control #2, and it is now a queue rather than a fail.

        **Contract change, not a relaxation.** This test used to assert
        ``served_sleeps == []`` and a ``SceneProviderUnavailable`` whose
        ``reason`` was ``"no_capacity"``, on the reasoning that a held
        slot comes back in seconds and the poller had already spent the
        whole wait on exactly that. That reasoning confused a lease with
        a deadline: a quota window is somebody else's meter on their
        clock and a park is a timestamp this process wrote, but a slot is
        a counter in this process, released when the agent holding it
        finishes. It has a timeline of its own, so waiting on it waits
        for something genuinely on its way — and the call that failed
        here was a call that could have run.

        So both rows are filled, one of them hands a slot back, and the
        call must come out the other side having dispatched rather than
        raised. The chain assertion is kept verbatim: queueing is not a
        licence to leave the tier.
        """
        import coding_tool

        _install_fake_claude(tmp_path, monkeypatch)
        tracker = ActiveConcurrencyTracker()
        tool = _two_tier_tool(tmp_path, monkeypatch, tracker)
        # Every medium provider at its dynamic cap (10 at 100% quota).
        for _ in range(10):
            tracker.try_acquire("Vendor A Pro", 10)
        for _ in range(10):
            tracker.try_acquire("Vendor A", 10)

        queued: list = []

        def _serve_slot(seconds):
            """Record the queue step and hand a slot back on the third.

            Serving rather than waiting is what keeps the test off the
            wall clock, and the release is the premise of the whole
            contract: a slot is on its way back, so waiting is not
            waiting on a wall that never moves.
            """
            queued.append(seconds)
            if len(queued) >= 3:
                tracker.release("Vendor A")

        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _serve_slot)
        tool.provider_slot_wait_sec = 0.0

        result, _session, _meta = tool._run_claude_interactive(
            "hi", idle_timeout=30, total_timeout=30,
        )

        assert result == "ok", (
            "a saturated pool is not a failed call: the slot was on its "
            "way back and the dispatch waited for it"
        )
        assert queued == [
            coding_tool._slot_wake_step_sec(i) for i in range(3)
        ], (
            f"the queue asked for {queued}; the steps have to come from "
            f"the queue's own clock"
        )
        assert tool.current_call_provider in ("Vendor A Pro", "Vendor A"), (
            f"the work went to {tool.current_call_provider!r}; a scene's "
            f"chain is its own tier and nothing else, queueing included"
        )
        assert tool.current_call_provider == "Vendor A", (
            "the row that handed a slot back is the row that served it"
        )

    def test_an_excluded_tier_still_fails_closed_immediately(
        self, tmp_path, monkeypatch, served_sleeps,
    ):
        """Reverse control #3: exclusion is an instruction, not a clock.

        The caller has said which providers this call may not use. No
        park is in play and none will lift, so there is nothing to wait
        for; the answer stays the fail-closed one.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        tracker = ActiveConcurrencyTracker()
        tool = _two_tier_tool(tmp_path, monkeypatch, tracker)

        with pytest.raises(SceneProviderUnavailable) as excinfo:
            tool._run_claude_interactive(
                "hi", idle_timeout=30, total_timeout=30,
                excluded_providers=["Vendor A Pro", "Vendor A"],
            )

        assert excinfo.value.reason == "all_candidates_skipped"
        assert served_sleeps == [], (
            "an exclusion is not a deadline; the wait must not spin on it"
        )

    def test_the_step_is_bounded_so_a_short_park_cannot_become_a_spin(
        self, tmp_path, monkeypatch,
    ):
        """The floor, stated on its own.

        A park with a couple of seconds left must still cost the floor,
        and a park with none left at all — which is what the walk sees
        between reading ``is_parked`` and reading ``remaining`` — must
        not cost zero, or the wait re-derives the same empty answer as
        fast as the clock allows.
        """
        import coding_tool
        from dynamic_provider_concurrency import ProviderCooldown

        cd = ProviderCooldown()
        cd.park("Vendor A Pro", seconds=0.2, reason="429")

        step = coding_tool._park_wake_step_sec(cd, ["Vendor A Pro"])

        assert step >= coding_tool.PARK_WAKE_MIN_STEP_SEC, step
        assert coding_tool._park_wake_step_sec(cd, ["Vendor A"]) == (
            coding_tool.PARK_WAKE_MIN_STEP_SEC
        ), "no park left at all must still cost the floor, not a spin"

    def test_the_step_reads_the_soonest_park_and_is_capped(
        self, tmp_path, monkeypatch,
    ):
        """The ceiling, and what it reads when the tier disagrees with itself."""
        import coding_tool
        from dynamic_provider_concurrency import ProviderCooldown

        cd = ProviderCooldown()
        cd.park("Vendor A Pro", seconds=1800.0, reason="quota")
        cd.park("Vendor A", seconds=1800.0, reason="quota")

        step = coding_tool._park_wake_step_sec(cd, ["Vendor A Pro", "Vendor A"])
        assert step == coding_tool.PARK_WAKE_MAX_STEP_SEC, (
            "a half-hour park must not become one uninterruptible sleep"
        )
