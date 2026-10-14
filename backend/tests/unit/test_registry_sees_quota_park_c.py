"""The heartbeat sweep must be able to tell a parked dispatch from a stall.

What this file pins
-------------------
``SubAgentHandle.waiting_on_quota_window`` answers one question of every
handle the heartbeat sweep holds: *is this tool asleep on somebody
else's quota meter?* It answers it from ``scoped_tool._work_clock`` and
the clock's ``is_parked``. A parked dispatch is waiting, not working --
it has no subprocess to kill and no stdout to read -- so a sweep that
treated it as stale would spend the kill path on nothing and then drop
the handle, which is the only thing protecting the process that really
does start once the window reopens.

The rebuild agent is the case where that question used to have no
answer. :meth:`BinaryRebuildAgent.run` registers its handle with
``scoped_tool=<the coding tool it dispatches on>`` and then calls
``query_json(..., timeout=TIMEOUT_SECONDS)``, but it never hands a
``WorkClock`` down: the clock belongs to the verification sub-agent's
own budget, and a rebuild dispatched outside one arrives with nothing.
So ``_run_claude_interactive`` -- the only thing in the process that
parks ``self._work_clock`` -- had nothing to park, and
``waiting_on_quota_window()`` could only ever answer "no".

The wiring that fixes it is ``_dispatch_work_clock``: a dispatch with
no clock adopts one for the length of the call and puts it back
afterwards. That is a change in what the registry can see, so it needs
a test that reads the registry from the **production entry point**
rather than from a handle dressed with a clock by hand.

Every test here drives ``BinaryRebuildAgent.run`` -- the same call
``attempt_intelligent_rebuild`` makes for its slow path -- with a real
``SubAgentRegistry``, a real ``ClaudeCodingTool`` and the real
closed-window wake. The only substitutions are the quota reader (a
queue of readings), the wait itself (``coding_tool._quota_wake_sleep``,
the module-level seam the wait is documented to take) and the temp
settings file the agent writes before dispatching. Nothing here reaches
the network and no test spawns a real CLI: a ``claude`` shell script on
PATH answers the one dispatch that is eventually admitted.

The gate the wait is held on matters as much as the wait: the dispatch
has to stay parked while the assertions are made, or "it was parked" is
a claim about a moment nobody can look at.
"""
from __future__ import annotations

import os
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
import provider_order  # noqa: E402
import provider_routing  # noqa: E402
import quota_watchdog  # noqa: E402
from binary_freshness import FreshnessReport  # noqa: E402
from binary_rebuild_agent import BinaryRebuildAgent  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402
from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    ProviderCooldown,
    set_shared_cooldown,
)
from sub_agent_registry import SubAgentRegistry  # noqa: E402

PLAN = "rebuild-park-plan"
VP = "VP-1"

CHAIN = ["Vendor A Pro", "Vendor A", "Vendor A API"]

#: Ten per pool, so "the reading closed the window" is ``round_half_up(10
#: * 0.04) == 0`` and the width is unambiguously the reading's doing.
_POOL_CAPS = [
    ("^vendor-a-pro$", 10),
    ("^vendor a$", 10),
    ("^vendor-a-api$", 10),
]

#: The JSON the fake CLI answers with. ``query_json`` parses the ``result``
#: field as a document in its own right.
REBUILD_REPLY = '{"type":"result","result":"{\\"rebuilt\\": true}"}'

#: The registry's staleness threshold is loaded from the environment at
#: import time; the shape the production sweep actually runs is a handle
#: whose ``last_progress_ts`` has aged past it, so the tests below reach
#: that shape by ageing the stamp rather than by passing a threshold that
#: cannot expire.
STALE_THRESHOLD_SEC = 1200


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(timezone.utc)


def _window(provider, *, used, resets_at):
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
                "resets_at": None if resets_at is None else resets_at.isoformat(),
            },
        ],
        "success": True,
    }


def _reading(*rows):
    return provider_order.parse_usage_payload(
        {"providers": list(rows)}, now=_now()
    )


def _starved(provider):
    """Nearly spent: not closed, but nothing left to hand out.

    The width the budget scales has run down to zero, which is the
    reading the closed-window wake is written for and the one that
    keeps a dispatch parked here.
    """
    return _window(provider, used=96.0, resets_at=_now() + timedelta(days=1))


def _starved_fleet():
    return _reading(*[_starved(name) for name in CHAIN])


def _open(provider):
    return _window(provider, used=4.0, resets_at=_now() + timedelta(hours=3))


