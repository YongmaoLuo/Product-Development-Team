"""Performance baseline for the harness layer — PERF-01 .. PERF-07.

What this file is for
=====================

The harness layer sits between a plan's tasks and a coding CLI. Every
task in a plan goes through it, and a plan holds dozens of tasks, so
every millisecond and every megabyte this layer adds is paid dozens of
times over. These seven cases are the baseline that says how much it
costs, measured **on the overhead this system adds** — never on the
end-to-end latency of the two backends, which is the model's bill and
not ours to assert on.

The seven cases
---------------

===== ============================== ====================================
PERF  what is pinned                the test that pins it
===== ============================== ====================================
01    the start-up cost, and that    ``test_startup_overhead_within_budget``
      it is paid **once**
                                      ``test_profile_enumerated_once``
02    ``build_invocation`` is cheap  ``test_build_invocation_latency``
03    the stream parses fast enough  ``test_parse_stream_throughput``
04    the stream does not pile up    ``test_parse_memory_is_bounded``
05    concurrent tasks do not        ``test_concurrent_tasks_are_isolated``
      contaminate each other
06    no descriptor / thread leak    ``test_no_fd_or_thread_leak``
07    one task costs one wrapper     ``test_one_wrapper_one_cli_per_task``
      and one CLI, and the wrapper
      hands over with ``exec``
===== ============================== ====================================

Why these are the right three overheads
=======================================

The layer adds exactly three kinds of cost, and each of them is
multiplied by the number of tasks in a plan:

* **A start-up cost.** The MiniMax Code deployment has to know which
  accounts exist before it runs anything, and that knowledge comes from
  another program. That program is spawned **once**, at start-up — never
  per task (arch decision point 5).
* **A per-task cost.** Every task builds a command line and spawns a
  process. The command line is built in Python, so it is a per-task
  cost that a plan pays dozens of times.
* **A per-byte cost.** Every task reads a stream of events. The stream
  is read line by line and never held as one blob (arch decision point
  2), so the memory a run costs does not grow with the length of the
  run.

Measurement constants
--------------------=

Every bound below is a *ceiling*, not a target. They were measured on

    2026-10-24 — Apple M5 Pro (18 cores), macOS 26.6.2 (Darwin 25.6.0),
    CPython 3.11.13

and they may only ever be **tightened**, on the strength of a
measurement. Loosening one to turn a red build green is not a repair,
it is the deletion of the alarm. Each test prints the number it
measured, so a change in the environment shows up as a change in the
measurement rather than as a silent pass.

The concurrency of the concurrent case (PERF-05) is **not** written
down here: it is read from ``provider_capacity.yaml``, the file the
operator uses to say how much parallelism their plan has. A number
hard-coded in a test would be a number that drifts away from the
deployment it claims to describe.

Marker
------

Every case carries the ``perf`` marker. The CI lane that runs them is
the serial one (``time_sensitive or perf``): a wall-clock bound
measured while eight shards fight over the same cores is not a
measurement, it is a coin toss.

The stand-ins
=============

Three stand-ins stand in for programs that are not installed on a
developer's machine, and each of them is a *file* rather than a mock,
because the thing under test is the process the layer really spawns:

``mcode``
    The MiniMax Code CLI. It records what it was handed — argv, the
    working directory, its own pid and its parent's pid, the prompt
    that arrived on standard input, and the ``.mcp.json`` it found in
    its workspace — and writes the event stream the real CLI writes.

``mcode-profile``
    The tool that owns the account profiles. Every invocation is
    appended to a log, so "how many times were the accounts
    enumerated" is answered by the tool itself rather than by a
    counter in the code that called it.

``sandbox-exec``
    macOS's sandbox wrapper, standing in so the hand-over is
    observable. The real one is exercised by
    ``tests/integration/harness/test_runner_order.py``; here the point
    is the *shape* of the hand-over, and the real one replaces itself
    with the command it runs, which leaves no trace of itself to
    measure.
"""

from __future__ import annotations

import dataclasses
import gc
import itertools
import json
import os
import resource
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Sequence, Tuple

import pytest

import provider_capacity
from harness.contract import TaskInvocation
from harness.mcode import MCODE_BINARY, McodeHarness, parse_stream
from harness.runner import Runner
from mcode_profiles import (
    ProfileBinding,
    list_profiles,
    load_startup_mapping,
    resolve_profile,
)

