"""The verification log writers are bounded: they rotate instead of growing.

Background
----------
``backend/bounded_log.py`` gave the plan directory's log writers a bound:
the live log is rotated aside before it would grow past
``MAX_PLAN_LOG_BYTES`` and the rotated copies are numbered
``<name>.1`` … ``<name>.N``.  The subprocess capture log was moved onto
that writer; the three verification writers were not:

* ``verification_persistence.write_audit_event`` — the durable audit
  trail (``logs/audit_events.jsonl``), one JSON line per event;
* ``verification_persistence.VerificationPersistenceManager._write_log_entry``
  — the per-round log (``logs/verification_{round}_{ts}.log``);
* ``verification_subagent.VerificationSubAgent._write_log`` — the
  per-attempt sub-agent log (``logs/vp_attempts/vp_attempt_*.log``);
* ``verification_agent.Orchestrator._emit_group_log`` — the *second*
  writer of the per-round log: the group-level trace lands in the same
  file the persistence manager writes into.

All four wrote with a bare ``open(path, "a")``, so a file that only
grows is a file with no ceiling: an audit trail that records every
event, a round log that records every sub-agent line, and an attempt
log that records every heartbeat.  A round that runs long enough
writes enough to make the log the slowest thing in the plan directory
— and the reader of a five-gigabyte file has to read five gigabytes
to find the one line it wanted.

The round log is the awkward one: it has **two** writers, so the file
is only as bounded as the least bounded of them.  Half of it going
through ``append_bounded_line`` and half through a bare append means
the bound depends on which writer ran last.

What the tests below pin
------------------------
* each of the four writers rotates: the copy ``<name>.1`` appears next
  to the live file once the bound is passed, and the live file stays
  within the bound afterwards;
* the live file keeps receiving new lines after a rotation — the
  bound is a rotation, not a stop;
* a write that cannot land on disk does not raise: a read-only
  directory must not end the verification round, and the audit writer
  still reports the failure through its return value;
* the group-log writer keeps its own boundary contract: a log it
  cannot write still raises, because that is what its caller expects;
* no bare append-mode ``open()`` is left in ``verification_agent.py``
  — the bound is a property of the writer, and a second writer
  without one undoes it.

The bound is read from the module constant **when the write happens**,
so the tests below change it with ``monkeypatch`` and do not have to
re-import anything.
"""

from __future__ import annotations

import ast
import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import bounded_log
import verification_agent
from verification_agent import Orchestrator
from verification_persistence import (
    AUDIT_LOG_FILENAME,
    VerificationPersistenceManager,
    write_audit_event,
)
from verification_subagent import VerificationSubAgent


#: The bound the tests write against.  Small enough that a handful of
#: lines fills it, large enough that a single line still fits.
TEST_MAX_BYTES = 300

#: How many lines each writer is given.  Ten lines of ~120 bytes is
#: three times the bound, so the writer has to rotate at least once.
LINES = 10

#: The bound the group-log tests write against.  Bigger than
#: :data:`TEST_MAX_BYTES` on purpose: a group entry is ~190 bytes, so
#: the small bound would rotate between the two writers and the test
#: could not tell "both wrote to the same file" from "the second
#: write rotated the first one away".
GROUP_MAX_BYTES = 2048

#: How many group entries the group-log tests write.  Enough to pass
#: the group bound several times over, so the rotation has to happen.
GROUP_LINES = 25


@pytest.fixture
def small_bound(monkeypatch):
    """Shrink the plan-log bound to something a test can fill.

    ``RotatingLogSink.max_bytes`` reads the module constant when the
    write happens rather than when the sink is built, so patching the
    constant is enough to change the bound for every writer.
    """
    monkeypatch.setattr(bounded_log, "MAX_PLAN_LOG_BYTES", TEST_MAX_BYTES)
    return TEST_MAX_BYTES


def _payload(index: int) -> dict:
    """A log payload big enough that ten of them exceed the bound."""
    return {"index": index, "filler": "x" * 40}


def _rotated_copy(path: Path) -> Path:
    """The newest rotated copy of ``path`` (``<name>.1``)."""
    return path.parent / f"{path.name}.1"


