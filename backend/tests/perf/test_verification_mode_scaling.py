"""Scaling budgets for the verification mode's mechanical steps.

What this pins
--------------
The verification mode runs three mechanical steps on every round, and all
three sit on the hot path of a plan that grows:

  1. **Parse + validate.**  The plan document is parsed and compared
     against the machine-format plan by
     :func:`verification_plan_translator.validate_equivalence` before
     anything is allowed to run.  It is a *mechanical* comparison, so its
     cost has to be a function of the number of verification points (n) —
     not of n².
  2. **Render.**  The report is rendered to Markdown after every write.
     The output grows with n, so the work must too.
  3. **Materialise the workspace.**  The verification worktree is derived
     from the frozen baseline, and it must stay *one* worktree however
     many times it is asked for.

Each of the three has a budget, and each budget is generous: these are
regression alarms, not performance targets.  What they are for is the
failure mode where a plan that grew turns into a run that stopped
finishing — a comparison that got quadratic, a report that got
quadratic, a worktree that got materialised again on every call.

The ratio is the load-bearing part
----------------------------------
A budget on its own is a number somebody raises when it is hit.  The
*ratio* between two plan sizes is a claim about the **shape** of the
work, and it is the claim that catches the nested loop nobody meant to
write: a quadratic comparison is 25× slower at 5× the size, and no
amount of raising the budget hides that.

How the numbers are measured
----------------------------
Every measurement is the **fastest** of three runs.  The fastest run is
the one that met the least interference from the rest of the machine;
taking the minimum removes scheduler noise without flattering the code,
because the code still has to do the whole job once.  The first measured
size is measured the same way as the others, so the ratio compares like
with like.

The *shape* measurement (the growth ratio between two plan sizes) is
different: a ratio divides two wall-clock numbers, so it carries the
noise of both sides, and the fastest run alone says nothing about how
trustworthy the ratio is.  That measurement therefore keeps every
sample, takes the median, and measures how much the samples disagree.
If they disagree by more than a few percent, the host cannot measure
the shape at that scale and the case says so instead of reporting a
regression that was not there.

Marker
------
Every test here carries the ``perf`` marker.  CI runs the ``perf``
marker in the serial lane (``-m "time_sensitive or perf"``, no ``-n 8``):
a wall-clock assertion measured beside seven other workers is a
measurement of the workers, not of the code.  The default lanes exclude
``perf`` explicitly (``not e2e and not integration and not time_sensitive
and not perf``), which is what keeps these tests out of the parallel
shards.
"""

from __future__ import annotations

import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

# Make the backend modules importable when pytest is launched from the
# project root or from ``backend/``.  Mirrors the pattern the sibling
# verification-mode tests use.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from tests import _host_load  # noqa: E402
from tests.fixtures.verification_mode import register_worktree  # noqa: E402
from verification_plan_doc import parse_plan_doc  # noqa: E402
from verification_plan_translator import validate_equivalence  # noqa: E402
from verification_report_md import render  # noqa: E402
from verification_workspace import Baseline, ensure_workspace  # noqa: E402

# All tests in this file are perf tests.  ``pytestmark`` applies the
# marker in one place so the individual tests do not have to decorate
# themselves; see the module docstring for why the marker is the whole
# of the serial-lane contract.
pytestmark = pytest.mark.perf


# =============================================================================
# The host-load gate
# =============================================================================
#
# A wall-clock budget measured on a host that is already carrying someone
# else's work is a measurement of the machine, not of the code under test.
# The question "is this host too loaded to measure on?" is therefore asked
# before the budget is asserted -- and it is asked the same way everywhere,
# because a decision that is made twice is a decision that drifts.
#
# The decision lives in one place: ``backend/tests/_host_load.py``.  One
# limit, one message format, one place to change them.  On CI the gate
# stands down: a hosted runner's load is not the runner's own doing, and a
# lane that quietly skipped would report green while asserting nothing.
#
# The dispersion check further down this file is a *different* question --
# whether the samples of this one measurement agree with each other -- and
# it stays: the load gate says the host is not busy, the dispersion check
# says the numbers came off the clock cleanly.


