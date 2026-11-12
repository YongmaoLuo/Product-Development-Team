"""Evidence contract for the verification methods that owe an artifact (2026-09-18).

The heading used to read "the LLM-judged verification methods".  That stopped
being true when ``e2e`` grew a ``command`` face (2026-11-04): the
deterministic runner judges that face on the exit code of its entry command,
and no LLM ever speaks about it.

Why this module exists
-----------------------
After C4/C5 the verification layer has five methods, and two of them
still need an LLM to reach a conclusion:

  * ``api_test``       — framework-executed, no LLM (see
                         :mod:`verification_api_runner`).
  * ``full_ci``        — framework-executed, no LLM (see
                         :mod:`verification_ci_runner`).
  * ``ui_validation``  — a puppeteer-driving sub-agent.
  * ``e2e``            — the same sub-agent, driving the whole flow —
                         or, for its ``command`` face, the framework
                         itself (see :mod:`verification_ci_runner`).
  * ``code_review``    — a reading sub-agent.

The methods on :attr:`EVIDENCE_REQUIRED_METHODS` owe an artifact: their
verdict has to be checkable, and for the LLM-judged ones that means
resting on a sub-agent's word.

``e2e`` is the one method on that list with two faces (2026-11-04, arch
决策点 7): its ``browser`` sub-class is judged by that sub-agent and
owes ``checkpoints.json``, while its ``command`` sub-class is run by the
deterministic runner — the exit code of its entry command *is* the
verdict, and no LLM ever speaks about it — so it owes ``ci_output.json``
instead.  Which contract applies is decided by the **declared**
sub-class, not by the method name (see :func:`_uses_ci_output_contract`).

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
  * ``e2e`` must write ``checkpoints.json`` for its ``browser``
    sub-class, and ``ci_output.json`` for its ``command`` sub-class.

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

The framework also has to know whether the tree it is checking is
still the tree the round produced its citations against.  Several
verification processes can share one worktree, and a concurrent
mutation (a "变异负控" running in parallel) can rewrite a file between
the moment a verdict was written and the moment its citations are read
— and the check then reads a tree that belongs to nobody.
:func:`tree_fingerprint` gives the round a content-based fingerprint of
the working tree (HEAD plus a hash of ``git status --porcelain``) so
the two states can be told apart; :func:`tree_mutated` compares a
later reading against it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from bounded_subprocess import run_bounded

# e2e 的两个子类与「这条验证点是哪一类」的读法，从执行侧借
# （``verification_ci_runner`` 是判定 e2e 能不能跑的那一层）。
# **同一份判定、不写第二遍**：子类判定的口径住在 runner 里，抄一份进本
# 模块，迟早会漂 —— 而漂的方向一定是「看起来能过」。
from verification_ci_runner import E2E_KIND_COMMAND, E2E_METHOD, e2e_kind

logger = logging.getLogger(__name__)

#: Artifact filenames, relative to ``plans/<id>/vp_artifacts/<vp_id>/``.
CITATIONS_FILENAME = "citations.json"
CHECKPOINTS_FILENAME = "checkpoints.json"

#: The deterministic runner's audit copy (``verification_ci_runner``).  It
#: is what an ``e2e`` **command** point owes: that point is judged by the
#: runner's exit code, not by an LLM, so it never writes a checkpoint.
CI_OUTPUT_FILENAME = "ci_output.json"

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
#:
#: 2026-11-09: the ``command`` face of ``e2e`` is **not** LLM-judged — the
#: deterministic runner grades it on the exit code of its entry command —
#: so it owes that runner's own artifact (``ci_output.json``) instead of a
#: checkpoint.  The method stays on this list either way: it owes an
#: artifact, and *which* one is decided by the declared sub-class (see
#: :func:`_uses_ci_output_contract`).
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


# ---------------------------------------------------------------------------
# Staging the plan directory's forensic artifacts
# ---------------------------------------------------------------------------
#
# 2026-11-01（VP-017 第 1 轮）：一条 code_review 引用是
# ``(file, line, snippet)``，框架按 ``project_dir`` 解析它
# （``verify_citations``：``target = (project_dir / rel)``）。计划目录
# （``citations.json`` 的所在地）一般在**另一个 checkout** 里，那里的取证
# 产物在 ``project_dir`` 下没有对应文件 —— 于是引用被逐条判成
# ``missing_file``，而执行方按计划目录自检得到的是"unresolved=0"。同一个
# 判据，两个基准，两个答案。
#
# 暂存把计划目录的取证产物**逐字节**复制到 ``project_dir`` 内的一份副本
# 上，让引用有一棵能解析的树。位置两个条件缺一不可：
#
#   * 在 ``project_dir`` 之内 —— 框架解析引用的基准；
#   * 不在被服务的 plans 树之下 —— plans 树里多出一个目录会让 api 服务的
#     ``_refuse_when_data_source_does_not_describe_the_tree`` 答 503
#     （``state_db_does_not_describe_plans_dir``），把 UI 验证点顶成失败。

#: Where the plan directory's artifacts are staged, relative to the
#: project dir.  Dot-prefixed and inside ``.pdt/`` on purpose: it is
#: runtime state, gitignored, and not a tree the api service serves.
EVIDENCE_STAGING_ROOT_PARTS = (".pdt", "evidence")


def evidence_staging_root(
    project_dir: Path, plan_id: str, plans_dir: Optional[Path] = None,
) -> Path:
    """The root the plan's forensic artifacts are staged under.

    ``plans_dir`` is the tree the api service serves.  When the staging
    root would land inside it, the root moves: a plan directory there
    that the state db does not know makes ``GET /api/plans`` answer 503.
    """
    project_dir = Path(project_dir)
    root = project_dir.joinpath(*EVIDENCE_STAGING_ROOT_PARTS) / str(plan_id)
    if plans_dir is None:
        return root
    try:
        root.resolve().relative_to(Path(plans_dir).resolve())
    except ValueError:
        return root
    return project_dir / ".pdt" / "evidence-staging" / str(plan_id)


def stage_plan_artifacts(
    plan_dir: Path, project_dir: Path, plan_id: str = "",
    plans_dir: Optional[Path] = None,
) -> Path:
    """Copy the plan directory's forensic artifacts into the project dir.

    Byte-for-byte, and idempotent: a staged copy that differs by a byte
    carries different line numbers, so the plan's own line anchors would
    stop resolving; a copy that already matches is left alone, so a second
    round does not rewrite the tree again.

    Returns the staging root — the tree a citation may name.
    """
    plan_dir = Path(plan_dir)
    root = evidence_staging_root(
        project_dir, plan_id or plan_dir.name, plans_dir,
    )
    sources: List[tuple] = []
    artifacts = plan_dir / "vp_artifacts"
    if artifacts.is_dir():
        for path in sorted(artifacts.rglob("*")):
            if path.is_file():
                # Relative to the PLAN directory, so the staged tree keeps
                # the shape the citation names (``vp_artifacts/<vp_id>/...``).
                sources.append((path, root / path.relative_to(plan_dir)))
    plan_file = plan_dir / "verification_plan.json"
    if plan_file.is_file():
        sources.append((plan_file, root / plan_file.name))
    for source, target in sources:
        _stage_one(source, target)
    return root


def _stage_one(source: Path, target: Path) -> None:
    """Copy one file into the staging tree, unless it is already there."""
    try:
        if target.is_file() and target.stat().st_size == source.stat().st_size:
            if target.read_bytes() == source.read_bytes():
                return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    except OSError as exc:
        logger.warning(
            "[verification_evidence] staging %s failed: %s", source, exc,
        )


def _uses_ci_output_contract(
    method: str, vp: Optional[Dict[str, Any]],
) -> bool:
    """Is this an ``e2e`` point the framework judges itself?

    ``e2e`` has two faces (2026-11-04, arch 决策点 7).  The ``browser``
    face is a sub-agent's job and owes ``checkpoints.json``; the
    ``command`` face is run by the deterministic runner, which leaves
    ``ci_output.json`` behind and never writes a checkpoint.

    So the contract is picked by the **declared** sub-class and not by
    the method name: holding a command point to the browser contract
    judged a deterministic PASSED FAILED for a file it was never going
    to write — and the whole job of this module is to refuse
    *unsupported* passes, not to invent one.  A point that declares no
    sub-class is a browser point (that is the runner's own reading), and
    a bare call — one that knows no VP at all — is held to the browser
    contract too.
    """
    if method != E2E_METHOD:
        return False
    if not isinstance(vp, dict):
        return False
    return e2e_kind(vp) == E2E_KIND_COMMAND


def _verify_ci_output(directory: Path, vp_id: str) -> List[EvidenceIssue]:
    """Check the deterministic runner's audit copy of an ``e2e`` command point.

    The runner wrote it; nobody else does.  Three things are asked of it:
    it is there, it is a JSON object, and — when it names a VP at all —
    it names **this** one.  A VP's artifact directory is reused across
    rounds, so an artifact that quotes another VP is not this VP's basis.
    """
    path = Path(directory) / CI_OUTPUT_FILENAME
    if not path.is_file():
        return [EvidenceIssue(
            "missing_artifact",
            f"缺少 {CI_OUTPUT_FILENAME} —— e2e(command) 由框架直接执行，"
            f"判定依据就是这条入口命令的退出码",
        )]
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return [EvidenceIssue(
            "malformed",
            f"{CI_OUTPUT_FILENAME} 不是可解析的 JSON 对象 —— 它记的是"
            f"这条入口命令的退出码与产物",
        )]
    named = str(payload.get("vp_id") or "").strip()
    if named and named != vp_id:
        return [EvidenceIssue(
            "vp_id_mismatch",
            f"{CI_OUTPUT_FILENAME} 记的是 {named} 的裁决，不是 {vp_id} 的 —— "
            f"产物目录串了",
        )]
    return []


def verify_evidence(
    method: str, plan_dir: Path, project_dir: Path, vp_id: str,
    vp: Optional[Dict[str, Any]] = None,
) -> List[EvidenceIssue]:
    """Verify the artifact ``method`` is required to produce.

    ``vp`` is the verification point itself — the only thing that knows
    **which** ``e2e`` sub-class this is.  It is optional: a bare call
    knows nothing about the sub-class and applies the browser contract,
    which is the one that holds by default.

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

        if method == E2E_METHOD and _uses_ci_output_contract(method, vp):
            return _verify_ci_output(directory, vp_id)

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
    vp: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Downgrade a PASSED whose stated basis cannot be verified.

    **Only PASSED is inspected.** That is the whole job: refuse to let an
    unsupported pass through. A FAILED verdict already stands on its own
    — and a reviewer reporting that something is *missing* often has no
    line to cite — so touching it would only add noise to every failure
    path (including the exception paths that never had a chance to write
    an artifact).

    ``vp`` is the verification point: it is what tells an ``e2e``
    ``command`` point apart from an ``e2e`` ``browser`` one, and the two
    owe different artifacts.  A bare call knows no VP and applies the
    browser contract.

    Returns ``verdict`` unchanged when the status is not PASSED or the
    evidence holds up.
    """
    if not isinstance(verdict, dict):
        return verdict
    if str(verdict.get("status")) != "PASSED":
        return verdict

    issues = verify_evidence(method, plan_dir, project_dir, vp_id, vp=vp)
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
# Subject check — is this verdict about *this* verification point?
# ---------------------------------------------------------------------------
#
# 2026-11-07（第 4 轮 · VP-028）：验证点问的是
# ``backend/verification_executor.py`` 的门禁重算，子 agent 交回来的却是
# 一份 PASSED，11 条理由讲的全是 ``backend/routes/verification.py``
# —— 另一个验证点（VP-038）的题目。别的闸门都满意了：引用解得开、
# 产物在、状态是 PASSED。**没有任何一道闸问过「这条裁决答的是不是这道
# 题」**，于是一份答错题的裁决以「已验证的通过」的身份进了最终报告。
#
# 下面的规则刻意很窄 —— 只在能*证明*答的是别的东西时才判：
#
#   * 只对 LLM 判定的方法（``EVIDENCE_REQUIRED_METHODS``）；
#   * 只在验证点的 ``expected_result`` 点名了一个 ``project_dir`` 下
#     **真实存在**的仓库路径时（点不出路径就不判）；
#   * 只在裁决本身也提到了路径时（纯文字的审查不该被罚）；
#   * 裁决用**裸文件名**指代验证点点名的那个文件时，视为同一路径
#     （判据 (5)）—— 裁决顺带提到的产物名（``checkpoints.json`` 等）
#     不算路径，不会凭空造出 mismatch；
#   * 裁决的路径与验证点的路径**一个都不相交** → 答的不是这道题。
#
# 它只做 downgrade：PASSED → FAILED。FAILED 仍是 FAILED，SKIPPED 仍是
# SKIPPED，绝不把失败升成通过，也不新增任何修复语义。

#: A path word: a slash-separated chain that ends in a file name with an
#: extension — ``backend/verification_executor.py``,
#: ``backend/routes/verification.py``.  The ``:1700-1701`` that follows a
#: citation is **not** part of the word (the character class stops at the
#: colon), and the lookbehind keeps a word from starting in the middle of
#: a longer path.
_PATH_WORD_RE = re.compile(
    r"(?<![\w/.\-])/?(?:[A-Za-z0-9_.\-]+/)*[A-Za-z0-9_\-]+\.[A-Za-z][A-Za-z0-9]*"
)

#: How many paths a reason names before it says "等".
_PATH_LIST_LIMIT = 3


def _path_words(text: str) -> List[str]:
    """Every path-shaped word in ``text``, in order, without duplicates."""
    words: List[str] = []
    for match in _PATH_WORD_RE.finditer(text or ""):
        token = match.group(0)
        if "/" not in token:
            continue
        if token not in words:
            words.append(token)
    return words


def _bare_file_names(text: str) -> Set[str]:
    """The file names ``text`` mentions *without* a path in front of them.

    A bare name is not a path of this repository on its own — that is
    exactly why :func:`_path_words` drops it, and why it must not become
    a path source of its own.  But for the file a verification point
    named, the bare name *is* that file: a verdict that calls the
    checked file ``verification_executor.py`` has answered the question
    about ``backend/verification_executor.py`` (judging (5)).

    Only the tokens that stand alone are collected — a name that is the
    last segment of a longer path (``backend/routes/verification.py``)
    is not a bare mention of ``verification.py``.
    """
    names: Set[str] = set()
    for match in _PATH_WORD_RE.finditer(text or ""):
        token = match.group(0)
        if "/" in token:
            continue
        names.add(token.lower())
    return names


def _repo_relative(token: str, root: Path) -> Optional[str]:
    """Map a path word onto a repo-relative path, or ``None``.

    An absolute path only counts when it points inside the project; a
    path outside the tree (``/usr/lib/…``, ``../elsewhere``) is not a
    path of this repository and is dropped.
    """
    token = str(token or "").strip()
    if not token:
        return None
    if token.startswith("/"):
        try:
            return Path(token).resolve().relative_to(
                Path(root).resolve(),
            ).as_posix()
        except (ValueError, OSError, RuntimeError):
            return None
    parts = [part for part in token.split("/") if part]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _exists_under(root: Path, rel: str) -> bool:
    """Does ``rel`` name a file that really exists inside ``root``?"""
    try:
        target = (Path(root) / rel).resolve()
        target.relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return target.exists()


def _same_path(left: str, right: str) -> bool:
    """Do two repo paths name the same file?

    Equal — or one is the other's path-segment suffix
    (``backend/verification_executor.py`` vs ``verification_executor.py``).
    The suffix rule is deliberately lenient: a verdict that names the
    right file by its bare name must not be punished.
    """
    first, second = left.lower(), right.lower()
    if first == second:
        return True
    return first.endswith("/" + second) or second.endswith("/" + first)


def _flatten_into(value: Any, out: List[str]) -> None:
    """Collect every string under ``value`` into ``out``."""
    if value is None:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _flatten_into(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _flatten_into(item, out)
    else:
        out.append(str(value))


def _flatten_text(value: Any) -> str:
    """Flatten a plan field (string / list / dict) into plain text."""
    parts: List[str] = []
    _flatten_into(value, parts)
    return "\n".join(parts)


def _verdict_text(verdict: Dict[str, Any]) -> str:
    """The verdict's answer: its reasons, its evidence and its citations.

    The framework's own attachments are **not** part of the answer: the
    ``evidence_command`` output is supporting material produced by the
    framework, and ``evidence_check`` is this module's own bookkeeping.
    Only what the sub-agent said counts.
    """
    parts: List[str] = []
    _flatten_into(verdict.get("reasons"), parts)
    evidence = verdict.get("evidence")
    if isinstance(evidence, dict):
        for key, value in evidence.items():
            if key in ("evidence_command", "evidence_check"):
                continue
            _flatten_into(value, parts)
    else:
        _flatten_into(evidence, parts)
    _flatten_into(verdict.get("citations"), parts)
    return "\n".join(parts)


def _project_root(
    project_dir: Optional[Path], vp: Dict[str, Any],
) -> Optional[Path]:
    """The tree the paths are resolved against — ``None`` when unknown."""
    raw = project_dir or (vp or {}).get("project_root")
    if not raw:
        return None
    try:
        root = Path(str(raw)).expanduser()
    except (TypeError, ValueError):
        return None
    if not root.is_dir():
        return None
    return root


def _expected_paths(vp: Dict[str, Any], root: Path) -> List[str]:
    """The repo paths the verification point names, and that really exist."""
    paths: List[str] = []
    for token in _path_words(_flatten_text(vp.get("expected_result"))):
        rel = _repo_relative(token, root)
        if rel is None or rel in paths:
            continue
        if not _exists_under(root, rel):
            continue
        paths.append(rel)
    return paths


def _paths_in_text(text: str, root: Path) -> List[str]:
    """The repo paths a piece of text names."""
    paths: List[str] = []
    for token in _path_words(text):
        rel = _repo_relative(token, root)
        if rel is None or rel in paths:
            continue
        paths.append(rel)
    return paths


def _verdict_paths(verdict: Dict[str, Any], root: Path) -> List[str]:
    """The repo paths the answer talks about."""
    return _paths_in_text(_verdict_text(verdict), root)


def _join_paths(paths: List[str]) -> str:
    """Name the paths in a reason — the first few, then "等"."""
    shown = paths[:_PATH_LIST_LIMIT]
    text = "、".join(shown)
    if len(paths) > len(shown):
        text += " 等"
    return text


def read_citation_files(plan_dir: Path, vp_id: str) -> List[str]:
    """The repo paths the verdict's own citations name.

    A verdict's prose is not the only place a review shows *what it
    read*: ``citations.json`` is the review's file list.  Both count as
    the answer — a review that cited the file the verification point is
    about is on subject, even when its prose never repeats the path.
    """
    artifact = artifact_dir_for(plan_dir, vp_id) / CITATIONS_FILENAME
    payload = _read_json(artifact)
    if not isinstance(payload, dict):
        return []
    citations = payload.get("citations")
    if not isinstance(citations, list):
        return []
    files: List[str] = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        rel = str(citation.get("file") or "").strip()
        if rel and rel not in files:
            files.append(rel)
    return files


def verdict_subject_mismatch(
    vp: Dict[str, Any],
    verdict: Dict[str, Any],
    project_dir: Optional[Path] = None,
    citation_files: Optional[List[str]] = None,
) -> Optional[str]:
    """Is this verdict about a different question than the one asked?

    Returns the reason when the verdict demonstrably answers another
    question, and ``None`` when that cannot be told — which is the
    common case, because the rule is deliberately narrow.
    """
    if not isinstance(vp, dict) or not isinstance(verdict, dict):
        return None
    method = str(
        vp.get("verification_method") or vp.get("method") or ""
    ).strip()
    if method not in EVIDENCE_REQUIRED_METHODS:
        return None
    root = _project_root(project_dir, vp)
    if root is None:
        return None

    expected = _expected_paths(vp, root)
    if not expected:
        # The point names no file of this tree — there is no subject to
        # compare the answer against.
        return None

    # One text, two readings: the paths the answer names and the bare
    # file names it mentions must come from the *same* answer, or the
    # two rules could disagree about what the verdict said.
    text = _verdict_text(verdict)

    answered = _paths_in_text(text, root)
    for cited in citation_files or []:
        rel = _repo_relative(str(cited), root)
        if rel and rel not in answered:
            answered.append(rel)
    if not answered:
        # The answer names no file either — a pure-text review is not
        # evidence of answering the wrong question.
        return None

    # Judging (5): the answer may call the point's own file by its bare
    # name (``verification_executor.py:3732``) — that is the same path.
    # Only the point's own file is answered this way: a bare name the
    # verdict mentions in passing (``checkpoints.json`` and friends)
    # never becomes a path of its own, so it can neither rescue a
    # verdict about another question nor manufacture a mismatch.
    bare = _bare_file_names(text)

    for one in expected:
        if Path(one).name.lower() in bare:
            return None
        for other in answered:
            if _same_path(one, other):
                return None

    return (
        f"本验证点点名 {_join_paths(expected)}，"
        f"而结论只涉及 {_join_paths(answered)}"
        " —— 裁决回答的不是这道验证点"
    )


def apply_subject_check_to_verdict(
    verdict: Dict[str, Any],
    vp: Dict[str, Any],
    project_dir: Optional[Path] = None,
    citation_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Downgrade a PASSED that answered a different question.

    **Only PASSED is inspected** — the same discipline as
    :func:`apply_to_verdict`: this check refuses to let a wrong pass
    through, and it never touches a failure (a FAILED stays FAILED, a
    SKIPPED stays SKIPPED).  Nothing here turns a failure into a pass,
    and nothing here adds a repair semantic: the only thing it does is
    say that this verdict did not answer this question — the point can
    be looked at again in the next round.

    Returns ``verdict`` unchanged when the verdict is not PASSED or when
    the subject cannot be told.
    """
    if not isinstance(verdict, dict):
        return verdict
    if str(verdict.get("status")) != "PASSED":
        return verdict

    reason = verdict_subject_mismatch(
        vp, verdict, project_dir, citation_files,
    )
    if not reason:
        return verdict

    method = str(
        (vp or {}).get("verification_method")
        or (vp or {}).get("method")
        or "code_review"
    ).strip()
    checked = dict(verdict)
    checked["reasons"] = list(verdict.get("reasons") or []) + [reason]
    checked["status"] = "FAILED"
    checked["actual_result"] = (
        f"{method} 判定为 PASSED，但结论回答的不是这道验证点，改判 FAILED："
        + reason
    )
    evidence = verdict.get("evidence")
    evidence = dict(evidence) if isinstance(evidence, dict) else (
        {"original_evidence": evidence}
    )
    evidence["subject_check"] = {"reason": reason}
    checked["evidence"] = evidence
    return checked


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

    The command runs **in the project dir**, and ``$PWD`` is set to that
    same directory rather than inherited.  A shell answers ``pwd`` with
    ``$PWD`` verbatim when the variable is set -- it does not re-derive it
    from the working directory the process was handed -- so an inherited
    relative ``$PWD`` (``.``, which is what a parent process running from
    its own directory exports) makes the command report a directory that is
    not the project dir at all.  The evidence would then describe somewhere
    else, which is the one thing an evidence command must not do.  The
    variable is normalised here rather than in ``run_bounded``, which is
    shared by every caller and has no opinion about where its command is
    supposed to run.
    """
    result = EvidenceCommandResult(command=command)
    start = time.monotonic()
    try:
        completed = run_bounded(
            command,
            cwd=str(project_dir),
            env=dict(os.environ, PWD=str(project_dir)),
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


# ---------------------------------------------------------------------------
# Working-tree fingerprint — is the tree I am about to read still mine?
# ---------------------------------------------------------------------------

#: Seconds either git probe may take before it is given up on.  The
#: fingerprint is a safety net, not a gate: a slow repository must not
#: be able to hold up a verification round, and a tree that cannot be
#: interrogated in time is handled exactly like a tree that is not a
#: repository at all — no fingerprint, no claim of a change.
TREE_FINGERPRINT_TIMEOUT_SECONDS = 10


def _git_output(args: List[str], project_dir: Path) -> Optional[str]:
    """Run ``git <args>`` in ``project_dir`` and return its stdout.

    ``None`` when git is not installed, the directory is not a
    repository, the command takes longer than
    :data:`TREE_FINGERPRINT_TIMEOUT_SECONDS`, or git exits non-zero.

    Never raises: the caller degrades to "no fingerprint" and the
    evidence check carries on as it did before.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=TREE_FINGERPRINT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def tree_fingerprint(project_dir: Path) -> Optional[str]:
    """A content-based fingerprint of the working tree at ``project_dir``.

    The fingerprint is ``"<HEAD sha>:<sha256 of git status --porcelain>"``:

      * **HEAD** — which commit the tree sits on.  A commit moves it even
        when the tree is clean again afterwards.
      * **porcelain** — the status of the tree relative to that commit,
        hashed.  This is what catches the mutation that matters for an
        evidence check: a file edited in the worktree, or a file that
        appeared, *after* the round recorded its citations.

    It is deliberately content-based and not mtime-based: touching a
    file without changing it does not move the fingerprint, so a
    checkout that merely refreshed timestamps is not mistaken for a
    concurrent edit.

    A **clean** tree still has a fingerprint — an empty porcelain output
    is a fact about the tree, not the absence of one.  Only a tree that
    cannot be interrogated at all (no git, not a repository, no HEAD
    yet, git too slow, git failed) yields ``None``.
    """
    project_dir = Path(project_dir)

    head = _git_output(["rev-parse", "HEAD"], project_dir)
    if head is None:
        return None
    head_sha = head.strip()
    if not head_sha:
        return None

    # ``--no-optional-locks`` keeps ``git status`` from taking the
    # optional index lock to refresh the stat cache.  This probe runs
    # against a worktree other processes may be writing to, and taking
    # a lock on their behalf is exactly the interference it is meant to
    # detect.  It changes what git *writes*, not what it *reports*.
    status = _git_output(
        ["--no-optional-locks", "status", "--porcelain"], project_dir
    )
    if status is None:
        return None

    status_digest = hashlib.sha256(status.encode("utf-8")).hexdigest()
    return f"{head_sha}:{status_digest}"


def tree_mutated(project_dir: Path, baseline: Optional[str]) -> bool:
    """Has the working tree at ``project_dir`` moved away from ``baseline``?

    ``baseline`` is a fingerprint taken earlier (:func:`tree_fingerprint`).

    Returns ``False`` — "cannot tell, so assume it did not move" — when
    there is no baseline to compare against, or when the current
    fingerprint cannot be computed (no git, not a repository, git timed
    out).  A caller that cannot establish a baseline keeps the
    behaviour it had before the fingerprint existed; it does not start
    refusing work on the strength of a missing measurement.

    ``True`` means the tree moved: either HEAD changed or the status
    changed.  That is the signal the caller needs to hold an evidence
    check until the concurrent writer is done.
    """
    if not baseline:
        return False
    current = tree_fingerprint(project_dir)
    if current is None:
        return False
    return current != baseline
