"""Evidence contract for the LLM-judged verification methods (2026-09-18).

Why this module exists
-----------------------
After C4/C5 the verification layer has five methods, and two of them
still need an LLM to reach a conclusion:

  * ``api_test``       — framework-executed, no LLM (see
                         :mod:`verification_api_runner`).
  * ``full_ci``        — framework-executed, no LLM (see
                         :mod:`verification_ci_runner`).
  * ``ui_validation``  — a puppeteer-driving sub-agent.
  * ``e2e``            — the same sub-agent, driving the whole flow.
  * ``code_review``    — a reading sub-agent.

The three LLM-judged methods (:attr:`EVIDENCE_REQUIRED_METHODS`) rest on
a sub-agent's word, which is why they owe an artifact.

The remaining risk is the one the 2026-09-18 post-mortem kept hitting:
an LLM claiming PASSED without having done anything checkable. The old
answer was a cross-method overlay keyed on a **self-reported exit code**
— which is exactly what made the same artifact grade PASSED in one round
and FAILED in the next (one run's VP-001).

This module re-anchors that anti-false-positive function on the artifact
the verification was supposed to produce:

  * ``code_review`` must write ``citations.json`` — the specific
    file/line/snippet each conclusion rests on.
  * ``ui_validation`` must write ``checkpoints.json`` — the selector,
    the expected label and what was actually observed.

The framework then checks those claims against reality: does the cited
file exist, does it have that line, does the quoted snippet actually
appear there? A citation that cannot be resolved is not evidence, and a
PASSED resting on unresolvable evidence is downgraded to FAILED.

**Only PASSED is inspected.** That is the whole job — refuse to let an
unsupported pass through. A FAILED verdict already stands on its own,
and a reviewer reporting that something is *missing* often has no line
to cite; checking those too would only add noise to every failure path,
including the exception paths that never had a chance to write an
artifact at all.

The artifact is also the audit trail: ``plans/<id>/vp_artifacts/<vp_id>/``
holds the citations/checkpoints (and the raw API response for
``api_test``), so an operator can re-check any verdict after the fact
without replaying the round.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from bounded_subprocess import run_bounded

logger = logging.getLogger(__name__)

#: Artifact filenames, relative to ``plans/<id>/vp_artifacts/<vp_id>/``.
CITATIONS_FILENAME = "citations.json"
CHECKPOINTS_FILENAME = "checkpoints.json"

#: How many lines either side of the cited line a snippet may be found
#: on. LLM citations are usually exact but occasionally off by one when
#: the snippet starts at a blank line or a docstring boundary; allowing a
#: small window keeps a correct citation from being rejected for an
#: off-by-one, while still refusing a citation of code that is not there.
_LINE_TOLERANCE = 2

#: Methods that must produce an evidence artifact.
#:
#: ``e2e`` joined on 2026-09-18 alongside its introduction: it is the
#: Phase-2 sibling of ``ui_validation`` (same puppeteer execution path,
#: whole-flow scope instead of one component), so it rests on exactly the
#: same kind of unverifiable claim and needs exactly the same checkpoints
#: artifact.
EVIDENCE_REQUIRED_METHODS = ("code_review", "ui_validation", "e2e")


@dataclass
class EvidenceIssue:
    """One reason a verdict's stated basis could not be verified."""

    kind: str
    detail: str

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "detail": self.detail}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None


def _normalize(text: str) -> str:
    """Collapse whitespace so a citation is not rejected over indentation."""
    return re.sub(r"\s+", " ", str(text)).strip()


# ---------------------------------------------------------------------------
# code_review — citations
# ---------------------------------------------------------------------------