def _skip_if_host_too_busy() -> None:
    """Skip this case when the host is too loaded to measure a budget on.

    The budget itself is not weakened: it is asserted at exactly the value
    it always had, wherever the host is measurable.
    """
    reason = _host_load.skip_reason()
    if reason is not None:
        pytest.skip(reason)


# =============================================================================
# The budgets
# =============================================================================

#: How many times each measurement is repeated.  The fastest run wins.
#:
#: Five rather than three: the growth ratio divides two measurements, so
#: the noise in the smaller one is amplified into the ratio.  More runs
#: means a better estimate of each side, and the whole set costs a few
#: dozen milliseconds.
_REPEATS = 5

#: The verification-point counts the budgets are stated at.  ``100`` is in
#: the list because it is the base of the growth ratio: 500 is five times
#: 100, so "five times the plan" has a number to be compared against.
VP_SIZES: Tuple[int, ...] = (50, 100, 200, 500)

#: The size the growth ratio is stated at: 500 VPs against 100 VPs.
SCALE_BASE = 100
SCALE_LARGE = 500

#: How much slower the 5× bigger plan may be.  Linear work is 5×;
#: quadratic work is 25×.  8× leaves room for the measurement noise
#: without ever letting a quadratic through.
MAX_GROWTH_FACTOR = 8.0

#: How many times each size is measured for the *shape* bound.
#:
#: More than the budget measurements need: the growth ratio divides
#: two measurements, so the noise of the smaller one is amplified into
#: the ratio.  Nine samples is enough for the quartiles to mean
#: something and costs a few dozen milliseconds.
_SCALING_REPEATS = 9

#: The relative dispersion above which the shape bound is not measurable.
#:
#: The growth ratio is a ratio of two wall-clock measurements, so it
#: carries the noise of both sides.  When the samples of one side
#: disagree by more than this fraction of their median, the ratio
#: describes the host rather than the code — the case then says so
#: (with the numbers that say so) instead of reporting a shape
#: regression that was not there.
_MAX_RELATIVE_DISPERSION = 0.08

#: The budget for parsing + validating a 200-VP plan, in milliseconds.
PARSE_VALIDATE_BUDGET_MS = 300.0

#: The budget for rendering a 200-VP report, in milliseconds.
RENDER_BUDGET_MS = 300.0

#: The bounds the rendered size must land in.  A report that grows with
#: the plan is linear in n; one that does not grow at all is broken in
#: the other direction, and one that grows like n² is the failure this
#: file exists to catch.
MIN_SIZE_RATIO = 3.0
MAX_SIZE_RATIO = 8.0


# =============================================================================
# The fixture
# =============================================================================
#
# The fixture is a *real* plan document and a *real* machine-format plan,
# built at whatever size the test asks for.  Nothing here is a mock: the
# point of the measurement is what the production code does with a real
# document, and a mock of the comparison would only measure the mock.
#
# The document exercises all four dimensions of the equivalence check
# (points / criteria / evidence / scope), so the comparison really walks
# the whole plan instead of short-circuiting on the id set.


def _vp_id(index: int) -> str:
    """The id of the ``index``-th (1-based) verification point."""
    return f"VP-{index:03d}"


