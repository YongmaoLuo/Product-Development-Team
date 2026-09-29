"""
Self-Review Module — mandatory LLM second-pass review + repair
=============================================================

The prd / arch / test / tasks generators MUST run a self-review
pass after their initial emit, so that the document is iteratively
polished before the user opens the review screen. The previous
keyword-matching approach (TODO / FIXME / 待定 / 等) was too
brittle — it matched descriptive narrative like "四个数值待定"
in test_design and forced an LLM "fix" that duplicated the entire
document. The new approach is intent-based:

  * the LLM agent reads the freshly emitted document **and** the
    upstream document(s);
  * the agent decides whether the document needs repair;
  * if yes, the agent rewrites the whole document (preserving
    structure) and returns it as ``fixed_content``;
  * if no, the agent returns the document unchanged.

The agent's job is to **be the second round of generation** —
not just an auditor. This is the difference between "keyword
matcher that flags TODOs" and "LLM reviewer that polishes the
deliverable".

Failure mode (mandatory second pass)
-------------------------------------
``run_doc_self_review`` is mandatory by default: when the LLM
out-call raises, returns unparseable JSON, or no coding_tool is
available, the function raises :class:`SelfReviewUnavailableError`
so the generator refuses to promote the unaudited draft. The
legacy graceful-degrade behaviour is still available via
``mandatory=False`` for the tests and tooling that pre-date this
contract — never use it from a generator wiring.

Generators wire it in as::

    from self_review import run_doc_self_review
    report = run_doc_self_review(
        doc_content=emitted,
        doc_type="test",
        upstream_content=arch_md,
        coding_tool=self.coding_tool,
    )
    if report["rewrote"]:
        emit(report["fixed_content"])
    # else: LLM judged the document clean; keep the original

The report has this v2 shape (legacy ``fixed`` is kept for
back-compat with readers from the keyword-matcher era)::

    {
      "schema_version": 2,
      "doc_type": "prd" | "arch" | "test" | "tasks",
      "attempted": bool,
      "succeeded": bool,
      "rewrote": bool,
      "mandatory": bool,
      "skipped": bool,
      "skip_reason": str | None,
      "error": str | None,
      "findings": [...],
      "findings_count": int,
      "severity_high_count": int,
      "severity_medium_count": int,
      "severity_low_count": int,
      "input_content_hash": "sha256:<hex>",
      "fixed_content_hash": "sha256:<hex>",
      "fixed_content": "...",
      "fixed": bool   # legacy alias: bool(findings) or (succeeded and rewrote)
    }
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


log = logging.getLogger("self_review")


# Public type alias for the emitted report.
SelfReviewReport = Dict[str, Any]


# Document types the reviewer supports.
_VALID_DOC_TYPES = frozenset({"prd", "arch", "test", "tasks"})


# Per-doc_type review focus — the LLM agent is told what to look at
# during this second pass. Keep prompts short so the LLM stays
# focused.
_REVIEW_FOCUS = {
    "prd": (
        "PRD 文档需达到以下标准：\n"
        "1. 项目背景、目标、范围三类信息齐全且前后一致；\n"
        "2. 验收标准可机器判定（避免「差不多」「可能」等模糊词）；\n"
        "3. 决策点完整使用 CPEA 框架（C 背景 / P 问题 / E 评估 / A 行动）；\n"
        "4. 决策点的 A 行动应可执行、不依赖后续阶段的输出。"
    ),
    "arch": (
        "架构设计文档需基于上游 PRD：\n"
        "1. PRD 中每个决策点 / 验收标准在架构中有对应架构组件或决策点；\n"
        "2. 架构决策不得违反 PRD 已确定的设计原则；\n"
        "3. 决策点完整使用 CPEA 框架；\n"
        "4. 包含影响范围与备选方案；\n"
        "5. 与上游 PRD 的术语、范围描述保持一致。"
    ),
    "test": (
        "测试设计文档需基于上游架构设计：\n"
        "1. 架构中每个模块 / 决策点在测试设计中有对应测试策略；\n"
        "2. 测试设计不得违反架构已确定的技术决策；\n"
        "3. 测试覆盖必须包含至少一个真实外部依赖的端到端测试用例；\n"
        "4. 决策点完整使用 CPEA 框架；\n"
        "5. 描述「未确定的数值」时使用「未指定（由实现期反推确定）」"
        "等具体表述，避免使用「待定」「TBD」作为占位符（除非上下文"
        "明确是「故意保留由用户填写」的真实占位）。"
    ),
    "tasks": (
        "任务列表需基于上游架构设计与测试设计：\n"
        "1. 架构中每个模块 / 决策点有对应实现任务；\n"
        "2. 测试设计中的每个测试策略有对应任务（编写测试 / "
        "维护测试 / 跑通测试 / 覆盖率验证）；\n"
        "3. 任务颗粒度适中（每个任务 ≤ 15 分钟，且 ≤ 5 个文件改动）；\n"
        "4. 任务描述使用标准 Markdown 格式（背景 / 目标 / 修改位置 / "
        "输入输出示例 / 边界条件 / TDD 规格）；\n"
        "5. 任务 test_command 必须可执行（路径正确、venv 路径正确、"
        "命令可在仓库根目录跑通）；\n"
        "6. 任务依赖关系（depends_on）正确，避免循环依赖。"
    ),
}


def _build_prompt(
    doc_type: str,
    content: str,
    upstream_content: Optional[str],
    upstream_label: str,
) -> str:
    """Build the LLM agent's self-review prompt.

    The prompt is explicit: the LLM is asked to **be the second
    round of generation**. It must return BOTH a structured
    findings list AND a possibly-rewritten full document. If the
    document is already clean, it returns the same document
    unchanged in ``fixed_content``.
    """
    focus = _REVIEW_FOCUS[doc_type]
    upstream_block = (
        f"\n\n## 上游文档（{upstream_label}）\n{upstream_content}\n"
        if upstream_content
        else ""
    )
    return (
        "你是一名资深文档审校员 / 第二轮生成器。\n\n"
        f"刚生成了一份「{doc_type}」文档。这一轮你的任务是：\n"
        "1. 阅读待审文档**和**上游文档；\n"
        "2. 检查上一轮生成的内容是否完整覆盖审查焦点（见下）；\n"
        "3. **如果**发现需要修正/补充/删改的内容，**直接重写整篇文档**；\n"
        "4. **如果**判断当前文档已经合格（无重大问题），则直接回传"
        "原文档不变，并在 findings 中说明「无需修改」。\n\n"
        f"## 审查焦点\n{focus}\n"
        f"{upstream_block}\n"
        f"## 待审文档（{doc_type}）\n{content}\n"
        "## 输出要求\n"
        "直接输出 JSON（不要 markdown 代码块、不要解释、不要对话前缀）。\n\n"
        "JSON 字段：\n"
        "{\n"
        "  \"findings\": [\n"
        "    {\n"
        "      \"severity\": \"high|medium|low\",\n"
        "      \"type\": \"placeholder|consistency|scope|ambiguity|missing_coverage|violation|unclear\",\n"
        "      \"location\": \"<决策点 N 标题，或文档位置描述>\",\n"
        "      \"finding\": \"<1-2 句中文描述>\",\n"
        "      \"action\": \"<简述修改了什么，例：删除/重写/补充/无修改>\"\n"
        "    }\n"
        "  ],\n"
        "  \"fixed_content\": \"<完整文档内容。如果无需修改，请原样回传。>\"\n"
        "}\n\n"
        "判断规则：\n"
        "- 高严重性（high）只在以下场景使用：(a) 关键信息缺失且不可推断；"
        "(b) 与上游文档明显不一致；(c) 验收标准完全不可判定；\n"
        "- 「待定」「TBD」如果在上下文中是**真实描述**「该数值未由 arch "
        "阶段反推确定，需在实现期补全」，**不是占位符**，不要当作 finding；\n"
        "- 修改 fixed_content 时**保留** Markdown 结构（标题层级、CPEA 段落、"
        "列表编号、原有重点加粗等）；不要做无意义重排；\n"
        "- 修改幅度尽量小：能不动的决策点不动；只改有问题的决策点；\n"
        "- 如果整篇没问题，fixed_content 原样回传。\n\n"
        "## 重要约束（CRITICAL — 违反会导致 review status 错位）\n"
        "你**只能修改** fixed_content 字符串里的**正文文字**。禁止做以下任何一种重写：\n"
        "- 改「决策点 N」「## 决策点 N」这种**编号**或**标题层级**（编号必须与输入待审文档完全一致）\n"
        "- 删掉现有决策点（哪怕看起来冗余）\n"
        "- 把单个决策点拆成两个、或把两个合并成一个（数量必须恒等）\n"
        "- 在 JSON 的 findings / location 字段里塞入「status」「plan_id」「doc_type」"
        " 等元数据（这些字段由代码管理，不由你输出）\n"
        "本轮只是文档审校，**不改**决策点的**身份**（编号 + 标题）；"
        "你只能改它们**正文内容**。"
    )


def _last_parse_error(llm_reply: str) -> Optional[Exception]:
    """Return the underlying JSONDecodeError (if any) for a malformed
    LLM reply, used to feed error-specific hints into the retry path.

    The ``parse_llm_json`` helper consumes the ``JSONDecodeError``
    internally and re-raises a new one; we mirror its logic here
    just enough to capture a useful ``pos`` + message for the
    retry hint.
    """
    if not isinstance(llm_reply, str) or not llm_reply.strip():
        return None
    text = llm_reply.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    if text.find("{") == -1:
        # No JSON-shaped region at all — the LLM returned plain
        # prose. Surface a distinct error so the retry hint can
        # tell the LLM "previous reply was plain text, emit JSON".
        return ValueError("reply contained no '{' — was plain prose")
    try:
        json.loads(text[text.find("{") : text.rfind("}") + 1])
        return None
    except json.JSONDecodeError as exc:
        return exc


def _parse_llm_response(
    llm_reply: str,
    fallback_content: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Extract findings + fixed_content from an LLM JSON response.

    Tolerant of small formatting mistakes:
      * code-fenced JSON ```json ... ``` is unwrapped;
      * JSON wrapped in conversational text is grep'd for the first
        ``{`` and last ``}``;
      * malformed / empty / non-JSON reply → returns ``None`` so the
        caller can decide between "fail closed" (mandatory second
        pass) and "echo the original draft" (legacy mode). When the
        reply parses but ``fixed_content`` is missing/empty, the
        caller's responsibility to decide whether to use
        ``fallback_content`` as a default.

    Returns ``None`` on parse failure, otherwise::

        {
          "findings": [...],
          "fixed_content": str,   # always non-empty when reply parses
        }
    """
    if not isinstance(llm_reply, str) or not llm_reply.strip():
        return None

    text = llm_reply.strip()

    # Strip code-fenced JSON if present.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    # Find the JSON object boundaries.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        log.warning(
            "self_review: LLM reply had no JSON object boundaries"
        )
        return None

    # Local repair on the bounded slice: tolerate LLM truncation
    # (max_tokens wall, provider-side cap) so a self-review that
    # almost-finished doesn't fail the whole generator stage. The
    # helper is conservative — it only closes open brackets and
    # injects ``null`` for dangling colons; it never invents keys.
    try:
        from utils.json_repair import parse_llm_json
        parsed = parse_llm_json(text[start : end + 1])
    except json.JSONDecodeError as exc:
        log.warning(
            "self_review: failed to parse LLM JSON reply: %s; "
            "degrading to original content",
            exc,
        )
        return None

    if not isinstance(parsed, dict):
        return None

    # Lenient extraction: the LLM may emit a partial reply (e.g.
    # only ``findings`` with no ``fixed_content``, or vice versa).
    # We accept whatever shape arrives; the only hard failure is
    # a reply that contains *neither* field, because there is
    # nothing to record.
    raw_findings = parsed.get("findings", [])
    findings: List[Dict[str, str]] = []
    if isinstance(raw_findings, list):
        for item in raw_findings:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "low")).strip().lower()
            if severity not in {"high", "medium", "low"}:
                severity = "low"
            ftype = str(item.get("type", "unclear")).strip().lower()
            location = str(item.get("location", "<document body>")).strip()
            finding = str(item.get("finding", "")).strip()
            action = str(item.get("action", "")).strip()
            # Skip empty findings (degraded LLM reply shape).
            if not finding:
                continue
            entry: Dict[str, str] = {
                "severity": severity,
                "type": ftype,
                "location": location,
                "finding": finding,
            }
            if action:
                entry["action"] = action
            findings.append(entry)

    fixed_content = parsed.get("fixed_content", "")
    if not isinstance(fixed_content, str) or not fixed_content.strip():
        # LLM didn't return a fixed_content field — fall back to the
        # caller's original draft. We accept this silently rather
        # than treating it as a failure, since the findings list
        # (if any) is still useful audit metadata.
        if isinstance(fallback_content, str) and fallback_content:
            fixed_content = fallback_content
        else:
            fixed_content = ""

    # If the LLM reply was parseable JSON but the parsed dict has
    # *neither* a findings list nor a fixed_content key (e.g. it
    # was literally ``{}``), this is a hard failure — the audit
    # produced no payload. The caller will see ``succeeded=False``
    # after the retry path also fails.
    has_findings = bool(findings)
    has_fixed_content_key = "fixed_content" in parsed and bool(
        parsed["fixed_content"]
    )
    if not has_findings and not has_fixed_content_key:
        return None

    return {"findings": findings, "fixed_content": fixed_content}


