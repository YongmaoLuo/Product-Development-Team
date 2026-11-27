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

import pytest

from verification_agent import Orchestrator
from verification_subagent import (
    DEFAULT_FAILURE_CLASS,
    DEFAULT_FAILURE_CLASS_SOURCE,
    DETERMINISTIC_FAILURE_CLASS_SOURCE,
    INFRA_FAILURE_CLASS,
    VERDICT_UNKNOWN,
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


# ---------------------------------------------------------------------------
# 6. "本轮没拿到结论" is not a code failure (2026-11-18, VP-032)
# ---------------------------------------------------------------------------
#
# 2026-11-18 incident (VP-032): the round produced
# **no conclusion at all** — the progress log had no pytest summary line
# and no exit status could be derived — and the report filed that round
# as ``FAILED`` with the *defaulted* class ``deliverable_defect``. That
# class buys a repair task, so the plan sent a repair after a deliverable
# nobody had found anything wrong with.
#
# The vocabulary already existed: ``UNKNOWN`` means "this round got no
# verdict" and it enters **no** bucket (see ``verification_buckets``).
# What was missing was the report layer recognising its own sentence —
# "未产生任何结论 / no conclusion" — and giving the entry that
# identity instead of inventing a failure.
#
# Two edges keep the rule from washing real defects:
#
#   * an entry that states a deliverable defect is left alone;
#   * an entry the **executor** concluded on is left alone — the ledger
#     is the authority on "did this run produce a verdict".

#: The judgment layer's own words, taken from the 2026-11-18 round-30
#: report for VP-032.
NO_CONCLUSION_ACTUAL = (
    "未产生任何结论：进度日志尾部没有 pytest 摘要行、无退出码可推导；"
    "抓到的内容是另一个 settings 页验证的无关 checkpoint 行。"
)

#: …and its evidence, which says the same thing twice — and, crucially,
#: says it WITHOUT claiming a defect ("依规不登记为交付物缺陷" is a
#: statement that this is *not* a deliverable defect).
NO_CONCLUSION_EVIDENCE = (
    "No pytest summary line present in /tmp/vp_VP-032_progress.log tail; "
    "判定依据 details 为空、raw 为空数组；依规不登记为交付物缺陷，但须重跑。"
)

#: The executor's own ledger text for the same round — it reached no
#: conclusion either.
NO_CONCLUSION_LEDGER = (
    "pytest did not run to completion for VP-032, so no exit status can "
    "be derived for VP-032"
)


def _no_conclusion_report() -> dict:
    """The judgment layer's report for the 2026-11-18 round."""
    return {
        "overall_status": "FAILED",
        "summary": "本轮 VP-032 没有拿到结论",
        "verification_results": [
            {
                "id": "VP-032",
                "status": "FAILED",
                "actual_result": NO_CONCLUSION_ACTUAL,
                "evidence": NO_CONCLUSION_EVIDENCE,
            },
        ],
        "requirement_deviations": [
            {
                "verification_point_id": "VP-032",
                "requirement": "合并完整性",
                "description": "没有拿到结论",
            },
        ],
    }


def test_no_conclusion_is_not_recorded_as_failed(tmp_path):
    """A round that reached no conclusion is UNKNOWN, not a code failure.

    ``UNKNOWN`` is the framework's own word for "no verdict was
    produced"; it enters no bucket, so it buys no repair task and it
    does not make the round red.
    """
    agent = _make_agent(tmp_path, _ReportingTool(_no_conclusion_report()))

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [
            {
                "id": "VP-032",
                "status": "FAILED",
                "actual_result": NO_CONCLUSION_LEDGER,
                "evidence": NO_CONCLUSION_EVIDENCE,
                "failure_class": DEFAULT_FAILURE_CLASS,
                "failure_class_source": DEFAULT_FAILURE_CLASS_SOURCE,
            }
        ],
    })

    entry = report["verification_results"][0]
    assert entry["status"] == VERDICT_UNKNOWN, entry
    # 「没拿到结论」是环境/流程的事，不是交付物的账
    assert entry["failure_class"] == INFRA_FAILURE_CLASS, entry
    assert entry["failure_class_source"] == DETERMINISTIC_FAILURE_CLASS_SOURCE, entry
    # 下游读的正是这个函数：它必须读出 infra，否则修复任务照旧会被生成
    assert failure_history.failure_class_of(entry) == INFRA_FAILURE_CLASS, entry
    # 一条「偏离」是对交付物的判决 —— 这一条没有判决可下
    assert report.get("requirement_deviations") == [], report.get(
        "requirement_deviations"
    )