def build_plan_doc(vp_count: int) -> str:
    """A structurally valid plan document with ``vp_count`` verification points.

    :param vp_count: How many verification points the document declares.
    :return: The document text.
    """
    blocks: List[str] = []
    for index in range(1, vp_count + 1):
        blocks.append(
            f"### {_vp_id(index)} 验证点 {index}\n"
            "\n"
            "- 范围：backend/src/verification_loop.py\n"
            "- 验证方法：automated_test\n"
            "- 通过判据：\n"
            f"  - C1: 验证点 {index} 的第一条判据\n"
            "- 证据要求：\n"
            f"  - E1: 验证点 {index} 的第一条证据\n"
            f"- 预期结果：验证点 {index} 全部满足\n"
        )
    items = "\n".join(blocks)
    return (
        "## 被验证对象\n"
        "\n"
        "baseline: repo=/tmp/example/repo ref=refs/pull/7/head pr=7\n"
        "\n"
        "被验证对象是上游仓库的 pull request 头提交。\n"
        "\n"
        "## 验证范围\n"
        "\n"
        "- backend/src 下的改动\n"
        "- http://127.0.0.1:8080/api/vp\n"
        "\n"
        "## 排除范围\n"
        "\n"
        "- 前端交互（本次未改动前端）\n"
        "\n"
        "## 验证方法\n"
        "\n"
        "- automated_test：在被验证对象自己的检出上跑一遍它自己的测试命令。\n"
        "\n"
        "## 验证点清单\n"
        "\n"
        f"{items}"
        "\n"
        "## 预期结果\n"
        "\n"
        "验证点清单里的每一条都通过。\n"
        "\n"
        "## 证据要求\n"
        "\n"
        "每个验证点的命令输出原文。\n"
        "\n"
        "## 通过判据\n"
        "\n"
        "全部验证点通过即本轮通过。\n"
        "\n"
        "## 环境与依赖\n"
        "\n"
        "git；不需要网络。\n"
        "\n"
        "## 风险面\n"
        "\n"
        "工作区写错仓库；由工作区根目录守卫拦下。\n"
    )


def build_plan_json(vp_count: int) -> Dict[str, Any]:
    """The machine-format plan that is equivalent to :func:`build_plan_doc`.

    Every verification point carries the execution details the translator
    would have added: a request (a URL), a test command (a path), the
    assertions that cover the criteria, and the normalised evidence
    reference prefix.  The scope of both locations is inside the scope
    the document declares, so the comparison has something to compare and
    finds nothing wrong.

    :param vp_count: How many verification points the plan holds.
    :return: The machine-format plan.
    """
    points: List[Dict[str, Any]] = []
    for index in range(1, vp_count + 1):
        points.append(
            {
                "id": _vp_id(index),
                "title": f"验证点 {index}",
                "verification_method": "automated_test",
                "related_prd_criteria": f"C1: 验证点 {index} 的第一条判据",
                "expected_result": f"E1 | 验证点 {index} 全部满足",
                "assertions": [
                    {"name": f"C1: 验证点 {index} 的第一条判据", "status": 200}
                ],
                "request": {
                    "method": "GET",
                    "url": "http://127.0.0.1:8080/api/vp",
                    "headers": {},
                    "body": None,
                    "timeout_seconds": 30,
                },
                "test_command": "backend/src/tests/test_vp.py",
            }
        )
    return {"services": [], "verification_points": points}


def build_report_json(vp_count: int) -> Dict[str, Any]:
    """The verification report that belongs to a plan of this size."""
    points: List[Dict[str, Any]] = []
    for index in range(1, vp_count + 1):
        points.append(
            {
                "id": _vp_id(index),
                "plan_item_id": _vp_id(index),
                "status": "passed",
                "title": f"验证点 {index} 的说明文字",
            }
        )
    return {"final_verdict": "passed", "verification_points": points}


def build_plan_items(vp_count: int) -> Dict[str, str]:
    """The plan-document item table the report is rendered against."""
    return {_vp_id(index): f"验证点 {index}" for index in range(1, vp_count + 1)}


# =============================================================================
# The measurements
# =============================================================================


def measure_parse_and_validate(vp_count: int) -> Tuple[float, List[Any]]:
    """Parse a plan document and compare it with its machine plan.

    :param vp_count: The size of the plan.
    :return: ``(fastest elapsed milliseconds, the divergences it reported)``.
    """
    document = build_plan_doc(vp_count)
    plan = build_plan_json(vp_count)

    fastest = float("inf")
    divergences: List[Any] = []
    for _ in range(_REPEATS):
        started = time.perf_counter()
        parsed = parse_plan_doc(document)
        divergences = validate_equivalence(parsed, plan)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        fastest = min(fastest, elapsed_ms)
    return fastest, divergences


