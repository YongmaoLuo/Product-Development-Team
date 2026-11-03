"""端到端回归：会写源码的假 sub-agent 撞上写入护栏 → 被验证仓库保持干净。

背景
----
2026-11-02：VP-055 的子 agent 直接改写了**被验证仓库**的源码，整轮验证因此
失真。落地这一串改动之后：

* 任务 1/3 —— 写护栏（``backend/verification_write_guard.py``）把越界的
  Edit / Write 机械拒绝，并留下证据
  ``<plan>/vp_write_scope/refusals/round-<n>/<vp>.jsonl``；
* 任务 5/6 —— 轮次收尾按拒绝证据把工作区隔离成 plan 目录里的 patch 并还原；
* 任务 7 —— 越权过的 VP 拿着 PASSED 回来时改判，原因
  ``verifier_mutated_tree``。

前面的用例各自钉住其中一段（钩子、隔离、改判）。本文件钉的是**整条链路**：
一个**会写源码的假 sub-agent** 真的去改被验证仓库的源码，链路的每一段都要
按自己的方式生效 —— 写被拒就一个字节都不许落地，落地了也要被还原，而那个
VP 无论如何拿不到 PASSED。

三条断言
--------
1. ``test_hook_blocks_the_source_write``：钩子挡住写入 → 仓库 ``git status``
   干净，拒绝证据在案；
2. ``test_direct_write_is_quarantined_and_restored``：绕过钩子的直接写入 →
   轮次收尾隔离成 plan 目录里的 patch，工作区还原；
3. ``test_refused_vp_reports_verifier_mutated_tree``：越权过的 VP 交回的
   PASSED 被改判，原因含 ``verifier_mutated_tree``。

替身仓库建在哪里
----------------
**不在 pytest 的 ``tmp_path`` 里** —— 护栏放行临时目录（``/tmp``、
``/private/tmp``、``tempfile.gettempdir()``），而 ``tmp_path`` 在 macOS 上
落在 ``$TMPDIR``、在 Linux 上落在 ``/tmp``：写进去会被当成"写临时目录"而
放行，测出来的是另一回事（macOS 上绿、Linux 上红的那种）。替身仓库因此建
在**启动时的家目录**下（``$PDT_TEST_REAL_HOME``，见
``backend/_pytest_hermetic_env.py``）—— 那是两个平台上都在所有临时根之外的
地方，与 ``tests/unit/test_allowed_write_files_hook.py`` 同一个理由、同一个
约定。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
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
from verification_subagent import SettingsJsonBuilder, Verdict  # noqa: E402

#: The commit identity every commit in these tests carries.  It is passed
#: with ``-c`` instead of being configured globally: the suite runs with
#: ``$HOME`` pointed at a scratch directory, where ``user.email`` is not
#: set, and a commit without an identity fails outright.
GIT_IDENTITY = (
    "-c",
    "user.email=t@example.com",
    "-c",
    "user.name=t",
    "-c",
    "commit.gpgsign=false",
)

#: The verification point whose write is refused.
VP_ID = "VP-055"

#: The round the refusal belongs to.
ROUND_NO = 1

#: The committed content of the stand-in repository's source file.
COMMITTED = "VALUE = 1\n"

#: What the stray sub-agent tries to put into that file.
MUTATED = "VALUE = 2\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _real_home() -> Path:
    """The home this run was launched with — not the redirected one.

    ``install_hermetic_env()`` points ``$HOME`` at a ``mkdtemp`` directory,
    which is inside the zone the write guard allows unconditionally.  A
    stand-in repository built there would be *allowed* to be written to, so
    the case would assert nothing at all.  The launch home is outside every
    temporary root on both platforms — the same helper, for the same
    reason, as ``tests/unit/test_allowed_write_files_hook.py``.
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


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command inside ``repo`` and fail loudly if it errors."""
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _status(repo: Path) -> str:
    """The porcelain status of ``repo`` — empty means a clean tree."""
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _build_settings(
    plan_dir: Path,
    vp_id: str = VP_ID,
    round_no: int = ROUND_NO,
) -> Tuple[Dict[str, Any], SettingsJsonBuilder]:
    """The settings.json the verification sub-agent really runs with.

    Returns:
        ``(document, builder)`` — the parsed document, and the builder so
        the caller can ``cleanup()`` the temporary file afterwards.
    """
    builder = SettingsJsonBuilder(
        vp_id=vp_id,
        plan_id="p",
        plan_dir=plan_dir,
        round_no=round_no,
    )
    path = builder.build()
    return json.loads(Path(path).read_text(encoding="utf-8")), builder


def _hook_command(document: Dict[str, Any], matcher: str) -> str:
    """The write-guard hook command the built settings.json carries."""
    for entry in document.get("hooks", {}).get("PreToolUse", []):
        if entry.get("matcher") != matcher:
            continue
        command = entry["hooks"][0]["command"]
        if "verification_write_guard.py" in command:
            return command
    raise AssertionError(f"settings.json 里没有 {matcher} 的写入护栏钩子")


def _hook_env(
    settings_env: Dict[str, Any],
    project_dir: Path,
) -> Dict[str, str]:
    """The environment a hook subprocess runs with.

    Claude Code folds a settings file's ``env`` into the process
    environment and the hook subprocesses inherit it — so the merged
    result is what a hook really sees.  The ambient ``PDT_VP_*`` variables
    are cleared first: a value left over on the host must not decide an
    assertion on the suite's behalf.
    """
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("PDT_VP_"):
            env.pop(name, None)
    env["PDT_PROJECT_DIR"] = str(project_dir)
    for key, value in (settings_env or {}).items():
        env[str(key)] = str(value)
    return env


def _run_hook(
    command: str,
    payload: Dict[str, Any],
    env: Dict[str, str],
) -> subprocess.CompletedProcess:
    """Ask the hook whether a tool call may proceed.

    The hook reads the tool payload from stdin as JSON and exits 2 to
    block the call or 0 to allow it.
    """
    return subprocess.run(
        command,
        shell=True,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _write_out_of_band(target: Path, content: str) -> None:
    """Write a file without going through the tool surface at all.

    This is what the guard cannot see: the write happens in another
    process, outside the tool call the hook was asked about.  It stands
    in for the sub-agent slipping past the guard — the case the round-end
    quarantine exists for.
    """
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; open(sys.argv[1], 'w', encoding='utf-8').write(sys.argv[2])",
            str(target),
            content,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


class _FakePersistence:
    """Stands in for the persistence manager the orchestrator writes through."""

    def __init__(self) -> None:
        self.events: List[Tuple[str, str, Dict[str, Any]]] = []

    def write_verification_point_log(
        self, vp_id: str, event_type: str, data: Dict[str, Any],
    ) -> None:
        self.events.append((vp_id, event_type, data))


def _orchestrator(
    plan_dir: Path,
    project_dir: Path,
    events: List[tuple],
    round_no: int = ROUND_NO,
) -> Orchestrator:
    """An ``Orchestrator`` with only the attributes this path reads.

    ``__init__`` is skipped on purpose: it wires a coding tool, a
    persistence manager and a venv ``PATH`` prefix, none of which these
    cases need — and the persistence manager is exactly what
    ``_safe_log_freshness_event`` writes through, so it is stubbed here.
    """
    agent = Orchestrator.__new__(Orchestrator)
    agent.plan_dir = plan_dir
    agent.project_dir = project_dir
    agent.coding_tool = None
    agent.persistence = _FakePersistence()
    agent._current_round = round_no
    agent._safe_log_freshness_event = (
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        )
    )
    return agent


def _refusal_marker(plan_dir: Path, round_no: int = ROUND_NO) -> Path:
    """The marker path the guard is contracted to write."""
    return (
        plan_dir
        / "vp_write_scope"
        / "refusals"
        / f"round-{round_no}"
        / f"{VP_ID}.jsonl"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_under_verification() -> Path:
    """A small git repository standing in for the tree under verification.

    One committed Python file is the whole tree: the sub-agent in these
    cases wants to change exactly that file, so a stray write is visible
    in ``git status`` without any bookkeeping.
    """
    root = _real_home() / f"pdt-vp-write-scope-{uuid.uuid4().hex}"
    (root / "backend").mkdir(parents=True)
    (root / "backend" / "app.py").write_text(COMMITTED, encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """The plan directory the refusal evidence and the patch live in."""
    path = tmp_path / "plan"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# 1. 钩子挡住写入 → 仓库干净，证据在案
# ---------------------------------------------------------------------------
def test_hook_blocks_the_source_write(repo_under_verification, plan_dir):
    """假 sub-agent 写被验证仓库的源码 → 钩子挡下，仓库一个字节都没变。"""
    document, builder = _build_settings(plan_dir)
    try:
        command = _hook_command(document, "Edit|Write")
        env = _hook_env(document.get("env", {}), repo_under_verification)
    finally:
        builder.cleanup()

    target = repo_under_verification / "backend" / "app.py"
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
    }

    # The fake sub-agent asks the hook first — Claude Code runs
    # PreToolUse before the tool — and writes only when it is allowed.
    proc = _run_hook(command, payload, env)
    if proc.returncode == 0:
        _write_out_of_band(target, MUTATED)

    # the write was refused
    assert proc.returncode == 2, (
        "写被验证仓库的源码必须被挡下："
        f"rc={proc.returncode} stderr={proc.stderr!r}"
    )

    # the tree the verifier was supposed to read is untouched
    assert _status(repo_under_verification) == ""
    assert target.read_text(encoding="utf-8") == COMMITTED

    # and the attempt is on record, in the round it happened in
    marker = _refusal_marker(plan_dir)
    assert marker.is_file(), f"拒绝必须留下证据：没有 {marker}"
    records = verification_write_guard.read_refusals(
        plan_dir, ROUND_NO, VP_ID,
    )
    assert len(records) == 1, records
    assert records[0]["path"] == str(target), records
    assert records[0]["reason"] == "outside_scope", records
    assert records[0]["vp_id"] == VP_ID, records
    assert records[0]["round"] == ROUND_NO, records


# ---------------------------------------------------------------------------
# 2. 绕过钩子的直接写入 → 轮次收尾隔离并还原
# ---------------------------------------------------------------------------
def test_direct_write_is_quarantined_and_restored(
    repo_under_verification, plan_dir,
):
    """越过护栏的写入 → 隔离成 plan 目录里的 patch，工作区回到干净。"""
    # the guard refused one attempt and left the evidence behind
    document, builder = _build_settings(plan_dir)
    try:
        command = _hook_command(document, "Edit|Write")
        env = _hook_env(document.get("env", {}), repo_under_verification)
    finally:
        builder.cleanup()

    target = repo_under_verification / "backend" / "app.py"
    refused = _run_hook(
        command,
        {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
        env,
    )
    assert refused.returncode == 2, refused.stderr
    assert _refusal_marker(plan_dir).is_file()

    # ... and another attempt slipped past the guard and really wrote
    _write_out_of_band(target, MUTATED)
    sneaky = repo_under_verification / "backend" / "sneaky.py"
    _write_out_of_band(sneaky, "print('sneaky')\n")
    assert _status(repo_under_verification) != ""

    # the round settles the question at its end
    events: List[tuple] = []
    agent = _orchestrator(plan_dir, repo_under_verification, events)
    patch = agent._quarantine_round_tree_mutation(None)

    # the mutation was saved, not thrown away
    assert patch is not None, "越权改动必须被存成 patch，不能凭空消失"
    assert patch.exists()
    assert patch.parent == plan_dir / "vp_write_scope" / "quarantine"
    saved = patch.read_text(encoding="utf-8", errors="replace")
    assert "+VALUE = 2" in saved, saved
    assert "sneaky.py" in saved, saved

    # and the working tree went back to the commit it was on
    assert _status(repo_under_verification) == ""
    assert target.read_text(encoding="utf-8") == COMMITTED
    assert not sneaky.exists()


# ---------------------------------------------------------------------------
# 3. 越权过的 VP 拿不到 PASSED
# ---------------------------------------------------------------------------
def test_refused_vp_reports_verifier_mutated_tree(
    monkeypatch, repo_under_verification, plan_dir,
):
    """越权写过的 VP 交回的 PASSED 被改判，原因含 verifier_mutated_tree。"""
    import verification_agent

    # the guard refused the write and left the evidence
    document, builder = _build_settings(plan_dir)
    try:
        command = _hook_command(document, "Edit|Write")
        env = _hook_env(document.get("env", {}), repo_under_verification)
    finally:
        builder.cleanup()

    target = repo_under_verification / "backend" / "app.py"
    refused = _run_hook(
        command,
        {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
        env,
    )
    assert refused.returncode == 2, refused.stderr

    # the sub-agent still hands back a PASSED — it cannot see the ledger
    passed = Verdict(
        verdict="PASSED",
        reasons=["stub"],
        evidence=["stub evidence"],
        provider="vendor-a-pro",
        chosen_model="Vendor A-M3",
        model_complexity="simple",
    )

    class _FakeSubAgent:
        """A sub-agent that always says PASSED."""

        def __init__(self, **kwargs) -> None:
            self.init_kwargs = kwargs

        async def run(self, **kwargs):
            return passed

    monkeypatch.setattr(
        verification_agent, "VerificationSubAgent", _FakeSubAgent,
    )

    events: List[tuple] = []
    agent = _orchestrator(plan_dir, repo_under_verification, events)

    verdict = asyncio.run(
        agent._delegate_to_sub_agent({"id": VP_ID}, "code_review", VP_ID),
    )

    # the verdict is no longer the sub-agent's
    assert verdict.verdict == "FAILED", verdict
    assert verdict.reasons, "改判过的 verdict 必须说明原因"
    assert any(
        "verifier_mutated_tree" in reason for reason in verdict.reasons
    ), verdict.reasons
    assert any(str(target) in reason for reason in verdict.reasons), (
        verdict.reasons
    )

    # the routing facts are still the facts of this attempt
    assert verdict.provider == passed.provider, verdict
    assert verdict.chosen_model == passed.chosen_model, verdict

    # and the tree is still clean — the refusal stopped the write
    assert _status(repo_under_verification) == ""