def _fleet_open():
    return _reading(*[_open(name) for name in CHAIN])


# ---------------------------------------------------------------------------
# The wait seam -- held open so the parked moment can be looked at
# ---------------------------------------------------------------------------


class _Wake:
    """``coding_tool._quota_wake_sleep``, held at the first park.

    ``parked`` is raised when the dispatch asks for a probe and stays
    raised until the test releases it, which is what makes the parked
    moment observable at all: without a gate the dispatch is asleep for
    as long as this test takes to look and the window for an assertion
    is a scheduling accident.

    ``served`` records what the wall clock was actually charged, and the
    sleep after the release is real. The thing under test is a wall
    clock, so a fabricated one would prove nothing.
    """

    def __init__(self):
        self.parked = threading.Event()
        self.release = threading.Event()
        self.asks: list = []
        self.served = 0.0
        self._gated = False

    def install(self, monkeypatch, readings, *, slice_sec: float = 0.2):
        queue = list(readings)
        reads = {"n": 0}
        wake = self

        def _load(*_a, **_kw):
            reads["n"] += 1
            return queue[min(reads["n"] - 1, len(queue) - 1)]

        monkeypatch.setattr(provider_order, "load_provider_quota_map", _load)
        monkeypatch.setattr(coding_tool, "_quota_wake_sleep", wake)
        return self

    def __call__(self, seconds: float) -> None:
        self.asks.append(seconds)
        if not self._gated:
            self._gated = True
            self.parked.set()
            self.release.wait(timeout=120)
        self.served += 0.2
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _NullSettingsBuilder:
    """``SettingsJsonBuilder`` stand-in that writes no temp file.

    The real builder writes the security hooks to a private temp dir;
    nothing here dispatches tool calls, so what the agent is really
    doing before it reaches ``query_json`` is not what is under test.
    """

    def __init__(self, vp_id=None, plan_id=None, **kwargs) -> None:
        pass

    def build(self):
        return None

    def cleanup(self) -> None:
        return None


