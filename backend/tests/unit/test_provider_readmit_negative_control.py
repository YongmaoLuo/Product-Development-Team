"""Negative controls for the two rules this change put in.

Two rules went in:

  **R1** — before a quota wait, the providers that were excluded for a
  reason that has nothing to do with quota are let back in, so the work
  goes to a provider that still has budget instead of waiting for budget
  that is not coming back.

  **R2** — a provider failure that carries no status at all is read as
  ``rate_limited`` by the wrapper that classifies provider failures,
  while the public classifier keeps its own answer for an empty status
  (it has another caller whose contract must not change).

Every other test in this change asserts what happens **while the rules
are in place**. An assertion like that is worth something only if it
would fail once the rule is gone — and "it would" is a claim about a
version of the code that does not exist, so it has to be checked rather
than asserted. This module holds the checks:

  * **R2** — the public classifier still answers ``other`` for an empty
    status, and the wrapper still answers ``rate_limited``. Drop the
    wrapper's override and the second half goes red; move the rule into
    the public function and the first half goes red.

  * **R1** — one scene, run twice. First with the readmission in place:
    the provider that was excluded for a transient failure comes back,
    the work goes to it, and the call does not wait. Then with the
    readmission neutralised — which is what removing it looks like from
    the dispatch's side — and the call **does** wait. The first half is
    the guard; the second half is what gives the first half teeth,
    because a scene that waited either way would prove nothing about
    the readmission.

The scene under test:

    Vendor A   — fails the first time with a 500, excluded for ``other``
    Vendor B   — the reading says its budget is gone
    Vendor C   — the reading says its budget is gone

So the first dispatch takes Vendor A and fails; the retry is left with
two providers whose budget is gone, and the wait is where Vendor A has
to come back.

The dispatches are driven through the REAL ``_run_claude_interactive``
against a fake ``claude`` on ``PATH``, so the exclusion, the wait and
the re-dispatch are exercised rather than mocked. Everything this module
needs — the fake CLI, the reading, the capacity rules and the park list
— is built here: no other test module is imported, and the quota reading
is a stub, so no local CC Switch is contacted.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
import provider_order  # noqa: E402
import provider_routing  # noqa: E402
from coding_tool import (  # noqa: E402
    ApiError,
    ClaudeCodingTool,
    _classify_provider_failure,
)
from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    ProviderCooldown,
    classify_capacity_failure,
    set_shared_cooldown,
)

#: The scene's tier: one provider that failed, two that ran out.
CHAIN = ["Vendor A", "Vendor B", "Vendor C"]

#: Ten per pool, so capacity never masks the quota.
_POOL_CAPS = [
    ("^vendor-a$", 10),
    ("^vendor-b$", 10),
    ("^vendor-c$", 10),
]

#: The provider row the dispatch finds for every candidate.
_PROVIDER_ENV = {
    "base_url": "https://api.vendor-a.example/anthropic",
    "api_key": "sk-fake",
    "models": {"default": "m", "opus": "m", "sonnet": "m", "haiku": "m"},
}


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
    message and a ``data`` payload. This module is not about the log, so
    the recorder keeps the events and answers nothing else; what matters
    here is which dispatch happened, and the log does not say that.
    """

    def __init__(self) -> None:
        self.events: list = []

    def _record(self, level, *args, **kwargs) -> None:
        self.events.append({
            "level": level,
            "event": args[0] if args else None,
            "message": args[1] if len(args) > 1 else "",
            "data": dict(kwargs.get("data") or {}),
        })

    def warning(self, *args, **kwargs) -> None:
        self._record("warning", *args, **kwargs)

    def info(self, *args, **kwargs) -> None:
        self._record("info", *args, **kwargs)

    def error(self, *args, **kwargs) -> None:
        self._record("error", *args, **kwargs)

    def debug(self, *args, **kwargs) -> None:
        self._record("debug", *args, **kwargs)


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
def _no_inherited_reading():
    """Start with no read state, and leave none behind.

    The reason of the last read lives in module state that the readers
    do not reset, so a test that ran earlier in the session could
    otherwise hand this one a reason of its own.
    """
    provider_order._record_usage_read(None, frozenset())
    provider_order._record_quota_read(None, frozenset())
    yield
    provider_order._record_usage_read(None, frozenset())
    provider_order._record_quota_read(None, frozenset())


def _shut(name: str):
    """A reading that says the provider has no budget left."""
    return provider_order.ProviderQuota(
        provider=name,
        shape=provider_order.SHAPE_ROLLING,
        remaining_pct=0.0,
        exhausted=True,
    )


