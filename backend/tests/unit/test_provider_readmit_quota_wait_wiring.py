"""The quota wait lets back in a provider that was excluded for other reasons.

The exclusion set is a list of names and it does not expire: it rides
down the retry chain and is only rebuilt when the chain is rebuilt. So a
provider dropped for a reason that has nothing to do with quota — a
transient 500, say — stays out of the candidate set for the whole call.

That is invisible while the fleet still has a candidate. It is not
invisible when the remaining candidates all go into a quota wait: the
call then waits for budget to come back while a provider that still has
budget sits outside the pool, excluded for a reason that expired the
moment the error did.

So before such a wait the excluded providers are offered back — once
each, and only the ones that can actually serve. This module pins the
WIRING of that decision: the pure function that makes it is covered by
``test_provider_readmit_after_quota_wait.py``, and what is checked here
is that the dispatch actually calls it, and that the call it makes is
bounded.

The fleet under test:

    Vendor A   — fails the first time with a 500, excluded for ``other``
    Vendor B   — the reading says its budget is gone
    Vendor C   — the reading says its budget is gone

So the first dispatch takes Vendor A and fails; the retry is left with
two exhausted providers, and the wait below is where Vendor A has to
come back. These tests drive the REAL ``_run_claude_interactive``
against a fake ``claude`` on ``PATH``, so the exclusion, the wait and
the re-dispatch are exercised rather than mocked.
"""

from __future__ import annotations

import os
import stat
import sys
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

#: The scene's tier: one provider that failed, two that ran out.
CHAIN = ["Vendor A", "Vendor B", "Vendor C"]

#: Ten per pool, so every provider in the tier has room to be picked.
_POOL_CAPS = [
    ("^vendor-a$", 10),
    ("^vendor-b$", 10),
    ("^vendor-c$", 10),
]

_PROVIDER_ENV = {
    "base_url": "https://api.vendor-a.example/anthropic",
    "api_key": "sk-fake",
    "models": {"default": "m", "opus": "m", "sonnet": "m", "haiku": "m"},
}

#: The event the readmission is reported through.
READMIT_EVENT = "provider_readmitted_after_quota_wait"


class _QuotaWaitObserved(RuntimeError):
    """The dispatch reached a wait.

    The wait is where the old behaviour parked a provider that still had
    budget, so the stubbed sleep ends the test instead of serving a
    window that is hours long: a test that waits for the real thing
    would hang rather than fail.
    """


class _Recorder:
    """A logger that keeps every event it is handed.

    The dispatch reports through the structured shape — an event name, a
    message and a ``data`` payload — and the payload is the thing under
    test: the readmission has to name the provider, the scene, the
    reason the provider was excluded for, and the providers whose budget
    is gone.
    """

    def __init__(self) -> None:
        self.events: list = []

    def _record(self, level, *args, **kwargs) -> None:
        event = args[0] if args else None
        message = args[1] if len(args) > 1 else ""
        data = kwargs.get("data")
        self.events.append({
            "level": level,
            "event": event,
            "message": message,
            "data": dict(data) if isinstance(data, dict) else {},
        })

    def warning(self, *args, **kwargs) -> None:
        self._record("warning", *args, **kwargs)

    def info(self, *args, **kwargs) -> None:
        self._record("info", *args, **kwargs)

    def error(self, *args, **kwargs) -> None:
        self._record("error", *args, **kwargs)

    def debug(self, *args, **kwargs) -> None:
        self._record("debug", *args, **kwargs)

    def of(self, event: str) -> list:
        return [e for e in self.events if e["event"] == event]


@pytest.fixture(autouse=True)
def _fresh_cooldown():
    """Give the test its own park list, and leave the process as found.

    A park left behind by another test would answer "is this provider
    parked?" for this one, and the park is one of the three facts the
    readmission reads: a parked provider is NOT let back in.
    """
    cooldown = ProviderCooldown()
    set_shared_cooldown(cooldown)
    yield cooldown
    set_shared_cooldown(None)


@pytest.fixture(autouse=True)
def _pools(capacity_config):
    """Ten slots per provider, so capacity never masks the quota."""
    capacity_config(_POOL_CAPS)


@pytest.fixture
def scene(monkeypatch):
    """Wire a three-provider scene chain and a reachable provider row."""
    monkeypatch.setattr(
        provider_routing, "resolve_provider_chain",
        lambda scene, *a, **k: list(CHAIN),
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability",
        staticmethod(lambda name: (True, dict(_PROVIDER_ENV))),
    )


def _open(name: str, remaining_pct: float = 22.0):
    """A reading that says the provider still has budget."""
    return provider_order.ProviderQuota(
        provider=name,
        shape=provider_order.SHAPE_ROLLING,
        remaining_pct=remaining_pct,
        exhausted=False,
    )


def _shut(name: str):
    """A reading that says the provider has no budget left."""
    return provider_order.ProviderQuota(
        provider=name,
        shape=provider_order.SHAPE_ROLLING,
        remaining_pct=0.0,
        exhausted=True,
    )


