"""round-1 真实拒绝记录的形状做夹具 —— 假阳性没了，真写仍改判。

背景（2026-11-04）
------------------
计划 ``20261027-service-readiness-2`` 的 round 1 留下了五条真实拒绝记录
（``<plan>/vp_write_scope/refusals/round-1/*.jsonl``）::

    VP-016.jsonl   Bash   <worktree>/len
    VP-017.jsonl   Bash   <worktree>/1:        （两条）
    VP-020.jsonl   Bash   <worktree>/len
    VP-022.jsonl   Bash   <worktree>/len
    VP-024.jsonl   Bash   <worktree>/single
                   Bash   <worktree>/index
                   Write  ~/.claude/projects/<project>/memory/…md

``len`` / ``single`` / ``index`` / ``1:`` 都不是文件，是 Python 代码片段：VP 把
``if a > len(b):`` 这种**比较表达式**写进了 heredoc 正文，而旧解析器逐行切词，
把正文里的 ``>`` 当成了 shell 重定向，于是"写目标"被算成 ``<worktree>/len``。
护栏据此拒绝、证据入账，两个本来 PASSED 的 VP 被改判成 FAILED
（``failure_class=infra``）—— 而被验证的仓库一个字节都没变过。

本文件用**同形状**的记录做夹具，钉住修复后的三条边界：

1. ``test_heredoc_vp_no_longer_writes_a_refusal`` —— VP-016 形状：同一条命令
   现在 exit 0，marker 根本不出现，verdict 原样；
2. ``test_out_of_scope_write_does_not_flip_the_verdict`` —— VP-024 形状：三条
   记录都在案（2 条 Bash + 1 条 Write memory），但没有一条落在被验证树里 →
   verdict 原样，另记 ``verifier_refused_outside_scope``；
3. ``test_genuine_in_tree_write_still_flips`` —— 真写树内文件 → 仍然 FAILED
   （护栏没有被削弱）。

Bash 分支现在不判定（2026-11-05）
--------------------------------
护栏的 Bash 分支**恒放行**：它不再解析命令，也不再产生任何新的拒绝记录（缺陷
A 第 4 条 —— 解析器猜写目标会持续产生假阳性，把没动过被验证仓库的 VP 改判成
FAILED）。"改过树"这件事改由**测量**回答：PreToolUse 拍快照、PostToolUse 比对，
测出改动就存证、回滚、记账（``verification_tree_guard.py``）。

于是本文件剩下的钉法是：

* Bash 分支一条新的拒绝记录都不写（历史形状一个都不再复现，树内写命令也
  不再被拒）；
* round-1 留下的**历史**记录仍然读得出来、仍然入账 —— 它们是证据，不是判定；
* Edit/Write 越界仍然被拒、仍然改判（判定没有被整体放宽）。

夹具仓库建在哪里
----------------
``_real_home() / f"pdt-heredoc-{uuid4().hex}"`` —— **不能**用 pytest 的
``tmp_path``：它在 macOS 上落在 ``$TMPDIR``、在 Linux 上落在 ``/tmp``，而临时
目录是护栏无条件放行的地方，写进去会被当成"写临时目录"而放行，测出来的是另
一回事（同 ``tests/unit/test_verifier_write_scope_regression.py`` 的理由）。
``finally`` 里 ``shutil.rmtree`` 收掉它。

进程内调用
----------
护栏的三个入口都在本进程里调（``check_bash_payload`` / ``check_edit_payload`` /
``record_refusal``），环境变量用 ``monkeypatch.setenv`` 设。不写 settings.json、
不起子进程：这里要验的是"证据长什么样、结论怎么算"，不是钩子怎么被接上
（那是 ``test_verifier_write_scope_negative_control.py`` 的事）。

``HOME`` 为什么被改写
---------------------
记忆写入的路径用 ``~`` 写成 ``~/.claude/projects/pdt/memory/…``（与 round-1 的
记录同形）。但 ``install_hermetic_env()`` 把 ``$HOME`` 指向了一个
``tempfile.mkdtemp(prefix="home-")`` 目录 —— 它在 ``$TMPDIR`` 底下，于是落在
护栏的临时目录白名单里：``~`` 展开出来的 memory 路径会被当成"写临时目录"而
**放行**，那条断言就成了空断言。所以本文件把 ``$HOME`` 指回**启动时的家目录**
（``PDT_TEST_REAL_HOME``）：``~`` 展开出来的路径在所有临时根之外，护栏按规矩
拒绝它。本文件不往那个家目录里写任何东西（只解析路径），``monkeypatch`` 会在
用例结束时把 ``HOME`` 还原。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_write_guard  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402
from verification_subagent import Verdict  # noqa: E402

#: The round the historical records belong to.
ROUND_NO = 1

#: The VP whose only round-1 record was the heredoc-derived ``<worktree>/len``.
VP_016 = "VP-016"

#: The VP whose round-1 records were ``single`` / ``index`` plus a memory write.
VP_024 = "VP-024"

#: The VP used for the constructed "really wrote into the tree" sample.
VP_055 = "VP-055"

#: The command that produced VP-016's ``<worktree>/len`` refusal.
#:
#: ``if a > len(b):`` 里的 ``>`` 是 Python 的比较运算符；旧解析器把它当成
#: shell 重定向，于是下一个 token ``len`` 被当成了写目标。
VP_016_COMMAND = "python3 - <<'PY'\nif a > len(b):\n    pass\nPY\n"

#: The two commands that produced VP-024's ``single`` / ``index`` refusals.
VP_024_COMMANDS = (
    "python3 - <<'PY'\nif a > single(b):\n    pass\nPY\n",
    "python3 - <<'PY'\nif a > index(b):\n    pass\nPY\n",
)

#: The memory file VP-024 tried to write — outside the writable scope, and
#: outside the tree under verification as well.
MEMORY_PATH = (
    "~/.claude/projects/pdt/memory/"
    "verification-write-guard-blocks-heredoc-redirects.md"
)

#: The event an out-of-tree refusal is recorded under.
OUTSIDE_EVENT = "verifier_refused_outside_scope"

#: The event an in-tree refusal is recorded under.
MUTATION_EVENT = "verifier_mutated_tree"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _real_home() -> Path:
    """The home this run was launched with — not the redirected one.

    ``install_hermetic_env()`` points ``$HOME`` at a ``mkdtemp`` directory
    inside the zone the write guard allows unconditionally.  A stand-in
    repository built there would be *allowed* to be written to, so the case
    would assert nothing at all.  The launch home is outside every temporary
    root on both platforms — the same helper, for the same reason, as
    ``tests/unit/test_verifier_write_scope_regression.py``.
    """
    value = os.environ.get("PDT_TEST_REAL_HOME")
    if not value:
        raise RuntimeError(
            "PDT_TEST_REAL_HOME is unset; install_hermetic_env() must run "
            "before this module derives a path.  Falling back to "
            "Path.home() would return the redirected tempdir home, which "
            "the write guard allows unconditionally."
        )
    return Path(value)


def _round1_worktree() -> Path:
    """The checkout the round-1 records point at.

    它**不是**本夹具里被验证的那棵树：round 1 的证据是在另一个 checkout 上
    写下的，而本夹具的被验证树是刚建出来的那个。两条 Bash 记录因此落在树外
    —— 这正是"账本上有一条拒绝"与"被验证的树被改过"之间的区别。
    """
    return _real_home() / "worktrees" / "svc-readiness"


def _bash_payload(command: str, cwd: Path) -> Dict[str, Any]:
    """A Bash payload running ``command`` in ``cwd``."""
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(cwd)},
    }


def _write_payload(path: Any) -> Dict[str, Any]:
    """A Write payload naming ``path``."""
    return {"tool_name": "Write", "tool_input": {"file_path": str(path)}}


def _arm(
    monkeypatch,
    plan_dir: Path,
    vp_id: str,
    project_dir: Path,
) -> None:
    """Set up the environment the guard and the ledger read.

    ``PDT_VP_WRITE_SCOPE`` / ``PDT_VP_ID`` / ``PDT_VP_ROUND`` 决定证据落在哪、
    归谁、哪一轮；``PDT_PROJECT_DIR`` 是 Bash 分支认的"被验证仓库"。``HOME``
    指回启动时的家目录 —— 理由见模块 docstring。
    """
    monkeypatch.setenv(verification_write_guard.SCOPE_ENV, str(plan_dir))
    monkeypatch.setenv(verification_write_guard.VP_ID_ENV, vp_id)
    monkeypatch.setenv(verification_write_guard.ROUND_ENV, str(ROUND_NO))
    monkeypatch.setenv("PDT_PROJECT_DIR", str(project_dir))
    monkeypatch.setenv("HOME", str(_real_home()))


def _marker(plan_dir: Path, vp_id: str) -> Path:
    """Where the guard records this VP's refusals for this round."""
    return (
        verification_write_guard.refusal_dir(plan_dir, ROUND_NO)
        / f"{vp_id}.jsonl"
    )


