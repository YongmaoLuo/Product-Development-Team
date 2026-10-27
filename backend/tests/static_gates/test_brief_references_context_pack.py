"""Static gate — the two briefs point at the context pack and inline nothing.

Why this gate exists
--------------------
Every sub-agent that starts inside a worktree has to answer the same
eight questions before it can do anything useful: *where am I*, *which
branch*, *what is the base commit*, *what has changed*, *where is the plan
directory*, *which service ports matter*, *what is the focused test
command*.  Each of them answered those questions by exploring — ``ls``,
``find``, ``git log`` — and the exploration was repeated, because a
sub-agent cannot see what the last one found.  That exploration was 69%
of the tool calls in the largest execution sessions.

Task 5 gave the round ONE file holding those facts
(:mod:`context_pack` → ``<plan_dir>/context/round-<n>.json``).  This gate
pins the half of that contract the *briefs* own:

1. **Both briefs point at the pack.**  The execution brief (the coder
   prompt assembled in ``agent.py``) and the verification brief (the
   prompt ``verification_subagent._build_prompt`` builds) carry the SAME
   fixed sentence, with the pack's absolute path in it.  A brief that
   says something else is a sub-agent that still goes exploring.
2. **Neither brief inlines the facts.**  The pack file is the only
   carrier of the environment facts.  A brief that repeats them is a
   second copy, and the second copy is the one that goes stale.
3. **One file for both sides.**  The two briefs name the SAME
   ``round-<n>.json``.  A second file would be a second version of the
   same facts, and the two versions would disagree.
4. **A missing pack is not a failure.**  The brief falls back to what it
   said before; it never names a file that is not there, and it never
   stops the round.

Why a rendered brief and not a source scan
------------------------------------------
A source scan can only see that the templates do not mention
``branch``.  It cannot see the facts a brief *ends up* carrying — those
arrive from the pack at runtime.  So the gate renders both briefs against
a real fixture (a real git worktree, a real pack) and inspects what came
out.  A source scan is still used for one thing only: to make sure the
fixed sentence exists in both modules, so that a brief assembled by hand
somewhere else cannot quietly use a different wording.

The gate is not vacuous: the detectors below are fed synthetic
violations and must flag them.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

#: ``backend/tests/static_gates`` is two levels below ``backend/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402
from config_registry import ConfigRegistry  # noqa: E402
from context_pack import CONTEXT_PACK_FIELDS  # noqa: E402
from task import SubTask  # noqa: E402
from verification_subagent import (  # noqa: E402
    CONTEXT_PACK_REFERENCE_INSTRUCTION,
    VerificationSubAgent,
)


#: The plan id the fixture uses.  It has to be a name the plan-id
#: derivation accepts (at least four characters, no reserved word),
#: because the agent reads its plan id off the tasks file's parent
#: directory and the verification side resolves the same directory.
PLAN_ID = "ctxpack01"

#: The branch the fixture worktree sits on.  It has to be a name that
#: cannot turn up in a brief by accident — "main" would.
FIXTURE_BRANCH = "ctx-pack-probe-branch"

#: The environment facts this gate checks for inlining.  ``changed_files``
#: is deliberately NOT one of them: the task's own ``files_to_modify``
#: hint legitimately names the same paths, so a brief that mentions one
#: of them has not inlined the environment.
GATED_FIELDS = ("branch", "base_commit", "diff_stat")

#: The fixed sentence, with the path as its only variable part.
_REFERENCE_RE = re.compile(
    r"环境事实见 (?P<path>\S+?)，不要再运行 ls / find / git log 重新探索环境"
)


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def referenced_pack_paths(brief: str) -> List[str]:
    """Every context-pack path the brief points at."""
    return [match.group("path") for match in _REFERENCE_RE.finditer(brief)]


def inlined_env_fields(brief: str, pack: Dict[str, Any]) -> List[str]:
    """Which gated environment facts the brief carries itself.

    A fact counts as inlined when its *value* appears in the brief (the
    branch name, the commit sha, the diff stat) — a brief that pastes
    the facts in has re-introduced the copy the pack exists to remove.
    """
    hits: List[str] = []
    for field in GATED_FIELDS:
        value = pack.get(field)
        if isinstance(value, str) and value and value in brief:
            hits.append(field)
    return hits


def inline_field_segments(brief: str) -> List[str]:
    """Which gated field names appear in the brief as ``name: value``.

    This is the other half of "inlined": a brief may name a fact
    without pasting its value (a field set to ``null``, say), and that
    is still an inline segment the pack was supposed to replace.
    """
    hits: List[str] = []
    for field in GATED_FIELDS:
        if re.search(rf"\b{re.escape(field)}\b\s*[:=]\s*\S", brief):
            hits.append(field)
    return hits


# ---------------------------------------------------------------------------
# The fixture: a plan, a worktree, and the pack the round runs on
# ---------------------------------------------------------------------------


def _git(repo_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _seed_repo(project_dir: Path) -> None:
    """A worktree on a distinctive branch, one commit ahead of ``main``.

    The facts the pack collects have to be *distinguishable* for the
    gate to mean anything: a branch called ``main`` could turn up in a
    brief by accident, and an empty diff would prove nothing.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    _git(project_dir, "init", "--initial-branch=main")
    _git(project_dir, "config", "user.email", "gate@example.com")
    _git(project_dir, "config", "user.name", "Context Pack Gate")
    (project_dir / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "-m", "seed")
    _git(project_dir, "checkout", "-b", FIXTURE_BRANCH)
    (project_dir / "probe.txt").write_text("probe\n", encoding="utf-8")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "-m", "probe")