@pytest.fixture
def group_bound(monkeypatch):
    """Shrink the plan-log bound for the group-log writers.

    Same mechanism as :func:`small_bound`, a different size: see
    :data:`GROUP_MAX_BYTES`.
    """
    monkeypatch.setattr(bounded_log, "MAX_PLAN_LOG_BYTES", GROUP_MAX_BYTES)
    return GROUP_MAX_BYTES


def _group_log_target(manager: VerificationPersistenceManager):
    """A stand-in for the orchestrator ``_emit_group_log`` needs.

    The method reaches for exactly one thing on ``self`` — the
    persistence manager's current log file — so a stub carrying the
    manager is enough, and the test does not have to stand up the
    whole orchestrator (with its coding tool, its provider slots and
    its plan directory) to write one line of log.
    """
    return SimpleNamespace(persistence=manager)


def _emit_group_entry(target, *, event_name: str, index: int) -> None:
    """Write one group-level entry through the orchestrator's writer."""
    Orchestrator._emit_group_log(
        target,
        event_name=event_name,
        group_name="grp",
        vps=[{"id": "VP-001"}],
        ts=datetime(2026, 11, 9),
        data=_payload(index),
    )


def test_audit_log_rotates(tmp_path, small_bound):
    """The durable audit trail rotates instead of growing without end."""
    plan_dir = tmp_path / "plan"

    for index in range(LINES):
        assert write_audit_event(
            plan_dir, "VP-001", "probe", _payload(index)
        ) is True

    audit_log = plan_dir / "logs" / AUDIT_LOG_FILENAME
    assert audit_log.exists(), "the audit trail must still be written"

    rotated = _rotated_copy(audit_log)
    assert rotated.exists(), (
        f"{rotated} must exist once the audit trail passed the bound"
    )

    # The live file is what the next event is appended to, and it is
    # within the bound — the rotation moved the overflow aside.
    assert audit_log.stat().st_size <= small_bound

    # The rotated copy holds the lines that were pushed out, and they
    # are still readable JSON lines.
    for line in rotated.read_text(encoding="utf-8").splitlines():
        if line.strip():
            assert "verification_point_id" in json.loads(line)


def test_verification_round_log_rotates(tmp_path, small_bound):
    """The per-round verification log rotates and keeps taking lines."""
    manager = VerificationPersistenceManager(tmp_path / "plan")
    log_path = manager.start_round(1)

    for index in range(LINES):
        manager.write_verification_point_log(
            "VP-001", "pytest_output", _payload(index)
        )

    assert log_path.exists()
    rotated = _rotated_copy(log_path)
    assert rotated.exists(), (
        f"{rotated} must exist once the round log passed the bound"
    )
    assert log_path.stat().st_size <= small_bound

    # The live log is still the live log: the round's next entry lands
    # in it, not in a copy.
    manager.write_verification_point_log(
        "VP-001", "pytest_output", _payload(LINES)
    )
    assert manager._log_file == log_path
    assert log_path.exists()


def test_vp_attempt_log_rotates(tmp_path, small_bound):
    """The per-attempt sub-agent log rotates too."""
    log_dir = tmp_path / "plan" / "logs"
    log_path = VerificationSubAgent._attempt_log_path(log_dir, "VP-001")

    for index in range(LINES):
        VerificationSubAgent._write_log(
            log_path, "attempt_started", _payload(index)
        )

    assert log_path.exists()
    rotated = _rotated_copy(log_path)
    assert rotated.exists(), (
        f"{rotated} must exist once the attempt log passed the bound"
    )
    assert log_path.stat().st_size <= small_bound

    # The attempt log lives in its own directory, and the rotated copy
    # is its neighbour — not a stray file in the round-log directory.
    assert rotated.parent == log_path.parent
    assert rotated.parent.name == "vp_attempts"


def test_single_oversized_line_is_truncated(tmp_path, small_bound):
    """One line bigger than the bound is cut to the bound, not allowed past it.

    The bound is the whole point of the writer: a single write that
    overshot it would put the file over the ceiling on its own, and the
    ceiling is what keeps the disk from filling up.
    """
    plan_dir = tmp_path / "plan"

    assert write_audit_event(
        plan_dir, "VP-001", "probe", {"filler": "x" * 5000}
    ) is True

    audit_log = plan_dir / "logs" / AUDIT_LOG_FILENAME
    assert audit_log.stat().st_size <= small_bound
    assert audit_log.stat().st_size > 0