def test_no_conclusion_is_not_recorded_as_failed_without_a_ledger(tmp_path):
    """Same verdict when the executor never judged the point at all.

    This is the LLM-judged methods' shape (code_review / ui_validation):
    the executor has no verdict for them, so the report layer has only
    the report's own sentence to go on.
    """
    agent = _make_agent(tmp_path, _ReportingTool(_no_conclusion_report()))

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [],
    })

    entry = report["verification_results"][0]
    assert entry["status"] == VERDICT_UNKNOWN, entry
    assert entry["failure_class"] == INFRA_FAILURE_CLASS, entry
    assert failure_history.failure_class_of(entry) == INFRA_FAILURE_CLASS, entry


def test_executor_failed_verdict_is_not_downgraded(tmp_path):
    """A conclusion the executor reached is not washed into UNKNOWN.

    The executor ran the command and it came back red — that is a
    verdict about the deliverable, whatever the judgment layer's prose
    says about the round.
    """
    agent = _make_agent(tmp_path, _ReportingTool({
        "overall_status": "FAILED",
        "summary": "s",
        "verification_results": [
            {
                "id": "VP-032",
                "status": "FAILED",
                "actual_result": NO_CONCLUSION_ACTUAL,
                "evidence": NO_CONCLUSION_EVIDENCE,
            },
        ],
        "requirement_deviations": [],
    }))

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [
            {
                "id": "VP-032",
                "status": "FAILED",
                "actual_result": "3 failed, 12 passed — backend/orphan_reaper.py 的 _run_probe 不存在",
                "evidence": "orphan_reaper.py:565 没有 _run_probe",
                "failure_class": DEFAULT_FAILURE_CLASS,
                "failure_class_source": DEFAULT_FAILURE_CLASS_SOURCE,
            }
        ],
    })

    entry = report["verification_results"][0]
    assert entry["status"] == "FAILED", entry
    assert entry["failure_class"] == DEFAULT_FAILURE_CLASS, entry
    assert failure_history.failure_class_of(entry) == DEFAULT_FAILURE_CLASS, entry


def test_defect_claiming_entry_is_not_downgraded(tmp_path):
    """An entry that states a defect is not washed into UNKNOWN either.

    The report may say "no conclusion" **and** name a defect — then it
    did conclude something, and the defect stands.
    """
    agent = _make_agent(tmp_path, _ReportingTool({
        "overall_status": "FAILED",
        "summary": "s",
        "verification_results": [
            {
                "id": "VP-032",
                "status": "FAILED",
                "actual_result": (
                    "交付物缺陷：backend/orphan_reaper.py 缺少 _run_probe，"
                    "两侧意图没有同时保留（未拿到结论的只有引用核对）"
                ),
                "evidence": "orphan_reaper.py:565 没有 _run_probe",
            },
        ],
        "requirement_deviations": [],
    }))

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [],
    })

    entry = report["verification_results"][0]
    assert entry["status"] == "FAILED", entry
    assert failure_history.failure_class_of(entry) == DEFAULT_FAILURE_CLASS, entry


