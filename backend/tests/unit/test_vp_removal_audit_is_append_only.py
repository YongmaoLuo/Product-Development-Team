"""Unit contract for the append-only VP removal audit.

背景
----
主题外 VP（与本次主题无关的验证点）的删除必须「先归档、后移除」：
审计文件是这次删除**唯一**的持久化真相。写不进去，就不得移除 ——
所以写路径必须「失败即抛」，读路径必须「坏行不致命」。

What this file pins
--------------------
1. ``test_append_only_mode`` —— 审计文件只以 ``"a"``（追加）模式打开；
   模块里没有 ``truncate`` / ``os.remove`` / 任何覆写或删除路径。
2. ``test_roundtrip_ids`` —— 归档 3 条 → 移除集恰为 3 个 id。
3. ``test_corrupt_line_skipped`` —— 文件含半行 / 坏行 → 不抛异常，
   其余完整行照常返回。
4. ``test_missing_file_empty`` —— 文件不存在 → 返回空集合。

Plus the two boundary conditions the brief names next to them: a failed
write must **raise** (the caller aborts the removal), and appending must
never erase what was archived before it (append, not overwrite).
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# ``backend/`` is two levels up from ``backend/tests/unit/``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (str(_BACKEND_DIR), str(_REPO_ROOT), str(_REPO_ROOT / "tools")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from verification.vp_removal_audit import (  # noqa: E402
    AUDIT_FILENAME,
    archive_removal,
    load_removed_vp_ids,
)

#: The module under test — the source the shape assertions read.
_MODULE_PATH = _BACKEND_DIR / "verification" / "vp_removal_audit.py"

#: Call names that would give the audit file a truncate / overwrite /
#: delete path.  Their mere presence in a call position is a violation:
#: prose in a docstring is not a call.
_DESTRUCTIVE_CALLS = frozenset({"truncate", "remove", "unlink", "rmtree"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(vp_id: str, **overrides: Any) -> Dict[str, Any]:
    """A removal record in the shape the executor archives."""
    record: Dict[str, Any] = {
        "schema_version": 1,
        "ts": "2026-10-26T12:00:00Z",
        "plan_id": "p1",
        "round": 3,
        "component": "verification_executor",
        "vp_id": vp_id,
        "decision": "llm_irrelevant",
        "tier": "llm",
        "reason": "主题外",
        "evidence": {"matched_criterion_ids": []},
        "vp": {"id": vp_id},
        "verdict": {"status": "pending"},
        "allocated_vp_ids_preserved": True,
    }
    record.update(overrides)
    return record


def _module_source() -> str:
    return _MODULE_PATH.read_text(encoding="utf-8")


def _open_modes(source: str) -> List[Tuple[int, Optional[str]]]:
    """Return ``(lineno, mode)`` for every ``open`` call in ``source``.

    Both spellings are covered: the builtin ``open(path, mode)`` (mode is
    the second positional argument) and the method ``path.open(mode)``
    (mode is the first).  A call without a mode argument yields ``None``
    (the read-only default).
    """
    modes: List[Tuple[int, Optional[str]]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            positional_index = 1
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            positional_index = 0
        else:
            continue
        modes.append((node.lineno, _mode_argument(node, positional_index)))
    return modes


def _mode_argument(call: ast.Call, positional_index: int) -> Optional[str]:
    """The literal mode argument of ``call``, or ``None`` / ``"<dynamic>"``."""
    argument: Optional[ast.AST] = None
    if len(call.args) > positional_index:
        argument = call.args[positional_index]
    else:
        for keyword in call.keywords:
            if keyword.arg == "mode":
                argument = keyword.value
                break
    if argument is None:
        return None
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    return "<dynamic>"


def _called_names(tree: ast.AST) -> set[str]:
    """Every name / attribute that appears in a call position."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _imported_modules(tree: ast.AST) -> set[str]:
    """The top-level module names the source imports."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module.split(".")[0])
    return modules


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_append_only_mode() -> None:
    """The audit file is only ever opened in append mode.

    A mode other than ``"a"`` — ``"w"``, ``"w+"``, ``"r+"``, ``"x"`` —
    is a way to lose an earlier record, which is exactly what the audit
    exists to prevent.  The check is AST-based so that prose in a
    docstring cannot satisfy it and cannot trip it either.
    """
    source = _module_source()
    modes = _open_modes(source)

    assert modes, (
        f"{_MODULE_PATH.name} must open the audit file at least once; "
        "an append-only audit needs a write path"
    )

    offenders = [
        f"line {line_no}: mode={mode!r}"
        for line_no, mode in modes
        if mode != "a"
    ]
    assert not offenders, (
        f"{_MODULE_PATH.name} opens the audit file with a mode other than "
        f"'a' (append). Offending call(s): {offenders!r}. The audit file "
        "is append-only: 'w' truncates it, 'r+' rewrites it, 'x' refuses "
        "to append to an existing file."
    )


def test_module_has_no_truncate_or_delete_path() -> None:
    """No truncate / overwrite / delete call, and no destructive import."""
    tree = ast.parse(_module_source())

    called = _called_names(tree)
    destructive = sorted(called & _DESTRUCTIVE_CALLS)
    assert not destructive, (
        f"{_MODULE_PATH.name} calls {destructive!r}. The removal audit is "
        "append-only: there must be no code path that truncates, "
        "overwrites or deletes it."
    )

    imported = _imported_modules(tree)
    assert "shutil" not in imported, (
        f"{_MODULE_PATH.name} imports shutil. The removal audit must not "
        "be able to move or delete itself."
    )


def test_roundtrip_ids(tmp_path: Path) -> None:
    """Three archived records rebuild into exactly three removed ids."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    for vp_id in ("VP-001", "VP-002", "VP-003"):
        archive_removal(_record(vp_id), plan_dir)

    removed = load_removed_vp_ids(plan_dir)

    assert removed == {"VP-001", "VP-002", "VP-003"}
    assert len(removed) == 3