def measure_render(vp_count: int) -> Tuple[float, int]:
    """Render the report of a plan of this size.

    :param vp_count: The size of the plan.
    :return: ``(fastest elapsed milliseconds, the size of the output in bytes)``.
    """
    document = build_plan_doc(vp_count)
    plan = build_plan_json(vp_count)
    parsed = parse_plan_doc(document)
    report = build_report_json(vp_count)
    plan_items = build_plan_items(vp_count)

    fastest = float("inf")
    size = 0
    for _ in range(_REPEATS):
        started = time.perf_counter()
        rendered = render(report, plan_items, plan_json=plan, parsed=parsed)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        fastest = min(fastest, elapsed_ms)
        size = len(rendered.encode("utf-8"))
    return fastest, size


def _measurements() -> Dict[str, float]:
    """The headline numbers, in the shape a reader wants to see them."""
    return {
        f"{size}_vp_ms": round(measure_parse_and_validate(size)[0], 3)
        for size in VP_SIZES
    }


def measure_parse_and_validate_samples(
    document: Any, plan: Any, repeats: int
) -> Tuple[List[float], List[Any]]:
    """Parse + validate, keeping every sample instead of the fastest.

    :param document: the plan document to parse.
    :param plan: the machine plan to compare the parsed document against.
    :param repeats: how many times to run the comparison.
    :return: ``(elapsed milliseconds per run, the divergences it reported)``.

    The budget measurements take the fastest run because a single number
    is enough there.  The *shape* measurement needs the whole
    distribution: a ratio of two wall-clock numbers is only as
    trustworthy as the spread of the samples behind them, and the
    spread is what says whether the host could measure them at all.
    """
    samples: List[float] = []
    divergences: List[Any] = []
    for _ in range(repeats):
        started = time.perf_counter()
        parsed = parse_plan_doc(document)
        divergences = validate_equivalence(parsed, plan)
        samples.append((time.perf_counter() - started) * 1000.0)
    return samples, divergences


def relative_dispersion(samples: List[float]) -> float:
    """How much the samples disagree, as a fraction of their median.

    The interquartile range is used rather than the full range: one
    sample that caught a scheduling hiccup should not make a whole run
    look unmeasurable — but a run where the quartiles themselves
    disagree has a problem the hiccup does not explain.
    """
    median = statistics.median(samples)
    if median <= 0.0:
        return float("inf")
    quartiles = statistics.quantiles(samples, n=4)
    return (quartiles[2] - quartiles[0]) / median


def _git_worktree_paths(repo_dir: Path) -> List[Path]:
    """The worktrees git has registered for ``repo_dir``."""
    completed = subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        Path(line.split(" ", 1)[1]).resolve()
        for line in completed.stdout.splitlines()
        if line.startswith("worktree ")
    ]


# =============================================================================
# The tests
# =============================================================================


def test_parse_and_validate_budget() -> None:
    """Parsing and comparing a 200-VP plan stays inside the budget.

    The comparison is the mechanical gate every verification round passes
    through, so its cost is part of the round's cost.  The budget is
    generous (the measured cost is a small fraction of it); what it
    catches is a comparison that turned quadratic.
    """

    _skip_if_host_too_busy()

    elapsed_ms, divergences = measure_parse_and_validate(200)

    print({"200_vp_ms": round(elapsed_ms, 3)})
    assert divergences == [], (
        "the fixture must be equivalent to its machine plan — the comparison "
        f"reported {len(divergences)} divergence(s): {divergences[:3]}"
    )
    assert elapsed_ms < PARSE_VALIDATE_BUDGET_MS, (
        f"parsing + comparing 200 VPs took {elapsed_ms:.1f} ms; the budget is "
        f"{PARSE_VALIDATE_BUDGET_MS:.0f} ms. Measurements: {_measurements()}"
    )


