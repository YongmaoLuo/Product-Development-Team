"""Round-end quarantine of a verifier's mutation of the tree it verified.

Why these exist
---------------
The write guard (``backend/verification_write_guard.py``) refuses a
verification sub-agent's out-of-scope write and leaves the refusal on
record under ``<plan>/vp_write_scope/refusals/round-<n>/<vp>.jsonl``.
A refusal is evidence, not a guarantee: the guard only knows the write
paths it can recognise, and a sub-agent that slips past it leaves the
tree it was supposed to only read changed.

So the round has to settle the question at its end.  If the round has
refusal evidence, whatever the tree is carrying is quarantined — the
mutation is saved as a patch under the plan directory, and the tree goes
back to the commit it was on.  The patch is what makes that safe: the
mutation is not thrown away, it is filed.

The contract pinned here:

  * **no refusals** → nothing is touched.  The tree may be dirty because
    somebody else is working in it, and the round does not get to decide
    that somebody else's change is garbage;
  * **refusals and a dirty tree** → the patch exists and the tree is
    clean again;
  * **refusals and a clean tree** → there is nothing to save, so the
    patch is ``None`` — but the event is still recorded, because "the
    verifier tried to write and was stopped" is exactly what the
    operator has to know;
  * a quarantine that fails is recorded with its reason and the round
    carries on.

Every repository here is built from scratch in a temporary directory, so
the tests never touch a real worktree.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_write_guard  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402

#: The identity every commit in these tests carries.  It is passed with
#: ``-c`` instead of being configured globally: the suite runs with
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

#: The event the round end writes when it quarantined something.
QUARANTINE_EVENT = "verifier_mutated_tree_quarantined"


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


def _repo(tmp_path: Path) -> Path:
    """A git repository with one committed file."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "app.py").write_text("line1\nline2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    return root


def _plan_dir(tmp_path: Path) -> Path:
    """The plan directory the patch and the refusal evidence live in."""
    return tmp_path / "plan"


def _agent(
    tmp_path: Path,
    repo: Path,
    plan_dir: Path,
    events: List[tuple],
    round_no: int = 1,
) -> Orchestrator:
    """An ``Orchestrator`` with only the attributes the quarantine reads.

    ``__init__`` is skipped on purpose: it wires a coding tool, a
    persistence manager and a venv ``PATH`` prefix, none of which this
    needs — and the persistence manager is exactly what
    ``_safe_log_freshness_event`` writes through, so it is stubbed here.
    """
    agent = Orchestrator.__new__(Orchestrator)
    agent.plan_dir = plan_dir
    agent.project_dir = repo
    agent._current_round = round_no
    agent._safe_log_freshness_event = (
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        )
    )
    return agent


def _record_refusal(
    monkeypatch,
    plan_dir: Path,
    round_no: int = 1,
    vp_id: str = "VP-055",
    path: Optional[Path] = None,
) -> Optional[Path]:
    """Write a refusal marker the way the guard itself writes it.

    The real writer is used on purpose: the marker format is the
    contract between the guard and this reader, and a hand-written
    marker would only prove that the reader can read a marker it wrote
    itself.
    """
    monkeypatch.setenv(verification_write_guard.SCOPE_ENV, str(plan_dir))
    monkeypatch.setenv(verification_write_guard.VP_ID_ENV, vp_id)
    monkeypatch.setenv(verification_write_guard.ROUND_ENV, str(round_no))
    return verification_write_guard.record_refusal(
        "Edit", str(path or (plan_dir / "app.py")), "outside_scope",
    )


def _quarantine_events(events: List[tuple]) -> List[tuple]:
    """The quarantine events out of the recorded ones."""
    return [event for event in events if event[1] == QUARANTINE_EVENT]


# ---------------------------------------------------------------------------
# 有拒绝证据 + 树被动过 → 存 patch，工作区还原
# ---------------------------------------------------------------------------
def test_refusals_trigger_quarantine_and_restore(tmp_path, monkeypatch):
    """越权改动被存成 patch，工作区回到干净状态。"""
    repo = _repo(tmp_path)
    plan_dir = _plan_dir(tmp_path)
    events: List[tuple] = []
    agent = _agent(tmp_path, repo, plan_dir, events)

    # the verifier changed a tracked file and left a new one behind
    (repo / "app.py").write_text("line1\nCHANGED\n", encoding="utf-8")
    (repo / "sneaky.py").write_text("brand new\n", encoding="utf-8")
    assert _status(repo) != ""

    _record_refusal(
        monkeypatch, plan_dir, round_no=1, vp_id="VP-055",
        path=repo / "app.py",
    )

    patch = agent._quarantine_round_tree_mutation(None)

    # the mutation was saved …
    assert patch is not None
    assert patch.exists()
    assert patch.parent == plan_dir / "vp_write_scope" / "quarantine"
    assert patch.name.startswith("round-1-")
    assert patch.suffix == ".patch"
    saved = patch.read_text(encoding="utf-8", errors="replace")
    assert "+CHANGED" in saved
    assert "sneaky.py" in saved

    # … and the tree was put back
    assert (repo / "app.py").read_text(encoding="utf-8") == "line1\nline2\n"
    assert not (repo / "sneaky.py").exists()
    assert _status(repo) == ""

    # and the round says what it did
    recorded = _quarantine_events(events)
    assert len(recorded) == 1
    vp_id, event_type, data = recorded[0]
    assert vp_id == "__round__"
    assert event_type == QUARANTINE_EVENT
    assert data["patch"] == str(patch)
    assert "app.py" in data["restored"]
    assert data["vps"] == ["VP-055"]


