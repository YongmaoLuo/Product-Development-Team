"""2026-10-21: 对抗性要求并入第一遍判定提示词。

背景
----
删除判定之后的逐 VP 补审（per-VP supplement review）之后，对抗性
要求不能跟着消失 —— 它被并入第一遍
``VerificationSubAgent._build_prompt``：

  1. 计划里的 ``related_prd_criteria`` 现在会真正进入 sub-agent 提示词
     （此前该字段从未传给 LLM）；
  2. 提示词里新增一段 ``ADVERSARIAL SELF-CHECK``，要求 sub-agent 在
     下判定前写下至少两条反假设并逐条用证据证伪。

这些测试是**纯字符串**断言：不调 LLM、不碰文件系统，只有
``_build_prompt`` 一个纯函数（``vp_node`` 是 dict，``attempt`` 是 int）。

2026-10-29 追加
---------------
``code_review`` 的结论靠行号落地，而"项目根"这个词指代不明：计划目录
（``citations.json`` 的所在地）通常在**另一个 checkout** 里，那里同名
文件的行号不是本 VP 的证据。VP-010 在第 1 轮就是这么把 30 条引用全部
判成 ``snippet_mismatch`` 的 —— 引用的树和判据的树不是同一棵。
契约因此必须点名**唯一**一棵允许引用的树，并用行号证明核对没有被放宽。
"""

import asyncio
import sys
from pathlib import Path

import pytest


# Ensure ``backend/`` is on ``sys.path`` so ``import verification_subagent``
# works regardless of the test runner entry point.  Mirrors the pattern
# used by ``test_verification_subagent_verdict.py``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from verification_subagent import VerificationSubAgent  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def code_review_subagent() -> VerificationSubAgent:
    """A sub-agent configured the same way the executor constructs one."""
    return VerificationSubAgent(method="code_review")


@pytest.fixture
def vp_node() -> dict:
    """Minimal verification-point node carrying a PRD criterion."""
    return {
        "id": "VP-001",
        "title": "t",
        "verification_method": "code_review",
        "priority": "high",
        "expected_result": "e",
        "related_prd_criteria": "PRD-XYZ",
        "evidence_artifact_dir": "/tmp/x",
    }


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


def test_prompt_carries_prd_criterion(
    code_review_subagent: VerificationSubAgent, vp_node: dict
) -> None:
    """The PRD criterion the plan recorded must reach the sub-agent LLM."""
    prompt = code_review_subagent._build_prompt(vp_node, 0)
    assert "PRD criterion: PRD-XYZ" in prompt


def test_prompt_demands_adversarial_self_check(
    code_review_subagent: VerificationSubAgent, vp_node: dict
) -> None:
    """The adversarial self-check replaced the deleted second pass.

    If this assertion ever fails, the adversarial requirement silently
    disappeared along with the per-VP supplement review it replaced.
    """
    prompt = code_review_subagent._build_prompt(vp_node, 0)
    assert "ADVERSARIAL SELF-CHECK" in prompt
    assert "counter-hypotheses" in prompt


def test_prompt_omits_prd_line_when_criterion_absent(
    code_review_subagent: VerificationSubAgent, vp_node: dict
) -> None:
    """A VP without ``related_prd_criteria`` must not raise or emit the line."""
    vp_node.pop("related_prd_criteria")
    prompt = code_review_subagent._build_prompt(vp_node, 0)
    assert "PRD criterion:" not in prompt
    # The adversarial section is unconditional — it does not depend on
    # whether the plan recorded a PRD criterion.
    assert "ADVERSARIAL SELF-CHECK" in prompt


def test_prompt_carries_process_safety_hard_rule(
    code_review_subagent: VerificationSubAgent, vp_node: dict
) -> None:
    """The prompt must forbid pattern-based process killing.

    2026-10-27: a verification sub-agent ran ``pkill -f "backend/cli.py"``
    and SIGTERMed the repair executors of three unrelated plans — every
    PDT executor on the host has that path in its argv. The prompt is one
    of the two defenses; the other is the ``pkill_killall_block``
    PreToolUse hook (see ``test_verification_process_safety_guard.py``).

    If this assertion ever fails, the sub-agent is being handed no
    instruction about how to stop a process — and the default it falls
    back on is the one that killed three plans.
    """
    prompt = code_review_subagent._build_prompt(vp_node, 0)
    assert "PROCESS SAFETY" in prompt
    assert "killall" in prompt
    assert "pkill" in prompt


# ---------------------------------------------------------------------------
# 2026-10-29: 引用必须落在计划的 project_dir 上
# ---------------------------------------------------------------------------
#
# 一条 code_review 引用是 ``(file, line, snippet)`` 三元组。框架拿
# ``file`` 去**计划的 project_dir** 里解析，行号对不上就是 ``snippet_mismatch``。
# 所以"引用的是哪棵树"不是措辞问题，是判据问题：计划目录一般在另一个
# checkout（那里的同名文件行号不同），引错树 = 判据无效。