def test_unknown_entry_does_not_defer_downstream(tmp_path):
    """UNKNOWN enters no bucket and does not make the round red.

    The failed bucket is the input of the repair-task generator and of
    the Phase 2 deferral cascade: a point filed as failed there buys a
    repair task and defers the gates behind it. An UNKNOWN entry must
    do neither — the round is **unjudged** (``SKIPPED``), not failed.
    """
    from verification_buckets import bucket_for_status, derive_index_buckets

    agent = _make_agent(tmp_path, _ReportingTool(_no_conclusion_report()))

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [],
    })

    entry = report["verification_results"][0]
    assert entry["status"] == VERDICT_UNKNOWN, entry
    assert bucket_for_status(entry["status"], vp_id="VP-032") is None
    buckets = derive_index_buckets({"VP-032": entry})
    assert buckets["failed_vps"] == [], buckets
    assert buckets["deferred_vps"] == [], buckets
    # 一轮「没判」不是一轮「红」
    assert report["overall_status"] == "SKIPPED", report["overall_status"]


#: Phrasings that pin the negation reading.  ``claims_defect`` says
#: whether the sentence asserts a deliverable defect.
DEFECT_PHRASING_CASES = [
    # a *denial* — the 2026-11-18 evidence, verbatim
    ("依规不登记为交付物缺陷，但须重跑。", False),
    ("这不是交付物缺陷", False),
    ("没有交付物缺陷", False),
    ("并非交付物缺陷", False),
    ("this is not a deliverable defect", False),
    ("there is no deliverable defect", False),
    # …and phrasings that do assert one
    ("交付物缺陷：两侧意图没有同时保留", True),
    ("未能复现该缺陷", True),
    ("存在未修复缺陷", True),
    ("该修复不完整，交付物缺陷仍在", True),
    ("the deliverable defect is real", True),
]


@pytest.mark.parametrize("text,claims_defect", DEFECT_PHRASING_CASES)
def test_a_defect_claim_is_not_downgraded_by_a_nearby_negation(
    text, claims_defect
):
    """A nearby negation must not disarm a real defect claim.

    The restore rule keys on "no conclusion"; it must not also swallow a
    defect that merely *mentions* a negation nearby.  "未能复现该缺陷"
    is a claim, "依规不登记为交付物缺陷" is a denial — reading the two
    the same way would either keep the 2026-11-18 round red or wash a
    genuine defect into UNKNOWN.
    """
    from verification_agent import _claims_deliverable_defect

    assert _claims_deliverable_defect(text) is claims_defect, text


# ---------------------------------------------------------------------------
# 7. VP-045 — the restore rule must not wash a verdict away (2026-11-19)
# ---------------------------------------------------------------------------
#
# The "本轮没拿到结论" rule landed on 2026-11-18 and its vocabulary mixed
# **two** families of sentence:
#
#   * "这一轮没有拿到结论" — this round reached **no verdict**;
#   * "须重跑 / 需重跑 / no verdict / must be re-run" — **run it again**.
#
# The second family says the opposite of the first: the round *did*
# conclude (the deliverable is incomplete — run it again).  A ledger
# whose text says only "须重跑" was washed into UNKNOWN together with
# the ``deliverable_defect`` classification that buys the repair task.
#
# Two more sentences were swept along: an entry that states the
# deliverable's defect **in its own words** ("断言缺失 / 行为不符 /
# 实现缺失") did conclude something, so it has to stay FAILED.

#: The executor's verdict — the command came back red and the
#: deliverable is incomplete; the round is over and another one is due.
RE_RUN_VERDICT = (
    "3 failed, 12 passed — backend/orphan_reaper.py 的 _run_probe 不存在；"
    "修复不完整，须重跑"
)

#: The three ways an entry can state the deliverable's defect in its own
#: words.  Each of them says "this round *did* conclude something".
SELF_REPORTED_DEFECT_CASES = [
    (
        "断言缺失",
        "断言缺失：backend/orphan_reaper.py 缺少 _run_probe 的断言；"
        "本轮未产生任何结论，未拿到结论。",
    ),
    (
        "行为不符",
        "行为不符：修复后的行为与验收预期不符；本轮未产生任何结论，未拿到结论。",
    ),
    (
        "实现缺失",
        "实现缺失：backend/orphan_reaper.py 未实现 _run_probe；"
        "本轮未产生任何结论，未拿到结论。",
    ),
]

