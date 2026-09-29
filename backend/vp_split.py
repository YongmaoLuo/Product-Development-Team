"""VP 拆分 —— 修复期的"判断者"把过大的验证点拆成可独立执行的子 VP。

背景 (2026-09-14)
-----------------
有些失败并不是产品缺陷，而是**当前这个 VP 太大**：它跑得慢、容易超时，
失败面又分散在若干测试目录里，用一个巨大的 VP 表达既慢也无法定位。
对这类 VP，正确的产出不是"一个可执行的修复任务"，而是把它按范围拆成
若干子 VP，下一轮验证去跑子 VP。

于是修复任务生成阶段同时承担一个判断者的角色：对每个失败 VP 决定
是生成修复任务，还是拆分它。

现实证据 (该计划 VP-023):
  * ``pytest tests/ -v`` 跑整个套件 ≈ 18 分钟 / 2200+ 个测试;
  * 子 agent 重试预算内每次 attempt 都全量重跑，单 VP 单轮烧掉
    ~100 分钟;
  * 失败是真实的产品缺陷，但失败面分散在若干测试目录里，用一个
    巨大的 VP 表达既慢又无法定位。

因此轮末的修复生成阶段多了一个判断: 对每个失败 VP，决定

  * ``repair`` —— 生成可执行的修复任务（原行为，代码/配置缺陷）;
  * ``split``  —— 不生成修复任务，把这个 VP 按测试范围拆成若干
    子 VP，下一轮验证跑子 VP。

分工遵循本仓库既有的 **LLM 只产内容 / 本地代码盖章** 约定
(见 ``repair_generator.RepairTaskAssembler``):

  * :class:`VpSplitJudge` —— 本地门禁决定"谁有资格被拆"(仅
    ``automated_test`` + pytest + 宽范围命令)，LLM 只在候选里选
    action 和拆分维度。LLM 调用失败 → 全部回退 ``repair``（保守，
    绝不搁浅）。
  * :class:`VpSplitter` —— 纯本地拆分执行: ``pytest --collect-only``
    枚举测试文件 → 按目录/分块分组 → 子 VP 写回
    ``verification_plan.json``，父 VP 标记 ``superseded_by``；同时把
    父 VP 的 ``SPLIT`` verdict 持久化进 state.db，让下一轮的 resume
    跳过父 VP、只跑子 VP。

为什么不用 ``verification_split.py`` / ``verification_split_llm.py``:
那两个模块解决的是 **轮内超时拆分** —— 一个 VP 在一次执行中超时，
就地按 ``expected_result`` 分号/LLM 拆成子 VP 并**当场并行跑完**，
不写回计划、下一轮也不认。本模块解决的是 **跨轮拆分**: 拆分是轮末
的一项持久化决策，子 VP 在**下一轮**验证里执行、带自己的 verdict。
两者作用域不同，故并存。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACTION_REPAIR = "repair"
ACTION_SPLIT = "split"

#: 每轮最多拆分多少个 VP —— 防止一轮里把整张验证计划拆碎。
DEFAULT_MAX_SPLITS_PER_ROUND = 3

#: 拆分深度上限。``VP-023`` 拆出 ``VP-023-1``（深度 1）；深度 1 的
#: 子 VP 若仍然失败且依旧"过大"，可以再拆一次（深度 2）；深度 2 再
#: 失败就只能走 repair —— 否则拆分本身会成为无限循环。
DEFAULT_MAX_SPLIT_DEPTH = 2

#: 单个父 VP 最多拆出多少个子 VP。
MAX_CHILDREN = 6

#: 允许的拆分维度 (LLM 只能从这几个里选)。
SPLIT_HINTS = ("by_directory", "by_file_group")

#: 判定"宽范围"的 pytest 作用域: 这些根路径说明命令跑的是整个套件
#: 或整个目录树，而不是一个精挑细选的测试文件/用例。
_BROAD_SCOPE_ROOTS = ("tests", "test", "backend/tests", "tests/")

_SPLIT_JUDGE_SYSTEM_PROMPT = """你是一位资深验证架构师。给你若干个失败的验证点 (VP)，\
请判断每一个应该「修复」还是「拆分」。

判断标准:
- **repair** — 失败由具体缺陷引起 (代码 bug、配置缺失、test_command 写错、\
依赖问题)，修复面明确，一个修复任务就能推进。
- **split** — VP 本身太大: test_command 跑的是整个测试套件/整个目录树，\
单次运行时间很长 (十几分钟以上)、容易超时，或者失败面分散在多个不相关的\
子模块里。这类 VP 应该按测试范围拆成若干子 VP 分别执行 —— 拆分后每个子 VP \
可独立跑、独立判、独立修。

