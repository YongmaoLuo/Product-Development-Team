"""Integration spec: the session mapping outlives the process that wrote it.

Why this spec exists
=====================

``mcode exec`` cannot be asked to create a conversation under an id the
caller chose. The CLI mints the id of the conversation it really
created, and the caller is left holding a *pinned* key — a uuid5 of the
plan and the task — that names nothing the CLI would honour. The
mapping between the two is what makes a resume possible at all, and it
is written down by :class:`harness.mcode.SessionMap`.

The unit spec for that class proves the mapping is durable by building a
second **instance** of it. That is not the same claim. An instance is a
Python object: two instances in one process share nothing but the file,
but two instances in one process also share the module, the imports and
every scrap of state the interpreter is holding. A mapping that only
lives in a process — in a module-level cache, in an object that outlived
its purpose, in a variable the test itself set up — passes that unit
spec and still fails the moment the run that created the conversation is
over.

The claim that matters is a claim about **processes**: the run that
created the conversation is gone, a *different* interpreter starts, and
the conversation is still there to be resumed. So this spec runs two
real Python interpreters, in two real subprocesses, over one shared
plans directory.

What is pinned here
-------------------

1. **The mapping is written by one process and read by another.** The
   first process registers the id the CLI minted; the second resolves
   the very same id from the file alone.
2. **The resumed command line names the real session.** The second
   process builds ``--session <mvs_...>``, and the pinned key — which
   the CLI could never honour — is nowhere on the command line.
3. **The two processes really are two processes.** They have different
   pids, the second starts after the first is gone, and the resolution
   the second one reports disappears when the file is not there — so it
   came from the file and not from anything a shared interpreter could
   have kept.

The observation point
--------------------

The CLI is a fake — ``fake_cli.py``, written into the temporary
directory by the fixture below. It stands in for ``mcode exec``: it
writes the stream a real run writes, including the ``session.started``
event that carries the id the CLI minted. It is a *file* and not a
mock because the thing under test is the boundary between two
processes: a mock installed in the test process would prove that the
test process can talk to itself.

The two children
----------------

``SCRIPT_A``
    Creates the conversation: it runs the fake CLI, reads its stream
    through the adapter, and registers the mapping. The id it hands
    back is the one the CLI minted, not the one the caller pinned.

``SCRIPT_B``
    Resumes it: it reads the mapping from the file and builds the
    command line that continues the conversation. It knows nothing but
    the plans directory and the pinned key — everything else it has to
    learn from the disk, which is the whole point.

Both are started with ``sys.executable -c``, so each one is a fresh
interpreter with its own imports and its own module state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.integration

#: The backend package root — ``<repo>/backend``. The children import
#: ``harness.mcode`` from it, so this directory has to be importable in
#: them.
BACKEND_DIR = Path(__file__).resolve().parents[3]

#: The plan and the task whose conversation is pinned. The pinned key
#: is derived from both, the way the caller derives it.
PLAN_ID = "plan-cross-process"
TASK_ID = "task-3"

#: The key the caller pins: a uuid5 of the plan and the task. The CLI
#: cannot be given this one; it is a handle, not a conversation.
PINNED = str(uuid.uuid5(uuid.NAMESPACE_URL, f"pdt:{PLAN_ID}:task:{TASK_ID}"))

#: The session id the CLI mints, in the shape the CLI mints it: the
#: ``mvs_`` prefix and 32 hex digits. The fake CLI hands out exactly
#: this one, so the spec can name the id it expects rather than
#: accepting whatever turned up.
OBSERVED = "mvs_0ecaf7d26ff845a19fbaf6ecd45285e9"

#: How long a child may take. A child that hangs must fail the spec,
#: not hold the lane until the lane's own timeout kills it.
CHILD_TIMEOUT = 60


# ---------------------------------------------------------------------------
# The fake CLI
# ---------------------------------------------------------------------------

#: The fake ``mcode exec``. It writes the stream a real run writes, and
#: the session id in it is the one the CLI mints — the caller has no
#: say in it, which is the whole reason the mapping exists.
FAKE_CLI = r'''
"""A fake ``mcode exec``: writes the stream a real run writes."""

import json
import os
import sys

session_id = os.environ["FAKE_SESSION_ID"]

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

# The pid of this process, so the spec can check that nothing was left
# running when the spec is over.
with open(os.environ["FAKE_CLI_PID_FILE"], "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))

for event in events:
    sys.stdout.write(json.dumps(event) + "\n")
'''


# ---------------------------------------------------------------------------
# The two children
# ---------------------------------------------------------------------------

#: Process A — creates the conversation, then records the id the CLI
#: minted.
SCRIPT_A = r'''
"""Create a conversation, then record the id the CLI minted."""

import json
import os
import subprocess
import sys
from pathlib import Path

from harness.mcode import McodeHarness, SessionMap

plans_dir = Path(os.environ["PLANS_DIR"])
pinned = os.environ["PINNED_SESSION_KEY"]
map_path = plans_dir / os.environ["PLAN_ID"] / "session_map.json"

# The CLI is a process of its own: its stdout is the stream.
child = subprocess.run(
    [sys.executable, os.environ["FAKE_CLI"]],
    capture_output=True,
    text=True,
    timeout=60,
)
if child.returncode != 0:
    sys.stderr.write(child.stderr)
    raise SystemExit("the fake CLI exited with %d" % child.returncode)

# The stream is read by the adapter, the way the runner reads it.
observed = None
for event in McodeHarness().parse_stream(child.stdout.splitlines()):
    if event.type == "system" and event.raw.get("subtype") == "session.started":
        observed = event.session_id
        break
if observed is None:
    raise SystemExit("the fake CLI printed no session.started event")

# The mapping is on disk before the id is handed back.
SessionMap(map_path).register(pinned, observed)

print(json.dumps({
    "step": "A",
    "pid": os.getpid(),
    "observed": observed,
    "map_path": str(map_path),
}))
'''

#: Process B — a fresh interpreter that resumes the conversation the
#: first one created.
SCRIPT_B = r'''
"""Resume the conversation the first process created."""

import json
import os
import sys
from pathlib import Path

from harness.contract import SessionMode, SessionRef, TaskInvocation
from harness.mcode import McodeHarness, SessionMap

plans_dir = Path(os.environ["PLANS_DIR"])
pinned = os.environ["PINNED_SESSION_KEY"]
map_path = plans_dir / os.environ["PLAN_ID"] / "session_map.json"

# The only thing this process knows about the conversation is the file.
resolved = SessionMap(map_path).lookup(pinned)

report = {
    "step": "B",
    "pid": os.getpid(),
    "resolved": resolved,
    "argv_has_session": False,
}

if resolved is not None:
    invocation = McodeHarness().build_invocation(
        TaskInvocation(
            prompt="continue the task",
            mode=SessionMode.RESUME,
            session=SessionRef(session_id=resolved, pinned_session_id=pinned),
        )
    )
    argv = list(invocation.argv)
    report["argv"] = argv
    report["argv_has_session"] = (
        "--session" in argv and argv[argv.index("--session") + 1] == resolved
    )

print(json.dumps(report))
'''


# ---------------------------------------------------------------------------
# Running the children
# ---------------------------------------------------------------------------


def _pythonpath() -> str:
    """The path the children import ``harness`` from.

    The backend directory is what makes ``harness.mcode`` importable.
    An inherited ``PYTHONPATH`` that already provides it is respected as
    it is — that is what inheriting one is for — and the backend
    directory is only prepended when it is missing.

    Args:
        None.

    Returns:
        The value of ``PYTHONPATH`` for the children.
    """
    inherited = [
        part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part
    ]
    if str(BACKEND_DIR) not in inherited:
        inherited.insert(0, str(BACKEND_DIR))
    return os.pathsep.join(inherited)


def _child_env(root: Path, plans_dir: Path) -> Dict[str, str]:
    """The environment the two children run in.

    Both children are given the same plans directory: it is the one
    thing they share, and sharing it is the whole mechanism.

    Args:
        root: The temporary directory of this spec.
        plans_dir: The plans directory the children share.

    Returns:
        The environment for one child.
    """
    env = dict(os.environ)
    env["PLANS_DIR"] = str(plans_dir)
    env["PLAN_ID"] = PLAN_ID
    env["PINNED_SESSION_KEY"] = PINNED
    env["FAKE_CLI"] = str(root / "fake_cli.py")
    env["FAKE_CLI_PID_FILE"] = str(root / "fake_cli.pid")
    env["FAKE_SESSION_ID"] = OBSERVED
    env["PYTHONPATH"] = _pythonpath()
    return env


def _run_child(script: str, env: Dict[str, str], label: str) -> Dict[str, Any]:
    """Run one child and read the report it printed.

    A child that did not exit cleanly fails the spec here, with its
    stdout and its stderr attached: a child that died has no answer,
    and "the child crashed" must never read as "the child resolved
    nothing".

    Args:
        script: The source the child runs.
        env: The environment it runs in.
        label: What the child is called in the failure message.

    Returns:
        The report the child printed.
    """
    try:
        child = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT,
            cwd=str(BACKEND_DIR),
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"the {label} child did not finish within {CHILD_TIMEOUT}s\n"
            f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
        )

    if child.returncode != 0:
        pytest.fail(
            f"the {label} child exited with {child.returncode}\n"
            f"stdout:\n{child.stdout}\nstderr:\n{child.stderr}"
        )

    return _read_report(child.stdout, label)


def _read_report(text: str, label: str) -> Dict[str, Any]:
    """The report a child printed on its standard output.

    Args:
        text: Everything the child printed.
        label: What the child is called in the failure message.

    Returns:
        The report, as a mapping.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        pytest.fail(f"the {label} child printed nothing")

    try:
        report = json.loads(lines[-1])
    except ValueError as exc:
        pytest.fail(f"the {label} child printed no JSON report ({exc})\n{text}")

    if not isinstance(report, dict):
        pytest.fail(f"the {label} child printed {report!r}, not a mapping")
    return report


