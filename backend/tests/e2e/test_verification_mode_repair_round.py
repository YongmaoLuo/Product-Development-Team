"""E2E：验证分支的修复回路 —— 轮次、终态与不可变性。

这份文件守的是**修复回路**（verify → fail → repair → verify again）在验证
分支上的行为，以及回路跑完之后**被验证对象一个字节都没动**。

被测链路（全部走生产代码，不 mock）
----------------------------------

* :class:`VerificationOrchestrator` —— 轮次循环、失败判定、修复任务生成、
  同失败收敛、终态落库。
* :class:`VerificationAgent` —— 真的执行 ``api_test`` 验证点（真的发 HTTP
  请求、真的核断言），真的生成验证报告。
* ``RepairTaskGenerator`` / ``RepairTaskAssembler`` —— 真的生成修复任务。
* :class:`PlanState` + SQLite —— 真的写状态（``plan_routing`` /
  ``plan_verification``）。
* git —— 真的建 worktree、真的提交修复。

唯一的替身是**模型边界**：答案来自脚本化 fixture 文件。这与 task-34 的
E2E 冒烟是同一个取舍 —— 被测的是回路，不是模型。

修复那一步（把缺陷改好并提交）由测试代替执行器完成：执行器属于开发分支
的链路，不属于本文件的被测范围；这里它被压缩成"worktree 上的一个提交"，
因为本文件要守的不变式正是"这个提交落在 ``fix/<plan_id>-verification``
分支上，源仓库与主 checkout 不动"。

判定为什么跟仓库内容挂钩
--------------------------

验证点用的是 ``api_test``：框架真的发 HTTP 请求，判定 = 响应 vs 断言。
探针（:class:`_Probe`）是测试进程里一个真的 HTTP 服务，它的回答取决于
worktree 里 ``feature.txt`` 的内容 —— 于是"父提交上失败、修复提交上通过"
是**仓库内容的函数**，不是模型说了算。

场景与预算
----------

每个场景 180 秒（:data:`SCENARIO_BUDGET_SECONDS`）。超了就把场景拆小，
不放宽预算。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

_E2E_DIR = Path(__file__).resolve().parent
_TESTS_DIR = _E2E_DIR.parent
_BACKEND_DIR = _TESTS_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from coding_tool import ScriptedCodingTool  # noqa: E402
from config_paths import resolve_plans_dir, resolve_state_db_path  # noqa: E402
from plan_state import PlanState  # noqa: E402
from tests.fixtures.verification_mode import (  # noqa: E402
    PR_NUMBER,
    PR_REF,
    register_worktree,
)
from verification.orchestrator import VerificationOrchestrator  # noqa: E402
from verification_plan_doc import parse_plan_doc  # noqa: E402
from verification_plan_review import (  # noqa: E402
    REVIEW_FILENAME,
    STATUS_ACCEPTED,
    ReviewItem,
    ReviewState,
    doc_sha256,
    extract_items,
)
from verification_workspace import Baseline, branch_name, ensure_workspace  # noqa: E402

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(180)]

#: 单个场景的预算（秒）。
SCENARIO_BUDGET_SECONDS = 180

#: 验证点 id。一个缺陷一个验证点。
VP_ID = "VP-001"

#: 修复提交之后 ``feature.txt`` 的内容；探针认这个字面量。
FIXED_CONTENT = "fixed"

#: 缺陷还在时的内容（上游 PR head 上的样子）。
BROKEN_CONTENT = "pr head"

#: 验证点的可运行命令。它在父提交上失败、在修复提交上通过。
TEST_COMMAND = "grep -qx fixed feature.txt"

#: 机器计划的文件名。
MACHINE_PLAN_FILENAME = "verification_plan.json"

#: 验证计划书的文件名 —— 有了它（且没有 ``tasks.json``）plan 目录就是
#: 验证分支，机器计划从盘上读，不退回模型。
PLAN_DOC_FILENAME = "verification-plan.md"

#: PRD 的文件名与 PRD 审核状态的文件名。
PRD_FILENAME = "prd.md"
PRD_REVIEW_FILENAME = "review.json"

# ---------------------------------------------------------------------------
# 脚本化模型：登记表里的 matcher 与答案
# ---------------------------------------------------------------------------

#: 判定提示词里带上执行结果，所以"这一轮失败"与"这一轮通过"能被区分开。
#: 匹配的是**执行结果**那一段（``### <vp_id>: <status>``），不是模型自述。
MATCHER_JUDGMENT_FAILED = f"### {VP_ID}: FAILED"
MATCHER_JUDGMENT_PASSED = f"### {VP_ID}: PASSED"

#: 拆还是修的裁决提示词。
MATCHER_SPLIT = "请对每个候选输出 repair 或 split 的判断"

#: 修复任务生成的提示词。
MATCHER_REPAIR = "generate one repair task per FAILED verification point"

#: 补验收命令的提示词（修复任务没带命令时回路会追问一次）。
MATCHER_COMMAND_FILL = "补验收命令"

#: 同因重复时的语义 triage 提示词。
MATCHER_TRIAGE = "CLASSES — pick exactly one:"


def _judgment_answer(status: str) -> Dict[str, Any]:
    """一份验证报告：整体状态与验证点状态一致。"""
    return {
        "overall_status": status,
        "verification_results": [
            {
                "id": VP_ID,
                "status": status,
                "actual_result": f"探针回 {200 if status == 'PASSED' else 500}",
                "evidence": "HTTP 探针的响应码",
            }
        ],
        "requirement_deviations": [],
        "summary": f"一条验证点，判定 {status}。",
    }


def _split_answer() -> Dict[str, Any]:
    """拆还是修：修。缺陷在代码里，拆开验证点没有意义。"""
    return {
        "decisions": [
            {
                "vp_id": VP_ID,
                "action": "repair",
                "split_hint": "",
                "reason": "缺陷在代码里，修一条就够。",
            }
        ]
    }


def _repair_answer() -> Dict[str, Any]:
    """一条修复任务：一个缺陷，一条修复。

    ``test_command`` 就是验证点自己那条命令 —— 修复任务必须带着一条**可运行**
    的验收命令，否则执行器只能听子 agent 自述。
    """
    return {
        "tasks": [
            {
                "failed_vp_id": VP_ID,
                "title": "把 feature.txt 修成 fixed",
                "description": "feature.txt 的内容不是 fixed，探针因此回 500。",
                "acceptance_criteria": "feature.txt 的内容是 fixed",
                "files_to_modify": ["feature.txt"],
                "test_command": TEST_COMMAND,
            }
        ]
    }


def _command_fill_answer() -> Dict[str, Any]:
    """补验收命令那次提问的答案。"""
    return {
        "commands": [
            {"failed_vp_id": VP_ID, "test_command": TEST_COMMAND}
        ]
    }


def _triage_answer() -> Dict[str, Any]:
    """triage 跑完了，但说不清这次失败的性质。

    同名失败连续两轮会触发一次语义 triage；它给出可读答案但没有可用的
    证据引用时，结论是 ``available=True, conclusive=False``，回路据此停机
    并把 ``same_failure_no_triage_conclusion`` 记进裁决。
    """
    return {
        "failure_class": "deliverable_defect",
        "evidence_quote": "",
        "rationale": "the triage could not characterise the failure",
    }


# ---------------------------------------------------------------------------
# git 辅助
# ---------------------------------------------------------------------------

_GIT_ENV_EXTRA = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "PDT E2E",
    "GIT_AUTHOR_EMAIL": "pdt-e2e@example.invalid",
    "GIT_COMMITTER_NAME": "PDT E2E",
    "GIT_COMMITTER_EMAIL": "pdt-e2e@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    """在 ``repo`` 里跑一条 git 命令，返回 stdout（去掉尾部换行）。"""
    env = dict(os.environ)
    env.update(_GIT_ENV_EXTRA)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} 在 {repo} 里失败："
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )
    return (proc.stdout or "").strip()


def _git_status(repo: Path) -> str:
    """``git status --porcelain`` 的原文（空串 = 干净）。"""
    return _git(repo, "status", "--porcelain")


# ---------------------------------------------------------------------------
# 探针：一个真的 HTTP 服务，回答取决于 worktree 的内容
# ---------------------------------------------------------------------------


class _Probe:
    """真的 HTTP 服务：读 worktree 里的 ``feature.txt`` 再作答。

    判定因此是仓库内容的函数 —— 父提交上回 500，修复提交上回 200。
    """

    def __init__(self) -> None:
        self.worktree: Optional[Path] = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._build_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host = self._server.server_address[0]
        port = self._server.server_address[1]
        return f"http://{host}:{port}/feature"

    def _build_handler(self):
        probe = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 的约定
                if probe.feature_content() == FIXED_CONTENT:
                    body = b"fixed"
                    self.send_response(200)
                else:
                    body = b"not fixed"
                    self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return

        return Handler

    def feature_content(self) -> str:
        """worktree 里 ``feature.txt`` 的内容（没有就是空串）。"""
        if self.worktree is None:
            return ""
        path = self.worktree / "feature.txt"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# ---------------------------------------------------------------------------
# 场景把手
# ---------------------------------------------------------------------------


class _Env:
    """一次修复回路场景的全部把手。"""

    def __init__(
        self,
        plan_id: str,
        plan_dir: Path,
        workspace: Any,
        upstream_repo: Path,
        tool: Any,
        probe: _Probe,
    ) -> None:
        self.plan_id = plan_id
        self.plan_dir = plan_dir
        self.workspace = workspace
        self.upstream_repo = upstream_repo
        self.tool = tool
        self.probe = probe

    @property
    def worktree(self) -> Path:
        return Path(self.workspace.path)

    @property
    def branch(self) -> str:
        return branch_name(self.plan_id)

    def orchestrator(self) -> VerificationOrchestrator:
        """一条**新的**回路实例 —— 生产代码每轮也是新起一个。"""
        return VerificationOrchestrator(self.plan_dir, self.worktree, self.tool)

    def plan_state(self) -> PlanState:
        return PlanState(self.plan_dir)

    def apply_fix_commit(self) -> str:
        """把缺陷修好并提交 —— 一个缺陷一个提交。

        提交落在 worktree 自己的分支上（``fix/<plan_id>-verification``），
        源仓库不动。
        """
        (self.worktree / "feature.txt").write_text(
            f"{FIXED_CONTENT}\n", encoding="utf-8"
        )
        _git(self.worktree, "add", "feature.txt")
        _git(
            self.worktree,
            "commit",
            "--quiet",
            "-m",
            f"fix: {VP_ID} 把 feature 修成 {FIXED_CONTENT}",
        )
        return _git(self.worktree, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def make_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream_repo: Any,
    scripted_llm: Any,
    workspace_root: Path,
    request: pytest.FixtureRequest,
) -> Any:
    """造一个验证分支的 plan：盘上的机器计划、冻结的基线、真的 worktree。

    返回一个**工厂**：同一个测试里可以造两个互相独立的 plan（两个
    worktree、两个探针），互不踩。
    """
    _install_scripted_answers(scripted_llm.script_dir)
    counter = {"n": 0}
    probes: List[_Probe] = []
    # plan id 有长度上限（64 字符），所以用测试名的短摘要而不是测试名本身。
    digest = hashlib.sha256(request.node.name.encode("utf-8")).hexdigest()[:8]

    def _make() -> _Env:
        counter["n"] += 1
        plan_id = f"e2e-repair-{digest}-{counter['n']}"
        plan_dir = resolve_plans_dir() / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)

        probe = _Probe()
        probes.append(probe)
        try:
            env = _build_env(
                plan_id=plan_id,
                plan_dir=plan_dir,
                upstream_repo=upstream_repo.repo,
                script_dir=scripted_llm.script_dir,
                probe=probe,
            )
        except Exception:
            probe.close()
            raise
        return env

    try:
        yield _make
    finally:
        for probe in probes:
            probe.close()


def _build_env(
    plan_id: str,
    plan_dir: Path,
    upstream_repo: Path,
    script_dir: Path,
    probe: _Probe,
) -> _Env:
    """把一个 plan 目录铺成"可以开跑"的样子。"""
    # PRD 与它的审核状态（验证分支的最小 PRD 允许整条 skip）。
    (plan_dir / PRD_FILENAME).write_text(
        "# PRD\n\n## 概述\n\n验证分支的 PRD。\n", encoding="utf-8"
    )
    (plan_dir / PRD_REVIEW_FILENAME).write_text(
        json.dumps(
            {"items": [{"index": 0, "status": STATUS_ACCEPTED, "note": "E2E：已审"}]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # 验证计划书：满足十章节契约，里面一条验证点。
    head_sha = _git(upstream_repo, "rev-parse", PR_REF)
    plan_doc = _plan_doc(upstream_repo, probe.url)
    (plan_dir / PLAN_DOC_FILENAME).write_text(plan_doc, encoding="utf-8")

    # 计划书的审核：条目全部已解决，且针对当前这份文档。
    parsed = parse_plan_doc(plan_doc)
    items = [
        ReviewItem(
            index=item.index,
            id=item.id,
            title=item.title,
            status=STATUS_ACCEPTED,
            note="E2E：已审",
        )
        for item in extract_items(parsed)
    ]
    review = ReviewState(items=items, doc_sha256=doc_sha256(plan_doc.encode("utf-8")))
    (plan_dir / REVIEW_FILENAME).write_text(
        json.dumps(review.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 冻结的基线：PR head 那个 commit。它是机器事实，走 PlanState 的入口。
    state = PlanState(plan_dir)
    state.set_verification_baseline(
        {
            "repo_dir": str(upstream_repo),
            "ref": PR_REF,
            "commit_sha": head_sha,
            "pr_number": PR_NUMBER,
        },
        doc_sha256=doc_sha256(plan_doc.encode("utf-8")),
    )
    # 验证分支的计划书审完之后直接进验证阶段。
    state.start_verification()

    # worktree：真的 git worktree，分支 fix/<plan_id>-verification。
    workspace = ensure_workspace(
        plan_id,
        Baseline(
            repo_dir=upstream_repo,
            ref=PR_REF,
            commit_sha=head_sha,
            pr_number=PR_NUMBER,
        ),
    )
    register_worktree(upstream_repo, workspace.path)
    probe.worktree = workspace.path

    # 机器计划：一条 api_test 验证点，判定挂在探针上。
    machine_plan = {
        "verification_points": [
            {
                "id": VP_ID,
                "title": "feature 标记已修复",
                "verification_method": "api_test",
                "priority": "P0",
                "expected_result": "feature.txt 的内容是 fixed",
                "evidence_command": TEST_COMMAND,
                "test_command": TEST_COMMAND,
                "request": {
                    "method": "GET",
                    "url": probe.url,
                    "timeout_seconds": 30,
                },
                "assertions": [
                    {"name": "C1: 探针回一个 200", "status": 200},
                ],
            }
        ]
    }
    (plan_dir / MACHINE_PLAN_FILENAME).write_text(
        json.dumps(machine_plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    tool = ScriptedCodingTool(str(script_dir), cwd=str(workspace.path))
    return _Env(
        plan_id=plan_id,
        plan_dir=plan_dir,
        workspace=workspace,
        upstream_repo=upstream_repo,
        tool=tool,
        probe=probe,
    )


def _plan_doc(repo_dir: Path, probe_url: str) -> str:
    """一份满足十章节契约的验证计划书。

    写在这里而不是交给模型：计划书是**被审核的对象**，它必须先是一份合法
    的文档，审核才有东西可审。
    """
    baseline_line = f"baseline: repo={repo_dir} ref={PR_REF} pr={PR_NUMBER}"
    return f"""## 被验证对象

