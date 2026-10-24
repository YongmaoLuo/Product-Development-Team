"""Unit tests for the diagnostic log excerpt in :mod:`service_manager`.

Defect 3 (2026-10-23): a service that never became ready reported only
the last fifteen lines of its log. On the real incident that hid the
cause outright — the interpreter's shutdown noise filled the tail while
the failure itself sat nine lines in — and the resulting ``detail``
string was the only evidence nine ``service_freshness_blocked``
verdicts carried.

The fix: the excerpt leads with the first error line (and the five
lines before it), keeps the tail after it, and caps the whole thing.
"""

from __future__ import annotations

import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402


#: The eight lines that precede the error in the fixture log.
_BOOT_LINES = [
    "INFO  starting service chart",
    "INFO  loaded config from plan.yaml",
    "INFO  kqueue watcher armed",
    "INFO  bound 127.0.0.1:45991",
    "INFO  worker pool size 4",
    "INFO  health probe registered",
    "INFO  tick 1",
    "INFO  tick 2",
]

#: The real failure — line 9 of the fixture log.
_ERROR_LINE = "OSError: [Errno 9] Bad file descriptor"

#: What the interpreter printed while it unwound: the tail.
_NOISE_LINES = [f"GeneratorExit {n}" for n in range(15)]

#: The pid the fake child reports.
_PID = 424242

#: The fixed prefix every "not ready" detail starts with.
_PREFIX = f"started (pid {_PID}, pgid {_PID}) but not ready after 180s; rc=None; "


def _fixture_log() -> str:
    """A log shaped like the real one: the error at line 9, noise after."""
    return "\n".join(_BOOT_LINES + [_ERROR_LINE] + _NOISE_LINES) + "\n"


def _not_ready_detail(tmp_path: Path, monkeypatch, log_text: str) -> str:
    """Run ``start_service`` against a service that never comes up.

    The child is faked (the pattern the rest of this file's suite
    uses) so the test stays a unit test: no process is spawned and no
    port is probed.
    """
    log = tmp_path / "service.log"
    log.write_text(log_text)

    decl = ServiceDeclaration(
        name="chart",
        port=45991,
        start_cmd="true",
        cwd=".",
        ready_timeout_seconds=180,
        log_path="service.log",
    )

    class _FakeProc:
        pid = _PID

        def poll(self):
            return None   # still running; the not-ready path is mocked

    monkeypatch.setattr(sm.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: False)
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)

    runtime = sm.start_service(decl, tmp_path)
    assert runtime.ready is False
    return runtime.detail


# ---------------------------------------------------------------------------
# first error, then tail
# ---------------------------------------------------------------------------


def test_detail_leads_with_first_error_then_tail(tmp_path, monkeypatch):
    """The cause comes first; the noise tail follows it."""
    detail = _not_ready_detail(tmp_path, monkeypatch, _fixture_log())

    assert _ERROR_LINE in detail
    assert "GeneratorExit" in detail
    # The first error is *before* the tail in the text, so a reader
    # meets the cause before the noise.
    assert detail.index(_ERROR_LINE) < detail.index("GeneratorExit")
    assert "first error at line 9" in detail
    assert "log tail:" in detail


def test_first_error_includes_five_context_lines(tmp_path, monkeypatch):
    """Five lines of context precede the error — and only five."""
    detail = _not_ready_detail(tmp_path, monkeypatch, _fixture_log())

    for line in _BOOT_LINES[-5:]:
        assert line in detail
    assert _BOOT_LINES[-6] not in detail


def test_first_error_excerpt_reports_line_number_and_context():
    """The helper is the piece that produces "first error at line N"."""
    lines = ["a", "b", "c", "d", "e", "f", "OSError: boom"]

    line_number, excerpt = sm._first_error_excerpt(lines)

    assert line_number == 7
    assert excerpt.endswith("OSError: boom")
    assert "b" in excerpt          # five lines of context
    assert "a" not in excerpt      # ...and no more than five