def _process_state(pid: int) -> Optional[str]:
    """The state of a process, as the process table has it.

    Args:
        pid: The process to ask about.

    Returns:
        The state (``"S"``, ``"Z"``, ...), or ``None`` when the process
        table cannot say.
    """
    try:
        probe = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    return probe.stdout.strip() or None


def _is_running(pid: int) -> bool:
    """Whether a pid is a process that is still running.

    ``ps`` is asked first: it tells a pid that is gone ("no such
    process") apart from one that is running, which the signal probe
    alone cannot. A zombie counts as not running — it holds nothing,
    and it is gone the moment its parent reaps it.

    Args:
        pid: The process to ask about.

    Returns:
        ``True`` when the process is still running.
    """
    state = _process_state(pid)
    if state is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return not state.startswith("Z")


def _assert_no_residual_children(root: Path, *reports: Dict[str, Any]) -> None:
    """No child of this spec is still running when the spec is over.

    Args:
        root: The temporary directory of this spec.
        reports: The reports the children printed.
    """
    pids: List[int] = [
        int(report["pid"]) for report in reports if report.get("pid") is not None
    ]
    pid_file = root / "fake_cli.pid"
    if pid_file.exists():
        pids.append(int(pid_file.read_text(encoding="utf-8").strip()))

    for pid in pids:
        assert not _is_running(pid), f"the child {pid} is still running"