#: Failure evidence that describes the **environment** — it must never
#: read as "this round reached no conclusion".
FAILURE_EVIDENCE_CASES = [
    ("传输失败", "传输失败：连接被重置，重试 3 次后仍失败"),
    ("非零退出码", "退出码 1（门禁报告某条 lane 红）"),
]


def _one_entry_report(actual_result: str, evidence: str) -> dict:
    """A judgment-layer report with a single failed entry."""
    return {
        "overall_status": "FAILED",
        "summary": "s",
        "verification_results": [
            {
                "id": "VP-045",
                "status": "FAILED",
                "actual_result": actual_result,
                "evidence": evidence,
            },
        ],
        "requirement_deviations": [],
    }


def _failed_ledger(actual_result: str, evidence: str = "") -> list:
    """The executor's ledger for the same entry."""
    return [
        {
            "id": "VP-045",
            "status": "FAILED",
            "actual_result": actual_result,
            "evidence": evidence,
            "failure_class": DEFAULT_FAILURE_CLASS,
            "failure_class_source": DEFAULT_FAILURE_CLASS_SOURCE,
        }
    ]


def test_a_re_run_request_does_not_wash_the_executors_verdict(tmp_path):
    """「须重跑」 says *run it again*, not *this round concluded nothing*.

    The executor ran the command, it came back red, and it said the
    deliverable is incomplete.  That is a verdict — washing it into
    UNKNOWN throws away the ``deliverable_defect`` classification, and
    with it the repair task the round earned.
    """
    agent = _make_agent(
        tmp_path,
        _ReportingTool(
            _one_entry_report(RE_RUN_VERDICT, "orphan_reaper.py:565 没有 _run_probe")
        ),
    )

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": _failed_ledger(RE_RUN_VERDICT),
    })

    entry = report["verification_results"][0]
    assert entry["status"] == "FAILED", entry
    assert entry["failure_class"] == DEFAULT_FAILURE_CLASS, entry
    assert failure_history.failure_class_of(entry) == DEFAULT_FAILURE_CLASS, entry


@pytest.mark.parametrize("label,actual_result", SELF_REPORTED_DEFECT_CASES)
def test_a_self_reported_defect_is_not_washed_into_unknown(
    tmp_path, label, actual_result
):
    """An entry that names the defect in its own words has concluded.

    "断言缺失 / 行为不符 / 实现缺失" are the deliverable's defects, said
    plainly.  Such an entry *did* reach a verdict, so the restore rule
    must leave it FAILED — even when it also says "未产生任何结论".
    """
    agent = _make_agent(
        tmp_path,
        _ReportingTool(
            _one_entry_report(actual_result, "orphan_reaper.py:565 没有 _run_probe")
        ),
    )

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [],
    })

    entry = report["verification_results"][0]
    assert entry["status"] == "FAILED", (label, entry)
    assert failure_history.failure_class_of(entry) == DEFAULT_FAILURE_CLASS, (
        label, entry
    )


@pytest.mark.parametrize("label,text", FAILURE_EVIDENCE_CASES)
def test_failure_evidence_is_not_read_as_no_conclusion(tmp_path, label, text):
    """Transport failures and non-zero exit codes are conclusions.

    They describe **how** the round went red, not that it stayed
    undecided — so they must never be read as "no conclusion".
    """
    agent = _make_agent(tmp_path, _ReportingTool(_one_entry_report(text, text)))

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": _failed_ledger(text),
    })

    entry = report["verification_results"][0]
    assert entry["status"] == "FAILED", (label, entry)
    assert failure_history.failure_class_of(entry) == DEFAULT_FAILURE_CLASS, (
        label, entry
    )


