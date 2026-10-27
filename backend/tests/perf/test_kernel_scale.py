"""Scale budgets for the three verification kernels.

What this pins
--------------
Three kernels stand between a plan and its verdict, and all three are
pure functions whose cost grows with the number of verification points:

  1. :func:`verification.topic_relevance.judge_topic_relevance` — decides
     whether the active VPs are still inside the plan's topic.  It is a
     two-tier decision: a deterministic layer that matches a VP's
     ``related_prd_criteria`` against the plan's criteria, and a model
     layer for whatever the deterministic layer could not settle.  Only
     the first tier is measured here — it is the one that runs on every
     VP, every round, with no model in the loop.
  2. :func:`verification.failure_fingerprint.failure_fingerprint` — the
     deterministic identity of one failed attempt.  It hashes a canonical
     string, and it is called once per failed attempt, per VP.
  3. :func:`verification.scan.scan_plans` — the whole-library aggregate:
     read every plan, count every VP, and report how many of them can be
     executed deterministically.

Why budgets at all
------------------
All three are candidates for the failure mode that is hardest to notice:
a pure function that quietly turns into O(n²) as the plan grows.  The
symptom is not a crash — it is a plan that stopped finishing, and it
arrives long after the change that caused it.  A budget is the alarm.

The budgets are deliberately generous
------------------------------------
These are regression alarms, not performance targets.  Every number below
is an order of magnitude above what the kernel actually costs on this
tree (tens of milliseconds against budgets of hundreds of milliseconds
and seconds), because a wall-clock assertion on a shared runner measures
the runner as much as the code.  The budgets exist to catch a shape
change — "this used to be linear and now it is quadratic" — not to catch
a machine that is busy.

What is deliberately NOT asserted
---------------------------------
* **No absolute-value comparison.**  Nothing here compares a measured
  duration against a recorded one, because a recorded duration is a
  statement about the machine that recorded it.  Every assertion is of
  the form "elapsed < a generous ceiling".
* **No timestamped parameterisation.**  A ``parametrize`` whose ids are
  derived from the clock makes two xdist workers that import the module
  in different seconds collect different case ids, and xdist then
  aborts the whole run with "Different tests were collected between gw2
  and gw14".  The repository already carries that scar
  (``backend/tests/unit/test_provider_quota_dispatch.py`` pins the ids
  of its timestamp cases by hand for exactly this reason).  There is no
  ``parametrize`` in this file at all, so the hazard cannot arise.

How the numbers are measured
----------------------------
Every measurement is the **fastest** of several runs.  The fastest run
is the one that met the least interference from the rest of the machine;
taking the minimum removes scheduler noise without flattering the code,
because the code still has to do the whole job once.  The first measured
shape is measured the same way as the others, so nothing is compared
against a differently-measured baseline.

Serial by construction
----------------------
Every test in this file carries the ``perf`` marker.  CI runs the
``perf`` marker in the serial lane (``-m "time_sensitive or perf"``, no
``-n 8``): a wall-clock assertion measured beside seven other workers is
a measurement of the workers, not of the code.  The default lanes
exclude ``perf`` explicitly (``not e2e and not integration and not
time_sensitive and not perf``), which is what keeps these tests out of
the parallel shards.

The three timing tests additionally skip themselves when they detect
that they are running under xdist.  That is not a claim that xdist is
always wrong for a wall-clock assertion — it is the refusal to produce
a number that does not mean what it says.  The fourth test (the
aggregation consistency check) asserts no wall-clock at all and runs
anywhere.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import pytest

# Make the backend modules importable when pytest is launched from the
# project root or from ``backend/``.  The repository root is needed as
# well: ``verification/__init__.py`` pulls in ``verification_agent``,
# which imports ``backend.framework.prompts`` by its package path.
BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification.failure_fingerprint import failure_fingerprint  # noqa: E402
from verification.scan import scan_plans  # noqa: E402
from verification.topic_relevance import (  # noqa: E402
    RelevanceDecision,
    judge_topic_relevance,
)

# All tests in this file are perf tests.  ``pytestmark`` applies the
# marker in one place so the individual tests do not have to decorate
# themselves; see the module docstring for why the marker is the whole
# of the serial-lane contract.
pytestmark = pytest.mark.perf


# =============================================================================
# The serial-run guard
# =============================================================================

#: ``PYTEST_XDIST_WORKER`` is set by pytest-xdist in every worker and is
#: absent when the run is not distributed.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER") or ""

requires_serial = pytest.mark.skipif(
    bool(_XDIST_WORKER),
    reason=(
        "a wall-clock budget measured beside other xdist workers is a "
        f"measurement of the workers; this run is distributed ({_XDIST_WORKER})"
    ),
)


# =============================================================================
# The budgets
# =============================================================================

#: How many times each measurement is repeated.  The fastest run wins.
#:
#: Five rather than three: the whole set costs well under a second, and
#: the minimum of more samples is a better estimate of "the machine was
#: not busy that time".
_REPEATS = 5

# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

#: How many verification points the relevance kernel is asked to judge.
#:
#: 627 is the size of the largest plan this repository has carried; it is
#: the size at which a per-VP cost stops being free.
JUDGE_VP_COUNT = 627

#: How many acceptance criteria those VPs are matched against.
JUDGE_CRITERION_COUNT = 100

#: How many fingerprints the fingerprint kernel is asked to produce.
#:
#: 10 000 is roughly "a plan that failed a lot, over many rounds".
FINGERPRINT_CALLS = 10_000

#: How many plan directories the scan kernel is asked to aggregate.
SCAN_PLAN_COUNT = 27

#: How many verification points each of those plans holds.
#:
#: 24 × 27 = 648, which is the same order as :data:`JUDGE_VP_COUNT` — the
#: two measurements are about the same amount of work.
SCAN_VPS_PER_PLAN = 24

# ---------------------------------------------------------------------------
# Ceilings
# ---------------------------------------------------------------------------

#: The budget for judging :data:`JUDGE_VP_COUNT` VPs against
#: :data:`JUDGE_CRITERION_COUNT` criteria, in milliseconds.
#:
#: Generous on purpose.  The measured cost is tens of milliseconds; this
#: is the alarm that rings when the shape of the work changes, not a
#: target to optimise towards.
JUDGE_BUDGET_MS = 200.0

#: The budget for :data:`FINGERPRINT_CALLS` fingerprints, in milliseconds.
FINGERPRINT_BUDGET_MS = 500.0

#: The budget for scanning :data:`SCAN_PLAN_COUNT` plans, in milliseconds.
SCAN_BUDGET_MS = 2000.0


# =============================================================================
# The measurement helper
# =============================================================================


def _fastest_ms(work: Callable[[], Any]) -> Tuple[float, Any]:
    """Run ``work`` a few times and return its fastest run.

    :param work: The call to measure.  It must do the whole job — the
        point of the measurement is what the production code does, not a
        partial slice of it.
    :return: ``(milliseconds, the value the fastest run produced)``.
    """
    best_ms: float | None = None
    best_value: Any = None
    for _ in range(_REPEATS):
        start = time.perf_counter()
        value = work()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if best_ms is None or elapsed_ms < best_ms:
            best_ms = elapsed_ms
            best_value = value
    assert best_ms is not None  # _REPEATS is at least 1
    return best_ms, best_value


# =============================================================================
# The synthetic data
# =============================================================================


def _judge_vps_by_id() -> List[Dict[str, Any]]:
    """627 VPs that each name the id of a criterion.

    This is the shape a healthy plan has: every VP says which acceptance
    criterion it verifies, so the deterministic layer settles it on the
    first try and no model is ever consulted.
    """
    return [
        {
            "id": f"VP-{index}",
            "related_prd_criteria": [f"AC-{index % JUDGE_CRITERION_COUNT}"],
        }
        for index in range(JUDGE_VP_COUNT)
    ]


def _judge_vps_by_text() -> List[Dict[str, Any]]:
    """627 VPs that name no criterion id — only the criterion's wording.

    This is the expensive shape, and it is the one worth measuring.  The
    id layer misses on every VP, so every VP falls through to the text
    layer, which walks all :data:`JUDGE_CRITERION_COUNT` criteria and
    normalises each one's body — 62 700 text normalisations for one
    call.  That nested walk is where an accidental n² would show up
    first, so the budget is asserted against this shape as well as
    against the cheap one.
    """
    return [
        {
            "id": f"VP-{index}",
            "related_prd_criteria": [f"标准 {index % JUDGE_CRITERION_COUNT}"],
        }
        for index in range(JUDGE_VP_COUNT)
    ]


def _judge_criteria() -> List[Dict[str, Any]]:
    """100 acceptance criteria, each with an id and a body."""
    return [
        {"id": f"AC-{index}", "text": f"标准 {index}"}
        for index in range(JUDGE_CRITERION_COUNT)
    ]


#: The method mix every synthetic plan carries.
#:
#: The counts add up to :data:`SCAN_VPS_PER_PLAN`, and they are
#: deliberately not all deterministic: a scan that counted every VP as
#: deterministic would be a scan with a broken numerator.
_PLAN_METHOD_MIX: Tuple[Tuple[str, int], ...] = (
    ("api_test", 8),
    ("full_ci", 8),
    ("e2e", 4),
    ("code_review", 2),
    ("(unspecified)", 2),
)

#: How many of each plan's :data:`SCAN_VPS_PER_PLAN` VPs are executable
#: without a model — the three deterministic methods.
DETERMINISTIC_PER_PLAN = 8 + 8 + 4


def _synthetic_vp(index: int, method: str) -> Dict[str, Any]:
    """One synthetic verification point of the given method.

    A ``api_test`` VP carries a complete request and a gradeable
    assertion; a ``full_ci`` VP carries a whole-repository entry that has
    not been narrowed down to a single test case; an ``e2e`` VP carries
    the URL it is driven against.  Those three are the deterministic
    ones.  ``code_review`` is a legitimate method the framework does not
    execute, and ``(unspecified)`` declares no method at all — both are
    counted but neither is deterministic.
    """
    vp: Dict[str, Any] = {
        "id": f"VP-{index:03d}",
        "title": f"合成验证点 {index}",
    }
    if method == "api_test":
        vp["verification_method"] = "api_test"
        vp["request"] = {"method": "GET", "url": "http://127.0.0.1:8000/api/items"}
        vp["assertions"] = [{"name": "状态码", "status": 200}]
    elif method == "full_ci":
        vp["verification_method"] = "full_ci"
        vp["ci_entry"] = "python scripts/ci_local.py"
    elif method == "e2e":
        vp["verification_method"] = "e2e"
        vp["target_url"] = "http://127.0.0.1:8080/"
    elif method == "code_review":
        vp["verification_method"] = "code_review"
    # "(unspecified)" declares no ``verification_method`` at all.
    return vp


def _build_plans(root: Path) -> Path:
    """Write :data:`SCAN_PLAN_COUNT` synthetic plans under ``root``.

    :param root: The directory that will *contain* the plan directories.
    :return: ``root``.
    """
    for plan_index in range(SCAN_PLAN_COUNT):
        plan_dir = root / f"20260101-plan-{plan_index:03d}"
        plan_dir.mkdir(parents=True, exist_ok=True)
        points: List[Dict[str, Any]] = []
        for method, count in _PLAN_METHOD_MIX:
            for _ in range(count):
                points.append(_synthetic_vp(len(points), method))
        (plan_dir / "verification_plan.json").write_text(
            json.dumps({"verification_points": points}, ensure_ascii=False),
            encoding="utf-8",
        )
    return root


# =============================================================================
# 1. the relevance kernel
# =============================================================================


@requires_serial
def test_judge_scale_under_200ms():
    """627 VPs against 100 criteria must stay under the 200 ms ceiling.

    Two shapes are measured, because they cost different things: the
    cheap one (every VP names a criterion id) and the expensive one (no
    VP names an id, so every VP walks all 100 criteria).  Both are
    measured the same way and both are held to the same ceiling — the
    point of the ceiling is the *shape* of the work, and only the
    expensive shape can go quadratic.
    """
    criteria = _judge_criteria()

    by_id_ms, by_id_verdicts = _fastest_ms(
        lambda: judge_topic_relevance(vps=_judge_vps_by_id(), criteria=criteria)
    )
    assert len(by_id_verdicts) == JUDGE_VP_COUNT, (
        f"the id shape produced {len(by_id_verdicts)} verdicts, "
        f"expected {JUDGE_VP_COUNT} — a verdict that goes missing makes "
        "the timing below meaningless"
    )
    assert all(
        verdict.decision == RelevanceDecision.DETERMINISTIC_MATCH
        for verdict in by_id_verdicts.values()
    ), "the id shape must settle every VP on the deterministic layer"

    by_text_ms, by_text_verdicts = _fastest_ms(
        lambda: judge_topic_relevance(vps=_judge_vps_by_text(), criteria=criteria)
    )
    assert len(by_text_verdicts) == JUDGE_VP_COUNT, (
        f"the text shape produced {len(by_text_verdicts)} verdicts, "
        f"expected {JUDGE_VP_COUNT} — a verdict that goes missing makes "
        "the timing below meaningless"
    )
    assert all(
        verdict.decision == RelevanceDecision.DETERMINISTIC_MATCH
        for verdict in by_text_verdicts.values()
    ), "the text shape must settle every VP on the deterministic layer"

    assert by_id_ms < JUDGE_BUDGET_MS, (
        f"judging {JUDGE_VP_COUNT} VPs against {JUDGE_CRITERION_COUNT} "
        f"criteria by id took {by_id_ms:.1f} ms "
        f"(budget {JUDGE_BUDGET_MS:.0f} ms)"
    )
    assert by_text_ms < JUDGE_BUDGET_MS, (
        f"judging {JUDGE_VP_COUNT} VPs against {JUDGE_CRITERION_COUNT} "
        f"criteria by text took {by_text_ms:.1f} ms "
        f"(budget {JUDGE_BUDGET_MS:.0f} ms)"
    )


# =============================================================================
# 2. the fingerprint kernel
# =============================================================================


@requires_serial
def test_fingerprint_10k_under_500ms():
    """10 000 fingerprints must stay under the 500 ms ceiling."""
    fingerprints: List[str] = []

    def produce() -> List[str]:
        produced: List[str] = []
        for index in range(FINGERPRINT_CALLS):
            produced.append(
                failure_fingerprint(
                    vp_id=f"VP-{index % 97}",
                    failure_class="deliverable_defect",
                    failed_assertions=["状态码 200", "响应体非空"],
                    command="pytest -x backend/tests/unit",
                    first_error_line=f"AssertionError at line {index}",
                )
            )
        return produced

    elapsed_ms, fingerprints = _fastest_ms(produce)

    assert len(fingerprints) == FINGERPRINT_CALLS, (
        f"the kernel produced {len(fingerprints)} fingerprints, "
        f"expected {FINGERPRINT_CALLS} — a fingerprint that goes missing "
        "makes the timing below meaningless"
    )
    assert all(len(value) == 16 and value.isalnum() for value in fingerprints), (
        "every fingerprint must be 16 hex digits"
    )

    assert elapsed_ms < FINGERPRINT_BUDGET_MS, (
        f"producing {FINGERPRINT_CALLS} fingerprints took {elapsed_ms:.1f} ms "
        f"(budget {FINGERPRINT_BUDGET_MS:.0f} ms)"
    )


# =============================================================================
# 3. the scan kernel
# =============================================================================


@requires_serial
def test_scan_27_plans_under_2s(tmp_path):
    """27 plans must be aggregated under the 2 s ceiling."""
    plans_dir = _build_plans(tmp_path / "plans")

    elapsed_ms, report = _fastest_ms(lambda: scan_plans(plans_dir))

    assert report["totals"]["plans"] == SCAN_PLAN_COUNT, (
        f"the scan reported {report['totals']['plans']} plans, "
        f"expected {SCAN_PLAN_COUNT} — a plan that goes missing makes the "
        "timing below meaningless"
    )

    assert elapsed_ms < SCAN_BUDGET_MS, (
        f"scanning {SCAN_PLAN_COUNT} plans took {elapsed_ms:.1f} ms "
        f"(budget {SCAN_BUDGET_MS:.0f} ms)"
    )


# =============================================================================
# 4. the scan kernel loses nothing
# =============================================================================


def test_scan_totals_self_consistent(tmp_path):
    """The aggregation layer must not drop an item.

    The scan is a pure aggregate: its global totals are supposed to be
    the sum of its per-plan blocks, and those blocks are supposed to be
    the sum of their own method buckets.  A count that goes missing
    anywhere along that chain shows up as a total that disagrees with
    the parts it was made of — which is why this test asserts the
    identity at every level, and then asserts that the identity holds
    with the numbers the synthetic data actually implies.

    No wall-clock is asserted here, so this test runs anywhere.
    """
    plans_dir = _build_plans(tmp_path / "plans")
    report = scan_plans(plans_dir)
    totals = report["totals"]
    plans = report["plans"]

    assert len(plans) == SCAN_PLAN_COUNT, (
        f"the scan reported {len(plans)} plan blocks, expected {SCAN_PLAN_COUNT}"
    )
    assert totals["plans"] == SCAN_PLAN_COUNT, (
        f"totals.plans is {totals['plans']}, expected {SCAN_PLAN_COUNT}"
    )

    # The global totals are exactly the sum of the per-plan blocks.
    assert sum(entry["vp_total"] for entry in plans) == totals["vp_total"], (
        "the global vp_total is not the sum of the per-plan vp_total — "
        "the aggregation layer dropped an item"
    )
    assert (
        sum(entry["deterministic_total"] for entry in plans)
        == totals["deterministic_total"]
    ), (
        "the global deterministic_total is not the sum of the per-plan "
        "deterministic_total — the aggregation layer dropped an item"
    )

    # And each per-plan block is the sum of its own buckets.
    for entry in plans:
        assert sum(entry["by_method"].values()) == entry["vp_total"], (
            f"plan {entry['plan_id']!r}: by_method does not add up to "
            f"vp_total ({entry['by_method']!r} vs {entry['vp_total']})"
        )
        assert (
            sum(entry["deterministic"].values()) == entry["deterministic_total"]
        ), (
            f"plan {entry['plan_id']!r}: deterministic does not add up to "
            f"deterministic_total ({entry['deterministic']!r} vs "
            f"{entry['deterministic_total']})"
        )

    # The totals are the ones this data implies.
    assert totals["vp_total"] == SCAN_PLAN_COUNT * SCAN_VPS_PER_PLAN, (
        f"vp_total is {totals['vp_total']}, expected "
        f"{SCAN_PLAN_COUNT * SCAN_VPS_PER_PLAN}"
    )
    assert totals["deterministic_total"] == SCAN_PLAN_COUNT * DETERMINISTIC_PER_PLAN, (
        f"deterministic_total is {totals['deterministic_total']}, expected "
        f"{SCAN_PLAN_COUNT * DETERMINISTIC_PER_PLAN}"
    )
    assert totals["ratio"] == round(
        totals["deterministic_total"] / totals["vp_total"], 3
    ), (
        f"ratio is {totals['ratio']}, expected "
        f"{round(totals['deterministic_total'] / totals['vp_total'], 3)}"
    )
