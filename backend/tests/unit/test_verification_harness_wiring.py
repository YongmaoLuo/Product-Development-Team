"""接线测试 —— 判定用的模块必须来自**被测树**，出处必须落盘（VP-049）。

背景
----
VP-049 要的是一件事：**这一轮判定用的代码，就是被测项目里的那份** ——
子 agent、执行器、方法表三者必须来自**同一轮**解析出来的模块，而且解析
来源要写进轮日志与持久化审计事件。

上一轮（repair-30）只新建了 ``backend/verification_harness_loader.py``
（``load_harness_modules`` / ``LoadedHarness`` / provenance）与它的 loader 级
测试；验收命令选中的两个测试文件都不碰接线，于是 38 条全绿而三条判据一条
没满足：

* 构造点仍走模块级导入（harness 进程自己 import 到的副本）；
* 方法表仍是写死的 ``from verification_subagent import SUPPORTED_METHODS``；
* 来源从未落盘。

本文件是**接线**的测试：它 import 真正的 ``verification_agent``，用
``VerificationAgent.__new__(VerificationAgent)`` 只摆出接线需要的那几个私有
属性，然后问四个问题：

1. 这一轮判定用的模块是不是从**被测树**解析出来的？
2. 子 agent 与执行器是不是**那一份**模块里的类？
3. 方法表是不是与子 agent / 执行器**同一份**？（不是另抄一份常量）
4. 解析来源是不是**落盘**了 —— 轮日志与 ``logs/audit_events.jsonl`` 各一份？

外加三条负控：回落时**晚绑定**到模块全局（既有
``monkeypatch.setattr(verification_agent, ...)`` 写法仍然有效）、被测树那份
**坏了**时回落并说清为什么、以及**每轮重新解析**（长驻进程不能拿着一份
陈旧的模块判定一整轮）。

测试命令::

    backend/.venv/bin/python -m pytest -q backend/tests/unit/test_verification_harness_wiring.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

# 真正的模块 —— 接线测试必须 import 真的那份，不能自己拼一个替身。
import verification_agent  # noqa: E402
from verification_agent import VerificationAgent  # noqa: E402
from verification_persistence import VerificationPersistenceManager  # noqa: E402


#: A project-side stand-in for ``verification_subagent``.  It is a valid
#: module with the names the framework uses, so the loader loads it
#: exactly as it would load the real one — and it **records** that it was
#: constructed, which is how the construction point is pinned.
_PROJECT_SUBAGENT_SOURCE = '''\
"""A project-side stand-in for ``verification_subagent``."""

SUPPORTED_METHODS = ("method_a", "method_b")
LEGACY_METHODS = ("legacy_a",)


class VerificationSubAgent:
    """Records every construction, so the wiring can be observed."""

    origin = "project"
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).instances.append(self)

    async def run(self, **kwargs):
        from verification_subagent import Verdict

        return Verdict(verdict="PASSED", reasons=["stand-in"], evidence=[])
'''

#: A project-side stand-in for ``verification_executor``.
_PROJECT_EXECUTOR_SOURCE = '''\
"""A project-side stand-in for ``verification_executor``."""


class VerificationExecutor:
    origin = "project"

    def __init__(self, **kwargs):
        self.kwargs = kwargs
'''

#: A project-side module that cannot even be parsed.
_BROKEN_SOURCE = "class VerificationSubAgent::\n"


def _framework_checkout(
    root: Path,
    subagent_source: str = _PROJECT_SUBAGENT_SOURCE,
    executor_source: str = _PROJECT_EXECUTOR_SOURCE,
) -> Path:
    """A project tree that *is* a checkout of this framework.

    ``backend/pyproject.toml`` is what says "this tree is the framework";
    the two modules beside it are what the loader resolves.
    """
    backend = root / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "pyproject.toml").write_text(
        "[project]\nname = \"stand-in\"\n", encoding="utf-8",
    )
    (backend / "verification_subagent.py").write_text(
        subagent_source, encoding="utf-8",
    )
    (backend / "verification_executor.py").write_text(
        executor_source, encoding="utf-8",
    )
    return root


def _plain_tree(root: Path) -> Path:
    """A project tree that is **not** a checkout of this framework."""
    backend = root / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    return root


def _agent(
    plan_dir: Path,
    project_dir: Path,
    round_number: int = 1,
) -> VerificationAgent:
    """A verification agent with only the wiring's attributes.

    ``VerificationAgent.__new__`` — the constructor starts services,
    rewrites ``PATH`` and builds a whole coding tool; none of that is
    what this spec is about.  The wiring needs four things and this
    gives it exactly those.
    """
    agent = VerificationAgent.__new__(VerificationAgent)
    agent.plan_dir = plan_dir
    agent.project_dir = project_dir
    agent._current_round = round_number
    agent.persistence = VerificationPersistenceManager(plan_dir)
    agent.persistence.start_round(round_number)
    agent.verification_plan_file = plan_dir / "verification_plan.json"
    agent.coding_tool = None
    agent.verif_repo = None
    return agent


def _audit_events(plan_dir: Path) -> list:
    """The plan's durable audit trail, one entry per line."""
    path = plan_dir / "logs" / "audit_events.jsonl"
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def _round_log_entries(plan_dir: Path) -> list:
    """Every entry of every round log of the plan."""
    entries = []
    logs_dir = plan_dir / "logs"
    if not logs_dir.is_dir():
        return entries
    for path in sorted(logs_dir.glob("verification_*.log")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _provenance_entries(entries: list, event_type: str) -> list:
    return [
        entry for entry in entries
        if entry.get("event_type") == event_type
    ]


# ---------------------------------------------------------------------------
# 1. 这一轮判定用的模块来自被测树
# ---------------------------------------------------------------------------


def test_round_resolves_the_harness_modules_from_the_tree_under_test(tmp_path):
    """The modules that judge the round come from the tree under test."""
    project_dir = _framework_checkout(tmp_path / "project")
    agent = _agent(tmp_path / "plan", project_dir)

    loaded = agent._harness_modules()

    provenance = loaded.provenance
    assert provenance["subagent"]["resolution"] == "project", (
        "the sub-agent module must come from the tree under test: "
        f"{provenance['subagent']}"
    )
    assert provenance["executor"]["resolution"] == "project", (
        "the executor module must come from the tree under test: "
        f"{provenance['executor']}"
    )
    assert provenance["subagent"]["module_path"] == str(
        project_dir / "backend" / "verification_subagent.py",
    )
    assert provenance["executor"]["module_path"] == str(
        project_dir / "backend" / "verification_executor.py",
    )


def test_the_resolution_is_reported_with_a_hash_and_a_head(tmp_path):
    """The provenance says **which file** judged — not just "it worked"."""
    project_dir = _framework_checkout(tmp_path / "project")
    agent = _agent(tmp_path / "plan", project_dir)

    provenance = agent._harness_modules().provenance["subagent"]

    assert provenance["sha256"], "the provenance must carry the file's hash"
    assert provenance["git_head"], "the provenance must carry the repo HEAD"
    assert "fallback_reason" in provenance


# ---------------------------------------------------------------------------
# 2. 子 agent 与执行器是那一份模块里的类
# ---------------------------------------------------------------------------


def test_the_subagent_class_is_the_resolved_module_s_class(tmp_path):
    """The class the framework constructs is the one in the resolved module."""
    project_dir = _framework_checkout(tmp_path / "project")
    agent = _agent(tmp_path / "plan", project_dir)

    loaded = agent._harness_modules()
    resolved = agent._verification_subagent_class()

    assert resolved is loaded.subagent_class
    assert resolved.origin == "project"
    assert resolved is not verification_agent.VerificationSubAgent


def test_the_executor_class_is_the_resolved_module_s_class(tmp_path):
    """Same for the executor."""
    project_dir = _framework_checkout(tmp_path / "project")
    agent = _agent(tmp_path / "plan", project_dir)

    loaded = agent._harness_modules()
    resolved = agent._verification_executor_class()

    assert resolved is loaded.executor_class
    assert resolved.origin == "project"
    assert resolved is not verification_agent.VerificationExecutor


# ---------------------------------------------------------------------------
# 3. 方法表与子 agent / 执行器同一份
# ---------------------------------------------------------------------------


def test_the_method_table_comes_from_the_same_module_as_the_subagent(tmp_path):
    """One copy answers "which methods exist" and "who judges"."""
    project_dir = _framework_checkout(tmp_path / "project")
    agent = _agent(tmp_path / "plan", project_dir)

    loaded = agent._harness_modules()

    assert agent._supported_methods() == loaded.supported_methods
    assert agent._legacy_methods() == loaded.legacy_methods
    # The tree under test's table, **not** the harness' own table.
    assert agent._supported_methods() == ("method_a", "method_b")
    assert agent._legacy_methods() == ("legacy_a",)


def test_the_method_table_actually_drives_the_retirement_guard(tmp_path):
    """The guard reads the round's table — not a hard-coded import.

    ``api_test`` is a method of the **harness'** table; it is not in the
    tree under test's table, so the round's own table must retire it.
    """
    project_dir = _framework_checkout(tmp_path / "project")
    agent = _agent(tmp_path / "plan", project_dir)

    plan = {
        "verification_points": [
            {"id": "VP-001", "verification_method": "api_test"},
            {"id": "VP-002", "verification_method": "legacy_a"},
        ],
    }

    normalized = agent._normalize_legacy_methods(plan)
    points = {vp["id"]: vp for vp in normalized["verification_points"]}

    assert points["VP-001"].get("obsolete") is True
    assert "method_a" in str(points["VP-001"].get("obsolete_reason"))
    assert points["VP-002"].get("obsolete") is True
    assert "retired" in str(points["VP-002"].get("obsolete_reason"))


# ---------------------------------------------------------------------------
# 4. 来源落盘
# ---------------------------------------------------------------------------


def test_the_round_start_records_the_provenance_in_the_audit_trail(tmp_path):
    """``logs/audit_events.jsonl`` carries one event per module."""
    project_dir = _framework_checkout(tmp_path / "project")
    plan_dir = tmp_path / "plan"
    agent = _agent(plan_dir, project_dir)

    agent.start_verification_round(1)

    entries = _provenance_entries(
        _audit_events(plan_dir), "harness_provenance_recorded",
    )
    assert len(entries) == 2, (
        "one audit event per module (sub-agent + executor): "
        f"{[e.get('data') for e in entries]}"
    )
    by_module = {
        Path(str(entry["data"]["module_path"])).name: entry["data"]
        for entry in entries
    }
    assert set(by_module) == {
        "verification_subagent.py", "verification_executor.py",
    }
    for name, data in by_module.items():
        assert data["resolution"] == "project", f"{name}: {data}"
        assert data["sha256"], f"{name}: the file's hash"
        assert data["git_head"], f"{name}: the repo HEAD"
        assert "fallback_reason" in data, f"{name}: the reason it fell back"


def test_the_round_start_records_the_provenance_in_the_round_log(tmp_path):
    """The same provenance reaches the round's own log."""
    project_dir = _framework_checkout(tmp_path / "project")
    plan_dir = tmp_path / "plan"
    agent = _agent(plan_dir, project_dir)

    agent.start_verification_round(1)

    entries = _provenance_entries(
        _round_log_entries(plan_dir), "harness_provenance_recorded",
    )
    assert len(entries) == 2, (
        "the round log carries the same provenance: "
        f"{[e.get('data') for e in entries]}"
    )
    resolutions = {entry["data"]["resolution"] for entry in entries}
    assert resolutions == {"project"}


# ---------------------------------------------------------------------------
# 5. 每轮重新解析
# ---------------------------------------------------------------------------


def test_a_new_round_resolves_the_modules_again(tmp_path):
    """A long-running process must not judge a round with a stale module."""
    project_dir = _framework_checkout(tmp_path / "project")
    plan_dir = tmp_path / "plan"
    agent = _agent(plan_dir, project_dir, round_number=1)

    first = agent._harness_modules().subagent_module
    agent.start_verification_round(2)
    second = agent._harness_modules().subagent_module

    assert first is not second, (
        "the modules must be re-resolved per round — a stale module "
        "judges the next round with the previous round's code"
    )


# ---------------------------------------------------------------------------
# 6. 负控：回落
# ---------------------------------------------------------------------------


def test_the_fallback_binds_to_the_module_global_late(tmp_path, monkeypatch):
    """A tree that is not a framework checkout falls back to the globals.

    Late binding is what keeps the framework's own injection point
    working: ``monkeypatch.setattr(verification_agent,
    "VerificationSubAgent", ...)`` must still swap the implementation.
    """
    class _FakeSubAgent:
        pass

    class _FakeExecutor:
        pass

    monkeypatch.setattr(
        verification_agent, "VerificationSubAgent", _FakeSubAgent,
    )
    monkeypatch.setattr(
        verification_agent, "VerificationExecutor", _FakeExecutor,
    )

    project_dir = _plain_tree(tmp_path / "project")
    agent = _agent(tmp_path / "plan", project_dir)

    assert agent._harness_modules().provenance["subagent"]["resolution"] == "harness"
    assert agent._verification_subagent_class() is _FakeSubAgent
    assert agent._verification_executor_class() is _FakeExecutor


def test_a_broken_module_falls_back_and_says_why(tmp_path):
    """A module that cannot even be parsed must not take the round down."""
    project_dir = _framework_checkout(
        tmp_path / "project", subagent_source=_BROKEN_SOURCE,
    )
    agent = _agent(tmp_path / "plan", project_dir)

    provenance = agent._harness_modules().provenance["subagent"]

    assert provenance["resolution"] == "harness"
    assert "load failed" in provenance["fallback_reason"], provenance
    # The executor is judged independently — one broken module must not
    # drag the other one down with it.
    assert agent._harness_modules().provenance["executor"]["resolution"] == "project"
    # And the round still runs: the class is the harness' own.
    assert agent._verification_subagent_class() is verification_agent.VerificationSubAgent


# ---------------------------------------------------------------------------
# 7. 构造点真的接上了
# ---------------------------------------------------------------------------


def test_the_subagent_is_constructed_from_the_resolved_module(tmp_path):
    """The construction point uses the resolved class, not the import."""
    project_dir = _framework_checkout(tmp_path / "project")
    agent = _agent(tmp_path / "plan", project_dir)

    asyncio.run(
        agent._delegate_to_sub_agent(
            {"id": "VP-001", "verification_method": "code_review"},
            "code_review",
            "VP-001",
        ),
    )

    resolved = agent._verification_subagent_class()
    assert len(resolved.instances) == 1, (
        "the resolved sub-agent must be the one that was constructed"
    )
    assert resolved.instances[0].kwargs["method"] == "code_review"


def test_the_executor_is_constructed_from_the_resolved_module(tmp_path):
    """Same for the executor."""
    project_dir = _framework_checkout(tmp_path / "project")
    agent = _agent(tmp_path / "plan", project_dir)

    executor = agent._build_verification_executor(
        {"verification_points": [{"id": "VP-001", "verification_method": "code_review"}]},
    )

    assert getattr(executor, "origin", None) == "project", (
        "the executor must be the tree under test's own class"
    )
    assert executor.kwargs["plan_id"] == agent.plan_dir.name