@pytest.fixture
def reading(monkeypatch):
    """Vendor A still has budget; Vendor B and Vendor C have none.

    The legacy percentage map says every provider is full — the live
    reading is what decides eligibility, and it is what says that two
    of the three are shut.
    """
    quota_map = {
        "Vendor A": _open("Vendor A"),
        "Vendor B": _shut("Vendor B"),
        "Vendor C": _shut("Vendor C"),
    }
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map",
        lambda *a, **k: dict(quota_map),
    )
    monkeypatch.setattr(
        provider_order, "load_provider_5h_usage",
        lambda *a, **k: {name: 100.0 for name in CHAIN},
    )
    return quota_map


@pytest.fixture
def waits(monkeypatch):
    """Record every wait the dispatch enters, and end the test there."""
    seen: list = []

    def _sleep(seconds):
        seen.append(seconds)
        raise _QuotaWaitObserved(
            f"the dispatch entered a wait of {seconds}s"
        )

    monkeypatch.setattr(coding_tool, "_quota_wake_sleep", _sleep)
    return seen


def _install_fake_claude(tmp_path, monkeypatch, failures: int) -> Path:
    """A fake ``claude`` that fails ``failures`` times, then answers.

    The count lives in a file, so the number of dispatches is readable
    after the fact — the fake has to be told apart from the real CLI
    only by how many times it was run.
    """
    counter = tmp_path / "claude-calls.txt"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(
        "#!/bin/sh\n"
        f'COUNTER="{counter}"\n'
        "N=0\n"
        'if [ -f "$COUNTER" ]; then N=$(cat "$COUNTER"); fi\n'
        'N=$((N + 1))\n'
        'echo "$N" > "$COUNTER"\n'
        f'if [ "$N" -le {failures} ]; then\n'
        "  echo '{\"type\":\"result\",\"is_error\":true,"
        "\"api_error_status\":\"500\","
        "\"result\":\"transient upstream failure\"}'\n"
        "else\n"
        "  echo '{\"type\":\"result\",\"result\":\"ok\"}'\n"
        "fi\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", "")
    )
    return counter


def _dispatches(counter: Path) -> int:
    """How many times the fake ``claude`` was run."""
    try:
        return int(counter.read_text(encoding="utf-8").strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def _tool(logger):
    return ClaudeCodingTool(
        scene="verification",
        concurrency_tracker=ActiveConcurrencyTracker(),
        logger=logger,
    )


# ---------------------------------------------------------------------------
# The provider comes back, and the call does not wait
# ---------------------------------------------------------------------------


def test_excluded_provider_is_readmitted_before_the_wait(
    tmp_path, monkeypatch, scene, reading, waits,
):
    """The excluded provider serves the work; the wait never happens.

    Vendor A fails the first dispatch with a 500 and is excluded for
    that reason. The retry is left with two providers whose budget is
    gone — and a wait for budget that Vendor A still has.

    So Vendor A comes back, and the work goes to it. The wait is not
    entered at all: the fleet was never out of budget, only out of
    candidates.
    """
    counter = _install_fake_claude(tmp_path, monkeypatch, failures=1)
    logger = _Recorder()
    tool = _tool(logger)

    assert tool.query("do the task", timeout=30) == "ok"

    assert tool.current_call_provider == "Vendor A", (
        "the provider that was excluded for a non-quota reason still had "
        f"budget; the work went to {tool.current_call_provider!r}"
    )

    readmissions = logger.of(READMIT_EVENT)
    assert len(readmissions) == 1, (
        f"the readmission has to be reported once; the log holds "
        f"{readmissions!r}"
    )
    assert readmissions[0]["data"] == {
        "provider": "Vendor A",
        "scene": "verification",
        "reason": "other",
        "quota_exhausted": ["Vendor B", "Vendor C"],
    }

    assert _dispatches(counter) == 2, (
        "the failure, then the re-dispatch to the provider that came back"
    )
    assert waits == [], (
        f"the dispatch entered a wait ({waits!r}); the fleet had a "
        "provider with budget, so there was nothing to wait for"
    )


# ---------------------------------------------------------------------------
# The readmission happens once per dispatch
# ---------------------------------------------------------------------------


def test_readmission_is_once_per_dispatch(
    tmp_path, monkeypatch, scene, reading, waits,
):
    """A provider that fails again is not let back in a second time.

    The readmission is a way of undoing an exclusion that has expired —
    not a way of keeping a provider out and then letting it in for as
    long as the caller keeps failing. Vendor A comes back, fails again,
    and the second wait finds it already readmitted: the exclusion
    stands from then on, and the call waits as it would have.
    """
    counter = _install_fake_claude(tmp_path, monkeypatch, failures=2)
    logger = _Recorder()
    tool = _tool(logger)

    with pytest.raises(_QuotaWaitObserved):
        tool.query("do the task", timeout=30)

    readmissions = logger.of(READMIT_EVENT)
    assert len(readmissions) == 1, (
        "a provider that was already readmitted must not be readmitted "
        f"again; the log holds {readmissions!r}"
    )
    assert readmissions[0]["data"]["provider"] == "Vendor A"

    assert waits, (
        "the second wait has to be entered: with the provider excluded "
        "again there is nothing left to dispatch to"
    )
    assert _dispatches(counter) == 2, (
        "the first failure, and the re-dispatch to the provider that came "
        "back — the dispatch after that enters the wait, so it never "
        "reaches the CLI"
    )