{baseline_line}

被验证对象是上游仓库 {repo_dir} 的 {PR_REF}。

## 验证范围

- {probe_url}
- 被验证对象自己的检出

## 排除范围

- 与本次改动无关的历史提交与其它分支。

## 验证方法

api_test：向被验证对象声明的服务发一条请求，按声明的断言判定。

## 验证点清单

### {VP_ID} 探针服务返回 200

- 范围：{probe_url}
- 验证方法：api_test
- 通过判据：
  - C1: 探针服务回一个 200 状态码
- 证据要求：
  - E1: 该请求的响应状态码原文
- 预期结果：请求得到 200。

## 预期结果

验证点清单里的每一条都通过。

## 证据要求

每个验证点的响应原文。

## 通过判据

全部验证点通过即本轮通过。

## 环境与依赖

git；一个回环上的 HTTP 服务；不需要网络。

## 风险面

工作区写错仓库；由工作区根目录守卫拦下。
"""


def _install_scripted_answers(script_dir: Path) -> None:
    """把这一轮要用的答案写进脚本化模型的目录，并把登记表补上。"""
    script_dir.mkdir(parents=True, exist_ok=True)
    answers = {
        "judgment_failed": _judgment_answer("FAILED"),
        "judgment_passed": _judgment_answer("PASSED"),
        "split": _split_answer(),
        "repair": _repair_answer(),
        "command_fill": _command_fill_answer(),
        "triage": _triage_answer(),
    }
    for key, payload in answers.items():
        (script_dir / f"{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    registry_path = script_dir / "registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        registry = {}
    table = registry.get("prompts")
    if not isinstance(table, dict):
        table = {}
        registry = {"prompts": table}
    table["judgment_failed"] = {
        "match": MATCHER_JUDGMENT_FAILED,
        "file": "judgment_failed.json",
    }
    table["judgment_passed"] = {
        "match": MATCHER_JUDGMENT_PASSED,
        "file": "judgment_passed.json",
    }
    table["split"] = {"match": MATCHER_SPLIT, "file": "split.json"}
    table["repair"] = {"match": MATCHER_REPAIR, "file": "repair.json"}
    table["command_fill"] = {"match": MATCHER_COMMAND_FILL, "file": "command_fill.json"}
    table["triage"] = {"match": MATCHER_TRIAGE, "file": "triage.json"}
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 断言辅助
# ---------------------------------------------------------------------------


def _assert_no_blocked_on_user(
    env: _Env,
    report: Dict[str, Any],
    decision: Dict[str, Any],
) -> None:
    """任何场景都不得落进"要人来决定"的状态。

    三处都要看：

    * 验证点判定里没有 ``BLOCKED``（服务起不来、证据缺失）；
    * 回路裁决里没有 ``waiting_for_user`` / ``needs_operator_decision``；
    * plan 没有停在进行中的阶段。
    """
    for result in report.get("verification_results", []) or []:
        assert result.get("status") != "BLOCKED", (
            f"{result.get('id')} 被判成 BLOCKED —— 场景不允许等用户"
        )
    assert decision.get("waiting_for_user") is False, decision
    assert decision.get("needs_operator_decision") is False, decision
    phase = env.plan_state().get_current_phase()
    assert phase not in (
        "verification_running",
        "verification_repairing",
        "interview",
        "prd_review",
    ), f"plan 还停在进行中的阶段：{phase}"


def _run_round(env: _Env, round_number: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """跑一轮真的验证，返回 ``(报告, 回路裁决)``。

    轮次循环、失败判定、修复任务生成全是生产代码；模型那一层是脚本答案。
    """
    orch = env.orchestrator()
    report = orch.start_verification_cycle(round_number=round_number)
    decision = orch.check_cycle_conditions(report, round_number=round_number)
    return report, decision


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------


@pytest.mark.timeout(180)
def test_passed_scenario(make_env: Any) -> None:
    """修复成功 → ``verification_passed``。

    第一轮失败（探针回 500），修一个提交，第二轮通过（探针回 200）。
    """
    env = make_env()

    # 第一轮：缺陷还在，判定失败，回路要修复任务。
    report_1, decision_1 = _run_round(env, round_number=1)
    assert report_1.get("overall_status") == "FAILED", report_1
    assert decision_1.get("status") == "verification_failed", decision_1
    assert decision_1.get("repair_tasks"), "失败一轮之后回路必须给出修复任务"

    # 修复：一个缺陷一个提交，落在 fix/<plan_id>-verification 分支上。
    baseline_sha = _git(env.worktree, "rev-parse", "HEAD")
    env.apply_fix_commit()
    assert _git(env.worktree, "rev-parse", "--abbrev-ref", "HEAD") == env.branch
    assert _git(env.worktree, "rev-list", "--count", f"{baseline_sha}..HEAD") == "1", (
        "一个缺陷只允许一个提交"
    )

    # 第二轮：探针回 200，判定通过。
    report_2, decision_2 = _run_round(env, round_number=2)
    assert report_2.get("overall_status") == "PASSED", report_2
    assert decision_2.get("status") == "passed", decision_2

    state = env.plan_state()
    assert state.get_current_phase() == "verification_passed", (
        f"终态相位不对：{state.get_current_phase()}"
    )
    assert state.get_verification_status() == "passed", state.get_verification_status()
    _assert_no_blocked_on_user(env, report_1, decision_1)
    _assert_no_blocked_on_user(env, report_2, decision_2)


@pytest.mark.timeout(180)
def test_same_failure_repeated_stops(make_env: Any) -> None:
    """连续两轮同失败 → ``verification_loop_stopped``。

    两轮的失败集合一模一样，回路必须收敛而不是继续修。
    """
    env = make_env()

    report_1, decision_1 = _run_round(env, round_number=1)
    assert decision_1.get("status") == "verification_failed", decision_1

    # 第二轮：缺陷没修，失败集合与上一轮相同。
    report_2, decision_2 = _run_round(env, round_number=2)
    assert report_2.get("overall_status") == "FAILED", report_2
    assert decision_2.get("status") == "loop_stopped", decision_2
    assert decision_2.get("stop_reason") == (
        "same_failure_repeated_after_max_attempts"
    ), decision_2

    state = env.plan_state()
    assert state.get_current_phase() == "verification_loop_stopped", (
        f"终态相位不对：{state.get_current_phase()}"
    )
    assert state.get_verification_status() == "loop_stopped", (
        state.get_verification_status()
    )
    assert state.get_verification_stop_reason() == (
        "same_failure_repeated_after_max_attempts"
    ), state.get_verification_stop_reason()
    _assert_no_blocked_on_user(env, report_1, decision_1)
    _assert_no_blocked_on_user(env, report_2, decision_2)


@pytest.mark.timeout(180)
def test_max_rounds_and_user_stopped(make_env: Any) -> None:
    """两个停机场景：轮次用尽、用户叫停 → 都是 ``verification_loop_stopped``。"""
    # --- 场景 A：轮次用尽 ---------------------------------------------------
    env = make_env()

    # 这个 plan 的轮次预算就是一轮 —— 跑完这一轮就没有下一轮了。
    # ``init_round`` 是回路自己开一轮时写的那一行（``plan_verification``）。
    _init_round(env.plan_id, round_n=1, max_rounds=1)
    env.plan_state().set_verification_max_rounds(1)

    report_a, decision_a = _run_round(env, round_number=1)
    assert decision_a.get("status") == "verification_failed", decision_a
    env.apply_fix_commit()

    # 生产代码判定"下一轮超预算"：问的是它自己的轮次预算。
    from verification_loop import _plan_next_round

    current = _current_verification_row(env.plan_id)
    next_round, max_rounds, exhausted = _plan_next_round(current)
    assert exhausted, "轮次预算应当已经用尽"
    assert next_round > max_rounds, (next_round, max_rounds)

    # 预算用尽 → 回路停机。这是状态机自己的停机入口，reason 是它的合法
    # 取值之一（``max_rounds_reached``）。
    state = env.plan_state()
    state.stop_verification_loop("max_rounds_reached")
    assert state.get_current_phase() == "verification_loop_stopped", (
        f"终态相位不对：{state.get_current_phase()}"
    )
    assert state.get_verification_status() == "loop_stopped", (
        state.get_verification_status()
    )
    assert state.get_verification_stop_reason() == "max_rounds_reached", (
        state.get_verification_stop_reason()
    )
    _assert_no_blocked_on_user(env, report_a, decision_a)

    # --- 场景 B：用户叫停 ---------------------------------------------------
    env_b = make_env()
    orch_b = env_b.orchestrator()
    report_b = orch_b.start_verification_cycle(round_number=1)
    orch_b.stop_verification()

    state_b = env_b.plan_state()
    assert state_b.get_current_phase() == "verification_loop_stopped", (
        f"终态相位不对：{state_b.get_current_phase()}"
    )
    assert state_b.get_verification_status() == "loop_stopped", (
        state_b.get_verification_status()
    )
    assert state_b.get_verification_stop_reason() == "user_stopped", (
        state_b.get_verification_stop_reason()
    )
    _assert_no_blocked_on_user(
        env_b,
        report_b,
        {"waiting_for_user": False, "needs_operator_decision": False},
    )


def _current_verification_row(plan_id: str) -> Any:
    """读 ``plan_verification`` 里这个 plan 的那一行。"""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        return VerificationRepository(conn).current(plan_id)
    finally:
        conn.close()


def _init_round(plan_id: str, round_n: int, max_rounds: int) -> None:
    """写下"第 ``round_n`` 轮开始，预算 ``max_rounds`` 轮"——回路自己的写法。"""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        VerificationRepository(conn).init_round(plan_id, round_n, max_rounds)
    finally:
        conn.close()


@pytest.mark.timeout(180)
def test_regression_fails_before_commit_passes_after(make_env: Any) -> None:
    """负控：父提交上 ``test_command`` 失败，修复提交上通过。

    这条命令就是修复任务带着的那条命令 —— 回路把"验证点失败"翻译成
    "可运行的修复任务"时靠它。命令在父提交上必须失败，否则"修好了"这句话
    就没有依据。
    """
    env = make_env()

    # 父提交（PR head 的父）上没有 feature.txt，命令失败。
    parent = _git(env.upstream_repo, "rev-parse", f"{PR_REF}^")
    _run_command_at(env.upstream_repo, parent, TEST_COMMAND, expect_ok=False)

    # PR head 上也失败（缺陷还在）。
    head = _git(env.upstream_repo, "rev-parse", PR_REF)
    _run_command_at(env.upstream_repo, head, TEST_COMMAND, expect_ok=False)

    # 缺陷还在时跑一轮：失败，而回路给出的修复任务带着的就是这条命令 ——
    # 执行器要拿它当验收标准。
    report_1, decision_1 = _run_round(env, round_number=1)
    assert decision_1.get("status") == "verification_failed", decision_1
    tasks = decision_1.get("repair_tasks") or []
    assert tasks, "失败一轮之后回路必须给出修复任务"
    assert len(tasks) == 1, f"一个缺陷只允许一条修复任务：{tasks}"
    assert tasks[0].get("test_command") == TEST_COMMAND, tasks[0]
    assert tasks[0].get("failed_vp_id") == VP_ID, tasks[0]

    # 修复提交之后同一条命令通过。
    fix_sha = env.apply_fix_commit()
    _run_command_at(env.worktree, fix_sha, TEST_COMMAND, expect_ok=True)

    # 而回路自己的判定也跟着翻：修复后通过。
    report_2, decision_2 = _run_round(env, round_number=2)
    assert decision_2.get("status") == "passed", decision_2
    assert report_2.get("overall_status") == "PASSED", report_2
    assert env.plan_state().get_current_phase() == "verification_passed"
    _assert_no_blocked_on_user(env, report_1, decision_1)
    _assert_no_blocked_on_user(env, report_2, decision_2)


def _run_command_at(repo: Path, commit: str, command: str, expect_ok: bool) -> None:
    """在 ``repo`` 的 ``commit`` 上跑一条命令，核对退出码。

    源仓库不能动（切分支、提交都不行），所以用 ``git archive`` 把那一棵树
    铺到临时目录再跑。
    """
    env = dict(os.environ)
    env.update(_GIT_ENV_EXTRA)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", commit],
        capture_output=True,
        check=True,
        env=env,
    )
    tmp = Path(tempfile.mkdtemp(prefix="pdt-repair-round-cmd-"))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            # ``filter="data"``：拒绝归档里的越界路径与特殊文件成员。
            tar.extractall(tmp, filter="data")
        proc = subprocess.run(
            ["sh", "-c", command],
            cwd=str(tmp),
            capture_output=True,
            text=True,
            env=env,
        )
        if expect_ok:
            assert proc.returncode == 0, (
                f"命令 {command!r} 在 {commit[:8]} 上应当成功，却退了 "
                f"{proc.returncode}: {proc.stderr.strip()}"
            )
        else:
            assert proc.returncode != 0, (
                f"命令 {command!r} 在 {commit[:8]} 上应当失败，却成功了"
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.timeout(180)
def test_pr_head_and_checkouts_immutable(make_env: Any) -> None:
    """回路跑完之后，PR head / 源仓库 HEAD / 各个 checkout 一个字节都没动。

    * **PR head SHA** —— ``refs/pull/7/head`` 不许动。
    * **源仓库 HEAD** —— 源仓库不许被切分支、不许被提交。
    * **PDT 主 checkout** —— 本仓库的主 checkout 不许被回路弄脏。
    """
    env = make_env()

    pr_head_before = _git(env.upstream_repo, "rev-parse", PR_REF)
    source_head_before = _git(env.upstream_repo, "rev-parse", "HEAD")
    source_status_before = _git_status(env.upstream_repo)
    pdt_checkout = _pdt_main_checkout()
    pdt_status_before = _git_status(pdt_checkout)

    # 跑完整的一轮修复：失败 → 修 → 通过。
    _run_round(env, round_number=1)
    env.apply_fix_commit()
    _run_round(env, round_number=2)

    assert _git(env.upstream_repo, "rev-parse", PR_REF) == pr_head_before, (
        "PR head SHA 变了"
    )
    assert _git(env.upstream_repo, "rev-parse", "HEAD") == source_head_before, (
        "源仓库 HEAD 变了"
    )
    assert _git_status(env.upstream_repo) == source_status_before, (
        "源仓库主 checkout 的工作区变了"
    )
    assert _git_status(pdt_checkout) == pdt_status_before, (
        "PDT 主 checkout 的工作区变了"
    )

    # 修复提交落在 worktree 自己的分支上，源仓库一个提交都没有多。
    assert _git(env.worktree, "rev-parse", "--abbrev-ref", "HEAD") == env.branch
    assert _git(env.upstream_repo, "rev-list", "--count", "HEAD") == "2"


def _pdt_main_checkout() -> Path:
    """本仓库（PDT）的主 checkout 路径。

    ``git worktree list`` 的第一条就是主 checkout；只有一个 worktree 的
    情况下（CI 里就是那样）它自己就是主 checkout。
    """
    repo_root = _BACKEND_DIR.parent
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):].strip())
    raise AssertionError("git worktree list 没有给出任何 worktree")