def test_first_error_excerpt_returns_none_without_a_marker():
    assert sm._first_error_excerpt(["INFO  fine", "0 errors"]) is None


# ---------------------------------------------------------------------------
# boundaries
# ---------------------------------------------------------------------------


def test_no_error_marker_keeps_tail_only(tmp_path, monkeypatch):
    """No error marker → the old wording, byte for byte."""
    lines = ["INFO  booting"] * 5 + [f"INFO  tick {n}" for n in range(20)]

    detail = _not_ready_detail(tmp_path, monkeypatch, "\n".join(lines) + "\n")

    assert "first error" not in detail
    assert detail == (
        f"{_PREFIX}log tail: {' | '.join(lines[-15:])}"
    )


def test_summary_line_is_not_an_error_marker(tmp_path, monkeypatch):
    """``0 errors`` is a summary, not a failure — matching is
    case-sensitive on purpose."""
    lines = ["INFO  summary: 0 errors, 3 warnings"] + [
        f"INFO  tick {n}" for n in range(20)
    ]

    detail = _not_ready_detail(tmp_path, monkeypatch, "\n".join(lines) + "\n")

    assert "first error" not in detail


def test_empty_log_reports_empty(tmp_path, monkeypatch):
    detail = _not_ready_detail(tmp_path, monkeypatch, "")

    assert detail.endswith("(empty)")


def test_excerpt_capped_at_1200(tmp_path, monkeypatch):
    """A huge log must not blow the detail up, and the cut says so."""
    lines = ["x" * 400] * 40
    lines.insert(3, _ERROR_LINE)

    detail = _not_ready_detail(tmp_path, monkeypatch, "\n".join(lines) + "\n")

    assert len(detail) <= 1200 + len(_PREFIX)
    assert "…[truncated" in detail


def test_first_error_inside_the_tail_window_is_not_repeated(tmp_path, monkeypatch):
    """When the tail already carries the error, say it once."""
    lines = (
        [f"INFO  tick {n}" for n in range(20)]
        + [_ERROR_LINE]
        + [f"INFO  late {n}" for n in range(14)]
    )

    detail = _not_ready_detail(tmp_path, monkeypatch, "\n".join(lines) + "\n")

    assert "first error at line" not in detail
    assert detail.count(_ERROR_LINE) == 1


def test_huge_log_scans_only_the_first_20000_lines(tmp_path, monkeypatch):
    """Past 20000 lines the scan stops, and the tail takes over.

    The scan runs *after* a readiness timeout; reading a service log
    that grew to hundreds of thousands of lines must not turn a slow
    failure into a slower one.
    """
    lines = [f"INFO  tick {n}" for n in range(20000)]
    lines.append(_ERROR_LINE)
    lines.extend(f"INFO  late {n}" for n in range(20))

    detail = _not_ready_detail(tmp_path, monkeypatch, "\n".join(lines) + "\n")

    # Past the scan window the error is not reported at all — and it
    # is not in the tail either, so this cannot pass by accident.
    assert "first error at line" not in detail
    assert _ERROR_LINE not in detail


def test_ready_service_detail_is_unchanged(tmp_path, monkeypatch):
    """A service that came up says exactly what it said before."""
    log = tmp_path / "service.log"
    log.write_text(_fixture_log())
    decl = ServiceDeclaration(
        name="chart",
        port=45991,
        start_cmd="true",
        cwd=".",
        ready_timeout_seconds=180,
        log_path="service.log",
    )

    class _FakeProc:
        pid = _PID

        def poll(self):
            return None

    monkeypatch.setattr(sm.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: True)
    monkeypatch.setattr(sm, "_await_exit", lambda proc, timeout: (False, None))
    monkeypatch.setattr(sm, "_process_group_of", lambda pid: pid)
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: 1.0)

    runtime = sm.start_service(decl, tmp_path)

    assert runtime.ready is True
    assert runtime.detail == f"started (pid {_PID}); ready within 180s"