pytestmark = pytest.mark.perf


# ---------------------------------------------------------------------------
# The bounds
# ---------------------------------------------------------------------------

#: PERF-01 — the start-up overhead this system adds, at p95, in
#: milliseconds. The start-up spawns one external program to learn
#: which accounts exist; a slow machine must not turn that into a slow
#: plan.
STARTUP_OVERHEAD_P95_MS = 2000.0

#: PERF-02 — ``build_invocation`` at p95, in milliseconds. It builds a
#: command line and touches nothing else, so it has no business taking
#: milliseconds.
BUILD_INVOCATION_P95_MS = 5.0

#: PERF-03 — the stream parsing throughput, in lines per second.
PARSE_LINES_PER_SECOND = 20_000.0

#: PERF-04 — the peak resident-memory growth while a million lines are
#: parsed, in mebibytes. The stream is read line by line, so the
#: growth is bounded by what one line costs, not by how many there are.
PARSE_PEAK_RSS_DELTA_MB = 200.0

#: How many samples the latency cases take. Thirty is enough for a
#: p95 to mean something and few enough to stay cheap.
SAMPLES = 30

#: How many iterations run before the measurement starts, so that the
#: first-call cost of a lazily imported module is not charged to the
#: case that happens to run first.
WARMUP = 5

#: PERF-03 — how many lines the throughput case parses.
THROUGHPUT_LINES = 100_000

#: PERF-04 — how many lines the memory case parses.
MEMORY_LINES = 1_000_000

#: PERF-06 — how many tasks the leak case runs.
LEAK_TASKS = 100

#: How long the stand-in CLI works, in seconds. A task that finishes the
#: moment it starts cannot overlap with another one, and the overlap is
#: what makes the concurrent case a concurrency case.
TASK_DURATION_SECONDS = 0.25

#: The account the stand-in profile tool reports, and the provider the
#: stand-in map file maps onto it.
PROFILE_NAME = "acc-1"
PROFILE_PROVIDER = "perf-vendor"


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------


def _percentile(values: Sequence[float], pct: float) -> float:
    """Return the ``pct`` percentile of ``values``.

    Nearest-rank, which is what a perf gate wants: the value a real
    sample actually took, not an interpolation between two of them.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
    index = max(0, min(len(ordered) - 1, index))
    return ordered[index]


def _peak_rss_bytes() -> int:
    """The process's peak resident set size, in bytes.

    macOS reports ``ru_maxrss`` in bytes and Linux in kilobytes. The
    number is a high-water mark, so the *difference* between two reads
    is what says whether this run grew the process — a peak never falls
    back down, which is exactly what makes the difference meaningful.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def _fd_count() -> int:
    """How many descriptors this process holds open."""
    psutil = pytest.importorskip("psutil", reason="psutil counts descriptors")
    return int(psutil.Process().num_fds())


def _thread_count() -> int:
    """How many threads this process holds."""
    psutil = pytest.importorskip("psutil", reason="psutil counts threads")
    return int(psutil.Process().num_threads())


def _live_processes_matching(needle: str) -> List[str]:
    """The command lines of the live processes that mention ``needle``."""
    psutil = pytest.importorskip("psutil", reason="psutil lists processes")
    found: List[str] = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = process.info.get("cmdline") or []
        except (psutil.Error, OSError):  # pragma: no cover - a race with exit
            continue
        if any(needle in part for part in cmdline):
            found.append(" ".join(cmdline))
    return found


# ---------------------------------------------------------------------------
# The stand-ins
# ---------------------------------------------------------------------------

