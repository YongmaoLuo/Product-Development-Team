"""Tests for the throttled-streaming ``_run_foreground`` in :mod:`executor`.

2026-09-08 plan — proves that the new executor:
  * emits a heartbeat INFO line every ``flush_interval`` seconds, even
    when the subprocess produces zero output (the "hung process"
    visibility requirement)
  * emits error lines (FAILED / ERROR / Traceback / Exception /
    AssertionError) IMMEDIATELY at WARNING level — never buffered
  * caps the per-line truncation and the buffer size so a runaway
    process cannot OOM the executor
  * returns the FULL output to the caller regardless of throttling
    (no data is dropped — only the log forwarding is throttled)
  * honours ``timeout`` and surfaces a clean ``-1`` return code
  * honours the final flush on clean exit

Each test runs a real subprocess (pytest is overkill — a ``bash -c``
loop or ``python -c`` script is plenty) so we exercise the actual
``subprocess.Popen`` / ``readline`` integration. The tests are
deterministic because we control the subprocess' output cadence
explicitly.
"""

import logging
import re
import time

import pytest

from executor import Executor


@pytest.fixture
def executor(tmp_path):
    return Executor(project_dir=str(tmp_path))


def _collect_log_records(caplog, level=logging.INFO):
    """Return records at >= ``level`` from the captured log."""
    return [r for r in caplog.records if r.levelno >= level]


def _hb_records(caplog):
    """Heartbeat records emitted by ``_run_foreground``."""
    return [r for r in caplog.records if "[EXEC HB" in r.getMessage()]


def _err_records(caplog):
    """Immediate-error records emitted by ``_run_foreground``."""
    return [r for r in caplog.records if "[EXEC ERR" in r.getMessage()]


# --- Heartbeat cadence ----------------------------------------------------


def test_heartbeat_emitted_even_when_subprocess_silent(executor, caplog):
    """A subprocess that produces zero output for >flush_interval must
    still emit a heartbeat — that's the "hung process" signal the
    operator needs to distinguish from "process gone".
    """
    caplog.set_level(logging.INFO, logger="executor")
    # Sleep longer than flush_interval so we expect at least 1 HB tick.
    # flush_interval=0.5s, sleep=1.5s → expect ≥ 1 heartbeat.
    exit_code, output = executor._run_foreground(
        "sleep 1.5",
        flush_interval=0.5,
    )
    assert exit_code == 0
    assert output == ""  # nothing produced
    hbs = _hb_records(caplog)
    assert len(hbs) >= 1, f"expected ≥1 heartbeat, got {len(hbs)}"
    # Heartbeat must include the empty-tail marker so an operator can
    # distinguish "no output" from "buffered but not flushed yet".
    assert "tail=''" in hbs[0].getMessage()


def test_heartbeat_tail_shows_last_three_lines(executor, caplog):
    """Heartbeat ``tail`` should summarise the last 3 buffered lines."""
    caplog.set_level(logging.INFO, logger="executor")
    cmd = (
        "for i in 1 2 3 4 5; do "
        "echo \"line $i\"; "
        "sleep 0.4; "  # so the HB ticks mid-run
        "done"
    )
    exit_code, output = executor._run_foreground(
        cmd,
        flush_interval=0.5,
    )
    assert exit_code == 0
    hbs = _hb_records(caplog)
    assert len(hbs) >= 1
    # At least one heartbeat should show "line" content in tail.
    any_with_tail = [h for h in hbs if "tail='line" in h.getMessage()]
    assert any_with_tail, f"no HB with tail content; got: {[h.getMessage() for h in hbs]}"


# --- Error line immediate emission ---------------------------------------


def test_failed_line_emitted_immediately(executor, caplog):
    """A line containing FAILED must emit a WARNING immediately, with
    no flush-interval delay.
    """
    caplog.set_level(logging.INFO, logger="executor")
    # Emit a "FAILED" line after 2s of silence. With flush_interval=5s
    # the line MUST land in the log before the next HB tick (i.e.
    # within ~50ms of being printed).
    start = time.time()
    cmd = "sleep 0.5; echo 'tests/test_x.py::test_y FAILED'; sleep 0.3"
    exit_code, output = executor._run_foreground(
        cmd,
        flush_interval=5.0,
    )
    elapsed = time.time() - start
    assert exit_code == 0
    errs = _err_records(caplog)
    assert len(errs) == 1, f"expected 1 ERROR record, got {len(errs)}: {errs}"
    assert "FAILED" in errs[0].getMessage()
    # The subprocess slept 0.8s total. Error was emitted at ~0.5s.
    # We assert it landed before the next heartbeat would have fired.
    assert elapsed < 1.5, f"error took too long: {elapsed:.2f}s"


