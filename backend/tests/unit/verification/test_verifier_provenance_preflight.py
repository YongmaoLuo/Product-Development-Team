"""TDD spec —— 出处预检：**出处对不上就必须响**（F2 的第三条）。

背景
----
2026-11-05 的 VP-015 失败了两轮，两轮的 detail 逐字节相同，而工作树里
那份评分器早就修好了。框架当时**答不了**"我是不是在跑新代码" —— 于是
"修复没部署到位"这件事从一个**一次就能定位**的显式信号，退化成一轮又
一轮的重复失败。

这条 spec 钉住那条信号：

1. **被测树就是框架仓、却回落到 harness 副本** → 预检产出显式 issue
   （负控：这正是 2026-11-05 那种静默重复失败）；
2. **被测树就是框架仓、评分器确实来自被测树** → 没有 issue；
3. **被测树不是框架仓** → 没有 issue（用 harness 副本是设计，不是事故）；
4. 预检的结论**在轮次开工那一刻就写进计划的 audit**
   （``start_verification_cycle`` 的第一步），所以它不会在别的东西被丢掉
   的时候一起消失。

   2026-11-06：出处预检原本挂在根因链的 preflight 上。链被删了，查被
   搬到 ``start_verification_cycle`` 的开头 —— 它跟链无关，它回答的是
   "这一轮会被哪份评分器判"。

测试命令::

    pytest backend/tests/unit/verification/test_verifier_provenance_preflight.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification.orchestrator import VerificationOrchestrator  # noqa: E402
from verification_ci_runner import run_ci_verification  # noqa: E402
from verification_ci_runner_loader import (  # noqa: E402
    RESOLUTION_HARNESS,
    RESOLUTION_PROJECT,
)


_PROJECT_RUNNER_SOURCE = '''\
"""A project-side stand-in for ``verification_ci_runner``."""


def run_ci_verification(vp, *, project_dir, artifact_dir=None):
    return {"status": "PASSED", "reasons": [], "evidence": {}}
'''

_BROKEN_RUNNER_SOURCE = "def run_ci_verification(vp, *, project_dir::\n"


def _framework_checkout(root: Path, source: str = _PROJECT_RUNNER_SOURCE) -> Path:
    """A project tree that *is* a checkout of this framework."""
    backend = root / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "pyproject.toml").write_text(
        "[project]\nname = 'fake-backend'\n", encoding="utf-8",
    )
    (backend / "verification_ci_runner.py").write_text(
        source, encoding="utf-8",
    )
    return backend / "verification_ci_runner.py"


def _orchestrator(plan_dir: Path, project_dir: Path) -> VerificationOrchestrator:
    """An orchestrator with only the two directories.

    The provenance preflight reads nothing else: it asks the loader
    which runner it would use for ``project_dir``.
    """
    orch = VerificationOrchestrator.__new__(VerificationOrchestrator)
    orch.plan_dir = plan_dir
    orch.project_dir = project_dir
    return orch


def _plan_dir(tmp_path: Path) -> Path:
    plan = tmp_path / "plans" / "p1"
    plan.mkdir(parents=True)
    return plan


# ---------------------------------------------------------------------------
# 1. 出处对不上 → 响（负控）
# ---------------------------------------------------------------------------
def test_mismatch_between_the_project_tree_and_the_runner_is_loud(tmp_path):
    """被测树就是框架仓、却回落到 harness 副本 → 预检必须报出来。"""
    project = tmp_path / "worktree"
    _framework_checkout(project, source=_BROKEN_RUNNER_SOURCE)
    orch = _orchestrator(_plan_dir(tmp_path), project)

    preflight = orch._verifier_provenance_preflight()

    assert preflight["project_is_framework_checkout"] is True
    assert preflight["resolution"] == RESOLUTION_HARNESS
    assert preflight["issues"], "出处对不上时预检必须出声"
    issue = preflight["issues"][0]
    assert issue["code"] == "verifier_provenance_mismatch"
    assert preflight["fallback_reason"]


def test_the_mismatch_issue_names_the_two_files(tmp_path):
    """issue 说得出"该用哪份"和"实际用了哪份"。"""
    project = tmp_path / "worktree"
    runner_path = _framework_checkout(project, source=_BROKEN_RUNNER_SOURCE)
    orch = _orchestrator(_plan_dir(tmp_path), project)

    preflight = orch._verifier_provenance_preflight()

    detail = preflight["issues"][0]["detail"]
    assert str(runner_path) in detail
    assert preflight["module_path"] in detail


# ---------------------------------------------------------------------------
# 2. 出处对得上 → 不响
# ---------------------------------------------------------------------------
def test_a_healthy_project_checkout_raises_nothing(tmp_path):
    """评分器确实来自被测树 → 没有 issue。"""
    project = tmp_path / "worktree"
    _framework_checkout(project)
    orch = _orchestrator(_plan_dir(tmp_path), project)

    preflight = orch._verifier_provenance_preflight()

    assert preflight["resolution"] == RESOLUTION_PROJECT
    assert preflight["issues"] == []
    assert preflight["module_path"] == str(
        project / "backend" / "verification_ci_runner.py"
    )


# ---------------------------------------------------------------------------
# 3. 不是框架仓的被测树 → 不响（设计，不是事故）
# ---------------------------------------------------------------------------
def test_a_plain_project_raises_nothing(tmp_path):
    """被测树不是框架仓 → 用 harness 副本是设计，不报 issue。"""
    project = tmp_path / "some-project"
    (project / "backend").mkdir(parents=True)
    orch = _orchestrator(_plan_dir(tmp_path), project)

    preflight = orch._verifier_provenance_preflight()

    assert preflight["project_is_framework_checkout"] is False
    assert preflight["issues"] == []


# ---------------------------------------------------------------------------
# 4. 结论在轮次开工时落到 audit（生产入口）
# ---------------------------------------------------------------------------
def test_the_round_start_writes_the_provenance_mismatch_to_the_audit(
    tmp_path,
):
    """轮次开工 → 出处对不上 → 计划的 audit 里有那一条。

    走的是**生产入口** ``start_verification_cycle``：出处预检是轮次的
    第一步，它的问题「这一轮会被哪份评分器判」在轮次真正跑起来之前就
    问掉了，而答案落在计划的 audit 里 —— 不是落在某个会被丢掉的东西上。
    """
    from unittest.mock import Mock

    from verification_persistence import AUDIT_LOG_FILENAME

    project = tmp_path / "worktree"
    _framework_checkout(project, source=_BROKEN_RUNNER_SOURCE)
    plan = _plan_dir(tmp_path)
    # The plan's state: the round has to be able to start (the phases
    # have to be legal) for the test to be about the provenance and not
    # about the phase machine.
    (plan / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan.name,
            "current_phase": "executing",
            "completed_phases": ["execution"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {},
            "verification": {
                "status": "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
            },
        }),
        encoding="utf-8",
    )
    orch = VerificationOrchestrator(
        plan_dir=plan, project_dir=project, coding_tool=Mock(),
    )
    orch.verification_agent = Mock()
    orch.verification_agent.run_full_verification.return_value = {
        "overall_status": "PASSED",
        "verification_results": [],
        "requirement_deviations": [],
    }

    orch.start_verification_cycle(round_number=1)

    audit = plan / "logs" / AUDIT_LOG_FILENAME
    assert audit.exists(), (
        f"轮次开工必须把出处结论写进 audit，{audit} 不存在"
    )
    entries = [
        json.loads(line)
        for line in audit.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    mismatches = [
        entry for entry in entries
        if entry.get("event_type") == "verifier_provenance_mismatch"
    ]
    assert mismatches, (
        f"audit 里必须有出处对不上的那一条，实际是 {entries!r}"
    )
    data = mismatches[0].get("data") or {}
    assert data.get("issues"), "那一条必须带着响亮的信号"
    assert data.get("resolution") == RESOLUTION_HARNESS


# ---------------------------------------------------------------------------
# 5. 失败的身份（2026-11-05 第 3 轮）
# ---------------------------------------------------------------------------
#
# 第 3 轮 VP-015 的失败指纹 ``be0a4e421f361e5a`` → ``db929c18eeb90214``，
# 而缺陷**逐字节没变**（三轮的 schema detail 一样，退出码都是 null，
# 一个用例都没跑过）。指纹之所以动，是因为它取自**报告的措辞**：
# 被 schema 拒绝的 full_ci 没有机器写的失败集合，于是旧路把证据的第一行
# 散文算进了哈希。报告换个说法 → 指纹变 → 循环判成"根因演进" → 一轮
# 烧在一个根本没变的问题上。
#
# 这一节钉住那条不变量：**同一个缺陷、换了措辞，指纹不变。**


def _rejected_by_the_preflight(
    plan_dir: Path,
    project_dir: Path,
    entry: str = "bash scripts/does_not_exist.sh",
) -> None:
    """让真的评分器拒绝一条入口，并把机器写的证据落在磁盘上。"""
    run_ci_verification(
        {
            "id": "VP-015",
            "verification_method": "full_ci",
            "ci_entry": entry,
        },
        project_dir=project_dir,
        artifact_dir=plan_dir / "vp_artifacts",
    )


def _report_entry(evidence: str) -> dict:
    """一条失败的报告条目 —— 只有散文不一样。"""
    return {
        "id": "VP-015",
        "status": "FAILED",
        "actual_result": "整仓门禁根本没有执行：schema 预检拒绝了这条入口。",
        "evidence": evidence,
    }


class TestFailureIdentity:
    """指纹认的是缺陷，不是报告的措辞。"""

    def _orchestrator_with_a_rejected_entry(self, tmp_path):
        plan = _plan_dir(tmp_path)
        project = tmp_path / "worktree"
        (project / "scripts").mkdir(parents=True)
        _rejected_by_the_preflight(plan, project)
        return _orchestrator(plan, project)

    def test_a_reworded_explanation_does_not_change_the_fingerprint(
        self, tmp_path,
    ):
        """同一个缺陷、换了措辞 → 指纹不变。

        这正是第 3 轮烧掉一轮的那个假信号：措辞一变，指纹就变，
        循环判成"根因演进"，于是又拿旧根因的方案去打第二轮。
        """
        orch = self._orchestrator_with_a_rejected_entry(tmp_path)

        first = orch._round_fingerprints({
            "verification_results": [
                _report_entry("执行结果记录：schema 预检即失败。"),
            ],
        })["VP-015"]
        second = orch._round_fingerprints({
            "verification_results": [
                _report_entry("验证执行器记录（原样）：schema 预检即失败。"),
            ],
        })["VP-015"]

        assert first == second, "换了措辞就换指纹 = 又一次假信号"

    def test_the_schema_rejection_is_fingerprinted_by_identity(
        self, tmp_path,
    ):
        """有机器写的失败集合 → 走身份态（旧路把散文算进哈希）。"""
        orch = self._orchestrator_with_a_rejected_entry(tmp_path)

        records = orch._round_fingerprint_records({
            "verification_results": [
                _report_entry("执行结果记录：schema 预检即失败。"),
            ],
        })

        assert records["VP-015"]["rule"] == "identity-v1"

    def test_the_machine_written_set_is_where_it_comes_from(
        self, tmp_path,
    ):
        """身份来自落盘的那份机器证据 —— 说得出是哪条规则。"""
        orch = self._orchestrator_with_a_rejected_entry(tmp_path)

        entry = _report_entry("执行结果记录：schema 预检即失败。")
        assert orch._failed_case_ids("VP-015", entry) == [
            "schema:ci_entry_path_missing:scripts/does_not_exist.sh",
        ]

    def test_without_machine_written_evidence_the_legacy_rule_still_applies(
        self, tmp_path,
    ):
        """没有机器证据时仍旧走旧路 —— 不假装知道。

        磁盘上什么证据都没有的时候，框架只能认报告的措辞；这一条把
        那个边界钉住，免得有人以为身份态是永远成立的。
        """
        orch = _orchestrator(_plan_dir(tmp_path), tmp_path / "no-tree")

        records = orch._round_fingerprint_records({
            "verification_results": [
                _report_entry("执行结果记录：schema 预检即失败。"),
            ],
        })

        assert records["VP-015"]["rule"] == "legacy-v2"

    def test_two_spellings_of_the_same_defect_share_one_fingerprint(
        self, tmp_path,
    ):
        """同一个缺陷、两种写法 → **同一个**指纹（2026-11-06）。

        身份取自机器写的失败集合，集合里的 id 取自「哪条路径不存在」。
        ``./scripts/does_not_exist.sh`` 与 ``scripts/does_not_exist.sh``
        是同一个缺陷 —— 路径写法不同不该让指纹换一次。
        """
        plan = _plan_dir(tmp_path)
        project = tmp_path / "worktree"
        (project / "scripts").mkdir(parents=True)
        orch = _orchestrator(plan, project)

        _rejected_by_the_preflight(
            plan, project, "bash ./scripts/does_not_exist.sh",
        )
        first = orch._round_fingerprints({
            "verification_results": [
                _report_entry("执行结果记录：schema 预检即失败。"),
            ],
        })["VP-015"]

        _rejected_by_the_preflight(
            plan, project, "bash scripts/does_not_exist.sh",
        )
        second = orch._round_fingerprints({
            "verification_results": [
                _report_entry("验证执行器记录（原样）：schema 预检即失败。"),
            ],
        })["VP-015"]

        assert first == second, "同一个缺陷换了写法就换指纹 = 又一次假信号"

    def test_a_different_machine_written_set_is_a_different_fingerprint(
        self, tmp_path,
    ):
        """机器写的集合**确实**不同 ⇒ 指纹不同 —— 这是设计，不是故障。

        两条**不同的**缺陷（两个不同的路径不存在）必须有两个身份：把
        它们合成一个，循环就会把"换了个缺陷"读成"同一个缺陷"。
        """
        plan = _plan_dir(tmp_path)
        project = tmp_path / "worktree"
        (project / "scripts").mkdir(parents=True)
        orch = _orchestrator(plan, project)

        _rejected_by_the_preflight(
            plan, project,
            "bash scripts/does_not_exist.sh && bash scripts/also_missing.sh",
        )
        two_cases = orch._round_fingerprints({
            "verification_results": [
                _report_entry("执行结果记录：schema 预检即失败。"),
            ],
        })["VP-015"]

        _rejected_by_the_preflight(
            plan, project, "bash scripts/does_not_exist.sh",
        )
        one_case = orch._round_fingerprints({
            "verification_results": [
                _report_entry("验证执行器记录（原样）：schema 预检即失败。"),
            ],
        })["VP-015"]

        assert two_cases != one_case

    def test_the_schema_id_spellings_canonicalize_to_one_case(self, tmp_path):
        """同一条 schema id 的两种写法 ⇒ **同一条** case id。

        门禁的 schema id 是框架自己铸的（``schema:<code>:<subject>``），
        它的 subject 是一条路径。路径带着命令行的标点进来
        （``backend/.venv)`` —— 那个 ``)`` 是 shell 的分组括号，不是
        文件名的一部分）。身份不能建在写法上。
        """
        orch = _orchestrator(_plan_dir(tmp_path), tmp_path / "no-tree")

        plain = orch._failed_case_ids("VP-015", {
            "failed_assertions": [
                "schema:ci_entry_path_missing:backend/.venv",
            ],
        })
        bracketed = orch._failed_case_ids("VP-015", {
            "failed_assertions": [
                "schema:ci_entry_path_missing:backend/.venv)",
            ],
        })

        assert plain == bracketed == [
            "schema:ci_entry_path_missing:backend/.venv",
        ]

    def test_the_three_recorded_rounds_are_one_failure(self, tmp_path):
        """VP-015 记下来的三轮 ⇒ **一个**失败（2026-11-06）。

        计划自己的记录（``verification_failure_history.json``）里，三轮
        各自记下了自己那一份机器写的失败集合 —— 逐字抄，不改一个字：

        * 第 1 轮两条：``backend/.venv`` 与 ``backend/.venv)``；
        * 第 2 轮一条：``backend/.venv)``；
        * 第 3 轮一条：``backend/.venv)``。

        三轮是**同一个**缺陷（入口指着的 venv 不存在），第 1 轮只是把
        同一个路径写了两遍。身份必须是一个：分成两个，循环就判成
        "根因演进"，于是又拿旧根因的方案去打下一轮 —— 2026-11-05
        烧掉的那一轮就是这么没的。
        """
        orch = _orchestrator(_plan_dir(tmp_path), tmp_path / "no-tree")

        recorded = [
            [
                "schema:ci_entry_path_missing:backend/.venv",
                "schema:ci_entry_path_missing:backend/.venv)",
            ],
            ["schema:ci_entry_path_missing:backend/.venv)"],
            ["schema:ci_entry_path_missing:backend/.venv)"],
        ]

        fingerprints = []
        for cases in recorded:
            entry = _report_entry("执行结果记录：schema 预检即失败。")
            entry["failure_class"] = "deliverable_defect"
            entry["failed_assertions"] = cases
            fingerprints.append(orch._round_fingerprints({
                "verification_results": [entry],
            })["VP-015"])

        assert len(set(fingerprints)) == 1, (
            "三轮记的是同一个缺陷，却算出 {0} 个身份".format(
                len(set(fingerprints)),
            )
        )
