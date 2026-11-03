"""2026-11-01: 取证产物必须暂存在 project_dir 内、且在被服务的 plans 树之外。

背景
----
``code_review`` 的引用由 ``verification_evidence.verify_citations`` 逐条解析：
``target = (project_dir / rel)``，文件必须存在、行号在范围内、snippet 必须命中。
所以"引用的是哪棵树"是判据问题，不是措辞问题：计划目录（``citations.json``
的所在地）通常在**另一个 checkout** 里，那里同名文件的行号不是本 VP 的证据。

VP-017 第 1 轮就是这么丢的：39 条引用里 26 条 ``missing_file`` —— 引用指向计划
目录里的取证产物（``kill_forensics.md`` / ``audit_ledger_excerpt.jsonl`` /
``session_census_at_signal.txt`` / ``sentinel_group_proof.txt`` /
``verification_plan.json``），而框架按 ``project_dir`` 解析，那里没有那棵树。
执行方自己的自检脚本按计划目录解析，于是报"unresolved=0"，而框架报 26 条
``missing_file`` —— 同一个判据，两个基准。

修复分两半：

  1. 框架在派发 code_review VP 之前，把计划目录的取证产物**逐字节**暂存到
     ``project_dir`` 内、但不在被服务的 plans 树里的位置
     （``<project_dir>/.pdt/evidence/<plan_id>/``）；
  2. 契约点名那棵暂存树：计划里的取证产物只能从暂存根引用（``project_dir``
     相对路径），计划目录本身不是可引用的树。

暂存位置两个条件缺一不可：

  * 在 ``project_dir`` 之内 —— 框架解析引用的基准；
  * 不在 ``PLANS_DIR`` 之下 —— plans 树里多出一个目录会让 api 服务的
    ``_refuse_when_data_source_does_not_describe_the_tree`` 答 503
    （``state_db_does_not_describe_plans_dir``），把 UI 验证点顶成失败。

这些用例不调 LLM、不起服务：被测的是暂存这一个纯文件动作 + 契约文本。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: A line of the forensic product the plan's citations point at.  The
#: snippet has to be quotable verbatim — that is what makes the staged
#: copy citable with the plan's own line numbers.
_FORENSIC_LINE = "the audit hook records every os.kill and every os.killpg"

#: The second line, so a citation can point at line 2 and quote it.
_FORENSIC_LINE_2 = "signal 15 arrived at the 143 moment"


@pytest.fixture
def plan_and_project(tmp_path: Path):
    """A plan directory holding forensic artifacts, and a project dir.

    The project dir carries a ``plans/`` tree on purpose: that tree is
    what the api service serves, so the staging root must not land in it.
    """
    plan_dir = tmp_path / "plan" / "20261027-service-readiness-2"
    artifacts = plan_dir / "vp_artifacts" / "VP-014"
    artifacts.mkdir(parents=True)
    (artifacts / "kill_forensics.md").write_text(
        f"# kill forensics\n{_FORENSIC_LINE}\n{_FORENSIC_LINE_2}\n",
        encoding="utf-8",
    )
    (plan_dir / "verification_plan.json").write_text(
        '{"verification_points": [{"id": "VP-014"}]}\n', encoding="utf-8",
    )

    project_dir = tmp_path / "project"
    (project_dir / "plans").mkdir(parents=True)

    return plan_dir, project_dir


# ---------------------------------------------------------------------------
# The staging itself
# ---------------------------------------------------------------------------


def test_the_plan_artifacts_are_staged_into_the_project_dir(plan_and_project):
    """The forensic products must be readable from inside ``project_dir``.

    Byte-for-byte: a staged copy that differs by a byte carries different
    line numbers, and the citation stops resolving.
    """
    from verification_evidence import stage_plan_artifacts

    plan_dir, project_dir = plan_and_project

    root = stage_plan_artifacts(plan_dir, project_dir, plan_dir.name)

    staged = root / "vp_artifacts" / "VP-014" / "kill_forensics.md"
    assert staged.is_file()
    assert staged.read_bytes() == (
        plan_dir / "vp_artifacts" / "VP-014" / "kill_forensics.md"
    ).read_bytes()
    assert (root / "verification_plan.json").is_file()


def test_the_staging_root_is_inside_the_project_dir_and_outside_the_plans_tree(
    plan_and_project,
):
    """Both halves of the placement rule, or the placement is wrong.

    Inside ``project_dir`` because that is what the framework resolves a
    citation against; outside the plans tree because a plan directory
    there that the state db does not know makes ``GET /api/plans`` answer
    503 and pushes the UI verification point to failure.
    """
    from verification_evidence import stage_plan_artifacts

    plan_dir, project_dir = plan_and_project
    plans_dir = project_dir / "plans"

    root = stage_plan_artifacts(plan_dir, project_dir, plan_dir.name)

    root.resolve().relative_to(project_dir.resolve())
    with pytest.raises(ValueError):
        root.resolve().relative_to(plans_dir.resolve())


def test_staging_twice_does_not_rewrite_the_tree(plan_and_project):
    """Staging is idempotent: a second round must not copy 7 MB again."""
    from verification_evidence import stage_plan_artifacts

    plan_dir, project_dir = plan_and_project

    first = stage_plan_artifacts(plan_dir, project_dir, plan_dir.name)
    marker = first / "vp_artifacts" / "VP-014" / "kill_forensics.md"
    stamp = marker.stat().st_mtime_ns
    second = stage_plan_artifacts(plan_dir, project_dir, plan_dir.name)

    assert second == first
    assert marker.stat().st_mtime_ns == stamp


def test_a_changed_source_refreshes_the_staged_copy(plan_and_project):
    """The staged copy follows its source; it does not freeze at the first round.

    2026-11-01（VP-020）：暂存面是**一次性快照**。计划目录里的取证产物
    更新之后，暂存副本还留着旧的那一份 —— 引用解析到的是一份没人能复现的
    文件（旧世代脚本的产物），而计划目录里的归档已经是当前脚本的产物。
    两面对不上，引用就失去归属。

    暂存本身不是 copy-once：内容不同就覆盖（``_stage_one`` 逐字节比较）。
    这条用例钉的就是这一点 —— 之前没有人钉它，所以那个过期副本一直活到
    下一轮之后。
    """
    from verification_evidence import stage_plan_artifacts

    plan_dir, project_dir = plan_and_project
    source = plan_dir / "vp_artifacts" / "VP-014" / "kill_forensics.md"

    first = stage_plan_artifacts(plan_dir, project_dir, plan_dir.name)
    staged = first / "vp_artifacts" / "VP-014" / "kill_forensics.md"
    assert staged.read_bytes() == source.read_bytes()

    # The source moves on: a new line lands at the end.
    source.write_text(
        f"# kill forensics\n{_FORENSIC_LINE}\n{_FORENSIC_LINE_2}\n"
        f"a fourth line the first round never produced\n",
        encoding="utf-8",
    )

    second = stage_plan_artifacts(plan_dir, project_dir, plan_dir.name)
    assert second == first
    assert staged.read_bytes() == source.read_bytes(), (
        "the staged copy still holds the source's earlier content, so the "
        "citable face answers for a product that has been replaced"
    )


# ---------------------------------------------------------------------------
# The runner stages before the sub-agent is dispatched
# ---------------------------------------------------------------------------


def test_the_runner_stages_the_artifacts_and_names_the_root(tmp_path: Path):
    """The executor must stage and hand the root to the sub-agent.

    The sub-agent is the one writing ``citations.json``: without the
    staging root in its node it has no way to know where the plan's
    forensic products became readable from.
    """
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan" / "20261027-service-readiness-2"
    artifacts = plan_dir / "vp_artifacts" / "VP-014"
    artifacts.mkdir(parents=True)
    (artifacts / "kill_forensics.md").write_text(_FORENSIC_LINE, encoding="utf-8")
    project_dir = tmp_path / "project"
    (project_dir / "plans").mkdir(parents=True)

    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )
    seen: list[dict] = []

    async def _capture(vp):
        seen.append(dict(vp))
        return {"status": "PASSED", "reasons": [], "evidence": {}}

    agent._run_single_vp_async = _capture  # type: ignore[assignment]
    plan = {"verification_points": [{
        "id": "VP-010", "title": "t", "verification_method": "code_review",
    }]}
    executor = agent._build_verification_executor(plan)

    asyncio.run(executor.sub_agent_runner(executor.verification_plan["vps"][0]))

    assert seen, "the sub-agent was never dispatched"
    staging_root = seen[0].get("evidence_staging_root")
    assert staging_root, "the runner did not name the staging root"
    staged = Path(staging_root) / "vp_artifacts" / "VP-014" / "kill_forensics.md"
    assert staged.is_file(), "the runner did not stage the plan's artifacts"


# ---------------------------------------------------------------------------
# The contract names the staging root
# ---------------------------------------------------------------------------


def test_the_contract_names_the_staging_root(tmp_path: Path):
    """The sub-agent must be told where the plan's artifacts became readable.

    Otherwise it cites the plan directory — a tree the framework cannot
    resolve — and every one of those citations is unverifiable.
    """
    from verification_subagent import VerificationSubAgent

    project_dir = tmp_path / "project"
    staging_root = project_dir / ".pdt" / "evidence" / "20261027-service-readiness-2"
    sub_agent = VerificationSubAgent(method="code_review")
    vp_node = {
        "id": "VP-010",
        "title": "t",
        "verification_method": "code_review",
        "priority": "high",
        "expected_result": "e",
        "evidence_artifact_dir": str(tmp_path / "plan" / "vp_artifacts" / "VP-010"),
        "project_root": str(project_dir),
        "evidence_staging_root": str(staging_root),
    }

    prompt = sub_agent._build_prompt(vp_node, 0)

    assert str(staging_root) in prompt
    assert "staged" in prompt
    assert "NOT a citable tree" in prompt


def test_the_contract_without_a_staging_root_still_builds(tmp_path: Path):
    """A node from before the field existed must not break the prompt."""
    from verification_subagent import VerificationSubAgent

    sub_agent = VerificationSubAgent(method="code_review")
    vp_node = {
        "id": "VP-010",
        "title": "t",
        "verification_method": "code_review",
        "priority": "high",
        "expected_result": "e",
        "evidence_artifact_dir": str(tmp_path / "plan" / "vp_artifacts" / "VP-010"),
        "project_root": str(tmp_path / "project"),
    }

    prompt = sub_agent._build_prompt(vp_node, 0)

    assert "citations.json" in prompt
    assert "staged" not in prompt


# ---------------------------------------------------------------------------
# The verifier is not relaxed
# ---------------------------------------------------------------------------


def test_a_citation_against_the_staged_copy_resolves(plan_and_project):
    """The staged copy is citable by its project-root-relative path."""
    from verification_evidence import stage_plan_artifacts, verify_citations

    plan_dir, project_dir = plan_and_project
    stage_plan_artifacts(plan_dir, project_dir, plan_dir.name)

    citations = [{
        "file": ".pdt/evidence/20261027-service-readiness-2/vp_artifacts/"
                "VP-014/kill_forensics.md",
        "line": 2,
        "snippet": _FORENSIC_LINE,
    }]

    assert verify_citations(citations, project_dir) == []


def test_a_citation_into_the_plan_directory_is_still_rejected(plan_and_project):
    """负控：暂存没有把校验器放宽。

    计划目录不在 ``project_dir`` 之内，引用它照样解析不了 —— 暂存只增加
    一条能走通的路，没有拿掉原来那道门。
    """
    from verification_evidence import verify_citations

    _plan_dir, project_dir = plan_and_project

    citations = [{
        "file": "plans/20261027-service-readiness-2/vp_artifacts/VP-014/"
                "kill_forensics.md",
        "line": 2,
        "snippet": _FORENSIC_LINE,
    }]

    issues = verify_citations(citations, project_dir)

    assert [issue.kind for issue in issues] == ["missing_file"]