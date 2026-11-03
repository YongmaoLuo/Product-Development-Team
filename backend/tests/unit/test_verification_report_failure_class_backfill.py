"""TDD tests for the 2026-11-03 report-side failure-class backfill.

Background (2026-11-03 incident, "infra vs defect"):

  The Phase 3 judgment LLM writes ``verification_report.json`` from a
  fixed output schema — ``id`` / ``status`` / ``actual_result`` /
  ``evidence`` (see the judgment prompt in ``backend/prompts.py``).
  That schema has no room for a failure class, so every report entry
  came out **unclassified**.

  Downstream, ``verification.failure_history.failure_class_of()`` reads
  a record that names no class as ``deliverable_defect`` — the class
  that counts, the one that generates a repair task.  So the nine
  infrastructure failures of the incident plan (the environment was
  down; the product was never asked) were filed as broken
  deliverables.

  The executor already knows the difference: its projection
  (``Orchestrator._verdict_to_result``) carries ``failure_class`` and
  ``failure_class_source``.  ``Orchestrator._backfill_failure_classes``
  is what copies that classification into the report before the report
  is persisted.

  Both directions are pinned here, because the incident had both:

    * the executor says ``infra``, the report says nothing → the report
      must end up saying ``infra`` (the missing-class case);
    * the executor says ``deliverable_defect``, the judgment LLM says
      ``infra`` → the report must end up saying ``deliverable_defect``
      (the LLM's guess must not win; the executor is the authority).

  And the two edges that make the rule safe:

    * a **passed** point carries no class — a green verification point
      must never be reported as a broken deliverable (the same
      semantics ``Verdict.__post_init__`` gives a passed verdict);
    * a point the executor has no verdict for is left **untouched** —
      the backfill fills in what the executor said, it does not
      invent a verdict for a point the executor never judged.
"""

import inspect
import json
import sys
from pathlib import Path

from verification_agent import Orchestrator
from verification_subagent import (
    DEFAULT_FAILURE_CLASS,
    DEFAULT_FAILURE_CLASS_SOURCE,
    DETERMINISTIC_FAILURE_CLASS_SOURCE,
    INFRA_FAILURE_CLASS,
)

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification import failure_history  # noqa: E402
from verification.failure_triage import (  # noqa: E402
    Disposition,
    disposition_for,
)

#: The verbatim text of an attempt that was cancelled while its dispatch
#: was in flight (taken from the attempt log of the 2026-10-24 round).
#: Both markers have to be there — "verification cancelled" alone may be
#: a remark about the deliverable, "dispatch was in flight" alone may be
#: a retelling — so the two together are what says *this attempt never
#: reached a verdict*.
CANCELLED_TEXT = (
    "coding tool call failed: verification cancelled for plan "
    "'20261021-PDT-HITL-1-infra-cri' while the dispatch was in flight"
)


def _backfill(report, execution_results):
    """Call the backfill the way the report path calls it."""
    return Orchestrator._backfill_failure_classes(report, execution_results)


# ---------------------------------------------------------------------------
# 1. The missing class — the incident's own shape.
# ---------------------------------------------------------------------------


def test_backfills_infra_from_executor():
    """Executor says infra, report says nothing → the report says infra.

    This is the incident: the executor classified the failure as
    infrastructure (the environment was the problem), the judgment LLM
    had no schema slot for the class, and the report landed on disk
    unclassified — which the history layer reads as
    ``deliverable_defect``.
    """
    report = {"verification_results": [{"id": "VP-006", "status": "FAILED"}]}
    execs = {
        "execution_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "failure_class": INFRA_FAILURE_CLASS,
                "failure_class_source": DETERMINISTIC_FAILURE_CLASS_SOURCE,
            }
        ]
    }

    out = _backfill(report, execs)

    entry = out["verification_results"][0]
    assert entry["failure_class"] == INFRA_FAILURE_CLASS, entry
    assert entry["failure_class_source"] == DETERMINISTIC_FAILURE_CLASS_SOURCE, entry