def test_audit_file_is_one_json_object_per_line(tmp_path: Path) -> None:
    """The audit file is JSON Lines: one record per line, newline-terminated."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    archive_removal(_record("VP-001"), plan_dir)
    archive_removal(_record("VP-002"), plan_dir)

    audit = plan_dir / AUDIT_FILENAME
    assert audit.is_file(), f"{AUDIT_FILENAME} must be written into the plan dir"
    lines = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line)["vp_id"] in {"VP-001", "VP-002"}


def test_append_preserves_earlier_records(tmp_path: Path) -> None:
    """Appending must not erase what was archived before it."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    archive_removal(_record("VP-001"), plan_dir)
    archive_removal(_record("VP-002"), plan_dir)

    audit = plan_dir / AUDIT_FILENAME
    assert len(audit.read_text(encoding="utf-8").splitlines()) == 2
    assert load_removed_vp_ids(plan_dir) == {"VP-001", "VP-002"}


def test_corrupt_line_skipped(tmp_path: Path) -> None:
    """A half-written line is skipped; the complete lines still come back."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    audit = plan_dir / AUDIT_FILENAME

    audit.write_text(
        json.dumps(_record("VP-001"), ensure_ascii=False)
        + "\n"
        + '{"vp_id": "VP-002", "decision": "llm_irrele'
        + "\n"
        + json.dumps(_record("VP-003"), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    removed = load_removed_vp_ids(plan_dir)

    assert removed == {"VP-001", "VP-003"}


def test_missing_file_empty(tmp_path: Path) -> None:
    """No audit file means no removal ever happened → empty set."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    assert not (plan_dir / AUDIT_FILENAME).exists()
    assert load_removed_vp_ids(plan_dir) == set()


def test_write_failure_raises(tmp_path: Path) -> None:
    """A write that cannot land must raise, so the caller aborts the removal."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    audit = plan_dir / AUDIT_FILENAME
    audit.write_text("{\n", encoding="utf-8")

    # A directory where the audit file must be → the open() itself fails.
    audit.unlink()
    audit.mkdir()

    with pytest.raises(OSError):
        archive_removal(_record("VP-001"), plan_dir)


def test_unserialisable_record_raises(tmp_path: Path) -> None:
    """A record that cannot be serialised raises instead of being dropped."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    with pytest.raises(TypeError):
        archive_removal(_record("VP-001", evidence={"bad": object()}), plan_dir)