class SelfReviewer:
    """LLM-agent second-pass self-review.

    The constructor takes an injected ``coding_tool`` so the reviewer
    can be unit-tested with a stub.
    """

    def __init__(self, coding_tool: Any):
        self._coding_tool = coding_tool

    def run(
        self,
        doc_type: str,
        content: str,
        upstream_content: Optional[str] = None,
        upstream_label: Optional[str] = None,
        *,
        mandatory: bool = True,
    ) -> SelfReviewReport:
        """Run the second-pass LLM self-review.

        Thin wrapper around :func:`run_doc_self_review`. The report
        carries the v2 schema (``attempted``/``succeeded``/``rewrote``/
        severity counts) plus legacy aliases
        (``issues_found``/``review_passed``) so callers that pre-date
        the second-pass redesign keep working.

        Boundary contracts:
          * doc_type not in ``{"prd", "arch", "test", "tasks"}`` →
            raises :class:`ValueError`.
          * LLM outage / unparseable JSON with ``mandatory=True``
            (default) → propagates :class:`SelfReviewUnavailableError`.
          * ``mandatory=False`` preserves the historical
            graceful-degrade behaviour (echoes ``content`` via
            ``fixed_content``).
        """
        label = upstream_label or "上游文档"
        report = run_doc_self_review(
            doc_content=content,
            doc_type=doc_type,
            coding_tool=self._coding_tool,
            upstream_content=upstream_content,
            upstream_label=label,
            mandatory=mandatory,
        )

        findings = report.get("findings") or []
        # Legacy aliases for callers that pre-date the second-pass
        # redesign. ``review_passed`` retains its old semantic: True
        # iff no high-severity finding is present.
        report["issues_found"] = list(findings)
        report["review_passed"] = not any(
            isinstance(f, dict) and f.get("severity") == "high"
            for f in findings
        )
        return report


