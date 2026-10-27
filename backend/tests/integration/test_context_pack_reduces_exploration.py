"""Observational case — the context pack must cut exploration by >= 80%.

Why this file exists
--------------------
The context pack (:mod:`context_pack`) exists because every sub-agent
that starts inside a worktree re-derives the same eight environment
facts by exploring: ``ls``, ``find``, ``git log``.  Task 19 put the
reference to the pack into both briefs, so a sub-agent that reads the
brief can answer those questions without exploring at all.

That is a claim about BEHAVIOUR, not about a function's return value, so
it needs its own measure.  This file provides it: a counter over an
execution session log, and the comparison that turns two logs into a
number.

What is counted
---------------
One unit is one **read-only exploration command** — a command whose
shape is ``ls``, ``find`` or ``git log``.  Those three shapes are the
ones the brief itself names (``CONTEXT_PACK_REFERENCE_INSTRUCTION``:
"不要再运行 ls / find / git log 重新探索环境"), so the metric and the
instruction it measures cannot drift apart — ``test_the_metric_measures
_what_the_brief_forbids`` pins that.

Two things are deliberately NOT counted:

* **Test execution.**  ``python -m pytest ...`` is work, not
  exploration.  It never matches, because only a command whose FIRST
  word is ``ls`` / ``find`` / ``git log`` counts — the shape is decided
  by the head of the command, never by a word somewhere inside it.
* **Code search.**  ``grep`` and the ``Grep``/``Glob`` tools are
  read-only, but the pack does not replace them: they answer "where is
  this function", which is not one of the eight environment facts.  The
  pack replaces ``ls`` / ``find`` / ``git log``, so the metric counts
  exactly those.

A compound command is split on ``&&``, ``||``, ``;``, ``|`` and
newlines, and every segment is judged on its own — so
``cd /repo && git status && git log --oneline`` is one exploration
(the ``git log``), not two.

The log format
--------------
The counter reads the Claude Code session transcript format: JSON
lines, one event per line, and a tool call is a ``tool_use`` block
inside an ``assistant`` message.  That is the format the backend
already parses in ``coding_tool.py`` when it forwards ``tool_use``
blocks to the operator log, so the counter reads what the backend
already sees.

Lanes
-----
This module is ``observational``: it CHARACTERISES a behaviour rather
than asserting a contract, and the case that measures real sessions
SKIPS when those sessions are not there — a skip is the honest answer,
a fake pass is not.  It also carries the ``integration`` marker, which
is what keeps it out of the default PR shard (that shard runs
``-m "not e2e and not integration and not time_sensitive and not
perf"``) while the nightly lane (``-m "not e2e and not time_sensitive
and not perf"``) still runs it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest

#: ``backend/tests/integration`` is two levels below ``backend/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification_subagent import (  # noqa: E402
    CONTEXT_PACK_REFERENCE_INSTRUCTION,
    context_pack_reference_line,
)


# ---------------------------------------------------------------------------
# Marker registration
# ---------------------------------------------------------------------------
#
# ``backend/pytest.ini`` runs pytest with ``--strict-markers``, so a
# marker that is not declared there is a collection ERROR rather than a
# warning.  The marker is declared from here instead, which keeps this
# module self-contained: the live config is reachable through
# ``pytest.mark`` (pytest's own ``MARK_GEN`` holds it after
# ``pytest_configure``), and ``addinivalue_line`` appends to the very
# list ``--strict-markers`` validates against.
_OBSERVATIONAL_MARKER_DESCRIPTION = (
    "observational: measurement-only case — it characterises a behaviour "
    "(e.g. the context pack cutting exploration by >= 80%) and skips when "
    "the artefact it measures is absent, rather than faking a pass."
)


def _register_observational_marker() -> None:
    """Declare the ``observational`` marker with the live pytest config."""
    config = getattr(pytest.mark, "_config", None)
    if config is None:  # pragma: no cover - a collection always has a config
        return
    known = {
        line.split(":")[0].split("(")[0].strip() for line in config.getini("markers")
    }
    if "observational" not in known:
        config.addinivalue_line("markers", _OBSERVATIONAL_MARKER_DESCRIPTION)


_register_observational_marker()


#: Every case in this module is observational, and every case is also
#: an integration case — the second marker is what keeps the module out
#: of the default PR shard.
pytestmark = [pytest.mark.observational, pytest.mark.integration]


# ---------------------------------------------------------------------------
# What counts as exploration
# ---------------------------------------------------------------------------

#: The executables whose invocation IS an exploration of the environment.
_EXPLORATION_EXECUTABLES = frozenset({"ls", "find"})

#: Git options that take a value, so the scan skips the value as well.
_GIT_VALUE_OPTIONS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--exec-path",
        "--config-env",
    }
)

#: The git subcommand that reads history — the third forbidden shape.
_GIT_EXPLORATION_SUBCOMMAND = "log"

#: A compound command is split here: ``&&`` and ``||`` before ``|``, so
#: a logical OR is not split twice.
_SEGMENT_BREAK_RE = re.compile(r"&&|\|\||[;\n|]")

#: A leading environment assignment (``FOO=bar ls -la``) is not part of
#: the command's shape.
_ENV_ASSIGNMENT_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S*)\s+"
)


def _strip_env_assignments(segment: str) -> str:
    """Drop the leading ``VAR=value`` assignments from one command segment."""
    text = segment.strip()
    while True:
        match = _ENV_ASSIGNMENT_RE.match(text)
        if match is None:
            return text
        text = text[match.end():].lstrip()


def _segment_words(segment: str) -> List[str]:
    """The words of one command segment, or ``[]`` when it is empty."""
    text = _strip_env_assignments(segment)
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError:
        # Unbalanced quotes: fall back to a plain split rather than
        # losing the segment entirely.
        return text.split()


def _git_subcommand(words: Sequence[str]) -> Optional[str]:
    """The git subcommand of ``git ...``, or ``None`` when there is none."""
    index = 1
    while index < len(words):
        word = words[index]
        if word in _GIT_VALUE_OPTIONS:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        return word
    return None


def _is_exploration_command(words: Sequence[str]) -> bool:
    """Whether one command segment is a read-only exploration command."""
    if not words:
        return False
    executable = os.path.basename(words[0])
    if executable in _EXPLORATION_EXECUTABLES:
        return True
    if executable == "git":
        return _git_subcommand(words) == _GIT_EXPLORATION_SUBCOMMAND
    return False


def count_exploration_commands(command: str) -> int:
    """Count the exploration commands inside one shell command string.

    A compound command contributes one unit per exploration segment, so
    ``ls && git log`` counts two and ``cd /repo && git log`` counts one.
    """
    if not isinstance(command, str) or not command.strip():
        return 0
    total = 0
    for segment in _SEGMENT_BREAK_RE.split(command):
        if _is_exploration_command(_segment_words(segment)):
            total += 1
    return total


# ---------------------------------------------------------------------------
# Reading a session log
# ---------------------------------------------------------------------------


def _iter_tool_use_blocks(
    event: Dict[str, Any],
) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield ``(tool_name, tool_input)`` for every tool call in one event."""
    blocks: List[Any] = []
    if event.get("type") == "tool_use":
        blocks.append(event)
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            blocks.extend(block for block in content if isinstance(block, dict))
    elif isinstance(event.get("content"), list):
        blocks.extend(block for block in event["content"] if isinstance(block, dict))

    for block in blocks:
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str):
            continue
        tool_input = block.get("input")
        yield name, tool_input if isinstance(tool_input, dict) else {}