def test_a_ledger_that_claims_a_defect_blocks_the_restore(tmp_path):
    """The ledger's own defect claim is a conclusion too.

    The entry says "未产生任何结论" but the executor's ledger says the
    deliverable is broken — the executor reached a verdict, so the
    entry stays FAILED.
    """
    agent = _make_agent(
        tmp_path,
        _ReportingTool(
            _one_entry_report(
                "未产生任何结论：进度日志尾部没有 pytest 摘要行。",
                "抓到的内容是无关的 checkpoint 行。",
            )
        ),
    )

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": _failed_ledger(
            "交付物缺陷：backend/orphan_reaper.py 缺少 _run_probe",
            "orphan_reaper.py:565 没有 _run_probe",
        ),
    })

    entry = report["verification_results"][0]
    assert entry["status"] == "FAILED", entry
    assert failure_history.failure_class_of(entry) == DEFAULT_FAILURE_CLASS, entry


def test_a_ledger_that_only_asks_for_a_re_run_blocks_the_restore(tmp_path):
    """A ledger asking for another run is a verdict, not a blank.

    This is the ledger half of the over-restore: the entry says "未产生
    任何结论", the executor's ledger says "修复不完整，须重跑" — and
    "须重跑" was read as "this round reached no conclusion", so the
    ledger never vetoed the restore.  A ledger that says only "run it
    again" has **concluded** (the deliverable is incomplete), so the
    entry stays FAILED.
    """
    agent = _make_agent(
        tmp_path,
        _ReportingTool(
            _one_entry_report(
                "未产生任何结论：进度日志尾部没有 pytest 摘要行。",
                "抓到的内容是无关的 checkpoint 行。",
            )
        ),
    )

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": _failed_ledger(RE_RUN_VERDICT),
    })

    entry = report["verification_results"][0]
    assert entry["status"] == "FAILED", entry
    assert failure_history.failure_class_of(entry) == DEFAULT_FAILURE_CLASS, entry


def test_a_round_that_really_reached_no_conclusion_is_still_unknown(tmp_path):
    """The control: the restore rule itself stays.

    A round that genuinely reached no verdict — and says so — is still
    UNKNOWN / infra.  The fix for VP-045 narrows the rule; it does not
    delete it.
    """
    agent = _make_agent(
        tmp_path,
        _ReportingTool(
            _one_entry_report(
                "未产生任何结论：进度日志尾部没有 pytest 摘要行；"
                "抓到的内容是无关的 checkpoint 行。",
                "判定依据 details 为空、raw 为空数组；依规不登记为交付物缺陷，但须重跑。",
            )
        ),
    )

    report = agent.generate_verification_report({
        "verification_points": [],
        "execution_results": [],
    })

    entry = report["verification_results"][0]
    assert entry["status"] == VERDICT_UNKNOWN, entry
    assert entry["failure_class"] == INFRA_FAILURE_CLASS, entry
    assert failure_history.failure_class_of(entry) == INFRA_FAILURE_CLASS, entry


def test_the_restore_decision_order_is_checkable():
    """VP-045 asks for a **checkable** decision order.

    The five steps have to run in the documented order — the rule's
    edges ("an entry that states a defect is left alone", "an entry the
    executor concluded on is left alone") are two independent checks,
    and nothing but the order says which one speaks first.
    """
    from verification_agent import _restore_no_conclusion_verdicts

    source = inspect.getsource(_restore_no_conclusion_verdicts)

    steps = [
        # ① the entry has to be FAILED at all
        'entry.get("status")',
        # ② the entry's own defect claim wins over the marker
        "_claims_deliverable_defect(text)",
        # ③ …and the entry must plainly say "no conclusion"
        "_contains_marker(text, NO_CONCLUSION_MARKERS)",
        # ④ the executor's ledger speaks last
        "_claims_deliverable_defect(ledger_text)",
        "_contains_marker(ledger_text, NO_CONCLUSION_MARKERS)",
    ]
    positions = [source.find(step) for step in steps]
    assert all(at != -1 for at in positions), (steps, positions)
    assert positions == sorted(positions), (steps, positions)
