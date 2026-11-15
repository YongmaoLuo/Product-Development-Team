"""Unit tests: the scheduler's default minimum runtime is zero.

What this file holds down
-------------------------
``min_runtime_seconds`` is the threshold ``M``: a running plan may only
be interrupted once it has been running for at least ``M`` seconds.  It
is a *mechanism* — an operator may set it to any value — but its
**default is zero**.

The default used to be 600 seconds, and that turned ``M`` into a
minimum protection period: a plan that had been running for less than
ten minutes could never enter the candidate set.  That contradicts the
rule the scheduler follows — the plan that has run the shortest is the
one that gets interrupted first — because the plan that has run the
shortest is exactly the one the threshold kept out of the candidate
set.  With the default at zero the two rules agree.

The contracts this file pins:

1. **The default is zero.**  :data:`DEFAULT_MIN_RUNTIME_SECONDS` is
   ``0.0``, and a snapshot that names no ``min_runtime_seconds`` says
   ``0.0`` as well.
2. **A zero threshold admits the shortest run.**  A plan that has been
   running for one second is a candidate when ``M`` is zero, and it is
   the plan that gets interrupted.
3. **The mechanism is untouched.**  An explicit ``M`` still protects:
   a threshold of 600 keeps a one-second-old plan out of the candidate
   set.  The default changed; the rule did not.

The threshold itself is read from the configuration — see
``backend/plan_scheduler/config.py`` — and this file is about the
default the scheduler runs with when nobody has said otherwise.
"""

from __future__ import annotations

import pytest

from plan_scheduler.config import DEFAULT_MIN_RUNTIME_SECONDS, SchedulerConfig
from plan_scheduler.policy import select_victim

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. The default is zero
# ---------------------------------------------------------------------------


def test_default_protection_is_zero() -> None:
    """The default threshold is zero — and a bare snapshot says so too.

    The constant is what the loader falls back to for a key the
    configuration file does not name, and :class:`SchedulerConfig` is
    what a snapshot is built from.  Both have to say zero: a constant
    of zero next to a snapshot default of 600 would leave the
    scheduler running with a protection window nobody asked for.
    """
    assert DEFAULT_MIN_RUNTIME_SECONDS == 0.0
    assert SchedulerConfig().min_runtime_seconds == 0.0


# ---------------------------------------------------------------------------
# 2. A zero threshold admits the shortest run
# ---------------------------------------------------------------------------


def test_zero_threshold_admits_the_shortest() -> None:
    """A plan that has run for one second is a candidate when ``M`` is zero.

    This is the whole point of the default: the plan that has run the
    shortest is the one that gets interrupted, so it has to be able to
    reach the candidate set at all.
    """
    snapshot = {"running": [{"plan_id": "fresh", "elapsed": 1.0}]}

    assert select_victim(snapshot, DEFAULT_MIN_RUNTIME_SECONDS) == "fresh"


# ---------------------------------------------------------------------------
# 3. An explicit threshold still protects
# ---------------------------------------------------------------------------


def test_explicit_threshold_still_protects() -> None:
    """The mechanism survives the change of the default.

    ``M`` is a configuration value: an operator who sets it to 600 gets
    600 seconds of protection, and a plan that has just started is not
    interrupted however far the capacity is exceeded.
    """
    snapshot = {"running": [{"plan_id": "fresh", "elapsed": 1.0}]}

    assert select_victim(snapshot, 600) is None
    assert SchedulerConfig(min_runtime_seconds=600).min_runtime_seconds == 600.0