def count_exploration_calls(session_log_path) -> int:
    """Count the read-only exploration calls in one execution session log.

    ``session_log_path`` is the path of a session transcript: JSON lines,
    one event per line, tool calls being ``tool_use`` blocks inside
    ``assistant`` messages.  A missing file raises — the caller decides
    whether that is a skip or a failure, and this function does not
    invent a number for a log it never read.
    """
    path = Path(session_log_path)
    total = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                # A truncated or non-JSON line is not a tool call.
                continue
            if not isinstance(event, dict):
                continue
            for name, tool_input in _iter_tool_use_blocks(event):
                if name.lower() != "bash":
                    continue
                command = tool_input.get("command")
                if isinstance(command, str):
                    total += count_exploration_commands(command)
    return total


# ---------------------------------------------------------------------------
# Fixtures — the session logs the comparison runs on
# ---------------------------------------------------------------------------


def _bash_event(command: str) -> str:
    """One assistant turn that runs ``command`` through Bash."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_0",
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        },
        ensure_ascii=False,
    )


def _read_event(file_path: Path) -> str:
    """One assistant turn that reads a file."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_0",
                        "name": "Read",
                        "input": {"file_path": str(file_path)},
                    }
                ],
            },
        },
        ensure_ascii=False,
    )


def _text_event(text: str) -> str:
    """One assistant turn that says ``text``."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        },
        ensure_ascii=False,
    )


def _session_log_without_pack() -> List[str]:
    """A round that was NOT handed the pack, so it explored for itself.

    Twelve exploration commands over a handful of turns: ``ls`` five
    times, ``find`` four times, ``git log`` three times (each behind a
    ``cd``), plus one test run that must not be counted.
    """
    events: List[str] = []
    for _ in range(5):
        events.append(_bash_event("ls -la"))
    for _ in range(4):
        events.append(_bash_event("find backend -name '*.py' | head -50"))
    for _ in range(3):
        events.append(_bash_event("cd /repo && git status && git log --oneline -20"))
    events.append(_bash_event("python -m pytest backend/tests/ -q"))
    return events


def _session_log_with_pack(pack_path: Path) -> List[str]:
    """A round that WAS handed the pack, so it read it instead of exploring.

    The brief carries the reference line task 19 puts in it, the
    sub-agent reads the pack file, and the only exploration left is the
    single ``ls`` of the directory the pack lives in.  The test run is
    not exploration.
    """
    events: List[str] = [_text_event(context_pack_reference_line(pack_path))]
    events.append(_read_event(pack_path))
    events.append(_bash_event(f"ls -la {pack_path.parent}"))
    events.append(_bash_event("python -m pytest backend/tests/ -q"))
    return events


def _write_log(path: Path, events: Sequence[str]) -> Path:
    path.write_text("\n".join(events) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_exploration_count_parses_log(tmp_path):
    """The counter reads a sample log exactly."""
    events = [
        _bash_event("ls -la"),  # 1
        _bash_event("find . -name '*.py'"),  # 2
        _bash_event("git log --oneline -20"),  # 3
        _bash_event("cd /repo && git status && git log --oneline"),  # 4
        _bash_event("git -C /repo log --oneline"),  # 5
        # None of the following is exploration.
        _bash_event("python -m pytest backend/tests/ -q"),
        _bash_event("pytest backend/tests/ -q"),
        _bash_event("git status"),
        _bash_event("grep -rn TODO backend/"),
        _bash_event("echo 'ls'"),
        _bash_event("lsblk"),
        _read_event(Path("/repo/backend/app.py")),
    ]
    log = _write_log(tmp_path / "sample.jsonl", events)

    assert count_exploration_calls(log) == 5


def test_test_execution_is_not_counted(tmp_path):
    """Running the tests is work, not exploration."""
    events = [
        _bash_event("python -m pytest backend/tests/ -q"),
        _bash_event("pytest backend/tests/integration/ -q"),
        _bash_event("cd backend && python -m pytest -k find"),
        _bash_event("npm test"),
    ]
    log = _write_log(tmp_path / "tests-only.jsonl", events)

    assert count_exploration_calls(log) == 0


def test_pack_reduces_exploration_by_80pct(tmp_path):
    """A round handed the pack explores at most a fifth as much."""
    pack_path = tmp_path / "context" / "round-1.json"
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    pack_path.write_text(json.dumps({"branch": "feat/example"}), encoding="utf-8")

    before = count_exploration_calls(
        _write_log(tmp_path / "before.jsonl", _session_log_without_pack())
    )
    after = count_exploration_calls(
        _write_log(tmp_path / "after.jsonl", _session_log_with_pack(pack_path))
    )

    assert before > 0, "the baseline round has to explore something"
    assert after <= before * 0.2, (
        f"exploration fell from {before} to {after} — "
        f"that is a {100 * (1 - after / before):.1f}% reduction, below the 80% bar"
    )


def test_the_metric_measures_what_the_brief_forbids():
    """The counted shapes are the shapes the brief tells the sub-agent to skip."""
    for shape in ("ls", "find", "git log"):
        assert shape in CONTEXT_PACK_REFERENCE_INSTRUCTION, (
            f"the brief no longer names {shape!r}, so counting it measures "
            "something the brief does not forbid"
        )


#: Where an operator points the real measurement at.
BEFORE_LOG_ENV = "PDT_EXPLORATION_BEFORE_LOG"
AFTER_LOG_ENV = "PDT_EXPLORATION_AFTER_LOG"
LOGS_DIR_ENV = "PDT_EXPLORATION_LOGS_DIR"


def _real_log_paths() -> Tuple[Optional[Path], Optional[Path]]:
    """The before/after session logs an operator supplied, if any."""
    before = os.environ.get(BEFORE_LOG_ENV)
    after = os.environ.get(AFTER_LOG_ENV)
    if before and after:
        return Path(before), Path(after)
    logs_dir = os.environ.get(LOGS_DIR_ENV)
    if logs_dir:
        base = Path(logs_dir)
        return base / "before.jsonl", base / "after.jsonl"
    return None, None


def test_real_session_logs_show_reduction():
    """The same comparison, on real execution sessions, when there are any."""
    before_path, after_path = _real_log_paths()
    missing = [
        str(path)
        for path in (before_path, after_path)
        if path is None or not Path(path).is_file()
    ]
    if missing:
        pytest.skip(
            "no execution session logs to measure — set "
            f"{BEFORE_LOG_ENV} and {AFTER_LOG_ENV} (or {LOGS_DIR_ENV} to a "
            "directory holding before.jsonl and after.jsonl). Skipping "
            "rather than faking a pass."
        )

    before = count_exploration_calls(before_path)
    after = count_exploration_calls(after_path)

    assert before > 0, "the baseline session has to explore something"
    assert after <= before * 0.2, (
        f"exploration fell from {before} to {after} — "
        f"that is a {100 * (1 - after / before):.1f}% reduction, below the 80% bar"
    )


def test_marker_observational(request):
    """Every case here is observational, and none of them is in the PR shard."""
    module_nodeid = request.node.nodeid.split("::")[0]
    items = [
        item
        for item in request.session.items
        if item.nodeid.startswith(module_nodeid + "::")
    ]
    assert items, "no cases were collected for this module"

    for item in items:
        assert item.get_closest_marker("observational") is not None, (
            f"{item.nodeid} is not marked observational"
        )
        # The default PR shard runs
        # ``-m "not e2e and not integration and not time_sensitive and not perf"``,
        # so the ``integration`` marker is what keeps an observational case
        # out of it while the nightly lane still runs it.
        assert item.get_closest_marker("integration") is not None, (
            f"{item.nodeid} would land in the default PR shard without the "
            "integration marker"
        )
