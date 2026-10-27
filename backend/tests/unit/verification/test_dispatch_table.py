"""TDD spec — backend/verification_subagent.py 的确定性分派表与三态适配。

Background
----------
``api_test`` / ``full_ci`` / ``e2e`` 的判据是**可机械执行的命令与断言**，
不是「必须问一次模型才知道答案」的问题。以前这些方法一律起 LLM 子会话，
于是同一条判据在两轮里能得出相反结论 —— 判定变成了子 agent 的自报，
而自报不可复跑。

所以「谁执行什么」必须是一条**显式的契约**，而不是长在 if 链里的惯例：

* :data:`verification_subagent.DETERMINISTIC_RUNNERS` —— 方法 → 执行它的
  确定性 runner 模块（显式常量，源码里一个字面量字典）。
* :func:`verification_subagent.resolve` —— 一条 VP 走确定性 runner，
  还是落回 LLM 路径。
* :class:`verification_subagent.DeterministicOutcome` —— 确定性判定的
  PASS / FAIL / ESCALATE 三态结果。

TDD contract — 4 cases
-----------------------
1. ``test_dispatch_table_covers_methods`` — 分派表覆盖三类方法，且是
   **显式常量**（源码里就是字面量字典，不是运行时拼出来的）。
2. ``test_dispatch_matches_validator`` — 分派结论与 runner 的校验器一致：
   校验器通过的走确定性路径，校验器不通过的落回 LLM 路径。
3. ``test_unknown_method_falls_back_to_llm`` — 未知方法走 LLM。
4. ``test_no_direct_field_reads`` — 分派器不直接读 VP 的字段（静态门禁在
   ``backend/tests/static_gates/test_dispatch_reads_no_vp_fields.py``）。

补充用例（同一份文件）：

* ``test_outcome_pass_fail_escalate`` — 三态：PASS 直接产裁决；
  FAIL 带上 stdout / stderr / 退出码；ESCALATE 带上触发原因与证据。
* ``test_deterministic_path_opens_no_llm_session`` — 确定性路径一次 LLM
  会话都不开。
* ``test_dispatch_never_exceeds_scan_criteria`` — 分派口径以全库扫描
  （:func:`verification.scan.scan_plans`）为对照：分派判成「可确定性
  判定」的，扫描必须也判成可确定性判定（分派不得比扫描更宽松）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import pytest  # noqa: E402

from verification.scan import DETERMINISTIC_METHODS, scan_plans  # noqa: E402
from verification_subagent import (  # noqa: E402
    DETERMINISTIC_MODE,
    DETERMINISTIC_RUNNERS,
    ESCALATE_PATH_MODE,
    LLM_ROUTE,
    OUTCOME_ESCALATE,
    OUTCOME_FAIL,
    OUTCOME_PASS,
    DeterministicOutcome,
    resolve,
    run_deterministic,
)

#: The source file the dispatch table lives in. The static gate scans the
#: same file; this test only needs the assignment node.
SUBAGENT_SOURCE = BACKEND_DIR / "verification_subagent.py"


# ---------------------------------------------------------------------------
# fixtures — VPs that exercise both sides of each runner's validator
# ---------------------------------------------------------------------------


def _api_vp(**overrides) -> dict:
    """A schema-valid ``api_test`` VP (request + assertions complete)."""
    vp = {
        "id": "VP-1",
        "verification_method": "api_test",
        "request": {"method": "GET", "url": "/api/plans"},
        "assertions": [{"status": 200}],
    }
    vp.update(overrides)
    return vp


def _ci_vp(**overrides) -> dict:
    """A schema-valid ``full_ci`` VP (a whole-repo gate entry, not narrowed)."""
    vp = {
        "id": "VP-2",
        "verification_method": "full_ci",
        "ci_entry": "python3 -m pytest tests/unit",
    }
    vp.update(overrides)
    return vp


# ---------------------------------------------------------------------------
# 1. the dispatch table is an explicit constant covering the three methods
# ---------------------------------------------------------------------------


def test_dispatch_table_covers_methods() -> None:
    """The table names a runner for each of the three deterministic methods."""
    assert DETERMINISTIC_RUNNERS == {
        "api_test": "verification_api_runner",
        "full_ci": "verification_ci_runner",
        "e2e": "verification_ci_runner",
    }
    # The table and the whole-library scan must agree on WHICH methods are
    # deterministically gradeable — the scan (task 4) is the yardstick.
    assert set(DETERMINISTIC_RUNNERS) == set(DETERMINISTIC_METHODS)


def test_dispatch_table_is_an_explicit_literal() -> None:
    """The table is a literal in the source, not assembled at import time.

    A table built by a loop or a comprehension would hide its contents
    from a reader (and from this test), which is exactly what an
    "explicit contract" is supposed to prevent.
    """
    tree = ast.parse(SUBAGENT_SOURCE.read_text(encoding="utf-8"))
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "DETERMINISTIC_RUNNERS"
    ]
    assert len(assignments) == 1, (
        "DETERMINISTIC_RUNNERS must be assigned exactly once, "
        f"found {len(assignments)} assignments"
    )
    value = assignments[0].value
    assert isinstance(value, ast.Dict), (
        "DETERMINISTIC_RUNNERS must be a literal dict in the source, "
        f"got {type(value).__name__}"
    )
    literal = ast.literal_eval(value)
    assert literal == dict(DETERMINISTIC_RUNNERS)


# ---------------------------------------------------------------------------
# 2. the dispatch conclusion matches the runner's own validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vp, runner_module",
    [
        (_api_vp(), "verification_api_runner"),
        (_ci_vp(), "verification_ci_runner"),
    ],
)
def test_dispatch_matches_validator(vp: dict, runner_module: str) -> None:
    """A VP the runner accepts goes to that runner; one it rejects escalates."""
    import importlib

    validator = getattr(importlib.import_module(runner_module), "validate_vp")
    issues = list(validator(vp))

    route, mode = resolve(vp)

    if issues:
        assert (route, mode) == (LLM_ROUTE, ESCALATE_PATH_MODE), (
            "the runner rejected this VP, so the dispatch must fall back "
            f"to the LLM path; validator issues: {issues!r}"
        )
    else:
        assert (route, mode) == (runner_module, DETERMINISTIC_MODE)


def test_dispatch_rejects_an_api_vp_without_assertions() -> None:
    """The documented example: a VP missing its assertions escalates."""
    vp = {
        "id": "VP-1",
        "verification_method": "api_test",
        "request": {"method": "GET", "url": "/api/plans"},
    }
    assert resolve(vp) == (LLM_ROUTE, ESCALATE_PATH_MODE)


def test_dispatch_rejects_an_api_vp_with_an_unsupported_method() -> None:
    """An unsupported HTTP verb is the runner's call, not the dispatcher's."""
    vp = _api_vp(request={"method": "BREW", "url": "/api/plans"})
    assert resolve(vp) == (LLM_ROUTE, ESCALATE_PATH_MODE)


def test_dispatch_rejects_a_narrowed_ci_entry() -> None:
    """A CI entry narrowed to one test file is not a whole-repo gate."""
    vp = _ci_vp(ci_entry="pytest tests/unit/test_one_thing.py")
    assert resolve(vp) == (LLM_ROUTE, ESCALATE_PATH_MODE)


def test_dispatch_rejects_a_ci_vp_without_an_entry() -> None:
    """No entry means no exit code to grade on."""
    vp = {"id": "VP-2", "verification_method": "full_ci"}
    assert resolve(vp) == (LLM_ROUTE, ESCALATE_PATH_MODE)


# ---------------------------------------------------------------------------
# 3. an unknown method falls back to the LLM path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        "code_review",
        "ui_validation",
        "automated_test",
        "manual_check",
        "api_tests",          # near miss — not the same method
        "made_up_method",
        "",
    ],
)
def test_unknown_method_falls_back_to_llm(method: str) -> None:
    """Only the three table methods may leave the LLM path."""
    vp = {"id": "VP-9", "verification_method": method}
    assert resolve(vp) == (LLM_ROUTE, ESCALATE_PATH_MODE)


def test_malformed_vp_falls_back_to_llm() -> None:
    """A VP that is not an object cannot be dispatched anywhere."""
    assert resolve(None) == (LLM_ROUTE, ESCALATE_PATH_MODE)
    assert resolve("not a vp") == (LLM_ROUTE, ESCALATE_PATH_MODE)


# ---------------------------------------------------------------------------
# the three states — PASS / FAIL / ESCALATE
# ---------------------------------------------------------------------------


def test_outcome_pass_carries_the_runner_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PASS hands the runner's own verdict straight through."""
    from verification_api_runner import ApiRunResult

    def _fake_run(vp, *, artifact_dir=None):
        return ApiRunResult(
            status="PASSED",
            reasons=["all assertions passed"],
            evidence={"vp_id": vp.get("id"), "status_code": 200},
        )

    monkeypatch.setattr(
        "verification_api_runner.run_api_verification", _fake_run
    )

    outcome = run_deterministic(_api_vp())

    assert outcome.status == OUTCOME_PASS
    assert outcome.route == "verification_api_runner"
    assert outcome.verdict is not None
    assert outcome.verdict["status"] == "PASSED"
    assert outcome.is_deterministic is True
    assert outcome.needs_llm is False


