"""The whole dual-backend chain, accepted end to end, with the evidence kept.

Why this file exists
====================

Four scenarios came before this one, and each of them answered one
question about one backend:

  * ``E2E-MCODE`` — the second backend, end to end, under the real
    seatbelt;
  * ``E2E-CLAUDE`` — the first backend, end to end, as the baseline;
  * ``E2E-PARITY`` — the two of them, side by side;
  * ``E2E-SESSION`` — a real continuation, and what happens when the
    map is missing.

None of them can answer the question an operator actually has: *does
the whole thing hold, on both backends, in one run, and can I check
your answer afterwards?*  A reader who has to take four files and two
runs to learn whether the chain works has no answer they can rely on.

So this file runs the acceptance once.  Both backends, both servers,
the fixed task, the four criteria, and one directory of evidence that
says what happened and why.

The four criteria
=================

**KPI1 — the task's output.**  Both backends are given the same fixed
task, and both have to leave the same artefact behind: a ``hello.txt``
whose content hash is the same on both sides.  Two runs that both
wrote ``hello.txt`` but wrote different bytes into it have not done
the same thing, so the comparison is on the hash and not on the file
name.

**KPI2 — the live fields.**  A run that produced a file but reported
nothing about itself is not a run anybody can bill, resume or audit.
So each side has to report: the four canonical usage keys, a positive
input count, no error, and a session id in the shape its own CLI
mints.  The two shapes differ on purpose — the MiniMax CLI mints a
``mvs_`` conversation, the claude CLI runs under the pinned uuid5 the
executor derived — and a criterion that insisted on one shape would be
a criterion about the naming.

**KPI3 — a failure that says what failed.**  The other three criteria
are about runs that worked.  This one is about a run that could not:
a backend that is not installed was named, and the failure has to
*name* that backend.  A failure that names nothing is
indistinguishable from a crash, and a run that quietly answered
through a different backend would spend another provider's money and
report a success that belongs to a backend nobody asked for.

**KPI4 — the evidence.**  A verdict nobody can check is an opinion.
So the run keeps what it saw: the harness's raw event stream and the
service's own record of the run (both redacted), the ``.mcp.json``
restore record, and the process-table samples — next to the summary
that says which criterion passed and why.

The archive
===========

Every run writes ``artifacts/harness-runs/<run_id>/summary.json``,
where ``<run_id>`` is the CI run id when there is one and a
timestamped local id otherwise.  The directory is the same one the
release lane archives, at the same place in the tree, so the evidence
a local run produces is the evidence the release lane publishes.

The summary carries one verdict per criterion, each with a reason, and
it is written whether the criteria ran or not: a criterion that did
not run says so, and says why.  A summary that only exists when
everything went well is a summary that is missing exactly when it is
needed.

The archive and the index
=========================

The evidence directory is a run's *output*, not a source file, and it
has to be unable to become a tracked directory.  The repository's
privacy gates read the top-level directories the repository tracks,
and a top-level ``artifacts/`` that a ``git add -A`` swept into the
index is a directory no gate reads and nothing says so.

So the run drops an ignore marker into the directory it owns --
``artifacts/harness-runs/.gitignore``, one line, ``*``.  Git honours
it, and the evidence becomes invisible to the index: ``git status``
stays clean, ``git add -A`` stages nothing, and ``git ls-tree`` still
reports no ``artifacts/``.  The marker sits *inside* the evidence
directory, so it covers the evidence and nothing else -- a file
written next to it is still seen.  The case below asserts this, so a
later change that moved the archive somewhere the index can see fails
here rather than in a gate that reads a different directory.

The evidence is checked by what it says
=======================================

A file that exists is not evidence.  Every evidence file is opened and
read, and what it has to say is checked: the raw stream has to hold
the harness's own events and a final result; the service log has to
hold the run's own events; the ``.mcp.json`` record has to say how
the configuration reached *this* backend, because the two backends
deliver it differently and only one of them has anything to restore;
the process sample has to show the runner that was sampled and no
orphan; the negative control has to name the backend that is missing.
A file that is present and empty passes a check that asks whether the
file is there, and tells a reader nothing at all.

What this file does *not* do
============================

It does not drive ``POST /api/plan/import`` and
``POST /api/execution/{plan_id}/start``.  Those two doors are the
operator's, and they are worth what they cost — a model call to author
a task list, and an executor to run it.  The acceptance does not need
either: its task is already authored, and the fields KPI2 asks about
(session id, usage block, sandbox state) live on the *dispatch's*
result object, which the HTTP API does not expose.  That is the same
reason the scenarios below drive the dispatch rather than reading it
back out of a log, and it is stated at length in ``E2E-CLAUDE``.

What this file does do is boot both servers for real — one per
backend, each with its own ``PDT_HARNESS``, its own plans directory,
its own state database and its own seatbelt profile — and speak to
them the way a client speaks to them, with the guard header.

Cost, and who runs it
=====================

The scenarios cost two real model calls and two real servers, so they
are off unless ``PDT_E2E_HARNESS=1`` is set; the deterministic gates
only *collect* them.  The negative control and the archive case cost
no model call and are not gated: they are the two halves of the
acceptance that have to hold in every lane, because they are the two
halves that can be checked without spending anybody's money.

Nothing survives a run.  Both servers are reclaimed, their temporary
trees are removed, and the process table is checked for what the runs
left behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

# The machinery the scenarios below produced.  The directory is on the
# path because pytest puts it there when it imports this module.
from test_e2e_claude import (  # noqa: E402 — the import needs the path above
    CLAUDE,
    CLAUDE_MODEL,
    E2E_ENV_VAR,
    HELLO_HASHES,
    MCODE,
    MCODE_MODEL,
    PROJECT_ROOT,
    TARGET_STDOUT,
    USAGE_KEYS,
    ScenarioResult,
    ScenarioServer,
    _server,
    run_scenario,
)

# The negative control's world: the recording ``claude``, the instrument
# that proves the recording can record, and the script whose failure has
# to name the backend.  Imported rather than restated so the acceptance
# and the control cannot drift apart: the acceptance reads the failure
# the control produces.
from test_harness_negative_control import (  # noqa: E402
    BACKEND_DIR,
    CONTROL_TIMEOUT,
    HARNESS_NAME as MISSING_BACKEND,
    SYSTEM_PATH,
    VENV_PYTHON,
    _FIRST_TASK,
    _control_environment,
    _failure_text,
    _prove_the_instrument,
    _recording_claude,
)

# --------------------------------------------------------------------------
# The four criteria
# --------------------------------------------------------------------------

#: The criteria, by name.  The names are the contract: the summary this
#: run writes carries one verdict per name, and a reader looks a failure
#: up by the name of the criterion it broke.
KPI_TASK_OUTPUT = "kpi1_task_output"
KPI_LIVE_FIELDS = "kpi2_live_fields"
KPI_READABLE_ERROR = "kpi3_readable_error"
KPI_EVIDENCE_ARCHIVE = "kpi4_evidence_archive"
KPI_NAMES: Tuple[str, ...] = (
    KPI_TASK_OUTPUT,
    KPI_LIVE_FIELDS,
    KPI_READABLE_ERROR,
    KPI_EVIDENCE_ARCHIVE,
)

#: The three verdicts a criterion can carry.  ``SKIPPED`` is not a
#: softer ``PASSED``: it says the criterion did not run, and the reason
#: says why.
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

#: The shape of a session id the MiniMax Code CLI mints.  A pinned id
#: is a key, not a session id — the CLI cannot create a conversation
#: under a chosen one — so the id that comes back has to look like this.
MCODE_SESSION_RE = re.compile(r"^mvs_[0-9a-f]{32}$")

# --------------------------------------------------------------------------
# Where the evidence goes
# --------------------------------------------------------------------------

#: The variable that names the run's evidence directory.  CI names its
#: own (``GITHUB_RUN_ID``); a local run names its own.
RUN_ID_ENV = "PDT_E2E_RUN_ID"

#: The variable that moves the evidence somewhere else.  The default is
#: the place the release lane archives, and a caller who wants the
#: evidence somewhere else says so rather than moving it afterwards.
ARTIFACTS_DIR_ENV = "PDT_E2E_ARTIFACTS_DIR"

#: The name of the summary inside a run's directory.
SUMMARY_NAME = "summary.json"

#: The name of the ignore marker the run drops into the evidence
#: directory.  One line, ``*``: the evidence is a run's output, and the
#: index has to be unable to see it.
IGNORE_NAME = ".gitignore"

#: The contents of that marker.  Everything inside the evidence
#: directory, including the marker itself, and nothing outside it.
IGNORE_CONTENTS = "*\n"

#: How each backend delivers the MCP configuration to the harness.
#:
#: The MiniMax adapter writes ``.mcp.json`` into the workspace for the
#: length of the run and takes it out again afterwards; the claude
#: adapter hands the same file to the CLI on the command line and
#: never writes it into the workspace at all.  These are two different
#: events, and the restore record has to say which one happened -- a
#: record that said "nothing was restored" for both would not tell a
#: reader whether the injection happened and the restore did not, or
#: whether there was never anything to restore.
MCP_DELIVERY: Dict[str, Dict[str, bool]] = {
    MCODE: {
        "mcp_json_seen_during_run": True,
        "mcp_config_on_command_line": False,
    },
    CLAUDE: {
        "mcp_json_seen_during_run": False,
        "mcp_config_on_command_line": True,
    },
}

# --------------------------------------------------------------------------
# What the acceptance produces
# --------------------------------------------------------------------------


@dataclass
class _Verdict:
    """One criterion's outcome.

    Attributes:
        name: The criterion's name.
        status: :data:`PASSED`, :data:`FAILED` or :data:`SKIPPED`.
        reason: Why.  Never empty: a verdict without a reason is a
            verdict nobody can act on.
        artifacts: The evidence files that back this verdict, by name.
    """

    name: str
    status: str
    reason: str
    artifacts: Tuple[str, ...] = ()


@dataclass
class _Outcome:
    """What one backend's run produced, read back into plain data.

    Everything the criteria ask about, and nothing that needs the run
    to still be alive: the summary is written after the servers are
    gone, so every fact here is a fact that was read while the run's
    world still existed.

    Attributes:
        harness: The backend that produced the run.
        model: The model that backend was pinned to.
        plan_id: The plan the run belongs to.
        hello_sha256: The content hash of the artefact the task had to
            produce, or ``None`` when there is no artefact.
        usage: The usage block, as the CLI reported it.
        usage_reported_keys: Every key the usage block carried.
        usage_partial: Whether the usage count is a partial count.
        is_error: Whether the run reported an error.
        exit_code: The CLI's own exit code.
        session_id: The conversation the run happened in.
        pinned_session_id: The session id that was pinned before the
            run.
        sandbox_state: What the sandbox did with the run.
        changed_files: The files the run changed, as
            ``(relative path, sha256)`` pairs.
        orphans: What was still running when the run ended.
        sampled_pids: Every process seen while the run was in flight.
        runner_pid: The process the runner spawned.
        mcp_json_seen: Whether the workspace held a ``.mcp.json``
            while the run was in flight.
        mcp_json_present_after_run: Whether it still holds one.
        mcp_config_on_command_line: Whether the configuration travelled
            on the command line instead.
        git_status: The workspace's ``git status --porcelain``.
        marker_written: Whether the MCP server the configuration named
            was ever started.
        escape_refused: Whether the write outside the workspace was
            refused.
        escape_errno: The errno the refusal reported.
        dispatch_error: What the dispatch raised, when it raised.
        raw_stream_lines: How many lines the raw event stream held.
    """

    harness: str
    model: str
    plan_id: str
    hello_sha256: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    usage_reported_keys: Tuple[str, ...] = ()
    usage_partial: Optional[bool] = None
    is_error: Optional[bool] = None
    exit_code: Optional[int] = None
    session_id: Optional[str] = None
    pinned_session_id: str = ""
    sandbox_state: Optional[str] = None
    changed_files: Tuple[Tuple[str, str], ...] = ()
    orphans: Tuple[str, ...] = ()
    sampled_pids: Tuple[int, ...] = ()
    runner_pid: Optional[int] = None
    mcp_json_seen: bool = False
    mcp_json_present_after_run: bool = False
    mcp_config_on_command_line: bool = False
    git_status: str = ""
    marker_written: bool = False
    escape_refused: bool = False
    escape_errno: Optional[int] = None
    dispatch_error: Optional[str] = None
    raw_stream_lines: int = 0


@dataclass
class _ServerFacts:
    """What one server was, and what was left of it.

    Attributes:
        harness: The backend the server drove.
        base_url: The root URL it answered on.
        port: The loopback port it was given.
        root: The temporary tree it lived in.
        health: What its guarded health probe answered.
        process_exit_code: The exit code of its process, or ``None``
            while it is still running.
        root_exists: Whether its temporary tree is still there.
        leftover_processes: The processes that were still carrying this
            run's marker after it ended.
    """

    harness: str
    base_url: str
    port: int
    root: str
    health: Any = None
    process_exit_code: Optional[int] = None
    root_exists: bool = True
    leftover_processes: Tuple[str, ...] = ()


@dataclass
class _ControlFacts:
    """What the negative control produced.

    Attributes:
        backend: The backend the run was configured with.
        selected: The backend the service actually selected.
        exit_code: The child's exit code.
        failure: The line the child reported as the failure, or
            ``None`` when it reported none.
        fallbacks: How many times the recording ``claude`` was run.
        output: The child's whole output, redacted.
    """

    backend: str
    selected: str = ""
    exit_code: int = 0
    failure: Optional[str] = None
    fallbacks: int = 0
    output: str = ""


@dataclass
class _AcceptanceRun:
    """Everything the acceptance produced.

    Attributes:
        run_id: The name the evidence is kept under.
        archive_dir: The directory the evidence is in.
        summary: The summary, as it was written.
        verdicts: One verdict per criterion.
        outcomes: What each backend's run produced.
        servers: What each server was, and what was left of it.
        control: What the negative control produced.
        scenarios_ran: Whether the real scenarios ran at all.
        evidence: The evidence files, by name.
    """

    run_id: str
    archive_dir: Path
    summary: Dict[str, Any] = field(default_factory=dict)
    verdicts: Dict[str, _Verdict] = field(default_factory=dict)
    outcomes: Dict[str, _Outcome] = field(default_factory=dict)
    servers: Dict[str, _ServerFacts] = field(default_factory=dict)
    control: Optional[_ControlFacts] = None
    scenarios_ran: bool = False
    evidence: Tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _run_id() -> str:
    """The name this run's evidence is kept under.

    CI names its own runs; a local run names its own with the time it
    started and the process it ran in, so two local runs never write
    over each other's evidence.

    Returns:
        The run id.
    """
    for name in (RUN_ID_ENV, "GITHUB_RUN_ID"):
        value = os.environ.get(name)
        if value:
            return value
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"local-{stamp}-{os.getpid()}"


def _artifacts_dir() -> Path:
    """The directory the run's evidence directories live in.

    Returns:
        The directory.
    """
    override = os.environ.get(ARTIFACTS_DIR_ENV)
    if override:
        return Path(override)
    return PROJECT_ROOT / "artifacts" / "harness-runs"


def _inside_the_checkout(path: Path) -> bool:
    """Whether ``path`` is inside this checkout.

    Args:
        path: The path.

    Returns:
        Whether the path is under the repository root.
    """
    try:
        path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return False
    return True


def _keep_out_of_the_index(root: Path) -> Optional[Path]:
    """Drop an ignore marker into the evidence directory.

    The evidence is a run's output, and the index has to be unable to
    see it: a top-level directory the repository tracks but no privacy
    gate reads is a defect the gates cannot see coming, because the
    gate that checks the list of tracked directories only fires once
    the directory is *committed*.

    The marker is one line -- ``*`` -- and it sits inside the
    directory the run owns, so it covers the evidence and nothing
    else.  Outside the checkout there is nothing to keep out: git
    cannot reach it, and a marker written into a directory somebody
    else chose would be a side effect on their files.

    Args:
        root: The evidence directory.

    Returns:
        The marker, or ``None`` when the evidence is outside the
        checkout and no marker was needed.
    """
    if not _inside_the_checkout(root):
        return None
    root.mkdir(parents=True, exist_ok=True)
    marker = root / IGNORE_NAME
    marker.write_text(IGNORE_CONTENTS, encoding="utf-8")
    return marker


def _git_ignores(path: Path) -> bool:
    """Whether git is told to ignore ``path``.

    The question is asked the way git asks it: ``check-ignore`` reads
    the ignore rules with the same rules git uses when it decides
    whether a file can be staged, so the answer is the answer that
    decides the file's fate.

    Args:
        path: The path.

    Returns:
        Whether the path is ignored.
    """
    completed = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "check-ignore", "-q", "--", str(path)],
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def _relative_to_root(path: Path) -> str:
    """``path`` as a repository-relative string, when it is one.

    An evidence directory outside the repository has no relative name,
    and the summary says so with the absolute path rather than with a
    path that would resolve to the wrong place.

    Args:
        path: The path.

    Returns:
        The string the summary carries.
    """
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _real_home() -> str:
    """The home the process was launched with.

    The hermetic test environment replaces ``HOME`` with a scratch
    directory, which is right for a test that must not touch the
    operator's files -- and wrong for a run that has to authenticate
    against a real account.  The real home is published by the
    hermetic environment itself, so it is read from there rather than
    written down here.
    """
    return (
        os.environ.get("PDT_TEST_REAL_HOME")
        or os.environ.get("HOME")
        or str(Path.home())
    )


#: The shapes a credential takes in a text.  The *name* of a credential
#: identifies nobody and is worth keeping; its value is the thing the
#: archive must not carry.
_CREDENTIAL_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}"),
)

#: The environment variables whose *values* must never reach the
#: archive.
CREDENTIAL_ENV_VARS: Tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_TOKEN",
    "MINIMAX_API_KEY",
    "MINIMAX_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
)

#: What a redacted value is replaced with.
REDACTED = "<redacted>"


def _redact(text: str) -> str:
    """Take the credentials and the operator's name out of a text.

    Two things are removed.  The credentials, because the archive is
    evidence that gets published.  And the home directory, because it
    names the machine and the account the run happened on -- which is
    the operator's, not the repository's.

    Args:
        text: The text.

    Returns:
        The text, with the two removed.
    """
    home = _real_home()
    if home and home not in ("/", ""):
        text = text.replace(home, "<home>")
    for name in CREDENTIAL_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, f"<{name}>")
    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _redact_value(value: Any) -> Any:
    """Redact every string inside a JSON-shaped value.

    Args:
        value: The value.

    Returns:
        The value, with every string inside it redacted.
    """
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


# --------------------------------------------------------------------------
# Reading the evidence back
# --------------------------------------------------------------------------


def _json_lines(path: Path) -> List[Any]:
    """The values a JSON-lines file holds.

    A line that does not parse is a fault in its own right, so it is
    handed back as the text it is -- a reader can then see which line
    is broken, rather than that something somewhere is.

    Args:
        path: The file.

    Returns:
        One value per non-empty line.
    """
    values: List[Any] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            values.append(json.loads(line))
        except ValueError:
            values.append(line)
    return values


def _json_document(path: Path) -> Dict[str, Any]:
    """The JSON object a file holds.

    Args:
        path: The file.

    Returns:
        The object, or an empty mapping when the file is missing or
        does not hold a JSON object.
    """
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _raw_stream_fault(
    harness: str,
    path: Path,
    outcome: Optional[_Outcome],
) -> Optional[str]:
    """The harness's own event stream, read and checked.

    The file has to hold the events the harness produced: every line a
    JSON object with an event type, and among them the final
    ``result`` event -- the one that carries the run's own account of
    itself.  That event has to agree with what the run reported: the
    same session id, no error, and a usage count.

    Args:
        harness: The backend that produced the stream.
        path: The stream.
        outcome: What the run reported, when it is known.

    Returns:
        The fault, or ``None`` when the stream says what it must.
    """
    values = _json_lines(path)
    if not values:
        return f"the {harness} raw stream is empty"
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            return (
                f"line {index + 1} of the {harness} raw stream is not a "
                f"JSON object"
            )
        if not value.get("type"):
            return (
                f"line {index + 1} of the {harness} raw stream carries no "
                f"event type"
            )
    results = [value for value in values if value.get("type") == "result"]
    if not results:
        return f"the {harness} raw stream holds no result event"
    final = results[-1]
    if final.get("is_error"):
        return f"the {harness} raw stream's result event is an error"
    if not final.get("usage"):
        return f"the {harness} raw stream's result event carries no usage"
    if outcome is not None and outcome.session_id:
        if final.get("session_id") != outcome.session_id:
            return (
                f"the {harness} raw stream's result event carries the "
                f"session {final.get('session_id')!r} and the run reported "
                f"{outcome.session_id!r}"
            )
    return None


def _service_log_fault(harness: str, path: Path) -> Optional[str]:
    """The service's own record of the run, read and checked.

    The log is the service's, not the harness's: every line has to be
    an event the service published, and the run has to have produced
    the event that says the sub-agent was sandboxed.  That event is
    the service's own account of the confinement, and it is the one
    thing in the log the harness's own stream cannot say.

    Args:
        harness: The backend the log belongs to.
        path: The log.

    Returns:
        The fault, or ``None`` when the log says what it must.
    """
    values = _json_lines(path)
    if not values:
        return f"the {harness} service log is empty"
    events: List[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            return (
                f"line {index + 1} of the {harness} service log is not a "
                f"JSON object"
            )
        if not value.get("event"):
            return (
                f"line {index + 1} of the {harness} service log carries no "
                f"event name"
            )
        events.append(str(value["event"]))
    if "subagent_sandboxed" not in events:
        return (
            f"the {harness} service log never says the run was sandboxed: "
            f"{events!r}"
        )
    return None


def _mcp_record_fault(harness: str, record: Dict[str, Any]) -> Optional[str]:
    """The ``.mcp.json`` restore record, read and checked.

    Two things have to hold.  The configuration has to have reached the
    harness *this* backend's way -- the file in the workspace for the
    MiniMax adapter, the command line for the claude one -- and the
    workspace has to be clean afterwards, so that a run that injected
    a file did not leave it behind and a run that injected none did
    not leave one either.

    The restore flag has to agree with what the run saw: there is
    something to restore exactly when the file was there during the
    run.

    Args:
        harness: The backend the record belongs to.
        record: The record.

    Returns:
        The fault, or ``None`` when the record says what it must.
    """
    if not record:
        return f"the {harness} restore record is empty"
    if record.get("harness") != harness:
        return (
            f"the restore record belongs to {record.get('harness')!r}, not "
            f"to {harness!r}"
        )
    expected = MCP_DELIVERY.get(harness)
    if expected is None:
        return f"no delivery contract is known for the {harness} backend"
    for key, value in expected.items():
        if record.get(key) is not value:
            return (
                f"the {harness} restore record says {key}="
                f"{record.get(key)!r} and the {harness} backend delivers "
                f"the configuration with {key}={value!r}"
            )
    if not record.get("mcp_server_started"):
        return f"the {harness} restore record says the MCP server never started"
    if record.get("mcp_json_present_after_run"):
        return (
            f"the {harness} workspace still holds the injected .mcp.json "
            f"after the run"
        )
    restored = record.get("restored")
    if restored is not bool(expected["mcp_json_seen_during_run"]):
        return (
            f"the {harness} restore record says restored={restored!r}, and "
            f"the run saw the file: "
            f"{expected['mcp_json_seen_during_run']!r}"
        )
    return None


def _ps_record_fault(harness: str, record: Dict[str, Any]) -> Optional[str]:
    """The process-table sample, read and checked.

    The sample has to show the process that ran the harness -- a
    sample of nothing is not a sample -- and the run must have left no
    orphan behind, because an orphan is a process that outlived the
    thing it belonged to.

    Args:
        harness: The backend the sample belongs to.
        record: The record.

    Returns:
        The fault, or ``None`` when the record says what it must.
    """
    if not record:
        return f"the {harness} process sample is empty"
    runner_pid = record.get("runner_pid")
    if not isinstance(runner_pid, int) or runner_pid <= 0:
        return f"the {harness} process sample names no runner: {runner_pid!r}"
    sampled = record.get("sampled_pids")
    if not isinstance(sampled, list) or not sampled:
        return f"the {harness} process sample holds no pids"
    if runner_pid not in sampled:
        return (
            f"the {harness} runner {runner_pid} is not among the sampled "
            f"pids: {sampled!r}"
        )
    if record.get("runner_pid_sampled") is not True:
        return f"the {harness} process sample does not say the runner was sampled"
    if record.get("orphans_after_run"):
        return (
            f"the {harness} run left processes behind: "
            f"{record.get('orphans_after_run')!r}"
        )
    if not record.get("service_log_lines"):
        return f"the {harness} process sample says the service wrote no log"
    return None


def _control_record_fault(record: Dict[str, Any]) -> Optional[str]:
    """The negative control's record, read and checked.

    The control is the criterion that has to hold in every lane, so
    its record is checked like the rest: the run failed, the failure
    names the backend that is missing, the backend it selected is the
    one it was configured with, and nothing answered through another
    backend.

    Args:
        record: The record.

    Returns:
        The fault, or ``None`` when the record says what it must.
    """
    if not record:
        return "the negative control's record is empty"
    backend = record.get("backend")
    if not backend:
        return "the negative control's record names no backend"
    if record.get("exit_code") == 0:
        return "the task succeeded with the backend missing"
    failure = record.get("failure")
    if not failure:
        return "the task failed without saying so"
    if backend not in failure:
        return (
            f"the failure does not name the backend that is missing: "
            f"{failure!r}"
        )
    if record.get("selected") != backend:
        return (
            f"the control selected {record.get('selected')!r} rather than "
            f"the backend it was configured with, {backend!r}"
        )
    if record.get("fallbacks") != 0:
        return (
            f"the run fell back to another backend: "
            f"{record.get('fallbacks')!r} call(s) were recorded"
        )
    return None


def _redaction_fault(text: str) -> Optional[str]:
    """Whether a text still carries something private.

    The archive is published with the release, so the home directory
    it was produced on and the value of any credential must not be in
    it.  The *names* are fine -- they identify nobody -- and the rules
    are the ones the redaction itself applies, so the check and the
    redaction cannot disagree about what counts.

    Args:
        text: The text.

    Returns:
        The fault, or ``None`` when nothing private is left in it.
    """
    home = _real_home()
    if home and home not in ("/", "") and home in text:
        return "the text carries the home directory it was produced in"
    for name in CREDENTIAL_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 8 and value in text:
            return f"the text carries the value of {name}"
    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(text):
            return "the text carries something shaped like a credential"
    return None


def _evidence_faults(
    archive_dir: Path,
    outcomes: Dict[str, _Outcome],
) -> List[str]:
    """Every fault in the archive, read from the files on disk.

    The archive is read rather than believed: the verdict and the case
    both ask what the files say, so a file that was written empty, or
    written wrong, is a fault and not a fact the run believes in.

    Args:
        archive_dir: The run's evidence directory.
        outcomes: What each backend's run produced.

    Returns:
        One line per fault.
    """
    faults: List[str] = []
    for harness, outcome in outcomes.items():
        fault = _raw_stream_fault(
            harness, archive_dir / f"raw-stream-{harness}.jsonl", outcome
        )
        if fault:
            faults.append(fault)
        fault = _service_log_fault(
            harness, archive_dir / f"service-log-{harness}.jsonl"
        )
        if fault:
            faults.append(fault)
        fault = _mcp_record_fault(
            harness,
            _json_document(archive_dir / f"mcp-restore-{harness}.json"),
        )
        if fault:
            faults.append(fault)
        fault = _ps_record_fault(
            harness,
            _json_document(archive_dir / f"ps-sampling-{harness}.json"),
        )
        if fault:
            faults.append(fault)
    fault = _control_record_fault(
        _json_document(archive_dir / "negative-control.json")
    )
    if fault:
        faults.append(fault)
    if archive_dir.is_dir():
        for name in sorted(archive_dir.iterdir()):
            if not name.is_file():
                continue
            try:
                text = name.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fault = _redaction_fault(text)
            if fault:
                faults.append(f"{name.name}: {fault}")
    return faults


def _service_log(plans_dir: Path, plan_id: str) -> Tuple[str, int]:
    """The service's own record of the run, redacted, as JSON lines.

    This is the log the service wrote while it ran: the events the
    service itself published, in the order it published them.  It is
    the record an operator would read, and it is kept beside the
    harness's own stream so that the two can be compared.

    Each line is redacted as a JSON value when it parses and as text
    when it does not, so the file stays a JSON-lines file either way.

    Args:
        plans_dir: The server's plans directory.
        plan_id: The plan the run belongs to.

    Returns:
        The redacted log, and how many lines it held.
    """
    path = Path(plans_dir) / plan_id / "execution.log"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", 0
    lines = [line for line in raw.splitlines() if line.strip()]
    out: List[str] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            out.append(_redact(line))
            continue
        out.append(json.dumps(_redact_value(parsed), ensure_ascii=False))
    return "\n".join(out) + ("\n" if out else ""), len(lines)


def _scenario_processes(markers: Sequence[str]) -> List[str]:
    """The processes that belong to this run, right now.

    Two ways a process belongs: it carries one of the run's own paths
    on its command line, or it is a direct child of this process.  The
    second one matters because the dispatch runs *in* this process, so
    the CLI it spawned is this process's child -- and a Node program
    rewrites the command line it was started with, so the tree it was
    given is no longer on it.

    Args:
        markers: The paths that mark this run's processes.

    Returns:
        One line per process that belongs to this run.
    """
    completed = subprocess.run(
        ["ps", "-Ao", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
    )
    mine = os.getpid()
    rows: List[str] = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, command = int(parts[0]), int(parts[1]), parts[2]
        if pid in (mine, os.getppid()) or command.startswith("ps -Ao"):
            continue
        if any(marker in command for marker in markers) or ppid == mine:
            rows.append(f"{pid}: {command}")
    return rows


def _usage_is_partial(usage: Optional[Dict[str, Any]]) -> Optional[bool]:
    """Whether a usage count is a partial count.

    The rule is the one the contract states: a count that is missing a
    canonical field understates what the run cost, and a reader who
    believes it is complete believes a number that is too low.  The
    field the two CLIs disagree on is cache creation -- the claude CLI
    reports it, the MiniMax CLI does not -- so a run through the
    latter reports a partial count and says so.

    Args:
        usage: The usage block, or ``None``.

    Returns:
        Whether the count is partial, or ``None`` when there is no
        count at all.
    """
    if not usage:
        return None
    return usage.get("cache_creation_input_tokens") is None


# --------------------------------------------------------------------------
# Reading a run back
# --------------------------------------------------------------------------


def _read_outcome(
    harness: str,
    model: str,
    plan_id: str,
    result: ScenarioResult,
    workspace: Path,
) -> _Outcome:
    """Read one run's facts into plain data.

    Everything here is read while the run's world still exists: the
    workspace is still there, the plan's directory is still there, and
    the process table still shows what the run left behind.

    Args:
        harness: The backend that produced the run.
        model: The model that backend was pinned to.
        plan_id: The plan the run belongs to.
        result: What the run produced.
        workspace: The directory the run wrote in.

    Returns:
        The run's facts.
    """
    hello_sha256: Optional[str] = None
    hello_path = Path(result.hello_path) if result.hello_path else None
    if hello_path is not None and hello_path.is_file():
        hello_sha256 = hashlib.sha256(
            hello_path.read_bytes()
        ).hexdigest()

    marker_path = Path(result.marker_path) if result.marker_path else None
    escape_path = Path(result.escape_path) if result.escape_path else None

    return _Outcome(
        harness=harness,
        model=model,
        plan_id=plan_id,
        hello_sha256=hello_sha256,
        usage=result.usage,
        usage_reported_keys=tuple(sorted(result.usage_reported_keys)),
        usage_partial=_usage_is_partial(result.usage),
        is_error=result.is_error,
        exit_code=result.exit_code,
        session_id=result.session_id,
        pinned_session_id=result.pinned_session_id,
        sandbox_state=result.sandbox_state,
        changed_files=result.changed_files,
        orphans=result.orphans,
        sampled_pids=result.sampled_pids,
        runner_pid=result.runner_pid,
        mcp_json_seen=result.mcp_json_seen,
        mcp_json_present_after_run=(workspace / ".mcp.json").exists(),
        mcp_config_on_command_line="--mcp-config" in result.argv,
        git_status=result.git_status,
        marker_written=bool(marker_path and marker_path.is_file()),
        escape_refused=bool(escape_path and not escape_path.exists()),
        escape_errno=result.escape_errno,
        dispatch_error=result.dispatch_error,
    )


def _live_field_fault(harness: str, outcome: _Outcome) -> Optional[str]:
    """Why one run's live fields are not what they have to be.

    Args:
        harness: The backend that produced the run.
        outcome: The run's facts.

    Returns:
        The fault, or ``None`` when there is none.
    """
    if outcome.dispatch_error:
        return (
            f"the {harness} dispatch raised: "
            f"{type(outcome.dispatch_error).__name__}: "
            f"{outcome.dispatch_error}"
        )
    if outcome.exit_code != 0:
        return f"the {harness} run exited {outcome.exit_code}"
    if outcome.is_error is not False:
        return f"the {harness} run reported an error"
    if not outcome.usage:
        return f"the {harness} run reported no usage"
    missing = USAGE_KEYS - set(outcome.usage_reported_keys)
    if missing:
        return (
            f"the {harness} usage block is missing "
            f"{sorted(missing)}"
        )
    if outcome.usage.get("input_tokens") is None:
        return f"the {harness} run reported no input tokens"
    if not (outcome.usage.get("input_tokens") or 0) > 0:
        return (
            f"the {harness} run reported no input tokens: "
            f"{outcome.usage.get('input_tokens')!r}"
        )
    if not outcome.session_id:
        return f"the {harness} run reported no session id"
    if harness == MCODE:
        if not MCODE_SESSION_RE.match(outcome.session_id):
            return (
                f"the {harness} session id is not a MiniMax Code one: "
                f"{outcome.session_id!r}"
            )
        return None
    if outcome.session_id != outcome.pinned_session_id:
        return (
            f"the {harness} run did not happen under the pinned session id: "
            f"{outcome.session_id!r} != {outcome.pinned_session_id!r}"
        )
    try:
        version = uuid.UUID(outcome.session_id).version
    except ValueError:
        return f"the {harness} session id is not a uuid: {outcome.session_id!r}"
    if version != 5:
        return (
            f"the {harness} pinned session id is not a uuid5: "
            f"{outcome.session_id!r}"
        )
    return None


# --------------------------------------------------------------------------
# The criteria
# --------------------------------------------------------------------------


def _kpi_task_output(outcomes: Dict[str, _Outcome]) -> _Verdict:
    """KPI1 -- both backends left the same artefact behind.

    Args:
        outcomes: What each backend's run produced.

    Returns:
        The verdict.
    """
    if set(outcomes) != {MCODE, CLAUDE}:
        return _Verdict(
            KPI_TASK_OUTPUT,
            SKIPPED,
            "the scenarios did not run, so there is no artefact to compare",
        )
    hashes = {harness: outcomes[harness].hello_sha256 for harness in (MCODE, CLAUDE)}
    missing = [harness for harness, value in hashes.items() if not value]
    if missing:
        return _Verdict(
            KPI_TASK_OUTPUT,
            FAILED,
            f"the {', '.join(missing)} run left no hello.txt behind",
        )
    if hashes[MCODE] != hashes[CLAUDE]:
        return _Verdict(
            KPI_TASK_OUTPUT,
            FAILED,
            "the two runs wrote different bytes into hello.txt: "
            f"mcode={hashes[MCODE]} claude={hashes[CLAUDE]}",
        )
    if hashes[MCODE] not in HELLO_HASHES:
        return _Verdict(
            KPI_TASK_OUTPUT,
            FAILED,
            f"hello.txt does not carry the expected content: {hashes[MCODE]}",
        )
    return _Verdict(
        KPI_TASK_OUTPUT,
        PASSED,
        f"both runs left the same hello.txt ({hashes[MCODE]})",
    )


def _kpi_live_fields(outcomes: Dict[str, _Outcome]) -> _Verdict:
    """KPI2 -- both backends reported the live fields.

    Args:
        outcomes: What each backend's run produced.

    Returns:
        The verdict.
    """
    if set(outcomes) != {MCODE, CLAUDE}:
        return _Verdict(
            KPI_LIVE_FIELDS,
            SKIPPED,
            "the scenarios did not run, so there are no live fields to read",
        )
    faults = [
        fault
        for fault in (
            _live_field_fault(harness, outcomes[harness])
            for harness in (MCODE, CLAUDE)
        )
        if fault
    ]
    if faults:
        return _Verdict(KPI_LIVE_FIELDS, FAILED, "; ".join(faults))
    return _Verdict(
        KPI_LIVE_FIELDS,
        PASSED,
        "both runs reported the four canonical usage keys, a positive "
        "input count, no error, and a session id in their own shape",
    )


def _kpi_readable_error(control: Optional[_ControlFacts]) -> _Verdict:
    """KPI3 -- the failure named the backend that was missing.

    Args:
        control: What the negative control produced.

    Returns:
        The verdict.
    """
    if control is None:
        return _Verdict(
            KPI_READABLE_ERROR,
            SKIPPED,
            "the negative control did not run",
        )
    if control.exit_code == 0:
        return _Verdict(
            KPI_READABLE_ERROR,
            FAILED,
            "the run with the missing backend succeeded: a backend that "
            "cannot run has to fail the task",
        )
    if not control.failure:
        return _Verdict(
            KPI_READABLE_ERROR,
            FAILED,
            "the task failed without saying so: a failure that names "
            "nothing is indistinguishable from a crash",
        )
    if control.backend not in control.failure:
        return _Verdict(
            KPI_READABLE_ERROR,
            FAILED,
            "the failure does not name the backend that is missing: "
            f"{control.failure!r}",
        )
    if control.fallbacks:
        return _Verdict(
            KPI_READABLE_ERROR,
            FAILED,
            f"the run fell back to another backend "
            f"{control.fallbacks} time(s)",
        )
    return _Verdict(
        KPI_READABLE_ERROR,
        PASSED,
        f"the failure named the backend that is missing: {control.failure!r}",
    )


def _kpi_evidence_archive(
    archive_dir: Path,
    evidence: Sequence[str],
    scenarios_ran: bool,
    outcomes: Dict[str, _Outcome],
) -> _Verdict:
    """KPI4 -- the run's evidence is on disk, and it says what it must.

    The files are read, not counted.  A file that is there and empty is
    not evidence, and a file that is there and wrong is worse than no
    file at all, because a reader who sees the name stops looking.

    Args:
        archive_dir: The run's evidence directory.
        evidence: The evidence files, by name.
        scenarios_ran: Whether the real scenarios ran.
        outcomes: What each backend's run produced.

    Returns:
        The verdict.
    """
    if not scenarios_ran:
        return _Verdict(
            KPI_EVIDENCE_ARCHIVE,
            SKIPPED,
            "the scenarios did not run, so there is no run to archive",
        )
    missing = [name for name in evidence if not (archive_dir / name).is_file()]
    if missing:
        return _Verdict(
            KPI_EVIDENCE_ARCHIVE,
            FAILED,
            f"the archive is missing {missing}",
        )
    faults = _evidence_faults(archive_dir, outcomes)
    if faults:
        return _Verdict(
            KPI_EVIDENCE_ARCHIVE,
            FAILED,
            f"the evidence does not say what it must: {faults[0]}",
        )
    return _Verdict(
        KPI_EVIDENCE_ARCHIVE,
        PASSED,
        f"{len(evidence)} evidence files are in "
        f"{_relative_to_root(archive_dir)}, and each one says what it must",
        tuple(evidence),
    )


# --------------------------------------------------------------------------
# The negative control
# --------------------------------------------------------------------------


def _negative_control(root: Path) -> _ControlFacts:
    """Ask for a backend that is not installed, and read the refusal.

    The world is the operator's: ``PDT_HARNESS`` names a backend the
    registry knows and the machine does not have.  The service starts,
    and the *task* fails -- and the failure has to name the backend
    that is missing, because a failure that names nothing leaves the
    operator no way to tell a missing backend from a broken one.

    The recording ``claude`` is there for the same reason it is in the
    control itself: a run that answered through another backend would
    not fail at all, and would look exactly like a run that worked.

    Args:
        root: The control's own tree.

    Returns:
        What the control produced.
    """
    bin_dir = root / "bin"
    marker = root / "claude-calls.log"
    _recording_claude(bin_dir, marker)
    # The instrument has to be able to record before "nothing was
    # recorded" means anything.
    _prove_the_instrument(bin_dir, marker)

    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    script = root / "first_task.py"
    script.write_text(_FIRST_TASK, encoding="utf-8")

    env = _control_environment(
        root,
        PATH=os.pathsep.join((str(bin_dir), SYSTEM_PATH)),
        PDT_TASK_CWD=str(workspace),
    )
    completed = subprocess.run(
        [str(VENV_PYTHON), str(script)],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=CONTROL_TIMEOUT,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    selected = ""
    for line in output.splitlines():
        if line.startswith("HARNESS_SELECTED:"):
            selected = line.split(":", 1)[1].strip().split(" ")[0]
            break
    fallbacks = 0
    try:
        fallbacks = len(
            [
                line
                for line in marker.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        )
    except (OSError, UnicodeDecodeError):
        fallbacks = 0
    return _ControlFacts(
        backend=MISSING_BACKEND,
        selected=selected,
        exit_code=completed.returncode,
        failure=_failure_text(output),
        fallbacks=fallbacks,
        output=_redact(output),
    )


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def _run_backend(
    harness: str,
    model: str,
    server: ScenarioServer,
    plan_id: str,
) -> Tuple[_Outcome, Dict[str, str]]:
    """Run the fixed task through one backend, and read back what it made.

    The dispatch's own result carries the harness's event stream, and
    the raw payloads in it are the closest thing to the CLI's output
    that survives the run -- the CLI's stdout is consumed by the
    parser and never kept anywhere else.  So the acceptance watches the
    same spawn point the scenario watches, and keeps what it sees.

    Args:
        harness: The backend to drive.
        model: The model that backend is pinned to.
        server: The server the run happens against.
        plan_id: The plan the run belongs to.

    Returns:
        The run's facts, and the evidence files it produced.
    """
    import coding_tool

    workspace = Path(server.workspace) / "acceptance"
    captured: List[Any] = []
    real_runner = coding_tool._DispatchRunner

    class _CapturingRunner(real_runner):  # type: ignore[misc, valid-type]
        """The runner, with the raw event stream kept.

        The scenario's own machinery watches this same spawn point and
        the acceptance watches it too, so the two do not fight over it:
        the scenario's watcher sits on top of this one and keeps the
        exit code and the session id, this one keeps the events.
        """

        def run(self, harness: Any, invocation: Any, stdin_text: Any = None) -> Any:
            dispatched = super().run(harness, invocation, stdin_text)
            captured.append(dispatched)
            return dispatched

    coding_tool._DispatchRunner = _CapturingRunner
    try:
        result = run_scenario(
            harness=harness,
            model=model,
            server=server,
            workspace=workspace,
            plan_id=plan_id,
        )
    finally:
        coding_tool._DispatchRunner = real_runner
    outcome = _read_outcome(harness, model, plan_id, result, workspace)

    # -- the harness's own raw stream -------------------------------------
    events: List[Dict[str, Any]] = []
    for dispatched in captured:
        for event in getattr(dispatched, "events", ()) or ():
            events.append(
                {
                    "type": getattr(event, "type", ""),
                    "session_id": getattr(event, "session_id", None),
                    "is_error": bool(getattr(event, "is_error", False)),
                    "usage": _redact_value(getattr(event, "usage", None)),
                    "raw": _redact_value(getattr(event, "raw", {})),
                }
            )
    outcome.raw_stream_lines = len(events)
    raw_stream = "".join(
        json.dumps(event, ensure_ascii=False) + "\n" for event in events
    )

    # -- the service's own record of the run ------------------------------
    service_log, log_lines = _service_log(Path(server.plans_dir), plan_id)

    mcp_record = {
        "harness": harness,
        "workspace": str(workspace),
        "mcp_config_path": str(result.mcp_config_path),
        "mcp_json_seen_during_run": result.mcp_json_seen,
        "mcp_json_present_after_run": outcome.mcp_json_present_after_run,
        "mcp_config_on_command_line": outcome.mcp_config_on_command_line,
        "mcp_server_started": outcome.marker_written,
        "git_status": result.git_status,
        "restored": (
            result.mcp_json_seen and not outcome.mcp_json_present_after_run
        ),
    }
    ps_record = {
        "harness": harness,
        "runner_pid": result.runner_pid,
        "sampled_pids": list(result.sampled_pids),
        "runner_pid_sampled": result.runner_pid in result.sampled_pids,
        "orphans_after_run": list(result.orphans),
        "ps_after_run": _scenario_processes([str(server.root)]),
        "service_log_lines": log_lines,
    }
    evidence = {
        f"raw-stream-{harness}.jsonl": raw_stream,
        f"service-log-{harness}.jsonl": service_log,
        f"mcp-restore-{harness}.json": json.dumps(
            mcp_record, indent=2, ensure_ascii=False
        )
        + "\n",
        f"ps-sampling-{harness}.json": json.dumps(
            ps_record, indent=2, ensure_ascii=False
        )
        + "\n",
    }
    return outcome, evidence


def _harness_block(outcome: Optional[_Outcome]) -> Dict[str, Any]:
    """The summary's block for one backend.

    Args:
        outcome: What the backend's run produced, or ``None``.

    Returns:
        The block.
    """
    if outcome is None:
        return {
            "ran": False,
            "hello_sha256": None,
            "usage_partial": None,
            "sandbox": None,
        }
    return {
        "ran": True,
        "hello_sha256": outcome.hello_sha256,
        "usage_partial": outcome.usage_partial,
        "sandbox": outcome.sandbox_state,
        "session_id": outcome.session_id,
        "usage": outcome.usage,
        "usage_reported_keys": list(outcome.usage_reported_keys),
        "is_error": outcome.is_error,
        "exit_code": outcome.exit_code,
        "model": outcome.model,
        "plan_id": outcome.plan_id,
        "changed_files": [
            {"path": path, "sha256": sha} for path, sha in outcome.changed_files
        ],
    }


# --------------------------------------------------------------------------
# The fixture
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def acceptance_run(tmp_path_factory) -> _AcceptanceRun:
    """Run the acceptance once, and keep what it produced.

    The fixture is the run: two servers -- one per backend, each with
    its own ``PDT_HARNESS`` -- the fixed task on each, the negative
    control, and the archive.  Everything it reads is read while the
    world it read it from still exists, and everything it writes is
    written before the servers go away.

    The scenarios are opt-in: without ``PDT_E2E_HARNESS=1`` they are
    skipped, because they cost a model call each and spawn the real
    CLIs under the real macOS seatbelt.  The negative control and the
    archive are not gated -- they cost no model call, and they are the
    two halves of the acceptance that have to hold in every lane.

    Yields:
        Everything the acceptance produced.
    """
    run_id = _run_id()
    archive_root = _artifacts_dir()
    # The evidence is a run's output, and the index has to be unable
    # to see it: a top-level directory the repository tracks but no
    # privacy gate reads is a defect no gate can see coming.
    ignore_marker = _keep_out_of_the_index(archive_root)
    archive_dir = archive_root / run_id
    enabled = os.environ.get(E2E_ENV_VAR) == "1"

    outcomes: Dict[str, _Outcome] = {}
    servers: Dict[str, _ServerFacts] = {}
    evidence: Dict[str, str] = {}
    scenarios_ran = False

    if enabled:
        scenarios_ran = True
        # The two servers are booted one after the other, and each is
        # reclaimed before the next one starts.  That is not tidiness:
        # a process is "this run's" when it is a child of this process,
        # and both servers are children of this process -- so with both
        # of them up at once, the second one is an orphan as far as the
        # first one's watcher is concerned, and a genuine orphan could
        # not be told from a bookkeeping mistake.
        for harness, model, plan_id in (
            (MCODE, MCODE_MODEL, "e2e-acceptance-mcode"),
            (CLAUDE, CLAUDE_MODEL, "e2e-acceptance-claude"),
        ):
            with _server(harness) as server:
                health = server.get_json("/api/health")
                outcome, files = _run_backend(harness, model, server, plan_id)
                outcomes[harness] = outcome
                evidence.update(files)
                base_url, port, root = server.base_url, server.port, server.root
            # The server is gone now.  What is left of it is recorded
            # before anything else is written.
            servers[harness] = _ServerFacts(
                harness=harness,
                base_url=base_url,
                port=port,
                root=str(root),
                health=health,
                process_exit_code=server.process.poll(),
                root_exists=Path(root).exists(),
                leftover_processes=tuple(
                    _scenario_processes([str(root)])
                ),
            )

    # The negative control runs either way: it costs no model call, and
    # it is the one criterion that has to hold in every lane.
    control = _negative_control(tmp_path_factory.mktemp("acceptance-control"))
    evidence["negative-control.json"] = json.dumps(
        {
            "backend": control.backend,
            "selected": control.selected,
            "exit_code": control.exit_code,
            "failure": control.failure,
            "fallbacks": control.fallbacks,
            "output": control.output,
        },
        indent=2,
        ensure_ascii=False,
    ) + "\n"

    # The evidence is written first, so the archive criterion can be
    # answered by looking at what is on disk rather than at what the
    # run believes it wrote.
    archive_dir.mkdir(parents=True, exist_ok=True)
    for name, content in evidence.items():
        (archive_dir / name).write_text(content, encoding="utf-8")

    verdicts = {
        KPI_TASK_OUTPUT: _kpi_task_output(outcomes),
        KPI_LIVE_FIELDS: _kpi_live_fields(outcomes),
        KPI_READABLE_ERROR: _kpi_readable_error(control),
        KPI_EVIDENCE_ARCHIVE: _kpi_evidence_archive(
            archive_dir, sorted(evidence), scenarios_ran, outcomes
        ),
    }

    files = sorted(evidence) + [SUMMARY_NAME]
    summary = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": _relative_to_root(archive_dir / SUMMARY_NAME),
        "ignore_marker": (
            _relative_to_root(ignore_marker) if ignore_marker else None
        ),
        "scenarios_ran": scenarios_ran,
        MCODE: _harness_block(outcomes.get(MCODE)),
        CLAUDE: _harness_block(outcomes.get(CLAUDE)),
        "scenarios": {
            name: {
                "status": verdict.status,
                "reason": verdict.reason,
                "artifacts": list(verdict.artifacts),
            }
            for name, verdict in verdicts.items()
        },
        "files": [_relative_to_root(archive_dir / name) for name in files],
    }
    (archive_dir / SUMMARY_NAME).write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return _AcceptanceRun(
        run_id=run_id,
        archive_dir=archive_dir,
        summary=summary,
        verdicts=verdicts,
        outcomes=outcomes,
        servers=servers,
        control=control,
        scenarios_ran=scenarios_ran,
        evidence=tuple(sorted(evidence)),
    )


# --------------------------------------------------------------------------
# The acceptance
# --------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.real_model
@pytest.mark.skipif(
    os.environ.get(E2E_ENV_VAR) != "1",
    reason=(
        "the acceptance scenarios need PDT_E2E_HARNESS=1: they spawn the "
        "real CLIs under the real macOS seatbelt and cost a model call "
        "each, so the deterministic gates only collect them"
    ),
)
def test_end_to_end_both_harnesses(acceptance_run: _AcceptanceRun) -> None:
    """KPI1 and KPI2, on both backends, in one run.

    The assertions are the criteria themselves, read out of the run's
    own facts -- the verdicts the summary carries are the record of
    them, not the source of them.

    Args:
        acceptance_run: What the acceptance produced.
    """
    outcomes = acceptance_run.outcomes
    assert set(outcomes) == {MCODE, CLAUDE}, (
        f"the acceptance did not run on both backends: {sorted(outcomes)}"
    )

    # -- KPI1: the task's output -----------------------------------------
    mcode_hash = outcomes[MCODE].hello_sha256
    claude_hash = outcomes[CLAUDE].hello_sha256
    assert mcode_hash, (
        f"the {MCODE} run left no hello.txt behind in "
        f"{outcomes[MCODE].changed_files!r}"
    )
    assert claude_hash, (
        f"the {CLAUDE} run left no hello.txt behind in "
        f"{outcomes[CLAUDE].changed_files!r}"
    )
    assert mcode_hash == claude_hash, (
        "the two runs wrote different bytes into hello.txt:\n"
        f"  {MCODE}:  {mcode_hash}\n"
        f"  {CLAUDE}: {claude_hash}"
    )
    assert mcode_hash in HELLO_HASHES, (
        f"hello.txt does not carry the expected content "
        f"({TARGET_STDOUT!r}): {mcode_hash}"
    )

    # -- KPI2: the live fields -------------------------------------------
    for harness in (MCODE, CLAUDE):
        outcome = outcomes[harness]
        fault = _live_field_fault(harness, outcome)
        assert fault is None, f"the {harness} run: {fault}"

    assert outcomes[MCODE].session_id, "the mcode run reported no session"
    assert MCODE_SESSION_RE.match(outcomes[MCODE].session_id or ""), (
        f"the mcode session id is not a MiniMax Code one: "
        f"{outcomes[MCODE].session_id!r}"
    )
    assert outcomes[CLAUDE].session_id == outcomes[CLAUDE].pinned_session_id, (
        f"the claude run did not happen under the pinned session id: "
        f"{outcomes[CLAUDE].session_id!r} != "
        f"{outcomes[CLAUDE].pinned_session_id!r}"
    )
    assert uuid.UUID(outcomes[CLAUDE].session_id or "").version == 5, (
        f"the claude pinned session id is not a uuid5: "
        f"{outcomes[CLAUDE].session_id!r}"
    )

    # -- and the sandbox held on both sides --------------------------------
    # The criterion list does not name the sandbox, but the two backends
    # are only equivalent if the confinement is the same on both sides:
    # a run that escaped the seatbelt on one side would produce the
    # same hello.txt and still not be the same thing.
    for harness in (MCODE, CLAUDE):
        assert outcomes[harness].sandbox_state == "applied", (
            f"the {harness} run did not run under the seatbelt: "
            f"{outcomes[harness].sandbox_state!r}"
        )

    # -- and the verdicts the summary carries agree ----------------------
    assert acceptance_run.verdicts[KPI_TASK_OUTPUT].status == PASSED, (
        acceptance_run.verdicts[KPI_TASK_OUTPUT].reason
    )
    assert acceptance_run.verdicts[KPI_LIVE_FIELDS].status == PASSED, (
        acceptance_run.verdicts[KPI_LIVE_FIELDS].reason
    )

    # -- and the summary says what the run did ---------------------------
    # The summary is the artifact a reader gets, so it has to agree with
    # the run it describes -- a summary that reports a different hash
    # than the one that was written is worse than no summary.
    summary = acceptance_run.summary
    for harness in (MCODE, CLAUDE):
        block = summary[harness]
        assert block["ran"] is True, f"the summary says the {harness} run did not happen"
        assert block["hello_sha256"] == outcomes[harness].hello_sha256, (
            f"the summary's {harness} hash is not the one that was written: "
            f"{block['hello_sha256']!r} != {outcomes[harness].hello_sha256!r}"
        )
        assert block["sandbox"] == outcomes[harness].sandbox_state, (
            f"the summary's {harness} sandbox state is not the one the run "
            f"reported: {block['sandbox']!r} != "
            f"{outcomes[harness].sandbox_state!r}"
        )
        # The count is partial exactly when a canonical field is
        # missing, and the summary has to say the same thing.  Which
        # side is partial is the two CLIs' business -- they report
        # different amounts -- so what is checked here is the rule, not
        # the value.
        assert block["usage_partial"] == _usage_is_partial(
            outcomes[harness].usage
        ), (
            f"the summary's {harness} usage flag does not follow the rule: "
            f"{block['usage_partial']!r}"
        )


def test_evidence_artifacts_written(
    acceptance_run: _AcceptanceRun,
    tmp_path: Path,
) -> None:
    """The evidence is on disk, it says what it must, and it is safe.

    The case is not gated: the archive is written whether the criteria
    ran or not, and a summary that only exists when everything went
    well is a summary that is missing exactly when it is needed.

    Args:
        acceptance_run: What the acceptance produced.
        tmp_path: A scratch directory for the negative controls below.
    """
    run = acceptance_run

    assert run.archive_dir.is_dir(), (
        f"the run wrote no evidence directory: {run.archive_dir}"
    )
    summary_path = run.archive_dir / SUMMARY_NAME
    assert summary_path.is_file(), f"the run wrote no summary: {summary_path}"

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["run_id"] == run.run_id, (
        f"the summary names another run: {summary['run_id']!r}"
    )
    assert summary["artifacts"].endswith(
        f"harness-runs/{run.run_id}/{SUMMARY_NAME}"
    ), f"the summary does not point at itself: {summary['artifacts']!r}"

    # -- the four criteria, each with a reason ----------------------------
    assert set(summary["scenarios"]) == set(KPI_NAMES), (
        f"the summary does not carry the four criteria: "
        f"{sorted(summary['scenarios'])}"
    )
    for name in KPI_NAMES:
        verdict = summary["scenarios"][name]
        assert verdict["status"] in (PASSED, FAILED, SKIPPED), (
            f"the {name} verdict carries an unknown status: "
            f"{verdict['status']!r}"
        )
        assert verdict["reason"], (
            f"the {name} verdict carries no reason: a verdict nobody can "
            f"act on is not a verdict"
        )

    # -- every file the summary names is really there ---------------------
    for name in summary["files"]:
        path = Path(name)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        assert path.is_file(), (
            f"the summary names {name!r} and it is not there"
        )

    # -- and it names every file that is there ---------------------------
    named = {Path(name).name for name in summary["files"]}
    for name in run.evidence:
        assert name in named, (
            f"the archive holds {name!r} and the summary does not name it"
        )

    # -- and the evidence says what it must -------------------------------
    # The files are read, not counted.  An empty file and a file that
    # says the wrong thing are both faults, and a check that asks only
    # whether the file is there calls both of them a pass.
    faults = _evidence_faults(run.archive_dir, run.outcomes)
    assert not faults, (
        "the evidence does not say what it must:\n  " + "\n  ".join(faults)
    )

    # -- and the verdict agrees with what the files say -------------------
    verdict = run.verdicts[KPI_EVIDENCE_ARCHIVE]
    if run.scenarios_ran:
        assert verdict.status == PASSED, verdict.reason
    else:
        assert verdict.status == SKIPPED, verdict.reason
        assert run.evidence == ("negative-control.json",), (
            f"the scenarios did not run, so the archive holds only the "
            f"negative control: {list(run.evidence)}"
        )

    # -- and the index cannot see the evidence ----------------------------
    # The evidence is a run's output.  If the index could see it, a
    # `git add -A` would turn it into a tracked top-level directory --
    # one that no privacy gate reads, which is a defect that only shows
    # up after the commit that carries it.
    if _inside_the_checkout(run.archive_dir):
        root = run.archive_dir.parent
        marker = root / IGNORE_NAME
        assert marker.is_file(), (
            f"the evidence directory has no ignore marker: {marker}"
        )
        assert _git_ignores(run.archive_dir), (
            f"git is not told to ignore the evidence directory: "
            f"{run.archive_dir}"
        )
        assert summary["ignore_marker"] == _relative_to_root(marker), (
            f"the summary names another ignore marker: "
            f"{summary['ignore_marker']!r}"
        )
        # The marker covers the evidence and nothing else: a file
        # written beside the evidence directory is still seen.
        assert not _git_ignores(root.parent / "beside-the-evidence.txt"), (
            f"the ignore marker reaches past the evidence directory: "
            f"{marker}"
        )

    # -- and the checks have teeth ----------------------------------------
    # A rule that cannot fail is a rule nothing enforces.  Each one is
    # asked about an archive that breaks it, and has to answer.
    good_mcp = {
        "harness": CLAUDE,
        "mcp_json_seen_during_run": False,
        "mcp_json_present_after_run": False,
        "mcp_config_on_command_line": True,
        "mcp_server_started": True,
        "restored": False,
    }
    assert _mcp_record_fault(CLAUDE, good_mcp) is None, (
        "the restore check rejected a record that says the right thing"
    )
    for field, value in (
        ("mcp_json_seen_during_run", True),
        ("mcp_config_on_command_line", False),
        ("mcp_json_present_after_run", True),
        ("mcp_server_started", False),
        ("restored", True),
    ):
        forged = dict(good_mcp)
        forged[field] = value
        assert _mcp_record_fault(CLAUDE, forged), (
            f"the restore check accepted a claude record whose {field} is "
            f"{value!r}"
        )

    good_ps = {
        "harness": CLAUDE,
        "runner_pid": 4242,
        "sampled_pids": [4242, 4243],
        "runner_pid_sampled": True,
        "orphans_after_run": [],
        "service_log_lines": 2,
    }
    assert _ps_record_fault(CLAUDE, good_ps) is None, (
        "the process check rejected a sample that says the right thing"
    )
    for field, value in (
        ("runner_pid", 0),
        ("sampled_pids", []),
        ("runner_pid_sampled", False),
        ("orphans_after_run", [4244]),
        ("service_log_lines", 0),
    ):
        forged = dict(good_ps)
        forged[field] = value
        assert _ps_record_fault(CLAUDE, forged), (
            f"the process check accepted a sample whose {field} is {value!r}"
        )

    good_control = {
        "backend": MISSING_BACKEND,
        "selected": MISSING_BACKEND,
        "exit_code": 3,
        "failure": "FileNotFoundError: 'mcode'",
        "fallbacks": 0,
    }
    assert _control_record_fault(good_control) is None, (
        "the control check rejected a record that says the right thing"
    )
    for field, value in (
        ("exit_code", 0),
        ("failure", ""),
        ("selected", "claude"),
        ("fallbacks", 1),
    ):
        forged = dict(good_control)
        forged[field] = value
        assert _control_record_fault(forged), (
            f"the control check accepted a record whose {field} is {value!r}"
        )

    empty_stream = tmp_path / "raw-stream.jsonl"
    empty_stream.write_text("", encoding="utf-8")
    assert _raw_stream_fault(CLAUDE, empty_stream, None), (
        "the raw-stream check accepted an empty stream"
    )
    broken_stream = tmp_path / "broken-stream.jsonl"
    broken_stream.write_text('{"no": "type"}\n', encoding="utf-8")
    assert _raw_stream_fault(CLAUDE, broken_stream, None), (
        "the raw-stream check accepted a line with no event type"
    )
    assert _service_log_fault(CLAUDE, tmp_path / "nothing.jsonl"), (
        "the service-log check accepted an empty log"
    )

    assert _redaction_fault(f"the home is {_real_home()}"), (
        "the redaction check accepted a text that carries the home directory"
    )
    assert _redaction_fault("the key is sk-abcdefghijklmnop"), (
        "the redaction check accepted a text that carries a credential"
    )
    assert _redaction_fault("nothing private in this line") is None, (
        "the redaction check rejected a text that carries nothing private"
    )


def test_error_message_is_readable(acceptance_run: _AcceptanceRun) -> None:
    """A backend that is not installed fails, and the failure names it.

    Args:
        acceptance_run: What the acceptance produced.
    """
    control = acceptance_run.control
    assert control is not None, "the negative control did not run"

    assert control.exit_code != 0, (
        "the task succeeded with the backend missing. The backend that "
        "was asked for cannot run, so the run has to fail -- answering "
        "through some other backend is the failure this control exists "
        "to catch."
    )
    assert control.failure, (
        "the task failed without saying so. A failure that names nothing "
        "is indistinguishable from a crash."
    )
    assert control.backend in control.failure, (
        f"the failure does not name the backend that is missing: "
        f"{control.failure!r}"
    )
    assert control.fallbacks == 0, (
        f"the run fell back to another backend: {control.fallbacks} "
        f"call(s) were recorded."
    )


@pytest.mark.e2e
@pytest.mark.skipif(
    os.environ.get(E2E_ENV_VAR) != "1",
    reason=(
        "the servers this case checks are the ones the acceptance "
        "scenarios boot, so it needs PDT_E2E_HARNESS=1 as well"
    ),
)
def test_servers_reclaimed(acceptance_run: _AcceptanceRun) -> None:
    """Both servers are gone, and nothing of theirs is still running.

    Args:
        acceptance_run: What the acceptance produced.
    """
    run = acceptance_run
    assert set(run.servers) == {MCODE, CLAUDE}, (
        f"the acceptance did not boot both servers: {sorted(run.servers)}"
    )

    for harness in (MCODE, CLAUDE):
        facts = run.servers[harness]
        assert facts.process_exit_code is not None, (
            f"the {harness} server is still running: {facts.base_url}"
        )
        assert not facts.root_exists, (
            f"the {harness} server's temporary tree was left behind: "
            f"{facts.root}"
        )
        assert facts.leftover_processes == (), (
            f"the {harness} server left processes behind: "
            f"{list(facts.leftover_processes)}"
        )

    for harness, outcome in run.outcomes.items():
        assert outcome.orphans == (), (
            f"the {harness} run left processes behind: {list(outcome.orphans)}"
        )