#: 本仓库的根（``backend/tests/unit/`` 往上三层是 ``backend/``，再上一层）。
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: 本轮判据所在的文件（VP-010 的六条内容要求都指向它）。
_SERVICE_MANAGER = _REPO_ROOT / "backend" / "service_manager.py"

#: 另一棵树（正式检出）与本分支的这份文件的行差：第 1 轮在
#: ``service_manager.py`` 第 918 行之后插入了进程组收窄代码，本分支之后的
#: 全部行整体下移 96 行。
_LINE_SHIFT_VS_OTHER_CHECKOUT = 96


def _anchor_line(needle: str) -> int:
    """The 1-based line of ``needle`` in this repository's file.

    Derived rather than hard-coded on purpose: the anchor moves whenever
    anything above it is edited, and a test that pins a stale number
    fails for the wrong reason.
    """
    lines = _SERVICE_MANAGER.read_text(encoding="utf-8").splitlines()
    for number, text in enumerate(lines, start=1):
        if needle in text:
            return number
    raise AssertionError(f"anchor not found in service_manager.py: {needle!r}")


def test_evidence_contract_states_absolute_project_root(
    code_review_subagent: VerificationSubAgent, tmp_path: Path
) -> None:
    """The contract must name the ONE tree a citation may come from.

    "the project root" was ambiguous: the plan directory lives in another
    checkout, so a sub-agent that read the file there cited line numbers
    that mean nothing in this one. The absolute path removes the
    ambiguity — there is now a single tree to read from.
    """
    project_root = str(tmp_path / "the-plan-project")
    vp_node = {
        "id": "VP-010",
        "title": "t",
        "verification_method": "code_review",
        "priority": "high",
        "expected_result": "e",
        "evidence_artifact_dir": str(
            tmp_path / "plan" / "vp_artifacts" / "VP-010"
        ),
        "project_root": project_root,
    }

    prompt = code_review_subagent._build_prompt(vp_node, 0)

    assert project_root in prompt
    assert "the ONLY tree your citations may come from" in prompt
    assert "DIFFERENT checkout" in prompt
    assert "not evidence for this VP" in prompt


def test_evidence_contract_without_project_root_still_builds(
    code_review_subagent: VerificationSubAgent, vp_node: dict
) -> None:
    """A VP node from before the field existed must not break the prompt.

    The paragraph is an addition, not a replacement: the rest of the
    contract (the artifact path, the JSON shape, the rules) is unchanged
    when ``project_root`` is absent.
    """
    vp_node.pop("project_root", None)

    prompt = code_review_subagent._build_prompt(vp_node, 0)

    assert "citations.json" in prompt
    assert "the ONLY tree your citations may come from" not in prompt


def test_the_runner_injects_the_project_root(tmp_path: Path) -> None:
    """The executor must tell the sub-agent which tree to cite from.

    Without the field the sub-agent only ever sees "the project root",
    which is how the citations ended up pointing at the plan's own
    checkout instead of the plan's project dir.
    """
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
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

    assert seen and seen[0]["project_root"] == str(agent.project_dir)


def test_citation_to_stale_checkout_line_is_rejected() -> None:
    """负控：引用另一棵树的行锚必须被逐条拒绝。

    另一棵树（正式检出）里这份文件的行号比本分支少 96 行。拿那个行锚
    来引本分支的文件，行号落在别处 —— 核对必须报 ``snippet_mismatch``，
    而不是"文件里有这句话就算数"。
    """
    from verification_evidence import verify_citations

    current = _anchor_line("def _data_plane_is_acceptable(")
    stale = current - _LINE_SHIFT_VS_OTHER_CHECKOUT
    assert stale >= 1
    lines = _SERVICE_MANAGER.read_text(encoding="utf-8").splitlines()
    assert "def _data_plane_is_acceptable(" not in lines[stale - 1]

    issues = verify_citations([{
        "file": "backend/service_manager.py",
        "line": stale,
        "snippet": "def _data_plane_is_acceptable(",
    }], _REPO_ROOT)

    assert [issue.kind for issue in issues] == ["snippet_mismatch"]


def test_citation_at_current_line_resolves() -> None:
    """当前树上的行锚必须全部解析成功（VP-010 的六条内容要求）。

    判据：签名带 decl / 判据取自声明 / 从声明库里取行数 / expected_rows
    兜底仍在 / DATA_PLANE_BROKEN 仍被拒 / 调用处把 decl 传了进去。
    """
    from verification_evidence import verify_citations

    wanted = [
        "def _data_plane_is_acceptable(",
        "decl: Optional[ServiceDeclaration] = None,",
        "if evidence.verdict == DATA_PLANE_BROKEN:",
        "declared = declared_state_db_path(decl)",
        "rows = _routing_rows_in(declared)",
        "return not (expected_rows or 0) > 0",
        "_data_plane_is_acceptable(evidence, expected_rows, decl)",
    ]

    citations = [
        {
            "file": "backend/service_manager.py",
            "line": _anchor_line(needle),
            "snippet": needle,
        }
        for needle in wanted
    ]

    issues = verify_citations(citations, _REPO_ROOT)

    assert issues == []
