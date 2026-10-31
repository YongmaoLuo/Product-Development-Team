"""One shared gate decides whether the host is busy enough to break a budget.

Why this exists
---------------
The performance and time-sensitive lanes assert on wall-clock numbers.  On a
host that is already carrying someone else's work those assertions fail for a
reason that has nothing to do with the code under test, and the failure reads
like a regression of the thing being measured.

So the gate is asked exactly one question: *is this host too loaded to
measure?*  When the answer is yes the case is skipped, and the reason carries
the numbers that decided it -- so the lane log says why the bound was not
asserted instead of leaving a silent gap in the report.

The gate is injectable on purpose.  The load, the limit and the environment
all arrive as arguments, so every branch is reachable from a test without
having to load the machine on purpose to prove that it works.

The three branches are the ones the lanes actually have:

* a loaded host is skipped, with the load and the limit in the message;
* a calm host is not skipped, and the budget is asserted as usual;
* CI never skips, because a CI runner's load is not the runner's own doing
  and a skipped lane is a lane that reports nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tests import _host_load  # noqa: E402


def test_a_loaded_host_is_skipped() -> None:
    """A host over the limit is skipped, and says so."""
    reason = _host_load.skip_reason(load=55.0, limit=20.0, env={})

    assert reason is not None, "a host at load 55 over a limit of 20 must be skipped"
    assert reason.startswith("skipped: host load")


def test_a_calm_host_is_not_skipped() -> None:
    """A host under the limit is measured as usual."""
    assert _host_load.skip_reason(load=3.0, limit=20.0, env={}) is None


def test_the_limit_itself_is_already_too_much() -> None:
    """The comparison is ``>=``: a load sitting exactly on the limit skips."""
    reason = _host_load.skip_reason(load=20.0, limit=20.0, env={})

    assert reason == "skipped: host load 20.0 >= 20"


def test_ci_disables_the_gate() -> None:
    """CI never skips -- a loaded CI runner is not a failed assertion."""
    assert _host_load.skip_reason(load=55.0, limit=20.0, env={"CI": "true"}) is None


def test_a_bad_limit_falls_back_to_20() -> None:
    """A limit that is not a number falls back to the default, not to zero."""
    assert _host_load.host_load_limit(env={_host_load.HOST_LOAD_LIMIT_ENV: "abc"}) == 20


def test_the_message_names_the_load_and_the_limit() -> None:
    """The reason carries both numbers, so the log can be read without a rerun."""
    reason = _host_load.skip_reason(load=55.0, limit=20.0, env={})

    assert reason == "skipped: host load 55.0 >= 20"


def test_ci_is_recognised_in_any_of_its_usual_spellings() -> None:
    """``1`` / ``true`` / ``yes``, in any case, all mean "a CI runner"."""
    for value in ("1", "true", "TRUE", "True", "yes", "YES", "Yes"):
        assert _host_load.ci_requested(env={"CI": value}) is True, value


def test_a_value_that_is_not_ci_is_not_ci() -> None:
    """Anything else -- including ``CI=false`` -- leaves the gate armed."""
    for env in ({}, {"CI": ""}, {"CI": "false"}, {"CI": "0"}, {"CI": "no"}):
        assert _host_load.ci_requested(env=env) is False, env


def test_a_limit_that_is_not_positive_falls_back_to_20() -> None:
    """Zero or a negative limit would disable the gate by accident."""
    for value in ("0", "-1"):
        assert (
            _host_load.host_load_limit(env={_host_load.HOST_LOAD_LIMIT_ENV: value}) == 20
        )


def test_a_readable_limit_is_honoured() -> None:
    """A limit that makes sense is used as given."""
    assert (
        _host_load.host_load_limit(env={_host_load.HOST_LOAD_LIMIT_ENV: "3.5"}) == 3.5
    )


def test_the_limit_comes_from_the_environment_by_default(monkeypatch) -> None:
    """With no environment injected, the real environment is the source."""
    monkeypatch.setenv(_host_load.HOST_LOAD_LIMIT_ENV, "7.5")

    assert _host_load.host_load_limit() == 7.5


def test_a_bad_limit_in_the_environment_falls_back_to_20(monkeypatch) -> None:
    """The environment is read the same way whether it is injected or real."""
    monkeypatch.setenv(_host_load.HOST_LOAD_LIMIT_ENV, "abc")

    assert _host_load.host_load_limit() == 20


def test_a_platform_without_load_average_reports_no_load(monkeypatch) -> None:
    """A platform that cannot measure the load reports zero, and is measured."""

    def _unsupported() -> tuple[float, float, float]:
        raise OSError("getloadavg is not supported on this platform")

    monkeypatch.setattr(os, "getloadavg", _unsupported)

    assert _host_load.current_host_load() == 0.0
    assert _host_load.skip_reason(limit=20.0, env={}) is None


def test_the_skip_reason_reads_the_host_it_is_running_on(monkeypatch) -> None:
    """With no load injected, the gate reads the host it is running on."""
    monkeypatch.setattr(os, "getloadavg", lambda: (12.5, 0.5, 0.5))
    assert _host_load.skip_reason(limit=20.0, env={}) is None

    monkeypatch.setattr(os, "getloadavg", lambda: (33.25, 0.5, 0.5))
    assert (
        _host_load.skip_reason(limit=20.0, env={})
        == "skipped: host load 33.2 >= 20"
    )