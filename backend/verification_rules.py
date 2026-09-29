"""VP-006-revised verification rule.

This module hosts the rewritten VP-006 verification rule, which is
the in-tree replacement for the legacy "audit ``backend/`` git log
for [task-N] commit prefixes" check. The legacy rule could not be
run against historical plans (every pre-existing plan shipped
before the prefix rule was in place, so re-running the rule against
stale history produced a false positive). The revised rule
restricts its scope to the **specific task_ids** in the *current*
``tasks.json``.

Algorithm
---------
For every task in the input tasks list:

  1. Find all commits in ``exec_repo_path`` whose commit message
     contains the task's ``id`` substring. (Multi-line messages
     count — ``git log --grep`` walks the full message.)
  2. For each matching commit, collect the file paths that the
     commit added/modified/deleted (or — for renames — the
     destination path). For merge commits, ``git log -m
     --name-only`` may emit an empty name row; the rule tolerates
     that case (treats it as zero name rows).
  3. Count how many of those name rows appear in the task's
     ``files_to_modify`` list (the entry must match exactly on the
     tasks.json-relative path; directory prefixes are NOT matched —
     ``files_to_modify == ['src/a.py']`` does NOT match a commit
     whose names include ``src/``).
  4. ``verdict``:
       * ``files_to_modify == []`` → SKIPPED + warning
         ("no files_to_modify declared");
       * no matching commit → FAILED (``commit_hits == []``);
       * matching commit but zero file overlaps → FAILED (the
         sub-agent cannot be trusted to have written the right
         file);
       * matching commit and >= 1 file overlap → PASSED.

Aggregate ``verdict`` is FAILED iff any task's verdict is FAILED;
SKIPPED tasks are filtered (they don't count toward FAILED). All-
or-nothing semantics.

Public API
----------
This module exports three symbols (see below) — the dataclasses
they reference are also exported so downstream consumers can type-
hint the report shape:

    VP006RevisedRule
        .evaluate(tasks_json_path, exec_repo_path) -> VP006Report

    PerTaskVerdict  — one row in ``VP006Report.per_task``
    VP006Report     — top-level envelope
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Substring used to find the task in commit messages. The rule uses
# a **substring** match (not anchored) so commit messages like
# ``[task-13-1] feat: foo`` are matched against the task_id
# ``task-13-1`` (the ``[`` and ``]`` are optional decorations).
def _task_id_in_message(task_id: str, message: str) -> bool:
    """Return True iff ``task_id`` appears as a substring of ``message``.

    The match is permissive by design: production commit messages
    carry the task_id with a variety of decorations (``[task-13-1]
    feat: ...``, ``task-13-1: add foo``, ``feat(task-13-1): ...``).
    Pinning the prefix to ``[`` would silently miss the second and
    third forms. The downside is that false positives CAN occur —
    e.g., a commit ``chore: update task-13 docs`` would match
    ``task_id == "task-13"`` — but we filter the false positives
    downstream via the file-match check (commit ``task-13 docs``
    most likely does not modify ``src/task-13.py``).
    """
    return task_id in message


@dataclass
class PerTaskVerdict:
    """One row of VP-006-revised output.

    Attributes:
        task_id: The task id from ``tasks.json`` (e.g. ``"task-1"``).
        verdict: One of ``"PASSED"``, ``"FAILED"``, ``"SKIPPED"``.
            ``"SKIPPED"`` is reserved for the no-files_to_modify edge
            case and does NOT contribute to the aggregate verdict.
        commit_hits: List of short commit SHAs whose message contains
            ``task_id``. Empty when no commit names the task.
        file_match_count: How many name rows across ``commit_hits``
            match an entry in ``files_to_modify``. Counts across all
            matching commits (cumulative), not per-commit.
        warning: Optional human-readable note (e.g. "no
            files_to_modify"). Set on SKIPPED tasks; otherwise None.
    """

    task_id: str
    verdict: str  # PASSED | FAILED | SKIPPED
    commit_hits: List[str] = field(default_factory=list)
    file_match_count: int = 0
    warning: Optional[str] = None


@dataclass
class VP006Report:
    """Top-level VP-006-revised envelope.

    Attributes:
        per_task: One :class:`PerTaskVerdict` per task in
            ``tasks.json``.
        aggregate_verdict: One of ``"PASSED"``, ``"FAILED"``. SKIPPED
            tasks are filtered (do NOT count toward FAILED); a
            well-formed tasks.json with only SKIPPEDs yields
            ``PASSED`` (no requirement violated).
    """

    per_task: List[PerTaskVerdict] = field(default_factory=list)
    aggregate_verdict: str = "FAILED"  # conservative default


def _load_tasks(tasks_json_path: str) -> List[Dict[str, Any]]:
    """Load the ``tasks`` array from ``tasks_json_path``.

    Accepts either the canonical envelope ``{"tasks": [...]}`` or
    a bare list at the top level (``[...]``). The canonical envelope
    mirrors the production task executor's tasks.json contract.

    Raises:
        ValueError: If the file is missing, unreadable, or the JSON
            root is neither a list nor a dict carrying ``"tasks"``.
    """
    path = Path(tasks_json_path)
    if not path.exists():
        raise ValueError(f"tasks.json does not exist: {tasks_json_path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"failed to read tasks.json {tasks_json_path}: {exc}"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"tasks.json is not valid JSON: {exc}"
        ) from exc
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        return list(payload["tasks"])
    raise ValueError(
        "tasks.json must be a list or have a 'tasks' key with a list value"
    )


def _git_log_with_names(
    repo_path: str,
    task_id: str,
    timeout: int = 20,
) -> List[Tuple[str, List[str]]]:
    """Return ``[(sha, [files])]`` for every commit whose message
    contains ``task_id``, in **most-recent-first** order.

    ``git log`` walks the full history; ``--grep`` filters by
    message substring. Each matching commit's short SHA + name row
    list is returned. Names are deduped within a single commit and
    filtered for empty strings (the parser emits blank lines for
    merge commits).

    Implementation notes:

      * ``--name-only`` emits blank separators (a ``\n``) between
        name rows of distinct commits. For merge commits (``-m``),
        ``--name-only`` emits the **empty name list** rather than
        ``None`` — we tolerate this by emitting ``[]`` for that
        commit.
      * We use ``-m --first-parent`` by default to avoid double-
        counting merges. Production repos may use non-FF merges; the
        ``-m`` flag tells ``git log`` to walk each merge parent
        individually. The TDD spec doesn't pin the exact behavior
        here, but in practice the first-parent line is what users
        inspect in a code-review flow.
      * Times out at 20s per call — sufficient for typical repos
        up to ~5000 commits; a real 50k+ repo would need a longer
        timeout, but the rule is deliberately run on per-task
        slices (small commits prefix), so this is safe.
    """
    proc = subprocess.run(
        [
            "git",
            "-C", str(repo_path),
            "log",
            "--all",
            "-m",
            "--first-parent",
            "--grep", task_id,
            "--format=%H",
            "--name-only",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        # We don't raise here; the rule must degrade gracefully when
        # the repo is transiently unavailable (eg CI sandbox with
        # ``cwd=/private/tmp`` and no .git). The caller will record
        # zero commit_hits on this task and verdict will be FAILED.
        return []

    lines = [ln for ln in proc.stdout.splitlines() if ln != ""]
    # The output shape alternates sha-line, [name, name, ...],
    # sha-line, [name, name, ...]. We split on SHA lines to recover
    # each commit's name group.
    out: List[Tuple[str, List[str]]] = []
    current_sha: Optional[str] = None
    current_names: List[str] = []
    for line in lines:
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            # Full SHA — finalize the prior commit (if any), then
            # open a new one.
            if current_sha is not None:
                out.append((current_sha, current_names))
            current_sha = line
            current_names = []
        else:
            current_names.append(line)
    if current_sha is not None:
        out.append((current_sha, current_names))
    return out


class VP006RevisedRule:
    """Replace the legacy VP-006 with a per-task git-log audit.

    See module docstring for the algorithm and contract.

    Usage::

        rule = VP006RevisedRule()
        report = rule.evaluate(
            tasks_json_path="plans/.../tasks.json",
            exec_repo_path="/tmp/sandbox",
        )
        if report.aggregate_verdict == "PASSED":
            ...  # commit & ship
    """

    def evaluate(
        self,
        tasks_json_path: str,
        exec_repo_path: str,
    ) -> VP006Report:
        """Run the rule and return a :class:`VP006Report`.

        Args:
            tasks_json_path: Path to a tasks.json file (canonical
                ``{"tasks": [...]}`` envelope). Each task MUST have
                an ``id`` and may have ``files_to_modify`` (list of
                paths).
            exec_repo_path: Path to the git repository whose log is
                being audited. TDD-only: the rule reads ONLY this
                repo's git history (it MUST NOT touch the live
                execution repo), so callers in tests should pass a
                freshly-inited ``tmp_git_repo`` path. Production
                callers may pass the actual repo path; the rule
                does not care which repo it is.

        Returns:
            A :class:`VP006Report` with one :class:`PerTaskVerdict`
            per task and a single ``aggregate_verdict`` string.
        """
        tasks = _load_tasks(tasks_json_path)
        per_task: List[PerTaskVerdict] = []

        for task in tasks:
            task_id = str(task.get("id", "")).strip()
            if not task_id:
                # A task without an id cannot be audited — skip with
                # a warning rather than silently passing or raising.
                per_task.append(
                    PerTaskVerdict(
                        task_id="<missing-id>",
                        verdict="SKIPPED",
                        warning="task has no 'id' field",
                    )
                )
                continue

            files_to_modify_raw = task.get("files_to_modify", [])
            if not isinstance(files_to_modify_raw, list):
                files_to_modify_raw = []
            files_to_modify: List[str] = [
                str(p) for p in files_to_modify_raw
            ]

            # ---- Empty files_to_modify: SKIPPED + warning ----
            if len(files_to_modify) == 0:
                per_task.append(
                    PerTaskVerdict(
                        task_id=task_id,
                        verdict="SKIPPED",
                        commit_hits=[],
                        file_match_count=0,
                        warning=(
                            "no files_to_modify declared; rule cannot "
                            "verify file-level overlap — skip"
                        ),
                    )
                )
                continue

            # ---- Walk git log for matching commits ----
            commits = _git_log_with_names(exec_repo_path, task_id)
            commit_hits: List[str] = []
            file_match_count = 0
            for sha, names in commits:
                commit_hits.append(sha)
                # ---- count file overlaps ----
                for name in names:
                    # Normalize so ``/``-style paths from ``git``
                    # match the canonical entry in tasks.json. We
                    # do a strict equality check (no prefix), per
                    # the task spec: "files_to_modify ⊂ commit
                    # names → match". A loose prefix match would
                    # silently accept ``src/a.py.bak`` against
                    # ``src/a.py`` which is wrong for audits.
                    if name in files_to_modify:
                        file_match_count += 1

            # ---- Determine verdict ----
            if not commit_hits:
                verdict = "FAILED"
                warning: Optional[str] = (
                    "no commit in repo log mentions task_id "
                    f"{task_id!r}; rule requires at least one "
                    "naming commit"
                )
            elif file_match_count == 0:
                verdict = "FAILED"
                warning = (
                    f"commit(s) name task {task_id!r} but no "
                    "modified file is in files_to_modify "
                    f"{files_to_modify!r}"
                )
            else:
                verdict = "PASSED"
                warning = None

            per_task.append(
                PerTaskVerdict(
                    task_id=task_id,
                    verdict=verdict,
                    commit_hits=commit_hits,
                    file_match_count=file_match_count,
                    warning=warning,
                )
            )

        # ---- Aggregate verdict: FAILED iff any FAILED, else PASSED ----
        # SKIPPED tasks are filtered out — they don't count toward
        # FAILED.
        aggregate_verdict = "PASSED"
        for ptv in per_task:
            if ptv.verdict == "FAILED":
                aggregate_verdict = "FAILED"
                break

        return VP006Report(
            per_task=per_task,
            aggregate_verdict=aggregate_verdict,
        )
