"""TDD spec —— 根因链记录的 ``head_sha`` 与陈旧判定。

背景
----
``reset_rounds`` 保留根因链与它的报告；下一次 ``/start`` 会先跑一整轮验证，
而链还在说**上一次**的事 —— 它记着「这个点欠一次诊断 / 欠一次实现」，可代码
已经换过一轮了。链记录里没有 ``head_sha``，于是「链描述的还是不是当前这次
失败」这个问题**无从回答**（arch 决策点 8）。

本任务交付的是回答这个问题的那两半：

1. **链记录多一个可空的 ``head_sha``** —— 诊断派发那一刻的 HEAD；
2. **``_chain_is_stale(chain, head_sha)``** —— 纯函数，回答「这条链还作数吗」。

谁在什么时候写 ``head_sha``
---------------------------
**只在派发诊断时写。** 诊断是链开始描述一次失败的那一刻。实现任务落地以后
HEAD 会变，而那时链描述的是**已经派出去过**的诊断 —— 把它洗成新的 HEAD，
链就永远不陈旧，陈旧保护也就没了。所以「已有值 + 本次不派诊断」= 不动它。

真值表（``_chain_is_stale``）
-----------------------------
======================  =============  ==========
链                      当前 HEAD      答案
======================  =============  ==========
空链（没有点、没有 sha）  任意           ``False``
老记录（有点、没有 sha）  任意           ``True``
有 sha，HEAD 变了        ``"bbb"``      ``True``
有 sha，HEAD 一样        ``"aaa"``      ``False``
有 sha，HEAD 读不到      ``None``       ``True``
======================  =============  ==========

边界条件
--------
* 链为空 / 没有点 → **不陈旧**：没有链，就没有陈旧可言（走正常轮）。
* 老记录没有 ``head_sha`` → **陈旧**：我们不知道它描述的是哪一次失败，
  保守起见当作陈旧（走正常轮）。
* 当前 HEAD 读不到（``None``）→ **陈旧**：同样保守。
* ``head_sha`` 只在**派发诊断**时写入；本次不派诊断就沿用旧值。

TDD contract —— 4 cases
-----------------------
1. ``test_chain_is_stale_when_head_changed`` —— HEAD 变了 → 陈旧。
2. ``test_legacy_chain_without_head_sha_is_stale`` —— 老记录 → 陈旧。
3. ``test_chain_same_head_not_stale`` —— HEAD 一样 → 不陈旧。
4. ``test_decision_writes_head_sha`` —— 派发诊断的那一轮把 HEAD 写进链记录。

测试命令::

    pytest backend/tests/unit/verification/test_root_cause_chain_head_sha.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification.orchestrator import (  # noqa: E402
    _CHAIN_STAGE_IMPLEMENTATION,
    _CHAIN_STAGE_ROOT_CAUSE,
    VerificationOrchestrator,
    _chain_is_stale,
)


PLAN_ID = "p1"
VP_ID = "VP-3"

#: A commit sha that is not the one the fixture repository sits on.
OTHER_SHA = "bbb"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` inside ``repo`` and return its stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _repo_with_one_commit(root: Path) -> tuple[Path, str]:
    """A workspace that is a git repository with exactly one commit."""
    repo = root / "workspace"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(
        repo,
        "-c", "user.email=t@example.com",
        "-c", "user.name=t",
        "commit", "-q", "--allow-empty", "-m", "init",
    )
    return repo, _git(repo, "rev-parse", "HEAD")


def _orchestrator(plan_dir: Path, project_dir: Path) -> VerificationOrchestrator:
    """An orchestrator with only the two directories.

    The chain's decision touches nothing else: it reads the chain's
    record, asks whether the point's report is there, and — when a
    diagnosis is dispatched — reads the workspace's HEAD.
    """
    orch = VerificationOrchestrator.__new__(VerificationOrchestrator)
    orch.plan_dir = plan_dir
    orch.project_dir = project_dir
    return orch


def _plan_dir(tmp_path: Path) -> Path:
    plan = tmp_path / "plans" / PLAN_ID
    plan.mkdir(parents=True)
    return plan


def _write_chain_record(plan_dir: Path, record: dict) -> None:
    (plan_dir / "verification_root_cause_chain.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8",
    )


def _write_report(orch: VerificationOrchestrator, vp_id: str, scope: list) -> None:
    """A root-cause report that declares ``scope``."""
    report = orch._root_cause_report_file(vp_id)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps({"paths": list(scope)}, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. HEAD 变了 → 陈旧
# ---------------------------------------------------------------------------
def test_chain_is_stale_when_head_changed():
    """链记着的 HEAD 与当前 HEAD 不一样 → 这条链描述的不是这次失败。"""
    chain = {
        "version": 1,
        "stage": "implementation",
        "head_sha": "aaa",
        "vps": {VP_ID: {"stage": "implementation"}},
    }

    assert _chain_is_stale(chain, OTHER_SHA) is True


def test_documented_example_is_stale():
    """任务书给的那条输入 → 陈旧。"""
    chain = {
        "version": 1,
        "stage": "implementation",
        "head_sha": "aaa",
        "vps": {},
    }

    assert _chain_is_stale(chain, "bbb") is True


# ---------------------------------------------------------------------------
# 2. 老记录没有 head_sha → 陈旧（保守）
# ---------------------------------------------------------------------------
def test_legacy_chain_without_head_sha_is_stale():
    """老记录不知道它描述的是哪一次失败 → 保守，当作陈旧。"""
    chain = {
        "version": 1,
        "stage": "root_cause",
        "vps": {VP_ID: {"stage": "root_cause"}},
    }

    assert _chain_is_stale(chain, "aaa") is True
    # 键在但值是空的，跟没有一样 —— 我们同样不知道。
    assert _chain_is_stale({**chain, "head_sha": None}, "aaa") is True
    assert _chain_is_stale({**chain, "head_sha": ""}, "aaa") is True


# ---------------------------------------------------------------------------
# 3. HEAD 一样 → 不陈旧
# ---------------------------------------------------------------------------
def test_chain_same_head_not_stale():
    """HEAD 没动 → 链描述的还是这次失败。"""
    chain = {
        "version": 1,
        "stage": "implementation",
        "head_sha": "aaa",
        "vps": {VP_ID: {"stage": "implementation"}},
    }

    assert _chain_is_stale(chain, "aaa") is False


def test_current_head_unreadable_is_stale():
    """当前 HEAD 读不到 → 同样保守，当作陈旧。"""
    chain = {
        "version": 1,
        "stage": "implementation",
        "head_sha": "aaa",
        "vps": {VP_ID: {"stage": "implementation"}},
    }

    assert _chain_is_stale(chain, None) is True


def test_empty_chain_is_not_stale():
    """空链没有陈旧可言 —— 没有链就走正常轮。"""
    assert _chain_is_stale({}, "aaa") is False
    assert _chain_is_stale({"version": 1, "stage": "", "vps": {}}, "aaa") is False
    assert _chain_is_stale({"version": 1, "stage": "", "vps": {}}, None) is False


def test_the_orchestrator_answers_the_same(tmp_path):
    """编排器上的方法形式给同一个答案 —— 预检会这样问。"""
    orch = _orchestrator(_plan_dir(tmp_path), tmp_path)
    chain = {
        "version": 1,
        "stage": "implementation",
        "head_sha": "aaa",
        "vps": {VP_ID: {"stage": "implementation"}},
    }

    assert orch._chain_is_stale(chain, OTHER_SHA) is True
    assert orch._chain_is_stale(chain, "aaa") is False


def test_current_head_sha_is_none_without_a_repository(tmp_path):
    """工作区不是 git 仓库 → HEAD 读不到 → ``None``（不抛错）。"""
    workspace = tmp_path / "not-a-repo"
    workspace.mkdir()
    orch = _orchestrator(_plan_dir(tmp_path), workspace)

    assert orch._current_head_sha() is None


# ---------------------------------------------------------------------------
# 4. 派发诊断的那一轮把 HEAD 写进链记录
# ---------------------------------------------------------------------------
def test_decision_writes_head_sha(tmp_path):
    """诊断派发出去的那一刻，链记下工作区停在哪个提交上。"""
    plan_dir = _plan_dir(tmp_path)
    repo, head_sha = _repo_with_one_commit(tmp_path)
    orch = _orchestrator(plan_dir, repo)

    decision = orch._root_cause_chain_decision([VP_ID], round_number=1)

    actions = decision["actions"]
    assert any(
        action["action"] == _CHAIN_STAGE_ROOT_CAUSE for action in actions
    ), f"the round must dispatch a diagnosis: {actions!r}"
    assert decision["payload"]["head_sha"] == head_sha, (
        "the chain's record must carry the HEAD the diagnosis was "
        f"dispatched at: {decision['payload']!r}"
    )


def test_decision_keeps_head_sha_when_no_diagnosis_is_dispatched(tmp_path):
    """本次不派诊断 → 不动 ``head_sha``（别把链洗新）。

    实现任务落地以后 HEAD 会变；那时候链描述的是**已经派出去过**的诊断，
    把它写成新的 HEAD，链就永远不陈旧了。
    """
    plan_dir = _plan_dir(tmp_path)
    repo, head_sha = _repo_with_one_commit(tmp_path)
    orch = _orchestrator(plan_dir, repo)
    _write_chain_record(plan_dir, {
        "version": 1,
        "stage": _CHAIN_STAGE_ROOT_CAUSE,
        "head_sha": "aaa",
        "vps": {
            VP_ID: {
                "stage": _CHAIN_STAGE_ROOT_CAUSE,
                "report_path": f"plans/{PLAN_ID}/reports/root-cause-{VP_ID}.md",
            },
        },
    })
    _write_report(orch, VP_ID, ["backend/app/service.py"])

    decision = orch._root_cause_chain_decision([], round_number=2)

    actions = decision["actions"]
    assert actions and all(
        action["action"] == _CHAIN_STAGE_IMPLEMENTATION for action in actions
    ), f"this round owes an implementation, not a diagnosis: {actions!r}"
    assert decision["payload"]["head_sha"] == "aaa", (
        "the head must not be refreshed while the chain is not dispatching "
        f"a diagnosis: {decision['payload']!r}"
    )
    assert head_sha != "aaa", "the fixture repository must sit on another sha"


def test_chain_record_round_trips_head_sha(tmp_path):
    """``head_sha`` 进出磁盘都不丢：写得进，读得出。"""
    plan_dir = _plan_dir(tmp_path)
    repo, head_sha = _repo_with_one_commit(tmp_path)
    orch = _orchestrator(plan_dir, repo)

    orch._save_root_cause_chain({
        "version": 1,
        "stage": _CHAIN_STAGE_ROOT_CAUSE,
        "vps": {VP_ID: {"stage": _CHAIN_STAGE_ROOT_CAUSE}},
        "head_sha": head_sha,
        "updated_round": 1,
    })

    loaded = orch._load_root_cause_chain()
    assert loaded["head_sha"] == head_sha
    assert loaded["vps"][VP_ID]["stage"] == _CHAIN_STAGE_ROOT_CAUSE


def test_legacy_chain_record_loads_without_head_sha(tmp_path):
    """老记录读得进来 —— ``head_sha`` 读作「不知道」，不是一个 KeyError。"""
    plan_dir = _plan_dir(tmp_path)
    repo, _head_sha = _repo_with_one_commit(tmp_path)
    orch = _orchestrator(plan_dir, repo)
    _write_chain_record(plan_dir, {
        "version": 1,
        "stage": _CHAIN_STAGE_ROOT_CAUSE,
        "vps": {VP_ID: {"stage": _CHAIN_STAGE_ROOT_CAUSE}},
    })

    loaded = orch._load_root_cause_chain()

    assert loaded["head_sha"] is None
    assert loaded["vps"][VP_ID]["stage"] == _CHAIN_STAGE_ROOT_CAUSE
