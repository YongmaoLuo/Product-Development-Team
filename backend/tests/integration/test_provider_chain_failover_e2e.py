"""The chain failover, end to end: one 429 storm, then the next provider.

What is pinned here
-------------------
The unit tests for this change each pin one rule: the breaker's state
machine, the breaker's wiring into the read loop, the exhaustion
verdict, the backoff sequence, the pre-dispatch quota check. Every one
of them holds on its own, and none of them reproduces the shape the
incident actually had:

    a three-provider chain, the FIRST provider answering nothing but
    429s for as long as it is allowed to, and the second and third
    providers never being tried at all.

This file reproduces that shape with a real child process and asserts
the three facts the incident's post-mortem asked for:

  * the dispatch moves to the SECOND provider and the answer comes from
    there (test_first_provider_429_storm_switches_within_90s);
  * the first provider's child leaves NO orphan behind
    (test_first_provider_child_is_reaped);
  * a chain that still has room is never declared exhausted
    (test_no_quota_wait_exhausted_when_chain_has_room).

Why the clock is compressed
---------------------------
The breaker's window is measured in seconds and the incident's window
was a minute; a test that has to sit through it is a test nobody runs.
``PDT_429_BREAKER_SEC`` / ``PDT_429_BREAKER_STREAK`` are the
operator's own knobs, so the test turns them down rather than
monkeypatching the state machine: what is exercised is the PRODUCTION
path, at a speed a test can afford. The 90-second bound is still the
incident's — the test would fail on a machine where the failover took
longer than the incident did.

Why the fake ``claude`` reads ``--settings``
--------------------------------------------
The provider a dispatch landed on is decided by the routing layer and
handed to the CLI through the ``--settings`` file's
``env.ANTHROPIC_BASE_URL`` — that is the only channel the CLI reads,
and ``--settings`` outranks the process environment. So the fake reads
the same file the real CLI would, and the test therefore proves the
routing layer wrote the right provider into the right place. A fake
that guessed from the process environment would pass even if the
settings file were never written at all.

Nothing here touches the network or CC Switch: the chain, the live
quota reading and the provider availability check are all stubbed, and
the only subprocess is the fake ``claude`` on ``PATH``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import coding_tool  # noqa: E402
import provider_order  # noqa: E402
import provider_routing  # noqa: E402
from capacity_wait import QuotaWaitExhausted  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402


#: The chain the scene routes to. Three members, so "the provider that
#: failed" and "the chain" cannot be mistaken for one another.
CHAIN = ["vendor-a-pro", "vendor-b-lite", "vendor-c"]

#: The provider that storms, and the one that must end up serving.
FIRST = CHAIN[0]
SECOND = CHAIN[1]

#: The endpoint each provider is given. The fake CLI reads the provider
#: name back out of the host, so the mapping has to be unambiguous.
BASE_URL_TEMPLATE = "https://{provider}.cc-switch.test/v1"

#: The operator's knobs, turned down so the case runs in seconds. The
#: window is what the streak has to span, and the streak is how many
#: consecutive errors are needed.
BREAKER_WINDOW_SEC = "0.2"
BREAKER_STREAK = "2"

#: The event the structured logger writes when the dispatch gives up on
#: a provider and moves to the next one.
FALLBACK_EVENT = "provider_fallback"

#: The event the breaker writes when it breaks a stalled stream.
BREAKER_EVENT = "provider_429_stream_breaker_tripped"

#: This file spawns a real child process and drives the real dispatch,
#: so it belongs to the integration lane (and to no model — the fake
#: CLI answers without a network).
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# The fake CLI
# ---------------------------------------------------------------------------

#: A ``claude`` that answers with nothing but rate limits, and then
#: stays alive — the incident's shape. It ignores stdin EOF, because
#: that is what the real CLI did while it was retrying the 429
#: internally: the stream was LOUD (every retry refreshed the idle
#: watchdog) and PRODUCTION-free.
#:
#: The provider it is pretending to be is read out of the ``--settings``
#: file the dispatch wrote — the same file the real CLI reads.
_FAKE_CLAUDE = """#!@PYTHON@
import json
import os
import sys
import time


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


def provider_from_settings(argv):
    \"\"\"Which provider this dispatch landed on, per the --settings file.\"\"\"
    if "--settings" not in argv:
        return "unknown"
    path = argv[argv.index("--settings") + 1]
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return "unknown"
    env = (data or {}).get("env") or {}
    base_url = env.get("ANTHROPIC_BASE_URL") or ""
    # https://vendor-a-pro.cc-switch.test/v1 -> vendor-a-pro
    host = base_url.split("//", 1)[-1].split("/", 1)[0]
    return host.split(".", 1)[0] or "unknown"