def test_scaling_is_not_quadratic() -> None:
    """Five times the plan costs less than eight times the work.

    A linear comparison costs 5× at 5× the size; a quadratic one costs
    25×.  The bound is 8× — above linear, far below quadratic.

    The bound is **noise-aware**.  The ratio divides two wall-clock
    measurements, so it carries the noise of both sides; on a loaded
    host the samples of one side disagree by a wide margin and the
    ratio then describes the host rather than the code.  Two things
    follow from that:

      * the two sizes are measured *alternately*, so a host that gets
        busier half-way through charges the same drift to both sides;
      * when the samples still disagree by more than
        ``_MAX_RELATIVE_DISPERSION`` of their median, the host cannot
        measure this shape, and the case says so — with the numbers
        that say so — instead of reporting a shape regression that was
        not there, and instead of passing one that was.
    """

    _skip_if_host_too_busy()

    base_document = build_plan_doc(SCALE_BASE)
    base_plan = build_plan_json(SCALE_BASE)
    large_document = build_plan_doc(SCALE_LARGE)
    large_plan = build_plan_json(SCALE_LARGE)

    base_samples: List[float] = []
    large_samples: List[float] = []
    base_divergences: List[Any] = []
    large_divergences: List[Any] = []
    for _ in range(_SCALING_REPEATS):
        round_base, base_divergences = measure_parse_and_validate_samples(
            base_document, base_plan, 1
        )
        round_large, large_divergences = measure_parse_and_validate_samples(
            large_document, large_plan, 1
        )
        base_samples.extend(round_base)
        large_samples.extend(round_large)

    assert base_divergences == [], (
        f"the {SCALE_BASE}-VP fixture must be equivalent to its machine plan: "
        f"{base_divergences[:3]}"
    )
    assert large_divergences == [], (
        f"the {SCALE_LARGE}-VP fixture must be equivalent to its machine plan: "
        f"{large_divergences[:3]}"
    )

    base_ms = statistics.median(base_samples)
    large_ms = statistics.median(large_samples)
    assert base_ms > 0.0, (
        f"the {SCALE_BASE}-VP measurement was {base_ms} ms; a ratio against it "
        "would mean nothing"
    )

    base_dispersion = relative_dispersion(base_samples)
    large_dispersion = relative_dispersion(large_samples)
    dispersion = max(base_dispersion, large_dispersion)
    growth = large_ms / base_ms
    print(
        {
            f"{SCALE_BASE}_vp_ms": round(base_ms, 3),
            f"{SCALE_LARGE}_vp_ms": round(large_ms, 3),
            "growth": round(growth, 3),
            f"{SCALE_BASE}_dispersion": round(base_dispersion, 4),
            f"{SCALE_LARGE}_dispersion": round(large_dispersion, 4),
            f"{SCALE_BASE}_samples": [round(value, 2) for value in base_samples],
            f"{SCALE_LARGE}_samples": [round(value, 2) for value in large_samples],
        }
    )

    if dispersion > _MAX_RELATIVE_DISPERSION:
        pytest.skip(
            f"this host cannot measure the shape: the {SCALE_BASE}-VP samples "
            f"spread {base_dispersion * 100:.0f}% and the {SCALE_LARGE}-VP "
            f"samples {large_dispersion * 100:.0f}% of their median, above the "
            f"{_MAX_RELATIVE_DISPERSION * 100:.0f}% this bound needs (the growth "
            f"measured at {growth:.1f}× is not trustworthy at that dispersion)"
        )

    assert growth < MAX_GROWTH_FACTOR, (
        f"{SCALE_LARGE} VPs took {large_ms:.1f} ms against {base_ms:.1f} ms for "
        f"{SCALE_BASE} VPs — {growth:.1f}× for 5× the plan. The bound is "
        f"{MAX_GROWTH_FACTOR:.0f}×; quadratic work would be 25×. "
        f"Measurements: {_measurements()}"
    )