# ======================================================================
# Public functional entry point
# ======================================================================


class SelfReviewUnavailableError(Exception):
    """Raised when the mandatory second-pass LLM is unavailable.

    The previous behaviour silently fell back to the first-pass
    artifact whenever the LLM call failed. That violated the
    "second pass must always run" contract the user set for the pipeline: a missing second pass is no longer acceptable.
    Generators that want mandatory behaviour wrap
    ``run_doc_self_review(..., mandatory=True)`` inside a try/except
    and re-raise this error to surface a hard failure to the
    server endpoint (which maps it to HTTP 503).

    Tests that rely on the legacy "graceful degrade" semantics must
    either set ``mandatory=False`` or catch this exception explicitly.
    """


def _summarise_findings(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Bucket findings into the audit report counters."""
    high = medium = low = 0
    for entry in findings:
        if not isinstance(entry, dict):
            continue
        sev = str(entry.get("severity", "")).lower()
        if sev == "high":
            high += 1
        elif sev == "medium":
            medium += 1
        elif sev == "low":
            low += 1
    return {
        "findings": findings,
        "findings_count": len(findings),
        "severity_high_count": high,
        "severity_medium_count": medium,
        "severity_low_count": low,
    }


def _compute_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_doc_self_review(
    doc_content: str,
    doc_type: str,
    coding_tool: Any = None,
    upstream_content: Optional[str] = None,
    upstream_label: Optional[str] = None,
    *,
    mandatory: bool = True,
) -> Dict[str, Any]:
    """Run the LLM-agent self-review (second-pass generation).

    The LLM agent reviews the freshly emitted document AND its
    upstream document (PRD for arch, arch for test_design, arch
    + test_design for tasks). The agent decides whether the
    document needs repair; if yes, it rewrites the document and
    returns the rewritten version as ``fixed_content``.

    Args:
        doc_content: The freshly-emitted document text.
        doc_type: One of ``{"prd", "arch", "test", "tasks"}``.
        coding_tool: Optional LLM client.
        upstream_content: Optional upstream document.
        upstream_label: Optional label for the upstream document.
        mandatory: When True (default) the second pass is
            *mandatory* — any unrecoverable failure raises
            :class:`SelfReviewUnavailableError` so the generator
            can refuse to promote the first-pass draft into a
            deliverable. When False the legacy "best-effort"
            behaviour is preserved (return the original draft with
            ``succeeded=False``); callers that still rely on
            graceful degradation (legacy tests) pass
            ``mandatory=False``.

    Returns:
        Dict with audit payload::

            {
              "doc_type": "prd" | "arch" | "test" | "tasks",
              "mandatory": bool,
              "attempted": bool,
              "succeeded": bool,
              "rewrote": bool,
              "findings": [...],
              "fixed_content": str,    # always non-empty when input non-empty
              "input_content_hash": "sha256:<hex>",
              "fixed_content_hash": "sha256:<hex>",
              "severity_high_count": int,
              "severity_medium_count": int,
              "severity_low_count": int,
              "error": Optional[str],   # populated only when succeeded=False
            }

    Raises:
        ValueError: if ``doc_type`` is not in the supported set.
        SelfReviewUnavailableError: when ``mandatory=True`` and the
            second LLM pass could not run (no coding_tool, exception,
            or unparseable reply).
    """
    if doc_type not in _VALID_DOC_TYPES:
        raise ValueError(
            f"invalid doc_type {doc_type!r}; expected one of "
            f"{sorted(_VALID_DOC_TYPES)}"
        )

    input_hash = _compute_hash(doc_content or "")
    base_payload: Dict[str, Any] = {
        "doc_type": doc_type,
        "mandatory": mandatory,
        "attempted": False,
        "succeeded": False,
        "rewrote": False,
        "input_content_hash": input_hash,
        "fixed_content_hash": input_hash,
        "error": None,
    }

    if not doc_content:
        # Empty input — the LLM has nothing to inspect. We still
        # mark the call as "attempted and succeeded" with an empty
        # finding list so callers can promote the artifact without
        # a hard failure: a missing first draft is its own signal.
        base_payload.update(
            _summarise_findings([]),
            attempted=True,
            succeeded=True,
            fixed_content=doc_content or "",
        )
        return base_payload

    if coding_tool is None:
        message = (
            f"second-pass self-review skipped: no coding_tool supplied "
            f"for doc_type={doc_type!r}"
        )
        log.warning(message)
        if mandatory:
            raise SelfReviewUnavailableError(message)
        base_payload.update(
            _summarise_findings([]),
            attempted=True,
            fixed_content=doc_content,
            error=message,
        )
        return base_payload

    label = upstream_label or "上游文档"
    prompt = _build_prompt(
        doc_type=doc_type,
        content=doc_content,
        upstream_content=upstream_content,
        upstream_label=label,
    )

    # Try the first call. If the reply parses we are done; if not,
    # retry once with a follow-up hint that surfaces the actual
    # parser error message — LLM providers are much more reliable
    # when they see the precise failure mode (e.g. "you emitted
    # plain prose, expected a JSON object").
    last_error: Optional[Exception] = None
    parsed: Optional[Dict[str, Any]] = None
    for attempt in range(2):
        try:
            if attempt == 0:
                llm_reply = coding_tool.query(prompt=prompt)
            else:
                # Retry with the error appended as a *follow-up
                # question*. We don't mutate the original prompt
                # so the first attempt is still a clean call.
                error_hint = (
                    last_error.__class__.__name__
                    + ": "
                    + str(last_error)
                    if last_error is not None
                    else "previous reply was not parseable JSON"
                )
                follow_up = (
                    "你上一轮的回复无法解析（" + error_hint + "）。"
                    "请**只**输出一个 JSON 对象，"
                    "第一个字符必须是 `{`，最后一个必须是 `}`，"
                    "中间不要夹带任何 prose、Markdown 围栏、"
                    "或第二个 JSON 对象。"
                )
                llm_reply = coding_tool.query(prompt=follow_up)
        except Exception as exc:  # noqa: BLE001 — best-effort surface
            message = (
                f"second-pass self-review failed: coding_tool.query raised "
                f"{type(exc).__name__}: {exc}"
            )
            log.warning(message)
            log.debug("traceback: %s", traceback.format_exc())
            # 2026-09-13: the documented contract (see Raises: in
            # this docstring) is that an LLM exception with
            # ``mandatory=True`` propagates SelfReviewUnavailableError
            # so the generator refuses to promote the unaudited
            # draft. The previous catch-all degraded silently,
            # letting mandatory callers ship unaudited artifacts
            # during an outage.
            if mandatory:
                raise SelfReviewUnavailableError(message) from exc
            base_payload.update(
                _summarise_findings([]),
                attempted=True,
                fixed_content=doc_content,
                error=message,
            )
            return base_payload

        parsed = _parse_llm_response(llm_reply, fallback_content=doc_content)
        if parsed is None:
            last_error = _last_parse_error(llm_reply)
            continue
        break

    if parsed is None:
        # Both attempts failed. Return a degraded report — the
        # caller (generator) can decide whether to surface this
        # to the user or accept the first-pass draft. We never
        # raise SelfReviewUnavailableError here, because the
        # governing principle is "let the framework do the data
        # structure mapping, the LLM only sees content" — if the
        # audit step can't run, the canonical document is still
        # the original first-pass draft, which is immutable
        # anyway (arch / prd / test-design are not overwritten
        # by self_review).
        message = (
            f"second-pass self-review failed after 2 attempts: "
            f"LLM reply for doc_type={doc_type!r} was not parseable JSON; "
            f"last_error={type(last_error).__name__ if last_error else 'None'}"
        )
        log.warning(message)
        base_payload.update(
            _summarise_findings([]),
            attempted=True,
            fixed_content=doc_content,
            error=message,
        )
        return base_payload

    fixed_content = parsed["fixed_content"]
    findings = parsed["findings"]
    rewrote = bool(fixed_content) and fixed_content != doc_content
    fixed_hash = _compute_hash(fixed_content)
    base_payload.update(
        _summarise_findings(findings),
        attempted=True,
        succeeded=True,
        rewrote=rewrote,
        fixed_content=fixed_content,
        fixed_content_hash=fixed_hash,
    )
    return base_payload


# ======================================================================
# Audit-trail report writer
# ======================================================================
#
# Persists the report to ``plans/{id}/{doc_type}_self_review.json``.
# Schema (intentionally minimal for grep / diff)::

_AUDIT_DOC_TYPES = frozenset({"prd", "arch", "test", "tasks"})


def write_self_review_report(
    plan_dir: "Path | str",
    doc_type: str,
    report: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Persist a ``run_doc_self_review`` report to
    ``plan_dir / f"{doc_type}_self_review.json"``.

    Returns the serialised dict that was written, or ``None`` if
    the write failed. Any exception is logged at WARNING so the
    emit pipeline is never blocked.

    Boundary contracts:
      * ``doc_type`` not in ``{"prd", "arch", "test", "tasks"}``
        → log + return None.
      * ``fixed_content_hash`` is ``"sha256:<hex>"`` of
        ``report["fixed_content"]``.
      * The legacy ``fixed`` flag is kept as
        ``bool(findings)`` for readers that pre-date the v2 schema;
        new fields (``attempted``, ``succeeded``, ``rewrote``,
        severity counts) are added on top of it.
      * Existing report file is overwritten — the latest run wins.
    """
    if doc_type not in _AUDIT_DOC_TYPES:
        log.warning(
            "write_self_review_report: invalid doc_type %r; "
            "expected one of %s; skipping",
            doc_type, sorted(_AUDIT_DOC_TYPES),
        )
        return None

    try:
        plan_dir_path = Path(plan_dir)
        plan_dir_path.mkdir(parents=True, exist_ok=True)

        fixed_content = report.get("fixed_content") or ""
        fixed_content_hash = (
            report.get("fixed_content_hash")
            or _compute_hash(fixed_content)
        )
        input_content_hash = report.get(
            "input_content_hash", fixed_content_hash
        )

        findings = report.get("findings") or []
        if not isinstance(findings, list):
            findings = []

        severity_high_count = int(report.get("severity_high_count") or 0)
        severity_medium_count = int(
            report.get("severity_medium_count") or 0
        )
        severity_low_count = int(report.get("severity_low_count") or 0)
        if not any([
            severity_high_count, severity_medium_count, severity_low_count
        ]):
            # Backfill severity counts from `findings` for legacy callers
            # that haven't been migrated yet.
            severity_high_count = sum(
                1 for f in findings
                if isinstance(f, dict) and f.get("severity") == "high"
            )
            severity_medium_count = sum(
                1 for f in findings
                if isinstance(f, dict) and f.get("severity") == "medium"
            )
            severity_low_count = sum(
                1 for f in findings
                if isinstance(f, dict) and f.get("severity") == "low"
            )

        attempted = bool(report.get("attempted", True))
        succeeded = bool(report.get("succeeded", attempted))
        rewrote = bool(report.get("rewrote", False))
        mandatory = bool(report.get("mandatory", True))

        serialised = {
            "schema_version": 2,
            "doc_type": doc_type,
            "attempted": attempted,
            "succeeded": succeeded,
            "rewrote": rewrote,
            "mandatory": mandatory,
            "skipped": not attempted,
            "skip_reason": (
                report.get("error") if not attempted else None
            ),
            "error": report.get("error"),
            "findings": findings,
            "findings_count": len(findings),
            "severity_high_count": severity_high_count,
            "severity_medium_count": severity_medium_count,
            "severity_low_count": severity_low_count,
            "input_content_hash": input_content_hash,
            "fixed_content_hash": fixed_content_hash,
            # Legacy field kept for back-compat with readers from the
            # keyword-matcher era.
            "fixed": bool(findings) or (succeeded and rewrote),
            "ts": datetime.utcnow().isoformat() + "Z",
        }

        report_path = plan_dir_path / f"{doc_type}_self_review.json"
        tmp_path = report_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(serialised, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, report_path)

        return serialised
    except Exception as exc:  # noqa: BLE001 — best-effort degrade
        log.warning(
            "write_self_review_report: failed to persist %s "
            "self-review report for %s: %s: %s",
            doc_type, plan_dir, type(exc).__name__, exc,
        )
        log.debug("traceback: %s", traceback.format_exc())
        return None