# ---------------------------------------------------------------------------
# 2. The executor overrides the judgment LLM — in both directions.
# ---------------------------------------------------------------------------


def test_executor_overrides_llm_deliverable_defect():
    """The LLM called it a deliverable defect, the executor says infra.

    The executor's verdict is the authority on **what happened**: it
    ran the command and knows whether the environment or the product
    was at fault.  The judgment LLM only saw a summary.  Its guess
    must not overwrite the executor's classification.
    """
    report = {
        "verification_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "failure_class": DEFAULT_FAILURE_CLASS,
                "failure_class_source": DEFAULT_FAILURE_CLASS_SOURCE,
            }
        ]
    }
    execs = {
        "execution_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "failure_class": INFRA_FAILURE_CLASS,
                "failure_class_source": DETERMINISTIC_FAILURE_CLASS_SOURCE,
            }
        ]
    }

    out = _backfill(report, execs)

    entry = out["verification_results"][0]
    assert entry["failure_class"] == INFRA_FAILURE_CLASS, entry
    assert entry["failure_class_source"] == DETERMINISTIC_FAILURE_CLASS_SOURCE, entry


def test_llm_infra_on_real_defect_is_not_adopted():
    """The LLM called it infra, the executor says deliverable_defect.

    The mirror image of the case above, and the one that keeps the
    backfill from becoming a second guesser: a real product defect
    must not be filed as an environment problem, because an
    ``infra`` class does **not** generate a repair task — the point
    would be retried forever while the product stayed broken.
    """
    report = {
        "verification_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "failure_class": INFRA_FAILURE_CLASS,
                "failure_class_source": DETERMINISTIC_FAILURE_CLASS_SOURCE,
            }
        ]
    }
    execs = {
        "execution_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "failure_class": DEFAULT_FAILURE_CLASS,
                "failure_class_source": DEFAULT_FAILURE_CLASS_SOURCE,
            }
        ]
    }

    out = _backfill(report, execs)

    entry = out["verification_results"][0]
    assert entry["failure_class"] == DEFAULT_FAILURE_CLASS, entry
    assert entry["failure_class_source"] == DEFAULT_FAILURE_CLASS_SOURCE, entry


# ---------------------------------------------------------------------------
# 3. The edges.
# ---------------------------------------------------------------------------


def test_passed_entry_keeps_empty_class():
    """A passed point carries no class.

    The same semantics ``Verdict.__post_init__`` gives a passed
    verdict: a point that passed has no failure to classify, and
    reading the empty class as "default it" would report every green
    verification point as a broken deliverable.
    """
    report = {"verification_results": [{"id": "VP-006", "status": "PASSED"}]}
    execs = {
        "execution_results": [
            {
                "id": "VP-006",
                "status": "PASSED",
                "failure_class": "",
                "failure_class_source": DEFAULT_FAILURE_CLASS_SOURCE,
            }
        ]
    }

    out = _backfill(report, execs)

    entry = out["verification_results"][0]
    assert entry.get("failure_class", "") == "", entry


def test_passed_entry_drops_a_class_the_llm_made_up():
    """A class the judgment LLM invented on a passed point is cleared.

    The LLM's guess has no authority at all, and a passed point has no
    failure to classify — so the guess is dropped rather than kept.
    """
    report = {
        "verification_results": [
            {
                "id": "VP-006",
                "status": "PASSED",
                "failure_class": INFRA_FAILURE_CLASS,
                "failure_class_source": DETERMINISTIC_FAILURE_CLASS_SOURCE,
            }
        ]
    }
    execs = {
        "execution_results": [
            {"id": "VP-006", "status": "PASSED", "failure_class": ""},
        ]
    }

    out = _backfill(report, execs)

    entry = out["verification_results"][0]
    assert entry.get("failure_class", "") == "", entry