def verify_citations(
    citations: Any, project_dir: Path,
) -> List[EvidenceIssue]:
    """Check every citation against the files on disk.

    A citation is ``{"file": "<path relative to project_dir>", "line": N,
    "snippet": "<the quoted text>", "supports": "<optional>"}``. All
    three of ``file`` / ``line`` / ``snippet`` are required: a citation
    that names no line, or quotes nothing, cannot be checked and
    therefore does not support a conclusion.
    """
    issues: List[EvidenceIssue] = []
    project_dir = Path(project_dir)

    if not isinstance(citations, list) or not citations:
        return [EvidenceIssue(
            "missing_evidence",
            "code_review 必须给出非空的 citations 列表 —— 结论必须有出处",
        )]

    for index, citation in enumerate(citations):
        prefix = f"citations[{index}]"
        if not isinstance(citation, dict):
            issues.append(EvidenceIssue(
                "malformed", f"{prefix} 不是对象（{type(citation).__name__}）"
            ))
            continue

        rel = str(citation.get("file") or "").strip()
        if not rel:
            issues.append(EvidenceIssue("malformed", f"{prefix} 缺少 file"))
            continue

        try:
            line_no = int(citation.get("line"))
        except (TypeError, ValueError):
            issues.append(EvidenceIssue(
                "malformed", f"{prefix} 缺少或非法的 line：{citation.get('line')!r}"
            ))
            continue

        snippet = str(citation.get("snippet") or "")
        if not _normalize(snippet):
            issues.append(EvidenceIssue(
                "malformed", f"{prefix} 缺少 snippet（引用必须给出被引用的原文）"
            ))
            continue

        target = (project_dir / rel).resolve()
        try:
            target.relative_to(project_dir.resolve())
        except ValueError:
            issues.append(EvidenceIssue(
                "outside_project", f"{prefix} 指向项目外的路径：{rel}"
            ))
            continue

        try:
            file_lines = target.read_text(
                encoding="utf-8", errors="replace",
            ).splitlines()
        except FileNotFoundError:
            issues.append(EvidenceIssue(
                "missing_file", f"{prefix} 引用的文件不存在：{rel}"
            ))
            continue
        except OSError as exc:
            issues.append(EvidenceIssue(
                "unreadable_file", f"{prefix} 无法读取 {rel}：{exc}"
            ))
            continue

        if not (1 <= line_no <= len(file_lines)):
            issues.append(EvidenceIssue(
                "line_out_of_range",
                f"{prefix} 行号 {line_no} 超出 {rel} 的范围（共 "
                f"{len(file_lines)} 行）",
            ))
            continue

        if not _snippet_matches(file_lines, line_no, snippet):
            issues.append(EvidenceIssue(
                "snippet_mismatch",
                f"{prefix} 在 {rel}:{line_no} 附近找不到所引用的原文："
                f"{_normalize(snippet)[:80]!r}",
            ))

    return issues


def _snippet_matches(file_lines: List[str], line_no: int, snippet: str) -> bool:
    """True when the quoted text actually appears at (or within a couple
    of lines of) the cited line.

    Matches on the snippet's **first non-empty line**, in either
    direction: an LLM may quote a longer block that starts on the cited
    line, or quote a shorter fragment of it. Whitespace is collapsed on
    both sides first, so indentation and re-wrapping do not matter.
    """
    wanted = next(
        (_normalize(part) for part in snippet.splitlines() if _normalize(part)),
        "",
    )
    if not wanted:
        return False

    start = max(0, line_no - 1 - _LINE_TOLERANCE)
    stop = min(len(file_lines), line_no + _LINE_TOLERANCE)
    for candidate in file_lines[start:stop]:
        normalized = _normalize(candidate)
        if not normalized:
            continue
        if wanted in normalized or normalized in wanted:
            return True
    return False


# ---------------------------------------------------------------------------
# ui_validation — checkpoints
# ---------------------------------------------------------------------------