def test_outcome_fail_carries_stdout_stderr_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FAIL must be reproducible: the process output and exit code travel."""
    from verification_ci_runner import CiRunResult

    def _fake_run(vp, *, project_dir, artifact_dir=None):
        return CiRunResult(
            status="FAILED",
            reasons=["exit code 2"],
            evidence={
                "vp_id": vp.get("id"),
                "exit_code": 2,
                "stdout_tail": "2 failed, 10 passed",
                "stderr_tail": "E   assert 1 == 2",
            },
        )

    monkeypatch.setattr(
        "verification_ci_runner.run_ci_verification", _fake_run
    )

    outcome = run_deterministic(_ci_vp(), project_dir=tmp_path)

    assert outcome.status == OUTCOME_FAIL
    assert outcome.route == "verification_ci_runner"
    assert outcome.exit_code == 2
    assert "2 failed" in outcome.stdout
    assert "assert 1 == 2" in outcome.stderr
    assert outcome.verdict is not None
    assert outcome.verdict["status"] == "FAILED"
    assert outcome.is_deterministic is True
    assert outcome.needs_llm is False


def test_outcome_escalate_carries_the_trigger_and_the_evidence() -> None:
    """ESCALATE says WHY it could not judge, and shows the evidence."""
    vp = {
        "id": "VP-1",
        "verification_method": "api_test",
        "request": {"method": "GET", "url": "/api/plans"},
    }

    outcome = run_deterministic(vp)

    assert outcome.status == OUTCOME_ESCALATE
    assert outcome.route == LLM_ROUTE
    assert outcome.reasons, "an escalation must say what triggered it"
    assert outcome.verdict is None, "an escalation carries no verdict"
    assert outcome.evidence.get("issues"), (
        "an escalation must carry the evidence that triggered it"
    )
    assert outcome.is_deterministic is False
    assert outcome.needs_llm is True


def test_outcome_escalates_when_the_runner_crashs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that blows up is "cannot judge", not "the deliverable broke"."""

    def _boom(vp, *, artifact_dir=None):
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(
        "verification_api_runner.run_api_verification", _boom
    )

    outcome = run_deterministic(_api_vp())

    assert outcome.status == OUTCOME_ESCALATE
    assert outcome.needs_llm is True
    assert "runner exploded" in " ".join(outcome.reasons)