# ---------------------------------------------------------------------------
# The workspace
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace():
    """A plans directory, a fake CLI, and nothing left behind.

    Yields:
        The temporary root and the plans directory inside it.
    """
    with tempfile.TemporaryDirectory(prefix="pdt-session-xproc-") as tmp:
        root = Path(tmp)
        (root / "fake_cli.py").write_text(FAKE_CLI, encoding="utf-8")
        plans = root / "plans"
        (plans / PLAN_ID).mkdir(parents=True)
        yield root, plans

    # The temporary directory is gone: a spec that leaves a plans
    # directory behind has left the conversation behind too.
    assert not root.exists(), f"the temporary directory {root} was left behind"


# ---------------------------------------------------------------------------
# IT-SESSION-01 — the mapping crosses the process boundary
# ---------------------------------------------------------------------------


def test_second_process_resolves_session(workspace):
    """The second process resolves the id the first one registered.

    The first process creates the conversation and writes the mapping
    down; the second process — a different interpreter, started after
    the first one was gone — resolves that very id from the file.
    """
    root, plans = workspace
    env = _child_env(root, plans)

    first = _run_child(SCRIPT_A, env, "first")
    second = _run_child(SCRIPT_B, env, "second")

    # The first process really created a conversation, and registered
    # the id the CLI minted.
    assert first["step"] == "A"
    assert first["observed"] == OBSERVED

    # And the mapping is on disk — not merely in the first process.
    map_path = Path(first["map_path"])
    assert map_path.exists(), f"the mapping {map_path} was not written"
    assert json.loads(map_path.read_text(encoding="utf-8")) == {PINNED: OBSERVED}

    # The second process resolved that very id.
    assert second["step"] == "B"
    assert second["resolved"] == OBSERVED

    # Two interpreters, not one interpreter used twice: the second
    # child has a pid of its own, and it started after the first one
    # was gone.
    assert first["pid"] != second["pid"]

    # The resolution came from the file: the same child, pointed at a
    # plans directory that holds no mapping, resolves nothing. A
    # process that had kept the mapping in memory would still have
    # answered here.
    empty = root / "plans-without-mapping"
    empty.mkdir()
    control = _run_child(SCRIPT_B, _child_env(root, empty), "control")
    assert control["resolved"] is None
    assert control["argv_has_session"] is False

    _assert_no_residual_children(root, first, second, control)


def test_second_process_argv_has_session(workspace):
    """The second process builds a command line that resumes the session.

    The command line names the real mcode session — the id the CLI
    minted — and not the pinned key, which the CLI could never honour.
    """
    root, plans = workspace
    env = _child_env(root, plans)

    first = _run_child(SCRIPT_A, env, "first")
    second = _run_child(SCRIPT_B, env, "second")

    # The second process found the conversation at all — without it
    # there is no command line to look at, and the reason has to be
    # said out loud rather than surfacing as a missing key.
    assert second["resolved"] == OBSERVED, (
        "the second process resolved no session, so it built no command "
        f"line to resume one: {second}"
    )

    argv = second["argv"]

    # The session is named, and the name is the real one.
    assert "--session" in argv, f"no --session on the command line: {argv}"
    assert argv[argv.index("--session") + 1] == OBSERVED
    assert second["argv_has_session"] is True

    # The pinned key is a handle, not a conversation: it is nowhere on
    # the command line.
    assert PINNED not in argv, f"the pinned key reached the command line: {argv}"

    # And it is an mcode command line: the claude-only spellings of a
    # session are not on it.
    assert "--session-id" not in argv

    _assert_no_residual_children(root, first, second)
