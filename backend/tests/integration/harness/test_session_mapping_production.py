"""Integration spec: the production path records the session the CLI minted.

Why this spec exists
=====================
``mcode exec`` cannot be asked to create a conversation under an id the
caller chose. The CLI mints the id of the conversation it really created
and reports it in the run's events; the caller is left holding the
*pinned* key it asked with — a uuid5 of the plan and the task — which
names nothing the CLI would honour.

Two things have to be true for a resume to work at all, and both of them
used to be false in production:

1. **the id the CLI minted has to reach the caller.** The dispatch
   reads its conversation out of the run's events. It used to read that
   id only out of the claude stream's ``init`` event, so an mcode run
   handed the *pinned* key back — the very id the CLI cannot honour —
   and the caller's next round tried to resume a conversation that does
   not exist.

2. **the mapping has to be written down.** The pinned key and the real
   session id are two names for one conversation, and only the file
   carries that knowledge from the run that created the conversation to
   the later run that continues it. Nothing in production ever built
   that mapping, so a later run had no way to learn what the earlier one
   had opened.

What is pinned here
-------------------

1. **The create hands back the id the CLI minted.** The handle carries
   the ``mvs_`` id, not the pinned uuid.
2. **The mapping is on disk, under the plan.** The pinned key and the
   observed id are recorded in the plan's own directory, next to the
   plan's other per-plan state.
3. **The resume uses the observed id.** A later round that holds only
   the pinned key puts ``--session <mvs_...>`` on the command line —
   the pinned uuid is nowhere on it, because the CLI would refuse it.

The observation point
---------------------
The CLI is a fake — a small script written into a temporary directory
and put on ``PATH``. It is a *file* and not a mock because the thing
under test is the boundary between the dispatch and the process it
spawns: the id has to travel out of a real child's event stream, through
the real runner, into the real handle, and the command line of the next
run has to be the one the adapter really built.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

#: The backend package root — ``<repo>/backend``. The dispatch and the
#: adapters are imported from it, so this directory has to be
#: importable here.
BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import coding_tool  # noqa: E402
from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    JsonSession,
    query_in_session,
)
from harness.mcode import McodeHarness  # noqa: E402
from harness.session_map import SESSION_MAP_FILENAME  # noqa: E402

pytestmark = pytest.mark.integration

#: The plan and the task whose conversation is pinned. The pinned key
#: is derived from both, the way the executor derives it.
PLAN_ID = "plan-production-mapping"
TASK_ID = "task-7"

#: The key the caller pins: a uuid5 of the plan and the task. The CLI
#: cannot be given this one; it is a handle, not a conversation.
PINNED = str(uuid.uuid5(uuid.NAMESPACE_URL, f"pdt:{PLAN_ID}:task:{TASK_ID}"))

#: The session id the CLI mints, in the shape the CLI mints it: the
#: ``mvs_`` prefix and 32 hex digits.
OBSERVED = "mvs_0ecaf7d26ff845a19fbaf6ecd45285e9"


# ---------------------------------------------------------------------------
# The fake CLI
# ---------------------------------------------------------------------------

#: The fake ``mcode exec``. It writes the stream a real run writes, and
#: the session id in it is the one the CLI mints — the caller has no
#: say in it, which is the whole reason the mapping exists. It also
#: writes the command line it was given, so the spec can read the
#: command line the adapter really built.
FAKE_CLI = r'''#!/usr/bin/env python3
"""A fake ``mcode exec``: writes the stream a real run writes."""

import json
import os
import sys

session_id = os.environ["FAKE_SESSION_ID"]

with open(os.environ["FAKE_ARGV_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")

usage = {
    "inputTokens": 11,
    "outputTokens": 7,
    "cacheReadTokens": 0,
    "totalTokens": 18,
}

events = [
    {"type": "exec.started", "exec_id": "exec-1", "session_id": session_id},
    {"type": "session.started", "session_id": session_id},
    {"type": "turn.started", "turn_id": "turn-1", "session_id": session_id},
    {
        "type": "item.completed",
        "item": {"type": "text", "text": "hello"},
        "session_id": session_id,
    },
    {
        "type": "turn.completed",
        "model": {"providerId": "minimax", "modelId": "MiniMax-M3.1-Flash"},
        "usage": usage,
        "session_id": session_id,
    },
    {
        "type": "exec.completed",
        "result": {"status": "succeeded", "output": "hello", "usage": usage},
        "session_id": session_id,
    },
]

for event in events:
    sys.stdout.write(json.dumps(event) + "\n")
'''


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mcode_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The production dispatch, driving a fake ``mcode`` on ``PATH``.

    The dispatch is the service's own — ``ClaudeCodingTool.query`` —
    and the backend is the real mcode adapter, so the command line, the
    event stream and the session id all travel the path they travel in
    production. Only the CLI is fake.

    Args:
        tmp_path: The temporary directory of this spec.
        monkeypatch: The patcher this spec owns.

    Returns:
        The tool, the file the fake CLI logs its command lines to, and
        the plan's directory.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "mcode"
    cli.write_text(FAKE_CLI, encoding="utf-8")
    cli.chmod(0o755)

    argv_log = tmp_path / "argv.log"
    monkeypatch.setenv(
        "PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    monkeypatch.setenv("FAKE_SESSION_ID", OBSERVED)
    monkeypatch.setenv("FAKE_ARGV_LOG", str(argv_log))
    # No provider walk: the dispatch is not about providers here, and a
    # walk would read the operator's live CC Switch state.
    monkeypatch.setenv("PDT_PROVIDER_MODE", "inherit")
    # The plan this dispatch belongs to. It is what puts the mapping
    # next to the plan's own per-plan state.
    monkeypatch.setenv("PDT_PLAN_ID", PLAN_ID)

    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))

    # The backend this process drives. It is resolved once, when the
    # module is imported, so the spec points the process at the backend
    # it is about.
    monkeypatch.setattr(coding_tool, "_HARNESS", McodeHarness())

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    tool = ClaudeCodingTool(cwd=str(workspace))
    tool.logger = MagicMock()
    return tool, argv_log, plan_dir


def _command_lines(argv_log: Path) -> List[List[str]]:
    """The command lines the fake CLI was given, in order.

    Args:
        argv_log: The file the fake CLI appends to.

    Returns:
        One list of arguments per run.
    """
    if not argv_log.exists():
        return []
    return [
        json.loads(line)
        for line in argv_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# The case
# ---------------------------------------------------------------------------


def test_coding_tool_records_observed_mvs_session_id_and_resume_uses_it(
    mcode_dispatch: Tuple[Any, Path, Path],
):
    """The create records the id the CLI minted, and the resume uses it.

    Args:
        mcode_dispatch: The dispatch, the command-line log and the
            plan's directory.
    """
    tool, argv_log, plan_dir = mcode_dispatch

    # -- one: the create -------------------------------------------------
    #
    # The handle carries the pinned key and nothing else: this is the
    # first dispatch of a task, and the conversation does not exist yet.
    handle = JsonSession(pinned_session_id=PINNED)

    assert query_in_session(tool, handle, "do the thing") == "hello"

    assert handle.session_id == OBSERVED, (
        "the handle must carry the id the CLI minted, not the pinned "
        f"key it asked with; got {handle.session_id!r}"
    )

    # The mapping is on disk, in the plan's own directory.
    mapping_path = plan_dir / SESSION_MAP_FILENAME
    assert mapping_path.exists(), (
        "the create must record the mapping before it hands the id back; "
        f"nothing was written to {mapping_path}"
    )
    mapping: Dict[str, str] = json.loads(
        mapping_path.read_text(encoding="utf-8")
    )
    assert mapping == {PINNED: OBSERVED}, (
        f"the mapping must name the conversation the CLI minted; got {mapping!r}"
    )

    # The create did not ask the CLI for a session: the CLI cannot be
    # asked for one, and a command line that carried the pinned key
    # would be a request it refuses.
    first = _command_lines(argv_log)[0]
    assert "--session" not in first, (
        f"a create must not name a session; got {first!r}"
    )
    assert "--session-id" not in first, (
        f"a create must not name a session id; got {first!r}"
    )

    # -- two: the resume -------------------------------------------------
    #
    # A later round that knows only the pinned key — the key is what
    # the plan recorded, and the id the CLI minted is what only the
    # earlier run saw.
    later = JsonSession(session_id=PINNED)

    assert query_in_session(tool, later, "do it again") == "hello"

    runs = _command_lines(argv_log)
    assert len(runs) == 2, f"the resume must be one run; got {len(runs)}"
    second = runs[1]
    assert "--session" in second, (
        f"the resume must name the conversation it continues; got {second!r}"
    )
    assert second[second.index("--session") + 1] == OBSERVED, (
        "the resume must continue the conversation the CLI minted; got "
        f"{second!r}"
    )
    assert PINNED not in second, (
        f"the pinned key is not a session the CLI can honour; got {second!r}"
    )