def test_outcome_escalates_when_the_runner_gives_no_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that returns no verdict has not judged anything."""

    def _silent(vp, *, artifact_dir=None):
        return {}

    monkeypatch.setattr(
        "verification_api_runner.run_api_verification", _silent
    )

    outcome = run_deterministic(_api_vp())

    assert outcome.status == OUTCOME_ESCALATE
    assert outcome.needs_llm is True


def test_outcome_dataclass_is_importable_and_named() -> None:
    """The three-state result is a named, importable type."""
    assert DeterministicOutcome.__name__ == "DeterministicOutcome"
    assert (OUTCOME_PASS, OUTCOME_FAIL, OUTCOME_ESCALATE) == (
        "PASS",
        "FAIL",
        "ESCALATE",
    )


# ---------------------------------------------------------------------------
# the deterministic path opens no LLM session
# ---------------------------------------------------------------------------


def test_deterministic_path_opens_no_llm_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grading a VP deterministically must not construct a coding tool."""
    import coding_tool
    from verification_api_runner import ApiRunResult

    def _no_llm(*args, **kwargs):
        raise AssertionError(
            "the deterministic path must not open an LLM session"
        )

    monkeypatch.setattr(coding_tool, "ClaudeCodingTool", _no_llm)
    monkeypatch.setattr(
        "verification_api_runner.run_api_verification",
        lambda vp, *, artifact_dir=None: ApiRunResult(
            status="PASSED", reasons=["ok"], evidence={},
        ),
    )

    outcome = run_deterministic(_api_vp())

    assert outcome.status == OUTCOME_PASS