def test_exception_keyword_triggers_immediate_emit(executor, caplog):
    """``Exception`` and ``Traceback`` must also bypass the buffer."""
    caplog.set_level(logging.INFO, logger="executor")
    cmd = (
        "sleep 0.3; "
        "echo 'Traceback (most recent call last):'; "
        "sleep 0.3; "
        "echo 'Exception: boom';"
    )
    exit_code, _ = executor._run_foreground(cmd, flush_interval=5.0)
    assert exit_code == 0
    errs = _err_records(caplog)
    # Two error patterns → two ERROR records
    assert len(errs) == 2, f"expected 2 ERROR records, got {len(errs)}"


# --- Buffer cap + line truncation ----------------------------------------


def test_buffer_caps_at_max_lines(executor, caplog):
    """A flood of non-error lines must not grow the buffer unboundedly."""
    caplog.set_level(logging.INFO, logger="executor")
    # Emit 200 lines; buffer cap default is 100 → after flush, buffer
    # should be at most 100 lines.
    cmd = "for i in $(seq 1 200); do echo \"flood line $i\"; done"
    exit_code, _ = executor._run_foreground(
        cmd,
        flush_interval=0.05,  # tick every 50ms so HB fires mid-flood
        buffer_max_lines=100,
    )
    assert exit_code == 0
    hbs = _hb_records(caplog)
    # Every HB must report buffered<=buffer_max_lines
    for h in hbs:
        m = re.search(r"buffered=(\d+)", h.getMessage())
        assert m, f"missing buffered=N in HB: {h.getMessage()}"
        assert int(m.group(1)) <= 100, (
            f"buffered={m.group(1)} > 100 in HB: {h.getMessage()}"
        )


def test_line_truncation_limits_buffered_string_size(executor, caplog):
    """Each buffered line must be truncated to ``line_truncate_chars``."""
    caplog.set_level(logging.INFO, logger="executor")
    long_line = "x" * 1000
    cmd = f"echo '{long_line}'"
    exit_code, _ = executor._run_foreground(
        cmd,
        flush_interval=0.05,
        line_truncate_chars=50,
    )
    assert exit_code == 0
    # The buffered entry in the final heartbeat's tail should be ≤ 50 chars.
    final_hb = _hb_records(caplog)[-1]
    tail_match = re.search(r"tail='([^']*)'", final_hb.getMessage())
    assert tail_match
    tail = tail_match.group(1)
    # tail shows last 3 buffered entries joined by " | "; here there's
    # just one truncated entry.
    assert all(len(p) <= 50 for p in tail.split(" | ")), (
        f"line not truncated: {tail!r}"
    )


# --- Output completeness (no data loss) ----------------------------------


def test_full_output_returned_to_caller(executor, caplog):
    """The return value's ``output`` field MUST contain every line —
    throttling only affects log forwarding, never the return value.
    """
    caplog.set_level(logging.INFO, logger="executor")
    lines = [f"line-{i}" for i in range(50)]
    cmd = "; ".join(f"echo '{l}'" for l in lines)
    exit_code, output = executor._run_foreground(cmd, flush_interval=5.0)
    assert exit_code == 0
    for line in lines:
        assert line in output, f"missing line in output: {line}"


# --- Timeout path --------------------------------------------------------


def test_timeout_terminates_and_returns_minus_one(executor, caplog):
    """When ``timeout`` is hit, return ``(-1, 'Command timed out...')``."""
    caplog.set_level(logging.INFO, logger="executor")
    exit_code, output = executor._run_foreground(
        "sleep 5",
        timeout=0.3,
        flush_interval=0.1,
    )
    assert exit_code == -1
    assert "timed out" in output.lower()


# --- Final summary line --------------------------------------------------


def test_done_summary_emitted_on_clean_exit(executor, caplog):
    """Clean exit must emit one ``[EXEC DONE]`` summary line."""
    caplog.set_level(logging.INFO, logger="executor")
    exit_code, _ = executor._run_foreground("echo hello", flush_interval=5.0)
    assert exit_code == 0
    done_records = [r for r in caplog.records if "[EXEC DONE" in r.getMessage()]
    assert len(done_records) == 1
    msg = done_records[0].getMessage()
    assert "lines=1" in msg
    assert "errors=0" in msg
    assert "exit=0" in msg


def test_done_summary_counts_errors(executor, caplog):
    """``[EXEC DONE]`` must report the cumulative error count."""
    caplog.set_level(logging.INFO, logger="executor")
    exit_code, _ = executor._run_foreground(
        "echo 'one FAILED line'; echo 'two ERROR line'",
        flush_interval=5.0,
    )
    assert exit_code == 0
    done = [r for r in caplog.records if "[EXEC DONE" in r.getMessage()][0]
    msg = done.getMessage()
    # 2 lines, 2 error patterns matched
    assert "lines=2" in msg
    assert "errors=2" in msg


