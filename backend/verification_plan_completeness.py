"""全量关卡完整性护栏（2026-09-18, D5）。

这个模块解决什么
----------------
2026-09-16 的两阶段决定要求：项目里存在全量 CI 门禁和全量 E2E 套件时，
计划最后必须各有一条对应的 Phase 2 关卡。2026-09-18 的重构把 VP 的
``test_command`` 删掉之后，CI 门禁一度失去了表达方式，重新生成的计划里
那条 Nightly CI 关卡就凭空消失了 —— 而它正是 PRD 验收标准第 9 条。

D2/D4 补回了表达方式（``full_ci`` / ``e2e``）与提示词。这个模块补的是
**兜底**：提示词说了但没人核对的东西，迟早还会再漂一次（同一个仓库里
``verification_command_guard`` 的注释已经把这条教训写死过一次："问题不是
提示词没写，而是提示词说了但没人兜底"）。

判据：项目事实 → 计划里必须有的关卡
-----------------------------------
:func:`detect_full_gate_entries` 只看**文件系统**：

* **CI 总入口** —— ``scripts/ci_local.py`` / ``scripts/ci_prgate.py`` /
  ``scripts/*nightly*`` / ``.github/workflows/nightly.yml`` /
  ``.github/workflows/ci.yml``
* **全量 E2E 套件** —— ``playwright.config.*`` / ``cypress.config.*`` /
  ``tests/e2e/`` / ``e2e/``（项目根与一级子目录都看，that run's
  ``frontend-app/playwright.config.ts`` 就在一级子目录）

**刻意不看 PRD/测试设计的正文。** 从文本里认"这个项目有没有全量关卡"看着
更聪明，但 2026-09-16 已经用实测否决过这种推断：``re.compile("e2e")`` 命中
的是**路径** ``tests/e2e/tooltip.spec.ts``，于是 5 条只跑单个 spec 的有界 VP
被误提升成 Phase 2，Phase 2 从设计上的 2 条涨到 11 条，一条 5 分钟的 ruff
门禁被排到 3600s 的全量 E2E **之后**。文件系统是事实，正文是修辞 —— 只认
事实。

**缺口怎么办**：挑大梁的是生成回路（``VerificationAgent`` 拿到缺口清单后
带反馈重问一次 LLM，和"服务引用违规""api_test 断言不合规"走同一条回路）；
本模块只负责"发现"和"记录"。仍然缺失时把缺口写进计划顶层的
``phase2_gap``，操作员一眼能看到这一轮少了哪道关 —— 绝不让一份缺了最后一关
的计划静默出厂。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: 计划顶层记录缺口的字段名。
GAP_FIELD = "phase2_gap"

#: 探测时跳过的目录（依赖 / 虚拟环境 / VCS / 构建产物 / 并行 agent 的临时
#: worktree）。``.claude/worktrees`` 尤其重要 —— 那些是别的 agent 的项目
#: 副本，把里面的 playwright.config.ts 当成"本项目有 E2E 套件"是误报。
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "venv1", "venv2", ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    "target", ".next", ".claude", "worktrees", "coverage",
    # Runtime state the nightly runner and the server produce. Since
    # 2026-09-28 the nightly artifact is ``.pdt/nightly-results.json``,
    # i.e. inside ``.pdt/`` alongside ``state.db`` — so ``.pdt`` is the
    # directory that actually has to be skipped now. ``.pdt-nightly`` is
    # the pre-move name and is kept so a stale directory from an older
    # checkout still gets skipped; neither is project source.
    ".pdt", ".pdt-nightly",
})

#: CI 总入口的候选路径（相对项目根）。``*`` 段由 :func:`_glob_relative` 处理。
_CI_ENTRY_PATTERNS = (
    "scripts/ci_local.py",
    "scripts/ci_prgate.py",
    "scripts/*nightly*",
    ".github/workflows/nightly.yml",
    ".github/workflows/nightly.yaml",
    ".github/workflows/ci.yml",
    ".github/workflows/ci.yaml",
)

#: 全量 E2E 套件的候选路径。
_E2E_ENTRY_PATTERNS = (
    "playwright.config.ts",
    "playwright.config.js",
    "playwright.config.mjs",
    "playwright.config.cjs",
    "cypress.config.ts",
    "cypress.config.js",
    "tests/e2e",
    "e2e",
)


@dataclass
class FullGateEntry:
    """一个被探测到的全量关卡入口。"""

    kind: str      # "ci" | "e2e"
    label: str     # 给人看的名字
    evidence: str  # 证明它存在的路径（相对项目根）

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "label": self.label, "evidence": self.evidence}


@dataclass
class MissingGate:
    """一条"项目里有入口、计划里却没有对应关卡"的缺口。

    每种入口**最多一条**缺口：Phase 2 的规格上限就是两条关卡（order 1 的
    CI、order 2 的 E2E），一个仓库里有四个 CI 脚本也不该要求四条关卡。
    其余同类入口进 ``alternatives`` —— 它们是"同一个缺口的其它证据"，
    交给规划 LLM 挑一个写成 ``ci_entry``。
    """

    kind: str
    label: str
    evidence: str
    expected_method: str
    alternatives: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "evidence": self.evidence,
            "expected_method": self.expected_method,
            "alternatives": list(self.alternatives),
        }


def _glob_relative(root: Path, pattern: str) -> Optional[Path]:
    """Resolve one pattern against ``root``; return the first hit or None.

    Only the trailing segment may contain ``*`` — enough for
    ``scripts/*nightly*``, and keeps the search shallow on purpose.
    """
    if "*" not in pattern:
        candidate = root / pattern
        return candidate if candidate.exists() else None
    parent, _, name = pattern.rpartition("/")
    base = root / parent if parent else root
    if not base.is_dir():
        return None
    for hit in sorted(base.glob(name)):
        return hit
    return None


def _scan_roots(project_dir: Path) -> List[Path]:
    """项目根 + 一级子目录（跳过依赖 / VCS / worktree 等）。"""
    roots = [project_dir]
    try:
        children = sorted(project_dir.iterdir())
    except OSError:
        return roots
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in _SKIP_DIRS:
            continue
        roots.append(child)
    return roots


def detect_full_gate_entries(project_dir: Any) -> List[FullGateEntry]:
    """Return every full-gate entry point the project actually has.

    Deterministic and cheap: a handful of stat calls over the project root
    and its first-level subdirectories. Returns ``[]`` for a missing /
    unreadable project dir (never raises) — "we could not look" must not
    be reported as "there is nothing there", so callers that care should
    check the directory themselves.
    """
    if not project_dir:
        return []
    root = Path(project_dir)
    if not root.is_dir():
        return []

    found: List[FullGateEntry] = []
    seen: set = set()
    for scan_root in _scan_roots(root):
        prefix = "" if scan_root == root else f"{scan_root.name}/"
        for kind, patterns, label in (
            ("ci", _CI_ENTRY_PATTERNS, "全量 CI 门禁"),
            ("e2e", _E2E_ENTRY_PATTERNS, "全量 E2E 套件"),
        ):
            for pattern in patterns:
                hit = _glob_relative(scan_root, pattern)
                if hit is None:
                    continue
                rel = f"{prefix}{pattern}"
                if (kind, rel) in seen:
                    continue
                seen.add((kind, rel))
                found.append(FullGateEntry(kind=kind, label=label, evidence=rel))
    return found


#: 浏览器驱动的验证种类 —— Phase 2 上任何一条都算"全量 E2E 门禁"。
#: ``e2e`` 是它自己的名字，``ui_validation`` 是同一个执行路径的老名字
#: （见 ``verification_subagent._compute_verdict``），两者都真开浏览器、
#: 都由子 agent 驱动。护栏查的是**覆盖**（这条入口有没有人把关），不是
#: **命名**（那条 VP 叫什么）—— 因为名字不同就把一份本来就有 E2E 门禁的
#: 计划打回重生成，只会换来一条重复的关卡。
_BROWSER_METHODS = ("e2e", "ui_validation")


def _satisfies(entry: FullGateEntry, vp: Dict[str, Any]) -> bool:
    """Does ``vp`` cover ``entry``?

    ``ci`` needs a ``full_ci`` gate, strictly — that method is the only
    one whose判定依据 is the repo entry's exit code, and a Phase-2
    ``api_test`` hitting one HTTP endpoint is not a整仓门禁.

    ``e2e`` accepts either browser-driven method. Every Phase-2 VP is a
    full gate by construction (Phase 2 *is* the full-gate phase), so a
    browser-driven VP sitting there is the E2E gate however it is named.
    What it must NOT be is a non-browser method: a Phase-2 ``api_test``
    or ``code_review`` is not an end-to-end run of the user flow, and
    letting one stand in is exactly how a missing gate hides.
    """
    method = str(vp.get("verification_method") or "").strip()
    if entry.kind == "ci":
        return method == "full_ci"
    return method in _BROWSER_METHODS


def find_missing_gates(
    plan_data: Any, entries: List[FullGateEntry],
) -> List[MissingGate]:
    """Which detected entries have no matching Phase-2 gate in the plan.

    Only Phase-2 VPs count. A ``full_ci`` sitting in Phase 1 is *not* a
    satisfied gate — the phase normalizer promotes it, and by the time
    this runs the plan has been normalized, so a Phase-1 ``full_ci``
    here means the normalizer did not run.

    **At most one gap per kind.** Phase 2 holds at most two gates (order
    1 = CI, order 2 = E2E), so a repository with four CI scripts owes one
    CI gate, not four. The remaining detected paths travel along as
    ``alternatives`` so the planner can pick whichever entry it can
    actually run; only the first is quoted as the headline evidence.
    """
    from verification_phases import is_final_gate

    if not isinstance(plan_data, dict) or not entries:
        return []
    points = plan_data.get("verification_points")
    if not isinstance(points, list):
        return []
    gates = [
        vp for vp in points
        if isinstance(vp, dict) and is_final_gate(vp)
    ]

    by_kind: Dict[str, List[FullGateEntry]] = {}
    for entry in entries:
        by_kind.setdefault(entry.kind, []).append(entry)

    missing: List[MissingGate] = []
    for kind in sorted(by_kind):
        kind_entries = by_kind[kind]
        if any(_satisfies(entry, vp) for entry in kind_entries for vp in gates):
            continue
        head = kind_entries[0]
        missing.append(MissingGate(
            kind=kind,
            label=head.label,
            evidence=head.evidence,
            expected_method="full_ci" if kind == "ci" else "e2e",
            alternatives=[e.evidence for e in kind_entries[1:]],
        ))
    return missing


def render_gap_feedback(missing: List[MissingGate]) -> str:
    """The section appended to the retry prompt (same shape as the other guards)."""
    lines = ["### Phase 2 全量关卡缺失"]
    for gap in missing:
        alternatives = ""
        if gap.alternatives:
            alternatives = (
                "（同类的其它入口，可以换成其中任意一个："
                + "、".join(f"`{a}`" for a in gap.alternatives[:6])
                + "）"
            )
        if gap.kind == "ci":
            lines.append(
                f"  - 项目里有 {gap.label}入口 `{gap.evidence}`{alternatives}，"
                f"但计划里没有对应的 Phase 2 关卡。补一条 "
                f"`verification_phase: 2` / `phase_order: 1` / "
                f"`verification_method: \"full_ci\"` 的 VP，`ci_entry` 写这个"
                f"入口在当前项目根目录下可直接执行的命令。"
            )
        else:
            lines.append(
                f"  - 项目里有 {gap.label} `{gap.evidence}`{alternatives}，"
                f"但计划里没有对应的 Phase 2 关卡。补一条 "
                f"`verification_phase: 2` / `phase_order: 2` / "
                f"`verification_method: \"e2e\"` 的 VP，`target_url` 用服务"
                f"占位符写。"
            )
    lines.append(
        "  （这些是 PRD/测试设计里点名的整仓验收标准，不能省；"
        "每种入口只需要**一条**关卡。其余 VP 保持不变。）"
    )
    return "\n".join(lines)


def annotate_plan(
    plan_data: Any, missing: List[MissingGate],
) -> List[Dict[str, Any]]:
    """Write the gap onto the plan (in place) and return it as findings.

    Called after the regeneration loop has run out of attempts: the plan
    is saved anyway (a runnable plan beats no plan), but it carries
    ``phase2_gap`` so the operator can see the round is missing its final
    gate rather than discovering it from a passing report.
    """
    if not isinstance(plan_data, dict):
        return []
    findings = [gap.to_dict() for gap in missing]
    if findings:
        plan_data[GAP_FIELD] = {"missing": findings}
    else:
        plan_data.pop(GAP_FIELD, None)
    return findings