def test_missing_executor_entry_untouched():
    """A point the executor never judged is left exactly as it was.

    The backfill copies what the executor said; it does not invent a
    verdict for a point the executor has no verdict for.  An entry the
    executor does not know keeps every field it came with.
    """
    report = {
        "verification_results": [
            {"id": "VP-006", "status": "FAILED"},
            {"id": "VP-009", "status": "FAILED"},
        ]
    }
    execs = {
        "execution_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "failure_class": INFRA_FAILURE_CLASS,
                "failure_class_source": DETERMINISTIC_FAILURE_CLASS_SOURCE,
            }
        ]
    }

    out = _backfill(report, execs)

    assert out["verification_results"][1] == {"id": "VP-009", "status": "FAILED"}


def test_unclassified_executor_entry_states_no_class():
    """The executor judged the point but named no class → the report states none.

    A class nobody stated is *assumed* by the consumer, and the
    assumption is ``deliverable_defect`` — but the consumer may also
    have a **rule** that says otherwise, and it only gets to run that
    rule on a record that names no class
    (:func:`verification.failure_history.classify_failure` returns a
    stated class as it stands and never asks the rules).

    So the default must not be **written into the report** as if the
    executor had said it: writing it would silence the rule and turn
    an attempt that was cancelled in flight into a broken deliverable.
    The default still applies at the consumer when no rule fires — it
    is just not the report's to claim.
    """
    report = {"verification_results": [{"id": "VP-006", "status": "FAILED"}]}
    execs = {
        "execution_results": [
            {"id": "VP-006", "status": "FAILED", "failure_class": ""},
        ]
    }

    out = _backfill(report, execs)

    entry = out["verification_results"][0]
    assert entry.get("failure_class", "") == "", entry
    assert entry["failure_class_source"] == DEFAULT_FAILURE_CLASS_SOURCE, entry
    # The consumer still reads it as the default — that is the default's
    # job, not the report's.
    assert failure_history.failure_class_of(entry) == DEFAULT_FAILURE_CLASS, entry


def test_cancelled_dispatch_is_not_turned_into_a_deliverable_defect():
    """An attempt cancelled in flight stays ``infra`` after the backfill.

    The negative control that matters most: the attempt never reached a
    verdict, so nothing about the deliverable is known, and the rules
    say ``infra`` — *retry the verification*, do not repair the
    product.  The repair gate reads that disposition
    (``retry_not_counted``): a ``deliverable_defect`` would be
    ``retry_counted``, the gate would let it through, and a repair task
    would be raised against a product that was never asked.

    The executor has no class for this attempt (it never judged it), so
    the backfill must not state one — a stated class is taken at face
    value and the rules never run.
    """
    report = {
        "verification_results": [
            {
                "id": "VP-031",
                "status": "FAILED",
                "actual_result": CANCELLED_TEXT,
            }
        ]
    }
    execs = {
        "execution_results": [
            {
                "id": "VP-031",
                "status": "FAILED",
                "actual_result": CANCELLED_TEXT,
                "evidence": CANCELLED_TEXT,
            }
        ]
    }

    out = _backfill(report, execs)

    entry = out["verification_results"][0]
    assert entry.get("failure_class", "") == "", entry
    assert failure_history.failure_class_of(entry) == INFRA_FAILURE_CLASS, entry
    assert disposition_for(failure_history.failure_class_of(entry)) == (
        Disposition.RETRY_NOT_COUNTED
    ), entry


def test_llm_guess_is_dropped_when_the_executor_classified_nothing():
    """The judgment LLM's guess never wins — not even as a leftover.

    The executor is the authority on what happened, and here its answer
    is "I did not classify this".  A guess the LLM wrote must not
    survive that, because a stated class is taken at face value by the
    consumer and silences the rules — the guess would decide the
    failure's nature all by itself.
    """
    report = {
        "verification_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "failure_class": INFRA_FAILURE_CLASS,
                "failure_class_source": DETERMINISTIC_FAILURE_CLASS_SOURCE,
            }
        ]
    }
    execs = {
        "execution_results": [
            {"id": "VP-006", "status": "FAILED", "actual_result": "boom"},
        ]
    }

    out = _backfill(report, execs)

    entry = out["verification_results"][0]
    assert entry.get("failure_class", "") == "", entry


