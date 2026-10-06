"""A parsed ``FILE:`` target must look like a path before it is written.

2026-10-05: task ``20-5-1-3`` died on
``[Errno 63] File name too long: '/…/product-development-team-dev/```;
line count far below the committed copy; …'``.

The subagent's report *discussed* a clobbered file inside a fenced
block. ``parse_files_from_response``'s regex —

    FILE:\\s*(.*?)\\n```(?:\\w+)?\\n(.*?)\\n```

— matched the literal ``FILE:`` somewhere in the prose and swept 5.5 KB
of that prose into the "path" group. The caller joined it onto
``project_dir`` and handed the result to ``open()``, where a single
path component over 255 bytes is ``ENAMETOOLONG``.

The report's own diagnosis was right about the file and wrong about the
cause: ``project_dir`` is 73 bytes. What overflowed was a markdown
paragraph wearing a filename.

A second, quieter hazard lived in the same function: an absolute path
*outside* the project was reduced to ``Path(path.name)`` and written at
the project root, so ``/etc/passwd`` became ``<project_dir>/passwd``.
Silently redirected, never refused.

These tests pin both: prose is not a path, and a write target stays
inside the tree.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402


class _LogRecorder:
    def __init__(self) -> None:
        self.warnings: List[str] = []

    def warning(self, event: str, message: str, **kwargs: Any) -> None:
        self.warnings.append(event)

    def info(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        pass

    def debug(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        pass


def _agent(project_dir: Path) -> AutonomousAgent:
    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = project_dir
    agent.logger = _LogRecorder()
    return agent


def _block(target: str, body: str = "x = 1") -> str:
    return f"FILE: {target}\n```python\n{body}\n```"


# ---------------------------------------------------------------------------
# The incident
# ---------------------------------------------------------------------------


def test_long_prose_target_is_skipped_not_written(tmp_path):
    """The 2026-10-05 shape: a report body captured as a filename.

    Before the fix this produced a 5.5 KB path component and raised
    ``OSError(ENAMETOOLONG)`` out of the executor's task loop.
    """
    agent = _agent(tmp_path)
    prose = "这是子 agent 的长篇缺陷分析报告，" * 120
    response = f"FILE: {prose}\n```python\nx = 1\n```"

    assert len(prose.encode()) > agent._MAX_PATH_BYTES, "fixture must overflow"
    assert agent.parse_files_from_response(response) == {}
    assert "file_write_path_rejected" in agent.logger.warnings


def test_rejection_is_a_skip_not_a_crash(tmp_path):
    """A bad block degrades to "no writes", not to a task-killing raise.

    The normal path is that the coding tool wrote the files itself; the
    ``FILE:`` block is a secondary mechanism, so losing one must not
    take down the attempt.
    """
    agent = _agent(tmp_path)
    response = _block("a" * 400) + "\n" + _block("backend/real.py")
    parsed = agent.parse_files_from_response(response)

    assert list(parsed) == ["backend/real.py"]


# ---------------------------------------------------------------------------
# Ordinary writes still work
# ---------------------------------------------------------------------------


def test_relative_path_is_parsed(tmp_path):
    agent = _agent(tmp_path)
    assert list(agent.parse_files_from_response(_block("backend/a.py"))) == [
        "backend/a.py",
    ]


def test_absolute_path_inside_project_is_made_relative(tmp_path):
    agent = _agent(tmp_path)
    target = tmp_path / "backend" / "b.py"
    response = _block(str(target))
    assert list(agent.parse_files_from_response(response)) == ["backend/b.py"]


def test_content_is_preserved(tmp_path):
    agent = _agent(tmp_path)
    parsed = agent.parse_files_from_response(_block("a.py", body="y = 2"))
    assert parsed["a.py"].strip() == "y = 2"


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


def test_absolute_path_outside_project_is_refused(tmp_path):
    """Previously reduced to its basename and written at the root."""
    agent = _agent(tmp_path)
    response = _block("/etc/passwd")

    assert agent.parse_files_from_response(response) == {}
    assert "file_write_path_rejected" in agent.logger.warnings


def test_traversal_out_of_project_is_refused(tmp_path):
    agent = _agent(tmp_path)
    response = _block("../../escape.py")

    assert agent.parse_files_from_response(response) == {}
    assert "file_write_path_rejected" in agent.logger.warnings


def test_empty_target_is_refused(tmp_path):
    agent = _agent(tmp_path)
    assert agent.parse_files_from_response("FILE:   \n```python\nx = 1\n```") == {}


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


def test_max_path_bytes_matches_the_filesystem_component_limit():
    """255 bytes is APFS/HFS+; the constant must not drift above it."""
    assert AutonomousAgent._MAX_PATH_BYTES == 255
