"""End to end: a closed window, a real sleep, and a dispatch that happens.

Why this file exists
--------------------
Every other test of the wake replaces the wait with a recorder, which is
the only way to assert "how long did it sleep" without the suite sitting
there for half an hour. That substitution is also the one thing that
could hide the defect this file exists to catch.

The path under test has four moving parts that no unit test holds
together: the HTTP read of CC Switch's usage endpoint, its TTL cache,
the wall-clock conversion of a ``resets_at`` stamp, and the sleep that
sits between them. A wake shorter than the cache TTL is the case where
those four disagree — the reading is fetched before the sleep and served
again after it, so the wake wakes to the verdict it already had, finds
its own stamp now in the past, and stops. Nothing about that is visible
from either side in isolation; it only appears when a real server is
reached over a real socket and a real clock passes.

So this module does the one thing the unit tests cannot: it points
``PDT_PROVIDER_ORDER_API_URL`` at a server started here, hands it a
window that closes in a fixed number of seconds, and lets the dispatch
sleep through a real wake before it dispatches.

What the wait is, and what it is not
------------------------------------
The wait is the *probe cadence* and nothing else. A closed window
belongs to somebody else's account and reopens on no schedule this
process was told about, so the code does not count down to the
``resets_at`` it was handed — it looks again on a fixed beat, five
minutes apart in production. The window this file serves is therefore
not the length of the wait: it is the moment the reading flips from
"empty" to "funded", and the wait is one beat of the clock the code
keeps. A test that waited out the window instead would be testing a
countdown the production code does not have.

Production's beat is the figure, and it is five minutes — longer than
the suite's own per-test timeout, so a test that waited the production
figure would be killed rather than reported. The beat is therefore
shrunk here rather than in the code: the probe reads
``dynamic_provider_concurrency.DEFAULT_COOLDOWN_SEC`` at call time, so
replacing that module attribute moves the real production path onto
this file's figure without a line of it changing. The smaller beat is
the same beat — the sleep is the code's own, and the read that follows
it is the code's own forced read.

Two constraints keep the shrunken beat faithful rather than a
different behaviour:

  * it is *shorter* than the quota cache TTL (60s), so the read after
    the sleep still has a warm cache entry to bypass — the case this
    file exists to cover;
  * it is *longer* than the window the fake switch serves, so one beat
    carries past the window's reopening and the read that follows it
    sees the window open.

The CC Switch here is a stand-in started on an ephemeral port for the
life of the module. Nothing in this file reads or writes the real
switch's state, and the endpoint it serves is the usage read alone —
the dispatch path issues no other request to it.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
import provider_order  # noqa: E402
import provider_routing  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402
from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    ProviderCooldown,
    set_shared_cooldown,
)

# Module-level, so it covers every case here rather than one decorator.
#
# Why ``e2e`` and not ``time_sensitive``. This file sleeps through a real
# wake and asserts on the elapsed time, so it is honest to call it
# time-sensitive — but the marker is a *lane selector*, not a description,
# and ``e2e-on-demand`` runs with ``-m "e2e and not time_sensitive"``. Both
# markers would put the case in a lane; the two lanes are disjoint, so it can
# only ever be in one, and ``time_sensitive`` would take it out of the e2e
# job entirely — the file would then run nowhere except the serial lane,
# losing the real-socket dispatch coverage that is the reason it exists.
# ``e2e`` puts it in the lane whose budget (``--timeout=600``) fits the
# wait this file takes, and the e2e job does not run beside the unit shards
# either (``needs: integration-tests``), so it keeps the machine it needs.
#
# What it was doing unmarked is the part worth recording: with no marker at
# all, the gating lanes' ``-m "not e2e and not integration and not
# time_sensitive and not perf"`` could not exclude it. The unit shard ran it
# under ``--timeout=60`` — shorter than the wait it was about to take, plus
# the interpreter start around it — so a case that had never been given a
# lane was, by default, collected by the fastest and least forgiving one.
pytestmark = pytest.mark.e2e

#: How long the served window stays closed. It is a fixture and not a
#: contract: the property under test has no duration in it, and a stamp
#: ten minutes out and a stamp ten seconds out travel identical code.
#: What it has to be is *shorter* than the probe beat below, so that one
#: beat carries past the moment the reading flips and the read that
#: follows it sees the window open — and comfortably *longer* than the
#: setup between the server starting and the first read reaching it, so
#: the first read is the one that finds the window shut.
WINDOW_SEC = 8.0

#: The beat this file drives the wake at. Production's is
#: ``dynamic_provider_concurrency.DEFAULT_COOLDOWN_SEC`` (five minutes),
#: which is longer than the suite's per-test timeout, so the figure is
#: replaced for the length of this test — see the module docstring for
#: why that is the same beat and not another behaviour.
#:
#: It is shorter than the quota cache TTL (60s), so the read after the
#: sleep still has a warm cache entry to bypass, and longer than
#: :data:`WINDOW_SEC`, so one beat carries past the window's reopening.
PROBE_SEC = 10.0

CHAIN = ["Vendor A Pro", "Vendor A", "Vendor A API"]

_POOL_CAPS = [
    ("^vendor-a-pro$", 10),
    ("^vendor a$", 10),
    ("^vendor-a-api$", 10),
]

OK_SCRIPT = "#!/bin/sh\necho '{\"type\":\"result\",\"result\":\"ok\"}'\n"


# ---------------------------------------------------------------------------
# A CC Switch stand-in: one endpoint, one window, and a clock of its own
# ---------------------------------------------------------------------------


class _Usage:
    """Serves ``/api/usage`` with one closed window that reopens on time.

    The reading is derived from the wall clock at request time rather
    than flipped by the test, so the dispatch sees what CC Switch would
    genuinely have served at that moment: still full before the stamp,
    funded after it. A payload the test toggled would let the two clocks
    drift apart and the assertion below would stop meaning what it says.
    """

    def __init__(self, resets_at: datetime):
        self.resets_at = resets_at
        self.requests = 0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    @property
    def base_url(self) -> str:
        return "127.0.0.1:{0}".format(self._server.server_address[1])

    def open_now(self) -> bool:
        return datetime.now(self.resets_at.tzinfo) >= self.resets_at

    def payload(self) -> dict:
        used = 4.0 if self.open_now() else 100.0
        rows = []
        for name in CHAIN:
            rows.append({
                "provider": name,
                "app_type": "claude",
                "tiers": [
                    {
                        "name": "five_hour",
                        "used_percent": used,
                        "remaining": round(100.0 - used, 2),
                        "total": 100.0,
                        "unit": "%",
                        "resets_at": self.resets_at.isoformat(),
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
            })
        return {"providers": rows}

    def start(self) -> "_Usage":
        usage = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's name
                with usage._lock:
                    usage.requests += 1
                body = json.dumps(usage.payload()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                """Silence the default stderr access log."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(
            target=self._server.serve_forever, daemon=True,
        ).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _install_fake_claude(tmp_path, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(OK_SCRIPT, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


@pytest.fixture
def fleet(monkeypatch, capacity_config):
    """A three-pool scene whose every candidate is served by the switch."""
    capacity_config(_POOL_CAPS, default=10)
    monkeypatch.setattr(
        provider_routing, "resolve_provider_chain",
        lambda scene, *a, **k: list(CHAIN),
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability",
        staticmethod(lambda name: (True, {
            "base_url": "https://api.vendor-a.example/anthropic",
            "api_key": "sk-fake",
            "models": {"default": "m", "opus": "m", "sonnet": "m", "haiku": "m"},
        })),
    )
    monkeypatch.setattr(
        provider_order, "load_provider_5h_usage", lambda *a, **k: {}
    )
    set_shared_cooldown(ProviderCooldown())
    yield
    set_shared_cooldown(None)


@pytest.fixture
def quota_cache():
    """The reader keeps its own state; a warm cache would hide the read."""
    provider_order.cache_clear()
    yield
    provider_order.cache_clear()


# ---------------------------------------------------------------------------
# The case
# ---------------------------------------------------------------------------


def test_a_closed_window_is_slept_through_and_then_dispatched(
    tmp_path, monkeypatch, fleet, quota_cache,
):
    """Nothing dispatches, the wait is real, and afterwards it does.

    The assertions are one argument in four parts. The dispatch could
    not happen first because the reading says every window is empty.
    The elapsed time could not be short because the wait is a whole
    probe beat — the sleep is the code's own, and the reader is
    reached over a socket rather than stubbed, so a run that came back
    inside a fraction of the beat did not sleep through it. It could
    not have been many beats either, because one beat carries past the
    window's reopening. And the dispatch could not have happened
    without a fresh reading, because the reading that reported the
    window shut was taken before the sleep, and the beat is shorter
    than the cache TTL that would otherwise have served that same
    reading again.
    """
    # The two numbers this file drives are a premise, not a detail, and
    # they are checked before anything is started so a violation costs a
    # second instead of a whole sleep.
    #
    #   * the beat is shorter than the quota cache TTL, so the read
    #     after the sleep still has a warm cache entry to bypass — the
    #     disagreement between the reading taken before the sleep and
    #     the one the wake needs is the case this file exists for;
    #   * the window is shorter than the beat, so one beat carries past
    #     the window's reopening and the read that follows it sees the
    #     window open.
    #
    # Both are relationships to *other* code's constants, so nothing else
    # would notice them drifting: a TTL that dropped to or below the beat
    # would leave this file passing while covering nothing, and a window
    # longer than the beat would leave it waiting through rounds the
    # assertions below do not expect.
    assert WINDOW_SEC < PROBE_SEC, (
        f"the window ({WINDOW_SEC:.0f}s) has to be shorter than the beat "
        f"({PROBE_SEC:.0f}s), or the read that follows the sleep would "
        f"still find the window shut"
    )
    assert PROBE_SEC < provider_order._QUOTA_TTL_SEC, (
        f"the beat ({PROBE_SEC:.0f}s) has to be shorter than the quota "
        f"cache TTL ({provider_order._QUOTA_TTL_SEC:.0f}s), or the read "
        f"that follows the sleep would miss the cache for the wrong "
        f"reason and this file would stop covering the case it exists for"
    )

    _install_fake_claude(tmp_path, monkeypatch)

    # The production beat is five minutes, which is longer than the
    # suite's own per-test timeout, so the figure is replaced for the
    # length of this test rather than in the code. The probe reads
    # ``dynamic_provider_concurrency.DEFAULT_COOLDOWN_SEC`` out of the
    # module at call time, so this moves the real production path onto
    # the figure below — the same sleep, the same forced re-read, a
    # beat the suite can afford.
    monkeypatch.setattr(
        "dynamic_provider_concurrency.DEFAULT_COOLDOWN_SEC", PROBE_SEC
    )

    usage = _Usage(
        datetime.now(timezone.utc) + timedelta(seconds=WINDOW_SEC)
    ).start()
    try:
        monkeypatch.setenv(
            "PDT_PROVIDER_ORDER_API_URL", usage.base_url
        )
        tracker = ActiveConcurrencyTracker()
        tool = ClaudeCodingTool(
            scene="verification", concurrency_tracker=tracker,
        )
        tool.provider_slot_wait_sec = 0.2

        started = time.monotonic()
        result, _session, _meta = tool._run_claude_interactive(
            "hi", idle_timeout=30, total_timeout=30,
        )
        elapsed = time.monotonic() - started
    finally:
        usage.stop()

    assert result == "ok"
    assert tool.current_call_provider in CHAIN, (
        "the dispatch has to come from the ordinary selection once the "
        "window is open, not from a shortcut around it"
    )
    assert elapsed >= PROBE_SEC * 0.9, (
        f"the wait was {elapsed:.1f}s for a probe beat of "
        f"{PROBE_SEC:.0f}s: the wake did not sleep through the beat it "
        f"was given"
    )
    assert elapsed < PROBE_SEC * 3, (
        f"the wait was {elapsed:.1f}s for a probe beat of "
        f"{PROBE_SEC:.0f}s: the wake should have dispatched after a "
        f"single beat, because the window reopens inside it"
    )
    assert usage.requests >= 2, (
        f"{usage.requests} request(s) reached the usage endpoint; the wake "
        f"has to ask again, because the reading that said 'full' was "
        f"taken before the sleep and the cache TTL is longer than the beat"
    )
    assert tracker.total() == 0, "the slot the dispatch took came back"