def test_missing_verification_results_is_returned_unchanged():
    """A report without ``verification_results`` is returned as it came."""
    report = {"overall_status": "PASSED"}

    out = _backfill(report, {"execution_results": []})

    assert out == {"overall_status": "PASSED"}


def test_verification_results_not_a_list_is_returned_unchanged():
    """A malformed ``verification_results`` is returned as it came."""
    report = {"verification_results": "not-a-list"}

    out = _backfill(report, {"execution_results": []})

    assert out == {"verification_results": "not-a-list"}


def test_backfill_mutates_in_place_and_returns_the_same_dict():
    """The report dict is updated in place and handed back."""
    report = {"verification_results": [{"id": "VP-006", "status": "FAILED"}]}
    execs = {
        "execution_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "failure_class": INFRA_FAILURE_CLASS,
                "failure_class_source": DETERMINISTIC_FAILURE_CLASS_SOURCE,
            }
        ]
    }

    out = _backfill(report, execs)

    assert out is report
    assert report["verification_results"][0]["failure_class"] == INFRA_FAILURE_CLASS


# ---------------------------------------------------------------------------
# 4. The call site — the backfill has to run on the report path.
# ---------------------------------------------------------------------------


def test_call_site_runs_before_the_environment_blocked_exclusion():
    """The backfill runs on the report path, before the exclusion.

    The exclusion is the last word on the report's entries, so the
    classification has to be in place before it runs: the blocked /
    invalid-env annotation rewrites the entries it touches, and a
    classification written after it would miss them.
    """
    source = inspect.getsource(Orchestrator.generate_verification_report)

    backfill_at = source.find("_backfill_failure_classes")
    exclude_at = source.find("_exclude_environment_blocked_from_failures")
    assert backfill_at != -1, source
    assert exclude_at != -1, source
    assert backfill_at < exclude_at, source


# ---------------------------------------------------------------------------
# 5. The whole path — the report that lands on disk.
# ---------------------------------------------------------------------------


class _ReportingTool:
    """A ``coding_tool`` stand-in whose judgment answers with a report.

    The judgment LLM writes the report from a schema that has **no
    slot** for a failure class, so the report it answers with carries
    none — exactly the shape the incident produced.
    """

    def __init__(self, report: dict):
        self.report = report
        self.calls = 0

    def query_json(self, *args, **kwargs):
        self.calls += 1
        return json.loads(json.dumps(self.report))


def _make_agent(tmp_path: Path, tool=None):
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    return Orchestrator(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=tool,
    )


def test_the_report_on_disk_carries_the_executors_class(tmp_path):
    """The incident's own path: the persisted report says ``infra``.

    The judgment LLM's schema has no room for the class, the executor
    knows the failure was the environment's, and the file the
    downstream layer reads is ``verification_report.json`` — so that
    file has to say ``infra``, or the history layer files the failure
    as ``deliverable_defect`` and sends a repair task after a product
    that was never asked.
    """
    tool = _ReportingTool({
        "overall_status": "FAILED",
        "summary": "the judgment LLM's own words",
        "verification_results": [
            {"id": "VP-006", "status": "FAILED", "actual_result": "connection refused"},
        ],
        "requirement_deviations": [],
    })
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "actual_result": "service_unreachable: connection refused",
                "evidence": "URLError: [Errno 61] Connection refused",
                "failure_class": INFRA_FAILURE_CLASS,
                "failure_class_source": DETERMINISTIC_FAILURE_CLASS_SOURCE,
            }
        ],
    })

    entry = report["verification_results"][0]
    assert entry["failure_class"] == INFRA_FAILURE_CLASS, entry
    assert entry["failure_class_source"] == DETERMINISTIC_FAILURE_CLASS_SOURCE, entry

    on_disk = json.loads(
        (tmp_path / "plan" / "verification_report.json").read_text(encoding="utf-8")
    )
    disk_entry = on_disk["verification_results"][0]
    assert disk_entry["failure_class"] == INFRA_FAILURE_CLASS, disk_entry
    assert disk_entry["failure_class_source"] == DETERMINISTIC_FAILURE_CLASS_SOURCE, disk_entry