def main():
    provider = provider_from_settings(sys.argv[1:])

    # Record which provider this child is, and its own pid, so the test
    # can prove which dispatches happened and that the first one's child
    # is gone.
    pid_dir = os.environ.get("PDT_FAKE_CLAUDE_PID_DIR")
    if pid_dir:
        with open(os.path.join(pid_dir, provider + ".pid"), "w",
                  encoding="utf-8") as handle:
            handle.write(str(os.getpid()))

    storm = set(
        (os.environ.get("PDT_FAKE_CLAUDE_STORM_PROVIDERS") or "").split(",")
    )
    if provider in storm:
        while True:
            emit({
                "type": "system",
                "subtype": "api_error",
                "error": {"status": 429},
            })
            time.sleep(0.35)

    emit({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "done"}]},
    })
    emit({
        "type": "result",
        "subtype": "success",
        "result": "done",
        "is_error": False,
    })


main()
"""


class _RecordingLogger:
    """A structured logger that keeps what it was told."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def _record(self, level: str, event: str, message: str, **kwargs) -> None:
        self.events.append(
            {
                "level": level,
                "event": event,
                "message": message,
                "data": kwargs.get("data"),
            }
        )

    def debug(self, event, message, **kwargs):
        self._record("debug", event, message, **kwargs)

    def info(self, event, message, **kwargs):
        self._record("info", event, message, **kwargs)

    def warning(self, event, message, **kwargs):
        self._record("warning", event, message, **kwargs)

    def error(self, event, message, **kwargs):
        self._record("error", event, message, **kwargs)

    def critical(self, event, message, **kwargs):
        self._record("critical", event, message, **kwargs)

    def named(self, event: str) -> list[dict]:
        return [e for e in self.events if e["event"] == event]