def _install_fake_claude(tmp_path, monkeypatch) -> Path:
    """A ``claude`` on PATH that answers the one admitted dispatch."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    launches = tmp_path / "launches.log"
    launches.write_text("", encoding="utf-8")
    body = [
        "#!/bin/sh",
        "echo launched >> '%s'" % launches,
        "echo '%s'" % REBUILD_REPLY,
    ]
    fake.write_text("\n".join(body) + "\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", "")
    )
    return launches


def _launches(log_path: Path) -> int:
    return len(log_path.read_text(encoding="utf-8").split())


def _stale() -> FreshnessReport:
    return FreshnessReport(
        kind="rust",
        status="FAILED",
        detail="the artefact is older than its source",
        evidence="mtime comparison",
        rebuild_command="cargo build --release",
        binary_path=None,
    )


@pytest.fixture
def scene(monkeypatch):
    monkeypatch.setattr(
        provider_routing, "resolve_provider_chain",
        lambda scene, *a, **k: list(CHAIN),
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability",
        staticmethod(lambda name: (True, {
            "base_url": "https://api.vendor-a.example/anthropic",
            "api_key": "sk-fake",
            "models": {"default": "m", "opus": "m", "sonnet": "m",
                       "haiku": "m"},
        })),
    )
    monkeypatch.setattr(
        provider_order, "load_provider_5h_usage", lambda *a, **k: {}
    )


@pytest.fixture(autouse=True)
def _pools(capacity_config):
    capacity_config(_POOL_CAPS, default=10)


@pytest.fixture
def _fresh_cooldown():
    set_shared_cooldown(ProviderCooldown())
    yield
    set_shared_cooldown(None)


@pytest.fixture
def _fresh_watchdog():
    """A watchdog of this test's own, so the probe count is this test's."""
    previous = quota_watchdog.set_shared_quota_watchdog(quota_watchdog.QuotaWatchdog())
    yield
    quota_watchdog.set_shared_quota_watchdog(previous)


def _tool():
    tool = ClaudeCodingTool(
        scene="verification",
        concurrency_tracker=ActiveConcurrencyTracker(),
        logger=None,
    )
    tool.provider_slot_wait_sec = 0.2
    return tool


def _dispatch_in_background(agent, project, monkeypatch):
    """Run the agent on its own thread and hand back where its answer lands.

    The registry can only be read while the dispatch is inside it, and the
    dispatch does not return until the window reopens, so the observation
    and the call cannot share a thread. ``box`` is where the agent's own
    return value lands, so "it parked" is never confused with "it parked
    and then fell over".
    """
    monkeypatch.setattr(
        "verification_subagent.SettingsJsonBuilder", _NullSettingsBuilder,
    )
    box = {}

    def _run():
        try:
            box["result"] = agent.run(project, _stale())
        except BaseException as exc:  # pragma: no cover - surfaced below
            box["error"] = exc

    thread = threading.Thread(target=_run, name="rebuild-dispatch", daemon=True)
    thread.start()
    return thread, box


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class TestTheRegistrySeesTheRebuildDispatchPark:
    def test_a_parked_rebuild_handle_reads_as_waiting_not_stale(
        self, tmp_path, monkeypatch, scene, _fresh_cooldown, _fresh_watchdog,
    ):
        """A rebuild asleep on a closed window must survive the sweep.

        The whole chain is real here: ``BinaryRebuildAgent.run``
        registers the handle with its own coding tool, dispatches
        ``query_json`` with the agent's own timeout, the dispatch finds
        every provider in its scene unable to serve and parks on the
        closed-window wake, and the registry is read while it is still
        parked. Nothing sets ``_work_clock`` by hand -- the only thing
        that installs one is the dispatch wrapper on the path under test.
        """
        registry = SubAgentRegistry()
        launches = _install_fake_claude(tmp_path, monkeypatch)
        wake = _Wake().install(
            monkeypatch, [_starved_fleet(), _fleet_open()],
        )
        tool = _tool()

        assert getattr(tool, "_work_clock", None) is None, (
            "precondition: the rebuild agent hands no clock down, which is "
            "why the park used to be invisible from the registry"
        )

        agent = BinaryRebuildAgent(
            tool, plan_id=PLAN, vp_id=VP, registry=registry,
        )
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        thread, answer = _dispatch_in_background(agent, project, monkeypatch)
        try:
            assert wake.parked.wait(timeout=90), (
                "the dispatch never asked to probe a window, so it never "
                f"parked; readings asked for: {len(wake.asks)}"
            )

            handles = registry.all_handles(PLAN)
            assert len(handles) == 1, (
                f"the rebuild registered {len(handles)} handles, not the "
                "one ``run`` registers"
            )
            handle = handles[0]
            assert handle.scoped_tool is tool, (
                "the sweep can only ask a parked tool about its clock; "
                f"the handle points at {handle.scoped_tool!r}"
            )

            # The question itself.
            assert handle.waiting_on_quota_window() is True, (
                "the dispatch is parked on a closed window and the handle "
                "says it is not waiting -- the sweep will treat it as a "
                "stall"
            )

            # And the decision that question exists to change: a wait
            # long enough to cross the staleness threshold is the case
            # the sweep would otherwise act on.
            handle.last_progress_ts = time.time() - (STALE_THRESHOLD_SEC + 60)
            stale = registry.find_stale(STALE_THRESHOLD_SEC)
            assert stale == [], (
                "a handle parked on a quota window was handed to the kill "
                "path; the handle protecting the process that starts when "
                "the window reopens is the thing being dropped"
            )
        finally:
            wake.release.set()
            thread.join(timeout=120)

        assert not thread.is_alive(), (
            "the dispatch never came back; releasing the gate should let "
            "it dispatch to the reopened fleet"
        )
        assert "error" not in answer, (
            f"the rebuild raised out of ``run``: {answer.get('error')!r}"
        )
        assert answer.get("result") is not None, (
            "the rebuild returned nothing; the dispatch under test has to "
            "be one that finishes, not one that dies while parked"
        )
        assert wake.asks, "the closed-window wake was never entered"
        assert _launches(launches) == 1, (
            f"{_launches(launches)} dispatches for one answer"
        )
        assert registry.all_handles(PLAN) == [], (
            "the rebuild agent's own ``finally`` unregisters; a handle left "
            "behind would be asked the same question forever"
        )
        assert getattr(tool, "_work_clock", None) is None, (
            "the adopted clock outlived the dispatch that adopted it, so a "
            "finished dispatch would keep answering ``is_parked``"
        )
        # The same question, the other way round, on the same handle. A
        # sweep that skipped every handle forever would satisfy an
        # assertion that only ever saw "yes", so the answer has to go
        # back to "no" the moment the dispatch is over — and it can only
        # do that if the clock went back with it.
        assert handle.waiting_on_quota_window() is False, (
            "a dispatch that already came back still reads as parked, so "
            "the sweep will now protect a handle whose subprocess really "
            "can be killed"
        )