def test_missing_parent_directory_is_created(tmp_path, small_bound):
    """A log whose parent directory is not there yet gets one.

    The writers have always created the directory they write into —
    ``logs/``, ``logs/vp_attempts/`` — and the bounded writer has to
    keep doing it: the caller names the path, not the directory tree
    above it.
    """
    plan_dir = tmp_path / "plan"

    # 1) The audit trail: ``plan_dir/logs`` does not exist yet.
    assert write_audit_event(
        plan_dir, "VP-001", "probe", _payload(0)
    ) is True
    assert (plan_dir / "logs").is_dir()

    # 2) The round log: the manager's log file points into a directory
    #    that has never been created.
    manager = VerificationPersistenceManager(plan_dir)
    manager._log_file = plan_dir / "logs" / "nested" / "deeper" / "round.log"
    manager._write_log_entry({"event": "probe", "data": _payload(0)})
    assert (plan_dir / "logs" / "nested" / "deeper").is_dir()
    assert manager._log_file.exists()

    # 3) The attempt log: same contract.
    attempt_path = plan_dir / "logs" / "vp_attempts" / "sub" / "attempt.log"
    VerificationSubAgent._write_log(attempt_path, "attempt_started", _payload(0))
    assert attempt_path.parent.is_dir()
    assert attempt_path.exists()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the write bit, so the directory stays writable",
)
def test_write_failure_is_swallowed(tmp_path):
    """A log that cannot be written does not end the verification.

    The three writers run inside a verification round that must finish
    whatever happens to its logs: a full disk or a read-only directory
    costs the log, not the run.  The audit writer says so through its
    return value; the other two have nothing to report and must not
    raise.
    """
    plan_dir = tmp_path / "plan"
    manager = VerificationPersistenceManager(plan_dir)
    manager.start_round(1)
    logs_dir = plan_dir / "logs"

    # The attempt log's own directory is created while the directory is
    # still writable, so the failure below is the *write* that fails
    # and not the mkdir that precedes it.
    attempts_dir = logs_dir / "vp_attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)

    os.chmod(logs_dir, 0o500)
    try:
        # The round log file already exists, so it is made unwritable
        # directly: appending to an existing file needs write access to
        # the file, not to the directory.
        for existing in logs_dir.glob("verification_*.log"):
            os.chmod(existing, 0o444)

        # 1) The round log entry: a lost line, not a lost round.
        manager.write_verification_point_log(
            "VP-001", "pytest_output", _payload(0)
        )

        # 2) The audit event: the failure is reported, not raised.
        assert write_audit_event(
            plan_dir, "VP-001", "probe", _payload(0)
        ) is False

        # 3) The sub-agent attempt log: same contract.
        VerificationSubAgent._write_log(
            attempts_dir / "vp_attempt_VP-001.log",
            "attempt_started",
            _payload(0),
        )
    finally:
        os.chmod(logs_dir, 0o700)
        for existing in logs_dir.glob("verification_*.log"):
            os.chmod(existing, 0o644)


# ---------------------------------------------------------------------------
# The fourth writer: the orchestrator's group-level round log
# ---------------------------------------------------------------------------

def test_emit_group_log_rotates_round_log(tmp_path, group_bound):
    """The orchestrator's group log is bounded like the other writers.

    ``_emit_group_log`` writes into the *same* round log the
    persistence manager writes into.  While it wrote with a bare
    ``open(path, "a")`` the file's ceiling was the other writer's
    business: the round log grew past the bound and no rotated copy
    appeared next to it — a file that only grows, half-bounded.
    """
    manager = VerificationPersistenceManager(tmp_path / "plan")
    log_path = manager.start_round(1)
    target = _group_log_target(manager)

    for index in range(GROUP_LINES):
        _emit_group_entry(target, event_name="group_started", index=index)

    assert log_path.exists(), "the group log must still be written"

    rotated = _rotated_copy(log_path)
    assert rotated.exists(), (
        f"{rotated} must exist once the round log passed the bound"
    )
    assert log_path.stat().st_size <= group_bound


