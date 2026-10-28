"""RED regression tests for the record phase's fixture-mismatch event.

Background
----------
The 2026-10-26 verification round (round 3) recorded a data-plane
fingerprint that said two contradictory things at once::

    port 59028, pid 14828, routing_rows=11, expected_rows=49

``routing_rows=11`` is what the instance answered on ``/health/data``;
``expected_rows=49`` is how many ``plan_routing`` rows the **agent's
own** state database held when the fingerprint was taken.  The two
numbers come from two different databases -- the service under test
reads its own fixture (11 rows), the agent reads its own (49) -- so
they can never be equal.

The record phase stored that contradiction as-is: it never asked
whether the instance it had just started was serving *this* round's
fixture.  The price was paid later, and paid repeatedly: the same
defect was rediscovered by every ``api_test`` VP, once per VP per
round, as an opaque ``data_plane_mismatch`` block -- 11 VPs, 11
blocks, and nowhere a statement of what was actually wrong.

What these tests pin
--------------------
The record phase has to *say so* once, when it takes the fingerprint:

  1. a fingerprint whose self-reported count disagrees with the
     fixture's count produces **one** ``data_plane_fixture_mismatch``
     event, carrying the resolved database path and both counts;
  2. the mismatching fingerprint stops carrying the foreign
     expectation (its ``expected_rows`` is emptied), so the round
     falls back to "recorded value vs observed value" instead of
     blocking every VP on a number that was never the instance's;
  3. the fixture's count is read from the **resolved** database path,
     so the event names the database it actually read;
  4. one event per mismatching service -- not one per round, and not
     one per VP.

And the boundary conditions a fix must not break (these hold on the
base point already, and have to hold afterwards):

  5. an instance that *does* serve the fixture produces no event, and
     the fingerprint keeps the count it was taken with;
  6. a fixture that cannot be read produces no event: an unknown
     count carries no verdict, and the fingerprint is kept as it was.

Tests 1-4 are the reproduction: on the base point they fail, because
the record phase stores the contradiction silently and reads the
fixture's count without a path.

Why the reproduction carries a marker
-------------------------------------
The reproduction has to fail on the base point -- that failure is the
evidence this file exists for -- but a test command that exits
non-zero is a **failed task**, and a failed task blocks every task
that depends on it: ``is_dependency_ready`` unlocks a dependent only
on ``completed`` or ``skipped``.  The fix (R2) is the next task and it
depends on this one, so a permanently red command here would strand
the whole chain behind it.  (The sibling reproduction in
``test_data_plane_fingerprint_identity.py`` carries the same marker,
for the same reason.)

The marker is therefore a ratchet rather than a waiver.  It applies
only while the record phase still fails to report the mismatch, and
the moment an implementation reaches the record phase the marker
stops applying and the assertions below become hard ones again.  An
implementation that reports the mismatch but computes the wrong
payload therefore fails these tests outright instead of being
absorbed by the marker.

To read the raw red on the base point, run the module with the
markers switched off::

    pytest tests/unit/test_data_plane_record_phase_mismatch.py --runxfail -v

The event contract (the payload the fix has to produce)::

    ("__preflight__", "data_plane_fixture_mismatch", {
        "service":       "<the service's name>",
        "db_path":       "<the resolved state database path>",
        "expected_rows": <rows the fixture holds>,
        "reported_rows": <rows the instance answered with>,
    })
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import config_paths  # noqa: E402
import service_freshness  # noqa: E402
import verification_agent  # noqa: E402
from service_freshness import DataPlaneFingerprint  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402


#: The event the record phase writes when the instance it just started
#: is not serving this round's fixture.
MISMATCH_EVENT = "data_plane_fixture_mismatch"

#: The vp id the record phase's own events carry.
PREFLIGHT = "__preflight__"

#: The event the record phase writes for every fingerprint it takes.
#: It is not a diagnosis of anything -- it is the audit trail -- so a
#: record phase that writes *nothing but* this event has not learned
#: to report the mismatch.
FINGERPRINTS_EVENT = "data_plane_fingerprints"


def _agent(events: list, declarations: dict):
    """An ``Orchestrator`` with only what the record phase touches.

    ``__init__`` is skipped on purpose: it wires a coding tool, a
    persistence manager and a venv ``PATH`` prefix, none of which the
    record phase needs -- and the persistence manager is exactly what
    ``_safe_log_freshness_event`` writes through, so it is stubbed here
    and the events are collected in a list instead.
    """
    agent = Orchestrator.__new__(Orchestrator)
    agent._service_declarations = {}
    agent._data_plane_fingerprints = {}
    agent._effective_declarations = lambda: {
        name: SimpleNamespace(port=port)
        for name, port in declarations.items()
    }
    agent._record_service_instance_identity = lambda *a, **k: None
    agent._safe_log_freshness_event = (
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        )
    )
    return agent


def _probe_returning(routing_rows, fixture_rows):
    """A ``probe_data_plane`` that answers with one fixed fingerprint.

    ``routing_rows`` is what the instance answered on ``/health/data``;
    ``fixture_rows`` is how many rows the fixture database holds -- the
    count the record phase is handed, which the fingerprint carries as
    its ``expected_rows``, exactly as the real probe does.
    """
    def _probe(port, expected_rows=None):
        return DataPlaneFingerprint(
            port=port, pid=14828, routing_rows=routing_rows,
            expected_rows=fixture_rows, status="ok",
        )
    return _probe


def _mismatch_events(events: list) -> list:
    """The fixture-mismatch events among *events*.

    The record phase also writes its usual ``data_plane_fingerprints``
    audit event; the contract under test is the mismatch event, so the
    assertions below look at those alone.
    """
    return [event for event in events if event[1] == MISMATCH_EVENT]


# ---------------------------------------------------------------------------
# The ratchet -- is R2 (the record phase reporting the mismatch) in yet?
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _reports_a_fixture_mismatch() -> bool:
    """Does the record phase report a fixture mismatch at all?

    Runs the record phase once, in process, with the same stubs the
    tests below use, and looks at what it wrote.  The question here is
    not "is the payload right" -- the assertions answer that -- but
    "does the record phase say anything at all when the instance does
    not serve the fixture".  An implementation that reports the
    mismatch under a *different* name still counts: the assertions
    have to fail loudly on it, not be absorbed by the marker.

    Never raises: a probe that blows up is a probe that has learned
    nothing, and "has learned nothing" is the safe answer here.
    """
    events: list = []
    agent = _agent(events, {"svc": 59028})

    original_expected = service_freshness.expected_routing_rows
    original_probe = service_freshness.probe_data_plane
    original_resolve = config_paths.resolve_state_db_path
    try:
        service_freshness.expected_routing_rows = lambda db_path=None: 49
        service_freshness.probe_data_plane = _probe_returning(11, 49)
        config_paths.resolve_state_db_path = lambda: Path("fixture.db")
        agent._capture_data_plane_fingerprints()
    except Exception:  # noqa: BLE001 - a probe never raises
        return False
    finally:
        service_freshness.expected_routing_rows = original_expected
        service_freshness.probe_data_plane = original_probe
        config_paths.resolve_state_db_path = original_resolve
    return any(event[1] != FINGERPRINTS_EVENT for event in events)


@lru_cache(maxsize=1)
def _mentions_the_mismatch_event() -> bool:
    """Does the record phase's module name the mismatch event at all?

    A structural probe, and the second half of the ratchet.  The
    behavioural question -- "does the record phase report the mismatch
    now?" -- cannot tell "R2 is not implemented" from "R2 is
    implemented wrongly", and the marker must not absorb the second
    one.  Naming the event in the module's own source is the trace an
    implementation leaves; an implementation that reports the
    mismatch only through a logger still leaves it.
    """
    try:
        source = Path(verification_agent.__file__).read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - defensive
        return False
    return MISMATCH_EVENT in source


def _r2_is_missing() -> bool:
    """Is the record phase's fixture-mismatch report still missing?

    True only while **both** probes agree that R2 has not landed:

      * behaviour -- the record phase still writes nothing but its
        audit event, so it has not learned to report the mismatch;
      * structure -- the record phase's module does not name the
        event at all.

    A correct implementation satisfies the first probe and the marker
    switches itself off; an implementation that reports the mismatch
    but gets the payload wrong satisfies the first probe too, so the
    hard assertions below fail it outright instead of hiding behind
    the marker.
    """
    if _reports_a_fixture_mismatch():
        return False
    return not _mentions_the_mismatch_event()


#: The marker the reproduction carries until R2 lands.  ``strict`` so
#: that an implementation which lands while the marker still applies
#: fails the test (XPASS) instead of being read as success.
_not_implemented_yet = pytest.mark.xfail(
    strict=True,
    reason=(
        "R2 has not landed yet: the record phase stores the mismatching "
        "fingerprint silently instead of reporting one fixture-mismatch "
        "event"
    ),
    condition=_r2_is_missing(),
)


# ---------------------------------------------------------------------------
# The reproduction
# ---------------------------------------------------------------------------


@_not_implemented_yet
def test_a_wrong_fixture_is_reported_once_at_record_time(
    tmp_path: Path, monkeypatch,
):
    """The instance is not serving the fixture -- say so, once, up front.

    The recorded fingerprint contradicts itself: the instance answered
    with 11 rows, the fixture holds 49.  The record phase has to say
    that out loud -- one event, naming the database it read and both
    counts -- instead of letting every VP of the round rediscover it.
    """
    fixture_db = tmp_path / "fixture.db"
    events: list = []
    agent = _agent(events, {"svc": 59028})

    monkeypatch.setattr(
        config_paths, "resolve_state_db_path", lambda: fixture_db,
    )
    monkeypatch.setattr(
        service_freshness, "expected_routing_rows",
        lambda db_path=None: 49,
    )
    monkeypatch.setattr(
        service_freshness, "probe_data_plane", _probe_returning(11, 49),
    )

    agent._capture_data_plane_fingerprints()

    assert _mismatch_events(events) == [(
        PREFLIGHT,
        MISMATCH_EVENT,
        {
            "service": "svc",
            "db_path": str(fixture_db),
            "expected_rows": 49,
            "reported_rows": 11,
        },
    )]


@_not_implemented_yet
def test_the_mismatching_fingerprint_falls_back_to_recorded_rows(
    tmp_path: Path, monkeypatch,
):
    """The mismatching fingerprint stops carrying the foreign count.

    ``expected_rows`` is the count of a database that is not the one
    the instance serves, so keeping it on the fingerprint means every
    later comparison asks a question whose answer is always "no".  The
    fingerprint has to fall back to what the instance itself reported:
    the recorded count against the observed one.
    """
    fixture_db = tmp_path / "fixture.db"
    events: list = []
    agent = _agent(events, {"svc": 59028})

    monkeypatch.setattr(
        config_paths, "resolve_state_db_path", lambda: fixture_db,
    )
    monkeypatch.setattr(
        service_freshness, "expected_routing_rows",
        lambda db_path=None: 49,
    )
    monkeypatch.setattr(
        service_freshness, "probe_data_plane", _probe_returning(11, 49),
    )

    fingerprints = agent._capture_data_plane_fingerprints()

    fingerprint = fingerprints["svc"]
    assert fingerprint.expected_rows is None
    observed = DataPlaneFingerprint(
        port=59028, pid=14828, routing_rows=11, expected_rows=49,
        status="ok",
    )
    assert fingerprint.matches(observed) is True


@_not_implemented_yet
def test_the_fixture_count_is_read_from_the_resolved_database(
    tmp_path: Path, monkeypatch,
):
    """The count is read from the database the round actually resolved.

    The count is only meaningful together with the database it came
    from: "49 rows" means nothing without "in *this* file".  The
    resolver is the only thing that knows which file that is, so the
    record phase has to ask it -- and hand the resolved path to the
    reader, so the event can name it.
    """
    fixture_db = tmp_path / "fixture.db"
    events: list = []
    agent = _agent(events, {"svc": 59028})
    seen: list = []

    def _recording_expected(db_path=None):
        seen.append(db_path)
        return 11

    monkeypatch.setattr(
        config_paths, "resolve_state_db_path", lambda: fixture_db,
    )
    monkeypatch.setattr(
        service_freshness, "expected_routing_rows", _recording_expected,
    )
    monkeypatch.setattr(
        service_freshness, "probe_data_plane", _probe_returning(11, 11),
    )

    agent._capture_data_plane_fingerprints()

    assert seen, "the fixture's row count was never read"
    assert seen[0] is not None, (
        "the row count was read without the database it belongs to"
    )
    assert Path(seen[0]) == fixture_db


@_not_implemented_yet
def test_each_mismatching_service_gets_its_own_event(
    tmp_path: Path, monkeypatch,
):
    """Two wrong services are two events -- one per service.

    The event names the service, so one event per service is what makes
    it actionable.  Collapsing them into a single round-level event
    would lose the very thing the event is for.
    """
    fixture_db = tmp_path / "fixture.db"
    events: list = []
    agent = _agent(events, {"svc_a": 59028, "svc_b": 59029})

    monkeypatch.setattr(
        config_paths, "resolve_state_db_path", lambda: fixture_db,
    )
    monkeypatch.setattr(
        service_freshness, "expected_routing_rows",
        lambda db_path=None: 49,
    )
    monkeypatch.setattr(
        service_freshness, "probe_data_plane", _probe_returning(11, 49),
    )

    agent._capture_data_plane_fingerprints()

    mismatches = _mismatch_events(events)
    assert len(mismatches) == 2, mismatches
    assert {event[2]["service"] for event in mismatches} == {
        "svc_a", "svc_b",
    }
    for vp_id, event_type, data in mismatches:
        assert vp_id == PREFLIGHT
        assert event_type == MISMATCH_EVENT
        assert data["db_path"] == str(fixture_db)
        assert data["expected_rows"] == 49
        assert data["reported_rows"] == 11


# ---------------------------------------------------------------------------
# The boundary conditions -- green on the base point, green afterwards
# ---------------------------------------------------------------------------


def test_a_fixture_shaped_instance_produces_no_mismatch_event(
    tmp_path: Path, monkeypatch,
):
    """An instance that serves the fixture is not reported at all.

    The mismatch event is a diagnosis, not a ritual: when the
    self-reported count and the fixture's count agree, there is nothing
    to report, and the fingerprint keeps the count it was taken with.
    """
    fixture_db = tmp_path / "fixture.db"
    events: list = []
    agent = _agent(events, {"svc": 59028})

    monkeypatch.setattr(
        config_paths, "resolve_state_db_path", lambda: fixture_db,
    )
    monkeypatch.setattr(
        service_freshness, "expected_routing_rows",
        lambda db_path=None: 11,
    )
    monkeypatch.setattr(
        service_freshness, "probe_data_plane", _probe_returning(11, 11),
    )

    fingerprints = agent._capture_data_plane_fingerprints()

    assert _mismatch_events(events) == []
    assert fingerprints["svc"].expected_rows == 11


def test_an_unreadable_fixture_produces_no_mismatch_event(
    tmp_path: Path, monkeypatch,
):
    """A fixture that cannot be read carries no verdict.

    ``expected_rows=None`` means "unknown", not "wrong": a database
    that could not be opened says nothing about the instance.  The
    fingerprint is kept as it was, and no event is written.
    """
    fixture_db = tmp_path / "fixture.db"
    events: list = []
    agent = _agent(events, {"svc": 59028})

    monkeypatch.setattr(
        config_paths, "resolve_state_db_path", lambda: fixture_db,
    )
    monkeypatch.setattr(
        service_freshness, "expected_routing_rows",
        lambda db_path=None: None,
    )
    monkeypatch.setattr(
        service_freshness, "probe_data_plane", _probe_returning(11, None),
    )

    fingerprints = agent._capture_data_plane_fingerprints()

    assert _mismatch_events(events) == []
    assert fingerprints["svc"].expected_rows is None
    assert fingerprints["svc"].routing_rows == 11
