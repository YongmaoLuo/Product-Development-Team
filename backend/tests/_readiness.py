"""One shared place for "how long may a spawned process take to answer?".

Why the module exists
---------------------
Five suites each carried their own wall-clock budget for the same
question — how long a spawned process may take to become ready — and
the copies had drifted: twenty seconds where a stub service was waited
for, thirty seconds in three places, and a hundred and twenty seconds
for a whole run.  A budget is a property of the *machine*, not of the
file that happens to assert it, so it belongs in one place; and the
suites that assert it should all read the same number, because a case
that fails because the host was busy is indistinguishable from a
regression at a glance.

Two readiness numbers, not one
------------------------------
The two numbers measure different things, and folding them into one
would make a single number mean two things:

* :data:`READY_TIMEOUT_SECONDS` is the budget for the **backend** to
  become ready.  The backend reads the machine before it answers — its
  own import, one pass of the orphan reaper, the scheduler's first
  tick — so it is the slow one;
* :data:`STUB_READY_TIMEOUT_SECONDS` is the budget for a **stub**
  service: a bare socket server the test starts itself, which answers
  in well under a second.  It keeps the number the suites already
  used, because the slow thing is the host, not the process being
  waited for.

Why the backend's number is what it is
--------------------------------------
:data:`READY_TIMEOUT_SECONDS` is set by the slowest thing the suites
wait for:

* the backend's start-up was measured at 18 s to 26 s on a host whose
  load average was in the forties: the application's own import, one
  pass of the orphan reaper over the machine, and the scheduler's first
  tick;
* the request-guard self-check inside the backend gives up after 60 s
  and calls the start-up broken.  The budget sits *above* that on
  purpose: a start-up that has grown past what the product itself
  tolerates is a failure, and a failure should be a failure of the
  case, not a hang.  The budget has to clear the guard's window too,
  because the guard's clock starts after the import phase, so the
  process can be older than the budget the product enforces;
* the guard is the product's own verdict, so a start-up that blows the
  budget fails the *server*, not only the test — the number here is
  patience, not a licence.

:data:`RUN_BUDGET_SECONDS` is the budget for a whole run: the readiness
budget plus the room the run's own work needs.  It is derived, not
tuned — one number to change, and the two budgets cannot drift apart.

One file is left out on purpose
-------------------------------
``backend/tests/integration/test_orphan_reaper_zero_impact.py`` keeps
its own budget: it waits for a *child the test itself spawned* to show
up in the machine's process table with a readable environment.  That is
a question about the process table, not about the backend's start-up,
and folding one into the other would make a number mean two things.

Why it is one module
--------------------
The decision was already being made, but inline, once per file, and the
copies had drifted.  One budget means one number and one place to
change it.

Standard library only, and deliberately no ``pytest`` import: the
module has to be usable from a ``conftest.py`` as well as from ordinary
test modules.

The load-aware budget
---------------------
A deadline is a property of the **machine**, not of the file that
happens to assert it, so the way a deadline is derived from the host's
load lives here too — one formula, one answer.  Two suites carried
their own copy of it, and the copies had already drifted; a third copy
would have drifted further.

A deadline only means something on a host that can keep time.  The gate
script measures the host before it runs anything and exports the answer
(:data:`tests._host_load.HOST_MEASURABLE_ENV`); when the answer is "no",
a suite that waits for a service **skips** instead of failing on a
deadline the machine was never going to meet.
"""

from __future__ import annotations

from typing import Optional

#: How long a spawned backend may take to become ready (seconds).
#:
#: The ceiling catches a **hung** backend, not a slow one.  It used to
#: be 30 s in the suites that carried their own copy, calibrated on an
#: idle machine: on a loaded host the real backend boots slower than
#: that, and the 2026-10-31 whole-repo gate reported four errors that
#: all said the same thing — ``the spawned server did not become ready
#: within 30.0s (status 502)``.  A red that only means "the machine was
#: busy" is a red that produces repair tasks against a delivery that
#: did not fail.  120 s still catches a hang and no longer blames the
#: delivery for the load.
READY_TIMEOUT_SECONDS = 120.0

#: How long a spawned stub service may take to become ready (seconds).
#:
#: A stub is a bare socket server the test starts itself; it answers in
#: well under a second.  The number is the one the suites already used.
STUB_READY_TIMEOUT_SECONDS = 20.0

#: How long a whole run may take (seconds): the readiness budget plus the
#: room the run's own work needs.
#:
#: The total is the one the suites already used; what changed is the
#: share of it that belongs to the start-up.  Nothing is loosened.
RUN_BUDGET_SECONDS = READY_TIMEOUT_SECONDS + 30.0

#: The ceiling of a load-aware readiness budget (seconds).
#:
#: A scaled budget is still a budget: without a ceiling a host that
#: never settles would keep the case waiting, and a hang would read as
#: a slow machine.  A suite whose own per-test watchdog is shorter
#: passes its own ceiling — the ceiling is a policy about *that*
#: module's watchdogs, not a constant of the machine.
READINESS_BUDGET_MAX_SECONDS = 120.0


def readiness_budget_seconds(
    base: float,
    *,
    ceiling: float = READINESS_BUDGET_MAX_SECONDS,
    limit: Optional[float] = None,
    load: Optional[float] = None,
) -> float:
    """How long a spawned process is given to come up, decided by the load.

    The budget follows the host's load average, because both the
    child's own start-up and the cost of one probe scale with it.  The
    scale is the shared gate's limit: a host at the limit gets twice
    the base budget, and the budget stops at ``ceiling``, because a
    scaled budget is still a budget.

    ``limit`` and ``load`` default to the shared gate's answers for the
    host this runs on; they are parameters so the rule can be exercised
    without loading the machine on purpose.
    """
    from tests import _host_load

    if limit is None:
        limit = _host_load.host_load_limit()
    if load is None:
        load = _host_load.current_host_load()
    if limit <= 0:
        return float(base)
    scaled = float(base) * (1.0 + max(0.0, load) / limit)
    return min(scaled, float(ceiling))


def host_unmeasurable_reason(
    env: Optional[dict] = None,
) -> Optional[str]:
    """Why this case must be skipped because the host cannot keep time.

    The gate script probes the host before it runs anything and exports
    the answer.  When the answer is "the host cannot measure wall-clock
    time", a deadline is not a deadline any more: the machine was
    never going to meet it, so a case that waits on one is skipped
    rather than failed.

    ``None`` means "keep going" — the host was never measured, or it
    was measured and can keep time.
    """
    from tests import _host_load

    if _host_load.host_measurable(env) is not False:
        return None
    return (
        "skipped: the host cannot measure wall-clock time "
        f"({_host_load.HOST_MEASURABLE_ENV}=0)"
    )


def skip_if_host_unmeasurable() -> None:
    """Skip the calling case when the host cannot measure wall-clock time.

    ``pytest`` is imported here rather than at the top of the module:
    this module is imported by ``conftest.py`` before the plugin
    machinery that provides ``pytest.skip`` is in place, so a
    module-level import would make it unusable there.
    """
    reason = host_unmeasurable_reason()
    if reason is None:
        return
    import pytest

    pytest.skip(reason)