def verify_checkpoints(checkpoints: Any) -> List[EvidenceIssue]:
    """Check the shape of a puppeteer run's checkpoint list.

    Each checkpoint is ``{"selector": ..., "expected": ...,
    "actual": ..., "passed": bool}``. The framework cannot re-run the
    browser here, but it can insist that the sub-agent reported an
    observable selector and an actual value for every checkpoint — "it
    looked fine" is not a checkpoint.
    """
    issues: List[EvidenceIssue] = []

    if not isinstance(checkpoints, list) or not checkpoints:
        return [EvidenceIssue(
            "missing_evidence",
            "ui_validation 必须给出非空的 checkpoints 列表 —— "
            "UI 结论必须有可观察的检查点",
        )]

    for index, checkpoint in enumerate(checkpoints):
        prefix = f"checkpoints[{index}]"
        if not isinstance(checkpoint, dict):
            issues.append(EvidenceIssue(
                "malformed", f"{prefix} 不是对象（{type(checkpoint).__name__}）"
            ))
            continue
        selector = str(checkpoint.get("selector") or "").strip()
        if not selector:
            issues.append(EvidenceIssue(
                "malformed", f"{prefix} 缺少 selector（没有定位就无从核对）"
            ))
        if "expected" not in checkpoint:
            issues.append(EvidenceIssue(
                "malformed",
                f"{prefix} 缺少 expected（没有期望值就无从判断这一条算不算过）",
            ))
        if "actual" not in checkpoint:
            issues.append(EvidenceIssue(
                "malformed", f"{prefix} 缺少 actual（必须记录实际观察到的值）"
            ))
        if not isinstance(checkpoint.get("passed"), bool):
            issues.append(EvidenceIssue(
                "malformed",
                f"{prefix}.passed 必须是布尔值，得到 "
                f"{checkpoint.get('passed')!r}",
            ))

    return issues


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def artifact_dir_for(plan_dir: Path, vp_id: str) -> Path:
    return Path(plan_dir) / "vp_artifacts" / str(vp_id)


def verify_evidence(
    method: str, plan_dir: Path, project_dir: Path, vp_id: str,
) -> List[EvidenceIssue]:
    """Verify the artifact ``method`` is required to produce.

    Returns ``[]`` for methods with no evidence contract (``api_test``
    carries its own, inline, in :mod:`verification_api_runner`).

    Never raises: a malformed artifact is reported as an issue, because
    the caller's response to either is the same — the verdict's basis is
    not checkable.
    """
    if method not in EVIDENCE_REQUIRED_METHODS:
        return []

    directory = artifact_dir_for(plan_dir, vp_id)
    try:
        if method == "code_review":
            payload = _read_json(directory / CITATIONS_FILENAME)
            if payload is None:
                return [EvidenceIssue(
                    "missing_artifact",
                    f"缺少 {CITATIONS_FILENAME} —— code_review 的每条结论都必须"
                    f"落到具体的 文件/行/原文",
                )]
            citations = payload.get("citations") if isinstance(payload, dict) else None
            return verify_citations(citations, project_dir)

        if method in ("ui_validation", "e2e"):
            payload = _read_json(directory / CHECKPOINTS_FILENAME)
            if payload is None:
                return [EvidenceIssue(
                    "missing_artifact",
                    f"缺少 {CHECKPOINTS_FILENAME} —— {method} 必须记录"
                    f"每一次实际观察",
                )]
            checkpoints = (
                payload.get("checkpoints") if isinstance(payload, dict) else None
            )
            return verify_checkpoints(checkpoints)
    except Exception as exc:  # noqa: BLE001 - evidence check never aborts
        logger.warning(
            "[verification_evidence] check failed for %s (%s): %s",
            vp_id, method, exc,
        )
        return [EvidenceIssue(
            "verification_error", f"依据核对过程出错：{type(exc).__name__}: {exc}"
        )]

    return []


def apply_to_verdict(
    verdict: Dict[str, Any],
    method: str,
    plan_dir: Path,
    project_dir: Path,
    vp_id: str,
) -> Dict[str, Any]:
    """Downgrade a PASSED whose stated basis cannot be verified.

    **Only PASSED is inspected.** That is the whole job: refuse to let an
    unsupported pass through. A FAILED verdict already stands on its own
    — and a reviewer reporting that something is *missing* often has no
    line to cite — so touching it would only add noise to every failure
    path (including the exception paths that never had a chance to write
    an artifact).

    Returns ``verdict`` unchanged when the status is not PASSED or the
    evidence holds up.
    """
    if not isinstance(verdict, dict):
        return verdict
    if str(verdict.get("status")) != "PASSED":
        return verdict

    issues = verify_evidence(method, plan_dir, project_dir, vp_id)
    if not issues:
        return verdict

    details = [f"[{i.kind}] {i.detail}" for i in issues]
    verdict = dict(verdict)
    verdict["reasons"] = list(verdict.get("reasons") or []) + details
    verdict["status"] = "FAILED"
    verdict["actual_result"] = (
        f"{method} 判定为 PASSED，但它的依据无法核对，改判 FAILED："
        + "；".join(details)
    )
    evidence = verdict.get("evidence")
    evidence = dict(evidence) if isinstance(evidence, dict) else (
        {"original_evidence": evidence}
    )
    evidence["evidence_check"] = {
        "method": method,
        "issues": [i.to_dict() for i in issues],
    }
    verdict["evidence"] = evidence
    return verdict