# ---------------------------------------------------------------------------
# The fixture: a chain, a stubbed fleet and a fake CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def failover(tmp_path, monkeypatch):
    """A three-provider chain where the first one storms 429s.

    Everything the dispatch would otherwise reach out for is stubbed:
    the routing table answers with :data:`CHAIN`, the live quota
    reading is empty (so every provider stays dispatchable), and the
    availability check answers from a local table. The only thing that
    really runs is the dispatch itself and the fake CLI it spawns.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_claude = bindir / "claude"
    fake_claude.write_text(
        _FAKE_CLAUDE.replace("@PYTHON@", sys.executable), encoding="utf-8",
    )
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", "")
    )

    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    monkeypatch.setenv("PDT_FAKE_CLAUDE_PID_DIR", str(pid_dir))
    monkeypatch.setenv("PDT_FAKE_CLAUDE_STORM_PROVIDERS", FIRST)

    # The breaker's own knobs, turned down to make the case run in
    # seconds instead of minutes.
    monkeypatch.setenv("PDT_429_BREAKER_SEC", BREAKER_WINDOW_SEC)
    monkeypatch.setenv("PDT_429_BREAKER_STREAK", BREAKER_STREAK)
    # The scene the breaker is armed for is the default; say so out
    # loud, so a change to the default shows up here as a failure
    # rather than as a mystery.
    monkeypatch.setenv("PDT_429_BREAKER_SCENES", "execution")

    # The graceful shutdown waits this long for the child to notice
    # stdin EOF. The fake CLI ignores stdin EOF (that is the incident),
    # so without a shorter patience every case would spend two minutes
    # in the escalation.
    monkeypatch.setattr(
        ClaudeCodingTool, "GRACEFUL_STOP_TIMEOUT", 2,
    )

    # The routing table: the scene routes to these three, in this order.
    def _fake_chain(scene, *args, **kwargs):
        return list(CHAIN)

    monkeypatch.setattr(
        provider_routing, "resolve_provider_chain", _fake_chain,
    )

    # The live reading: empty. No provider is metered, so no provider
    # is out of budget, and every member of the chain stays
    # dispatchable.
    def _fake_quota_map(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        provider_order, "load_provider_quota_map", _fake_quota_map,
    )

    def _fake_usage_map(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        provider_order, "load_provider_5h_usage", _fake_usage_map,
    )

    # The availability check: every member of the chain is reachable and
    # correctly configured, each with its own endpoint.
    def _fake_availability(provider_name):
        return True, {
            "base_url": BASE_URL_TEMPLATE.format(provider=provider_name),
            "api_key": "sk-test-key",
            "models": {},
        }

    monkeypatch.setattr(
        ClaudeCodingTool,
        "_check_provider_availability",
        staticmethod(_fake_availability),
    )

    # The settings template the dispatch reads and re-writes per
    # dispatch. Its ``env`` block is where the routed provider's
    # endpoint lands.
    settings_path = tmp_path / "subagent_settings.json"
    settings_path.write_text(
        json.dumps({"env": {}, "hooks": {}}), encoding="utf-8",
    )

    log = _RecordingLogger()
    tool = ClaudeCodingTool(
        logger=log,
        cwd=str(tmp_path),
        settings=settings_path,
        scene="execution",
    )
    # The mode is pinned rather than detected: the dispatch must do the
    # provider walk, and the walk is what the case is about.
    tool.inherit_providers = False

    def _pids() -> dict:
        """``{provider: pid}`` for every child that was spawned."""
        found = {}
        for entry in sorted(pid_dir.glob("*.pid")):
            found[entry.stem] = int(entry.read_text().strip())
        return found

    yield SimpleNamespace(
        tool=tool,
        log=log,
        pid_dir=pid_dir,
        pids=_pids,
    )

    tool.cleanup()


# ---------------------------------------------------------------------------
# The storm
# ---------------------------------------------------------------------------


def test_first_provider_429_storm_switches_within_90s(failover):
    """The first provider storms 429s; the second one answers.

    This is the incident's shape in one dispatch: three providers in the
    chain, the first one answering nothing but rate limits, and the
    question the post-mortem asked — "did the dispatch move on?" —
    answered by the answer itself.

    The 90-second bound is the incident's own: the switch has to
    happen long before a human would have given up.
    """
    started = time.monotonic()

    text = failover.tool.query("hello", scene="execution")

    elapsed = time.monotonic() - started

    assert "done" in text, (
        f"the answer must come from the provider that took over; got {text!r}"
    )
    assert failover.tool.current_call_provider == SECOND, (
        f"the dispatch must end on {SECOND}; got "
        f"{failover.tool.current_call_provider!r}"
    )
    assert elapsed < 90, (
        f"the failover took {elapsed:.1f}s — the incident's own budget is 90s"
    )

    # The provider that failed is on the record, and the switch is on
    # the record too. Both matter: the first says the storm really
    # happened, the second says the move was a decision and not a
    # coincidence.
    fallback = failover.log.named(FALLBACK_EVENT)
    assert fallback, (
        "the dispatch must record that it gave up on the first provider"
    )
    assert fallback[0]["data"]["from_provider"] == FIRST

    # The breaker is what ended the first stream. Without it the storm
    # would have run until the idle watchdog fired, which is the
    # incident.
    assert failover.log.named(BREAKER_EVENT), (
        "the 429 storm must be broken by the stream breaker"
    )

    # And the third provider was never touched: the second one answered,
    # so the chain stopped there. The incident's other half was that the
    # providers after the failing one were never tried at all.
    pids = failover.pids()
    assert set(pids) == {FIRST, SECOND}, (
        f"exactly the first two providers must have been dispatched to; "
        f"got {sorted(pids)!r}"
    )


# ---------------------------------------------------------------------------
# The orphan
# ---------------------------------------------------------------------------


def test_first_provider_child_is_reaped(failover):
    """The first provider's child is gone — no orphan left behind.

    The break is the graceful one, but "graceful" is a claim about HOW
    the child was asked to leave, not about WHETHER it left. Only a real
    process can answer the second question, so this is the one case
    that spawns a child and then checks that the pid is gone.
    """
    failover.tool.query("hello", scene="execution")

    pids = failover.pids()
    assert FIRST in pids, (
        f"the first provider must have been dispatched to; got {sorted(pids)!r}"
    )
    first_pid = pids[FIRST]

    # ``os.kill(pid, 0)`` is the "is this pid alive?" probe: it sends
    # no signal, so a process that is gone answers with
    # ``ProcessLookupError`` and a process that is still there answers
    # with nothing at all. A zombie would answer with nothing too —
    # which is why the child has to be REAPED, not merely signalled.
    with pytest.raises(ProcessLookupError):
        os.kill(first_pid, 0)


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def _task():
    """A task object good enough for the wait's log line."""
    from task import SubTask

    return SubTask(
        id="14-2",
        title="implement the thing",
        description="implement the thing",
        test_command="exit 1",
        status="pending",
        project_dir=".",
        files_to_modify=["changed.py"],
        model_type="medium",
    )


def test_no_quota_wait_exhausted_when_chain_has_room(failover, monkeypatch):
    """A chain with room is never declared exhausted.

    The exhaustion verdict is asked of the WHOLE chain, and the chain
    here still has two members that could serve: the second one just
    did. So the wait must not turn this dispatch into a
    "waiting_for_quota" marker — the work was done, and saying
    otherwise would strand everything downstream of it.
    """
    from agent import AutonomousAgent

    # A wait budget of two seconds: the point of this case is that the
    # wait is never entered, so the budget is only here to keep a
    # failure from hanging the suite.
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "2")

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.coding_tool = failover.tool
    agent.logger = failover.log
    agent._capacity_wait_clock = time.monotonic
    agent._capacity_wait_sleep = lambda seconds: None

    try:
        result = agent._query_with_capacity_wait(
            lambda: failover.tool.query("hello", scene="execution"),
            _task(),
            (1, 2),
            scene="execution",
        )
    except QuotaWaitExhausted as exc:
        pytest.fail(
            f"the chain still had room — {list(exc.providers)!r} — so the "
            f"dispatch must not be declared exhausted"
        )

    assert "done" in result, (
        f"the dispatch must have produced an answer; got {result!r}"
    )
    assert failover.tool.current_call_provider == SECOND, (
        f"the answer must come from {SECOND}; got "
        f"{failover.tool.current_call_provider!r}"
    )