@pytest.fixture
def round_context(tmp_path, monkeypatch):
    """The plan directory, the worktree, and the plans root both sides read.

    ``PDT_PLANS_DIR`` points at the fixture's own plans tree so the
    verification side resolves the SAME plan directory the agent does.
    The two briefs can only name one file if they agree on where the
    plan lives.
    """
    project_dir = tmp_path / "worktree"
    _seed_repo(project_dir)
    plans_root = tmp_path / "plans"
    plan_dir = plans_root / PLAN_ID
    plan_dir.mkdir(parents=True)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_root))
    return plan_dir, project_dir


def _agent(plan_dir: Path, project_dir: Path) -> AutonomousAgent:
    return AutonomousAgent(
        requirement="context pack gate",
        project_dir=project_dir,
        coding_tool=MagicMock(),
        config=ConfigRegistry.get("coding"),
        logger=MagicMock(),
        tasks_file=plan_dir / "tasks.json",
    )


def _task() -> SubTask:
    return SubTask(
        id="T-1",
        title="probe",
        description="probe task",
        test_command="pytest -q tests/test_probe.py",
        files_to_modify=["probe.txt"],
    )


def _execution_brief(plan_dir: Path, project_dir: Path) -> str:
    """The brief the coder sub-agent opens with."""
    return _agent(plan_dir, project_dir)._build_task_brief(_task())


def _verification_brief(plan_id: str = PLAN_ID) -> str:
    """The brief the verification sub-agent opens with."""
    sub_agent = VerificationSubAgent(method="code_review", plan_id=plan_id)
    return sub_agent._build_prompt(
        {
            "id": "VP-001",
            "title": "probe",
            "expected_result": "the probe holds",
            "verification_method": "code_review",
        },
        attempt=0,
    )


def _read_pack(plan_dir: Path) -> Path:
    """The pack the execution side wrote for this round."""
    packs = sorted((plan_dir / "context").glob("round-*.json"))
    assert packs, "the execution side wrote no context pack"
    return packs[-1]