@pytest.mark.asyncio
async def test_subagent_run_takes_the_deterministic_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``VerificationSubAgent.run`` must branch to the runner, not a session."""
    import coding_tool
    from verification_api_runner import ApiRunResult
    from verification_subagent import VerificationSubAgent

    def _no_llm(*args, **kwargs):
        raise AssertionError(
            "the deterministic path must not open an LLM session"
        )

    monkeypatch.setattr(coding_tool, "ClaudeCodingTool", _no_llm)
    monkeypatch.setattr(
        "verification_api_runner.run_api_verification",
        lambda vp, *, artifact_dir=None: ApiRunResult(
            status="PASSED", reasons=["all assertions passed"], evidence={},
        ),
    )

    agent = VerificationSubAgent(method="api_test", max_retries=0)
    verdict = await agent.run(
        vp_node=_api_vp(), log_dir=tmp_path / "logs",
    )

    assert verdict.verdict == "PASSED"
    assert verdict.reasons == ["all assertions passed"]


# ---------------------------------------------------------------------------
# the dispatch never exceeds the whole-library scan's yardstick
# ---------------------------------------------------------------------------


def test_dispatch_never_exceeds_scan_criteria(tmp_path: Path) -> None:
    """Whatever the dispatch calls deterministic, the scan calls so too.

    The scan (task 4) is the yardstick for "can be graded without a
    model". If the dispatch were more permissive than the scan, the two
    would report different numbers for the same library — and the scan's
    number is the one the benefit ceiling is measured against.
    """
    plan_dir = tmp_path / "20260101-sample"
    plan_dir.mkdir()
    (plan_dir / "verification_plan.json").write_text(
        """
        {
          "verification_points": [
            {"id": "VP-1", "verification_method": "api_test",
             "request": {"method": "GET", "url": "/api/plans"},
             "assertions": [{"status": 200}]},
            {"id": "VP-2", "verification_method": "api_test",
             "request": {"method": "GET", "url": "/api/plans"}},
            {"id": "VP-3", "verification_method": "full_ci",
             "ci_entry": "python3 -m pytest tests/unit"},
            {"id": "VP-4", "verification_method": "full_ci",
             "ci_entry": "pytest tests/unit/test_one_thing.py"},
            {"id": "VP-5", "verification_method": "code_review"}
          ]
        }
        """,
        encoding="utf-8",
    )

    report = scan_plans(tmp_path)
    scanned = report["plans"][0]["deterministic"]

    dispatched = {
        method: 0 for method in DETERMINISTIC_METHODS
    }
    points = [
        {"id": "VP-1", "verification_method": "api_test",
         "request": {"method": "GET", "url": "/api/plans"},
         "assertions": [{"status": 200}]},
        {"id": "VP-2", "verification_method": "api_test",
         "request": {"method": "GET", "url": "/api/plans"}},
        {"id": "VP-3", "verification_method": "full_ci",
         "ci_entry": "python3 -m pytest tests/unit"},
        {"id": "VP-4", "verification_method": "full_ci",
         "ci_entry": "pytest tests/unit/test_one_thing.py"},
        {"id": "VP-5", "verification_method": "code_review"},
    ]
    for vp in points:
        route, mode = resolve(vp)
        if mode == DETERMINISTIC_MODE:
            dispatched[vp["verification_method"]] += 1

    for method in DETERMINISTIC_METHODS:
        assert dispatched[method] <= scanned[method], (
            f"the dispatch calls {dispatched[method]} {method} VPs "
            f"deterministic, but the scan only counts {scanned[method]} — "
            "the dispatch must never be more permissive than the scan"
        )

    # The fixture must actually exercise both sides, or the assertion
    # above would hold vacuously.
    assert scanned["api_test"] == 1
    assert scanned["full_ci"] == 1
    assert dispatched["api_test"] == 1
    assert dispatched["full_ci"] == 1