def _passed_verdict() -> Verdict:
    """The verdict a sub-agent hands back when everything went fine."""
    return Verdict(
        verdict="PASSED",
        reasons=["stub"],
        evidence=["stub evidence"],
        provider="vendor-a-pro",
        chosen_model="Vendor A-M3",
        model_complexity="simple",
    )


def _orchestrator(
    plan_dir: Path,
    project_dir: Path,
) -> Tuple[Orchestrator, Verdict, List[tuple]]:
    """An ``Orchestrator`` with only the attributes this path reads.

    ``__init__`` is skipped on purpose: it wires a coding tool, a persistence
    manager and a venv ``PATH`` prefix, none of which these cases need.  The
    event sink is stubbed because ``_safe_log_freshness_event`` is where the
    verdict rewrite announces itself.
    """
    events: List[tuple] = []
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.plan_dir = plan_dir
    orchestrator.project_dir = project_dir
    orchestrator.coding_tool = None
    orchestrator.persistence = None
    orchestrator._current_round = ROUND_NO
    orchestrator._safe_log_freshness_event = (
        lambda vp_id, event_type, data: events.append((vp_id, event_type, data))
    )
    return orchestrator, _passed_verdict(), events


def _events_of(events: List[tuple], event_type: str) -> List[tuple]:
    """The recorded events of one type."""
    return [event for event in events if event[1] == event_type]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_under_verification() -> Path:
    """The tree under verification — a stand-in checkout, outside every
    temporary root.

    ``backend/app.py`` is the file the constructed "really wrote into the
    tree" sample targets.
    """
    root = _real_home() / f"pdt-heredoc-{uuid.uuid4().hex}"
    (root / "backend").mkdir(parents=True)
    (root / "backend" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """The plan directory — the guard's one writable root, and where the
    refusal evidence lands."""
    path = tmp_path / "plan"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# 1. VP-016 形状：heredoc 不再产生拒绝记录
# ---------------------------------------------------------------------------
def test_heredoc_vp_no_longer_writes_a_refusal(
    monkeypatch, repo_under_verification, plan_dir,
):
    """VP-016 形状：正文里的 ``>`` 不再是重定向 → exit 0，marker 不出现。

    这就是 round-1 那条 ``<worktree>/len`` 的来路：正文是字面数据，
    ``if a > len(b):`` 里的 ``>`` 是 Python 的比较运算符。护栏不再把它算成
    写目标，于是这次调用根本不该被拒 —— 也就没有证据，没有改判。
    """
    _arm(monkeypatch, plan_dir, VP_016, repo_under_verification)

    command = VP_016_COMMAND

    # the parser is still a pure function, and still gets heredoc bodies
    # right — but it is no longer on any decision path, so this is a check
    # of the function itself, not of the guard's verdict
    targets = verification_write_guard.bash_write_targets(
        command, str(repo_under_verification),
    )
    assert targets == [], (
        f"heredoc 正文不是重定向，却算出了写目标 {targets}"
    )

    # and the call itself is allowed — the Bash branch no longer judges
    rc = verification_write_guard.check_bash_payload(
        _bash_payload(command, repo_under_verification),
    )
    assert rc == 0, f"heredoc 正文不该触发拒绝，却拿到了 rc={rc}"

    # no refusal evidence at all — the marker file is not even created
    marker = _marker(plan_dir, VP_016)
    assert not marker.exists(), f"不该留下拒绝证据，却有 {marker}"
    assert verification_write_guard.read_refusals(
        plan_dir, ROUND_NO, VP_016,
    ) == [], "不该留下拒绝记录"

    # so the verdict stands
    orchestrator, passed, events = _orchestrator(
        plan_dir, repo_under_verification,
    )
    verdict = orchestrator._apply_refusal_evidence(passed, VP_016)
    assert verdict is passed, "没有拒绝证据时 verdict 原样返回"
    assert verdict.verdict == "PASSED", verdict
    assert _events_of(events, MUTATION_EVENT) == [], events


# ---------------------------------------------------------------------------
# 2. VP-024 形状：三条记录都在案，但树外的拒绝不改判
# ---------------------------------------------------------------------------
def test_out_of_scope_write_does_not_flip_the_verdict(
    monkeypatch, repo_under_verification, plan_dir,
):
    """VP-024 形状：2 条 Bash + 1 条 Write memory 在案 → verdict 原样。

    round-1 的三条记录被原样重放（记录形状与顺序都照抄：Bash、Bash、Write）。
    其中没有一条落在被验证树里 —— 两条 Bash 指向 round-1 那个 checkout，
    Write 指向家目录下的 memory 目录 —— 所以它们改不了 verdict，只留下
    ``verifier_refused_outside_scope`` 这条事件。

    注意那两条 Bash 记录是**重放**出来的历史证据：Bash 分支现在一条新记录都
    不写（下面第一步就钉了这件事），历史记录仍然读得出来、仍然入账。
    """
    _arm(monkeypatch, plan_dir, VP_024, repo_under_verification)
    worktree = _round1_worktree()

    # 1. the Bash branch produces no refusal record at all — not for the
    #    historical heredoc shapes, and not for a command that writes into
    #    the tree either.  The branch no longer judges; measurement does.
    tree_write = f"echo x >> {repo_under_verification / 'backend' / 'app.py'}"
    for command in VP_024_COMMANDS + (tree_write,):
        assert verification_write_guard.check_bash_payload(
            _bash_payload(command, repo_under_verification),
        ) == 0, "Bash 分支不再判定 —— 树内写命令必须放行"
    assert not _marker(plan_dir, VP_024).exists(), (
        "Bash 分支不再产生拒绝证据 —— 那两条 Bash 记录是旧解析器的产物"
    )

    # 2. replay the two historical Bash records, in the order round-1 wrote them
    for name in ("single", "index"):
        recorded = verification_write_guard.record_refusal(
            "Bash", str(worktree / name),
            verification_write_guard.REASON_OUTSIDE_SCOPE,
        )
        assert recorded is not None, "拒绝证据没有落盘"

    # 3. the memory write is still refused — it is outside the writable scope
    decision = verification_write_guard.check_edit_payload(
        _write_payload(MEMORY_PATH),
    )
    assert decision.allowed is False, "写家目录下的 memory 必须被拒"
    assert decision.reason == verification_write_guard.REASON_OUTSIDE_SCOPE
    assert verification_write_guard.record_refusal(
        "Write", MEMORY_PATH, decision.reason,
    ) is not None, "拒绝证据没有落盘"

    # the ledger now carries the round-1 shape: two Bash refusals and one
    # Write refusal, in that order
    records = verification_write_guard.read_refusals(
        plan_dir, ROUND_NO, VP_024,
    )
    assert len(records) == 3, records
    assert [record["tool"] for record in records] == ["Bash", "Bash", "Write"], (
        records
    )
    assert [record["reason"] for record in records] == [
        verification_write_guard.REASON_OUTSIDE_SCOPE,
    ] * 3, records
    assert records[2]["path"] == MEMORY_PATH, records

    # 4. and the verdict stands: none of the three touched the tree
    orchestrator, passed, events = _orchestrator(
        plan_dir, repo_under_verification,
    )
    verdict = orchestrator._apply_refusal_evidence(passed, VP_024)
    assert verdict is passed, "树外的拒绝不改判"
    assert verdict.verdict == "PASSED", verdict
    assert verdict.failure_class == "", verdict

    # the refusals are on record, under their own event
    outside = _events_of(events, OUTSIDE_EVENT)
    assert len(outside) == 1, events
    vp_id, _, data = outside[0]
    assert vp_id == VP_024, outside
    assert data["count"] == 3, data
    assert data["round"] == ROUND_NO, data
    assert data["paths"] == [
        str(worktree / "single"),
        str(worktree / "index"),
        MEMORY_PATH,
    ], data

    # and no mutation was claimed
    assert _events_of(events, MUTATION_EVENT) == [], events


# ---------------------------------------------------------------------------
# 3. 真写树内文件 → 仍然改判
# ---------------------------------------------------------------------------
def test_genuine_in_tree_write_still_flips(
    monkeypatch, repo_under_verification, plan_dir,
):
    """真写被验证树里的文件 → 护栏拒绝 → verdict 改判 FAILED。

    这是负控：证明前两条不是把护栏整体放宽了。写进被验证树的写仍然被拒，
    证据仍然改判（``verifier_mutated_tree`` / ``infra`` / ``deterministic``）。

    负控盯的是 **Edit/Write** 分支：Bash 分支是**故意**放开的（树里真的被改
    过，由测量链路的存证/回滚/记账回答），所以"真写树内文件仍被拒"这条断言
    说的是另一条路 —— 它证明护栏没有整体失效。
    """
    _arm(monkeypatch, plan_dir, VP_055, repo_under_verification)
    target = repo_under_verification / "backend" / "app.py"

    decision = verification_write_guard.check_edit_payload(
        _write_payload(target),
    )
    assert decision.allowed is False, "写被验证树必须被拒"
    assert decision.reason == verification_write_guard.REASON_OUTSIDE_SCOPE
    assert verification_write_guard.record_refusal(
        "Write", str(target), decision.reason,
    ) is not None, "拒绝证据没有落盘"

    orchestrator, passed, events = _orchestrator(
        plan_dir, repo_under_verification,
    )
    verdict = orchestrator._apply_refusal_evidence(passed, VP_055)

    assert verdict.verdict == "FAILED", verdict
    assert verdict.failure_class == "infra", verdict
    assert verdict.failure_class_source == "deterministic", verdict
    assert verdict.reasons, "改判过的 verdict 必须说明原因"
    assert verdict.reasons[0].startswith("verifier_mutated_tree"), (
        verdict.reasons
    )
    assert any(str(target) in reason for reason in verdict.reasons), (
        verdict.reasons
    )

    # the mutation was claimed on its own event
    mutation = _events_of(events, MUTATION_EVENT)
    assert len(mutation) == 1, events
    assert mutation[0][2]["count"] == 1, mutation

    # and the tree itself is untouched — the guard stopped the write
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


# ---------------------------------------------------------------------------
# 4. 本轮起点之前的旧记录不再改判（2026-11-06）
# ---------------------------------------------------------------------------
#
# ``POST /api/verification/{id}/reset_rounds`` 把轮次计数器拨回 1，而
# ``round-1/`` 目录**不清** —— 于是上一次尝试（可能是昨天、可能是上一次
# reset）留下的拒绝记录还躺在那里，被当成这一轮的证据，把整轮 VP 误判成
# ``verifier_mutated_tree``。读的时候按"本轮开工的时刻"过滤，旧记录就不
# 再算数了。
#
# 下面两条钉的是**改判路径**本身：起点之前的记录不再改判，起点之后的照旧
# 改判（护栏没有因此被削弱）。

#: 本轮开工的时刻。
ROUND_START = "2026-11-06T09:57:06Z"

#: 上一次尝试留下的记录的时间戳（在 ``ROUND_START`` 之前）。
OLD_TS = "2026-11-04T09:00:00Z"

#: 本轮之内写下的记录的时间戳（在 ``ROUND_START`` 之后）。
NEW_TS = "2026-11-06T10:00:00Z"


def _refusal_record(vp_id: str, path: Path, ts: str) -> Dict[str, Any]:
    """一条与护栏写出的形状相同的拒绝记录。"""
    return {
        "ts": ts,
        "tool": "Write",
        "path": str(path),
        "reason": verification_write_guard.REASON_OUTSIDE_SCOPE,
        "vp_id": vp_id,
        "round": ROUND_NO,
    }


def _write_refusal_record(plan_dir: Path, record: Dict[str, Any]) -> Path:
    """把一条拒绝记录追加进本轮的拒绝账本。"""
    marker = (
        verification_write_guard.refusal_dir(plan_dir, ROUND_NO)
        / f"{record['vp_id']}.jsonl"
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    with open(marker, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return marker


def test_refusal_from_before_the_round_start_does_not_flip_the_verdict(
    repo_under_verification, plan_dir,
):
    """本轮起点**之前**写下的拒绝记录 → verdict 原样成立。

    这条记录落在被验证树里（路径是真的树内路径），所以在没有起点过滤的
    时候它会把 PASSED 改判成 FAILED —— 那是旧证据在替今天说话。
    """
    target = repo_under_verification / "backend" / "app.py"
    _write_refusal_record(
        plan_dir, _refusal_record(VP_055, target, OLD_TS),
    )

    orchestrator, passed, events = _orchestrator(
        plan_dir, repo_under_verification,
    )
    orchestrator._round_started_at = ROUND_START

    verdict = orchestrator._apply_refusal_evidence(passed, VP_055)
    assert verdict is passed, "本轮起点之前的记录不是本轮的证据"
    assert verdict.verdict == "PASSED", verdict
    assert verdict.failure_class == "", verdict
    assert _events_of(events, MUTATION_EVENT) == [], events


def test_refusal_from_after_the_round_start_still_flips(
    repo_under_verification, plan_dir,
):
    """负控：本轮起点**之后**写下的记录照旧改判 —— 护栏没有被削弱。

    两条记录都在案：旧的（起点之前）被忽略，新的（起点之内）照旧改判。
    """
    target = repo_under_verification / "backend" / "app.py"
    _write_refusal_record(
        plan_dir, _refusal_record(VP_055, target, OLD_TS),
    )
    _write_refusal_record(
        plan_dir, _refusal_record(VP_055, target, NEW_TS),
    )

    orchestrator, passed, events = _orchestrator(
        plan_dir, repo_under_verification,
    )
    orchestrator._round_started_at = ROUND_START

    verdict = orchestrator._apply_refusal_evidence(passed, VP_055)
    assert verdict.verdict == "FAILED", verdict
    assert verdict.failure_class == "infra", verdict
    assert verdict.reasons[0].startswith("verifier_mutated_tree"), verdict

    mutation = _events_of(events, MUTATION_EVENT)
    assert len(mutation) == 1, events
    assert mutation[0][2]["count"] == 1, (
        "只有本轮起点之后的那一条算证据 —— 起点之前的不算"
    )


def test_refusal_without_a_round_start_still_flips(
    repo_under_verification, plan_dir,
):
    """回归：拿不到起点（``None``）→ 照旧全读，改判照旧发生。

    拿不到起点时退回今天的行为（全读）：宁可能疑，也不能因为读不出起点
    就放过一次越权写。
    """
    target = repo_under_verification / "backend" / "app.py"
    _write_refusal_record(
        plan_dir, _refusal_record(VP_055, target, OLD_TS),
    )

    orchestrator, passed, events = _orchestrator(
        plan_dir, repo_under_verification,
    )
    orchestrator._round_started_at = None

    verdict = orchestrator._apply_refusal_evidence(passed, VP_055)
    assert verdict.verdict == "FAILED", verdict


# ---------------------------------------------------------------------------
# 5. 轮次起点落盘，并在"重启"之后被采用
# ---------------------------------------------------------------------------
class _StubPersistence:
    """``start_verification_round`` 只要 persistence 开一个新日志文件。"""

    def start_round(self, round_number: int) -> Path:
        return Path(f"verification_{round_number}.log")

    def update_report(self, report: Any) -> None:
        """轮次被阻断时，报告会走这条路写回去。"""


def _bare_orchestrator(plan_dir: Path, project_dir: Path) -> Orchestrator:
    """一个只够跑 ``start_verification_round`` 的 orchestrator。"""
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.plan_dir = plan_dir
    orchestrator.project_dir = project_dir
    orchestrator.persistence = _StubPersistence()
    orchestrator._current_round = 0
    orchestrator._safe_log_freshness_event = lambda *args, **kwargs: None
    return orchestrator


def _started_marker(plan_dir: Path) -> Path:
    """本轮起点标记落在哪。"""
    return (
        plan_dir / "vp_write_scope" / "rounds" / f"round-{ROUND_NO}.started"
    )


def test_round_start_is_written_when_the_round_starts(
    repo_under_verification, plan_dir,
):
    """轮次开始 → 起点被记下，并落盘成一行 ISO 串。"""
    orchestrator = _bare_orchestrator(plan_dir, repo_under_verification)
    orchestrator.start_verification_round(ROUND_NO)

    stamp = orchestrator._round_started_at
    assert stamp, "轮次开始必须记下起点"
    assert len(stamp) == 20 and stamp.endswith("Z"), stamp

    marker = _started_marker(plan_dir)
    assert marker.is_file(), "起点必须落盘 —— 否则重启后会退化成全读"
    assert marker.read_text(encoding="utf-8").strip() == stamp


def test_round_start_survives_a_restart(
    repo_under_verification, plan_dir,
):
    """重启（换一个 agent 实例）→ 接着跑的这一轮仍然用原来的起点。

    这是落盘的意义：进程换了一个，本轮的起点不能跟着换 —— 换了就等于把
    本轮中途已经写下的越权写排除在本轮之外。
    """
    marker = _started_marker(plan_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("2026-11-04T09:00:00Z\n", encoding="utf-8")

    orchestrator = _bare_orchestrator(plan_dir, repo_under_verification)
    orchestrator.start_verification_round(ROUND_NO)

    assert orchestrator._round_started_at == "2026-11-04T09:00:00Z", (
        "重启后必须采用盘上的起点，而不是现在"
    )
    assert marker.read_text(encoding="utf-8").strip() == "2026-11-04T09:00:00Z", (
        "采用盘上的起点不等于把它改掉"
    )


def test_a_new_attempt_after_the_round_ends_gets_a_fresh_start(
    repo_under_verification, plan_dir,
):
    """这一轮结束 → 起点标记收掉 → 下一次对同一个轮次号的尝试是新起点。

    没有这一步，``reset_rounds`` 之后的 round 1 会接着用上一次尝试的起点，
    上一次尝试留下的拒绝记录就又算进了这一轮的证据里 —— 正是这个改动要
    修的坑。
    """
    marker = _started_marker(plan_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("2026-11-04T09:00:00Z\n", encoding="utf-8")

    orchestrator = _bare_orchestrator(plan_dir, repo_under_verification)
    orchestrator.start_verification_round(ROUND_NO)
    assert orchestrator._round_started_at == "2026-11-04T09:00:00Z"

    # 这一轮结束
    orchestrator._clear_round_started_at(ROUND_NO)
    assert not marker.exists(), "轮次结束就该把起点标记收掉"

    # 下一次对同一个轮次号的尝试：新的起点
    again = _bare_orchestrator(plan_dir, repo_under_verification)
    again.start_verification_round(ROUND_NO)
    assert again._round_started_at != "2026-11-04T09:00:00Z", (
        "新的一次尝试必须有它自己的起点"
    )


def test_the_round_itself_closes_its_start_marker_when_it_ends(
    repo_under_verification, plan_dir,
):
    """轮次方法自己收尾：轮次期间标记在，轮次结束标记不在。

    上一条测试直接调 ``_clear_round_started_at`` —— 它证明那个方法能用，
    但不证明**轮次方法**真的调它。接线漏了的话，收尾就成了"看心情"，
    而 ``reset_rounds`` 之后的 round 1 又会接着用上一次尝试的起点 —— 正是
    这个改动要修的坑。

    这里让轮次真的跑起来：base drift 预检直接判阻断，轮次在那一步就结束
    （这是最短的一条"轮次结束"的路），预检那一刻正好是轮次的中途。
    """
    marker = _started_marker(plan_dir)
    seen_during_the_round = []

    orchestrator = _bare_orchestrator(plan_dir, repo_under_verification)
    orchestrator._clear_verification_state_files = lambda: None
    orchestrator._freshness_cache = SimpleNamespace(clear=lambda: None)
    orchestrator._rebuild_coordinator = SimpleNamespace(clear=lambda: None)

    def _blocked():
        # 轮次的中途：起点标记必须已经在了 —— 证据窗口是开着的。
        seen_during_the_round.append(marker.is_file())
        return {"overall_status": "BLOCKED", "summary": "drift"}

    orchestrator._run_base_drift_preflight = _blocked

    report = orchestrator.run_full_verification(ROUND_NO)

    assert report["overall_status"] == "BLOCKED", report
    assert seen_during_the_round == [True], (
        "轮次期间起点标记必须在 —— 读账本要靠它"
    )
    assert not marker.exists(), (
        "轮次结束必须把起点标记收掉 —— 否则下一次对同一个轮次号的尝试"
        "会接着用上一次尝试的起点"
    )


def test_a_round_that_raises_also_closes_its_start_marker(
    repo_under_verification, plan_dir,
):
    """轮次抛异常 → 起点标记同样收掉。

    一次尝试就是一次尝试：它以异常收场也是**结束了**。下一次对同一个轮次号
    的尝试是新的尝试，它该有自己的起点 —— 否则上一次尝试（崩掉的那次）留下
    的拒绝记录又算进了这一轮的证据。

    注意区分：**进程被杀掉**（后端重启）走不到收尾，标记留在盘上，接着跑的
    那一轮因此仍然用原来的起点 —— 那是 brief 要的"重启不丢起点"。
    """
    marker = _started_marker(plan_dir)
    seen_during_the_round = []

    orchestrator = _bare_orchestrator(plan_dir, repo_under_verification)
    orchestrator._clear_verification_state_files = lambda: None
    orchestrator._freshness_cache = SimpleNamespace(clear=lambda: None)
    orchestrator._rebuild_coordinator = SimpleNamespace(clear=lambda: None)

    def _boom():
        # 轮次的中途：起点标记必须已经在了。
        seen_during_the_round.append(marker.is_file())
        raise RuntimeError("轮次崩了")

    orchestrator._run_base_drift_preflight = _boom

    with pytest.raises(RuntimeError):
        orchestrator.run_full_verification(ROUND_NO)

    assert seen_during_the_round == [True], (
        "轮次期间起点标记必须在 —— 没有它就没有证据窗口"
    )
    assert not marker.exists(), (
        "崩掉的尝试也是结束了 —— 起点标记必须收掉"
    )
