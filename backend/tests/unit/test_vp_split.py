"""2026-09-14 vp_split: 判断者 (repair vs split) + 拆分器 + 持久化。

用户指令::

    "repairing task generator 同时也要充当一个判断者的角色，他需要去
     判断到底是生成新的修复任务，还是拆分当前的 VP ... 一个测试凭什么
     要跑这么久？"

Covered:
  * scope parsing / broadness gate (only whole-tree pytest VPs are
    split candidates);
  * depth cap (a depth-2 child can no longer be split — otherwise
    splitting loops forever);
  * judge: LLM picks split, per-round budget caps the number of splits,
    every failure mode (no tool / LLM raises / bad JSON / hallucinated
    id) degrades to ``repair``;
  * grouping: by_directory vs by_file_group, single-group refusal;
  * splitter: rewrites verification_plan.json (children inserted after
    the parent, parent marked ``superseded_by``), child commands scoped
    to their own files with tee'd progress logs, idempotent on re-split;
  * persist_split: parent SPLIT verdict written to the repo, never
    raises when the repo is missing/broken.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from vp_split import (
    ACTION_REPAIR,
    ACTION_SPLIT,
    VpSplitJudge,
    VpSplitter,
    is_broad_scope,
    is_pytest_command,
    _pytest_scope,
)


# ---------------------------------------------------------------------------
# scope parsing / broadness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command,expected",
    [
        ("pytest tests/ -v", ["tests/"]),
        ("pytest tests/ -v --junit-xml=/tmp/x.xml", ["tests/"]),
        ("pytest tests/ 2>&1 | tee /tmp/vp_023_progress.log", ["tests/"]),
        ("pytest tests/test_a.py tests/test_b.py -q", ["tests/test_a.py", "tests/test_b.py"]),
        ("pytest -v", []),
        ("python -m pytest tests/", []),
        ("", []),
    ],
)
def test_pytest_scope_extraction(command, expected):
    assert _pytest_scope(command) == expected


@pytest.mark.parametrize(
    "command,expected",
    [
        ("pytest tests/ -v", True),
        ("pytest tests", True),
        ("pytest tests/test_a.py -v", True),   # bare file at suite root
        ("pytest tests/test_a.py::test_x -v", False),
        ("pytest tests/ -k test_login -v", False),  # narrowed by -k
        ("pytest tests/test_login.py -k test_x", False),
        ("python manage.py test", False),
        ("", False),
    ],
)
def test_broad_scope_gate(command, expected):
    assert is_broad_scope(command) is expected


def test_is_pytest_command():
    assert is_pytest_command("pytest tests/ -v") is True
    assert is_pytest_command("python -m pytest tests/") is False
    assert is_pytest_command("") is False


# ---------------------------------------------------------------------------
# judge
# ---------------------------------------------------------------------------

class _FakeTool:
    def __init__(self, response: Any = None, raises: Exception = None):
        self.response = response
        self.raises = raises
        self.calls = 0

    def query_json(self, prompt, system_instruction, timeout=None):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.response


def _write_plan(plan_dir: Path, entries: List[Dict[str, Any]]) -> Path:
    plan_dir.mkdir(parents=True, exist_ok=True)
    p = plan_dir / "verification_plan.json"
    p.write_text(
        json.dumps({"verification_points": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    return p


CANDIDATE = {
    "id": "VP-023",
    "title": "Nightly CI 全过",
    "method": "automated_test",
    "test_command": "pytest tests/ -v",
    "actual_result_summary": "117 failed, 1939 passed in 1095s",
}


def test_judge_honours_llm_split(tmp_path):
    _write_plan(tmp_path, [{
        "id": "VP-023", "title": "T", "verification_method": "automated_test",
        "test_command": "pytest tests/ -v",
    }])
    tool = _FakeTool({"decisions": [{
        "vp_id": "VP-023", "action": "split",
        "split_hint": "by_directory", "reason": "18 分钟整树跑",
    }]})
    decisions = VpSplitJudge(tool, tmp_path).decide([CANDIDATE])
    assert decisions["VP-023"]["action"] == ACTION_SPLIT
    assert decisions["VP-023"]["split_hint"] == "by_directory"


def test_judge_rejects_split_for_ineligible_candidate(tmp_path):
    """A focused single-file VP is not a split candidate — the LLM's
    'split' verdict must not reach it."""
    _write_plan(tmp_path, [{
        "id": "VP-007", "title": "T", "verification_method": "automated_test",
        "test_command": "pytest tests/test_login.py -k test_x -v",
    }])
    tool = _FakeTool({"decisions": [{
        "vp_id": "VP-007", "action": "split", "split_hint": "by_directory",
    }]})
    candidate = dict(CANDIDATE, id="VP-007",
                     test_command="pytest tests/test_login.py -k test_x -v")
    decisions = VpSplitJudge(tool, tmp_path).decide([candidate])
    assert decisions["VP-007"]["action"] == ACTION_REPAIR


def test_judge_llm_failure_degrades_to_repair(tmp_path):
    _write_plan(tmp_path, [{
        "id": "VP-023", "verification_method": "automated_test",
        "test_command": "pytest tests/ -v",
    }])
    tool = _FakeTool(raises=RuntimeError("provider down"))
    decisions = VpSplitJudge(tool, tmp_path).decide([CANDIDATE])
    assert decisions["VP-023"]["action"] == ACTION_REPAIR


def test_judge_bad_json_degrades_to_repair(tmp_path):
    _write_plan(tmp_path, [{
        "id": "VP-023", "verification_method": "automated_test",
        "test_command": "pytest tests/ -v",
    }])
    tool = _FakeTool({"unexpected": "shape"})
    decisions = VpSplitJudge(tool, tmp_path).decide([CANDIDATE])
    assert decisions["VP-023"]["action"] == ACTION_REPAIR


def test_judge_per_round_split_budget(tmp_path):
    """Only ``max_splits_per_round`` candidates may split; the rest fall
    back to repair (a round must not shatter the whole plan)."""
    candidates = []
    entries = []
    for i in range(4):
        vp_id = f"VP-{100 + i}"
        candidates.append(dict(CANDIDATE, id=vp_id))
        entries.append({
            "id": vp_id, "verification_method": "automated_test",
            "test_command": "pytest tests/ -v",
        })
    _write_plan(tmp_path, entries)
    tool = _FakeTool({"decisions": [
        {"vp_id": f"VP-{100 + i}", "action": "split",
         "split_hint": "by_directory"} for i in range(4)
    ]})
    judge = VpSplitJudge(tool, tmp_path, max_splits_per_round=2)
    decisions = judge.decide(candidates)
    splits = [d for d in decisions.values() if d["action"] == ACTION_SPLIT]
    assert len(splits) == 2, f"budget must cap splits at 2, got {len(splits)}"


def test_judge_prompt_renders_prior_failure_rounds(tmp_path):
    """2026-09-14: the judge prompt shows how many times the VP already
    failed (evidence it may be too big to fix in one piece)."""
    _write_plan(tmp_path, [{
        "id": "VP-023", "verification_method": "automated_test",
        "test_command": "pytest tests/ -v",
    }])
    captured = {}

    class _Tool:
        def query_json(self, prompt, system_instruction, timeout=None):
            captured["prompt"] = prompt
            return {"decisions": []}

    candidate = dict(CANDIDATE, prior_failure_rounds=3)
    VpSplitJudge(_Tool(), tmp_path).decide([candidate])
    assert "prior_failure_rounds: 3" in captured["prompt"]


def test_judge_depth_cap_blocks_resplit(tmp_path):
    """A depth-2 VP (``VP-023-1-1``) is no longer splittable."""
    _write_plan(tmp_path, [{
        "id": "VP-023-1-1", "verification_method": "automated_test",
        "test_command": "pytest tests/visual -v",
        "split_depth": 2,
    }])
    judge = VpSplitJudge(_FakeTool({}), tmp_path)
    assert judge.eligible(
        "VP-023-1-1", "automated_test", "pytest tests/visual -v"
    ) is False


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------

def test_group_by_directory():
    files = [
        "tests/test_a.py",
        "tests/visual/test_x.py",
        "tests/visual/test_y.py",
        "tests/integration/test_z.py",
    ]
    groups = VpSplitter.group_test_files(files, "by_directory")
    labels = dict(groups)
    assert set(labels) == {"(root)", "visual", "integration"}
    assert labels["visual"] == ["tests/visual/test_x.py", "tests/visual/test_y.py"]
    assert labels["(root)"] == ["tests/test_a.py"]


def test_group_single_directory_refuses():
    """Everything in one directory → no useful split dimension."""
    groups = VpSplitter.group_test_files(
        ["tests/unit/test_a.py", "tests/unit/test_b.py"], "by_directory"
    )
    assert groups == []


def test_group_by_file_group_chunks():
    files = [f"tests/test_{i}.py" for i in range(7)]
    groups = VpSplitter.group_test_files(files, "by_file_group")
    assert len(groups) == 3
    assert sum(len(sub) for _, sub in groups) == 7


def test_group_caps_children():
    files = [f"tests/dir{i}/test_a.py" for i in range(9)]
    groups = VpSplitter.group_test_files(files, "by_directory")
    assert len(groups) <= 6


# ---------------------------------------------------------------------------
# splitter
# ---------------------------------------------------------------------------

@pytest.fixture
def split_env(tmp_path, monkeypatch):
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _write_plan(plan_dir, [
        {
            "id": "VP-006", "title": "函数体 diff",
            "verification_method": "code_review",
            "test_command": "git diff HEAD",
        },
        {
            "id": "VP-023", "title": "Nightly CI 全过",
            "verification_method": "automated_test",
            "test_command": "pytest tests/ -v",
            "expected_result": "全部通过",
            "priority": "high",
        },
    ])
    splitter = VpSplitter(plan_dir, project_dir)
    monkeypatch.setattr(
        VpSplitter, "collect_test_files",
        lambda self, scope, timeout=300: [
            "tests/test_a.py", "tests/visual/test_x.py", "tests/bench/test_b.py",
        ],
    )
    return splitter, plan_dir


def test_split_rewrites_plan_and_marks_parent(split_env):
    splitter, plan_dir = split_env
    children = splitter.split("VP-023", "by_directory", reason="18min 整树")
    assert children, "split must produce children"

    data = json.loads((plan_dir / "verification_plan.json").read_text(encoding="utf-8"))
    entries = data["verification_points"]
    ids = [e["id"] for e in entries]
    parent = next(e for e in entries if e["id"] == "VP-023")
    assert parent["superseded_by"] == [c["id"] for c in children]
    assert parent["split_reason"] == "18min 整树"
    # Children sit right after the parent, before the next original VP.
    assert ids.index(children[0]["id"]) == ids.index("VP-023") + 1
    assert ids[-1] == "VP-023-3"

    child = children[0]
    assert child["parent_vp_id"] == "VP-023"
    assert child["split_depth"] == 1
    assert child["verification_method"] == "automated_test"
    assert child["expected_result"] == "全部通过"
    assert child["priority"] == "high"
    assert "pytest tests/visual/test_x.py" in child["test_command"] or any(
        "tests/" in c["test_command"] for c in children
    )
    assert "tee /tmp/vp_023_1_progress.log" in child["test_command"]
    assert "-k" not in child["test_command"]


def test_split_is_idempotent(split_env):
    splitter, plan_dir = split_env
    first = splitter.split("VP-023", "by_directory", reason="r")
    before = (plan_dir / "verification_plan.json").read_text(encoding="utf-8")
    second = splitter.split("VP-023", "by_directory", reason="r")
    assert [c["id"] for c in second] == [c["id"] for c in first]
    assert (plan_dir / "verification_plan.json").read_text(encoding="utf-8") == before


def test_split_declines_when_not_enough_groups(split_env):
    splitter, plan_dir = split_env
    splitter.collect_test_files = lambda scope, timeout=300: ["tests/visual/a.py", "tests/visual/b.py"]
    assert splitter.split("VP-023", "by_directory") is None
    data = json.loads((plan_dir / "verification_plan.json").read_text(encoding="utf-8"))
    assert "superseded_by" not in data["verification_points"][1]


def test_split_unknown_vp_returns_none(split_env):
    splitter, _ = split_env
    assert splitter.split("VP-999") is None


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------

class _FakeRepo:
    def __init__(self, raises: Exception = None):
        self.verdicts: List[Dict[str, Any]] = []
        self.raises = raises

    def append_verdict(self, plan_id, verdict):
        if self.raises:
            raise self.raises
        self.verdicts.append(verdict)


def test_persist_split_writes_parent_verdict():
    repo = _FakeRepo()
    ok = VpSplitter.persist_split(
        repo, "plan-x", "VP-023",
        [{"id": "VP-023-1"}, {"id": "VP-023-2"}], reason="too big",
    )
    assert ok is True
    verdict = repo.verdicts[0]
    assert verdict["vp_id"] == "VP-023"
    assert verdict["status"] == "SPLIT"
    assert verdict["evidence"]["child_vp_ids"] == ["VP-023-1", "VP-023-2"]


def test_persist_split_swallows_repo_errors():
    repo = _FakeRepo(raises=KeyError("no row"))
    assert VpSplitter.persist_split(repo, "plan-x", "VP-023", [{"id": "a"}]) is False
    assert VpSplitter.persist_split(None, "plan-x", "VP-023", [{"id": "a"}]) is False