_FAKE_CLI_SOURCE = r'''
"""A stand-in for the ``mcode`` CLI.

It records what the run handed it and writes the event stream the real
CLI writes, so the layer above can be exercised end to end without a
model and without a network.
"""

import json
import os
import sys
import time


def _read_stdin():
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def _mcp_json():
    """The workspace's ``.mcp.json``, if the run left one there."""
    path = os.path.join(os.getcwd(), ".mcp.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _stream():
    session = "mvs_" + "0" * 32
    model = {
        "providerId": "minimax",
        "modelId": "MiniMax-M3.1-Flash",
        "variant": "thinking",
        "providerSource": "provider",
        "providerKind": "minimax-managed",
    }
    usage = {
        "inputTokens": 15095,
        "outputTokens": 3,
        "cacheReadTokens": 1792,
        "totalTokens": 15098,
    }
    yield {"type": "exec.started", "sessionId": session, "runId": "exec_turn_1"}
    yield {"type": "session.started", "sessionId": session}
    yield {"type": "turn.started", "sessionId": session, "turnId": "turn_1"}
    yield {
        "type": "item.completed",
        "sessionId": session,
        "item": {
            "id": "e53a7798-2893-4959-8c0e-ad2ac4cab933:message",
            "type": "agent_message",
            "content": "done",
        },
    }
    yield {
        "type": "turn.completed",
        "sessionId": session,
        "model": model,
        "usage": usage,
        "usageSource": "completed_responses",
        "usageIncomplete": False,
        "durationMs": 12,
    }
    yield {
        "type": "exec.completed",
        "sessionId": session,
        "result": {
            "type": "exec.result",
            "runId": "exec_turn_1",
            "sessionId": session,
            "turnId": "turn_1",
            "status": "succeeded",
            "output": "done",
            "model": model,
            "usage": usage,
            "usageSource": "completed_responses",
            "usageIncomplete": False,
            "durationMs": 12,
        },
    }


def main():
    out_path = os.environ.get("FAKE_CLI_OUT", "")
    started = time.time()
    stdin_text = _read_stdin()
    mcp_text = _mcp_json()
    delay = os.environ.get("FAKE_CLI_DELAY", "")
    if delay:
        time.sleep(float(delay))
    finished = time.time()

    report = {
        "argv": list(sys.argv),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "stdin": stdin_text,
        "mcp_json": mcp_text,
        "started": started,
        "finished": finished,
    }
    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True)

    for event in _stream():
        sys.stdout.write(json.dumps(event, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_FAKE_PROFILE_TOOL_SOURCE = r'''
"""A stand-in for the ``mcode-profile`` tool.

Every invocation is appended to a log, so "how many times were the
accounts enumerated" is answered by the tool itself and not by a
counter in the code that called it.
"""

import json
import os
import sys


def main():
    log = os.environ.get("FAKE_PROFILE_LOG", "")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(list(sys.argv[1:])) + "\n")

    home = os.environ.get("FAKE_PROFILE_HOME", "/tmp")
    document = {
        "current": "acc-1",
        "profiles": [
            {"name": "acc-1", "home": home, "login_state": "none"},
            {"name": "acc-2", "home": home, "login_state": "none"},
        ],
    }
    sys.stdout.write(json.dumps(document))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_FAKE_SANDBOX_SOURCE = r'''
"""A stand-in for macOS's ``sandbox-exec``.

It does what the real one does with the argv it is handed: hand over to
the command behind ``--`` by replacing itself with it. The point of the
stand-in is that the hand-over is *observable* — the command's process
is the wrapper's process, so the command's parent is the process that
spawned the wrapper and nothing in between.
"""

import json
import os
import sys


def main():
    argv = list(sys.argv)
    log = os.environ.get("FAKE_SANDBOX_LOG", "")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")

    rest = argv[1:]
    while rest and rest[0] in ("-f", "-p"):
        rest = rest[2:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        return 2

    os.execvp(rest[0], rest)
    return 127


if __name__ == "__main__":
    sys.exit(main())
'''