def test_emit_group_log_keeps_writing_after_rotation(tmp_path, group_bound):
    """A rotation is a rotation, not a stop: the group log keeps taking lines.

    The bound is a ceiling, not a switch.  A writer that stopped at
    the bound would lose every group event of the second half of the
    round, and the group trace is what tells an operator which group
    was still running when the round ended.
    """
    manager = VerificationPersistenceManager(tmp_path / "plan")
    log_path = manager.start_round(1)
    target = _group_log_target(manager)

    for index in range(GROUP_LINES):
        _emit_group_entry(target, event_name="group_started", index=index)

    assert _rotated_copy(log_path).exists(), (
        "the round log must have rotated before the post-rotation write"
    )

    _emit_group_entry(target, event_name="group_completed", index=GROUP_LINES)

    assert log_path.exists()
    assert log_path.stat().st_size <= group_bound
    assert "group_completed" in log_path.read_text(encoding="utf-8"), (
        "the entry written after the rotation must land in the live log"
    )


def test_emit_group_log_and_persistence_share_one_bounded_log(
    tmp_path, group_bound
):
    """The two writers of the round log write one file, and it stays bounded.

    The group trace and the per-VP trace share a file on purpose — an
    operator reads one log per round.  They must also share the
    *bound*: a file with two writers is as bounded as the least
    bounded of them, and the group writer is the one that used to
    append without a ceiling.
    """
    manager = VerificationPersistenceManager(tmp_path / "plan")
    log_path = manager.start_round(1)
    target = _group_log_target(manager)

    manager.write_verification_point_log(
        "VP-001", "pytest_output", _payload(0)
    )
    _emit_group_entry(target, event_name="group_started", index=0)

    live = log_path.read_text(encoding="utf-8")
    assert "pytest_output" in live, (
        "the persistence manager's entry must still be in the live log"
    )
    assert "group_started" in live, (
        "the group entry must land in the same live log"
    )
    assert log_path.stat().st_size <= group_bound

    # One log file, not one per writer.
    assert sorted(
        path.name for path in log_path.parent.glob("verification_*.log")
    ) == [log_path.name]


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the write bit, so the directory stays writable",
)
def test_emit_group_log_still_raises_on_unwritable_log(tmp_path):
    """A log the group writer cannot write still raises.

    This is the boundary contract the method documents: the caller
    catches the failure locally so the VP loop is not affected, and
    the method deliberately does not swallow it.  Moving the write
    onto the bounded writer must not quietly turn that into a
    swallowed error — the difference between "the caller knows the
    log is broken" and "the log silently stops".
    """
    manager = VerificationPersistenceManager(tmp_path / "plan")
    log_path = manager.start_round(1)
    logs_dir = log_path.parent
    target = _group_log_target(manager)

    os.chmod(log_path, 0o444)
    os.chmod(logs_dir, 0o500)
    try:
        with pytest.raises(OSError):
            _emit_group_entry(target, event_name="group_started", index=0)
    finally:
        os.chmod(logs_dir, 0o700)
        os.chmod(log_path, 0o644)


def test_verification_agent_has_no_bare_append_writer():
    """No bare append-mode ``open()`` is left in the verification agent.

    The bound is a property of the *writer*, so a second writer
    without one undoes it — which is exactly how the round log ended
    up half-bounded.  The check is on the parsed source rather than on
    the text, so reformatting the module neither passes nor fails it.

    Only the append modes are forbidden: ``"w"`` writes a file from
    scratch (the verification plan, the report) and does not grow.
    """
    source = Path(verification_agent.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    append_modes = {"a", "ab", "a+"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "open"):
            continue
        mode = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for keyword in node.keywords:
            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                mode = keyword.value.value
        if isinstance(mode, str) and mode in append_modes:
            offenders.append(node.lineno)

    assert not offenders, (
        "verification_agent.py must not open a plan log in append mode "
        f"(lines {offenders}) — the bounded writer is the only writer"
    )