def _read_pack_data(pack_path: Path) -> Dict[str, Any]:
    return json.loads(pack_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_brief_has_fixed_instruction(round_context) -> None:
    """Both briefs carry the fixed instruction, with the pack's path."""
    plan_dir, project_dir = round_context
    execution = _execution_brief(plan_dir, project_dir)
    verification = _verification_brief(plan_dir.name)

    pack_path = _read_pack(plan_dir)
    expected = CONTEXT_PACK_REFERENCE_INSTRUCTION.format(pack_path=str(pack_path))

    assert expected in execution, (
        "the execution brief does not point at the context pack — a "
        "sub-agent that is not told where the facts are goes and looks "
        "for them (ls / find / git log), which is the exploration the "
        "pack was made to remove"
    )
    assert expected in verification, (
        "the verification brief does not point at the context pack — "
        "the verifier explores the environment the same way the coder "
        "does, and it is the same pack both of them should read"
    )


def test_brief_no_inlined_env_fields(round_context) -> None:
    """Neither brief pastes the environment facts the pack holds."""
    plan_dir, project_dir = round_context
    execution = _execution_brief(plan_dir, project_dir)
    verification = _verification_brief(plan_dir.name)

    pack_data = _read_pack_data(_read_pack(plan_dir))
    for name, brief in (
        ("execution", execution),
        ("verification", verification),
    ):
        assert inlined_env_fields(brief, pack_data) == [], (
            f"the {name} brief inlines environment facts the context pack "
            f"already holds — a second copy of the facts is a second copy "
            "that goes stale"
        )
        assert inline_field_segments(brief) == [], (
            f"the {name} brief names an environment field inline — the "
            "pack is the only carrier of those facts"
        )


def test_same_pack_for_both_sides(round_context) -> None:
    """The two briefs name ONE file — the same one."""
    plan_dir, project_dir = round_context
    execution = _execution_brief(plan_dir, project_dir)
    verification = _verification_brief(plan_dir.name)

    execution_paths = referenced_pack_paths(execution)
    verification_paths = referenced_pack_paths(verification)

    assert len(execution_paths) == 1, (
        f"the execution brief points at {len(execution_paths)} packs: "
        f"{execution_paths!r} — one round, one pack"
    )
    assert execution_paths == verification_paths, (
        "the two briefs point at DIFFERENT packs "
        f"({execution_paths!r} vs {verification_paths!r}) — the coder and "
        "the verifier would then be working from two versions of the "
        "same facts"
    )

    pack_path = Path(execution_paths[0])
    assert pack_path.is_absolute(), (
        f"the brief names a relative pack path ({pack_path}) — a "
        "sub-agent's working directory is not the plan's"
    )
    assert re.fullmatch(r"round-\d+\.json", pack_path.name), (
        f"the pack is not named round-<n>.json ({pack_path.name}) — the "
        "execution and the verification side have to agree on the shape"
    )
    assert pack_path == _read_pack(plan_dir)


def test_concurrent_briefs_share_one_pack(round_context) -> None:
    """Briefs built at the same time must not make two packs.

    The tasks of one layer are dispatched concurrently, so two of them
    can ask for the round's pack at the same moment.  If each of them
    decided "this plan has no pack yet" on its own, the second one to
    land would see the first one's file and write a SECOND pack for the
    same round — and the two briefs would then name different files.
    """
    plan_dir, project_dir = round_context
    agent = _agent(plan_dir, project_dir)
    task = _task()

    briefs: List[str] = []
    barrier = threading.Barrier(4)

    def _build() -> None:
        barrier.wait()
        briefs.append(agent._build_task_brief(task))

    threads = [threading.Thread(target=_build) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    packs = sorted((plan_dir / "context").glob("round-*.json"))
    assert len(packs) == 1, (
        f"one round produced {len(packs)} packs: {[p.name for p in packs]!r} "
        "— the round's facts have to live in ONE file"
    )
    for brief in briefs:
        assert referenced_pack_paths(brief) == [str(packs[0])], (
            "two briefs of the same round named different packs"
        )


def test_missing_pack_falls_back_to_the_existing_brief(
    round_context, monkeypatch
) -> None:
    """A pack that cannot be written must not change the brief."""
    plan_dir, project_dir = round_context

    def _boom(**_kwargs):
        raise RuntimeError("no pack for you")

    monkeypatch.setattr("agent.build_context_pack", _boom)

    brief = _execution_brief(plan_dir, project_dir)
    assert "Subtask: probe task" in brief, (
        "the brief lost its content when the context pack could not be "
        "written — a missing pack must never interrupt the round"
    )
    assert referenced_pack_paths(brief) == [], (
        "the brief names a context pack that does not exist — a sub-agent "
        "that reads it finds nothing and starts exploring anyway"
    )


def test_verification_brief_without_a_pack_says_nothing_about_one(
    tmp_path, monkeypatch
) -> None:
    """The verification side does not invent a pack path."""
    plans_root = tmp_path / "plans"
    (plans_root / PLAN_ID).mkdir(parents=True)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_root))

    brief = _verification_brief(PLAN_ID)
    assert referenced_pack_paths(brief) == [], (
        "the verification brief names a context pack that is not there"
    )


# ---------------------------------------------------------------------------
# The detectors are not vacuous
# ---------------------------------------------------------------------------


def test_reference_detector_finds_a_synthetic_reference() -> None:
    brief = (
        "环境事实见 /tmp/plans/p1/context/round-2.json，"
        "不要再运行 ls / find / git log 重新探索环境"
    )
    assert referenced_pack_paths(brief) == [
        "/tmp/plans/p1/context/round-2.json"
    ], "the reference detector missed a synthetic reference"


def test_inline_detector_flags_a_synthetic_brief() -> None:
    pack = {
        "branch": FIXTURE_BRANCH,
        "base_commit": "0" * 40,
        "diff_stat": "probe.txt | 1 +\n1 file changed, 1 insertion(+)",
    }
    brief = f"Subtask: x\nbranch: {FIXTURE_BRANCH}\n"
    assert inlined_env_fields(brief, pack) == ["branch"], (
        "the inline detector missed a synthetic violation"
    )
    assert inline_field_segments(brief) == ["branch"], (
        "the inline-segment detector missed a synthetic violation"
    )

    clean = "Subtask: x\n"
    assert inlined_env_fields(clean, pack) == []
    assert inline_field_segments(clean) == []


def test_the_pack_carries_the_gated_fields() -> None:
    """The gate's fields are the pack's fields — not a private list."""
    for field in GATED_FIELDS:
        assert field in CONTEXT_PACK_FIELDS, (
            f"{field!r} is gated but is not one of the pack's fields — "
            "the gate would be checking a fact the pack does not hold"
        )