def test_render_budget_and_linear_size() -> None:
    """Rendering 200 VPs is inside the budget, and the output grows linearly.

    Two claims in one test because they are the same claim seen from two
    ends: the *time* a render takes and the *size* of what it produces
    must both scale with the number of verification points.
    """

    _skip_if_host_too_busy()

    render_ms, size_200 = measure_render(200)
    _, size_100 = measure_render(100)
    _, size_500 = measure_render(500)

    print(
        {
            "200_vp_render_ms": round(render_ms, 3),
            "100_vp_bytes": size_100,
            "200_vp_bytes": size_200,
            "500_vp_bytes": size_500,
        }
    )
    assert render_ms < RENDER_BUDGET_MS, (
        f"rendering a 200-VP report took {render_ms:.1f} ms; the budget is "
        f"{RENDER_BUDGET_MS:.0f} ms"
    )

    growth = size_500 / size_100
    assert MIN_SIZE_RATIO <= growth <= MAX_SIZE_RATIO, (
        f"the rendered report grew {growth:.2f}× from {SCALE_BASE} VPs to "
        f"{SCALE_LARGE} VPs ({size_100} → {size_500} bytes). Linear growth is "
        f"about 5×, quadratic about 25×; the band is "
        f"{MIN_SIZE_RATIO:.0f}×–{MAX_SIZE_RATIO:.0f}×. A report that does not "
        "grow at all is broken in the other direction: it is dropping rows."
    )

    doubling = size_200 / size_100
    assert 1.5 <= doubling <= 3.0, (
        f"the rendered report grew {doubling:.2f}× from {SCALE_BASE} VPs to "
        f"200 VPs ({size_100} → {size_200} bytes); doubling the plan must "
        "roughly double the report, not quadruple it and not leave it alone"
    )


def test_repeated_workspace_call_faster(upstream_repo, workspace_root) -> None:
    """Asking for the workspace again reuses it — and is cheaper.

    The worktree is derived from the frozen baseline and it is *one*
    worktree: a second request must not materialise a second directory,
    and it must not cost what materialising one costs.  The materialising
    call writes a checkout; the reusing call only reads the registration.
    """

    _skip_if_host_too_busy()

    baseline = Baseline(
        repo_dir=upstream_repo.repo,
        ref=upstream_repo.pr_ref,
        commit_sha=upstream_repo.head_sha(),
        pr_number=upstream_repo.pr_number,
    )
    plan_id = "scaling-plan"

    started = time.perf_counter()
    first = ensure_workspace(plan_id, baseline)
    first_ms = (time.perf_counter() - started) * 1000.0
    # The teardown has to be able to give this worktree back.
    register_worktree(upstream_repo.repo, first.path)

    reuse_ms: List[float] = []
    for _ in range(_REPEATS):
        started = time.perf_counter()
        again = ensure_workspace(plan_id, baseline)
        reuse_ms.append((time.perf_counter() - started) * 1000.0)
        assert again.path == first.path, (
            f"the second call returned {again.path}, not the worktree the "
            f"first call made ({first.path})"
        )

    median_reuse_ms = statistics.median(reuse_ms)
    print(
        {
            "first_workspace_ms": round(first_ms, 3),
            "reuse_workspace_ms": round(median_reuse_ms, 3),
        }
    )
    assert median_reuse_ms < first_ms, (
        f"reusing the workspace took {median_reuse_ms:.1f} ms against "
        f"{first_ms:.1f} ms for making it — the second call must not cost "
        "what materialising a checkout costs"
    )

    # No second directory: the workspace root holds this plan and nothing else.
    root = Path(workspace_root)
    directories = sorted(child.name for child in root.iterdir() if child.is_dir())
    assert directories == [plan_id], (
        f"the workspace root holds {directories}, expected exactly "
        f"['{plan_id}'] — the second call materialised a new directory"
    )

    # And no second registration: git knows about this worktree once.
    registered = _git_worktree_paths(upstream_repo.repo)
    assert registered.count(first.path.resolve()) == 1, (
        f"git has this worktree registered {registered.count(first.path.resolve())} "
        f"times ({registered}) — the second call registered it again"
    )
