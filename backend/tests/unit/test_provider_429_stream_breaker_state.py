"""Is this stream stalled, or is it just talking to itself?

Why this file exists
--------------------
2026-10-27: a sub-agent's stream emitted ``api_error`` lines and
nothing else for 38 minutes. The CLI was retrying the 429 internally,
so the process was alive and every line it wrote refreshed
``last_line_time`` — the exact quantity the idle watchdog measures.
The watchdog therefore never fired, and the dispatch sat there for the
better part of an hour.

Silence is not the only shape a dead stream takes, and it was not this
one: the stream was *loud*. What it lacked was PRODUCTION. So the
breaker asks a different question from the watchdog's: not "has it gone
quiet?" but "has it been erroring for a long time without producing
anything?".

What is pinned here
-------------------
* three consecutive errors spanning more than the window → the breaker
  trips;
* three errors that came and went inside the window → it does not.
  This is the "do not kill on the first 429" half of the contract;
* a production line in between → the streak and the window both start
  over, so a stream that recovered is never broken;
* a non-error ``system`` event (``init`` and friends) is neither
  production nor an error: it neither counts nor clears;
* a window of zero → the breaker is off, and says so forever;
* the operator's three knobs read their environment variables and fall
  back to their defaults when those are unset or unreadable.

The state machine is pure: it takes an event, a subtype and a
timestamp, and answers a question. No thread, no clock, no subprocess —
so everything here is deterministic and nothing touches the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from coding_tool import (  # noqa: E402
    _StreamStallBreaker,
    breaker_scenes,
    breaker_streak,
    breaker_window_sec,
)

#: The operator's defaults, as the module declares them.
DEFAULT_WINDOW_SEC = 60.0
DEFAULT_STREAK = 3
DEFAULT_SCENES = {"execution"}


# ---------------------------------------------------------------------------
# The stall itself
# ---------------------------------------------------------------------------


def test_trips_after_streak_and_window():
    """Three errors over 61s is a stall: the stream is broken.

    The 429 is not one failure but four — the CLI retries it
    internally — so a single rate-limit answer is not evidence of
    anything. Three of them, spread over more than the window, is.
    """
    breaker = _StreamStallBreaker(window_sec=60.0, streak=3)

    assert breaker.observe("system", "api_error", 0.0) is False
    assert breaker.observe("system", "api_error", 30.0) is False
    assert breaker.observe("system", "api_error", 61.0) is True


def test_window_not_elapsed_does_not_trip():
    """Three errors inside the window are not a stall.

    This is the half of the contract that says the breaker is not a
    "kill on 429" switch: a provider that answers three rate limits
    inside five seconds is a provider that is busy, not a stream that
    is wedged.
    """
    breaker = _StreamStallBreaker(window_sec=60.0, streak=3)

    assert breaker.observe("system", "api_error", 0.0) is False
    assert breaker.observe("system", "api_error", 2.0) is False
    assert breaker.observe("system", "api_error", 5.0) is False


def test_output_resets_the_breaker():
    """A production line ends the streak AND restarts the window.

    Upstream test decision point 10: a 429 followed by normal output
    must not trip the breaker. Both halves matter — the streak count
    and the window — so the errors after the output are three in
    number and still not enough, because the window they are measured
    against starts at the production line.
    """
    breaker = _StreamStallBreaker(window_sec=60.0, streak=3)

    assert breaker.observe("system", "api_error", 0.0) is False
    assert breaker.observe("system", "api_error", 30.0) is False

    # The stream produced something: the streak ends here.
    assert breaker.observe("assistant", "text", 40.0) is False

    # Three more errors — but the window they sit in starts at the
    # production line, so they are 21s of noise, not a stall.
    assert breaker.observe("system", "api_error", 45.0) is False
    assert breaker.observe("system", "api_error", 50.0) is False
    assert breaker.observe("system", "api_error", 61.0) is False


def test_user_output_also_resets_the_breaker():
    """A ``user`` event is a production line too.

    The stream's ``user`` events carry the tool results the model asked
    for; they are as much proof of life as an ``assistant`` line, and
    a breaker that ignored them would trip on a working stream.
    """
    breaker = _StreamStallBreaker(window_sec=60.0, streak=3)

    assert breaker.observe("system", "api_error", 0.0) is False
    assert breaker.observe("system", "api_error", 30.0) is False
    assert breaker.observe("user", "tool_result", 40.0) is False
    assert breaker.observe("system", "api_error", 45.0) is False
    assert breaker.observe("system", "api_error", 50.0) is False
    assert breaker.observe("system", "api_error", 61.0) is False


def test_non_error_system_event_is_neither_output_nor_error():
    """``system/init`` and friends neither count nor clear.

    A ``system`` event that is not an ``api_error`` says nothing about
    whether the stream is producing, so it must not end a streak — and
    it must not start one either.
    """
    breaker = _StreamStallBreaker(window_sec=60.0, streak=3)

    # A stream of init-ish events is not a stream of errors.
    assert breaker.observe("system", "init", 0.0) is False
    assert breaker.observe("system", "init", 30.0) is False
    assert breaker.observe("system", "init", 61.0) is False

    # And it does not clear one: the two errors below still trip with
    # the third, because the init did not reset the streak.
    assert breaker.observe("system", "api_error", 62.0) is False
    assert breaker.observe("system", "api_error", 63.0) is False
    assert breaker.observe("system", "init", 64.0) is False
    assert breaker.observe("system", "api_error", 122.0) is True


def test_zero_window_disables():
    """A window of zero switches the breaker off, and off means off.

    ``PDT_429_BREAKER_SEC=0`` is the operator's way of turning the
    thing off without removing it — so it must never trip, however long
    the errors go on.
    """
    breaker = _StreamStallBreaker(window_sec=0.0, streak=3)

    assert breaker.observe("system", "api_error", 0.0) is False
    assert breaker.observe("system", "api_error", 30.0) is False
    assert breaker.observe("system", "api_error", 600.0) is False


def test_negative_window_disables():
    """A negative window is a mistake, and a mistake must not kill."""
    breaker = _StreamStallBreaker(window_sec=-5.0, streak=3)

    assert breaker.observe("system", "api_error", 0.0) is False
    assert breaker.observe("system", "api_error", 600.0) is False


# ---------------------------------------------------------------------------
# The operator's knobs
# ---------------------------------------------------------------------------


def test_breaker_scenes_defaults_to_execution(monkeypatch):
    """With no environment set, the breaker is armed for ``execution``.

    The scene whose dispatch is a long unattended run is the one whose
    stream nobody is watching.
    """
    monkeypatch.delenv("PDT_429_BREAKER_SCENES", raising=False)

    assert breaker_scenes() == DEFAULT_SCENES


def test_breaker_scenes_reads_the_environment(monkeypatch):
    """The operator can arm other scenes by name."""
    monkeypatch.setenv("PDT_429_BREAKER_SCENES", "execution, planning")

    assert breaker_scenes() == {"execution", "planning"}


def test_breaker_scenes_falls_back_when_empty(monkeypatch):
    """A blank list names no scene, which is not a configuration."""
    monkeypatch.setenv("PDT_429_BREAKER_SCENES", "  ,  ")

    assert breaker_scenes() == DEFAULT_SCENES


def test_breaker_window_sec_defaults_and_reads_the_environment(monkeypatch):
    """The window is the operator's number, and its default is a minute."""
    monkeypatch.delenv("PDT_429_BREAKER_SEC", raising=False)
    assert breaker_window_sec() == DEFAULT_WINDOW_SEC

    monkeypatch.setenv("PDT_429_BREAKER_SEC", "12.5")
    assert breaker_window_sec() == 12.5


def test_breaker_window_sec_falls_back_on_rubbish(monkeypatch):
    """A knob set to a word is a typo, and a typo is ignored."""
    monkeypatch.setenv("PDT_429_BREAKER_SEC", "soon")

    assert breaker_window_sec() == DEFAULT_WINDOW_SEC


def test_breaker_streak_defaults_and_reads_the_environment(monkeypatch):
    """Three consecutive errors is the default, and the operator's."""
    monkeypatch.delenv("PDT_429_BREAKER_STREAK", raising=False)
    assert breaker_streak() == DEFAULT_STREAK

    monkeypatch.setenv("PDT_429_BREAKER_STREAK", "5")
    assert breaker_streak() == 5


def test_breaker_streak_falls_back_on_rubbish(monkeypatch):
    """A streak that is not a number falls back to the default."""
    monkeypatch.setenv("PDT_429_BREAKER_STREAK", "many")

    assert breaker_streak() == DEFAULT_STREAK