# ---------------------------------------------------------------------------
# evidence_command — optional supporting evidence, run by the framework
# ---------------------------------------------------------------------------

#: Ceiling for an ``evidence_command``. It is supporting material, not the
#: verdict, so it must not be able to eat a round the way the old per-VP
#: command could.
EVIDENCE_COMMAND_TIMEOUT_SECONDS = 600

#: Output kept per stream in the verdict evidence.
MAX_EVIDENCE_OUTPUT_CHARS = 4000


@dataclass
class EvidenceCommandResult:
    """What an optional ``evidence_command`` produced."""

    command: str
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": round(self.duration_seconds, 2),
            "timed_out": self.timed_out,
            "error": self.error,
        }


def run_evidence_command(
    command: str,
    project_dir: Path,
    timeout_seconds: int = EVIDENCE_COMMAND_TIMEOUT_SECONDS,
) -> EvidenceCommandResult:
    """Run a VP's optional ``evidence_command`` and capture its output.

    This is the sanctioned residue of the retired VP ``test_command``: the
    minority case where an existing test *is* the only evidence for an
    assertion. It is run by the **framework** (no sub-agent, no LLM) so it
    cannot be reported dishonestly, and its result is attached to the
    verdict as supporting material.

    It deliberately does **not** influence the verdict. The old design let
    a command's exit code override the reviewer's conclusion — and since
    the reviewer both produced and interpreted that number, the same
    artifact could flip verdict between rounds. Evidence is evidence.

    Bounded and never-raising: a timeout or a missing binary is reported,
    not escalated.
    """
    result = EvidenceCommandResult(command=command)
    start = time.monotonic()
    try:
        completed = run_bounded(
            command,
            cwd=str(project_dir),
            text=True,
            timeout=timeout_seconds,
        )
        result.exit_code = completed.returncode
        result.stdout = (completed.stdout or "")[:MAX_EVIDENCE_OUTPUT_CHARS]
        result.stderr = (completed.stderr or "")[:MAX_EVIDENCE_OUTPUT_CHARS]
    except subprocess.TimeoutExpired:
        result.timed_out = True
        result.error = f"timed out after {timeout_seconds}s"
    except Exception as exc:  # noqa: BLE001 - evidence is best-effort
        result.error = f"{type(exc).__name__}: {exc}"
    result.duration_seconds = time.monotonic() - start
    return result



def attach_evidence_command(
    verdict: Dict[str, Any], vp: Dict[str, Any], project_dir: Path,
) -> Dict[str, Any]:
    """Run the VP's ``evidence_command`` (if any) and attach the result.

    Never changes ``status``. Returns ``verdict`` unchanged when the VP
    declares no command.
    """
    if not isinstance(verdict, dict):
        return verdict
    command = str(vp.get("evidence_command") or "").strip()
    if not command:
        return verdict

    outcome = run_evidence_command(command, project_dir)
    if not outcome.error and not outcome.timed_out:
        summary = f"exit {outcome.exit_code}"
    else:
        summary = outcome.error

    verdict = dict(verdict)
    verdict["reasons"] = list(verdict.get("reasons") or []) + [
        f"[evidence] evidence_command {summary}（仅作佐证，不参与判定）"
    ]
    evidence = verdict.get("evidence")
    evidence = dict(evidence) if isinstance(evidence, dict) else (
        {"original_evidence": evidence}
    )
    evidence["evidence_command"] = outcome.to_dict()
    verdict["evidence"] = evidence
    return verdict
