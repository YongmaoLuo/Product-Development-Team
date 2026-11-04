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

import os
import shutil
import sys
import uuid
from pathlib import Path
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

    # the write target the old parser used to derive out of the body
    targets = verification_write_guard.bash_write_targets(
        command, str(repo_under_verification),
    )
    assert targets == [], (
        f"heredoc 正文不是重定向，却算出了写目标 {targets}"
    )

    # and the call itself is allowed
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
    """
    _arm(monkeypatch, plan_dir, VP_024, repo_under_verification)
    worktree = _round1_worktree()

    # 1. the two heredoc commands no longer produce anything at all
    for command in VP_024_COMMANDS:
        assert verification_write_guard.check_bash_payload(
            _bash_payload(command, repo_under_verification),
        ) == 0, "heredoc 正文不该触发拒绝"
    assert not _marker(plan_dir, VP_024).exists(), (
        "heredoc 正文不该留下拒绝证据 —— 那两条 Bash 记录是旧解析器的产物"
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