# --- Idle-timeout (hung subprocess) --------------------------------------


def test_idle_timeout_kills_hung_subprocess(executor, caplog):
    """A subprocess that emits nothing for >max_idle_seconds must be
    killed with return code ``-2`` and a clear 'hung' message — NOT
    left to run until the wall-clock ``timeout`` fires.
    """
    caplog.set_level(logging.INFO, logger="executor")
    # ``sleep 5`` produces zero stdout; max_idle=0.5s should kill it
    # in well under 1s, well before wall timeout=10s.
    start = time.time()
    exit_code, output = executor._run_foreground(
        "sleep 5",
        timeout=10.0,
        flush_interval=60.0,
        max_idle_seconds=0.5,
    )
    elapsed = time.time() - start
    assert exit_code == -2, f"expected -2 (hung), got {exit_code}"
    assert "Subprocess hung" in output
    assert "no stdout for" in output
    # Hung-kill must be fast, not waiting for the wall timeout.
    assert elapsed < 3.0, f"hung-kill took {elapsed:.2f}s, expected <3s"
    # The reason=idle-timeout HB should have been logged before the kill.
    idle_hbs = [
        r for r in caplog.records
        if "[EXEC HB" in r.getMessage() and "idle-timeout" in r.getMessage()
    ]
    assert len(idle_hbs) == 1, f"expected 1 idle-timeout HB, got {len(idle_hbs)}"


def test_active_subprocess_not_killed_by_idle_timeout(executor, caplog):
    """A subprocess that emits a line every 0.1s MUST NOT be killed by
    a 1s idle timeout — the idle clock resets on every line.
    """
    caplog.set_level(logging.INFO, logger="executor")
    # Emit a line every 0.1s for 0.6s total. idle=1s should never fire
    # because we never go 1s without a new line.
    cmd = "for i in 1 2 3 4 5 6; do echo \"tick $i\"; sleep 0.1; done"
    exit_code, output = executor._run_foreground(
        cmd,
        timeout=10.0,
        flush_interval=60.0,
        max_idle_seconds=1.0,
    )
    assert exit_code == 0, f"expected clean exit, got {exit_code}"
    # All 6 ticks should be in the output.
    for i in range(1, 7):
        assert f"tick {i}" in output, f"missing tick {i} in output"


def test_idle_timeout_zero_disables_check(executor):
    """``max_idle_seconds=None`` (or 0) must disable the idle check —
    the wall-clock ``timeout`` is the only bound.
    """
    # sleep 2 with no idle check, no wall timeout → runs to completion
    exit_code, output = executor._run_foreground(
        "sleep 2",
        timeout=None,
        flush_interval=60.0,
        max_idle_seconds=None,
    )
    assert exit_code == 0


# --- Pre-error flush preserves context -----------------------------------


def test_error_flushes_buffer_first(executor, caplog):
    """Before logging an error, the executor must flush the recent
    buffered non-error lines so the operator has context for WHY the
    error happened.
    """
    caplog.set_level(logging.INFO, logger="executor")
    cmd = (
        "echo 'collecting items'; "
        "sleep 0.2; "
        "echo 'more collecting'; "
        "sleep 0.2; "
        "echo 'items collected'; "
        "sleep 0.2; "
        "echo 'oh no FAILED line';"
    )
    exit_code, _ = executor._run_foreground(cmd, flush_interval=5.0)
    assert exit_code == 0
    # Order matters: the pre-error HB (reason=pre-error) must come
    # BEFORE the [EXEC ERR] record in the log.
    records = list(caplog.records)
    pre_err_idx = next(
        (i for i, r in enumerate(records)
         if "[EXEC HB" in r.getMessage() and "pre-error" in r.getMessage()),
        None,
    )
    err_idx = next(
        (i for i, r in enumerate(records) if "[EXEC ERR" in r.getMessage()),
        None,
    )
    assert pre_err_idx is not None, "no pre-error flush recorded"
    assert err_idx is not None, "no ERROR record"
    assert pre_err_idx < err_idx, (
        f"pre-error HB (idx={pre_err_idx}) must precede ERR (idx={err_idx})"
    )
    # The pre-error HB's tail should include the buffered context lines.
    pre_err_hb = records[pre_err_idx].getMessage()
    assert "collecting" in pre_err_hb or "items collected" in pre_err_hb, (
        f"pre-error HB missing context: {pre_err_hb}"
    )
