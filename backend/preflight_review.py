"""
Pre-flight cross-document consistency reviewer (DP2).

Runs **before** ``tasks.json`` generation. Calls the LLM with the
PRD / arch / test design docs and asks it to flag alignment gaps in
three dimensions:

  * 维度 1 — PRD ``acceptance`` → arch components
  * 维度 2 — arch modules       → test strategies
  * 维度 3 — test scenarios     → task placeholders

Returns a ``PreFlightReport`` dict the task generator can use to
decide whether to proceed. The report shape is::

    {
      "findings": [
        {
          "source_doc": "prd" | "arch" | "test",
          "source_id": str,
          "target_doc": "prd" | "arch" | "test" | "task",
          "target_id": str | None,
          "severity": "high" | "medium" | "low",
          "finding": str,
          "suggested_fix": str
        }
      ],
      "high_count": int,
      "report_path": str,
      "passed": bool   # back-compat — ``high_count == 0``
    }

Boundary conditions pinned by the spec:

  * ``prd.json`` missing → ``run`` raises ``FileNotFoundError``
    (hard error — task generation MUST stop).
  * ``plan_state.flags.arch_enabled == False`` → dimensions 1 and 2
    are skipped; only dimension 3 runs (if ``test_enabled``).
  * ``plan_state.flags.test_enabled == False`` → dimensions 2 and 3
    are skipped; only dimension 1 runs (if ``arch_enabled``).
  * Both flags disabled → entire preflight skipped; the report
    contains empty findings with ``high_count == 0``.
  * LLM failure → ``findings == []`` and ``high_count == 0``
    (degraded but non-blocking — task generation must NOT be
    blocked by a flaky reviewer).

The persisted artifact is ``plans/<plan_id>/preflight_report.json``.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coding_tool import CodingTool
from plan_state import MIGRATION_AUDIT_SCHEMA as _MIGRATION_AUDIT_SCHEMA
from plan_state import migration_audit as _migration_audit


# Re-export the shared schema constant under the local name so
# tests/integration can assert ``preflight_review.MIGRATION_AUDIT_SCHEMA``
# IS the same object as ``plan_state.MIGRATION_AUDIT_SCHEMA`` (DP7
# part (b) — single source of truth across the three scope files).
MIGRATION_AUDIT_SCHEMA = _MIGRATION_AUDIT_SCHEMA


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------











# Canonical filename for the per-plan state JSON (see plan_state.py).
# A single literal — no string-concat bypass. This is the contract
# pinned by backend/tests/test_plan_state_filename_constant.py.
PLAN_STATE_FILENAME: str = "plan_state.json"

_SYSTEM_PROMPT = """你是一位资深软件架构评审专家，负责审查 PRD / 架构设计 / 测试设计 三份文档之间的一致性。

重要：直接输出 JSON 内容，不要输出任何对话、解释、确认或提问。

任务：审查三份文档，识别下列三种一致性问题并以 JSON findings 数组输出：

1. **维度 1 — PRD 验收项 (acceptance) → 架构组件 (arch)**
   - 扫描 PRD acceptance 数组中每一条 AC-xxx
   - 在 arch 文档 (Markdown 文本) 中检查是否存在对应架构组件 / 模块 / 决策点
   - 如果某 某条验收标准在 arch 中完全无对应 → severity="high"

2. **维度 2 — 架构模块 (arch) → 测试策略 (test)**
   - 扫描 arch 文档中每个模块 / 决策点
   - 在 test-design 文档 (Markdown 文本) 中检查是否存在对应测试场景 / 决策点
   - 如果某 arch 模块在 test 中完全无对应 → severity="high"

3. **维度 3 — 测试场景 (test) → 任务占位 (tasks.json，如果存在)**
   - 扫描 test-design 中每个测试决策点
   - 如果 plans/<plan_id>/tasks.json 已存在，检查每个 test 决策点是否被 tasks 覆盖
   - 如果 tasks.json 不存在 → 跳过此维度，不输出任何 finding

输出格式（仅 JSON，无 markdown 代码块）：

{
  "findings": [
    {
      "source_doc": "prd" | "arch" | "test",
      "source_id": "<AC-xxx 或 决策点标题>",
      "target_doc": "arch" | "test" | "task",
      "target_id": "<目标文档中的标识符（如果有）；否则 null>",
      "severity": "high" | "medium" | "low",
      "finding": "<中文描述，1-2 句话>",
      "suggested_fix": "<具体可执行的修复建议，1-2 句话>"
    }
  ]
}

判断规则：

- 完全无对应 → "high"
- 有部分对应但覆盖不完整 → "medium"
- 命名不一致但实质上对应 → "low"（可选，可直接省略）
- 没有发现问题时输出 {"findings": []}

只输出你找到的实际问题，不要编造。**只检查 prompt 中明确列出的维度，未列出的维度不要输出任何 finding。**"""


_MAX_DOC_CHARS = 12_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = _MAX_DOC_CHARS) -> str:
    """Truncate long document bodies before sending to the LLM."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[... 文档过长已截断 ...]"