@pytest.fixture
def scene(monkeypatch, capacity_config):
    """Wire the three-provider scene and the reading that goes with it.

    The reading says Vendor B and Vendor C are shut. Vendor A is
    deliberately ABSENT from it: "no reading" is the absence of a fact,
    not a verdict, so a provider dropped for no reason at all is a
    provider dropped for no reason — and that is the one the readmission
    brings back.

    The two shut rows are what put the dispatch on the quota-wait path.
    Without them the remaining candidates would still be dispatchable and
    the wait would never be reached, which would make the negative
    control below a check of nothing. The rows are a stub, so no local
    CC Switch is contacted.
    """
    capacity_config(_POOL_CAPS)
    monkeypatch.setattr(
        provider_routing, "resolve_provider_chain",
        lambda scene, *a, **k: list(CHAIN),
    )
    # The availability probe is a class-level static method, so this is
    # where it has to be replaced for the dispatch to see the stub.
    monkeypatch.setattr(
        ClaudeCodingTool, "_check_provider_availability",
        staticmethod(lambda name: (True, dict(_PROVIDER_ENV))),
    )
    monkeypatch.setattr(
        provider_order, "load_provider_5h_usage",
        lambda *a, **k: {name: 100.0 for name in CHAIN},
    )
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map",
        lambda *a, **k: {
            "Vendor B": _shut("Vendor B"),
            "Vendor C": _shut("Vendor C"),
        },
    )


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


def _install_fake_claude(tmp_path, monkeypatch, counter_name: str) -> Path:
    """A fake ``claude`` that fails its first call and answers after that.

    The count lives in a file, so the number of dispatches is readable
    after the fact — the fake has to be told apart from the real CLI
    only by how many times it was run. Each half of the scene gets its
    own counter, so the second half starts from the same "the first
    dispatch fails" state the first half did.
    """
    counter = tmp_path / counter_name
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
        'if [ "$N" -le 1 ]; then\n'
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


def _tool():
    """A tool with its own slot counter, as every dispatch has."""
    return ClaudeCodingTool(
        scene="verification",
        concurrency_tracker=ActiveConcurrencyTracker(),
        logger=_Recorder(),
    )


# ---------------------------------------------------------------------------
# R2 — the empty status is read in the wrapper, and only in the wrapper
# ---------------------------------------------------------------------------


def test_r2_public_classifier_still_returns_other_for_empty_status(
    monkeypatch,
):
    """The rule lives in the wrapper, and only in the wrapper.

    The public classifier answers ``other`` for an empty status — it has
    another caller whose contract must not change — and the wrapper
    around it answers ``rate_limited`` for the very same failure. Both
    halves are needed: the first says the rule did not leak into the
    public function, the second says it is still there.
    """
    # The wrapper asks for the quota map on every classification, so it
    # is stubbed empty: an empty map is the answer that keeps this test
    # off the local CC Switch.
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map", lambda *a, **k: {},
    )

    assert classify_capacity_failure("upstream hiccup", "") == "other", (
        "the public classifier's answer for an empty status moved; the "
        "rule belongs in the wrapper, which has a second caller whose "
        "contract must not change"
    )

    assert _classify_provider_failure(
        ApiError("upstream hiccup", status=None), "Vendor A",
    ) == "rate_limited", (
        "the wrapper stopped reading an empty status as a rate limit, so "
        "the provider is discarded instead of waited out"
    )


# ---------------------------------------------------------------------------
# R1 — the readmission is what keeps the call out of the wait
# ---------------------------------------------------------------------------


def test_r1_without_readmission_the_quota_wait_is_entered(
    tmp_path, monkeypatch, scene, waits,
):
    """The readmission is what keeps the call out of the wait.

    One scene, run twice.

    First with the readmission in place: Vendor A fails with a 500 and
    is excluded for that reason, the retry is left with two providers
    whose budget is gone, Vendor A comes back before the wait, and the
    work goes to it. The call never waits.

    Then with the readmission neutralised — a function that lets nobody
    back in is what removing the rule looks like from the dispatch's
    side — and the excluded provider stays out. The remaining
    candidates are all shut, so the call waits for budget that is not
    coming back.

    The second half is the negative control for the first: a scene that
    waited either way would say nothing about the readmission, and a
    rule that can be removed without the first half noticing is not a
    rule.
    """
    # ---- the rule in place: the excluded provider comes back ---------
    counter = _install_fake_claude(
        tmp_path, monkeypatch, "claude-calls-guard.txt",
    )
    tool = _tool()

    assert tool.query("do the task", timeout=30) == "ok", (
        "the provider that was excluded for a non-quota reason still had "
        "budget, so the work had to go to it"
    )
    assert waits == [], (
        f"the dispatch entered a wait ({waits!r}); the fleet had a "
        "provider with budget, so there was nothing to wait for"
    )
    assert _dispatches(counter) == 2, (
        "the failure, then the re-dispatch to the provider that came back"
    )

    # ---- the rule removed: the wait is entered ------------------------
    monkeypatch.setattr(
        coding_tool,
        "_readmit_providers_for_quota_wait",
        lambda *a, **k: [],
    )
    counter = _install_fake_claude(
        tmp_path, monkeypatch, "claude-calls-control.txt",
    )
    tool = _tool()
    waits.clear()

    with pytest.raises(_QuotaWaitObserved):
        tool.query("do the task", timeout=30)

    assert waits, (
        "without the readmission the call has to wait: the excluded "
        "provider stays out and the remaining candidates are all shut"
    )
    assert _dispatches(counter) == 1, (
        "only the first dispatch reached the CLI; the second one had "
        "nothing to dispatch to and waited instead"
    )