def _write_executable(path: Path, source: str) -> Path:
    """Write ``source`` as an executable script at ``path``."""
    interpreter = sys.executable
    shebang = (
        f"#!{interpreter}\n"
        if interpreter and " " not in interpreter
        else "#!/usr/bin/env python3\n"
    )
    path.write_text(shebang + source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _prepend_to_path(monkeypatch, directory: Path) -> None:
    """Put ``directory`` at the front of ``PATH`` for this test."""
    monkeypatch.setenv(
        "PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
    )


def _install_fake_cli(tmp_path: Path, monkeypatch) -> Path:
    """Put the stand-in ``mcode`` CLI on ``PATH``; return its path."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    cli = _write_executable(bin_dir / MCODE_BINARY, _FAKE_CLI_SOURCE)
    _prepend_to_path(monkeypatch, bin_dir)
    return cli


def _install_fake_profile_tool(tmp_path: Path, monkeypatch) -> Tuple[Path, Path]:
    """Put the stand-in profile tool on ``PATH``.

    Returns the tool's path and the log it appends every invocation to.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    tool = _write_executable(bin_dir / "mcode-profile", _FAKE_PROFILE_TOOL_SOURCE)
    _prepend_to_path(monkeypatch, bin_dir)

    log = tmp_path / "profile-enumerations.log"
    monkeypatch.setenv("FAKE_PROFILE_LOG", str(log))
    home = tmp_path / "profile-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("FAKE_PROFILE_HOME", str(home))
    return tool, log


def _install_fake_sandbox(tmp_path: Path, monkeypatch) -> Tuple[Path, Path]:
    """Put the stand-in ``sandbox-exec`` on ``PATH``.

    Returns the wrapper's path and the log it appends every invocation
    to.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    wrapper = _write_executable(bin_dir / "sandbox-exec", _FAKE_SANDBOX_SOURCE)
    _prepend_to_path(monkeypatch, bin_dir)

    log = tmp_path / "sandbox-invocations.log"
    monkeypatch.setenv("FAKE_SANDBOX_LOG", str(log))
    return wrapper, log


def _enumeration_count(log: Path) -> int:
    """How many times the profile tool was run."""
    if not log.exists():
        return 0
    return len([line for line in log.read_text(encoding="utf-8").splitlines() if line])


def _no_sandbox(monkeypatch, tmp_path: Path) -> None:
    """Make sure no sandbox profile is configured for the run."""
    monkeypatch.delenv("PDT_SANDBOX_PROFILE", raising=False)
    empty_config = tmp_path / "empty-config"
    empty_config.mkdir(exist_ok=True)
    monkeypatch.setenv("PDT_CONFIG_DIR", str(empty_config))


def _binding(tmp_path: Path) -> ProfileBinding:
    """A profile binding whose home is a directory of this test's own."""
    home = tmp_path / "account-home"
    home.mkdir(exist_ok=True)
    return ProfileBinding(name=PROFILE_NAME, home=str(home))


def _mcp_config(index: int) -> str:
    """An MCP configuration that belongs to exactly one task."""
    return json.dumps(
        {"mcpServers": {f"perf-server-{index}": {"command": "true"}}}
    )


# ---------------------------------------------------------------------------
# PERF-01 — the start-up cost, and that it is paid once
# ---------------------------------------------------------------------------


def test_startup_overhead_within_budget(tmp_path, monkeypatch):
    """The start-up this system adds costs at most two seconds at p95.

    The start-up spawns one external program — the tool that knows
    which accounts exist — and then reads the account map. Both are
    paid once, before any task runs, and the number below is what a
    plan pays for that.
    """
    _install_fake_profile_tool(tmp_path, monkeypatch)
    monkeypatch.setenv("PDT_HARNESS", "mcode")

    map_file = tmp_path / "mcode_profile_map.yaml"
    map_file.write_text(
        f"profiles:\n  {PROFILE_PROVIDER}: {PROFILE_NAME}\n", encoding="utf-8"
    )

    for _ in range(WARMUP):
        load_startup_mapping(map_file=map_file)

    samples: List[float] = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        load_startup_mapping(map_file=map_file)
        samples.append((time.perf_counter() - started) * 1000.0)

    p95 = _percentile(samples, 95)
    print(
        f"\nPERF-01 start-up overhead: p50={_percentile(samples, 50):.1f}ms "
        f"p95={p95:.1f}ms max={max(samples):.1f}ms "
        f"(bound {STARTUP_OVERHEAD_P95_MS:.0f}ms)"
    )
    assert p95 <= STARTUP_OVERHEAD_P95_MS, (
        f"the start-up overhead is {p95:.1f}ms at p95, over the "
        f"{STARTUP_OVERHEAD_P95_MS:.0f}ms bound"
    )


def test_profile_enumerated_once(tmp_path, monkeypatch):
    """Five tasks cost one enumeration of the accounts, not five.

    The accounts exist before the first task runs and do not change
    while it runs, so the enumeration is a start-up cost. A layer that
    enumerated per task would spawn a program once per task, and a
    plan holds dozens of tasks.
    """
    _install_fake_profile_tool(tmp_path, monkeypatch)
    _install_fake_cli(tmp_path, monkeypatch)
    _no_sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("PDT_HARNESS", "mcode")

    map_file = tmp_path / "mcode_profile_map.yaml"
    map_file.write_text(
        f"profiles:\n  {PROFILE_PROVIDER}: {PROFILE_NAME}\n", encoding="utf-8"
    )

    # The start-up: enumerate the accounts, then check the map against
    # them. One enumeration.
    profiles = list_profiles()
    mapping = load_startup_mapping(profiles=profiles, map_file=map_file)
    assert mapping == {PROFILE_PROVIDER: PROFILE_NAME}
    assert _enumeration_count(Path(os.environ["FAKE_PROFILE_LOG"])) == 1

    binding = resolve_profile(mapping, PROFILE_PROVIDER, profiles)
    assert binding is not None
    harness = McodeHarness(binding)

    # Five tasks, each one a real spawn through the real runner.
    for index in range(5):
        workspace = tmp_path / f"ws-{index}"
        workspace.mkdir()
        request = TaskInvocation(
            prompt=f"task {index}",
            cwd=str(workspace),
            model="minimax/MiniMax-M3.1-Flash",
        )
        invocation = harness.build_invocation(request)
        result = Runner().run(harness, invocation, stdin_text=f"task {index}")
        assert result.exit_code == 0

    assert _enumeration_count(Path(os.environ["FAKE_PROFILE_LOG"])) == 1, (
        "the accounts were enumerated again while the tasks ran; the "
        "enumeration is a start-up cost, not a per-task one"
    )


# ---------------------------------------------------------------------------
# PERF-02 — building the command line
# ---------------------------------------------------------------------------


def test_build_invocation_latency(tmp_path):
    """``build_invocation`` is a p95 of five milliseconds or less.

    It builds a command line and reads nothing, so it has no business
    taking milliseconds — and a plan pays it once per task.
    """
    harness = McodeHarness(_binding(tmp_path))
    request = TaskInvocation(
        prompt="do the thing",
        cwd=str(tmp_path),
        model="minimax/MiniMax-M3.1-Flash",
        mcp_config=_mcp_config(0),
    )

    for _ in range(WARMUP):
        harness.build_invocation(request)

    samples: List[float] = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        invocation = harness.build_invocation(request)
        samples.append((time.perf_counter() - started) * 1000.0)

    p95 = _percentile(samples, 95)
    print(
        f"\nPERF-02 build_invocation: p50={_percentile(samples, 50):.3f}ms "
        f"p95={p95:.3f}ms max={max(samples):.3f}ms "
        f"(bound {BUILD_INVOCATION_P95_MS:.1f}ms)"
    )

    # The measurement is of a real command line, not of a no-op.
    assert invocation.argv[0] == MCODE_BINARY
    assert invocation.prepare is not None

    assert p95 <= BUILD_INVOCATION_P95_MS, (
        f"build_invocation took {p95:.3f}ms at p95, over the "
        f"{BUILD_INVOCATION_P95_MS:.1f}ms bound"
    )


# ---------------------------------------------------------------------------
# PERF-03 — reading the stream
# ---------------------------------------------------------------------------


def _stream_lines() -> List[str]:
    """A realistic mcode stream, as the lines the CLI writes."""
    session = "mvs_" + "0" * 32
    model = {
        "providerId": "minimax",
        "modelId": "MiniMax-M3.1-Flash",
        "variant": "thinking",
        "providerSource": "provider",
        "providerKind": "minimax-managed",
    }
    usage = {
        "inputTokens": 15095,
        "outputTokens": 3,
        "cacheReadTokens": 1792,
        "totalTokens": 15098,
    }
    events = [
        {"type": "exec.started", "sessionId": session, "runId": "exec_turn_1"},
        {"type": "session.started", "sessionId": session},
        {"type": "turn.started", "sessionId": session, "turnId": "turn_1"},
        {
            "type": "item.completed",
            "sessionId": session,
            "item": {
                "id": "e53a7798-2893-4959-8c0e-ad2ac4cab933:message",
                "type": "agent_message",
                "content": "done",
            },
        },
        {
            "type": "turn.completed",
            "sessionId": session,
            "model": model,
            "usage": usage,
            "usageSource": "completed_responses",
            "usageIncomplete": False,
            "durationMs": 12,
        },
        {
            "type": "exec.completed",
            "sessionId": session,
            "result": {
                "type": "exec.result",
                "runId": "exec_turn_1",
                "sessionId": session,
                "turnId": "turn_1",
                "status": "succeeded",
                "output": "done",
                "model": model,
                "usage": usage,
                "usageSource": "completed_responses",
                "usageIncomplete": False,
                "durationMs": 12,
            },
        },
    ]
    return [json.dumps(event) for event in events]


class _CountingLines:
    """An iterable of lines that remembers how many it handed out."""

    def __init__(self, source: Iterable[str]) -> None:
        self._source = source
        self.consumed = 0

    def __iter__(self) -> Iterator[str]:
        for line in self._source:
            self.consumed += 1
            yield line


def test_parse_stream_throughput():
    """The stream parses at twenty thousand lines a second or more.

    The lines are the ones the real CLI writes, and the measurement is
    of the parser alone: the lines are prepared before the clock
    starts, so what is measured is the work the layer adds and not the
    work of producing the input.
    """
    lines = _stream_lines()
    # The stream is finite: a run ends, and the parser is handed an
    # end. ``islice`` is what gives the cycle its end.
    stream = _CountingLines(itertools.islice(itertools.cycle(lines), THROUGHPUT_LINES))

    started = time.perf_counter()
    count = 0
    last_type = ""
    for event in parse_stream(stream):
        count += 1
        last_type = event.type
    elapsed = time.perf_counter() - started

    rate = THROUGHPUT_LINES / elapsed if elapsed > 0 else float("inf")
    print(
        f"\nPERF-03 parse throughput: {rate:,.0f} lines/s "
        f"({count:,} events in {elapsed:.3f}s, "
        f"bound {PARSE_LINES_PER_SECOND:,.0f} lines/s)"
    )

    # The measurement is of a real parse.
    assert stream.consumed == THROUGHPUT_LINES
    assert last_type == "result"

    assert rate >= PARSE_LINES_PER_SECOND, (
        f"the stream parsed at {rate:,.0f} lines/s, under the "
        f"{PARSE_LINES_PER_SECOND:,.0f} lines/s bound"
    )


# ---------------------------------------------------------------------------
# PERF-04 — the stream does not pile up
# ---------------------------------------------------------------------------


def test_parse_memory_is_bounded():
    """A million lines grow the process by at most 200 mebibytes.

    The stream is read line by line and never held as one blob, so the
    memory a run costs is what one line costs and not what the whole
    run said.
    """
    lines = _stream_lines()
    # The stream is finite: a run ends, and the parser is handed an
    # end. ``islice`` is what gives the cycle its end.
    stream = _CountingLines(itertools.islice(itertools.cycle(lines), MEMORY_LINES))

    before = _peak_rss_bytes()
    count = 0
    for _event in parse_stream(stream):
        count += 1
    after = _peak_rss_bytes()

    delta_mb = (after - before) / (1024.0 * 1024.0)
    print(
        f"\nPERF-04 parse memory: {delta_mb:.1f} MiB peak growth over "
        f"{stream.consumed:,} lines (bound {PARSE_PEAK_RSS_DELTA_MB:.0f} MiB)"
    )

    # The measurement is of a real parse of a million lines.
    assert stream.consumed == MEMORY_LINES
    assert count > 0

    assert delta_mb <= PARSE_PEAK_RSS_DELTA_MB, (
        f"parsing {MEMORY_LINES:,} lines grew the process by "
        f"{delta_mb:.1f} MiB, over the {PARSE_PEAK_RSS_DELTA_MB:.0f} MiB bound"
    )


# ---------------------------------------------------------------------------
# PERF-05 — concurrent tasks do not contaminate each other
# ---------------------------------------------------------------------------


def _peak_overlap(intervals: Sequence[Tuple[float, float]]) -> int:
    """How many of ``intervals`` were open at the same moment."""
    events: List[Tuple[float, int]] = []
    for start, finish in intervals:
        events.append((start, 1))
        events.append((finish, -1))
    events.sort()
    peak = 0
    current = 0
    for _moment, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def test_concurrent_tasks_are_isolated(tmp_path, monkeypatch, capacity_config):
    """Concurrent tasks do not contaminate each other's workspace.

    The concurrency is the sum of the caps in ``provider_capacity.yaml``
    — the file the operator uses to say how much parallelism their
    deployment has. It is read, not written down here.

    Two things are pinned. First, a task's MCP servers are its own: the
    configuration one task wrote into its workspace is never seen by
    another task, and the workspace is put back afterwards. Second, a
    task leaves nothing behind: no process outlives it.
    """
    capacity_config([("^perf-vendor-a", 3), ("^perf-vendor-b", 5)])
    concurrency = provider_capacity.configured_capacity_ceiling()
    assert concurrency >= 2, (
        "the capacity file declares no parallelism; the case would not "
        "be exercising concurrency at all"
    )

    cli = _install_fake_cli(tmp_path, monkeypatch)
    _no_sandbox(monkeypatch, tmp_path)

    harness = McodeHarness(_binding(tmp_path))

    def run_one(index: int) -> Tuple[Any, Path, Path]:
        workspace = tmp_path / f"ws-{index}"
        workspace.mkdir()
        request = TaskInvocation(
            prompt=f"task {index}",
            cwd=str(workspace),
            mcp_config=_mcp_config(index),
        )
        invocation = harness.build_invocation(request)
        report = tmp_path / f"report-{index}.json"
        invocation = dataclasses.replace(
            invocation,
            env_delta={
                **invocation.env_delta,
                "FAKE_CLI_OUT": str(report),
                # A task that finishes the moment it starts cannot
                # overlap with anything, and the overlap is what makes
                # this a concurrency case rather than a sequence of
                # runs. The stand-in therefore works for a moment.
                "FAKE_CLI_DELAY": str(TASK_DURATION_SECONDS),
            },
        )
        result = Runner().run(harness, invocation, stdin_text=f"task {index}")
        return result, report, workspace

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        outcomes = list(pool.map(run_one, range(concurrency)))

    reports: List[Dict[str, Any]] = []
    for index, (result, report, workspace) in enumerate(outcomes):
        assert result.exit_code == 0, f"task {index} did not finish cleanly"
        assert result.outcome.kind == "ok"

        payload = json.loads(report.read_text(encoding="utf-8"))
        reports.append(payload)

        # The task saw its own servers and nobody else's.
        servers = json.loads(payload["mcp_json"])["mcpServers"]
        assert set(servers) == {f"perf-server-{index}"}, (
            f"task {index} saw the servers {sorted(servers)}; a task's MCP "
            "configuration must not reach another task's workspace"
        )

        # And the workspace was put back.
        assert not (workspace / ".mcp.json").exists()

    # The tasks really did run at the same time — otherwise the case
    # would be a sequence of runs wearing a concurrent hat.
    overlap = _peak_overlap(
        [(payload["started"], payload["finished"]) for payload in reports]
    )
    print(
        f"\nPERF-05 {concurrency} concurrent tasks (capacity from "
        f"provider_capacity.yaml): peak overlap {overlap}"
    )
    assert overlap >= 2, f"the tasks never overlapped (peak {overlap})"

    # Nothing outlived its task.
    orphans = _live_processes_matching(str(cli))
    assert not orphans, f"tasks left processes behind: {orphans}"


# ---------------------------------------------------------------------------
# PERF-06 — nothing leaks
# ---------------------------------------------------------------------------


def test_no_fd_or_thread_leak(tmp_path, monkeypatch):
    """A hundred tasks leave the descriptors and threads where they were.

    Every task spawns a process, opens three pipes and starts a thread
    to drain the child's standard error. A hundred tasks that each left
    one descriptor or one thread behind would take the process to the
    point where it cannot open a file any more.
    """
    _install_fake_cli(tmp_path, monkeypatch)
    _no_sandbox(monkeypatch, tmp_path)

    harness = McodeHarness(_binding(tmp_path))

    # The first task pays whatever one-time cost there is, so the
    # baseline is taken after it.
    def run_one(index: int) -> None:
        workspace = tmp_path / f"ws-{index}"
        workspace.mkdir()
        request = TaskInvocation(prompt=f"task {index}", cwd=str(workspace))
        invocation = harness.build_invocation(request)
        result = Runner().run(harness, invocation, stdin_text=f"task {index}")
        assert result.exit_code == 0

    run_one(0)
    gc.collect()
    baseline_fds = _fd_count()
    baseline_threads = _thread_count()

    for index in range(1, LEAK_TASKS):
        run_one(index)

    gc.collect()
    final_fds = _fd_count()
    final_threads = _thread_count()

    fd_delta = final_fds - baseline_fds
    thread_delta = final_threads - baseline_threads
    print(
        f"\nPERF-06 after {LEAK_TASKS} tasks: "
        f"descriptors {baseline_fds} -> {final_fds} ({fd_delta:+d}), "
        f"threads {baseline_threads} -> {final_threads} ({thread_delta:+d})"
    )

    assert abs(fd_delta) <= 2, (
        f"{LEAK_TASKS} tasks moved the descriptor count by {fd_delta} "
        f"(from {baseline_fds} to {final_fds}); the tolerance is 2"
    )
    assert abs(thread_delta) <= 2, (
        f"{LEAK_TASKS} tasks moved the thread count by {thread_delta} "
        f"(from {baseline_threads} to {final_threads}); the tolerance is 2"
    )


# ---------------------------------------------------------------------------
# PERF-07 — one wrapper, one CLI, one hand-over
# ---------------------------------------------------------------------------


def test_one_process_per_task_without_a_wrapper(tmp_path, monkeypatch):
    """A task with no wrapper costs exactly one process.

    The layer above the runner must not add a process of its own: the
    CLI is spawned directly, and the CLI's parent is the runner. A
    layer that forked an intermediate would make every task cost two
    processes, and a plan holds dozens of tasks.
    """
    cli = _install_fake_cli(tmp_path, monkeypatch)
    _no_sandbox(monkeypatch, tmp_path)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    report = tmp_path / "report.json"
    invocation = McodeHarness(_binding(tmp_path)).build_invocation(
        TaskInvocation(prompt="one task", cwd=str(workspace))
    )
    invocation = dataclasses.replace(
        invocation,
        env_delta={**invocation.env_delta, "FAKE_CLI_OUT": str(report)},
    )

    result = Runner().run(McodeHarness(), invocation, stdin_text="one task")

    assert result.exit_code == 0
    assert result.sandbox_state == "unsandboxed"

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ppid"] == os.getpid(), (
        "the CLI was not spawned by the runner itself; something in "
        "between cost an extra process per task"
    )
    assert payload["argv"][0] == str(cli) or payload["argv"][0] == MCODE_BINARY


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the sandbox wrapper is macOS-only, and a configured profile "
    "on another platform is a configuration error rather than something "
    "that can be applied",
)
def test_one_wrapper_one_cli_per_task(tmp_path, monkeypatch):
    """A task costs one wrapper and one CLI, and the wrapper execs.

    The wrapper is the sandbox the operator configured. It is put in
    front of the CLI once — not twice — and it hands over with
    ``exec``: the wrapper's process *becomes* the CLI, so the task
    costs one process rather than two. The evidence is the CLI's own
    parent, which is the runner and not the wrapper.
    """
    _install_fake_cli(tmp_path, monkeypatch)
    wrapper, wrapper_log = _install_fake_sandbox(tmp_path, monkeypatch)

    profile = tmp_path / "profile.sb"
    profile.write_text("(version 1)\n(allow default)\n", encoding="utf-8")
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(profile))

    workspace = tmp_path / "ws"
    workspace.mkdir()
    report = tmp_path / "report.json"
    invocation = McodeHarness(_binding(tmp_path)).build_invocation(
        TaskInvocation(prompt="one task", cwd=str(workspace))
    )
    invocation = dataclasses.replace(
        invocation,
        env_delta={**invocation.env_delta, "FAKE_CLI_OUT": str(report)},
    )

    result = Runner().run(McodeHarness(), invocation, stdin_text="one task")

    assert result.exit_code == 0
    assert result.sandbox_state == "applied", "the wrapper was not applied"

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ppid"] == os.getpid(), (
        "the CLI's parent was not the runner; the wrapper forked instead "
        "of handing over with exec, so the task cost two processes"
    )

    # The wrapper was asked to run exactly one command line, and that
    # command line was one CLI.
    invocations = [
        json.loads(line)
        for line in wrapper_log.read_text(encoding="utf-8").splitlines()
        if line
    ]
    wrapped = [argv for argv in invocations if MCODE_BINARY in argv]
    assert len(wrapped) == 1, (
        f"the task's command line was wrapped {len(wrapped)} times; it "
        "must be wrapped exactly once"
    )
    argv = wrapped[0]
    assert Path(argv[0]).resolve() == wrapper.resolve()
    assert argv.count("--") == 1
    separator = argv.index("--")
    head, tail = argv[:separator], argv[separator + 1:]
    assert head[1:] == ["-f", str(profile)]
    assert tail[0] == MCODE_BINARY