def _read_text(path: Path) -> str:
    """Best-effort UTF-8 read; returns "" on any error."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_json(path: Path) -> Optional[dict]:
    """Best-effort UTF-8 JSON read; returns None on any error."""
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# PreFlightReviewer
# ---------------------------------------------------------------------------


class PreFlightReviewer:
    """Cross-document pre-flight consistency reviewer.

    Parameters
    ----------
    coding_tool
        A ``CodingTool`` instance (see ``backend/coding_tool.py``).
        ``query_json`` is invoked exactly once per ``run()`` call
        (or zero times if both phases are disabled / PRD is missing).
    plans_root
        Root directory containing ``<plans_root>/<plan_id>/...``.
        Defaults to ``Path("plans")`` relative to the working
        directory.
    """

    def __init__(
        self,
        coding_tool: CodingTool,
        plans_root: Optional[Path] = None,
    ):
        self.coding_tool = coding_tool
        self.plans_root = Path(plans_root) if plans_root is not None else Path("plans")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, plan_id: str) -> Dict[str, Any]:
        """Run the alignment check and return the report dict.

        Raises ``FileNotFoundError`` if ``prd.json`` is missing —
        this is a hard error per the spec (task generation MUST
        stop). LLM failures are swallowed and degrade to an empty
        report so the downstream pipeline is never blocked by a
        flaky reviewer.
        """
        plan_dir = self.plans_root / plan_id
        report_path_str = str(plan_dir / "preflight_report.json")

        # ---- Hard error: PRD missing ----
        prd_path = plan_dir / "prd.json"
        if not prd_path.exists():
            raise FileNotFoundError(
                f"PRD not found for plan '{plan_id}': {prd_path}"
            )
        prd = _read_json(prd_path)
        if prd is None:
            # File exists but unreadable as JSON — also a hard error.
            raise FileNotFoundError(
                f"PRD unreadable or invalid JSON for plan '{plan_id}': {prd_path}"
            )

        # ---- Resolve phase flags ----
        arch_enabled, test_enabled = self._resolve_flags(plan_dir)

        # ---- Both disabled → skip entire preflight ----
        if not arch_enabled and not test_enabled:
            report: Dict[str, Any] = {
                "findings": [],
                "high_count": 0,
                "report_path": report_path_str,
                "passed": True,
                "skipped": True,
                "skip_reason": "arch_and_test_both_disabled",
            }
            self._persist(plan_id, report)
            return report

        # ---- Load only the docs the enabled dimensions need ----
        arch_text = _read_text(plan_dir / "arch-design.md") if arch_enabled else ""
        test_text = _read_text(plan_dir / "test-design.md") if test_enabled else ""

        # ---- Determine which dimensions to request ----
        dims: List[int] = []
        if arch_enabled:
            dims.append(1)  # PRD → arch
        if arch_enabled and test_enabled:
            dims.append(2)  # arch → test
        if test_enabled:
            dims.append(3)  # test → task

        prompt = self._build_alignment_prompt(prd, arch_text, test_text, dims)

        # ---- Call LLM (degrade gracefully on failure) ----
        try:
            llm_output = self.coding_tool.query_json(
                prompt=prompt,
                system_instruction=_SYSTEM_PROMPT,
            )
        except Exception:
            report = {
                "findings": [],
                "high_count": 0,
                "report_path": report_path_str,
                "passed": True,
                "degraded": True,
                "degrade_reason": "llm_call_failed",
            }
            self._persist(plan_id, report)
            return report

        # ---- Parse + persist ----
        report = self._parse_report(llm_output)
        report["report_path"] = report_path_str
        self._persist(plan_id, report)
        return report

    # ------------------------------------------------------------------
    # Internal — flag resolution
    # ------------------------------------------------------------------

    def _resolve_flags(self, plan_dir: Path) -> Tuple[bool, bool]:
        """Decide which dimensions to run.

        Resolution order (per flag):

          1. ``plan-state sidecar`` → ``flags.arch_enabled`` /
             ``flags.test_enabled`` if explicitly set.
          2. Fall back to file existence (``arch-design.md`` /
             ``test-design.md``) when the flag is absent.

        This dual-source strategy keeps the reviewer robust to:

          * **Old call sites / tests** that never write
            ``plan-state sidecar`` — the reviewer still picks up the
            arch / test docs they wrote.
          * **New plan-state-driven workflows** that explicitly
            disable a phase via flags even when the file exists
            on disk.
        """
        arch_file_exists = (plan_dir / "arch-design.md").exists()
        test_file_exists = (plan_dir / "test-design.md").exists()

        state = _read_json(plan_dir / PLAN_STATE_FILENAME)
        if not isinstance(state, dict):
            # No plan-state sidecar (or corrupt) — fall back to file
            # existence so old callers keep working.
            return arch_file_exists, test_file_exists

        flags = state.get("flags", {})
        if not isinstance(flags, dict):
            flags = {}

        arch_enabled = flags.get("arch_enabled", arch_file_exists)
        test_enabled = flags.get("test_enabled", test_file_exists)
        return bool(arch_enabled), bool(test_enabled)

    # ------------------------------------------------------------------
    # Internal — prompt construction
    # ------------------------------------------------------------------

    def _build_alignment_prompt(
        self,
        prd: Optional[dict],
        arch: str,
        test: str,
        dims: List[int],
    ) -> str:
        """Build the LLM prompt that asks for the alignment report.

        Each requested dimension is rendered with an explicit
        ``## 维度 N`` header so callers can verify which dimensions
        were actually included (e.g. ``test_preflight_skip_when_arch_disabled``
        greps the prompt for ``## 维度 1`` / ``## 维度 2`` / ``## 维度 3``).
        """
        prd_block = json.dumps(
            {
                "title": prd.get("title", "") if isinstance(prd, dict) else "",
                "acceptance": prd.get("acceptance", []) if isinstance(prd, dict) else [],
                "decision_points": [
                    dp.get("title", "") if isinstance(dp, dict) else ""
                    for dp in (prd.get("decision_points", []) if isinstance(prd, dict) else [])
                ],
            },
            ensure_ascii=False,
            indent=2,
        ) if isinstance(prd, dict) else "(no PRD available)"

        arch_block = _truncate(arch) if arch else "(no arch design available)"
        test_block = _truncate(test) if test else "(no test design available)"

        dim_sections: List[str] = []
        if 1 in dims:
            dim_sections.append(
                "## 维度 1 — PRD 验收项 → 架构组件\n"
                "检查 PRD 的 acceptance 数组中每一条 AC-xxx 是否在 arch 文档中"
                "有对应组件 / 模块 / 决策点。无对应 → severity=\"high\"。"
            )
        if 2 in dims:
            dim_sections.append(
                "## 维度 2 — 架构模块 → 测试策略\n"
                "检查 arch 文档中每个模块 / 决策点是否在 test 文档中有对应"
                "测试场景。无对应 → severity=\"high\"。"
            )
        if 3 in dims:
            dim_sections.append(
                "## 维度 3 — 测试场景 → 任务占位\n"
                "检查 test 文档中每个测试决策点是否被 tasks 覆盖。"
                "若 tasks.json 不存在则跳过此维度。"
            )

        dims_block = "\n\n".join(dim_sections) if dim_sections else (
            "(no dimensions requested — return {\"findings\": []})"
        )

        return (
            "# Pre-flight Consistency Check\n\n"
            "请按系统提示中的 JSON 格式输出 findings。**只检查下列明确列出的维度，"
            "未列出的维度不要输出任何 finding。**\n\n"
            "## 文档 1. PRD（产品需求文档）\n\n"
            "```json\n" + prd_block + "\n```\n\n"
            "## 文档 2. Architecture Design（架构设计）\n\n"
            "```\n" + arch_block + "\n```\n\n"
            "## 文档 3. Test Design（测试设计）\n\n"
            "```\n" + test_block + "\n```\n\n"
            "# 需要检查的维度\n\n"
            + dims_block + "\n\n"
            "请逐项检查上述维度，输出找到的所有一致性问题（findings 数组）。"
        )

    # ------------------------------------------------------------------
    # Internal — LLM output parsing
    # ------------------------------------------------------------------

    def _parse_report(self, llm_output: Any) -> Dict[str, Any]:
        """Translate the LLM JSON output into the canonical report shape.

        Tolerates:

          * non-dict LLM output → empty findings
          * missing findings key → empty findings
          * items missing required keys → skipped
          * invalid severity values → coerced to "low"
        """
        empty: Dict[str, Any] = {
            "findings": [],
            "high_count": 0,
            "passed": True,
        }
        if not isinstance(llm_output, dict):
            return dict(empty)

        raw_findings = llm_output.get("findings", [])
        if not isinstance(raw_findings, list):
            return dict(empty)

        findings: List[Dict[str, Any]] = []
        for item in raw_findings:
            if not isinstance(item, dict):
                continue

            severity = item.get("severity", "low")
            if severity not in ("high", "medium", "low"):
                severity = "low"

            findings.append({
                "source_doc": item.get("source_doc", ""),
                "source_id": str(item.get("source_id", "")),
                "target_doc": item.get("target_doc", ""),
                "target_id": item.get("target_id"),
                "severity": severity,
                "finding": str(item.get("finding", "")),
                "suggested_fix": str(item.get("suggested_fix", "")),
            })

        high_count = sum(1 for f in findings if f["severity"] == "high")
        return {
            "findings": findings,
            "high_count": high_count,
            "passed": high_count == 0,
        }

    # ------------------------------------------------------------------
    # Internal — persistence
    # ------------------------------------------------------------------

    def _persist(self, plan_id: str, report: Dict[str, Any]) -> None:
        """Write ``plans/<plan_id>/preflight_report.json``.

        Failure to write the artifact is intentionally swallowed: the
        caller already has the in-memory report, and a transient disk
        error must not block tasks generation.
        """
        plan_dir = self.plans_root / plan_id
        try:
            plan_dir.mkdir(parents=True, exist_ok=True)
            with open(plan_dir / "preflight_report.json", "w", encoding="utf-8") as fp:
                json.dump(report, fp, ensure_ascii=False, indent=2)
        except OSError:
            pass