约束:
1. 只对候选 VP 做判断，不要新增 VP，不要改 id。
2. split_hint 只能是 "by_directory" (按测试目录分组) 或 "by_file_group" (按文件均匀分块)。
3. 拿不准的时候选 repair。
4. 严格输出 JSON，不要 markdown 代码块标记:

{
  "decisions": [
    {"vp_id": "VP-023", "action": "split", "split_hint": "by_directory",
     "reason": "整个 tests/ 套件 18 分钟，失败分散在 visual/raw_payload 等多个目录"}
  ]
}
"""


# ---------------------------------------------------------------------------
# Small helpers (pure)
# ---------------------------------------------------------------------------

def _pytest_scope(command: str) -> List[str]:
    """Extract the path arguments a pytest command selects.

    Only the *selection* arguments are returned (tokens before the first
    flag, i.e. not starting with ``-``). Shell operators and everything
    after them are ignored: ``pytest tests/ -v 2>&1 | tee x.log`` →
    ``["tests/"]``.
    """
    if not isinstance(command, str):
        return []
    tokens = command.strip().split()
    # Drop the invocation itself (pytest / python -m pytest).
    if tokens and tokens[0].endswith("pytest"):
        tokens = tokens[1:]
    else:
        return []
    scope: List[str] = []
    for token in tokens:
        if token.startswith("-"):
            break
        if token in ("|", "&&", ";", ">", "2>&1"):
            break
        scope.append(token)
    return scope


def is_pytest_command(command: str) -> bool:
    """``True`` when the command invokes pytest as the primary binary."""
    if not isinstance(command, str):
        return False
    head = command.strip().split()
    return bool(head) and head[0].endswith("pytest")


def is_broad_scope(command: str) -> bool:
    """``True`` when the command's selection is a whole tree, not a slice.

    Broad means: a pytest command whose scope is a directory
    (``tests`` / ``tests/``) or a bare ``tests/x.py``-style path that is
    really the suite root, AND no ``-k`` filter narrows it. A command
    that already targets one file (``pytest tests/test_login.py -v``) or
    one node id (``tests/test_a.py::test_x``) is NOT broad — splitting it
    would produce children with nothing to gain. Non-pytest commands are
    never broad.
    """
    if not is_pytest_command(command):
        return False
    scope = _pytest_scope(command)
    if not scope:
        # No path argument at all → the whole configured suite.
        return "-k" not in command
    if "-k" in command:
        return False
    for entry in scope:
        if "::" in entry:
            # Node-id selection is as narrow as it gets.
            continue
        if entry.rstrip("/").startswith(_BROAD_SCOPE_ROOTS) or entry.endswith("/"):
            return True
        if entry.endswith(".py") and "/" not in entry.rstrip("/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

class VpSplitJudge:
    """决定每个失败 VP 走 ``repair`` 还是 ``split``。

    两道门:

      1. **本地门禁** (:meth:`eligible`) —— 只有 ``automated_test`` +
         pytest 命令 + 宽范围作用域 + 未超过拆分深度的 VP 才是候选。
         LLM 无权把非候选变成 split。
      2. **LLM 选择** (:meth:`decide`) —— 在候选里选 action 与维度。
         调用失败/解析失败/超时 → 全部候选回退 ``repair``。
    """

    def __init__(self, coding_tool: Any, plan_dir: Path,
                 max_splits_per_round: int = DEFAULT_MAX_SPLITS_PER_ROUND,
                 max_depth: int = DEFAULT_MAX_SPLIT_DEPTH):
        self.coding_tool = coding_tool
        self.plan_dir = Path(plan_dir)
        self.max_splits_per_round = max_splits_per_round
        self.max_depth = max_depth

    # -- gate ---------------------------------------------------------

    def plan_entry(self, vp_id: str) -> Optional[Dict[str, Any]]:
        """The VP's entry in ``verification_plan.json`` (or ``None``)."""
        for entry in self._plan_entries():
            if str(entry.get("id")) == str(vp_id):
                return entry
        return None

    def _plan_entries(self) -> List[Dict[str, Any]]:
        path = self.plan_dir / "verification_plan.json"
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
        entries = data.get("verification_points") or data.get("vps") or []
        return [e for e in entries if isinstance(e, dict)]

    def split_depth(self, vp_id: str) -> int:
        """Depth of a VP in the split tree: ``VP-023`` → 0,
        ``VP-023-1`` → 1 (explicit ``split_depth`` field wins when the
        splitter wrote one)."""
        entry = self.plan_entry(vp_id)
        if entry is not None:
            try:
                return int(entry.get("split_depth", 0) or 0)
            except (TypeError, ValueError):
                pass
        # Fall back to the id shape when there is no plan entry to read.
        return max(0, len(re.findall(r"-\d+$", str(vp_id))))

    def eligible(self, vp_id: str, method: str, test_command: str) -> bool:
        """Can this VP be split at all? (local guard, no LLM)"""
        if str(method) != "automated_test":
            return False
        if not is_pytest_command(test_command):
            return False
        if not is_broad_scope(test_command):
            return False
        if self.split_depth(vp_id) >= self.max_depth:
            return False
        return True

    # -- decision -----------------------------------------------------

    def decide(
        self, candidates: Sequence[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """``{vp_id: {"action", "split_hint", "reason"}}`` for every candidate.

        ``candidates`` carries the shapes produced by
        ``verification.verification_report_reader.extract_failed_vps_with_paths``
        (``id`` / ``title`` / ``actual_result_summary`` /
        ``evidence_paths`` ...) plus ``method`` and ``test_command``.

        Failure handling: any problem (no coding tool, LLM error, bad
        JSON, empty decisions) yields ``repair`` for every candidate —
        the auto-loop then takes the ordinary repair path, exactly as if
        this judge did not exist.
        """
        decisions: Dict[str, Dict[str, Any]] = {
            str(c.get("id")): {
                "action": ACTION_REPAIR, "split_hint": "", "reason": "default",
            }
            for c in candidates if c.get("id")
        }
        eligible = [c for c in candidates if self.eligible(
            str(c.get("id", "")),
            str(c.get("method", c.get("verification_method", ""))),
            str(c.get("test_command", "")),
        )]
        if not eligible or self.coding_tool is None:
            return decisions
        try:
            response = self.coding_tool.query_json(
                prompt=self._build_prompt(eligible),
                system_instruction=_SPLIT_JUDGE_SYSTEM_PROMPT,
                timeout=600,
            )
        except Exception as exc:  # noqa: BLE001 — conservative fallback
            print(f"[VpSplitJudge] LLM call failed, all candidates → repair: {exc}")
            return decisions
        parsed = self._parse(response, {str(c.get("id")) for c in eligible})
        granted = 0
        for vp_id, decision in parsed.items():
            if decision["action"] == ACTION_SPLIT:
                if granted >= self.max_splits_per_round:
                    decision["action"] = ACTION_REPAIR
                    decision["reason"] = (
                        f"split budget exhausted "
                        f"({self.max_splits_per_round}/round)"
                    )
                else:
                    granted += 1
            decisions[vp_id] = decision
        return decisions

    def _build_prompt(self, candidates: Sequence[Dict[str, Any]]) -> str:
        parts = [
            f"计划目录: {self.plan_dir}",
            "",
            f"共 {len(candidates)} 个候选失败 VP：",
            "",
        ]
        for i, vp in enumerate(candidates, start=1):
            vp_id = vp.get("id")
            parts.append(f"--- 候选 {i}: {vp_id} ---")
            if vp.get("title"):
                parts.append(f"title: {vp['title']}")
            parts.append(f"test_command: {vp.get('test_command', '')}")
            if vp.get("actual_result_summary"):
                parts.append(
                    f"actual_result_summary: {vp['actual_result_summary']}"
                )
            if vp.get("evidence_summary"):
                parts.append(f"evidence_summary: {vp['evidence_summary']}")
            if vp.get("evidence_paths"):
                parts.append("evidence_paths (可用 Read 读取):")
                parts.extend(f"  - {p}" for p in vp["evidence_paths"])
            if vp.get("prior_failure_rounds"):
                parts.append(
                    f"prior_failure_rounds: {vp['prior_failure_rounds']}"
                )
            parts.append("")
        parts.append(
            "请对每个候选输出 repair 或 split 的判断 (纯 JSON，不要 "
            "markdown 代码块标记)。"
        )
        return "\n".join(parts)

    @staticmethod
    def _parse(
        response: Any, eligible_ids: set,
    ) -> Dict[str, Dict[str, Any]]:
        """Normalise the LLM reply; drop anything that is not a legal
        split decision for an eligible candidate."""
        if not isinstance(response, dict):
            return {}
        raw = response.get("decisions")
        if not isinstance(raw, list):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            vp_id = str(item.get("vp_id", "") or "")
            if not vp_id or vp_id not in eligible_ids:
                continue
            action = str(item.get("action", "") or "").strip().lower()
            if action not in (ACTION_REPAIR, ACTION_SPLIT):
                continue
            hint = str(item.get("split_hint", "") or "").strip()
            if action == ACTION_SPLIT and hint not in SPLIT_HINTS:
                # Unknown dimension: fall back to directory grouping
                # rather than dropping the split entirely.
                hint = "by_directory"
            out[vp_id] = {
                "action": action,
                "split_hint": hint,
                "reason": str(item.get("reason", "") or "")[:500],
            }
        return out


# ---------------------------------------------------------------------------
# Splitter
# ---------------------------------------------------------------------------

class VpSplitter:
    """把一个大 VP 拆成若干子 VP 并持久化。

    持久化面:

      * ``verification_plan.json`` —— 子 VP 条目 + 父 VP 的
        ``superseded_by`` 标记（执行器 fresh-init 的 VP 全集来源）;
      * state.db ``plan_verification.verdicts`` —— 父 VP 的
        ``SPLIT`` verdict（resume 时被 ``_backfill_index_lists_from_verdicts``
        归入跳过集，下一轮只跑子 VP）。
    """

    def __init__(self, plan_dir: Path, project_dir: Path):
        self.plan_dir = Path(plan_dir)
        self.project_dir = Path(project_dir)

    @property
    def plan_file(self) -> Path:
        return self.plan_dir / "verification_plan.json"

    # -- plan file IO -------------------------------------------------

    def _load_plan(self) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
        with open(self.plan_file, encoding="utf-8") as f:
            data = json.load(f)
        key = "verification_points"
        entries = data.get(key)
        if not isinstance(entries, list):
            key = "vps"
            entries = data.get(key)
        if not isinstance(entries, list):
            return data, "verification_points", []
        return data, key, entries

    def _write_plan(self, data: Dict[str, Any]) -> None:
        """Atomic rewrite (tmp + rename) so a crash cannot leave a
        half-written plan the executor would misread."""
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.plan_dir), prefix=".verification_plan.", suffix=".tmp"
        )
        try:
            os.close(fd)
            Path(tmp).write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            Path(tmp).replace(self.plan_file)
        except Exception:
            try:
                Path(tmp).unlink()
            except OSError:
                pass
            raise

    # -- grouping (pure) ----------------------------------------------

    @staticmethod
    def group_test_files(
        files: Sequence[str], hint: str = "by_directory",
    ) -> List[Tuple[str, List[str]]]:
        """Group collected test files into ``[(label, [files]), ...]``.

        ``by_directory`` groups by the first path component **below the
        suite root** (``tests/visual/test_x.py`` → ``visual``;
        ``tests/test_x.py`` → ``(root)``), merging into at most
        :data:`MAX_CHILDREN` groups. ``by_file_group`` chops the sorted
        file list round-robin into 3 buckets. Either way, fewer than two
        groups is useless — the caller then declines to split.
        """
        ordered = sorted({str(f) for f in files if str(f).strip()})
        if not ordered:
            return []
        if hint == "by_file_group":
            buckets = min(3, len(ordered))
            groups: List[Tuple[str, List[str]]] = []
            for i in range(buckets):
                subset = ordered[i::buckets]
                if subset:
                    groups.append((f"chunk{i + 1}", subset))
            return groups

        # by_directory
        root = VpSplitter._suite_root(ordered)
        by_label: Dict[str, List[str]] = {}
        for path in ordered:
            rel = path[len(root):].lstrip("/") if root and path.startswith(root) else path
            parts = [p for p in rel.split("/") if p]
            label = parts[0] if len(parts) > 1 else "(root)"
            by_label.setdefault(label, []).append(path)
        if len(by_label) < 2:
            return []
        items = sorted(
            by_label.items(), key=lambda kv: (-len(kv[1]), kv[0])
        )
        if len(items) > MAX_CHILDREN:
            head = items[: MAX_CHILDREN - 1]
            tail_files: List[str] = []
            for _, subset in items[MAX_CHILDREN - 1:]:
                tail_files.extend(subset)
            items = head + [("(merged)", sorted(tail_files))]
        return [(label, subset) for label, subset in items]

    @staticmethod
    def _suite_root(files: Sequence[str]) -> str:
        """Longest common *directory* prefix of the file list."""
        if not files:
            return ""
        split = [f.split("/") for f in files]
        common: List[str] = []
        for parts in zip(*split):
            if len(set(parts)) == 1:
                common.append(parts[0])
            else:
                break
        # Never treat the file name itself as the root.
        if len(common) == len(split[0]):
            common = common[:-1]
        return "/".join(common)

    # -- collection ---------------------------------------------------

    def collect_test_files(
        self, scope: Sequence[str], timeout: int = 300,
    ) -> List[str]:
        """Enumerate test files via ``pytest --collect-only -q``.

        Never raises: any failure (timeout, non-zero exit, unparseable
        output) returns ``[]`` and the caller declines to split.
        """
        args = ["pytest", "--collect-only", "-q", *scope]
        try:
            proc = subprocess.run(
                args,
                cwd=str(self.project_dir),
                capture_output=True, text=True, timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[VpSplitter] collect-only failed: {exc}")
            return []
        files: List[str] = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if "::" not in line:
                continue
            path = line.split("::", 1)[0].strip()
            if path and path.endswith(".py") and path not in files:
                files.append(path)
        return files

    # -- split --------------------------------------------------------

    def split(
        self, vp_id: str, hint: str = "by_directory",
        *, reason: str = "",
    ) -> Optional[List[Dict[str, Any]]]:
        """Split ``vp_id``; returns the child VP dicts (``None`` on any
        refusal). Idempotent: a VP already marked ``superseded_by``
        returns its existing children without writing anything.
        """
        try:
            data, key, entries = self._load_plan()
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[VpSplitter] cannot read plan for {vp_id}: {exc}")
            return None

        parent = next(
            (e for e in entries if str(e.get("id")) == str(vp_id)), None
        )
        if parent is None:
            return None
        existing = parent.get("superseded_by")
        if isinstance(existing, list) and existing:
            return [e for e in entries if str(e.get("id")) in set(existing)]

        command = str(parent.get("test_command", "") or "")
        scope = _pytest_scope(command)
        files = self.collect_test_files(scope)
        groups = self.group_test_files(files, hint)
        if len(groups) < 2:
            print(
                f"[VpSplitter] {vp_id}: only {len(groups)} group(s) from "
                f"{len(files)} collected file(s) — declining to split"
            )
            return None

        children: List[Dict[str, Any]] = []
        for index, (label, subset) in enumerate(groups, start=1):
            child_id = f"{vp_id}-{index}"
            token = re.sub(r"-", "_", child_id).lower()
            child = {
                k: v for k, v in parent.items()
                if k not in ("superseded_by", "split_reason", "status")
            }
            child.update({
                "id": child_id,
                "parent_vp_id": str(vp_id),
                "original_vp_id": str(vp_id),
                "title": f"{parent.get('title', vp_id)}（拆分{index}: {label}）",
                "test_command": (
                    f"pytest {' '.join(subset)} -v "
                    f"--junit-xml=/tmp/{token}_junit.xml "
                    f"2>&1 | tee /tmp/{token}_progress.log"
                ),
                "split_depth": int(parent.get("split_depth", 0) or 0) + 1,
                "split_label": label,
                "split_reason": reason,
            })
            children.append(child)

        parent["superseded_by"] = [c["id"] for c in children]
        parent["split_reason"] = reason
        # Insert children right after the parent so plan order stays
        # readable (and the layer builder keeps insertion order).
        parent_idx = entries.index(parent)
        entries[parent_idx + 1: parent_idx + 1] = children
        self._write_plan(data)
        return children

    # -- DB persistence -----------------------------------------------

    @staticmethod
    def persist_split(
        verif_repo: Any, plan_id: str, parent_id: str,
        children: Sequence[Dict[str, Any]], reason: str = "",
    ) -> bool:
        """Record the parent's ``SPLIT`` verdict in state.db.

        The verdict is what makes the NEXT round's resume skip the
        parent (``_backfill_index_lists_from_verdicts`` buckets SPLIT
        with SKIPPED) while the children — already in
        ``verification_plan.json`` — become the run universe. Without
        this row the executor would re-run the giant parent forever.

        Returns ``True`` when the row was written. Never raises: a repo
        without the plan row (KeyError) or any SQLite hiccup is logged
        and reported as ``False`` — the plan-file split still happened,
        so the caller can decide how loud to be.
        """
        if verif_repo is None or not plan_id:
            return False
        child_ids = [str(c.get("id")) for c in children if c.get("id")]
        verdict = {
            "vp_id": str(parent_id),
            "status": "SPLIT",
            "reasons": [
                f"split into {len(child_ids)} sub-VP(s): "
                f"{', '.join(child_ids)}",
                *([f"reason: {reason}"] if reason else []),
            ],
            "evidence": {"child_vp_ids": child_ids},
        }
        try:
            verif_repo.append_verdict(plan_id, verdict)
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort persistence
            print(
                f"[VpSplitter] persist_split failed plan={plan_id} "
                f"vp={parent_id}: {exc}"
            )
            return False
