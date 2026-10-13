"""The verification dispatch must be visible in the plan's own execution log.

What this file pins
-------------------
Every provider-routing event is emitted under ``if self.logger:``, and
``create_coding_tool`` takes its logger as an argument. The three
verification call sites never passed one, so ``ClaudeCodingTool.logger``
was ``None`` for every dispatch the verification path made -- and the
whole set of routing events went with it. Five of them:

  * ``provider_scene_slot_wait`` -- a saturated tier, queued for
  * ``provider_scene_park_wait`` -- a tier this process parked
  * ``provider_scene_all_pools_exhausted``
  * ``provider_quota_window_wait`` -- a closed quota window, being probed
  * ``provider_selected`` -- the one carrying ``in_flight`` /
    ``fleet_in_flight``, i.e. the only place that ever said how busy the
    fleet actually was

The queue itself was never broken by that: it waits correctly. What was
gone is the record. A verification round whose verification points were
all asleep in the provider queue wrote nothing at all, which is
indistinguishable from a round that had stopped.

So the assertion here is deliberately not "the call site passes a
logger". Every event below is produced by a **real dispatch** through the
real ``ClaudeCodingTool`` and is read back out of the **real sink** -- the
plan's ``execution.log``, the same file ``agent.py`` and
``VerificationAgent`` already write -- so a logger that was passed but
never written to, or a sink somewhere else, would fail here.

The waits themselves are the slow part (minutes), so each one goes
through ``_quota_wake_sleep``, the seam the dispatch already uses for all
three waits; the durations are recorded rather than served. No network is
reached: the provider chain and the quota payload are supplied by the
test.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
import provider_order  # noqa: E402
import provider_routing  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402
from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    ProviderCooldown,
    get_shared_cooldown,
    set_shared_cooldown,
)
from execution_logger import ExecutionLogger  # noqa: E402

# Read through ``server``, not ``verification_loop`` directly: the two
# import each other, and ``server`` is the side that finishes. The route
# under test reaches this name the same way, so the test and the
# production call site agree on which binding is live.
from server import _verification_event_logger  # noqa: E402

#: The three rows this scene's own tier names. Every test here either
#: fills them, parks them or shuts them; none of them may ever be
#: reached from outside the tier.
TIER = ["Vendor A Pro", "Vendor A", "Vendor B"]
NEIGHBOUR = "Vendor D API"
ALL_ROWS = TIER + [NEIGHBOUR]

CAPS = [
    ("^vendor-a-pro$", 10),
    ("^vendor a$", 10),
    ("^vendor b$", 10),
    ("^vendor d", 10),
]

OK_SCRIPT = "#!/bin/sh\necho '{\"type\":\"result\",\"result\":\"ok\"}'\n"

#: The five events this file exists for. Named here rather than inline so
#: that dropping one shows up as a diff rather than as an omission found
#: later.
ROUTING_EVENTS = (
    "provider_scene_slot_wait",
    "provider_scene_park_wait",
    "provider_scene_all_pools_exhausted",
    "provider_quota_window_wait",
    "provider_selected",
)


# ---------------------------------------------------------------------------
# The scene: a three-row ``verification`` tier with an idle neighbour
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_cooldown():
    """The park list is process-shared; every test gets its own."""
    set_shared_cooldown(ProviderCooldown())
    yield
    set_shared_cooldown(None)


@pytest.fixture(autouse=True)
def _capacity(capacity_config):
    capacity_config(CAPS, default=10)


@pytest.fixture
def routing(tmp_path, monkeypatch):
    cfg = tmp_path / "provider_routing.yaml"
    cfg.write_text(textwrap.dedent("""
        version: 1
        default_tier: medium
        tiers:
          medium: ["^Vendor A", "^Vendor B"]
          high:   ["^Vendor D"]
        scenes:
          verification: medium
    """), encoding="utf-8")

    monkeypatch.setattr(
        provider_routing, "_config_path", lambda config_path=None: cfg,
    )
    monkeypatch.setattr(
        provider_routing, "_list_display_names",
        lambda db_path=None: list(ALL_ROWS),
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
    yield cfg
    provider_routing.clear_routing_cache()


def _install_fake_claude(tmp_path, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(OK_SCRIPT, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""),
    )


# ---------------------------------------------------------------------------
# Building the tool exactly the way the verification call sites do
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path):
    """A plan directory, standing in for the one the loop is running."""
    d = tmp_path / "plans" / "vp012-events"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def verification_tool(plan_dir):
    """``create_coding_tool`` called the way the three verification sites call it.

    Same factory, same ``scene``, and the same ``logger`` argument they now
    pass -- so a wiring change that took the logger away again would break
    this fixture too, rather than leaving the dispatch tests green against
    a tool nothing had configured.

    The tracker is the one thing the factory does not take: it shares a
    process-wide in-flight counter, and a test that has to *fill* a pool
    needs its own so the fill does not leak into the next one.
    """
    def _make(tracker=None, **kw):
        tool = coding_tool.create_coding_tool(
            None,
            cwd=str(plan_dir),
            scene="verification",
            logger=_verification_event_logger(plan_dir),
            **kw,
        )
        if tracker is not None:
            tool._concurrency_tracker = tracker
        return tool
    return _make


def _log_path(plan_dir: Path) -> Path:
    return plan_dir / "execution.log"


def _entries(plan_dir: Path):
    path = _log_path(plan_dir)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _events_on_disk(plan_dir: Path):
    return {entry.get("event") for entry in _entries(plan_dir)}


def _payloads(plan_dir: Path, event: str):
    return [e for e in _entries(plan_dir) if e.get("event") == event]


def _run(tool):
    return tool._run_claude_interactive(
        "hi", idle_timeout=30, total_timeout=30,
    )


# ---------------------------------------------------------------------------
# The quota reading for the closed-window case
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(timezone.utc)


def _window(provider, *, used, resets_in_minutes):
    stamp = _now() + timedelta(minutes=resets_in_minutes)
    return {
        "provider": provider,
        "app_type": "claude",
        "tiers": [
            {
                "name": "five_hour",
                "used_percent": used,
                "remaining": round(100.0 - used, 2),
                "total": 100.0,
                "unit": "%",
                "resets_at": stamp.isoformat(),
            },
            {
                "name": "weekly_limit",
                "used_percent": 0.0,
                "remaining": 100.0,
                "total": 100.0,
                "unit": "%",
                "resets_at": None,
            },
        ],
        "success": True,
    }


def _reading(*rows):
    return provider_order.parse_usage_payload(
        {"providers": list(rows)}, now=_now(),
    )


# ---------------------------------------------------------------------------
# The headline: all five events reach the plan's execution log
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTheVerificationDispatchIsRecorded:
    def test_all_five_provider_events_reach_the_plan_execution_log(
        self, tmp_path, monkeypatch, routing, plan_dir, verification_tool,
    ):
        """Every routing event, produced by real dispatches, read off disk.

        Three dispatches against one plan, one per kind of "nothing to
        dispatch to": a saturated tier (slot queue plus the exhausted
        warning), a parked tier, and a closed quota window. Each ends by
        taking a slot and selecting a provider, so ``provider_selected`` is
        covered three times over.

        The read is from the file, not from a recording double, so an event
        emitted to nowhere -- or to a sink the call sites do not use --
        cannot pass.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        served = []
        monkeypatch.setattr(provider_order, "load_provider_5h_usage",
                            lambda *a, **k: {})

        # (1) A saturated tier: queued for, warned about, then served.
        tracker = ActiveConcurrencyTracker()
        for name in TIER:
            for _ in range(10):
                assert tracker.try_acquire(name, 10), name

        def _sleep_then_free(seconds):
            served.append(seconds)
            tracker.release(TIER[0])

        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _sleep_then_free)
        tool = verification_tool(tracker)
        tool.provider_slot_wait_sec = 0.0
        result, _session, _meta = _run(tool)
        assert result == "ok"

        # (2) A parked tier: this process wrote the park, so it is waited
        # on its own clock, and no slot queue is entered at all.
        cooldown = get_shared_cooldown()
        for name in TIER:
            cooldown.park(name, seconds=600, reason="429")

        def _sleep_then_unpark(seconds):
            served.append(seconds)
            cooldown.clear()

        monkeypatch.setattr(coding_tool, "_quota_wake_sleep",
                            _sleep_then_unpark)
        tool = verification_tool(ActiveConcurrencyTracker())
        tool.provider_slot_wait_sec = 0.0
        result, _session, _meta = _run(tool)
        assert result == "ok"

        # (3) A closed quota window: probed, re-read, then served.
        shut = [_window(n, used=100.0, resets_in_minutes=12) for n in TIER]
        open_ = [_window(n, used=4.0, resets_in_minutes=180) for n in TIER]
        readings = [_reading(*shut), _reading(*open_)]
        calls = []

        def _load(*_a, **_k):
            calls.append(1)
            return readings[min(len(calls) - 1, len(readings) - 1)]

        monkeypatch.setattr(provider_order, "load_provider_quota_map", _load)
        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", served.append)
        tool = verification_tool(ActiveConcurrencyTracker())
        tool.provider_slot_wait_sec = 0.0
        result, _session, _meta = _run(tool)
        assert result == "ok"

        seen = _events_on_disk(plan_dir)
        missing = [name for name in ROUTING_EVENTS if name not in seen]
        assert not missing, (
            f"these routing events never reached {plan_dir.name}/execution.log: "
            f"{missing}. The log holds {sorted(x for x in seen if x)}."
        )

    def test_provider_selected_carries_the_fleet_depth(
        self, tmp_path, monkeypatch, routing, plan_dir, verification_tool,
    ):
        """``provider_selected`` is the only event that says how busy the fleet was.

        ``in_flight`` and ``fleet_in_flight`` are the two numbers an
        operator needs to tell "everything is running" from "everything is
        asleep in a queue", and they exist nowhere else. If this one event
        were what stayed dropped, the round would be as unreadable as it was
        before the logger was threaded through -- so the fields are
        asserted here, not the event name alone.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        monkeypatch.setattr(provider_order, "load_provider_5h_usage",
                            lambda *a, **k: {})
        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", lambda _s: None)

        tool = verification_tool(ActiveConcurrencyTracker())
        tool.provider_slot_wait_sec = 0.0
        result, _session, _meta = _run(tool)

        assert result == "ok"
        selected = _payloads(plan_dir, "provider_selected")
        assert selected, (
            "no provider_selected reached the plan's execution log, so the "
            "fleet-depth fields have nowhere to live"
        )
        data = selected[-1].get("data") or {}
        assert data.get("in_flight") == 1, data
        assert data.get("fleet_in_flight") == 1, data

    def test_the_sink_is_the_plan_execution_log(
        self, tmp_path, monkeypatch, routing, plan_dir, verification_tool,
    ):
        """Not a second sink: the file the rest of the plan already writes.

        An operator debugging a stalled round reads one file. A routing
        event that landed somewhere else -- a new handler, a second
        directory -- would be a thing to go and find, which is how the queue
        went unobserved to begin with.
        """
        _install_fake_claude(tmp_path, monkeypatch)
        monkeypatch.setattr(provider_order, "load_provider_5h_usage",
                            lambda *a, **k: {})
        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", lambda _s: None)

        tool = verification_tool(ActiveConcurrencyTracker())
        tool.provider_slot_wait_sec = 0.0
        _run(tool)

        log = tool.logger
        assert isinstance(log, ExecutionLogger)
        assert log.plan_id == plan_dir.name
        assert Path(log._log_file) == _log_path(plan_dir), (
            f"the verification logger wrote to {log._log_file}; the plan's "
            f"own execution log is {_log_path(plan_dir)}"
        )
        # And the reader the API and the card use can see it.
        assert ExecutionLogger.read_logs(plan_dir.name), (
            "the plan's execution log reads back empty through the reader "
            "the product actually queries"
        )


# ---------------------------------------------------------------------------
# The call site itself: a real request, not an inspected signature
# ---------------------------------------------------------------------------


class _InlineThread:
    """A worker thread that runs its body now.

    The handler builds the round's coding tool inside the worker body, so a
    thread stand-in that never runs would assert nothing about the call site.
    This one runs it on the calling thread and keeps ``start`` / ``join`` /
    ``args`` / ``kwargs`` shaped like the real thing.
    """

    def __init__(self, target=None, daemon=None, *args, **kwargs):
        self._target = target
        self._args = kwargs.pop("args", ())
        self._kwargs = kwargs.pop("kwargs", {})

    def start(self):
        self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        return None


@pytest.mark.integration
class TestTheStartRouteWiresTheLogger:
    def test_starting_a_round_hands_the_dispatch_a_live_plan_logger(
        self, tmp_path, monkeypatch, sample_plan_factory, plan_sqlite_seeder,
    ):
        """The route reaches the call site, and the call site passes a logger.

        Driven through the real handler on the real request shape, with the
        worker collapsed into the calling thread so the body actually runs.
        What is asserted is the ``ClaudeCodingTool`` that reached the
        orchestrator -- the object the round will really dispatch through --
        and not a source line: a call site that kept the old signature would
        leave this test failing, while every dispatch test above would
        still pass against their own fixture.
        """
        import server as _server
        from fastapi.testclient import TestClient

        from server import app

        created = []
        real_factory = _server.create_coding_tool

        def _record(*args, **kwargs):
            tool = real_factory(*args, **kwargs)
            created.append(tool)
            return tool

        built = []

        class _Orchestrator:
            def __init__(self, plan_dir_, project_dir_, **kw):
                self.plan_dir = plan_dir_
                self.project_dir = project_dir_
                built.append(kw.get("coding_tool"))

            def start_verification_cycle(self, **kw):
                # The round body is not what this test is about; returning
                # a shape the handler copes with is enough, and the handler
                # guards the rest.
                return {"results": []}

        monkeypatch.setattr(_server, "create_coding_tool", _record)
        monkeypatch.setattr(_server.threading, "Thread", _InlineThread)
        monkeypatch.setattr(_server, "VerificationOrchestrator", _Orchestrator)

        plan = sample_plan_factory("vp012-events", phase="executing")
        project_dir = plan / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (plan / "execution.json").write_text(
            json.dumps({"project_dir": str(project_dir)}), encoding="utf-8",
        )
        plan_sqlite_seeder(plan, phase="executing", project_dir=project_dir)

        resp = TestClient(app).post(
            "/api/verification/vp012-events/start",
            json={"max_rounds": 1, "auto_fix": False},
        )

        assert resp.status_code == 200, resp.text
        assert built, (
            "the orchestrator was never built -- the handler body did not run"
        )
        assert created, (
            "no coding tool was built -- the call site was never reached"
        )
        assert built[0] is created[0], (
            "the orchestrator was handed a different tool than the one the "
            "factory built"
        )

        tool = built[0]
        assert isinstance(tool.logger, ExecutionLogger), (
            f"the tool this round dispatches through has logger="
            f"{tool.logger!r}, and every provider-routing event is emitted "
            f"under `if self.logger:` -- so a None here is a round that "
            f"writes nothing while it waits"
        )
        assert tool.logger.plan_id == "vp012-events"
        assert Path(tool.logger._log_file) == plan / "execution.log"