# ---------------------------------------------------------------------------
# 没有拒绝证据 → 一个字节都不动
# ---------------------------------------------------------------------------
def test_no_refusals_means_no_restore(tmp_path):
    """没有拒绝证据 → 不碰工作区（那可能是别人的改动）。"""
    repo = _repo(tmp_path)
    plan_dir = _plan_dir(tmp_path)
    events: List[tuple] = []
    agent = _agent(tmp_path, repo, plan_dir, events)

    (repo / "app.py").write_text("line1\nCHANGED\n", encoding="utf-8")
    dirty_before = _status(repo)

    patch = agent._quarantine_round_tree_mutation(None)

    assert patch is None
    # the tree is exactly as it was — not restored, not touched
    assert (repo / "app.py").read_text(encoding="utf-8") == "line1\nCHANGED\n"
    assert _status(repo) == dirty_before
    # and nothing was written anywhere
    assert not (plan_dir / "vp_write_scope" / "quarantine").exists()
    assert _quarantine_events(events) == []


# ---------------------------------------------------------------------------
# 有拒绝证据但树是干净的 → 没有 patch，事件照记
# ---------------------------------------------------------------------------
def test_refusals_with_clean_tree_log_patch_none(tmp_path, monkeypatch):
    """干净树 → patch=None，但事件仍然在（护栏拦住了，这是要让人知道的）。"""
    repo = _repo(tmp_path)
    plan_dir = _plan_dir(tmp_path)
    events: List[tuple] = []
    agent = _agent(tmp_path, repo, plan_dir, events)

    _record_refusal(
        monkeypatch, plan_dir, round_no=1, vp_id="VP-055",
        path=repo / "app.py",
    )

    patch = agent._quarantine_round_tree_mutation(None)

    assert patch is None
    assert _status(repo) == ""

    recorded = _quarantine_events(events)
    assert len(recorded) == 1
    vp_id, event_type, data = recorded[0]
    assert vp_id == "__round__"
    assert event_type == QUARANTINE_EVENT
    assert data["patch"] is None
    assert data["restored"] == []
    assert data["vps"] == ["VP-055"]
    assert not (plan_dir / "vp_write_scope" / "quarantine").exists()


# ---------------------------------------------------------------------------
# 位置承重：隔离必须早于 deferred 核对的重跑
# ---------------------------------------------------------------------------
def test_quarantine_runs_before_the_deferred_flush():
    """树还原之后 deferred 核对才能重跑 —— 顺序不能反。"""
    source = inspect.getsource(Orchestrator.run_full_verification)
    quarantine_at = source.find("_quarantine_round_tree_mutation(")
    flush_at = source.find("_flush_deferred_evidence_checks(")

    assert quarantine_at != -1, "the round end must quarantine the tree"
    assert flush_at != -1, "the round end must flush the deferred checks"
    assert quarantine_at < flush_at


# ---------------------------------------------------------------------------
# 隔离失败 → 记下原因，轮次继续
# ---------------------------------------------------------------------------
def test_quarantine_failure_is_recorded_and_does_not_raise(
    tmp_path, monkeypatch,
):
    """隔离不了 → 事件里带 error，不抛异常，轮次继续。"""
    plan_dir = _plan_dir(tmp_path)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "keep.txt").write_text("do not touch\n", encoding="utf-8")
    events: List[tuple] = []
    agent = _agent(tmp_path, plain, plan_dir, events)

    _record_refusal(
        monkeypatch, plan_dir, round_no=1, vp_id="VP-055",
        path=plain / "keep.txt",
    )

    # no exception: the round's own verdict must not be replaced by a
    # failure of the clean-up
    patch = agent._quarantine_round_tree_mutation(None)

    assert patch is None
    recorded = _quarantine_events(events)
    assert len(recorded) == 1
    _, _, data = recorded[0]
    assert data["patch"] is None
    assert data["error"], "a failed quarantine has to say why"
    assert data["vps"] == ["VP-055"]
    # and nothing was deleted on the way out
    assert (plain / "keep.txt").read_text(encoding="utf-8") == "do not touch\n"
